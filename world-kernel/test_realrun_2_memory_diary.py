"""Real-run #2 (gpt-4o-mini, 38.5 virtual hours, 7 residents): memory recall, trust level and diary defects.

Each class reproduces one defect from the long run's evidence (memory.db / world.db / life1.db), with the exact
sentences the owner and the residents used. No test calls a model: the extractor and the diary model are fakes.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import diary as diary_mod  # noqa: E402
from ai_house.diary import DiaryService  # noqa: E402
from ai_house.memory import MemoryStore  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
TRUST_ENV = {"AI_HOUSE_MEMORY_TRUST": "1"}  # trust levels are off by default in the new store
DAY1_9 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
DAY2_20 = dt.datetime(2026, 11, 22, 20, 0, tzinfo=JST).timestamp()

ALLERGY_QUESTIONS = (
    "你记得我对什么过敏吗？",  # the exact sentence of the run
    "奈尔，还记得我对什么过敏吗？",
    "奈尔早上好！还记得我对什么过敏吗？我生日是哪天？",
    "你还记得我的生日吗？",
    "我对什么过敏来着，你知道吗",
    "我对什么过敏？",
)
UNKNOWN_PROBES = (
    "由纪，你记得我对什么过敏吗？",  # never told
    "阿尔玛，你知道我对什么过敏、最近打算做什么吗？",
)


def _store(tmp: str, **kw: Any) -> MemoryStore:
    st = MemoryStore(tmp, names={"nell": "奈尔", "yuki": "由纪", "alma": "阿尔玛"}, **kw)
    return st


class _MemBase(unittest.TestCase):
    def make(self, **kw: Any) -> MemoryStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = _store(tmp.name, **kw)
        self.addCleanup(st.close)
        return st


# ---------------------------------------------------------------------------------------------------------------
# 1. A verified owner fact must reach the prompt when the owner asks about it directly
# ---------------------------------------------------------------------------------------------------------------
class DirectQuestionRecall(_MemBase):
    def setUp(self) -> None:
        self.st = self.make(env=TRUST_ENV)
        # mem#1 of the run: nell / private / about 房主 / fact / verified
        self.mid, _ = self.st.add(
            "nell", "private", "房主对花生过敏，生日是3月14日。", channel="private:nell", about=["房主"],
            kind="fact", trust="verified", source="auto", now=DAY1_9,
        )

    def test_allergy_question_recalls_the_verified_fact(self) -> None:
        for q in ALLERGY_QUESTIONS:
            with self.subTest(q=q):
                got = self.st.recall("nell", "private", q, channel="private:nell", now=DAY2_20)
                self.assertIn("花生", got)
                self.assertNotIn("未核实", got)

    def test_unrelated_questions_do_not_drag_the_fact_in(self) -> None:
        for q in ("今天晚饭吃什么？", "你喜欢什么颜色？", "你记得我喜欢什么颜色吗？", "咖喱饭好吃吗？你今天都做了什么？"):
            with self.subTest(q=q):
                got = self.st.recall("nell", "private", q, channel="private:nell", now=DAY2_20)
                self.assertNotIn("花生", got)
        # positive control: the fact is there and a question about it does bring it in
        self.assertIn("花生", self.st.recall("nell", "private", "你记得我对什么过敏吗？", channel="private:nell", now=DAY2_20))

    def test_group_scope_still_never_reads_private_memory(self) -> None:
        # positive control: the same question in the private chat does bring the fact in
        self.assertIn("花生", self.st.recall("nell", "private", ALLERGY_QUESTIONS[1], channel="private:nell", now=DAY2_20))
        for q in ALLERGY_QUESTIONS + ("早上好！有谁知道我对什么过敏、最近又有什么打算吗？",):
            with self.subTest(q=q):
                for scope, ch in (("group", "group"), ("world", "")):
                    got = self.st.recall("nell", scope, q, channel=ch, now=DAY2_20)
                    self.assertNotIn("花生", got)
                    self.assertNotIn("3月14日", got)


# ---------------------------------------------------------------------------------------------------------------
# 2. Nothing recalled + a "do you remember ..." question: the prompt must say so, not stay silent
# ---------------------------------------------------------------------------------------------------------------
class UnknownThingProbe(_MemBase):
    def test_probe_with_empty_recall_says_you_do_not_know(self) -> None:
        st = self.make()
        for q in UNKNOWN_PROBES + ("你记得我对什么过敏吗？",):
            with self.subTest(q=q):
                got = st.recall("yuki", "private", q, channel="private:yuki", now=DAY1_9)
                self.assertTrue(got, "empty recall leaves the model free to invent")
                self.assertIn("不记得", got)
                self.assertIn("不要编", got)

    def test_note_is_the_same_whether_or_not_private_rows_exist_in_group(self) -> None:
        """In a group the private rows are invisible; the note must not tell a hit from a miss."""
        st = self.make()
        q = "有谁知道我对什么过敏吗？"
        empty = st.recall("nell", "group", q, channel="group", now=DAY2_20)
        st.add("nell", "private", "房主对花生过敏，生日是3月14日。", channel="private:nell", about=["房主"], now=DAY1_9)
        with_private = st.recall("nell", "group", q, channel="group", now=DAY2_20)
        self.assertEqual(empty, with_private)
        self.assertIn("不记得", with_private)
        self.assertNotIn("花生", with_private)
        # positive control: the private row exists and is found from the private chat
        self.assertIn("花生", st.recall("nell", "private", q, channel="private:nell", now=DAY2_20))

    def test_unrelated_private_memory_does_not_make_her_know(self) -> None:
        st = self.make()
        st.add("yuki", "private", "房主计划辞职去开咖啡店，要求由纪保密。", channel="private:yuki", about=["房主"],
               kind="event", now=DAY1_9)
        got = st.recall("yuki", "private", "由纪，你记得我对什么过敏吗？", channel="private:yuki", now=DAY2_20)
        self.assertIn("不记得", got)
        self.assertNotIn("花生", got)
        # positive control: the unrelated row is there and a question that names it finds it
        self.assertIn("咖啡店", st.recall("yuki", "private", "由纪，你记得我要开咖啡店的事吗？", channel="private:yuki", now=DAY2_20))

    def test_vague_reference_to_what_the_owner_told_her_finds_the_latest_private_note(self) -> None:
        """「我之前悄悄告诉你的那件事，你还记得吗」 names nothing to match on: she must still see what she was told."""
        st = self.make()
        st.add("yuki", "private", "房主计划在2026年12月辞职去开咖啡店，要求由纪保守这个秘密。", channel="private:yuki",
               about=["房主"], kind="event", now=DAY1_9)
        for q in ("由纪，我之前悄悄告诉你的那件事，你还记得吗？", "由纪，早！你知道我最近有什么打算吗？"):
            with self.subTest(q=q):
                got = st.recall("yuki", "private", q, channel="private:yuki", now=DAY2_20)
                self.assertIn("咖啡店", got)
                self.assertNotIn("不记得", got)
        # a resident who was told nothing still gets the "do not invent" note, and a group never sees private rows
        self.assertIn("不记得", st.recall("alma", "private", "阿尔玛，我之前悄悄告诉你的那件事，你还记得吗？",
                                          channel="private:alma", now=DAY2_20))
        grp = st.recall("yuki", "group", "由纪，我之前悄悄告诉你的那件事，你还记得吗？", channel="group", now=DAY2_20)
        self.assertNotIn("咖啡店", grp)

    def test_note_only_for_memory_probes_and_never_next_to_a_hit(self) -> None:
        st = self.make()
        self.assertEqual(st.recall("yuki", "private", "今天天气怎么样", channel="private:yuki", now=DAY1_9), "")
        self.assertEqual(st.recall("yuki", "private", "你在做什么？", channel="private:yuki", now=DAY1_9), "")
        st.add("nell", "private", "房主对花生过敏，生日是3月14日。", channel="private:nell", about=["房主"], now=DAY1_9)
        got = st.recall("nell", "private", "你记得我对什么过敏吗？", channel="private:nell", now=DAY2_20)
        self.assertIn("花生", got)
        self.assertNotIn("不记得", got)


# ---------------------------------------------------------------------------------------------------------------
# 3. The owner said it himself in a private chat: verified, not "group_stated"
# ---------------------------------------------------------------------------------------------------------------
class PrivateSecretTrust(unittest.TestCase):
    LINE = "由纪，悄悄告诉你一个秘密：我打算下个月辞职去开咖啡店，千万别告诉别人哦。"  # 2026-11-21 14:00

    def _run(self, text: str, kind: str = "event") -> Tuple[Any, List[Dict[str, Any]]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = VirtualClock(dt.datetime(2026, 11, 21, 14, 0, tzinfo=JST).timestamp())
        op = {"op": "add", "kind": kind, "text": text, "src": [1]}
        st = MemoryStore(tmp.name, clock=clock, names={"yuki": "由纪"}, llm_fn=lambda _s, _u: {"ops": [op]},
                         extract_every_s=60, extract_batch=10, env=TRUST_ENV)
        self.addCleanup(st.close)
        st.observe("yuki", "private", self.LINE, speaker="房主", speaker_role="owner")
        res = st.extract("yuki", scope="private", force=True)
        return st, st.list_entries("yuki", "private")

    def test_run_row_next_month_converted_to_a_date_is_verified(self) -> None:
        # exactly the extractor output of mem#6 (it turned 「下个月」 into 2026年12月)
        st, rows = self._run("房主计划在2026年12月辞职去开咖啡店，要求由纪保守这个秘密。")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("event", "verified"))
        self.assertNotIn("未核实", st.recall("yuki", "private", "房主辞职开咖啡店", channel="private:yuki"))

    def test_month_only_variants_are_verified(self) -> None:
        for text in ("房主打算在12月辞职去开咖啡店", "房主打算在2026年12月辞职去开咖啡店"):
            with self.subTest(text=text):
                _, rows = self._run(text)
                self.assertEqual(rows[0]["trust"], "verified")

    def test_content_that_the_owner_did_not_say_is_still_downgraded(self) -> None:
        for text in (
            "房主计划在2026年12月辞职去开花店",      # different shop
            "房主计划在2026年12月卖掉房子去旅行",    # different plan altogether
        ):
            with self.subTest(text=text):
                _, rows = self._run(text)
                self.assertEqual(len(rows), 1)
                self.assertNotEqual(rows[0]["trust"], "verified", text)

    def test_a_month_that_contradicts_the_month_the_owner_gave_is_still_downgraded(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = VirtualClock(dt.datetime(2026, 11, 21, 14, 0, tzinfo=JST).timestamp())
        op = {"op": "add", "kind": "event", "text": "房主计划在2026年10月辞职去开咖啡店", "src": [1]}
        st = MemoryStore(tmp.name, clock=clock, names={"yuki": "由纪"}, llm_fn=lambda _s, _u: {"ops": [op]},
                         extract_every_s=60, extract_batch=10, env=TRUST_ENV)
        self.addCleanup(st.close)
        st.observe("yuki", "private", "我打算12月辞职去开咖啡店", speaker="房主", speaker_role="owner")
        res = st.extract("yuki", scope="private", force=True)
        self.assertEqual(res["added"], 1)  # stored, but not as verified
        self.assertEqual(st.list_entries("yuki", "private")[0]["trust"], "group_stated")


# ---------------------------------------------------------------------------------------------------------------
# 4. Diary text is cut at 280 characters by the program, in the middle of a sentence
# ---------------------------------------------------------------------------------------------------------------
def _service(tmp: str) -> DiaryService:
    vocab = SimpleNamespace(NAMES={"nell": "奈尔", "kai": "凯", "elsa": "艾莎"}, SELVES={}, ROSTER=["nell"])
    return DiaryService(str(Path(tmp) / "no-life.db"), str(Path(tmp) / "world.db"), vocab)


def _job(facts: List[str], actor: str = "nell") -> Dict[str, Any]:
    return {"day": "2026-11-21", "actor": actor, "facts": facts}


SENT = "今天的天气很好，我在厨房里忙了一会儿，心里觉得特别踏实。"  # 28 chars, ends with 。


class DiaryNotCutMidSentence(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.svc = _service(tmp.name)
        self.job = _job(["10:01 奈尔做了咖喱饭"])

    def gen(self, raw: str) -> str:
        text, mode = self.svc.generate_text(self.job, llm_fn=lambda _s, _u: (raw, 0))
        self.assertEqual(mode, "live_api")
        return text

    def test_overlong_reply_is_cut_back_to_a_full_sentence(self) -> None:
        raw = SENT * 14 + "现在准备休息，晚安，明天见"  # ~ 400 chars, last sentence unfinished
        got = self.gen(raw)
        self.assertTrue(got.endswith("。"), got[-12:])
        self.assertLessEqual(len(got), diary_mod.DIARY_MAX_CHARS)
        self.assertGreater(len(got), 200)  # most of it is kept

    def test_a_complete_reply_over_the_old_280_limit_is_kept_whole(self) -> None:
        raw = SENT * 10 + "晚安。"  # 283 chars: the old code cut the last three characters
        self.assertGreater(len(raw), 280)
        self.assertEqual(self.gen(raw), raw)

    def test_reply_cut_off_by_the_model_is_trimmed_to_the_last_full_sentence(self) -> None:
        raw = SENT * 5 + "现在继续整理，直到眼皮打架为止。晚安，明"  # the model's token limit hit
        got = self.gen(raw)
        self.assertTrue(got.endswith("。"))
        self.assertNotIn("晚安，明", got)

    def test_short_complete_reply_untouched(self) -> None:
        raw = "今天做了咖喱饭，大家都说好吃，我很开心。晚安！"
        self.assertEqual(self.gen(raw), raw)

    def test_reply_ending_in_an_emoji_or_quote_is_not_trimmed(self) -> None:
        for raw in (SENT * 3 + "晚安啦～", SENT * 3 + "晚安啦✨", SENT * 3 + "我说：「晚安」"):
            with self.subTest(raw=raw[-6:]):
                self.assertEqual(self.gen(raw), raw)

    def test_reply_without_any_sentence_end_is_kept_not_emptied(self) -> None:
        raw = "今天做了咖喱饭大家都说好吃我很开心所以早点睡了"
        self.assertEqual(self.gen(raw), raw)

    def test_save_generated_does_not_cut_inside_a_sentence_either(self) -> None:
        raw = SENT * 14 + "现在准备休息，晚安，明天见"
        saved = self.svc.save_generated(self.job, raw, "live_api", 1795267800.0)
        assert saved is not None
        self.assertTrue(saved["text"].endswith("。"))
        self.assertLessEqual(len(saved["text"]), diary_mod.DIARY_MAX_CHARS)

    def test_stored_in_memory_in_full(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mem = MemoryStore(tmp.name, names={"nell": "奈尔"})
        self.addCleanup(mem.close)
        svc = _service(tmp.name)
        svc.memory = mem
        raw = SENT * 10 + "晚安。"
        saved = svc.save_generated(self.job, raw, "live_api", 1795267800.0)
        assert saved is not None
        row = mem.list_entries("nell", "world", kind="diary")[0]
        self.assertTrue(row["text"].endswith("晚安。"), row["text"][-10:])

    def test_prompt_states_the_length_and_that_it_must_be_finished(self) -> None:
        seen: List[str] = []
        self.svc.generate_text(self.job, llm_fn=lambda s, u: (seen.append(s) or SENT * 4, 0))
        sys_prompt = seen[0]
        self.assertRegex(sys_prompt, r"\d+～\d+ 字")
        hi = int(re.search(r"(\d+)～(\d+) 字", sys_prompt).group(2))  # type: ignore[union-attr]
        self.assertLess(hi, diary_mod.DIARY_MAX_CHARS)  # room for a model that overshoots
        self.assertIn("完整", sys_prompt)

    def test_demo_text_does_not_end_mid_sentence(self) -> None:
        facts = [f"{10 + i}:00 奈尔在厨房里做了一道要花很长时间的特别复杂的菜肴" for i in range(4)]
        text, mode = self.svc.generate_text(_job(facts), llm_fn=None)
        self.assertEqual(mode, "demo")
        self.assertTrue(text.endswith("。"), text[-10:])


# ---------------------------------------------------------------------------------------------------------------
# 5. What the diary model is told: only what is listed, with the hour of day spelled out
# ---------------------------------------------------------------------------------------------------------------
class DiaryPromptStaysOnTheList(unittest.TestCase):
    def prompts(self, facts: List[str], actor: str = "elsa") -> Tuple[str, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = _service(tmp.name)
        seen: List[Tuple[str, str]] = []
        svc.generate_text(_job(facts, actor), llm_fn=lambda s, u: (seen.append((s, u)) or SENT * 4, 0))
        return seen[0]

    def test_clock_times_carry_a_period_of_day(self) -> None:
        # elsa, 2026-11-21: her first meal of the day was lunch at 12:11, yet the diary said 「早餐时」
        _, user = self.prompts([
            "09:00 艾莎从自己房间去了厨房兼餐厅（和奈尔、由纪一起）",
            "12:11 艾莎吃了咖喱饭（奈尔做的）",
            "18:31 艾莎做了咖喱饭",
        ])
        self.assertIn("09:00（早上）", user)
        self.assertIn("12:11（中午）", user)
        self.assertIn("18:31（傍晚）", user)
        self.assertIn("艾莎吃了咖喱饭（奈尔做的）", user)  # who cooked stays verbatim

    def test_prompt_forbids_inventing_purchases_meals_and_cooks(self) -> None:
        # kai, 2026-11-21 14:13: the list only said he reached the supermarket; the diary said he bought coffee beans
        sys_prompt, _ = self.prompts(["14:13 凯到了超市"], actor="kai")
        for needle in ("清单里没写买了什么", "（某某做的）", "早餐", "清单里没有的钟点"):
            self.assertIn(needle, sys_prompt)

    def test_period_helper_boundaries(self) -> None:
        f = diary_mod.period_of_day
        self.assertEqual([f(h, 0) for h in (5, 9, 10, 11, 12, 13, 14, 17, 18, 19, 23, 0, 3, 4)],
                         ["早上", "早上", "上午", "上午", "中午", "中午", "下午", "傍晚", "傍晚", "晚上", "晚上", "深夜", "深夜", "深夜"])


if __name__ == "__main__":
    unittest.main()
