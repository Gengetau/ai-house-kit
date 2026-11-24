"""意图与承诺 V1 (intents.py, 2026-10-20): found from chat with evidence, joint needs both, no duplicates, the owner's
own plans not kept, done only with evidence, quiet expiry, card / world / outreach hooks.
Run: python3 -m unittest -v test_intents.py   (temp database, no model, no network)."""

import json
from unittest.mock import patch

import intents as I
import world as W
from test_world import WorldBase, answer, at

NELL, WANDA, OWNER = "[奈尔(你)|1000000074]", "[婉妲|1000000037]", "[房主别名1|1000000259]"
G = "group:1000000296"


class IntentBase(WorldBase):
    def setUp(self):
        super().setUp()
        for p in (patch.object(I, "ON", True), patch.object(I, "SHOW", True), patch.object(W, "INTENTS", I)):
            p.start()
            self.addCleanup(p.stop)
        I.init()

    def rows(self):
        with W.db() as c:
            return [I._row(r) for r in c.execute("select * from intents order by id")]

    def tea(self, lines, consumer="nell", private=0, now=None, **kw):
        op = {"op": "intent", "text": "奈尔和婉妲明天下午一起去买红茶", "kind": "joint", "with": ["婉妲"],
              "agreed": ["奈尔", "婉妲"], "due": "2026-10-15 15:00", "quote": "明天下午一起去买红茶吧", **kw}
        return I.apply_chat(consumer, G if not private else "private:1000000259", private, [op], lines,
                            now=now or self.now)


class FoundTests(IntentBase):
    both = [f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 好呀，三点出门"]

    def test_joint_needs_both_to_speak(self):
        self.assertEqual(self.tea([f"{NELL} 明天下午一起去买红茶吧？"])["intents"], 1)
        r = self.rows()[0]
        self.assertEqual((r["status"], r["agreed"], r["party"]), ("proposed", ["nell"], ["nell", "wanda"]))
        self.tea(self.both)  # 婉妲 says yes later: the same one, now agreed
        r, = self.rows()
        self.assertEqual((r["status"], r["agreed"]), ("accepted", ["nell", "wanda"]))
        self.assertEqual((r["due_from"], r["due_to"]), (at(14, day=15), at(17, day=15)))

    def test_repeated_and_roommates_copy_do_not_duplicate(self):
        self.tea(self.both)
        self.tea(self.both, now=self.now + 60)
        self.tea(self.both, consumer="wanda_c", text="婉妲和奈尔明天下午一起去买红茶", **{"with": ["奈尔"]})
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.rows()[0]["refs"]), 3)

    def test_talking_is_not_saying_yes(self):
        """婉妲's review (#4): she spoke, but not to agree; the model calling her agreed is not enough."""
        self.tea([f"{NELL} 明天下午一起去买红茶吧？", f"{WANDA} 茶叶放柜子里", f"{WANDA} 一起？"])
        self.assertEqual((self.rows()[0]["status"], self.rows()[0]["agreed"]), ("proposed", ["nell"]))

    def test_quote_must_be_in_the_batch(self):
        got = self.tea(self.both, quote="下周去海边")
        self.assertEqual((got["intents"], got["rejected"]), (0, 1))
        self.assertEqual(self.rows(), [])

    def test_the_owners_own_plan_is_not_hers(self):
        got = I.apply_chat("nell", G, 0, [{"op": "intent", "text": "房主明天给奈尔买蛋糕", "with": ["房主"],
                                             "kind": "joint", "quote": "明天给你买蛋糕"}],
                           [f"{OWNER} 明天给你买蛋糕", f"{WANDA} 我也要"], now=self.now)
        self.assertEqual(got["rejected"], 1)
        self.assertEqual(self.rows(), [])

    def test_own_plan_and_private_text(self):
        I.apply_chat("nell", G, 0, [{"op": "intent", "text": "奈尔打算今晚读完《雪国》", "kind": "plan",
                                       "quote": "今晚要把雪国读完"}], [f"{NELL} 今晚要把雪国读完"], now=self.now)
        I.apply_chat("nell", G, 0, [{"op": "intent", "text": "奈尔把电话 09012345678 记下", "kind": "plan",
                                       "quote": "记一下"}], [f"{NELL} 记一下"], now=self.now)
        r, = self.rows()
        self.assertEqual((r["kind"], r["status"], r["party"]), ("plan", "accepted", ["nell"]))

    def test_caps(self):
        things = ["读完雪国", "烤一盘司康", "整理书架", "给花浇水", "写读书笔记", "练习钢琴", "缝好裙摆", "擦窗户", "泡梅酒",
                  "修台灯"]
        lines = [f"{NELL} 我要{t}" for t in things]
        ops = [{"op": "intent", "text": f"奈尔打算{t}", "kind": "plan", "quote": f"我要{t}"} for t in things]
        got = I.apply_chat("nell", G, 0, ops, lines, now=self.now)
        self.assertEqual(got["intents"], I.PER_DAY)


