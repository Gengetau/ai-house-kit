"""cutover fixes. The fix tests fail on 26c3cc3 (maint-ext/life1-fixes) and pass with the fix; the tests
marked "guard" pass on both and keep the fix from going too far.

B3 (real-model day 11-04): l0 asked her bedtime decision and went on in the same tick, so a routine action
changed her generation and her answer always came back stale (asked twice, the first decision thrown away).
B2: a sleep she decided herself (a model step) went to bed in her day clothes.
A4 (fake week 11-04): home from an outing she kept her going-out clothes on all evening.
A2 (fake week 11-04): 5 yes answers were refused as outing_schedule_conflict and then just expired; one of
them only because an outing she was already back from still counted with its planned hours.
"""
import copy
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, JST, ROSTER  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.outings import Outings  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402


def at(*parts):
    return dt.datetime(*parts, tzinfo=JST).timestamp()


class Scripted:
    """Answers the decision kinds it is given (the part before ':' -> steps); any other decision: no steps."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, ctx):
        if ctx.get("purpose") == "express":
            return {"text": "嗯。", "tokens": 0}
        kind = ctx.get("decision") or ""
        self.calls.append((ctx["actor"], kind))
        make = self.answers.get(kind.split(":", 1)[0])
        return {"steps": make(ctx) if make else [], "latency": 30, "tokens": 0}


def go_to_bed(ctx):
    """What the real model answered at bedtime on 11-04: to her room (unless she is there) and sleep ~7 h."""
    me = ctx["actor"]
    steps = [] if ctx["location"] == V.bed(me) else [{"action_type": "move", "args": {"to": V.bed(me)}}]
    return steps + [{"action_type": "sleep", "args": {"seconds": 7 * 3600}}]


class World:
    def __init__(self, model, start):
        self.path = os.path.join(tempfile.mkdtemp(), "k.db")
        self.clock = VirtualClock(start)
        self.s = Store(self.path, self.clock, world_id="ai-house/life1-test", synthetic=True, holder="test")
        self.s.acquire()
        self.life = Life(self.s, model=model)

    def run_until(self, ts):
        while self.clock.now() < ts:
            self.life.step()
            self.clock.advance(60)

    def rows(self, sql, *args):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def close(self):
        self.s.release()


class Bedtime(unittest.TestCase):
    """One evening, everyone's bedtime decided by the model the way the real model did it on 11-04."""

    @classmethod
    def setUpClass(cls):
        cls.model = Scripted({"bedtime": go_to_bed})
        cls.w = World(cls.model, at(2026, 11, 3, 17, 0))
        cls.w.run_until(at(2026, 11, 4, 4, 30))

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_her_bedtime_answer_is_not_stale(self):  # B3
        self.assertEqual(self.w.rows("SELECT actor_id, reason FROM decision_points"
                                     " WHERE kind='bedtime' AND outcome='retry'"), [])
        self.assertEqual(sorted(a for a, k in self.model.calls if k == "bedtime"), sorted(ROSTER))  # one call each
        slept = self.w.rows("SELECT actor_id FROM actions WHERE origin='model' AND action_type='sleep'"
                            " AND state IN ('running', 'succeeded')")
        self.assertEqual(sorted(r[0] for r in slept), sorted(ROSTER))

    def test_she_sleeps_in_her_sleepwear_whoever_decided(self):  # B2
        for a in ROSTER:
            self.assertEqual(self.w.life.k.actor(a)["activity"], "sleeping", a)
            self.assertEqual((self.w.life.wardrobe.current(a) or {}).get("occasion"), "sleep", a)
        # changed in her own room, before the sleep started
        for a, started in self.w.rows("SELECT actor_id, started_at FROM actions WHERE origin='model'"
                                      " AND action_type='sleep'"):
            last = self.w.rows("SELECT o.occasion, e.location FROM outfit_log o JOIN events e ON e.event_id=o.event_id"
                               " WHERE o.actor=? AND o.at<=? ORDER BY o.rowid DESC LIMIT 1", a, started)
            self.assertEqual(last, [("sleep", V.bed(a))], a)

    def test_the_fake_night_owl_stays_up_where_she_may_use_her_phone(self):
        """With B3 fixed nothing moves her first: from the study or yard the fake model's phone_browsing was
        refused (guard:activity_not_allowed_here, 6 times in the 11-05 fake week)."""
        m = FakeLifeModel()
        for minute in range(60):
            life = {"now": f"2026-11-03 23:{minute:02d}", "weekday": "二"}
            got = m({"actor": "mina", "decision": "bedtime", "location": "study", "life": life})["steps"]
            if got:
                break
        self.assertEqual([(s["action_type"], s["args"].get("to")) for s in got],
                         [("move", V.bed("mina")), ("begin_activity", None)])
        got = m({"actor": "mina", "decision": "bedtime", "location": V.bed("mina"), "life": life})["steps"]
        self.assertEqual([s["action_type"] for s in got], ["begin_activity"])


