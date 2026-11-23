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
    updated_at REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_inbound_spool_state ON inbound_spool(state, kind, spool_id);
"""

# `<sep>`, `<sep/>`, `<sep />`, `</sep>` (any case, optional inner spaces) all end a bubble.
_PARA_SPLIT_RE = re.compile(r"\s*<\s*/?\s*sep\s*/?\s*>\s*|\n\s*\n+", re.IGNORECASE)
_BREAK_CHARS = "。！？!?；;\n，, "
_FENCE_RE = re.compile(r"```[\s\S]*?```")
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_TERMINAL_STATES = frozenset({"done", "skipped", "expired", "blocked", "dropped", "failed_wire"})

HARD_PART_FACTOR = 2  # no bubble may be longer than max(max_part_chars * this, HARD_PART_MIN) (code / URLs included)
HARD_PART_MIN = 800
MAX_TOTAL_PARTS = 12  # absolute ceiling on bubbles per reply, whatever max_parts says
TRUNCATION_NOTICE = "\n……（内容太长，后面的省略了）"


class _Splitter:
    """Length-aware splitter: code blocks / URLs are masked as tokens but always measured at their real length."""

    def __init__(self, limit: int, hard: int, prefix: str) -> None:
        self.limit = limit
        self.hard = hard
        self.prefix = prefix
        self.spans: List[str] = []
        self.tok_re = re.compile(re.escape(prefix) + r"_(\d+)@@")

    def new_token(self, value: str) -> str:
        self.spans.append(value)
        return f"{self.prefix}_{len(self.spans) - 1}@@"

    def sub(self, m: "re.Match[str]") -> str:
        return self.new_token(m.group(0))

    def rlen(self, s: str) -> int:
        n = len(s)
        for m in self.tok_re.finditer(s):
            n += len(self.spans[int(m.group(1))]) - len(m.group(0))
        return n

    def unmask(self, s: str) -> str:
        return self.tok_re.sub(lambda m: self.spans[int(m.group(1))], s)

    def _window_end(self, rest: str, limit: int) -> int:
        i = 0
        cum = 0
        while i < len(rest):
            m = self.tok_re.match(rest, i)
            if m:
                tl, step = len(self.spans[int(m.group(1))]), m.end() - i
            else:
                tl, step = 1, 1
            if cum + tl > limit:
                break
            cum += tl
            i += step
        return i

    def _split_span(self, span: str) -> List[str]:
        """An atomic span longer than the hard limit has to be cut after all; code stays fenced chunk by chunk."""
        hard = self.hard
        if span.startswith("```"):
            body = span[3:-3] if (span.endswith("```") and len(span) >= 6) else span[3:]
            nl = body.find("\n")
            lang, content = (body[:nl], body[nl + 1 :]) if nl >= 0 else ("", body)
            if len(lang) > 24 or not re.fullmatch(r"[\w.+#-]*", lang.strip()):
                # Not a language tag (a very long or free-text first line): it is part of the code, otherwise
                # repeating it on every chunk would push each chunk over the hard limit.
                lang, content = "", body
            lang = lang.strip()
            room = max(20, hard - len(lang) - 8)
            chunks: List[str] = []
            cur = ""
            for line in content.split("\n"):
                while len(line) > room:
                    if cur:
                        chunks.append(cur)
                        cur = ""
                    chunks.append(line[:room])
                    line = line[room:]
                if cur and len(cur) + 1 + len(line) > room:
                    chunks.append(cur)
                    cur = line
                else:
                    cur = f"{cur}\n{line}" if cur else line
            if cur:
                chunks.append(cur)
            return [f"```{lang}\n{c}\n```" for c in chunks]
        return [span[i : i + hard] for i in range(0, len(span), hard)]

    def split_piece(self, chunk: str) -> List[str]:
        pieces: List[str] = []
        rest = chunk.strip()
        while rest:
            if self.rlen(rest) <= self.limit:
                pieces.append(rest)
                break
            end = self._window_end(rest, self.limit)
            if end <= 0:
                m = self.tok_re.match(rest)
                if m is None:  # cannot happen (limit >= 20); never loop forever
                    pieces.append(rest[: self.limit])
                    rest = rest[self.limit :].strip()
                    continue
                span = self.spans[int(m.group(1))]
                if len(span) <= self.hard:
                    pieces.append(m.group(0))
                else:
                    pieces.extend(self.new_token(x) for x in self._split_span(span))
                rest = rest[m.end() :].strip()
                continue
            win = rest[:end]
            cut = -1
            lo = max(1, end // 2)
            for ch in _BREAK_CHARS:
                pos = win.rfind(ch, lo)
                if pos > cut:
                    cut = pos + 1
            if cut <= 0:
                cut = end
            head = rest[:cut].strip()
            tail = rest[cut:].strip()
            if not head:
                head, tail = rest[:end].strip(), rest[end:].strip()
            if head:
                pieces.append(head)
            rest = tail
        return pieces


def split_bubbles(text: str, *, max_part_chars: int = 400, max_parts: int = 3) -> List[str]:
    """Split a reply into natural chat bubbles (`<sep>` variants / blank lines first).

    Code blocks and URLs are never cut unless one alone is longer than the hard limit. No bubble is longer than
    `max(max_part_chars * HARD_PART_FACTOR, HARD_PART_MIN)`. Beyond `max_parts` the remainder is packed into further bubbles (at most
    `MAX_TOTAL_PARTS` / three times `max_parts` in all); whatever still does not fit is cut off with a visible notice
    instead of being dropped silently."""
    raw = str(text or "").strip()
    if not raw:
        return []
    limit = max(20, int(max_part_chars or 400))
    cap = max(1, int(max_parts or 3))
    hard = max(limit * HARD_PART_FACTOR, HARD_PART_MIN)
    prefix = "@@AHKSPAN"
    while prefix in raw:
        prefix += "X"
    sp = _Splitter(limit, hard, prefix)

    masked = _FENCE_RE.sub(sp.sub, raw)
    masked = _URL_RE.sub(sp.sub, masked)

    pieces: List[str] = []
    for para in _PARA_SPLIT_RE.split(masked):
        cleaned = para.strip()
        if cleaned:
            pieces.extend(sp.split_piece(cleaned))
    if not pieces:
        return []

    if len(pieces) > cap:
        # Fold the overflow into as few further bubbles as the hard limit allows (never one giant tail bubble).
        head, tail = pieces[: cap - 1], pieces[cap - 1 :]
        packed: List[str] = []
        cur = ""
        for p in tail:
            if cur and sp.rlen(cur) + 1 + sp.rlen(p) <= hard:
                cur = f"{cur}\n{p}"
            else:
                if cur:
                    packed.append(cur)
                cur = p
        if cur:
            packed.append(cur)
        pieces = head + packed

    out = [sp.unmask(p) for p in pieces]
    total_cap = max(cap, min(cap * 3, MAX_TOTAL_PARTS))
    if len(out) > total_cap:
        out = out[:total_cap]
        last = out[-1]
        room = hard - len(TRUNCATION_NOTICE) - 5
        if len(last) > room:
            last = last[:room]
            pos = max(last.rfind(ch) for ch in _BREAK_CHARS)
            if pos >= room // 2:
                last = last[: pos + 1]
            if last.count("```") % 2:
                last = last.rstrip() + "\n```"
        out[-1] = last.rstrip() + TRUNCATION_NOTICE
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
            spool_cols = {r[1] for r in c.execute("PRAGMA table_info(inbound_spool)").fetchall()}
            if "reason" not in spool_cols:  # spools created before the reason column existed
                c.execute("ALTER TABLE inbound_spool ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_inbound_spool_updated ON inbound_spool(updated_at)")

    def close(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with self._connect() as c:
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass

    def reset(self) -> None:
        """Drop every cached inbound row and dedup mark, leaving a fresh, usable (empty) spool database."""
        with self._lock:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
            self.migrate()

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
        media: Sequence[Mapping[str, Any]] = (),
        now: Optional[float] = None,
    ) -> None:
        """Persist what is about to be sent. `sent_parts` later counts bubbles first, then `media` items."""
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        plan_doc: Dict[str, Any] = {"bubbles": list(bubbles), "target": target, "kind": kind, "at": at}
        if media:
            plan_doc["media"] = [dict(m) for m in media]
        plan = json.dumps(plan_doc, ensure_ascii=False)
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

    def ack(
        self,
        spool_ids: Sequence[int],
        *,
        state: str = "done",
        reason: str = "",
        now: Optional[float] = None,
    ) -> None:
        ids = [int(i) for i in spool_ids if i]
        if not ids:
            return
        ts = self._now(now)
        eff_state = str(state or "done")
        why = str(reason or "")[:200]
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as c:
            if eff_state in _TERMINAL_STATES:
                # the raw event and the saved reply (which may carry base64 media) are no longer needed
                c.execute(
                    f"UPDATE inbound_spool SET state=?, raw_event_json='{{}}', reply_bubbles_json='[]', reason=?, "
                    f"updated_at=? WHERE spool_id IN ({marks})",
                    [eff_state, why, ts, *ids],
                )
            else:
                c.execute(
                    f"UPDATE inbound_spool SET state=?, reason=?, updated_at=? WHERE spool_id IN ({marks})",
                    [eff_state, why, ts, *ids],
                )

    def prune(self, *, keep_done_s: float = 86400.0, now: Optional[float] = None) -> int:
        """Delete spool rows and expired dedup records untouched for `keep_done_s`.

        Terminal rows go after that time; so do rows stuck in `queued` / `processing` / `reply_ready` (a crash or a
        killed worker left them behind and nothing resumes a row that old)."""
        ts = self._now(now)
        cutoff = ts - max(60.0, float(keep_done_s))
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM inbound_dedup WHERE seen_at < ?", (ts - self.dedup_ttl_s,))
            cur = c.execute("DELETE FROM inbound_spool WHERE updated_at < ?", [cutoff])
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
                "reason, received_at, updated_at FROM inbound_spool ORDER BY spool_id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(r) for r in rows]
