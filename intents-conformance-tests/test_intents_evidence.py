"""Task 87 synthetic evidence checks; no model, network or production database."""
import json
from unittest.mock import patch

import intents as I
import world as W
from test_intents import IntentBase, NELL, WANDA, G

# Ten fixed, separately scored cases. Unknown numeric identities are synthetic.
CASES = [
    ("unrelated_yes", "new", [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 今天咖啡好喝"], False),
    ("dream_done", "done", [f"{WANDA} 梦里红茶买回来了"], False),
    ("uncertain_done", "done", [f"{WANDA} 红茶差不多买回来了吧"], False),
    ("proxy_yes", "new", [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 由纪说好呀"], False),
    ("unrelated_intervening", "new", [f"{NELL} 明天下午一起去买红茶吧？", f"{NELL} 窗帘颜色怎么样", f"{WANDA} 好呀"], False),
    ("direct_yes", "new", [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 好呀"], True),
    ("topic_yes", "new", [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 好，红茶一起买"], True),
    ("unknown_named_actor", "new", [f"{NELL} 明天下午一起去买红茶吧？", "[婉妲|999000111] 好呀"], False),
    ("different_task_done", "done", [f"{WANDA} 窗帘换好了"], False),
    ("real_done", "done", [f"{WANDA} 红茶买回来了"], True),
]


def run_case(t, case):
    name, kind, lines, expected = case
    with W.db() as c:
        c.execute("delete from intents")
    if kind == "new":
        t.tea(lines)
        rows = t.rows()
        actual = bool(rows and rows[0]["status"] == "accepted")
    else:
        t.tea([f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 好呀"])
        iid = t.rows()[0]["id"]
        quote = lines[0].split("] ", 1)[1]
        I.apply_chat("nell", G, 0, [{"op": "intent_done", "id": iid, "quote": quote}], lines, now=t.now)
        actual = t.rows()[0]["status"] == "completed"
    return {"case": name, "expected": expected, "actual": actual}


class EvidenceTests(IntentBase):
    def test_ten_cases(self):
        for case in CASES:
            with self.subTest(case=case[0]):
                r = run_case(self, case)
                self.assertEqual(r["actual"], r["expected"])

    def test_done_fiction_negation_joke_and_question(self):
        for text in ("假如红茶买回来了", "开玩笑说红茶买回来了", "红茶没有买回来", "红茶买回来了（反话）",
                     "红茶买回来了吗？", "应该红茶买回来了", "红茶买回来了吧，大概"):
            with self.subTest(text=text):
                self.assertFalse(run_case(self, ("negative", "done", [f"{WANDA} {text}"], False))["actual"])

    def test_affirmative_substrings_are_not_consent(self):
        for text in ("好看的花", "今天红茶好喝", "好，咖啡真香", "她说红茶一起买", "梦里答应买红茶",
                     "好呀，不过我不去", "好呀，这是玩笑", "好呀？当然不去", "好，红茶一起买但我不参加",
                     "好，红茶一起买，由纪说的"):
            with self.subTest(text=text):
                self.assertFalse(run_case(self, ("unrelated", "new",
                    [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} {text}"], False))["actual"])

    def test_unknown_identity_cannot_finish(self):
        self.assertFalse(run_case(self, ("unknown", "done", ["[婉妲|999000111] 红茶买回来了"], False))["actual"])

    def test_later_explicit_quote_links_to_the_proposal(self):
        result = run_case(self, ("explicit_quote", "new", [
            f"{NELL} 明天下午一起去买红茶吧？", f"{NELL} 窗帘颜色怎么样",
            f'{WANDA} 「明天下午一起去买红茶吧？」好呀'], True))
        self.assertTrue(result["actual"])
        self.assertTrue(run_case(self, ("participation", "new",
            [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 好，我们一起去"], True))["actual"])

    def test_quote_from_unrelated_matter_does_not_become_proposal(self):
        result = self.tea([f"{NELL} 我今天换窗帘", f"{WANDA} 好呀"], quote="我今天换窗帘")
        self.assertEqual(result["intents"], 0)

    def test_personal_plan_with_same_words_is_bound_to_its_author(self):
        result = I.apply_chat("nell", G, 0, [{"op": "intent", "kind": "plan", "text": "奈尔打算更换窗帘",
            "quote": "我要更换窗帘"}], [f"{WANDA} 我要更换窗帘", f"{NELL} 我要更换窗帘"], now=self.now)
        self.assertEqual(result["intents"], 1)
        self.assertEqual(self.rows()[0]["party"], ["nell"])

    def test_other_quote_match_cannot_supply_the_speakers_line(self):
        # An outsider's matching quote is not completion evidence for a participant.
        self.tea([f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 好呀"])
        iid = self.rows()[0]["id"]
        got = I.apply_chat("nell", G, 0, [{"op": "intent_done", "id": iid, "quote": "红茶买回来"}],
            ["[访客|999000111] 红茶买回来了", f"{WANDA} 梦里红茶买回来了"], now=self.now)
        self.assertEqual(got["ended"], 0)

    def test_switch_off_retains_old_behavior(self):
        with patch.object(I, "EVIDENCE_GATE", False, create=True):
            self.assertTrue(run_case(self, CASES[0])["actual"])

    def test_default_show_stays_off(self):
        import ast
        from pathlib import Path
        source = ast.parse(Path(I.__file__).read_text())
        assignment = next(n for n in source.body if isinstance(n, ast.Assign)
                          and any(isinstance(v, ast.Name) and v.id == "SHOW" for v in n.targets))
        self.assertIn('"0"', ast.unparse(assignment).replace("'", '"'))


if __name__ == "__main__":
    import unittest
    unittest.main()