def nap(ctx):
    """A nap she decides in the afternoon: to her room and sleep an hour."""
    return go_to_bed(ctx)[:-1] + [{"action_type": "sleep", "args": {"seconds": 3600}}]


class Nap(unittest.TestCase):
    def test_a_nap_in_the_day_is_in_her_clothes(self):  # guard (review P2 on B2)
        w = World(Scripted({"free_time": nap}), at(2026, 11, 3, 13, 0))
        try:
            w.run_until(at(2026, 11, 3, 18, 0))
            napped = w.rows("SELECT DISTINCT actor_id FROM actions WHERE origin='model' AND action_type='sleep'"
                            " AND state='succeeded'")
            self.assertTrue(napped)
            self.assertEqual(w.rows("SELECT actor, at FROM outfit_log WHERE occasion='sleep'"), [])
        finally:
            w.close()


class StaleSleep(unittest.TestCase):
    def test_a_sleep_refused_as_stale_leaves_her_clothes_alone(self):  # 奈尔, her probe as a test
        w = World(Scripted({"bedtime": lambda ctx: [{"action_type": "sleep", "args": {"seconds": 7 * 3600}}]}),
                  at(2026, 11, 3, 22, 30))
        life, a = w.life, "nell"
        try:
            life.wardrobe.dress(a, "home", 20)
            life.k.submit({"idempotency_key": "t:bed", "principal": "channel:nell", "actor_id": a, "origin": "self",
                           "action_type": "move", "args": {"to": V.bed(a)}})
            w.clock.advance(300)
            life.k.tick()
            self.assertEqual(life.k.actor(a)["location"], V.bed(a))
            self.assertEqual(life.think(a, "bedtime", gap=0), "asked")
            life.k.submit({"idempotency_key": "t:rest", "principal": "channel:nell", "actor_id": a, "origin": "self",
                           "action_type": "rest", "args": {"seconds": 1}})  # what she saw moves on while she answers
            w.clock.advance(60)
            life.k.tick()
            before = life.wardrobe.current(a)["occasion"]
            res = life.cog.run_due()
            self.assertEqual(res[0].get("reason"), "stale_generation", res)
            self.assertEqual(life.wardrobe.current(a)["occasion"], before)
        finally:
            w.close()


