"""Kernel -> legacy world.db projection: one simulated day, a restart, no duplicates."""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, JST, run_days  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.legacy import Projector  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402
from worldkernel.legacy import LEGACY_SCHEMA as LEGACY  # noqa: E402


class LegacyProjection(unittest.TestCase):
    def test_day_restart(self):
        d = tempfile.mkdtemp()
        wpath, kpath = os.path.join(d, "world.db"), os.path.join(d, "life1.db")
        c = sqlite3.connect(wpath)
        c.executescript(LEGACY)
        # a synthetic (simulated) kernel only writes into an offline target
        c.execute("create table if not exists kernel_bind(k text primary key, v text not null)")
        c.execute("insert into kernel_bind values('mode', 'offline')")
        for a in V.PEOPLE:
            c.execute("insert into reality values(?, 'room', '卧室', 'sleeping', '睡觉', '平静', 60, 30, 70, 'sleeping',"
                      " 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
        c.commit()
        c.close()
        start = dt.datetime(2026, 11, 3, 5, 0, tzinfo=JST).timestamp()
        clock, model = VirtualClock(start), FakeLifeModel()

        def build():
            s = Store(kpath, clock, world_id="ai-house/life1-test", synthetic=True, holder="test")
            s.acquire()
            life = Life(s, model=model)
            return life, Projector(life, wpath)

        life, proj = build()
        hits = {}
        hours = iter(range(10 ** 6))

        def until(h):
            while clock.now() < start + h * 3600:
                life.step()
                if next(hours) % 3 == 0:
                    proj.run()
                    if clock.now() >= start + 7.5 * 3600 and "nell_morning" not in hits:
                        hits["nell_morning"] = sqlite3.connect(wpath).execute(
                            "select activity, source from reality where actor='nell'").fetchone()
                    if clock.now() >= start + 16 * 3600 and "card_21" not in hits:  # 21:00 JST
                        hits["card_21"] = json.loads(sqlite3.connect(wpath).execute(
                            "select data from kernel_card where actor='nell'").fetchone()[0])
                clock.advance(60)

        until(14)
        life.s.release()
        life, proj = build()
        life.k.resume(clock.now())
        until(24)
        proj.run()
        proj.run()
        c = sqlite3.connect(wpath)
        c.row_factory = sqlite3.Row
        self.assertEqual(hits["nell_morning"][1], "kernel")
        self.assertNotEqual(hits["nell_morning"][0], "sleeping")  # nell gets up at 06:00
        lines = [r["text"] for r in c.execute("select text from script where source='kernel'")]
        self.assertTrue(any("吃了" in x for x in lines), lines[:5])
        self.assertTrue(any("做了" in x for x in lines))
        dup = c.execute("select count(*) from (select ts, text from script group by ts, text having count(*)>1)").fetchone()[0]
        self.assertEqual(dup, 0)
        for r in c.execute("select knowers from script where source='kernel'"):
            self.assertTrue(set(json.loads(r[0])) <= set(V.PEOPLE))
        cards = {r["actor"]: json.loads(r["data"]) for r in c.execute("select * from kernel_card")}
        self.assertEqual(set(cards), set(V.PEOPLE))
        self.assertIn("晚饭", hits["card_21"]["meals"], hits["card_21"])
        self.assertTrue(hits["card_21"]["outfit"])
        edup = c.execute("select count(*) from (select actor, entry from emotions group by actor, entry having count(*)>1)"
                         ).fetchone()[0]
        self.assertEqual(edup, 0)
        life.s.release()

    def test_authority_runner(self):
        """authority.py: seeds from world.db once, steps + projects, and comes back after a restart."""
        import authority
        d = tempfile.mkdtemp()
        wpath, kpath = os.path.join(d, "world.db"), os.path.join(d, "life1.db")
        c = sqlite3.connect(wpath)
        c.executescript(LEGACY)
        for a in V.PEOPLE:
            c.execute("insert into reality values(?, 'living', '客厅', 'idle', '发呆', '平静', 50, 60, 30, 'free',"
                      " 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
        c.commit()
        clock = VirtualClock(dt.datetime(2026, 11, 7, 10, 0, tzinfo=JST).timestamp())
        a = authority.Authority(kpath, wpath, None, {}, clock=clock, model=FakeLifeModel())
        for _ in range(90):
            a.step()
            clock.advance(60)
        a.store.release()
        b = authority.Authority(kpath, wpath, None, {}, clock=clock, model=FakeLifeModel())
        for _ in range(30):
            b.step()
            clock.advance(60)
        rows = {r[0]: r[1] for r in c.execute("select actor, source from reality")}
        self.assertEqual(set(rows.values()), {"kernel"})
        self.assertEqual(b.store.meta("seeded") is not None, True)
        b.store.release()


if __name__ == "__main__":
    unittest.main()
