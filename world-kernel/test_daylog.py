"""她们这一周 · 生活细节: worldkernel.daylog over a hand-built life1.db (only what the kernel would record).

Run: python3 -m unittest -v test_daylog   (temp in-memory db, no model, no network)
"""
import datetime as dt
import json
import sqlite3
import unittest

from worldkernel import daylog
from worldkernel.store import SCHEMA
from worldkernel.wardrobe import SCHEMA as WARDROBE

JST = dt.timezone(dt.timedelta(hours=9))
M = dt.datetime(2026, 11, 7, tzinfo=JST).timestamp()
H = 3600


class Day:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for s in WARDROBE:
            self.db.execute(s)
        self.db.execute("CREATE TABLE wallets (account TEXT PRIMARY KEY,balance INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE wallet_entries (entry_key TEXT PRIMARY KEY,account TEXT NOT NULL,delta INTEGER NOT NULL,"
                        "event_id INTEGER NOT NULL,at REAL NOT NULL)")
        self.n = 0

    def emit(self, kind, actor, aid, at, loc, payload=None):
        cur = self.db.execute(
            "INSERT INTO events(kind,actor_id,action_id,occurred_at,recorded_at,location,payload,knowers,"
            "disclosure_scope,source_kind,trust,retention_class,synthetic,runtime_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, actor, aid, at, at, loc, json.dumps(payload or {}), "[]", "room", "kernel", "kernel", "life", 1, 1))
        return cur.lastrowid

    def act(self, actor, typ, args, start, end, loc, state="succeeded", instant=False):
        self.n += 1
        aid = f"act-{self.n:03d}"
        self.db.execute("INSERT INTO actions(action_id,command_id,actor_id,origin,action_type,args,state,generation,"
                        "interrupt_policy,created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, "cmd-" + aid, actor, "routine", typ, json.dumps(args), state, 0, "now", start,
                         None if instant else start, end))
        if not instant:
            self.emit("action_started", actor, aid, start, loc, {"action_type": typ})
            if state == "succeeded":
                self.emit("action_succeeded", actor, aid, end, loc, {"action_type": typ})
        return aid

    def move(self, actor, frm, to, at, secs=30):
        aid = self.act(actor, "move", {"to": to}, at, at + secs, frm)
        self.emit("departed", actor, aid, at + secs, frm, {"to": to})
        self.emit("arrived", actor, aid, at + secs, to, {"action_type": "move"})
        return aid

    def do(self, actor, activity, loc, at, mins, state="succeeded", **extra):
        return self.act(actor, "begin_activity", {"activity": activity, "seconds": mins * 60, **extra}, at,
                        None if state == "running" else at + mins * 60, loc, state=state)

    def clothes(self, actor, items, occasion, loc, at):
        aid = self.act(actor, "change_clothes", {"items": items, "occasion": occasion}, at, at, loc, instant=True)
        self.emit("changed_clothes", actor, aid, at, loc, {"items": items, "occasion": occasion})

    def buy(self, actor, item, qty, yen, loc, at, account="house"):
        aid = self.act(actor, "buy", {"item": item, "kind": "ingredient", "qty": qty}, at, at, loc, instant=True)
        ev = self.emit("purchased", actor, aid, at, loc, {"account": account, "item": item, "qty": qty, "yen": yen})
        self.db.execute("INSERT INTO wallet_entries VALUES(?,?,?,?,?)", ("buy:" + aid, account, -yen, ev, at))

    def lines(self, now=None, start=M, end=M + 24 * H):
        return daylog.entries(self.db, start, end, now or end)


