import asyncio
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


def test_remove_by_id_keeps_others():
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 128)
    token = "00000000-0000-0000-0000-000000000003"
    first = store.enqueue(token, {"message": "one"})
    second = store.enqueue(token, {"message": "two"})
    store.remove(first.id)
    remaining = store.pending(token)
    assert [page.id for page in remaining] == [second.id]
    assert store.pop(token).id == second.id
    assert store.pop(token) is None


def test_remove_by_id_sqlite(tmp_path):
    store = MailboxStore(str(tmp_path), 30 * 86400, 7 * 86400, 128)
    token = "00000000-0000-0000-0000-000000000004"
    first = store.enqueue(token, {"message": "one"})
    second = store.enqueue(token, {"message": "two"})
    store.remove(first.id)
    remaining = store.pending(token)
    assert [page.id for page in remaining] == [second.id]


async def _wait_payload(client, token):
    resp = await client.get(f"/mailboxes/{token}/wait")
    assert resp.status == 200
    return await resp.json()


@pytest.mark.asyncio
async def test_wait_does_not_redeliver_consumed_page(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "10")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        waiter = asyncio.create_task(_wait_payload(client, token))
        await asyncio.sleep(0.05)
        accepted = await client.post("/notify", json={"uuid": token, "message": "hello"})
        assert accepted.status == 202
        page_id = (await accepted.json())["id"]
        delivered = await waiter
        assert delivered["id"] == page_id
        assert delivered["message"] == "hello"
        monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "0.05")
        second = await client.get(f"/mailboxes/{token}/wait")
        assert second.status == 408
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_delivers_backlog_when_no_waiter(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "5")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        accepted = await client.post("/notify", json={"uuid": token, "message": "queued"})
        page_id = (await accepted.json())["id"]
        delivered = await _wait_payload(client, token)
        assert delivered["id"] == page_id
        assert delivered["message"] == "queued"
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_notify_with_multiple_waiters_delivers_once_per_waiter(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "10")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        waiters = [asyncio.create_task(_wait_payload(client, token)) for _ in range(2)]
        await asyncio.sleep(0.05)
        accepted = await client.post("/notify", json={"uuid": token, "message": "fanout"})
        page_id = (await accepted.json())["id"]
        delivered = await asyncio.gather(*waiters)
        assert [page["id"] for page in delivered] == [page_id, page_id]
        monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "0.05")
        extra = await client.get(f"/mailboxes/{token}/wait")
        assert extra.status == 408
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


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
        assert data["version"] == "0.3.2"
        assert data["build"] == __build__
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_root_serves_readme_as_html():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        body = await resp.text()
        assert "<h1>PagerMCP</h1>" in body
        assert "When to use PagerMCP" in body
        assert "<style>" in body
        assert "<table>" in body
        assert "max-age=3600" in resp.headers["Cache-Control"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rfc_endpoint_serves_rfc_as_plain_text():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/rfc")
        assert resp.status == 200
        assert "text/plain" in resp.headers["Content-Type"]
        assert "RFC-0001: Pager Protocol" in await resp.text()
        assert "max-age=3600" in resp.headers["Cache-Control"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sample_endpoint_serves_pager_sh():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/sample/pager.sh")
        assert resp.status == 200
        assert "application/x-sh" in resp.headers["Content-Type"]
        body = await resp.text()
        assert "PAGER_URL" in body
        assert "max-age=3600" in resp.headers["Cache-Control"]
        listing = await client.get("/sample/")
        assert listing.status == 404
        missing = await client.get("/sample/missing.sh")
        assert missing.status == 404
    finally:
        await client.close()


def test_pager_script_is_generic():
    content = (Path(__file__).parent.parent / "sample" / "pager.sh").read_text(encoding="utf-8")
    assert "10.13.17.60" not in content
    assert "truenas-fbsd15" not in content
    assert "1ce7c04b" not in content
    assert "${PAGER_URL:-https://pager.ofloo.io}" in content
    assert "--dry-run" in content
    assert "--tail" in content
    assert "--session" in content
    assert '"exit_code":$exit_code' in content


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
