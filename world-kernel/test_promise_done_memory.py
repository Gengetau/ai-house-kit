"""A finished promise ("约定") becomes a memory of hers, once.

Defect (live run): the promise was registered with a memory "…答应，并设了提醒"; when the kernel later closed the
errand nothing was written, so from the next day recall only said she had promised. No network, no real model.
Every test says what would make it fail.
"""
import contextlib
import datetime as dt
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir, OWNER_LINE, T0, egg_ops, fake_model, rows  # noqa: E402

from ai_house.daemon import HouseDaemon, JST  # noqa: E402

DONE_MARK = "把答应房主的事办完了"


def done_rows(d, actor="nell"):
    """Every active memory row of hers that says a promise was finished, whatever its scope."""
    out = []
    for scope in ("private", "group", "world"):
        out += [r for r in d.memory.list_entries(actor, scope, limit=200) if DONE_MARK in r["text"]]
    return out


def fmt(ts):
    t = dt.datetime.fromtimestamp(ts, JST)
    return f"{t.month}月{t.day}日 {t:%H:%M}"


class Base(Dir):
    def private_house(self, env=None, name="奈尔"):
        d = self.daemon(env={"INTENTS": "1", **(env or {})})
        d.memory.observe("nell", "private", OWNER_LINE, speaker_role="owner", speaker="房主", reply="好的", now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model(egg_ops(name)))
        return d

    def group_house(self):
        d = self.daemon(env={"INTENTS": "1"})
        d.memory.observe("nell", "group", OWNER_LINE, channel="group", speaker_role="owner", speaker="房主", reply="好的", now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model(egg_ops("奈尔")))
        return d

    def finish(self, d):
        for _ in range(12):
            d.fast_forward(240)
            if d.intents.list_intents(actor="nell")[0]["status"] == "completed":
                return
        self.fail("the errand never completed")

    def forget_done_rows(self, d):
        for r in d.memory.list_entries("nell", "private", limit=200):
            if DONE_MARK in r["text"]:
                d.memory.invalidate("nell", "private", r["id"])


class WriteTests(Base):
    def test_a_private_promise_done_is_one_private_memory_in_the_same_chat(self):
        """Fails if nothing is written when the kernel closes the errand, if the row is not private to the owner's chat
        with her, or if its kind / wording / house-clock time are not as promised."""
        d = self.private_house()
        self.assertEqual(done_rows(d), [])
        self.finish(d)
        got = done_rows(d)
        self.assertEqual(len(got), 1)
        r = got[0]
        self.assertEqual((r["scope"], r["channel"], r["owner"], r["kind"]), ("private", "private:nell", "nell", "event"))
        done_at = rows(d.world_db, "select updated from intents where status='completed'")[0]["updated"]
        self.assertEqual(r["text"], f"{fmt(done_at)} 奈尔把答应房主的事办完了：奈尔买2个鸡蛋")

    def test_a_second_post_step_run_adds_none(self):
        """Fails if the same completed promise is written again by a later post-step run."""
        d = self.private_house()
        self.finish(d)
        for _ in range(3):
            d._run_post_step_jobs()
        d.fast_forward(15)
        self.assertEqual(len(done_rows(d)), 1)

    def test_reopening_the_house_adds_none(self):
        """Fails if the marker is not durable (a restart on the same data dir writes the finished promise again)."""
        d = self.private_house()
        self.finish(d)
        rt = d.runtime_dir
        d.close()
        d2 = HouseDaemon(rt, pack_dir=None, env={"INTENTS": "1"}, virtual_clock=True, allow_onboarding=False)
        self.addCleanup(d2.close)
        d2._run_post_step_jobs()
        d2.fast_forward(15)
        self.assertEqual(len(done_rows(d2)), 1)

    def test_a_group_promise_is_a_memory_of_that_group(self):
        """Fails if a promise made in the web group is written anywhere but that group's scope."""
        d = self.group_house()
        self.assertEqual(rows(d.world_db, "select visibility from intents")[0]["visibility"], "group")
        self.finish(d)
        got = done_rows(d)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["scope"], got[0]["channel"], got[0]["owner"]), ("group", "group", "nell"))

    def test_a_private_completion_is_not_recalled_in_a_group(self):
        """Fails if the private row can be recalled from a group turn while the private-in-groups switch is off, or is
        not recalled in the private chat."""
        d = self.private_house()
        self.finish(d)
        q = "鸡蛋 办完了"
        in_group = d.memory.recall("nell", "group", q, channel="group", speaker_role="owner", speaker="房主", now=d.clock.now())
        self.assertNotIn(DONE_MARK, in_group)
        in_private = d.memory.recall("nell", "private", q, now=d.clock.now())
        self.assertIn(DONE_MARK, in_private)

    def test_a_private_completion_follows_the_private_in_groups_switch(self):
        """Fails if, with the existing private-in-groups switch on, the row is withheld from a group turn (it must follow
        the same rules as every other private memory, no stricter and no looser)."""
        d = self.private_house(env={"AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS": "1"})
        self.finish(d)
        in_group = d.memory.recall("nell", "group", "鸡蛋 办完了", channel="group", speaker_role="owner", speaker="房主", now=d.clock.now())
        self.assertIn(DONE_MARK, in_group)

    def test_intents_off_writes_nothing(self):
        """Fails if a completed row is turned into a memory while INTENTS=0."""
        d = self.private_house()
        self.finish(d)
        d.intents.intents_module.ON = False
        with contextlib.closing(sqlite3.connect(d.world_db)) as c, c:
            c.execute("delete from kit_intent_done_memos")
        self.forget_done_rows(d)
        d._run_post_step_jobs()
        self.assertEqual(done_rows(d), [])

    def test_a_failing_write_does_not_reach_the_step_loop_and_is_tried_again(self):
        """Fails if an error from the memory store escapes `_run_post_step_jobs`, or if a failed write is marked done
        (so that it is never written)."""
        d = self.private_house()
        real = d.memory.add
        boom = mock.Mock(side_effect=RuntimeError("disk full"))
        d.memory.add = boom
        try:
            self.finish(d)  # fast_forward runs the post-step jobs; no exception
        finally:
            d.memory.add = real
        self.assertTrue(boom.called)
        self.assertEqual(done_rows(d), [])
        d._run_post_step_jobs()
        self.assertEqual(len(done_rows(d)), 1)

    def test_promises_done_before_the_marker_existed_are_not_back_filled(self):
        """Fails if an existing house, on first start with this code, writes a memory for every old completed promise."""
        d = self.private_house()
        self.finish(d)
        rt = d.runtime_dir
        self.forget_done_rows(d)
        d.close()
        with contextlib.closing(sqlite3.connect(Path(rt) / "world.db")) as c, c:
            c.execute("drop table kit_intent_done_memos")  # a house from before this change
        d2 = HouseDaemon(rt, pack_dir=None, env={"INTENTS": "1"}, virtual_clock=True, allow_onboarding=False)
        self.addCleanup(d2.close)
        d2._run_post_step_jobs()
        self.assertEqual(done_rows(d2), [])


if __name__ == "__main__":
    unittest.main()
