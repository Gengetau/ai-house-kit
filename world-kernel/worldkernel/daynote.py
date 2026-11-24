"""今天的小屋: one paragraph a night over the day's kernel facts.

From 23:30 JST (and until 03:00 the next morning, for a night the house was down at 23:30) the authority writes, once
per day, 150~300 characters about that day for the owner's week page. The only input is that day's timeline as the
console shows it: world.db's script lines the kernel projector wrote (source='kernel': cooked, ate with whom,
together, went where, invites) and daylog.entries (activities, walks, outfits, shopping, sleep, rest, with who else
was there). One call through the kernel's own model route, counted first in its own budget pool (2 a day).

The answer is kept only if it passes check(): a JSON text of the right length, no quotes, no roommate / 房主 / place of
the house or town the facts do not name (the kitchen counts as named on a day with cooking or a meal: those script
lines carry no room), no word for a feeling, a mood or talk the facts do not have. check() is a backstop for the prompt,
not a proof: an invented happening in words the facts also use still passes. A failed call or a refused answer leaves the
day without a note and is not retried: the attempt is remembered in life_runtime (day_note:<date>) before the call,
its outcome in day_note:<date>:result (no text, for the operator). Stored in life1.db summaries: summary_id
'day:<date>', scene 'day', audience 'owner'; one row a day, write(day, force=True) rewrites it in place.

Off unless WORLD_KERNEL_DAY_NOTE=1 (authority.py). Never sends anything anywhere, never writes world.db.
"""
import datetime
import json
import re
import sqlite3
from pathlib import Path

from . import daylog
from . import vocab_life1 as V
from .budget import Budget
from .legacy import NAMES
from .store import dumps

JST = datetime.timezone(datetime.timedelta(hours=9))
VERSION = "day-note/1"
WRITE_AT = (23, 30)      # JST, the day's note is due from here ...
LATE_UNTIL_H = 3         # ... until 03:00 the next morning
MIN_FACTS = 3            # a day with fewer lines has nothing to tell
MAX_FACTS = 220          # the prompt carries at most this many lines (a full day was 87 on 11-07); walks go first
LO, HI = 130, 330        # characters (no whitespace) accepted; the model is asked for 150~300
POOL = "world-kernel/day-note"
LIMITS = {"total": 10 ** 9, "per_hour": 2, "per_day": 2}
QUOTES = "「」『』“”\"‘’"
# feelings, moods, motives and guesses: refused unless the day's facts themselves have the word
FEELINGS = ("开心", "高兴", "快乐", "难过", "伤心", "生气", "心情", "情绪", "兴奋", "失落", "寂寞", "孤单", "幸福", "烦躁",
            "郁闷", "愉快", "温馨", "惬意", "享受", "满足", "放松", "疲惫", "累了", "无聊", "期待", "喜欢", "想要", "想去",
            "觉得", "感到", "感觉", "似乎", "好像", "也许", "仿佛", "悠闲", "舒服", "舒适", "轻松", "热闹", "充实", "美好",
            "温暖", "平静", "安静", "满意", "笑", "愉悦", "甜蜜", "亲密", "默契", "其乐融融")
# talk: the facts never carry words, so saying anyone said / chatted / asked anything is made up
SPEECH = ("说", "聊", "问", "告诉", "讲", "谈", "商量", "答应")
# words that only look like a place or talk (日光 is a town, 日光浴 is not; 小说 is a book)
NOT_WORDS = ("日光浴", "小说")
PREFIXES = ("一楼", "附近", "镇上", "镇", "小")

SYSTEM = ("你在替 AI 小屋写一段当天的记录，给房主看。你只能根据给你的事实清单写，"
          "清单就是这一天的全部，你不知道清单以外的任何事。输出必须是 JSON。")
ASK = """请写一段 150~300 字的中文「今天的小屋」：
- 只写清单里有的人、地点和事情；清单里没有的人、地方、东西、事情一概不写，不补细节，不猜原因。
- 不写对话或台词，不用任何引号；不写任何人的心情、感受、想法和动机（比如开心、放松、累了、想要、觉得），只写她们做了什么、在哪、和谁。
- 按时间顺序挑主要的事写，零碎的走动可以略过；时间可以写钟点，也可以写上午、傍晚这类说法。
- 写成一段话，不分点，不加标题。
只输出一个 JSON：{"text": "..."}"""


def jday(ts):
    return datetime.datetime.fromtimestamp(ts, JST).date()


def t0(day):
    return datetime.datetime.combine(day, datetime.time(), JST).timestamp()


def hm(ts):
    return datetime.datetime.fromtimestamp(ts, JST).strftime("%H:%M")