class EndTests(IntentBase):
    def setUp(self):
        super().setUp()
        self.tea(FoundTests.both)
        self.iid = self.rows()[0]["id"]

    def test_done_needs_a_quote_from_someone_in_it(self):
        bad = I.apply_chat("nell", G, 0, [{"op": "intent_done", "id": self.iid, "quote": "红茶买回来了"}],
                           [f"{OWNER} 红茶买回来了吗"], now=self.now)
        self.assertEqual(bad["rejected"], 1)
        ok = I.apply_chat("nell", G, 0, [{"op": "intent_done", "id": self.iid, "quote": "红茶买回来了"}],
                          [f"{WANDA} 红茶买回来了，真香"], now=self.now)
        self.assertEqual(ok["ended"], 1)
        self.assertEqual(self.rows()[0]["status"], "completed")

    def test_asking_or_almost_is_not_done(self):
        for line in ("红茶买回来了吗？", "红茶快买回来了", "红茶应该买回来了"):
            got = I.apply_chat("wanda_c", G, 0, [{"op": "intent_done", "id": self.iid, "quote": line[:6]}],
                               [f"{WANDA} {line}"], now=self.now)
            self.assertEqual(got["rejected"], 1, line)
        self.assertEqual(self.rows()[0]["status"], "accepted")

    def test_declined(self):
        I.apply_chat("wanda_c", G, 0, [{"op": "intent_drop", "id": self.iid, "why": "declined", "quote": "明天去不了"}],
                     [f"{WANDA} 抱歉明天去不了了"], now=self.now)
        self.assertEqual(self.rows()[0]["status"], "declined")

    def test_quiet_expiry_and_restore(self):
        self.assertEqual(I.card_lines("nell", now=at(18, day=15)), [])  # the window has closed: not shown
        self.assertEqual(self.rows()[0]["status"], "accepted")
        I.card_lines("nell", now=at(6, day=16))
        self.assertEqual(self.rows()[0]["status"], "expired")  # 12 h after the window, quietly
        with W.db() as c:
            c.execute("update intents set status='accepted' where id=?", (self.iid,))
        self.assertEqual(self.rows()[0]["status"], "accepted")

    def test_private_ones_stay_private(self):
        I.apply_chat("nell", "private:1000000259", 1, [{"op": "intent", "text": "奈尔答应房主周末一起整理书架",
                                                          "kind": "promise", "with": ["房主"], "quote": "周末一起整理"}],
                     [f"{OWNER} 周末一起整理书架？", f"{NELL} 好，周末一起整理"], now=self.now)
        priv = [r for r in I.listing() if r["visibility"] == "private"]
        self.assertEqual(priv[0]["text"], "（房主私聊里的约定）")
        self.assertNotIn("quote", priv[0]["refs"][0])
        self.assertNotIn("书架", " ".join(I.card_lines("nell", now=self.now)))
        self.assertNotIn("书架", I.open_text("nell", False, now=self.now))
        self.assertIn("书架", I.open_text("nell", True, now=self.now))
        got = I.apply_chat("nell", G, 0, [{"op": "intent_done", "id": priv[0]["id"], "quote": "整理好了"}],
                           [f"{NELL} 整理好了"], now=self.now)
        self.assertEqual(got["rejected"], 1)  # not ended from a group chat


