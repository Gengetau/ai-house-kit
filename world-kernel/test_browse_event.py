"""客厅合租号刷站 as a random kernel event: settings, the draw's guardrails, a whole session through Life, her
pastime stopping in time, and a 400-day draw simulation (distribution, caps, gaps, no night, restart-safe).
Run: python3 -m unittest -v test_browse_event.py   (temp dbs, fake model, fake world: no network)"""
import hashlib
import os
import random
import tempfile
import types
import unittest
from datetime import datetime

from worldkernel import browse_event as B
from worldkernel.life import Life
from worldkernel.life_model import FakeLifeModel
from worldkernel.store import Store, VirtualClock
from worldkernel.wallet import JST


def at(clock="14:00", day="2026-11-11"):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=JST).timestamp()


def cfg(**over):
    c = B.settings({"WORLD_KERNEL_BROWSE_EVENT": "1"})
    c.update(over)
    return c


class FakeWorld:
    def __init__(self, ready=(True, "")):
        self.ready, self.opened, self.notes, self.asked = ready, [], [], 0

    def kernel_browse_ready(self, now, sites):
        self.asked += 1
        return self.ready

    def kernel_browse_open(self, members, minutes, sites, now, least=1, key=None):
        self.opened.append((list(members), minutes, list(sites), now))
        self.keys = getattr(self, "keys", []) + [(key, least)]
        return {"ok": True, "ids": [101, 102], "members": list(members), "sites": list(sites)}

    def kernel_browse_note(self, ev):
        self.notes.append(dict(ev))


class SettingsTest(unittest.TestCase):
    def test_defaults_are_off_and_low(self):
        c = B.settings({})
        self.assertFalse(c["event"])
        self.assertEqual((c["p"], c["per_day"], c["gap_h"], c["sites"]), (0.06, 1, 6.0, ("bili",)))
        self.assertEqual((c["from"], c["to"]), (600, 1380))
        self.assertIsNone(B.install(FakeWorld(), {}))
        self.assertIsNone(B.install(None, {"WORLD_KERNEL_BROWSE_EVENT": "1"}))
        self.assertIsNotNone(B.install(FakeWorld(), {"WORLD_KERNEL_BROWSE_EVENT": "1"}))

    def test_bad_values_fall_back(self):
        c = B.settings({"WORLD_KERNEL_BROWSE_P": "2", "WORLD_KERNEL_BROWSE_PER_DAY": "x", "WORLD_KERNEL_BROWSE_TO": "25:99",
                        "WORLD_KERNEL_BROWSE_SITES": "weibo", "WORLD_KERNEL_BROWSE_MIN_MINUTES": "60",
                        "WORLD_KERNEL_BROWSE_MAX_MINUTES": "30"})
        self.assertEqual((c["p"], c["per_day"], c["to"], c["sites"]), (0.06, 1, 1380, ("bili",)))
        self.assertEqual((c["min_minutes"], c["max_minutes"]), (60, 60))


class LifeBase(unittest.TestCase):
    def make(self, world=None, now=None, **over):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = VirtualClock(now or at())
        self.s = Store(os.path.join(self.tmp.name, "life1.db"), self.clock, world_id="test/browse")
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.world = world or FakeWorld()
        self.cfg = cfg(**over)
        self.l = Life(self.s, model=FakeLifeModel(), weather=lambda _: dict(rain=False, temp_c=14),
                      extensions=[lambda life: B.BrowseEvent(life, self.world, self.cfg)])
        self.ev = self.l.extensions[0]
        for a in self.l.k.roster:  # up, dressed and fed: the afternoon at home
            for k in ("up", "dressed"):
                self.l.put(f"{k}:{a}:2026-11-11", 1)
            self.l.once(f"free:{a}:2026-11-11")
        self.place("living", *self.l.k.roster)

    def place(self, room, *who, activity="idle"):
        with self.s.tx() as db:
            for a in who:
                db.execute("UPDATE actors SET location=?, activity=?, action_id=NULL WHERE actor_id=?", (room, activity, a))

    def steps(self, n):
        for _ in range(n):
            self.l.step()
            self.clock.advance(60)


