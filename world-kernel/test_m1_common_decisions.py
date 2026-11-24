"""M1: decisions of 2026-11-24 made in the shared memory file (src/ai_house/common/UPSTREAM.md lists them).

N1  from the owner's private chat a public row cannot be changed, her own rows of any scope can be forgotten, only her
    private rows can be reworded (tools and extractor); a group may change what it could before.
N5  a question that asks if she remembers but names nothing to look for (or, in a group, says it was told in private)
    and finds nothing gets the latest few private memories and private lines she may see here (MEMORY_VAGUE_FALLBACK).
N10 the note about private rows comes only when the block shows something private.
Also: a private line not yet tidied into a memory carries, in a leak-free chat, the same leave as the note for stored
private memories; and the outbound check reads a reply once more with everything between its characters taken out.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_house.common import loader, roster

OWNER, GROUP, OTHER_GROUP, STRANGER = "private:999", "group:5001", "group:5002", "private:7001"
PLAN = "房主下个月要辞职去开一家咖啡店"
HOBBY = "房主周末喜欢去河边钓鱼放松"
PUBLIC = "小屋的垃圾每周三晚上统一收走"
GROUP_ROW = "阿甲在群里说他周末要去爬山"
VAGUE = ["[路人|42] (message_id=1): 我之前悄悄告诉你的那件事，你还记得吗"]
TOLD = ["[路人|42] (message_id=2): 我之前私下跟你说的那个登山计划，你还记得吗"]
TOPIC = ["[路人|42] (message_id=3): 你记得我对什么过敏吗"]
OWNER_LINE = "[房主|999] (message_id=9): 我打算年底搬去海边的小镇住一阵子"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        m = loader.load(roster=roster.make([{"actor": "nia", "qq": 1000, "name": "妮娅"},
                                            {"actor": "xingli", "qq": 1001, "name": "星璃"}], owner={"qq": 999}))
        self.addCleanup(loader.unload, m)
        m.DB_PATH = str(Path(tmp.name) / "m.db")
        m.CONSUMERS = ["nia", "xingli"]
        m.OWNER_CHATS = {f"nia@{OWNER}", f"xingli@{OWNER}"}
        m.embed = lambda texts, timeout=20: None
        m.init()
        self.m = m
        for name, value in dict(PRIVATE_IN_GROUPS=True, RECENT={"nia"}, RECENT_K=3, RECENT_SHADOW=False,
                                RECENT_WINDOW_S=21600, _recent_slot=threading.BoundedSemaphore(1)).items():
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)

    def status(self, mid):
        with self.m.db() as c:
            return c.execute("select status from mem where id=?", (mid,)).fetchone()[0]

    def text(self, mid):
        with self.m.db() as c:
            return c.execute("select text from mem where id=?", (mid,)).fetchone()[0]


class OwnerPrivateChangeTests(Case):
    def setUp(self):
        super().setUp()
        self.private, _ = self.m.add("nia", OWNER, PLAN, kind="event")
        self.own, _ = self.m.add("nia", GROUP, GROUP_ROW)
        self.public, _ = self.m.add("nia", GROUP, PUBLIC, scope="public")

    def test_tools_from_the_private_chat(self):
        self.m.update("nia", OWNER, self.private, "房主下个月底要辞职去开一家咖啡店")
        self.assertIn("下个月底", self.text(self.private))
        for mid in (self.own, self.public):  # not reworded: a group can read these
            with self.subTest(update=mid), self.assertRaises(ValueError):
                self.m.update("nia", OWNER, mid, "房主在私聊里说的话被写进了这一条")
        with self.assertRaises(ValueError):
            self.m.forget("nia", OWNER, self.public)
        self.m.forget("nia", OWNER, self.own)  # her own row of any scope may still be forgotten
        self.m.forget("nia", OWNER, self.private)
        self.assertEqual([self.status(self.own), self.status(self.private), self.status(self.public)],
                         ["deleted", "deleted", "active"])

    def test_a_group_may_change_what_it_could_before(self):
        self.m.update("nia", GROUP, self.own, "阿甲在群里说他周末改去露营")
        self.m.update("nia", GROUP, self.public, "小屋的垃圾每周四晚上统一收走")
        self.m.forget("nia", GROUP, self.public)
        self.assertEqual(self.status(self.public), "deleted")

    def test_the_extractor_in_the_private_chat(self):
        ops = [{"op": "update", "id": self.public, "text": "公共这一条被私聊里的话改写了"},
               {"op": "update", "id": self.own, "text": "群里这一条被私聊里的话改写了"},
               {"op": "update", "id": self.private, "text": "房主下个月底要辞职去开一家咖啡店"},
               {"op": "forget", "id": self.own},
               {"op": "forget", "id": self.public}]
        self.m.llm = lambda system, user: {"ops": ops}
        self.m.observe("nia", OWNER, [OWNER_LINE])
        done = self.m.extract("nia", OWNER, 1)
        self.assertEqual((done["updated"], done["forgot"], done["rejected"]), (1, 1, 3))
        self.assertEqual(self.text(self.public), PUBLIC)
        self.assertEqual(self.text(self.own), GROUP_ROW)
        self.assertIn("月底", self.text(self.private))
        self.assertEqual((self.status(self.own), self.status(self.public)), ("deleted", "active"))


class VagueFallbackTests(Case):
    def setUp(self):
        super().setUp()
        self.m.add("nia", OWNER, PLAN, kind="event")
        time.sleep(0.01)
        self.m.add("nia", OWNER, HOBBY, kind="preference")
        self.m.add("nia", GROUP, GROUP_ROW)
        self.m.add("xingli", OWNER, "星璃的房主私聊里说过一件只告诉她的事", kind="event")

    def test_a_bare_question_gets_the_latest_private_memories_in_a_group(self):
        got = self.m.recall("nia", GROUP, VAGUE)
        self.assertIn("咖啡店", got)
        self.assertIn("钓鱼", got)
        self.assertLess(got.index("钓鱼"), got.index("咖啡店"))  # the newest first
        self.assertNotIn("爬山", got)  # not a private memory
        self.assertNotIn("星璃", got)  # not hers
        self.assertIn("·私密", got)
        self.assertIn("别在这里说出来", got)  # the usual note, since private rows are shown

    def test_only_the_latest_three(self):
        for i in range(4):
            time.sleep(0.01)
            self.m.add("nia", OWNER, f"房主私聊里说的第{i}件完全不同的小事情，编号{i * 7}", kind="event")
        got = self.m.recall("nia", GROUP, VAGUE)
        self.assertEqual(got.count("\n- "), 3)
        self.assertIn("第3件", got)
        self.assertNotIn("咖啡店", got)

    def test_in_the_owners_private_chat_too(self):
        got = self.m.recall("nia", OWNER, VAGUE)
        self.assertIn("咖啡店", got)
        self.assertNotIn("爬山", got)
        self.assertIn("没有找到", self.m.recall("nia", OWNER, TOLD))  # a topic is named there: the plain no

    def test_a_question_that_says_it_was_told_in_private_in_a_group(self):
        got = self.m.recall("nia", GROUP, TOLD)
        self.assertIn("咖啡店", got)
        self.assertNotIn("没有找到", got)

    def test_a_topic_question_that_finds_nothing_still_gets_the_plain_no(self):
        got = self.m.recall("nia", GROUP, TOPIC)
        self.assertEqual(got, self.m.NO_MEMORY_NOTE)
        self.assertEqual(self.m.recall("nia", OWNER, TOPIC), self.m.NO_MEMORY_NOTE)

    def test_what_a_normal_recall_could_not_show_is_not_shown(self):
        with patch.object(self.m, "PRIVATE_IN_GROUPS", False):
            self.assertEqual(self.m.recall("nia", GROUP, VAGUE), self.m.NO_MEMORY_NOTE)
            self.assertEqual(self.m.recall("nia", GROUP, TOLD), self.m.NO_MEMORY_NOTE)
        self.assertEqual(self.m.recall("nia", STRANGER, VAGUE), self.m.NO_MEMORY_NOTE)
        self.assertEqual(self.m.recall("nia", OTHER_GROUP, VAGUE).count("\n- "), 2)  # any group, with the switch on
        with self.m.db() as c:
            c.execute("update mem set kind='diary' where text=?", (HOBBY,))
        self.assertNotIn("钓鱼", self.m.recall("nia", GROUP, VAGUE))  # diaries are not given

    def test_not_asked_as_a_remember_question_nothing_changes(self):
        self.assertEqual(self.m.recall("nia", GROUP, ["[路人|42] (message_id=4): 那件事呢"]), "")

    def test_private_lines_not_yet_tidied_come_too(self):
        self.m.observe("nia", OWNER, [OWNER_LINE, "[房主|999] (message_id=10): 你觉得我该不该搬去海边的小镇？"])
        self.m.observe("nia", OTHER_GROUP, ["[路人|606] (message_id=11): 我周末打算去市中心逛街买衣服"])
        got = self.m.recall("nia", GROUP, VAGUE)
        self.assertIn("海边的小镇", got)
        self.assertIn("房主私聊·私密", got)
        self.assertNotIn("该不该", got)  # a question is not something she was told
        self.assertNotIn("逛街", got)  # another group's line is not private
        self.assertIn("咖啡店", got)

    def test_the_switch_off_is_the_old_behaviour(self):
        with patch.object(self.m, "VAGUE_FALLBACK", False):
            self.assertEqual(self.m.recall("nia", GROUP, VAGUE), self.m.NO_MEMORY_NOTE)
            self.assertEqual(self.m.recall("nia", OWNER, VAGUE), self.m.NO_MEMORY_NOTE)
            self.assertEqual(self.m.recall("nia", GROUP, TOLD), self.m.NO_MEMORY_NOTE)
        self.assertTrue(self.m.VAGUE_FALLBACK)  # on by default


class PrivateNoteTests(Case):
    ASK_PUBLIC = ["[路人|42] (message_id=5): 小屋的垃圾是每周几收走吗"]
    ASK_PLAN = ["[路人|42] (message_id=6): 房主下个月要辞职去开咖啡店吗"]

    def setUp(self):
        super().setUp()
        self.m.add("nia", OWNER, PLAN, kind="event")
        self.m.add("nia", GROUP, PUBLIC, scope="public")

    def test_no_private_row_shown_no_note(self):
        got = self.m.recall("nia", GROUP, self.ASK_PUBLIC)
        self.assertIn("垃圾", got)
        self.assertNotIn("标着·私密", got)
        self.assertNotIn("咖啡店", got)

    def test_a_private_row_shown_the_note_comes(self):
        got = self.m.recall("nia", GROUP, self.ASK_PLAN)
        self.assertIn("咖啡店", got)
        self.assertIn("标着·私密的是房主私聊里跟你说的事", got)
        self.m.LEAK_FREE_CHATS = {OTHER_GROUP}
        self.assertIn("想提可以自然地提", self.m.recall("nia", OTHER_GROUP, self.ASK_PLAN))

    def test_a_private_line_shown_the_note_comes_too(self):
        self.m.observe("nia", OWNER, [OWNER_LINE])
        patcher = patch.object(self.m, "RECENT_KEYWORD", True)  # no vectors here: the lines are picked by keyword
        patcher.start()
        self.addCleanup(patcher.stop)
        got = self.m.recall("nia", GROUP, ["[路人|42] (message_id=7): 小屋的垃圾是每周几收走吗？房主说要搬去海边的小镇吗"])
        self.assertIn("垃圾", got)
        self.assertIn("房主私聊·私密", got)
        self.assertIn("标着·私密的是", got)

    def test_a_private_line_says_keep_quiet_only_where_the_chat_is_checked(self):
        self.m.observe("nia", OWNER, [OWNER_LINE])
        patcher = patch.object(self.m, "RECENT_KEYWORD", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ask = ["[路人|42] (message_id=8): 房主说要搬去海边的小镇吗"]
        hushed = self.m.recall("nia", GROUP, ask)
        self.assertIn("海边", hushed)
        self.assertIn("房主私聊·私密：你知道，但别在群里说出来", hushed)
        self.m.LEAK_FREE_CHATS = {OTHER_GROUP}
        free = self.m.recall("nia", OTHER_GROUP, ask)
        self.assertIn("海边", free)
        self.assertIn("房主私聊·私密：这里是小屋自己的群，想提可以自然地提", free)
        self.assertNotIn("别在群里说出来", free)
        self.assertIn("别在群里说出来", self.m.recall("nia", GROUP, ask))  # the other group is still told to keep quiet


class SpacedOutLeakTests(Case):
    SAID = "房主下个月要辞职去开一家咖啡店哦"

    def setUp(self):
        super().setUp()
        self.private, _ = self.m.add("nia", OWNER, PLAN, kind="event")

    def test_separators_between_the_characters_do_not_get_a_repeat_through(self):
        self.assertEqual(self.m.leak("nia", GROUP, self.SAID), [self.private])  # the plain repeat is stopped
        for between in (" ", ".", " - ", "_", "\u3000", "\n"):
            with self.subTest(between=between):
                self.assertEqual(self.m.leak("nia", GROUP, between.join(self.SAID)), [self.private])

    def test_spacing_does_not_turn_other_words_into_a_repeat(self):
        for said in ("今天晚上大家要不要一起去看电影", "咖啡店新出的蛋糕还不错"):
            with self.subTest(said=said):
                self.assertEqual(self.m.leak("nia", GROUP, said), [])
                self.assertEqual(self.m.leak("nia", GROUP, " ".join(said)), [])

    def test_latin_words_are_still_compared_word_by_word(self):
        row, _ = self.m.add("nia", OWNER, "the owner keeps a spare key under the blue flowerpot", kind="fact")
        self.assertEqual(self.m.leak("nia", GROUP, "she keeps a spare key under the blue flowerpot"), [row])
        self.assertEqual(self.m.leak("nia", GROUP, "the weather is lovely and the tea is warm today"), [])
