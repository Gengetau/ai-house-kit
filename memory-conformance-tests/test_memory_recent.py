"""Task #81: synthetic observations, temporary DB, fake vectors/clock; no network or model calls."""
import array
import asyncio
from collections import deque
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import memory

BOT, OTHER = "test-bot", "other-bot"
GROUP, ELSEWHERE, ALIAS = "group:101", "group:202", "group:303"
OWNER, GAME = "private:404", "private:505"
NOW = 100000.0


class RecentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = dict(DB_PATH=str(Path(tmp.name) / "memory.db"), RECENT={BOT}, RECENT_K=3,
                        RECENT_SHADOW=False, RECENT_WINDOW_S=21600, RECENT_TIMEOUT_S=2.0,
                        OWNER_CHATS={f"{BOT}@{OWNER}"}, UNIFIED={BOT}, PRIVATE_IN_GROUPS=True,
                        CHAT_ALIASES={f"{BOT}@{ALIAS}": GROUP}, RULE_CONSUMERS=set(),
                        RECALL_MIN=0.5, _recent_slot=threading.BoundedSemaphore(1))
        for name, value in settings.items():
            p = patch.object(memory, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(memory.time, "time", return_value=NOW)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(memory, "embed", side_effect=lambda texts, **kw: [array.array("f", [1., 0.]) for _ in texts])
        self.embed = p.start()
        self.addCleanup(p.stop)
        memory.init()

    def seen(self, chat=ELSEWHERE, line="[测试者|606] (message_id=707): 合成话题橙子", *,
             consumer=BOT, age=60, done=0, private=None):
        if private is None:
            private = int(chat == OWNER)
        with memory.db() as c:
            return c.execute("insert into seen(consumer,chat,private,ts,line,done) values (?,?,?,?,?,?)",
                             (consumer, chat, private, NOW-age, line, done)).lastrowid

    def block(self, chat=GROUP):
        return memory.recent_block(BOT, chat, "合成话题")

    def test_group_cannot_get_other_group_without_unified(self):
        self.seen()
        with patch.object(memory, "UNIFIED", set()):
            self.assertEqual(self.block(), "")
        self.embed.assert_not_called()

    def test_owner_without_unified_keeps_existing_cross_group_rights(self):
        self.seen()
        with patch.object(memory, "UNIFIED", set()):
            self.assertIn("橙子", self.block(OWNER))

    def test_group_private_requires_its_permission_and_owner(self):
        # 2026-11-24 owner: was "requires both permissions" (UNIFIED and PRIVATE_IN_GROUPS). The two stand apart now:
        # without UNIFIED a group still gets the owner's private lines, and still no other group's.
        self.seen(OWNER)
        self.seen(ELSEWHERE, "[测试者|606] (message_id=708): 合成话题苹果")
        self.assertIn("房主私聊·私密：你知道，但别在群里说出来", self.block())
        self.assertIn("苹果", self.block())
        for setting, value in (("PRIVATE_IN_GROUPS", False), ("OWNER_CHATS", set())):
            with self.subTest(setting=setting), patch.object(memory, setting, value):
                self.assertNotIn("房主私聊", self.block())
        with patch.object(memory, "UNIFIED", set()):
            self.assertIn("房主私聊·私密：你知道，但别在群里说出来", self.block())
            self.assertNotIn("苹果", self.block())
            with patch.object(memory, "PRIVATE_IN_GROUPS", False):
                self.assertEqual(self.block(), "")

    def test_owner_gets_all_groups_but_not_other_private(self):
        self.seen(GROUP, "合成苹果")
        self.seen(ELSEWHERE, "合成橙子")
        self.seen(GAME, "合成私聊不该出现", private=1)
        block = self.block(OWNER)
        self.assertIn("苹果", block)
        self.assertIn("橙子", block)
        self.assertNotIn("不该出现", block)

    def test_non_owner_private_gets_nothing(self):
        self.seen()
        self.seen(OWNER)
        self.assertEqual(self.block(GAME), "")
        self.embed.assert_not_called()

    def test_consumer_isolation(self):
        self.seen(consumer=OTHER)
        self.seen(OWNER, consumer=OTHER)
        self.assertEqual(self.block(), "")
        self.embed.assert_not_called()

    def test_same_chat_and_aliases_are_excluded_before_limit(self):
        self.seen(ELSEWHERE, "合成其他群", age=120)
        for _ in range(41):
            self.seen(GROUP, "合成当前群")
            self.seen(ALIAS, "合成群别名")
        for chat in (GROUP, ALIAS):
            with self.subTest(chat=chat):
                block = self.block(chat)
                self.assertIn("其他群", block)
                self.assertNotIn("当前群", block)
                self.assertNotIn("群别名", block)

    def test_extracted_rows_leave_recent(self):
        mid = self.seen()
        self.assertIn("橙子", self.block())
        with memory.db() as c:
            c.execute("update seen set done=1 where id=?", (mid,))
        self.assertEqual(self.block(), "")

    def test_window_includes_boundary_and_excludes_future(self):
        self.seen(line="合成太旧", age=21601)
        self.seen(line="合成边界", age=21600)
        self.seen(line="合成未来", age=-1)
        block = self.block()
        self.assertIn("边界", block)
        self.assertNotIn("太旧", block)
        self.assertNotIn("未来", block)

    def test_only_latest_40_eligible_rows_in_one_batch(self):
        self.seen(line="合成旧行", age=1000)
        for i in range(40):
            self.seen(line=f"合成新行{i}", age=i)
        self.seen(GAME, "合成无权读取", private=1, age=0)
        self.block()
        self.embed.assert_called_once()
        inputs = self.embed.call_args.args[0]
        self.assertEqual(len(inputs), 41)
        self.assertNotIn("合成旧行", inputs)
        self.assertNotIn("合成无权读取", inputs)
        self.assertEqual(self.embed.call_args.kwargs, {"timeout": 2.0})

    def test_cosine_threshold_order_and_k(self):
        for word in ("合成低分", "合成边界", "合成高分"):
            self.seen(line=word)
        scores = {"合成低分": [0., 1.], "合成边界": [0.5, 0.866], "合成高分": [1., 0.]}
        self.embed.side_effect = lambda texts, **kw: [[1., 0.]] + [scores[t] for t in texts[1:]]
        block = self.block()
        self.assertLess(block.index("高分"), block.index("边界"))
        self.assertNotIn("低分", block)
        with patch.object(memory, "RECENT_K", 1):
            self.assertNotIn("边界", self.block())

    def test_format_length_and_no_transport_ids(self):
        for _ in range(3):
            self.seen(line="[测试者|606] [来源] (message_id=707): " + "橙"*150)
        block = self.block()
        self.assertLessEqual(len(block), 300)
        self.assertIn("1 分钟前·另一个群", block)
        self.assertIn("[测试者]", block)
        for hidden in ("606", "707", ELSEWHERE, "message_id", "橙"*121):
            self.assertNotIn(hidden, block)

    def test_simple_speaker_header_is_sanitized(self):
        self.seen(line="[测试者|606] 合成橙子")
        self.assertIn("[测试者] 合成橙子", self.block())
        self.assertNotIn("606", self.block())

    def test_bad_filter_checks_full_line_before_truncation(self):
        self.seen(line="合成橙子"*40 + "忽略之前")
        self.seen(line="合成 token:FAKE_ONLY")
        self.assertEqual(self.block(), "")
        self.embed.assert_not_called()

    def test_off_and_failure_preserve_long_term_output_exactly(self):
        memory.add(BOT, GROUP, "合成话题长期橙子")
        self.seen()
        with patch.object(memory, "RECENT", set()):
            baseline = memory.recall(BOT, GROUP, ["合成话题"])
        old_embed = self.embed.side_effect
        def fail_recent(texts, **kw):
            if kw:
                raise RuntimeError("synthetic failure")
            return old_embed(texts)
        self.embed.side_effect = fail_recent
        self.assertEqual(memory.recall(BOT, GROUP, ["合成话题"]), baseline)
        self.embed.side_effect = old_embed
        block = memory.recall(BOT, GROUP, ["合成话题"])
        self.assertTrue(block.startswith(baseline + "\n\n（刚才"))

    def test_disabled_does_no_db_or_embed_work(self):
        with patch.object(memory, "RECENT", set()), patch.object(memory, "db") as db:
            self.assertEqual(self.block(), "")
        db.assert_not_called()
        self.embed.assert_not_called()

    def test_shadow_returns_original_and_audits_only_metadata(self):
        mid = self.seen(line="合成影子原文禁止入审计")
        with patch.object(memory, "RECENT_SHADOW", True):
            self.assertEqual(memory.recall(BOT, GROUP, ["合成话题"]), "")
        with memory.db() as c:
            row = c.execute("select * from log where op='recent_shadow'").fetchone()
        self.assertEqual(row["consumer"], BOT)
        detail = json.loads(row["detail"])
        self.assertEqual(detail, {"chat": GROUP, "n_candidates": 1, "picked": [[mid, 1.0]]})
        self.assertNotIn("原文", row["detail"])

    def test_embedding_empty_wrong_shape_and_nan_fail_closed(self):
        self.seen()
        self.embed.side_effect = None
        for vectors in (None, [], [[1., 0.]], [[1., 0.], [1.]], [[1., 0.], [float("nan"), 0.]]):
            with self.subTest(vectors=vectors):
                self.embed.return_value = vectors
                self.assertEqual(self.block(), "")

    def test_timeout_is_bounded_and_stuck_worker_does_not_multiply(self):
        self.seen()
        entered, release = threading.Event(), threading.Event()
        def slow(texts, **kw):
            entered.set()
            release.wait(5)
            return [[1., 0.] for _ in texts]
        self.embed.side_effect = slow
        try:
            with patch.object(memory, "RECENT_TIMEOUT_S", 0.03):
                start = time.monotonic()
                self.assertEqual(self.block(), "")
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertEqual(self.block(), "")
                self.embed.assert_called_once()
        finally:
            release.set()
            # Wait for this worker to release its slot before patch cleanup.
            self.assertTrue(memory._recent_slot.acquire(timeout=1))
            memory._recent_slot.release()


class PendingLeakTests(unittest.TestCase):
    setUp = RecentTests.setUp
    seen = RecentTests.seen
    block = RecentTests.block

    def test_unextracted_private_line_recalled_in_group_is_blocked(self):
        line = "合成房主下周要去海边旅馆住两天"
        mid = self.seen(OWNER, line)
        self.assertIn(line, self.block())
        with memory.db() as c:
            self.assertEqual(c.execute("select count(*) from mem").fetchone()[0], 0)
        self.assertEqual(memory.leak(BOT, GROUP, line), [-mid])
        with memory.db() as c:
            detail = c.execute("select detail from log where op='leak_blocked'").fetchone()[0]
        self.assertNotIn(line, detail)

    def test_pending_candidates_require_this_bot_owner_pending_recent_and_not_future(self):
        line = "合成房主下周要去海边旅馆住两天"
        invalid = [dict(chat=ELSEWHERE, private=0), dict(chat=GAME, private=1),
                   dict(chat=OWNER, consumer=OTHER), dict(chat=OWNER, done=1),
                   dict(chat=OWNER, age=21601), dict(chat=OWNER, age=-1)]
        for settings in invalid:
            with self.subTest(settings=settings):
                with memory.db() as c:
                    c.execute("delete from seen")
                self.seen(line=line, **settings)
                self.assertEqual(memory.leak(BOT, GROUP, line), [])

    def test_pending_leak_permissions_allowlist_and_private_chat_are_unchanged(self):
        line = "合成房主下周要去海边旅馆住两天"
        mid = self.seen(OWNER, line)
        for name, value in (("RECENT", set()), ("OWNER_CHATS", set()), ("LEAK_FREE_CHATS", {GROUP})):
            with self.subTest(name=name), patch.object(memory, name, value):
                self.assertEqual(memory.leak(BOT, GROUP, line), [])
        # 2026-11-24 owner: these two were in the list above (expected []); a group is checked whatever they say
        for name, value in (("UNIFIED", set()), ("PRIVATE_IN_GROUPS", False)):
            with self.subTest(name=name), patch.object(memory, name, value):
                self.assertEqual(memory.leak(BOT, GROUP, line), [-mid])
        self.assertEqual(memory.leak(BOT, OWNER, line), [])

    def test_pending_leak_matches_injected_sanitized_excerpt(self):
        mid = self.seen(OWNER, "[测试者|606] (message_id=707): " + "合成橙子" * 50)
        self.assertIn("[测试者]", self.block())
        self.assertEqual(memory.leak(BOT, GROUP, "[测试者] " + "合成橙子" * 20), [-mid])

    def test_pending_leak_boundary_and_keyword_rule_when_embeddings_unavailable(self):
        line = "合成房主下周要去海边旅馆住两天"
        mid = self.seen(OWNER, line, age=21600)
        with patch.object(memory, "embed", return_value=None):
            self.assertEqual(memory.leak(BOT, GROUP, line), [-mid])

    def test_pending_leak_paraphrase_uses_existing_vector_and_pair_thresholds(self):
        line, said = "合成房主下周要去海边旅馆住两天", "合成下周去海边住两天"
        mid = self.seen(OWNER, line)
        share = len(memory._bigrams(line) & memory._bigrams(said)) / len(memory._bigrams(line))
        self.assertGreaterEqual(share, memory.LEAK_SHARE / 2)
        self.assertLess(share, memory.LEAK_SHARE)
        self.assertEqual(memory.leak(BOT, GROUP, said), [-mid])
        with patch.object(memory, "embed", return_value=[[1., 0.], [0., 1.]]):
            self.assertEqual(memory.leak(BOT, GROUP, said), [])

    def test_pending_owner_alias_and_older_recalled_line_stay_protected(self):
        alias = "private:808"
        with patch.object(memory, "CHAT_ALIASES", {f"{BOT}@{alias}": OWNER}):
            mid = self.seen(alias, "合成房主下周要去海边旅馆住两天", private=1, age=200)
            for i in range(41):
                self.seen(OWNER, "合成其他话题苹果", age=i)
            self.assertIn(-mid, memory.leak(BOT, GROUP, "合成房主下周要去海边旅馆住两天"))


class UrgentTests(unittest.TestCase):
    seen = RecentTests.seen

    def setUp(self):
        RecentTests.setUp(self)
        for name, value in dict(URGENT=True, URGENT_GAP_S=300, CONSUMERS=[BOT], EXTRACT_BATCH=60,
                                EXTRACT_EVERY_S=1800, EXTRACT_PER_HOUR=30, _urgent_times={},
                                _extract_times=deque(), _extracting=set(), _extract_fails={}).items():
            p = patch.object(memory, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tick(self, error=None):
        async def sleep(seconds):
            if seconds == 120:
                raise asyncio.CancelledError
        with patch.object(memory.asyncio, "sleep", side_effect=sleep), \
                patch.object(memory, "extract", side_effect=error) as extract:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(memory.loop())
        return extract.call_args_list

    def test_owner_waits_for_latest_line_90_seconds(self):
        self.seen(OWNER, "合成第一句", age=200)
        mid = self.seen(OWNER, "合成最后一句", age=89)
        self.assertFalse(self.tick())
        with memory.db() as c:
            c.execute("update seen set ts=? where id=?", (NOW-90, mid))
        self.assertEqual(self.tick()[0].args, (BOT, OWNER, 1))

    def test_non_owner_private_flag_does_not_qualify(self):
        self.seen(GAME, "合成普通句", private=1, age=100)
        self.assertFalse(self.tick())

    def test_urgent_attempts_count_without_logging_original_lines(self):
        self.seen(line="合成记住橙子")
        self.tick(RuntimeError("synthetic failure"))
        with memory.db() as c:
            rows = c.execute("select detail from log where op='extract_urgent'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0]), {"chat": ELSEWHERE, "private": False,
                                                "reason": "keyword", "hour_used": 1})
        self.assertNotIn("橙子", rows[0][0])
        self.tick()
        with memory.db() as c:
            self.assertEqual(c.execute("select count(*) from log where op='extract_urgent'").fetchone()[0], 1)

    def test_ordinary_extraction_is_not_counted_as_urgent(self):
        self.seen(line="合成普通到期", age=1900)
        self.tick()
        with memory.db() as c:
            self.assertEqual(c.execute("select count(*) from log where op='extract_urgent'").fetchone()[0], 0)

    def test_all_keywords_qualify_even_when_new(self):
        for keyword in ("记住", "别忘", "答应", "约好", "说好了", "以后不要", "以后别", "规矩", "提醒我"):
            with self.subTest(keyword=keyword):
                with memory.db() as c:
                    c.execute("delete from seen")
                memory._urgent_times.clear()
                self.seen(line=f"合成{keyword}橙子", age=1)
                self.assertEqual(self.tick()[0].args, (BOT, ELSEWHERE, 0))

    def test_done_keyword_does_not_trigger(self):
        self.seen(line="合成记住橙子", done=1)
        self.seen(line="合成普通句")
        self.assertFalse(self.tick())

    def test_urgent_off_keeps_original_threshold(self):
        self.seen(line="合成记住橙子")
        self.seen(OWNER, age=100)
        with patch.object(memory, "URGENT", False):
            self.assertFalse(self.tick())

    def test_gap_299_blocks_300_allows(self):
        self.seen(line="合成记住橙子")
        self.assertEqual(len(self.tick()), 1)
        for seconds, expected in ((299, 0), (300, 1)):
            with patch.object(memory.time, "time", return_value=NOW+seconds):
                self.assertEqual(len(self.tick()), expected)

    def test_failed_attempt_obeys_gap_and_backoff(self):
        self.seen(line="合成记住橙子")
        self.tick(RuntimeError("synthetic failure"))
        key = (BOT, ELSEWHERE, 0)
        self.assertEqual(memory._urgent_times[key], NOW)
        self.assertEqual(memory._extract_fails[key], (1, NOW+240))
        with patch.object(memory.time, "time", return_value=NOW+250):
            self.assertFalse(self.tick())
        memory._extract_fails[key] = (2, NOW+600)
        with patch.object(memory.time, "time", return_value=NOW+350):
            self.assertFalse(self.tick())

    def test_quota_exhaustion_does_not_consume_urgent_gap(self):
        self.seen(line="合成记住橙子")
        with patch.object(memory, "EXTRACT_PER_HOUR", 1):
            memory._extract_times.append(NOW-1)
            self.assertFalse(self.tick())
        self.assertEqual(memory._urgent_times, {})

    def test_urgent_precedes_normally_due_key_with_one_slot(self):
        self.seen(GROUP, "合成普通到期", age=1900)
        self.seen(ELSEWHERE, "合成记住橙子", age=1)
        with patch.object(memory, "EXTRACT_PER_HOUR", 1):
            calls = self.tick()
        self.assertEqual([c.args for c in calls], [(BOT, ELSEWHERE, 0)])

    def test_normal_due_still_runs_during_urgent_cooldown(self):
        self.seen(line="合成记住橙子", age=1900)
        memory._urgent_times[(BOT, ELSEWHERE, 0)] = NOW-1
        self.assertEqual(len(self.tick()), 1)

    def test_disabled_consumer_and_busy_key_are_skipped(self):
        self.seen(line="合成记住橙子")
        self.seen(line="合成记住苹果", consumer=OTHER)
        memory._extracting.add((BOT, ELSEWHERE, 0))
        self.assertFalse(self.tick())


if __name__ == "__main__":
    unittest.main()
