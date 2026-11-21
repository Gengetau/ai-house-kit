"""The P1b shadow vocabulary (world-catalog p1b.json on top of catalog.json), for Kernel(vocab=...).

Same names as worldkernel.vocab, plus ACTIVITIES (begin_activity), bed() (her own bedroom), owner_of() and
public_location(). The default vocabulary of the kernel stays the P1a one.
"""
import sys

from . import vocab as _p1a  # noqa: F401  (puts world-catalog on sys.path)
from .vocab import edible

from worldcatalog.kernel_vocab_p1b import (ACTIONS, ACTIVITIES, BASE_DRIFT, BODY_KEYS, BODY_RANGE,  # noqa: E402
                                           CATALOG, LOCATIONS, RECIPES, ROUTES, VOCAB_VERSION, activity_allowed_at,
                                           bed, guard, next_hop, owner_of, public_location, route_seconds,
                                           validate)

EDIBLE = edible(sys.modules[__name__])

__all__ = ["ACTIONS", "ACTIVITIES", "BASE_DRIFT", "BODY_KEYS", "BODY_RANGE", "CATALOG", "EDIBLE", "LOCATIONS",
           "RECIPES", "ROUTES", "VOCAB_VERSION", "activity_allowed_at", "bed", "edible", "guard", "next_hop",
           "owner_of", "public_location", "route_seconds", "validate"]
