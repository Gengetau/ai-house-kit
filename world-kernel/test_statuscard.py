"""住客状态卡: worldkernel.statuscard over a hand-built life1.db and world.db (only what the kernel records).

Run: python3 -m unittest -v test_statuscard   (in-memory dbs, no model, no network)
"""
import datetime as dt
import json
import sqlite3
import unittest

from worldkernel import statuscard
from worldkernel.meals import SCHEMA as MEALS
from worldkernel.store import SCHEMA
from worldkernel.wardrobe import SCHEMA as WARDROBE

JST = dt.timezone(dt.timedelta(hours=9))
M = dt.datetime(2026, 11, 8, tzinfo=JST).timestamp()
H = 3600
NOW = M + 16 * H
DAY = "2026-11-08"


def life_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for s in (*WARDROBE, *MEALS):
        db.execute(s)
    for gid, actor, label, cat in (("wanda_khaki_dress", "wanda", "卡其长裙", "dress"),
                                   ("wanda_apron_kitchen", "wanda", "打下手围裙", "accessory"),
                                   ("wanda_pajamas", "wanda", "兔子睡衣", "sleepwear"),
                                   ("wanda_coat", "wanda", "海军蓝大衣", "outer"),
                                   ("wanda_skirt", "wanda", "百褶裙", "bottom"),
                                   ("wanda_blouse", "wanda", "白衬衫", "top"),
                                   ("nell_work_dress", "nell", "制服长裙", "dress")):
        db.execute("INSERT INTO garments(garment_id,actor,label,category,temp_lo,temp_hi,occasions,liking,source,created_at)"
                   " VALUES(?,?,?,?,0,30,'[]',3,'initial_wardrobe',0)", (gid, actor, label, cat))
    return db


def world_db(tables=True):
    w = sqlite3.connect(":memory:")
    w.row_factory = sqlite3.Row
    if tables:
        w.execute("create table reality(actor text primary key, location text, place text, activity text, doing text,"
                  " availability text, started real, until real, source text, updated real, feel text, mood text)")
        w.execute("create table kernel_card(actor text primary key, data text not null, updated real not null)")
    return w


def wear(db, actor, items, occasion, at, eid, current=False):
    db.execute("INSERT INTO outfit_log VALUES(?,?,?,?,?)", (actor, json.dumps(items), occasion, at, eid))
    if current:
        db.execute("INSERT OR REPLACE INTO outfits VALUES(?,?,?,?,?)", (actor, json.dumps(items), occasion, at, eid))


def meal(db, actor, slot, starts, ends, status, reason=None, eaten=()):
    db.execute("INSERT INTO meal_attendance(day,slot,actor,starts,ends,status,reason) VALUES(?,?,?,?,?,?,?)",
               (DAY, slot, actor, starts, ends, status, reason))
    for n, (item, at) in enumerate(eaten):
        db.execute("INSERT INTO meal_consumption VALUES(?,?,?,?,?,?,?)",
                   (f"eat-{actor}-{slot}-{n}", actor, at, item, "main", DAY, slot))


