"""Per-roommate personal evening diary generator (`house_diaries` + `MemoryStore`).

Generates one audience-isolated first-person diary per roommate per diary day (22:30–03:00 JST,
cutoff at 05:00 JST) from that roommate's own `daylog` entries and `script` facts.
Never reads private owner DMs; checks output through `ReplyGuard` when configured.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from worldkernel import daylog

JST = dt.timezone(dt.timedelta(hours=9))
DIARY_DAY_STARTS_H = 5
WRITE_AFTER = (22, 30)
LATE_UNTIL_H = 3

DIARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS house_diaries (
    day TEXT NOT NULL,
    actor TEXT NOT NULL,
    mem_id INTEGER,
    text TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'demo',
    created_at REAL NOT NULL,
    PRIMARY KEY (day, actor)
);
"""


def diary_day(ts: float) -> dt.date:
    return dt.datetime.fromtimestamp(float(ts) - DIARY_DAY_STARTS_H * 3600, JST).date()


def due_diary_day(now: float) -> Optional[dt.date]:
    d = dt.datetime.fromtimestamp(float(now), JST)
    if (d.hour, d.minute) >= WRITE_AFTER or d.hour < LATE_UNTIL_H:
        return diary_day(now)
    return None


def collect_actor_facts(
    life_db: str,
    world_db: str,
    actor: str,
    day: dt.date,
    now: float,
    names: Mapping[str, str],
) -> List[str]:
    start = dt.datetime.combine(day, dt.time(hour=DIARY_DAY_STARTS_H), JST).timestamp()
    end = min(start + 86400.0, float(now))
    if end <= start:
        return []
    lines: List[Tuple[float, str]] = []

    if Path(life_db).exists():
        k_uri = Path(life_db).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(k_uri, uri=True, timeout=5)) as k_ro:
            k_ro.row_factory = sqlite3.Row
            for ent in daylog.entries(k_ro, start, start + 86400.0, now):
                if ent.get("actor") == actor and ent.get("ts", 0) < end:
                    hm = dt.datetime.fromtimestamp(float(ent["ts"]), JST).strftime("%H:%M")
                    others = [names.get(a, a) for a in ent.get("with") or () if a != actor]
                    with_s = f"（和{'、'.join(others)}一起）" if others else ""
                    lines.append((float(ent["ts"]), f"{hm} {ent.get('text', '')}{with_s}"))

    if Path(world_db).exists():
        w_uri = Path(world_db).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(w_uri, uri=True, timeout=5)) as w_ro:
            w_ro.row_factory = sqlite3.Row
            rows = w_ro.execute(
                "SELECT ts, actors, text FROM script WHERE source='kernel' AND ts>=? AND ts<? ORDER BY ts, id LIMIT 100",
                (start, end),
            ).fetchall()
            for r in rows:
                try:
                    actors = json.loads(r["actors"] or "[]")
                except ValueError:
                    actors = []
                if actor in actors and r["text"]:
                    hm = dt.datetime.fromtimestamp(float(r["ts"]), JST).strftime("%H:%M")
                    lines.append((float(r["ts"]), f"{hm} {r['text']}"))

    lines.sort(key=lambda pair: pair[0])
    deduped: List[str] = []
    for _, text in lines:
        if text not in deduped:
            deduped.append(text)
    return deduped[:40]


