from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import __build__, __version__


def server_url() -> str:
    if os.getenv("SERVER_URL"):
        return os.environ["SERVER_URL"].rstrip("/")
    return f"http://{os.getenv('SERVER_HOST', 'localhost')}:{os.getenv('SERVER_PORT', '8080')}"


def session_file() -> Path:
    return Path(os.getenv("PROJECT_DIR", os.getcwd())) / ".pager_session"


def session_owner() -> tuple[int, int]:
    uid = int(os.getenv("PAGER_UID", os.getuid()))
    gid = int(os.getenv("PAGER_GID", os.getgid()))
    return uid, gid


def fix_session_file_ownership(path: Path) -> None:
    uid, gid = session_owner()
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        if (path.stat().st_uid, path.stat().st_gid) != (uid, gid):
            raise


def get_token() -> str:
    path = session_file()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        try:
            uuid.UUID(token)
            path.chmod(0o600)
            fix_session_file_ownership(path)
            return token
        except ValueError:
            pass
    request = urllib.request.Request(server_url() + "/mailboxes", method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        token = json.load(response)["uuid"]
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    fix_session_file_ownership(path)
    return token


def get_current_session_key() -> str:
    return get_token()


def request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server_url() + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=None) as response:
        return json.load(response)


def wait_for_page(token: str) -> dict:
    """Block in bounded slices, then acknowledge so the page is removed.

    Delivery is at-least-once: /wait hands out a page without removing it, and
    the page leaves the queue only after /ack. If this client (or the network)
    dies before the ack, the page stays queued and the next wait returns it,
    instead of vanishing into a connection nobody can read. The wait is sliced
    so no single request outlives a reverse proxy's idle timeout.
    """
    slice_seconds = int(os.getenv("PAGER_MCP_WAIT_SLICE_SECONDS", "55"))
    deadline = time.monotonic() + float(os.getenv("PAGER_MCP_WAIT_SECONDS", "1800"))
    url = "/mailboxes/" + urllib.parse.quote(token, safe="")
    query = f"?timeout_seconds={slice_seconds}" if slice_seconds > 0 else ""
    while True:
        try:
            page = request(url + "/wait" + query)
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 408:
                raise
            if time.monotonic() >= deadline:
                return {"timeout": True, "message": "no page within the wait window; call wait_for_event again"}
    if isinstance(page, dict) and page.get("id"):
        request(url + "/ack", method="POST", body={"id": page["id"]})
    return page


def check_version() -> None:
    try:
        remote = request("/version")
    except (OSError, urllib.error.URLError):
        return
    if str(remote.get("version")) != __version__ or str(remote.get("build")) != __build__:
        print(f"PagerMCP version mismatch: client {__version__} build {__build__}, server {remote.get('version')} build {remote.get('build')}", file=sys.stderr)


def main() -> None:
    check_version()
    token = get_token()
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}}, "serverInfo": {"name": "pagermcp", "version": f"{__version__}+{__build__}"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "wait_for_event", "description": "Wait for the next page in the session mailbox without polling.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
        elif method == "tools/call" and message.get("params", {}).get("name") == "wait_for_event":
            result = {"content": [{"type": "text", "text": json.dumps(wait_for_page(token))}], "isError": False}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
