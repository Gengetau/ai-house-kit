import copy
import unittest
from worldcatalog.catalog import load, guard_request
from worldcatalog.fixtures import fixture_vocabulary, PROFILE, VERSION


def fixture(**kw):
    return fixture_vocabulary(PROFILE, **dict(world_id="synthetic-kitchen-w7", synthetic=True,
                                             fake_channels=True, paid_calls=False, **kw))


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.v = fixture()
        self.actor = {"actor_id": "nell", "location": "kitchen"}
        self.msg = dict(message_id="dm1", recipient="nell", world_id=self.v.world_id,
                        synthetic=True, source="fake_mailbox")

    def test_profile_is_explicit_and_default_unchanged(self):
        self.assertEqual(self.v.VOCAB_VERSION, VERSION)
        self.assertEqual(self.v.validate(), [])
        base = load()
        self.assertEqual(len(base['p1a']['action_types']), 8)
        self.assertEqual(base['actions']['read_phone']['phase'], 'P2')
        self.assertTrue(guard_request(base, 'read_phone', {'message_id': 'dm1'}, self.actor))
        for kw in [dict(), dict(synthetic=True), dict(synthetic=True, fake_channels=True),
                   dict(synthetic=True, fake_channels=True, paid_calls=True)]:
            with self.assertRaises(ValueError):
                fixture_vocabulary(PROFILE, world_id='x', **kw)
        with self.assertRaises(ValueError):
            fixture_vocabulary('unknown', world_id='x', synthetic=True, fake_channels=True, paid_calls=False)

    def test_registered_durations_and_routes(self):
        self.assertEqual(self.v.ACTIONS['eat']['duration_rule'], {'fixed': 2})
        self.assertEqual(self.v.RECIPES['rice']['seconds'], 10)
        self.assertEqual(self.v.route_seconds('kitchen', 'living'), 5)
        self.assertEqual(self.v.route_seconds('kitchen', 'exit_rooms'), 17)
        self.assertEqual(self.v.next_hop('kitchen', 'exit_rooms'), 'living')
        self.assertEqual(self.v.ACTIONS['read_phone']['duration_rule'], {'fixed': 1})

    def test_phone_only_own_fake_mailbox_at_safe_attention(self):
        args = {'message_id': 'dm1'}
        self.assertEqual(self.v.guard('read_phone', args, self.actor, mailbox=[self.msg], attention_allowed=True), [])
        self.assertTrue(self.v.guard('read_phone', args, self.actor, mailbox=[self.msg]))
        for key, val in [('recipient', 'wanda'), ('world_id', 'live'), ('synthetic', False),
                         ('source', 'real_phone'), ('message_id', 'other')]:
            msg = dict(self.msg, **{key: val})
            self.assertTrue(self.v.guard('read_phone', args, self.actor, mailbox=[msg], attention_allowed=True))
        for args in [{'message_id': ''}, {'message_id': 1}, {'message_id': 'dm1', 'recipient': 'wanda'}]:
            self.assertTrue(self.v.guard('read_phone', args, self.actor, mailbox=[self.msg], attention_allowed=True))

    def test_food_guards_and_mutation_fail_closed(self):
        row = dict(resource_id='egg', item='egg', kind='food', qty=1, location='kitchen', holder=None)
        self.assertIn('item_not_ready_food', self.v.guard('eat', {'item': 'egg'}, self.actor, [row]))
        self.v.ACTIONS['eat']['duration_rule']['fixed'] = 0
        self.assertTrue(self.v.validate())
        self.assertEqual(self.v.guard('eat', {'item': 'egg'}, self.actor, [row]), ['fixture_modified'])

if __name__ == '__main__':
    unittest.main()
