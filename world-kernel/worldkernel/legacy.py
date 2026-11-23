"""Kernel -> legacy world.db projection (WORLD_KERNEL=authority).

When the kernel is the authority, the old world stops writing where she is and what she does (world.py skips
catch_up and advance). This projector is then the one writer of those rows, from kernel facts only:
  reality      location/place/activity/doing/availability/started/until/energy/hunger/sleepiness (mood and
               feel stay the old world's: emotions are not moved this week)
  script       short lines of what happened today (cooked, ate with whom, did what together, went where),
               source='kernel', knowers = who was there; the card's 今天 and the diary read them
  emotions     small, rule-made feelings from those facts (a liked dish, time together, a declined invite),
               source='kernel', entry = kernel event id (each event counts once)
  kernel_card  per person: what she wears, today's meals, what comes next (plans, bedtime)
Everything is idempotent: a restart re-projects the same rows and skips events already written.
"""
import json
import sqlite3
import time

from . import vocab_life1 as V
from . import city_projection

NAMES = dict(V.NAMES)
PLACE_LEGACY = {"supermarket": "shop", "konbini": "shop", "shotengai": "street", "cafe": "cafe", "riverside": "river",
                "park": "river", "bakery": "shop", "bookshop": "shop"}
ACT_LEGACY = {"reading": "reading", "working": "working", "gaming": "gaming", "phone": "phone", "drawing": "hobby",
              "music": "hobby", "gardening": "hobby", "drama": "hobby", "chores_clean": "chores",
              "chores_dishes": "chores", "laundry": "chores", "bathing": "bathing", "tea_time": "socializing",
              "movie_night": "socializing", "board_game": "socializing", "stroll": "walking", "shopping": "outing",
              "eat_out": "outing", "movie_theater": "outing", "sightseeing": "outing", "onsen": "outing",
              # / catalog life1 ids (anything not listed is "hobby")
              "idling": "idle", "resting": "resting", "eating_snack": "eating", "kitchen_help": "chores",
              "housework": "chores", "phone_browsing": "phone", "being_together": "socializing",
              "tea_party": "socializing", "game_tournament": "socializing", "hotpot_night": "eating",
              "stargazing_together": "socializing", "shopping_together": "outing", "library_visit": "outing",
              "cafe_time": "outing"}
AVAIL = {"sleeping": "sleeping", "bathing": "occupied", "cooking": "limited", "eating": "limited",
         "outing": "limited", "socializing": "limited", "walking": "limited", "chores": "limited"}
CARD_SCHEMA = "create table if not exists kernel_card(actor text primary key, data text not null, updated real not null)"
# the projection's own receipts live in the TARGET db, in the same transaction as the rows they cover:
# kernel_bind = which kernel world/stream this world.db takes facts from (+ its cursor), kernel_fx = one key per
# written fact (world#epoch:event:what), so a crash on either side retries without losing or doubling anything.
BIND_SCHEMA = ("create table if not exists kernel_bind(k text primary key, v text not null)",
               "create table if not exists kernel_fx(key text primary key, at real not null)")
TOGETHER_MIN_S = 600  # 「一起」 needs at least ten minutes of the same activity in the same room
# The rows of shared-services/world.py this projector writes, for offline runs (simulate.py, tests) only.
LEGACY_SCHEMA = """
create table if not exists reality(actor text primary key, location text not null, place text not null,
  activity text not null, doing text not null, mood text not null, energy integer not null, hunger integer not null,
  sleepiness integer not null, availability text not null, started real not null, until real not null,
  source text not null, entry integer, updated real not null, cursor real not null, next_due real not null,
  fails integer not null default 0, feel text not null default '{}');
create table if not exists script(id integer primary key, ts real not null, actors text not null, scene integer,
  text text not null, source text not null, created real not null, knowers text);
create table if not exists emotions(id integer primary key, actor text not null, ts real not null,
  emotion text not null, strength integer not null, why text not null, source text not null, entry integer);
"""


