"""(before the 11-07 cutover): what the model is told, and the limits on what she does.
The fix tests fail on 8ae902d (maint-ext/life1-meals) and pass with the fix; the tests marked "guard" pass on both
and keep the fix from going too far.

What happened on the real-model day of 11-04 (its world date 11-03):
- B4: elsa began an hour of reading at 11:51 (her free_time), through lunch. The routine stops a pastime for the
  meal she attends; the model's activity did not, and her context had no meal times.
- B5: nobody invited anyone. The reason to think was a bare code (free_time), and nothing showed the others' day.
- B6: the routine walked wanda and yuki to the living room for the console while mina had it, 7 refusals.
- B8: wanda's line called yuki 星莉: the model had roommate ids only.

And one found by this branch's own 7-day run (sim7-r1): it rains on 11-06 and 11-07, the household errand
went eleven times in a row to someone without an umbrella and turned back at the door, the fridge ran out, and on
11-08 nobody could cook breakfast or lunch. Only nell owns an umbrella (v2); whether the others get one, or the
hall gets a shared one, is still to be decided.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cutover_fixes import Scripted, World, at  # noqa: E402
from test_life_outings import Base as OutingsBase  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402
from worldkernel.life import NAMES, ROSTER  # noqa: E402
from worldkernel.life_model import LiveLifeModel  # noqa: E402

DAY = (2026, 11, 3)  # the world date of the real-model day (a Tuesday)
READ = V.ACTIVITIES["reading"]


def read_long(ctx):
    """free_time: two hours of reading in the study, whatever the time (what elsa did at 11:51, longer)."""
    steps = [] if ctx["location"] == "study" else [{"action_type": "move", "args": {"to": "study"}}]
    return steps + [{"action_type": "begin_activity", "args": {"activity": "reading", "seconds": READ["max"]}}]


def own(w, a, action_type, args, key):
    got = w.life.k.submit({"idempotency_key": f"t:{a}:{key}", "principal": f"channel:{a}", "actor_id": a,
                           "origin": "self", "action_type": action_type, "args": args})
    assert got["status"] in ("running", "succeeded", "accepted"), (a, action_type, got)


def settle(w, minutes=3):
    for _ in range(minutes):
        w.clock.advance(60)
        w.life.k.tick()


class MealStop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = World(Scripted({"free_time": read_long}), at(*DAY, 5, 0))
        cls.w.run_until(at(*DAY, 11, 50))
        cls.lunch = [r[0] for r in cls.w.rows("SELECT actor FROM meal_attendance WHERE day='2026-11-03'"
                                              " AND slot='lunch' AND status='pending'")]
        cls.ctx = {a: cls.w.life.life_context(a, "free_time") for a in cls.lunch}
        cls.w.run_until(at(*DAY, 18, 30))

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_she_is_told_when_her_next_meal_is(self):  # B4
        self.assertTrue(self.lunch)
        for a in self.lunch:
            self.assertEqual(self.ctx[a].get("next_meal"), {"slot": "lunch", "starts": "12:00", "ends": "13:30"}, a)

    def test_an_activity_she_decides_at_home_stops_for_her_next_meal(self):  # B4
        rows = self.w.rows(
            "SELECT a.actor_id, a.started_at, json_extract(a.args,'$.seconds'), m.starts FROM actions a"
            " JOIN meal_attendance m ON m.actor=a.actor_id AND m.day='2026-11-03' AND m.slot IN ('lunch','dinner')"
            " AND m.starts=(SELECT MIN(starts) FROM meal_attendance WHERE actor=a.actor_id AND ends>a.started_at)"
            " WHERE a.origin='model' AND a.action_type='begin_activity' AND a.started_at IS NOT NULL")
        crossing = [r for r in rows if r[1] + READ["max"] > r[3]]
        self.assertTrue(crossing, rows)  # the scene happened: a model activity began less than two hours before a meal
        for who, started, sec, starts in rows:
            if started >= starts:  # landed after that meal opened (none had eaten it yet in this scene): its shortest
                want = READ["min"]
            else:
                want = READ["max"] if started + READ["max"] <= starts else max(READ["min"], starts - started)
            self.assertLessEqual(abs(sec - want), 60, (who, started, sec, starts))

    def test_a_plan_or_the_outside_keeps_its_time(self):  # B4 bounds
        life = self.w.life
        a = self.lunch[0]
        self.assertIsNone(life._stop_for_meal(a, {"activity": "reading", "seconds": READ["max"], "party": "p"}))
        self.assertIsNone(life._stop_for_meal(a, {"activity": "reading", "seconds": 60}))  # the kernel refuses it


class MealJustOpened(unittest.TestCase):  # review P3: decided just before the meal, submitted after it opened
    @classmethod
    def setUpClass(cls):
        cls.w = World(Scripted(), at(*DAY, 5, 0))
        life, ask = cls.w.life, {"activity": "reading", "seconds": READ["max"]}
        cls.w.run_until(at(*DAY, 12, 1))
        a, real = cls.home(life, "lunch"), life.k.actor
        cls.opened = life._stop_for_meal(a, ask)
        with mock.patch.object(life.k, "actor", lambda x: dict(real(x), location="away")):
            cls.outside = life._stop_for_meal(a, ask)
        # review P2-2: at a meal decision "something else instead" is one of her choices, made knowing the meal is on
        with mock.patch.dict(life.cog.plans, {a: {"kind": "scheduled_meal_needs_preparation:lunch"}}):
            cls.at_meal = life._before_step(a, {"action_type": "begin_activity", "args": dict(ask)})
        # review P2-1: her eat ends in the tick at the start of a step and her next step is submitted before the
        # step's second sync; put back to that moment, her lunch is not marked eaten yet (here lunch is eaten
        # 13:12-13:13, nell and wanda at 13:32 in the late grace)
        cls.w.run_until(at(*DAY, 13, 15))
        cls.after_eat = life._stop_for_meal(cls.just_ate(life, "lunch"), ask)
        cls.w.run_until(at(*DAY, 13, 35))
        cls.after = life._stop_for_meal(cls.home(life, "dinner"), ask)

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    @staticmethod
    def home(life, slot):
        """Someone at home whose next meal (one not had yet, still on) is this one."""
        now = life.clock.now()
        found = [a for a in ROSTER if (life.next_meal_row(a, now) or ("",))[0] == slot
                 and V.LOCATIONS.get(life.k.actor(a)["location"], {}).get("kind") == "room"]
        assert found, slot
        return found[0]

    @staticmethod
    def just_ate(life, slot):
        """Someone at home who has had this meal, put back to the moment her eat ended: not yet counted."""
        for a, act in life.s.db.execute("SELECT actor, action_id FROM meal_consumption WHERE day='2026-11-03'"
                                        " AND slot=? AND kind='main' ORDER BY at", (slot,)).fetchall():
            if V.LOCATIONS.get(life.k.actor(a)["location"], {}).get("kind") == "room":
                with life.s.tx() as db:
                    db.execute("UPDATE meal_attendance SET status='pending', reason=NULL, action_id=NULL"
                               " WHERE day='2026-11-03' AND slot=? AND actor=?", (slot, a))
                    db.execute("DELETE FROM meal_consumption WHERE action_id=?", (act,))
                return a
        raise AssertionError(f"nobody at home has had {slot}")

    def test_a_step_that_lands_after_the_meal_opened_runs_its_shortest(self):
        self.assertEqual(self.opened, {"activity": "reading", "seconds": READ["min"]})

    def test_out_of_the_house_it_is_hers(self):  # guard
        self.assertIsNone(self.outside)

    def test_at_the_meal_decision_doing_something_else_is_hers(self):  # review P2-2
        self.assertIsNone(self.at_meal)

    def test_after_her_meal_what_she_does_next_keeps_its_time(self):  # review P2-1: 13:15 + 2 h, dinner at 18:00
        self.assertIsNone(self.after_eat)

    def test_one_that_ends_before_her_next_meal_is_left_alone(self):  # guard: 13:35 + 2 h, dinner at 18:00
        self.assertIsNone(self.after)


def say_to(ctx):
    """free_time: a word with to written the way an invitation's is (a list): one name for the first three of the
    roster, an empty list (the room) for the fourth, two names for the others."""
    me = ctx["actor"]
    others = [r for r in ROSTER if r != me]
    i = ROSTER.index(me)
    return [{"action_type": "say", "args": {"to": others[:1] if i < 3 else [] if i == 3 else others[:2]}}]


class SayTo(unittest.TestCase):  # review P1 (older than this branch): say's to as a list stopped the world
    @classmethod
    def setUpClass(cls):
        cls.w = World(Scripted({"free_time": say_to}), at(*DAY, 5, 0))
        cls.w.run_until(at(*DAY, 13, 0))  # on 62b5366 the first such plan raises TypeError out of every tick

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_a_word_to_a_list_of_one_reaches_the_kernel_as_her_name(self):
        says = self.w.rows("SELECT json_type(args,'$.to'), COALESCE(reason,'') FROM actions WHERE action_type='say'"
                           " AND origin='model' AND actor_id IN (?,?,?)", *ROSTER[:3])
        self.assertTrue(says)
        self.assertEqual({t for t, _ in says}, {"text"})
        self.assertNotIn("not_expressed_for_this_audience", {r for _, r in says})

    def test_an_empty_list_is_the_room(self):  # review P3: as no to, not refused
        says = self.w.rows("SELECT json_type(args,'$.to'), COALESCE(reason,'') FROM actions WHERE action_type='say'"
                           " AND origin='model' AND actor_id=?", ROSTER[3])
        self.assertTrue(says)
        self.assertEqual({t for t, _ in says}, {None})
        self.assertNotIn("not_expressed_for_this_audience", {r for _, r in says})

    def test_two_at_once_is_refused_and_the_day_goes_on(self):
        refused = self.w.rows("SELECT COUNT(*) FROM decision_points WHERE reason='say_refused:bad_args'")[0][0]
        self.assertGreater(refused, 0)
        self.assertGreaterEqual(self.w.clock.now(), at(*DAY, 13, 0))


class Privacy(unittest.TestCase):  # review P3: roommates_now shows what a housemate would know, no more
    def setUp(self):
        self.w = World(Scripted(), at(*DAY, 10, 0))
        self.addCleanup(self.w.close)

    def test_an_unanswered_invitation_and_what_a_plan_is_stay_theirs(self):
        w = self.w
        own(w, "wanda", "invite", {"to": ["yuki"], "kind": "activity", "what": "movie_night", "location": "living",
                                   "start_at": at(*DAY, 21, 0), "end_at": at(*DAY, 23, 0)}, "inv")
        seen = w.life.roommates_now("elsa")
        self.assertNotIn("plans", seen["wanda"])
        self.assertNotIn("plans", seen["yuki"])
        inv = w.life.plans.pending_for("yuki")[-1]["inv_id"]
        own(w, "yuki", "respond_invite", {"inv_id": inv, "answer": "yes"}, "yes")
        seen = w.life.roommates_now("elsa")
        self.assertEqual(seen["wanda"].get("plans"), ["11-03 21:00–23:00 有安排"])
        self.assertNotIn("movie_night", json.dumps(seen, ensure_ascii=False))

    def test_a_plan_overnight_shows_the_day_it_ends(self):  # review P3
        now = self.w.clock.now()
        with self.w.s.tx() as db:
            db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("plan-overnight", "wanda", "trip", "sightseeing", "place:kamakura", at(2026, 11, 4, 9, 0),
                        at(2026, 11, 5, 17, 0), json.dumps(["wanda", "mina"]), "confirmed", "", now, now, None))
        self.assertEqual(self.w.life.roommates_now("elsa")["wanda"].get("plans"), ["11-04 09:00–11-05 17:00 有安排"])

    def test_behind_a_bedroom_door_or_out_of_the_house_not_what_she_does(self):
        with self.w.s.tx() as db:
            for who, where in (("elsa", "kitchen"), ("wanda", V.bed("wanda")), ("nell", "away"), ("mina", "living")):
                db.execute("UPDATE actors SET location=?, activity='reading', action_id=NULL WHERE actor_id=?",
                           (where, who))
        seen = self.w.life.roommates_now("elsa")
        self.assertEqual(seen["wanda"], {"name": NAMES["wanda"], "where": "她的卧室"})
        self.assertEqual(seen["nell"], {"name": NAMES["nell"], "where": "外出"})
        self.assertEqual(seen["mina"].get("doing"), READ["label"])  # guard: in the living room she is seen


class Stub:
    """A chat answering the way the real model does when it wants company (the documented shape): at free_time,
    an invitation to a roommate to watch a film tonight; anything else, no steps. Keeps every prompt."""

    def __init__(self):
        self.prompts = []

    def __call__(self, system, user):
        self.prompts.append((system, user))
        if "让你想一想的原因：free_time" not in user:
            return '{"steps": []}', 10
        life = json.loads(user.split("你现在的情况：\n", 1)[1].split("\n让你想一想的原因", 1)[0])["life"]
        to = life["roommates"][0]
        return json.dumps({"steps": [{"action_type": "invite", "args": {
            "to": [to], "kind": "activity", "what": "movie_night", "location": "living",
            "start": life["now"][:10] + " 21:00"}}]}, ensure_ascii=False), 10


class Invite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chat = Stub()
        cls.w = World(LiveLifeModel(cls.chat, {a: a for a in ROSTER}), at(*DAY, 5, 0))
        cls.w.run_until(at(*DAY, 20, 0))
        cls.free = [u for _, u in cls.chat.prompts if "让你想一想的原因：free_time" in u]

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_free_time_says_in_words_she_may_ask_the_others_along(self):  # B5
        self.assertTrue(self.free)
        for u in self.free:
            reason = u.split("让你想一想的原因：", 1)[1].split("\n", 1)[0]
            self.assertIn("invite", reason)
            self.assertIn("roommates_now", reason)

    def test_free_time_shows_where_the_others_are_and_their_day(self):  # B5
        for u in self.free:
            life = json.loads(u.split("你现在的情况：\n", 1)[1].split("\n让你想一想的原因", 1)[0])["life"]
            now = life.get("roommates_now")
            self.assertEqual(sorted(now or {}), sorted(life["roommates"]))
            for seen in now.values():
                self.assertIn(seen["name"], "奈尔艾莎阿尔玛米娜由纪婉妲")
                self.assertTrue(seen["where"])

    def test_the_format_says_what_an_invitation_needs(self):  # B5
        for u in self.free:
            self.assertIn("activity=在家里一起做", u)
            self.assertIn("start 要晚于现在", u)

    def test_an_invitation_in_that_shape_reaches_the_world_and_the_invitee(self):  # guard: the chat stub's shape
        hosts = {r[0] for r in self.w.rows("SELECT host FROM plans WHERE kind='activity' AND what='movie_night'")}
        self.assertTrue(hosts)
        asked = [u for _, u in self.chat.prompts if "让你想一想的原因：invitation:" in u]
        self.assertTrue(asked)


class Names(unittest.TestCase):
    def test_her_line_is_written_with_their_names(self):  # B8
        w = World(Scripted(), at(*DAY, 10, 0))
        try:
            life = w.life.life_context("wanda", "free_time")
            self.assertEqual(life.get("names", {}).get("yuki"), "由纪")
            chat = Stub()
            LiveLifeModel(chat, {"wanda": "婉妲"})({"actor": "wanda", "purpose": "express", "to": "yuki",
                                                  "life": life, "location": "living",
                                                  "here": ["wanda", "yuki"], "recent": []})
            user = chat.prompts[-1][1]
            self.assertIn("你决定对由纪说一句话", user)
            self.assertIn("yuki=由纪", user)
        finally:
            w.close()


class Console(unittest.TestCase):
    def setUp(self):
        self.w = World(Scripted(), at(*DAY, 14, 50))
        own(self.w, "mina", "move", {"to": "living"}, "to_living")
        settle(self.w)
        own(self.w, "mina", "begin_activity", {"activity": "gaming", "seconds": 7200}, "gaming")

    def tearDown(self):
        self.w.close()

    def refused(self):
        return self.w.rows("SELECT actor_id, created_at FROM actions WHERE origin='routine' AND state='failed'"
                           " AND action_type='begin_activity' AND json_extract(args,'$.activity')='gaming'")

    def test_nobody_walks_over_for_the_console_while_mina_has_it(self):  # guard
        self.w.run_until(at(*DAY, 16, 50))
        self.assertEqual(self.refused(), [])

    def test_a_day_of_habits_has_no_walk_to_a_taken_console(self):  # B6
        w = World(Scripted(), at(*DAY, 5, 0))
        try:
            w.run_until(at(2026, 11, 4, 3, 0))
            refused = w.rows("SELECT actor_id, created_at FROM actions WHERE origin='routine' AND state='failed'"
                             " AND action_type='begin_activity' AND reason LIKE 'resource_unavailable%'")
            self.assertEqual(refused, [])
        finally:
            w.close()

    def test_refused_once_she_leaves_it_for_an_hour(self):  # B6
        life = self.w.life
        own(self.w, "wanda", "move", {"to": "living"}, "to_living")
        settle(self.w)
        # she picked it while it looked free (the race of 21:32 on 11-03: mina let go 43 s after she was refused)
        life.pastime_options = lambda a, now: [("gaming", 5, ["living"])]
        life._pastime("wanda", "2026-11-03", self.w.clock.now())
        self.assertEqual([r[0] for r in self.refused()], ["wanda"])
        del life.pastime_options
        life.anchor_taken = lambda rid: False  # free again: mina has stopped
        later = self.w.clock.now()
        self.assertNotIn("gaming", [k for k, _, _ in life.pastime_options("wanda", later + 1800)])
        self.assertIn("gaming", [k for k, _, _ in life.pastime_options("wanda", later + 3700)])


class RainyErrand(OutingsBase):
    def errand(self):
        self.o.shopping.targets = {"egg": 14}
        for _ in range(3):
            self.o.step()
            self.clock.advance(60)
        return self.l.get("shopping:current")

    def test_in_the_rain_the_errand_goes_with_the_one_who_has_an_umbrella(self):  # A1
        self.rain = True
        cur = self.errand()
        self.assertEqual((cur or {}).get("actor"), "nell")
        self.finish(cur["plan_id"])
        self.assertIsNone(self.o.job("nell").get("failure"))
        self.assertEqual(self.s.db.execute("SELECT COALESCE(SUM(qty),0) FROM resources WHERE item='egg'"
                                           " AND resource_id!='egg'").fetchone()[0], 2)

    def test_with_no_umbrella_in_the_house_no_errand_turns_back_at_the_door(self):  # A1
        with self.s.tx() as db:
            db.execute("DELETE FROM garments WHERE actor='nell' AND garment_id LIKE '%umbrella%'")
        self.rain = True
        self.assertIsNone(self.errand())
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM plans WHERE what='shopping'").fetchone()[0], 0)

    def test_a_dry_day_keeps_its_turns(self):  # guard
        cur = self.errand()
        self.assertIsNotNone(cur)
        self.assertNotEqual(cur["actor"], "nell")  # the round of whoever is free, as before

    def test_not_while_her_meal_is_on(self):  # A1 follow-up: sim7-r2, nell left for it right after cooking lunch
        self.rain = True
        self.clock.advance(at(2026, 11, 2, 12, 5) - self.clock.now())
        self.l.meals.sync()
        self.assertEqual(self.l.next_meal_row("nell", self.clock.now())[0], "lunch")
        self.assertIsNone(self.errand())
        self.clock.advance(at(2026, 11, 2, 13, 35) - self.clock.now())
        self.l.meals.sync()
        self.assertEqual((self.errand() or {}).get("actor"), "nell")

    def test_not_while_she_may_still_eat_what_she_made_late(self):  # review P3: the late grace after the window
        self.rain = True
        self.clock.advance(at(2026, 11, 2, 13, 29) - self.clock.now())
        self.l.meals.sync()
        with self.s.tx() as db:  # her lunch closes at 13:30 while she is still making it: the last sync in the
            # window saw her cooking (what sync writes then: the reason and when, the row stays pending)
            db.execute("UPDATE meal_attendance SET reason='preparing', last_seen=? WHERE actor='nell' AND slot='lunch'"
                       " AND status='pending'", (self.clock.now(),))
        self.clock.advance(at(2026, 11, 2, 13, 35) - self.clock.now())
        self.l.meals.sync()  # after the window, through the grace, sync leaves that row as it is (review r3 P2)
        self.assertEqual(tuple(self.s.db.execute("SELECT status, reason FROM meal_attendance WHERE actor='nell'"
                                                 " AND slot='lunch' AND day='2026-11-02'").fetchone()),
                         ("pending", "preparing"))
        self.assertIsNone(self.errand())
        self.clock.advance(at(2026, 11, 2, 14, 15) - self.clock.now())  # 40 minutes on, what she made is no lunch
        self.l.meals.sync()
        self.assertEqual((self.errand() or {}).get("actor"), "nell")

    def test_held_for_rain_leaves_a_mark_once_a_day(self):  # review P3: no errand and no record of why
        with self.s.tx() as db:
            db.execute("DELETE FROM garments WHERE actor='nell' AND garment_id LIKE '%umbrella%'")
        self.rain = True
        self.assertIsNone(self.errand())
        rows = self.s.db.execute("SELECT detail FROM audits WHERE kind='errand_held'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["reason"], "rain_no_umbrella")
        self.assertIsNone(self.errand())  # later the same day: no second mark
        self.clock.advance(86400)  # the next day, still raining
        self.assertIsNone(self.errand())
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM audits WHERE kind='errand_held'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
