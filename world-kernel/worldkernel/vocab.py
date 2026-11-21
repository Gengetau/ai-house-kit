"""vocabulary = the catalog (world-catalog, single source catalog.json), plus its mandatory guard.

The P1a placeholder that stood here until 10-23 is replaced by worldcatalog.kernel_vocab; this module only
makes world-catalog importable next to world-kernel and re-exports the names the kernel uses. guard() is
the catalog's pure request check; the kernel calls it at start, before settling, and when it queues.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_DIR = os.environ.get("WORLD_CATALOG_DIR") or os.path.join(_HERE, "..", "..", "world-catalog")
if _CATALOG_DIR not in sys.path:
    sys.path.insert(0, _CATALOG_DIR)

from worldcatalog.kernel_vocab import (ACTIONS, BASE_DRIFT, BODY_KEYS, BODY_RANGE, CATALOG, LOCATIONS,  # noqa: E402
                                       RECIPES, ROUTES, VOCAB_VERSION, guard, next_hop, route_seconds, validate)

def edible(vocab):
    """Items that may be eaten as they are, from whichever catalog object the kernel was given."""
    return frozenset(k for k, v in vocab.CATALOG["items"].items() if v.get("kind") == "food" and v.get("edible"))


EDIBLE = edible(sys.modules[__name__])

__all__ = ["ACTIONS", "BASE_DRIFT", "BODY_KEYS", "BODY_RANGE", "CATALOG", "EDIBLE", "LOCATIONS", "RECIPES", "ROUTES",
           "VOCAB_VERSION", "edible", "guard", "next_hop", "route_seconds", "validate"]