class StatusCardTests(unittest.TestCase):
    def setUp(self):
        self.k, self.w = life_db(), world_db()

    def card(self, actor="wanda", now=NOW):
        return statuscard.cards(self.k, self.w, now)["residents"][actor]

    def test_what_she_wears_garment_by_garment(self):
        wear(self.k, "wanda", ["wanda_khaki_dress", "wanda_apron_kitchen"], "home", M + 9 * H, 41, current=True)
        o = self.card()["outfit"]
        self.assertEqual(o["labels"], ["卡其长裙", "打下手围裙"])  # in the order she put them on
        self.assertEqual([g["kind"] for g in o["garments"]], ["裙装", "配饰"])
        self.assertEqual(o["outfit_id"], "wanda_apron_kitchen+wanda_khaki_dress")  # sorted: the order does not matter
        self.assertEqual((o["occasion"], o["occasion_label"], o["since"], o["event_id"]), ("home", "居家", M + 9 * H, 41))
        self.assertEqual(statuscard.outfit_id(["b", "a"]), statuscard.outfit_id(["a", "b"]))

    def test_a_garment_not_hers_is_not_named(self):
        wear(self.k, "wanda", ["wanda_khaki_dress", "nell_work_dress", "lost_ribbon"], "home", M + 9 * H, 41, current=True)
        o = self.card()["outfit"]
        self.assertEqual(o["labels"], ["卡其长裙", "衣物名称未记录", "衣物名称未记录"])
        self.assertEqual(o["garments"][1]["kind"], "单品")

    def test_no_outfit_yet(self):
        c = self.card("mina")
        self.assertIsNone(c["outfit"])
        self.assertEqual(c["changes"], [])
        self.assertEqual(c["history"], {"home": None, "sleep": None, "out": None})
        self.assertNotIn("outfit_error", c)

    def test_todays_changes_in_order_and_the_latest_of_each_kind(self):
        wear(self.k, "wanda", ["wanda_pajamas"], "sleep", M - 1 * H, 30)  # last night: not today's
        wear(self.k, "wanda", ["wanda_khaki_dress"], "home", M + 7 * H, 32)
        wear(self.k, "wanda", ["wanda_pajamas"], "sleep", M + 0.5 * H, 31)  # inserted out of order
        wear(self.k, "wanda", ["wanda_blouse", "wanda_skirt", "wanda_coat"], "out", M + 13 * H, 33)
        wear(self.k, "wanda", ["wanda_khaki_dress", "wanda_apron_kitchen"], "home", M + 15 * H, 34, current=True)
        wear(self.k, "wanda", ["wanda_pajamas"], "sleep", M + 23 * H, 35)  # after now: not shown
        c = self.card()
        self.assertEqual([(x["event_id"], x["occasion"]) for x in c["changes"]],
                         [(31, "sleep"), (32, "home"), (33, "out"), (34, "home")])
        self.assertEqual(c["changes"][2]["labels"], ["白衬衫", "百褶裙", "海军蓝大衣"])
        self.assertEqual(c["changes"][-1]["event_id"], c["outfit"]["event_id"])
        h = c["history"]
        self.assertEqual((h["home"]["event_id"], h["sleep"]["event_id"], h["out"]["event_id"]), (34, 31, 33))
        self.assertEqual(h["sleep"]["at"], M + 0.5 * H)

    def test_meals_only_as_recorded(self):
        meal(self.k, "wanda", "breakfast", M + 5 * H, M + 7.5 * H, "missed", "sleeping")
        meal(self.k, "wanda", "lunch", M + 11 * H, M + 14 * H, "eaten", eaten=[("yakisoba", M + 12 * H)])
        meal(self.k, "wanda", "dinner", M + 17 * H, M + 21 * H, "pending")
        meal(self.k, "nell", "breakfast", M + 5 * H, M + 7.5 * H, "eaten")  # eaten, but no dish recorded
        meal(self.k, "nell", "lunch", M + 11 * H, M + 17 * H, "pending")  # the window is open, nothing eaten yet
        meal(self.k, "nell", "dinner", M + 15 * H, M + 21 * H, "declined")
        self.k.execute("INSERT INTO meal_consumption VALUES('snack-1','wanda',?,'onigiri','snack',?,NULL)", (M + 15 * H, DAY))
        w = {m["slot"]: (m["state"], m["text"]) for m in self.card()["meals"]}
        self.assertEqual(w, {"breakfast": ("missed", "睡过去了"), "lunch": ("eaten", "炒面"),
                             "dinner": ("future", "还没到饭点")})  # a snack is not a meal
        self.assertEqual(self.card()["meals"][1]["eaten"][0]["at"], M + 12 * H)
        n = {m["slot"]: (m["state"], m["text"]) for m in self.card("nell")["meals"]}
        self.assertEqual(n, {"breakfast": ("eaten", "吃了 · 菜名未记录"), "lunch": ("pending", "还没有进餐记录"),
                             "dinner": ("missed", "没吃")})
        self.assertEqual([m["text"] for m in self.card("mina")["meals"]], ["未记录"] * 3)
        yesterday = statuscard.cards(self.k, self.w, M - 2 * H)["residents"]["wanda"]["meals"]
        self.assertEqual([m["state"] for m in yesterday], ["unknown"] * 3)  # another day's records are not today's

    def test_where_what_until_mood_and_next(self):
        feel = {"mood": "relaxed", "degree": 1, "why": "打完一局", "at": NOW - 60, "hunger": 40, "chat_h": 1}
        self.w.execute("insert into reality values('wanda','living','一楼客厅','gaming','在客厅打游戏','limited',?,?,"
                       "'kernel',?,?,'relaxed')", (NOW - H, NOW + H, NOW - 30, json.dumps(feel)))
        self.w.execute("insert into kernel_card values('wanda',?,?)",
                       (json.dumps({"outfit": "x", "next": ["23:00 差不多该睡了"]}, ensure_ascii=False), NOW))
        self.k.execute("INSERT INTO actors(actor_id,simulated,location,activity,action_id,availability,updated_at)"
                       " VALUES('wanda',0,'living','gaming','act-1','free',?)", (NOW - H,))
        self.k.execute("INSERT INTO actions(action_id,command_id,actor_id,origin,action_type,args,state,generation,"
                       "interrupt_policy,created_at,started_at,due_at) VALUES('act-1','c1','wanda','routine',"
                       "'begin_activity',?,'running',0,'now',?,?,?)",
                       (json.dumps({"activity": "gaming", "seconds": 7200}), NOW - H, NOW - H, NOW + H))
        c = self.card()
        r = c["reality"]
        self.assertEqual((r["place"], r["doing"], r["availability"], r["until"], r["source"]),
                         ("一楼客厅", "在客厅打游戏", "limited", NOW + H, "kernel"))
        self.assertEqual(r["feel"], {"mood": "relaxed", "degree": 1, "why": "打完一局", "at": NOW - 60})  # no body numbers
        self.assertEqual(c["kernel"]["place"], "一楼客厅")
        self.assertEqual(c["kernel"]["action"], {"type": "begin_activity", "state": "running", "started": NOW - H,
                                                 "due": NOW + H, "activity": "gaming", "label": "打游戏"})
        self.assertEqual(c["next"], ["23:00 差不多该睡了"])
        other = self.card("nell")
        self.assertEqual((other["reality"], other["kernel"], other["next"]), (None, None, None))

    def test_a_part_that_cannot_be_read_says_so(self):
        wear(self.k, "wanda", ["wanda_khaki_dress"], "home", M + 9 * H, 41, current=True)
        self.w = world_db(tables=False)
        self.k.execute("DROP TABLE outfit_log")
        d = statuscard.cards(self.k, self.w, NOW)
        self.assertEqual((d["reality_error"], d["next_error"]), ("OperationalError", "OperationalError"))
        c = d["residents"]["wanda"]
        self.assertEqual((c["reality"], c["next"]), (None, None))
        self.assertEqual((c["changes"], c["changes_error"], c["history"], c["history_error"]),
                         (None, "OperationalError", None, "OperationalError"))  # 读不到, not 「今天没换过」
        self.assertEqual(c["outfit"]["labels"], ["卡其长裙"])  # the rest of the card stays
        self.assertEqual(len(c["meals"]), 3)
        self.assertEqual(sorted(d["residents"]), sorted(["nell", "elsa", "alma", "mina", "yuki", "wanda"]))

    def test_unreadable_garments_are_not_guessed(self):
        """review P2-4: the garments table failing leaves the clothes parts 读不到 and the rest of the card there."""
        wear(self.k, "wanda", ["wanda_khaki_dress"], "home", M + 9 * H, 41, current=True)
        meal(self.k, "wanda", "lunch", M + 11 * H, M + 14 * H, "eaten", eaten=[("yakisoba", M + 12 * H)])
        self.k.execute("DROP TABLE garments")
        d = statuscard.cards(self.k, self.w, NOW)
        c = d["residents"]["wanda"]
        self.assertEqual(d["garments_error"], "OperationalError")
        self.assertEqual((c["outfit"], c["outfit_error"], c["changes"], c["history"]), (None, "RuntimeError", None, None))
        self.assertEqual(c["meals"][1]["text"], "炒面")

    def test_mood_column_comes_along(self):
        """review P2-3: with feel still '{}' the page falls back to reality.mood (as 生活 / 像素小屋 do), not 平静."""
        self.w.execute("insert into reality(actor,location,place,activity,doing,mood,availability,started,until,source,"
                       "updated,feel) values('wanda','living','一楼客厅','idle','发呆','tired','free',?,?,'kernel',?,'{}')",
                       (NOW, NOW + 1800, NOW))
        r = self.card()["reality"]
        self.assertEqual((r["mood"], r["feel"]["mood"]), ("tired", None))

    def test_no_running_action_no_end_time(self):
        """review P1: idle, the kernel has no action and so no end time (reality.until is the projector's now+30 min)."""
        self.k.execute("INSERT INTO actors(actor_id,simulated,location,activity,action_id,availability,updated_at)"
                       " VALUES('wanda',0,'living','idle',NULL,'free',?)", (NOW,))
        self.assertEqual(self.card()["kernel"]["action"], None)


if __name__ == "__main__":
    unittest.main()
