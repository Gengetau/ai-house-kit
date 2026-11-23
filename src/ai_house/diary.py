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
import threading
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
DEMO_PICK_MAX = 6  # the template mentions at most this many things, spread over the morning, the afternoon and the evening
DEMO_PICK_MIN = 3  # ...and is thinned down to this many when the lines are long
# A real model that fails keeps the diary for a later tick instead of the template: at most RETRY_MAX_TRIES real tries,
# RETRY_DELAY_S apart, and only while a try can still start before RETRY_UNTIL (JST, the morning after the diary day;
# the diary window itself closes at LATE_UNTIL_H, and the template must still be written inside it).
RETRY_MAX_TRIES = 3
RETRY_DELAY_S = 600.0
RETRY_UNTIL = (2, 30)
# A call refused by the open circuit breaker never reached the endpoint: it is not a try; the diary waits this long
# (just over the breaker's 30s open time) and asks again.
BREAKER_WAIT_S = 35.0
MAX_FACTS = 40  # facts given to the model for one day (a busier day is thinned, not cut off at the evening)
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
    lines: List[Tuple[float, str, bool]] = []  # (time, text, is a bare walk)

    if Path(life_db).exists():
        k_uri = Path(life_db).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(k_uri, uri=True, timeout=5)) as k_ro:
            k_ro.row_factory = sqlite3.Row
            for ent in daylog.entries(k_ro, start, start + 86400.0, now, names=names):
                if ent.get("actor") == actor and ent.get("ts", 0) < end:
                    hm = dt.datetime.fromtimestamp(float(ent["ts"]), JST).strftime("%H:%M")
                    others = [names.get(a, a) for a in ent.get("with") or () if a != actor]
                    with_s = f"（和{'、'.join(others)}一起）" if others else ""
                    lines.append((float(ent["ts"]), f"{hm} {ent.get('text', '')}{with_s}", _bare_walk(ent)))

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
                    lines.append((float(r["ts"]), f"{hm} {r['text']}", False))

    lines.sort(key=lambda row: row[0])
    seen: set = set()
    rows: List[Tuple[float, str, bool]] = []
    for row in lines:
        if row[1] not in seen:
            seen.add(row[1])
            rows.append(row)
    return [text for _, text, _ in _within_limit(rows, MAX_FACTS)]


def _bare_walk(ent: Mapping[str, Any]) -> bool:
    """A walk from room to room. A walk out of the house or back home (to or from a place outside) is a thing."""
    if ent.get("kind") != "move":
        return False
    return not any(daylog._outside(p) for p in (ent.get("frm"), ent.get("to"), *(ent.get("via") or ())))


