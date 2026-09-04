import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pager_mcp import __build__, __version__
from pager_mcp.client import fix_session_file_ownership, get_current_session_key
from pager_mcp.server import build_app
from pager_mcp.storage import MailboxStore


def test_mailbox_rejects_when_full():
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 1)
    token = "00000000-0000-0000-0000-000000000000"
    assert store.enqueue(token, {"message": "one"})
    assert store.enqueue(token, {"message": "two"}) is None
    assert len(store.pending(token)) == 1


def test_pop_removes_oldest():
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 128)
    token = "00000000-0000-0000-0000-000000000001"
    store.enqueue(token, {"message": "one"})
    assert store.pop(token).payload["message"] == "one"
    assert store.pop(token) is None


@pytest.mark.asyncio
async def test_version_endpoint():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/version")
        assert resp.status == 200
        data = await resp.json()
        assert data["version"] == __version__
        assert data["version"] == "0.2.1"
        assert data["build"] == __build__
    finally:
        await client.close()


def test_session_key_and_ownership(tmp_path, monkeypatch):
    path = tmp_path / ".pager_session"
    token = "00000000-0000-0000-0000-000000000002"
    path.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PAGER_UID", str(os.getuid()))
    monkeypatch.setenv("PAGER_GID", str(os.getgid()))
    assert get_current_session_key() == token
    assert path.stat().st_mode & 0o777 == 0o600
    assert (path.stat().st_uid, path.stat().st_gid) == (os.getuid(), os.getgid())


def test_pager_plugin_syntax():
    plugin_path = Path("plugins/pager.js")
    assert plugin_path.exists()
    content = plugin_path.read_text(encoding="utf-8")
    assert "export const PagerPlugin" in content
    assert 'const DEFAULT_PAGER_URL = "https://pager.ofloo.io"' in content
    assert "PAGER_URL" in content
    assert "Pager Plugin loaded for" in content
    assert '"experimental.chat.system.transform"' in content
    assert "Do not manually call wait_for_event" in content
    assert "/mailboxes/" in content
    assert "/wait" in content
