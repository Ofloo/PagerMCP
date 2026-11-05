import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from aiohttp import web
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


async def _ack(client, token, page_id):
    return await client.post(f"/mailboxes/{token}/ack", json={"id": page_id})


@pytest.mark.asyncio
async def test_wait_then_ack_removes_page(monkeypatch):
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
        # at-least-once: the page is still queued until it is acknowledged
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert [m["id"] for m in pending["messages"]] == [page_id]
        assert (await _ack(client, token, page_id)).status == 200
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_without_ack_returns_same_page(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "10")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        accepted = await client.post("/notify", json={"uuid": token, "message": "retry-me"})
        page_id = (await accepted.json())["id"]
        first = await _wait_payload(client, token)
        second = await _wait_payload(client, token)
        assert first["id"] == page_id
        assert second["id"] == page_id  # redelivered because it was never acked
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disconnected_waiter_does_not_consume_page(monkeypatch):
    """A client that drops mid-wait must not swallow a page the server later delivers."""
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "10")
    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(
            f"GET /mailboxes/{token}/wait HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode()
        )
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        await asyncio.sleep(0.2)
        accepted = await client.post("/notify", json={"uuid": token, "message": "survivor"})
        page_id = (await accepted.json())["id"]
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert [m["id"] for m in pending["messages"]] == [page_id]
        delivered = await _wait_payload(client, token)
        assert delivered["id"] == page_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_notify_wakes_all_waiters_without_consuming(monkeypatch):
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
        await _ack(client, token, page_id)
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_timeout_seconds_returns_408(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.get(f"/mailboxes/{token}/wait?timeout_seconds=0.05")
        assert resp.status == 408
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_timeout_rejects_bad_value(monkeypatch):
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.get(f"/mailboxes/{token}/wait?timeout_seconds=abc")
        assert resp.status == 400
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
        assert data["version"] == "0.13.1"
        assert data["build"] == __build__
        assert "hostname" in data
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
        # fenced blocks must render as code, never leak literal fences
        assert "```" not in body
        assert 'class="language-bash"' in body
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
async def test_privacy_endpoint_serves_privacy_as_plain_text():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/privacy")
        assert resp.status == 200
        assert "text/plain" in resp.headers["Content-Type"]
        body = await resp.text()
        assert "Privacy" in body
        assert "logt standaard niets" in body
        assert "<uuid>" in body
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


def test_pager_script_reports_own_failures():
    content = (Path(__file__).parent.parent / "sample" / "pager.sh").read_text(encoding="utf-8")
    assert "trap 'on_exit' EXIT" in content
    assert "trap 'on_term' TERM" in content
    assert "trap 'on_int' INT" in content
    assert "pager.sh terminated by SIGTERM" in content
    assert "pager.sh interrupted by SIGINT" in content
    assert "unknown argument" in content
    assert "NOTIFIED=1" in content
    assert "print_help()" in content
    assert "cannot write log" in content
    assert 'mkdir -p "$(dirname "$LOG")"' in content
    assert "fallback_log=$(mktemp" in content


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_pager_script_syntax(shell):
    script = str(Path(__file__).parent.parent / "sample" / "pager.sh")
    result = subprocess.run([shell, "-n", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_pager_script_help_prints_header():
    script = str(Path(__file__).parent.parent / "sample" / "pager.sh")
    result = subprocess.run(["sh", script, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "Modes:" in result.stdout
    assert "--run" in result.stdout
    assert "--dry-run" in result.stdout


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


@pytest.mark.asyncio
async def test_wait_max_age_skips_stale_page(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "0.05")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post("/notify", json={"uuid": token, "message": "stale"})
        fresh = await client.get(f"/mailboxes/{token}/wait?max_age_seconds=60")
        data = await fresh.json()
        assert data["message"] == "stale"
        assert "age_seconds" in data
        assert data["age_seconds"] < 60
        await _ack(client, token, data["id"])
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_max_age_expired_page_is_skipped(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "0.05")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post("/notify", json={"uuid": token, "message": "old"})
        # max_age_seconds=0 marks the page stale: the waiter skips (and drops) it
        resp = await client.get(f"/mailboxes/{token}/wait?max_age_seconds=0")
        assert resp.status == 408
        pending = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert pending["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_max_age_rejects_bad_value(monkeypatch):
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "0.05")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.get(f"/mailboxes/{token}/wait?max_age_seconds=abc")
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_connection_cap_wait_and_settings(monkeypatch):
    monkeypatch.setenv("MAX_CONNECTIONS_PER_MAILBOX", "2")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        settings = await (await client.get("/settings")).json()
        assert settings["connections"]["max_per_mailbox"] == 2
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        t1 = asyncio.create_task(client.get(f"/mailboxes/{token}/wait?timeout_seconds=5"))
        t2 = asyncio.create_task(client.get(f"/mailboxes/{token}/wait?timeout_seconds=5"))
        await asyncio.sleep(0.3)
        third = await client.get(f"/mailboxes/{token}/wait?timeout_seconds=1")
        assert third.status == 429
        # cap is per mailbox: another mailbox is unaffected
        other = (await (await client.post("/mailboxes")).json())["uuid"]
        other_resp = await client.get(f"/mailboxes/{other}/wait?timeout_seconds=0.2")
        assert other_resp.status == 408
        # delivering a page releases the waiters, after which new ones are accepted
        await client.post("/notify", json={"uuid": token, "message": "x"})
        r1 = await asyncio.wait_for(t1, 2)
        r2 = await asyncio.wait_for(t2, 2)
        assert r1.status == 200 and r2.status == 200
        fourth = await client.get(f"/mailboxes/{token}/wait?timeout_seconds=0.2")
        assert fourth.status == 200  # the unacked page is delivered again
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_connection_cap_sse(monkeypatch):
    monkeypatch.setenv("MAX_CONNECTIONS_PER_MAILBOX", "1")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        stream = await client.get(f"/mailboxes/{token}/events")
        assert stream.status == 200
        second = await client.get(f"/mailboxes/{token}/events")
        assert second.status == 429
        second.close()
        stream.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_stream_pushes_page(monkeypatch):
    monkeypatch.setenv("SSE_KEEPALIVE_SECONDS", "30")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.get(f"/mailboxes/{token}/events")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        await client.post("/notify", json={"uuid": token, "message": "pushed"})
        found = False
        for _ in range(6):
            frame = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 5)
            if b"pushed" in frame:
                found = True
                break
        assert found
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_replays_backlog_on_connect(monkeypatch):
    monkeypatch.setenv("SSE_KEEPALIVE_SECONDS", "30")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post("/notify", json={"uuid": token, "message": "backlog"})
        resp = await client.get(f"/mailboxes/{token}/events")
        found = False
        for _ in range(6):
            frame = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 5)
            if b"backlog" in frame:
                found = True
                break
        assert found
        resp.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_sends_keepalive(monkeypatch):
    monkeypatch.setenv("SSE_KEEPALIVE_SECONDS", "0.1")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.get(f"/mailboxes/{token}/events")
        found = False
        for _ in range(6):
            frame = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), 5)
            if frame.startswith(b": keepalive"):
                found = True
                break
        assert found
        resp.close()
    finally:
        await client.close()


def test_subscription_lease_expires_and_renews():
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 128, subscription_ttl=30 * 86400, max_subscriptions=16)
    token = "00000000-0000-0000-0000-000000000009"
    sub = store.add_subscription(token, "unifiedpush", "https://example.test/hook", None)
    assert sub is not None
    assert sub.expires > time.time()
    assert [s.id for s in store.subscriptions_for(token)] == [sub.id]
    # past the 30-day lease it is no longer active (push off)
    assert store.subscriptions_for(token, now=time.time() + 31 * 86400) == []
    renewed = store.renew_subscription(token, sub.id)
    assert renewed is not None and renewed.renewed >= sub.renewed
    assert store.subscriptions_for(token, now=time.time() + 1) != []
    assert store.remove_subscription(token, sub.id) is True
    assert store.subscriptions_for(token) == []