def facts(db, world_db, day, now, names=None):
    """That day's timeline lines up to now, oldest first: {ts, text, with, events}. `db` is a read connection to
    life1.db (sqlite3.Row rows). The same lines the week page shows; a script 「X到了超市」 that a detail line already
    tells (same person, within 2 minutes) is left out, as the page does with the details on."""
    start = t0(day)
    end = min(start + 86400, now)
    detail = [x for x in daylog.entries(db, start, start + 86400, now, names=names) if x["ts"] < end]
    w = sqlite3.connect(Path(world_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    w.row_factory = sqlite3.Row
    try:
        script = w.execute("SELECT id, ts, actors, text FROM script WHERE source='kernel' AND ts>=? AND ts<?"
                           " ORDER BY ts, id LIMIT 2000", (start, end)).fetchall()
    finally:
        w.close()
    told = set()
    for x in detail:
        if x["reached"]:
            hit = next((r for r in script if r["id"] not in told and abs(r["ts"] - x["reached"]) <= 120
                        and x["actor"] in json.loads(r["actors"] or "[]")
                        and any(str(r["text"]).startswith(n + "到了") for n in (NAMES if names is None else names).values())), None)
            if hit:
                told.add(hit["id"])
    lines = [{"ts": r["ts"], "end": None, "text": str(r["text"] or ""), "with": [], "events": [], "kind": "script"}
             for r in script if r["id"] not in told and r["text"]]
    lines += [{"ts": x["ts"], "end": None if x["running"] else x["end"], "text": x["text"], "with": x["with"],
               "events": [int(e) for e in x["events"]], "kind": x["kind"]} for x in detail]
    lines.sort(key=lambda x: x["ts"])
    return lines


def shown(lines):
    """The lines the model is given: all of them, or on a very full day the walks left out first, then the latest."""
    if len(lines) <= MAX_FACTS:
        return lines, False
    keep = [x for x in lines if x.get("kind") != "move"]
    return keep[:MAX_FACTS], True


def render(lines, names=None):
    nmap = NAMES if names is None else names
    out = []
    for x in shown(lines)[0]:
        when = hm(x["ts"]) + (f"–{hm(x['end'])}" if x["end"] and hm(x["end"]) != hm(x["ts"]) else "")
        who = f"（同在：{'、'.join(nmap.get(a, a) for a in x['with'])}）" if x["with"] else ""
        out.append(f"{when} {x['text']}{who}")
    return "\n".join(out)


def prompt(day, lines, names=None):
    wd = "一二三四五六日"[day.weekday()]
    cut = "（这天记下的事很多，清单略去了零碎的走动。）" if shown(lines)[1] else ""
    user = (f"日期：{day.year} 年 {day.month} 月 {day.day} 日（星期{wd}），日本时间。下面是小屋今天记下的事实，"
            f"一行一件，按时间排{cut}：\n{render(lines, names=names)}\n\n{ASK}")
    return SYSTEM, user


def _names(label):
    """A room's or place's label with its short forms (一楼客厅 -> 客厅, 厨房兼餐厅 -> 厨房, 餐厅)."""
    names = {label, *label.split("兼")}
    names |= {n[len(p):] for n in list(names) for p in PREFIXES if n.startswith(p) and len(n) - len(p) >= 2}
    return {n for n in names if len(n) >= 2}


def _places(vocab=None):
    """Every room of the house and place in town, as {name: location id}."""
    vc = V if vocab is None else vocab
    out = {}
    for loc in vc.LOCATIONS:
        label = vc.label(loc)
        if label and label not in ("自己的卧室", "出门在外"):
            out.update({n: loc for n in _names(label)})
    return out


def read(answer):
    """The note from the model's answer: the "text" string of its JSON; anything else (no JSON, a cut-off answer, a
    text that is not a string) is no note."""
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return ""
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return ""
    text = got.get("text") if isinstance(got, dict) else None
    return text.strip() if isinstance(text, str) else ""


def check(text, lines, names=None, vocab=None):
    """None when the note may be kept, else why not (a short code, no text of the note)."""
    if not text:
        return "no_text"
    nmap = NAMES if names is None else names
    vc = V if vocab is None else vocab
    known = render(lines, names=nmap)
    if any(x.get("kind") == "script" and ("做了" in x["text"] or "吃了" in x["text"]) for x in shown(lines)[0]):
        known += "\n" + vc.label("kitchen")  # the cooking and the meals happen in the kitchen; their lines say no room
    size = len(re.sub(r"\s", "", text))
    if size < LO:
        return "too_short"
    if size > HI:
        return "too_long"
    if any(q in text for q in QUOTES):
        return "quoted_speech"
    words = text
    for w in NOT_WORDS:
        words = words.replace(w, "　")
    for k, n in nmap.items():
        if n in text and n not in known:
            return f"person_not_in_facts:{k}"
    if "房主" in text and "房主" not in known:
        return "owner_not_in_facts"
    places = _places(vc)
    for p, loc in places.items():
        if p in words and not any(n in known for n in _names(vc.label(loc))):
            return "place_not_in_facts"
    for f in FEELINGS:
        if f in words and f not in known:
            return "feeling"
    for s in SPEECH:
        if s in words and s not in known:
            return "speech"
    return None


class DayNote:
    def __init__(self, life, world_db, chat, log=print):
        self.life, self.s, self.world_db, self.chat, self.log = life, life.s, world_db, chat, log
        self.budget = Budget(self.s, pool=POOL, limits=LIMITS)
        self.names = dict(getattr(life, "names", NAMES))
        self.vocab = getattr(life, "V", V)

    def due(self, now=None):
        """The day whose note is due now (23:30 that day until 03:00 the next morning), or None."""
        d = datetime.datetime.fromtimestamp(self.s.clock.now() if now is None else now, JST)
        if (d.hour, d.minute) >= WRITE_AT:
            return d.date()
        if d.hour < LATE_UNTIL_H:
            return d.date() - datetime.timedelta(days=1)
        return None

    def step(self):
        day = self.due()
        if day is None or not self.life.once(f"day_note:{day.isoformat()}"):
            return None
        return self.write(day, trigger="nightly")

    def _result(self, day, outcome, **kw):
        self.life.put(f"day_note:{day.isoformat()}:result", dict(kw, outcome=outcome, at=self.s.clock.now()))
        self.log(f"day note {day.isoformat()}: {outcome}" + "".join(f" {k}={v}" for k, v in kw.items()))
        return outcome

    def write(self, day, trigger="nightly", force=False):
        """Write (or with force, rewrite) the note of `day`. Returns the outcome code; one model call at most."""
        sid = f"day:{day.isoformat()}"
        try:
            with self.s.read() as db:
                if not force and db.execute("SELECT 1 FROM summaries WHERE summary_id=?", (sid,)).fetchone():
                    return "exists"
                lines = facts(db, self.world_db, day, self.s.clock.now(), names=self.names)
                span = db.execute("SELECT MIN(event_id), MAX(event_id) FROM events WHERE occurred_at>=? AND"
                                  " occurred_at<?", (t0(day), t0(day) + 86400)).fetchone()
        except Exception as e:  # world.db locked, a row daylog cannot read: said in the result, not retried
            return self._result(day, "facts_error", error=type(e).__name__)
        if len(lines) < MIN_FACTS:
            return self._result(day, "too_few_facts", facts=len(lines))
        rid = self.budget.reserve("system", "day_note")
        if rid is None:
            return self._result(day, "no_budget", facts=len(lines))
        system, user = prompt(day, lines, names=self.names)
        try:  # outside any transaction
            answer, tokens = self.chat(system, user)
        except Exception as e:  # unknown cost: keep counting it, never ask again for this day
            self.budget.unknown(rid, type(e).__name__)
            return self._result(day, "model_error", error=type(e).__name__, facts=len(lines))
        self.budget.settle(rid, tokens)
        text = read(answer)
        why = check(text, lines, names=self.names, vocab=self.vocab)
        if why:
            # the answer's size tells a cut-off answer (max_tokens) from a refused one; never its words
            return self._result(day, "refused", reason=why, facts=len(lines), answer_chars=len(answer or ""))
        ids = sorted({e for x in lines for e in x["events"]})
        lo, hi = (span[0], span[1]) if span and span[0] is not None else (-day.toordinal(), -day.toordinal())
        with self.s.tx() as db:
            db.execute("INSERT INTO summaries(summary_id,scene,audience,source_from,source_to,summary_version,"
                       "source_event_ids,text,triggers,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                       " ON CONFLICT(summary_id) DO UPDATE SET source_from=excluded.source_from,"
                       " source_to=excluded.source_to, summary_version=excluded.summary_version,"
                       " source_event_ids=excluded.source_event_ids, text=excluded.text, triggers=excluded.triggers,"
                       " created_at=excluded.created_at",
                       (sid, "day", "owner", lo, hi, VERSION, dumps(ids), text, dumps([trigger]), self.s.clock.now()))
        return self._result(day, "written", chars=len(re.sub(r"\s", "", text)), facts=len(lines))


def note(db, day):
    """The stored note of `day` from a read connection to life1.db: {text, at} or None."""
    r = db.execute("SELECT text, created_at FROM summaries WHERE summary_id=? AND scene='day' AND audience='owner'",
                   (f"day:{day.isoformat()}",)).fetchone()
    return {"text": r[0], "at": r[1]} if r else None


def enabled(env):
    return str(env.get("WORLD_KERNEL_DAY_NOTE") or "").strip() == "1"
