"""FB261020-01 (2026-10-20): a bot reads back her own diary through memory_search, by the day she names, whole.
Run: python3 -m unittest -v test_memory_diary.py   (temp database, no embeddings, no network)."""

import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory

G = "group:1000000296"


def ts(day, hh, mm=0):
    return datetime.datetime(2026, 10, day, hh, mm, tzinfo=memory.JST).timestamp()


class AskedDaysTests(unittest.TestCase):
    now = ts(20, 15)

    def days(self, q):
        return sorted(d.isoformat() for d in memory.asked_days(q, self.now))

    def test_forms(self):
        self.assertEqual(self.days("17号日记 42"), ["2026-10-17"])
        self.assertEqual(self.days("10月18日写了什么"), ["2026-10-18"])
        self.assertEqual(self.days("10-03 的日记"), ["2026-10-03"])
        self.assertEqual(self.days("2025-12-24"), ["2025-12-24"])
        self.assertEqual(self.days("昨天的日记"), ["2026-10-19"])
        self.assertEqual(self.days("25号"), ["2026-09-25"])  # not yet this month: last month
        self.assertEqual(self.days("12月1日"), ["2025-12-01"])
        self.assertEqual(self.days("31号"), ["2026-08-31"])  # September has no 31st

    def test_plain_numbers_are_not_days(self):
        self.assertEqual(self.days("日记里的 42 是什么"), [])
        self.assertEqual(self.days("找一下 1000000259"), [])


class DiarySearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(memory, "DB_PATH", str(Path(tmp.name) / "memory.db")),
                  patch.object(memory, "OWNER_CHATS", {"alma@private:100", "nell@private:100"}),
                  patch.object(memory, "embed", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        memory.init()
        self.long = "今天过得挺满。" * 30 + "接着聊灵魂和思维，我懒得绕，回了句42。" + "晚安。" * 10
        self.add("alma", ts(17, 21, 40), "十七号这天下了雨，大家在客厅看电视，我坐地上打游戏，玩到很晚才睡。")
        self.add("alma", ts(18, 21, 44), self.long)
        self.add("alma", ts(20, 1, 10), "过了零点才写完的日记，算十九号的，今天小由1又啃小饼干。")
        self.add("nell", ts(18, 22, 0), "奈尔的日记，十八号读完了一本诗集，泡了大吉岭红茶，很安静。")

    def add(self, owner, created, text):
        with memory.db() as c:
            c.execute("insert into mem(owner, scope, chat, about, kind, text, source, created, updated) "
                      "values (?,?,?,?,?,?,?,?,?)", (owner, "own", G, "", "diary", text, "diary", created, created))

    def search(self, query, consumer="alma", now=ts(20, 15)):
        with patch("time.time", return_value=now):
            # A-08: date/full-text diary search is available in her owner DM.
            return asyncio.run(memory.tool_search(consumer, {"query": query, "chat": "private:100"}))

    def test_the_day_asked_is_shown_whole(self):
        got = self.search("18号日记 42")
        self.assertIn("回了句42", got)
        self.assertIn("（10-18）", got)
        self.assertNotIn("奈尔的日记", got)  # only her own

    def test_after_midnight_belongs_to_the_evening_before(self):
        self.assertIn("过了零点才写完", self.search("昨天的日记"))

    def test_no_diary_that_day_says_so(self):
        got = self.search("15号日记")
        self.assertTrue(got.startswith("（10-15 没有你写的日记）"), got)

    def test_without_a_day_diaries_still_short_unless_asked(self):
        got = self.search("灵魂和思维 今天过得挺满")
        self.assertNotIn("回了句42", got)  # an ordinary search keeps a diary to its first 100 chars
        self.assertIn("回了句42", self.search("日记 灵魂和思维 今天过得挺满"))
