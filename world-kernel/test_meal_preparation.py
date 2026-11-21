"""synthetic contention, recovery and bounded meal preparation."""
import json
import unittest
from unittest.mock import patch

import test_shadow_meals as fixture


class MealPreparationTests(unittest.TestCase):
    setUp = fixture.ShadowMealIntegration.setUp
    digest = fixture.ShadowMealIntegration.digest
    make = fixture.ShadowMealIntegration.make
    def cooks(self):
        self.calls = []
        def chat(system, user):
            self.calls.append(1)
            return json.dumps({'steps': [{'action_type': 'cook',
                                          'args': {'recipe': 'omelette'}}]}), 10
        sh = self.make(chat)
        with sh.store.tx() as c:
            c.execute("UPDATE actors SET location='kitchen', activity='idle', body=?",
                      (json.dumps({'hunger': 20, 'energy': 90}),))
        return sh, chat

    def run_minutes(self, sh, minutes):
        for _ in range(minutes * 2):
            sh.step()
            self.clock.advance(30)

    def test_six_chosen_cooks_serve_six_without_more_model_calls(self):
        sh, _ = self.cooks()
        self.run_minutes(sh, 85)
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM meal_attendance WHERE slot='lunch' AND status='eaten'").fetchone()[0], 6)
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='succeeded'").fetchone()[0], 6)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(sh.k.resource('egg')['qty'], 0)
        self.assertEqual(self.digest(), self.before)
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='eat' AND state='succeeded'").fetchone()[0], 6)

    def test_waiting_survives_restart_without_duplicate_cooks_or_calls(self):
        sh, chat = self.cooks()
        self.run_minutes(sh, 2)
        sh.store.close(); sh.samples.close()
        sh = self.make(chat)
        self.run_minutes(sh, 83)
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM meal_attendance WHERE slot='lunch' AND status='eaten'").fetchone()[0], 6)
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='succeeded'").fetchone()[0], 6)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(sh.k.resource('egg')['qty'], 0)

    def test_decline_or_intervening_action_cancels_wait(self):
        sh, _ = self.cooks()
        self.run_minutes(sh, 2)
        sh.meals.respond('wanda', 'skip')
        result = sh.k.submit({'idempotency_key': 'own-new-choice', 'principal': 'channel:yuki',
                              'actor_id': 'yuki', 'origin': 'self', 'action_type': 'wait',
                              'args': {'seconds': 60}})
        self.assertEqual(result['status'], 'running')
        self.run_minutes(sh, 83)
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='succeeded' AND actor_id IN ('wanda','yuki')").fetchone()[0], 0)
        self.assertEqual(sh.meals.current('wanda')['status'], 'declined')
        self.assertEqual(len(self.calls), 6)

    def test_wait_expires_without_starting_after_meal_window(self):
        sh, _ = self.cooks()
        self.run_minutes(sh, 2)
        # Finish the first cook after an outage, but no new decision is backfilled.
        self.clock.advance(2 * 3600)
        sh.step()
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='succeeded'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='running'").fetchone()[0], 0)

    def test_no_ingredient_means_no_fabricated_food_or_retry_storm(self):
        sh, _ = self.cooks()
        self.run_minutes(sh, 2)
        from worldkernel import admin
        admin.correction(sh.k, 'admin:world', 'egg', 2, 'synthetic stock shortage')
        self.run_minutes(sh, 83)
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='succeeded'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM meal_attendance WHERE slot='lunch' AND status='eaten'").fetchone()[0], 1)
        self.assertEqual(len(self.calls), 6)

    def test_pending_model_answer_is_not_invalidated_by_l0(self):
        sh, _ = self.cooks()
        # High hunger would otherwise make L0 eat a snack after asking, changing generation.
        with sh.store.tx() as c:
            c.execute("UPDATE actors SET body=?", (json.dumps({'hunger': 90, 'energy': 90}),))
        sh.step()
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='eat'").fetchone()[0], 0)
        self.clock.advance(30); sh.step()
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM decision_points WHERE reason='refused:stale_generation'").fetchone()[0], 0)

    def test_reserved_last_serving_is_not_offered_to_preparation_model(self):
        sh, _ = self.cooks()
        with sh.store.tx() as c:
            c.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) "
                      "VALUES('last-serving','food','omelette','kitchen',1,'synthetic')")
        sh.meals.sync()
        self.assertEqual(sh.meals.act('nell')['result']['status'], 'running')
        ctx = sh.cog.context('wanda')
        self.assertTrue(any(i['resource_id'] == 'last-serving' for i in ctx['items_here']))
        with patch.object(sh, 'model', return_value={'steps': []}) as model:
            sh._ask(ctx)
        offered = model.call_args.args[0]['items_here']
        self.assertFalse(any(i['resource_id'] == 'last-serving' for i in offered))
        self.assertTrue(any(i['item'] == 'egg' for i in offered))

    def test_crash_after_retry_submit_does_not_cook_twice(self):
        sh, chat = self.cooks()
        self.run_minutes(sh, 2)
        submit = sh.k.submit
        def crash(cmd):
            result = submit(cmd)
            if cmd['idempotency_key'].endswith(':meal-resource-retry'):
                raise RuntimeError('synthetic crash after commit')
            return result
        with patch.object(sh.k, 'submit', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'after commit'):
                self.run_minutes(sh, 12)
        sh.store.close(); sh.samples.close()
        sh = self.make(chat)
        self.run_minutes(sh, 70)
        with sh.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM actions WHERE action_type='cook' AND state='succeeded'").fetchone()[0], 6)
            self.assertEqual(c.execute("SELECT count(*) FROM meal_attendance WHERE slot='lunch' AND status='eaten'").fetchone()[0], 6)
        self.assertEqual(len(self.calls), 6)

