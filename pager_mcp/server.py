from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

try:
    import markdown
except ImportError:
    markdown = None

from . import __build__, __version__
from .storage import MailboxStore


def env_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    return value * 86400 if name.endswith("_DAYS") else value


def build_app() -> web.Application:
    max_bytes = int(os.getenv("MAX_MESSAGE_BYTES", "65536"))
    store = MailboxStore(os.getenv("DATA_DIR") or None, env_float("MAILBOX_TTL_DAYS", 30), env_float("MESSAGE_TTL_DAYS", 7), int(os.getenv("MAX_MESSAGES_PER_MAILBOX", "128")))
    waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
    static_cache = {"Cache-Control": "max-age=3600"}
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def version(_: web.Request) -> web.Response:
        return web.json_response({"version": __version__, "build": __build__, "hostname": os.getenv("HOSTNAME", "")})

    async def root(_: web.Request) -> web.Response:
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
        return {"id": page.id, "created_at": page.created_at, "age_seconds": max(0, int(time.time() - page.created_at)), **page.payload}

    def take_fresh(token: str, max_age: float | None) -> Any:
        # Return the oldest queued page within the age window *without removing it*.
        # Pages outside the window are discarded (removed) so a stale page cannot
        # pin the head of the queue forever. Removal of a delivered page is the
        # client's job via /ack, which makes delivery at-least-once: a waiter that
        # dies before acknowledging cannot swallow the page.
        now = time.time()
        for page in store.pending(token):
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
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or payload.pop("uuid", "")
        if not token:
            raise web.HTTPUnauthorized(text="UUID is required")
        try:
            uuid.UUID(token)
        except ValueError as exc:
            raise web.HTTPUnauthorized(text="invalid UUID") from exc
        page = store.enqueue(token, payload)
        if page is None:
            raise web.HTTPTooManyRequests(text="mailbox is full")
        data = page_data(page)
        # Wake blocked waiters, but leave the page in the queue. It is removed
        # only when a waiter acknowledges it, so a connection that dies between
        # this notify and the response cannot lose the message.
        for waiter in [w for w in waiters.pop(token, []) if not w.done()]:
            waiter.set_result(data)
        return web.json_response({"accepted": True, "id": page.id}, status=202)

    async def pending(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        return web.json_response({"messages": [page_data(page) for page in store.pending(token)]})
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
        if page_id:
            store.remove(str(page_id))
        else:
            oldest = store.pop(token)
            page_id = oldest.id if oldest else None
        return web.json_response({"acked": page_id is not None, "id": page_id})

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
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiters.setdefault(token, []).append(future)
        try:
            return web.json_response(await asyncio.wait_for(future, timeout))
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout(text="wait timed out")
        finally:
            drop_waiter(token, future)

    app = web.Application(client_max_size=max_bytes)
    app.add_routes([web.get("/healthz", health), web.get("/version", version), web.get("/", root), web.get("/rfc", rfc), web.get("/privacy", privacy), web.get("/sample/{name}", sample), web.post("/mailboxes", new_mailbox), web.post("/notify", notify), web.get("/mailboxes/{token}/messages", pending), web.post("/mailboxes/{token}/consume", consume), web.post("/mailboxes/{token}/ack", ack), web.get("/mailboxes/{token}/wait", wait)])
    return app


def main() -> None:
    web.run_app(build_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
