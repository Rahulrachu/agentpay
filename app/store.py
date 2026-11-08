"""SQLite persistence for AgentPay.

One small file database (``DB_PATH``, default ``data/agentpay.db``) holds everything the app
would otherwise forget on restart:

* ``objects``: JSON documents keyed by (kind, id): checkout orders, pending approvals,
  multi-step plans, and (in mock mode) the fake PayPal's invoices/orders/refunds/payouts.
* ``audit``: an append-only log of every proposal, approval, decline, execution and failure,
  with a UTC timestamp and who approved it.
* ``meta``: small key/value settings (chat history, the mock ID counter).

``path=":memory:"`` gives a throwaway database, which is what the tests use.
Standard library only (sqlite3), so there is nothing extra to install.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    id         TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, id)
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    event       TEXT NOT NULL,
    tool        TEXT,
    summary     TEXT NOT NULL,
    approved_by TEXT,
    action_id   TEXT,
    plan_id     TEXT,
    why         TEXT,
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    """UTC timestamp as ISO 8601 ('2026-11-08T13:20:05Z'), built field by field so it is always parseable."""
    t = time.gmtime()
    return (f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T"
            f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}Z")


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path or ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    @property
    def persistent(self) -> bool:
        return self.path != ":memory:"

    # -- documents
    def put(self, kind: str, obj_id: str, data: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO objects (kind, id, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (kind, obj_id, json.dumps(data, default=str), utcnow()))

    def get(self, kind: str, obj_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT data FROM objects WHERE kind = ? AND id = ?", (kind, obj_id)).fetchone()
        return json.loads(row["data"]) if row else None

    def all(self, kind: str) -> list[dict]:
        """All documents of a kind, oldest first (insertion order is kept across updates)."""
        with self.lock:
            rows = self.conn.execute("SELECT data FROM objects WHERE kind = ? ORDER BY seq", (kind,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def delete(self, kind: str, obj_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM objects WHERE kind = ? AND id = ?", (kind, obj_id))

    def sync(self, kind: str, items: dict[str, dict]) -> None:
        """Make the stored set of `kind` equal to `items` (upsert all, delete the rest)."""
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                existing = {r["id"] for r in self.conn.execute("SELECT id FROM objects WHERE kind = ?", (kind,))}
                for gone in existing - set(items):
                    self.conn.execute("DELETE FROM objects WHERE kind = ? AND id = ?", (kind, gone))
                now = utcnow()
                for obj_id, data in items.items():
                    self.conn.execute(
                        "INSERT INTO objects (kind, id, data, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                        (kind, obj_id, json.dumps(data, default=str), now))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # -- meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    # -- audit log
    def add_audit(self, *, actor: str, event: str, summary: str, tool: str = "", approved_by: str = "",
                  action_id: str = "", plan_id: str = "", why: str = "", detail: Any = None) -> dict:
        row = {"at": utcnow(), "actor": actor, "event": event, "tool": tool or "", "summary": summary,
               "approved_by": approved_by or "", "action_id": action_id or "", "plan_id": plan_id or "",
               "why": why or "", "detail": detail}
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO audit (at, actor, event, tool, summary, approved_by, action_id, plan_id, why, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["at"], actor, event, row["tool"], summary, row["approved_by"], row["action_id"],
                 row["plan_id"], row["why"], json.dumps(detail, default=str) if detail is not None else None))
        row["id"] = cur.lastrowid
        return row

    def audit(self, limit: int = 200) -> list[dict]:
        """Newest first."""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    def ping(self) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1").fetchone()[0] == 1
