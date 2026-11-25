"""Her own `move` from home to a place:* becomes a one-person outing instead of `outing_plan_required`.

Offline: reuses the in-memory mock bridge from test_city_m1a (no network, no paid calls).
The checks of an invitation (route, money, schedule, trip notice) still apply; their reason is the failure.
"""
import json
import unittest

import test_city_m1a as m1a
from worldkernel.life_model import LiveLifeModel


class OutingMoveGuardTests(unittest.TestCase):
    setUp = m1a.CityBridgeM1ATests.setUp
    _boot = m1a.CityBridgeM1ATests._boot
    _restart_kernel = m1a.CityBridgeM1ATests._restart_kernel
    tearDown = m1a.CityBridgeM1ATests.tearDown

    def _quiet(self):
        # Quiet mid-afternoon: nobody is cooking or eating, so a due plan is not held back by a meal.
        self.clock.advance(2.5 * 3600)
        with self.s.tx() as db:
            for a in m1a.ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)

    def _move(self, to, key="mv:1", origin="model", actor="nell"):
        return self.k.submit(dict(idempotency_key=key, principal=f"channel:{actor}", actor_id=actor,
                                  origin=origin, action_type="move", args={"to": to}))

    def _plans(self):
        with self.s.read() as db:
            return [dict(r) for r in db.execute("SELECT * FROM plans WHERE kind IN ('outing','trip')")]

    def _carry(self):
        with self.s.read() as db:
            return [dict(r) for r in db.execute("SELECT * FROM city_visit_carry")]

    def _audits(self, kind):
        with self.s.read() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT detail FROM audits WHERE kind=?", (kind,))]

    def _run(self, minutes, step_s=60):
        for _ in range(int(minutes * 60 / step_s)):
            self.clock.advance(step_s)
            self.life.step()

    def test_move_to_city_place_becomes_one_city_visit(self):
        self._quiet()
        r = self._move("place:muginohi")
        self.assertEqual("succeeded", r["status"], r)
        plans = self._plans()
        self.assertEqual(1, len(plans))
        self.assertEqual(("nell", "place:muginohi", "confirmed"), (plans[0]["host"], plans[0]["location"], plans[0]["state"]))
        self.assertEqual(["nell"], json.loads(plans[0]["members"]))
        self.assertEqual(plans[0]["plan_id"], r["plan_id"])
        self.assertTrue(r.get("action_id"), "a finished action is left behind, like an instant action")
        with self.s.read() as db:
            self.assertEqual("succeeded", db.execute("SELECT state FROM actions WHERE action_id=?",
                                                     (r["action_id"],)).fetchone()[0])
        self.assertEqual(1, len(self._audits("move_became_outing")))
        self._run(15)
        self.assertEqual(1, len(self.engine.visits), "the city bridge ran exactly one visit")
        self.assertEqual(1, len(self._carry()))
        self._run(10)
        self.assertEqual(1, len(self.engine.visits))
        self.assertEqual(1, len(self._plans()), "no second plan")

    def test_move_to_local_place_becomes_local_outing(self):
        self._quiet()
        r = self._move("place:library")
        self.assertEqual("succeeded", r["status"], r)
        pid = r["plan_id"]
        job = None
        for _ in range(30):
            self._run(1)
            job = self.adapter._outings.job("nell")
            if job:
                break
        self.assertIsNotNone(job, "local outing started")
        self.assertEqual(pid, job["plan_id"])
        self.assertEqual({}, self.engine.visits)
        self.assertEqual(1, len(self._plans()))

    def test_model_plan_step_completes_and_does_not_hold_her(self):
        # the real path: her model plan submits the move, follows its action_id and moves on (review P1)
        self._quiet()
        cog = self.life.cog
        ctx = cog.context("nell")
        ctx["domain_epoch"] = self.k.city_visits.authority("nell")["domain_epoch"]
        cog.plans["nell"] = {"steps": [{"action_type": "move", "args": {"to": "place:muginohi"}}], "rid": "rqmv",
                            "i": 0, "action_id": None, "context": ctx, "retries": 0, "kind": "free_time",
                            "expires_at": self.clock.now() + 3600}
        cog.advance_plans()
        self.assertEqual(1, len(self._plans()))
        cog.advance_plans()
        self.assertNotIn("nell", cog.plans, "the plan's only step succeeded, so the plan is over")
        self.assertFalse(self.life.busy("nell"))
        self._run(15)
        self.assertEqual(1, len(self.engine.visits))
        self.assertEqual(1, len(self._plans()))

    def test_move_with_extra_arguments_is_not_taken_over(self):
        self._quiet()
        r = self.k.submit(dict(idempotency_key="mv:extra", principal="channel:nell", actor_id="nell", origin="model",
                               action_type="move", args={"to": "place:library", "with": ["elsa"]}))
        self.assertNotEqual("succeeded", r["status"])
        self.assertEqual([], self._plans())

    def test_replay_of_the_same_command_makes_one_plan(self):
        self._quiet()
        a = self._move("place:muginohi", key="mv:replay")
        b = self._move("place:muginohi", key="mv:replay")
        self.assertEqual("succeeded", a["status"])
        self.assertEqual(a["plan_id"], b["plan_id"])
        self.assertEqual(1, len(self._plans()))

    def test_second_move_while_the_first_is_pending_is_refused(self):
        self._quiet()
        self.assertEqual("succeeded", self._move("place:muginohi", key="mv:a")["status"])
        r = self._move("place:park", key="mv:b")
        self.assertEqual("failed", r["status"])
        self.assertEqual("outing_schedule_conflict", r["reason"])
        self.assertEqual(1, len(self._plans()))

    def test_move_inside_the_house_is_untouched(self):
        self._quiet()
        r = self._move("kitchen")
        self.assertNotEqual("outing_plan_required", r.get("reason"))
        self.assertNotIn("plan_id", r)
        self.assertEqual([], self._plans())

    def test_other_origins_still_need_a_plan(self):
        self._quiet()
        r = self._move("place:muginohi", key="mv:self", origin="self")
        self.assertEqual("failed", r["status"])
        self.assertIn("outing_plan_required", r["reason"])
        self.assertEqual([], self._plans())

    def test_not_enough_money_is_refused_with_the_reason(self):
        self._quiet()
        outings = self.adapter._outings
        outings.places["library"]["travel"]["price_yen"] = 50000
        r = self._move("place:library")
        self.assertEqual("failed", r["status"])
        self.assertEqual("insufficient_funds", r["reason"])
        self.assertEqual([], self._plans())

    def test_trip_notice_still_applies(self):
        self._quiet()
        r = self._move("place:hakone")
        self.assertEqual("failed", r["status"])
        # an overnight place cannot be reached by a bare move: it needs nights and notice, which only an invite carries
        self.assertEqual("unsupported_trip_nights", r["reason"])
        self.assertEqual([], self._plans())

    def test_rain_rule_still_applies(self):
        self._quiet()
        self.adapter._outings.l.weather = lambda when: {"rain": True, "temp_c": 12}
        self.adapter._outings.places["park"]["rain"] = "avoid"
        r = self._move("place:park")
        self.assertEqual("failed", r["status"])
        self.assertEqual("avoid_place_in_rain", r["reason"])
        self.assertEqual([], self._plans())

    def test_unknown_place_is_refused(self):
        self._quiet()
        r = self._move("place:nowhere-at-all")
        self.assertEqual("failed", r["status"])
        self.assertEqual([], self._plans())

    def test_busy_actor_falls_through_to_the_old_guard(self):
        self._quiet()
        busy = self.k.submit(dict(idempotency_key="busy:1", principal="channel:nell", actor_id="nell", origin="model",
                                  action_type="wait", args={"seconds": 600}))
        self.assertIn(busy["status"], ("running", "accepted"), busy)
        r = self._move("place:muginohi", key="mv:busy")
        self.assertNotEqual("succeeded", r["status"])
        self.assertEqual([], self._plans())

    def _cognize(self, steps, actor="nell"):
        prev = self.life.model
        self.life.model = LiveLifeModel(lambda system, user: (json.dumps({"steps": steps}), 0), {})
        try:
            self.assertEqual("asked", self.life.think(actor, "free_time", gap=0))
        finally:
            self.life.model = prev
        first, = self.life.cog.run_due()
        return self.life.cog, first

    def _nonalias_city_place(self):
        return next(
            p["id"] for p in self.engine.places
            if p["id"] not in self.adapter.city_place_targets.values()
        )

    def _catalog_reqs(self):
        return sum(1 for m, p, _, _ in self.engine.request_log if m == "GET" and p == "/catalog")

    def test_multistep_move_then_sleep_drops_remaining_steps_and_completes_one_visit(self):
        # P2-1: multi-step reply must not run trailing home steps (e.g. sleep) after move becomes an outing
        self._quiet()
        self.adapter.enable_all_roommates()
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", location=self.k.V.bed("nell"))
        cog, first = self._cognize([
            {"action_type": "move", "args": {"to": "place:muginohi"}},
            {"action_type": "sleep", "args": {"seconds": 10800}},
        ])
        self.assertEqual("succeeded", first["status"], first)
        pid = first["plan_id"]
        self.assertEqual([], cog.advance_plans(), "trailing sleep step must not be submitted")
        self.assertNotIn("nell", cog.plans)
        with self.s.read() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM cognition_plans WHERE actor_id='nell'").fetchone()[0])
        self.assertFalse(self.life.busy("nell"))
        self.assertIsNone(self.k.actor("nell")["action_id"])
        dropped = self._audits("cognition_steps_dropped_for_outing")
        self.assertEqual(1, len(dropped))
        self.assertEqual(("nell", pid, 1, ["sleep"]),
                         (dropped[0]["actor"], dropped[0]["plan_id"], dropped[0]["dropped_count"],
                          dropped[0]["dropped_actions"]))
        # From her bedroom she walks bedroom -> living -> away, then completes handover to active.
        self.adapter.step()
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        with self.s.read() as db:
            visits = [dict(r) for r in db.execute(
                "SELECT actor_id, visit_id, state FROM city_visits JOIN city_visit_carry USING(visit_id,actor_id)"
                " WHERE plan_id=?", (pid,))]
            sleeps = db.execute(
                "SELECT COUNT(*) FROM actions WHERE actor_id='nell' AND action_type='sleep'").fetchone()[0]
        self.assertEqual(0, sleeps)
        self.assertEqual(1, len(visits))
        self.assertEqual("active", visits[0]["state"])
        self.engine.simulate_return_and_settle(visits[0]["visit_id"])
        self.adapter.step()
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        self.assertEqual(1, len(self.engine.visits))
        self.assertEqual("done", self.life.plans.plan(pid)["state"])
        self.assertFalse(self.life.busy("nell"))

    def test_multistep_indoor_move_then_outing_move_then_sleep_drops_tail(self):
        # P2-1: when move->place:* is a later step (not step 0), its tail is still dropped cleanly
        self._quiet()
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", location=self.k.V.bed("nell"))
        cog, first = self._cognize([
            {"action_type": "move", "args": {"to": "living"}},
            {"action_type": "move", "args": {"to": "place:muginohi"}},
            {"action_type": "sleep", "args": {"seconds": 10800}},
        ])
        self.assertEqual("running", first["status"], first)
        self.clock.advance(120)
        self.k.tick()
        second, = cog.advance_plans()
        self.assertEqual("succeeded", second["status"], second)
        self.assertEqual("started_for_you", second.get("outing"))
        self.assertNotIn("nell", cog.plans)
        with self.s.read() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM cognition_plans WHERE actor_id='nell'").fetchone()[0])
        self.assertFalse(self.life.busy("nell"))
        self.assertEqual([], cog.advance_plans())
        dropped = self._audits("cognition_steps_dropped_for_outing")
        self.assertEqual(1, len(dropped))
        self.assertEqual(["sleep"], dropped[0]["dropped_actions"])
        self._run(15)
        self.assertEqual(1, len(self.engine.visits))
        with self.s.read() as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM actions WHERE actor_id='nell' AND action_type='sleep'").fetchone()[0])

    def test_multistep_move_to_local_place_drops_remaining_steps_and_starts_local_outing(self):
        self._quiet()
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", location=self.k.V.bed("nell"))
        cog, first = self._cognize([
            {"action_type": "move", "args": {"to": "place:library"}},
            {"action_type": "sleep", "args": {"seconds": 10800}},
        ])
        self.assertEqual("succeeded", first["status"], first)
        pid = first["plan_id"]
        self.assertEqual([], cog.advance_plans())
        self.assertNotIn("nell", cog.plans)
        with self.s.read() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM cognition_plans WHERE actor_id='nell'").fetchone()[0])
        self.assertFalse(self.life.busy("nell"))
        self.assertEqual(1, len(self._audits("cognition_steps_dropped_for_outing")))
        job = None
        for _ in range(30):
            self._run(1)
            job = self.adapter._outings.job("nell")
            if job:
                break
        self.assertIsNotNone(job)
        self.assertEqual(pid, job["plan_id"])
        self.assertEqual({}, self.engine.visits)

    def test_persisted_outing_step_in_advance_plans_also_drops_remaining_steps(self):
        # Defense-in-depth: a persisted cognition plan whose action_id was already turned into an outing
        self._quiet()
        r = self._move("place:muginohi", key="mv:persisted")
        self.assertEqual("succeeded", r["status"], r)
        cog = self.life.cog
        ctx = cog.context("nell")
        ctx["domain_epoch"] = self.k.city_visits.authority("nell")["domain_epoch"]
        cog.plans["nell"] = {
            "steps": [
                {"action_type": "move", "args": {"to": "place:muginohi"}},
                {"action_type": "sleep", "args": {"seconds": 10800}},
            ],
            "rid": "rqmv-legacy", "i": 0, "action_id": r["action_id"], "context": ctx,
            "retries": 0, "kind": "free_time", "expires_at": self.clock.now() + 3600,
        }
        cog._save_plan("nell")
        self.assertEqual([], cog.advance_plans())
        self.assertNotIn("nell", cog.plans)
        with self.s.read() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM cognition_plans WHERE actor_id='nell'").fetchone()[0])
        self.assertFalse(self.life.busy("nell"))
        dropped = self._audits("cognition_steps_dropped_for_outing")
        self.assertEqual(1, len(dropped))
        self.assertEqual(["sleep"], dropped[0]["dropped_actions"])

    def test_cold_catalog_move_to_nonalias_city_place_matches_invite(self):
        # P2-2: cold catalog warms for move->place:<area>:<place> just like invite
        self._quiet()
        self.adapter.enable_all_roommates()
        known = self._nonalias_city_place()
        target = "place:" + known
        self.assertEqual({}, self.adapter._places_by_id)
        r = self._move(target, key="mv:cold:nonalias")
        self.assertEqual("succeeded", r["status"], r)
        self.assertIn(known, self.adapter._places_by_id)
        plans = self._plans()
        self.assertEqual(1, len(plans))
        self.assertEqual(("nell", target, "confirmed"), (plans[0]["host"], plans[0]["location"], plans[0]["state"]))
        self._run(15)
        self.assertEqual(1, len(self.engine.visits))
        visit, = self.engine.visits.values()
        self.assertEqual(known, visit["plan"]["destination"])

    def test_cold_catalog_move_via_cognition_accepts_nonalias_city_place(self):
        # P2-2 on the real Cognition path (where k.submit runs inside s.tx() after before_step)
        self._quiet()
        self.adapter.enable_all_roommates()
        known = self._nonalias_city_place()
        target = "place:" + known
        self.assertEqual({}, self.adapter._places_by_id)
        cog, first = self._cognize([{"action_type": "move", "args": {"to": target}}])
        self.assertEqual("succeeded", first["status"], first)
        self.assertIn(known, self.adapter._places_by_id)
        self.assertNotIn("nell", cog.plans)
        with self.s.read() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM cognition_plans WHERE actor_id='nell'").fetchone()[0])
        self.assertFalse(self.life.busy("nell"))
        self.assertEqual([], self._audits("cognition_steps_dropped_for_outing"))
        self._run(15)
        self.assertEqual(1, len(self.engine.visits))
        visit, = self.engine.visits.values()
        self.assertEqual(known, visit["plan"]["destination"])

    def test_cold_catalog_offline_and_nonmodel_boundaries_match_invite(self):
        # P2-2 boundaries: non-model move does not warm; offline bridge fails non-alias like invite,
        # while alias still accepts without pre-checking bridge reachability.
        self._quiet()
        self.adapter.enable_all_roommates()
        known = self._nonalias_city_place()
        target = "place:" + known
        self.assertEqual({}, self.adapter._places_by_id)

        # origin='self' is not taken over by move_as_outing, so it must not trigger /catalog warming
        r_self = self._move(target, key="mv:cold:self", origin="self")
        self.assertEqual("failed", r_self["status"])
        self.assertEqual(0, self._catalog_reqs())
        self.assertEqual({}, self.adapter._places_by_id)

        # Offline bridge: non-alias move probes once and fails with unknown_location, matching invite
        self.engine.catalog_offline = True
        r_move_down = self._move(target, key="mv:down:nonalias")
        self.assertEqual(("failed", "unknown_location"), (r_move_down["status"], r_move_down["reason"]))
        self.assertEqual(1, self._catalog_reqs())
        r_inv_down = self.k.submit(dict(
            idempotency_key="inv:down:nonalias", principal="channel:elsa", actor_id="elsa",
            origin="self", action_type="invite",
            args={"to": ["alma"], "kind": "outing", "location": target, "start_at": self.clock.now() + 1200},
        ))
        self.assertEqual(("failed", "unknown_location"), (r_inv_down["status"], r_inv_down["reason"]))
        self.assertEqual(1, self._catalog_reqs(), "cooldown suppresses duplicate probe")

        # Alias city place still accepts while bridge is offline (no acceptance-time reachability precheck)
        r_alias_down = self._move("place:muginohi", key="mv:down:alias")
        self.assertEqual("succeeded", r_alias_down["status"], r_alias_down)
        self.assertEqual(1, self._catalog_reqs())


if __name__ == "__main__":
    unittest.main()
