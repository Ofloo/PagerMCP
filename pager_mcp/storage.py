from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Page:
    id: str
    mailbox: str
    payload: dict[str, Any]
    created_at: float


@dataclass(frozen=True)
class Subscription:
    id: str
    mailbox: str
    channel: str
    endpoint: str
    keys: dict[str, Any] | None
    created: float
    renewed: float
    expires: float


class MailboxStore:
    def __init__(self, data_dir: str | None, mailbox_ttl: float, message_ttl: float, max_messages: int, subscription_ttl: float = 0, max_subscriptions: int = 16):
        self.mailbox_ttl = mailbox_ttl
        self.message_ttl = message_ttl
        self.max_messages = max_messages
        self.subscription_ttl = subscription_ttl
        self.max_subscriptions = max_subscriptions
        self.lock = threading.RLock()
        self.db: sqlite3.Connection | None = None
        self.mailboxes: dict[str, tuple[float, list[Page]]] = {}
        self.subscriptions: dict[str, list[Subscription]] = {}
        if data_dir:
            path = Path(data_dir)
            path.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path / "pager.sqlite3", check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS mailboxes (token TEXT PRIMARY KEY, last_seen REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, token TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS push_subscriptions (id TEXT PRIMARY KEY, token TEXT NOT NULL, channel TEXT NOT NULL, endpoint TEXT NOT NULL, keys TEXT, created REAL NOT NULL, renewed REAL NOT NULL, expires REAL NOT NULL)")
            self.db.commit()

    def _expiry(self, now: float) -> float:
        return now + self.subscription_ttl if self.subscription_ttl else now + 100 * 365 * 86400

    def _purge(self, now: float) -> None:
        cutoff_page = now - self.message_ttl if self.message_ttl else float("-inf")
        cutoff_box = now - self.mailbox_ttl if self.mailbox_ttl else float("-inf")
        if self.db:
            self.db.execute("DELETE FROM pages WHERE created < ?", (cutoff_page,))
            self.db.execute("DELETE FROM mailboxes WHERE last_seen < ? AND token NOT IN (SELECT DISTINCT token FROM pages)", (cutoff_box,))
            self.db.execute("DELETE FROM push_subscriptions WHERE expires <= ?", (now,))
            self.db.execute("DELETE FROM push_subscriptions WHERE token NOT IN (SELECT token FROM mailboxes) AND token NOT IN (SELECT DISTINCT token FROM pages)")
            self.db.commit()
        else:
            for token, (last_seen, pages) in list(self.mailboxes.items()):
                pages[:] = [page for page in pages if page.created_at >= cutoff_page]
                if last_seen < cutoff_box and not pages:
                    del self.mailboxes[token]
            for token in list(self.subscriptions):
                if token not in self.mailboxes:
                    del self.subscriptions[token]
                    continue
                self.subscriptions[token] = [sub for sub in self.subscriptions[token] if sub.expires > now]
                if not self.subscriptions[token]:
                    del self.subscriptions[token]

    def touch(self, token: str) -> None:
        with self.lock:
            now = time.time()
            self._purge(now)
            if self.db:
                self.db.execute("INSERT INTO mailboxes(token,last_seen) VALUES(?,?) ON CONFLICT(token) DO UPDATE SET last_seen=excluded.last_seen", (token, now))
                self.db.commit()
            else:
                self.mailboxes.setdefault(token, (now, []))
                self.mailboxes[token] = (now, self.mailboxes[token][1])

    def purge(self) -> None:
        """Expire mailboxes, messages and push subscriptions now.

        Called by the periodic reaper so an idle server sheds forgotten
        accounts and dead subscriptions even when no request ever arrives.
        """
        with self.lock:
            self._purge(time.time())

    def enqueue(self, token: str, payload: dict[str, Any]) -> Page | None:
        with self.lock:
            self.touch(token)
            now = time.time()
            page = Page(str(uuid.uuid4()), token, payload, now)
            if len(self.pending(token)) >= self.max_messages:
                return None
            if self.db:
                self.db.execute("INSERT INTO pages VALUES(?,?,?,?)", (page.id, token, json.dumps(payload), now))
                self.db.commit()
            else:
                last_seen, pages = self.mailboxes[token]
                pages.append(page)
                self.mailboxes[token] = (last_seen, pages)
            return page

    def pending(self, token: str) -> list[Page]:
        with self.lock:
            self._purge(time.time())
            if self.db:
                rows = self.db.execute("SELECT id,token,payload,created FROM pages WHERE token=? ORDER BY created,id", (token,)).fetchall()
                return [Page(row[0], row[1], json.loads(row[2]), row[3]) for row in rows]
            return list(self.mailboxes.get(token, (0, []))[1])

    def pop(self, token: str) -> Page | None:
        pages = self.pending(token)
        if not pages:
            return None
        page = pages[0]
        with self.lock:
            if self.db:
                self.db.execute("DELETE FROM pages WHERE id=?", (page.id,))
                self.db.commit()
            else:
                last_seen, stored = self.mailboxes[token]
                self.mailboxes[token] = (last_seen, stored[1:])
        return page

    def remove(self, page_id: str) -> None:
        with self.lock:
            if self.db:
                self.db.execute("DELETE FROM pages WHERE id=?", (page_id,))
                self.db.commit()
            else:
                for token, (last_seen, pages) in self.mailboxes.items():
                    self.mailboxes[token] = (last_seen, [page for page in pages if page.id != page_id])

    def add_subscription(self, token: str, channel: str, endpoint: str, keys: dict[str, Any] | None) -> Subscription | None:
        """Register a push subscription with a fresh lease. Returns None if the mailbox already has the maximum number of active subscriptions."""
        with self.lock:
            self.touch(token)
            now = time.time()
            expires = self._expiry(now)
            if len(self.subscriptions_for(token, now)) >= self.max_subscriptions:
                return None
            sub = Subscription(str(uuid.uuid4()), token, channel, endpoint, keys, now, now, expires)
            if self.db:
                self.db.execute("INSERT INTO push_subscriptions VALUES(?,?,?,?,?,?,?,?)", (sub.id, token, channel, endpoint, json.dumps(keys) if keys is not None else None, now, now, expires))
                self.db.commit()
            else:
                self.subscriptions.setdefault(token, []).append(sub)
            return sub

    def renew_subscription(self, token: str, sub_id: str) -> Subscription | None:
        """Extend the lease of an existing subscription. Returns None if not found."""
        with self.lock:
            now = time.time()
            expires = self._expiry(now)
            if self.db:
                row = self.db.execute("SELECT id,token,channel,endpoint,keys,created FROM push_subscriptions WHERE id=? AND token=?", (sub_id, token)).fetchone()
                if not row:
                    return None
                self.db.execute("UPDATE push_subscriptions SET renewed=?, expires=? WHERE id=?", (now, expires, sub_id))
                self.db.commit()
                return Subscription(row[0], row[1], row[2], row[3], json.loads(row[4]) if row[4] else None, row[5], now, expires)
            for index, sub in enumerate(self.subscriptions.get(token, [])):
                if sub.id == sub_id:
                    renewed = Subscription(sub.id, sub.mailbox, sub.channel, sub.endpoint, sub.keys, sub.created, now, expires)
                    self.subscriptions[token][index] = renewed
                    return renewed
            return None

    def subscriptions_for(self, token: str, now: float | None = None) -> list[Subscription]:
        """Active (non-expired) subscriptions for a mailbox."""
        moment = time.time() if now is None else now
        with self.lock:
            self._purge(time.time())
            if self.db:
                rows = self.db.execute("SELECT id,token,channel,endpoint,keys,created,renewed,expires FROM push_subscriptions WHERE token=? AND expires > ? ORDER BY created,id", (token, moment)).fetchall()
                return [Subscription(row[0], row[1], row[2], row[3], json.loads(row[4]) if row[4] else None, row[5], row[6], row[7]) for row in rows]
            return [sub for sub in self.subscriptions.get(token, []) if sub.expires > moment]

    def remove_subscription(self, token: str, sub_id: str) -> bool:
        with self.lock:
            if self.db:
                cursor = self.db.execute("DELETE FROM push_subscriptions WHERE id=? AND token=?", (sub_id, token))
                self.db.commit()
                return cursor.rowcount > 0
            for index, sub in enumerate(self.subscriptions.get(token, [])):
                if sub.id == sub_id:
                    del self.subscriptions[token][index]
                    return True
            return False

    def remove_subscriptions(self, token: str, channel: str | None = None) -> int:
        with self.lock:
            if self.db:
                if channel:
                    cursor = self.db.execute("DELETE FROM push_subscriptions WHERE token=? AND channel=?", (token, channel))
                else:
                    cursor = self.db.execute("DELETE FROM push_subscriptions WHERE token=?", (token,))
                self.db.commit()
                return cursor.rowcount
            current = self.subscriptions.get(token, [])
            if channel:
                kept = [sub for sub in current if sub.channel != channel]
                removed = len(current) - len(kept)
                if kept:
                    self.subscriptions[token] = kept
                else:
                    self.subscriptions.pop(token, None)
                return removed
            removed = len(current)
            self.subscriptions.pop(token, None)
            return removed
