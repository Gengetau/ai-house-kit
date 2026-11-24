"""Regression coverage for lossless memory extraction after an extractor outage."""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import memory


class ExtractBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbpath = str(Path(self.tmp.name) / "memory.db")
        self.patches = [
            patch.object(memory, "DB_PATH", self.dbpath),
            patch.object(memory, "_related", return_value="（还没有）"),
            patch.object(memory, "_apply", return_value={
                "added": 0, "merged": 0, "updated": 0, "forgot": 0, "rejected": 0,
            }),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        memory.init()

    def insert(self, count, chat="group:1000000296", width=40, ts=None, done=0):
        start = self.count()
        with memory.db() as c:
            for i in range(count):
                n = start + i + 1
                line = f"line{n:05d}-" + "a" * width
                c.execute("insert into seen(consumer,chat,private,ts,line,done) values(?,?,?,?,?,?)",
                          ("nell", chat, 0, ts or time.time(), line, done))

    def count(self, done=None, chat=None):
        q = "select count(*) from seen where 1=1"
        args = []
        if done is not None:
            q += " and done=?"
            args.append(done)
        if chat:
            q += " and chat=?"
            args.append(chat)
        with memory.db() as c:
            return c.execute(q, args).fetchone()[0]

    def test_processes_oldest_only_and_retries_next_batch(self):
        self.insert(370)
        prompts = []

        def fake_llm(system, user):
            prompts.append(user)
            return {"ops": []}

        with patch.object(memory, "llm", fake_llm):
            memory.extract("nell", "group:1000000296", 0)
            self.assertEqual(self.count(done=1), 150)
            self.assertEqual(self.count(done=0), 220)
            self.assertIn("line00001-", prompts[0])
            self.assertIn("line00150-", prompts[0])
            self.assertNotIn("line00151-", prompts[0])
            memory.extract("nell", "group:1000000296", 0)
        self.assertEqual(self.count(done=1), 300)
        self.assertEqual(self.count(done=0), 70)
        self.assertIn("line00151-", prompts[1])
        self.assertNotIn("line00001-", prompts[1])

    def test_character_budget_never_silently_truncates(self):
        self.insert(9, width=690)
        prompts = []
        with patch.object(memory, "EXTRACT_MAX_CHARS", 2100), patch.object(
                memory, "llm", lambda s, u: prompts.append(u) or {"ops": []}):
            memory.extract("nell", "group:1000000296", 0)
        self.assertEqual(self.count(done=1), 2)
        self.assertEqual(self.count(done=0), 7)
        self.assertIn("line00001-", prompts[0])
        self.assertIn("line00002-", prompts[0])
        self.assertNotIn("line00003-", prompts[0])

    def test_failed_model_call_does_not_acknowledge_any_lines(self):
        self.insert(30)
        with patch.object(memory, "llm", side_effect=RuntimeError("upstream HTTP 400")):
            with self.assertRaisesRegex(RuntimeError, "400"):
                memory.extract("nell", "group:1000000296", 0)
        self.assertEqual(self.count(done=0), 30)
        self.assertEqual(self.count(done=1), 0)

    def test_answer_without_ops_list_does_not_acknowledge(self):
        self.insert(30)
        for bad in ({}, {"ops": None}, {"ops": "add"}, ["ops"]):
            with patch.object(memory, "llm", return_value=bad):
                with self.assertRaisesRegex(ValueError, "no ops list"):
                    memory.extract("nell", "group:1000000296", 0)
        self.assertEqual(self.count(done=0), 30)
        self.assertEqual(self.count(done=1), 0)

    def test_purge_never_drops_expired_pending_observations(self):
        old = time.time() - 4 * 86400
        self.insert(2, chat="group:222", ts=old)
        self.insert(1, chat="group:333", ts=old, done=1)
        self.insert(3)
        with patch.object(memory, "llm", return_value={"ops": []}), patch.object(memory, "SEEN_KEEP_S", 3 * 86400):
            memory.extract("nell", "group:1000000296", 0)
        self.assertEqual(self.count(done=0, chat="group:222"), 2)
        self.assertEqual(self.count(chat="group:333"), 0)
        self.assertEqual(self.count(done=1, chat="group:1000000296"), 3)

    def test_raw_lines_kept_for_good_by_default(self):
        # 2026-11-14 owner: SEEN_KEEP_S = 0, extracted raw lines are never purged
        self.insert(1, chat="group:333", ts=time.time() - 400 * 86400, done=1)
        self.insert(3)
        with patch.object(memory, "llm", return_value={"ops": []}):
            memory.extract("nell", "group:1000000296", 0)
        self.assertEqual(self.count(chat="group:333"), 1)


if __name__ == "__main__":
    unittest.main()


class CarryTests(unittest.TestCase):
    """2026-10-20: the note a fresh session gets from the one it replaces."""
    group, private = "group:1000000296", "private:1000000259"
    setUp, insert, count = ExtractBatchTests.setUp, ExtractBatchTests.insert, ExtractBatchTests.count

    def test_note_is_made_from_this_chat_and_given_once(self):
        self.insert(10)
        self.insert(10, chat="group:222")
        seen = []
        with patch.object(memory, "llm", lambda s, u: seen.append(u) or {"carry": "房主让她晚上提醒喝水，还没提醒"}):
            self.assertEqual(memory.carry_make("nell", self.group), "房主让她晚上提醒喝水，还没提醒")
        self.assertIn("line00001-", seen[0])
        self.assertNotIn("line00011-", seen[0])  # the other group's lines are not in it
        self.assertEqual(memory.carry_take("nell", self.private), "")  # a group's note stays in that group
        self.assertEqual(memory.carry_take("yuki", self.group), "")  # and with her
        self.assertEqual(memory.carry_take("nell", self.group), "房主让她晚上提醒喝水，还没提醒")
        self.assertEqual(memory.carry_take("nell", self.group), "")  # once

    def test_nothing_to_carry_or_a_failed_call_leaves_no_note(self):
        self.insert(3)
        with patch.object(memory, "llm", side_effect=AssertionError("no call for 3 lines")):
            self.assertEqual(memory.carry_make("nell", self.group), "")
        self.insert(10)
        with patch.object(memory, "llm", side_effect=RuntimeError("upstream 429")):
            with self.assertRaises(RuntimeError):
                memory.carry_make("nell", self.group)
        with patch.object(memory, "llm", return_value={"carry": ""}):
            self.assertEqual(memory.carry_make("nell", self.group), "")
        self.assertEqual(memory.carry_take("nell", self.group), "")

    def test_old_lines_and_old_notes_are_not_carried(self):
        self.insert(10, ts=time.time() - 13 * 3600)
        with patch.object(memory, "llm", side_effect=AssertionError("lines are too old")):
            self.assertEqual(memory.carry_make("nell", self.group), "")
        self.insert(10)
        with patch.object(memory, "llm", return_value={"carry": "在聊周末去哪"}):
            memory.carry_make("nell", self.group)
        self.assertEqual(memory.carry_take("nell", self.group, now=time.time() + 25 * 3600), "")  # a day old
