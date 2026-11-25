"""`import intents` for the tests in this directory: the shared module (src/ai_house/common/intents.py), in the one
loader copy of _copy.py."""

import sys

import _copy

sys.modules[__name__] = _copy.part("intents")
