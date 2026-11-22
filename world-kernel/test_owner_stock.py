"""after the cutover the house's stock is the kernel's resources.

- stock_rows: the read-only view the bots' card and the console use (shelf, who brought it, eaten today).
- queue_owner_stock / process_owner_stock_queue / receive_owner_stock: the owner's console add/set/del reach the
  kernel the same road a food delivery does (queued in world.db, applied by the scheduler once per request key).
Run: python3 -m unittest -v test_owner_stock_169   (temp dbs, synthetic clock, no model, no network)
"""
import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, JST  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.legacy import LEGACY_SCHEMA  # noqa: E402
from worldkernel import inventory, vocab_life1 as V  # noqa: E402

T0 = 1794000000.0
LEGACY = [dict(id=43, name="派之实", place="snacks", qty=4, unit="盒", kind="food", owner="", holder="", note="",
               by="owner", owner_events=[4358], owner_qty=4),
          dict(id=13, name="红茶茶叶", place="study", qty=2, unit="罐", kind="food", owner="elsa", holder="", note="",
               by="nell", owner_events=[], owner_qty=0)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kdb = os.path.join(self.tmp.name, "life1.db")
        self.wdb = os.path.join(self.tmp.name, "world.db")
        sqlite3.connect(self.wdb).close()
        self.s = Store(self.kdb, VirtualClock(T0), synthetic=True)
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.addCleanup(self.s.release)
        self.life = Life(self.s, seed_items=LEGACY)
        self.k = self.life.k

    def rows(self, **kw):
        return {r["id"]: r for r in inventory.stock_rows(self.kdb, **kw)}

    def res(self, rid):
        return self.k.resource(rid)

    def edit(self, op, args, key):
        q = inventory.queue_owner_stock(self.wdb, "cmd:owner", op, args, key)
        self.assertEqual(q["status"], "queued", q)
        inventory.process_owner_stock_queue(self.k, self.wdb)
        return inventory.queue_owner_stock(self.wdb, "cmd:owner", op, args, key)  # now the receipt

    def used(self, rid, n, state="used", at=None, action="eat"):
        """Someone ate n from rid (state used) or has it in hand right now (held)."""
        at = T0 if at is None else at
        aid = f"act-test-{rid}-{state}-{at}-{action}"
        with self.s.tx() as db:
            db.execute("INSERT INTO actions(action_id,command_id,actor_id,origin,action_type,args,state,generation,"
                       "interrupt_policy,created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, "cmd-" + aid, "nell", "test", action, "{}", "succeeded" if state == "used" else "running",
                        0, "none", at, at, at if state == "used" else None))
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,1,?,?)", ("rv-" + aid, rid, aid, n, state, at))


