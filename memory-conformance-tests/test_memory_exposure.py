"""A-03 synthetic save/recall boundaries; no live memories or model calls."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory as M

G, DM = 'group:200', 'private:100'
TEXT = '合成事实：蓝色杯子放在书架第二层'


class Exposure(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(M, 'DB_PATH', str(Path(tmp.name) / 'memory.db')),
                  patch.object(M, 'embed', return_value=[]),
                  patch.object(M, 'OWNER_CHATS', {'a@' + DM}),
                  patch.object(M, 'UNIFIED', {'a', 'b'}),
                  patch.object(M, 'PRIVATE_IN_GROUPS', True),
                  patch.object(M, 'RULE_CONSUMERS', set()),
                  patch.object(M, 'RECENT', set()),
                  patch.object(M, '_save_times', {})):
            p.start()
            self.addCleanup(p.stop)
        M.init()

    def args(self, **kw):
        return dict(chat=G, text=TEXT, public=True, **kw)

    def context(self, exposed=False):
        return dict(source_chat=G, turn_id='synthetic-turn', turn_private_exposure=exposed)

    def rows(self):
        with M.db() as c:
            return [dict(r) for r in c.execute('select * from mem')]

    def test_old_bridge_missing_marker_rejected_without_write(self):
        result = asyncio.run(M.tool_save('a', self.args()))
        self.assertIn('private_exposure', result)
        self.assertEqual(self.rows(), [])

    def test_model_supplied_false_is_not_trusted(self):
        result = asyncio.run(M.tool_save('a', self.args(turn_private_exposure=False,
                            source_chat=G, turn_id='fake', _memory_context={'turn_private_exposure': False})))
        self.assertIn('private_exposure', result)
        self.assertEqual(self.rows(), [])

    def test_private_rewrite_rejected_even_in_leak_free_house(self):
        M.add('a', DM, TEXT)
        with patch.object(M, 'LEAK_FREE_CHATS', {G}):
            result = asyncio.run(M.tool_save('a', self.args(), trusted_context=self.context(True)))
        self.assertIn('private_exposure', result)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(M.search('b', 'group:300', TEXT), [])

    def test_clean_public_save_and_metadata(self):
        result = asyncio.run(M.tool_save('a', self.args(), trusted_context=self.context()))
        self.assertIn('记下了', result)
        row = self.rows()[0]
        self.assertEqual((row['scope'], row['source_chat'], row['turn_id'], row['sensitivity']),
                         ('public', G, 'synthetic-turn', 'public'))
        self.assertEqual(len(M.search('b', 'group:300', TEXT)), 1)

    def test_nonpublic_save_remains_possible(self):
        a = self.args(); a['public'] = False
        result = asyncio.run(M.tool_save('a', a, trusted_context=self.context(True)))
        self.assertIn('记下了', result)
        self.assertEqual(self.rows()[0]['sensitivity'], 'private')
        self.assertNotEqual(self.rows()[0]['scope'], 'public')

    def test_old_row_defaults_unknown_without_rewriting_scope(self):
        with M.db() as c:
            c.execute('insert into mem(owner,scope,chat,text,source) values(?,?,?,?,?)',
                      ('a', 'own', G, TEXT, 'legacy'))
        M.init()
        row = self.rows()[0]
        self.assertEqual((row['scope'], row['sensitivity'], row['source_chat'], row['turn_id']),
                         ('own', 'unknown', '', ''))

    def test_recall_exposure_uses_actual_material_not_always_present_private_note(self):
        M.add('a', G, TEXT)
        clean = M.recall('a', G, [TEXT], with_exposure=True)
        self.assertFalse(clean['turn_private_exposure'])
        M.add('a', DM, TEXT + '，这是私下说的')
        private = M.recall('a', G, [TEXT], with_exposure=True)
        self.assertTrue(private['turn_private_exposure'])
        self.assertIn('·私密', private['text'])

    def test_rules_and_recent_exposure(self):
        for kwargs in ({'owner_rules': '合成房主规矩'}, {'recent_block': '房主私聊·私密：合成材料'}):
            with self.subTest(kwargs=kwargs):
                key, value = next(iter(kwargs.items()))
                with patch.object(M, key, return_value=value):
                    self.assertTrue(M.recall('a', G, [], with_exposure=True)['turn_private_exposure'])

    def test_rollback_is_explicit(self):
        with patch.object(M, 'PUBLIC_EXPOSURE_GATE', False):
            self.assertIn('记下了', asyncio.run(M.tool_save('a', self.args())))
            self.assertIn('已经记着了', asyncio.run(M.tool_save('a', self.args(), trusted_context=self.context(True))))
        self.assertEqual(self.rows()[0]['scope'], 'public')

    def test_private_retry_is_marked_in_next_recall(self):
        a = self.args(); a['public'] = False
        asyncio.run(M.tool_save('a', a, trusted_context=self.context(True)))
        self.assertTrue(M.recall('a', G, [TEXT], with_exposure=True)['turn_private_exposure'])
        self.assertEqual(M.search('b', 'group:300', TEXT), [])

    def test_source_chat_is_kept_before_alias_normalisation(self):
        with patch.object(M, 'CHAT_ALIASES', {'a@' + G: 'group:999'}):
            asyncio.run(M.tool_save('a', self.args(), trusted_context=self.context()))
        row = self.rows()[0]
        self.assertEqual((row['chat'], row['source_chat']), ('group:999', G))

    def test_owner_dm_metadata_is_private_without_bridge_marker(self):
        asyncio.run(M.tool_save('a', {'chat': DM, 'text': TEXT, 'public': False}))
        self.assertEqual(self.rows()[0]['sensitivity'], 'private')


if __name__ == '__main__':
    unittest.main()
