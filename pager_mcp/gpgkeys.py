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


def _gpg_binary() -> str | None:
    """Resolve the gpg binary without relying on PATH alone.

    Service daemons (rc.d on FreeBSD, some container supervisors) run with a
    minimal PATH that does not include /usr/local/bin, where gnupg installs.
    shutil.which() alone would then wrongly report gpg as unavailable.
    """
    found = shutil.which("gpg")
    if found:
        return found
    for candidate in ("/usr/local/bin/gpg", "/usr/bin/gpg", "/bin/gpg", "/usr/sbin/gpg"):
        if os.path.exists(candidate):
            return candidate
    return None


_GPG_BIN = _gpg_binary()


def _gpg(home: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_GPG_BIN or "gpg", "--homedir", str(home), "--batch", "--yes", *args],
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
        return _GPG_BIN is not None

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

    def ensure(self, legacy_ttl: int = 14 * 86400) -> None:
        """Key lifecycle: create on first start, rotate on demand or near expiry.

        P2P_GPG_NEWKEY=true forces a new keypair; the previous one becomes the
        legacy key. Legacy keys are kept for 2x the message TTL after the new
        key was made (so even a message sent on the old key's last day still
        decrypts within its own TTL), then removed as obsolete.
        """
        newkey = os.getenv("P2P_GPG_NEWKEY", "").lower() in ("1", "true", "yes", "on")
        if not self._public_keypairs() or newkey:
            self._generate()
        import time

        if self._expiry_of_newest_public() - int(time.time()) < RENEW_BEFORE_SECONDS:
            self._generate()
        self._prune_obsolete(legacy_ttl)

    def _delete_keypair(self, fingerprint: str) -> None:
        _gpg(self.home, "--delete-secret-and-public-key", fingerprint)

    def _prune_obsolete(self, legacy_ttl: int) -> None:
        pairs = self._public_keypairs()
        if len(pairs) < 2:
            return
        import time

        newest_created = pairs[-1][0]
        for created, fingerprint in pairs[:-1]:
            if time.time() > newest_created + legacy_ttl:
                self._delete_keypair(fingerprint)

    def fingerprints(self) -> list[str]:
        return self._secret_fingerprints()

    def has_key(self, fingerprint: str) -> bool:
        return fingerprint in self._secret_fingerprints()

    def _public_keypairs(self) -> list[tuple[int, str]]:
        """(created, fingerprint) of every public keypair, oldest first."""
        result = _gpg(self.home, "--list-keys", "--with-colons")
        pairs: list[list] = []
        for line in result.stdout.decode().splitlines():
            if line.startswith("pub:"):
                fields = line.split(":")
                created = int(fields[5]) if fields[5].isdigit() else 0
                pairs.append([created, None])
            elif line.startswith("fpr:") and pairs:
                pairs[-1][1] = line.split(":")[9]
        return sorted([(created, fp) for created, fp in pairs if fp], key=lambda item: item[0])

    def key_id(self) -> str | None:
        return self._newest_public_fingerprint()

    def legacy_key_id(self) -> str | None:
        """The previous keypair's fingerprint (during a rotation overlap)."""
        pairs = self._public_keypairs()
        return pairs[-2][1] if len(pairs) >= 2 else None

    def legacy_public_key(self) -> str | None:
        fingerprint = self.legacy_key_id()
        if not fingerprint:
            return None
        result = _gpg(self.home, "--armor", "--export", fingerprint)
        return result.stdout.decode() if result.returncode == 0 else None

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
