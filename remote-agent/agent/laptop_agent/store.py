"""Chat context store. LOCAL ONLY: lives in the user's config dir on this laptop, never on the relay.

Every chat keeps its history, rolling notes (written by whichever agent is working on it), task events and
hand-over log, so ANY agent can take a chat over with full context."""
import os
import sqlite3
import threading
import time
from pathlib import Path

from .config import config_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  channel TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
  assigned TEXT, pinned TEXT, notes TEXT NOT NULL DEFAULT '', notes_by TEXT, notes_ts REAL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, who TEXT NOT NULL, agent TEXT,
  text TEXT NOT NULL, ts REAL NOT NULL, relay_id INTEGER, status TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_msg_chan ON messages (channel, id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, ts REAL NOT NULL, agent TEXT, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ev_chan ON events (channel, id);
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, ts REAL NOT NULL, from_agent TEXT, to_agent TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
  agent TEXT, channel TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
KEEP_MESSAGES = 2000
KEEP_EVENTS = 500


def default_title(channel: str) -> str:
    if channel == "app":
        return "General"
    if channel.startswith("local:"):
        return "API · " + channel[6:]
    if channel.startswith("relay:"):
        return "Relay · " + channel[6:]
    return "Chat " + channel.split(":", 1)[-1][:6]


class ChatStore:
    def __init__(self, path=None):
        p = Path(path) if path else config_dir() / "chats.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    def _q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _x(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # ---- chats
    def ensure_chat(self, channel: str, title: str | None = None) -> dict:
        now = time.time()
        with self._lock:
            self._x("INSERT OR IGNORE INTO chats (channel,title,created,updated) VALUES (?,?,?,?)",
                    (channel, (title or default_title(channel))[:60], now, now))
        return self.chat(channel)

    def chat(self, channel: str) -> dict | None:
        rows = self._q("SELECT * FROM chats WHERE channel=?", (channel,))
        return rows[0] if rows else None

    def chats(self) -> list[dict]:
        return self._q("SELECT * FROM chats ORDER BY updated DESC")

    def set_title(self, channel: str, title: str):
        self._x("UPDATE chats SET title=? WHERE channel=?", (title[:60], channel))

    def set_assigned(self, channel: str, agent: str | None):
        self._x("UPDATE chats SET assigned=? WHERE channel=?", (agent, channel))

    def set_pinned(self, channel: str, agent: str | None):
        self._x("UPDATE chats SET pinned=? WHERE channel=?", (agent, channel))

    def load_counts(self) -> dict:
        return {r["assigned"]: r["n"] for r in self._q(
            "SELECT assigned, COUNT(*) n FROM chats WHERE assigned IS NOT NULL GROUP BY assigned")}

    # ---- messages
    def add_message(self, channel: str, who: str, text: str, agent=None, relay_id=None, status="") -> int:
        now = time.time()
        with self._lock:
            cur = self._x("INSERT INTO messages (channel,who,agent,text,ts,relay_id,status) VALUES (?,?,?,?,?,?,?)",
                          (channel, who, agent, text, now, relay_id, status))
            self._x("UPDATE chats SET updated=? WHERE channel=?", (now, channel))
            self._x("DELETE FROM messages WHERE channel=? AND id <= (SELECT MAX(id) FROM messages WHERE channel=?) - ?",
                    (channel, channel, KEEP_MESSAGES))
            return cur.lastrowid

    def update_message(self, local_id: int, **fields):
        for k in fields:
            assert k in ("status", "relay_id")
        sets = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE messages SET {sets} WHERE id=?", (*fields.values(), local_id))

    def has_relay_message(self, relay_id: int, who: str) -> bool:
        return bool(self._q("SELECT 1 FROM messages WHERE relay_id=? AND who=?", (relay_id, who)))

    def messages(self, channel: str, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT * FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, limit))
        return list(reversed(rows))

    # ---- notes, events, handoffs
    def set_notes(self, channel: str, notes: str, agent: str | None):
        self._x("UPDATE chats SET notes=?, notes_by=?, notes_ts=?, updated=? WHERE channel=?",
                (notes[:4000], agent, time.time(), time.time(), channel))

    def add_event(self, channel: str, kind: str, text: str, agent: str | None = None):
        with self._lock:
            self._x("INSERT INTO events (channel,ts,agent,kind,text) VALUES (?,?,?,?,?)",
                    (channel, time.time(), agent, kind, text[:500]))
            self._x("DELETE FROM events WHERE channel=? AND id <= (SELECT MAX(id) FROM events WHERE channel=?) - ?",
                    (channel, channel, KEEP_EVENTS))

    def events(self, channel: str, limit: int = 15) -> list[dict]:
        return list(reversed(self._q("SELECT * FROM events WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, limit))))

    def add_handoff(self, channel: str, from_agent, to_agent, reason: str):
        self._x("INSERT INTO handoffs (channel,ts,from_agent,to_agent,reason) VALUES (?,?,?,?,?)",
                (channel, time.time(), from_agent, to_agent, reason[:200]))

    def handoffs(self, channel: str, limit: int = 5) -> list[dict]:
        return list(reversed(self._q("SELECT * FROM handoffs WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, limit))))

    # ---- media library (photos / videos / pages that agents sent or showed)
    def add_media(self, path: str, kind: str, agent=None, channel=None, source="") -> dict:
        with self._lock:
            self._x("INSERT INTO media (path,kind,ts,agent,channel,source) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET ts=excluded.ts, agent=excluded.agent, channel=excluded.channel, source=excluded.source",
                    (path, kind, time.time(), agent, channel, source))
        return self._q("SELECT * FROM media WHERE path=?", (path,))[0]

    def media(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        if kind:
            return self._q("SELECT * FROM media WHERE kind=? ORDER BY ts DESC LIMIT ?", (kind, limit))
        return self._q("SELECT * FROM media ORDER BY ts DESC LIMIT ?", (limit,))

    # ---- meta
    def get_meta(self, key: str, default=None):
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value):
        self._x("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    # ---- the context packet handed to an agent with every message
    def packet(self, channel: str, exclude_id: int | None = None, n_msgs: int = 30, n_events: int = 15,
               max_chars: int = 12000) -> dict:
        chat = self.chat(channel) or {}
        now = time.time()
        msgs = [m for m in self.messages(channel, n_msgs + 1) if m["id"] != exclude_id][-n_msgs:]
        pk = {
            "chat": {"channel": channel, "title": chat.get("title", "")},
            "notes": chat.get("notes", ""), "notes_by": chat.get("notes_by"),
            "recent_messages": [{"who": "user" if m["who"] == "you" else m["who"], "agent": m["agent"],
                                 "text": m["text"][:600], "age_s": int(now - m["ts"])} for m in msgs],
            "recent_events": [{"agent": e["agent"], "kind": e["kind"], "text": e["text"], "age_s": int(now - e["ts"])}
                              for e in self.events(channel, n_events)],
            "handoffs": [{"from": h["from_agent"], "to": h["to_agent"], "reason": h["reason"]} for h in self.handoffs(channel, 3)],
        }
        import json
        while len(json.dumps(pk)) > max_chars and pk["recent_messages"]:
            pk["recent_messages"].pop(0)           # drop the oldest messages first; notes always survive
        return pk
