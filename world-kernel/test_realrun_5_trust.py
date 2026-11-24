"""Real-run #5 (three houses terra / mixed / gemini): the trust level of what the owner said about himself.

Evidence (the extractor's own wording, copied from the runs' memory.db files):
  * the owner told 奈尔 in a private chat "我对花生过敏，你记一下哦。还有，我的生日是3月14日。"; terra and mixed stored two
    `kind=claim` rows ("2026年11月21日，房主说自己对花生过敏。") which came out `group_stated` and recalled as
    「推断·未核实」, so 奈尔 answered "我记得你可能对花生过敏" the next day;
  * the same secret told to 由纪 was stored as `event/verified` in terra ("…房主表示打算2026年12月辞职开咖啡店。"), but
    mixed worded it "房主私下告诉由纪，计划于2026年12月辞职开咖啡店，并要求保密。" and got downgraded: the manner of speaking
    (私下 / 告诉 / 要求保密) was counted as content the owner never said.
No test calls a model: the extractor is a fake that returns the exact text the real extractor wrote.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.memory import MemoryStore  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
TRUST_ENV = {"AI_HOUSE_MEMORY_TRUST": "1"}
from worldkernel.store import VirtualClock  # noqa: E402

NAMES = {"nell": "奈尔", "yuki": "由纪", "kai": "凯"}
NELL_LINE = "奈尔早安！我对花生过敏，你记一下哦。还有，我的生日是3月14日。"  # 2026-11-21 09:00
YUKI_LINE = "由纪，悄悄告诉你一个秘密：我打算下个月辞职去开咖啡店，千万别告诉别人哦。"  # 2026-11-21 14:00
DAY1_9 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
DAY1_14 = dt.datetime(2026, 11, 21, 14, 0, tzinfo=JST).timestamp()
DAY2_20 = dt.datetime(2026, 11, 22, 20, 0, tzinfo=JST).timestamp()

# (text the real extractor wrote, kind it used) -- terra / mixed / gemini rows, verbatim
NELL_ROWS: List[Tuple[str, str]] = [
    ("2026年11月21日，房主说自己对花生过敏。", "claim"),         # terra#1, mixed#1
    ("2026年11月21日，房主说自己的生日是3月14日。", "claim"),     # terra#2, mixed#2
    ("房主对花生过敏，生日是3月14日。", "fact"),                  # first gpt-4o-mini run
    ("房主对花生过敏（2026年11月21日告知）。", "person"),         # gemini#1
    ("房主的生日是3月14日（2026年11月21日告知）。", "person"),     # gemini#2
]
YUKI_ROWS: List[Tuple[str, str]] = [
    ("2026年11月21日，房主表示打算2026年12月辞职开咖啡店。", "event"),                       # terra#5
    ("2026年11月21日，房主私下告诉由纪，计划于2026年12月辞职开咖啡店，并要求保密。", "event"),   # mixed#3
    ("2026年11月21日，房主私下告诉由纪，打算于2026年12月辞职去开咖啡店，并叮嘱保密不要告诉别人。", "claim"),  # gemini#4
    ("房主计划在2026年12月辞职去开咖啡店，要求由纪保守这个秘密。", "event"),                # realrun#2 mem#6
]


class _Base(unittest.TestCase):
    def extract_rows(
        self,
        actor: str,
        lines: Sequence[Tuple[str, str, str, str]],  # (text, speaker, role, reply)
        op: Dict[str, Any],
        *,
        scope: str = "private",
        channel: str = "",
        at: float = DAY1_14,
    ) -> Tuple[MemoryStore, List[Dict[str, Any]], Dict[str, Any]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = MemoryStore(tmp.name, clock=VirtualClock(at), names=NAMES, llm_fn=lambda _s, _u: {"ops": [op]},
                         extract_every_s=60, extract_batch=10, env=TRUST_ENV)
        self.addCleanup(st.close)
        for text, speaker, role, reply in lines:
            st.observe(actor, scope, text, channel=channel, speaker=speaker, speaker_role=role, reply=reply)
        res = st.extract(actor, scope=scope, channel=channel or None, force=True)
        return st, st.list_entries(actor, scope, channel=channel), res

    def private(self, actor: str, line: str, text: str, kind: str, at: float, about: Sequence[str] = ("房主",),
                src: Sequence[int] = (1,)) -> Tuple[MemoryStore, List[Dict[str, Any]]]:
        op = {"op": "add", "kind": kind, "text": text, "about": list(about), "src": list(src)}
        st, rows, _ = self.extract_rows(actor, [(line, "房主", "owner", "")], op, at=at)
        return st, rows

    def assert_downgraded(self, actor: str, got: Tuple[MemoryStore, List[Dict[str, Any]]], query: str) -> None:
        st, rows = got
        self.assertEqual(len(rows), 1, query)
        # fact/preference/person/claim fall back to kind=claim; the other kinds (event, promise) keep theirs
        self.assertEqual(rows[0]["trust"], "group_stated", (query, rows[0]["text"]))
        self.assertIn(rows[0]["kind"], ("claim", "event"))
        self.assertIn("推断·未核实", st.recall(actor, "private", query, channel=f"private:{actor}", now=DAY2_20))


class OwnerSelfStatementIsVerified(_Base):
    def test_nell_rows_of_every_wording_are_verified(self) -> None:
        for text, kind in NELL_ROWS:
            with self.subTest(text=text, kind=kind):
                st, rows = self.private("nell", NELL_LINE, text, kind, DAY1_9)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["trust"], "verified")
                self.assertNotEqual(rows[0]["kind"], "claim")  # the 「推断·未核实」 header keys on kind=claim too
                # the new recall matches on content words: ask about what the row is about
                ask = "奈尔，还记得我对什么过敏吗？" if "花生" in text else "奈尔，还记得我的生日是哪天吗？"
                got = st.recall("nell", "private", ask, channel="private:nell", now=DAY2_20)
                self.assertNotIn("未核实", got)
                self.assertIn("花生" if "花生" in text else "3月14日", got)

    def test_yuki_secret_rows_of_every_wording_are_verified(self) -> None:
        for text, kind in YUKI_ROWS:
            with self.subTest(text=text, kind=kind):
                st, rows = self.private("yuki", YUKI_LINE, text, kind, DAY1_14)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["trust"], "verified")
                self.assertNotEqual(rows[0]["kind"], "claim")
                got = st.recall("yuki", "private", "由纪，我之前悄悄告诉你的那件事，你还记得吗？",
                                channel="private:yuki", now=DAY2_20)
                self.assertIn("咖啡店", got)
                self.assertNotIn("未核实", got)

    def test_claim_without_about_or_src_is_verified_when_the_line_backs_it(self) -> None:
        op = {"op": "add", "kind": "claim", "text": "2026年11月21日，房主说自己对花生过敏。"}
        _, rows, _ = self.extract_rows("nell", [(NELL_LINE, "房主", "owner", "")], op, at=DAY1_9)
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("fact", "verified"))

    def test_the_leading_date_is_only_free_when_it_is_the_day_the_line_was_said(self) -> None:
        # the line names 3月14日, so a different leading date is a claim of its own and is not backed
        got = self.private("nell", NELL_LINE, "2026年11月25日，房主说自己对花生过敏。", "claim", DAY1_9)
        self.assert_downgraded("nell", got, "房主对花生过敏")


class MannerWordsAreNotContent(_Base):
    """The old tests called the helper `_owner_lines_support` directly; it is no longer importable. The same rule is
    checked through the store: a text the owner's line backs comes out verified, one it does not back is downgraded."""

    def backed(self, text: str, line: str, kind: str = "event", at: float = DAY1_14) -> bool:
        st, rows = self.private("yuki", line, text, kind, at)
        self.assertEqual(len(rows), 1, text)
        return rows[0]["trust"] == "verified"

    def test_support_check_through_the_store(self) -> None:
        for text in (
            "房主私下告诉由纪，计划于2026年12月辞职开咖啡店，并要求保密。",
            "房主悄悄告诉由纪一个秘密：打算辞职开咖啡店，千万不要告诉别人。",
            "房主偷偷打算辞职去开咖啡店，叮嘱由纪保守秘密",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.backed(text, YUKI_LINE), text)

    def test_manner_words_alone_back_nothing(self) -> None:
        line = YUKI_LINE.replace("下个月", "下个月〔即2026年12月〕")
        # positive control: the same line does back the content when the content is what the owner said
        self.assertTrue(self.backed("房主私下告诉由纪，计划于2026年12月辞职开咖啡店，并要求保密。", line))
        for text in (
            "房主私下告诉由纪一个秘密，并要求保密。",
            "房主悄悄告诉由纪，千万别告诉别人。",
            "房主私下告诉由纪，打算养一只狗，并要求保密。",       # secret content the owner never said
            "房主说自己有一个秘密，欠了很多钱",
            "房主私下告诉由纪，计划于2026年10月辞职开咖啡店，并要求保密。",  # 「下个月」 is December, not October
        ):
            with self.subTest(text=text):
                self.assertFalse(self.backed(text, line), text)

    def test_negation_in_a_confidentiality_request_does_not_flip_the_content(self) -> None:
        # 「不要告诉别人」 contains 「不」, but it is a request, not a denial of the content
        self.assertTrue(self.backed("房主打算辞职开咖啡店，并叮嘱保密不要告诉别人", YUKI_LINE, kind="fact"))
        # genuinely negated content still needs a negated owner clause
        self.assertFalse(self.backed("房主不打算辞职开咖啡店，并要求保密", YUKI_LINE, kind="fact"))


