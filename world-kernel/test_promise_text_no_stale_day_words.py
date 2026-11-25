"""A promise text must not keep a stale relative-day note such as （后天）next to its absolute date.

Defect (live run): the text "…11月24日（后天）帮房主带一瓶可乐回来" was shown back to her a day later and she said the cola
was "后天的事". No network, no real model. Every test says what would make it fail."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir, T0, fake_model  # noqa: E402

from ai_house.intents_common import strip_stale_day_notes as clean  # noqa: E402

OWNER_LINE = "后天出门时帮我带一瓶可乐回来吧"
COLA = "奈尔答应房主：11月24日（后天）帮房主带一瓶可乐回来"


class CleanTests(unittest.TestCase):
    def test_the_note_goes_when_an_absolute_date_stays(self):
        """Fails if the relative note stays, or anything else in the text changes."""
        self.assertEqual(clean(COLA), "奈尔答应房主：11月24日帮房主带一瓶可乐回来")
        self.assertEqual(clean("和房主11月25日（大后天）一起去公园散步"), "和房主11月25日一起去公园散步")
        self.assertEqual(clean("奈尔答应房主：11月22日（原话说“明天”）顺路带两盒牛奶回来"),
                         "奈尔答应房主：11月22日顺路带两盒牛奶回来")
        self.assertEqual(clean("奈尔答应房主：2026-11-24 (即「明晚」) 带牛奶回来"), "奈尔答应房主：2026-11-24  带牛奶回来")

    def test_no_absolute_date_keeps_the_note(self):
        """Fails if the only date information is cut."""
        text = "奈尔答应房主：（明天）带两盒牛奶回来"
        self.assertEqual(clean(text), text)

    def test_other_notes_stay(self):
        """Fails if a note that is not a relative day word is cut."""
        text = "奈尔答应房主：11月22日（周日）买牛奶"
        self.assertEqual(clean(text), text)
        self.assertEqual(clean("奈尔答应房主：11月22日（明天下午）买牛奶"), "奈尔答应房主：11月22日（明天下午）买牛奶")

    def test_not_a_string(self):
        self.assertIsNone(clean(None))


class WrapperTests(Dir):
    def test_the_stored_row_has_the_cleaned_text_and_the_op_is_not_mutated(self):
        """Fails if the intents row keeps （后天）, or the caller's op dict is changed."""
        d = self.daemon(env={"INTENTS": "1"})
        op = {"op": "intent", "text": COLA, "kind": "promise", "for": ["房主"], "agreed": ["奈尔"],
              "scope": "world", "when": "vague", "quote": OWNER_LINE}
        before = dict(op)
        d.memory.observe("nell", "private", OWNER_LINE, speaker_role="owner", speaker="房主", reply="好的", now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model([op]))
        with d.intents._conn() as c:
            texts = [r["text"] for r in c.execute("select text from intents")]
        self.assertEqual(texts, ["奈尔答应房主：11月24日帮房主带一瓶可乐回来"])
        self.assertEqual(op, before)


if __name__ == "__main__":
    unittest.main()
