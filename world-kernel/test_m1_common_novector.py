"""M1: recall in the shared memory file for a house with no vector service (src/ai_house/common/UPSTREAM.md).

Decided 2026-11-24: where a query or a memory has no vector, the question's content words decide (filler like
「你记得我…吗」 left out), so 「你记得我对什么过敏吗」 finds 「房主对花生过敏」; unrelated questions still find nothing.
Unfinished private lines (recent_block) are picked the same way when MEMORY_RECENT_KEYWORD is on. backfill_vectors()
gives vectors to memories saved before a vector service was turned on. With vectors on both sides nothing changes:
the scores pinned below were measured on the file as it was before this change.
"""

import array
import hashlib
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_house.common import loader, roster

OWNER_QQ = 999
OWNER, GROUP, OTHER_GROUP = "private:999", "group:5001", "group:5002"
NOW = 100000.0
NO_NOTE = "你的记忆里没有找到"


def fake_embed(texts, timeout=20):
    """Deterministic normalised vectors from hashed character pairs."""
    out = []
    for t in texts:
        v, t = [0.0] * 24, "".join(str(t).split())
        for i in range(max(len(t) - 1, 1)):
            v[int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16) % 24] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append(array.array("f", (x / n for x in v)))
    return out


def no_embed(texts, timeout=20):
    return None


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        m = loader.load(roster=roster.make([{"actor": "nia", "qq": 1000, "name": "妮娅"}], owner={"qq": OWNER_QQ}))
        self.addCleanup(loader.unload, m)
        m.DB_PATH = str(Path(tmp.name) / "m.db")
        m.CONSUMERS = ["nia"]
        m.OWNER_CHATS = {f"nia@{OWNER}"}
        m.embed = no_embed
        m.init()
        self.m = m

    def found(self, question, chat=GROUP, min_score=0.5):
        return [r["text"] for _, r in self.m.search("nia", chat, question, k=20, min_score=min_score)]


class NoVectorSearchTests(Case):
    FACTS = ["房主对花生过敏", "房主下个月要辞职去开一家咖啡店", "阿甲喜欢周末去爬山", "房主的生日是3月6日",
             "团子是房主在三月领养的猫", "星期五晚上大家约好一起看电影"]

    def setUp(self):
        super().setUp()
        for t in self.FACTS:
            self.m.add("nia", GROUP, t)

    def test_natural_questions_find_the_fact(self):
        for q, want in (("你记得我对什么过敏吗？", "房主对花生过敏"), ("你还记得我对什么过敏吗", "房主对花生过敏"),
                        ("你记得我什么时候生日吗", "房主的生日是3月6日"), ("你们记得谁喜欢爬山吗", "阿甲喜欢周末去爬山"),
                        ("最近有什么好看的电影吗", "星期五晚上大家约好一起看电影")):
            with self.subTest(q=q):
                self.assertEqual(self.found(q)[:1], [want])

    def test_these_were_missed_before(self):
        # the bare character-pair rule (kept as the floor) does not reach the fact for the first three
        qb = self.m._bigrams
        for q, fact in (("你记得我对什么过敏吗？", self.FACTS[0]), ("你记得我什么时候生日吗", self.FACTS[3])):
            self.assertLess(self.m._kw(qb(q), fact), 0.15)

    def test_unrelated_questions_find_nothing(self):
        for q in ("你记得我喜欢什么颜色吗", "今天天气真不错", "你记得我的银行卡吗", "你知道我最近的打算吗",
                  "你记得我的车是什么牌子吗", "你记得我的工作吗", "我之前说过要开什么店来着"):
            with self.subTest(q=q):
                self.assertEqual(self.found(q), [])

    def test_what_the_old_rule_found_is_still_found_with_the_same_score(self):
        for q in ("大家周五晚上看什么", "房主下个月要辞职去开一家咖啡店吗"):
            rows = self.m.search("nia", GROUP, q, k=20)
            kw = self.m._kw(self.m._bigrams(q), rows[0][1]["text"])
            self.assertGreaterEqual(rows[0][0], 0.4 + 0.6 * kw - 1e-9)
            self.assertIn(rows[0][1]["text"], self.found(q))

    def test_recall_gives_the_memory_and_not_the_no_note(self):
        got = self.m.recall("nia", GROUP, ["[路人|42] (message_id=1): 你记得我对什么过敏吗"])
        self.assertIn("花生", got)
        self.assertNotIn(NO_NOTE, got)
        none = self.m.recall("nia", GROUP, ["[路人|42] (message_id=2): 你记得我喜欢什么颜色吗"])
        self.assertIn(NO_NOTE, none)
        self.assertNotIn("花生", none)

    def test_a_row_without_a_vector_is_found_beside_rows_with_one(self):
        self.m.embed = fake_embed
        self.m.add("nia", GROUP, "房主对虾过敏，吃了会起疹子")  # has a vector
        self.assertIn("房主对花生过敏", self.found("你记得我对什么过敏吗", min_score=0.5))  # has none
        self.assertEqual(self.found("你记得我喜欢什么颜色吗"), [])