class TrustSwitch(_Base):
    def test_default_is_off_no_labels_and_claims_read_as_plain_memories(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        op = {"op": "add", "kind": "claim", "text": "2026年11月21日，房主说自己对虾过敏。", "about": ["房主"], "src": [1]}
        st = MemoryStore(tmp.name, clock=VirtualClock(DAY1_9), names=NAMES, llm_fn=lambda _s, _u: {"ops": [op]},
                         env={})
        self.addCleanup(st.close)
        st.observe("nell", "private", "我对虾过敏，你记一下哦。", speaker="房主", speaker_role="owner")
        res = st.extract("nell", scope="private", force=True)
        self.assertEqual(res["added"], 1)
        rows = st.list_entries("nell", "private")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "fact")  # claim is not a kind while the switch is off
        self.assertNotIn(rows[0]["trust"], ("group_stated", "hearsay"))
        got = st.recall("nell", "private", "奈尔，还记得我对什么过敏吗？", channel="private:nell", now=DAY2_20)
        self.assertIn("虾", got)  # positive control: it is found
        self.assertNotIn("未核实", got)
        self.assertNotIn("推断", got)

    def test_switch_on_labels_an_unbacked_claim(self) -> None:
        # same scenario as above with the switch on: the label appears
        st, rows = self.private("nell", "我对虾过敏，你记一下哦。", "2026年11月21日，房主说自己对坚果过敏。", "claim", DAY1_9)
        self.assertEqual(rows[0]["trust"], "group_stated")
        self.assertIn("推断·未核实", st.recall("nell", "private", "奈尔，还记得我对什么过敏吗？", channel="private:nell", now=DAY2_20))