class DaylogTests(unittest.TestCase):
    def test_a_walk_folds_into_what_she_does_there(self):
        d = Day()
        d.move("yuki", "living", "bath", M + 21 * H)
        d.act("yuki", "wait", {"seconds": 120}, M + 21 * H + 30, M + 21 * H + 150, "bath")  # never shown
        d.do("yuki", "bathing", "bath", M + 21 * H + 150, 30)
        got = d.lines()
        self.assertEqual([(x["kind"], x["text"]) for x in got], [("activity", "由纪去浴室洗澡/泡澡 30 分钟")])
        x = got[0]
        self.assertEqual((x["ts"], x["end"], x["loc"], x["running"]), (M + 21 * H, M + 21 * H + 150 + 1800, "bath", False))
        self.assertEqual(x["walk"]["from"], "living")
        self.assertEqual(len(x["events"]), 6)  # started, succeeded, departed, arrived (move) + started, succeeded (bath)

    def test_a_shopping_trip_is_two_lines(self):
        d = Day()
        t = M + 13.5 * H
        d.move("nell", "kitchen", "away", t)
        d.move("nell", "away", "place:supermarket", t + 60, secs=600)
        d.buy("nell", "cabbage", 1, 120, "place:supermarket", t + 700)
        d.buy("nell", "carrot", 2, 140, "place:supermarket", t + 760)
        d.move("nell", "place:supermarket", "away", t + 900, secs=600)
        d.move("nell", "away", "living", t + 1500)
        d.act("nell", "wait", {"seconds": 300}, t + 1530, t + 1830, "living")
        d.move("nell", "living", "room_nell", t + 1830)
        d.db.execute("INSERT INTO garments(garment_id, actor, label) VALUES ('nell_apron', 'nell', '家务围裙')")
        d.clothes("nell", ["nell_apron"], "home", "room_nell", t + 1900)
        got = d.lines()
        self.assertEqual([x["text"] for x in got],
                         ["奈尔出门去超市，买了卷心菜 ×1、胡萝卜 ×2，共 ¥260（公账）",
                          "奈尔从超市回到家，在自己房间换上了家务围裙（居家）"])
        self.assertEqual(got[0]["reached"], t + 660)  # the caller drops world.db's 「奈尔到了超市」 for this
        self.assertIsNone(got[1]["reached"])
        self.assertEqual(got[0]["ts"], t)

    def test_own_money_and_a_lone_purchase(self):
        d = Day()
        d.buy("mina", "cabbage", 1, 120, "place:konbini", M + 10 * H, account="mina")
        self.assertEqual(d.lines()[0]["text"], "米娜在便利店买了卷心菜 ×1，共 ¥120（自己的钱）")

    def test_script_covered_things_are_not_written_twice(self):
        d = Day()
        d.move("nell", "living", "kitchen", M + 12 * H)
        d.act("nell", "cook", {"recipe": "curry_rice"}, M + 12 * H + 60, M + 13 * H, "kitchen")
        d.act("nell", "eat", {"item": "curry_rice", "resource_id": "curry_rice@act-002"}, M + 13 * H, M + 13.2 * H,
              "kitchen")
        d.move("elsa", "study", "living", M + 20 * H)
        d.do("elsa", "movie_night", "living", M + 20 * H + 60, 90, party="plan-1")
        self.assertEqual(d.lines(), [])  # the walk to the kitchen / to the gathering belongs to those lines

    def test_resumed_activity_is_one_line_and_a_walk_alone_says_from_where(self):
        d = Day()
        d.do("nell", "reading", "room_nell", M + 19 * H, 60)
        d.act("nell", "wait", {"seconds": 60}, M + 20 * H, M + 20 * H + 60, "room_nell")
        d.do("nell", "reading", "room_nell", M + 20 * H + 60, 30)
        d.do("nell", "resting", "room_nell", M + 20.6 * H, 10)
        d.move("nell", "room_nell", "study", M + 22 * H)
        got = [x["text"] for x in d.lines()]
        self.assertEqual(got, ["奈尔在自己房间看书 1 小时 31 分", "奈尔在自己房间休息 10 分钟", "奈尔从自己房间去了书房"])

    def test_who_else_was_there(self):
        d = Day()
        d.move("wanda", "kitchen", "living", M + 15 * H - 600)   # in the living room the whole time
        d.move("mina", "kitchen", "living", M + 15 * H + 600)    # passes through for a minute
        d.move("mina", "living", "room_mina", M + 15 * H + 660)
        d.move("elsa", "study", "room_elsa", M + 14 * H)     # elsewhere
        d.do("yuki", "being_together", "living", M + 15 * H, 60)
        x = [x for x in d.lines() if x["actor"] == "yuki"][0]
        self.assertEqual((x["with"], x["people"]), (["wanda"], ["yuki"]))  # the filter is by whom it is about
        lone = [x for x in d.lines() if x["actor"] == "wanda"][0]
        self.assertEqual(lone["text"], "婉妲从厨房兼餐厅去了一楼客厅")

    def test_still_going_and_sleep(self):
        d = Day()
        d.act("alma", "sleep", {"seconds": 27000}, M + 0.5 * H, M + 8 * H, "room_alma")
        d.do("mina", "gaming", "living", M + 22 * H, 60, state="running")
        got = {x["actor"]: x for x in d.lines(now=M + 22.5 * H)}
        self.assertEqual(got["alma"]["text"], "阿尔玛在自己房间睡了 7 小时 30 分")
        self.assertEqual((got["mina"]["text"], got["mina"]["end"], got["mina"]["running"]),
                         ("米娜在一楼客厅打游戏 30 分钟，还在继续", None, True))

    def test_the_day_is_its_own_and_nothing_unrecorded(self):
        d = Day()
        d.do("nell", "reading", "living", M - 2 * H, 60)                      # yesterday
        d.act("nell", "begin_activity", {"activity": "reading"}, M + 9 * H, M + 9 * H, "living", state="failed")
        d.act("nell", "say", {"to": "elsa"}, M + 10 * H, M + 10 * H, "living")  # not a thing she does with her body
        d.act("nell", "move", {"to": "yard"}, M + 11 * H, None, "living", state="running")  # still on the way
        self.assertEqual(d.lines(), [])
        self.assertEqual([x["text"] for x in d.lines(start=M - 24 * H, end=M)], ["奈尔在一楼客厅看书 1 小时"])

    def test_a_quick_second_outfit_is_one_line_and_rooms_are_named(self):
        d = Day()
        d.db.executemany("INSERT INTO garments(garment_id, actor, label) VALUES (?,?,?)",
                         [("a", "nell", "开衫"), ("b", "nell", "帆布袋")])
        d.clothes("nell", ["a"], "out", "kitchen", M + 13 * H)
        d.clothes("nell", ["a", "b"], "out", "kitchen", M + 13 * H + 30)
        d.do("elsa", "reading", "room_nell", M + 14 * H, 20)
        self.assertEqual([x["text"] for x in d.lines()],
                         ["奈尔在厨房兼餐厅换上了开衫、帆布袋（出门）", "艾莎在奈尔的房间看书 20 分钟"])

    # reviewer round 1 (P2/P3)
    def test_a_snack_is_eaten_only_when_the_kernel_finished_it(self):
        d = Day()
        d.act("nell", "eat", {"item": "cookie"}, M + 15 * H, M + 15 * H + 300, "living", state="cancelled")
        d.act("elsa", "eat", {"item": "cookie"}, M + 16 * H, None, "living", state="running")
        d.act("mina", "eat", {"item": "cookie"}, M + 17 * H, M + 17 * H + 300, "living")
        got = [x["text"] for x in d.lines(now=M + 16 * H + 60)]
        label = daylog._label("cookie")
        self.assertEqual(got, [f"艾莎在一楼客厅正在吃{label}", f"米娜在一楼客厅吃了{label}"])

    def test_a_together_activity_done_alone_is_not_together(self):
        d = Day()
        d.do("wanda", "movie_night", "living", M + 20 * H, 90)
        self.assertEqual(d.lines()[0]["text"], "婉妲在一楼客厅看电影 1 小时 30 分")

    def test_there_and_back_and_leaving_a_place(self):
        d = Day()
        d.move("nell", "living", "kitchen", M + 10 * H)
        d.act("nell", "check", {"what": "fridge"}, M + 10 * H + 30, M + 10 * H + 60, "kitchen")
        d.move("nell", "kitchen", "living", M + 10 * H + 60)
        d.do("nell", "reading", "living", M + 10 * H + 120, 30)
        d.move("mina", "place:supermarket", "away", M + 14 * H, secs=600)
        got = [x["text"] for x in d.lines()]
        self.assertEqual(got, ["奈尔在一楼客厅看书 30 分钟", "米娜离开超市"])
        self.assertEqual(d.lines()[0]["ts"], M + 10 * H + 120)  # the time is the reading's, not the little walk's

    def test_under_a_minute_is_not_a_line(self):
        d = Day()
        d.do("nell", "reading", "living", M + 10 * H, 0.5, state="cancelled")
        self.assertEqual(d.lines(), [])

    def test_someone_who_has_not_walked_is_still_somewhere(self):
        d = Day()
        d.do("elsa", "reading", "living", M + 10 * H, 120)  # never arrived anywhere since the kernel took over
        d.do("nell", "housework", "living", M + 10.5 * H, 30)
        nell = [x for x in d.lines() if x["actor"] == "nell"][0]
        self.assertEqual(nell["with"], ["elsa"])

    def test_shop_goods_by_their_own_name(self):
        d = Day()
        aid = d.act("alma", "buy", {"item": "shop_scarf_01", "kind": "clothing", "qty": 1}, M + 15 * H, M + 15 * H,
                    "place:shotengai", instant=True)
        d.emit("purchased", "alma", aid, M + 15 * H, "place:shotengai",
               {"account": "alma", "item": "shop_scarf_01", "name": "格纹围巾", "qty": 1, "yen": 2400})
        self.assertEqual(d.lines()[0]["text"], "阿尔玛在商店街买了格纹围巾 ×1，共 ¥2,400（自己的钱）")


if __name__ == "__main__":
    unittest.main()
