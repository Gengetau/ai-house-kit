"""The full-life runner (life1): one world, six people, a whole day every day.

Life wires the kernel (vocab_life1) to its modules: wardrobe, plans and invitations, the shared meal clock
(with a head cook and late breakfasts), her L0 day (dress, bathe, sleep with her own bedtime, hobbies by
preference, chores, snacks, rest), her own decisions through Cognition (answer an invitation, what the head
cook makes and whom she asks, how to spend free time, whether to stay up) and optional extensions (outings,
wallet, trips, shopping:). All of it goes through kernel commands; nothing is written around the kernel.

L0 never answers an invitation, never decides for anyone and never invents food, clothes or places.
Runner memory that must survive a restart (who decided what today) lives in the store, not in Python.
"""
import datetime as dt
import hashlib
import json
import random

from . import vocab_life1 as V
from . import inventory
from .kernel import Kernel
from .store import dumps
from .budget import Budget
from .cognition import Cognition
from .meals import Meals, WINDOWS
from .plans import Plans
from .wardrobe import Wardrobe

JST = dt.timezone(dt.timedelta(hours=9))
ROSTER = V.ROSTER
NAMES = V.NAMES
COOK_ORDER = V.COOK_ORDER  # derived from WorldPack manifest & people/*.json
# The house fridge target (genesis and the 17:30 restock). Ids are catalog life1 ingredients (renamed
# salmon -> fish, noodles -> noodle, the bought pudding -> pudding_cup; beef is in because nikujaga now uses it).
STOCK = {"egg": 12, "rice": 10, "tofu": 4, "miso": 4, "pork": 4, "chicken": 3, "fish": 3, "beef": 2, "potato": 8,
         "carrot": 4, "onion": 6, "cabbage": 2, "curry_roux": 3, "noodle": 6, "flour": 3, "butter": 3, "milk": 6,
         "black_tea": 20, "bread": 8, "fruit": 6, "pudding_cup": 4, "onigiri": 0}
PASS_TIME = "phone_browsing"  # the little thing she does while waiting for a meal
SCHEMA = ("CREATE TABLE IF NOT EXISTS life_runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL)",)
LATE_CAP_S = 3 * 3600
LATE_WAKE_S = 2 * 3600
THINK_GAP_S = 15 * 60
# from a dish being ready to the last of those waiting sitting down to it, before the meal closes
EAT_MARGIN_S = 5 * 60
# a model's answer is acted on a tick after she is asked, and on the real clock a tick is 60 s plus the model calls in
# it: a dish offered at a meal decision is in time if she starts it two minutes later (review P3-2)
DECIDE_LAG_S = 2 * 60
# a pastime refused because its thing (the console) was taken is not her habit's pick again for an hour
PASTIME_COOL_S = 3600
# a refused yes she can no longer answer at all (asking her again would only cost a call)
CANNOT_ANSWER = ("plan_closed", "invitation_not_pending", "invitation_expired", "not_her_invitation")