class ContentThatDoesNotMatchIsStillDowngraded(_Base):
    def test_allergen_changed(self) -> None:
        for kind in ("claim", "fact", "person"):
            for text in ("2026年11月21日，房主说自己对坚果过敏。", "房主对坚果过敏", "房主对虾过敏"):
                with self.subTest(kind=kind, text=text):
                    self.assert_downgraded("nell", self.private("nell", NELL_LINE, text, kind, DAY1_9), "房主过敏")

    def test_birthday_changed(self) -> None:
        for kind in ("claim", "fact"):
            with self.subTest(kind=kind):
                got = self.private("nell", NELL_LINE, "2026年11月21日，房主说自己的生日是3月15日。", kind, DAY1_9)
                self.assert_downgraded("nell", got, "房主的生日")

    def test_month_changed(self) -> None:
        for kind in ("claim", "event"):
            for text in (
                "2026年11月21日，房主私下告诉由纪，计划于2026年10月辞职开咖啡店，并要求保密。",  # 下个月 = 12月
                "2026年11月21日，房主表示打算2027年1月辞职开咖啡店。",
            ):
                with self.subTest(kind=kind, text=text):
                    self.assert_downgraded("yuki", self.private("yuki", YUKI_LINE, text, kind, DAY1_14), "房主辞职开咖啡店")

    def test_month_the_owner_gave_explicitly_wins_over_the_item(self) -> None:
        op = {"op": "add", "kind": "claim", "text": "2026年11月21日，房主表示打算2026年10月辞职开咖啡店。",
              "about": ["房主"], "src": [1]}
        _, rows, _ = self.extract_rows("yuki", [("我打算12月辞职去开咖啡店", "房主", "owner", "")], op)
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("claim", "group_stated"))

    def test_different_shop_or_plan(self) -> None:
        for text in (
            "2026年11月21日，房主表示打算2026年12月辞职开花店。",
            "2026年11月21日，房主私下告诉由纪，计划卖掉房子去旅行，并要求保密。",
        ):
            with self.subTest(text=text):
                self.assert_downgraded("yuki", self.private("yuki", YUKI_LINE, text, "claim", DAY1_14), text)


