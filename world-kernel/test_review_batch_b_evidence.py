"""PR-B tests: group owner-evidence binding, conflict suppression & commitment rigor (B1, B2, B4-B6).

The old B3 (schema v3 migration of the old memory.db) is gone with that schema. The trust levels in B1/B2 are the
shared module's, which are off unless `AI_HOUSE_MEMORY_TRUST=1`.
B4-B6 now make their promises through memory extraction with a fake model (the chat turn records nothing) and close
them, or fail to close them, with rows of the kernel's evidence stream.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.daemon import HouseDaemon, JST  # noqa: E402
from ai_house.memory import MemoryStore  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402


NAMES = {"sora": "苍空", "rin": "凛音"}


def _owner_asks(d: HouseDaemon, actor: str, line: str, text: str, now: float) -> None:
    """The owner says `line` to `actor` in private and she answers with a plain yes; then one forced memory extraction
    runs with a fake model whose only op is the promise `text`. That is the only way a promise comes to exist."""
    d.memory.observe(actor, "private", line, speaker_role="owner", speaker="房主", reply="好呀", now=now)
    ops = [{"op": "intent", "text": text, "kind": "promise", "for": ["房主"], "agreed": [NAMES[actor]],
            "scope": "world", "when": "vague", "quote": line}]
    d.memory.extract(force=True, now=now + 2, llm_fn=lambda system, user: {"ops": ops})


def _add_evidence(d: HouseDaemon, key: str, event_id: int, at: float, doc: Dict[str, Any]) -> None:
    """A row in the kernel's evidence stream (the promise code reads only the bound stream; rows carry key and stream)."""
    with contextlib.closing(sqlite3.connect(d.world_db)) as c:
        with c:
            stream = c.execute("SELECT v FROM kernel_bind WHERE k='world'").fetchone()[0]
            c.execute("INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, ?, ?, ?, ?)",
                      (key, stream, event_id, at, json.dumps({**doc, "key": key, "stream": stream, "at": at})))


def _receipt(errand: Dict[str, Any], actor: str, at: float, n: int) -> Dict[str, Any]:
    """The purchase receipt the kernel writes for one errand as `errands()` described it."""
    return {"kind": "purchased", "actor": actor, "item": errand["item"], "qty": errand["qty"],
            "intent_ref": errand["intent"], "account": errand["account"], "beneficiary": errand["beneficiary"],
            "fingerprint": errand["fingerprint"], "action_id": f"act-{n}", "resource_id": f"res-{n}"}


def _states(d: HouseDaemon, actor: str) -> Dict[str, str]:
    """state of every promise of `actor`, by intent id (a private promise's text is hidden in the listing)."""
    return {r["intent_id"]: r["state"] for r in d.intents.list_intents(actor=actor)}


