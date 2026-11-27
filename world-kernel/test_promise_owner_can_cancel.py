"""The owner can cancel a promise made to him; only the one who promised can say it is done.

Defect (live run): she promised to bring a cola, the owner said "后天的可乐不用买了，我自己带了。", the model's intent_drop
quoted the OWNER's line and was rejected ("no evidence from anyone in it"), so the promise stayed open. No network, no real
model. Every test says what would make it fail.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir, T0, fake_model  # noqa: E402

ASK = "后天出门时帮我带一瓶可乐回来吧"
COLA = "奈尔答应房主，11月23日帮他带一瓶可乐回来"
CANCEL = "后天的可乐不用买了，我自己带了。"
ANSWER = "好，那后天的可乐我就不买啦。"


class Cancel(Dir):
    def run_batch(self, owner_line, reply, op_extra):
        d = self.daemon(env={"INTENTS": "1"})
        make = {"op": "intent", "text": COLA, "kind": "promise", "for": ["房主"], "agreed": ["奈尔"],
                "scope": "world", "when": "vague", "quote": ASK}
        d.memory.observe("nell", "private", ASK, speaker_role="owner", speaker="房主", reply="好的，可乐我记下了", now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model([make]))
        row = d.intents.list_intents(actor="nell")[0]
        self.assertEqual(row["status"], "accepted")
        d.memory.observe("nell", "private", owner_line, speaker_role="owner", speaker="房主", reply=reply, now=T0 + 100)
        d.memory.extract(force=True, now=T0 + 105, llm_fn=fake_model([{"id": int(row["intent_id"]), **op_extra}]))
        return d.intents.list_intents(actor="nell")[0]["status"]

    def test_drop_quoting_the_owner_ends_it(self):
        """Fails if a drop that quotes the owner's cancellation is rejected and the row stays accepted."""
        self.assertEqual(self.run_batch(CANCEL, ANSWER, {"op": "intent_drop", "why": "cancelled", "quote": CANCEL}),
                         "cancelled")

    def test_drop_quoting_her_still_ends_it(self):
        """Fails if quoting her own answer stops working."""
        self.assertEqual(self.run_batch(CANCEL, ANSWER, {"op": "intent_drop", "why": "cancelled", "quote": ANSWER}),
                         "cancelled")

    def test_done_quoting_the_owner_is_rejected(self):
        """Fails if the owner's line can mark her promise as completed."""
        self.assertEqual(self.run_batch("可乐我已经收到了", ANSWER, {"op": "intent_done", "quote": "可乐我已经收到了"}),
                         "accepted")

    def test_drop_quoting_the_owner_question_is_rejected(self):
        """Fails if a question from the owner ends the promise."""
        self.assertEqual(self.run_batch("可乐还要买吗？", "要的", {"op": "intent_drop", "why": "cancelled",
                                                              "quote": "可乐还要买吗？"}), "accepted")


if __name__ == "__main__":
    unittest.main()
