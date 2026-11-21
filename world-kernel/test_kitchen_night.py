"""K6: the kitchen-night replay and its R1/R2 recording (for)."""
import json
import os
import tempfile
import unittest

import kitchen_night as KN


class KitchenNightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp()
        cls.manifest, cls.night = KN.record(cls.out)

    def load(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return json.load(f)

    def timeline(self):
        with open(os.path.join(self.out, "timeline.md"), encoding="utf-8") as f:
            return f.read()

    def pages(self):
        with open(os.path.join(self.out, "r2_pages.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_recording_passes_its_own_r1_r2_contract(self):
        self.assertEqual(self.manifest["verify"], "ok")
        self.assertEqual(KN.verify(self.load("r1_initial.json"), self.load("r1_mid.json"), self.load("r1_final.json"),
                                   self.pages()), [])

    def test_contract_check_catches_a_dropped_event(self):
        pages = self.pages()
        arrived = next(i for i, e in enumerate(pages[0]["events"]) if e["kind"] == "arrived")
        del pages[0]["events"][arrived]
        self.assertTrue(KN.verify(self.load("r1_initial.json"), None, self.load("r1_final.json"), pages))

    def test_same_seed_same_bytes(self):
        again, _ = KN.record(tempfile.mkdtemp())
        self.assertEqual(again["files"], self.manifest["files"])

    def test_zero_cost_and_within_budget(self):
        self.assertEqual(self.manifest["paid_calls"], 0)
        self.assertLessEqual(self.manifest["model_calls"], 60)
        self.assertTrue(all(c["actor"] in KN.THINKERS for c in self.night.model.calls))

    def test_the_evening_happened_without_casting_anyone(self):
        tl = self.timeline()
        self.assertIn("stale:resource_unavailable → retry", tl)  # wanda's cake plan lost to mina's meal
        self.assertIn("说：汤好了，一起喝吧？（对 wanda）", tl)
        final = self.load("r1_final.json")
        self.assertEqual(final["owner"]["state"], "unknown")  # client dropped: never "the owner left"
        self.assertEqual({v["location"] for v in final["actors"].values()}, {"exit_rooms"})
        for ev in (e for p in self.pages() for e in p["events"] if e["kind"] == "guide_offered"):
            self.assertEqual(set(ev), {"kind", "actor", "location", "at", "guide_id", "expires_at", "seq"})
            self.assertIsNone(ev["actor"])

    def test_private_things_stay_out_of_the_game_stream(self):
        text = json.dumps(self.pages(), ensure_ascii=False)
        self.assertNotIn("今晚有比赛", text)  # phone body
        self.assertNotIn("phone", text)
        self.assertNotIn("好香啊", text)  # owner's own words are not replayed to the game as a housemate event
        said = [e for p in self.pages() for e in p["events"] if e["kind"] in ("said", "talked_quietly")]
        self.assertEqual(said, [])  # the owner's avatar was not in the kitchen at 18:16: nothing to show

    def test_reading_waited_for_her_hands(self):
        tl = self.timeline().splitlines()
        rows = [r for r in tl if "nell" in r and ("phone_read" in r or "完成 cook" in r or "完成 eat" in r)]
        self.assertEqual([("phone_read" in r) for r in rows], [False, False, True])

    def test_mid_snapshot_is_a_valid_resume_point(self):
        mid = self.load("r1_mid.json")
        self.assertEqual(mid["owner"]["state"], "present")
        world, epoch, aud, seq = mid["cursor"].rsplit("|", 3)
        self.assertEqual((world, epoch, aud, int(seq)), (KN.WORLD, "night-1", "game", mid["view_revision"]))


if __name__ == "__main__":
    unittest.main()
