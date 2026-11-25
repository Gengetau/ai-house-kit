"""M2: promise_rules bound to a pack. The module-level functions are the default pack's; bound(vocab) is any pack's."""
import os
import unittest
from pathlib import Path

from worldkernel import promise_rules as P, vocab_life1 as V

PACK = str(Path(__file__).resolve().parents[1] / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


def accepted(text, party, terms, now=1000.0):
    return {"id": 7, "status": "accepted", "party": party, "terms": terms, "created": now, "said_at": now,
            "deadline": now + 86400, "beneficiary": [], "scope": "world"}


def purchase(actor, now=1100.0, item="egg", qty=2):
    return {"key": "k1", "kind": "purchased", "actor": actor, "at": now, "item": item, "qty": qty, "account": actor,
            "action_id": "a1", "resource_id": "r1", "intent_ref": 7, "beneficiary": [], "plan_id": "p1"}


class DefaultPack(unittest.TestCase):
    def test_module_level_functions_keep_the_default_people(self):
        self.assertEqual(P.compile_terms("奈尔买2个鸡蛋", V, ["nell"]), [{"kind": "purchased", "item": "egg", "qty": 2}])
        terms = P.compile_terms("奈尔买2个鸡蛋", V, ["nell"])
        done = P.judge(accepted("", ["nell"], terms), [purchase("nell")], 2000.0)
        self.assertTrue(done["complete"])
        self.assertFalse(P.judge(accepted("", ["sora"], terms), [purchase("sora")], 2000.0)["complete"])

    def test_bound_to_the_default_vocab_gives_the_same_answers(self):
        r = P.bound(V)
        for text in ("奈尔买2个鸡蛋", "婉妲去超市买两袋薯片带回来", "艾莎买薯片并修好电脑", "由纪买薯片"):
            self.assertEqual(r.compile_terms(text, V, ["nell"]), P.compile_terms(text, V, ["nell"]), text)
            self.assertEqual(r.compile_terms(text, party=["nell"]), P.compile_terms(text, V, ["nell"]), text)
        terms = P.compile_terms("奈尔买2个鸡蛋", V, ["nell"])
        for ev in ([purchase("nell")], [purchase("nell", qty=1)], []):
            it = accepted("", ["nell"], terms)
            self.assertEqual(r.judge(it, ev, 2000.0), P.judge(it, ev, 2000.0))


class OtherPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = V.load_pack(PACK)
        cls.rules = P.bound(cls.pack)

    def test_the_module_level_rules_cannot_finish_an_errand_here(self):
        self.assertEqual(P.compile_terms("苍空买2个鸡蛋", self.pack, ["sora"]), [{"kind": "unsupported"}])
        terms = [{"kind": "purchased", "item": "egg", "qty": 2}]
        self.assertFalse(P.judge(accepted("", ["sora"], terms), [purchase("sora")], 2000.0)["complete"])

    def test_a_promise_is_completed(self):
        terms = self.rules.compile_terms("苍空买2个鸡蛋", self.pack, ["sora"])
        self.assertEqual(terms, [{"kind": "purchased", "item": "egg", "qty": 2}])
        it = accepted("", ["sora"], terms)
        self.assertFalse(self.rules.judge(it, [purchase("sora", qty=1)], 2000.0)["complete"])
        done = self.rules.judge(it, [purchase("sora")], 2000.0)
        self.assertTrue(done["complete"])
        self.assertEqual(done["keys"], ["k1"])

    def test_two_housemates_and_the_owner_name_are_not_residue(self):
        t = self.rules.compile_terms("苍空和凛音买2个鸡蛋", self.pack, ["sora", "rin"])
        self.assertEqual(t, [{"kind": "purchased", "item": "egg", "qty": 2}])
        self.assertEqual(self.rules.compile_terms("房主和苍空买2个鸡蛋", self.pack, ["sora"]), t)

    def test_names_of_another_pack_are_not_recognised(self):
        self.assertEqual(self.rules.compile_terms("奈尔买2个鸡蛋", self.pack, ["sora"]), [{"kind": "unsupported"}])
        it = accepted("", ["nell"], [{"kind": "purchased", "item": "egg", "qty": 2}])
        self.assertFalse(self.rules.judge(it, [purchase("nell")], 2000.0)["complete"])
        # and the other way: the default pack's rules do not know this pack's people
        default = P.bound(V)
        self.assertEqual(default.compile_terms("苍空买2个鸡蛋", self.pack, ["sora"]), [{"kind": "unsupported"}])


if __name__ == "__main__":
    unittest.main()
