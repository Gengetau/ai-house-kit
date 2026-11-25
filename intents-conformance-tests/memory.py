"""`import memory` for the tests in this directory: the shared memory module (src/ai_house/common/memory.py), in the
one loader copy of _copy.py (the roster of made-up housemates in roster.py, and the two settings the deployment
hard-codes and the kit leaves empty)."""

import sys

import _copy

sys.modules[__name__] = _copy.MEMORY
