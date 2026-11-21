"""(real-model day of 11-04,; its world date 11-03): lunch for 2 of 6. The fix tests fail on 5c4eefc
(maint-ext/life1-cutover)
and pass with the fix; the tests marked "guard" pass on both and keep the fix from going too far.

What happened: at 12:01 the head cook (nell) was shown only dish ids and cooked oyakodon, 2 servings for 6. At 12:37
her habit topped up with curry (60 min, ready 13:37), after lunch closed at 13:30. From 13:00 the four still waiting
were asked to decide; with no dishes, no deadline and nothing about the pot on the stove they chose kitchen_help
(no effect in the catalog) for half an hour, and missed lunch busy.
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cutover_fixes import Scripted, World, at  # noqa: E402
from worldkernel.life import JST  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402
from worldkernel.life_model import life_format  # noqa: E402

DAY = (2026, 11, 3)  # the world date the real-model day ran (a Tuesday): the same seeds for her habits
LUNCH_ENDS = at(*DAY, 13, 30)
MARGIN = 5 * 60  # a dish for the meal is ready five minutes before it closes (life.EAT_MARGIN_S)
LAG = 2 * 60  # a dish offered at a meal decision is started up to two minutes after it (life.DECIDE_LAG_S)


def oyakodon(ctx):
    """What the real model cooked at 12:01 for six: a dish for two."""
    return [{"action_type": "cook", "args": {"recipe": "oyakodon"}}]


def curry(ctx):
    return [{"action_type": "cook", "args": {"recipe": "curry_rice"}}]


def chikuzenni(ctx):
    """The fake week's head cook at 11-03 18:02: a dish for four, for six."""
    return [{"action_type": "cook", "args": {"recipe": "chikuzenni"}}]


def toast(ctx):
    return [{"action_type": "cook", "args": {"recipe": "toast_breakfast"}}]


def quickest_or_nothing(ctx):
    """What the fake model does at a meal decision: the quickest dish it is shown, or nothing (the stove is taken)."""
    shown = (ctx.get("life") or {}).get("cook_options") or {}
    if not shown:
        return []
    steps = [] if ctx["location"] == "kitchen" else [{"action_type": "move", "args": {"to": "kitchen"}}]
    return steps + [{"action_type": "cook", "args": {"recipe": min(shown, key=lambda r: (V.RECIPES[r]["seconds"], r))}}]


def biggest_pot(ctx):
    """A pot for everyone: of the dishes she is shown, the one with the most servings (of those, the slowest)."""
    shown = (ctx.get("life") or {}).get("cook_options") or {}
    if not shown:
        return []
    steps = [] if ctx["location"] == "kitchen" else [{"action_type": "move", "args": {"to": "kitchen"}}]
    pick = max(shown, key=lambda r: (V.RECIPES[r]["makes"][r], V.RECIPES[r]["seconds"], r))
    return steps + [{"action_type": "cook", "args": {"recipe": pick}}]


def own(w, a, action_type, args):
    """Something she does of her own accord (origin self), now."""
    got = w.life.k.submit({"idempotency_key": f"t:{a}:{action_type}", "principal": f"channel:{a}", "actor_id": a,
                           "origin": "self", "action_type": action_type, "args": args})
    assert got["status"] in ("running", "succeeded"), (a, action_type, got)


class Timed(Scripted):
    """Scripted, and when (her world time) each decision was asked."""

    def __call__(self, ctx):
        self.__dict__.setdefault("when", []).append((ctx["actor"], ctx.get("decision") or "",
                                                     (ctx.get("life") or {}).get("now")))
        return super().__call__(ctx)


def kitchen_help(ctx):
    """What the four still waiting chose at 13:01-13:09: help in the kitchen for half an hour."""
    steps = [] if ctx["location"] == "kitchen" else [{"action_type": "move", "args": {"to": "kitchen"}}]
    return steps + [{"action_type": "begin_activity", "args": {"activity": "kitchen_help", "seconds": 1800}}]