def test_subscription_cap_and_sqlite(tmp_path):
    token = "00000000-0000-0000-0000-00000000000a"
    memory = MailboxStore(None, 30 * 86400, 7 * 86400, 128, subscription_ttl=30 * 86400, max_subscriptions=1)
    assert memory.add_subscription(token, "unifiedpush", "https://a.test/hook", None)
    assert memory.add_subscription(token, "unifiedpush", "https://b.test/hook", None) is None
    disk = MailboxStore(str(tmp_path), 30 * 86400, 7 * 86400, 128, subscription_ttl=30 * 86400, max_subscriptions=16)
    first = disk.add_subscription(token, "unifiedpush", "https://a.test/hook", {"p256dh": "x"})
    second = disk.add_subscription(token, "webpush", "https://b.test/hook", None)
    assert {s.id for s in disk.subscriptions_for(token)} == {first.id, second.id}
    assert disk.remove_subscriptions(token, channel="unifiedpush") == 1
    assert [s.id for s in disk.subscriptions_for(token)] == [second.id]
    assert disk.remove_subscriptions(token) == 1
    assert disk.subscriptions_for(token) == []


@pytest.mark.asyncio
async def test_notify_sends_data_only_trigger_to_subscription(monkeypatch):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "127.0.0.1")
    received = []

    async def hook(request):
        received.append(await request.json())
        return web.json_response({"ok": True})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/hook", hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        endpoint = f"http://127.0.0.1:{receiver.server.port}/hook"
        created = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
        assert created.status == 201
        subscription = await created.json()
        listed = await (await client.get(f"/mailboxes/{token}/subscriptions")).json()
        assert [s["id"] for s in listed["subscriptions"]] == [subscription["id"]]
        # the endpoint/token is a secret and is never listed
        assert "endpoint" not in listed["subscriptions"][0]
        await client.post("/notify", json={"uuid": token, "message": "boom", "secret": "classified"})
        await asyncio.sleep(0.4)
        assert received and received[0]["type"] == "page"
        assert "mailbox" not in received[0]  # hash-only: no raw UUID on the wire
        # rotating hash alongside the raw mailbox (sha256("<uuid>:<unix-hour>"),
        # tolerant of an hour tick mid-test)
        hour = int(time.time() // 3600)
        candidates = {hashlib.sha256(f"{token}:{h}".encode()).hexdigest() for h in (hour, hour - 1)}
        assert received[0]["mailbox_hash"] in candidates
        assert received[0]["mailbox_hash"] != token
        # data-only: the trigger never carries message content
        assert "message" not in received[0] and "secret" not in received[0]
        assert (await client.delete(f"/mailboxes/{token}/subscriptions/{subscription['id']}")).status == 200
        received.clear()
        await client.post("/notify", json={"uuid": token, "message": "again"})
        await asyncio.sleep(0.4)
        assert received == []
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_subscription_validation(monkeypatch):
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        bad = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": "ftp://nope"})
        assert bad.status == 400
        missing = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush"})
        assert missing.status == 400
        unknown = await client.post(f"/mailboxes/{token}/subscriptions", json={"id": "does-not-exist"})
        assert unknown.status == 404
    finally:
        await client.close()


def test_subscription_warning_before_expiry():
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 128, subscription_ttl=2 * 86400, max_subscriptions=16)
    token = "00000000-0000-0000-0000-00000000000b"
    sub = store.add_subscription(token, "unifiedpush", "https://e.test/hook", None)
    # lease (2d) is inside the 3-day warning window
    assert [s.id for s in store.subscriptions_nearing_expiry(time.time(), 3 * 86400)] == [sub.id]
    store.mark_warned(sub.id)
    assert store.subscriptions_nearing_expiry(time.time(), 3 * 86400) == []
    store.renew_subscription(token, sub.id)
    assert [s.id for s in store.subscriptions_nearing_expiry(time.time(), 3 * 86400)] == [sub.id]


