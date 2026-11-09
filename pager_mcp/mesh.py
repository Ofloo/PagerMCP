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
        # Fingerprints are exchanged during peering: no WHO HAS needed for them.
        self.peer_fingerprints: dict[str, str] = {}
        self.own_fingerprint: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.peers)

    def hash_for(self, value: str, at: float | None = None) -> str:
        # WHOHAS <unixtime> <sha256>: the hash is sha256("<value>:<period-index>")
        # computed with the unix time carried by the query, so the receiver can
        # recompute exactly the same hash (no guessing at period boundaries).
        at = time.time() if at is None else at
        period = int(at // self.hash_period) if self.hash_period else 0
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

    def note_peer_fingerprint(self, peer_url: str, fingerprint: str | None) -> None:
        if peer_url and fingerprint:
            # A peer's public instance URL may change (e.g. an internal IP becomes
            # a public HTTPS host): the newest mapping for that fingerprint wins,
            # so a 302 always points at the current public URL.
            peer_url = peer_url.rstrip("/")
            for url, fp in list(self.peer_fingerprints.items()):
                if fp == fingerprint and url != peer_url:
                    del self.peer_fingerprints[url]
            self.peer_fingerprints[peer_url] = fingerprint

    def instance_for_fingerprint(self, fingerprint: str) -> str | None:
        for peer_url, fp in self.peer_fingerprints.items():
            if fp == fingerprint:
                return peer_url
        return None

    async def query(self, session: ClientSession, kind: str, value: str) -> str | None:
        """Ask the mesh WHO HAS <value>; return the owner's base URL.

        Mailbox lookups carry a rotating hash (privacy); fingerprint lookups
        carry the fingerprint itself (a public key id, public by design).
        """
        if not self.enabled:
            return None
        query_id = str(uuid.uuid4())
        self._seen(query_id)  # never process our own query when it comes back
        started = self.max_hops
        if kind == "fingerprint":
            # WHOHAS <fingerprint> remains as a fallback for unknown peers;
            # normally fingerprints are already exchanged while peering.
            body = {"fingerprint": value, "query_id": query_id, "hops": self.max_hops, "started": started, "origin": self.public_url}
        else:
            now = time.time()
            # WHOHAS <unixtime> <sha256>: hash + the time it was computed with.
            body = {"time": now, "hash": self.hash_for(value, now), "query_id": query_id, "hops": self.max_hops, "started": started, "origin": self.public_url}
        if self.own_fingerprint:
            body["origin_fingerprint"] = self.own_fingerprint
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
                    # Peering: learn the responder's fingerprint AND its public
                    # instance URL (the field a 302 should point at), not merely
                    # the configured peer URL we happened to connect to.
                    self.note_peer_fingerprint(data.get("instance") or peer, data.get("fingerprint"))
                    if data.get("found") and data.get("instance"):
                        return str(data["instance"])
            except Exception:
                continue
        return None

    async def handle_query(self, session: ClientSession, body: dict[str, Any], owns) -> dict[str, Any]:
        """Answer a WHO HAS query: locally owned, forwarded, or not found."""
        query_id = str(body.get("query_id") or "")
        hops = int(body.get("hops") or 0)
        origin = str(body.get("origin") or "")
        self.note_peer_fingerprint(origin, body.get("origin_fingerprint"))
        fingerprint = body.get("fingerprint")
        digest = body.get("hash")
        at = body.get("time")
        started = int(body.get("started") or self.max_hops)
        if not query_id or (not fingerprint and not digest):
            return {"found": False}
        # Enforce the hop limit: an inflated hop count (a peer setting more than
        # the maximum) or an exhausted query (the "Nth hop" beyond the limit)
        # is denied, not silently answered with "not found".
        if hops > self.max_hops or hops <= 0:
            return {"found": False, "error": "hop limit exceeded"}
        # A peer must keep the hop bookkeeping in sync: the remaining budget can
        # never exceed what the query started with, otherwise the link is out
        # of sync and the query is rejected.
        if hops > started:
            return {"found": False, "error": "hops not in sync"}
        # The whole mesh uses ONE hop budget: a query from a server configured
        # with a different MESH_QUERY_HOPS would make the chain inconsistent,
        # so it is rejected as out of sync.
        if started != self.max_hops:
            return {"found": False, "error": "hops not in sync"}
        # Two query shapes: WHOHAS <fingerprint> (a public key id) and
        # WHOHAS <unixtime> <sha256> (a mailbox hash with its timestamp).
        # Only a recognised value yields "I have it".
        owned = owns("fingerprint", fingerprint, None) if fingerprint else owns("mailbox", digest, at)
        if owned:
            return {"found": True, "instance": self.public_url}
        if self._seen(query_id):
            return {"found": False, "seen": True}
        forwarded = {**body, "hops": hops - 1, "origin": self.public_url}
        instance = await self._ask_peers(session, forwarded, exclude=origin)
        return {"found": True, "instance": instance} if instance else {"found": False}