def lunch(w):
    return dict((a, (s, r)) for a, s, r in w.rows(
        "SELECT actor, status, reason FROM meal_attendance WHERE day='2026-11-03' AND slot='lunch'"))


class RealModelLunch(unittest.TestCase):
    """That lunch again: six at home, the head cook makes a dish for two, the rest answer like the real model did."""

    @classmethod
    def setUpClass(cls):
        cls.model = Scripted({"head_cook": oyakodon, "scheduled_meal_needs_preparation": kitchen_help})
        cls.w = World(cls.model, at(*DAY, 11, 40))
        cls.w.run_until(at(*DAY, 14, 30))

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_everyone_home_has_lunch(self):
        got = lunch(self.w)
        self.assertEqual(len(got), 6)
        self.assertEqual({a: s for a, (s, _) in got.items() if s != "eaten"}, {})

    def test_a_top_up_is_ready_while_lunch_is_on(self):
        cooks = self.w.rows("SELECT json_extract(args,'$.recipe'), created_at, due_at FROM actions WHERE action_type='cook'"
                            " AND created_at>=? AND created_at<?", at(*DAY, 12, 0), LUNCH_ENDS)
        self.assertGreaterEqual(len(cooks), 2)  # the model's dish for two, then her habit tops up
        late = [(r, round((due - LUNCH_ENDS) / 60)) for r, _, due in cooks if due > LUNCH_ENDS - 300]
        self.assertEqual(late, [])

    def test_nobody_is_asked_while_the_pot_is_on_in_time(self):
        asked = [a for a, kind in self.model.calls if kind.startswith("scheduled_meal")]
        self.assertEqual(asked, [])


class PotReadyNearClose(unittest.TestCase):
    """Review F7: those waiting for a pot that was coming waited in their rooms and walked over once it was ready, a
    hop a tick. On the real clock (after the cutover) a tick is 60 s plus the model calls in it; two minutes here.
    A pot for four ready at 13:25, five minutes before lunch closes: on 3d6e158 nell ate at 13:27, alma and mina
    reached the living room at 13:27 and the kitchen at 13:29, and lunch had closed (missed, busy). They wait for it
    in the kitchen."""

    TICK = 120

    def test_those_waiting_sit_down_before_lunch_closes(self):
        w = World(Scripted(), at(*DAY, 12, 44, 30))
        try:
            k = w.life.k
            w.life.meals.sync()

            def mine(a, action_type, args):
                got = k.submit({"idempotency_key": f"t:{a}:{action_type}", "principal": f"channel:{a}", "actor_id": a,
                                "origin": "self", "action_type": action_type, "args": args})
                self.assertIn(got["status"], ("running", "succeeded"), (a, action_type, got))

            for a in ("elsa", "yuki", "wanda"):
                mine(a, "sleep", {"seconds": 3 * 3600})  # a nap through lunch: three of them are not waiting for it
            mine("nell", "move", {"to": "kitchen"})
            w.clock.advance(30)
            k.tick()
            mine("nell", "cook", {"recipe": "tonjiru"})  # 12:45, 40 minutes, four servings: ready 13:25
            self.assertEqual(w.rows("SELECT due_at FROM actions WHERE action_type='cook'"), [(LUNCH_ENDS - MARGIN,)])
            while w.clock.now() < at(*DAY, 13, 45):
                w.life.step()
                w.clock.advance(self.TICK)
            got = lunch(w)
            self.assertEqual({a: got[a] for a in ("nell", "alma", "mina") if got[a][0] != "eaten"}, {})
        finally:
            w.close()


class LateDish(unittest.TestCase):
    """guard: a dish that will not be ready before lunch closes does not keep the others waiting for it."""

    def test_they_are_asked_when_nothing_comes_in_time(self):
        model = Scripted({"head_cook": curry})
        w = World(model, at(*DAY, 12, 45))
        try:
            w.run_until(at(*DAY, 13, 20))
            cooked = w.rows("SELECT due_at FROM actions WHERE action_type='cook' AND state IN ('running','succeeded')")
            self.assertTrue(cooked and cooked[0][0] > LUNCH_ENDS)  # her own choice still stands
            self.assertTrue([a for a, kind in model.calls if kind == "scheduled_meal_needs_preparation"])
        finally:
            w.close()


