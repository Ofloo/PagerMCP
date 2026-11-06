"""Federated mailbox/key discovery over a peer mesh (the WHO HAS protocol).

Each instance is standalone by default. With PAGER_PEERS set, an unknown
mailbox UUID (or an unknown P2P key fingerprint) triggers a WHO HAS query to
the configured peers, forwarded with a hop limit and a query-id seen-set so it
cannot loop. When a peer owns the value, the client gets a 302 to that
instance; when nobody owns it, the mailbox is simply created locally.

Privacy: only period-rotating hashes travel the mesh, never raw UUIDs. Message
payloads never leave the owning instance. A static and a dynamic (rate-based)
blacklist keep abusive peers out.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any

from aiohttp import ClientSession, ClientTimeout


def _split(raw: str) -> list[str]:
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


class Mesh:
    def __init__(
        self,
        peers: str,
        blacklist: str,
        public_url: str,
        hash_period: int = 86400,
        max_hops: int = 3,
        max_qpm: int = 120,
        blacklist_seconds: int = 3600,
    ) -> None:
        self.peers = _split(peers)
        self.static_blacklist = set(_split(blacklist))
        self.public_url = public_url.rstrip("/")
        self.hash_period = hash_period
        self.max_hops = max_hops
        self.max_qpm = max_qpm
        self.blacklist_seconds = blacklist_seconds
        self.seen_queries: dict[str, float] = {}
        self.query_times: dict[str, list[float]] = {}
        self.dynamic_blacklist: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.peers)

    def hash_for(self, value: str) -> str:
        period = int(time.time() // self.hash_period) if self.hash_period else 0
        return hashlib.sha256(f"{value}:{period}".encode()).hexdigest()

    def _peer_host(self, peer: str) -> str:
        return peer.split("://")[-1].split("/")[0]

    def is_blacklisted(self, peer: str) -> bool:
        if peer in self.static_blacklist or self._peer_host(peer) in self.static_blacklist:
            return True
        until = self.dynamic_blacklist.get(peer) or self.dynamic_blacklist.get(self._peer_host(peer), 0)
        return bool(until and until > time.time())

    def note_query(self, peer: str) -> bool:
        """Track per-peer query rates; abusive peers are blacklisted for a while."""
        if self.is_blacklisted(peer):
            return False
        now = time.time()
        times = [t for t in self.query_times.get(peer, []) if t > now - 60]
        times.append(now)
        self.query_times[peer] = times
        if self.max_qpm and len(times) > self.max_qpm:
            self.dynamic_blacklist[peer] = now + self.blacklist_seconds
            return False
        return True

    def _seen(self, query_id: str) -> bool:
        now = time.time()
        self.seen_queries = {qid: ts for qid, ts in self.seen_queries.items() if ts > now - 3600}
        if query_id in self.seen_queries:
            return True
        self.seen_queries[query_id] = now
        return False

    async def query(self, session: ClientSession, kind: str, value: str) -> str | None:
        """Ask the mesh WHO HAS <hash of value>; return the owner's base URL."""
        if not self.enabled:
            return None
        query_id = str(uuid.uuid4())
        self._seen(query_id)  # never process our own query when it comes back
        body = {
            "kind": kind,
            "hash": self.hash_for(value),
            "query_id": query_id,
            "hops": self.max_hops,
            "origin": self.public_url,
        }
        return await self._ask_peers(session, body, exclude=None)

    async def _ask_peers(self, session: ClientSession, body: dict[str, Any], exclude: str | None) -> str | None:
        for peer in self.peers:
            if peer == exclude or self.is_blacklisted(peer):
                continue
            try:
                async with session.post(f"{peer}/mesh/query", json=body, timeout=ClientTimeout(total=8), allow_redirects=False) as response:
                    if response.status != 200:
                        continue
                    data = await response.json()
                    if data.get("found") and data.get("instance"):
                        return str(data["instance"])
            except Exception:
                continue
        return None

    async def handle_query(self, session: ClientSession, body: dict[str, Any], owns) -> dict[str, Any]:
        """Answer a WHO HAS query: locally owned, forwarded, or not found."""
        kind = body.get("kind")
        digest = body.get("hash")
        query_id = str(body.get("query_id") or "")
        hops = int(body.get("hops") or 0)
        origin = str(body.get("origin") or "")
        if not kind or not digest or not query_id:
            return {"found": False}
        if owns(kind, digest):
            return {"found": True, "instance": self.public_url}
        if self._seen(query_id):
            return {"found": False, "seen": True}
        if hops <= 0:
            return {"found": False}
        forwarded = {**body, "hops": hops - 1, "origin": self.public_url}
        instance = await self._ask_peers(session, forwarded, exclude=origin)
        return {"found": True, "instance": instance} if instance else {"found": False}
