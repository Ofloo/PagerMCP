from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

try:
    import markdown
except ImportError:
    markdown = None

try:
    import markdown
except ImportError:
    markdown = None

from . import __build__, __version__
from .gpgkeys import GpgKeyring
from .mesh import Mesh
from .storage import MailboxStore


def env_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    return value * 86400 if name.endswith("_DAYS") else value


class Subscriber:
    """A live SSE connection: a bounded queue of pages plus an overflow flag."""

    __slots__ = ("queue", "overflowed")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = asyncio.Event()


def sse_frame(data: dict[str, Any]) -> bytes:
    return f"id: {data.get('id', '')}\nevent: page\ndata: {json.dumps(data)}\n\n".encode()


def load_fcm_account(raw: str) -> dict[str, Any] | None:
    """Read a Firebase service-account from a file path or an inline JSON string."""
    if not raw:
        return None
    try:
        text = Path(raw).read_text(encoding="utf-8") if os.path.isfile(raw) else raw
        account = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(account, dict) and account.get("client_email") and account.get("private_key"):
        return account
    return None


def _mailbox_hash_hex(mailbox: str, period: int = 3600) -> str:
    """Rotating, non-identifying trigger identifier: sha256("<mailbox>:<unix-period>").

    Rotation (default: hourly) keeps one period's pushes unlinkable from the
    next for any observer of the push channel, while a client that knows its
    mailbox UUID can recompute the hash locally (aligning clocks via /settings'
    server_time; the period is advertised as identity_period_seconds).
    """
    return hashlib.sha256(f"{mailbox}:{int(time.time() // period)}".encode()).hexdigest()


def _parse_allowlist(raw: str) -> tuple[set[str], list[Any]]:
    """PUSH_ENDPOINT_ALLOWLIST: comma-separated hostnames, IPs or CIDR networks.

    A hostname entry exempts that exact host; an IP/CIDR entry exempts endpoints
    whose resolved address falls inside it. Empty by default: nothing is exempt.
    """
    hosts: set[str] = set()
    networks: list[Any] = []
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            hosts.add(item)
    return hosts, networks


_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _non_public(ip: Any) -> bool:
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) is unwrapped so the check behaves the
    # same on every Python version; CGNAT (100.64.0.0/10, o.a. Tailscale) is not
    # covered by is_private and is blocked explicitly, like every other range
    # that is not publicly routable (10/8, 172.16/12, 192.168/16, 127/8,
    # 169.254/16, 192.0.2/24, 198.51.100/24, 203.0.113/24, 198.18/15, ...).
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip in _CGNAT:
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


