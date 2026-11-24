"""#68 (owner 2026-10-25): a batch the memory model refuses outright no longer holds a chat's queue, and a group
message is checked against her owner-DM memories before it goes (the 小屋 group is not checked).
Run: python3 -m unittest -v test_memory_guard.py   (temp database, no network)."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import memory

OWNER_DM, G, HOUSE = "private:1000000259", "group:100000037", "group:1000000296"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(memory, "DB_PATH", str(Path(tmp.name) / "memory.db")),
                  patch.object(memory, "embed", return_value=[]),
                  patch.object(memory, "OWNER_CHATS", {f"elsa@{OWNER_DM}"}),
                  patch.object(memory, "UNIFIED", {"elsa"}),
                  patch.object(memory, "PRIVATE_IN_GROUPS", True),
                  patch.object(memory, "_related", return_value="（还没有）"),
                  patch.object(memory, "INTENTS", None)):
            p.start()
            self.addCleanup(p.stop)
        memory.init()


class RefusedTests(Base):
    def lines(self, n):
        with memory.db() as c:
            for i in range(n):
                c.execute("insert into seen(consumer,chat,private,ts,line,done) values(?,?,?,?,?,0)",
                          ("elsa", OWNER_DM, 1, time.time(), f"[房主|1] (message_id={i}): 第{i}句"))

    def pending(self):
        with memory.db() as c:
            return [r[0] for r in c.execute("select line from seen where done=0 order by id")]

    def test_a_refused_line_is_found_by_halves_and_left_out(self):
        self.lines(8)
        calls = []

        def llm(system, user):
            calls.append(user.count("第"))
            if "第5句" in user:
                raise memory.Blocked("blocked")
            return {"ops": []}
        with patch.object(memory, "llm", llm):
            for _ in range(8):
                if not self.pending():
                    break
                memory.extract("elsa", OWNER_DM, 1)
        self.assertEqual(self.pending(), [])  # the queue moves on
        with memory.db() as c:
            got = [json.loads(r[0]) for r in c.execute("select detail from log where op='extract_refused'")]
        self.assertEqual(got, [{"chat": OWNER_DM, "lines": 1}])  # only that one line, asked twice
        self.assertEqual(calls[:4], [8, 4, 4, 2])  # halves; the good ones go through on the way
        self.assertLessEqual(len(calls), 10)

    def test_other_failures_still_hold_the_lines(self):
        self.lines(3)
        with patch.object(memory, "llm", side_effect=ValueError("no JSON in memory model answer")):
            with self.assertRaises(ValueError):
                memory.extract("elsa", OWNER_DM, 1)
        self.assertEqual(len(self.pending()), 3)

    def test_empty_answer_raises_blocked(self):
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"choices": [{"message": {"content": ""}}],
                                   "usage": {"prompt_tokens": 4888, "completion_tokens": 0}}).encode()
        with patch.object(memory, "load_cpa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=R()):
            with self.assertRaises(memory.Blocked):
                memory.llm("s", "u")


class LeakTests(Base):
    def setUp(self):
        super().setUp()
        memory.add("elsa", OWNER_DM, "房主下周三要去大阪出差三天", kind="event")
        memory.add("elsa", G, "路人喜欢在群里发猫猫图", kind="event")

    def test_a_group_message_repeating_an_owner_dm_memory_is_stopped(self):
        self.assertTrue(memory.leak("elsa", G, "听说房主下周三要去大阪出差呢"))
        with memory.db() as c:
            self.assertEqual(c.execute("select count(*) from log where op='leak_blocked'").fetchone()[0], 1)

    def test_ordinary_and_public_things_go(self):
        self.assertEqual(memory.leak("elsa", G, "大家晚上好，今天吃什么"), [])
        self.assertEqual(memory.leak("elsa", G, "路人又在群里发猫猫图了"), [])  # her own group memory

    def test_the_house_group_and_private_chats_are_not_checked(self):
        self.assertEqual(memory.leak("elsa", HOUSE, "房主下周三要去大阪出差三天"), [])
        self.assertEqual(memory.leak("elsa", OWNER_DM, "房主下周三要去大阪出差三天"), [])

    def test_the_house_group_is_not_told_to_keep_quiet(self):  # owner 10-25
        ask = ["[路人|42] (message_id=1): 房主下周三去哪"]
        house, other = memory.recall("elsa", HOUSE, ask), memory.recall("elsa", G, ask)
        self.assertIn("大阪", house)
        self.assertIn("可以自然地提", house)
        self.assertNotIn("别在这里说出来", house)
        self.assertIn("别在这里说出来", other)

    def test_a_group_is_checked_whatever_the_two_switches_say(self):
        # 2026-11-24 owner: was "off when owner-DM memories never reach groups" (expected []). A group that is not
        # exempt is now checked with either switch off; only LEAK_FREE_CHATS takes a group out.
        for off in ({"PRIVATE_IN_GROUPS": False}, {"UNIFIED": set()}, {"PRIVATE_IN_GROUPS": False, "UNIFIED": set()}):
            with self.subTest(off=sorted(off)), patch.multiple(memory, **off):
                self.assertTrue(memory.leak("elsa", G, "房主下周三要去大阪出差三天"))
                self.assertEqual(memory.leak("elsa", HOUSE, "房主下周三要去大阪出差三天"), [])

    def test_non_owner_private_checks_even_when_group_recall_is_disabled(self):
        with patch.object(memory, "PRIVATE_IN_GROUPS", False), patch.object(memory, "UNIFIED", set()):
            self.assertTrue(memory.leak("elsa", "private:7001", "房主下周三要去大阪出差三天"))
            self.assertEqual(memory.leak("elsa", "private:7001", "晚上好，今天吃什么"), [])

    def test_said_again_in_other_words(self):  # close vectors and still some shared pairs
        with memory.db() as c:
            mid = c.execute("select id from mem where scope='private'").fetchone()[0]
        vec = memory.array.array("f", [1.0, 0.0])
        with memory.db() as c:
            c.execute("update mem set vec=? where id=?", (vec.tobytes(), mid))
        with patch.object(memory, "embed", return_value=[memory.array.array("f", [0.9, 0.43589])]):
            self.assertEqual(memory.leak("elsa", G, "他说要去大阪那边待几天，下周的事"), [mid])


if __name__ == "__main__":
    unittest.main()
