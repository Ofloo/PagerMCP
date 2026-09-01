from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from aiohttp import web

from .storage import MailboxStore


def env_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    return value * 86400 if name.endswith("_DAYS") else value


def build_app() -> web.Application:
    max_bytes = int(os.getenv("MAX_MESSAGE_BYTES", "65536"))
    store = MailboxStore(os.getenv("DATA_DIR") or None, env_float("MAILBOX_TTL_DAYS", 30), env_float("MESSAGE_TTL_DAYS", 7), int(os.getenv("MAX_MESSAGES_PER_MAILBOX", "128")))
    waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def new_mailbox(_: web.Request) -> web.Response:
        token = str(uuid.uuid4())
        store.touch(token)
        return web.json_response({"uuid": token})

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
        for waiter in waiters.pop(token, []):
            if not waiter.done():
                waiter.set_result({"id": page.id, **page.payload})
        return web.json_response({"accepted": True, "id": page.id}, status=202)

    async def pending(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        store.touch(token)
        return web.json_response({"messages": [{"id": page.id, **page.payload} for page in store.pending(token)]})

    async def consume(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        page = store.pop(token)
        return web.json_response({"message": ({"id": page.id, **page.payload} if page else None)})

    async def wait(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        page = store.pop(token)
        if page:
            return web.json_response({"id": page.id, **page.payload})
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiters.setdefault(token, []).append(future)
        try:
            return web.json_response(await asyncio.wait_for(future, float(os.getenv("WAIT_TIMEOUT_SECONDS", "0")) or None))
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout(text="wait timed out")

    app = web.Application(client_max_size=max_bytes)
    app.add_routes([web.get("/healthz", health), web.post("/mailboxes", new_mailbox), web.post("/notify", notify), web.get("/mailboxes/{token}/messages", pending), web.post("/mailboxes/{token}/consume", consume), web.get("/mailboxes/{token}/wait", wait)])
    return app


def main() -> None:
    web.run_app(build_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