class WithVectorsNothingChangesTests(Case):
    MEMS = ["房主对花生过敏", "房主下个月要辞职去开一家咖啡店", "阿甲喜欢周末去爬山", "妮娅养了一只叫团子的猫", "房主的生日是3月6日",
            "星期五晚上大家约好一起看电影", "房主不喜欢香菜，吃面不要放"]
    # (id, score) of every row, measured on the shared file before the no-vector change, about=["房主"]
    PINNED = {
        "你记得我对什么过敏吗": [(4, 0.483494), (1, 0.295137), (2, 0.252825), (6, 0.141456), (5, 0.136364), (3, 0.07151), (7, 0.0)],
        "房主要开咖啡店吗": [(2, 0.456081), (5, 0.287908), (7, 0.271628), (1, 0.120408), (6, 0.118217), (3, 0.089642), (4, 0.075761)],
        "阿甲周末去哪": [(3, 0.648915), (2, 0.225), (6, 0.209814), (4, 0.179284), (5, 0.10113), (7, 0.07151), (1, 0.0)],
        "团子是什么猫": [(4, 0.393924), (2, 0.15), (3, 0.106066), (5, 0.10113), (1, 0.096825), (6, 0.0), (7, 0.0)],
        "今天天气真不错": [(4, 0.42521), (3, 0.251558), (2, 0.237171), (7, 0.113067), (5, 0.07995), (6, 0.055291), (1, 0.0)],
        "生日是哪天": [(5, 0.544926), (7, 0.522233), (2, 0.273861), (1, 0.265165), (3, 0.096825), (6, 0.063844), (4, 0.0)],
        "你知道我不爱吃什么吗": [(4, 0.467707), (2, 0.391312), (7, 0.373101), (6, 0.312772), (5, 0.301511), (1, 0.216506), (3, 0.079057)],
        "周五晚上做什么": [(6, 0.353335), (1, 0.273861), (4, 0.253546), (5, 0.143019), (2, 0.106066), (3, 0.075), (7, 0.0)],
    }

    def test_scores_are_what_they_were(self):
        self.m.embed = fake_embed
        for t in self.MEMS:
            self.m.add("nia", GROUP, t)
        for q, want in self.PINNED.items():
            with self.subTest(q=q):
                got = [(r["id"], round(s, 6)) for s, r in self.m.search("nia", GROUP, q, about=["房主"], k=20)]
                self.assertEqual(got, want)

    def test_a_precomputed_query_vector_is_used_as_before(self):
        self.m.embed = fake_embed
        for t in self.MEMS:
            self.m.add("nia", GROUP, t)
        q = "你记得我对什么过敏吗"
        a = self.m.search("nia", GROUP, q, about=["房主"], k=20)
        b = self.m.search("nia", GROUP, q, about=["房主"], k=20, qvec=fake_embed([q])[0])
        self.assertEqual([(r["id"], s) for s, r in a], [(r["id"], s) for s, r in b])