@pytest.mark.asyncio
async def test_subscription_expiry_warning_reaches_endpoint(monkeypatch):
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "127.0.0.1")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "0.01")
    monkeypatch.setenv("SUBSCRIPTION_WARN_DAYS", "1")
    received = []

    async def hook(request):
        received.append(await request.json())
        return web.json_response({"ok": True})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/hook", hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        endpoint = f"http://127.0.0.1:{receiver.server.port}/hook"
        await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
        for _ in range(30):
            await asyncio.sleep(0.2)
            if any(item.get("type") == "warning" for item in received):
                break
        assert any(item.get("type") == "warning" and item.get("reason") == "subscription-expiring" for item in received)
        # warnings identify the mailbox both raw (compat) and hashed (rotating)
        warning = next(item for item in received if item.get("type") == "warning")
        assert "mailbox" not in warning
        hour = int(time.time() // 3600)
        candidates = {hashlib.sha256(f"{token}:{h}".encode()).hexdigest() for h in (hour, hour - 1)}
        assert warning["mailbox_hash"] in candidates
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_settings_endpoint_exposes_client_relevant_config(monkeypatch):
    monkeypatch.setenv("MESSAGE_TTL_DAYS", "3")
    monkeypatch.setenv("MAILBOX_TTL_DAYS", "10")
    monkeypatch.setenv("MAX_MESSAGES_PER_MAILBOX", "50")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "7")
    monkeypatch.setenv("SUBSCRIPTION_WARN_DAYS", "2")
    monkeypatch.setenv("WAIT_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("SSE_KEEPALIVE_SECONDS", "20")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "1800")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/settings")
        assert resp.status == 200
        data = await resp.json()
        assert data["version"] == __version__ and data["build"] == __build__
        assert data["limits"]["max_messages_per_mailbox"] == 50
        assert data["retention"]["message_ttl_seconds"] == 3 * 86400
        assert data["retention"]["mailbox_ttl_seconds"] == 10 * 86400
        assert data["wait"]["timeout_seconds"] == 90
        assert data["sse"]["keepalive_seconds"] == 20
        assert data["subscriptions"]["lease_seconds"] == 7 * 86400
        assert data["subscriptions"]["warn_seconds"] == 2 * 86400
        assert "unifiedpush" in data["subscriptions"]["channels"]
        assert data["cleanup_interval_seconds"] == 1800
        # fresh server time for client clock-offset alignment (rotating hashes)
        assert isinstance(data["server_time"], int)
        assert abs(data["server_time"] - int(time.time())) < 60
        # never leak secrets
        assert "secret" not in str(data).lower()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mailbox_status_reports_push_state(monkeypatch):
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "e.test")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        status = await (await client.get(f"/mailboxes/{token}/status")).json()
        assert status["mailbox"] == token
        assert status["messages"] == 0
        assert status["push"]["enabled"] is False
        assert status["push"]["next_expiry"] is None
        await client.post("/notify", json={"uuid": token, "message": "hi"})
        await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": "https://e.test/hook"})
        status = await (await client.get(f"/mailboxes/{token}/status")).json()
        assert status["messages"] == 1
        assert status["push"]["enabled"] is True
        assert status["push"]["subscriptions"] == 1
        assert status["push"]["channels"] == ["unifiedpush"]
        assert status["push"]["next_expiry"] > time.time()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ntfy_channel_publishes_content_free_wakeup(monkeypatch):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    received = []

    async def hook(request):
        received.append({"topic": request.match_info["topic"], "title": request.headers.get("Title"), "body": await request.text()})
        return web.json_response({"ok": True})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/{topic}", hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    base = f"http://127.0.0.1:{receiver.server.port}"
    monkeypatch.setenv("NTFY_URL", base)
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        created = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "ntfy", "topic": "pager-test"})
        assert created.status == 201
        settings = await (await client.get("/settings")).json()
        assert "ntfy" in settings["subscriptions"]["channels"]
        assert settings["subscriptions"]["ntfy_url"] == base
        listed = await (await client.get(f"/mailboxes/{token}/subscriptions")).json()
        assert "pager-test" not in json.dumps(listed)
        await client.post("/notify", json={"uuid": token, "message": "secret-content"})
        await asyncio.sleep(0.4)
        assert received and received[0]["topic"] == "pager-test"
        assert received[0]["title"] == "PagerMCP"
        # hash-only: the body carries the rotating hash, never the UUID
        assert token not in received[0]["body"]
        assert "mailbox_hash:" in received[0]["body"]
        # data-only: the wake-up never carries the message content
        assert "secret-content" not in received[0]["body"]
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_ntfy_channel_requires_config(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "ntfy", "topic": "x"})
        assert resp.status == 400
    finally:
        await client.close()