async def _endpoint_error(url: str, allow_hosts: set[str], allow_nets: list[Any]) -> str | None:
    """Guard generic push endpoints against SSRF into internal networks.

    Only public addresses are accepted, unless the host/address is explicitly on
    the operator allowlist. Re-checked at every push (DNS may have changed) and
    redirects are never followed.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return "endpoint has no host"
    if host in allow_hosts:
        return None
    try:
        ip = ipaddress.ip_address(host)
        addresses = [ip]
    except ValueError:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port)
        except OSError:
            return "endpoint hostname does not resolve"
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in addresses:
        if any(address in net for net in allow_nets):
            continue
        if _non_public(address):
            return "endpoint resolves to a non-public address (use PUSH_ENDPOINT_ALLOWLIST to exempt a trusted host/network)"
    return None


def build_app() -> web.Application:
    max_bytes = int(os.getenv("MAX_MESSAGE_BYTES", "65536"))
    p2p_enabled = os.getenv("P2P_DEDUPE_ENABLED", "").lower() in ("1", "true", "yes", "on")
    p2p_dedupe_ttl = env_float("P2P_DEDUPE_TTL_SECONDS", 604800)
    p2p_max_age = env_float("P2P_MAX_MESSAGE_AGE_SECONDS", 604800)
    p2p_max_entries = int(os.getenv("P2P_DEDUPE_MAX_ENTRIES", "1000"))
    p2p_max_per_mailbox = int(os.getenv("P2P_DEDUPE_MAX_PER_MAILBOX", "256"))
    # P2P encrypted destination UUID: a keypair is generated on first start
    # (DATA_DIR/gnupg), valid for a year, renewed automatically near expiry.
    p2p_gpg_enabled = os.getenv("P2P_GPG_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    p2p_keyring = None
    p2p_public_key = None
    p2p_key_id = None
    if p2p_gpg_enabled and GpgKeyring.available():
        try:
            p2p_keyring = GpgKeyring(os.getenv("DATA_DIR") or None)
            p2p_keyring.ensure(legacy_ttl=int(2 * env_float("MESSAGE_TTL_DAYS", 7) * 86400))
            p2p_public_key = p2p_keyring.public_key()
            p2p_key_id = p2p_keyring.key_id()
        except Exception as _e:
            import traceback; traceback.print_exc()
            p2p_keyring = None
            p2p_public_key = None
            p2p_key_id = None

    # Federated peer mesh (one-sided, open peering): unknown mailboxes and
    # unknown P2P key fingerprints are looked up with WHO HAS <hash> and get a
    # 302 to the owning instance. No peers configured = standalone (unchanged).
    mesh_public_url = os.getenv("PAGER_PUBLIC_URL", "").rstrip("/") or f"http://{os.getenv('HOSTNAME', 'localhost')}:{os.getenv('PORT', '8080')}"
    mesh = Mesh(
        os.getenv("PAGER_PEERS", ""),
        os.getenv("PAGER_PEER_BLACKLIST", ""),
        mesh_public_url,
        int(os.getenv("MESH_HASH_PERIOD_SECONDS", "86400")),
        int(os.getenv("MESH_QUERY_HOPS", "3")),
        int(os.getenv("MESH_PEER_MAX_QPM", "120")),
        int(os.getenv("MESH_BLACKLIST_SECONDS", "3600")),
    )
    mesh_open_queries = os.getenv("MESH_OPEN_QUERIES", "1").lower() in ("1", "true", "yes", "on")
    mesh.own_fingerprint = p2p_key_id
    if mesh.enabled and not os.getenv("PAGER_PUBLIC_URL"):
        import sys as _sys
        # Your peering hostname: the public base URL you hand out to peers.
        # Without it, 302 redirects would point at an unreachable hostname.
        print("PagerMCP: PAGER_PEERS is set but PAGER_PUBLIC_URL is not; set it to the public base URL you hand out to peers.", file=_sys.stderr)
    store = MailboxStore(
        os.getenv("DATA_DIR") or None,
        env_float("MAILBOX_TTL_DAYS", 30),
        env_float("MESSAGE_TTL_DAYS", 7),
        int(os.getenv("MAX_MESSAGES_PER_MAILBOX", "128")),
        env_float("SUBSCRIPTION_TTL_DAYS", 30),
        int(os.getenv("MAX_SUBSCRIPTIONS_PER_MAILBOX", "16")),
        p2p_max_entries,
        p2p_max_per_mailbox,
    )
    cleanup_interval = env_float("CLEANUP_INTERVAL_SECONDS", 3600)
    warn_seconds = env_float("SUBSCRIPTION_WARN_DAYS", 3)
    # Webclient retention lease: a mailbox that enables it keeps acked messages
    # until a new message arrives; the client must refresh the mode within this
    # window or retention lapses. Defaults to the message retention (same lifetime).
    webclient_ttl = env_float("WEBCLIENT_TTL_DAYS", 0) or store.message_ttl
    # Cap on simultaneously open long-lived connections (/wait futures + SSE
    # subscribers) per mailbox, so one client cannot pin unlimited coroutines
    # and queues. 0 disables the cap. Advertised via /settings so clients can
    # size their connection pools accordingly.
    max_conns = int(os.getenv("MAX_CONNECTIONS_PER_MAILBOX", "5"))
    # Channels that receive a content-less JSON trigger. `ntfy` is added when an
    # ntfy server is configured; it publishes a plain-text wake-up to a topic.
    ntfy_url = os.getenv("NTFY_URL", "").rstrip("/")
    ntfy_token = os.getenv("NTFY_TOKEN", "")
    # SSRF guard for generic push endpoints: non-public addresses are refused
    # unless the operator explicitly exempts a host/network (default: none).
    push_allow_hosts, push_allow_nets = _parse_allowlist(os.getenv("PUSH_ENDPOINT_ALLOWLIST", ""))
    # mailbox_hash in push triggers: on/off for everyone, and the rotation
    # period (default 3600s = hourly), both advertised via /settings.
    hash_enabled = os.getenv("MAILBOX_HASH_ENABLED", "1").lower() in ("1", "true", "yes", "on")
    hash_period = int(os.getenv("MAILBOX_HASH_PERIOD_SECONDS", "3600")) or 3600
    # FCM (Firebase Cloud Messaging) HTTP v1 via a service-account, opt-in per
    # mailbox: a client registers {\"channel\":\"fcm\",\"token\":\"<device token>\"}.
    fcm_project = os.getenv("FCM_PROJECT_ID", "")
    fcm_base = os.getenv("FCM_BASE_URL", "https://fcm.googleapis.com").rstrip("/")
    fcm_token_url = os.getenv("FCM_TOKEN_URL", "https://oauth2.googleapis.com/token")
    fcm_include_payload = os.getenv("FCM_INCLUDE_PAYLOAD", "").lower() in ("1", "true", "yes", "on")
    fcm_account = load_fcm_account(os.getenv("FCM_CREDENTIALS", ""))
    generic_channels = {"unifiedpush", "http", "webhook"}
    push_channels = generic_channels | ({"ntfy"} if ntfy_url else set()) | ({"fcm"} if fcm_account else set())
    push_state: dict[str, Any] = {"session": None, "reaper": None, "tasks": set(), "fcm": {"account": fcm_account, "token": None, "expires": 0.0}}
    waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
    subscribers: dict[str, set[Subscriber]] = {}
    static_cache = {"Cache-Control": "max-age=3600"}
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Effective, non-secret server settings a client can read to align itself:
    # how long it may wait, how big messages may be, the retention windows, the
    # push-subscription lease it must renew within, etc.
    hide_ntfy_url = os.getenv("SETTINGS_HIDE_NTFY_URL", "").lower() in ("1", "true", "yes", "on")
    subscriptions_settings = {"enabled": True, "channels": sorted(push_channels), "fcm_enabled": fcm_account is not None, "lease_seconds": store.subscription_ttl, "warn_seconds": warn_seconds, "max_per_mailbox": store.max_subscriptions, "identity": "hash" if hash_enabled else "uuid", "identity_period_seconds": hash_period}
    if not hide_ntfy_url:
        # ntfy_url can be an internal hostname; an operator may hide it from the
        # public /settings response (the channel itself keeps working).
        subscriptions_settings["ntfy_url"] = ntfy_url or None
    settings = {
        "version": __version__,
        "build": __build__,
        "hostname": os.getenv("HOSTNAME", ""),
        "limits": {"max_message_bytes": max_bytes, "max_messages_per_mailbox": store.max_messages},
        "retention": {"message_ttl_seconds": store.message_ttl, "mailbox_ttl_seconds": store.mailbox_ttl},
        "wait": {"timeout_seconds": float(os.getenv("WAIT_TIMEOUT_SECONDS", "0"))},
        "sse": {"enabled": True, "keepalive_seconds": env_float("SSE_KEEPALIVE_SECONDS", 15), "queue_max": int(os.getenv("SSE_QUEUE_MAX", "256"))},
        "subscriptions": subscriptions_settings,
        "cleanup_interval_seconds": cleanup_interval,
        "webclient": {"ttl_seconds": webclient_ttl},
        "p2p": {
            "enabled": p2p_enabled,
            "dedupe_ttl_seconds": p2p_dedupe_ttl,
            "max_message_age_seconds": min(p2p_max_age, store.message_ttl) if store.message_ttl else p2p_max_age,
            "max_entries": p2p_max_entries,
            "max_per_mailbox": p2p_max_per_mailbox,
            "gpg_enabled": p2p_keyring is not None,
            "public_key": p2p_public_key,
            "key_id": p2p_key_id,
            "legacy_public_key": p2p_keyring.legacy_public_key() if p2p_keyring else None,
            "legacy_key_id": p2p_keyring.legacy_key_id() if p2p_keyring else None,
        },
        "connections": {"max_per_mailbox": max_conns},
        "mesh": {"enabled": mesh.enabled, "peers": len(mesh.peers), "hash_period_seconds": mesh.hash_period, "max_hops": mesh.max_hops, "open_queries": mesh_open_queries},
        "persistence": store.db is not None,
    }

    def mesh_owns(kind: str, digest: str, at: float | None = None) -> bool:
        # WHOHAS <unixtime> <sha256> for mailboxes (hash recomputed with the
        # query's own timestamp, so both sides are in sync), WHOHAS
        # <fingerprint> for keys (a public key id, checked directly).
        # Only a recognised value yields "I have it".
        if kind == "fingerprint":
            return p2p_keyring is not None and digest in p2p_keyring.fingerprints()
        if kind == "mailbox":
            return any(mesh.hash_for(token, at) == digest for token in store.mailbox_tokens())
        return False

    async def mesh_query(request: web.Request) -> web.Response:
        peer = request.remote or ""
        if mesh.is_blacklisted(peer):
            raise web.HTTPForbidden(text="peer is blacklisted")
        if not mesh.note_query(peer):
            raise web.HTTPTooManyRequests(text="peer is temporarily blacklisted for query flooding")
        if not mesh_open_queries and not any(peer in mesh._peer_host(p) or peer == mesh._peer_host(p) for p in mesh.peers):
            raise web.HTTPForbidden(text="this instance only answers configured peers")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        session = push_state["session"]
        if session is None or session.closed:
            raise web.HTTPServiceUnavailable(text="mesh is not ready")
        data = await mesh.handle_query(session, body, mesh_owns)
        if p2p_key_id:
            data["fingerprint"] = p2p_key_id
        if data.get("error") in ("hop limit exceeded", "hops not in sync"):
            return web.json_response(data, status=403)
        return web.json_response(data)

    @web.middleware
    async def mesh_discovery(request: web.Request, handler):
        # Unknown mailbox on a meshed instance: WHO HAS the hash, and if a peer
        # owns it the client gets a 302 to that instance. Otherwise it is just
        # created locally, exactly as in standalone mode.
        if mesh.enabled and request.path.startswith("/mailboxes/"):
            token = request.match_info.get("token", "")
            if token:
                try:
                    uuid.UUID(token)
                except ValueError:
                    return await handler(request)
                if token not in store.mailbox_tokens():
                    session = push_state["session"]
                    if session is not None and not session.closed:
                        target = await mesh.query(session, "mailbox", token)
                        if target:
                            return web.Response(status=302, headers={"Location": target + request.path_qs})
        return await handler(request)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def version(_: web.Request) -> web.Response:
        return web.json_response({"version": __version__, "build": __build__, "hostname": os.getenv("HOSTNAME", "")})

    async def settings_endpoint(_: web.Request) -> web.Response:
        # server_time must be fresh per request: clients use it to align their
        # clock with the server's before computing rotating mailbox hashes.
        return web.json_response({**settings, "server_time": int(time.time())}, headers={"Cache-Control": "no-cache"})

    readme_cache: dict[str, str] = {}

    async def root(_: web.Request) -> web.Response:
        # The README is static for the lifetime of the process; render it once
        # and serve the cached HTML on every subsequent request.
        cached = readme_cache.get("html")
        if cached is not None:
            return web.Response(text=cached, headers={**static_cache, "Content-Type": "text/html; charset=utf-8"})
        readme_path = Path(base_dir) / "README.md"
        if not readme_path.is_file():
            raise web.HTTPNotFound()
        if markdown is None:
            raise web.HTTPInternalServerError(text="markdown package is required to serve the README")
        body = markdown.markdown(readme_path.read_text(encoding="utf-8"), output_format="html5", extensions=["tables", "fenced_code"])
        css = (Path(__file__).parent / "readme.css").read_text(encoding="utf-8")
        page = (
            "<!DOCTYPE html>"
            '<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>PagerMCP</title><style>{css}</style></head>"
            f"<main>{body}</main></html>"
        )
        readme_cache["html"] = page
        return web.Response(text=page, headers={**static_cache, "Content-Type": "text/html; charset=utf-8"})

    async def rfc(_: web.Request) -> web.Response:
        rfc_path = Path(base_dir) / "RFC-0001-pager-protocol.md"
        if not rfc_path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(rfc_path, headers={**static_cache, "Content-Type": "text/plain"})

    async def privacy(_: web.Request) -> web.Response:
        privacy_path = Path(__file__).parent / "PRIVACY.md"
        if not privacy_path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(privacy_path, headers={**static_cache, "Content-Type": "text/plain; charset=utf-8"})

    doc_cache: dict[str, str] = {}

    async def doc(_: web.Request) -> web.Response:
        """Illustrated protocol documentation, fully self-contained.

        DOC.md is rendered once and cached; the vendored Bootstrap stylesheet is
        inlined, so the page makes zero external requests (no CDN, no fonts).
        """
        cached = doc_cache.get("html")
        if cached is not None:
            return web.Response(text=cached, headers={**static_cache, "Content-Type": "text/html; charset=utf-8"})
        doc_path = Path(__file__).parent / "DOC.md"
        if not doc_path.is_file():
            raise web.HTTPNotFound()
        if markdown is None:
            raise web.HTTPInternalServerError(text="markdown package is required to serve the documentation")
        body = markdown.markdown(doc_path.read_text(encoding="utf-8"), output_format="html5", extensions=["tables", "fenced_code"])
        body = body.replace("<table>", '<table class="table table-striped table-sm">')
        css = (Path(__file__).parent / "bootstrap.min.css").read_text(encoding="utf-8")
        page = (
            "<!DOCTYPE html>"
            '<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>PagerMCP protocol documentation</title>"
            f"<style>{css}</style></head>"
            '<body><main class="container py-4" style="max-width: 960px">'
            f"{body}</main></body></html>"
        )
        doc_cache["html"] = page
        return web.Response(text=page, headers={**static_cache, "Content-Type": "text/html; charset=utf-8"})

    async def sample(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        sample_dir = os.path.join(base_dir, "sample")
        filepath = os.path.abspath(os.path.join(sample_dir, name))
        if not name or not filepath.startswith(os.path.abspath(sample_dir) + os.sep) or not os.path.isfile(filepath):
            raise web.HTTPNotFound()
        return web.FileResponse(filepath, headers={**static_cache, "Content-Type": "application/x-sh"})

    async def new_mailbox(_: web.Request) -> web.Response:
        token = str(uuid.uuid4())
        store.touch(token)
        return web.json_response({"uuid": token})

    def page_data(page: Any) -> dict[str, Any]:
        return {"id": page.id, "created_at": page.created_at, "age_seconds": max(0, int(time.time() - page.created_at)), **page.payload, "acked": page.acked}

    def take_fresh(token: str, max_age: float | None) -> Any:
        # Return the oldest *unacked* page within the age window without removing
        # it. Pages outside the window are discarded so a stale page cannot pin the
        # head of the queue forever. Removal of a delivered page is the client's
        # job via /ack, which makes delivery at-least-once: a waiter that dies
        # before acknowledging cannot swallow the page.
        now = time.time()
        for page in store.unacked(token):
            if max_age is not None and now - page.created_at > max_age:
                store.remove(page.id)
                continue
            return page
        return None

    def drop_waiter(token: str, future: asyncio.Future[dict[str, Any]]) -> None:
        bucket = waiters.get(token)
        if bucket and future in bucket:
            bucket.remove(future)
        if bucket is not None and not bucket:
            waiters.pop(token, None)

    def fan_out(token: str, page: Any) -> None:
        data = page_data(page)
        # Wake blocked waiters, but leave the page in the queue. It is removed
        # only when a waiter acknowledges it, so a connection that dies between
        # this notify and the response cannot lose the message.
        for waiter in [w for w in waiters.pop(token, []) if not w.done()]:
            waiter.set_result(data)
        # Push the same page to any live SSE subscribers. A full queue marks the
        # connection overflowed so it is closed and the client reconnects, which
        # replays the still-queued page from the backlog.
        for subscriber in list(subscribers.get(token, ())):
            try:
                subscriber.queue.put_nowait(data)
            except asyncio.QueueFull:
                subscriber.overflowed.set()
        # Fire a data-only trigger to registered push subscriptions. It carries no
        # message content: it only wakes the device, which then fetches the page
        # over SSE or /wait. Best-effort, so a slow or dead endpoint never blocks
        # or loses the page (it stays queued until acked).
        session = push_state["session"]
        if session is not None and not session.closed:
            for subscription in store.subscriptions_for(token):
                if subscription.channel not in push_channels:
                    continue
                task = asyncio.create_task(push_trigger(session, token, subscription, data))
                push_state["tasks"].add(task)
                task.add_done_callback(push_state["tasks"].discard)

    async def notify(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > max_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=request.content_length)
        try:
            raw = await request.read()
            if len(raw) > max_bytes:
                raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=len(raw))
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or payload.pop("uuid", "")
        if payload.get("p2p") and p2p_keyring is not None:
            # Only the P2P path requires encryption: relays must not see the
            # destination UUID. The server decrypts to route; failure = 400.
            if token:
                raise web.HTTPBadRequest(text="p2p requires mailbox_encrypted (GPG-encrypted destination UUID), not a plain UUID")
            armored = payload.pop("mailbox_encrypted", None)
            if not isinstance(armored, str) or not armored.strip():
                raise web.HTTPBadRequest(text="mailbox_encrypted is required for p2p (encrypt the destination UUID with p2p.public_key from /settings)")
            key_id = payload.pop("key_id", None)
            if not isinstance(key_id, str) or not key_id.strip():
                raise web.HTTPBadRequest(text="key_id is required with mailbox_encrypted (the fingerprint of the public key used)")
            if not p2p_keyring.has_key(key_id):
                # Not our key: fingerprints are exchanged while peering, so a
                # known peer gets a direct 302 without any extra lookup. The
                # WHO HAS fingerprint query remains as a fallback for unknowns.
                if mesh.enabled:
                    target = mesh.instance_for_fingerprint(key_id)
                    if target:
                        return web.Response(status=302, headers={"Location": target + "/notify"})
                    session = push_state["session"]
                    if session is not None and not session.closed:
                        target = await mesh.query(session, "fingerprint", key_id)
                        if target:
                            return web.Response(status=302, headers={"Location": target + "/notify"})
                raise web.HTTPBadRequest(text="unknown key_id for this instance")
            token = p2p_keyring.decrypt(armored)
            if not token:
                raise web.HTTPBadRequest(text="cannot decrypt mailbox_encrypted")
        if not token:
            raise web.HTTPUnauthorized(text="UUID is required")
        try:
            uuid.UUID(token)
        except ValueError as exc:
            raise web.HTTPUnauthorized(text="invalid UUID") from exc

        if payload.get("p2p"):
            # Opt-in idempotent delivery for P2P store-and-forward clients: several
            # peers may post the same envelope. Duplicates are recognised per
            # (mailbox, p2p_id) and never create a second message or notification.
            if not p2p_enabled:
                raise web.HTTPForbidden(text="p2p dedupe is not enabled")
            p2p_id = payload.get("p2p_id")
            try:
                uuid.UUID(str(p2p_id))
            except (ValueError, TypeError) as exc:
                raise web.HTTPBadRequest(text="p2p_id must be a UUID") from exc
            expires_raw = payload.get("p2p_expires_at")
            if isinstance(expires_raw, bool) or not isinstance(expires_raw, (int, float)):
                raise web.HTTPBadRequest(text="p2p_expires_at is required (unix seconds)")
            now = time.time()
            message_expires = float(expires_raw)
            if now >= message_expires:
                raise web.HTTPGone(text="p2p message expired")
            max_age = min(p2p_max_age, store.message_ttl) if store.message_ttl else p2p_max_age
            if max_age and message_expires - now > max_age:
                raise web.HTTPBadRequest(text="p2p_expires_at exceeds the maximum message age")
            core = {key: value for key, value in payload.items() if key not in ("p2p", "p2p_id", "p2p_created_at", "p2p_expires_at", "p2p_payload_sha256")}
            digest = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            cache_expires = message_expires
            if p2p_dedupe_ttl:
                cache_expires = min(cache_expires, now + p2p_dedupe_ttl)
            if store.message_ttl:
                cache_expires = min(cache_expires, now + store.message_ttl)
            status, result = store.p2p_enqueue(token, str(p2p_id), payload, digest, cache_expires)
            if status == "duplicate":
                return web.json_response({"id": result, "duplicate": True})
            if status == "conflict":
                raise web.HTTPConflict(text="p2p_id already used with a different payload")
            if status == "full":
                raise web.HTTPTooManyRequests(text="mailbox is full")
            fan_out(token, result)
            return web.json_response({"id": result.id, "duplicate": False})

        page = store.enqueue(token, payload)
        if page is None:
            raise web.HTTPTooManyRequests(text="mailbox is full")
        fan_out(token, page)
        return web.json_response({"accepted": True, "id": page.id}, status=202)

    async def pending(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        order = request.query.get("order", "oldest")
        if order not in ("oldest", "asc", "newest", "desc"):
            raise web.HTTPBadRequest(text="order must be oldest/asc or newest/desc")
        pages = store.pending(token)
        if order in ("newest", "desc"):
            pages = list(reversed(pages))
        return web.json_response({"messages": [page_data(page) for page in pages]})
    async def consume(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        page = store.pop(token)
        return web.json_response({"message": (page_data(page) if page else None)})

    async def ack(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        page_id = None
        if request.can_read_body:
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                body = None
            if isinstance(body, dict):
                page_id = body.get("id")
        if store.webclient_active(token):
            # Webclient retention: acknowledge but keep a copy in the queue until a
            # new message arrives (or the mode lease lapses).
            retained = store.retain(token, str(page_id) if page_id else None)
            return web.json_response({"acked": retained is not None, "retained": retained is not None, "id": retained.id if retained else (str(page_id) if page_id else None)})
        if page_id:
            store.remove(str(page_id))
        else:
            oldest = store.pop(token)
            page_id = oldest.id if oldest else None
        return web.json_response({"acked": page_id is not None, "retained": False, "id": page_id})

    async def get_mode(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        expires = store.webclient_expires(token)
        active = expires is not None and expires > time.time()
        return web.json_response({"webclient": active, "expires": expires if active else None})

    async def set_mode(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("webclient"), bool):
            raise web.HTTPBadRequest(text="webclient must be true or false")
        if body["webclient"]:
            expires = time.time() + webclient_ttl
            store.set_webclient(token, expires)
            return web.json_response({"webclient": True, "expires": expires})
        store.set_webclient(token, None)
        return web.json_response({"webclient": False, "expires": None})

    async def wait(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        raw_max_age = request.query.get("max_age_seconds")
        if raw_max_age:
            try:
                max_age = float(raw_max_age)
            except ValueError:
                raise web.HTTPBadRequest(text="max_age_seconds must be a number")
        else:
            max_age = None
        raw_timeout = request.query.get("timeout_seconds")
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError:
                raise web.HTTPBadRequest(text="timeout_seconds must be a number")
        else:
            timeout = float(os.getenv("WAIT_TIMEOUT_SECONDS", "0")) or None
        page = take_fresh(token, max_age)
        if page:
            return web.json_response(page_data(page))
        if max_conns > 0 and len(waiters.get(token, [])) + len(subscribers.get(token, ())) >= max_conns:
            raise web.HTTPTooManyRequests(text="too many open connections for this mailbox")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiters.setdefault(token, []).append(future)
        try:
            return web.json_response(await asyncio.wait_for(future, timeout))
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout(text="wait timed out")
        finally:
            drop_waiter(token, future)

    async def events(request: web.Request) -> web.StreamResponse:
        """Server-Sent Events stream: push each page as it arrives.

        Push is a wake-up, not a new delivery guarantee: the page stays queued
        until the client acks, exactly as with /wait, and the backlog is replayed
        on every (re)connect so a dropped stream never loses a message.
        """
        token = request.match_info["token"]
        store.touch(token)
        if max_conns > 0 and len(waiters.get(token, [])) + len(subscribers.get(token, ())) >= max_conns:
            raise web.HTTPTooManyRequests(text="too many open connections for this mailbox")
        keepalive = env_float("SSE_KEEPALIVE_SECONDS", 15) or 15
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        subscriber = Subscriber(int(os.getenv("SSE_QUEUE_MAX", "256")))
        subscribers.setdefault(token, set()).add(subscriber)
        try:
            await response.write(b"retry: 3000\n\n")
            for page in store.pending(token):
                await response.write(sse_frame(page_data(page)))
            while not subscriber.overflowed.is_set():
                try:
                    data = await asyncio.wait_for(subscriber.queue.get(), keepalive)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                await response.write(sse_frame(data))
        except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
            pass
        finally:
            bucket = subscribers.get(token)
            if bucket:
                bucket.discard(subscriber)
                if not bucket:
                    subscribers.pop(token, None)
        return response

    def ntfy_headers(title: str) -> dict[str, str]:
        headers = {"Title": title, "Tags": "pager"}
        if ntfy_token:
            headers["Authorization"] = f"Bearer {ntfy_token}"
        return headers

    async def fcm_access_token(session: ClientSession) -> str | None:
        state = push_state["fcm"]
        now = time.time()
        if state["token"] and state["expires"] > now + 30:
            return state["token"]
        account = state["account"]
        if not account:
            return None
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        def b64(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = b64(json.dumps({"iss": account["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging", "aud": fcm_token_url, "iat": int(now), "exp": int(now) + 3600}).encode())
        signing_input = header + b"." + claims
        key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
        assertion = (signing_input + b"." + b64(key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))).decode()
        try:
            async with session.post(fcm_token_url, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}, timeout=ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
        except Exception as _e:
            import traceback; traceback.print_exc()
            return None
        state["token"] = payload.get("access_token")
        state["expires"] = now + float(payload.get("expires_in", 3600))
        return state["token"]

    async def fcm_send(session: ClientSession, subscription: Any, kind: str, mailbox: str, data: dict[str, Any]) -> int | None:
        access = await fcm_access_token(session)
        project = fcm_project or (push_state["fcm"]["account"] or {}).get("project_id")
        if not access or not project:
            return None
        message: dict[str, Any] = {"token": subscription.endpoint, "android": {"priority": "high"}}
        if kind == "page":
            message["data"] = {"type": "page", "id": str(data.get("id", ""))}
            if hash_enabled:
                message["data"]["mailbox_hash"] = _mailbox_hash_hex(mailbox, hash_period)
            else:
                message["data"]["mailbox"] = mailbox
            if fcm_include_payload:
                message["notification"] = {"title": "PagerMCP", "body": str(data.get("message", "New page"))}
        else:
            message["data"] = {"type": "warning", "reason": "subscription-expiring"}
            if hash_enabled:
                message["data"]["mailbox_hash"] = _mailbox_hash_hex(mailbox, hash_period)
        try:
            async with session.post(f"{fcm_base}/v1/projects/{project}/messages:send", json={"message": message}, headers={"Authorization": f"Bearer {access}"}, timeout=ClientTimeout(total=10), allow_redirects=False) as response:
                return response.status
        except Exception as _e:
            import traceback; traceback.print_exc()
            return None

    async def push_trigger(session: ClientSession, token: str, subscription: Any, data: dict[str, Any]) -> None:
        try:
            timeout = ClientTimeout(total=10)
            if subscription.channel == "fcm":
                status = await fcm_send(session, subscription, "page", token, data)
            elif subscription.channel == "ntfy":
                ntfy_body = "New page"
                if hash_enabled:
                    ntfy_body += f"\nmailbox_hash: {_mailbox_hash_hex(token, hash_period)}"
                else:
                    ntfy_body = f"New page in mailbox {token}"
                async with session.post(subscription.endpoint, data=ntfy_body.encode(), headers=ntfy_headers("PagerMCP"), timeout=timeout, allow_redirects=False) as response:
                    status = response.status
            else:
                # Re-validate at push time (DNS answers can change since registration).
                error = await _endpoint_error(subscription.endpoint, push_allow_hosts, push_allow_nets)
                if error:
                    return
                body = {"type": "page", "id": data.get("id"), "created_at": data.get("created_at")}
                if hash_enabled:
                    body["mailbox_hash"] = _mailbox_hash_hex(token, hash_period)
                else:
                    body["mailbox"] = token
                async with session.post(subscription.endpoint, json=body, timeout=timeout, allow_redirects=False) as response:
                    status = response.status
            remove_on = (400, 404, 410) if subscription.channel == "fcm" else (404, 410)
            if status is not None and status in remove_on:
                store.remove_subscription(token, subscription.id)
        except Exception as _e:
            import traceback; traceback.print_exc()
            pass

    async def push_warning(session: ClientSession, subscription: Any) -> None:
        """Warn a subscription that its lease is about to end, so the client can renew before push turns itself off."""
        try:
            timeout = ClientTimeout(total=10)
            if subscription.channel == "fcm":
                status = await fcm_send(session, subscription, "warning", subscription.mailbox, {})
            elif subscription.channel == "ntfy":
                ntfy_body = "PagerMCP: push expires soon"
                if hash_enabled:
                    ntfy_body += f"\nmailbox_hash: {_mailbox_hash_hex(subscription.mailbox, hash_period)}"
                async with session.post(subscription.endpoint, data=ntfy_body.encode(), headers=ntfy_headers("PagerMCP"), timeout=timeout, allow_redirects=False) as response:
                    status = response.status
            else:
                error = await _endpoint_error(subscription.endpoint, push_allow_hosts, push_allow_nets)
                if error:
                    return
                body = {"type": "warning", "reason": "subscription-expiring", "expires": subscription.expires}
                if hash_enabled:
                    body["mailbox_hash"] = _mailbox_hash_hex(subscription.mailbox, hash_period)
                else:
                    body["mailbox"] = subscription.mailbox
                async with session.post(subscription.endpoint, json=body, timeout=timeout, allow_redirects=False) as response:
                    status = response.status
            if status is not None and status in ((400, 404, 410) if subscription.channel == "fcm" else (404, 410)):
                store.remove_subscription(subscription.mailbox, subscription.id)
            elif status is not None and status < 400:
                store.mark_warned(subscription.id)
        except Exception as _e:
            import traceback; traceback.print_exc()
            pass

    def resolve_target(channel: str, body: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        if channel == "ntfy":
            if not ntfy_url:
                raise web.HTTPBadRequest(text="ntfy channel is not configured (set NTFY_URL)")
            topic = body.get("topic")
            if not topic or not re.match(r"^[A-Za-z0-9_-]{1,64}$", str(topic)):
                raise web.HTTPBadRequest(text="a valid topic is required for the ntfy channel")
            return f"{ntfy_url}/{topic}", {"topic": str(topic)}
        if channel == "fcm":
            if not fcm_account:
                raise web.HTTPBadRequest(text="fcm channel is not configured (set FCM_CREDENTIALS)")
            device = body.get("token")
            if not device or not re.match(r"^[A-Za-z0-9_:\-]{10,4096}$", str(device)):
                raise web.HTTPBadRequest(text="a device token is required for the fcm channel")
            return str(device), None
        endpoint = body.get("endpoint")
        if not endpoint:
            raise web.HTTPBadRequest(text="endpoint is required")
        if not re.match(r"^https?://", str(endpoint)):
            raise web.HTTPBadRequest(text="endpoint must be an http(s) URL")
        keys = body.get("keys")
        return str(endpoint), (keys if isinstance(keys, dict) else None)

    async def add_subscription(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        existing = body.get("id")
        channel = str(body.get("channel") or "")
        value = body.get("token") if channel == "fcm" else body.get("topic") if channel == "ntfy" else body.get("endpoint")

        if existing:
            # Renew; overwrite the stored endpoint/token only when a non-empty new value is
            # given (an empty value means "leave it as it is"). The value is write-only: it is
            # never echoed back. The id comes from the create reply.
            if not value:
                subscription = store.renew_subscription(token, str(existing))
            else:
                endpoint, keys = resolve_target(channel, body)
                if channel not in ("ntfy", "fcm"):
                    error = await _endpoint_error(endpoint, push_allow_hosts, push_allow_nets)
                    if error:
                        raise web.HTTPBadRequest(text=error)
                subscription = store.update_subscription(token, str(existing), endpoint, keys)
            if subscription is None:
                raise web.HTTPNotFound(text="subscription not found")
            return web.json_response({"id": subscription.id, "channel": subscription.channel, "expires": subscription.expires})

        if not channel:
            raise web.HTTPBadRequest(text="channel is required")
        endpoint, keys = resolve_target(channel, body)
        if channel not in ("ntfy", "fcm"):
            error = await _endpoint_error(endpoint, push_allow_hosts, push_allow_nets)
            if error:
                raise web.HTTPBadRequest(text=error)
        subscription = store.add_subscription(token, channel, endpoint, keys)
        if subscription is None:
            raise web.HTTPTooManyRequests(text="too many subscriptions for this mailbox")
        return web.json_response({"id": subscription.id, "channel": subscription.channel, "expires": subscription.expires}, status=201)

    async def list_subscriptions(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        # Never return the endpoint/token: for `ntfy` it holds the (secret) topic
        # and for `fcm` it is the device token. The id is all a client needs to
        # renew or delete.
        return web.json_response({"subscriptions": [{"id": s.id, "channel": s.channel, "expires": s.expires} for s in store.subscriptions_for(token)]})

    async def remove_subscription(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        return web.json_response({"removed": store.remove_subscription(token, request.match_info["sub_id"])})

    async def remove_subscriptions(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        return web.json_response({"removed": store.remove_subscriptions(token, request.query.get("channel"))})

    async def mailbox_status(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        subscriptions = store.subscriptions_for(token)
        mode_expires = store.webclient_expires(token)
        return web.json_response({
            "mailbox": token,
            "messages": len(store.pending(token)),
            "unacked": len(store.unacked(token)),
            "push": {
                "enabled": bool(subscriptions),
                "subscriptions": len(subscriptions),
                "channels": sorted({sub.channel for sub in subscriptions}),
                "next_expiry": min((sub.expires for sub in subscriptions), default=None),
            },
            "webclient": {"enabled": mode_expires is not None, "expires": mode_expires},
            "retention": {"message_ttl_seconds": store.message_ttl, "mailbox_ttl_seconds": store.mailbox_ttl},
        })

    async def warn_expiring() -> None:
        if warn_seconds <= 0:
            return
        session = push_state["session"]
        for subscription in store.subscriptions_nearing_expiry(time.time(), warn_seconds):
            if session is not None and not session.closed and subscription.channel in push_channels:
                task = asyncio.create_task(push_warning(session, subscription))
                push_state["tasks"].add(task)
                task.add_done_callback(push_state["tasks"].discard)

    async def reaper() -> None:
        while True:
            await asyncio.sleep(cleanup_interval or 3600)
            await warn_expiring()
            store.purge()

    async def on_startup(_: web.Application) -> None:
        push_state["session"] = ClientSession()
        if cleanup_interval:
            push_state["reaper"] = asyncio.create_task(reaper())

    async def on_cleanup(_: web.Application) -> None:
        if push_state["reaper"] is not None:
            push_state["reaper"].cancel()
        for task in list(push_state["tasks"]):
            task.cancel()
        if push_state["session"] is not None:
            await push_state["session"].close()

    app = web.Application(client_max_size=max_bytes)
    app.middlewares.append(mesh_discovery)
    app.add_routes([web.post("/mesh/query", mesh_query), web.get("/healthz", health), web.get("/version", version), web.get("/settings", settings_endpoint), web.get("/", root), web.get("/rfc", rfc), web.get("/privacy", privacy), web.get("/doc", doc), web.get("/sample/{name}", sample), web.post("/mailboxes", new_mailbox), web.post("/notify", notify), web.get("/mailboxes/{token}/messages", pending), web.post("/mailboxes/{token}/consume", consume), web.post("/mailboxes/{token}/ack", ack), web.get("/mailboxes/{token}/wait", wait), web.get("/mailboxes/{token}/events", events), web.post("/mailboxes/{token}/subscriptions", add_subscription), web.get("/mailboxes/{token}/subscriptions", list_subscriptions), web.delete("/mailboxes/{token}/subscriptions/{sub_id}", remove_subscription), web.delete("/mailboxes/{token}/subscriptions", remove_subscriptions), web.get("/mailboxes/{token}/status", mailbox_status), web.get("/mailboxes/{token}/mode", get_mode), web.post("/mailboxes/{token}/mode", set_mode)])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class _UuidMaskFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _UUID_RE.sub("<uuid>", super().format(record))


def configure_access_log() -> logging.Logger | None:
    """Optional, rotated access log for debugging.

    Only active when PAGER_LOG_FILE is set. The UUID (the bearer credential) is
    masked, and the file auto-rotates daily with PAGER_LOG_RETENTION_DAYS (default
    3) kept, so the exposure window stays small.
    """
    log_file = os.getenv("PAGER_LOG_FILE", "")
    if not log_file:
        return None
    days = int(os.getenv("PAGER_LOG_RETENTION_DAYS", "3"))
    handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=days)
    handler.setFormatter(_UuidMaskFormatter('%(asctime)s %(message)s'))
    logger = logging.getLogger("aiohttp.access")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def drop_privileges() -> None:
    """Root at start (backwards compatible), then run as an unprivileged user.

    Fixes ownership of DATA_DIR (including an existing SQLite database from an
    older, root-run image) and of an existing access log, then drops to
    PAGER_UID/PAGER_GID (default: nobody, 65534). A no-op when the process
    already runs unprivileged (e.g. the Bastille/Pot jails, which start as the
    `pagermcp` user).
    """
    if os.geteuid() != 0:
        return
    uid = int(os.getenv("PAGER_UID", "65534"))
    gid = int(os.getenv("PAGER_GID", "65534"))
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        os.chown(data_dir, uid, gid)
        for entry in os.scandir(data_dir):
            try:
                os.chown(entry.path, uid, gid)
            except OSError:
                pass
    log_file = os.getenv("PAGER_LOG_FILE", "")
    if log_file and os.path.exists(log_file):
        try:
            os.chown(log_file, uid, gid)
        except OSError:
            pass
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    drop_privileges()
    access_log = configure_access_log()
    web.run_app(build_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")), access_log=access_log)


if __name__ == "__main__":
    main()