class OnlyTheOwnersOwnWordsAboutHimselfAreUpgraded(_Base):
    def test_a_claim_about_somebody_else_stays_a_claim(self) -> None:
        # the owner did say it, but it is about 凯 (the claim label means: an unconfirmed statement about a third party)
        for text, about in (("2026年11月21日，房主说凯对花生过敏。", ("房主", "凯")), ("凯对花生过敏", ("凯",))):
            with self.subTest(text=text):
                got = self.private("nell", "我觉得凯对花生过敏", text, "claim", DAY1_9, about=about)
                self.assert_downgraded("nell", got, "凯对花生过敏")

    def test_a_relayed_line_stays_a_claim(self) -> None:
        for line in ("小林说他对花生过敏", "听说小林对花生过敏"):
            with self.subTest(line=line):
                got = self.private("nell", line, "2026年11月21日，房主说小林对花生过敏。", "claim", DAY1_9,
                                   about=("房主", "小林"))
                self.assert_downgraded("nell", got, "小林对花生过敏")
        # even if the extractor files the relayed content under the owner
        got = self.private("nell", "小林说他对花生过敏", "房主说对花生过敏", "claim", DAY1_9)
        self.assert_downgraded("nell", got, "房主对花生过敏")

    def test_the_residents_own_guess_is_not_owner_words(self) -> None:
        owner_line = ("我最近老是睡不好", "房主", "owner", "你是不是对花生过敏呀")  # line 2 is 奈尔's own reply
        # cannot be cited at all
        op = {"op": "add", "kind": "claim", "text": "房主对花生过敏", "about": ["房主"], "src": [2]}
        st, rows, res = self.extract_rows("nell", [owner_line], op, at=DAY1_9)
        self.assertEqual((res["added"], res["rejected"], rows), (0, 1, []))
        # citing the owner line that does not say it: the extractor refuses a new entity that only the resident's own
        # reply introduced (before: it was kept as a downgraded claim; no verified row either way)
        op["src"] = [1]
        st, rows, res = self.extract_rows("nell", [owner_line], op, at=DAY1_9)
        self.assertEqual((res["added"], res["rejected"], rows), (0, 1, []))
        # positive control: when the owner's own line says it, the same text is stored and verified
        st, rows, res = self.extract_rows("nell", [("我对花生过敏", "房主", "owner", "你是不是对花生过敏呀")], op, at=DAY1_9)
        self.assertEqual((res["added"], [r["trust"] for r in rows]), (1, ["verified"]))

    def test_hearsay_about_a_third_party_is_not_verified(self) -> None:
        st, rows = self.private("nell", "小林说他对花生过敏", "小林对花生过敏", "claim", DAY1_9, about=("小林",))
        self.assertNotEqual(rows[0]["trust"], "verified")


