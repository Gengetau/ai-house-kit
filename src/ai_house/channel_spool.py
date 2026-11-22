"""Durable inbound spool, message_id deduplication, natural bubble splitting & restart recovery (`0.3.0`)."""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

SPOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_dedup (
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY(platform, message_id)
);
CREATE INDEX IF NOT EXISTS idx_inbound_dedup_seen ON inbound_dedup(seen_at);
CREATE TABLE IF NOT EXISTS inbound_spool (
    spool_id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    uid TEXT NOT NULL,
    nick TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    raw_event_json TEXT NOT NULL,
    received_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    reply_bubbles_json TEXT NOT NULL DEFAULT '[]',
    sent_parts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_spool_state ON inbound_spool(state, kind, spool_id);
"""

_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
_BREAK_CHARS = "。！？!?；;\n，, "


def _split_long_piece(chunk: str, max_len: int) -> List[str]:
    pieces: List[str] = []
    rest = chunk.strip()
    while rest:
        if len(rest) <= max_len:
            pieces.append(rest)
            break
        win = rest[:max_len]
        cut = -1
        # Search for a natural sentence/clause break in the second half so bubbles don't chop right at the start.
        lo = max(1, max_len // 2)
        for ch in _BREAK_CHARS:
            pos = win.rfind(ch, lo)
            if pos > cut:
                cut = pos + 1
        if cut <= 0:
            cut = max_len
        head = rest[:cut].strip()
        tail = rest[cut:].strip()
        if not head:
            head = rest[:max_len].strip()
            tail = rest[max_len:].strip()
        if head:
            pieces.append(head)
        rest = tail
    return pieces


def split_bubbles(text: str, *, max_part_chars: int = 400, max_parts: int = 3) -> List[str]:
    """Split a reply into natural chat bubbles (`\\n\\n` paragraphs first, then sentence/clause breaks)."""
    raw = str(text or "").strip()
    if not raw:
        return []
    limit = max(20, int(max_part_chars or 400))
    cap = max(1, int(max_parts or 3))
    out: List[str] = []
    for para in _PARA_SPLIT_RE.split(raw):
        cleaned = para.strip()
        if not cleaned:
            continue
        for piece in _split_long_piece(cleaned, limit):
            out.append(piece)
            if len(out) >= cap:
                return out
    return out


class ChannelSpool:
    """SQLite durability layer for inbound channel messages, dedup, and crash-safe reply resume."""

    DB_FILENAME = "channel_spool.db"

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        dedup_ttl_s: float = 86400.0,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.db_path = self.runtime_dir / self.DB_FILENAME
        self._clock = clock
        self.dedup_ttl_s = max(60.0, float(dedup_ttl_s))
        self._lock = threading.RLock()
        self.migrate()

    @contextlib.contextmanager
    def _connect(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        with self._lock, self._connect() as c:
            c.executescript(SPOOL_SCHEMA)

    def close(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with self._connect() as c:
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass

    def _now(self, now: Optional[float] = None) -> float:
        return float(now) if now is not None else float(self._clock())

    def check_and_mark_dedup(self, platform: str, message_id: str, *, now: Optional[float] = None) -> bool:
        """Return `True` if `message_id` was already seen recently on `platform`."""
        mid = str(message_id or "").strip()
        if not mid:
            return False
        plat = str(platform or "unknown").strip()
        ts = self._now(now)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM inbound_dedup WHERE seen_at < ?", (ts - self.dedup_ttl_s,))
            row = c.execute(
                "SELECT 1 FROM inbound_dedup WHERE platform=? AND message_id=?",
                (plat, mid),
            ).fetchone()
            if row is not None:
                return True
            c.execute(
                "INSERT INTO inbound_dedup(platform, message_id, seen_at) VALUES(?, ?, ?)",
                (plat, mid, ts),
            )
            return False

    def enqueue_event(
        self,
        platform: str,
        raw_event: Mapping[str, Any],
        *,
        message_id: str = "",
        session_key: str = "",
        kind: str = "",
        target: str = "",
        uid: str = "",
        nick: str = "",
        actor: str = "",
        now: Optional[float] = None,
    ) -> Optional[int]:
        """Persist an inbound event atomically; returns `None` when deduplicated."""
        ts = self._now(now)
        plat = str(platform or "unknown").strip()
        mid = str(message_id or "").strip()
        raw_json = json.dumps(dict(raw_event), ensure_ascii=False)
        with self._lock, self._connect() as c:
            if mid:
                c.execute("DELETE FROM inbound_dedup WHERE seen_at < ?", (ts - self.dedup_ttl_s,))
                hit = c.execute(
                    "SELECT 1 FROM inbound_dedup WHERE platform=? AND message_id=?",
                    (plat, mid),
                ).fetchone()
                if hit is not None:
                    return None
                c.execute(
                    "INSERT INTO inbound_dedup(platform, message_id, seen_at) VALUES(?, ?, ?)",
                    (plat, mid, ts),
                )
            cur = c.execute(
                "INSERT INTO inbound_spool(platform, message_id, session_key, kind, target, uid, nick, actor, "
                "raw_event_json, received_at, state, attempts, reply_bubbles_json, sent_parts, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, '[]', 0, ?)",
                (
                    plat,
                    mid,
                    str(session_key or ""),
                    str(kind or ""),
                    str(target or ""),
                    str(uid or ""),
                    str(nick or ""),
                    str(actor or ""),
                    raw_json,
                    ts,
                    ts,
                ),
            )
            return int(cur.lastrowid)

    def mark_processing(self, spool_ids: Sequence[int], *, now: Optional[float] = None) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as c:
            c.execute(
                f"UPDATE inbound_spool SET state='processing', attempts=attempts+1, updated_at=? WHERE spool_id IN ({marks})",
                [ts, *ids],
            )

    def save_reply_plan(
        self,
        spool_ids: Sequence[int],
        bubbles: Sequence[str],
        *,
        target: str = "",
        kind: str = "",
        at: str = "",
        now: Optional[float] = None,
    ) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        plan = json.dumps({"bubbles": list(bubbles), "target": target, "kind": kind, "at": at}, ensure_ascii=False)
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as c:
            c.execute(
                f"UPDATE inbound_spool SET state='reply_ready', reply_bubbles_json=?, sent_parts=0, updated_at=? "
                f"WHERE spool_id IN ({marks})",
                [plan, ts, *ids],
            )

    def record_part_sent(self, spool_ids: Sequence[int], sent_parts: int, *, now: Optional[float] = None) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as c:
            c.execute(
                f"UPDATE inbound_spool SET sent_parts=?, updated_at=? WHERE spool_id IN ({marks})",
                [int(sent_parts), ts, *ids],
            )

    def ack(self, spool_ids: Sequence[int], *, state: str = "done", now: Optional[float] = None) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as c:
            c.execute(
                f"UPDATE inbound_spool SET state=?, updated_at=? WHERE spool_id IN ({marks})",
                [str(state or "done"), ts, *ids],
            )

    def recover_unfinished(
        self,
        platform: str = "onebot_v11",
        *,
        stale_s: float = 120.0,
        only_ready: bool = False,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch unfinished inbound or `reply_ready` rows (owner private chat first) on startup/reconnect."""
        ts = self._now(now)
        states = ("reply_ready",) if only_ready else ("queued", "processing", "reply_ready")
        st_marks = ",".join("?" for _ in states)
        out: List[Dict[str, Any]] = []
        with self._lock, self._connect() as c:
            if not only_ready and stale_s > 0:
                c.execute(
                    "UPDATE inbound_spool SET state='expired', updated_at=? "
                    "WHERE platform=? AND state IN ('queued', 'processing') AND received_at < ?",
                    (ts, platform, ts - float(stale_s)),
                )
            rows = c.execute(
                f"SELECT * FROM inbound_spool WHERE platform=? AND state IN ({st_marks}) "
                "ORDER BY CASE WHEN kind='private' THEN 0 ELSE 1 END, spool_id ASC LIMIT 100",
                [platform, *states],
            ).fetchall()
        for r in rows:
            try:
                raw_ev = json.loads(r["raw_event_json"] or "{}")
            except ValueError:
                raw_ev = {}
            try:
                plan = json.loads(r["reply_bubbles_json"] or "{}")
            except ValueError:
                plan = {}
            out.append({
                "spool_id": int(r["spool_id"]),
                "state": str(r["state"]),
                "kind": str(r["kind"]),
                "target": str(r["target"]),
                "uid": str(r["uid"]),
                "raw_event": raw_ev,
                "plan": plan if isinstance(plan, dict) else {"bubbles": plan if isinstance(plan, list) else []},
                "sent_parts": int(r["sent_parts"] or 0),
                "received_at": float(r["received_at"]),
            })
        return out

    def list_rows(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as c:
            rows = c.execute(
                "SELECT spool_id, platform, message_id, session_key, kind, target, uid, state, attempts, sent_parts, "
                "received_at, updated_at FROM inbound_spool ORDER BY spool_id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(r) for r in rows]
