"""2026-10-25 owner: one memory per bot across chats (MEMORY_UNIFIED), owner-DM memories known-not-said in
groups (MEMORY_PRIVATE_IN_GROUPS, owner chose A), private diaries with earlier ones in the diary material.
Run: python3 -m unittest -v test_memory_unified.py   (temp database, no embeddings, no network)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import memory

OWNER_DM = "private:1000000259"
G, G2, BOT_DM = "group:100000037", "group:1000000296", "private:1000000074"
ASK = ["[路人|42] (message_id=1): 拉面好吃吗 猫咖去了吗"]


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(memory, "DB_PATH", str(Path(tmp.name) / "memory.db")),
                  patch.object(memory, "embed", return_value=[]),
                  patch.object(memory, "OWNER_CHATS", {f"mina@{OWNER_DM}", f"alma@{OWNER_DM}"}),
                  patch.object(memory, "UNIFIED", {"mina"}),
                  patch.object(memory, "PRIVATE_IN_GROUPS", True)):
            p.start()
            self.addCleanup(p.stop)
        memory.init()
        memory.add("mina", G, "路人说猫咖去了，很好玩", kind="event")
        memory.add("mina", OWNER_DM, "房主说拉面很好吃", kind="event")
        memory.add("alma", G, "阿尔玛记得猫咖去了", kind="event")

    def test_group_memory_comes_to_mind_in_another_group(self):
        block = memory.recall("mina", G2, ASK)
        self.assertIn("猫咖", block)
        self.assertNotIn("阿尔玛", block)  # another bot's own memory stays hers

    def test_owner_dm_memory_in_groups_known_not_said(self):
        block = memory.recall("mina", G, ASK)  # G2 is the 小屋 group: there it may be said (owner 10-25)
        self.assertIn("拉面", block)
        self.assertIn("·私密", block)
        self.assertIn("别在这里说出来", block)

    def test_bot_private_chats_never_see_owner_dm_memories(self):
        block = memory.recall("mina", BOT_DM, ASK)
        self.assertIn("猫咖", block)
        self.assertNotIn("拉面", block)

    def test_option_off_keeps_owner_dm_in_the_dm(self):
        with patch.object(memory, "PRIVATE_IN_GROUPS", False):
            block = memory.recall("mina", G2, ASK)
        self.assertIn("猫咖", block)
        self.assertNotIn("拉面", block)
        self.assertNotIn("别在这里说出来", block)

    def test_bots_outside_unified_keep_per_chat_memory(self):
        self.assertEqual(memory.recall("alma", G2, ASK), "")
        self.assertIn("猫咖", memory.recall("alma", G, ASK))

    def test_diaries_only_in_the_owner_dm(self):
        memory.diary("mina", G2, "今天其实有点吃醋，房主一直夸别人，我嘴上说没事。拉面猫咖都不想提。")
        for chat in (G, G2, BOT_DM):
            self.assertNotIn("吃醋", memory.recall("mina", chat, ASK), chat)
        with patch.object(memory, "asked_days", return_value={memory.diary_day(time.time())}):
            import asyncio
            got = asyncio.run(memory.tool_search("mina", {"chat": G2, "query": "今天的日记"}))
            self.assertNotIn("吃醋", got)
            got = asyncio.run(memory.tool_search("mina", {"chat": OWNER_DM, "query": "今天的日记"}))
            self.assertIn("吃醋", got)

    def test_diary_material_from_all_chats_with_earlier_diaries(self):
        old = memory.diary("mina", G2, "前天偷偷决定要给房主织一条围巾，还没告诉任何人，先记在这里。")
        with memory.db() as c:
            c.execute("update mem set created=created-2*86400 where id=?", (old,))
            now = time.time()
            c.executemany("insert into seen(consumer, chat, private, ts, line) values (?,?,?,?,?)", [
                ("mina", OWNER_DM, 1, now, "[米娜(你)|1000000185] (message_id=2): 房主晚安"),
                ("mina", G, 0, now, "[米娜(你)|1000000185] (message_id=3): 猫咖好玩"),
                ("alma", G, 0, now, "[阿尔玛(你)|1000000148] (message_id=4): 不是米娜的话")])
        text = memory.today("mina", G2)["text"]
        self.assertIn("【你前几天的日记】", text)
        self.assertIn("围巾", text)
        self.assertIn("[房主私聊]", text)
        self.assertIn("[群]", text)
        self.assertIn("拉面", text)  # today's memories from every chat
        self.assertNotIn("不是米娜的话", text)


if __name__ == "__main__":
    unittest.main()
