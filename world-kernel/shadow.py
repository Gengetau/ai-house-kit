"""Live shadow of the world kernel.

The kernel runs next to the live world.py inside shared-services, on its own database, and changes nothing
anyone sees: it never writes world.db (read-only, for genesis and comparison samples), never hands a card,
availability or line to a bot, and never touches QQ, memory or diaries. Off unless WORLD_KERNEL=shadow.

- Genesis: once, from a read-only look at world.db reality (source=legacy_genesis): where each housemate is
  and her hunger/energy. The P1a catalog has three places, so study/yard/bath/room/outside all map to
  exit_rooms ("not in the shared rooms"); the report says so.
- Life: wall clock. L0 habits come from world.py ROUTINE (the sleeping slot, by one fixed rule). Guide offers
  as in P1a. She thinks with the real model only at a decision point: an offer she can see, being hungry with
  nothing ready to eat, or being spoken to. Each call is reserved in the kernel budget first (pool of its own:
  10 an hour, 60 a day) and is never retried beyond the kernel's one corrective retry.
- Outside input: the shopping trip in world.py ROUTINE (nell, 16:30-17:30) restocks the kitchen at 17:30,
  as an unwitnessed admin correction. The owner is never simulated: presence stays absent.
- Comparison: every 10 minutes one row per housemate (legacy reality vs kernel) in a separate samples
  database; report() turns a JST day into markdown.
- Catalog: p1b runs on the bigger house of
  world-catalog p1b.json: kitchen, living, study, yard, bath, a bedroom of her own, "away" for going out;
  eight everyday activities through begin_activity; bread/onigiri/fruit as ready food; breakfast and lunch
  offers. p1b has its own world and files (kernel-p1b.db, samples-p1b.db); p1a keeps the first ones.

  python3 shadow.py --report 2026-10-25   # inside shared-services, prints the report
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from worldkernel import admin
from worldkernel import vocab as vocab_p1a
from worldkernel import vocab_p1b
from worldkernel.budget import Budget
from worldkernel.cognition import Cognition
from worldkernel.guide import RULES, RULES_P1B, Guide, jst_minute
from worldkernel.habits import HabitBook
from worldkernel.kernel import Kernel
from worldkernel.routine import Routine
from worldkernel.meals import Meals
from worldkernel.store import LockHeld, Store, WallClock

ROSTER = ("nell", "elsa", "alma", "mina", "yuki", "wanda")
HOLDER = "shared-services-shadow"
STEP_S = 30
SAMPLE_S = 600
THINK_GAP_S = 20 * 60  # at most one decision of her own per 20 minutes
HUNGRY_GAP_S = 2 * 3600  # being hungry with nothing ready asks again only after this long
LIMITS = {"total": 10 ** 9, "per_hour": 10, "per_day": 60}
POOL = "world-kernel/default-pool"
RESTOCK_AT = 17 * 60 + 30
STOCK = {"egg": 12, "tofu": 4, "miso": 4, "rice": 6}  # enough for three meals a day for six
ANCHORS = ("stove", "pan", "pot", "rice_cooker", "fridge")
SHARED = ("kitchen", "living")
HOME = ("kitchen", "living", "study", "yard", "bath")
JST = datetime.timezone(datetime.timedelta(hours=9))

# one entry per catalog profile: its vocabulary, world, files, guide rules and what the kitchen holds
PROFILES = {
    "p1a": {"vocab": vocab_p1a, "world": "ai-house-shadow-1", "epoch": "shadow-1", "files": ("kernel.db", "samples.db"),
            "rules": RULES, "stock": STOCK, "anchors": [(k, "kitchen") for k in ANCHORS]},
    "p1b": {"vocab": vocab_p1b, "world": "ai-house-shadow-2", "epoch": "shadow-2",
            "files": ("kernel-p1b.db", "samples-p1b.db"), "rules": RULES_P1B,
            "stock": dict(STOCK, bread=6, onigiri=6, fruit=6),  # ready food: a snack needs no cook
            "anchors": [(k, "kitchen") for k in ANCHORS] + [("bathtub", "bath"), ("game_console", "living")]},
}

SAMPLES = """
CREATE TABLE IF NOT EXISTS samples (at REAL NOT NULL, actor TEXT NOT NULL, legacy_location TEXT, legacy_activity TEXT,
  legacy_availability TEXT, legacy_hunger REAL, legacy_energy REAL, kernel_location TEXT, kernel_activity TEXT,
  kernel_hunger REAL, kernel_energy REAL, PRIMARY KEY (at, actor));