class HomeAgain(unittest.TestCase):
    """A4: what her habit put on for going out comes off once she is home and nothing takes her out again soon."""

    def setUp(self):
        self.w = World(None, at(2026, 11, 3, 13, 0))
        self.w.run_until(at(2026, 11, 3, 13, 10))  # up and dressed for a day at home

    def tearDown(self):
        self.w.close()

    def put_on(self, origin, occasion="out", extra=()):
        life, a = self.w.life, "nell"
        w = life.weather(self.w.clock.now())
        if extra:  # what Outings does on a rainy way out: the outfit she has on, plus the item
            items = life.wardrobe.current(a)["items"] + list(extra)
        else:
            items = life.wardrobe.pick(a, occasion, w["temp_c"], w["rain"])
        cmd = {"idempotency_key": f"test:{occasion}:{origin}:{len(extra)}", "actor_id": a, "origin": origin,
               "principal": "routine" if origin == "routine" else f"channel:{a}",
               "action_type": "change_clothes", "args": {"items": items, "occasion": occasion}}
        self.assertEqual(life.k.submit(cmd)["status"], "succeeded", cmd)
        return self.w.clock.now()

    def put_on_out(self, origin):
        self.assertEqual(self.w.life.wardrobe.current("nell")["occasion"], "home")
        return self.put_on(origin)

    def changes_since(self, since):
        return self.w.rows("SELECT o.occasion, e.location FROM outfit_log o JOIN events e ON e.event_id=o.event_id"
                           " WHERE o.actor='nell' AND o.at>=? ORDER BY o.rowid", since)

    def test_back_home_she_changes_out_of_going_out_clothes(self):  # A4
        since = self.put_on_out("routine")  # what the outing engine puts on her before she leaves
        self.w.run_until(since + 3 * 3600)
        changes = self.changes_since(since)
        self.assertEqual(changes[0][0], "out")
        self.assertEqual(changes[1:2], [("home", V.bed("nell"))])  # in her own room

    def test_what_she_chose_to_wear_herself_stays_on(self):
        since = self.put_on_out("model")
        self.w.run_until(since + 3 * 3600)
        self.assertEqual([c[0] for c in self.changes_since(since)], ["out"])

    def test_her_own_choice_stays_on_with_the_umbrella_added(self):  # guard (review P3)
        since = self.put_on_out("model")
        self.put_on("routine", extra=["nell_umbrella"])  # what Outings adds on a rainy way out
        self.w.run_until(since + 3 * 3600)
        self.assertEqual([c[0] for c in self.changes_since(since)], ["out", "out"])

    def test_she_stays_dressed_when_she_goes_out_again_soon(self):  # guard
        life, now = self.w.life, self.w.clock.now()
        r = life.k.submit({"idempotency_key": "test:invite", "principal": "channel:elsa", "actor_id": "elsa",
                           "origin": "self", "action_type": "invite",
                           "args": {"to": ["nell"], "kind": "outing", "what": "stroll", "location": "place:park",
                                    "start_at": now + 2 * 3600, "end_at": now + 5 * 3600}})
        self.assertEqual(r["status"], "succeeded", r)
        inv = life.plans.pending_for("nell")[0]["inv_id"]
        self.assertEqual(life.k.submit({"idempotency_key": "test:yes", "principal": "channel:nell", "actor_id": "nell",
                                        "origin": "self", "action_type": "respond_invite",
                                        "args": {"inv_id": inv, "answer": "yes"}})["status"], "succeeded")
        since = self.put_on_out("routine")
        self.w.run_until(since + 90 * 60)
        self.assertEqual([c[0] for c in self.changes_since(since)], ["out"])

    def test_up_in_the_day_in_sleepwear_she_changes(self):  # review P2 on B2: a refused or long day sleep
        since = self.put_on("routine", "sleep")  # put on where she is (the kitchen)
        self.w.run_until(since + 3 * 3600)
        changes = self.changes_since(since)
        self.assertEqual(changes[0][0], "sleep")
        self.assertEqual(changes[1:2], [("home", V.bed("nell"))])  # changed back in her own room

    def test_an_umbrella_she_adds_herself_makes_it_her_choice(self):  # review r2 P2-1
        since = self.put_on_out("routine")
        self.put_on("model", extra=["nell_umbrella"])  # she takes her umbrella along herself
        self.w.run_until(since + 3 * 3600)
        self.assertEqual([c[0] for c in self.changes_since(since)], ["out", "out"])

    def test_going_out_on_top_of_her_own_home_clothes_comes_off(self):  # review r2 P2-1
        since = self.put_on("model", "home")  # her own choice for a day at home
        self.put_on("routine", "out", extra=["nell_umbrella"])  # her habit dresses her to go out: one item more
        self.w.run_until(since + 3 * 3600)
        changes = self.changes_since(since)
        self.assertEqual([c[0] for c in changes[:2]], ["home", "out"])
        self.assertEqual(changes[2:3], [("home", V.bed("nell"))])

    def test_first_up_today_away_from_her_room_she_changes_in_her_room(self):  # review r2 P2-2
        since = self.put_on_out("routine")  # where she is (the kitchen), as if the outing engine had her all day
        self.assertFalse(self.w.life._dress_for_day("nell", "test-day", self.w.clock.now()))
        self.w.run_until(since + 3 * 3600)
        changes = self.changes_since(since)
        self.assertEqual(changes[0][0], "out")
        self.assertEqual(changes[1:2], [("home", V.bed("nell"))])


