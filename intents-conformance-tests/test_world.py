# From the private deployment's test_world.py (scrubbed): the parts the promise tests import.
import datetime
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import world as W

JST = W.JST


def at(hh, mm=0, day=14):
    return datetime.datetime(2026, 10, day, hh, mm, tzinfo=JST).timestamp()


class WorldBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(W, "DB_PATH", str(Path(self.tmp.name) / "world.db")),
                  patch.object(W, "SEED_FILE", str(Path(self.tmp.name) / "seed.json")),
                  patch.object(W, "WEATHER", False),
                  patch.object(W, "_seed_cache", (0.0, None)),
                  patch.object(W, "_calls", W.deque())):
            p.start()
            self.addCleanup(p.stop)
        self.now = at(15)
        with patch("time.time", return_value=self.now):
            W.init()

    def set_state(self, actor, **kw):
        with W.db() as c:
            sets = ", ".join(f"{k}=?" for k in kw)
            c.execute(f"update reality set {sets} where actor=?", [*kw.values(), actor])

    def set_feel(self, actor, at=None, **kw):
        """Her body numbers (情绪), counted from `at` (default now)."""
        with W.db() as c:
            r = c.execute("select * from reality where actor=?", (actor,)).fetchone()
            f = {**W._feel(r), **kw, "at": self.now if at is None else at}
            c.execute("update reality set feel=? where actor=?", (json.dumps(f), actor))

    def refresh(self, actor, t):
        with W.db() as c:
            return W._refresh(c, actor, t, sky="")


def answer(members, at_list, state=None, shares=()):
    st = state or {"location": "study", "place": "书房窗边", "activity": "reading", "doing": "看诗集", "mood": "relaxed",
                   "energy": 70, "hunger": 30, "sleepiness": 20, "availability": "free", "until": "17:00", "beat": 0}
    beats = [{"at": t, "text": f"第{i}拍：在书房里慢慢翻书，泡了一壶红茶。"} for i, t in enumerate(at_list)]
    got = {"beats": beats, "shares": list(shares)}
    if len(members) > 1:
        got["states"] = {m: dict(st) for m in members}
    else:
        got["state"] = st
    return got