class ShownTests(IntentBase):
    def setUp(self):
        super().setUp()
        FoundTests.tea(self, FoundTests.both)
        self.due = at(15, day=15)

    def test_card_shows_due_soon_only_when_on(self):
        self.assertEqual(I.card_lines("nell", now=self.now), [])  # tomorrow: not yet
        line, = I.card_lines("nell", now=self.due - 3600)
        self.assertIn("一起去买红茶", line)
        self.assertIn("和婉妲", line)
        self.assertIn("15:00", line)
        self.place("nell", "study")
        self.assertIn("惦记着：", W.card("nell", now=self.due - 3600))
        with patch.object(I, "SHOW", False):
            self.assertEqual(I.card_lines("nell", now=self.due - 3600), [])

    def place(self, actor, location, activity="resting", availability="free", hours=1, now=None):
        t = now or self.now
        self.set_state(actor, location=location, activity=activity, availability=availability,
                       until=t + hours * 3600, next_due=t + 7200)

    def test_world_scene_with_both_completes_it(self):
        for a in ("nell", "wanda"):
            self.place(a, "living", now=self.due)
            self.set_state(a, cursor=self.due - 1800)
        st = {"location": "living", "place": "客厅", "activity": "socializing", "doing": "聊天", "mood": "happy",
              "energy": 60, "hunger": 40, "sleepiness": 20, "availability": "free", "until": "17:00", "beat": 0}
        got = answer(["nell", "wanda"], ["14:50"], st)
        got["intents_done"] = [{"id": 1, "beat": 0}]
        with patch.object(W, "llm", return_value=got) as m, patch("time.time", return_value=self.due):
            self.assertTrue(W.advance(["nell", "wanda"], now=self.due))
        self.assertIn("#1 奈尔、婉妲：奈尔和婉妲明天下午一起去买红茶", m.call_args[0][1])
        self.assertIn("intents_done", m.call_args[0][0])
        r = self.rows()[0]
        self.assertEqual(r["status"], "completed")
        self.assertIn("script", r["refs"][-1])

    def test_world_alone_cannot_complete_a_joint_one(self):
        self.place("nell", "study", now=self.due)
        self.set_state("nell", cursor=self.due - 1800)
        got = answer(["nell"], ["14:50"])
        got["intents_done"] = [{"id": 1, "beat": 0}]
        with patch.object(W, "llm", return_value=got) as m, patch("time.time", return_value=self.due):
            self.assertTrue(W.advance(["nell"], now=self.due))
        self.assertIn("有人不在这儿", m.call_args[0][1])
        self.assertEqual(self.rows()[0]["status"], "accepted")

    def test_due_and_apart_reaches_her_only_when_free(self):
        self.place("wanda", "shop", activity="outing", now=self.due)
        self.place("nell", "room", activity="sleeping", availability="sleeping", now=self.due)
        with patch("time.time", return_value=self.due):
            self.assertEqual(W.outreach("nell", now=self.due), {})
            self.assertEqual(self.rows()[0]["nudged"], 0)  # asleep: nothing happens
            self.place("nell", "study", now=self.due)
            got = W.outreach("nell", now=self.due)
        self.assertIn("之前说好的", got["about"])
        with W.db() as c:
            self.assertEqual(c.execute("select origin from contacts").fetchone()[0], "intent")
        self.assertEqual(self.rows()[0]["nudged"], 1)


class MemoryHookTests(IntentBase):
    def test_extract_passes_intent_ops_and_its_lines(self):
        import memory
        with patch.object(memory, "DB_PATH", W.DB_PATH.replace("world.db", "memory.db")), \
                patch.object(memory, "_related", return_value="（还没有）"), patch.object(memory, "INTENTS", I):
            memory.init()
            with memory.db() as c:
                for line in FoundTests.both:
                    c.execute("insert into seen(consumer,chat,private,ts,line,done) values(?,?,?,?,?,0)",
                              ("nell", G, 0, self.now, line))
            op = {"op": "intent", "text": "奈尔和婉妲明天下午一起去买红茶", "with": ["婉妲"], "kind": "joint",
                  "agreed": ["婉妲", "奈尔"], "due": "2026-10-15 15:00", "quote": "一起去买红茶"}
            seen = []
            with patch.object(memory, "llm", lambda s, u: seen.append((s, u)) or {"ops": [op]}):
                memory.extract("nell", G, 0)
        self.assertIn("意图与承诺", seen[0][0])
        self.assertIn("已有的意图", seen[0][1])
        self.assertEqual(self.rows()[0]["status"], "accepted")
