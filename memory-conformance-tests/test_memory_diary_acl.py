"""A-08: diary ACL is independent of UNIFIED, including legacy own/public rows.

Only synthetic data in a temporary database; no embedding or model calls.
"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import memory as M

G, OTHER, DM, PEER = 'group:200', 'group:300', 'private:100', 'private:400'
CANARY = '合成日记标记：在虚构房间拼好蓝色拼图，今天练习了一整晚。'


class DiaryACL(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(M, 'DB_PATH', str(Path(tmp.name) / 'memory.db')),
                  patch.object(M, 'embed', return_value=[]),
                  patch.object(M, 'OWNER_CHATS', {'a@' + DM, 'b@' + DM}),
                  patch.object(M, 'UNIFIED', set()),
                  patch.object(M, 'PRIVATE_IN_GROUPS', True),
                  patch.object(M, 'RULE_CONSUMERS', set()),
                  patch.object(M, 'RECENT', set())):
            p.start()
            self.addCleanup(p.stop)
        M.init()

    def legacy(self, scope='own', owner='a', age=0):
        with M.db() as c:
            return c.execute(
                'insert into mem(owner,scope,chat,kind,text,source,created,updated) '
                'values(?,?,?,?,?,?,?,?)',
                (owner, scope, G, 'diary', CANARY, 'diary', time.time()-age, time.time()-age)
            ).lastrowid

    def test_search_recall_and_tool_hide_legacy_diaries_in_all_non_owner_chats(self):
        for scope in ('own', 'private', 'public'):
            self.legacy(scope)
        for unified in (set(), {'a', 'b'}):
            with patch.object(M, 'UNIFIED', unified):
                for who in ('a', 'b'):
                    for chat in (G, OTHER, PEER):
                        with self.subTest(unified=bool(unified), who=who, chat=chat):
                            self.assertFalse(M.search(who, chat, CANARY, min_score=0))
                            self.assertNotIn(CANARY, M.recall(who, chat, [CANARY]))
                            self.assertNotIn(CANARY, asyncio.run(M.tool_search(
                                who, {'chat': chat, 'query': '今天的日记 蓝色拼图'})))

    def test_owner_dm_reads_only_her_own_diaries_even_if_legacy_public(self):
        mine = [self.legacy(scope) for scope in ('own', 'private', 'public')]
        theirs = self.legacy('public', owner='b')
        for unified in (set(), {'a', 'b'}):
            with patch.object(M, 'UNIFIED', unified):
                ids = {r['id'] for _, r in M.search('a', DM, CANARY, k=20)}
                self.assertEqual(ids, set(mine))
                self.assertNotIn(theirs, ids)
                self.assertIn(CANARY, asyncio.run(M.tool_search('a', {'chat': DM, 'query': '今天的日记'})))

    def test_group_forget_cannot_return_a_legacy_diary_body(self):
        for scope in ('own', 'public'):
            mid = self.legacy(scope)
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                asyncio.run(M.tool_forget('a', {'chat': G, 'id': mid}))

    def test_owner_cannot_forget_another_bots_public_legacy_diary(self):
        mid = self.legacy('public', owner='b')
        with self.assertRaises(ValueError):
            M.forget('a', DM, mid)

    def test_owner_can_forget_her_own_diary(self):
        mid = self.legacy()
        self.assertEqual(M.forget('a', DM, mid)['id'], mid)

    def test_new_diary_is_private_even_when_written_from_group(self):
        mid = M.diary('a', G, CANARY)
        with M.db() as c:
            self.assertEqual(c.execute('select scope from mem where id=?', (mid,)).fetchone()[0], 'private')

    def test_trusted_diary_material_keeps_previous_diaries_with_unified_off(self):
        self.legacy(age=3*86400)
        self.assertIn(CANARY, M.today('a', G)['text'])
        self.assertNotIn(CANARY, M.today('a', G, public=True)['text'])
        self.assertNotIn(CANARY, M.today('b', G)['text'])

    def test_ordinary_private_recall_policy_and_public_facts_are_preserved(self):
        private, _ = M.add('a', DM, '合成事实：喜爱薄荷色的杯子')
        public, _ = M.add('a', G, '合成事实：周末整理公共书架', scope='public')
        with patch.object(M, 'UNIFIED', {'a', 'b'}):
            a = {r['id'] for _, r in M.search('a', OTHER, '合成事实', k=20)}
            b = {r['id'] for _, r in M.search('b', OTHER, '合成事实', k=20)}
            self.assertIn(private, a)
            self.assertNotIn(private, b)
            self.assertIn(public, a & b)

    def test_init_and_reads_do_not_rewrite_existing_scope(self):
        mid = self.legacy()
        M.init()
        M.search('a', G, CANARY)
        with M.db() as c:
            self.assertEqual(c.execute('select scope from mem where id=?', (mid,)).fetchone()[0], 'own')

    def test_explicit_rollback_restores_legacy_acl_without_rewriting_rows(self):
        mid = self.legacy()
        with patch.object(M, 'DIARY_PRIVATE', False):
            self.assertIn(mid, {r['id'] for _, r in M.search('a', G, CANARY)})
            # Changed from the private side's copy, which says assertFalse for 'a' here and fails there as well:
            # since 2026-11-03 (_visible_sql) a unified bot recalls her OWN diaries everywhere with the switch
            # off, never another's.
            with patch.object(M, 'UNIFIED', {'a', 'b'}):
                self.assertIn(mid, {r['id'] for _, r in M.search('a', G, CANARY)})
                self.assertFalse(M.search('b', G, CANARY))


if __name__ == '__main__':
    unittest.main()