class GuardTest(LifeBase):
    def test_no_draw_at_night_or_running_past_the_window(self):
        self.make()
        for clock, why in (("02:00", "outside_hours"), ("08:30", "outside_hours"), ("22:30", "outside_hours"),
                           ("23:40", "outside_hours"), ("14:00", "")):
            self.assertEqual(self.ev.guard(at(clock), at(clock) + 65 * 60), why, clock)

    def test_day_cap_and_gap(self):
        self.make(per_day=2, gap_h=6)
        self.l.put("browse:count:2026-11-11", 2)
        self.assertEqual(self.ev.guard(at("15:00"), at("16:00")), "day_cap")
        self.l.put("browse:count:2026-11-11", 1)
        self.l.put("browse:last_start", at("11:00"))
        self.assertEqual(self.ev.guard(at("15:00"), at("16:00")), "gap")
        self.assertEqual(self.ev.guard(at("17:00"), at("18:00")), "")

    def test_world_says_no_nothing_is_scheduled(self):
        for why in ("game_on", "site_paused", "phone_taken", "game_unknown"):
            with self.subTest(why=why):
                self.make(FakeWorld(ready=(False, why)), p=1.0)
                self.ev.step()
                self.assertIsNone(self.l.get("browse:event"))
                self.assertEqual(self.l.get("browse:skips:2026-11-11"), {why: 1})
                self.ev.step()  # the same slot is not drawn twice
                self.assertEqual(self.world.asked, 1)

    def test_off_never_asks_the_world(self):
        self.make(event=False, p=1.0)
        self.steps(5)
        self.assertEqual(self.world.asked, 0)
        self.assertIsNone(self.l.get("browse:event"))

    def test_who_leaves_out_asleep_away_and_a_meal(self):
        self.make(p=1.0)
        self.place("bed_mina", "mina", activity="sleeping")
        self.place("place:park", "yuki")
        self.l.meals.sync()  # today's meal rows (dinner)
        people = self.ev.who(at("14:00"), at("18:30"), 1)  # runs into dinner (18:00)
        self.assertEqual(people, [])
        people = self.ev.who(at("14:00"), at("15:00"), 1)
        self.assertNotIn("mina", people)
        self.assertNotIn("yuki", people)

    def test_who_leaves_out_a_long_pastime(self):
        """review P2: her habit never cuts her pastime short, so one that runs past start + grace is not asked."""
        self.make(p=1.0)
        for a, sec in (("nell", 3 * 3600), ("wanda", 600)):
            self.l.submit(a, "begin_activity", {"activity": "reading", "seconds": sec}, "test")
        everyone = self.ev.who(at("14:00"), at("15:00"), 1)
        free_by = self.ev.who(at("14:00"), at("15:00"), 1, by=at("14:30"))
        self.assertNotIn("nell", free_by)
        self.assertEqual([a for a in everyone if a != "nell"], free_by)  # wanda's 10 minutes end in time

    def test_too_few_leaves_the_day_free(self):
        self.make()
        self.l.put("browse:event", {"id": "kb1", "start": at("13:00"), "end": at("13:40"), "people": ["nell", "wanda"],
                                    "joined": {}, "state": "scheduled", "sites": ["bili"], "tasks": []})
        self.ev._run(self.l.get("browse:event"), at("13:45"))  # a restart after its end: nothing opened late
        self.assertEqual(self.world.opened, [])
        self.assertEqual(self.l.get("browse:events")[-1]["state"], "too_few")
        self.assertEqual(self.l.get("browse:count:2026-11-11", 0), 0)
        self.assertEqual(self.ev.guard(at("14:00"), at("15:00")), "")  # another may still come today


class SessionTest(LifeBase):
    def test_a_whole_session(self):
        self.make(p=1.0, min_people=2)
        self.steps(1)
        ev = self.l.get("browse:event")
        self.assertEqual(ev["state"], "scheduled")
        self.assertGreaterEqual(len(ev["people"]), 2, ev)
        self.assertEqual(ev["start"], at("14:20"))
        self.steps(40)  # to 14:41: everyone free sat down, the session opened at the start
        self.assertEqual(len(self.world.opened), 1)
        members, minutes, sites, when = self.world.opened[0]
        self.assertEqual(self.world.keys, [(ev["id"], 2)])  # the event id makes the world's open idempotent
        self.assertGreaterEqual(len(members), 2)
        self.assertTrue(set(members) <= set(ev["people"]))
        self.assertEqual(sites, ["bili"])
        self.assertTrue(at("14:20") <= when <= at("14:30"))
        for a in members:
            me = self.l.k.actor(a)
            self.assertEqual((me["location"], me["activity"]), ("living", "phone_browsing"), a)
        self.assertEqual(self.l.get("browse:event")["state"], "open")
        self.steps(60)
        self.assertIsNone(self.l.get("browse:event"))
        done = self.l.get("browse:events")
        self.assertEqual((len(done), done[-1]["state"], done[-1]["tasks"]), (1, "done", [101, 102]))
        self.assertEqual([n["state"] for n in self.world.notes], ["scheduled", "open", "done"])
        self.assertEqual(len(self.world.opened), 1)  # one a day (per_day 1)
        self.assertEqual(self.l.get("browse:count:2026-11-11"), 1)
        self.assertEqual(self.ev.guard(at("16:00"), at("17:00")), "day_cap")

    def test_too_few_by_the_grace_opens_nothing(self):
        self.make(p=1.0, min_people=2)
        self.steps(1)
        ev = self.l.get("browse:event")
        self.assertTrue(ev["people"])
        stay = ev["people"][1:]  # all but one go to bed before it starts
        self.place("bed_nell", *stay, activity="sleeping")
        with self.s.tx() as db:
            db.execute("UPDATE actors SET action_id=NULL")
        for a in stay:
            self.l.submit(a, "sleep", {"seconds": 4 * 3600}, "test")
        self.steps(100)
        self.assertEqual(self.world.opened, [])
        self.assertEqual(self.l.get("browse:events")[-1]["state"], "too_few")

    def test_her_pastime_stops_for_it(self):
        self.make(p=1.0, lead_min=40)
        self.steps(1)
        ev = self.l.get("browse:event")
        start = ev["start"]
        self.steps(5)
        import json
        with self.s.read() as db:
            rows = db.execute("SELECT actor_id, action_type, args, created_at FROM actions WHERE state!='failed' AND"
                              " (action_type='wait' OR (action_type='begin_activity' AND"
                              " json_extract(args,'$.activity')!='phone_browsing'))").fetchall()
        mine = [r for r in rows if r[0] in ev["people"]]
        self.assertTrue(mine, rows)  # her habit did pick something before it, and it ends in time
        for actor, kind, args, created in mine:
            self.assertLessEqual(created + json.loads(args)["seconds"], start + 60, (actor, kind, args))