class ReviewBatchBEvidenceTests(unittest.TestCase):
    def _make_memory(self, llm_fn=None, env=None) -> Tuple[Path, MemoryStore, VirtualClock]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        clock = VirtualClock(t0)
        st = MemoryStore(tmp.name, clock=clock, names={"sora": "苍空", "rin": "凛音"}, llm_fn=llm_fn,
                         env={"AI_HOUSE_MEMORY_TRUST": "1", **(env or {})})
        self.addCleanup(st.close)
        return Path(tmp.name), st, clock

    def test_b1_group_owner_evidence_binding_rejects_omitted_src_or_member_import(self) -> None:
        def batch_llm(_sys: str, _usr: str) -> Dict[str, Any]:
            return {
                "ops": [
                    # 1) Omits src while batch contains owner line L1 + member claim L2 -> must downgrade!
                    {
                        "op": "add",
                        "text": "苍空讨厌吃胡萝卜",
                        "kind": "preference",
                    },
                    # 2a) Cites owner line L1 in src, but steals member-only claim "胡萝卜" -> must downgrade!
                    {
                        "op": "add",
                        "text": "房主出门时听说苍空讨厌吃胡萝卜",
                        "kind": "fact",
                        "src": [1],
                    },
                    # 2b) Same, with more of L1's own words around the stolen claim -> must downgrade too!
                    {
                        "op": "add",
                        "text": "房主明天要去京都出差，苍空讨厌吃胡萝卜",
                        "kind": "fact",
                        "src": [1],
                    },
                    # 3) Explicitly cites owner statement L1 with lexical alignment and no member words -> verified!
                    {
                        "op": "add",
                        "text": "房主明天要去京都出差",
                        "kind": "fact",
                        "src": [1],
                    },
                    # 4) Group rule without owner evidence -> rejected by gate
                    {
                        "op": "add",
                        "text": "以后群里禁止提胡萝卜",
                        "kind": "rule",
                    },
                ]
            }

        _, st, _ = self._make_memory(llm_fn=batch_llm)
        st.observe("sora", "group", "我明天要去京都出差啦", channel="group", speaker="房主", speaker_role="owner")
        st.observe("sora", "group", "顺便说苍空好像讨厌吃胡萝卜", channel="group", speaker="群友甲", speaker_role="member")
        res = st.extract("sora", scope="group", channel="group", force=True)
        self.assertEqual(res["added"], 4)
        self.assertEqual(res["rejected"], 1)

        rows = st.list_entries("sora", "group", channel="group")
        by_text = {r["text"]: r for r in rows}
        self.assertEqual(len(rows), 4)
        self.assertNotIn("以后群里禁止提胡萝卜", by_text)
        self.assertEqual(by_text["苍空讨厌吃胡萝卜"]["kind"], "claim")
        self.assertEqual(by_text["苍空讨厌吃胡萝卜"]["trust"], "group_stated")
        self.assertEqual(by_text["房主出门时听说苍空讨厌吃胡萝卜"]["kind"], "claim")
        self.assertEqual(by_text["房主出门时听说苍空讨厌吃胡萝卜"]["trust"], "group_stated")
        self.assertEqual(by_text["房主明天要去京都出差，苍空讨厌吃胡萝卜"]["kind"], "claim")
        self.assertEqual(by_text["房主明天要去京都出差，苍空讨厌吃胡萝卜"]["trust"], "group_stated")
        self.assertEqual(by_text["房主明天要去京都出差"]["kind"], "fact")
        self.assertEqual(by_text["房主明天要去京都出差"]["trust"], "verified")
        self.assertEqual(by_text["房主明天要去京都出差"]["source"], "auto:owner_verified")

    def test_b2_conflict_suppression_ranks_old_verified_above_fresh_conflicting_claim(self) -> None:
        _, st, clock = self._make_memory()
        old_ts = clock.now() - 60 * 86400
        fresh_ts = clock.now()

        # 60-day-old verified allergy fact
        st.add(
            "sora",
            "group",
            "房主对海鲜和虾仁严重过敏，绝对不能吃",
            channel="group",
            about=["房主", "海鲜"],
            kind="fact",
            trust="verified",
            source="owner_verified",
            now=old_ts,
        )
        # Fresh conflicting group claim
        st.add(
            "sora",
            "group",
            "群友说房主最爱吃海鲜和麻辣大虾",
            channel="group",
            about=["房主", "海鲜"],
            kind="claim",
            trust="group_stated",
            source="auto",
            now=fresh_ts,
        )

        # Even with min_score=0.05 to inspect ranking, verified beats conflicting fresh claim by a wide margin
        ranked = st.search("sora", "group", "房主吃海鲜和虾怎么样", channel="group", min_score=0.05, k=5)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][1]["trust"], "verified")
        self.assertEqual(ranked[1][1]["trust"], "group_stated")
        self.assertTrue(ranked[1][1].get("_conflict_verified"))
        self.assertGreater(ranked[0][0], ranked[1][0] * 1.5)

        # Default recall surfaces verified fact and keeps conflicting claim out or warning-tagged below
        recalled = st.recall("sora", "group", "房主能吃海鲜虾仁吗", channel="group", now=fresh_ts)
        self.assertIn("严重过敏", recalled)
        # the conflicting claim is found by search above, but its pushed-down score keeps it out of the recall block
        self.assertNotIn("麻辣大虾", recalled)

    def _house(self, t0: float) -> HouseDaemon:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        return d

    def test_b4_out_of_catalog_errand_stays_open_and_ignores_unrelated_purchases(self) -> None:
        """Fails if a promise to buy things the shop does not sell (a durian, batteries) is offered to the kernel as an
        errand, or is closed by a purchase that belongs to another errand (an onion, or the egg errand's receipt), or
        if the control breaks: the egg errand in the same house is listed as an errand and its own receipt does close
        it. (Reminder rows for such items and the `record_promise` tool are gone, dropped items D4 and D2; the
        "买不了" reply tail is tested in test_realrun_1_promises.)"""
        t0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        d = self._house(t0)
        _owner_asks(d, "sora", "今天帮忙买一个榴莲回来吧", "苍空买一个榴莲", t0)
        _owner_asks(d, "sora", "明天记得顺路买两节电池", "苍空买两节电池", t0 + 10)
        _owner_asks(d, "sora", "顺便帮家里买2个鸡蛋", "苍空买2个鸡蛋", t0 + 20)
        before = _states(d, "sora")
        self.assertEqual(len(before), 3)
        self.assertEqual(set(before.values()), {"open"})
        errands = d.intents.errands("sora", t0 + 60)
        self.assertEqual([(e["item"], e["qty"]) for e in errands], [("egg", 2)])  # only the egg is the kernel's to buy
        egg = errands[0]

        # Purchasing an unrelated catalog item (onion) MUST NOT close any of them
        _add_evidence(d, "purchased:onion:1", 1, t0 + 120,
                      {"kind": "purchased", "actor": "sora", "item": "onion", "qty": 2})
        d.intents.sweep(t0 + 130)
        self.assertEqual(_states(d, "sora"), before)

        # control: the egg errand's own receipt closes the egg and only the egg
        _add_evidence(d, "purchased:egg:1", 2, t0 + 140, _receipt(egg, "sora", t0 + 140, 1))
        d.intents.sweep(t0 + 150)
        after = _states(d, "sora")
        self.assertEqual(after[str(egg["intent"])], "completed")
        self.assertEqual(sorted(v for k, v in after.items() if k != str(egg["intent"])), ["open", "open"])
        self.assertEqual(d.intents.errands("sora", t0 + 160), [])

    def test_b5_private_promises_never_merge_across_residents(self) -> None:
        """Fails if the same owner request made to two residents in private ends up as one promise, if either
        promise's party holds the other resident, or if a receipt for one resident's errand closes (or counts for)
        the other's. Control: the receipt does close the resident it belongs to. (`plan_ref` merging is gone, dropped
        item D1; the guarantee is now kept by the promise row being per resident.)"""
        t0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        d = self._house(t0)
        _owner_asks(d, "sora", "顺便帮家里买2个鸡蛋", "苍空买2个鸡蛋", t0)
        _owner_asks(d, "rin", "顺便帮家里买2个鸡蛋", "凛音买2个鸡蛋", t0 + 1)

        sora_rows = d.intents.list_intents(actor="sora")
        rin_rows = d.intents.list_intents(actor="rin")
        self.assertEqual(len(sora_rows), 1)
        self.assertEqual(len(rin_rows), 1)
        self.assertNotEqual(sora_rows[0]["intent_id"], rin_rows[0]["intent_id"])
        self.assertEqual(sora_rows[0]["party"], ["sora"])
        self.assertEqual(rin_rows[0]["party"], ["rin"])
        self.assertEqual(sora_rows[0]["visibility"], "private")
        sora_errand = d.intents.errands("sora", t0 + 60)[0]
        rin_errand = d.intents.errands("rin", t0 + 60)[0]
        self.assertEqual((sora_errand["account"], rin_errand["account"]), ("sora", "rin"))

        # Completing sora's private promise by her receipt leaves rin's open
        _add_evidence(d, "purchased:egg:sora", 11, t0 + 60, _receipt(sora_errand, "sora", t0 + 60, 1))
        d.intents.sweep(t0 + 70)
        self.assertEqual(d.intents.list_intents(actor="sora")[0]["state"], "completed")
        self.assertEqual(d.intents.list_intents(actor="rin")[0]["state"], "open")
        self.assertEqual(len(d.intents.errands("rin", t0 + 80)), 1)

        # and a receipt made out to rin's errand but carrying sora's name does not close hers either
        stray = dict(_receipt(rin_errand, "sora", t0 + 90, 2))
        _add_evidence(d, "purchased:egg:stray", 12, t0 + 90, stray)
        d.intents.sweep(t0 + 100)
        self.assertEqual(d.intents.list_intents(actor="rin")[0]["state"], "open")
        _add_evidence(d, "purchased:egg:rin", 13, t0 + 110, _receipt(rin_errand, "rin", t0 + 110, 3))  # control
        d.intents.sweep(t0 + 120)
        self.assertEqual(d.intents.list_intents(actor="rin")[0]["state"], "completed")

    def test_b6_wrong_place_departure_and_sleep_dialogue_never_false_close(self) -> None:
        """Fails if a departure alone, an arrival at the wrong place, or a script line about sleeping closes a
        promise, or if the promise to sleep early (words the kernel cannot verify) is closed by anything the kernel
        writes, even a real sleep activity. Control in the same scenario: a real arrival at the library closes the
        library promise. (Kind-based closing, the 'outing' / 'chat_promise' kinds and the fixed 24 h life are gone,
        dropped items D1 and D6; the old case also expected the structured sleep activity to close the sleep
        promise, which the shared rules do not do: words they cannot compile stay open until people say so or time
        runs out.)"""
        t0 = dt.datetime(2026, 11, 22, 15, 0, tzinfo=JST).timestamp()
        d = self._house(t0)
        _owner_asks(d, "sora", "下午去图书馆吧", "苍空去图书馆", t0)
        _owner_asks(d, "sora", "今晚早点睡哦", "苍空今晚早点睡", t0 + 10)
        before = _states(d, "sora")
        self.assertEqual(len(before), 2)
        self.assertEqual(set(before.values()), {"open"})

        # 1) departed alone + arrived at supermarket (wrong place) + script dialogue mentioning "睡" must NOT close either!
        _add_evidence(d, "departed:1", 20, t0 + 30, {"kind": "departed", "actor": "sora", "place": "place:library",
                                                    "plan_id": "plan-lib"})
        _add_evidence(d, "arrived:market", 21, t0 + 40, {"kind": "arrived", "actor": "sora", "place": "place:supermarket",
                                                        "plan_id": "plan-lib"})
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(script)").fetchall()}
                if "created" in cols:
                    c.execute(
                        "INSERT INTO script(ts, actors, text, created, source) VALUES(?, ?, ?, ?, 'kernel')",
                        (t0 + 50, json.dumps(["sora"]), "我现在还不困，先不想睡觉呢。", t0 + 50),
                    )
                else:
                    c.execute(
                        "INSERT INTO script(ts, actors, text, source) VALUES(?, ?, ?, 'kernel')",
                        (t0 + 50, json.dumps(["sora"]), "我现在还不困，先不想睡觉呢。"),
                    )
        d.intents.sweep(t0 + 60)
        self.assertEqual(_states(d, "sora"), before)

        # 2) Real arrival at the library closes the library promise; a structured sleep activity closes nothing
        _add_evidence(d, "arrived:lib", 22, t0 + 120, {"kind": "arrived", "actor": "sora", "place": "place:library",
                                                       "plan_id": "plan-lib"})
        _add_evidence(d, "act:sleep", 23, t0 + 180,
                      {"kind": "activity", "actor": "sora", "activity": "sleep", "place": "room_sora", "outcome": "succeeded",
                       "started_at": t0 + 100, "ended_at": t0 + 180, "plan_id": "plan-sleep"})
        d.intents.sweep(t0 + 190)
        done = _states(d, "sora")
        lib_id, sleep_id = sorted(before, key=int)  # the promises were made in this order
        self.assertEqual(done, {lib_id: "completed", sleep_id: "open"})


if __name__ == "__main__":
    unittest.main()