def hm(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def jst(ts):
    return dt.datetime.fromtimestamp(ts, JST)


def day_of(ts):
    """The life day: from 05:00 to 05:00 JST, so staying up past midnight is still 'tonight'."""
    return (jst(ts) - dt.timedelta(hours=5)).date().isoformat()


def synthetic_weather(ts):
    """Seasonal Tokyo-ish weather, deterministic per day (fast-forward and tests; live uses the real cache)."""
    d = jst(ts)
    h = int(hashlib.sha256(d.date().isoformat().encode()).hexdigest()[:8], 16)
    month_mean = {1: 6, 2: 7, 3: 10, 4: 15, 5: 19, 6: 22, 7: 26, 8: 27, 9: 24, 10: 18, 11: 13, 12: 8}[d.month]
    swing = 4 * (1 - abs(d.hour + d.minute / 60 - 14) / 12)
    return {"temp_c": round(month_mean - 3 + (h % 5) + swing, 1), "rain": h % 10 < 3}


class Life:
    def __init__(self, store, model=None, limits=None, weather=None, seed_reality=None, extensions=(), seed_items=None,
                 city_recovery=None, vocab=None):
        self.V = vocab or V
        self.roster = tuple(getattr(self.V, 'ROSTER', ROSTER))
        self.names = dict(getattr(self.V, 'NAMES', NAMES))
        self.cook_order = tuple(getattr(self.V, 'COOK_ORDER', self.roster))
        self.s = store
        self.k = Kernel(store, self.roster, vocab=self.V)
        self.city_visits = self.k.city_visits
        self.preparing_city_visits = self.city_visits.recover_preparing(city_recovery)
        with self.s.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)
        if not self.s.meta("seeded"):
            # what is left in the old house (inventory.legacy_items) comes along, once, with where it came from
            fixture = self._genesis(seed_reality or {})
            extra, report = inventory.migrate(seed_items or (), STOCK, self.V, self.roster)
            fixture["resources"] += extra
            with self.s.tx():
                self.k.seed(fixture, source="life1_genesis")
                if seed_items is not None:
                    self.put("genesis:inventory", report)
        self.weather = weather or synthetic_weather
        self.wardrobe = Wardrobe(self.k, self.V.PEOPLE)
        self.plans = Plans(self.k, self.roster)
        self.meals = Meals(self.k, self.roster, late_grace_s=2400)
        self.budget = Budget(store, pool="world-kernel/life1", limits=limits or {"total": 10 ** 9, "per_hour": 40,
                                                                                 "per_day": 300})
        self.model = model
        self.cog = Cognition(self.k, self.budget, self._ask, on_refused=self.meals.defer_cook,
                             before_step=self._before_step)
        self.k.on_complete.append(self._completed)
        self.extensions = [e(self) if callable(e) and not hasattr(e, "step") else e for e in extensions]
        self._kind = {}
        # pure caches of what the store already says (restart-safe without them): outfits she chose herself,
        # invitations already asked once, invitations with no more asks
        self._kept, self._inv_asked, self._inv_settled = {}, set(), set()

    @property
    def clock(self):
        """Follow Store.clock dynamically so runtime clock swaps stay in sync across all subsystems."""
        return self.s.clock

    @clock.setter
    def clock(self, value):
        self.s.clock = value

    # ------------------------------------------------------------------ seed
    def _genesis(self, reality):
        actors = {}
        for a in self.roster:
            r = reality.get(a) or {}
            actors[a] = {"location": self.V.bed(a), "profile_ref": f"life1:people/{a}",
                         "body": {"hunger": float(r.get("hunger") or 30), "energy": float(r.get("energy") or 70)}}
        res = [{"id": k, "kind": "food", "location": "kitchen", "qty": q} for k, q in STOCK.items()]
        res += [{"id": k, "kind": "anchor", "location": where, "qty": 1} for k, where in self.V.HOUSE["anchors"].items()]
        return {"actors": actors, "resources": res}

    # ------------------------------------------------------------------ runner memory (restart-safe)
    def get(self, key, default=None):
        with self.s.read() as c:
            r = c.execute("SELECT value FROM life_runtime WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key, value):
        with self.s.tx() as c:
            c.execute("INSERT OR REPLACE INTO life_runtime VALUES(?,?)", (key, dumps(value)))

    def once(self, key):
        """True the first time (persisted): one decision of a kind per day survives a restart."""
        with self.s.tx() as c:
            return c.execute("INSERT OR IGNORE INTO life_runtime VALUES(?,?)", (key, "1")).rowcount == 1

    # ------------------------------------------------------------------ helpers
    def person(self, a):
        return self.V.PEOPLE[a]

    def schedule(self, a, ts=None):
        d = jst(ts or self.clock.now())
        part = "weekend" if d.weekday() >= 5 else "weekday"
        return self.person(a)["schedule"][part]

    def minute(self, ts=None):
        d = jst(ts or self.clock.now())
        return d.hour * 60 + d.minute

    def rng(self, *parts):
        return random.Random(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest())

    def busy(self, a):
        return (not self.city_visits.home_allowed(a) or bool(self.k.actor(a)["action_id"])
                or a in self.cog.plans or any(j["actor"] == a for j in self.cog.pending))

    def submit(self, a, action_type, args, tag):
        now = self.clock.now()
        return self.k.submit({"idempotency_key": f"life:{a}:{tag}:{action_type}:{int(now // 60)}", "principal": "routine",
                              "actor_id": a, "origin": "routine", "action_type": action_type, "args": args})

    def go(self, a, to, tag):
        """One move toward `to` (the whole way: the kernel prices the route). True if already there."""
        if self.k.actor(a)["location"] == to:
            return True
        self.submit(a, "move", {"to": to}, tag)
        return False

    def unonce(self, key):
        """Take back a `once` claim whose decision was never asked (the model was not reached)."""
        with self.s.tx() as c:
            c.execute("DELETE FROM life_runtime WHERE key=?", (key,))

    def think(self, a, kind, gap=THINK_GAP_S, once_key=None):
        if not self.city_visits.home_allowed(a):
            return "degraded"
        if self.model is None:
            return "degraded"
        now = self.clock.now()
        last = self.get(f"think:{a}", 0)
        if gap and now - last < gap:
            return None
        self.put(f"think:{a}", now)
        self._kind[a] = kind
        got = self.cog.begin(a, kind)
        if got == "not_called":  # the model was never asked: this was not a decision, so no gap to wait out and
            self.put(f"think:{a}", last)  # no once-a-day claim spent; the next step asks again
            if once_key:
                self.unonce(once_key)
        return got

    def bedtime_window(self, a, now=None):
        """(cue, cap) as timestamps for tonight, from her schedule's bedtime."""
        now = now or self.clock.now()
        bed = hm(self.schedule(a, now - 5 * 3600)["bedtime"])
        base = dt.datetime.fromisoformat(day_of(now)).replace(tzinfo=JST) + dt.timedelta(minutes=bed)
        if bed < 5 * 60:
            base += dt.timedelta(days=1)
        cue = base.timestamp()
        return cue, cue + LATE_CAP_S

    def wake_at(self, a, slept_at):
        """Her wake time after sleeping at `slept_at`: habit wake + how late she went to bed (at most 2 h)."""
        cue, _ = self.bedtime_window(a, slept_at)
        late = max(0.0, min(slept_at - cue, LATE_WAKE_S))
        d = jst(slept_at + 6 * 3600)
        wake = hm(self.schedule(a, slept_at + 6 * 3600)["wake"])
        t = d.replace(hour=wake // 60, minute=wake % 60, second=0, microsecond=0).timestamp()
        if t <= slept_at + 3600:
            t += 86400
        return t + late

    # ------------------------------------------------------------------ the step
    def step(self):
        self.meals.sync()
        self.plans.sync()
        self.k.expire_presence()
        self.k.tick()
        self.cog.run_due()
        self.k.tick()
        self.cog.advance_plans()
        self.meals.sync()
        for ext in self.extensions:
            ext.step()
        if not any(getattr(ext, "shops", False) for ext in self.extensions):
            self.restock_fallback()
        for a in self.roster:
            if not self.city_visits.home_allowed(a):
                continue
            self._answer_invitations(a)  # a phone thing: she can answer while her hands are busy
            if not self.busy(a):
                self.l0(a)
        self.k.tick()
        self.meals.sync()

    def restock_fallback(self):
        """Until the shopping extension runs: the fridge is refilled at 17:30 as a world input (admin
        correction, recorded as such), exactly like the shadow did."""
        from . import admin
        now = self.clock.now()
        day = day_of(now)
        if self.minute(now) < 17 * 60 + 30 or not self.once(f"restocked:{day}"):
            return
        for rid, qty in STOCK.items():
            r = self.k.resource(rid)
            if r and r["qty"] < qty:
                admin.correction(self.k, "admin:world", rid, qty, "world input: house shopping (fallback)")

    def _answer_invitations(self, a):
        me = self.k.actor(a)
        if me["activity"] == "sleeping" or a in self.cog.plans or any(j["actor"] == a for j in self.cog.pending):
            return
        for inv in self.plans.pending_for(a):
            iid = inv["inv_id"]
            if iid in self._inv_settled or inv["plan_state"] in ("cancelled", "done"):
                continue
            if iid not in self._inv_asked:
                self._inv_asked.add(iid)
                if self.once(f"asked:{iid}"):
                    if self.think(a, f"invitation:{iid}", gap=0, once_key=f"asked:{iid}") == "not_called":
                        self._inv_asked.discard(iid)
                    return
            # a yes the world refused (already booked, rain, money) is hers to answer once more, knowing
            # why; left alone it only expired and the host never heard. Once more only.
            why = self.yes_refused(a, iid)
            if why:
                self._inv_settled.add(iid)
                if why not in CANNOT_ANSWER and self.once(f"asked_again:{iid}"):
                    if self.think(a, f"invitation:{iid}", gap=0, once_key=f"asked_again:{iid}") == "not_called":
                        self._inv_settled.discard(iid)
                    return

    def yes_refused(self, a, inv_id):
        """Why the world refused her yes to this invitation, or None."""
        with self.s.read() as c:
            r = c.execute("SELECT reason FROM actions WHERE actor_id=? AND action_type='respond_invite' AND state='failed'"
                          " AND json_extract(args,'$.inv_id')=? AND json_extract(args,'$.answer')='yes'"
                          " ORDER BY created_at DESC LIMIT 1", (a, inv_id)).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------------ her L0 day
    def l0(self, a):
        if not self.city_visits.home_allowed(a):
            return
        now = self.clock.now()
        me = self.k.actor(a)
        if me["activity"] == "sleeping":
            return
        for ext in self.extensions:
            if getattr(ext, "owns", None) and ext.owns(a):
                return  # out on an outing/trip: the outing engine runs her day
        day = day_of(now)
        cue, cap = self.bedtime_window(a, now)
        # 0) before her wake time on a day she has not been up yet: she is still asleep (start/restart)
        wake = self.wake_today(a, now)
        if now < wake and not self.get(f"up:{a}:{day}"):
            return self._to_bed(a, day, now, until=wake)
        if not self.get(f"up:{a}:{day}"):
            self.put(f"up:{a}:{day}", now)
        # 1) night: her own bedtime (cue -> she decides; cap -> she falls asleep)
        if now >= cue:
            if now >= cap:
                return self._to_bed(a, day, now)
            up = self._may_stay_up(a, day, now, cue)
            if up == "asked":
                return  # she is deciding: nothing else this tick, or her answer comes back stale
            if not up:
                return self._to_bed(a, day, now)
        # 2) waking up: dressed for the day
        if self._dress_for_day(a, day, now):
            return
        # 4) plans she is in that start now
        if self._join_plans(a, now):
            return
        # 5) meals: the shared clock, with one head cook
        if self._meal(a, day, now):
            return
        # 5b) out of clothes that no longer fit: home again in going-out clothes, sleepwear in the day
        if self._change_back(a, now, cue):
            return
        body = me["body"]
        # 6) tired: rest in her room
        if body.get("energy", 50) <= 20:
            if self.go(a, self.V.bed(a), "rest"):
                self.submit(a, "rest", {"seconds": 1800}, "rest")
            return
        # 7) hungry between meals: a snack, unless a meal is near
        if body.get("hunger", 0) >= 75 and not self._meal_soon(now, 3600):
            if self._snack(a):
                return
        # 8) evening bath
        if cue - 2.5 * 3600 <= now < cue and self._bath(a, day):
            return
        # 9) free time: once a day she may plan something with others (her decision)
        if 10 * 60 <= self.minute(now) < 20 * 60 and self.once(f"free:{a}:{day}"):
            if self.think(a, "free_time", gap=0, once_key=f"free:{a}:{day}") == "asked":
                return
        # 10) her own hobbies and chores, by preference
        self._pastime(a, day, now)

    def _may_stay_up(self, a, day, now, cue):
        """True while she has chosen to stay up, "asked" when she was just asked (her answer is pending)."""
        if self.once(f"bedtime_asked:{a}:{day}"):
            got = self.think(a, "bedtime", gap=0, once_key=f"bedtime_asked:{a}:{day}")
            if got == "asked":
                self.put(f"stayup:{a}:{day}", now)
                return "asked"
            return False  # no budget: she goes to bed at her usual time
        asked_at = self.get(f"stayup:{a}:{day}")
        # she chose to stay up: after half an hour of that she gets the cue again and goes
        return bool(asked_at) and now < asked_at + 1800

    def wake_today(self, a, now):
        """Her habitual wake time on this life day (a day starts 05:00 JST)."""
        base = dt.datetime.fromisoformat(day_of(now)).replace(tzinfo=JST)
        wake = hm(self.schedule(a, base.timestamp() + 12 * 3600)["wake"])
        t = base + dt.timedelta(minutes=wake)
        if wake < 5 * 60:
            t += dt.timedelta(days=1)
        return t.timestamp()

    def _to_bed(self, a, day, now, until=None):
        if self.k.actor(a)["location"] != self.V.bed(a) and not self.V.LOCATIONS[self.k.actor(a)["location"]].get("lodging"):
            return self.go(a, self.V.bed(a), "bed")
        w = self.weather(now)
        if self.wardrobe.dress(a, "sleep", w["temp_c"]):
            return
        seconds = max(600, min((until or self.wake_at(a, now)) - now, 14 * 3600 - 60))
        cue, _ = self.bedtime_window(a, now)
        if until is None:
            kind = "forced_sleep" if now >= cue + LATE_CAP_S else "own_bedtime"
            self.put(f"slept:{a}:{day}", {"at": now, "kind": kind, "late_min": round(max(0, now - cue) / 60)})
        self.submit(a, "sleep", {"seconds": round(seconds)}, "sleep")

    def _before_step(self, a, step):
        """Her habit around a step she decided herself (Cognition calls this before submitting it): into her
        sleepwear before she goes to sleep for the night in her own room or a lodging, the same as _to_bed
; a nap in the day is in her clothes, and a sleep the kernel would refuse changes nothing.
        change_clothes is instant and leaves her generation alone, so the decided step stands on what she saw.
        An activity she starts at home stops in time for her next meal, and an eating activity gets the
        snack she has in it (eating_snack ate nothing from the stock); the args it returns replace the
        step's."""
        if step.get("action_type") == "begin_activity":
            plan = self.cog.plans.get(a) or {}
            args = step.get("args")
            if str(plan.get("kind") or "").split(":", 1)[0].startswith(("scheduled_meal", "head_cook")):
                shaped = None  # decided at the meal itself, knowing it is on ("do something else instead"): hers
            else:
                shaped = self._stop_for_meal(a, args)
            return self._with_snack(a, shaped or args) or shaped
        atype = self.k.V.CATALOG["aliases"].get(step.get("action_type"), step.get("action_type"))
        if atype in ("invite", "move") and self.s._in_tx is None:
            for hook in self.k.before_submit:
                hook({"action_type": atype, "actor_id": a, "origin": "model", "args": step.get("args")})
            return
        if step.get("action_type") != "sleep":
            return
        me, now, args = self.k.actor(a), self.clock.now(), step.get("args")
        seconds = args.get("seconds") if isinstance(args, dict) else None
        if me["action_id"] or type(seconds) not in (int, float) or not 0 < seconds <= 14 * 3600:
            return
        cue, _ = self.bedtime_window(a, now)
        if now < cue - 3600 and seconds < 4 * 3600:
            return
        if me["location"] == self.V.bed(a) or self.V.LOCATIONS.get(me["location"], {}).get("lodging"):
            self.wardrobe.dress(a, "sleep", self.weather(now)["temp_c"])

    def _stop_for_meal(self, a, args):
        """an activity she decided at home runs at most till her next meal starts, like the ones her habit
        picks (_pastime), and at least the activity's shortest, which the kernel insists on (on 11-03 elsa
        began an hour of reading at 11:51, through lunch). One for a plan keeps the plan's time; out of the house
        what she does about the meal is hers. The changed args, or None to leave them."""
        if not isinstance(args, dict) or args.get("party"):
            return None
        spec, sec = self.V.ACTIVITIES.get(args.get("activity")), args.get("seconds")
        if not spec or type(sec) not in (int, float) or not spec["min"] <= sec <= spec["max"]:
            return None  # the kernel refuses it as it is
        now = self.clock.now()
        if self.V.LOCATIONS.get(self.k.actor(a)["location"], {}).get("kind") != "room":
            return None
        # the meal she is told of (next_meal: one not had yet, still on): a step decided just before it opens is
        # submitted a tick later, or after a move, when it is already on (review P3); then it runs its shortest.
        # A meal she has just eaten (her eat ended this tick, before the step's sync) is not on any more.
        self.meals.sync()
        row = self.next_meal_row(a, now)
        meal_at = row[1] if row else None
        if not meal_at or now + sec <= meal_at:
            return None
        cut = int(max(spec["min"], meal_at - now))
        if cut >= sec or now + cut >= row[2]:
            return None  # nothing to cut, or cut she still could not make it before it closes
        return dict(args, seconds=cut)

    def _outing_soon(self, a, now):
        """An outing or trip of hers that has not started yet and starts within 3 h. One that started without
        her running l0 is not hers any more (the outing engine runs a member's day once it starts)."""
        return any(p["kind"] in ("outing", "trip") and 0 < p["start_at"] - now < 3 * 3600
                   for p in self.plans.upcoming(a, 6 * 3600))

    def _dress_for_day(self, a, day, now):
        if not self.once(f"dressed:{a}:{day}"):
            return False
        # first up today somewhere else than her room (the outing engine had her from the moment she woke): what she
        # wears is left to _change_back, which takes her to her room first and leaves what she chose herself on
        if self.wardrobe.current(a) and self.k.actor(a)["location"] != self.V.bed(a):
            return False
        w = self.weather(now)
        return bool(self.wardrobe.dress(a, "out" if self._outing_soon(a, now) else "home", w["temp_c"], w["rain"]))

    def _change_back(self, a, now, cue):
        """home again in the going-out clothes her habit put on (the outing engine let go of her, no outing
        of hers starts within 3 h), she changes back in her own room; the same for sleepwear while she is
        up in the day. What she chose to wear herself stays on, and at night nothing changes (bed comes next)."""
        cur = self.wardrobe.current(a)
        if not cur or cur["occasion"] not in ("out", "sleep") or cur["event_id"] == self._kept.get(a):
            return False
        if now >= cue - (3600 if cur["occasion"] == "sleep" else 0):
            return False
        if self.V.LOCATIONS.get(self.k.actor(a)["location"], {}).get("kind") != "room":
            return False
        soon = self._outing_soon(a, now)
        if cur["occasion"] == "out" and soon:
            return False
        if not self._habit_outfit(a):
            self._kept[a] = cur["event_id"]
            return False
        w = self.weather(now)
        to = "out" if soon else "home"
        if not self.wardrobe.pick(a, to, w["temp_c"], w["rain"]):
            return False
        if not self.go(a, self.V.bed(a), "change"):
            return True
        return bool(self.wardrobe.dress(a, to, w["temp_c"], w["rain"]))

    def _habit_outfit(self, a):
        """Whether her habit (routine) put on what she wears, not a step she decided. The umbrella the outing engine
        adds on a rainy way out (routine, same occasion, one umbrella on top) keeps the origin of what it went on;
        anything she added herself, or a change of occasion, is a new outfit with its own origin."""
        with self.s.read() as c:
            rows = c.execute("SELECT o.items, o.occasion, a.origin FROM outfit_log o JOIN events e"
                             " ON e.event_id=o.event_id JOIN actions a ON a.action_id=e.action_id WHERE o.actor=?"
                             " ORDER BY o.rowid DESC LIMIT 4", (a,)).fetchall()
        if not rows:
            return True
        items, occasion, origin = set(json.loads(rows[0][0])), rows[0][1], rows[0][2]
        for prev_items, prev_occasion, prev_origin in rows[1:]:
            prev = set(json.loads(prev_items))
            added = items - prev
            if not (origin == "routine" and prev_occasion == occasion and prev < items and len(added) == 1
                    and "umbrella" in next(iter(added))):
                break
            items, occasion, origin = prev, prev_occasion, prev_origin
        return origin == "routine"

    def _join_plans(self, a, now):
        for p in self.plans.upcoming(a, 1800):
            if p["kind"] != "activity" or p["state"] not in ("confirmed", "running"):
                continue
            if not p["location"] or p["start_at"] - now > 600 or self.get(f"joined:{a}:{p['plan_id']}"):
                continue
            if p["state"] == "running" and now > p["start_at"] + 3600:
                continue  # too late to join in
            if not self.go(a, p["location"], f"plan:{p['plan_id']}"):
                return True
            if now >= p["start_at"] - 60:
                act = p["what"] if p["what"] in self.V.ACTIVITIES else None
                self.put(f"joined:{a}:{p['plan_id']}", now)
                if act:
                    spec = self.V.ACTIVITIES[act]
                    end = p["end_at"] or (p["start_at"] + spec["default"])
                    sec = max(spec["min"], min(spec["max"], end - now))
                    args = {"activity": act, "seconds": round(sec), "party": p["plan_id"]}
                    args = self._with_snack(a, args) or args  # review P2: a snack together, one each
                    r = self.submit(a, "begin_activity", args, f"plan:{p['plan_id']}")
                    if r["status"] in ("running", "accepted") and p["state"] == "confirmed":
                        self.plans.set_state(p["plan_id"], "running")
                return True
            return True
        return False

    # ------------------------------------------------------------------ meals
    def _meal_soon(self, now, within):
        m = self.minute(now)
        return any(0 <= (lo - m) <= within / 60 or lo <= m < hi for _, lo, hi in WINDOWS)

    def head_cook(self, slot, day):
        """Who cooks this meal: the first in COOK_ORDER awake and home when the window opens (stable for the meal)."""
        key = f"head:{day}:{slot}"
        got = self.get(key)
        if got and self.city_visits.home_allowed(got):
            return got
        for a in self.cook_order:
            if not self.city_visits.home_allowed(a):
                continue
            me = self.k.actor(a)
            if me["activity"] != "sleeping" and self.V.LOCATIONS[me["location"]]["kind"] == "room" and not any(
                    ext.owns(a) for ext in self.extensions if getattr(ext, "owns", None)):
                self.put(key, a)
                return a
        return None

    def eaters(self, slot, day):
        """People still expecting this meal at home: pending and not asleep/away, and not eating a serving of it
        already (her eat is still running, so the meal is still pending; three of them were counted as
        waiting and the head cook made a pot for five for the last two)."""
        with self.s.read() as c:
            rows = c.execute("SELECT m.actor, x.action_type, json_extract(x.args,'$.item') FROM meal_attendance m"
                             " JOIN actors p ON p.actor_id=m.actor LEFT JOIN actions x ON x.action_id=p.action_id"
                             " WHERE m.day=? AND m.slot=? AND m.status='pending'", (day, slot)).fetchall()
        out = []
        for r in rows:
            if not self.city_visits.home_allowed(r[0]):
                continue
            if r[1] == "eat" and r[2] in self.V.MAIN_FOODS:
                continue
            me = self.k.actor(r[0])
            if me["activity"] != "sleeping" and self.V.LOCATIONS[me["location"]]["kind"] == "room":
                out.append(r[0])
        return out

    def ready_servings(self):
        with self.s.read() as c:
            rows = c.execute("SELECT resource_id,item FROM resources WHERE kind='food' AND location='kitchen'"
                             " AND holder IS NULL AND qty>0").fetchall()
            return sum(self.k._free_qty(c, r[0]) or 0 for r in rows if r[1] in self.V.MAIN_FOODS)

    def cooking_now(self):
        with self.s.read() as c:
            return c.execute("SELECT COUNT(*) FROM actions WHERE action_type='cook' AND state IN ('running','accepted')"
                             ).fetchone()[0]

    def can_cook(self, recipe):
        rec = self.V.RECIPES[recipe]
        with self.s.read() as c:
            for item, q in rec["consume"].items():
                rows = c.execute("SELECT resource_id FROM resources WHERE item=? AND location='kitchen' AND holder IS NULL",
                                 (item,)).fetchall()
                if not any((self.k._free_qty(c, r[0]) or 0) >= q for r in rows):
                    return False
            for anchor in rec["hold"]:
                if (self.k._free_qty(c, anchor) or 0) < 1:
                    return False
        return True

    def cook_choices(self, a, slot, for_others=True, lag=0):
        """The dishes she could start now for this meal: a main of the slot, not one she cannot make, ingredients
        and tools free, and ready in time while the meal is still on for whoever is waiting. Not for_others
        (her own meal decision): only if none is, the dishes for her own serving within the late grace the meal clock
        gives a dish she starts inside the window (review P2-4: at 13:00 a curry for four, ready 14:00, was on her
        list next to the quick ones; made for everyone, it fed her alone and kept the stove from the others' top-up).
        lag: a model's cook starts a tick or so after she is asked (review P3-2, P3-5)."""
        now, row = self.clock.now() + lag, self.meals.current(a)
        ends = row["ends"] if row and row["slot"] == slot else None
        cannot = set(self.person(a)["cooking"].get("cannot", ()))

        def ready_by(until):
            return [r for r, rec in self.V.RECIPES.items() if rec["main"] and slot in rec["meal"] and r not in cannot
                    and (until is None or now + rec["seconds"] + EAT_MARGIN_S <= until) and self.can_cook(r)]
        got = ready_by(ends)
        if got or for_others or ends is None or now >= ends:
            return got
        return ready_by(ends + self.meals.late_grace_s)

    def cooking(self):
        """What is on the stove now: (who, recipe, ready at), the soonest first."""
        with self.s.read() as c:
            rows = c.execute("SELECT actor_id, json_extract(args,'$.recipe'), due_at FROM actions"
                             " WHERE action_type='cook' AND state='running' AND due_at IS NOT NULL ORDER BY due_at").fetchall()
        return [tuple(r) for r in rows]

    def meal_coming(self, row):
        """when the main dishes on the stove are ready, if they are in time for this meal and, with what is
        ready, make a serving for everyone still expecting it (a pot for one is not hers to wait for); else None."""
        due = [(d, r) for _, r, d in self.cooking() if self.V.RECIPES.get(r, {}).get("main") and d + EAT_MARGIN_S <= row["ends"]]
        if not due:
            return None
        servings = self.ready_servings() + sum(self.V.RECIPES[r]["makes"].get(r, 0) for _, r in due)
        return min(d for d, _ in due) if servings >= len(self.eaters(row["slot"], row["day"])) else None

    def l0_recipe(self, a, slot, need, quick=False):
        """Her habitual choice: a dish she is good at for this meal, enough servings, ingredients in stock, ready in
        time (the 12:37 top-up of 11-03 was a 60-minute curry, ready after lunch had closed). A top-up for
        those still waiting (quick) is the quickest dish that feeds them, her favourites only breaking ties."""
        p = self.person(a)["cooking"]
        options = self.cook_choices(a, slot)
        if not options:
            return None
        rng = self.rng(a, slot, day_of(self.clock.now()))
        if quick:
            return max(options, key=lambda r: (min(self.V.RECIPES[r]["makes"][r], need), -self.V.RECIPES[r]["seconds"],
                                               r in p["good_at"], self.V.RECIPES[r]["difficulty"] <= p["skill"], rng.random()))
        return max(options, key=lambda r: (r in p["good_at"], min(self.V.RECIPES[r]["makes"][r], need),
                                           self.V.RECIPES[r]["difficulty"] <= p["skill"], rng.random()))

    def _meal(self, a, day, now):
        row = self.meals.current(a)
        if not row:
            got = self.meals.act(a)  # a late breakfast of her own
            return bool(got.get("action"))
        slot = row["slot"]
        head = self.head_cook(slot, day)
        waiting = [e for e in self.eaters(slot, day) if e != a or row["status"] == "pending"]
        need = len(waiting)
        ready = self.ready_servings()
        # her turn as head cook only with a pot on or something she can still make in time: else (she has eaten and
        # nothing is in time any more) it sent her to the kitchen and the rest of her day back out, every minute
        # (sim7-b1r7, 11-07 dinner: 36 times from 19:02 to 20:15)
        if a == head and need > ready and now < row["ends"] - 900 and \
                (self.cooking_now() or self.cook_choices(a, slot)):
            # the head cook keeps cooking until everyone still expecting this meal has a serving, and tops up as soon as
            # she sees it will not go round, before sitting down to what is ready (sim7-b1r5 11-03/11-09:
            # curry for four ready 13:02 for six; she ate first, and at 13:12 nothing was quick enough for 13:30 any more)
            if not self.go(a, "kitchen", f"cook:{slot}"):
                return True
            if self.cooking_now():
                return True
            self.put(f"head_started:{day}:{slot}", now)
            # asked only with something to cook (review P3-3: from 13:05 nothing for lunch is in time for the others,
            # and she was asked what to cook for everyone with an empty list)
            if self.cook_choices(a, slot, lag=DECIDE_LAG_S) and self.once(f"headcook_asked:{day}:{slot}"):
                if self.think(a, f"head_cook:{slot}", gap=0, once_key=f"headcook_asked:{day}:{slot}") == "asked":
                    return True
            # someone has had this meal already or a dish is ready: the rest are waiting, so something quick (
            # 11-03 in the fake week: a 60-minute curry for the last two, who left for their 20:00 plan before it was ready)
            with self.s.read() as c:
                served = c.execute("SELECT COUNT(*) FROM meal_attendance WHERE day=? AND slot=? AND status='eaten'",
                                   (row["day"], slot)).fetchone()[0]
            recipe = self.l0_recipe(a, slot, max(1, need - ready), quick=served > 0 or ready > 0)
            if recipe:
                self.submit(a, "cook", {"recipe": recipe}, f"cook:{slot}")
                return True
        if row["status"] != "pending":
            return False
        if ready > 0:
            got = self.meals.act(a)  # walk to the kitchen and eat what is ready
            return bool(got.get("action"))
        # not the head cook: wait for the meal (a short while at a time) unless it is clearly not coming; a main dish
        # on the stove that is ready while the meal is on is coming (from 13:00 the four still waiting were
        # asked to decide while the pot was on, and missed it)
        hm_ = self.k.actor(head) if head else None
        head_on_duty = hm_ and hm_["activity"] != "sleeping" and self.V.LOCATIONS[hm_["location"]]["kind"] == "room"
        coming = self.meal_coming(row) if a != head else None
        if a != head and (coming or head_on_duty and now < row["ends"] - 1800):
            # a pot that is coming is waited for in the kitchen, so it is served as soon as it is ready (review F7: on
            # the real clock a tick is 60 s plus the model calls in it, and the walk over, a hop a tick, took the
            # margin; for a pot ready five minutes before close two of them sat down after lunch had closed). Only from
            # inside the house: anywhere else she waits where she is, as before.
            if coming and self.V.LOCATIONS[self.k.actor(a)["location"]]["kind"] == "room" and \
                    not self.go(a, "kitchen", f"wait_meal:{slot}"):
                return True
            sec = int(max(60, min(600, coming - now + 60))) if coming else 600
            here = self.k.actor(a)["location"]
            if PASS_TIME in self.V.ACTIVITIES and self.V.activity_allowed_at(PASS_TIME, a, here) and \
                    sec >= self.V.ACTIVITIES[PASS_TIME]["min"]:
                self.submit(a, "begin_activity", {"activity": PASS_TIME, "seconds": sec}, f"wait_meal:{slot}")
            else:
                self.submit(a, "wait", {"seconds": sec}, f"wait_meal:{slot}")
            return True
        got = self.meals.act(a)
        if not got.get("would_call"):
            return bool(got.get("action"))
        # the next main dish on the stove, if it is ready before the meal closes (sim7-b1r5 11-09: wanda waited
        # ten minutes at a time and was still waiting when mina's yakisoba came off at 13:28, two minutes before close)
        soon = [d for _, r, d in self.cooking() if self.V.RECIPES.get(r, {}).get("main") and d + 60 < row["ends"]]
        # she decides when she has a choice: with that pot on and nothing she could start herself (one stove, taken)
        # she waits for it first, and her one decision of the meal is not spent on an empty list (review P2-1: from
        # 09:00 the others were asked while a toast for one was on, and none of them made breakfast after it)
        if not (soon and not self.cook_choices(a, slot, for_others=False, lag=DECIDE_LAG_S)) and \
                self.meals.claim_decision(a):
            if self.think(a, got["would_call"], gap=0) == "not_called":
                self.meals.release_decision(a)  # not asked: the one decision of this meal is still hers
            return True
        if now < row["ends"]:  # hungry, waiting in the kitchen: till that pot is ready
            sec = int(max(60, min(600, min(soon) - now + 60))) if soon else 600
            self.submit(a, "wait", {"seconds": sec}, f"wait_meal2:{slot}")
            return True
        return False

    def _snack(self, a):
        """Hungry between meals: something to eat (not a drink) from the kitchen or her own, picked as snack_for does
        (by resource id it was always the house bread, never the owner's 75 kinds)."""
        r = self.snack_for(a, classes=("snack", "dish"))
        if not r:
            return False
        if r[2] == a or self.go(a, "kitchen", "snack"):
            self.submit(a, "eat", {"item": r[1], "resource_id": r[0]}, "snack")
        return True

    def snack_for(self, a, classes=None, here=None, activity="eating_snack"):
        """(resource_id, item, holder) of a snack or drink she would have now, or None. Of what is left and not held
        for someone's action: hers, and the house's in the kitchen (with `here`, only what she can reach from there for
        eating_snack). Never one she dislikes or never eats; one she likes four times as likely, otherwise as likely as
        there are pieces of it up to five, so the owner's gifts and the house bread are both eaten and no one big row
        (the house's 20 black tea) is most of it (review P3-5). Deterministic per person and minute."""
        classes = classes or self.V.ACTIVITIES[activity]["eats"]
        taste = self.person(a).get("taste", {})
        no, likes = set(taste.get("dislikes", ())) | set(taste.get("never", ())), set(taste.get("likes", ()))
        with self.s.read() as c:
            rows = [dict(r) for r in c.execute("SELECT resource_id, kind, item, location, holder, qty FROM resources"
                                               " WHERE kind='food' AND qty>0 AND (holder=? OR (holder IS NULL AND"
                                               " location='kitchen')) ORDER BY resource_id", (a,))]
            options = []
            for r in rows:
                spec = self.V.CATALOG["items"].get(r["item"], {})
                if r["item"] not in self.V.SNACKS or spec.get("class") not in classes or r["item"] in no:
                    continue
                if here is not None and not self.V.snack_ok(r, activity, a, here):
                    continue
                free = self.k._free_qty(c, r["resource_id"]) or 0
                if free > 0:
                    options.append(((r["resource_id"], r["item"], r["holder"]),
                                    min(free, 5) * (4 if r["item"] in likes else 1)))
        if not options:
            return None
        rng = self.rng(a, "snack", int(self.clock.now() // 60))
        return rng.choices([o for o, _ in options], weights=[w for _, w in options])[0]

    def _with_snack(self, a, args):
        """an eating activity she starts gets the snack she has in it (args "snack"), from where she is; the
        args with it, or None (not such an activity, already named, or nothing left: the kernel then refuses it)."""
        if not isinstance(args, dict) or "snack" in args:
            return None
        spec = self.V.ACTIVITIES.get(args.get("activity"))
        if not spec or not spec.get("eats"):
            return None
        got = self.snack_for(a, here=self.k.actor(a)["location"], activity=args["activity"])
        return dict(args, snack=got[0]) if got else None

    def _bath(self, a, day):
        if self.get(f"bathed:{a}:{day}"):
            return False
        with self.s.read() as c:
            if (self.k._free_qty(c, "bathtub") or 0) < 1:
                return False
        if self.go(a, "bath", "bath"):
            r = self.submit(a, "begin_activity", {"activity": "bathing", "seconds": 1800}, "bath")
            if r["status"] in ("running", "accepted"):
                self.put(f"bathed:{a}:{day}", self.clock.now())
        return True

    def _pastime(self, a, day, now):
        options = self.pastime_options(a, now)
        meal_at = self.next_meal_start(a, now)
        room = (meal_at - now) if meal_at else None  # time left before a meal she attends
        if not options:
            if room is not None and room > 60:
                hold = self.hold_until(a, now)  # not past the 一起刷站 she goes to
                self.submit(a, "wait", {"seconds": int(min(room, 1800, max(60, hold - now) if hold else 1800))},
                            "before_meal")
            return
        n = self.get(f"pastimes:{a}:{day}", 0)
        rng = self.rng(a, day, n)
        pick = rng.choices(options, weights=[w * w for _, w, _ in options])[0]
        k, _, homes = pick
        spec = self.V.ACTIVITIES[k]
        hold = self.hold_until(a, now)  # the 一起刷站 she goes to; too close for this: she waits for it
        if hold and hold < now + spec["min"]:
            if hold - now > 60:
                self.submit(a, "wait", {"seconds": int(hold - now)}, "before_browse")
            return
        here = self.k.actor(a)["location"]
        where = here if (here in homes or (here == self.V.bed(a) and "own_room" in homes)) else (
            self.V.bed(a) if homes[0] == "own_room" else homes[0])
        if not self.go(a, where, "pastime"):
            return
        sec = rng.randint(spec["min"], spec["max"])
        cue, _ = self.bedtime_window(a, now)
        limit = cue - now if cue > now + spec["min"] else spec["min"]
        if meal_at and meal_at >= now + spec["min"]:
            limit = min(limit, meal_at - now)  # she stops in time for the meal she attends
        if hold:
            limit = min(limit, hold - now)  # and stops in time for it
        sec = max(spec["min"], min(sec, limit))
        args = {"activity": k, "seconds": int(sec)}
        args = self._with_snack(a, args) or args  # eating_snack has one of the snacks
        r = self.submit(a, "begin_activity", args, f"pastime:{n}")
        if r["status"] in ("running", "accepted"):
            self.put(f"pastimes:{a}:{day}", n + 1)
        elif r.get("reason", "").startswith("resource_unavailable") or "anchor" in r.get("reason", ""):
            self.put(f"pastimes:{a}:{day}", n + 1)  # the console/piano is taken: something else next time
            self.put(f"pastime_cool:{a}:{k}", now + PASTIME_COOL_S)  # and not that again for a while

    def pastime_options(self, a, now):
        """[(activity, weight, homes)] her habit may pick now. Not one whose thing (the console, the piano) someone
        is using, nor one refused for that within the hour (on 11-03 wanda walked to the living room
        for the console four times while mina had it, and twice in a row at 17:14/17:15)."""
        prefs = self.person(a)["activities"]
        avoid = set(prefs.get("avoid", ()))
        m = self.minute(now)
        meal_at = self.next_meal_start(a, now)
        room = (meal_at - now) if meal_at else None  # time left before a meal she attends
        with self.s.read() as c:
            cooling = {key.rsplit(":", 1)[1] for key, until in c.execute(
                "SELECT key, value FROM life_runtime WHERE key LIKE ?", (f"pastime_cool:{a}:%",)) if json.loads(until) > now}
        options = []
        for k, spec in self.V.ACTIVITIES.items():
            if k in avoid or spec["party"] and spec["party"].get("min", 1) > 1 or k == "bathing":
                continue
            if room is not None and spec["min"] > room:
                continue  # would run into the meal
            if k in cooling or any(self.anchor_taken(h) for h in spec["hold"]):
                continue
            if spec.get("eats") and not self.snack_for(a, activity=k):
                continue  # nothing left to have (she may buy some: shopping)
            homes = [w for w in spec["where"] if w == "own_room" or w in self.V.HOUSE["rooms"]]
            if not homes:
                continue
            if k in ("music", "gardening") and not 8 * 60 <= m < 21 * 60:
                continue
            if k.startswith("chores") and not 8 * 60 <= m < 21 * 60:
                continue
            hours = spec.get("hours")
            if hours and not hm(hours[0]) <= m < hm(hours[1]):
                continue
            w = prefs.get("prefer", {}).get(k, 1)
            if k == "laundry":
                worn = sum(1 for g in self.wardrobe.owned(a) if g["state"] in ("worn", "dirty"))
                w = 6 if worn >= 6 else 0
            if w > 0:
                options.append((k, w, homes))
        return options

    def hold_until(self, a, now):
        """The earliest time an extension expects her somewhere (the living room's 一起刷站), or None."""
        got = [t for t in (e.hold_until(a, now) for e in self.extensions if getattr(e, "hold_until", None)) if t]
        return min(got) if got else None

    def anchor_taken(self, rid):
        """Whether a house thing (the console, the TV, the bathtub) is in someone's hands now."""
        with self.s.read() as db:
            free = self.k._free_qty(db, rid)
        return free is not None and free < 1

    # ------------------------------------------------------------------ completion hooks
    def next_meal_start(self, a, now):
        with self.s.read() as c:
            r = c.execute("SELECT starts FROM meal_attendance WHERE actor=? AND status='pending' AND starts>?"
                          " ORDER BY starts LIMIT 1", (a, now)).fetchone()
        return r[0] if r else None

    def _completed(self, k, db, action, actor, args, eid, location):
        if action["action_type"] == "begin_activity" and args.get("activity") == "laundry":
            self.wardrobe.laundry_done(db, actor["actor_id"])
        if action["action_type"] == "begin_activity" and args.get("party"):
            p = db.execute("SELECT kind, state, end_at FROM plans WHERE plan_id=?", (args["party"],)).fetchone()
            if p and p["kind"] == "activity" and p["state"] == "running":  # outings close in Outings._finish
                still = db.execute("SELECT COUNT(*) FROM actions WHERE state='running' AND action_type='begin_activity'"
                                   " AND json_extract(args,'$.party')=? AND action_id!=?",
                                   (args["party"], action["action_id"])).fetchone()[0]
                if not still:
                    self.plans.finish(args["party"], "done")

    # ------------------------------------------------------------------ her decisions
    def _ask(self, ctx):
        a = ctx["actor"]
        kind = self._kind.get(a, "")
        context = self.life_context(a, kind)
        if ctx.get("purpose") == "express":
            context.pop("promises", None)
            context.pop("qq_chat", None)
        ctx = dict(ctx, decision=kind, life=context)
        return self.model(ctx)

    def life_context(self, a, kind):
        now = self.clock.now()
        d = jst(now)
        cue, cap = self.bedtime_window(a, now)
        out = {"now": d.strftime("%Y-%m-%d %H:%M"), "weekday": "一二三四五六日"[d.weekday()],
               "weather": self.weather(now), "bedtime": jst(cue).strftime("%H:%M"),
               "outfit": (self.wardrobe.current(a) or {}).get("labels"),
               "plans": [{k: p[k] for k in ("plan_id", "kind", "what", "location", "host", "members")} |
                         {"start": jst(p["start_at"]).strftime("%m-%d %H:%M")} for p in self.plans.upcoming(a)],
               "invitations": [self._invitation_seen(a, i) for i in self.plans.pending_for(a)],
               "roommates": [r for r in self.roster if r != a],
               # their names (on 11-03 wanda's line called yuki 星莉 and mina's called nell "nell")
               "names": {r: self.names.get(r, r) for r in self.roster if r != a} | {"owner": "房主"}}
        choices = self.wardrobe.wardrobe_choices(a)
        if choices:
            out["wardrobe_choices"] = choices
        picks = self.wardrobe.shelf_picks(a)
        if picks:
            out["shelf_picks"] = picks
        nxt = self.next_meal_row(a, now)
        if nxt:  # her next meal, which an activity she starts at home stops for
            out["next_meal"] = {"slot": nxt[0], "starts": jst(nxt[1]).strftime("%H:%M"),
                                "ends": jst(nxt[2]).strftime("%H:%M")}
        if kind.startswith("head_cook"):
            slot = kind.split(":", 1)[1]
            out.update(self.meal_context(a, slot, head=True))
            out["eaters"] = self.eaters(slot, day_of(now))
        elif kind.startswith("scheduled_meal"):
            row = self.meals.current(a)
            if row:
                out.update(self.meal_context(a, row["slot"]))
        if kind == "free_time":
            out["activities"] = {k: s["label"] for k, s in self.V.ACTIVITIES.items()}
            out["places"] = {k: p["label"] for k, p in self.V.PLACES.items()}
            out["roommates_now"] = self.roommates_now(a)
        for ext in self.extensions:
            if hasattr(ext, "context"):
                out.update(ext.context(a, kind) or {})
        return out

    def next_meal_row(self, a, now):
        """(slot, starts, ends) of her first meal still on today that she has not had or missed, or None."""
        with self.s.read() as c:
            return c.execute("SELECT slot, starts, ends FROM meal_attendance WHERE actor=? AND status='pending'"
                             " AND ends>? ORDER BY starts LIMIT 1", (a, now)).fetchone()

    def roommates_now(self, a):
        """where each roommate is, what she is doing till when, and what she has on later (in a shared
        house she knows that much). On 11-03 the real model never invited anyone: free_time showed her nobody's day,
        and an invitation needs a time and a place. Only what a housemate would know (review P3): what someone does
        behind a bedroom door or out of the house is not shown unless she is there too, and of the others' plans
        only the time they are taken, once agreed (an invitation not yet answered, and what or with whom, is theirs)."""
        now, out = self.clock.now(), {}
        bedrooms = {self.V.bed(r) for r in self.roster}
        with self.s.read() as db:
            mine = db.execute("SELECT location FROM actors WHERE actor_id=?", (a,)).fetchone()
            here = mine[0] if mine else None
            for r in self.roster:
                row = db.execute("SELECT location, activity, action_id FROM actors WHERE actor_id=?", (r,)).fetchone()
                if r == a or not row:
                    continue
                loc, act = row[0], row[1]
                seen = {"name": self.names.get(r, r)}
                outside = self.V.LOCATIONS.get(loc, {}).get("kind") != "room"
                if outside:
                    seen["where"] = "外出"
                else:
                    seen["where"] = "她的卧室" if loc == self.V.bed(r) else loc
                if act and act != "idle" and (loc == here or not (outside or loc in bedrooms)):
                    seen["doing"] = "睡觉" if act == "sleeping" else self.V.ACTIVITIES.get(act, {}).get("label", act)
                    due = db.execute("SELECT due_at FROM actions WHERE action_id=?", (row[2],)).fetchone() \
                        if row[2] else None
                    if due and due[0] and due[0] > now:
                        seen["until"] = jst(due[0]).strftime("%H:%M")
                out[r] = seen
        for r, seen in out.items():
            later = [self._taken(p["start_at"], p.get("end_at")) for p in self.plans.upcoming(r, 86400)
                     if p["start_at"] > now and p["state"] in ("confirmed", "running")]
            if later:
                seen["plans"] = later
        return out

    @staticmethod
    def _taken(start, end):
        """'11-06 09:00–17:00 有安排'; with the date at the end too when it is another day (a trip overnight)."""
        s = jst(start)
        if not end:
            return f"{s:%m-%d %H:%M} 起有安排"
        e = jst(end)
        return f"{s:%m-%d %H:%M}–{e:%H:%M} 有安排" if e.date() == s.date() else f"{s:%m-%d %H:%M}–{e:%m-%d %H:%M} 有安排"

    def meal_context(self, a, slot, head=False):
        """what a meal decision needs (on 11-03 the head cook saw dish ids only, the others nothing): the
        dishes she could still make in time with servings and minutes, when the meal closes, how many are waiting
        and what is on the stove already."""
        now, row = self.clock.now(), self.meals.current(a)
        ends = row["ends"] if row and row["slot"] == slot else None
        opts = {}
        for r in self.cook_choices(a, slot, for_others=head, lag=DECIDE_LAG_S):
            rec, done = self.V.RECIPES[r], now + DECIDE_LAG_S + self.V.RECIPES[r]["seconds"]
            # when it is ready, and (review P2-4) that a dish ready after the meal closes is hers alone
            late = "（ends 之后才好，只算你自己这顿）" if ends is not None and done + EAT_MARGIN_S > ends else ""
            opts[r] = f"{rec['label']}，{rec['makes'][r]}人份，做{rec['seconds'] // 60}分钟，约{jst(done).strftime('%H:%M')}好{late}"
        # main dishes only, and how many each feeds (review P3-4: a soup on the stove was there to wait for)
        meal = {"slot": slot, "eaters": len(self.eaters(slot, day_of(now))), "ready_servings": self.ready_servings(),
                "cooking": [{"who": w, "recipe": r, "servings": self.V.RECIPES[r]["makes"].get(r, 0),
                             "ready": jst(due).strftime("%H:%M")}
                            for w, r, due in self.cooking() if self.V.RECIPES.get(r, {}).get("main")]}
        if ends is not None:
            meal["ends"] = jst(ends).strftime("%H:%M")
        meal["note"] = ("要吃这顿的人数 eaters 里也算你。cook_options 只列现在开做、ends 前来得及做好的菜；一锅不够可以再做一锅。"
                        if head else "饭还没好。cooking 是灶上正在做的主食、几人份、几点好。可以等它；自己做一份 cook_options 里的菜；"
                        "饿了先在厨房用 eat 吃一点 items_here 里的零食（不算这顿饭）；或者去做别的事，这顿就不吃了。"
                        "你在 ends 前开做的那份，晚一点吃也算这顿。")
        return {"cook_options": opts, "meal": meal}

    def _invitation_seen(self, a, i):
        out = {"inv_id": i["inv_id"], "from": i["inviter"], "kind": i["kind"], "what": i["what"],
               "location": i["location"], "start": jst(i["start_at"]).strftime("%m-%d %H:%M")}
        why = self.yes_refused(a, i["inv_id"])
        if why:  # her yes did not go through, and why (e.g. outing_schedule_conflict)
            out["yes_refused"] = why
        return out


def run_days(life, days, step_s=60, on_day=None):
    """Fast-forward: move a VirtualClock through `days` days in steps (tests and the simulated week)."""
    end = life.clock.now() + days * 86400
    last_day = day_of(life.clock.now())
    while life.clock.now() < end:
        life.step()
        life.clock.advance(step_s)
        d = day_of(life.clock.now())
        if d != last_day and on_day:
            on_day(last_day)
        last_day = d
