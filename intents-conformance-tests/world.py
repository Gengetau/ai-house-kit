"""`import world` for the tests in this directory.

The tests were written beside the private deployment's world.py, a large module that runs the housemates' days. The
kit does not carry it. What the promise code uses from it is src/ai_house/common/world_shim.py, and this gives the
tests that (in the one loader copy of _copy.py) plus what only the tests touch, kept here and not in src/:
- `HOME_CHAT`, the house's own group, which the private tests take for granted (it is where `kernel_context` looks);
- the names `WorldBase.setUp` patches (`SEED_FILE`, `WEATHER`, `_seed_cache`, `_calls`, `deque`): harmless values;
- the `reality` table that `WorldBase.set_state` updates, made by `init()`;
- `card`, `advance`, `llm`, `outreach`: the world engine, which the kit does not have. Four test cases call them; they
  raise SkipTest, so those four show as skipped with that reason and nothing else does.
"""

import sys
import unittest
from collections import deque

import _copy  # noqa: F401 (puts world-kernel on sys.path)
from worldkernel import legacy  # noqa: E402 (_copy puts world-kernel on sys.path)

_W = _copy.part("world_shim")
_init = _W.init


def init():
    _init()
    with _W.db() as c:
        c.executescript(legacy.LEGACY_SCHEMA)  # the kernel's own copy of the tables of the private world.db it writes


def _needs_the_world_engine(*_a, **_k):
    raise unittest.SkipTest("needs the private deployment's world engine, which the kit does not carry")


_W.init = init
_W.HOME_CHAT = "group:1000000296"
_W.SEED_FILE = ""
_W.WEATHER = False
_W._seed_cache = (0.0, None)
_W._calls = deque()
_W.deque = deque
_W.card = _W.advance = _W.llm = _W.outreach = _needs_the_world_engine
sys.modules[__name__] = _W
