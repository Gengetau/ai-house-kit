"""Tail batch 2 regressions: buy-request parsing, commitment evidence windows, memory trust and conflicts (trust levels on).

Each section pairs positive cases (must still work) with negative ones (the acceptance-round bugs).
Evidence rows are written exactly as the kernel exporter writes them; no test touches the matcher tables.
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
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.memory_common import CommonMemoryStore as MemoryStore  # noqa: E402
from worldkernel.intents_lite import CommitmentStore  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
H = 3600.0
GROUP = "qq-group:5001"  # the old tests' channel "g1"
TRUST_ON = {"AI_HOUSE_MEMORY_TRUST": "1"}  # trust levels, the claim kind and conflict flags are off unless switched on


class _IntentBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "world.db")
        self.store = CommitmentStore(self.db)
        self.store.init()
        self._n = 0

    def evidence(self, body: Dict[str, Any]) -> None:
        self._n += 1
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', ?, ?, ?)",
                    (f"k{self._n}", self._n, float(body.get("ended_at") or body["at"]), json.dumps(body)),
                )

    def rows(self, actor: str = "sora") -> List[Dict[str, Any]]:
        return self.store.list_intents(actor=actor)


class BuyRequestParsing(_IntentBase):
    # (owner text, resident reply, expected raw_item) -- explicit requests / errands outside the catalog
    SHOULD_CREATE = [
        ("帮我买点榴莲", "好的", "榴莲"),
        ("顺便带两瓶酱油", "好", "酱油"),
        ("记得买两节电池", "好呀", "电池"),
        ("麻烦你去超市买点洗洁精", "嗯", "洗洁精"),
        ("明天去便利店买个榴莲回来吧", "好", "榴莲"),
        ("去买点榴莲吧", "好", "榴莲"),
        ("别忘了买洗洁精", "好的", "洗洁精"),
        ("顺路帮忙带一包葱", "收到", "葱"),
        ("能不能帮我买一盒抹茶粉？", "可以", "抹茶粉"),
        ("今天顺便帮我买 2 个榴莲回来", "好，我去买", "榴莲"),
        # resident explicitly promises the purchase
        ("榴莲吃完了呢", "那我这就去买榴莲", "榴莲"),
        ("买点榴莲吧", "好，我去买", "榴莲"),
        # second-review cases: verb complements, bare measures, cross-clause markers
        ("帮我买一下榴莲", "好", "榴莲"),
        ("帮我买下榴莲", "好", "榴莲"),
        ("帮我买下午茶", "好", "下午茶"),
        ("顺路带个榴莲", "好", "榴莲"),
        ("帮我带两杯奶茶回来", "好", "奶茶"),
        ("麻烦带杯热可可上来", "好", "热可可"),
        ("路过便利店，捎份报纸", "好", "报纸"),
        ("帮我买包子", "好", "包子"),
        ("帮我买个包", "好", "包"),
        ("帮我买根香草荚", "好", "香草荚"),
    ]
    SHOULD_NOT_CREATE = [
        "我今天买了榴莲回来",
        "你买了吗",
        "为什么买这个",
        "最近缺钱",
        "他说缺乏锻炼",
        "我最近缺少睡眠",
        "我最近缺觉",
        "买买买",
        "不用买榴莲",
        "别买榴莲了",
        "昨天买过榴莲",
        "买不起榴莲",
        "你买了榴莲吗",
        "你有没有买榴莲",
        "我想买榴莲",
        "榴莲好贵，谁买啊",
        "他说我缺点很多",
        "我今天买了牛奶回来",
        "你买了牛奶吗",
        "不用买牛奶",
        # second-review cases: these used to create errands for "卖", "下", "瓶", "下路", "单", "下孩子", "伞"
        "你再给我讲讲买卖",
        "你帮忙买一下",
        "酱油用完了，麻烦买一瓶",
        "能不能带一下路",
        "可以买单吗",
        "帮忙买个单",
        "帮我带一下孩子",
        "请带把伞",
        "麻烦带个话给小林",
        "帮我捎个口信",
        "请带个头",
        "帮我买个教训",
        # nobody is asked: a subject-less sentence may just as well be the owner's own plan
        "下班带杯奶茶",
        "明天买点榴莲",
    ]

    def _errands(self, actor: str = "sora") -> List[Dict[str, Any]]:
        return [r for r in self.rows(actor) if r["kind"] == "errand"]

    def test_requests_and_commitments_become_open_errands_with_raw_item(self) -> None:
        for i, (text, reply, expected) in enumerate(self.SHOULD_CREATE):
            with self.subTest(text=text):
                actor = f"a{i}"
                self.store.enqueue_chat(actor, text, reply, where="private", now=T0)
                errs = self._errands(actor)
                self.assertEqual(len(errs), 1, errs)
                self.assertEqual(errs[0]["state"], "open")
                self.assertEqual(errs[0]["raw_item"], expected)
                self.assertFalse(errs[0]["item"])

    def test_catalog_request_still_resolves_to_item(self) -> None:
        self.store.enqueue_chat("sora", "帮我买点牛奶", "好", where="private", now=T0)
        errs = self._errands()
        self.assertEqual([(e["item"], e["raw_item"]) for e in errs], [("milk", "")])

    def test_chitchat_never_becomes_an_errand(self) -> None:
        for i, text in enumerate(self.SHOULD_NOT_CREATE):
            with self.subTest(text=text, path="no_llm_intents"):
                actor = f"n{i}"
                self.store.enqueue_chat(actor, text, "嗯", where="private", now=T0)
                self.assertEqual(self._errands(actor), [])
            with self.subTest(text=text, path="llm_intents_present"):
                actor = f"m{i}"
                self.store.enqueue_chat(
                    actor, text, "嗯", where="private",
                    intents=[{"text": "和房主聊天", "kind": "chat_promise"}], now=T0,
                )
                self.assertEqual(self._errands(actor), [])

    def test_particles_are_stripped_from_raw_item(self) -> None:
        for i, (text, reply, expected) in enumerate(self.SHOULD_CREATE):
            self.store.enqueue_chat(f"p{i}", text, reply, where="private", now=T0)
            for r in self._errands(f"p{i}"):
                self.assertNotRegex(r["raw_item"], r"^[了吗吧呢啊的这]|[了吗吧呢啊回来]$")


class EvidenceWindowAndStrength(_IntentBase):
    def _outing(self, text: str = "下午去图书馆借书吧", summary: str = "去图书馆借书") -> None:
        self.store.enqueue_chat(
            "sora", text, "好呀", where="private",
            intents=[{"text": summary, "kind": "outing", "答应了的": ["sora"]}], now=T0,
        )

    def _together(self) -> None:
        self.store.enqueue_chat(
            "sora", "一起大扫除吧", "好呀", where="private",
            intents=[{"text": "一起大扫除", "kind": "together", "答应了的": ["sora"]}], now=T0,
        )

    def _state(self) -> str:
        return self.rows()[0]["state"]

    # --- invite_settled / plan_finished strength
    def test_invite_settled_does_not_close_outing_or_together(self) -> None:
        self._outing()
        self._together()
        self.evidence({"kind": "invite_settled", "actor": "sora", "members": ["sora", "rin"],
                       "place": "library", "at": T0 + 60})
        self.store.sweep(T0 + 120)
        self.assertEqual([r["state"] for r in self.rows()], ["open", "open"])

    def test_plan_finished_only_closes_on_successful_outcome(self) -> None:
        for outcome in ("cancelled", "lapsed", "missed", None, "bogus"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self._together()
                self._outing()
                body = {"kind": "plan_finished", "actor": "sora", "at": T0 + 60}
                if outcome is not None:
                    body["outcome"] = outcome
                self.evidence(body)
                self.store.sweep(T0 + 120)
                self.assertEqual([r["state"] for r in self.rows()], ["open", "open"])
        for outcome in ("done", "early_home"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self._together()
                self.evidence({"kind": "plan_finished", "actor": "sora", "outcome": outcome,
                               "members": ["sora", "rin"], "at": T0 + 60})
                self.store.sweep(T0 + 120)
                self.assertEqual(self._state(), "completed")

    def test_together_plan_finished_needs_more_than_one_member_when_known(self) -> None:
        self._together()
        self.evidence({"kind": "plan_finished", "actor": "sora", "outcome": "done",
                       "members": ["sora"], "at": T0 + 60})
        self.store.sweep(T0 + 120)
        self.assertEqual(self._state(), "open")

    def test_together_closes_on_real_shared_activity_only(self) -> None:
        self._together()
        # standalone action (own group) and failed/cancelled activity do not count
        self.evidence({"kind": "activity", "actor": "sora", "activity": "housework", "outcome": "succeeded",
                       "plan_id": "action:a1", "ended_at": T0 + 2 * H, "at": T0 + 2 * H})
        self.evidence({"kind": "activity", "actor": "sora", "activity": "housework", "outcome": "cancelled",
                       "plan_id": "plan:p1", "ended_at": T0 + 2 * H, "at": T0 + 2 * H})
        self.evidence({"kind": "activity", "actor": "sora", "activity": "gaming", "outcome": "succeeded",
                       "plan_id": "plan:p2", "ended_at": T0 + 2 * H, "at": T0 + 2 * H})
        self.store.sweep(T0 + 3 * H)
        self.assertEqual(self._state(), "open")
        self.evidence({"kind": "activity", "actor": "sora", "activity": "housework", "outcome": "succeeded",
                       "plan_id": "plan:p1", "ended_at": T0 + 2 * H, "at": T0 + 2 * H})
        self.store.sweep(T0 + 3 * H)
        self.assertEqual(self._state(), "completed")

    def test_outing_arrival_at_named_place_still_closes(self) -> None:
        self._outing()
        self.evidence({"kind": "arrived", "actor": "sora", "place": "library", "at": T0 + 60})
        self.store.sweep(T0 + 120)
        self.assertEqual(self._state(), "completed")

    def test_cook_plan_finished_cancelled_does_not_close(self) -> None:
        self.store.enqueue_chat("sora", "晚上做饭吧", "好", where="private",
                                intents=[{"text": "晚上做饭", "kind": "cook", "答应了的": ["sora"]}], now=T0)
        self.evidence({"kind": "plan_finished", "actor": "sora", "plan_kind": "cook",
                       "outcome": "cancelled", "at": T0 + 60})
        self.store.sweep(T0 + 120)
        self.assertEqual(self._state(), "open")
        self.evidence({"kind": "plan_finished", "actor": "sora", "plan_kind": "cook",
                       "outcome": "done", "at": T0 + 90})
        self.store.sweep(T0 + 120)
        self.assertEqual(self._state(), "completed")

    # --- time window
    def test_errand_evidence_after_expiry_does_not_complete(self) -> None:
        self.store.enqueue_chat("sora", "记得买洋葱", "好", where="private", now=T0)
        self.evidence({"kind": "purchased", "actor": "sora", "item": "onion", "qty": 2, "at": T0 + 30 * H})
        self.store.sweep(T0 + 31 * H)
        self.assertEqual(self._state(), "expired")

    def test_errand_evidence_inside_window_completes_even_if_swept_late(self) -> None:
        self.store.enqueue_chat("sora", "记得买洋葱", "好", where="private", now=T0)
        self.evidence({"kind": "purchased", "actor": "sora", "item": "onion", "qty": 2, "at": T0 + 3 * H})
        self.store.sweep(T0 + 31 * H)
        self.assertEqual(self._state(), "completed")

    def test_evidence_before_creation_is_ignored(self) -> None:
        self.evidence({"kind": "purchased", "actor": "sora", "item": "onion", "qty": 2, "at": T0 - 2 * H})
        self.store.enqueue_chat("sora", "记得买洋葱", "好", where="private", now=T0)
        self.store.sweep(T0 + H)
        self.assertEqual(self._state(), "open")

    def test_outing_arrival_after_expiry_does_not_complete(self) -> None:
        self._outing()
        self.evidence({"kind": "arrived", "actor": "sora", "place": "library", "at": T0 + 30 * H})
        self.store.sweep(T0 + 31 * H)
        self.assertEqual(self._state(), "expired")

    def test_together_plan_finished_after_expiry_does_not_complete(self) -> None:
        self._together()
        self.evidence({"kind": "plan_finished", "actor": "sora", "outcome": "done",
                       "members": ["sora", "rin"], "at": T0 + 40 * H})
        self.store.sweep(T0 + 41 * H)
        self.assertEqual(self._state(), "expired")

    # --- destination map against the real catalog ids
    def test_outing_destination_map_matches_real_place_ids(self) -> None:
        places = json.loads((ROOT / "world-catalog/worldcatalog/life1/places.json").read_text("utf-8"))
        cases = [
            ("去喫茶店坐坐", "kissa_tomoshibi", "library"),
            ("去咖啡店坐坐", "cafe", "library"),
            ("去超市逛逛", "supermarket", "park"),
            ("去公园走走", "park", "library"),
            ("去图书馆借书", "library", "park"),
            ("去书店逛逛", "bookshop", "park"),
            ("去书签堂看看", "shiorido", "park"),
            ("去便利店转转", "konbini", "park"),
            ("去面包房看看", "bakery", "park"),
            ("去神社参拜", "shrine", "park"),
            ("去钱汤泡澡", "sento", "park"),
            ("去拉面店吃饭", "ramen_shop", "park"),
            ("去商店街逛逛", "shotengai", "park"),
        ]
        for summary, good, bad in cases:
            self.assertIn(good, places)
            for place, expect in ((bad, "open"), (good, "completed")):
                with self.subTest(summary=summary, place=place):
                    self.setUp()
                    self._outing(text=summary, summary=summary)
                    self.evidence({"kind": "arrived", "actor": "sora", "place": place, "at": T0 + 60})
                    self.store.sweep(T0 + 120)
                    self.assertEqual(self._state(), expect)


class _MemoryBase(unittest.TestCase):
    def make(self, llm_fn: Any = None, embed_plugin: Any = None) -> MemoryStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = MemoryStore(tmp.name, clock=VirtualClock(T0), names={"sora": "苍空", "rin": "凛音"}, llm_fn=llm_fn,
                         embed_plugin=embed_plugin, env=TRUST_ON)
        self.addCleanup(st.close)
        self.tmp = Path(tmp.name)
        return st

    @staticmethod
    def group_rows(st: MemoryStore) -> List[Dict[str, Any]]:
        return sorted(st.list_entries("sora", "group", channel=GROUP, now=T0 + 200), key=lambda r: r["id"])


class MemoryTrust(_MemoryBase):
    # New store: trust levels are on only with AI_HOUSE_MEMORY_TRUST=1 (set in `make`); rows are read through
    # `list_entries` (the old tests read the `mem` table), and the extraction ops come in the shared module's format.
    def _extract(self, lines: List[tuple], ops: List[Dict[str, Any]], st: Optional[MemoryStore] = None) -> MemoryStore:
        st = st or self.make()
        for i, (txt, role, spk) in enumerate(lines):
            st.observe("sora", "group", txt, channel=GROUP, speaker=spk, speaker_role=role, now=T0 + i)
        st.extract("sora", scope="group", channel=GROUP, force=True, llm_fn=lambda s, u: {"ops": ops}, now=T0 + 100)
        return st

    def _seed_verified_friday(self) -> tuple:
        st = self.make()
        mid, _ = st.add("sora", "group", "房主周五要去京都出差", channel=GROUP, about=["房主"], kind="fact",
                        trust="verified", source="auto:owner_verified", now=T0 - 1000)
        return st, mid

    # --- update path
    def test_member_update_cannot_rewrite_verified_fact(self) -> None:
        st, mid = self._seed_verified_friday()
        self._extract([("房主周三要去京都出差哦", "member", "小林")],
                      [{"op": "update", "id": mid, "text": "房主周三要去京都出差", "src": [1]}], st)
        rows = {r["id"]: r for r in self.group_rows(st)}
        self.assertEqual(rows[mid]["text"], "房主周五要去京都出差")
        self.assertEqual(rows[mid]["trust"], "verified")
        others = [r for r in rows.values() if r["id"] != mid and "周三" in r["text"]]
        for r in others:
            self.assertEqual((r["kind"], r["trust"]), ("claim", "group_stated"))
        self.assertFalse([r for r in rows.values() if "周三" in r["text"] and r["trust"] == "verified"])

    def test_member_update_without_src_cannot_rewrite_verified_fact(self) -> None:
        st, mid = self._seed_verified_friday()
        self._extract([("房主周三要去京都出差哦", "member", "小林")],
                      [{"op": "update", "id": mid, "text": "房主周三要去京都出差"}], st)
        rows = {r["id"]: r for r in self.group_rows(st)}
        self.assertEqual(rows[mid]["text"], "房主周五要去京都出差")
        self.assertFalse([r for r in rows.values() if "周三" in r["text"] and r["trust"] == "verified"])

    def test_owner_update_still_rewrites_verified_fact(self) -> None:
        st, mid = self._seed_verified_friday()
        self._extract([("我改到周三去京都出差了", "owner", "房主")],
                      [{"op": "update", "id": mid, "text": "房主周三要去京都出差", "src": [1]}], st)
        row = {r["id"]: r for r in self.group_rows(st)}[mid]
        self.assertEqual(row["text"], "房主周三要去京都出差")
        self.assertEqual(row["trust"], "verified")

    def test_member_update_of_unverified_claim_is_still_allowed(self) -> None:
        st = self.make()
        mid, _ = st.add("sora", "group", "小林好像周五有空", channel=GROUP, about=["小林"], kind="claim",
                        trust="group_stated", source="auto", now=T0 - 1000)
        self._extract([("小林其实周六才有空", "member", "小林")],
                      [{"op": "update", "id": mid, "text": "小林周六有空", "src": [1]}], st)
        self.assertEqual({r["id"]: r for r in self.group_rows(st)}[mid]["text"], "小林周六有空")

    def test_add_merge_cannot_overwrite_verified_text_with_weaker_trust(self) -> None:
        # A constant embedding makes every pair "near duplicates", forcing the dedupe-merge path.
        st = self.make(embed_plugin=lambda texts: [[1.0, 0.0] for _ in texts])
        mid, _ = st.add("sora", "group", "房主周五要去京都出差", channel=GROUP, about=["房主"], kind="fact",
                        trust="verified", source="auto:owner_verified", now=T0 - 1000)
        st.add("sora", "group", "房主周三要去京都出差", channel=GROUP, about=["房主"], kind="claim",
               trust="group_stated", source="auto", now=T0)
        rows = {r["id"]: r for r in self.group_rows(st)}
        self.assertEqual(rows[mid]["text"], "房主周五要去京都出差")
        self.assertEqual(rows[mid]["trust"], "verified")
        self.assertEqual(len(rows), 2)

    def test_add_merge_still_merges_equal_or_stronger_trust(self) -> None:
        st = self.make(embed_plugin=lambda texts: [[1.0, 0.0] for _ in texts])
        mid, _ = st.add("sora", "group", "小林好像周五有空", channel=GROUP, about=["小林"], kind="claim",
                        trust="group_stated", source="auto", now=T0 - 1000)
        mid2, how = st.add("sora", "group", "小林周六有空", channel=GROUP, about=["小林"], kind="claim",
                           trust="group_stated", source="auto", now=T0)
        self.assertEqual((mid2, how), (mid, "merged"))

    # --- relayed statements
    def test_owner_relaying_a_third_party_is_a_claim_marked_as_relayed(self) -> None:
        for line in ("小林说他对花生过敏", "听说小林对花生过敏", "小林告诉我他对花生过敏", "医生说小林对花生过敏"):
            with self.subTest(line=line):
                st = self._extract([(line, "owner", "房主")],
                                   [{"op": "add", "text": "小林对花生过敏", "kind": "fact", "src": [1]}])
                row = self.group_rows(st)[0]
                self.assertEqual((row["kind"], row["trust"]), ("claim", "group_stated"))
                self.assertIn("relay", row["source"])

    def test_owner_first_hand_statements_stay_verified(self) -> None:
        for line, text in (("我对花生过敏", "房主对花生过敏"), ("小林下周来", "小林下周要来"),
                           ("我说过我对花生过敏", "房主对花生过敏"),
                           # what somebody told the owner about the owner is still first-hand
                           ("医生说我对花生过敏", "房主对花生过敏"), ("医生说，我对花生过敏", "房主对花生过敏"),
                           ("妈妈说我不能吃海鲜", "房主不能吃海鲜"), ("老板告诉我我下周出差", "房主下周出差")):
            with self.subTest(line=line):
                st = self._extract([(line, "owner", "房主")],
                                   [{"op": "add", "text": text, "kind": "fact", "src": [1]}])
                row = self.group_rows(st)[0]
                self.assertEqual((row["kind"], row["trust"]), ("fact", "verified"))
                self.assertEqual(row["source"], "auto:owner_verified")


class MemoryConflicts(_MemoryBase):
    # New store: `search` flags a conflicting claim only with trust levels on; the flag is `_conflict_verified`, as before.
    def _search(self, verified: str, claim: str, query: str, vabout: tuple, cabout: tuple) -> List[Dict[str, Any]]:
        st = self.make()
        st.add("sora", "group", verified, channel=GROUP, about=list(vabout), kind="fact", trust="verified",
               source="owner_verified", now=T0 - 60 * 86400)
        st.add("sora", "group", claim, channel=GROUP, about=list(cabout), kind="claim", trust="group_stated",
               source="auto", now=T0)
        hits = st.search("sora", "group", query, channel=GROUP, min_score=0.05, k=5, now=T0)
        return [r for _, r in hits if r["kind"] == "claim"]

    def test_cross_person_preference_is_not_a_conflict(self) -> None:
        claims = self._search("凛音喜欢吃咖喱", "群友说苍空不喜欢吃咖喱", "咖喱", ("凛音",), ("苍空",))
        self.assertTrue(claims)
        self.assertFalse(any(c.get("_conflict_verified") for c in claims))

    def test_cross_person_subject_taken_from_text_when_about_missing(self) -> None:
        claims = self._search("凛音喜欢吃咖喱", "苍空不喜欢吃咖喱", "咖喱", (), ())
        self.assertTrue(claims)
        self.assertFalse(any(c.get("_conflict_verified") for c in claims))

    def test_same_person_opposite_preference_is_still_a_conflict(self) -> None:
        claims = self._search("凛音喜欢吃咖喱", "群友说凛音不喜欢吃咖喱", "咖喱", ("凛音",), ("凛音",))
        self.assertTrue(claims and all(c.get("_conflict_verified") for c in claims))

    def test_denial_of_allergy_conflicts_with_verified_allergy(self) -> None:
        for claim in ("群友说房主其实对花生不过敏，随便吃", "房主其实对花生没有过敏", "房主对花生并不过敏"):
            with self.subTest(claim=claim):
                claims = self._search("房主对花生严重过敏，绝对不能吃", claim, "房主能吃花生吗", ("房主",), ("房主",))
                self.assertTrue(claims and all(c.get("_conflict_verified") for c in claims))

    def test_agreeing_claim_is_not_flagged(self) -> None:
        claims = self._search("房主对花生严重过敏，绝对不能吃", "群友说房主对花生也会过敏", "房主能吃花生吗",
                              ("房主",), ("房主",))
        self.assertTrue(claims)
        self.assertFalse(any(c.get("_conflict_verified") for c in claims))

    def test_weekday_conflict_between_same_subject_still_detected(self) -> None:
        claims = self._search("房主周五要去京都出差", "群友说房主周三要去京都出差", "房主什么时候去京都出差",
                              ("房主",), ("房主",))
        self.assertTrue(claims and all(c.get("_conflict_verified") for c in claims))


if __name__ == "__main__":
    unittest.main()