class GroupScopeIsUnchanged(_Base):
    def test_member_statement_in_group_stays_group_stated(self) -> None:
        for kind in ("claim", "fact", "preference"):
            with self.subTest(kind=kind):
                op = {"op": "add", "kind": kind, "text": "房主说自己对花生过敏", "about": ["房主"], "src": [1]}
                st, rows, _ = self.extract_rows("nell", [("房主对花生过敏哦，我听他说过", "群友甲", "member", "")], op,
                                                scope="group", channel="g")
                self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("claim", "group_stated"))
                self.assertIn("未核实", st.recall("nell", "group", "房主对花生过敏", channel="g"))

    def test_owner_relaying_somebody_in_group_is_still_relayed(self) -> None:
        op = {"op": "add", "kind": "fact", "text": "小林对花生过敏", "src": [1]}
        _, rows, _ = self.extract_rows("nell", [("小林说他对花生过敏", "房主", "owner", "")], op, scope="group", channel="g")
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("claim", "group_stated"))
        self.assertIn("relay", rows[0]["source"])

    def test_owner_first_hand_in_group_is_still_a_verified_fact(self) -> None:
        op = {"op": "add", "kind": "fact", "text": "房主对花生过敏", "src": [1]}
        _, rows, _ = self.extract_rows("nell", [("我对花生过敏", "房主", "owner", "")], op, scope="group", channel="g")
        self.assertEqual((rows[0]["kind"], rows[0]["trust"]), ("fact", "verified"))


class RementionUpgradePath(unittest.TestCase):
    """Documents the existing mechanism (unchanged): a re-mentioned fact raises an old row's trust only when
    `MemoryStore.add` merges a near-duplicate (cosine >= DEFAULT_DEDUPE_COS, so it needs an embedder); an identical text
    returns 'same' without touching trust, and a merge never rewrites `kind`."""

    def _store(self, embed: Any) -> MemoryStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = MemoryStore(tmp.name, names=NAMES, embed_plugin=embed, env=TRUST_ENV)
        self.addCleanup(st.close)
        st.add("nell", "private", "2026年11月21日，房主说自己对花生过敏。", channel="private:nell", about=["房主"],
               kind="claim", trust="group_stated", source="auto", now=DAY1_9)
        return st

    def test_near_duplicate_verified_add_raises_trust_but_not_kind(self) -> None:
        st = self._store(lambda texts: [[1.0, 0.0] for _ in texts])
        _, how = st.add("nell", "private", "房主对花生过敏。", channel="private:nell", about=["房主"], kind="fact",
                        trust="verified", source="auto", now=DAY2_20)
        self.assertEqual(how, "merged")
        row = st.list_entries("nell", "private")[0]
        self.assertEqual((row["trust"], row["kind"]), ("verified", "claim"))

    def test_without_embedder_or_with_identical_text_nothing_is_upgraded(self) -> None:
        st = self._store(None)
        _, how = st.add("nell", "private", "房主对花生过敏。", channel="private:nell", about=["房主"], kind="fact",
                        trust="verified", source="auto", now=DAY2_20)
        self.assertEqual(how, "added")  # a second row; the old group_stated one stays as it was
        _, how = st.add("nell", "private", "2026年11月21日，房主说自己对花生过敏。", channel="private:nell",
                        about=["房主"], kind="fact", trust="verified", source="auto", now=DAY2_20)
        self.assertEqual(how, "same")
        self.assertIn("group_stated", {r["trust"] for r in st.list_entries("nell", "private")})


if __name__ == "__main__":
    unittest.main()