class RecentLinesTests(Case):
    LINE = "[路人|606] (message_id=707): 我对花生过敏，吃了会起疹子"
    OTHER = "[路人|606] (message_id=708): 周末天气不错适合出去玩"

    def setUp(self):
        super().setUp()
        for name, value in dict(RECENT={"nia"}, RECENT_K=3, RECENT_SHADOW=False, RECENT_WINDOW_S=21600,
                                RECENT_TIMEOUT_S=2.0, UNIFIED={"nia"}, PRIVATE_IN_GROUPS=True, RECALL_MIN=0.5,
                                _recent_slot=threading.BoundedSemaphore(1)).items():
            p = patch.object(self.m, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(self.m.time, "time", return_value=NOW)
        p.start()
        self.addCleanup(p.stop)
        for chat, line, private in ((OTHER_GROUP, self.LINE, 0), (OTHER_GROUP, self.OTHER, 0)):
            with self.m.db() as c:
                c.execute("insert into seen(consumer,chat,private,ts,line,done) values (?,?,?,?,?,?)",
                          ("nia", chat, private, NOW - 60, line, 0))

    def block(self, q):
        return self.m.recent_block("nia", GROUP, q)

    def test_off_by_default_no_vectors_no_lines(self):
        self.assertFalse(self.m.RECENT_KEYWORD)
        self.assertEqual(self.block("你记得我对什么过敏吗"), "")

    def test_on_the_line_is_picked_by_keyword(self):
        with patch.object(self.m, "RECENT_KEYWORD", True):
            got = self.block("你记得我对什么过敏吗")
            self.assertIn("花生过敏", got)
            self.assertNotIn("天气", got)
            self.assertEqual(self.block("你记得我喜欢什么颜色吗"), "")
            self.assertEqual(self.block("今天去哪里吃饭"), "")

    def test_her_own_lines_bare_questions_and_scraps_are_not_things_she_was_told(self):
        with self.m.db() as c:
            c.execute("delete from seen")
            for mid, line in enumerate(("[妮娅|1000] (message_id=1): 好的，花生过敏我记下了",  # her own reply
                                        "[路人|606] (message_id=2): 你觉得花生过敏严重吗？",  # only asks
                                        "[路人|606] (message_id=3): 过敏",  # a scrap
                                        "[妮娅过敏|606] (message_id=4): 周末天气不错适合出去玩")):  # the words are in the name
                c.execute("insert into seen(consumer,chat,private,ts,line,done) values (?,?,?,?,?,?)",
                          ("nia", OTHER_GROUP, 0, NOW - 60 + mid, line, 0))
        with patch.object(self.m, "RECENT_KEYWORD", True):
            self.assertEqual(self.block("妮娅，你记得我对什么过敏吗？"), "")
            with self.m.db() as c:
                c.execute("insert into seen(consumer,chat,private,ts,line,done) values (?,?,?,?,?,?)",
                          ("nia", OTHER_GROUP, 0, NOW - 30, "[路人|606] (message_id=5): 我一吃花生就过敏起疹子。严重吗？", 0))
            got = self.block("妮娅，你记得我对什么过敏吗？")
            self.assertIn("起疹子", got)  # one statement in it and it is something she was told
            self.assertEqual(got.count("\n- "), 1)
            for word in ("记下了", "你觉得", "天气"):
                self.assertNotIn(word, got)

    def test_it_keeps_the_rights_and_the_window(self):
        with patch.object(self.m, "RECENT_KEYWORD", True):
            self.assertEqual(self.m.recent_block("nia", "private:404", "你记得我对什么过敏吗"), "")  # not the owner's chat
            with patch.object(self.m, "UNIFIED", set()), patch.object(self.m, "PRIVATE_IN_GROUPS", False):
                self.assertEqual(self.block("你记得我对什么过敏吗"), "")
            with patch.object(self.m.time, "time", return_value=NOW + 30 * 3600):
                self.assertEqual(self.block("你记得我对什么过敏吗"), "")
            with patch.object(self.m, "RECENT_SHADOW", True):
                self.assertEqual(self.block("你记得我对什么过敏吗"), "")

    def test_with_vectors_the_switch_changes_nothing(self):
        self.m.embed = lambda texts, **kw: [array.array("f", [1.0, 0.0]) for _ in texts]
        off = self.block("你记得我对什么过敏吗")
        with patch.object(self.m, "RECENT_KEYWORD", True):
            on = self.block("你记得我对什么过敏吗")
        self.assertEqual(on, off)
        self.assertIn("花生", off)
        self.assertIn("天气", off)  # same vector for every line: the cosine, not the words, decides


class BackfillTests(Case):
    def test_fills_what_has_no_vector_and_nothing_else(self):
        for t in ("房主对花生过敏", "阿甲喜欢周末去爬山", "房主的生日是3月6日"):
            self.m.add("nia", GROUP, t)
        gone, _ = self.m.add("nia", GROUP, "这条会被忘掉的记忆")
        with self.m.db() as c:
            c.execute("update mem set status='deleted' where id=?", (gone,))
        self.m.embed = fake_embed
        self.assertEqual(self.m.backfill_vectors(batch=2), 3)  # two batches
        self.assertEqual(self.m.backfill_vectors(batch=2), 0)
        with self.m.db() as c:
            rows = c.execute("select id, status, vec from mem order by id").fetchall()
        self.assertEqual([(r["status"], r["vec"] is not None) for r in rows],
                         [("active", True), ("active", True), ("active", True), ("deleted", False)])
        self.assertEqual(self.m.search("nia", GROUP, "你记得我对什么过敏吗", k=1)[0][1]["text"], "房主对花生过敏")

    def test_nothing_without_a_service(self):
        self.m.add("nia", GROUP, "房主对花生过敏")
        self.assertEqual(self.m.backfill_vectors(), 0)
        with self.m.db() as c:
            self.assertIsNone(c.execute("select vec from mem").fetchone()["vec"])

    def test_a_service_that_stops_midway_keeps_what_was_filled(self):
        for t in ("房主对花生过敏", "阿甲喜欢周末去爬山", "房主的生日是3月6日"):
            self.m.add("nia", GROUP, t)
        calls = []

        def flaky(texts, timeout=20):
            calls.append(len(texts))
            return fake_embed(texts) if len(calls) == 1 else None
        self.m.embed = flaky
        self.assertEqual(self.m.backfill_vectors(batch=2), 2)
        self.m.embed = fake_embed
        self.assertEqual(self.m.backfill_vectors(batch=2), 1)

    def test_a_row_edited_meanwhile_is_left_alone(self):
        mid, _ = self.m.add("nia", GROUP, "房主对花生过敏")

        def edits_then_embeds(texts, timeout=20):
            with self.m.db() as c:
                c.execute("update mem set text=? where id=?", ("房主对虾过敏", mid))
            return fake_embed(texts)
        self.m.embed = edits_then_embeds
        self.assertEqual(self.m.backfill_vectors(), 0)
        with self.m.db() as c:
            self.assertIsNone(c.execute("select vec from mem where id=?", (mid,)).fetchone()["vec"])


if __name__ == "__main__":
    unittest.main()
