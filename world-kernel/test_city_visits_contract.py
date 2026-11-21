"""K1 contract tests: synthetic Stores only; no engine, model/network or live DB."""
import datetime as dt
import json
import os
import tempfile
import unittest

from worldkernel.store import Store, VirtualClock, LockHeld
from worldkernel.life import Life
from worldkernel.kernel import Refused
from worldkernel.routine import Routine
from worldkernel import materials
from worldkernel.guide import Guide
from unittest.mock import patch

# Contract oracle, independent of the implementation's edge table.
TRANSITIONS = {
    'preparing': ('prepared', 'aborted'), 'prepared': ('active', 'aborted'),
    'active': ('returning',), 'returning': ('settling',), 'settling': ('settled',),
    'settled': ('closed', 'reconcile'), 'closed': (), 'reconcile': (), 'aborted': (),
}

START = dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()


class Visits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "synthetic.db")
        self.clock = VirtualClock(START)
        self.open()
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", location="living")
            self.k._set_actor(db, "elsa", location="living")

    def open(self, **kwargs):
        self.s = Store(self.path, self.clock, synthetic=True, seed=237)
        self.s.acquire()
        self.life = Life(self.s, weather=lambda _: dict(rain=False, temp_c=20), **kwargs)
        self.k, self.v = self.life.k, self.life.city_visits

    def tearDown(self):
        self.s.release()
        self.s.db.close()
        self.tmp.cleanup()

    def plan(self):
        return dict(destination="rurigai:kissa-tomoshibi", start_at_ms=int(self.clock.now() * 1000),
                    return_by_ms=int((self.clock.now() + 3600) * 1000))

    def begin(self, actor="nell", vid="v1", tid="t1", req="begin"):
        return self.v.begin(vid, actor, tid, req, self.plan())

    def receipt(self, state, req, vid="v1"):
        return dict(receipt_id="r:" + req, request_id=req, visit_id=vid, state=state,
                    accepted_at_ms=int(self.clock.now() * 1000), payload={})

    def change(self, state, vid="v1", req=None):
        req = req or state
        kw = {}
        if state in ("prepared", "active", "aborted"):
            kw["receipt"] = self.receipt(state, req, vid)
        if state == "settled":
            kw["settlement_id"] = "set:" + vid
        return self.v.transition(vid, state, req, self.v.get(vid)["domain_epoch"], **kw)

    def depart(self):
        self.begin()
        self.clock.advance(60)
        self.k.tick()
        self.change("prepared")
        return self.change("active")

    def settled(self):
        self.depart()
        for state in ("returning", "settling", "settled"):
            self.change(state)

    def command(self, key="home-command", epoch=None, actor="nell", action="check", args=None):
        cmd = dict(idempotency_key=key, actor_id=actor, principal="channel:" + actor, origin="self",
                   action_type=action, args=args if args is not None else {"resource_id": "tv"})
        if epoch is not None:
            cmd["domain_epoch"] = epoch
        return self.k.submit(cmd)

    def reason(self, reason, fn):
        with self.assertRaises(Refused) as cm:
            fn()
        self.assertEqual(reason, cm.exception.reason)

    def snapshot(self):
        tables = ("city_visits", "actor_domains", "city_visit_requests", "city_visit_receipts",
                  "city_visit_log", "events", "actors", "actions", "resources", "jobs")
        return {t: [tuple(r) for r in self.s.db.execute("SELECT * FROM " + t)] for t in tables}

    def test_t1_atomic_record_regular_door_move_and_body_drift(self):
        before = self.k.actor("nell")["body"]
        visit = self.begin()
        self.assertEqual(visit["domain_epoch"], 1)
        self.assertEqual(self.v.authority("nell")["domain_epoch"], 0)
        self.assertEqual(self.v.authority("nell")["domain"], "home")
        self.assertFalse(self.v.home_allowed("nell"))
        self.assertEqual(self.k.actor("nell")["activity"], "walking")
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual(self.k.actor("nell")["location"], "away")
        self.assertEqual(self.k.actor("nell")["activity"], "idle")
        self.assertNotEqual(before, self.k.actor("nell")["body"])
        self.assertEqual(1, self.s.db.execute("SELECT count(*) FROM city_visit_log").fetchone()[0])
        self.assertEqual("succeeded", self.s.db.execute("SELECT state FROM actions WHERE action_id=?",
                                                      (visit["departure_action_id"],)).fetchone()[0])

    def test_full_cycle_and_second_visit_monotone_epochs(self):
        self.settled()
        self.assertEqual(self.v.authority("nell")["domain"], "city")
        self.assertFalse(self.v.home_allowed("nell"))
        row = self.change("closed")
        self.assertEqual(2, row["domain_epoch"])
        self.assertEqual("home", self.v.authority("nell")["domain"])
        self.assertTrue(self.v.home_allowed("nell"))
        self.assertEqual("away", self.k.actor("nell")["location"])
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("living", self.k.actor("nell")["location"])
        self.assertEqual(3, self.begin(vid="v2", tid="t2", req="begin2")["domain_epoch"])
        self.clock.advance(60)
        self.k.tick()
        self.change("prepared", vid="v2", req="prepared2")
        self.change("active", vid="v2", req="active2")
        self.assertEqual(3, self.v.authority("nell")["domain_epoch"])

    def test_every_illegal_edge_rejected_without_mutation(self):
        self.begin()
        route = ("preparing", "prepared", "active", "returning", "settling", "settled", "closed")
        for current in route:
            with self.subTest(current=current):
                for target in TRANSITIONS:
                    if target in TRANSITIONS[current]:
                        continue
                    snap = self.snapshot()
                    self.reason("ILLEGAL_CITY_TRANSITION", lambda: self.v.transition(
                        "v1", target, "illegal:" + current + ":" + target, self.v.get("v1")["domain_epoch"]))
                    self.assertEqual(snap, self.snapshot())
                if current != "closed":
                    if current == "prepared":
                        self.clock.advance(60)
                        self.k.tick()
                    self.change(route[route.index(current) + 1])

    def test_aborted_state_is_terminal(self):
        self.begin()
        self.change("aborted")
        for state in TRANSITIONS:
            self.reason("ILLEGAL_CITY_TRANSITION", lambda: self.v.transition("v1", state, state + ":again", 1))

    def test_duplicate_begin_and_different_payload_conflict(self):
        plan = self.plan()
        first = self.v.begin("v1", "nell", "t1", "begin", plan)
        snapshot = self.snapshot()
        self.assertEqual(first, self.v.begin("v1", "nell", "t1", "begin", plan))
        self.assertEqual(snapshot, self.snapshot())
        self.reason("CITY_REQUEST_CONFLICT", lambda: self.v.begin("v1", "nell", "t1", "begin",
                                                                 dict(plan, destination="kazeminato:southern-terrace")))
        self.assertEqual(snapshot, self.snapshot())

    def test_duplicate_transfer_or_visit_is_conflict(self):
        self.begin()
        for vid, tid in (("v2", "t1"), ("v1", "t2")):
            snap = self.snapshot()
            self.reason("CITY_TRANSFER_CONFLICT", lambda: self.begin("elsa", vid, tid, "other"))
            self.assertEqual(snap, self.snapshot())

    def test_same_transfer_new_request_returns_original_even_after_progress(self):
        plan = self.plan()
        original = self.v.begin("v1", "nell", "t1", "begin", plan)
        self.clock.advance(60)
        self.k.tick()
        self.change("prepared")
        self.assertEqual(original, self.v.begin("v1", "nell", "t1", "alias", plan))
        self.assertEqual("prepared", self.v.get("v1")["state"])
        changed = dict(plan, destination="kazeminato:southern-terrace")
        self.reason("CITY_TRANSFER_CONFLICT", lambda: self.v.begin("v1", "nell", "t1", "alias2", changed))

    def test_nested_transaction_catching_failure_still_rolls_back_visit(self):
        def bad(db, _):
            db.execute("INSERT INTO runtime_meta VALUES('temporary-hold','1')")
            raise Refused("fake-custody-failure")
        self.v.before_departure.append(bad)
        snap = self.snapshot()
        with self.s.tx():
            self.reason("fake-custody-failure", self.begin)
            self.s.set_meta("outer-still-works", "1")
        self.assertEqual(snap, self.snapshot())
        self.assertIsNone(self.s.meta("temporary-hold"))
        self.assertEqual("1", self.s.meta("outer-still-works"))

    def test_old_writer_cannot_change_visit(self):
        self.begin()
        self.clock.advance(61)
        other = Store(self.path, self.clock, synthetic=True)
        other.acquire()
        try:
            snap = self.snapshot()
            with self.assertRaises(LockHeld):
                self.change("prepared")
            self.assertEqual(snap, self.snapshot())
        finally:
            other.release()
            other.close()
            self.s.acquire()

    def test_one_open_visit_per_actor_including_reconcile(self):
        self.begin()
        self.reason("ACTOR_OUT_IN_CITY", lambda: self.begin(vid="v2", tid="t2", req="other"))
        self.change("prepared")
        self.clock.advance(60)
        self.k.tick()
        self.change("active")
        for state in ("returning", "settling", "settled", "reconcile"):
            self.change(state)
        self.assertTrue(self.v.home_allowed("nell"))
        self.clock.advance(60)
        self.k.tick()
        self.reason("CITY_VISIT_ALREADY_OPEN", lambda: self.begin(vid="v2", tid="t2", req="other"))
        self.assertTrue(self.v.home_allowed("elsa"))

    def test_no_early_activation(self):
        self.begin()
        self.change("prepared")
        snap = self.snapshot()
        self.reason("CITY_DOOR_NOT_REACHED", lambda: self.change("active"))
        self.assertEqual(snap, self.snapshot())
        self.assertEqual(0, self.v.authority("nell")["domain_epoch"])

    def test_old_and_missing_home_epoch_and_city_epoch(self):
        self.depart()
        self.assertEqual("STALE_DOMAIN_EPOCH", self.command(epoch=0)["reason"])
        self.reason("STALE_DOMAIN_EPOCH", lambda: self.v.check_city("v1", 0))
        self.assertEqual("active", self.v.check_city("v1", 1)["state"])
        self.change("returning")
        self.reason("CITY_ACTIONS_DISABLED", lambda: self.v.check_city("v1", 1))
        self.change("settling")
        self.change("settled")
        self.change("closed")
        self.clock.advance(60)
        self.k.tick()
        self.reason("STALE_DOMAIN_EPOCH", lambda: self.v.check_city("v1", 1))
        self.assertEqual("STALE_DOMAIN_EPOCH", self.command("old", epoch=1)["reason"])
        self.assertEqual("STALE_DOMAIN_EPOCH", self.command("missing")["reason"])
        self.assertEqual("running", self.command("new", epoch=2)["status"])

    def test_stale_transition_epoch_is_rejected(self):
        self.depart()
        snap = self.snapshot()
        self.reason("STALE_DOMAIN_EPOCH", lambda: self.v.transition("v1", "returning", "stale", 0))
        self.assertEqual(snap, self.snapshot())

    def test_home_actions_blocked_all_nonterminal_states(self):
        self.begin()
        for state in ("preparing", "prepared", "active", "returning", "settling", "settled"):
            if state != "preparing":
                if state == "active":
                    self.clock.advance(60)
                    self.k.tick()
                self.change(state)
            for action, args in (("check", {}), ("move", {"to": "living"}), ("cook", {"recipe": "onigiri"})):
                result = self.command(state + action, epoch=self.v.authority("nell")["domain_epoch"],
                                      action=action, args=args)
                self.assertEqual("ACTOR_OUT_IN_CITY", result["reason"])

    def test_family_schedulers_skip_without_spending_model_budget(self):
        self.begin()
        self.life.model = lambda _: self.fail("home model must not run")
        before = self.s.db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0]
        self.assertEqual("degraded", self.life.think("nell", "free_time", gap=0))
        self.assertEqual("degraded", self.life.cog.begin("nell", "free_time"))
        self.assertEqual({}, Routine(self.k).check("nell"))
        self.assertEqual({}, self.life.meals.act("nell"))
        self.life.l0("nell")
        self.assertTrue(self.life.busy("nell"))
        self.life.put("head:2026-11-17:lunch", "nell")
        self.assertNotEqual("nell", self.life.head_cook("lunch", "2026-11-17"))
        self.assertEqual(before, self.s.db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0])

    def test_late_model_answer_cannot_act_after_departure_or_return(self):
        self.life.model = lambda _: dict(latency=120, steps=[dict(action_type="check")])
        self.assertEqual("asked", self.life.cog.begin("nell", "free"))
        self.begin()
        before = self.s.db.execute("SELECT count(*) FROM commands").fetchone()[0]
        self.clock.advance(120)
        self.k.tick()
        self.life.cog.run_due()
        self.assertEqual(before, self.s.db.execute("SELECT count(*) FROM commands").fetchone()[0])
        self.assertNotIn("nell", self.life.cog.plans)

    def test_saved_plan_cannot_resume_after_round_trip(self):
        self.life.model = lambda _: dict(steps=[dict(action_type="check", args={"resource_id": "tv"}),
                                               dict(action_type="check", args={"resource_id": "tv"})])
        self.life.cog.begin("nell", "free")
        self.life.cog.run_due()
        self.clock.advance(5)
        self.k.tick()
        self.settled()
        self.change("closed")
        self.clock.advance(60)
        self.k.tick()
        before = self.s.db.execute("SELECT count(*) FROM commands").fetchone()[0]
        # advance_plans has an independent supersession fence. Exercise the
        # saved-step entry itself so that removing its epoch check fails.
        self.assertIn('nell', self.life.cog.plans)
        result = self.life.cog._submit_step('nell', first=False)
        self.assertEqual('STALE_DOMAIN_EPOCH', result['reason'])
        self.assertEqual(before, self.s.db.execute("SELECT count(*) FROM commands").fetchone()[0])
        self.assertNotIn("nell", self.life.cog.plans)

    def test_abort_in_transit_and_at_door_no_epoch_handoff(self):
        self.begin()
        self.change("aborted")
        self.assertEqual("living", self.k.actor("nell")["location"])
        self.assertIsNone(self.k.actor("nell")["action_id"])
        self.assertEqual(0, self.v.authority("nell")["domain_epoch"])
        self.begin(vid="v2", tid="t2", req="begin2")
        self.clock.advance(60)
        self.k.tick()
        self.change("prepared", "v2", "prepared2")
        self.change("aborted", "v2", "abort2")
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("living", self.k.actor("nell")["location"])
        self.assertEqual(0, self.v.authority("nell")["domain_epoch"])

    def test_hook_failure_rolls_back_t1_and_t3(self):
        def bad(db, _):
            db.execute("INSERT INTO runtime_meta VALUES('fake-hold','1')")
            raise RuntimeError("injected")
        self.v.before_departure.append(bad)
        snap = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.begin()
        self.assertEqual(snap, self.snapshot())
        self.assertIsNone(self.s.meta("fake-hold"))
        self.v.before_departure.clear()
        self.settled()
        self.v.on_settle.append(bad)
        snap = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.change("closed")
        self.assertEqual(snap, self.snapshot())
        self.assertIsNone(self.s.meta("fake-hold"))
        self.assertEqual("city", self.v.authority("nell")["domain"])

    def test_settlement_hook_exactly_once_and_reconcile_stays_locked(self):
        self.settled()
        def hook(db, visit):
            self.assertEqual("set:v1", visit["settlement_id"])
            db.execute("INSERT INTO runtime_meta VALUES('settled-once','yes')")
        self.v.on_settle.append(hook)
        first = self.change("reconcile")
        snap = self.snapshot()
        again = self.v.transition("v1", "reconcile", "reconcile", 1)
        self.assertEqual(first, again)
        self.assertEqual(snap, self.snapshot())
        self.reason("ILLEGAL_CITY_TRANSITION", lambda: self.change("closed"))
        self.assertEqual(2, self.v.authority("nell")["domain_epoch"])
        self.assertEqual("yes", self.s.meta("settled-once"))

    def test_receipt_required_and_content_checks_atomic(self):
        self.begin()
        snap = self.snapshot()
        self.reason("CITY_RECEIPT_REQUIRED", lambda: self.v.transition("v1", "prepared", "prepare", 1))
        self.assertEqual(snap, self.snapshot())
        bad = self.receipt("prepared", "prepare", vid="wrong")
        self.reason("BAD_CITY_RECEIPT", lambda: self.v.transition("v1", "prepared", "prepare", 1, receipt=bad))
        self.assertEqual(snap, self.snapshot())
        self.change("prepared")
        self.assertEqual("r:prepared", self.v.get("v1")["receipt_ids"]["prepared"])
        self.assertEqual(1, self.s.db.execute("SELECT count(*) FROM city_visit_receipts").fetchone()[0])

    def test_settlement_requires_id(self):
        self.depart()
        self.change("returning")
        self.change("settling")
        snap = self.snapshot()
        self.reason("CITY_SETTLEMENT_ID_REQUIRED", lambda: self.v.transition("v1", "settled", "settled", 1))
        self.assertEqual(snap, self.snapshot())

    def test_restart_scans_preparing_without_network_or_unlock(self):
        self.begin()
        self.s.release()
        self.s.db.close()
        seen = []
        self.open(city_recovery=lambda row: seen.append((row["visit_id"], self.s._in_tx)))
        self.assertEqual([("v1", None)], seen)
        self.assertEqual("preparing", self.life.preparing_city_visits[0]["state"])
        self.assertFalse(self.v.home_allowed("nell"))
        def unavailable(_):
            raise ConnectionError("fake engine unavailable")
        self.reason("ACTOR_OUT_IN_CITY", lambda: self.v.check_home("nell"))
        with self.assertRaises(ConnectionError):
            self.v.recover_preparing(unavailable)
        self.assertFalse(self.v.home_allowed("nell"))

    def test_restart_replays_committed_t3_without_reapplying(self):
        self.settled()
        def fail():
            raise RuntimeError("lost acknowledgement")
        self.s.faults["city_transition:after_commit"] = fail
        with self.assertRaises(RuntimeError):
            self.change("closed")
        self.s.faults.clear()
        self.s.release()
        self.s.db.close()
        self.open()
        snap = self.snapshot()
        row = self.v.transition("v1", "closed", "closed", 1)
        self.assertEqual("closed", row["state"])
        self.assertEqual(snap, self.snapshot())
        self.assertEqual(2, self.v.authority("nell")["domain_epoch"])

    def test_t1_precommit_crash_no_partial_state(self):
        def fail():
            raise RuntimeError("crash")
        self.s.faults["city_begin:before_commit"] = fail
        snap = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.begin()
        self.s.faults.clear()
        self.assertEqual(snap, self.snapshot())

    def test_conflicting_plan_busy_and_same_life_day(self):
        with self.s.tx() as db:
            db.execute("INSERT INTO plans(plan_id,host,kind,start_at,end_at,members,state) VALUES"
                       "('p','nell','activity',?,?,?,'confirmed')",
                       (START, START + 1800, json.dumps(["nell"])))
        self.reason("CITY_CONFLICTING_PLAN", self.begin)
        with self.s.tx() as db:
            db.execute("DELETE FROM plans WHERE plan_id='p'")
        plan = dict(self.plan(), return_by_ms=int((START + 86400) * 1000))
        self.reason("CITY_RETURN_MUST_BE_SAME_LIFE_DAY", lambda: self.v.begin("v1", "nell", "t1", "begin", plan))
        self.command(action="move", args={"to": "kitchen"})
        self.reason("CITY_DEPARTURE_NOT_IDLE_AT_LIVING", self.begin)

    def test_other_actor_unaffected_and_regular_home_command_backwards_compatible(self):
        self.begin()
        self.assertEqual("running", self.command(actor="elsa")["status"])
        self.clock.advance(5)
        self.k.tick()
        other = self.begin("elsa", "v2", "t2", "other")
        self.assertEqual(1, other["domain_epoch"])
        self.assertFalse(self.v.home_allowed("elsa"))



    def test_cancel_cannot_stop_departure(self):
        visit = self.begin()
        snap = self.snapshot()
        result = self.k.cancel('nell', visit['departure_action_id'], 'channel:nell')
        self.assertEqual('ACTOR_OUT_IN_CITY', result.get('reason'))
        self.assertEqual(snap, self.snapshot())
        self.clock.advance(60)
        self.k.tick()
        self.change('prepared')
        self.assertEqual('active', self.change('active')['state'])

    def test_cancel_cannot_stop_return_move_after_closed(self):
        self.settled()
        self.change('closed')
        action = self.k.actor('nell')['action_id']
        self.assertIsNotNone(action)
        snap = self.snapshot()
        result = self.k.cancel('nell', action, 'channel:nell')
        self.assertEqual('CITY_DOOR_MOVE_REQUIRED', result.get('reason'))
        self.assertEqual(snap, self.snapshot())
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual('living', self.k.actor('nell')['location'])

    def test_direct_home_writes_blocked_while_active(self):
        materials.phone_receive(self.k, 'nell', 'synthetic-message', 'self:nell', 'synthetic body')
        guide = Guide(self.s, kernel=self.k)
        gid = guide.offer_now('dinner')
        self.depart()
        for label, fn in (
            ('phone', lambda: materials.read_message(self.k, 'nell', 'synthetic-message', 'channel:nell', domain_epoch=1)),
            ('guide', lambda: guide.respond('nell', gid, 'adopt', 'channel:nell', domain_epoch=1)),
            ('guide-store-only', lambda: Guide(self.s).respond('nell', gid, 'decline', 'channel:nell', domain_epoch=1)),
        ):
            with self.subTest(entry=label):
                tables = ('materials', 'scene_offers', 'decision_points', 'events')
                before = {t: list(map(tuple, self.s.db.execute('SELECT * FROM ' + t))) for t in tables}
                self.assertEqual('ACTOR_OUT_IN_CITY', fn().get('reason'))
                self.assertEqual(before, {t: list(map(tuple, self.s.db.execute('SELECT * FROM ' + t))) for t in tables})
        self.assertEqual('unread', self.s.db.execute("SELECT state FROM materials WHERE source_ref='synthetic-message'").fetchone()[0])

    def test_new_home_choice_cannot_replace_return_move(self):
        self.settled()
        self.change('closed')
        action = self.k.actor('nell')['action_id']
        result = self.command('interrupt-return', epoch=2, action='move', args={'to': 'kitchen'})
        self.assertEqual('CITY_DOOR_MOVE_REQUIRED', result.get('reason'))
        self.assertEqual(action, self.k.actor('nell')['action_id'])
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual('living', self.k.actor('nell')['location'])

    def test_direct_home_writes_reject_old_epoch_after_return(self):
        materials.phone_receive(self.k, 'nell', 'synthetic-message', 'self:nell', 'synthetic body')
        guide = Guide(self.s, kernel=self.k)
        gid = guide.offer_now('dinner')
        self.settled()
        self.change('closed')
        self.clock.advance(60)
        self.k.tick()
        for epoch in (None, 0):
            self.assertEqual('STALE_DOMAIN_EPOCH', materials.read_message(
                self.k, 'nell', 'synthetic-message', 'channel:nell', domain_epoch=epoch).get('reason'))
            self.assertEqual('STALE_DOMAIN_EPOCH', guide.respond(
                'nell', gid, 'adopt', 'channel:nell', domain_epoch=epoch).get('reason'))
        self.assertEqual('succeeded', materials.read_message(
            self.k, 'nell', 'synthetic-message', 'channel:nell', domain_epoch=2)['status'])
        self.assertEqual('succeeded', guide.respond('nell', gid, 'adopt', 'channel:nell', domain_epoch=2)['status'])

    def test_answer_started_before_departure_arrives_only_after_closed(self):
        self.life.model = lambda _: dict(latency=180, steps=[dict(action_type='say', args={'intent': 'synthetic'})])
        self.assertEqual('asked', self.life.cog.begin('nell', 'free'))
        self.settled()
        self.change('closed')
        self.clock.advance(120)
        self.k.tick()
        self.assertTrue(self.v.home_allowed('nell'))
        with patch.object(self.life.cog, 'express', side_effect=AssertionError('stale expression called')):
            self.assertEqual([], self.life.cog.run_due())
        self.assertNotIn('nell', self.life.cog.plans)

    def test_active_routine_and_busy_skip_with_real_need(self):
        self.depart()
        self.assertIsNone(self.k.actor('nell')['action_id'])
        self.assertTrue(self.life.busy('nell'))
        with self.s.tx() as db:
            db.execute("UPDATE actors SET body=json_set(body,'$.hunger',95) WHERE actor_id='nell'")
        before = self.s.db.total_changes
        self.assertEqual({}, Routine(self.k).check('nell'))
        self.assertEqual(before, self.s.db.total_changes)

    def test_meal_window_active_skips_without_reserving_or_deciding(self):
        self.clock.advance(2 * 3600)
        self.life.meals.sync()
        self.depart()
        self.assertEqual('pending', self.life.meals.current('nell')['status'])
        self.assertEqual({}, self.life.meals.act('nell'))
        self.assertNotIn('nell', self.life.eaters('lunch', '2026-11-17'))
        cmd = dict(actor_id='nell', action_type='cook', args={'recipe': 'omelette'}, idempotency_key='chosen-cook')
        self.assertFalse(self.life.meals.defer_cook(cmd, {'reason': 'resource_unavailable'}, 'scheduled_meal_lunch'))
        self.assertEqual(0, self.s.db.execute("SELECT count(*) FROM meal_preparations WHERE actor='nell'").fetchone()[0])

    def test_abort_requires_confirmed_receipt_before_releasing_home(self):
        self.begin()
        self.change('prepared')
        snap = self.snapshot()
        self.reason('CITY_RECEIPT_REQUIRED', lambda: self.v.transition('v1', 'aborted', 'abort', 1))
        self.assertEqual(snap, self.snapshot())
        self.assertFalse(self.v.home_allowed('nell'))
        self.assertEqual('aborted', self.change('aborted')['state'])

    def test_preparing_at_lunch_not_counted_as_home_eater(self):
        self.clock.advance(2 * 3600)
        self.life.meals.sync()
        self.assertIn('nell', self.life.eaters('lunch', '2026-11-17'))
        self.begin()
        self.assertEqual('living', self.k.actor('nell')['location'])
        self.assertNotIn('nell', self.life.eaters('lunch', '2026-11-17'))

    def test_active_dispatch_skips_invitation_handler(self):
        self.depart()
        with patch.object(self.life, '_answer_invitations') as answer, patch.object(self.life, 'l0'):
            self.life.step()
        self.assertNotIn('nell', [call.args[0] for call in answer.call_args_list])
        self.assertIn('elsa', [call.args[0] for call in answer.call_args_list])

if __name__ == "__main__":
    unittest.main()
