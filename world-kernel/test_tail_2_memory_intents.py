"""Tail batch 2 regressions: buy-request parsing, promise evidence windows, memory trust and conflicts (trust levels on).

Each section pairs positive cases (must still work) with negative ones (the acceptance-round bugs).
Promise evidence is judged by the shared rules (`worldkernel.promise_rules`) with rows shaped like the kernel's.
"""
from __future__ import annotations

import datetime as dt
import json
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
from worldkernel import buy_words, promise_rules, vocab_life1 as V  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
H = 3600.0
GROUP = "qq-group:5001"  # the old tests' channel "g1"
TRUST_ON = {"AI_HOUSE_MEMORY_TRUST": "1"}  # trust levels, the claim kind and conflict flags are off unless switched on


class BuyRequestParsing(unittest.TestCase):
    """What the owner asked to have bought, read by `worldkernel.buy_words` (the words moved out of the old promise
    code). The old "become an open errand with raw_item" half is gone (D1, D4): nothing is recorded by the chat
    turn; what the parser still decides is whether the line is an explicit request (so the reply may say that the
    thing cannot be bought) and which noun it names."""

    # (owner text, resident reply, expected noun) -- explicit requests / commitments for things outside the catalog
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

    def test_requests_and_commitments_name_the_thing_to_buy(self) -> None:
        """Fails if an explicit request or the resident's own promise to buy ("好，我去买") no longer yields the noun
        (22 sentences, one subTest each), or if a noun that is not a catalogue item resolves to an item id."""
        for text, reply, expected in self.SHOULD_CREATE:
            with self.subTest(text=text):
                self.assertEqual(buy_words.extract_buy_request_noun(text, reply), expected)
                self.assertIsNone(buy_words.resolve_item_id(V, expected))

    def test_catalog_request_still_resolves_to_item(self) -> None:
        """Fails if a request for a catalogue item stops resolving to its item id (the kernel shops for it), or is
        reported as a thing that cannot be bought."""
        self.assertEqual(buy_words.extract_buy_request_noun("帮我买点牛奶", "好"), "牛奶")
        self.assertEqual(buy_words.resolve_item_id(V, "牛奶"), "milk")
        self.assertEqual(buy_words.detect_errand_item(V, "帮我买点牛奶")[0], "milk")
        self.assertEqual(buy_words.unbuyable_noun(V, "帮我买点牛奶"), "")
        self.assertEqual(buy_words.unbuyable_noun(V, "帮我买点榴莲"), "榴莲")  # control: the same shape, not in stock

    def test_chitchat_never_reads_as_a_purchase_request(self) -> None:
        """Fails if past tense, questions, negations, "买卖/买单", things one carries ("带伞") or a subject-less
        sentence is read as a request (a noun comes out, or the line is reported as unbuyable). The control: the
        explicit requests of the other table all yield their noun in the same run."""
        yielding = [t for t, r, e in self.SHOULD_CREATE if buy_words.extract_buy_request_noun(t, r) == e]
        self.assertEqual(len(yielding), len(self.SHOULD_CREATE))
        for text in self.SHOULD_NOT_CREATE:
            with self.subTest(text=text):
                self.assertEqual(buy_words.extract_buy_request_noun(text, "嗯"), "")
                self.assertEqual(buy_words.unbuyable_noun(V, text), "")

    def test_particles_are_stripped_from_the_noun(self) -> None:
        """Fails if a noun keeps a sentence particle or a direction word at either end ("榴莲吧", "奶茶回来")."""
        for text, reply, _ in self.SHOULD_CREATE:
            noun = buy_words.extract_buy_request_noun(text, reply)
            self.assertTrue(noun, text)
            self.assertNotRegex(noun, r"^[了吗吧呢啊的这]|[了吗吧呢啊回来]$")