class TopUp(unittest.TestCase):
    """The fake week (sim7, 11-03 dinner): the head cook's dish fed four of six; her habit then made a 60-minute curry
    for the last two, who left for their 20:00 plan before it was ready. A top-up for those waiting is quick."""

    def test_a_top_up_for_those_waiting_is_quick(self):
        w = World(Scripted({"head_cook": chikuzenni}), at(*DAY, 17, 58))
        try:
            w.run_until(at(*DAY, 19, 45))
            cooks = w.rows("SELECT json_extract(args,'$.recipe'), origin FROM actions WHERE action_type='cook'"
                           " AND created_at>=? ORDER BY created_at", at(*DAY, 18, 0))
            self.assertEqual(cooks[0], ("chikuzenni", "model"))
            top_up = [r for r, origin in cooks[1:] if origin == "routine"]
            self.assertTrue(top_up)
            self.assertLessEqual(V.RECIPES[top_up[0]]["seconds"], 25 * 60, top_up[0])
            got = w.rows("SELECT actor, status FROM meal_attendance WHERE day='2026-11-03' AND slot='dinner'")
            self.assertEqual([a for a, s in got if s != "eaten"], [])  # all six by 19:45
        finally:
            w.close()


class CurryForFour(unittest.TestCase):
    """sim7-b1r5 (c334f06), 11-03 and 11-09 lunch: the head cook made curry for four at 12:02 (ready 13:02) for six.
    She sat down to it herself and only looked to top up once she had eaten (13:12: nothing was quick enough for 13:30
    any more); wanda, asked at 13:01 with the stove taken, then waited ten minutes at a time in the kitchen and was
    still waiting when mina's yakisoba (two servings, ready 13:28) came off the stove. A head cook tops up as soon as
    she sees it will not go round; someone who has decided waits for the next pot, not ten minutes blind."""

    @classmethod
    def setUpClass(cls):
        cls.model = Scripted({"head_cook": curry, "scheduled_meal_needs_preparation": quickest_or_nothing})
        cls.w = World(cls.model, at(*DAY, 11, 40))
        cls.w.run_until(at(*DAY, 14, 30))

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def test_everyone_home_has_lunch(self):
        got = lunch(self.w)
        self.assertEqual(len(got), 6)
        self.assertEqual({a: s for a, (s, _) in got.items() if s != "eaten"}, {})

    def test_the_head_cooks_curry_comes_first(self):  # guard: her own choice for the meal still stands
        cooks = self.w.rows("SELECT actor_id, json_extract(args,'$.recipe'), origin FROM actions WHERE action_type='cook'"
                            " AND created_at>=? ORDER BY created_at", at(*DAY, 12, 0))
        self.assertEqual(cooks[0], ("nell", "curry_rice", "model"))

    def test_the_head_cook_tops_up_when_it_will_not_go_round(self):
        """When the curry comes off at 13:02 with four servings for six, she starts the top-up then (the old code sat
        her down to the curry first; by 13:12 no dish was ready before 13:30)."""
        top_up = self.w.rows("SELECT json_extract(args,'$.recipe'), created_at, due_at FROM actions WHERE actor_id='nell'"
                             " AND action_type='cook' AND origin='routine' AND created_at>=? AND created_at<?",
                             at(*DAY, 13, 2), at(*DAY, 13, 5))
        self.assertEqual(len(top_up), 1, top_up)
        self.assertLessEqual(top_up[0][2] + MARGIN, LUNCH_ENDS, top_up)