class StockRows(Base):
    def test_shelf_by_and_names(self):
        r = self.rows()
        c = r["cookie@legacy:43"]
        self.assertEqual((c["name"], c["shelf"], c["by"], c["unit"], c["origin"]),
                         ("派之实", "snacks", "owner", "盒", "legacy"))
        tea = r["black_tea@legacy:13"]
        self.assertEqual((tea["holder"], tea["location"], tea["shelf"], tea["by"]), ("elsa", "study", "study", "nell"))
        self.assertEqual((r["milk"]["shelf"], r["milk"]["by"], r["milk"]["origin"], r["milk"]["name"]),
                         ("fridge", "", "genesis", "牛奶"))
        self.assertEqual(r["rice"]["shelf"], "pantry")
        self.assertNotIn("stove", r)  # anchors are not stock
        self.assertTrue(all(x["qty"] > 0 for x in r.values()))

    def test_empty_rows_only_when_asked(self):
        with self.s.tx() as db:
            db.execute("UPDATE resources SET qty=0 WHERE resource_id='milk'")
        self.assertNotIn("milk", self.rows())
        self.assertEqual(self.rows(empty=True)["milk"]["qty"], 0)

    def test_eaten_today_and_held(self):
        self.used("cookie@legacy:43", 1, at=T0 - 3600)
        self.used("cookie@legacy:43", 2, at=T0 - 48 * 3600)  # the day before yesterday: not today
        self.used("cookie@legacy:43", 1, state="held")
        r = self.rows(since=T0 - 6 * 3600)["cookie@legacy:43"]
        self.assertEqual((r["eaten_today"], r["held"]), (1, 1))
        self.assertIsNone(self.rows()["cookie@legacy:43"]["eaten_today"])

    def test_cooking_is_not_eating(self):
        self.used("rice", 2, at=T0 - 600, action="cook")
        self.used("rice", 1, at=T0 - 300)
        r = self.rows(since=T0 - 3600)["rice"]
        self.assertEqual((r["eaten_today"], r["cooked_today"]), (1, 2))

    def test_old_living_room_row_is_not_said_to_be_in_the_living_room(self):
        with self.s.tx() as db:
            db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES(?,?,?,?,?,?)",
                       ("chips@legacy:99", "food", "chips", "kitchen", 1,
                        inventory.PREFIX + json.dumps({"name": "客厅的薯片", "place": "living", "by": "mina"})))
        r = self.rows()["chips@legacy:99"]
        self.assertEqual((r["location"], r["shelf"], r["by"]), ("kitchen", "snacks", "mina"))

    def test_bought_and_cooked_say_who(self):
        with self.s.tx() as db:
            eid = self.k._emit("action_succeeded", "wanda", None, "kitchen", {"action_type": "buy"},
                               scope="room:kitchen", occurred=T0)
            db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES(?,?,?,?,?,?)",
                       ("bought:act-1", "food", "onion", "kitchen", 1, f"event:{eid}"))
            db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES(?,?,?,?,?,?)",
                       ("curry_rice@act-2", "food", "curry_rice", "kitchen", 2, f"event:{eid}"))
        r = self.rows()
        self.assertEqual((r["bought:act-1"]["by"], r["bought:act-1"]["origin"]), ("wanda", "bought"))
        self.assertEqual((r["curry_rice@act-2"]["origin"], r["curry_rice@act-2"]["shelf"]), ("cooked", "fridge"))

    def test_read_only(self):
        with self.assertRaises(FileNotFoundError):
            inventory.stock_rows(os.path.join(self.tmp.name, "nope.db"))
        with self.s.read() as db:
            before = db.execute("SELECT COUNT(*), SUM(version) FROM resources").fetchone()[:]
        inventory.stock_rows(self.kdb, since=T0)
        with self.s.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*), SUM(version) FROM resources").fetchone()[:], before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nope.db")))

    def test_takes_agree_with_the_counts(self):  # B: 今天的取用, one entry per eat / cook, newest first
        self.used("cookie@legacy:43", 1, at=T0 - 3600)
        self.used("rice", 2, at=T0 - 600, action="cook")
        self.used("milk", 1, at=T0 - 300)
        self.used("milk", 1, state="held")  # in hand, not taken yet
        self.used("cookie@legacy:43", 2, at=T0 - 48 * 3600)  # before `since`
        since = T0 - 6 * 3600
        takes = inventory.stock_takes(self.kdb, since=since)
        self.assertEqual([(t["actor"], t["kind"], t["items"]) for t in takes],
                         [("nell", "eat", [{"id": "milk", "qty": 1}]), ("nell", "cook", [{"id": "rice", "qty": 2}]),
                          ("nell", "eat", [{"id": "cookie@legacy:43", "qty": 1}])])
        self.assertEqual([t["at"] for t in takes], [T0 - 300, T0 - 600, T0 - 3600])
        rows = self.rows(since=since, empty=True)
        for rid in ("milk", "rice", "cookie@legacy:43"):
            self.assertEqual(sum(i["qty"] for t in takes for i in t["items"] if i["id"] == rid),
                             rows[rid]["eaten_today"] + rows[rid]["cooked_today"], rid)
        self.assertEqual(len(inventory.stock_takes(self.kdb, since=since, limit=1)), 1)
        with self.assertRaises(FileNotFoundError):
            inventory.stock_takes(os.path.join(self.tmp.name, "nope.db"), since=since)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nope.db")))


