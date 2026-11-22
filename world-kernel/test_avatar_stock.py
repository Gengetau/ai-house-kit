"""房主化身 on the authority kernel. A snack he hands her comes out of the kernel stock the views read (inventory.giveable / hand_over), so the bots' card, the console's stock page and the house page all
count the same pieces; nothing on the console side writes the kernel.
Run: python3 -m unittest -v test_avatar_stock_171   (temp dbs, synthetic clock, fake model, no network)
"""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from worldkernel import avatar, inventory, vocab_life1 as V  # noqa: E402
from worldkernel.legacy import LEGACY_SCHEMA  # noqa: E402
from worldkernel.life import Life, JST  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.store import Store, VirtualClock  # noqa: E402

T0 = dt.datetime(2026, 11, 7, 15, 0, tzinfo=JST).timestamp()
DAY = dt.datetime(2026, 11, 7, 0, 0, tzinfo=JST).timestamp()
LEGACY = [dict(id=43, name="派之实", place="snacks", qty=4, unit="盒", kind="food", owner="", holder="", note="",
               by="owner", owner_events=[4358], owner_qty=4)]
PIE = "cookie@legacy:43"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kdb = os.path.join(self.tmp.name, "life1.db")
        self.wdb = os.path.join(self.tmp.name, "world.db")
        sqlite3.connect(self.wdb).close()
        self.clock = VirtualClock(T0)
        self.s = Store(self.kdb, self.clock, synthetic=True)
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.addCleanup(self.s.release)
        self.life = Life(self.s, model=FakeLifeModel(), seed_items=LEGACY,
                         weather=lambda _: dict(rain=False, temp_c=15), extensions=[avatar.install(self.wdb)])
        self.k = self.life.k
        self.av = self.life.extensions[0]
        self.n = 0
        with self.s.tx() as db:
            db.execute("UPDATE actors SET location='kitchen', activity='idle', action_id=NULL WHERE actor_id='mina'")

    def send(self, op, **kw):
        self.n += 1
        body = dict(op=op, cmd_id=f"c{self.n}", session="s1", **kw)
        avatar.queue(self.wdb, "cmd:owner", body, self.clock.now())
        self.av.drain()
        return avatar.receipt(self.wdb, body["cmd_id"])

    def rows(self):
        return {r["id"]: r for r in inventory.stock_rows(self.kdb, since=DAY, empty=True)}

    def snacks(self):
        db = sqlite3.connect(Path(self.kdb).resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            return {s["label"]: s for s in inventory.giveable_snacks(db, V)}
        finally:
            db.close()

    def hold(self, rid, n):
        aid = f"act-hold-{rid}-{n}"
        with self.s.tx() as db:
            db.execute("INSERT INTO actions(action_id,command_id,actor_id,origin,action_type,args,state,generation,"
                       "interrupt_policy,created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, "cmd-" + aid, "nell", "test", "eat", "{}", "running", 0, "none", T0, T0, None))
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,1,'held',?)", ("rv-" + aid, rid, aid, n, T0))


class GiveFromStock(Base):
    def test_one_piece_leaves_the_row_and_is_hers_with_its_name(self):
        self.assertEqual(self.snacks()["派之实"]["qty"], 4)
        self.assertEqual(self.send("enter", location="kitchen")["status"], "succeeded")
        got = self.send("give", to="mina", resource_id=self.snacks()["派之实"]["resource_id"])
        self.assertEqual(got["status"], "succeeded", got)
        rows = self.rows()
        pie, gift = rows[PIE], rows[got["resource_id"]]
        self.assertEqual((pie["qty"], pie["given_today"], pie["eaten_today"]), (3, 1, 0))
        self.assertEqual((gift["qty"], gift["holder"], gift["location"], gift["origin"], gift["by"], gift["name"],
                          gift["unit"]), (1, "mina", "kitchen", "gift", "owner", "派之实", "盒"))
        self.assertEqual(self.snacks()["派之实"]["qty"], 3)  # the house page sees the same count
        takes = inventory.stock_takes(self.kdb, since=DAY)
        self.assertEqual([(t["kind"], t["actor"], t["to"], t["items"]) for t in takes],
                         [("gift", "owner", "mina", [{"id": PIE, "qty": 1}])])
        with self.s.read() as db:
            p = json.loads(db.execute("SELECT payload FROM events WHERE kind='owner_gave'").fetchone()[0])
        self.assertEqual((p["label"], p["from"], p["to"]), ("派之实", PIE, "mina"))

    def test_reserved_pieces_cannot_be_handed_over(self):
        self.send("enter", location="kitchen")
        self.hold(PIE, 4)  # someone has all four in hand right now
        self.assertNotIn("派之实", self.snacks())
        got = self.send("give", to="mina", resource_id=PIE)
        self.assertEqual(got["reason"], "none_left")
        self.assertEqual((self.rows()[PIE]["qty"], self.rows()[PIE]["given_today"]), (4, 0))

    def test_owner_edit_and_give_on_the_same_row_both_count(self):
        """A console -1 (delta) after a hand-over: neither undoes the other (4 - 1 given - 1 edited = 2)."""
        self.send("enter", location="kitchen")
        self.assertEqual(self.send("give", to="mina", resource_id=PIE)["status"], "succeeded")
        inventory.queue_owner_stock(self.wdb, "cmd:owner", "set", dict(id=PIE, delta=-1), "edit-1")
        inventory.process_owner_stock_queue(self.k, self.wdb)
        self.assertEqual(self.rows()[PIE]["qty"], 2)
        inventory.queue_owner_stock(self.wdb, "cmd:owner", "del", dict(id=PIE), "edit-2")
        inventory.process_owner_stock_queue(self.k, self.wdb)
        self.assertEqual(self.send("give", to="mina", resource_id=PIE)["reason"], "none_left")

    def test_her_gift_is_not_on_offer_again_and_stays_hers(self):
        self.send("enter", location="kitchen")
        got = self.send("give", to="mina", resource_id=PIE)
        again = self.send("give", to="mina", resource_id=got["resource_id"])  # in her hand, not the kitchen's
        self.assertEqual(again["reason"], "not_a_house_snack")
        self.assertNotIn(got["resource_id"], {s["resource_id"] for s in self.snacks().values()})
        with self.s.read() as db:
            mine = db.execute("SELECT 1 FROM resources WHERE kind='food' AND qty>0 AND holder='mina'"
                              " AND resource_id=?", (got["resource_id"],)).fetchone()
        self.assertIsNotNone(mine)  # life.py's snack choice reads holder=her rows

    def test_replay_does_not_take_a_second_piece(self):
        self.send("enter", location="kitchen")
        body = dict(op="give", cmd_id="same", session="s1", to="mina", resource_id=PIE)
        self.assertEqual(self.av.apply(body), self.av.apply(body))
        self.assertEqual(self.rows()[PIE]["qty"], 3)


class InboxNeverStopsHerDay(Base):
    """Review P2/P3: a row that can never apply, a locked world.db, a command that waited out an outage."""

    def test_bad_row_gets_a_failed_receipt_and_the_next_one_still_applies(self):
        self.send("enter", location="kitchen")
        with patch.object(self.av, "_give", side_effect=RuntimeError("boom")):
            bad = self.send("give", to="mina", resource_id=PIE)
        self.assertEqual(bad, {"status": "failed", "reason": "error:RuntimeError"})
        self.assertEqual(self.send("give", to="mina", resource_id=PIE)["status"], "succeeded")

    def test_a_locked_world_db_is_logged_not_raised(self):
        with patch.object(self.av, "drain", side_effect=sqlite3.OperationalError("database is locked")), \
                patch("worldkernel.avatar._log") as log:
            self.av.step()  # Life.step would go on with the rest of her day
        self.assertIn("database is locked", log.call_args[0][0])
        from worldkernel import chatlink
        link = chatlink.ChatLink(self.life, self.wdb)
        with patch.object(link, "drain", side_effect=sqlite3.OperationalError("database is locked")):
            link.step()

    def test_a_command_that_waited_out_an_outage_is_not_applied(self):
        avatar.queue(self.wdb, "cmd:owner", dict(op="enter", cmd_id="old", session="s1", location="kitchen"),
                     self.clock.now())
        self.clock.advance(avatar.QUEUE_MAX_AGE_S + 1)
        self.av.drain()
        self.assertEqual(avatar.receipt(self.wdb, "old")["reason"], "expired")
        with self.s.read() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM owner_presence WHERE state='present'").fetchone())

    def test_applied_before_a_crash_keeps_its_receipt_even_when_old(self):
        body = dict(op="enter", cmd_id="crash", session="s1", location="kitchen")
        avatar.queue(self.wdb, "cmd:owner", body, self.clock.now())
        first = self.av.apply(body)  # kernel committed; the world.db ack never happened
        self.clock.advance(avatar.QUEUE_MAX_AGE_S + 1)
        self.av.drain()
        self.assertEqual(avatar.receipt(self.wdb, "crash"), first)


class AuthorityStep(unittest.TestCase):
    """The authority wires avatar + chatlink, and runs the owner's stock queue before the avatar inbox."""

    def setUp(self):
        import authority
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.wpath, self.kpath = os.path.join(d.name, "world.db"), os.path.join(d.name, "life1.db")
        c = sqlite3.connect(self.wpath)
        c.executescript(LEGACY_SCHEMA)
        for a in V.PEOPLE:
            c.execute("insert into reality values(?, 'living', '客厅', 'idle', '发呆', '平静', 50, 60, 30, 'free',"
                      " 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
        c.commit()
        c.close()
        self.clock = VirtualClock(dt.datetime(2026, 11, 7, 10, 0, tzinfo=JST).timestamp())
        self.a = authority.Authority(self.kpath, self.wpath, None, {}, clock=self.clock, model=FakeLifeModel(),
                                     migrate_items=False)
        self.addCleanup(self.a.store.close)
        self.addCleanup(self.a.store.release)

    def test_extensions_installed(self):
        names = {type(e).__name__ for e in self.a.life.extensions}
        self.assertTrue({"Avatar", "ChatLink"} <= names, names)

    def test_one_step_adds_then_hands_over(self):
        inventory.queue_owner_stock(self.wpath, "cmd:owner", "add", dict(name="芥末薯片", qty=2), "add-chips")
        chips = "owner:" + hashlib.sha256(b"add-chips").hexdigest()[:24]  # receive_owner_stock's id for this key
        who = "nell"
        with self.a.store.tx() as db:
            db.execute("UPDATE actors SET location='living', activity='idle', action_id=NULL WHERE actor_id=?", (who,))
        for op, kw in (("enter", dict(location="living")), ("give", dict(to=who, resource_id=chips))):
            avatar.queue(self.wpath, "cmd:owner", dict(op=op, cmd_id=op + "-1", session="page", **kw),
                         self.clock.now())
        life = self.a.life
        with patch.object(life.cog, "run_due"), patch.object(life.cog, "advance_plans"), \
                patch.object(life, "l0"):  # nobody walks off before the inbox: this is about the step's order
            self.a.step()
        got = avatar.receipt(self.wpath, "give-1")
        self.assertEqual(got["status"], "succeeded", got)
        rows = {r["id"]: r for r in inventory.stock_rows(self.kpath, empty=True)}
        self.assertEqual(rows[chips]["qty"], 1)
        self.assertEqual((rows[got["resource_id"]]["holder"], rows[got["resource_id"]]["name"]), (who, "芥末薯片"))

    def test_stage_reads_the_kernel_stock_names(self):
        inventory.queue_owner_stock(self.wpath, "cmd:owner", "add", dict(name="芥末薯片", qty=2), "add-chips")
        self.a.step()
        st = avatar.stage(self.kpath, self.clock.now())
        self.assertIn("芥末薯片", {s["label"] for s in st["snacks"]})


if __name__ == "__main__":
    unittest.main()
