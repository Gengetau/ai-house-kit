"""Durable inbound spool, message_id deduplication, natural bubble splitting & restart recovery (`0.3.0`)."""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SPOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_dedup (
    platform TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY(platform, scope_key, message_id)
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

_PARA_SPLIT_RE = re.compile(r"\s*<sep>\s*|\n\s*\n+")
_BREAK_CHARS = "。！？!?；;\n，, "
_FENCE_RE = re.compile(r"```[\s\S]*?```")
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_TERMINAL_STATES = frozenset({"done", "skipped", "expired", "blocked", "dropped", "failed_wire"})


def _split_long_piece(chunk: str, max_len: int, span_tokens: Sequence[str] = ()) -> List[str]:
    pieces: List[str] = []
    rest = chunk.strip()
    while rest:
        if len(rest) <= max_len:
            pieces.append(rest)
            break
        # Protect atomic spans (code blocks / URLs): if the chunk starts with one longer than max_len, emit it whole.
        leading_tok = next((t for t in span_tokens if rest.startswith(t)), None)
        if leading_tok and len(leading_tok) > max_len:
            pieces.append(leading_tok)
            rest = rest[len(leading_tok) :].strip()
            continue

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
        # Ensure `cut` never bisects any placeholder token (`prefix_idx@@`).
        for tok in span_tokens:
            start = rest.find(tok)
            while start != -1 and start < cut:
                end = start + len(tok)
                if start < cut < end:
                    cut = start if start > 0 else end
                    break
                start = rest.find(tok, end)
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
    """Split a reply into natural chat bubbles (`<sep>` / `\\n\\n` first), preserving code blocks, URLs & overflow."""
    raw = str(text or "").strip()
    if not raw:
        return []
    limit = max(20, int(max_part_chars or 400))
    cap = max(1, int(max_parts or 3))

    # Mask fenced code blocks and URLs with collision-free tokens so splits never break inside them.
    spans: List[str] = []
    prefix = "@@AHKSPAN"
    while prefix in raw:
        prefix += "X"

    def _sub(m: re.Match[str]) -> str:
        idx = len(spans)
        spans.append(m.group(0))
        return f"{prefix}_{idx}@@"

    masked = _FENCE_RE.sub(_sub, raw)
    masked = _URL_RE.sub(_sub, masked)
    span_tokens = [f"{prefix}_{i}@@" for i in range(len(spans))]

    all_pieces: List[str] = []
    for para in _PARA_SPLIT_RE.split(masked):
        cleaned = para.strip()
        if not cleaned:
            continue
        all_pieces.extend(_split_long_piece(cleaned, limit, span_tokens))

    if not all_pieces:
        return []
    if len(all_pieces) > cap:
        # PR-C6: Never drop trailing content beyond `max_parts` — fold remaining pieces into the final bubble.
        merged_tail = "\n".join(all_pieces[cap - 1 :])
        all_pieces = all_pieces[: cap - 1] + [merged_tail]

    def _unmask(s: str) -> str:
        for tok, val in zip(span_tokens, spans):
            if tok in s:
                s = s.replace(tok, val)
        return s

    return [_unmask(p) for p in all_pieces]


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
        self._last_prune_ts = 0.0
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
            has_dedup = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound_dedup'").fetchone() is not None
            if has_dedup:
                cols = {r[1] for r in c.execute("PRAGMA table_info(inbound_dedup)").fetchall()}
                if "scope_key" not in cols:
                    c.execute(
                        "CREATE TABLE IF NOT EXISTS inbound_dedup_v2 ("
                        "platform TEXT NOT NULL, scope_key TEXT NOT NULL DEFAULT '', "
                        "message_id TEXT NOT NULL, seen_at REAL NOT NULL, "
                        "PRIMARY KEY(platform, scope_key, message_id))"
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO inbound_dedup_v2(platform, scope_key, message_id, seen_at) "
                        "SELECT platform, '', message_id, seen_at FROM inbound_dedup"
                    )
                    c.execute("DROP TABLE inbound_dedup")
                    c.execute("ALTER TABLE inbound_dedup_v2 RENAME TO inbound_dedup")
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

    def check_and_mark_dedup(
        self,
        platform: str,
        message_id: str,
        *,
        scope_key: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """Return `True` if `message_id` was already seen recently on `(platform, scope_key)`."""
        mid = str(message_id or "").strip()
        if not mid:
            return False
        plat = str(platform or "unknown").strip()
        skey = str(scope_key or "").strip()
        ts = self._now(now)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM inbound_dedup WHERE seen_at < ?", (ts - self.dedup_ttl_s,))
            row = c.execute(
                "SELECT 1 FROM inbound_dedup WHERE platform=? AND scope_key=? AND message_id=?",
                (plat, skey, mid),
            ).fetchone()
            if row is not None:
                return True
            c.execute(
                "INSERT INTO inbound_dedup(platform, scope_key, message_id, seen_at) VALUES(?, ?, ?, ?)",
                (plat, skey, mid, ts),
            )
            return False

    def enqueue_event(
        self,
        platform: str,
        raw_event: Mapping[str, Any],
        *,
        message_id: str = "",
        session_key: str = "",
        scope_key: str = "",
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
        eff_scope = str(scope_key or session_key or (f"{kind}:{target}" if (kind or target) else "")).strip()
        raw_json = json.dumps(dict(raw_event), ensure_ascii=False)
        with self._lock, self._connect() as c:
            if mid:
                c.execute("DELETE FROM inbound_dedup WHERE seen_at < ?", (ts - self.dedup_ttl_s,))
                hit = c.execute(
                    "SELECT 1 FROM inbound_dedup WHERE platform=? AND scope_key=? AND message_id=?",
                    (plat, eff_scope, mid),
                ).fetchone()
                if hit is not None:
                    return None
                c.execute(
                    "INSERT INTO inbound_dedup(platform, scope_key, message_id, seen_at) VALUES(?, ?, ?, ?)",
                    (plat, eff_scope, mid, ts),
                )
            cur = c.execute(
                "INSERT INTO inbound_spool(platform, message_id, session_key, kind, target, uid, nick, actor, "
                "raw_event_json, received_at, state, attempts, reply_bubbles_json, sent_parts, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, '[]', 0, ?)",
                (
                    plat,
                    mid,
                    str(session_key or eff_scope or ""),
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
            row_id = int(cur.lastrowid)
        if abs(ts - self._last_prune_ts) >= 300.0:
            self._last_prune_ts = ts
            self.prune(now=ts)
        return row_id

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
        primary_id = ids[-1]
        merged_ids = ids[:-1]
        with self._lock, self._connect() as c:
            if merged_ids:
                m_marks = ",".join("?" for _ in merged_ids)
                c.execute(
                    f"UPDATE inbound_spool SET state='done', raw_event_json='{{}}', reply_bubbles_json='[]', sent_parts=0, updated_at=? "
                    f"WHERE spool_id IN ({m_marks})",
                    [ts, *merged_ids],
                )
            c.execute(
                "UPDATE inbound_spool SET state='reply_ready', reply_bubbles_json=?, sent_parts=0, updated_at=? "
                "WHERE spool_id=?",
                [plan, ts, primary_id],
            )

    def record_part_sent(self, spool_ids: Sequence[int], sent_parts: int, *, now: Optional[float] = None) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        primary_id = ids[-1]
        with self._lock, self._connect() as c:
            c.execute(
                "UPDATE inbound_spool SET sent_parts=?, updated_at=? WHERE spool_id=?",
                [int(sent_parts), ts, primary_id],
            )

    def ack(self, spool_ids: Sequence[int], *, state: str = "done", now: Optional[float] = None) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        eff_state = str(state or "done")
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as c:
            if eff_state in _TERMINAL_STATES:
                c.execute(
                    f"UPDATE inbound_spool SET state=?, raw_event_json='{{}}', updated_at=? WHERE spool_id IN ({marks})",
                    [eff_state, ts, *ids],
                )
            else:
                c.execute(
                    f"UPDATE inbound_spool SET state=?, updated_at=? WHERE spool_id IN ({marks})",
                    [eff_state, ts, *ids],
                )

    def prune(self, *, keep_done_s: float = 86400.0, now: Optional[float] = None) -> int:
        """Delete terminal spool rows and expired dedup records older than `keep_done_s`."""
        ts = self._now(now)
        cutoff = ts - max(60.0, float(keep_done_s))
        term = tuple(sorted(_TERMINAL_STATES))
        marks = ",".join("?" for _ in term)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM inbound_dedup WHERE seen_at < ?", (ts - self.dedup_ttl_s,))
            cur = c.execute(
                f"DELETE FROM inbound_spool WHERE state IN ({marks}) AND updated_at < ?",
                [*term, cutoff],
            )
            return int(cur.rowcount or 0)

    def recover_unfinished(
        self,
        platform: str = "onebot_v11",
        *,
        stale_s: float = 120.0,
        reply_stale_s: float = 1800.0,
        only_ready: bool = False,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch unfinished inbound or non-stale `reply_ready` rows on startup/reconnect."""
        ts = self._now(now)
        states = ("reply_ready",) if only_ready else ("queued", "processing", "reply_ready")
        st_marks = ",".join("?" for _ in states)
        out: List[Dict[str, Any]] = []
        with self._lock, self._connect() as c:
            if not only_ready and stale_s > 0:
                c.execute(
                    "UPDATE inbound_spool SET state='expired', raw_event_json='{}', updated_at=? "
                    "WHERE platform=? AND state IN ('queued', 'processing') AND received_at < ?",
                    (ts, platform, ts - float(stale_s)),
                )
            if reply_stale_s > 0:
                c.execute(
                    "UPDATE inbound_spool SET state='expired', raw_event_json='{}', updated_at=? "
                    "WHERE platform=? AND state='reply_ready' AND updated_at < ?",
                    (ts, platform, ts - float(reply_stale_s)),
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
