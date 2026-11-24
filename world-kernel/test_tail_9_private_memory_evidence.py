"""Tail 9: in private scope an extracted item is `verified` only if the cited owner line(s) actually support it.

The check belongs to trust levels, which are off by default: every store here is made with `AI_HOUSE_MEMORY_TRUST=1`.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.memory_common import CommonMemoryStore as MemoryStore  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
TRUST_ON = {"AI_HOUSE_MEMORY_TRUST": "1"}  # the evidence check on what an extraction may store is part of trust levels


class PrivateMemoryEvidenceTests(unittest.TestCase):
    def _run_private(self, owner_lines: List[str], op: Dict[str, Any], *, reply: str = "") -> tuple:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = VirtualClock(dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp())
        st = MemoryStore(
            tmp.name,
            clock=clock,
            names={"elsa": "艾莎"},
            llm_fn=lambda _s, _u: {"ops": [op]},
            env=TRUST_ON,
            extract_every_s=60,
            extract_batch=10,
        )
        self.addCleanup(st.close)
        for i, line in enumerate(owner_lines):
            st.observe("elsa", "private", line, speaker="房主", speaker_role="owner", reply=reply if i == 0 else "")
        res = st.extract("elsa", scope="private", force=True)
        return st, res, st.list_entries("elsa", "private")

    def _assert_verified(self, owner_lines: List[str], text: str, src: List[int], kind: str = "fact") -> None:
        st, res, rows = self._run_private(owner_lines, {"op": "add", "kind": kind, "text": text, "src": src})
        self.assertEqual((res["added"], len(rows)), (1, 1), (text, res))
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), (kind, "verified"), (text, owner_lines))
        self.assertNotIn("未核实", st.recall("elsa", "private", text))

    def _assert_downgraded(self, owner_lines: List[str], text: str, src: List[int], kind: str = "fact") -> None:
        st, res, rows = self._run_private(owner_lines, {"op": "add", "kind": kind, "text": text, "src": src})
        self.assertEqual((res["added"], len(rows)), (1, 1), (text, res))  # kept, not silently dropped
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("claim", "group_stated"), (text, owner_lines))
        self.assertEqual(rows[0]["scope"], "private")
        recalled = st.recall("elsa", "private", text)
        self.assertIn("推断·未核实", recalled)
        self.assertNotIn("群聊陈述", recalled)  # nobody but the owner speaks in a private room

    # ---- the reproduced defect ------------------------------------------------------------------

    def test_fabricated_fact_citing_unrelated_owner_line_is_not_verified(self) -> None:
        self._assert_downgraded(["我喜欢吃草莓蛋糕"], "房主对海鲜过敏", [1])

    def test_fabricated_variants_are_not_verified(self) -> None:
        for text in ("房主对花生过敏", "房主喜欢吃海鲜", "房主养了一只狗", "房主的生日是3月6日"):
            with self.subTest(text=text):
                self._assert_downgraded(["我喜欢吃草莓蛋糕"], text, [1])
        self._assert_downgraded(["我对花生过敏"], "房主对虾过敏", [1])  # same pattern, different allergen
        self._assert_downgraded(["我生日是3月5日"], "房主的生日是3月6日", [1])

    # ---- legitimate paraphrases stay verified ---------------------------------------------------

    def test_paraphrases_of_the_cited_line_stay_verified(self) -> None:
        for text in (
            "房主喜欢吃草莓蛋糕",
            "房主爱吃草莓蛋糕",
            "喜欢草莓蛋糕",
            "房主非常喜欢吃草莓蛋糕（11月22日）",
            "2026年11月22日，房主喜欢吃草莓蛋糕",
        ):
            with self.subTest(text=text):
                self._assert_verified(["我喜欢吃草莓蛋糕"], text, [1], kind="preference")
        self._assert_verified(["我对花生过敏"], "房主对花生过敏", [1])
        self._assert_verified(["我生日是3月5日"], "房主的生日是3月5日", [1])
        self._assert_verified(["我住东京"], "房主住在东京", [1])
        # the roommate's own name stands in for the owner's 「你」
        self._assert_verified(["周末的抹茶粉我私下给你买好宇治款啦"], "房主私下为艾莎买好了宇治抹茶粉", [1], kind="event")

    # ---- negation ---------------------------------------------------------------------------------

    def test_negation_flip_is_not_verified_but_same_polarity_is(self) -> None:
        self._assert_downgraded(["我不吃香菜"], "房主爱吃香菜", [1], kind="preference")
        self._assert_downgraded(["我喜欢吃香菜"], "房主不吃香菜", [1], kind="preference")
        self._assert_verified(["我不吃香菜"], "房主不吃香菜", [1], kind="preference")
        self._assert_verified(["我不吃香菜但是爱吃芹菜"], "房主爱吃芹菜", [1], kind="preference")

    # ---- multi-line src ---------------------------------------------------------------------------

    def test_multi_line_src_pools_cited_lines_only(self) -> None:
        lines = ["我喜欢吃草莓蛋糕", "我也很爱抹茶", "明天天气不错"]
        self._assert_verified(lines, "房主喜欢草莓蛋糕和抹茶", [1, 2])
        self._assert_verified(lines, "房主爱抹茶", [2])
        self._assert_downgraded(lines, "房主爱抹茶", [1])  # the matching line (2) is not cited
        self._assert_downgraded(lines, "房主对海鲜过敏", [1, 2, 3])  # citing everything proves nothing

    # ---- regressions: existing behaviour unchanged ------------------------------------------------

    def test_item_citing_only_roommate_own_line_is_still_rejected(self) -> None:
        # line 1 = owner, line 2 = the roommate's own reply
        st, res, rows = self._run_private(
            ["我喜欢吃草莓蛋糕"],
            {"op": "add", "kind": "fact", "text": "房主对海鲜过敏", "src": [2]},
            reply="那你是不是对海鲜过敏呀",
        )
        self.assertEqual((res["added"], res["rejected"], rows), (0, 1, []))
        # control: the same text citing the owner's own line is stored
        _st, res, rows = self._run_private(
            ["我对海鲜过敏"],
            {"op": "add", "kind": "fact", "text": "房主对海鲜过敏", "src": [1]},
            reply="那你是不是对海鲜过敏呀",
        )
        self.assertEqual((res["added"], res["rejected"], len(rows)), (1, 0, 1))

    def test_group_scope_is_unchanged(self) -> None:
        def make(op: Dict[str, Any]) -> MemoryStore:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            clock = VirtualClock(dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp())
            st = MemoryStore(tmp.name, clock=clock, llm_fn=lambda _s, _u: {"ops": [op]}, env=TRUST_ON,
                             extract_every_s=60, extract_batch=10)
            self.addCleanup(st.close)
            return st

        st = make({"op": "add", "kind": "preference", "text": "房主喜欢抹茶泡芙", "src": [1]})
        st.observe("elsa", "group", "我挺喜欢吃抹茶泡芙的", channel="g", speaker="房主", speaker_role="owner")
        st.extract("elsa", scope="group", channel="g", force=True)
        row = st.list_entries("elsa", "group", channel="g")[0]
        self.assertEqual((row["kind"], row["trust"]), ("preference", "verified"))

        st = make({"op": "add", "kind": "fact", "text": "合租屋周六晚上聚餐准备吃寿喜锅", "src": [1]})
        st.observe("elsa", "group", "大家周六晚上聚餐好像要吃寿喜锅", channel="g", speaker="群友甲", speaker_role="member")
        st.extract("elsa", scope="group", channel="g", force=True)
        row = st.list_entries("elsa", "group", channel="g")[0]
        self.assertEqual((row["kind"], row["trust"]), ("claim", "group_stated"))
        self.assertIn("未核实", st.recall("elsa", "group", "周六晚上聚餐吃寿喜锅", channel="g"))


if __name__ == "__main__":
    unittest.main()