class OwnerEdits(Base):
    def test_add_goes_into_the_kernel_once(self):
        args = dict(name="焦糖布丁", qty=3, unit="个", place="fridge")
        rec = self.edit("add", args, "req-1")
        self.assertEqual((rec["status"], rec["item"], rec["qty"]), ("succeeded", "pudding_cup", 3))
        r = self.res(rec["id"])
        self.assertEqual((r["kind"], r["item"], r["location"], r["holder"], r["qty"]),
                         ("food", "pudding_cup", "kitchen", None, 3))
        row = self.rows()[rec["id"]]
        self.assertEqual((row["name"], row["by"], row["shelf"], row["origin"], row["unit"]),
                         ("焦糖布丁", "owner", "fridge", "owner", "个"))
        # the same click again (a retry), and the scheduler running again: still one row of 3
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", args, "req-1"), rec)
        inventory.process_owner_stock_queue(self.k, self.wdb)
        self.assertEqual(inventory.receive_owner_stock(self.k, "cmd:owner", "req-1", "add", args), rec)
        with self.s.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*), SUM(qty) FROM resources WHERE source LIKE 'owner_stock:%'"
                                        ).fetchone()[:], (1, 3))
            ev = db.execute("SELECT kind, source_kind, payload FROM events WHERE kind='owner_stock_changed'").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["source_kind"], json.loads(ev[0]["payload"])["op"]), ("owner_input", "add"))

    def test_add_hers_in_her_room(self):
        rec = self.edit("add", dict(name="草莓百奇", qty=2, place="room", holder="yuki"), "req-room")
        r = self.res(rec["id"])
        self.assertEqual((r["item"], r["location"], r["holder"]), ("chocolate", "room_yuki", "yuki"))
        self.assertEqual(self.rows()[rec["id"]]["shelf"], "room")

    def test_add_refusals(self):
        for args, why in ((dict(name="洗发水", qty=1, kind="thing"), "not_food"),
                          (dict(name="", qty=1), "bad_name"),
                          (dict(name="薯片", qty=0), "bad_qty"),
                          (dict(name="薯片", qty="3"), "bad_qty"),
                          (dict(name="薯片", qty=2, place="room"), "room_needs_holder")):
            rec = self.edit("add", args, "req-" + why + str(args.get("qty")))
            self.assertEqual((rec["status"], rec["reason"]), ("failed", why), args)
        with self.s.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM resources WHERE source LIKE 'owner_stock:%'"
                                        ).fetchone()[0], 0)

    def test_unknown_name_is_other_snack(self):
        rec = self.edit("add", dict(name="不知道是什么", qty=1), "req-x")
        self.assertEqual((rec["item"], rec["matched"]), ("other_snack", False))

    def test_set_and_del(self):
        rec = self.edit("set", dict(id="cookie@legacy:43", qty=9), "req-set")
        self.assertEqual((rec["status"], rec["before"], rec["qty"]), ("succeeded", 4, 9))
        self.assertEqual(self.res("cookie@legacy:43")["qty"], 9)
        rec = self.edit("del", dict(id="cookie@legacy:43"), "req-del")
        self.assertEqual((rec["status"], rec["qty"]), ("succeeded", 0))
        self.assertEqual(self.res("cookie@legacy:43")["qty"], 0)  # the row stays (history), the stock is 0
        self.assertNotIn("cookie@legacy:43", self.rows())

    def test_not_below_what_she_has_in_hand(self):
        self.used("cookie@legacy:43", 2, state="held")
        self.assertEqual(self.edit("set", dict(id="cookie@legacy:43", qty=1), "r1")["reason"], "in_use")
        self.assertEqual(self.edit("del", dict(id="cookie@legacy:43"), "r2")["reason"], "in_use")
        self.assertEqual(self.edit("set", dict(id="cookie@legacy:43", qty=2), "r3")["status"], "succeeded")
        self.assertEqual(self.res("cookie@legacy:43")["qty"], 2)
        self.assertEqual(self.edit("set", dict(id="cookie@legacy:43", delta=-1), "r4")["reason"], "in_use")

    def test_delta_counts_from_what_the_kernel_has_then(self):
        q = inventory.queue_owner_stock(self.wdb, "cmd:owner", "set", dict(id="cookie@legacy:43", delta=1), "plus")
        self.assertEqual(q["status"], "queued")
        with self.s.tx() as db:  # while it waits in the queue, someone eats one (4 -> 3)
            db.execute("UPDATE resources SET qty=qty-1 WHERE resource_id='cookie@legacy:43'")
        inventory.process_owner_stock_queue(self.k, self.wdb)
        self.assertEqual(self.res("cookie@legacy:43")["qty"], 4)  # 3 + 1, the eaten one stays eaten
        for d, key in ((0, "z"), ("1", "s"), (-5, "neg"), (True, "b")):
            self.assertEqual(self.edit("set", dict(id="cookie@legacy:43", delta=d), key)["reason"], "bad_qty", d)
        self.assertEqual(self.res("cookie@legacy:43")["qty"], 4)

    def test_keys_sharing_a_long_prefix_are_two_rows(self):
        a, b = "life-page:2026-11-07T15:30:12+09:00:add:0001", "life-page:2026-11-07T15:30:12+09:00:add:0002"
        ra = self.edit("add", dict(name="薯片", qty=1), a)
        rb = self.edit("add", dict(name="薯片", qty=2), b)
        self.assertEqual((ra["status"], rb["status"]), ("succeeded", "succeeded"))
        self.assertNotEqual(ra["id"], rb["id"])

    def test_a_row_that_cannot_apply_does_not_stay_queued(self):
        with self.s.tx() as db:  # an id already taken (whatever made it): the insert fails, the row gets a receipt
            db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES(?,?,?,?,?,?)",
                       ("owner:" + inventory.hashlib.sha256(b"taken").hexdigest()[:24], "food", "chips", "kitchen",
                        1, "test"))
        rec = self.edit("add", dict(name="薯片", qty=1), "taken")
        self.assertEqual(rec["status"], "failed")
        self.assertTrue(rec["reason"].startswith("conflict:"))

    def test_old_queued_edits_expire(self):  # (queued, rolled back to shadow, authority again later)
        inventory.queue_owner_stock(self.wdb, "cmd:owner", "set", dict(id="milk", qty=1), "old")
        with contextlib.closing(sqlite3.connect(self.wdb)) as c:
            with c:
                c.execute("UPDATE owner_stock_queue SET created=? WHERE source_key='old'", (time.time() - 7200,))
        inventory.process_owner_stock_queue(self.k, self.wdb)
        rec = inventory.queue_owner_stock(self.wdb, "cmd:owner", "set", dict(id="milk", qty=1), "old")
        self.assertEqual((rec["status"], rec["reason"]), ("failed", "expired"))
        self.assertEqual(self.res("milk")["qty"], 6)

    def test_applied_before_a_long_outage_is_not_called_expired(self):  # re-review A
        args = dict(name="可乐", qty=2)
        inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", args, "late")
        first = inventory.receive_owner_stock(self.k, "cmd:owner", "late", "add", args)  # committed, then a crash
        with contextlib.closing(sqlite3.connect(self.wdb)) as c:  # and the restart comes 2 hours later
            with c:
                c.execute("UPDATE owner_stock_queue SET created=? WHERE source_key='late'", (time.time() - 7200,))
        inventory.process_owner_stock_queue(self.k, self.wdb)
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", args, "late"), first)
        self.assertEqual(first["status"], "succeeded")

    def test_set_refusals(self):
        self.assertEqual(self.edit("set", dict(id="nope", qty=1), "a")["reason"], "no_such_food")
        self.assertEqual(self.edit("set", dict(id="stove", qty=0), "b")["reason"], "no_such_food")  # not an anchor
        self.assertEqual(self.edit("set", dict(id="milk", qty=1000), "c")["reason"], "bad_qty")
        self.assertEqual(self.res("stove")["qty"], 1)

    def test_queue_checks(self):
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "qq:123", "add", {}, "k")["reason"], "owner_only")
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "cmd:owner", "drop", {}, "k")["reason"], "bad_request")
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", {}, "")["reason"], "bad_request")
        inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", dict(name="薯片", qty=1), "same")
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", dict(name="薯片", qty=5), "same"
                                                     )["reason"], "request_conflict")
        self.assertEqual(inventory.receive_owner_stock(self.k, "qq:1", "z", "add", {})["reason"], "owner_only")

    def test_crash_between_kernel_and_queue_ack_replays_nothing(self):
        args = dict(name="可乐", qty=6)
        inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", args, "crash")
        first = inventory.receive_owner_stock(self.k, "cmd:owner", "crash", "add", args)  # kernel committed, no ack
        inventory.process_owner_stock_queue(self.k, self.wdb)  # the next step replays the still-queued row
        self.assertEqual(inventory.queue_owner_stock(self.wdb, "cmd:owner", "add", args, "crash"), first)
        with self.s.read() as db:
            self.assertEqual(db.execute("SELECT SUM(qty) FROM resources WHERE item='cola' AND source LIKE"
                                        " 'owner_stock:%'").fetchone()[0], 6)

    def test_kernel_food_choice_sees_the_owner_row(self):
        """The point of the queue: the row is shaped like the kitchen stock meals/snacks pick from."""
        rec = self.edit("add", dict(name="薯片", qty=2), "req-chips")
        with self.s.read() as db:
            hit = db.execute("SELECT resource_id FROM resources WHERE kind='food' AND location='kitchen'"
                             " AND holder IS NULL AND qty>0 AND resource_id=?", (rec["id"],)).fetchone()
        self.assertIsNotNone(hit)