class NextPot(unittest.TestCase):
    """Someone who has had her one meal decision waits in the kitchen; she waited ten minutes at a time, blind to the
    stove (sim7-b1r5, 11-09: wanda missed mina's yakisoba, ready 13:28, by two minutes). Here mina's omelette rice
    (two servings) is ready 13:28; wanda's waits end 13:03, 13:13, 13:23, 13:33 on c334f06. She waits for the pot."""

    def test_she_sits_down_to_the_next_pot(self):
        w = World(Scripted(), at(*DAY, 12, 52, 30))
        try:
            k = w.life.k
            w.life.meals.sync()

            def mine(a, action_type, args):
                got = k.submit({"idempotency_key": f"t:{a}:{action_type}", "principal": f"channel:{a}", "actor_id": a,
                                "origin": "self", "action_type": action_type, "args": args})
                self.assertIn(got["status"], ("running", "succeeded"), (a, action_type, got))

            for a in ("elsa", "yuki", "alma"):
                mine(a, "sleep", {"seconds": 3 * 3600})
            mine("nell", "begin_activity", {"activity": "working", "seconds": 2 * 3600})  # the head cook, busy at home
            mine("mina", "move", {"to": "kitchen"})
            mine("wanda", "move", {"to": "kitchen"})
            self.assertTrue(w.life.meals.claim_decision("wanda"))  # her one decision for this lunch is used
            w.clock.advance(30)
            k.tick()
            mine("mina", "cook", {"recipe": "omelette_rice"})  # 12:53, 35 minutes: ready 13:28
            self.assertEqual(w.rows("SELECT due_at FROM actions WHERE action_type='cook'"), [(at(*DAY, 13, 28),)])
            w.run_until(at(*DAY, 13, 45))
            self.assertEqual(lunch(w)["wanda"][0], "eaten")
        finally:
            w.close()


class SmallPot(unittest.TestCase):
    """sim7 of the first version, 11-03 breakfast: wanda waited for a pot with one serving and missed breakfast. Then
    review P2-1: from 09:00 the others were asked while the head cook's toast for one was on (one stove: nothing they
    could make) and spent their one decision of the meal on an empty list; none of them had breakfast after it. Now
    they wait for the pot that is coming and decide when they can cook: one it does not feed makes her own."""

    def test_after_a_pot_for_one_the_others_can_still_make_breakfast(self):
        model = Timed({"head_cook": toast, "scheduled_meal_needs_preparation": quickest_or_nothing})
        w = World(model, at(*DAY, 8, 58))
        try:
            w.run_until(at(*DAY, 10, 15))
            due = w.rows("SELECT due_at FROM actions WHERE action_type='cook'"
                         " AND json_extract(args,'$.recipe')='toast_breakfast' ORDER BY created_at")
            ready = dt.datetime.fromtimestamp(due[0][0], JST).strftime("%Y-%m-%d %H:%M")
            asked = sorted(t for _, kind, t in model.when if kind == "scheduled_meal_needs_preparation")
            self.assertTrue(asked)
            self.assertGreaterEqual(asked[0], ready)  # not while the pot for one was on
            eaten = w.rows("SELECT COUNT(*) FROM meal_attendance WHERE day='2026-11-03' AND slot='breakfast'"
                           " AND status='eaten'")[0][0]
            self.assertGreaterEqual(eaten, 2)
        finally:
            w.close()


class BigPot(unittest.TestCase):
    """Review P2-4: the head cook at work, the first one asked at 13:02 was offered a curry for four (ready after two,
    inside her own late grace) next to the quick dishes; making it for everyone, she fed herself alone and kept the
    stove: the other one waiting missed lunch. While the others wait, what she is offered is ready for them too."""

    def test_a_pot_for_everyone_is_ready_for_everyone(self):
        w = World(Scripted({"scheduled_meal_needs_preparation": biggest_pot}), at(*DAY, 12, 40))
        try:
            w.life.meals.sync()
            for a in ("elsa", "yuki", "alma"):
                own(w, a, "sleep", {"seconds": 3 * 3600})
            own(w, "nell", "begin_activity", {"activity": "working", "seconds": 2 * 3600})  # the head cook, busy
            w.run_until(at(*DAY, 14, 15))
            got = lunch(w)
            self.assertEqual((got["wanda"][0], got["mina"][0]), ("eaten", "eaten"), got)
        finally:
            w.close()