class StubLife:
    """Only the runner memory and rng a draw needs (Life.get/put/rng), for a fast many-day simulation."""

    def __init__(self):
        self.mem = {}

    def get(self, k, d=None):
        return self.mem.get(k, d)

    def put(self, k, v):
        self.mem[k] = v

    def rng(self, *parts):
        return random.Random(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest())


class SimEvent(B.BrowseEvent):
    def __init__(self, life, world, c, clock):  # no kernel: who() and the session are stubbed
        self.life, self.world, self.cfg = life, world, c
        self.s = types.SimpleNamespace(clock=clock)

    def who(self, now, end, slot, by=None):
        return ["nell", "mina", "wanda"]

    def _run(self, ev, now):
        if ev["state"] == "scheduled" and now >= ev["start"]:  # everyone comes: it opens (counted as the real one)
            ev["state"] = "open"
            day = B._date(now)
            self.life.put(f"browse:count:{day}", self.life.get(f"browse:count:{day}", 0) + 1)
            self.life.put("browse:last_start", ev["start"])
            self.life.put("browse:event", ev)
        if now >= ev["end"]:
            self.life.put("browse:events", (self.life.get("browse:events") or []) + [ev])
            self.life.put("browse:event", None)


def simulate(days, c):
    life, world = StubLife(), FakeWorld()
    clock = VirtualClock(at("00:00", "2026-12-01"))
    end = clock.now() + days * 86400
    ev = SimEvent(life, world, c, clock)
    while clock.now() < end:
        ev.step()
        clock.advance(60)
    return [e["start"] for e in life.get("browse:events") or []]


class SimulationTest(unittest.TestCase):
    def test_distribution_caps_gaps_and_hours(self):
        c = cfg()  # the defaults: p 0.06 every 30 min from 10:00, one a day, 6 h apart, done by 23:00
        starts = simulate(400, c)
        days = {datetime.fromtimestamp(s, JST).date() for s in starts}
        self.assertEqual(len(days), len(starts))  # at most one a day
        # the draws that can win in a day: 10:00 .. 23:00 - lead 20 - up to 45 min -> about 24 half hours
        lo, hi = 1 - 0.94 ** 20, 1 - 0.94 ** 26
        share = len(starts) / 400
        self.assertTrue(lo - 0.06 <= share <= hi + 0.06, share)
        for s in starts:
            d = datetime.fromtimestamp(s, JST)
            self.assertTrue(10 * 60 + 20 <= d.hour * 60 + d.minute <= 22 * 60 + 35, d)
        hours = [datetime.fromtimestamp(s, JST).hour for s in starts]
        self.assertGreater(len(set(hours)), 8)  # spread over the day, not one fixed slot

    def test_more_a_day_keep_the_gap(self):
        starts = simulate(120, cfg(p=0.5, per_day=3, gap_h=4))
        self.assertTrue(starts)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 4 * 3600)
        per = {}
        for s in starts:
            per[datetime.fromtimestamp(s, JST).date()] = per.get(datetime.fromtimestamp(s, JST).date(), 0) + 1
        self.assertLessEqual(max(per.values()), 3)

    def test_zero_probability_never(self):
        self.assertEqual(simulate(60, cfg(p=0.0)), [])

    def test_a_slot_draws_the_same_every_time(self):
        """The draw is seeded by the slot alone: a restart (or a second runner) draws exactly what the first did."""
        c = cfg(p=0.2)
        self.assertEqual(simulate(60, c), simulate(60, c))


if __name__ == "__main__":
    unittest.main()