class AuthorityStep(unittest.TestCase):
    def setUp(self):
        import authority
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.wpath, kpath = os.path.join(d.name, "world.db"), os.path.join(d.name, "life1.db")
        c = sqlite3.connect(self.wpath)
        c.executescript(LEGACY_SCHEMA)
        for a in V.PEOPLE:
            c.execute("insert into reality values(?, 'living', '客厅', 'idle', '发呆', '平静', 50, 60, 30, 'free',"
                      " 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
        c.commit()
        c.close()
        self.clock = VirtualClock(dt.datetime(2026, 11, 7, 10, 0, tzinfo=JST).timestamp())
        self.a = authority.Authority(kpath, self.wpath, None, {}, clock=self.clock, model=FakeLifeModel(),
                                     migrate_items=False)
        self.addCleanup(self.a.store.close)
        self.addCleanup(self.a.store.release)
        self.authority = authority

    def test_step_applies_the_owner_queue(self):
        inventory.queue_owner_stock(self.wpath, "cmd:owner", "add", dict(name="弹珠汽水", qty=6), "step-1")
        self.a.step()
        rec = inventory.queue_owner_stock(self.wpath, "cmd:owner", "add", dict(name="弹珠汽水", qty=6), "step-1")
        self.assertEqual((rec["status"], self.a.life.k.resource(rec["id"])["qty"]), ("succeeded", 6))

    def test_a_failing_queue_never_stops_her_day(self):
        inventory.queue_owner_stock(self.wpath, "cmd:owner", "add", dict(name="薯片", qty=1), "boom")
        with patch.object(inventory, "receive_owner_stock", side_effect=RuntimeError("db is locked")), \
                patch.object(self.authority, "log") as log:
            self.a.step()
        self.assertEqual(float(self.a.store.meta("last_step")), self.clock.now())  # life.step and the projection ran
        self.assertIn("process_owner_stock_queue failed", log.call_args[0][0])
        self.a.step()  # next minute it is applied
        self.assertEqual(inventory.queue_owner_stock(self.wpath, "cmd:owner", "add", dict(name="薯片", qty=1),
                                                     "boom")["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
