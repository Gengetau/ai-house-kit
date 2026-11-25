"""M1: the house runs on the memory store that shares the private deployment's code (`CommonMemoryStore`, its own
`memory-common.db`), and a house that ran on the earlier store keeps what she remembered.

`AI_HOUSE_MEMORY_BACKEND` chose between the two stores while there were two; it does nothing now. A whole house is
run here with a scripted model: no network, made-up names. Old-format `memory.db` files are written by the frozen
copy of the earlier store kept for that purpose (`legacy_memory_store.py`).
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
from ai_house import memory as memory_mod  # noqa: E402
from ai_house.daemon import JST, MEMORY_BACKEND_ENV, HouseDaemon, memory_backend_note  # noqa: E402
from ai_house.memory_common import CommonMemoryStore  # noqa: E402
from legacy_memory_store import MemoryStore as OldMemoryStore  # noqa: E402

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


class TheOldSwitchTests(unittest.TestCase):
    """`AI_HOUSE_MEMORY_BACKEND` is still accepted so a `.env` that has it keeps working; it changes nothing."""

    def test_only_a_value_that_asked_for_something_else_is_remarked_on(self):
        for quiet in (None, "", "common", " Common "):
            self.assertEqual(memory_backend_note(quiet), "", quiet)
        for said in ("legacy", "new", "comon", "1"):
            self.assertIn(MEMORY_BACKEND_ENV, memory_backend_note(said), said)

    def test_the_house_runs_on_the_one_store_whatever_it_says(self):
        for backend in (None, "", "common", "legacy", "comon"):
            d, rt, said = house(self, backend)
            self.assertIs(type(d.memory), CommonMemoryStore, backend)
            self.assertTrue((rt / "memory-common.db").exists(), backend)
            self.assertFalse((rt / "memory.db").exists(), backend)
            self.assertEqual(MEMORY_BACKEND_ENV in said, backend in ("legacy", "comon"), backend)

    def test_the_name_callers_have_always_imported_is_that_store(self):
        self.assertIs(memory_mod.MemoryStore, CommonMemoryStore)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = memory_mod.configure(tmp.name, env={})
        self.addCleanup(memory_mod.reset_default_store)
        self.assertIs(type(st), CommonMemoryStore)
        memory_mod.reset_default_store()
        with self.assertRaises(RuntimeError):
            memory_mod.recall("sora", "private", "x")

    def test_doctor_says_the_setting_does_nothing_and_does_not_fail_on_it(self):
        import json
        import os
        from unittest.mock import patch

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / ".env"
        seen = {}
        for value in ("legacy", "common"):
            env_file.write_text(f"{MEMORY_BACKEND_ENV}={value}\n", encoding="utf-8")
            out = io.StringIO()
            with patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(out):
                os.environ.pop(MEMORY_BACKEND_ENV, None)
                cli.main(["doctor", "--env-file", str(env_file), "--data-dir", str(Path(tmp.name) / "rt"), "--json"])
            seen[value] = {c["name"]: c for c in json.loads(out.getvalue())["checks"]}
        self.assertTrue(seen["legacy"]["memory_backend"]["ok"])
        self.assertIn(MEMORY_BACKEND_ENV, seen["legacy"]["memory_backend"]["detail"])
        self.assertNotIn("memory_backend", seen["common"])


class HouseOnTheNewStore(unittest.TestCase):
    def setUp(self):
        self.model = Model()
        self.d, self.rt, _ = house(self, None, self.model)

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
    """A house that ran on the earlier store keeps what she remembers: the start carries the old file's memories over."""

    FACT = "房主对花生过敏，吃了会起疹子"

    def old_house(self, rt=None):
        """A house folder as the earlier version left it: everything but a `memory-common.db`, and a `memory.db`
        written by the earlier store itself. With `rt`, only the `memory.db` is written into that folder."""
        if rt is None:
            d, rt, _ = house(self)
            d.close()
            for left in rt.glob("memory-common.db*"):
                left.unlink()
        old = OldMemoryStore(rt, names={"sora": "Sora", "rin": "Rin"}, env={})
        old.add("sora", "private", self.FACT, now=T0)
        old.close()
        return rt

    def texts(self, d, actor="sora"):
        return " ".join(r["text"] for r in d.list_memories(actor=actor, scope="private"))

    def test_what_she_remembered_is_there_after_the_switch(self):
        rt = self.old_house()
        before = sha(rt / "memory.db")
        model = Model()
        d, _, said = house(self, None, model, rt=rt)
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
        d, _, _ = house(self, None, rt=rt)
        mid = next(r["id"] for r in d.list_memories(actor="sora", scope="private") if "花生" in r["text"])
        d.forget_memory(actor="sora", scope="private", mem_id=mid)
        d.close()
        d, _, said = house(self, None, rt=rt)
        self.assertNotIn("花生", self.texts(d))
        self.assertNotIn("搬进新库", said)
        self.assertEqual(sorted(rt.glob("memory-common.db.bak-*")), [])

    def test_an_old_file_that_cannot_be_read_does_not_stop_the_house(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        rt.mkdir()
        (rt / "memory.db").write_bytes(b"this is not a database " * 64)
        d, _, said = house(self, None, rt=rt)
        self.assertIs(type(d.memory), CommonMemoryStore)
        self.assertIn("旧记忆库", said)
        self.assertEqual(self.texts(d), "")

    def test_a_reset_removes_the_copies_the_carrying_over_kept(self):
        d, rt, _ = house(self)  # the new file exists first, so carrying over keeps a copy of it
        d.close()
        self.old_house(rt)
        d, _, _ = house(self, None, rt=rt)
        self.assertIn("花生过敏", self.texts(d))
        self.assertEqual(len(list(rt.glob("memory-common.db.bak-*"))), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            d.reset_house()
        self.assertEqual([p.name for p in rt.iterdir() if p.name.startswith("memory")], [])


if __name__ == "__main__":
    unittest.main()
