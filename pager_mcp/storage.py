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


class MailboxStore:
    def __init__(self, data_dir: str | None, mailbox_ttl: float, message_ttl: float, max_messages: int):
        self.mailbox_ttl = mailbox_ttl
        self.message_ttl = message_ttl
        self.max_messages = max_messages
        self.lock = threading.RLock()
        self.db: sqlite3.Connection | None = None
        self.mailboxes: dict[str, tuple[float, list[Page]]] = {}
        if data_dir:
            path = Path(data_dir)
            path.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path / "pager.sqlite3", check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS mailboxes (token TEXT PRIMARY KEY, last_seen REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, token TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)")
            self.db.commit()

    def _purge(self, now: float) -> None:
        cutoff_page = now - self.message_ttl if self.message_ttl else float("-inf")
        cutoff_box = now - self.mailbox_ttl if self.mailbox_ttl else float("-inf")
        if self.db:
            self.db.execute("DELETE FROM pages WHERE created < ?", (cutoff_page,))
            self.db.execute("DELETE FROM mailboxes WHERE last_seen < ? AND token NOT IN (SELECT DISTINCT token FROM pages)", (cutoff_box,))
            self.db.commit()
        else:
            for token, (last_seen, pages) in list(self.mailboxes.items()):
                pages[:] = [page for page in pages if page.created_at >= cutoff_page]
                if last_seen < cutoff_box and not pages:
                    del self.mailboxes[token]

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