def _fcm_service_account(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    path = tmp_path / "fcm.json"
    path.write_text(json.dumps({"client_email": "svc@test.iam.gserviceaccount.com", "private_key": pem, "project_id": "testproj"}))
    return path


@pytest.mark.asyncio
async def test_fcm_channel_sends_data_only(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    sent = []
    token_calls = []

    async def token_hook(request):
        token_calls.append(await request.post())
        return web.json_response({"access_token": "test-access", "expires_in": 3600})

    async def send_hook(request):
        sent.append(await request.json())
        return web.json_response({"name": "projects/testproj/messages/1"})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/token", token_hook), web.post("/v1/projects/{tail:.*}", send_hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    base = f"http://127.0.0.1:{receiver.server.port}"
    monkeypatch.setenv("FCM_CREDENTIALS", str(_fcm_service_account(tmp_path)))
    monkeypatch.setenv("FCM_PROJECT_ID", "testproj")
    monkeypatch.setenv("FCM_BASE_URL", base)
    monkeypatch.setenv("FCM_TOKEN_URL", f"{base}/token")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        created = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "fcm", "token": "device-token-1234567890"})
        assert created.status == 201
        settings = await (await client.get("/settings")).json()
        assert "fcm" in settings["subscriptions"]["channels"]
        assert settings["subscriptions"]["fcm_enabled"] is True
        listed = await (await client.get(f"/mailboxes/{token}/subscriptions")).json()
        assert "device-token-1234567890" not in json.dumps(listed)
        await client.post("/notify", json={"uuid": token, "message": "secret-content"})
        await asyncio.sleep(0.5)
        assert token_calls and token_calls[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert sent
        message = sent[0]["message"]
        assert message["token"] == "device-token-1234567890"
        assert message["android"]["priority"] == "high"
        assert message["data"]["type"] == "page"
        assert "mailbox" not in message["data"]
        assert message["data"]["mailbox_hash"]
        # data-only by default: the wake-up never carries the message content
        assert "secret-content" not in json.dumps(sent[0])
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_fcm_invalid_token_removes_subscription(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")

    async def token_hook(request):
        return web.json_response({"access_token": "test-access", "expires_in": 3600})

    async def send_hook(request):
        return web.json_response({"error": {"status": "INVALID_ARGUMENT"}}, status=400)

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/token", token_hook), web.post("/v1/projects/{tail:.*}", send_hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    base = f"http://127.0.0.1:{receiver.server.port}"
    monkeypatch.setenv("FCM_CREDENTIALS", str(_fcm_service_account(tmp_path)))
    monkeypatch.setenv("FCM_PROJECT_ID", "testproj")
    monkeypatch.setenv("FCM_BASE_URL", base)
    monkeypatch.setenv("FCM_TOKEN_URL", f"{base}/token")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "fcm", "token": "device-token-1234567890"})
        await client.post("/notify", json={"uuid": token, "message": "x"})
        await asyncio.sleep(0.5)
        listed = await (await client.get(f"/mailboxes/{token}/subscriptions")).json()
        assert listed["subscriptions"] == []
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_fcm_token_can_be_overwritten_but_not_read(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    sent = []

    async def token_hook(request):
        return web.json_response({"access_token": "test-access", "expires_in": 3600})

    async def send_hook(request):
        sent.append(await request.json())
        return web.json_response({"name": "ok"})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/token", token_hook), web.post("/v1/projects/{tail:.*}", send_hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    base = f"http://127.0.0.1:{receiver.server.port}"
    monkeypatch.setenv("FCM_CREDENTIALS", str(_fcm_service_account(tmp_path)))
    monkeypatch.setenv("FCM_PROJECT_ID", "testproj")
    monkeypatch.setenv("FCM_BASE_URL", base)
    monkeypatch.setenv("FCM_TOKEN_URL", f"{base}/token")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        mailbox = (await (await client.post("/mailboxes")).json())["uuid"]
        created = await client.post(f"/mailboxes/{mailbox}/subscriptions", json={"channel": "fcm", "token": "device-token-OLD-000000"})
        subscription = await created.json()
        # overwrite the token via the id (write-only), never reading it back
        rotated = await client.post(f"/mailboxes/{mailbox}/subscriptions", json={"id": subscription["id"], "channel": "fcm", "token": "device-token-NEW-999999"})
        assert rotated.status == 200
        await client.post("/notify", json={"uuid": mailbox, "message": "x"})
        await asyncio.sleep(0.5)
        assert sent and sent[-1]["message"]["token"] == "device-token-NEW-999999"
        # an empty value only renews; it must not clobber the stored token
        renewed = await client.post(f"/mailboxes/{mailbox}/subscriptions", json={"id": subscription["id"], "channel": "fcm", "token": ""})
        assert renewed.status == 200
        sent.clear()
        await client.post("/notify", json={"uuid": mailbox, "message": "y"})
        await asyncio.sleep(0.5)
        assert sent and sent[-1]["message"]["token"] == "device-token-NEW-999999"
        listed = await (await client.get(f"/mailboxes/{mailbox}/subscriptions")).json()
        blob = json.dumps(listed)
        assert subscription["id"] in blob
        assert "device-token-OLD-000000" not in blob and "device-token-NEW-999999" not in blob
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_fcm_channel_requires_config(monkeypatch):
    monkeypatch.delenv("FCM_CREDENTIALS", raising=False)
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "fcm", "token": "device-token-1234567890"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webclient_mode_retains_ack_until_new_message(monkeypatch):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        enabled = await client.post(f"/mailboxes/{token}/mode", json={"webclient": True})
        assert enabled.status == 200
        body = await enabled.json()
        assert body["webclient"] is True and body["expires"] > time.time()

        accepted = await client.post("/notify", json={"uuid": token, "message": "first"})
        page_id = (await accepted.json())["id"]
        delivered = await (await client.get(f"/mailboxes/{token}/wait?timeout_seconds=3")).json()
        assert delivered["id"] == page_id and delivered["acked"] is False

        acked = await (await client.post(f"/mailboxes/{token}/ack", json={"id": page_id})).json()
        assert acked["acked"] is True and acked["retained"] is True

        messages = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        assert [m["id"] for m in messages] == [page_id]
        assert messages[0]["acked"] is True

        # a retained (acked) page is never redelivered by /wait
        again = await client.get(f"/mailboxes/{token}/wait?timeout_seconds=0.05")
        assert again.status == 408

        # a new message is queued normally; the retained (read) copy stays until
        # the queue is full, then the oldest read message is evicted
        await client.post("/notify", json={"uuid": token, "message": "second"})
        messages = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        assert [m["message"] for m in messages] == ["first", "second"]
        assert [m["acked"] for m in messages] == [True, False]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webclient_mode_disable_drops_retained(monkeypatch):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post(f"/mailboxes/{token}/mode", json={"webclient": True})
        accepted = await client.post("/notify", json={"uuid": token, "message": "keep"})
        page_id = (await accepted.json())["id"]
        await client.post(f"/mailboxes/{token}/ack", json={"id": page_id})
        assert len((await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]) == 1
        disabled = await client.post(f"/mailboxes/{token}/mode", json={"webclient": False})
        assert (await disabled.json())["webclient"] is False
        assert (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webclient_mode_evicts_oldest_read_when_full(monkeypatch):
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MAX_MESSAGES_PER_MAILBOX", "2")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post(f"/mailboxes/{token}/mode", json={"webclient": True})
        first = await (await client.post("/notify", json={"uuid": token, "message": "a"})).json()
        await client.post(f"/mailboxes/{token}/ack", json={"id": first["id"]})
        assert (await client.post("/notify", json={"uuid": token, "message": "b"})).status == 202
        # queue is full (a=read, b=unread): a new message evicts the oldest read one
        assert (await client.post("/notify", json={"uuid": token, "message": "c"})).status == 202
        messages = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        assert [m["message"] for m in messages] == ["b", "c"]
        assert all(m["acked"] is False for m in messages)
    finally:
        await client.close()


def test_webclient_mode_lease_expiry_drops_retained():
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 128)
    token = "00000000-0000-0000-0000-00000000000c"
    page = store.enqueue(token, {"message": "x"})
    store.set_webclient(token, time.time() + 0.05)
    store.retain(token, page.id)
    assert store.pending(token)[0].acked is True
    time.sleep(0.1)
    store.purge()
    assert store.pending(token) == []
    assert store.webclient_active(token) is False


@pytest.mark.asyncio
async def test_messages_order_query(monkeypatch):
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        await client.post("/notify", json={"uuid": token, "message": "one"})
        await asyncio.sleep(0.02)
        await client.post("/notify", json={"uuid": token, "message": "two"})
        oldest = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        newest = (await (await client.get(f"/mailboxes/{token}/messages?order=newest")).json())["messages"]
        assert [m["message"] for m in oldest] == ["one", "two"]
        assert [m["message"] for m in newest] == ["two", "one"]
        # the queue itself stays FIFO: /wait returns the oldest
        delivered = await (await client.get(f"/mailboxes/{token}/wait?timeout_seconds=2")).json()
        assert delivered["message"] == "one"
        bad = await client.get(f"/mailboxes/{token}/messages?order=sideways")
        assert bad.status == 400
    finally:
        await client.close()


async def _p2p_post(client, token, p2p_id, message="m", expires_in=3600.0):
    body = {"p2p": True, "p2p_id": p2p_id, "p2p_expires_at": time.time() + expires_in, "message": message}
    return await client.post("/notify", headers={"Authorization": f"Bearer {token}"}, json=body)


@pytest.mark.asyncio
async def test_p2p_disabled_rejects(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "false")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await _p2p_post(client, token, str(uuid.uuid4()))
        assert resp.status == 403
        assert (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_repeated_post_is_duplicate(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        p2p_id = str(uuid.uuid4())
        first = await _p2p_post(client, token, p2p_id, "hello")
        assert first.status == 200
        d1 = await first.json()
        assert d1["duplicate"] is False
        second = await _p2p_post(client, token, p2p_id, "hello")
        assert second.status == 200
        d2 = await second.json()
        assert d2["duplicate"] is True and d2["id"] == d1["id"]
        messages = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        assert [m["id"] for m in messages] == [d1["id"]]
        # the delivered message keeps the p2p id so the client can dedupe too
        assert messages[0]["p2p_id"] == p2p_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_same_id_different_payload_conflicts(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        p2p_id = str(uuid.uuid4())
        assert (await _p2p_post(client, token, p2p_id, "one")).status == 200
        conflict = await _p2p_post(client, token, p2p_id, "two")
        assert conflict.status == 409
        messages = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        assert len(messages) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_expired_is_rejected_and_stored_nowhere(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        p2p_id = str(uuid.uuid4())
        resp = await _p2p_post(client, token, p2p_id, "old", expires_in=-60)
        assert resp.status == 410
        assert (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"] == []
        # a later (valid) post with the same id is new, proving nothing was cached
        ok = await _p2p_post(client, token, p2p_id, "old")
        assert (await ok.json())["duplicate"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_expiry_beyond_max_age_rejected(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    monkeypatch.setenv("P2P_MAX_MESSAGE_AGE_SECONDS", "100")
    monkeypatch.setenv("MESSAGE_TTL_DAYS", "7")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await _p2p_post(client, token, str(uuid.uuid4()), expires_in=500)
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_invalid_id_rejected(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        resp = await _p2p_post(client, token, "not-a-uuid")
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_ack_keeps_dedupe_entry(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        p2p_id = str(uuid.uuid4())
        first = await (await _p2p_post(client, token, p2p_id, "keep")).json()
        await client.post(f"/mailboxes/{token}/ack", json={"id": first["id"]})
        assert (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"] == []
        # late duplicate after the message was acked is still recognised
        again = await (await _p2p_post(client, token, p2p_id, "keep")).json()
        assert again["duplicate"] is True and again["id"] == first["id"]
        assert (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_concurrent_identical_posts_create_one_message(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        p2p_id = str(uuid.uuid4())
        responses = await asyncio.gather(_p2p_post(client, token, p2p_id), _p2p_post(client, token, p2p_id))
        bodies = [await r.json() for r in responses]
        assert sorted(b["duplicate"] for b in bodies) == [False, True]
        assert bodies[0]["id"] == bodies[1]["id"]
        messages = (await (await client.get(f"/mailboxes/{token}/messages")).json())["messages"]
        assert len(messages) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p2p_flag_required_for_dedupe(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        p2p_id = str(uuid.uuid4())
        # normal notify that happens to carry a p2p_id but no p2p flag: no cache
        normal = await client.post("/notify", headers={"Authorization": f"Bearer {token}"}, json={"message": "a", "p2p_id": p2p_id})
        assert normal.status == 202
        p2p = await _p2p_post(client, token, p2p_id, "a")
        assert (await p2p.json())["duplicate"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_settings_reports_p2p(monkeypatch):
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "true")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    monkeypatch.setenv("P2P_DEDUPE_MAX_ENTRIES", "42")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/settings")).json()
        assert data["p2p"]["enabled"] is True
        assert data["p2p"]["max_entries"] == 42
        assert data["p2p"]["dedupe_ttl_seconds"] > 0
        assert data["p2p"]["max_message_age_seconds"] > 0
    finally:
        await client.close()


def test_p2p_cache_persists_across_restart(tmp_path):
    token = "00000000-0000-0000-0000-00000000000d"
    expires = time.time() + 3600
    store = MailboxStore(str(tmp_path), 30 * 86400, 7 * 86400, 128, 0, 16, 1000, 10)
    status, page = store.p2p_enqueue(token, "p2p-1", {"message": "a"}, "hash", expires)
    assert status == "new"
    # a fresh store on the same DATA_DIR still remembers the id
    restarted = MailboxStore(str(tmp_path), 30 * 86400, 7 * 86400, 128, 0, 16, 1000, 10)
    status, message_id = restarted.p2p_enqueue(token, "p2p-1", {"message": "a"}, "hash", expires)
    assert status == "duplicate" and message_id == page.id


def test_p2p_cache_capacity_evicts_oldest():
    token = "00000000-0000-0000-0000-00000000000e"
    expires = time.time() + 3600
    store = MailboxStore(None, 30 * 86400, 7 * 86400, 128, 0, 16, 10, 1)
    assert store.p2p_enqueue(token, "id-1", {"message": "1"}, "h1", expires)[0] == "new"
    # per-mailbox cap is 1: adding id-2 evicts id-1
    assert store.p2p_enqueue(token, "id-2", {"message": "2"}, "h2", expires)[0] == "new"
    # id-1 was evicted, so it is new again (and evicts id-2)
    assert store.p2p_enqueue(token, "id-1", {"message": "1"}, "h1", expires)[0] == "new"


@pytest.mark.asyncio
async def test_subscription_blocks_non_public_endpoints_by_default():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        for endpoint in [
            "http://127.0.0.1:9/hook",
            "http://localhost:9/hook",
            "http://169.254.169.254/latest/meta-data",
            "http://192.168.1.10/hook",
            "http://10.0.0.5/hook",
            "http://172.16.0.1/hook",
            "http://[::1]:9/hook",
            "http://0.0.0.0/hook",
            "http://100.64.0.1/hook",
            "http://100.127.255.255/hook",
            "http://192.0.2.1/hook",
            "http://198.51.100.1/hook",
            "http://203.0.113.1/hook",
            "http://198.18.0.1/hook",
            "http://[::ffff:127.0.0.1]/hook",
            "http://224.0.0.1/hook",
            "http://255.255.255.255/hook",
            "http://[ff02::1]/hook",
        ]:
            resp = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
            assert resp.status == 400, endpoint
        # public addresses are fine without any DNS lookup (100.128.x is outside CGNAT)
        for endpoint in ["https://93.184.215.14/hook", "http://100.128.0.1/hook"]:
            ok = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
            assert ok.status == 201, endpoint
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subscription_allowlist_exempts_host_and_cidr(monkeypatch):
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "up.internal.example, 10.13.40.0/24")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        allowed_host = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": "https://up.internal.example/hook"})
        assert allowed_host.status == 201
        allowed_net = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": "http://10.13.40.50/hook"})
        assert allowed_net.status == 201
        other_private = await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": "http://10.0.0.5/hook"})
        assert other_private.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_push_trigger_never_follows_redirects(monkeypatch):
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "127.0.0.1")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    followed = []

    async def redirect_hook(request):
        raise web.HTTPFound(location=f"http://127.0.0.1:{request.url.port}/landed")

    async def landed(request):
        followed.append(await request.json())
        return web.json_response({"ok": True})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/hook", redirect_hook), web.post("/landed", landed)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        endpoint = f"http://127.0.0.1:{receiver.server.port}/hook"
        await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
        await client.post("/notify", json={"uuid": token, "message": "redirect test"})
        await asyncio.sleep(0.5)
        assert followed == []
    finally:
        await client.close()
        await receiver.close()


def test_drop_privileges_defaults_to_nobody(tmp_path, monkeypatch):
    from pager_mcp import server as srv

    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda g: calls.append(("setgroups", g)))
    monkeypatch.setattr(os, "setgid", lambda g: calls.append(("setgid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: calls.append(("setuid", u)))
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append(("chown", str(p), u, g)))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PAGER_UID", raising=False)
    monkeypatch.delenv("PAGER_GID", raising=False)
    monkeypatch.delenv("PAGER_LOG_FILE", raising=False)
    srv.drop_privileges()
    assert ("setuid", 65534) in calls
    assert ("setgid", 65534) in calls
    assert calls.index(("setgid", 65534)) < calls.index(("setuid", 65534))
    assert ("chown", str(tmp_path), 65534, 65534) in calls


def test_drop_privileges_noop_when_unprivileged(monkeypatch):
    from pager_mcp import server as srv

    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def boom(*a):
        raise AssertionError("must not be called when already unprivileged")

    monkeypatch.setattr(os, "setuid", boom)
    monkeypatch.setattr(os, "setgid", boom)
    srv.drop_privileges()


def test_client_parse_message_skips_malformed_lines():
    from pager_mcp.client import parse_message

    assert parse_message('{"id": 1, "method": "initialize"}') == {"id": 1, "method": "initialize"}
    assert parse_message("not json at all") is None
    assert parse_message('{"broken": ') is None
    assert parse_message("[1, 2, 3]") is None
    assert parse_message("42") is None


def test_session_file_ownership_does_not_follow_symlinks(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("chown to another uid requires root")
    from pager_mcp.client import fix_session_file_ownership

    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / ".pager_session"
    link.symlink_to(target)
    monkeypatch.setenv("PAGER_UID", "65534")
    monkeypatch.setenv("PAGER_GID", "65534")
    fix_session_file_ownership(link)
    assert target.stat().st_uid != 65534  # the target is untouched
    assert os.lstat(link).st_uid == 65534  # only the link itself changed


def test_pager_sh_json_escape_handles_control_chars():
    script = 'eval "$(sed -n \'/^json_escape()/,/^}/p\' sample/pager.sh)"; json_escape "$1"'
    arg = 'a\tb\rc\x01d"e\\f'
    out = subprocess.run(["sh", "-c", script, "sh", arg], capture_output=True, text=True)
    assert out.returncode == 0
    result = out.stdout
    assert "\t" not in result and "\r" not in result and "\x01" not in result
    assert "a bcd" in result
    assert '\\"' in result


def test_pager_sh_session_file_must_contain_uuid(tmp_path):
    good = tmp_path / "good"
    good.write_text("550e8400-e29b-41d4-a716-446655440000\n")
    bad = tmp_path / "bad"
    bad.write_text("not-a-uuid\n")
    extract = 'eval "$(sed -n \'/^UUID_PATTERN=/p;/^resolve_uuid()/,/^}/p\' sample/pager.sh)"; SESSION="$1"; resolve_uuid'
    ok = subprocess.run(["sh", "-c", extract, "sh", str(good)])
    assert ok.returncode == 0
    bad_result = subprocess.run(["sh", "-c", extract, "sh", str(bad)])
    assert bad_result.returncode != 0


@pytest.mark.asyncio
async def test_settings_can_hide_ntfy_url(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.internal.example")
    monkeypatch.setenv("SETTINGS_HIDE_NTFY_URL", "1")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/settings")).json()
        assert "ntfy_url" not in data["subscriptions"]
        assert "ntfy" in data["subscriptions"]["channels"]
    finally:
        await client.close()


def test_purge_indexes_exist(tmp_path):
    store = MailboxStore(str(tmp_path), 30 * 86400, 7 * 86400, 128)
    names = {row[1] for row in store.db.execute("SELECT type, name FROM sqlite_master WHERE type='index'")}
    assert "pages_created_idx" in names
    assert "push_subscriptions_expires_idx" in names


@pytest.mark.asyncio
async def test_root_readme_is_rendered_once(monkeypatch):
    import pager_mcp.server as srv

    calls = []
    real = srv.markdown.markdown

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(srv.markdown, "markdown", counting)
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r1 = await client.get("/")
        r2 = await client.get("/")
        assert r1.status == 200 and r2.status == 200
        assert (await r1.text()) == (await r2.text())
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mailbox_hash_can_be_disabled_and_period_configurable(monkeypatch):
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "127.0.0.1")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MAILBOX_HASH_ENABLED", "0")
    monkeypatch.setenv("MAILBOX_HASH_PERIOD_SECONDS", "1800")
    received = []

    async def hook(request):
        received.append(await request.json())
        return web.json_response({"ok": True})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/hook", hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        settings = await (await client.get("/settings")).json()
        assert settings["subscriptions"]["identity"] == "uuid"
        assert settings["subscriptions"]["identity_period_seconds"] == 1800
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        endpoint = f"http://127.0.0.1:{receiver.server.port}/hook"
        await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
        await client.post("/notify", json={"uuid": token, "message": "no hash"})
        await asyncio.sleep(0.4)
        assert received and received[0]["mailbox"] == token
        assert "mailbox_hash" not in received[0]
    finally:
        await client.close()
        await receiver.close()


@pytest.mark.asyncio
async def test_mailbox_hash_custom_period_and_settings(monkeypatch):
    monkeypatch.setenv("PUSH_ENDPOINT_ALLOWLIST", "127.0.0.1")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MAILBOX_HASH_PERIOD_SECONDS", "1800")
    received = []

    async def hook(request):
        received.append(await request.json())
        return web.json_response({"ok": True})

    receiver_app = web.Application()
    receiver_app.add_routes([web.post("/hook", hook)])
    receiver = TestClient(TestServer(receiver_app))
    await receiver.start_server()
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        settings = await (await client.get("/settings")).json()
        assert settings["subscriptions"]["identity"] == "hash"
        assert settings["subscriptions"]["identity_period_seconds"] == 1800
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        endpoint = f"http://127.0.0.1:{receiver.server.port}/hook"
        await client.post(f"/mailboxes/{token}/subscriptions", json={"channel": "unifiedpush", "endpoint": endpoint})
        await client.post("/notify", json={"uuid": token, "message": "half hour"})
        await asyncio.sleep(0.4)
        period = int(time.time() // 1800)
        candidates = {hashlib.sha256(f"{token}:{p}".encode()).hexdigest() for p in (period, period - 1)}
        assert received and received[0]["mailbox_hash"] in candidates
    finally:
        await client.close()
        await receiver.close()


import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

GPG_AVAILABLE = shutil.which("gpg") is not None
requires_gpg = pytest.mark.skipif(not GPG_AVAILABLE, reason="gpg is not installed")


def gpg_encrypt(armored_public_key: str, plaintext: str, home: Path) -> str:
    home.mkdir(exist_ok=True)
    home.chmod(0o700)
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--import"], input=armored_public_key.encode(), capture_output=True, check=True)
    result = subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--yes", "--armor", "--encrypt", "--trust-model", "always", "--recipient", "PagerMCP"],
        input=plaintext.encode(),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode()


@requires_gpg
@pytest.mark.asyncio
async def test_p2p_gpg_settings_and_encrypted_uuid_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "1")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        settings = await (await client.get("/settings")).json()
        p2p = settings["p2p"]
        assert p2p["gpg_enabled"] is True
        assert "BEGIN PGP PUBLIC KEY BLOCK" in p2p["public_key"]
        assert p2p["key_id"]

        token = (await (await client.post("/mailboxes")).json())["uuid"]
        armored = gpg_encrypt(p2p["public_key"], token, tmp_path / "client-gnupg")
        # the wire format is ciphertext, never the UUID itself
        assert armored != token and token not in armored
        import re as _re
        assert not _re.match(r"^[0-9a-f-]{36}$", armored.strip())

        expires = int(time.time()) + 3600
        p2p_id = str(uuid.uuid4())
        first = await client.post("/notify", json={"p2p": True, "p2p_id": p2p_id, "p2p_expires_at": expires, "mailbox_encrypted": armored, "message": "secret"})
        assert first.status == 200
        assert (await first.json())["duplicate"] is False
        # the server decrypted and routed to the right mailbox
        messages = await (await client.get(f"/mailboxes/{token}/messages")).json()
        assert len(messages["messages"]) == 1
        # dedupe still works on the decrypted mailbox
        second = await client.post("/notify", json={"p2p": True, "p2p_id": p2p_id, "p2p_expires_at": expires, "mailbox_encrypted": armored, "message": "secret"})
        assert (await second.json())["duplicate"] is True
    finally:
        await client.close()


@requires_gpg
@pytest.mark.asyncio
async def test_p2p_plain_uuid_rejected_and_bad_ciphertext_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "1")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        plain = await client.post("/notify", json={"p2p": True, "p2p_id": str(uuid.uuid4()), "p2p_expires_at": int(time.time()) + 3600, "uuid": token, "message": "x"})
        assert plain.status == 400
        bad = await client.post("/notify", json={"p2p": True, "p2p_id": str(uuid.uuid4()), "p2p_expires_at": int(time.time()) + 3600, "mailbox_encrypted": "not-a-gpg-message"})
        assert bad.status == 400
        missing = await client.post("/notify", json={"p2p": True, "p2p_id": str(uuid.uuid4()), "p2p_expires_at": int(time.time()) + 3600, "message": "x"})
        assert missing.status == 400
    finally:
        await client.close()


@requires_gpg
@pytest.mark.asyncio
async def test_p2p_plain_uuid_allowed_when_gpg_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("P2P_DEDUPE_ENABLED", "1")
    monkeypatch.setenv("P2P_GPG_ENABLED", "0")
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        settings = await (await client.get("/settings")).json()
        assert settings["p2p"]["gpg_enabled"] is False
        assert settings["p2p"]["public_key"] is None
        token = (await (await client.post("/mailboxes")).json())["uuid"]
        response = await client.post("/notify", json={"p2p": True, "p2p_id": str(uuid.uuid4()), "p2p_expires_at": int(time.time()) + 3600, "uuid": token, "message": "x"})
        assert response.status == 200
    finally:
        await client.close()


@requires_gpg
def test_gpg_key_renews_near_expiry_and_old_key_still_decrypts(tmp_path):
    from pager_mcp.gpgkeys import GpgKeyring

    home = tmp_path / "gnupg"
    home.mkdir()
    home.chmod(0o700)
    subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--yes", "--gen-key"],
        input=b"%no-protection\nKey-Type: RSA\nKey-Length: 2048\nName-Real: PagerMCP\nExpire-Date: 1d\n%commit\n",
        capture_output=True,
        check=True,
    )
    old_public = subprocess.run(["gpg", "--homedir", str(home), "--armor", "--export", "PagerMCP"], capture_output=True, check=True).stdout.decode()

    ring = GpgKeyring(str(tmp_path))
    ring.home = home
    ring.ensure()  # key expires within the renewal window -> a second key is made
    pub_records = subprocess.run(["gpg", "--homedir", str(home), "--list-keys", "--with-colons"], capture_output=True, check=True).stdout.decode()
    assert sum(1 for line in pub_records.splitlines() if line.startswith("pub:")) == 2
    assert ring.public_key() != old_public  # the newest key is advertised now

    # a message encrypted to the OLD key still decrypts during the overlap
    armored = gpg_encrypt(old_public, "550e8400-e29b-41d4-a716-446655440000", tmp_path / "client2")
    assert ring.decrypt(armored) == "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.asyncio
async def test_doc_endpoint_serves_self_contained_documentation():
    app = build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/doc")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        body = await resp.text()
        assert "PagerMCP" in body
        assert "mailbox_hash" in body
        assert "mailbox_encrypted" in body
        assert "identity_period_seconds" in body
        # fully self-contained: inline stylesheet, no external resources
        assert "<style>" in body
        assert "<link" not in body
        assert "cdn." not in body
        assert "https://cdn" not in body
    finally:
        await client.close()


def test_gpg_binary_resolves_even_without_path(monkeypatch):
    from pager_mcp import gpgkeys

    monkeypatch.setattr(gpgkeys.shutil, "which", lambda _: None)
    resolved = gpgkeys._gpg_binary()
    assert resolved is not None and os.path.exists(resolved)


def test_access_log_masks_uuid(tmp_path, monkeypatch):
    from pager_mcp.server import configure_access_log
    import logging

    log = tmp_path / "pager.log"
    monkeypatch.setenv("PAGER_LOG_FILE", str(log))
    monkeypatch.setenv("PAGER_LOG_RETENTION_DAYS", "1")
    logger = configure_access_log()
    assert logger is not None
    assert isinstance(logger.handlers[0], logging.Handler)
    import io
    from datetime import datetime

    record = logging.LogRecord("aiohttp.access", logging.INFO, "x", 0, '10.0.0.1 - - [05/Nov/2026] "GET /mailboxes/550e8400-e29b-41d4-a716-446655440000/wait HTTP/1.1" 200 0.1', (), None)
    line = logger.handlers[0].format(record)
    assert "550e8400-e29b-41d4-a716-446655440000" not in line
    assert "<uuid>" in line
    logger.handlers[0].close()
    logger.removeHandler(logger.handlers[0])


def test_access_log_off_by_default(monkeypatch):
    from pager_mcp.server import configure_access_log

    monkeypatch.delenv("PAGER_LOG_FILE", raising=False)
    assert configure_access_log() is None


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
    assert "DEFAULT_MAX_AGE_MS" in content
    assert "max_age_seconds" in content
    assert "STALE" in content


def test_plugin_acknowledges_and_scopes_sessions():
    content = Path("plugins/pager.js").read_text(encoding="utf-8")
    # at-least-once: acknowledge only after a successful hand-off
    assert "/ack" in content
    assert "page kept queued" in content
    # bounded long-poll instead of hanging into a proxy 504
    assert "timeout_seconds" in content
    assert "response.status === 408" in content
    # SSE push with automatic fallback to long-poll
    assert "/events" in content
    assert "text/event-stream" in content
    assert "getReader" in content
    assert "PAGER_TRANSPORT" in content
    assert "streamSse" in content
    assert "longPollOnce" in content
    # never deliver into another directory or a sub-agent session
    assert "parentID" in content
    assert "inDirectory" in content
    # self-provision the mailbox so delivery does not depend on the MCP client
    assert "ensureToken" in content
    assert "/mailboxes" in content
    # redelivered (at-least-once) pages must not prompt the session twice
    assert "deliveredIds" in content
    assert "rememberDelivered" in content


def test_client_waits_in_bounded_slices_and_acks():
    content = (Path(__file__).parent.parent / "pager_mcp" / "client.py").read_text(encoding="utf-8")
    assert "wait_for_page" in content
    assert "timeout_seconds" in content
    assert "/ack" in content
    assert "PAGER_MCP_WAIT_SECONDS" in content