class PromiseEvidenceRules(unittest.TestCase):
    """The generic evidence rules of the shared promise code (`worldkernel.promise_rules`), which replace the kit's
    own evidence windows. The kind-based closing of the old code (an `outing` closed by an arrival, a `together`
    by `plan_finished`, a `cook` by a finished plan) is gone (D6); what is kept is the window (a receipt counts only
    between the time she said it and the deadline), the strength of the evidence (only real arrivals, purchases and
    shared activity close a promise) and the place words."""

    R = promise_rules.bound(V)

    def intent(self, text: str, party: List[str], created: float = T0, deadline: float = T0 + 24 * H) -> Dict[str, Any]:
        terms = self.R.compile_terms(text, V, party)
        self.assertNotEqual(terms, [{"kind": "unsupported"}], text)
        return {"id": 7, "status": "accepted", "party": party, "terms": terms, "created": created,
                "said_at": created, "deadline": deadline, "beneficiary": [], "scope": "world"}

    @staticmethod
    def purchase(actor: str, at: float, item: str = "onion", qty: int = 1, key: str = "k1") -> Dict[str, Any]:
        return {"key": key, "kind": "purchased", "actor": actor, "at": at, "item": item, "qty": qty, "account": actor,
                "action_id": "a" + key, "resource_id": "r" + key, "intent_ref": 7, "beneficiary": [], "plan_id": "p1"}

    @staticmethod
    def arrival(actor: str, place: str, at: float, key: str = "k2") -> Dict[str, Any]:
        return {"key": key, "kind": "arrived", "actor": actor, "place": place, "at": at, "plan_id": "p1"}

    @staticmethod
    def together(actor: str, plan: str, start: float, end: float, outcome: str = "succeeded", place: str = "living",
                 key: str = "") -> Dict[str, Any]:
        return {"key": key or f"t-{actor}-{plan}", "kind": "activity", "actor": actor, "activity": "housework",
                "outcome": outcome, "plan_id": plan, "place": place, "started_at": start, "ended_at": end, "at": end}

    def done(self, it: Dict[str, Any], evidence: List[Dict[str, Any]], now: float) -> bool:
        return bool(self.R.judge(it, evidence, now)["complete"])

    # --- evidence strength
    def test_invite_settled_and_plan_finished_never_satisfy_a_term(self) -> None:
        """Fails if an `invite_settled` or a `plan_finished` row (any outcome) closes an arrival, a purchase or a
        shared activity, or if the real evidence of the same scenario does not close it (the control)."""
        cases = [
            ("奈尔去公园", ["nell"], [self.arrival("nell", "place:park", T0 + 60)]),
            ("奈尔买洋葱", ["nell"], [self.purchase("nell", T0 + 60)]),
            ("奈尔和艾莎一起做家务", ["nell", "elsa"],
             [self.together("nell", "plan:p1", T0 + 60, T0 + 1500), self.together("elsa", "plan:p1", T0 + 60, T0 + 1500)]),
        ]
        for text, party, real in cases:
            it = self.intent(text, party)
            weak = []
            for n, outcome in enumerate(("done", "early_home", "cancelled", "lapsed", "missed", None, "bogus")):
                for actor in party:
                    weak.append({"key": f"f{n}{actor}", "kind": "plan_finished", "actor": actor, "outcome": outcome,
                                 "members": list(party), "plan_id": "p1", "at": T0 + 70})
            for actor in party:
                weak.append({"key": f"i{actor}", "kind": "invite_settled", "actor": actor, "members": list(party),
                             "place": "library", "at": T0 + 70})
            with self.subTest(text=text):
                self.assertFalse(self.done(it, weak, T0 + 120))
                self.assertTrue(self.done(it, weak + real, T0 + 2000))

    def test_together_closes_on_real_shared_activity_only(self) -> None:
        """Fails if a shared activity closes when the two did it in different plans, when one of them is missing,
        when the overlap is shorter than ten minutes, or when the evidence is for another activity; or if the
        genuine shared one (same plan, same place, long enough overlap) does not close it."""
        it = self.intent("奈尔和艾莎一起做家务", ["nell", "elsa"])
        a, b = T0 + H, T0 + H + 1200
        bad = {
            "different plans": [self.together("nell", "plan:p1", a, b), self.together("elsa", "plan:p2", a, b)],
            "one member only": [self.together("nell", "plan:p1", a, b)],
            "overlap under ten minutes": [self.together("nell", "plan:p1", a, a + 500),
                                          self.together("elsa", "plan:p1", a + 400, b)],
            "different places": [self.together("nell", "plan:p1", a, b),
                                 self.together("elsa", "plan:p1", a, b, place="kitchen")],
            "another activity": [dict(self.together("nell", "plan:p1", a, b), activity="gaming"),
                                 dict(self.together("elsa", "plan:p1", a, b), activity="gaming")],
        }
        for label, ev in bad.items():
            with self.subTest(label):
                self.assertFalse(self.done(it, ev, T0 + 3 * H))
        good = [self.together("nell", "plan:p1", a, b), self.together("elsa", "plan:p1", a + 300, b)]
        self.assertTrue(self.done(it, good, T0 + 3 * H))

    def test_outing_arrival_at_the_named_place_closes(self) -> None:
        """Fails if an arrival at the named place does not close the promise, or an arrival somewhere else, or
        by another housemate, does."""
        it = self.intent("奈尔去公园", ["nell"])
        self.assertFalse(self.done(it, [self.arrival("nell", "place:library", T0 + 60)], T0 + 120))
        self.assertFalse(self.done(it, [self.arrival("elsa", "place:park", T0 + 60)], T0 + 120))
        self.assertTrue(self.done(it, [self.arrival("nell", "place:park", T0 + 60)], T0 + 120))

    # --- time window
    def test_errand_evidence_after_the_deadline_does_not_complete(self) -> None:
        """Fails if a purchase after the deadline closes the promise; the control is a purchase inside the window."""
        it = self.intent("奈尔买洋葱", ["nell"])
        self.assertFalse(self.done(it, [self.purchase("nell", T0 + 30 * H)], T0 + 31 * H))
        self.assertTrue(self.done(it, [self.purchase("nell", T0 + 3 * H)], T0 + 5 * H))

    def test_errand_evidence_inside_the_window_completes_even_if_judged_late(self) -> None:
        """Fails if a receipt that came inside the window stops counting because the judgement runs after the
        deadline (the receipt must be judged by its own time)."""
        it = self.intent("奈尔买洋葱", ["nell"])
        self.assertTrue(self.done(it, [self.purchase("nell", T0 + 3 * H)], T0 + 31 * H))

    def test_evidence_before_creation_is_ignored(self) -> None:
        """Fails if a purchase made before she said it closes the promise; the control is the same purchase after."""
        it = self.intent("奈尔买洋葱", ["nell"])
        self.assertFalse(self.done(it, [self.purchase("nell", T0 - 2 * H)], T0 + H))
        self.assertTrue(self.done(it, [self.purchase("nell", T0 + 60)], T0 + H))

    def test_outing_arrival_after_the_deadline_does_not_complete(self) -> None:
        """Fails if an arrival after the deadline closes the promise; the control is an arrival inside the window."""
        it = self.intent("奈尔去公园", ["nell"])
        self.assertFalse(self.done(it, [self.arrival("nell", "place:park", T0 + 30 * H)], T0 + 31 * H))
        self.assertTrue(self.done(it, [self.arrival("nell", "place:park", T0 + 3 * H)], T0 + 31 * H))

    def test_together_activity_after_the_deadline_does_not_complete(self) -> None:
        """Fails if a shared activity that ended after the deadline closes the promise; the control is the same
        activity inside the window."""
        it = self.intent("奈尔和艾莎一起做家务", ["nell", "elsa"])
        late = [self.together(x, "plan:p1", T0 + 40 * H, T0 + 41 * H) for x in ("nell", "elsa")]
        self.assertFalse(self.done(it, late, T0 + 42 * H))
        ok = [self.together(x, "plan:p1", T0 + 2 * H, T0 + 3 * H) for x in ("nell", "elsa")]
        self.assertTrue(self.done(it, ok, T0 + 42 * H))

    # --- destination words against the real catalogue ids
    def test_place_words_map_to_real_place_ids(self) -> None:
        """Fails if a place word ("去超市", "去神社" ...) is not read as an arrival at that catalogue place, or if an
        arrival somewhere else closes it. A word that maps to a wrong id would make every outing promise for it
        unclosable (or closed by the wrong visit)."""
        cases = [
            ("去咖啡店", "cafe", "library"),
            ("去超市", "supermarket", "park"),
            ("去公园", "park", "library"),
            ("去图书馆", "library", "park"),
            ("去书店", "bookshop", "park"),
            ("去书签堂", "shiorido", "park"),
            ("去便利店", "konbini", "park"),
            ("去面包房", "bakery", "park"),
            ("去神社", "shrine", "park"),
            ("去钱汤", "sento", "park"),
            ("去拉面店", "ramen_shop", "park"),
            ("去商店街", "shotengai", "park"),
        ]
        places = json.loads((ROOT / "world-catalog/worldcatalog/life1/places.json").read_text("utf-8"))
        for words, good, bad in cases:
            self.assertIn(good, places)
            it = self.intent("奈尔" + words, ["nell"])
            self.assertEqual(it["terms"], [{"kind": "arrived", "place": "place:" + good}], words)
            with self.subTest(words=words):
                self.assertFalse(self.done(it, [self.arrival("nell", "place:" + bad, T0 + 60)], T0 + 120))
                self.assertTrue(self.done(it, [self.arrival("nell", "place:" + good, T0 + 60)], T0 + 120))


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
