from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def server_url() -> str:
    if os.getenv("SERVER_URL"):
        return os.environ["SERVER_URL"].rstrip("/")
    return f"http://{os.getenv('SERVER_HOST', 'localhost')}:{os.getenv('SERVER_PORT', '8080')}"


def session_file() -> Path:
    return Path(os.getenv("PROJECT_DIR", os.getcwd())) / ".pager_session"


def get_token() -> str:
    path = session_file()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        try:
            uuid.UUID(token)
            return token
        except ValueError:
            pass
    request = urllib.request.Request(server_url() + "/mailboxes", method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        token = json.load(response)["uuid"]
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server_url() + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=None) as response:
        return json.load(response)


def main() -> None:
    token = get_token()
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-03-26"), "capabilities": {"tools": {}}, "serverInfo": {"name": "pagermcp", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "wait_for_event", "description": "Wait for the next page in the session mailbox without polling.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
        elif method == "tools/call" and message.get("params", {}).get("name") == "wait_for_event":
            result = {"content": [{"type": "text", "text": json.dumps(request("/mailboxes/" + urllib.parse.quote(token, safe="") + "/wait"))}], "isError": False}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
