"""the live model's answers must become kernel steps, and an answer that cannot is reported, not
silently turned into "chose nothing" (11-04 real-model day: 20/20 answers dropped, 0 model actions)."""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, JST  # noqa: E402
from worldkernel import life_model as LM  # noqa: E402

CTX = {"actor": "nell", "decision": "free_time", "location": "living",
       "life": {"now": "2026-11-03 10:00", "weekday": "二"}}


def model_answering(text):
    return LM.LiveLifeModel(lambda system, user: (text, 100), {"nell": "奈尔"})


class Format(unittest.TestCase):
    def test_every_step_is_shown_in_full_shape(self):
        f = LM.life_format("nell", {})
        for name in LM.STEP_TYPES:
            self.assertIn(f'"action_type": "{name}"', f, name)
        self.assertIn('"args": {', f)


class Parse(unittest.TestCase):
    def steps(self, text):
        return model_answering(text)(dict(CTX))

    def test_kernel_shape(self):
        a = self.steps('{"steps": [{"action_type": "move", "args": {"to": "kitchen"}}]}')
        self.assertEqual(a["steps"], [{"action_type": "move", "args": {"to": "kitchen"}}])
        self.assertNotIn("unusable", a)

    def test_fenced(self):
        a = self.steps('```json\n{"steps": [{"action_type": "cook", "args": {"recipe": "curry"}}]}\n```')
        self.assertEqual(a["steps"][0]["args"], {"recipe": "curry"})

    def test_shorthand_name_as_key(self):  # the shape the old prompt showed: move {"to": ...}
        a = self.steps('{"steps": [{"move": {"to": "kitchen"}}, {"cook": {"recipe": "curry"}}]}')
        self.assertEqual([s["action_type"] for s in a["steps"]], ["move", "cook"])
        self.assertEqual(a["steps"][0]["args"], {"to": "kitchen"})

    def test_action_key_and_flat_args(self):
        a = self.steps('{"steps": [{"action": "begin_activity", "activity": "reading", "seconds": 1800},'
                       ' {"type": "say", "to": "elsa", "about": []}]}')
        self.assertEqual(a["steps"][0], {"action_type": "begin_activity",
                                         "args": {"activity": "reading", "seconds": 1800}})
        self.assertEqual(a["steps"][1], {"action_type": "say", "args": {"to": "elsa"}, "about": []})

    def test_invite_times_still_parsed(self):
        a = self.steps('{"steps": [{"invite": {"to": ["elsa"], "kind": "activity", "what": "reading",'
                       ' "location": "living", "start": "2026-11-03 20:00"}}]}')
        self.assertIn("start_at", a["steps"][0]["args"])

    def test_unknown_shape_is_reported(self):
        a = self.steps('{"steps": [{"go": "kitchen"}, {"action_type": "move", "args": {"to": "living"}}]}')
        self.assertEqual(len(a["steps"]), 1)
        self.assertEqual(a["unusable"], {"dropped": 1, "shapes": [["<other>"]]})

    def test_no_json_is_reported(self):
        a = self.steps("我想先去厨房看看。")
        self.assertEqual(a["steps"], [])
        self.assertEqual(a["unusable"], {"reason": "no_json"})

    def test_empty_choice_is_not_unusable(self):
        a = self.steps('{"steps": []}')
        self.assertEqual(a["steps"], [])
        self.assertNotIn("unusable", a)

    def test_answer_is_logged_without_content(self):  # only key names, never her words
        a = self.steps('{"steps": [{"secret_words": "我喜欢房主", "今天想和艾莎一起": true}]}')
        self.assertNotIn("我喜欢房主", json.dumps(a, ensure_ascii=False))
        self.assertNotIn("艾莎", json.dumps(a, ensure_ascii=False))
        self.assertEqual(a["unusable"]["shapes"], [["<other>"]])

    def test_kernel_shape_fields_stay_beside_args(self):
        a = self.steps('{"steps": [{"action_type": "say", "args": {"to": "elsa"}, "about": ["event:3"],'
                       ' "guide_id": "g1", "uses": []}]}')
        self.assertEqual(a["steps"], [{"action_type": "say", "args": {"to": "elsa"}, "about": ["event:3"],
                                       "guide_id": "g1", "uses": []}])

    def test_about_not_a_list_drops_only_that_step(self):  # review P2: would fail the whole answer for good
        a = self.steps('{"steps": [{"type": "say", "to": "elsa", "about": "昨天的事"},'
                       ' {"say": {"to": "elsa", "about": null}}, {"move": {"to": "living"}}]}')
        self.assertEqual([s["action_type"] for s in a["steps"]], ["move"])
        self.assertEqual(a["unusable"]["dropped"], 2)
        self.assertNotIn("昨天", json.dumps(a, ensure_ascii=False))

    def test_bad_json_no_steps_bad_steps(self):
        self.assertEqual(self.steps('{"steps": [{"move": }]}')["unusable"], {"reason": "bad_json"})
        self.assertEqual(self.steps('{"plan": "去厨房"}')["unusable"], {"reason": "no_steps", "shapes": [["<other>"]]})
        self.assertEqual(self.steps('{"steps": "move"}')["unusable"]["reason"], "bad_steps")

    def test_braces_after_the_answer(self):
        a = self.steps('{"steps": [{"action_type": "move", "args": {"to": "kitchen"}}]}\n（备注：{随便}）')
        self.assertEqual(len(a["steps"]), 1)
        self.assertNotIn("unusable", a)