CREATE TABLE IF NOT EXISTS runner (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS errors (at REAL NOT NULL, what TEXT NOT NULL);
"""


def log(msg):
    print(f"[world-kernel-shadow] {msg}", flush=True)


def coarse(location):
    """world.py location -> the P1a catalog's three places."""
    return location if location in SHARED else "exit_rooms"


def place(profile, actor, location):
    """world.py location -> a place of this profile. p1b: the five shared rooms as they are, her room -> her
    own bedroom, every place outside the house -> away."""
    if profile == "p1a":
        return coarse(location)
    if location in HOME:
        return location
    return vocab_p1b.bed(actor) if location == "room" else "away"


def public(profile, location):
    """A kernel place as the legacy world names it, for the comparison (p1a: the coarse three)."""
    return location if profile == "p1a" else vocab_p1b.public_location(location)


def legacy_public(profile, location):
    """A legacy location at the comparison's precision (p1b: everything outside the house counts as away)."""
    if profile == "p1a":
        return coarse(location or "")
    return location if location in HOME + ("room",) else "away"


def sleep_window(slots):
    """The sleeping slot of a world.py ROUTINE entry as 'HH:MM-HH:MM' (fixed derivation rule)."""
    for start, end, _loc, act, *_ in slots:
        if act == "sleeping":
            return f"{start}-{end}"
    return None


def profiles(routine):
    """Habit profiles from world.py ROUTINE; the version is a hash of exactly what was read."""
    out = {}
    for a in ROSTER:
        slots = routine.get(a) or []
        version = hashlib.sha256(json.dumps(slots, ensure_ascii=False).encode()).hexdigest()[:12]
        out[a] = {"actor_id": a, "profile_ref": f"world.py:ROUTINE:{a}", "profile_version": version,
                  "sleep": sleep_window(slots), "hunger_eat_at": 70, "rest_at_energy": 20}
    return out


def legacy_reality(world_db):
    """Read-only look at world.db reality; {} when the file is not there."""
    if not os.path.exists(world_db):
        return {}
    c = sqlite3.connect(Path(world_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return {r["actor"]: dict(r) for r in c.execute(
            "SELECT actor, location, activity, availability, hunger, energy FROM reality")}
    finally:
        c.close()


def genesis(reality, profile="p1a"):
    P = PROFILES[profile]
    actors = {}
    for a in ROSTER:
        r = reality.get(a) or {}
        actors[a] = {"location": place(profile, a, r.get("location") or "room"), "profile_ref": f"world.py:ROUTINE:{a}",
                     "body": {"hunger": float(r["hunger"]) if r.get("hunger") is not None else 30.0,
                              "energy": float(r["energy"]) if r.get("energy") is not None else 75.0}}
    resources = [{"id": k, "kind": "food", "location": "kitchen", "qty": q} for k, q in P["stock"].items()]
    resources += [{"id": k, "kind": "anchor", "location": where, "qty": 1} for k, where in P["anchors"]]
    return {"actors": actors, "resources": resources}


# ---------------------------------------------------------------------------------------------- model
def api_key():
    key = os.environ.get("WORLD_KERNEL_API_KEY", "")
    if not key:
        raise RuntimeError("WORLD_KERNEL_API_KEY is not set")
    return key


def chat_completion(system, user, model, base, timeout=90):
    """One chat completion on an OpenAI-compatible endpoint. Returns (text, total_tokens).
    The key is read from WORLD_KERNEL_API_KEY and never leaves this function."""
    body = {"model": model, "temperature": 0.7, "max_tokens": 1500,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(), {
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    text = str((((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or "")
    return text, int((data.get("usage") or {}).get("total_tokens") or 0)


def first_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in the answer")
    return json.loads(m.group(0))


DECIDE_FORMAT = """只输出一个 JSON：{"steps": [...]}，最多 3 步，什么都不想做就输出 {"steps": []}。
可用的步骤（只能用这些）：
- {"action_type": "move", "args": {"to": "kitchen|living|exit_rooms"}}  （exit_rooms = 回自己房间或去别处）
- {"action_type": "rest", "args": {"seconds": 60-3600}}
- {"action_type": "wait", "args": {"seconds": 60-1800}}
- {"action_type": "cook", "args": {"recipe": "omelette|miso_soup|rice"}, "uses": ["用到的 resource_id"]}  （只能在厨房）
- {"action_type": "eat", "args": {"item": "现成能吃的东西", "resource_id": "它的 resource_id"}}
- {"action_type": "sleep", "args": {"seconds": 600-36000}}  （只能在 exit_rooms）
- {"action_type": "say", "args": {"to": "可选，对谁说"}, "about": []}  （只写想对谁说，不要写台词）
采用某个引导机会时，在那一步加 "guide_id"。不要替别人决定，不要编你没看到的事。"""

ABOUT = re.compile(r"^(?:event:\d+|material:\S+)$")


def decide_format(profile, me):
    """The step list she may use, for her (p1b names her own bedroom and the activities it allows)."""
    if profile == "p1a":
        return DECIDE_FORMAT
    V = vocab_p1b
    room = V.bed(me)
    acts = []
    for k, a in V.ACTIVITIES.items():
        where = "、".join("自己卧室" if w == "own_room" else w for w in a["where"])
        acts.append(f"{k}（{a['label']}，在 {where}，{a['min'] // 60 or 1}-{a['max'] // 60} 分钟）")
    return ("只输出一个 JSON：{\"steps\": [...]}，最多 3 步，什么都不想做就输出 {\"steps\": []}。\n"
            "可用的步骤（只能用这些）：\n"
            f"- {{\"action_type\": \"move\", \"args\": {{\"to\": \"kitchen|living|study|yard|bath|{room}|away\"}}}}"
            f"  （{room} = 你自己的卧室，away = 出门；别人的卧室不能进）\n"
            "- {\"action_type\": \"begin_activity\", \"args\": {\"activity\": \"下面之一\", \"seconds\": 秒数}}  "
            "（要先走到能做它的地方）\n  " + "\n  ".join(acts) + "\n"
            "- {\"action_type\": \"rest\", \"args\": {\"seconds\": 60-3600}}\n"
            "- {\"action_type\": \"wait\", \"args\": {\"seconds\": 60-1800}}\n"
            "- {\"action_type\": \"cook\", \"args\": {\"recipe\": \"omelette|miso_soup|rice\"}, \"uses\": [\"用到的 resource_id\"]}"
            "  （只能在厨房）\n"
            "- {\"action_type\": \"eat\", \"args\": {\"item\": \"现成能吃的东西\", \"resource_id\": \"它的 resource_id\"}}"
            "  （面包、饭团、水果和做好的饭都能直接吃，要在它所在的房间）\n"
            f"- {{\"action_type\": \"sleep\", \"args\": {{\"seconds\": 600-36000}}}}  （只能在 {room}）\n"
            "- {\"action_type\": \"say\", \"args\": {\"to\": \"可选，对谁说\"}, \"about\": []}  "
            "（只写想对谁说，不要写台词；about 一般留空，要提某件事只能写 recent 里的 \"event:编号\"）\n"
            "采用某个引导机会时，在那一步加 \"guide_id\"。不要替别人决定，不要编你没看到的事。")


class LiveModel:
    """Cognition's model: ctx -> {"steps"} or, for purpose=express, {"text"}. chat(system, user) -> (text, tokens)."""

    def __init__(self, chat, selves, rules="", profile="p1a"):
        self.chat, self.selves, self.rules, self.calls, self.profile = chat, selves, rules, 0, profile

    def __call__(self, ctx):
        self.calls += 1
        me = ctx["actor"]
        now = datetime.datetime.now(JST).strftime("%m-%d %H:%M")
        seen = {k: ctx.get(k) for k in ("location", "activity", "body", "here", "items_here", "recent", "offers",
                                        "materials")}
        if self.profile == "p1b" and ctx.get("meal") is not None:
            seen["meal"] = ctx["meal"]
        system = (f"你是 AI小屋里的{self.selves.get(me, me)}\n现在是日本时间 {now}。"
                  "只根据下面给你的、你自己看到和知道的东西行动。输出必须是 JSON。")
        if ctx.get("purpose") == "express":
            who = ctx.get("to") or "这个房间里的人"
            user = (f"你决定对{who}说一句话。下面是对方也能听到、看到的内容：\n"
                    f"{json.dumps(seen, ensure_ascii=False)}\n"
                    '用你自己的口吻说一句，不超过 40 字，只输出 {"text": "..."}。')
            text, tokens = self.chat(system, user)
            got = first_json(text)
            return {"text": str(got.get("text") or "")[:120], "tokens": tokens}
        user = (f"你现在的情况（只有你知道的）：\n{json.dumps(seen, ensure_ascii=False)}\n"
                f"让你想一想的原因：{ctx.get('decision')}\n\n{decide_format(self.profile, me)}"
                + ("\n现在是统一正餐时间。面包、饭团、水果单吃是零食，不算完成正餐；优先吃现有熟食或做饭。"
                   '不想或不能参加可在JSON加 "meal_response":{"choice":"skip","reason":"not_hungry|unwell|busy|away|personal_choice"}。'
                   if ctx.get('meal') and ctx['meal']['status']=='pending' else ''))
        text, tokens = self.chat(system, user)
        got = first_json(text)
        steps = [s for s in (got.get("steps") or []) if isinstance(s, dict) and isinstance(s.get("action_type"), str)]
        for s in steps:  # a topic written as free words is no ref; the kernel would refuse the whole line for it
            if isinstance(s.get("about"), list):
                s["about"] = [x for x in s["about"] if isinstance(x, str) and ABOUT.match(x)]
        return {"steps": steps[:3], "latency": 0, "tokens": tokens, "meal_response": got.get("meal_response")}


# ---------------------------------------------------------------------------------------------- runner
class Shadow:
    def __init__(self, db_path, samples_path, world_db, routine, selves, chat, clock=None, profile="p1a"):
        self.world_db, self.clock = world_db, clock or WallClock()
        self.profile, P = profile, PROFILES[profile]
        self.samples = sqlite3.connect(samples_path, isolation_level=None, check_same_thread=False)
        self.samples.row_factory = sqlite3.Row
        self.samples.executescript(SAMPLES)
        self.store = Store(db_path, self.clock, world_id=P["world"], synthetic=True, holder=HOLDER,
                           stream_epoch=P["epoch"])
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER, vocab=P["vocab"])
        if not self.store.meta("seeded"):
            self.k.seed(genesis(legacy_reality(world_db), profile), source="legacy_genesis")
            log(f"seeded from world.db (legacy_genesis, catalog {profile})")
        self.book = HabitBook(self.k)
        for p in profiles(routine).values():
            if p["sleep"]:
                self.book.install(p)
        self.guide = Guide(self.store, rules=P["rules"], kernel=self.k)
        self.routine = Routine(self.k, book=self.book)
        self.meals = Meals(self.k, ROSTER, routine=self.routine) if profile == "p1b" else None
        self.model = LiveModel(chat, selves, profile=profile)
        self.budget = Budget(self.store, pool=POOL, limits=LIMITS)
        self.cog = Cognition(self.k, self.budget, self._ask, self.guide, self.routine,
                             on_refused=self.meals.defer_cook if self.meals else None)
        self._kind, self.last_think, self.considered, self.heard = None, {}, set(), {}
        last = self._get("last_step")
        if last:
            self.k.resume(float(last))
            log(f"resumed after an outage since {datetime.datetime.fromtimestamp(float(last), JST):%m-%d %H:%M}")

    def _get(self, key):
        r = self.samples.execute("SELECT value FROM runner WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set(self, key, value):
        self.samples.execute("INSERT INTO runner(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET"
                             " value=excluded.value", (key, str(value)))

    def _ask(self, ctx):
        meal = self.meals.context(ctx['actor']) if self.meals else None
        if self.meals and ctx.get('purpose') != 'express':
            # A serving reserved by somebody eating is no longer available to her.
            # Keep the same local visibility boundary; never add global inventory.
            with self.store.read() as c:
                items = [dict(item, qty=self.k._free_qty(c, item['resource_id']) or 0)
                         for item in ctx['items_here']]
            ctx = dict(ctx, items_here=[item for item in items if item['qty'] > 0])
        result = self.model(dict(ctx, decision=self._kind, meal=meal))
        response = result.pop('meal_response', None)
        if meal and isinstance(response, dict):
            self.meals.respond(ctx['actor'], response.get('choice'), response.get('reason'))
        return result

    def busy(self, a):
        return (bool(self.k.actor(a)["action_id"]) or a in self.cog.plans
                or any(job['actor'] == a for job in self.cog.pending))

    def think(self, a, kind):
        now = self.clock.now()
        if now - self.last_think.get(a, 0) < THINK_GAP_S:
            return None
        self.last_think[a] = now
        self._kind = kind
        # A scheduled meal has one preparation decision, not an automatic stale-plan retry.
        return self.cog.begin(a, kind, retries=1 if kind.startswith('scheduled_meal_') else 0)

    def restock(self, now):
        day = datetime.datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d")
        if jst_minute(now) < RESTOCK_AT or self._get("restocked") == day:
            return
        for rid, qty in PROFILES[self.profile]["stock"].items():
            r = self.k.resource(rid)
            if r and r["qty"] < qty:
                admin.correction(self.k, "admin:world", rid, qty, "world input: nell's shopping trip (world.py ROUTINE)")
        self._set("restocked", day)

    def decision_points(self):
        for a in ROSTER:
            if self.busy(a) or self.k.actor(a)["activity"] == "sleeping":
                continue
            meal = self.meals.act(a) if self.meals else {}
            if meal.get('action'):
                if meal['result'].get('status') in ('running', 'accepted'):
                    continue
                # A rejected routine action must not hide conversations or normal needs.
            ctx = self.cog.context(a)
            for e in ctx["recent"]:
                if e["kind"] == "said" and e.get("to") == a and e["event_id"] > self.heard.get(a, 0):
                    self.heard[a] = e["event_id"]
                    if self.think(a, f"addressed_by:{e['actor']}"):
                        break
            day = datetime.datetime.fromtimestamp(self.clock.now(), JST).strftime("%Y-%m-%d")
            for o in ctx["offers"]:
                if self.meals and any(o['guide_id'].startswith('guide-' + key + '-') for key in ('breakfast','lunch','dinner')):
                    continue  # shared meal clock owns meal decisions, not repeated half-hour guide offers
                seen = (a, o["guide_id"].rsplit("-", 1)[0], day)  # one look per kind of offer a day, not per slot
                if seen not in self.considered and not self.busy(a):
                    self.considered.add(seen)
                    self.think(a, f"guide:{o['guide_id']}")
            if not self.busy(a):
                if (meal.get('would_call')
                        and self.clock.now() - self.last_think.get(a, 0) >= THINK_GAP_S
                        and self.meals.claim_decision(a)):
                    self.think(a, meal['would_call'])
                if self.busy(a):
                    continue
                got = self.routine.check(a)
                # While waiting for this meal, hunger must not re-enter the same preparation loop.
                if (not meal.get('would_call') and got.get("would_call")
                        and self.clock.now() - self.last_think.get(a, 0) >= HUNGRY_GAP_S):
                    self.think(a, got["would_call"])

    def sample(self, now):
        last = float(self._get("last_sample") or 0)
        if now - last < SAMPLE_S:
            return
        legacy = legacy_reality(self.world_db)
        for a in ROSTER:
            r, k = legacy.get(a) or {}, self.k.actor(a)
            self.samples.execute(
                "INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, a, r.get("location"), r.get("activity"), r.get("availability"), r.get("hunger"), r.get("energy"),
                 k["location"], k["activity"], k["body"].get("hunger"), k["body"].get("energy")))
        self._set("last_sample", now)

    def step(self):
        now = self.clock.now()
        self.restock(now)
        if self.meals:
            self.meals.sync()
        self.guide.run([])
        self.k.expire_presence()
        self.k.tick()
        self.cog.run_due()
        self.k.tick()
        self.cog.advance_plans()
        if self.meals:
            self.meals.sync()  # consume this tick's eat completion before choosing another serving
        self.decision_points()
        self.k.tick()
        if self.meals:
            self.meals.sync()
        self.sample(now)
        self._set("last_step", now)

    def run_forever(self, stop=None):
        fails = 0
        while not (stop and stop.is_set()):
            try:
                self.step()
                fails = 0
            except LockHeld as e:
                log(f"lock lost, stopping: {e}")
                return
            except Exception as e:  # never take shared-services down; back off and keep a short record
                fails += 1
                self.samples.execute("INSERT INTO errors VALUES(?,?)", (time.time(), f"{type(e).__name__}: {e}"[:500]))
                log(f"step failed ({fails}): {type(e).__name__}: {e}")
                if fails >= 5:
                    traceback.print_exc()
                    time.sleep(600)
                    fails = 0
            time.sleep(STEP_S)


def start(runtime_dir, world_db, routine, selves, rules=""):
    """Start the shadow in a daemon thread (called by shared-services when WORLD_KERNEL=shadow)."""
    model = os.environ.get("WORLD_KERNEL_MODEL", "example/chat-model")
    base = os.environ.get("WORLD_KERNEL_BASE_URL", "http://127.0.0.1:8000")
    profile = os.environ.get("WORLD_KERNEL_CATALOG", "p1a")
    if profile not in PROFILES:
        log(f"unknown WORLD_KERNEL_CATALOG {profile!r}, not starting")
        return None
    kernel_db, samples_db = PROFILES[profile]["files"]
    os.makedirs(runtime_dir, exist_ok=True)

    def chat(system, user):
        return chat_completion(system, user, model, base)

    def run():
        try:
            sh = Shadow(os.path.join(runtime_dir, kernel_db), os.path.join(runtime_dir, samples_db), world_db,
                        routine, selves, chat, profile=profile)
            log(f"running: catalog {profile}, model {model}, pool {POOL} {LIMITS['per_hour']}/h "
                f"{LIMITS['per_day']}/day")
            sh.run_forever()
        except Exception as e:
            log(f"could not start: {type(e).__name__}: {e}")
            traceback.print_exc()

    t = threading.Thread(target=run, name="world-kernel-shadow", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------------------------- report
def _display(actor):
    """Use the shared current name when available; standalone reports need no roster module."""
    try:
        import roster
    except ImportError:
        pass
    else:
        name = getattr(roster, '_BY', {}).get(actor, {}).get('name')
        aliases = getattr(roster, 'ALIASES', {}).get(actor, ())
        if name or aliases:
            return name or aliases[0]
    return {'nell':'奈尔', 'elsa':'艾莎', 'alma':'阿尔玛', 'mina':'米娜',
            'yuki':'由纪', 'wanda':'婉妲'}.get(actor, actor)


def report(runtime_dir, day, profile="p1a"):
    """Markdown comparison of one JST day (YYYY-MM-DD), from the samples and the kernel's own tables."""
    lo = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=JST).timestamp()
    hi = lo + 86400
    kernel_db, samples_db = PROFILES[profile]["files"]
    s = sqlite3.connect(Path(os.path.join(runtime_dir, samples_db)).resolve().as_uri() + "?mode=ro", uri=True)
    s.row_factory = sqlite3.Row
    k = sqlite3.connect(Path(os.path.join(runtime_dir, kernel_db)).resolve().as_uri() + "?mode=ro", uri=True)
    k.row_factory = sqlite3.Row
    try:
        return _render_report(runtime_dir, day, profile, s, k)
    finally:
        s.close()
        k.close()


def _render_report(runtime_dir, day, profile, s, k):
    lo = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=JST).timestamp()
    hi = lo + 86400
    if profile == "p1a":
        intro = ("第一版只比两件事：在不在公共区（厨房/客厅），睡没睡。P1a 目录只有三个地方，书房、院子、浴室、"
                 "卧室和出门都算「别处」。")
    else:
        intro = ("P1b 目录：厨房、客厅、书房、院子、浴室、自己卧室按原地点比；商店街、车站、河边、店里、咖啡店和"
                 "出门在外都算「出门」(away)。")
    has_meals = k.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meal_attendance'").fetchone()
    has_meals_day = has_meals and k.execute("SELECT 1 FROM meal_attendance WHERE day=? LIMIT 1",(day,)).fetchone()
    meal_heading = '正餐已吃/3' if profile == 'p1b' else '进食动作（旧口径）'
    out = [f"# 世界内核影子对照 {day}（目录 {profile}）", "", intro, "",
           f"| 谁 | 样本 | 地点一致 | 睡/醒一致 | {meal_heading} | 内核做的活动 | 内核平均饥饿 | 旧世界平均饥饿 |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for a in ROSTER:
        rows = s.execute("SELECT * FROM samples WHERE actor=? AND at>=? AND at<?", (a, lo, hi)).fetchall()
        n = len(rows)
        if not n:
            out.append(f"| {_display(a)} | 0 | - | - | - | - | - | - |")
            continue
        same = sum(legacy_public(profile, r["legacy_location"]) == public(profile, r["kernel_location"]) for r in rows)
        asleep = sum((r["legacy_activity"] == "sleeping") == (r["kernel_activity"] == "sleeping") for r in rows)
        meals = k.execute("SELECT COUNT(*) FROM actions WHERE actor_id=? AND action_type='eat' AND state='succeeded'"
                          " AND created_at>=? AND created_at<?", (a, lo, hi)).fetchone()[0]
        if profile == 'p1b':
            meals = k.execute("SELECT count(*) FROM meal_attendance WHERE day=? AND actor=? AND status='eaten'",(day,a)).fetchone()[0] if has_meals_day else '-'
        acts = k.execute("SELECT json_extract(args,'$.activity'), COUNT(*) FROM actions WHERE actor_id=? AND"
                         " action_type='begin_activity' AND state='succeeded' AND created_at>=? AND created_at<?"
                         " GROUP BY 1 ORDER BY 2 DESC", (a, lo, hi)).fetchall()
        kh = sum(r["kernel_hunger"] or 0 for r in rows) / n
        lh = sum(r["legacy_hunger"] or 0 for r in rows) / n
        did = "、".join(f"{x}×{c}" for x, c in acts) or "-"
        out.append(f"| {_display(a)} | {n} | {same * 100 // n}% | {asleep * 100 // n}% | {meals} | {did} | {kh:.0f} | {lh:.0f} |")
    used = k.execute("SELECT COUNT(*), COALESCE(SUM(used_tokens),0) FROM budget_reservations WHERE created_at>=?"
                     " AND created_at<? AND state IN ('reserved','settled','unknown')", (lo, hi)).fetchone()
    unknown = k.execute("SELECT COUNT(*) FROM budget_reservations WHERE created_at>=? AND created_at<? AND state='unknown'",
                        (lo, hi)).fetchone()[0]
    points = k.execute("SELECT outcome, COUNT(*) FROM decision_points WHERE at>=? AND at<? GROUP BY outcome"
                       " ORDER BY outcome", (lo, hi)).fetchall()
    said = k.execute("SELECT COUNT(*) FROM events WHERE kind='said' AND occurred_at>=? AND occurred_at<?",
                     (lo, hi)).fetchone()[0]
    errors = s.execute("SELECT COUNT(*) FROM errors WHERE at>=? AND at<?", (lo, hi)).fetchone()[0]
    out += ["", f"- 模型调用：{used[0]} 次（其中结果未知 {unknown} 次），约 {used[1]} tokens；上限 "
                f"{LIMITS['per_hour']}/小时、{LIMITS['per_day']}/天",
            "- 决策点：" + ("，".join(f"{p[0]} {p[1]}" for p in points) or "无"),
            f"- 影子里说的话：{said} 句（只在影子库里，没有发给任何人）",
            f"- 运行错误：{errors} 次"]
    if profile == "p1b":
        out += ["", "## 三餐出席（每天固定三个时间带，吃零食不算完成正餐）", "",
                "| 谁 | 早餐 07:00–09:30 | 午餐 12:00–13:30 | 晚餐 18:00–20:30 | 零食次数 | 餐外主食/未分类 |",
                "| --- | --- | --- | --- | --- | --- |"]
        has = has_meals_day
        labels = {'pending':'待参加/进行中','eaten':'已吃','missed':'未吃','declined':'未参加'}
        reasons = {'sleeping':'睡觉','away':'外出','busy':'正忙','preparing':'正在备餐','no_main_food':'没有现成正餐',
                   'not_eaten':'未完成正餐','not_observed':'未观察','not_hungry':'不饿','unwell':'身体不适','personal_choice':'自主不参加'}
        for actor in ROSTER:
            cells=[]
            for slot in ('breakfast','lunch','dinner'):
                r=k.execute("SELECT status,reason FROM meal_attendance WHERE day=? AND slot=? AND actor=?",(day,slot,actor)).fetchone() if has else None
                cells.append(labels.get(r[0],r[0]) + (f"（{reasons.get(r[1], r[1])}）" if r[1] else '') if r else '未观察（旧数据不补造）')
            counts=dict(k.execute("SELECT kind,count(*) FROM meal_consumption WHERE actor=? AND at>=? AND at<? GROUP BY kind",(actor,lo,hi)).fetchall()) if has else {}
            extra = k.execute("SELECT count(*) FROM meal_consumption WHERE actor=? AND at>=? AND at<? AND kind='main' AND slot IS NULL",(actor,lo,hi)).fetchone()[0] if has else '-'
            unclassified = counts.get('unclassified',0) if has else '-'
            detail = f"{extra}/{unclassified}"
            if has and extra + unclassified:
                entries = k.execute("SELECT at,item,kind FROM meal_consumption WHERE actor=? AND at>=? AND at<? "
                                    "AND ((kind='main' AND slot IS NULL) OR kind='unclassified') "
                                    "ORDER BY at,action_id LIMIT 5", (actor,lo,hi)).fetchall()
                groups = []
                for kind in ('main', 'unclassified'):
                    groups.append('、'.join(datetime.datetime.fromtimestamp(r['at'],JST).strftime('%H:%M')
                                           + ' ' + (r['item'] or '未记名') for r in entries if r['kind'] == kind))
                detail += '：' + '；'.join(groups)
                if extra + unclassified > len(entries):
                    detail += f"…等 {extra + unclassified} 条"
            out.append(f"| {_display(actor)} | " + ' | '.join(cells) + f" | {counts.get('snack',0) if has else '-'} | {detail} |")
        out += ["", "每天固定早/午/晚三个机会；已吃必须有对应完成动作。零食、加餐不增加正餐次数，历史未观察数据不补造；未来时间带仍为待参加。"]
    return "\n".join(out) + "\n"


def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default=os.environ.get("WORLD_KERNEL_DIR", "/runtime/world-kernel-shadow"))
    ap.add_argument("--report", metavar="YYYY-MM-DD")
    ap.add_argument("--catalog", default=os.environ.get("WORLD_KERNEL_CATALOG", "p1a"), choices=sorted(PROFILES))
    a = ap.parse_args()
    if a.report:
        print(report(a.runtime, a.report, a.catalog), end="")


if __name__ == "__main__":
    _cli()
