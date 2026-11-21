"""聊天介入生活: world.db owner_chat_queue -> one decision of her own in the house; and his own
「我到家了」 from chat puts the avatar in for two hours (owner card 11-06).
Run: python3 -m unittest -v test_chatlink.py"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from worldkernel import avatar, chatlink
from worldkernel import vocab_life1 as V
from worldkernel.life import Life
from worldkernel.life_model import FakeLifeModel
from worldkernel.store import Store, VirtualClock
from worldkernel.wallet import JST


def at(clock="22:30", day="2026-11-06"):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=JST).timestamp()


class ChatLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world_db = os.path.join(self.tmp.name, "world.db")
        c = sqlite3.connect(self.world_db)
        c.execute("create table owner_chat_queue(id integer primary key, actor text, body text, state text, created real)")
        c.commit()
        c.close()
        self.clock = VirtualClock(at())
        self.s = Store(os.path.join(self.tmp.name, "life1.db"), self.clock, world_id="test/chatlink")
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.l = Life(self.s, model=FakeLifeModel(), weather=lambda _: dict(rain=False, temp_c=12),
                      extensions=[avatar.install(self.world_db), chatlink.install(self.world_db)])
        self.av, self.cl = self.l.extensions

    def place(self, room, *who, activity="idle"):
        with self.s.tx() as db:
            for a in who:
                db.execute("UPDATE actors SET location=?, activity=?, action_id=NULL WHERE actor_id=?",
                           (room, activity, a))

    def chat(self, actor, where="private", owner=("今晚早点睡",), she=("好，我这就去睡",), intents=()):
        body = {"where": where, "chat": "private" if where == "private" else "9000000001", "owner_said": list(owner),
                "she_said": list(she), "intents": list(intents), "at": self.clock.now()}
        c = sqlite3.connect(self.world_db)
        with c:
            c.execute("insert into owner_chat_queue(actor, body, state, created) values (?,?,?,?)",
                      (actor, json.dumps(body, ensure_ascii=False), "queued", self.clock.now()))
        c.close()

    def sleep_intent(self, a):
        return {"id": 1, "kind": "promise", "text": "今晚早点睡", "status": "accepted", "party": [a, "owner"],
                "agreed": [a], "due_from": None, "due_to": None}

    def run_steps(self, n=4):
        for _ in range(n):
            for ext in self.l.extensions:
                ext.step()
            self.l.cog.run_due()
            self.l.k.tick()
            self.l.cog.advance_plans()
            self.l.k.tick()
            self.clock.advance(60)

    def asks(self):
        return sum(v for k, v in self.l_items("chat:asks:"))

    def l_items(self, prefix):
        hour = int(self.clock.now() // 3600)
        return [(f"{prefix}{h}", self.l.get(f"{prefix}{h}", 0)) for h in range(hour - 2, hour + 1)]

    def test_she_keeps_her_promise_to_sleep(self):
        self.place("living", "elsa")
        self.chat("elsa", intents=[self.sleep_intent("elsa")])
        self.run_steps(12)
        me = self.l.k.actor("elsa")
        self.assertEqual((me["location"], me["activity"]), (V.bed("elsa"), "sleeping"))
        self.assertEqual(self.l.get("chat:elsa")[0]["owner_said"], ["今晚早点睡"])

    def test_group_talk_without_a_plan_only_stays_in_front_of_her(self):
        self.place("living", "nell")
        self.chat("nell", where="group", owner=("今天好冷",), she=("是呀",))
        self.run_steps(2)
        self.assertEqual(self.asks(), 0)
        self.assertEqual(self.cl.context("nell", "free_time")["qq_chat"][0]["在哪"], "小屋群聊")

    def test_asleep_she_is_not_woken(self):
        self.place(V.bed("alma"), "alma", activity="sleeping")
        self.chat("alma", intents=[self.sleep_intent("alma")])
        self.run_steps(2)
        self.assertEqual((self.asks(), self.l.k.actor("alma")["activity"]), (0, "sleeping"))
        self.assertTrue(self.l.get("chat:alma"))  # she will have it in front of her when she next decides

    def test_a_private_chat_alone_asks_her_at_most_every_15_min(self):
        self.place("study", "mina")
        self.chat("mina", owner=("在干嘛",), she=("看书",))
        self.run_steps(3)
        self.chat("mina", owner=("好看吗",), she=("还行",))
        self.run_steps(3)
        self.assertEqual(self.asks(), 1)

    def test_hour_cap(self):
        for a in V.PEOPLE:
            self.place("living", a)
        self.l.put(f"chat:asks:{int(self.clock.now() // 3600)}", chatlink.PER_HOUR)
        self.chat("wanda", intents=[self.sleep_intent("wanda")])
        self.run_steps(1)
        self.assertTrue(self.l.get("chat:missed:wanda"))

    def test_said_home_on_qq_he_stays_two_hours_and_out_takes_him_out(self):
        import json
        import sqlite3

        def from_chat(body):  # what shared-services intents.presence_from_chat writes (the page cannot send via)
            with sqlite3.connect(self.world_db) as c:
                c.execute(avatar.QUEUE_SCHEMA)
                c.execute("INSERT INTO owner_avatar_queue VALUES(?,?,'queued',NULL,?)",
                          (body["cmd_id"], json.dumps(body, sort_keys=True), self.clock.now()))
        self.place("living", "yuki")
        from_chat({"op": "enter", "cmd_id": "chat-a", "session": "qqchat", "via": "chat", "location": "living",
                   "said_at": self.clock.now()})
        self.run_steps(1)
        self.clock.advance(30 * 60)
        self.assertEqual(avatar.stage(self.s.path, self.clock.now())["owner"]["location"], "living")
        from_chat({"op": "leave", "cmd_id": "chat-b", "session": "qqchat", "via": "chat",
                   "said_at": self.clock.now()})
        self.run_steps(1)
        self.assertIsNone(avatar.stage(self.s.path, self.clock.now())["owner"])


if __name__ == "__main__":
    unittest.main()
