"""A promise "for the next time I go out" is run by the next shopping trip.

Defect (live run): `下次出门顺路帮他带一盒红茶回来` was registered with when="after"; the shared code never infers an
"after" condition as met, so three trips went by and the tea was never bought. No network, no real model. Every test
says what would make it fail."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir, T0, rows  # noqa: E402

from ai_house.intents_common import _clean_ops  # noqa: E402

OWNER = "红茶买到了吗？"
HERS = "还没买到红茶，红茶我记着呢，下次出门顺路帮你带回来。"
TEA = "奈尔答应房主，下次出门顺路帮他带一盒红茶回来"


def op(text=TEA, when="after", due="", **kw):
    return {"op": "intent", "kind": "promise", "text": text, "for": ["房主"], "when": when, "due": due,
            "agreed": ["奈尔"], "quote": "红茶我记着呢，下次出门顺路帮你带回来", **kw}


def when_of(o):
    return _clean_ops([o])[0]["when"]


class RewriteTests(unittest.TestCase):
    def test_outing_only_after_becomes_vague(self):
        """Fails if the sentence from the live run keeps `after`, or the other outing phrases are not recognised."""
        self.assertEqual(when_of(op()), "vague")
        for phrase in ("下回出门", "出门的时候", "出去时", "下次路过超市", "下次去超市"):
            self.assertEqual(when_of(op(f"奈尔答应房主：{phrase}帮他带一盒红茶")), "vague", phrase)

    def test_real_conditions_stay_after(self):
        """Fails if a real condition (a wage day, a discount) is turned into an errand that runs at once."""
        self.assertEqual(when_of(op("奈尔答应房主：等房主发工资之后帮他买一盒红茶")), "after")
        self.assertEqual(when_of(op("奈尔答应房主：下次出门如果有打折就带一盒红茶")), "after")
        self.assertEqual(when_of(op("奈尔答应房主：下次出门前先问清楚再说")), "after")

    def test_dated_or_other_ops_are_untouched(self):
        """Fails if an op with a due date, another `when`, or another kind of op is changed."""
        dated = op(due="2026-11-28")
        self.assertEqual(_clean_ops([dated]), [dated])
        for when in ("vague", "day", "exact", ""):
            self.assertEqual(when_of(op(when=when)), when)
        other = {"op": "memory", "text": TEA, "when": "after"}
        self.assertEqual(_clean_ops([other]), [other])
        self.assertEqual(_clean_ops(None), None)

    def test_caller_dict_is_not_mutated(self):
        """Fails if the rewrite edits the caller's op instead of a copy."""
        o = op()
        before = dict(o)
        out = _clean_ops([o])
        self.assertEqual(o, before)
        self.assertIsNot(out[0], o)


class EndToEndTests(Dir):
    def extract(self, o):
        d = self.daemon(env={"INTENTS": "1"})
        d.memory.observe("nell", "private", OWNER, speaker_role="owner", speaker="房主", reply=HERS, now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=lambda system, user: {"ops": [o]})
        return d

    def test_row_is_vague_and_the_errand_is_handed_to_the_kernel(self):
        """Fails if the stored row keeps `after`, or errands() has nothing (or not black_tea x1) for it."""
        d = self.extract(op())
        got = rows(d.world_db, "select when_kind, status, text from intents")
        self.assertEqual([(r["when_kind"], r["status"]) for r in got], [("vague", "accepted")])
        errands = d.intents.errands("nell", T0 + 60)
        self.assertEqual([(e["item"], e["qty"]) for e in errands], [("black_tea", 1)])

    def test_a_row_that_stays_after_gives_no_errand(self):
        """Fails if a real-condition promise runs (so the rewrite is too wide), or if the check above proves nothing
        because errands() would also be empty for the plain outing text."""
        d = self.extract(op("奈尔答应房主，下次出门如果有打折就带一盒红茶回来"))
        got = rows(d.world_db, "select when_kind from intents")
        self.assertEqual([r["when_kind"] for r in got], ["after"])
        self.assertEqual(d.intents.errands("nell", T0 + 60), [])


if __name__ == "__main__":
    unittest.main()