def prices():
    """Synthetic fares, as in test_life_outings: the wallet never decides these tests."""
    out = copy.deepcopy(V.PLACES)
    for p in out.values():
        p["travel"]["price_yen"] = 0 if p["travel"]["mode"] == "walk" else 500
        p["activity_costs_yen"] = {a: 0 for a in p["activities"]}
    return out


def answer_invitation(ctx):
    """Yes; once her yes was refused she sees why and says no."""
    inv_id = ctx["decision"].split(":", 1)[1]
    inv = next(i for i in ctx["life"]["invitations"] if i["inv_id"] == inv_id)
    answer = "no" if inv.get("yes_refused") else "yes"
    return [{"action_type": "respond_invite", "args": {"inv_id": inv_id, "answer": answer, "reason": "嗯"}}]


def always_yes(ctx):
    return [{"action_type": "respond_invite", "args": {"inv_id": ctx["decision"].split(":", 1)[1], "answer": "yes"}}]


class Invitations(unittest.TestCase):
    def setUp(self):
        self.model = Scripted({"invitation": answer_invitation})
        self.clock = VirtualClock(at(2026, 11, 2, 10, 0))
        self.s = Store(os.path.join(tempfile.mkdtemp(), "w.db"), self.clock, world_id="test/life1")
        self.s.acquire()
        self.l = Life(self.s, model=self.model, weather=lambda _: dict(rain=False, temp_c=15),
                      extensions=[lambda life: Outings(life, places=prices(), targets={})])
        self.o, self.n = self.l.extensions[0], 0

    def tearDown(self):
        self.s.release()

    def command(self, actor, action, args):
        self.n += 1
        return self.l.k.submit(dict(idempotency_key=f"test:{self.n}", principal=f"channel:{actor}", actor_id=actor,
                                    origin="self", action_type=action, args=args))

    def invite(self, host, guest, start, end, kind="outing", place="park", what="stroll"):
        r = self.command(host, "invite", dict(to=[guest], kind=kind, what=what, location="place:" + place,
                                              start_at=start, end_at=end))
        self.assertEqual(r["status"], "succeeded", r)
        return r["plan_id"], next(i["inv_id"] for i in self.l.plans.pending_for(guest) if i["plan_id"] == r["plan_id"])

    def answer(self, guest, inv, answer="yes"):
        return self.command(guest, "respond_invite", dict(inv_id=inv, answer=answer))

    def test_an_outing_she_is_back_from_is_not_in_the_way(self):
        now = self.clock.now()
        p, inv = self.invite("nell", "wanda", now, now + 4 * 3600)
        self.assertEqual(self.answer("wanda", inv)["status"], "succeeded")
        for _ in range(600):  # the walk to the park, the stroll and home again, well before the planned 14:00
            self.l.k.tick()
            self.o.step()
            if self.l.plans.plan(p)["state"] == "done":
                break
            self.clock.advance(60)
        self.assertEqual(self.l.plans.plan(p)["state"], "done")
        self.assertLess(self.clock.now(), now + 4 * 3600)
        _, inv2 = self.invite("elsa", "wanda", self.clock.now() + 600, at(2026, 11, 2, 17, 0))
        self.assertEqual(self.answer("wanda", inv2)["status"], "succeeded")

    def test_her_own_journey_over_while_the_others_stay_on(self):
        now = self.clock.now()
        p, inv = self.invite("nell", "wanda", now, now + 4 * 3600)
        self.assertEqual(self.answer("wanda", inv)["status"], "succeeded")
        self.l.plans.set_state(p, "running")
        self.l.put(f"journey:{p}:wanda", "done")  # she turned back; nell is still out
        _, inv2 = self.invite("elsa", "wanda", now + 3600, now + 5 * 3600)
        self.assertEqual(self.answer("wanda", inv2)["status"], "succeeded")
        _, inv3 = self.invite("mina", "nell", now + 3600, now + 5 * 3600)
        self.assertEqual(self.answer("nell", inv3).get("reason"), "outing_schedule_conflict")

    def test_a_trip_she_is_back_from_still_counts_for_the_month(self):
        p, inv = self.invite("elsa", "wanda", at(2026, 11, 5, 10, 0), at(2026, 11, 6, 17, 0), "trip", "hakone",
                             "sightseeing")
        self.assertEqual(self.answer("wanda", inv)["status"], "succeeded")
        self.l.plans.set_state(p, "done")
        _, inv2 = self.invite("nell", "wanda", at(2026, 11, 12, 10, 0), at(2026, 11, 13, 17, 0), "trip", "hakone",
                              "sightseeing")
        self.assertEqual(self.answer("wanda", inv2).get("reason"), "monthly_overnight_limit")

    def test_a_refused_yes_is_hers_to_answer_again_not_left_to_expire(self):
        _, inv = self.invite("elsa", "nell", at(2026, 11, 2, 15, 0), at(2026, 11, 2, 18, 0))
        self.assertEqual(self.answer("nell", inv)["status"], "succeeded")
        _, inv2 = self.invite("mina", "nell", at(2026, 11, 2, 16, 0), at(2026, 11, 2, 19, 0))
        for _ in range(10):
            self.l.step()
            self.clock.advance(60)
        self.assertEqual([k for a, k in self.model.calls if a == "nell" and k.startswith("invitation:")],
                         [f"invitation:{inv2}"] * 2)
        self.assertEqual(self.l.yes_refused("nell", inv2), "outing_schedule_conflict")
        state = self.s.db.execute("SELECT state FROM invitations WHERE inv_id=?", (inv2,)).fetchone()[0]
        self.assertEqual(state, "declined")  # the host hears she cannot make it

    def asks(self, inv):
        return [k for a, k in self.model.calls if k == f"invitation:{inv}"]

    def test_asked_again_once_only(self):  # review: a model that keeps saying yes is not asked every minute
        self.model.answers["invitation"] = always_yes
        _, inv = self.invite("elsa", "nell", at(2026, 11, 2, 15, 0), at(2026, 11, 2, 18, 0))
        self.assertEqual(self.answer("nell", inv)["status"], "succeeded")
        _, inv2 = self.invite("mina", "nell", at(2026, 11, 2, 16, 0), at(2026, 11, 2, 19, 0))
        while self.clock.now() < at(2026, 11, 2, 13, 5):  # past the invitation's deadline (13:00)
            self.l.step()
            self.clock.advance(60)
        self.assertEqual(len(self.asks(inv2)), 2)
        self.assertEqual(self.s.db.execute("SELECT state FROM invitations WHERE inv_id=?", (inv2,)).fetchone()[0],
                         "expired")

    def test_no_asks_for_a_closed_plan(self):  # review P3
        q, inv = self.invite("mina", "nell", at(2026, 11, 2, 16, 0), at(2026, 11, 2, 19, 0))
        self.l.plans.set_state(q, "cancelled")
        for _ in range(5):
            self.l.step()
            self.clock.advance(60)
        self.assertEqual(self.asks(inv), [])

    def test_the_fake_model_says_why_she_cannot(self):
        life = {"now": "2026-11-02 10:00", "invitations": [{"inv_id": "inv-1", "kind": "outing", "what": "stroll",
                                                            "yes_refused": "insufficient_funds"}]}
        got = FakeLifeModel()({"actor": "nell", "decision": "invitation:inv-1", "life": life})["steps"]
        self.assertEqual([(s["args"]["answer"], s["args"]["reason"]) for s in got], [("no", "这个月钱不太够")])


if __name__ == "__main__":
    unittest.main()
