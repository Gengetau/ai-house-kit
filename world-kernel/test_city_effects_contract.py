"""Synthetic city-body/version contract tests; no real bridge or DB."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from worldkernel.city_effects import CityBodyEffects, body_delta
from worldkernel.kernel import Refused
from worldkernel.life import Life
from worldkernel.store import Store, VirtualClock, dumps

START = dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()


class CityEffects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'synthetic.db')
        self.clock = VirtualClock(START)
        self.open()
        with self.s.tx() as db:
            self.k._set_actor(db, 'nell', location='living')

    def open(self):
        self.s = Store(self.path, self.clock, synthetic=True, seed=239)
        self.s.acquire()
        self.life = Life(self.s, weather=lambda _: dict(rain=False, temp_c=20))
        self.k, self.v = self.life.k, self.life.city_visits
        self.effects = CityBodyEffects(self.k)

    def tearDown(self):
        self.s.release()
        self.s.db.close()
        self.tmp.cleanup()

    def plan(self):
        return dict(destination='rurigai:kissa-tomoshibi', start_at_ms=int(self.clock.now() * 1000),
                    return_by_ms=int((self.clock.now() + 3600) * 1000))

    def begin(self, **kw):
        return self.v.begin('v1', 'nell', 't1', 'begin', self.plan(), **kw)

    def change(self, state):
        kw = {}
        if state in ('prepared', 'active'):
            kw['receipt'] = dict(receipt_id='r:' + state, request_id=state, visit_id='v1', state=state,
                                 accepted_at_ms=int(self.clock.now() * 1000), payload={})
        if state == 'settled':
            kw['settlement_id'] = 'set:v1'
        return self.v.transition('v1', state, state, 1, **kw)

    def depart(self):
        self.begin()
        self.clock.advance(60)
        self.k.tick()
        self.change('prepared')
        self.change('active')
        self.body(energy=50, hunger=80)

    def body(self, **body):
        with self.s.tx() as db:
            self.k._set_actor(db, 'nell', body=dumps(body), body_at=self.clock.now())

    def read_body(self):
        return self.k.actor('nell')['body']

    def apply(self, name='snack', eid='v1:engine:1', **kw):
        return self.effects.apply('v1', eid, name, int(self.clock.now() * 1000), **kw)

    def refused(self, reason, fn):
        with self.assertRaises(Refused) as cm:
            fn()
        self.assertEqual(reason, cm.exception.reason)

    def snapshot(self):
        # All persisted facts, not just the body: no spurious event/job/freeze.
        return {row[0]: list(map(tuple, self.s.db.execute('SELECT * FROM ' + row[0])))
                for row in self.s.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}

    def test_versions_persist_and_prepare_carries_two(self):
        visit = self.begin(city_catalog_version='city-test/2')
        self.assertEqual(2, visit['mappings_version'])
        self.assertTrue(visit['vocab_version'].startswith('life1-'))
        self.assertEqual(self.k.V.VOCAB_VERSION, visit['vocab_version'])
        self.assertEqual('city-bridge/1', visit['protocol'])
        payload = self.v.prepare_payload('v1', 'prepare', carry={'yen': 1000, 'items': []}, source_hold_ref='visit:v1')
        digest = payload.pop('payload_hash')
        self.assertEqual(hashlib.sha256(dumps(payload).encode()).hexdigest(), digest)
        for key in ('mappings_version', 'vocab_version', 'protocol', 'city_catalog_version'):
            self.assertEqual(visit[key], payload[key])
        self.s.release()
        self.s.db.close()
        self.open()
        self.assertEqual(2, self.v.get('v1')['mappings_version'])

    def test_wrong_mapping_refused_before_holds_or_movement(self):
        def no_assets(db, visit):
            self.fail('asset hold/custody must not run')
        self.v.before_departure.append(no_assets)
        for version in (0, 1, 4, True, '2', '3'):
            snap = self.snapshot()
            self.refused('VERSION_MISMATCH', lambda: self.begin(mappings_version=version))
            self.assertEqual(snap, self.snapshot())

    def test_mappings_v3_persists_and_applies_body_effects(self):
        visit = self.begin(city_catalog_version='toto-p3.0', mappings_version=3)
        self.assertEqual(3, visit['mappings_version'])
        payload = self.v.prepare_payload('v1', 'prepare-v3', carry={'yen': 1000, 'items': []}, source_hold_ref='visit:v1')
        self.assertEqual(3, payload['mappings_version'])
        self.clock.advance(60)
        self.k.tick()
        self.change('prepared')
        self.change('active')
        self.body(energy=50, hunger=80)
        self.assertEqual('applied', self.apply('drink_coffee', eid='v1:v3:coffee')['status'])
        self.assertEqual({'energy': 50, 'hunger': 20}, self.read_body())

    def test_engine_version_mismatch_refused_before_t1(self):
        def fake_engine():
            return {'error': {'code': 'VERSION_MISMATCH'}}
        def fake_hold(db, visit):
            db.execute("INSERT INTO runtime_meta VALUES('would-freeze','1')")
        self.v.before_departure.append(fake_hold)
        snap = self.snapshot()
        self.refused('VERSION_MISMATCH', lambda: self.begin(prepare_response=fake_engine()))
        self.assertEqual(snap, self.snapshot())
        self.assertEqual('living', self.k.actor('nell')['location'])
        self.assertIsNone(self.k.actor('nell')['action_id'])

    def test_prepare_response_boundary_never_treats_other_errors_as_success(self):
        for value, reason in ((None, 'BAD_CITY_PREPARE_RESPONSE'), ({'code':'VERSION_MISMATCH'}, 'VERSION_MISMATCH'),
                              ({'error':{'code':'UNAVAILABLE'}}, 'CITY_PREPARE_REJECTED')):
            self.refused(reason, lambda: self.v.check_prepare_response(value))

    def test_existing_k1_rows_migrate_as_one_without_relabel(self):
        self.begin()
        self.s.release()
        self.s.db.close()
        con = sqlite3.connect(self.path)
        con.execute('ALTER TABLE city_visits DROP COLUMN mappings_version')
        con.commit()
        con.close()
        self.open()
        self.assertEqual(1, self.v.get('v1')['mappings_version'])
        self.assertFalse(self.v.home_allowed('nell'))
        payload = self.v.prepare_payload('v1', 'prepare', carry={}, source_hold_ref='visit:v1')
        self.assertEqual(1, payload['mappings_version'])

    def test_three_food_effects_match_current_eat_rule(self):
        self.depart()
        for name in ('snack', 'drink_coffee', 'drink_tea'):
            with self.subTest(name=name):
                self.body(energy=50, hunger=80)
                self.assertEqual('applied', self.apply(name, eid='v1:' + name)['status'])
                self.assertEqual({'energy':50, 'hunger':20}, self.read_body())

    def test_walk_is_activity_rate_only_not_base_drift(self):
        self.depart()
        self.clock.advance(15 * 60)
        self.k.tick()
        self.assertEqual({'energy':49.25, 'hunger':81.5}, self.read_body())
        self.apply('walk_15m')
        self.assertEqual({'energy':48.5, 'hunger':81.5}, self.read_body())

    def test_duplicate_id_does_not_repeat_or_emit(self):
        self.depart()
        first = self.apply()
        snap = self.snapshot()
        self.assertEqual(first, self.apply())
        self.assertEqual(snap, self.snapshot())
        row = self.s.db.execute('SELECT effect_key,event_id FROM effects WHERE effect_key=?', ('v1:engine:1',)).fetchone()
        self.assertEqual(('v1:engine:1', first['event_id']), tuple(row))

    def test_duplicate_survives_restart_and_lost_ack(self):
        self.depart()
        def lost():
            raise RuntimeError('lost acknowledgement')
        self.s.faults['city_body_effect:after_commit'] = lost
        with self.assertRaises(RuntimeError):
            self.apply()
        self.s.faults.clear()
        self.s.release()
        self.s.db.close()
        self.open()
        snap = self.snapshot()
        self.assertEqual('applied', self.apply()['status'])
        self.assertEqual(snap, self.snapshot())

    def test_same_id_changed_payload_conflicts(self):
        self.depart()
        self.apply()
        snap = self.snapshot()
        self.refused('CITY_EFFECT_CONFLICT', lambda: self.apply('drink_tea'))
        self.refused('CITY_EFFECT_CONFLICT', lambda: self.apply(quantity=2))
        self.assertEqual(snap, self.snapshot())

    def test_unknown_effect_rejected_audited_and_idempotent(self):
        self.depart()
        before = self.read_body()
        other = self.k.actor('elsa')
        result = self.apply('unknown-effect')
        self.assertEqual('UNKNOWN_CITY_EFFECT', result['reason'])
        self.assertEqual('rejected', result['status'])
        self.assertEqual(before, self.read_body())
        self.assertEqual(other, self.k.actor('elsa'))
        event = dict(self.s.db.execute('SELECT * FROM events WHERE event_id=?', (result['event_id'],)).fetchone())
        self.assertEqual('city_body_effect_rejected', event['kind'])
        self.assertEqual('self:nell', event['disclosure_scope'])
        self.assertEqual(0, self.s.db.execute("SELECT count(*) FROM effects WHERE effect_key='v1:engine:1'").fetchone()[0])
        snap = self.snapshot()
        self.assertEqual(result, self.apply('unknown-effect'))
        self.assertEqual(snap, self.snapshot())

    def test_clamped_servings_and_walk_blocks(self):
        self.depart()
        self.apply(quantity=2)
        self.assertEqual({'energy':50, 'hunger':0}, self.read_body())
        self.body(energy=0.5, hunger=100)
        self.apply('walk_15m', eid='v1:walk', quantity=2)
        self.assertEqual({'energy':0, 'hunger':100}, self.read_body())

    def test_inactive_or_closed_visit_does_not_change_body(self):
        self.begin()
        before = self.read_body()
        self.assertEqual('CITY_BODY_EFFECT_NOT_ACTIVE', self.apply()['reason'])
        self.assertEqual(before, self.read_body())
        self.clock.advance(60)
        self.k.tick()
        for state in ('prepared', 'active', 'returning', 'settling', 'settled', 'closed'):
            self.change(state)
        before = self.read_body()
        self.assertEqual('CITY_BODY_EFFECT_NOT_ACTIVE', self.apply(eid='v1:late')['reason'])
        self.assertEqual(before, self.read_body())

    def test_returning_effect_allowed_before_settlement(self):
        self.depart()
        self.change('returning')
        self.assertEqual('applied', self.apply('drink_tea')['status'])
        self.assertEqual(20, self.read_body()['hunger'])

    def test_effect_collision_with_existing_ledger_is_audited(self):
        self.depart()
        with self.s.tx():
            self.k._effect('v1:engine:1', 999)
        before = self.read_body()
        self.assertEqual('CITY_EFFECT_KEY_CONFLICT', self.apply()['reason'])
        self.assertEqual(before, self.read_body())

    def test_fault_rolls_back_body_effect_and_audit_then_retry(self):
        self.depart()
        def crash():
            raise RuntimeError('crash before commit')
        self.s.faults['city_body_effect:before_commit'] = crash
        snap = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply()
        self.s.faults.clear()
        self.assertEqual(snap, self.snapshot())
        self.assertEqual('applied', self.apply()['status'])

    def test_pending_jobs_do_not_consume_effect_before_tick(self):
        self.depart()
        with self.s.tx() as db:
            db.execute("INSERT INTO jobs VALUES('probe','probe','probe',?,'pending',?,NULL)",
                       (self.clock.now(), self.clock.now()))
        snap = self.snapshot()
        self.refused('CITY_BODY_JOBS_DUE', self.apply)
        self.assertEqual(snap, self.snapshot())
        with self.s.tx() as db:
            db.execute("DELETE FROM jobs WHERE job_id='probe'")
        self.assertEqual('applied', self.apply()['status'])

    def test_late_effect_integrates_now_without_rewinding_time(self):
        self.depart()
        occurred = int(self.clock.now() * 1000)
        self.clock.advance(60)
        self.effects.apply('v1', 'v1:late', 'drink_tea', occurred)
        self.assertAlmostEqual(20.1, self.read_body()['hunger'])
        self.assertEqual(self.clock.now(), self.k.actor('nell')['body_at'])

    def test_bad_quantity_time_and_unknown_visit_leave_no_facts(self):
        self.depart()
        snap = self.snapshot()
        for qty in (0, -1, True, 1.5):
            self.refused('BAD_CITY_EFFECT_QUANTITY', lambda: self.apply(quantity=qty))
        self.refused('BAD_CITY_EFFECT_TIME', lambda: self.effects.apply('v1', 'v1:e', 'snack', int((self.clock.now()+1)*1000)))
        self.refused('UNKNOWN_CITY_VISIT', lambda: self.effects.apply('missing', 'missing:e', 'snack', int(self.clock.now()*1000)))
        self.assertEqual(snap, self.snapshot())

    def test_old_mapping_visit_effect_does_not_change_body(self):
        self.depart()
        with self.s.tx() as db:
            db.execute("UPDATE city_visits SET mappings_version=1 WHERE visit_id='v1'")
        before = self.read_body()
        self.assertEqual('VERSION_MISMATCH', self.apply()['reason'])
        self.assertEqual(before, self.read_body())

    def test_life1_catalog_minor_upgrade_uses_its_current_rules(self):
        # K8 changes routes/version; names must not be pinned to life1-0.2.
        next_vocab = SimpleNamespace(VOCAB_VERSION='life1-next', BODY_KEYS=('energy', 'hunger'),
                                     ACTIONS=self.k.V.ACTIONS, ACTIVITIES=self.k.V.ACTIVITIES)
        self.assertEqual({'hunger': -60}, body_delta(next_vocab, 'drink_tea'))
        self.assertEqual({'energy': -0.75}, body_delta(next_vocab, 'walk_15m'))
        next_vocab.VOCAB_VERSION = 'world-catalog/p1a'
        self.refused('CITY_EFFECT_VOCAB_MISMATCH', lambda: body_delta(next_vocab, 'snack'))

    def test_inflight_visit_cannot_apply_under_different_vocab_version(self):
        self.depart()
        before = self.read_body()
        with patch.object(self.k.V, 'VOCAB_VERSION', 'life1-next'):
            self.assertEqual('CITY_EFFECT_VOCAB_MISMATCH', self.apply()['reason'])
        self.assertEqual(before, self.read_body())


if __name__ == '__main__':
    unittest.main()