class NothingToCook(unittest.TestCase):
    """Review P3-3: from 13:05 no dish for lunch is ready in time for the others; at 13:10 the head cook was still
    asked what to cook for everyone, with an empty list (a model call for nothing)."""

    def test_the_head_cook_is_not_asked_with_nothing_to_cook(self):
        model = Scripted()
        w = World(model, at(*DAY, 13, 8))
        try:
            w.run_until(at(*DAY, 13, 16))
            self.assertEqual([c for c in model.calls if c[1].startswith("head_cook")], [])
            self.assertIn("nell", [a for a, _ in model.calls])  # guard: her own meal decision still comes
        finally:
            w.close()


class NothingLeftToCook(unittest.TestCase):
    """sim7-b1r7 (8e5a691), 11-07 dinner: the head cook had eaten, two were still waiting and nothing could be made
    in time any more; from 19:02 to 20:15 she walked to the kitchen and back every minute (36 times): her turn as head
    cook sent her to the stove before it looked whether there was anything to make."""

    def test_she_does_not_walk_to_the_kitchen_for_nothing(self):
        w = World(Scripted(), at(*DAY, 13, 6))  # nothing for lunch is ready by 13:25 any more
        try:
            w.life.meals.sync()
            with w.life.s.tx() as c:
                c.execute("UPDATE meal_attendance SET status='eaten' WHERE day='2026-11-03' AND slot='lunch'"
                          " AND actor='nell'")
            w.run_until(at(*DAY, 13, 15))
            trips = w.rows("SELECT COUNT(*) FROM actions WHERE actor_id='nell' AND action_type='move'"
                           " AND json_extract(args,'$.to')='kitchen'")[0][0]
            self.assertLessEqual(trips, 1)
        finally:
            w.close()