class LiveDay(unittest.TestCase):
    """Six hours with a live model that answers in the old prompt's shorthand: her steps must land."""

    def run_hours(self, text, hours=6):
        d = tempfile.mkdtemp()
        start = dt.datetime(2026, 11, 3, 5, 0, tzinfo=JST).timestamp()
        clock = VirtualClock(start)
        s = Store(os.path.join(d, "k.db"), clock, world_id="ai-house/life1-test", synthetic=True, holder="test")
        s.acquire()
        life = Life(s, model=model_answering(text))
        while clock.now() < start + hours * 3600:
            life.step()
            clock.advance(60)
        db = sqlite3.connect(os.path.join(d, "k.db"))
        model_actions = db.execute("select count(*) from actions where origin='model'").fetchone()[0]
        reasons = dict(db.execute("select reason, count(*) from decision_points where outcome not in"
                                  " ('called', 'retry', 'deferred') group by reason").fetchall())
        s.release()
        return model_actions, reasons

    def test_shorthand_answers_become_actions(self):
        n, reasons = self.run_hours('```json\n{"steps": [{"move": {"to": "living"}}]}\n```')
        self.assertGreater(n, 0)
        self.assertNotIn("answer_unusable", reasons)

    def test_unusable_answers_are_not_chose_nothing(self):
        n, reasons = self.run_hours('{"steps": [{"go": "living"}]}')
        self.assertEqual(n, 0)
        self.assertGreater(reasons.get("answer_unusable", 0), 0)
        self.assertNotIn("chose_nothing", reasons)
        self.assertNotIn("unresolved_previous_call", reasons)

    def test_partly_unusable_answer_still_acts(self):  # one good step beside a dropped one crashed run_due
        n, reasons = self.run_hours('{"steps": [{"go": "living"}, {"move": {"to": "living"}}]}')
        self.assertGreater(n, 0)
        self.assertGreater(reasons.get("answer_partly_unusable", 0), 0)
        self.assertNotIn("answer_unusable", reasons)
        self.assertNotIn("chose_nothing", reasons)

    def test_bad_about_never_blocks_her_next_decisions(self):  # review P2, end to end
        n, reasons = self.run_hours('{"steps": [{"say": {"to": "elsa", "about": "昨天"}}]}', hours=30)
        self.assertNotIn("unresolved_previous_call", reasons)
        self.assertFalse(any(r.startswith("model_error") for r in reasons))


if __name__ == "__main__":
    unittest.main()
