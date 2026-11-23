"""Per-roommate personal evening diary generator (`house_diaries` + `MemoryStore`).

Generates one audience-isolated first-person diary per roommate per diary day (22:30–03:00 JST,
cutoff at 05:00 JST) from that roommate's own `daylog` entries and `script` facts.
Never reads private owner DMs; checks output through `ReplyGuard` when configured.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import inspect
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
# The diary is stored whole in house_diaries and, with its 「YYYY-MM-DD 日记：」 prefix (14 chars), in memory (diary kind
# keeps 320). The model is asked for DIARY_ASK_MIN～DIARY_ASK_MAX characters and may overshoot by a good margin; only a
# reply longer than DIARY_MAX_CHARS is shortened, and then back to the last finished sentence, never mid-sentence.
DIARY_ASK_MIN = 100
DIARY_ASK_MAX = 220
DIARY_MAX_CHARS = 300
DEMO_MAX_CHARS = 260
_MIN_KEEP_CHARS = 30  # never trim a reply down to less than this
_SENTENCE_END = "。！？!?…"
_OPEN_TAIL_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9，,、；;：:]$")  # a reply ending like this stopped mid-sentence
_PERIODS = ((5, 0, "早上"), (10, 0, "上午"), (11, 30, "中午"), (14, 0, "下午"), (17, 0, "傍晚"), (19, 0, "晚上"))
_FACT_CLOCK_RE = re.compile(r"^(\d{2}):(\d{2})(?=\s)")

DIARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS house_diaries (
    day TEXT NOT NULL,
    actor TEXT NOT NULL,
    mem_id INTEGER,
    text TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'demo',
    image_album_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (day, actor)
);
"""


def period_of_day(hour: int, minute: int = 0) -> str:
    """早上 / 上午 / 中午 / 下午 / 傍晚 / 晚上 / 深夜 for a clock time (what a meal at that hour is called)."""
    label = "深夜"
    for h, m, name in _PERIODS:
        if (hour, minute) >= (h, m):
            label = name
    return label


def label_fact_clock(fact: str) -> str:
    """「12:11 艾莎吃了…」 -> 「12:11（中午）艾莎吃了…」: the model must not guess whether 12:11 is breakfast."""
    m = _FACT_CLOCK_RE.match(fact)
    if not m:
        return fact
    return f"{m.group(1)}:{m.group(2)}（{period_of_day(int(m.group(1)), int(m.group(2)))}）" + fact[m.end():]


def finish_diary_text(raw: str, limit: int = DIARY_MAX_CHARS) -> str:
    """Control chars removed; over `limit`, or ending mid-sentence, it is cut back to the last finished sentence.

    A reply with no finished sentence to fall back to (or one that would shrink below `_MIN_KEEP_CHARS`) is kept as it is
    (hard-cut at `limit` only when over it, at the last comma if there is one), rather than emptied.
    """
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", str(raw or "")).strip()
    over = len(text) > limit
    if over:
        text = text[:limit]
    elif not _OPEN_TAIL_RE.search(text):
        return text
    ends = [i for i, ch in enumerate(text) if ch in _SENTENCE_END or ch == "\n"]
    # closing quotes / brackets right after the full stop belong to that sentence
    cut = -1
    for i in reversed(ends):
        j = i + 1
        while j < len(text) and text[j] in "」』”’）)】\"'~～":
            j += 1
        if j <= limit and j >= _MIN_KEEP_CHARS and (over or j < len(text)):
            cut = j
            break
    if cut > 0:
        return text[:cut].rstrip()
    if over:
        comma = max(text.rfind("，"), text.rfind("、"), text.rfind("；"), text.rfind(","))
        if comma >= _MIN_KEEP_CHARS:
            return text[:comma] + "。"
    return text


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
                cols = {r[1] for r in c.execute("PRAGMA table_info(house_diaries)").fetchall()}
                if "image_album_id" not in cols:
                    c.execute("ALTER TABLE house_diaries ADD COLUMN image_album_id TEXT NOT NULL DEFAULT ''")

    def set_diary_image(self, day: str, actor: str, album_id: str) -> None:
        self.init_schema()
        with contextlib.closing(self._conn()) as c:
            with c:
                c.execute(
                    "UPDATE house_diaries SET image_album_id=? WHERE day=? AND actor=?",
                    (str(album_id or "").strip(), str(day), str(actor)),
                )

    def list_diaries(self, actor: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        self.init_schema()
        with contextlib.closing(self._conn()) as c:
            if actor:
                rows = c.execute(
                    "SELECT day, actor, mem_id, text, mode, image_album_id, created_at FROM house_diaries "
                    "WHERE actor=? ORDER BY day DESC, created_at DESC LIMIT ?",
                    (actor, int(limit)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT day, actor, mem_id, text, mode, image_album_id, created_at FROM house_diaries "
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
                f"现在是 {day_str} 夜晚，请以「{name}」第一人称口吻写一篇 {DIARY_ASK_MIN}～{DIARY_ASK_MAX} 字的睡前小日记，"
                "一定要写完整，最后一句话用句号收尾，宁可少写几件事也不要写到一半断掉。\n"
                "要求：只能写下面事实清单里实际经历过的事，不编造没发生过的人或事，不写系统提示、路径、密钥或他人私聊秘密。\n"
                "具体细节一律以清单为准：\n"
                "1. 买东西：清单里没写买了什么，就只写去了哪里，不要说买了什么；写了哪些就只写哪些，数量和钱数不要改。\n"
                f"2. 吃饭：谁做的饭看清单里的“（某某做的）”，不要换人；只有清单里写了“{name}做了…”的，才是你自己做的饭。"
                "早餐、午餐、晚餐按清单里的钟点说（括号里写了早上/中午/傍晚等），清单里没有早上的用餐记录就不要写早餐。\n"
                "3. 钟点、地点、一起的人都照清单写；清单里没有的钟点、人、东西、对话和心里想的计划，不要补。"
            )
            user_prompt = "今天你经历过的事：\n" + "\n".join(f"- {label_fact_clock(f)}" for f in facts)
            try:
                accepts_actor = False
                try:
                    sig = inspect.signature(llm_fn)
                    accepts_actor = "actor" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    )
                except (TypeError, ValueError):
                    accepts_actor = False
                raw, _ = llm_fn(sys_prompt, user_prompt, actor=aid) if accepts_actor else llm_fn(sys_prompt, user_prompt)
                cleaned = finish_diary_text(raw)
                if len(cleaned) >= 20 and (self.guard is None or not self.guard.check("world", cleaned)):
                    return cleaned, "live_api"
            except Exception:  # noqa: BLE001
                pass

        highlights = [re.sub(r"^\d{2}:\d{2}\s+", "", f) for f in facts[:4]]
        body = f"{day_str} 日记：今天在小屋里{'，然后'.join(highlights)}。忙完这一天准备早点休息，明天继续好好生活。"
        return finish_diary_text(body, DEMO_MAX_CHARS), "demo"

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
        clean = finish_diary_text(text)
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
