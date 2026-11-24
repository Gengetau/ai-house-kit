"""M1: the behaviour changes made in the shared memory file itself (src/ai_house/common/UPSTREAM.md lists them).

Decided 2026-11-24: the outbound check runs in every group that is not exempt, whatever the two memory switches
say; "private memories in groups" no longer needs "unified memory"; the secret filter also knows what looks like a
key by itself; with the diary switch off a housemate's own diaries come to mind in every chat, unified or not.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_house.common import loader, roster

OWNER_QQ = 999
OWNER, GROUP, OTHER_GROUP, FREE, STRANGER = "private:999", "group:5001", "group:5002", "group:5003", "private:7001"
PLAN = "房主下个月要辞职去开一家咖啡店"
ASK = ["[路人|42] (message_id=1): 房主下个月要辞职去开咖啡店吗"]
DIARY = "今天和房主一起收拾了阳台，种下了新买的薄荷，晚上聊了很久的旅行计划。"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        m = loader.load(roster=roster.make([{"actor": "nia", "qq": 1000, "name": "妮娅"},
                                            {"actor": "xingli", "qq": 1001, "name": "星璃"}], owner={"qq": OWNER_QQ}))
        self.addCleanup(loader.unload, m)
        m.DB_PATH = str(Path(tmp.name) / "m.db")
        m.CONSUMERS = ["nia", "xingli"]
        m.OWNER_CHATS = {f"nia@{OWNER}", f"xingli@{OWNER}"}
        m.LEAK_FREE_CHATS = {FREE}
        m.embed = lambda texts, timeout=20: None
        m.init()
        self.m = m

    def switches(self, *, unified=False, private_in_groups=False, diary_private=True):
        p = patch.multiple(self.m, UNIFIED={"nia", "xingli"} if unified else set(),
                           PRIVATE_IN_GROUPS=private_in_groups, DIARY_PRIVATE=diary_private)
        p.start()
        self.addCleanup(p.stop)


class LeakCheckTests(Case):
    def test_every_group_that_is_not_exempt_is_checked_with_any_switches(self):
        self.m.add("nia", OWNER, PLAN, kind="event")
        for unified in (False, True):
            for pig in (False, True):
                with self.subTest(unified=unified, private_in_groups=pig), \
                        patch.multiple(self.m, UNIFIED={"nia"} if unified else set(), PRIVATE_IN_GROUPS=pig):
                    self.assertEqual(len(self.m.leak("nia", GROUP, PLAN)), 1)
                    self.assertEqual(len(self.m.leak("nia", STRANGER, PLAN)), 1)
                    self.assertEqual(self.m.leak("nia", FREE, PLAN), [])
                    self.assertEqual(self.m.leak("nia", OWNER, PLAN), [])
                    self.assertEqual(self.m.leak("nia", GROUP, "今天天气不错，出去走走"), [])
                    self.assertEqual(self.m.leak("xingli", GROUP, PLAN), [])  # another housemate's is not hers


class PrivateInGroupsTests(Case):
    def test_without_unified_a_group_gets_her_private_memories_and_not_another_groups(self):
        self.switches(private_in_groups=True)
        self.m.add("nia", OWNER, PLAN, kind="event")
        self.m.add("nia", OTHER_GROUP, "阿甲在另一个群里说他周末要去爬山", about="阿甲")
        got = self.m.recall("nia", GROUP, ASK)
        self.assertIn("咖啡店", got)
        self.assertIn("·私密", got)
        self.assertIn("别在这里说出来", got)
        self.assertIn("想提可以自然地提", self.m.recall("nia", FREE, ASK))
        self.assertEqual(self.m.recall("nia", GROUP, ["[路人|42] (message_id=2): 阿甲周末要去爬山吗"]), "")
        self.assertEqual(self.m.recall("nia", STRANGER, ASK), "")  # never in someone else's private chat
        self.assertEqual(self.m.recall("xingli", GROUP, ASK), "")

    def test_off_nothing_private_reaches_a_group_unified_or_not(self):
        self.m.add("nia", OWNER, PLAN, kind="event")
        for unified in (False, True):
            with self.subTest(unified=unified), patch.multiple(self.m, UNIFIED={"nia"} if unified else set(),
                                                               PRIVATE_IN_GROUPS=False):
                self.assertEqual(self.m.recall("nia", GROUP, ASK), "")

    def test_what_a_group_may_change_is_not_widened(self):
        self.switches(private_in_groups=True)
        mid, _ = self.m.add("nia", OWNER, PLAN, kind="event")
        with self.assertRaises(ValueError):
            self.m.forget("nia", GROUP, mid)


class SecretFilterTests(Case):
    def test_what_looks_like_a_key_is_refused_with_no_word_before_it(self):
        fake = "x" * 4 + "9" * 8  # made up here, so nothing key-shaped is written in this file
        for text in (f"记一下这个 sk-{fake}zzzz 以后用", f"网关的是 agw_{fake}", f"secret key: {fake}",
                     f"bridge-key：{fake}", f"token是{fake}"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.m.add("nia", GROUP, text)
        self.m.add("nia", GROUP, "阿甲说他新买了一把 sk-2 型号的滑板")  # short, not a key

    def test_a_diary_with_a_key_in_it_is_refused(self):
        with self.assertRaises(ValueError):
            self.m.diary("nia", GROUP, DIARY + "顺手记下 sk-" + "x" * 20)
        self.assertGreater(self.m.diary("nia", GROUP, DIARY + "房主说 token 换米饭的玩笑真好笑。"), 0)


class DiaryEverywhereTests(Case):
    def test_with_the_switch_off_her_own_diaries_come_to_mind_in_every_chat(self):
        for unified in (False, True):
            with self.subTest(unified=unified):
                with self.m.db() as c:
                    c.execute("delete from mem")
                with patch.multiple(self.m, UNIFIED={"nia", "xingli"} if unified else set(), DIARY_PRIVATE=False):
                    self.m.diary("nia", GROUP, DIARY)
                    for chat in (GROUP, OTHER_GROUP, OWNER, STRANGER):
                        self.assertTrue(self.m.search("nia", chat, "种下了新买的薄荷"), chat)
                        self.assertEqual(self.m.search("xingli", chat, "种下了新买的薄荷"), [], chat)
                with patch.multiple(self.m, UNIFIED={"nia", "xingli"} if unified else set(), DIARY_PRIVATE=True):
                    self.assertTrue(self.m.search("nia", OWNER, "种下了新买的薄荷"))  # the switch back on hides it again
                    for chat in (GROUP, OTHER_GROUP, STRANGER):
                        self.assertEqual(self.m.search("nia", chat, "种下了新买的薄荷"), [], chat)

    def test_a_diary_kept_private_stays_out_of_other_private_chats(self):
        self.switches(diary_private=False)
        self.m.diary("nia", OWNER, DIARY)  # written in the owner's private chat: private
        self.assertTrue(self.m.search("nia", OWNER, "种下了新买的薄荷"))
        for chat in (GROUP, STRANGER):
            self.assertEqual(self.m.search("nia", chat, "种下了新买的薄荷"), [], chat)
        with patch.object(self.m, "PRIVATE_IN_GROUPS", True):
            self.assertTrue(self.m.search("nia", GROUP, "种下了新买的薄荷"))
            self.assertEqual(self.m.search("nia", STRANGER, "种下了新买的薄荷"), [])


if __name__ == "__main__":
    unittest.main()
