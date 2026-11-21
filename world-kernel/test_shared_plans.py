"""nell's send-back: shared resources only for a real plan, the projection's cursor and
receipts committed with its rows, one world per world.db, 「一起」 only for who really came, expired invites."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from worldkernel.life import Life
from worldkernel.legacy import Projector, offline_world
from worldkernel.store import Store, VirtualClock
from worldkernel.wallet import JST


def at(clock):
    return datetime.fromisoformat("2026-11-03T" + clock).replace(tzinfo=JST).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.clock = VirtualClock(at("19:50"))
        self.s = Store(os.path.join(self.d, "life1.db"), self.clock, world_id="test/r91", synthetic=True, holder="t")
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.l = Life(self.s, model=None)
        self.k = self.l.k
        self.n = 0
        self.wpath = os.path.join(self.d, "world.db")
        offline_world(self.wpath)

    def cmd(self, actor, action, **args):
        self.n += 1
        return self.k.submit({"idempotency_key": f"r91:{self.n}", "principal": f"channel:{actor}", "actor_id": actor,
                              "origin": "self", "action_type": action, "args": args})

    def go(self, actor, to):
        r = self.cmd(actor, "move", to=to)
        self.assertIn(r["status"], ("running", "succeeded"), r)
        for _ in range(10):
            if self.k.actor(actor)["location"] == to:
                return
            self.clock.advance(60)
            self.k.tick()
        self.fail(f"{actor} never reached {to}")

    def plan(self, what, guests, host="nell", start=None):
        r = self.cmd(host, "invite", to=list(guests), kind="activity", what=what, location="living",
                     start_at=start or self.clock.now() + 600)
        self.assertEqual(r["status"], "succeeded", r)
        return r["plan_id"]

    def accept(self, guest, plan_id):
        inv = [i for i in self.l.plans.pending_for(guest) if i["plan_id"] == plan_id][-1]["inv_id"]
        r = self.cmd(guest, "respond_invite", inv_id=inv, answer="yes")
        self.assertEqual(r["status"], "succeeded", r)

    def held(self, rid):
        return sorted(r[0] for r in self.s.db.execute(
            "SELECT a.actor_id FROM reservations v JOIN actions a ON a.action_id=v.action_id"
            " WHERE v.resource_id=? AND v.state='held'", (rid,)))

    def world(self):
        c = sqlite3.connect(self.wpath)
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        return c


class SharedResources(Base):
    """a made-up party does not share the bathtub; a real plan shares the tv, each with her own hold."""

    def test_fake_party_does_not_share_the_bathtub(self):
        self.go("nell", "bath")
        self.go("elsa", "bath")
        r = self.cmd("nell", "begin_activity", activity="bathing", seconds=1200, party="us")
        self.assertEqual(r["status"], "running", r)
        r = self.cmd("elsa", "begin_activity", activity="bathing", seconds=1200, party="us")
        self.assertEqual((r["status"], r.get("reason")), ("failed", "resource_unavailable"), r)
        self.assertEqual(self.held("bathtub"), ["nell"])

    def test_movie_night_plan_shares_the_tv_and_the_host_leaving_keeps_it_held(self):
        p = self.plan("movie_night", ["elsa"])
        self.accept("elsa", p)
        for a in ("nell", "elsa", "mina"):
            self.go(a, "living")
        self.clock.advance(600)
        self.assertEqual(self.cmd("nell", "begin_activity", activity="movie_night", seconds=6000, party=p)["status"],
                         "running")
        r = self.cmd("elsa", "begin_activity", activity="movie_night", seconds=7200, party=p)
        self.assertEqual(r["status"], "running", r)
        self.assertEqual(self.held("tv"), ["elsa", "nell"])
        # not invited: naming the plan does not make her a member
        r = self.cmd("mina", "begin_activity", activity="movie_night", seconds=6000, party=p)
        self.assertEqual((r["status"], r.get("reason")), ("failed", "resource_unavailable"), r)
        self.clock.advance(6060)
        self.k.tick()
        self.assertEqual(self.held("tv"), ["elsa"])
        self.assertEqual(self.k.actor("elsa")["activity"], "movie_night")
        r = self.cmd("mina", "begin_activity", activity="movie_night", seconds=6000)
        self.assertEqual((r["status"], r.get("reason")), ("failed", "resource_unavailable"), r)

    def test_uninvited_holder_first_does_not_make_the_tv_shared(self):
        """mina names the real plan id and takes the tv first; the plan's host cannot share HER hold."""
        p = self.plan("movie_night", ["elsa"])
        self.accept("elsa", p)
        for a in ("nell", "mina"):
            self.go(a, "living")
        self.clock.advance(600)
        self.assertEqual(self.cmd("mina", "begin_activity", activity="movie_night", seconds=6000, party=p)["status"],
                         "running")
        r = self.cmd("nell", "begin_activity", activity="movie_night", seconds=6000, party=p)
        self.assertEqual((r["status"], r.get("reason")), ("failed", "resource_unavailable"), r)
        self.assertEqual(self.held("tv"), ["mina"])


