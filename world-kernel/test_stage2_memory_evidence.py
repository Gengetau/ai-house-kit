"""Stage 2.4 tests: memory source evidence gating, group claim downgrade & outbound leak guard."""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.memory import JST, MemoryStore  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402


class Stage2MemoryEvidenceTests(unittest.TestCase):
    def _make_store(self, llm_fn=None) -> MemoryStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        clock = VirtualClock(t0)
        st = MemoryStore(
            tmp.name,
            clock=clock,
            names={"sora": "苍空", "rin": "凛音"},
            llm_fn=llm_fn,
            extract_every_s=60,
            extract_batch=10,
        )
        self.addCleanup(st.close)
        return st

    def test_pure_question_plus_self_guess_is_rejected_by_evidence_gate(self) -> None:
        def guessing_llm(_sys: str, _usr: str) -> Dict[str, Any]:
            return {
                "ops": [
                    {
                        "op": "add",
                        "text": "房主可能对花生或芒果过敏",
                        "about": ["房主"],
                        "kind": "fact",
                        "src": [2],  # only points to roommate's own guess
                    },
                    {
                        "op": "add",
                        "text": "房主大概对花生或芒果过敏吧",
                        "about": ["房主"],
                        "kind": "fact",
                        "src": [1, 2],  # [1] is a pure question; entity only comes from self guess
                    },
                    {
                        "op": "add",
                        "text": "房主可能对花生过敏",
                        "about": ["房主"],
                        "kind": "claim",
                        "src": [1],  # pure question cannot become claim either
                    },
                ]
            }

        st = self._make_store(llm_fn=guessing_llm)
        # Reproduces Trial Issue #4: member asks question, bot guesses allergy
        st.observe(
            "sora",
            "group",
            "房主有什么过敏或者忌口吗？",
            channel="qq-group:9001",
            speaker="群友阿晴",
            speaker_role="member",
            speaker_id="8002",
            reply="我不太确定，好像可能是花生或者芒果有点过敏吧？",
        )
        res = st.extract("sora", scope="group", channel="qq-group:9001", force=True)
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["rejected"], 3)
        self.assertEqual(st.list_entries("sora", "group", channel="qq-group:9001"), [])

    def test_group_member_claim_downgraded_and_ranked_below_verified(self) -> None:
        def claim_llm(_sys: str, _usr: str) -> Dict[str, Any]:
            return {
                "ops": [
                    {
                        "op": "add",
                        "text": "合租屋周六晚上聚餐准备吃寿喜锅",
                        "about": ["聚餐"],
                        "kind": "fact",
                        "src": [1],
                    }
                ]
            }

        st = self._make_store(llm_fn=claim_llm)
        st.observe(
            "sora",
            "group",
            "大家周六晚上聚餐好像要吃寿喜锅，我听隔壁说的",
            channel="group",
            speaker="群友甲",
            speaker_role="member",
            speaker_id="8003",
        )
        res = st.extract("sora", scope="group", channel="group", force=True)
        self.assertEqual(res["added"], 1)

        entries = st.list_entries("sora", "group", channel="group")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "claim")
        self.assertEqual(entries[0]["trust"], "group_stated")

        # Add an owner-verified memory in group about the same topic with slightly different wording
        st.add("sora", "group", "房主确认周六晚上聚餐主菜吃寿喜锅", channel="group", kind="fact", trust="verified")
        ranked = st.search("sora", "group", "周六晚上聚餐吃寿喜锅", channel="group")
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][1]["trust"], "verified")
        self.assertEqual(ranked[1][1]["trust"], "group_stated")

        recalled = st.recall("sora", "group", "周六晚上聚餐吃寿喜锅", channel="group")
        self.assertIn("[群聊陈述·未核实", recalled)

    def test_owner_claim_stays_verified_and_paraphrased_private_allergy_leak_blocked(self) -> None:
        st = self._make_store(
            llm_fn=lambda _s, _u: {
                "ops": [
                    {
                        "op": "add",
                        "text": "房主在群里说过喜欢抹茶泡芙",
                        "kind": "preference",
                        "src": [1],
                    }
                ]
            }
        )
        # Owner statement in group keeps verified trust
        st.observe("sora", "group", "我挺喜欢吃抹茶泡芙的", channel="group", speaker="房主", speaker_role="owner")
        st.extract("sora", scope="group", channel="group", force=True)
        grp_rows = st.list_entries("sora", "group", channel="group")
        self.assertEqual(grp_rows[0]["kind"], "preference")
        self.assertEqual(grp_rows[0]["trust"], "verified")

        # Private allergy line even paraphrased across shorter sentence triggers outbound leak check
        st.observe("sora", "private", "我其实对虾仁和荞麦有点过敏，先别外传", speaker="房主", speaker_role="owner")
        self.assertTrue(st.check_outbound_leak("sora", "group", "点菜注意别放虾仁，可能会过敏"))
        self.assertEqual(st.check_outbound_leak("sora", "group", "晚上大家要不要一起看电影"), [])


if __name__ == "__main__":
    unittest.main()
