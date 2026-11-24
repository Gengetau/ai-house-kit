"""M1: the switch that puts the house on the memory store sharing the private deployment's code.

`AI_HOUSE_MEMORY_BACKEND=common` -> `CommonMemoryStore` (its own `memory-common.db`); unset, empty or `legacy` -> the
old `MemoryStore`, as before. A whole house is run here with a scripted model: no network, made-up names.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import cli  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import (  # noqa: E402
    JST,
    MEMORY_BACKEND_ENV,
    HouseDaemon,
    memory_backend,
    memory_backend_problem,
)
from ai_house.memory import MemoryStore  # noqa: E402
from ai_house.memory_common import CommonMemoryStore  # noqa: E402

PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
T0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
TOLD = "悄悄跟你说，我对花生过敏，吃了会起疹子，这件事别告诉别人"
ASK = "你还记得我对什么过敏吗？"


class Model:
    """Answers every chat turn with `reply`; keeps what it was shown."""

    def __init__(self, reply="好的，我记下了。"):
        self.reply, self.seen = reply, []

    def __call__(self, system, user):
        self.seen.append((system, user))
        return self.reply, 10


def house(tc, backend=None, model=None, rt=None, **env):
    """A house in a new folder, or (`rt`) started again on a folder an earlier one used."""
    if rt is None:
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
    full = dict(env)
    if backend is not None:
        full[MEMORY_BACKEND_ENV] = backend
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        d = HouseDaemon(rt, pack_dir=PACK, env=full, start_ts=T0, virtual_clock=True,
                        allow_onboarding=False, llm_fn=model or Model())
    tc.addCleanup(d.close)
    return d, rt, out.getvalue()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SwitchTests(unittest.TestCase):
    def test_reading_the_switch(self):
        for raw, want in ((None, "legacy"), ("", "legacy"), ("legacy", "legacy"), (" Common ", "common"),
                          ("common", "common"), ("new", "legacy"), ("1", "legacy")):
            self.assertEqual(memory_backend({} if raw is None else {MEMORY_BACKEND_ENV: raw}), want, raw)
        for ok in ("", "legacy", "common", " COMMON "):
            self.assertEqual(memory_backend_problem(ok), "", ok)
        for bad in ("new", "1", "on", "comon"):
            self.assertIn(MEMORY_BACKEND_ENV, memory_backend_problem(bad), bad)

    def test_the_house_runs_on_the_old_store_unless_told(self):
        for backend in (None, "", "legacy"):
            d, rt, _ = house(self, backend)
            self.assertIs(type(d.memory), MemoryStore, backend)
            self.assertFalse((rt / "memory-common.db").exists())

    def test_common_puts_it_on_the_new_store_and_leaves_the_old_file_alone(self):
        d, rt, _ = house(self, "common")
        self.assertIs(type(d.memory), CommonMemoryStore)
        self.assertTrue((rt / "memory-common.db").exists())
        self.assertFalse((rt / "memory.db").exists())

    def test_a_misspelt_name_built_in_code_falls_back_and_says_so(self):
        d, _, said = house(self, "comon")
        self.assertIs(type(d.memory), MemoryStore)
        self.assertIn(MEMORY_BACKEND_ENV, said)

    def test_up_refuses_a_misspelt_name(self):
        import os
        from unittest.mock import patch

        err = io.StringIO()
        args = type("A", (), {"env_file": None, "host": None, "port": None, "data_dir": None})()
        with patch.dict(os.environ, {MEMORY_BACKEND_ENV: "comon"}), patch.object(cli, "_load_dotenv", lambda p: None), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_up(args), 2)
        self.assertIn(MEMORY_BACKEND_ENV, err.getvalue())


class HouseOnTheNewStore(unittest.TestCase):
    def setUp(self):
        self.model = Model()
        self.d, self.rt, _ = house(self, "common", self.model)

    def say(self, channel, text):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.d.converse(channel, text, owner_speaker("房主"))

    def shown(self):
        return "\n".join(s + "\n" + u for s, u in self.model.seen)

    def test_what_the_owner_said_in_private_comes_back_in_private(self):
        self.assertTrue(self.say("private:sora", TOLD)["ok"])
        self.model.seen.clear()
        self.d.clock.advance(120)
        self.assertTrue(self.say("private:sora", ASK)["ok"])
        self.assertIn("花生过敏", self.shown().split(ASK)[0])  # in what she was given before the question itself
        with contextlib.closing(sqlite3.connect(self.rt / "memory-common.db")) as c:
            self.assertGreaterEqual(c.execute("select count(*) from seen where consumer='sora'").fetchone()[0], 1)

    def test_it_does_not_reach_another_housemate_or_the_group(self):
        self.say("private:sora", TOLD)
        self.model.seen.clear()
        self.d.clock.advance(120)
        self.say("private:rin", ASK)
        self.say("group", ASK)
        self.assertNotIn("花生", self.shown())

    def test_saying_it_again_in_the_group_is_stopped(self):
        self.say("private:sora", TOLD)
        self.d.clock.advance(120)
        self.model.reply = "房主跟我说过，" + TOLD
        res = self.say("group", "sora，房主有什么忌口？")
        self.assertNotIn("花生", " ".join(r.get("text", "") for r in res.get("replies", [])))
        self.assertNotIn("花生", " ".join(r["text"] for r in self.d.list_chat("group", limit=10)))

    def test_the_owner_pages_read_and_remove_through_it(self):
        mid, _ = self.d.memory.add("sora", "private", "房主的生日是3月6日", now=self.d.clock.now())
        rows = self.d.list_memories(actor="sora", scope="private")
        self.assertIn("3月6日", " ".join(r["text"] for r in rows))
        self.d.forget_memory(actor="sora", scope="private", mem_id=mid)
        self.assertNotIn("3月6日", " ".join(r["text"] for r in self.d.list_memories(actor="sora", scope="private")))

    def test_a_reset_removes_its_file(self):
        self.d.memory.add("sora", "private", "房主的生日是3月6日", now=self.d.clock.now())
        with contextlib.redirect_stdout(io.StringIO()):
            self.d.reset_house()
        self.assertFalse((self.rt / "memory-common.db").exists())
        self.assertEqual(self.d.list_memories(actor="sora", scope="private"), [])


class AHouseThatRanOnTheOldStore(unittest.TestCase):
    """Switching to the new store keeps what she remembers: the start carries the old file's memories over."""

    FACT = "房主对花生过敏，吃了会起疹子"

    def old_house(self, rt=None):
        d, rt, _ = house(self, None, rt=rt)
        d.memory.add("sora", "private", self.FACT, now=d.clock.now())
        d.close()
        return rt

    def texts(self, d, actor="sora"):
        return " ".join(r["text"] for r in d.list_memories(actor=actor, scope="private"))

    def test_what_she_remembered_is_there_after_the_switch(self):
        rt = self.old_house()
        before = sha(rt / "memory.db")
        model = Model()
        d, _, said = house(self, "common", model, rt=rt)
        self.assertIn("花生过敏", self.texts(d))
        self.assertNotIn("花生", self.texts(d, "rin"))  # still hers alone
        self.assertEqual(sha(rt / "memory.db"), before)  # the old file is only read
        self.assertIn("搬进新库 1 条", said)
        self.assertNotIn("花生", said)  # the log counts, it does not quote
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(d.converse("private:sora", ASK, owner_speaker("房主"))["ok"])
        self.assertIn("花生过敏", "\n".join(s + "\n" + u for s, u in model.seen).split(ASK)[0])

    def test_a_second_start_adds_nothing_and_what_was_removed_stays_removed(self):
        rt = self.old_house()
        d, _, _ = house(self, "common", rt=rt)
        mid = next(r["id"] for r in d.list_memories(actor="sora", scope="private") if "花生" in r["text"])
        d.forget_memory(actor="sora", scope="private", mem_id=mid)
        d.close()
        d, _, said = house(self, "common", rt=rt)
        self.assertNotIn("花生", self.texts(d))
        self.assertNotIn("搬进新库", said)
        self.assertEqual(sorted(rt.glob("memory-common.db.bak-*")), [])

    def test_an_old_file_that_cannot_be_read_does_not_stop_the_house(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        rt.mkdir()
        (rt / "memory.db").write_bytes(b"this is not a database " * 64)
        d, _, said = house(self, "common", rt=rt)
        self.assertIs(type(d.memory), CommonMemoryStore)
        self.assertIn("旧记忆库", said)
        self.assertEqual(self.texts(d), "")

    def test_the_old_store_does_not_look_at_any_of_this(self):
        rt = self.old_house()
        d, _, said = house(self, None, rt=rt)
        self.assertIs(type(d.memory), MemoryStore)
        self.assertFalse((rt / "memory-common.db").exists())
        self.assertNotIn("搬进新库", said)

    def test_a_reset_removes_the_copies_the_carrying_over_kept(self):
        d, rt, _ = house(self, "common")  # the new file exists first, so carrying over keeps a copy of it
        d.close()
        self.old_house(rt)
        d, _, _ = house(self, "common", rt=rt)
        self.assertIn("花生过敏", self.texts(d))
        self.assertEqual(len(list(rt.glob("memory-common.db.bak-*"))), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            d.reset_house()
        self.assertEqual([p.name for p in rt.iterdir() if p.name.startswith("memory")], [])


if __name__ == "__main__":
    unittest.main()