def _spread(rows: List[Any], k: int) -> List[Any]:
    """k of the rows, evenly spaced by position (the first and the last included when k >= 2), in the same order."""
    if k <= 0:
        return []
    if k >= len(rows):
        return list(rows)
    if k == 1:
        return [rows[len(rows) // 2]]
    return [rows[round(i * (len(rows) - 1) / (k - 1))] for i in range(k)]


def _within_limit(rows: List[Tuple[float, str, bool]], limit: int) -> List[Tuple[float, str, bool]]:
    """At most `limit` rows that still cover the whole day, in time order. Meals, purchases, shared activities, going
    out, outfits, sleep (everything but a bare walk from room to room) are kept first; the walks fill what is left,
    evenly spaced; if the things alone are too many they are thinned evenly. Cutting the tail instead dropped the
    evening and the night of a busy day."""
    if len(rows) <= limit:
        return rows
    things = [r for r in rows if not r[2]]
    walks = [r for r in rows if r[2]]
    if len(things) >= limit:
        kept = _spread(things, limit)
    else:
        kept = things + _spread(walks, limit - len(things))
    return sorted(kept, key=lambda r: r[0])


class DiaryLiveFailure(Exception):
    """The model call for a diary failed; `code` is a short, key-free reason (HTTP_502, circuit_open, TimeoutError...)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def failure_code(exc: BaseException) -> str:
    """A short reason for the log. Never the exception text: an HTTP error body may carry a key."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        detail = str(getattr(exc, "detail", "") or "")
        if detail in ("circuit_open", "deadline_exceeded"):
            return detail
        return "EMPTY_REPLY" if detail == "empty_reply" else f"HTTP_{status}"
    return type(exc).__name__


def _retry_cutoff(day_str: str) -> float:
    """The last moment a retry may start: RETRY_UNTIL on the morning after the diary day, JST."""
    d = dt.date.fromisoformat(day_str) + dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(*RETRY_UNTIL), JST).timestamp()


def _pick_demo_facts(facts: List[str], k: int) -> List[str]:
    """At most k lines that cover the day: an equal share from the morning, the afternoon and the evening/night (evenly
    spaced inside each), the places a short part leaves free filled from what is left; always in time order."""
    if len(facts) <= k:
        return list(facts)

    def part(fact: str) -> int:
        m = _FACT_CLOCK_RE.match(fact)
        if not m:
            return 1
        minutes = int(m.group(1)) * 60 + int(m.group(2))
        if DIARY_DAY_STARTS_H * 60 <= minutes < 11 * 60 + 30:
            return 0
        return 1 if 11 * 60 + 30 <= minutes < 17 * 60 else 2

    chosen: set = set()
    for p in (0, 1, 2):
        idx = [i for i, f in enumerate(facts) if part(f) == p]
        chosen.update(_spread(idx, min(len(idx), k // 3)))
    rest = [i for i in range(len(facts)) if i not in chosen]
    chosen.update(_spread(rest, k - len(chosen)))
    return [facts[i] for i in sorted(chosen)]


def _demo_body(day_str: str, facts: List[str], name: str) -> str:
    """「日期 日记：早上我…；上午我…；…。」 in the first person (the diarist's own name becomes 我)."""
    parts: List[str] = []
    prev = ""
    last_hour = -1
    for f in facts:
        m = _FACT_CLOCK_RE.match(f)
        text = re.sub(r"^\d{2}:\d{2}\s+", "", f)
        if name:
            text = text.replace(name, "我")
        label = period_of_day(int(m.group(1)), int(m.group(2))) if m else ""
        parts.append(("接着" if label and label == prev else label) + text)
        prev = label or prev
        if m:
            last_hour = int(m.group(1))
    closing = (
        "忙完这一天准备早点休息，明天继续好好生活。"
        if last_hour >= 18 or 0 <= last_hour < DIARY_DAY_STARTS_H
        else "先写到这里，明天继续好好生活。"
    )
    return f"{day_str} 日记：" + "；".join(parts) + "。" + closing


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
        # (day, actor) -> {"tries": real failed tries so far, "next_at": earliest time of the next try}; in memory only
        self._retry: Dict[Tuple[str, str], Dict[str, float]] = {}
        self._retry_lock = threading.Lock()
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

    def _live_text(self, job: Mapping[str, Any], llm_fn: Callable[..., Tuple[str, int]]) -> Optional[str]:
        """The model's diary, or None when it answered with something unusable (too short, or stopped by the guard).

        Raises `DiaryLiveFailure` when the call itself failed (HTTP error, timeout, breaker open, ...)."""
        aid = str(job["actor"])
        day_str = str(job["day"])
        facts: Sequence[str] = job["facts"]
        name = self.V.NAMES.get(aid, aid)
        persona = self.V.SELVES.get(aid, f"你是合租屋里的{name}。")
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
        accepts_actor = False
        try:
            sig = inspect.signature(llm_fn)
            accepts_actor = "actor" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_actor = False
        try:
            raw, _ = llm_fn(sys_prompt, user_prompt, actor=aid) if accepts_actor else llm_fn(sys_prompt, user_prompt)
        except Exception as exc:  # noqa: BLE001
            raise DiaryLiveFailure(failure_code(exc)) from exc
        cleaned = finish_diary_text(raw)
        if len(cleaned) >= 20 and (self.guard is None or not self.guard.check("world", cleaned)):
            return cleaned
        return None

    def generate_text(
        self,
        job: Mapping[str, Any],
        llm_fn: Optional[Callable[[str, str], Tuple[str, int]]] = None,
    ) -> Tuple[str, str]:
        """One-shot: the model's diary, else the template (the daemon goes through `run_due`, which retries first)."""
        if llm_fn is not None:
            try:
                text = self._live_text(job, llm_fn)
            except DiaryLiveFailure:
                text = None
            if text:
                return text, "live_api"
        return self._template_text(job), "demo"

    def _template_text(self, job: Mapping[str, Any]) -> str:
        """The stand-in diary: up to DEMO_PICK_MAX things spread over the whole day, in the first person."""
        aid = str(job["actor"])
        day_str = str(job["day"])
        name = self.V.NAMES.get(aid, aid)
        picked = _pick_demo_facts(list(job["facts"]), DEMO_PICK_MAX)
        while True:
            body = _demo_body(day_str, picked, name)
            if len(body) <= DEMO_MAX_CHARS or len(picked) <= DEMO_PICK_MIN:
                break
            picked = _spread(picked, len(picked) - 1)
        return finish_diary_text(body, DEMO_MAX_CHARS)

    # ------------------------------------------------------------------ scheduling with retry
    def run_due(
        self,
        now: float,
        llm_fn: Optional[Callable[..., Tuple[str, int]]],
        *,
        log: Optional[Callable[[str], None]] = None,
        on_saved: Optional[Callable[[Dict[str, Any], Mapping[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Write the diaries that are due now; returns the ones saved by this call.

        Without a model (`llm_fn` None) every due diary is the template at once. With one, a failed call keeps that
        diary for a later call (RETRY_DELAY_S on, at most RETRY_MAX_TRIES real tries, never past RETRY_UNTIL); a call
        refused by the open circuit breaker is not a try and waits BREAKER_WAIT_S. Only when the tries or the night
        run out is the template written, with one log line. Each diary is independent of the others."""
        say = log or (lambda _m: None)
        saved_all: List[Dict[str, Any]] = []
        for job in self.collect_due_jobs(now):
            try:
                saved = self._run_job(job, llm_fn, now, say)
            except Exception as exc:  # noqa: BLE001
                say(f"diary error: {job.get('actor')} {job.get('day')} {type(exc).__name__}: {exc}")
                continue
            if saved is None:
                continue
            saved_all.append(saved)
            if on_saved is not None:
                on_saved(saved, job)
        return saved_all

    def _run_job(
        self,
        job: Mapping[str, Any],
        llm_fn: Optional[Callable[..., Tuple[str, int]]],
        now: float,
        say: Callable[[str], None],
    ) -> Optional[Dict[str, Any]]:
        aid, day_str = str(job["actor"]), str(job["day"])
        key = (day_str, aid)
        if llm_fn is None:
            return self.save_generated(job, self._template_text(job), "demo", now)
        with self._retry_lock:
            st = self._retry.setdefault(key, {"tries": 0, "next_at": 0.0})
            if now < st["next_at"]:
                return None
            tries = int(st["tries"])
        try:
            text = self._live_text(job, llm_fn)
        except DiaryLiveFailure as fail:
            blocked = fail.code == "circuit_open"
            if not blocked:
                tries += 1
            wait = BREAKER_WAIT_S if blocked else RETRY_DELAY_S
            with self._retry_lock:
                self._retry[key] = {"tries": tries, "next_at": now + wait}
            if tries < RETRY_MAX_TRIES and now + wait <= _retry_cutoff(day_str):
                say(
                    f"diary 稍后重试: {aid} {day_str} 已真失败 {tries}/{RETRY_MAX_TRIES} 次（{fail.code}），"
                    f"{int(wait)} 秒后再试" + ("（熔断中，不计次数）" if blocked else "")
                )
                return None
            why = f"{tries} 次都失败" if tries >= RETRY_MAX_TRIES else f"这一晚等不到下一次（已真失败 {tries} 次）"
            say(f"diary 改用模板: {aid} {day_str} 最后一次原因 {fail.code}，{why}")
            saved = self.save_generated(job, self._template_text(job), "demo", now)
        else:
            if text:
                saved = self.save_generated(job, text, "live_api", now)
            else:  # the model answered but the answer is unusable: as before, the template, not a retry
                saved = self.save_generated(job, self._template_text(job), "demo", now)
        if saved is not None:
            with self._retry_lock:
                self._retry.pop(key, None)
                for k in [k for k in self._retry if k[0] != day_str]:
                    del self._retry[k]
        return saved

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
