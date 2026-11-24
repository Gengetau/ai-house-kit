"""`import memory` for the tests in this directory.

The tests were written beside the private deployment's memory.py and say `import memory`. Here that name gives
them the kit's shared copy of the same file (src/ai_house/common/memory.py), loaded with the roster of made-up
housemates in roster.py, with the two settings the deployment hard-codes and the kit leaves empty.
"""

import os
import sys

import roster

# the deployment's default for the group that is not checked for leaks; the kit's default is "no group"
os.environ.setdefault("MEMORY_LEAK_FREE_CHATS", "group:1000000296")
# the memory model's address: never called here (the tests patch the call), only put into a request object
os.environ.setdefault("CPA_BASE", "http://memory-model.invalid")

from ai_house.common import loader  # noqa: E402

sys.modules[__name__] = loader.load(roster=roster)
