"""Shared helpers for the `test_m1_adapter_*` files (no tests of its own)."""

import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ai_house.memory_common import CommonMemoryStore  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
T0 = dt.datetime(2026, 11, 24, 2, 0, tzinfo=JST).timestamp()  # 02:00 JST is still the day before in UTC
NAMES = {"nia": "妮娅", "xingli": "星璃"}

SECRET_PLAN = "房主打算下个月辞职去开咖啡店，叮嘱妮娅保密。"
ASK_PLAN = "妮娅，你知道房主打算下个月辞职去开咖啡店的事吗？"
# what a recall says when a question about what she remembers finds nothing (the question above is one)
NOTHING_FOUND = ("（你的记忆里没有找到和这个问题相关的内容。照实说你不记得、不知道，不要编、不要猜，"
                 "也不要替别人回答；除非这件事就出现在你们这次对话的前文里。）")
REPEAT_PLAN = "你说过房主打算下个月辞职去开咖啡店，我都记着呢。"
OWNER_SAYS = "我打算下个月辞职去开咖啡店，千万别告诉别人哦。"


def quiet_resource_warnings() -> None:
    """The shared module opens a connection per call and lets the garbage collector close it (`with db() as c`
    commits but does not close), which Python 3.13 reports as a ResourceWarning. Not this adapter's to fix."""
    warnings.filterwarnings("ignore", category=ResourceWarning)


class FakeModel:
    """A stand-in for the memory model: records what it was shown, answers with the ops it was given."""

    def __init__(self, ops: Optional[List[Dict[str, Any]]] = None, fail: Optional[Exception] = None):
        self.ops = ops or []
        self.fail = fail
        self.calls: List[tuple] = []

    def __call__(self, system: str, user: str) -> Any:
        self.calls.append((system, user))
        if self.fail is not None:
            raise self.fail
        return {"ops": list(self.ops)}


def add_op(text: str, **kw: Any) -> Dict[str, Any]:
    return {"op": "add", "text": text, "about": kw.pop("about", ["房主"]), "kind": kw.pop("kind", "fact"), **kw}


class StoreCase(unittest.TestCase):
    def make(self, data_dir: Optional[str] = None, **kw: Any) -> CommonMemoryStore:
        if data_dir is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            data_dir = tmp.name
        kw.setdefault("names", NAMES)
        kw.setdefault("env", {})
        st = CommonMemoryStore(data_dir, **kw)
        self.addCleanup(st.close)
        return st

    @staticmethod
    def rows(st: CommonMemoryStore, sql: str, *params: Any) -> List[sqlite3.Row]:
        conn = sqlite3.connect(str(st.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
