"""房主化身走进小屋: inbox -> kernel -> the roommates in that room answer, in the house only."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from worldkernel import avatar
from worldkernel import vocab_life1 as V
from worldkernel.life import Life, ROSTER
from worldkernel.life_model import FakeLifeModel
from worldkernel.store import Store, VirtualClock
from worldkernel.wallet import JST


def at(clock="15:00", day="2026-11-06"):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=JST).timestamp()


class AvatarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world_db = os.path.join(self.tmp.name, "world.db")
        sqlite3.connect(self.world_db).close()
        self.clock = VirtualClock(at())
        self.s = Store(os.path.join(self.tmp.name, "life1.db"), self.clock, world_id="test/avatar")
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.l = Life(self.s, model=FakeLifeModel(), weather=lambda _: dict(rain=False, temp_c=15),
                      extensions=[avatar.install(self.world_db)])
        self.av = self.l.extensions[0]
        self.n = 0

    def place(self, room, *who, activity="idle"):
        with self.s.tx() as db:
            for a in who:
                db.execute("UPDATE actors SET location=?, activity=?, action_id=NULL WHERE actor_id=?",
                           (room, activity, a))

    def send(self, op, **kw):
        self.n += 1
        body = dict(op=op, cmd_id=f"c{self.n}", session="s1", **kw)
        return avatar.queue(self.world_db, "cmd:owner", body, self.clock.now())["cmd_id"]

    def run_steps(self, n=4):
        for _ in range(n):
            self.av.step()
            self.l.cog.run_due()
            self.l.k.tick()
            self.l.cog.advance_plans()
            self.l.k.tick()
            self.clock.advance(60)

    def said(self, room):
        with self.s.read() as db:
            return [(r["actor_id"], json.loads(r["payload"])["text"]) for r in db.execute(
                "SELECT actor_id, payload FROM events WHERE kind='said' AND location=? ORDER BY event_id", (room,))]

    def test_enter_and_say_get_answers_in_the_room_only(self):
        self.place("living", "nell", "elsa")
        self.place("study", "alma")
        self.send("enter", location="living")
        self.run_steps()
        who = {a for a, _ in self.said("living")}
        self.assertEqual(who, {"nell", "elsa"})
        self.assertEqual(self.said("study"), [])
        cid = self.send("say", text="我回来啦，今天好冷")
        self.run_steps()
        self.assertEqual(avatar.receipt(self.world_db, cid)["status"], "succeeded")
        with self.s.read() as db:
            seen = {r[0] for r in db.execute("SELECT o.observer_id FROM events e JOIN observations o USING(event_id)"
                                             " WHERE e.kind='owner_input_received'")}
        self.assertEqual(seen, {"nell", "elsa", "owner"})  # alma in the study never heard it
        self.assertIn(("nell", "嗯，我在听。"), self.said("living"))

    def test_sleeping_roommate_does_not_answer(self):
        self.place("living", "wanda", activity="sleeping")
        self.send("enter", location="living")
        self.run_steps()
        self.assertEqual(self.said("living"), [])

    def test_give_moves_one_snack_to_her(self):
        self.place("kitchen", "mina")
        with self.s.read() as db:
            snack = next(r for r in db.execute("SELECT resource_id, item, qty FROM resources WHERE kind='food'"
                                               " AND location='kitchen' AND holder IS NULL AND qty>0")
                         if r["item"] in V.SNACKS)
        self.send("enter", location="kitchen")
        cid = self.send("give", to="mina", resource_id=snack["resource_id"])
        self.run_steps(1)
        got = avatar.receipt(self.world_db, cid)
        self.assertEqual(got["status"], "succeeded", got)
        with self.s.read() as db:
            left = db.execute("SELECT qty FROM resources WHERE resource_id=?", (snack["resource_id"],)).fetchone()[0]
            gift = db.execute("SELECT holder, qty, source FROM resources WHERE resource_id=?",
                              (got["resource_id"],)).fetchone()
        self.assertEqual(left, snack["qty"] - 1)
        self.assertEqual((gift["holder"], gift["qty"]), ("mina", 1))
        self.assertTrue(gift["source"].startswith("owner_gift:"))
        self.run_steps(3)
        self.assertIn(("mina", "谢谢房主！"), self.said("kitchen"))

    def test_give_refused_when_she_is_elsewhere_or_not_a_snack(self):
        self.place("study", "yuki")
        self.send("enter", location="kitchen")
        a = self.send("give", to="yuki", resource_id="nope")
        self.run_steps(1)
        self.assertEqual(avatar.receipt(self.world_db, a)["reason"], "not_here")
        self.place("kitchen", "yuki")
        b = self.send("give", to="yuki", resource_id="nope")
        self.run_steps(1)
        self.assertEqual(avatar.receipt(self.world_db, b)["reason"], "not_a_house_snack")

    def test_say_without_being_in_the_house_is_refused(self):
        cid = self.send("say", text="有人吗")
        self.run_steps(1)
        self.assertEqual(avatar.receipt(self.world_db, cid)["reason"], "not_in_house")

    def test_only_owner_and_checked_bodies(self):
        self.assertEqual(avatar.queue(self.world_db, "channel:nell", dict(op="enter"), 0)["reason"], "owner_only")
        self.assertEqual(self.av.apply(dict(op="leave", cmd_id="x", session="s"), principal="routine")["reason"],
                         "owner_only")
        for bad in (dict(op="fly", cmd_id="a", session="s"), dict(op="enter", cmd_id="a", session="s", location="moon"),
                    dict(op="say", cmd_id="a", session="s", text="x" * 201), dict(op="say", cmd_id="a b", session="s",
                                                                                   text="hi")):
            with self.assertRaises(avatar.AvatarError):
                avatar.queue(self.world_db, "cmd:owner", bad, 0)

    def test_replay_is_idempotent_and_conflict_refused(self):
        self.send("enter", location="living")
        body = dict(op="say", cmd_id="same", session="s1", text="一次")
        avatar.queue(self.world_db, "cmd:owner", body, self.clock.now())  # queued now (older ones expire)
        self.run_steps(1)
        first = avatar.receipt(self.world_db, "same")
        self.assertEqual(self.av.apply(body), first)  # a crash between kernel commit and inbox ack: no second line
        with self.s.read() as db:
            n = db.execute("SELECT COUNT(*) FROM events WHERE kind='owner_input_received'").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(avatar.queue(self.world_db, "cmd:owner", dict(body, text="两次"), 0)["reason"], "cmd_conflict")

    def test_move_and_leave_and_lease(self):
        self.send("enter", location="living")
        self.send("move", location="kitchen")
        self.run_steps(1)
        st = avatar.stage(self.s.path if hasattr(self.s, "path") else os.path.join(self.tmp.name, "life1.db"),
                          self.clock.now())
        self.assertEqual(st["owner"]["location"], "kitchen")
        kinds = [e["kind"] for e in st["events"]]
        self.assertEqual(kinds[:3], ["owner_entered", "owner_left_room", "owner_entered"])
        self.clock.advance(avatar.LEASE_S + 60)
        self.l.k.expire_presence()
        self.assertIsNone(avatar.stage(os.path.join(self.tmp.name, "life1.db"), self.clock.now())["owner"])

    def test_stage_hides_quiet_words_between_roommates(self):
        self.place("living", "nell", "elsa")
        with self.s.tx():
            self.l.k._emit("said", "nell", None, "living", {"text": "悄悄话", "to": "elsa"}, scope="dm:elsa,nell",
                           occurred=self.clock.now(), knowers=["nell", "elsa"])
            self.l.k._emit("said", "nell", None, "living", {"text": "大家好"}, scope="room:living",
                           occurred=self.clock.now())
        texts = [e.get("text") for e in avatar.stage(os.path.join(self.tmp.name, "life1.db"), self.clock.now())["events"]]
        self.assertEqual(texts, ["大家好"])
        snacks = avatar.stage(os.path.join(self.tmp.name, "life1.db"), self.clock.now())["snacks"]
        self.assertTrue(snacks and all(s["item"] in V.SNACKS for s in snacks))

    def test_reactions_are_capped_per_hour(self):
        self.place("living", *ROSTER)
        for i in range(avatar.REACT_PER_HOUR):
            self.send("say", text=f"第{i}句") if i else self.send("enter", location="living")
            self.run_steps(1)
        with self.s.read() as db:
            calls = db.execute("SELECT COUNT(*) FROM cognition_requests WHERE purpose!='express'").fetchone()[0]
        self.assertLessEqual(calls, avatar.REACT_PER_HOUR + len(ROSTER))


if __name__ == "__main__":
    unittest.main()