def offline_world(path, roster=None):
    """A legacy-shaped world.db with the six in bed (offline runs only; the live one belongs to world.py)."""
    c = sqlite3.connect(path)
    c.executescript(LEGACY_SCHEMA)
    for s in BIND_SCHEMA:
        c.execute(s)
    c.execute("insert or ignore into kernel_bind values('mode', 'offline')")  # synthetic worlds may write here
    for a in (roster or V.PEOPLE):
        c.execute("insert or ignore into reality values(?, 'room', '卧室', 'sleeping', '睡觉', '平静', 60, 30, 70,"
                  " 'sleeping', 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
    c.commit()
    c.close()


def _who(ids, names=None):
    nm = names or NAMES
    return "、".join(nm.get(i, i) for i in ids)


class Projector:
    def __init__(self, life, world_db):
        self.names = dict(getattr(life, 'names', NAMES))
        self.V = getattr(life, 'V', V)
        self.life, self.k, self.path = life, life.k, world_db
        s = self.k.s
        self.world = f"{s.world_id}#{s.meta('stream_epoch', '1')}"
        self.synthetic = bool(s.synthetic)
        self._outings_cache = {}

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute(CARD_SCHEMA)
        for s in BIND_SCHEMA:
            c.execute(s)
        self._bind(c)
        return c

    def _bind(self, c):
        """a world.db takes facts from ONE kernel world/stream; a synthetic (simulated) kernel never writes into
        a real one (only into offline_world() targets). Refuses loudly instead of mixing two worlds' event ids."""
        bind = {r[0]: r[1] for r in c.execute("select k, v from kernel_bind")}
        if self.synthetic and bind.get("mode") != "offline":
            c.close()
            raise RuntimeError("legacy projection: a synthetic kernel may not write into a real world.db")
        if bind.get("world") and bind["world"] != self.world:
            c.close()
            raise RuntimeError(f"legacy projection: this world.db is bound to {bind['world']}, not {self.world}")
        if not bind.get("world"):
            c.execute("insert or replace into kernel_bind values('world', ?)", (self.world,))
            c.commit()

    def _once(self, c, key):
        """True the first time this fact key is seen in the target (inside the caller's transaction)."""
        k = f"{self.world}:{key}"
        if c.execute("select 1 from kernel_fx where key=?", (k,)).fetchone():
            return False
        c.execute("insert into kernel_fx values(?, ?)", (k, self.life.clock.now()))
        return True

    def _together(self, db, party, activity, action_id, actor, start, end):
        """who really did the same plan's activity with her: same party and activity, started in the same room,
        and at least TOGETHER_MIN_S of overlap, plan members only. Plan members who never came are not 'together'."""
        me = db.execute("SELECT location FROM events WHERE action_id=? AND kind='action_started'", (action_id,)).fetchone()
        try:
            p = db.execute("SELECT host, members FROM plans WHERE plan_id=?", (party,)).fetchone()
        except sqlite3.OperationalError:
            p = None
        members = {p["host"], *json.loads(p["members"] or "[]")} if p else set()
        if actor not in members:
            return []
        out = []
        for r in db.execute("SELECT a.action_id, a.actor_id, a.started_at, a.finished_at, a.state,"
                            " (SELECT location FROM events x WHERE x.action_id=a.action_id AND x.kind='action_started')"
                            " AS loc FROM actions a WHERE a.action_type='begin_activity' AND a.action_id!=?"
                            " AND json_extract(a.args,'$.party')=? AND json_extract(a.args,'$.activity')=?"
                            " AND a.started_at IS NOT NULL", (action_id, party, activity)):
            if r["actor_id"] == actor or r["actor_id"] not in members or r["actor_id"] not in self.V.PEOPLE or not me \
                    or r["loc"] != me[0]:
                continue
            their_end = r["finished_at"] if r["finished_at"] is not None else self.life.clock.now()
            if min(end, their_end) - max(start, r["started_at"]) >= TOGETHER_MIN_S:
                out.append(r["actor_id"])
        return sorted(set(out))

    # ------------------------------------------------------------------ what she is doing now
    def _outing_phase(self, a, loc, legacy, place, action_id):
        job = self.life.get(f"outing:active:{a}")
        if not job or job.get("state") not in ("running", "returning") or not job.get("place"):
            return None
        if action_id and job.get("action_id") != action_id:
            return None
        steps = job.get("steps") or []
        idx = int(job.get("index", 0))
        dest = self.V.label(job["place"])
        if idx >= len(steps):
            return ("away", "路上", "outing", f"从{dest}回家的路上") if loc == "away" else None
        step = steps[idx]
        if step.get("packing"):
            return (legacy, place, "outing", f"收拾去{dest}的行李")
        if step.get("unpack"):
            return (legacy, place, "chores", "收拾买回来的东西")
        arr_idx = next((i for i, s in enumerate(steps)
                        if s.get("action") == "move" and (s.get("args") or {}).get("to") == job["place"]), None)
        dep_idx = next((i for i, s in enumerate(steps)
                        if (arr_idx is None or i > arr_idx) and s.get("action") == "move"
                        and (s.get("args") or {}).get("to") in ("away", "living")), None)
        if job["state"] == "running" and arr_idx is not None and idx <= arr_idx:
            plan = self.life.plans.plan(job.get("plan_id")) or {}
            others = [m for m in (plan.get("members") or []) if m != a and m in self.V.PEOPLE]
            doing = f"去{dest}的路上" + (f"（和{_who(others, self.names)}约好一起）" if others else "")
            return ("away", "路上", "outing", doing)
        if job["state"] == "returning" or (dep_idx is not None and idx >= dep_idx):
            return ("away", "路上", "outing", f"从{dest}回家的路上")
        return None

    def now_row(self, a):
        me = self.k.actor(a)
        loc = me["location"]
        if loc.startswith(self.V.PLACE):
            legacy, place = PLACE_LEGACY.get(self.V.place_id(loc), "away"), self.V.label(loc)
        elif loc.startswith(self.V.BEDROOM):
            legacy, place = "room", "自己的卧室"
        elif loc == "away":
            legacy, place = "street", "家门外"
        else:
            legacy, place = loc, self.V.label(loc)
        now = self.life.clock.now()
        act, doing, started, until = "idle", "发呆", now, now + 1800
        if me["action_id"]:
            with self.k.s.read() as c:
                x = c.execute("SELECT action_type,args,started_at,due_at FROM actions WHERE action_id=?",
                              (me["action_id"],)).fetchone()
            args = json.loads(x["args"] or "{}")
            started, until = x["started_at"] or started, x["due_at"] or until
            t = x["action_type"]
            if t == "cook":
                act, doing = "cooking", f"在厨房做{self.V.RECIPES[args['recipe']]['label']}"
            elif t == "eat":
                act, doing = "eating", f"吃{self.V.CATALOG['items'].get(args['item'], {}).get('label', args['item'])}"
            elif t == "sleep":
                act, doing = "sleeping", "睡觉"
            elif t == "rest":
                act, doing = "resting", "躺着歇一会儿"
            elif t == "wait":
                act, doing = "idle", "等着"
            elif t == "move":
                to = args.get("to", "")
                act = "outing" if to.startswith(self.V.PLACE) or loc.startswith(self.V.PLACE) else "idle"
                doing = f"去{self.V.label(to)}的路上"
                if to.startswith(self.V.PLACE):
                    legacy, place = "away", "路上"
            elif t == "begin_activity":
                name = args.get("activity")
                act = ACT_LEGACY.get(name, "hobby")
                doing = self.V.ACTIVITIES.get(name, {}).get("label", name)
                if args.get("party"):
                    # only who is actually doing it with her right now, here
                    with self.k.s.read() as c:
                        p = c.execute("SELECT host, members FROM plans WHERE plan_id=?", (args["party"],)).fetchone()
                        members = {p["host"], *json.loads(p["members"] or "[]")} if p else set()
                        others = sorted({r[0] for r in c.execute(
                            "SELECT x.actor_id FROM actions x JOIN actors b ON b.action_id=x.action_id"
                            " WHERE x.state='running' AND x.action_type='begin_activity' AND x.actor_id!=?"
                            " AND json_extract(x.args,'$.party')=? AND json_extract(x.args,'$.activity')=?"
                            " AND b.location=?", (a, args["party"], name, me["location"]))} & set(self.V.PEOPLE) & members)
                    if others and a in members:
                        doing = f"和{_who(others, self.names)}一起{_bare(doing)}"
                elif loc.startswith(self.V.PLACE):
                    job = self.life.get(f"outing:active:{a}") or {}
                    plan = self.life.plans.plan(job.get("plan_id")) if job.get("plan_id") else None
                    members = set((plan or {}).get("members") or [])
                    if a in members and len(members) > 1:
                        with self.k.s.read() as c:
                            others = sorted({r[0] for r in c.execute(
                                "SELECT b.actor_id FROM actors b"
                                " LEFT JOIN actions x ON x.action_id=b.action_id AND x.state='running'"
                                " WHERE b.location=? AND b.actor_id!=? AND b.activity!='sleeping'"
                                " AND COALESCE(x.action_type,'')!='move'", (loc, a))} & set(self.V.PEOPLE) & members)
                        if others:
                            doing = f"和{_who(others, self.names)}一起{_bare(doing)}"
        phase = self._outing_phase(a, loc, legacy, place, me["action_id"])
        if phase:
            legacy, place, act, doing = phase
        city_proj = city_projection.resolve_projection(self.k.s, a, now=now)
        if city_proj:
            legacy = "away"
            place = city_proj["place"]
            act = "outing"
            doing = city_proj["doing"]
            started = city_proj["started_at_s"]
            until = city_proj["until_at_s"]
        return {"location": legacy, "place": place, "activity": act, "doing": doing,
                "availability": AVAIL.get(act, "free"), "started": started, "until": until,
                "energy": int(round(me["body"].get("energy", 70))), "hunger": int(round(me["body"].get("hunger", 30))),
                "sleepiness": int(max(0, min(100, 100 - me["body"].get("energy", 70))))}

    # ------------------------------------------------------------------ the projection
    def run(self):
        now = self.life.clock.now()
        c = self._conn()
        try:
            for a in self.V.PEOPLE:
                r = self.now_row(a)
                c.execute("update reality set location=?, place=?, activity=?, doing=?, availability=?, started=?,"
                          " until=?, energy=?, hunger=?, sleepiness=?, source='kernel', updated=? where actor=?",
                          (r["location"], r["place"], r["activity"], r["doing"], r["availability"], r["started"],
                           r["until"], r["energy"], r["hunger"], r["sleepiness"], now, a))
                c.execute("insert or replace into kernel_card values(?,?,?)",
                          (a, json.dumps(self.card(a), ensure_ascii=False), now))
            self.facts(c)
            c.commit()
        finally:
            c.close()

    def card(self, a):
        life = self.life
        now = life.clock.now()
        me = self.k.actor(a)
        outfit = (life.wardrobe.current(a) or {}).get("labels") or []
        day = life.meals._day(now)
        day_start = ((int(now) + 9 * 3600) // 86400) * 86400 - 9 * 3600
        near = []
        with self.k.s.read() as db:
            meals = {r["slot"]: r for r in db.execute("SELECT * FROM meal_attendance WHERE day=? AND actor=?", (day, a))}
            ate = {}
            for r in db.execute("SELECT item, slot FROM meal_consumption WHERE actor=? AND day=? AND slot IS NOT NULL",
                                (a, day)):
                ate[r["slot"]] = self.V.CATALOG["items"].get(r["item"], {}).get("label", r["item"])
            if me["location"].startswith(self.V.PLACE):
                own_act = db.execute("SELECT action_type FROM actions WHERE action_id=? AND state='running'",
                                     (me["action_id"],)).fetchone() if me["action_id"] else None
                if not (own_act and own_act[0] == "move"):
                    near = [self.names[r[0]] for r in db.execute(
                        "SELECT b.actor_id FROM actors b"
                        " LEFT JOIN actions x ON x.action_id=b.action_id AND x.state='running'"
                        " WHERE b.location=? AND b.actor_id!=? AND b.activity!='sleeping'"
                        " AND COALESCE(x.action_type,'')!='move' ORDER BY b.actor_id",
                        (me["location"], a)) if r[0] in self.names]
            day_map = self._day_outings_map(db, day_start, now, live=True)
            outings_today = day_map.get(a, "")
            city_proj = city_projection.resolve_projection_conn(db, a)
            if city_proj:
                city_text = city_proj["status_text"]
                if outings_today:
                    if city_text not in outings_today:
                        outings_today = f"{outings_today}；{city_text}"
                else:
                    outings_today = city_text
            outings_by_day = {day: outings_today} if outings_today else {}
            if now - day_start < 6 * 3600:
                prev_start = day_start - 86400
                prev_day = life.meals._day(prev_start + 12 * 3600)
                prev_out = self._day_outings_map(db, prev_start, day_start - 0.001, live=False).get(a, "")
                if prev_out:
                    outings_by_day[prev_day] = prev_out
            city_plan_ids = set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='city_visit_carry'").fetchone():
                city_plan_ids = {
                    r[0]
                    for r in db.execute(
                        "SELECT plan_id FROM city_visit_carry WHERE actor_id=? AND plan_id IS NOT NULL", (a,)
                    )
                }
        names = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭"}
        meal_line = []
        for slot in ("breakfast", "lunch", "dinner"):
            m = meals.get(slot)
            if not m or m["starts"] > now:
                continue
            if slot in ate:
                meal_line.append(f"{names[slot]}吃了{ate[slot]}")
            elif m["status"] == "missed" and m["reason"] == "sleeping":
                meal_line.append(f"{names[slot]}睡过去了")
            elif m["status"] in ("missed", "declined"):
                meal_line.append(f"{names[slot]}没吃")
        nxt = []
        for p in life.plans.upcoming(a, 18 * 3600):
            if p["kind"] in ("outing", "trip"):
                if life.get(f"journey:{p['plan_id']}:{a}") in ("running", "done") or p["plan_id"] in city_plan_ids:
                    continue
            elif p["start_at"] < now - 600:
                continue
            what = self.V.ACTIVITIES.get(p["what"], {}).get("label") or self.V.RECIPES.get(p["what"], {}).get("label") or p["what"]
            loc_raw = str(p.get("location") or "")
            if loc_raw.startswith(self.V.PLACE) or (":" in loc_raw and loc_raw.split(":", 1)[0] in city_projection.AREA_LABELS):
                loc_label = self.V.label(loc_raw) if loc_raw.startswith(self.V.PLACE) else loc_raw
                if loc_label == loc_raw:
                    pid = (self.V.place_id(loc_raw) if loc_raw.startswith(self.V.PLACE) else loc_raw) or ""
                    loc_label = city_projection.AREA_LABELS.get(pid.split(":", 1)[0], loc_label)
                what = f"去{loc_label}{_bare(what)}"
            who = [m for m in p["members"] if m != a]
            nxt.append(f"{_hhmm(p['start_at'])} {'和' + _who(who) + '一起' if who else ''}{what}")
        cue, _ = life.bedtime_window(a, now)
        if cue > now:
            nxt.append(f"{_hhmm(cue)} 差不多该睡了")
        return {"outfit": "、".join(outfit), "meals": "；".join(meal_line), "outings": outings_today,
                "outings_by_day": outings_by_day, "near": near, "next": nxt[:3], "city_outing": city_proj}

    def _day_outings_map(self, db, day_start, end, live=True):
        from . import daylog
        end = min(day_start + 86400, end)
        if end < day_start:
            return {}
        max_eid = db.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()[0]
        actor_rows = {r["actor_id"]: r for r in db.execute(
            "SELECT b.actor_id, b.location, b.activity, x.action_type, x.args, x.started_at FROM actors b"
            " LEFT JOIN actions x ON x.action_id=b.action_id AND x.state='running'") if r["actor_id"] in self.names}
        outside_open = any((r["location"] or "").startswith(self.V.PLACE) or r["location"] == "away"
                           for r in actor_rows.values()) if live else False
        sig = (max_eid, day_start, live, int(end // 60) if outside_open else -1,
               tuple((k, actor_rows[k]["location"], actor_rows[k]["action_type"]) for k in sorted(actor_rows)) if live else ())
        cached = self._outings_cache.get((day_start, live))
        if cached and cached[0] == sig:
            return cached[1]
        evs = db.execute(
            "SELECT event_id, actor_id, kind, action_id, occurred_at, location, payload FROM events"
            " WHERE occurred_at>=? AND occurred_at<=? AND kind IN ('arrived','departed','purchased','action_started')"
            " ORDER BY occurred_at, event_id",
            (day_start - 3 * 86400, end)).fetchall()
        stay = {p: [] for p in self.names}
        arrs_by, deps_by, homes_by, purch_by, act_loc = {p: [] for p in self.names}, {p: [] for p in self.names}, {p: [] for p in self.names}, {p: [] for p in self.names}, {}
        for ev in evs:
            aid = ev["actor_id"]
            knd, loc, ts = ev["kind"], ev["location"] or "", ev["occurred_at"]
            if knd in ("arrived", "action_started") and aid in stay and loc:
                stay[aid].append((ts, loc))
            if knd == "action_started" and ev["action_id"]:
                act_loc[ev["action_id"]] = loc
            if aid not in self.names or ts < day_start - 86400:
                continue
            if knd == "arrived" and loc.startswith(self.V.PLACE):
                arrs_by[aid].append(ev)
            elif knd == "departed" and loc.startswith(self.V.PLACE):
                deps_by[aid].append(ev)
            elif knd == "arrived" and loc and loc != "away" and not loc.startswith(self.V.PLACE):
                homes_by[aid].append(ev)
            elif knd == "purchased" and loc.startswith(self.V.PLACE):
                purch_by[aid].append(ev)
        acts_by = {p: [] for p in self.names}
        if any(arrs_by.values()):
            for ac in db.execute(
                    "SELECT action_id, actor_id, args, COALESCE(started_at, finished_at) AS t FROM actions"
                    " WHERE action_type='begin_activity' AND COALESCE(started_at, finished_at)>=?"
                    " AND COALESCE(started_at, finished_at)<=? AND state IN ('succeeded','running','cancelled')"
                    " ORDER BY t, created_at", (day_start - 86400, end + 1)):
                if ac["actor_id"] in acts_by:
                    acts_by[ac["actor_id"]].append(ac)
        result = {}
        for a, arrs in arrs_by.items():
            if not arrs:
                continue
            deps, homes, purches, acts_a = deps_by[a], homes_by[a], purch_by[a], acts_by[a]
            me_row = actor_rows.get(a)
            parts = []
            for idx, ar in enumerate(arrs):
                ploc, arr_ts = ar["location"], ar["occurred_at"]
                next_arr = arrs[idx + 1]["occurred_at"] if idx + 1 < len(arrs) else end + 1
                dep_ev = next((d for d in deps if d["location"] == ploc and arr_ts <= d["occurred_at"] <= next_arr), None)
                dep_ts = dep_ev["occurred_at"] if dep_ev else None
                if dep_ts is None and live and me_row and me_row["location"] == ploc and me_row["action_type"] == "move":
                    m_to = json.loads(me_row["args"] or "{}").get("to")
                    if m_to != ploc and (me_row["started_at"] or end) >= arr_ts:
                        dep_ts = me_row["started_at"] or end
                home_ts = None
                if dep_ts is not None:
                    hm = next((h for h in homes if dep_ts <= h["occurred_at"] <= next_arr), None)
                    if hm:
                        home_ts = hm["occurred_at"]
                visit_end = home_ts or dep_ts or end
                if arr_ts < day_start and visit_end < day_start:
                    continue
                if arr_ts >= day_start + 86400:
                    continue
                span_end = dep_ts if dep_ts is not None else end
                mates = [m for m in daylog._with(stay, a, ploc, arr_ts, span_end) if m in self.names]
                if dep_ts is None and live:
                    here_now = {oid for oid, r in actor_rows.items()
                                if oid != a and r["location"] == ploc and r["activity"] != "sleeping"
                                and (r["action_type"] or "") != "move"}
                    mates = sorted(set(mates) | here_now)
                bought, menus = [], []
                for pe in purches:
                    if pe["location"] == ploc and arr_ts - 1 <= pe["occurred_at"] <= span_end + 1:
                        p = json.loads(pe["payload"] or "{}")
                        lbl = daylog._label(p.get("item"), p.get("name"))
                        if not lbl or p.get("kind") == "lodging":
                            continue
                        target = menus if p.get("kind") == "menu" else bought
                        if lbl not in target:
                            target.append(lbl)
                acts = []
                for ac in acts_a:
                    if act_loc.get(ac["action_id"]) == ploc and arr_ts - 1 <= ac["t"] <= span_end + 1:
                        act_name = json.loads(ac["args"] or "{}").get("activity")
                        lbl = daylog.SAY.get(act_name) or _bare(self.V.ACTIVITIES.get(act_name, {}).get("label") or str(act_name or ""))
                        if lbl and lbl not in acts:
                            acts.append(lbl)
                who_s = f"和{_who(mates, self.names)}一起" if mates else ""
                if bought:
                    b_txt = "买了" + "、".join(bought[:3]) + (f"等{len(bought)}样" if len(bought) > 3 else "")
                    what_s = f"{who_s}{b_txt}"
                elif acts:
                    what_s = f"{who_s}{acts[0]}"
                elif menus:
                    what_s = f"{who_s}吃了{menus[0]}"
                else:
                    what_s = f"（和{_who(mates, self.names)}同在）" if mates else ""
                pname = self.V.label(ploc)
                if dep_ts is None:
                    sep = "，" if what_s and not what_s.startswith("（") else ""
                    parts.append(f"{_hhmm(arr_ts)} 到了{pname}{sep}{what_s}")
                elif home_ts is None:
                    parts.append(f"{_hhmm(arr_ts)} 去{pname}{what_s}，{_hhmm(dep_ts)} 离开正在回家路上")
                else:
                    parts.append(f"{_hhmm(arr_ts)} 去{pname}{what_s}，{_hhmm(home_ts)} 回到家")
            if parts:
                result[a] = "；".join(parts)
        if len(self._outings_cache) > 8:
            self._outings_cache.clear()
        self._outings_cache[(day_start, live)] = (sig, result)
        return result

    def facts(self, c):
        """Script lines and feelings from kernel events not projected yet. The cursor is kept in the target db and
        committed with the rows: the caller's commit failing leaves both the rows and the cursor unwritten."""
        life = self.life
        r = c.execute("select v from kernel_bind where k='cursor'").fetchone()
        last = int(r[0]) if r else 0
        with self.k.s.read() as db:
            evs = db.execute("SELECT e.*, a.args AS a_args, a.action_type AS a_type FROM events e LEFT JOIN actions a"
                             " ON a.action_id=e.action_id WHERE e.event_id>? ORDER BY e.event_id LIMIT 500",
                             (last,)).fetchall()
            for e in evs:
                last = e["event_id"]
                actor = e["actor_id"]
                p = json.loads(e["payload"] or "{}")
                if e["kind"] == "invite_expired":  # nobody's act, so no actor; both of them get to know
                    self._expired(c, db, e, p)
                    continue
                if actor not in self.V.PEOPLE:
                    continue
                args = json.loads(e["a_args"] or "{}")
                knowers = [k for k in json.loads(e["knowers"] or "[]") if k in self.V.PEOPLE]
                line, feel = None, None
                line_actors = [actor]
                if e["kind"] == "action_succeeded" and e["a_type"] == "cook":
                    line = f"{self.names[actor]}做了{self.V.RECIPES[args['recipe']]['label']}"
                elif e["kind"] == "action_succeeded" and e["a_type"] == "eat" and args.get("item") in self.V.MAIN_FOODS:
                    dish = self.V.CATALOG["items"][args["item"]]["label"]
                    rid = args.get("resource_id") or ""
                    _, _, act = rid.partition("@")
                    cook = db.execute("SELECT actor_id FROM actions WHERE action_id=?", (act,)).fetchone() if act else None
                    # "together" means the same dish at the same table: someone at the next seat who ate another dish
                    # (a soup, not the beef bowl) did not share this meal and is not named in its line
                    mates = sorted({r[0] for r in db.execute(
                        "SELECT x.actor_id FROM events x JOIN actions y ON y.action_id=x.action_id WHERE"
                        " x.kind='action_succeeded' AND y.action_type='eat' AND x.actor_id!=? AND x.location=? AND"
                        " ABS(x.occurred_at-?)<900 AND json_extract(y.args,'$.item')=?",
                        (actor, e["location"], e["occurred_at"], args["item"]))} & set(self.V.PEOPLE))
                    by = f"（{self.names[cook[0]]}做的）" if cook and cook[0] != actor else ""
                    # one line per shared dish: whoever was already named in a line about this dish is not written again
                    told = c.execute("select 1 from script where source='kernel' and abs(ts-?)<900 and text like ?"
                                     " and text like ?", (e["occurred_at"], f"%{self.names[actor]}%",
                                                          f"%吃了{dish}%")).fetchone()
                    if not (mates and told):
                        line = f"{self.names[actor]}{'和' + _who(mates, self.names) + '一起' if mates else ''}吃了{dish}{by}"
                        line_actors = sorted({actor, *mates})
                    if args["item"] in self.V.PEOPLE[actor]["taste"]["likes"]:
                        feel = ("happy", 1, f"吃到了喜欢的{dish}")
                    elif len(mates) >= 2:
                        feel = ("cozy", 1, "和大家一起吃饭")
                elif e["kind"] in ("action_succeeded", "action_cancelled") and e["a_type"] == "begin_activity" \
                        and args.get("party"):
                    # written when she is done (or leaves early: what she shared still happened), from who
                    # really came and stayed, not from the plan's list
                    a = db.execute("SELECT started_at, finished_at FROM actions WHERE action_id=?",
                                   (e["action_id"],)).fetchone()
                    others = self._together(db, args["party"], args.get("activity"), e["action_id"], actor,
                                            a["started_at"] or e["occurred_at"], a["finished_at"] or e["occurred_at"])
                    label = _bare(self.V.ACTIVITIES.get(args["activity"], {}).get("label", args["activity"]))
                    if others:
                        # one line per gathering: whoever an earlier line already named is not written again
                        if self._once(c, f"party:{args['party']}:{actor}"):
                            line = f"{self.names[actor]}和{_who(others, self.names)}一起{label}"
                            line_actors = sorted({actor, *others})
                            for o in others:
                                self._once(c, f"party:{args['party']}:{o}")
                        # a shared experience is known by who shared it, not by who was in the room at the end
                        knowers = [actor, *others]
                        feel = ("happy", 1, f"和{_who(others[:2])}一起{label}")
                    elif e["kind"] == "action_succeeded" and self._once(c, f"party:{args['party']}:{actor}"):
                        line = f"{self.names[actor]}{label}"
                elif e["kind"] == "arrived" and str(e["location"]).startswith(self.V.PLACE):
                    line = f"{self.names[actor]}到了{self.V.label(e['location'])}"
                elif e["kind"] == "invite_answered":
                    inv = db.execute("SELECT inviter FROM invitations WHERE inv_id=?", (p.get("inv_id"),)).fetchone()
                    plan = life.plans.plan(p.get("plan_id")) or {}
                    if plan.get("what") in self.V.RECIPES:
                        what = f"一起吃她做的{self.V.RECIPES[plan['what']]['label']}"
                    else:
                        what = "一起" + _bare(self.V.ACTIVITIES.get(plan.get("what"), {}).get("label") or "吃饭")
                    if inv:
                        ok = p.get("answer") == "yes"
                        line = f"{self.names[inv[0]]}约{self.names[actor]}{what}，{'她答应了' if ok else '她没答应'}"
                        if not ok and plan.get("kind") != "meal":
                            self._feel(c, inv[0], e, ("sad", 1, f"约{self.names[actor]}{what}没约成"))
                if line and self._once(c, f"{e['event_id']}:line"):
                    c.execute("insert into script(ts, actors, scene, text, source, created, knowers) values (?,?,?,?,?,?,?)",
                              (e["occurred_at"], json.dumps(line_actors), None, line, "kernel", life.clock.now(),
                               json.dumps(sorted(set(knowers) | set(line_actors)))))
                if feel:
                    self._feel(c, actor, e, feel)
        c.execute("insert or replace into kernel_bind values('cursor', ?)", (str(last),))

    def _expired(self, c, db, e, p):
        """an invitation nobody answered in time. A plain fact for the two who know (no refusal, no feeling)."""
        inv = db.execute("SELECT inviter, invitee FROM invitations WHERE inv_id=?", (p.get("inv_id"),)).fetchone()
        if not inv or inv[0] not in self.V.PEOPLE or inv[1] not in self.V.PEOPLE or not self._once(c, f"{e['event_id']}:line"):
            return
        plan = self.life.plans.plan(p.get("plan_id")) or {}
        if plan.get("what") in self.V.RECIPES:
            what = f"一起吃她做的{self.V.RECIPES[plan['what']]['label']}"
        else:
            what = "一起" + _bare(self.V.ACTIVITIES.get(plan.get("what"), {}).get("label") or "吃饭")
        line = f"{self.names[inv[0]]}约{self.names[inv[1]]}{what}，到点还没等到回复"
        c.execute("insert into script(ts, actors, scene, text, source, created, knowers) values (?,?,?,?,?,?,?)",
                  (e["occurred_at"], json.dumps([inv[0]]), None, line, "kernel", self.life.clock.now(),
                   json.dumps(sorted({inv[0], inv[1]}))))

    def _feel(self, c, actor, e, feel):
        emo, strength, why = feel
        if self._once(c, f"{e['event_id']}:feel:{actor}"):
            c.execute("insert into emotions(actor, ts, emotion, strength, why, source, entry) values (?,?,?,?,?,?,?)",
                      (actor, e["occurred_at"], emo, strength, why, "kernel", e["event_id"]))


def _hhmm(ts):
    return time.strftime("%H:%M", time.gmtime(ts + 9 * 3600))


def _bare(label):
    """「一起看电影」 -> 「看电影」, so 「和她们一起…」 is not said twice."""
    return label[2:] if label.startswith("一起") else label
