"""authenticated explicit deliveries, bounded full lists and exact counts."""
import json
import os
import tempfile
import unittest

from worldkernel import inventory
from worldkernel.store import Store, VirtualClock
from worldkernel.life import Life
from worldkernel.life_model import LiveLifeModel
from test_life_outings import Base


class Intake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(os.path.join(self.tmp.name, 'kernel.db'), VirtualClock(1794000000),
                       world_id='food-test', synthetic=True, holder='test')
        self.s.acquire()
        self.l = Life(self.s)

    def tearDown(self):
        self.s.release()
        self.tmp.cleanup()

    def give(self, text, mid='1', principal='cmd:owner'):
        return inventory.receive_food(self.l.k, principal, 'group:test', mid, text)

    def resources(self):
        with self.s.read() as db:
            return [dict(r) for r in db.execute("SELECT * FROM resources WHERE source LIKE 'food_delivery:%'")]

    def test_requires_owner_and_explicit_quantity(self):
        self.assertEqual(self.give('薯片 ×6', principal='routine')['reason'], 'owner_only')
        r = self.give('薯片\n巧克力 ×0\n牛奶 ×1.5\n糖果 ×1000')
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(self.resources(), [])

    def test_full_list_and_unknown_preserve_counts_and_provenance(self):
        names = [('特别口味%d' % i, i % 7 + 1) for i in range(75)]
        text = '\n'.join('%d. %s ×%d' % (i + 1, n, q) for i, (n, q) in enumerate(names))
        self.assertGreater(len(text), 400)
        r = self.give(text)
        self.assertEqual((r['rows'], r['qty'], r['unmatched']), (75, sum(q for n, q in names), 75))
        rows = self.resources()
        self.assertEqual(len(rows), 75)
        for row, (name, qty) in zip(rows, names):
            src = json.loads(row['source'].split(':', 1)[1])
            self.assertEqual((row['item'], row['qty'], src['name']), ('other_snack', qty, name))
            self.assertEqual((src['giver'], src['channel'], src['message_id']), ('owner', 'group:test', '1'))
            with self.s.read() as db:
                self.assertIsNotNone(db.execute('SELECT 1 FROM events WHERE event_id=?', (src['event_id'],)).fetchone())

    def test_receipt_dedup_and_conflict_survive_restart(self):
        first = self.give('湖池屋海苔盐薯片 ×6\n草莓百奇 ×8')
        self.assertEqual(self.give('湖池屋海苔盐薯片 ×6\n草莓百奇 ×8'), first)
        self.assertEqual(self.give('薯片 ×7')['reason'], 'delivery_conflict')
        self.s.release()
        self.s.acquire()
        self.assertEqual(self.give('湖池屋海苔盐薯片 ×6\n草莓百奇 ×8'), first)
        self.assertEqual(sum(r['qty'] for r in self.resources()), 14)

    def test_limits_and_rejected_lines_are_counted(self):
        r = self.give('\n'.join('薯片 ×2' for _ in range(300)))
        self.assertEqual(r['rows'], inventory.MAX_DELIVERY_LINES)
        self.assertEqual(r['truncated_lines'], 300 - inventory.MAX_DELIVERY_LINES)
        r = self.give('薯片 ×2\n' + 'x' * (inventory.MAX_DELIVERY_CHARS + 8), mid='2')
        self.assertGreater(r['truncated_chars'], 0)
        self.assertEqual(r['rows'], 1)
        self.assertGreaterEqual(r['rejected_lines'], 1)

    def test_markdown_and_units(self):
        r = self.give('零食送到了\n- 草莓百奇（つぶつぶいちご） ×8盒\n| 湖池屋薯片 | 6 | 袋 |')
        self.assertEqual((r['rows'], r['qty'], r['rejected_lines']), (2, 14, 1))
        self.assertEqual({r['item'] for r in self.resources()}, {'chips', 'chocolate'})

    def test_receipt_and_stock_roll_back_together(self):
        emit = self.l.k._emit
        def fail(*a, **kw):
            raise RuntimeError('fault')
        self.l.k._emit = fail
        with self.assertRaises(RuntimeError):
            self.give('薯片 ×6')
        self.assertEqual(self.resources(), [])
        self.l.k._emit = emit
        self.assertEqual(self.give('薯片 ×6')['qty'], 6)

    def test_console_queue_drains_and_ack_loss_does_not_duplicate(self):
        path = os.path.join(self.tmp.name, 'legacy.db')
        args = ('cmd:owner', 'console', 'delivery-1', '薯片 ×6\n原味酸奶 ×3')
        q = inventory.queue_food(path, *args)
        self.assertEqual(q['status'], 'queued')
        self.assertEqual(inventory.queue_food(path, *args), q)
        inventory.process_food_queue(self.l.k, path)
        self.assertEqual(sum(r['qty'] for r in self.resources()), 9)
        import sqlite3
        with sqlite3.connect(path) as c:
            self.assertEqual(json.loads(c.execute('SELECT receipt FROM food_input_queue').fetchone()[0])['qty'], 9)
            c.execute("UPDATE food_input_queue SET state='queued'")  # kernel committed, queue ack was lost
        inventory.process_food_queue(self.l.k, path)
        self.assertEqual(sum(r['qty'] for r in self.resources()), 9)

    def test_prompt_groups_same_kind_without_losing_usable_resource(self):
        self.give('\n'.join('特别口味%d ×2' % i for i in range(75)))
        with self.s.tx() as db:
            db.execute("UPDATE actors SET location='kitchen' WHERE actor_id='nell'")
        ctx = self.l.cog.context('nell')
        ctx['life'] = {'now': '2026-11-07 10:00'}
        prompts = []
        model = LiveLifeModel(lambda system, user: (prompts.append(user) or '{"steps": []}', 0), {})
        model(ctx)
        seen = json.loads(prompts[0].split('你现在的情况：\n', 1)[1].split('\n让你想一想的原因：', 1)[0])
        rows = [r for r in seen['items_here'] if r['item'] == 'other_snack']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['qty'], rows[0]['variants']), (150, 75))
        self.assertIsNotNone(self.l.k.resource(rows[0]['resource_id']))