class Projection(Base):
    def movie(self, guests, came):
        p = self.plan("movie_night", guests)
        for g in guests:
            self.accept(g, p)
        for a in ["nell", *came]:
            self.go(a, "living")
        self.clock.advance(600)
        for a in ["nell", *came]:
            r = self.cmd(a, "begin_activity", activity="movie_night", seconds=6000, party=p)
            self.assertEqual(r["status"], "running", r)
        self.clock.advance(6060)
        self.k.tick()
        return p

    def test_only_who_came_is_together_and_one_line_per_gathering(self):
        """wanda said yes and never came: no 「一起」 with her, no happy feeling for her."""
        self.movie(["elsa", "wanda"], came=["elsa"])
        Projector(self.l, self.wpath).run()
        c = self.world()
        lines = [r["text"] for r in c.execute("select text from script where source='kernel'")]
        together = [x for x in lines if "一起看电影" in x and "约" not in x]  # the invite answers are their own lines
        self.assertEqual(len(together), 1, lines)
        self.assertIn("奈尔", together[0])
        self.assertIn("艾莎", together[0])
        self.assertNotIn("婉妲", "".join(together))
        feel = {r["actor"] for r in c.execute("select actor from emotions where source='kernel' and emotion='happy'")}
        self.assertEqual(feel, {"nell", "elsa"})

    def test_failed_commit_leaves_cursor_and_rows_and_a_retry_writes_them_once(self):
        """the cursor lives in the target and commits with the rows."""
        self.movie(["elsa"], came=["elsa"])

        class Broken:
            def __init__(self, c):
                self.c = c

            def __getattr__(self, name):
                return getattr(self.c, name)

            def commit(self):
                raise sqlite3.OperationalError("disk I/O error")

        broken = Projector(self.l, self.wpath)
        real = broken._conn
        broken._conn = lambda: Broken(real())
        with self.assertRaises(sqlite3.OperationalError):
            broken.run()
        c = self.world()
        self.assertEqual(c.execute("select count(*) from script").fetchone()[0], 0)
        self.assertIsNone(c.execute("select v from kernel_bind where k='cursor'").fetchone())
        self.assertEqual(c.execute("select count(*) from kernel_fx").fetchone()[0], 0)
        p = Projector(self.l, self.wpath)
        p.run()
        n = c.execute("select count(*) from script").fetchone()[0]
        e = c.execute("select count(*) from emotions").fetchone()[0]
        self.assertGreater(n, 0)
        c.execute("delete from kernel_bind where k='cursor'")  # even a lost cursor does not double anything
        c.commit()
        p.run()
        self.assertEqual(c.execute("select count(*) from script").fetchone()[0], n)
        self.assertEqual(c.execute("select count(*) from emotions").fetchone()[0], e)

    def test_left_early_still_shared_and_only_sharers_know(self):
        """nell leaves after 15 minutes, mina walks in later: the line is known by nell and elsa, not mina."""
        p = self.plan("movie_night", ["elsa"])
        self.accept("elsa", p)
        for a in ("nell", "elsa"):
            self.go(a, "living")
        self.clock.advance(600)
        for a in ("nell", "elsa"):
            self.assertEqual(self.cmd(a, "begin_activity", activity="movie_night", seconds=6000, party=p)["status"],
                             "running")
        proj = Projector(self.l, self.wpath)
        self.clock.advance(900)
        self.k.tick()
        r = self.k.cancel("nell", self.k.actor("nell")["action_id"], "channel:nell")  # she stops: what she shared happened
        self.assertEqual(r["status"], "cancelled", r)
        self.go("nell", "kitchen")
        proj.run()  # a restart/replay in between must not change who knows
        self.go("mina", "living")
        self.clock.advance(6000)
        self.k.tick()
        proj.run()
        proj.run()
        c = self.world()
        rows = [(r["text"], json.loads(r["knowers"])) for r in
                c.execute("select * from script where source='kernel' and text like '%看电影%' and text not like '%约%'")]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("奈尔", rows[0][0])
        self.assertIn("艾莎", rows[0][0])
        self.assertEqual(rows[0][1], ["elsa", "nell"])
        self.assertNotIn("米娜", rows[0][0])

    def test_now_row_together_only_counts_plan_members(self):
        """now_row: mina, not invited, runs movie_night with the same plan id in the same room on a second
        tv; 「此刻」 shows nell and elsa together, never mina."""
        p = self.plan("movie_night", ["elsa"])
        self.accept("elsa", p)
        for a in ("nell", "elsa", "mina"):
            self.go(a, "living")
        self.clock.advance(600)
        for a in ("nell", "elsa"):
            r = self.cmd(a, "begin_activity", activity="movie_night", seconds=6000, party=p)
            self.assertEqual(r["status"], "running", r)
        # only now a second tv, so mina's run is legal on its own and shares nothing (added earlier, elsa would
        # take the free one instead of joining nell)
        with self.s.tx() as db:
            db.execute("INSERT INTO resources(resource_id,kind,item,location,holder,qty,capacity,source)"
                       " SELECT 'z:tv2',kind,item,location,holder,qty,capacity,'fixture' FROM resources"
                       " WHERE resource_id='tv'")
        r = self.cmd("mina", "begin_activity", activity="movie_night", seconds=6000, party=p)
        self.assertEqual(r["status"], "running", r)
        self.assertEqual(self.held("tv"), ["elsa", "nell"])
        self.assertEqual(self.held("z:tv2"), ["mina"])
        proj = Projector(self.l, self.wpath)
        nell, elsa = proj.now_row("nell")["doing"], proj.now_row("elsa")["doing"]
        self.assertNotIn("米娜", nell)
        self.assertNotIn("米娜", elsa)
        self.assertIn("和艾莎一起", nell)
        self.assertIn("和奈尔一起", elsa)
        # mina is not a member: just the activity's own label (「一起看电影」), no 「和…一起」 naming anyone
        self.assertEqual(proj.now_row("mina")["doing"], "一起看电影")

    def test_one_world_per_world_db_and_no_synthetic_into_a_real_one(self):
        """Regression test."""
        Projector(self.l, self.wpath).run()
        clock = VirtualClock(self.clock.now())
        other = Store(os.path.join(self.d, "other.db"), clock, world_id="test/other", synthetic=True, holder="t2")
        other.acquire()
        self.addCleanup(other.close)
        with self.assertRaises(RuntimeError):
            Projector(Life(other, model=None), self.wpath).run()
        real = os.path.join(self.d, "real.db")
        offline_world(real)
        c = sqlite3.connect(real)
        c.execute("delete from kernel_bind")  # a real world.db: world.py's, never marked offline
        c.commit()
        c.close()
        with self.assertRaises(RuntimeError):
            Projector(self.l, real).run()
        self.assertEqual(sqlite3.connect(real).execute("select count(*) from script").fetchone()[0], 0)

    def test_expired_invite_is_a_plain_fact_for_both(self):
        """nobody answered in time: both know, no refusal and no feeling."""
        p = self.plan("movie_night", ["elsa"], start=self.clock.now() + 3600)
        self.clock.advance(4 * 3600)
        self.l.plans.sync()
        self.assertEqual(self.s.db.execute("select state from invitations where plan_id=?", (p,)).fetchone()[0],
                         "expired")
        Projector(self.l, self.wpath).run()
        c = self.world()
        rows = [(r["text"], json.loads(r["knowers"])) for r in c.execute("select * from script where text like '%约%'")]
        self.assertEqual(rows, [("奈尔约艾莎一起看电影，到点还没等到回复", ["elsa", "nell"])])
        self.assertEqual(c.execute("select count(*) from emotions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