class WhatSheIsShown(unittest.TestCase):
    def setUp(self):
        self.w = World(Scripted(), at(*DAY, 12, 40))
        self.w.life.meals.sync()

    def tearDown(self):
        self.w.close()

    def options(self, who, kind):
        return self.w.life.life_context(who, kind)

    def test_dishes_come_with_servings_minutes_and_when_they_are_ready(self):
        # at 12:40, lunch for six still to make: what anyone is offered is ready for the others too, started up to two
        # minutes after she decides (review P2-4: elsa's list had a curry for four, ready 13:45, for her alone)
        for who, kind in (("nell", "head_cook:lunch"), ("elsa", "scheduled_meal_needs_preparation")):
            life = self.options(who, kind)
            opts = life.get("cook_options")
            self.assertIsInstance(opts, dict, kind)
            self.assertTrue(opts, kind)
            for rid, text in opts.items():
                rec = V.RECIPES[rid]
                done = self.w.clock.now() + LAG + rec["seconds"]
                self.assertIn(f"{rec['makes'][rid]}人份", text)
                self.assertIn(f"{rec['seconds'] // 60}分钟", text)
                self.assertIn(dt.datetime.fromtimestamp(done, JST).strftime("%H:%M"), text)
                self.assertLessEqual(done + MARGIN, LUNCH_ENDS, (kind, rid))
            self.assertNotIn("curry_rice", opts, kind)
            self.assertEqual(life["meal"]["ends"], "13:30")
            self.assertEqual(life["meal"]["eaters"], 6)
            self.assertEqual(life["meal"]["cooking"], [])

    def test_no_dish_she_cannot_make(self):
        self.assertIn("curry_rice", V.PEOPLE["mina"]["cooking"]["cannot"])
        w = World(Scripted(), at(*DAY, 12, 0))
        try:
            w.life.meals.sync()
            self.assertNotIn("curry_rice", w.life.life_context("mina", "head_cook:lunch")["cook_options"])
        finally:
            w.close()

    def test_her_own_late_breakfast_is_on_the_list(self):
        """yuki up at 09:14 (sim7, 11-05 and 11-09): a breakfast she starts now still counts with the late grace
        the meal clock gives her own dish, so it is on her list (the first version of the fix left her none)."""
        w = World(Scripted(), at(2026, 11, 5, 9, 14))
        try:
            w.life.meals.sync()
            opts = w.life.life_context("yuki", "scheduled_meal_needs_preparation").get("cook_options") or {}
            self.assertTrue(opts)
            own_until = at(2026, 11, 5, 9, 30) + w.life.meals.late_grace_s
            for rid in opts:
                self.assertIn("breakfast", V.RECIPES[rid]["meal"])
                self.assertLessEqual(w.clock.now() + V.RECIPES[rid]["seconds"], own_until, rid)
            # for the others the head cook only starts what is ready before 09:30 (guard)
            self.assertEqual(w.life.life_context("nell", "head_cook:breakfast")["cook_options"], {})
        finally:
            w.close()

    def test_nothing_for_her_own_serving_once_it_would_start_after_the_window(self):
        """Review P3-5: what she decides at 09:29 she starts after breakfast has closed, and the late grace is for a
        dish started inside the window: it would not count. Nothing on her list (at 09:14, above, a full one)."""
        w = World(Scripted(), at(2026, 11, 5, 9, 29))
        try:
            w.life.meals.sync()
            self.assertEqual(w.life.life_context("yuki", "scheduled_meal_needs_preparation").get("cook_options"), {})
        finally:
            w.close()

    def test_what_is_on_the_stove_is_main_dishes_and_how_many_they_feed(self):
        """Review P3-4: a soup on the stove was listed as something to wait for, and no pot said how many it feeds."""
        due = self.w.clock.now() + 600
        self.w.life.cooking = lambda: [("nell", "miso_soup", due), ("mina", "gyudon", due)]
        cooking = self.options("elsa", "scheduled_meal_needs_preparation")["meal"]["cooking"]
        self.assertEqual([(c["recipe"], c.get("servings")) for c in cooking], [("gyudon", 2)])

    def test_her_habit_cooks_only_what_is_ready_in_time(self):
        for h, m, ok in ((12, 37, True), (13, 12, False)):
            w = World(Scripted(), at(*DAY, h, m))
            try:
                w.life.meals.sync()
                got = w.life.l0_recipe("nell", "lunch", 4)
                if ok:
                    self.assertIsNotNone(got)
                    self.assertLessEqual(w.clock.now() + V.RECIPES[got]["seconds"] + 300, LUNCH_ENDS, got)
                else:
                    self.assertIsNone(got)
            finally:
                w.close()

    def test_a_meal_decision_offers_nothing_that_only_looks_like_eating(self):
        """kitchen_help (11-03); review P2-2: hotpot_night (一起吃火锅, in the kitchen) and eat_out have no effect
        either, nothing is eaten (and eat_out takes her out of the house)."""
        meal = life_format("elsa", self.options("elsa", "scheduled_meal_needs_preparation"))
        free = life_format("elsa", self.options("elsa", "free_time"))
        for act in ("kitchen_help", "hotpot_night", "eat_out"):
            self.assertNotIn(act, meal)
            self.assertIn(act, free)  # guard

    def test_a_snack_at_a_meal_decision_is_something_she_eats(self):
        """Review F4: the note said to have a snack first, and the list offered eating_snack, an activity with no
        effect (nothing is eaten), the same trap as kitchen_help. A snack is an eat of what she sees."""
        life = self.options("elsa", "scheduled_meal_needs_preparation")
        self.assertNotIn("eating_snack", life_format("elsa", life))
        self.assertIn("eating_snack", life_format("elsa", self.options("elsa", "free_time")))  # guard
        note = life["meal"]["note"]
        if "零食" in note:
            self.assertIn("eat", note)
            self.assertIn("items_here", note)


if __name__ == "__main__":
    unittest.main()