class NamesAndMigration(unittest.TestCase):
    def test_no_ingredient_substring_guess(self):
        for name in ('杏仁豆腐', '牛肉干', '鲷鱼烧', '炒面面包'):
            self.assertEqual(inventory.category(name), ('other_snack', False), name)
        self.assertEqual(inventory.category('冰红茶'), ('bottled_tea', True))
        self.assertEqual(inventory.category('鸡蛋'), ('egg', True))

    def test_only_explicit_gift_quantity_added_above_staples(self):
        from worldkernel import vocab_life1 as v
        from worldkernel.life import STOCK, ROSTER
        rows = [{'id': 1, 'name': '鸡蛋', 'qty': 20, 'kind': 'food', 'by': 'owner', 'owner': '',
                 'owner_events': [], 'owner_qty': 0},
                {'id': 2, 'name': '牛奶', 'qty': 8, 'kind': 'food', 'by': 'owner', 'owner': '',
                 'owner_events': [3], 'owner_qty': 2}]
        resources, report = inventory.migrate(rows, STOCK, v, ROSTER)
        self.assertEqual([(r['item'], r['qty']) for r in resources], [('milk', 2)])
        self.assertEqual(report['owner_qty'], 2)


class Purchase(Base):
    def test_name_mapping_count_cost_source_and_replay(self):
        with self.s.tx() as db:
            db.execute("UPDATE actors SET location='place:supermarket' WHERE actor_id='nell'")
        args = dict(kind='ingredient', item='湖池屋海苔盐薯片', qty=6)
        before = self.o.wallet.available('nell')
        r = self.command('nell', 'buy', args, key='snack-purchase')
        self.assertEqual(r['status'], 'succeeded', r)
        replay = self.command('nell', 'buy', args, key='snack-purchase')
        self.assertEqual((replay['action_id'], replay['event_id']), (r['action_id'], r['event_id']))
        self.assertTrue(replay['replayed'])
        row = self.l.k.resource('bought:' + r['action_id'])
        self.assertEqual((row['item'], row['qty']), ('chips', 6))
        self.assertEqual(before - self.o.wallet.available('nell'), 6 * self.l.k.V.INGREDIENTS['chips']['price_yen'])
        with self.s.read() as db:
            event = json.loads(db.execute('SELECT payload FROM events WHERE event_id=?', (r['event_id'],)).fetchone()[0])
        self.assertEqual((event['name'], event['qty']), ('湖池屋海苔盐薯片', 6))

    def test_missing_quantity_and_unknown_unpriced_food_refused(self):
        with self.s.tx() as db:
            db.execute("UPDATE actors SET location='place:supermarket' WHERE actor_id='nell'")
        for args in (dict(kind='ingredient', item='chips'), dict(kind='ingredient', item='神秘零食', qty=3),
                     dict(kind='ingredient', item='other_snack', qty=3), dict(kind='ingredient', item='chips', qty=True)):
            r = self.command('nell', 'buy', args)
            self.assertEqual(r['status'], 'failed', r)


if __name__ == '__main__':
    unittest.main()
