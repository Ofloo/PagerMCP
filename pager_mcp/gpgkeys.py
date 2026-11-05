"""PGP key management for the P2P encrypted-destination-UUID feature.

The server owns a keypair. Its public key is advertised via /settings (p2p), so
any peer can encrypt the destination mailbox UUID to it; relay peers only see
ciphertext. The server decrypts with its private key to route the message.

Keys live in DATA_DIR/gnupg (or a temporary directory in in-memory mode), are
generated automatically on first start, are valid for one year, and are renewed
automatically when they come within RENEW_BEFORE_SECONDS of expiry. The old key
stays in the keyring during the overlap, so in-flight messages still decrypt.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

RENEW_BEFORE_SECONDS = 30 * 86400  # renew 30 days before expiry
KEY_VALIDITY = "1y"


def _gpg(home: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--yes", *args],
        input=input_bytes,
        capture_output=True,
        timeout=30,
    )


class GpgKeyring:
    def __init__(self, data_dir: str | None) -> None:
        if data_dir:
            self.home = Path(data_dir) / "gnupg"
            self.home.mkdir(parents=True, exist_ok=True)
            self.home.chmod(0o700)
        else:
            self.home = Path(tempfile.mkdtemp(prefix="pagermcp-gnupg-"))
            self.home.chmod(0o700)

    @staticmethod
    def available() -> bool:
        return shutil.which("gpg") is not None

    def _secret_fingerprints(self) -> list[str]:
        result = _gpg(self.home, "--list-secret-keys", "--with-colons")
        return [line.split(":")[9] for line in result.stdout.decode().splitlines() if line.startswith("fpr:")]

    def _newest_public_fingerprint(self) -> str | None:
        result = _gpg(self.home, "--list-keys", "--with-colons")
        fingerprints = [line.split(":")[9] for line in result.stdout.decode().splitlines() if line.startswith("fpr:")]
        return fingerprints[-1] if fingerprints else None

    def _expiry_of_newest_public(self) -> int:
        result = _gpg(self.home, "--list-keys", "--with-colons")
        expires = [int(line.split(":")[6]) for line in result.stdout.decode().splitlines() if line.startswith("pub:") and line.split(":")[6].isdigit()]
        return max(expires) if expires else 0

    def _generate(self) -> None:
        spec = (
            "%no-protection\n"
            "Key-Type: RSA\n"
            "Key-Length: 4096\n"
            "Name-Real: PagerMCP\n"
            f"Name-Email: pagermcp@{os.uname().nodename}\n"
            f"Expire-Date: {KEY_VALIDITY}\n"
            "%commit\n"
        )
        result = _gpg(self.home, "--gen-key", input_bytes=spec.encode())
        if result.returncode != 0:
            raise RuntimeError(f"gpg key generation failed: {result.stderr.decode()[:300]}")

    def ensure(self) -> None:
        """Generate a keypair on first start and renew when expiry is near."""
        if not self._secret_fingerprints():
            self._generate()
            return
        import time

        if self._expiry_of_newest_public() - int(time.time()) < RENEW_BEFORE_SECONDS:
            self._generate()

    def key_id(self) -> str | None:
        return self._newest_public_fingerprint()

    def public_key(self) -> str | None:
        fingerprint = self._newest_public_fingerprint()
        if not fingerprint:
            return None
        result = _gpg(self.home, "--armor", "--export", fingerprint)
        return result.stdout.decode() if result.returncode == 0 else None

    def decrypt(self, armored: str) -> str | None:
        result = _gpg(self.home, "--decrypt", input_bytes=armored.encode())
        if result.returncode != 0:
            return None
        return result.stdout.decode().strip()