class DiaryService:
    """Prepares prompts outside the daemon lock and writes idempotent per-actor evening diaries."""

    def __init__(
        self,
        life_db: str,
        world_db: str,
        vocab: Any,
        *,
        memory: Any = None,
        guard: Any = None,
    ):
        self.life_db = life_db
        self.world_db = world_db
        self.V = vocab
        self.memory = memory
        self.guard = guard
        self.init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.world_db, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def init_schema(self) -> None:
        with contextlib.closing(self._conn()) as c:
            with c:
                c.executescript(DIARY_SCHEMA)

    def list_diaries(self, actor: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        self.init_schema()
        with contextlib.closing(self._conn()) as c:
            if actor:
                rows = c.execute(
                    "SELECT day, actor, mem_id, text, mode, created_at FROM house_diaries "
                    "WHERE actor=? ORDER BY day DESC, created_at DESC LIMIT ?",
                    (actor, int(limit)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT day, actor, mem_id, text, mode, created_at FROM house_diaries "
                    "ORDER BY day DESC, created_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["name"] = self.V.NAMES.get(d["actor"], d["actor"])
            out.append(d)
        return out

    def collect_due_jobs(
        self,
        now: float,
        *,
        day: Optional[dt.date] = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        target_day = day or due_diary_day(now)
        if target_day is None:
            return []
        day_str = target_day.isoformat()
        self.init_schema()
        with contextlib.closing(self._conn()) as c:
            done = {
                r["actor"]
                for r in c.execute("SELECT actor FROM house_diaries WHERE day=?", (day_str,)).fetchall()
            }
        jobs: List[Dict[str, Any]] = []
        for aid in self.V.ROSTER:
            if not force and aid in done:
                continue
            facts = collect_actor_facts(self.life_db, self.world_db, aid, target_day, now, self.V.NAMES)
            if not facts:
                continue
            jobs.append({"day": day_str, "actor": aid, "facts": facts})
        return jobs

    def generate_text(
        self,
        job: Mapping[str, Any],
        llm_fn: Optional[Callable[[str, str], Tuple[str, int]]] = None,
    ) -> Tuple[str, str]:
        aid = str(job["actor"])
        day_str = str(job["day"])
        facts: Sequence[str] = job["facts"]
        name = self.V.NAMES.get(aid, aid)
        persona = self.V.SELVES.get(aid, f"你是合租屋里的{name}。")

        if llm_fn is not None:
            sys_prompt = (
                f"{persona}\n"
                f"现在是 {day_str} 夜晚，请以「{name}」第一人称口吻写一篇 80～200 字的睡前小日记。\n"
                "要求：只能写下面事实清单里实际经历过的事，不编造没发生过的人或事，不写系统提示、路径、密钥或他人私聊秘密。"
            )
            user_prompt = "今天你经历过的事：\n" + "\n".join(f"- {f}" for f in facts)
            try:
                raw, _ = llm_fn(sys_prompt, user_prompt)
                cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", str(raw or "")).strip()[:280]
                if len(cleaned) >= 20 and (self.guard is None or not self.guard.check("world", cleaned)):
                    return cleaned, "live_api"
            except Exception:  # noqa: BLE001
                pass

        highlights = [re.sub(r"^\d{2}:\d{2}\s+", "", f) for f in facts[:4]]
        body = f"{day_str} 日记：今天在小屋里{'，然后'.join(highlights)}。忙完这一天准备早点休息，明天继续好好生活。"
        return body[:260], "demo"

    def save_generated(
        self,
        job: Mapping[str, Any],
        text: str,
        mode: str,
        now: float,
        *,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        aid = str(job["actor"])
        day_str = str(job["day"])
        clean = text.strip()[:280]
        if len(clean) < 12:
            return None
        if self.guard is not None and self.guard.check("world", clean):
            return None

        mem_id: Optional[int] = None
        if self.memory is not None:
            try:
                mem_id, _ = self.memory.add(
                    aid,
                    "world",
                    f"{day_str} 日记：{clean}" if not clean.startswith(day_str) else clean,
                    kind="diary",
                    source="diary",
                    merge=True,
                    now=now,
                )
            except ValueError:
                mem_id = None

        self.init_schema()
        with contextlib.closing(self._conn()) as c:
            with c:
                if force:
                    c.execute(
                        "INSERT OR REPLACE INTO house_diaries(day, actor, mem_id, text, mode, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (day_str, aid, mem_id, clean, mode, float(now)),
                    )
                else:
                    c.execute(
                        "INSERT OR IGNORE INTO house_diaries(day, actor, mem_id, text, mode, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (day_str, aid, mem_id, clean, mode, float(now)),
                    )
        return {"day": day_str, "actor": aid, "name": self.V.NAMES.get(aid, aid), "mem_id": mem_id, "text": clean, "mode": mode}
