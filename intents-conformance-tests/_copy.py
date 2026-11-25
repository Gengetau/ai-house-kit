"""The one loader copy that every shim in this directory hands out, so `import intents`, `import world`,
`import qq_identity` and `import memory` are the four modules of the same copy, bound to the same roster."""

import os
import sys

# the deployment's default for the group that is not checked for leaks; the kit's default is "no group"
os.environ.setdefault("MEMORY_LEAK_FREE_CHATS", "group:1000000296")
# the memory model's address: never called here (the tests patch the call), only put into a request object
os.environ.setdefault("CPA_BASE", "http://memory-model.invalid")

_KERNEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "world-kernel")
if _KERNEL not in sys.path:  # `authority`, `worldkernel`, `test_life_outings` (the tests add it too)
    sys.path.insert(0, _KERNEL)

import roster  # noqa: E402
from ai_house.common import loader  # noqa: E402

MEMORY = loader.load(roster=roster)


def part(name):
    return loader.part(MEMORY, name)
