"""The life1 full-life vocabulary (world-catalog life1/*.json), for Kernel(vocab=...)."""
import sys

from . import vocab as _p1a  # noqa: F401  (puts world-catalog on sys.path)
from .vocab import edible

from worldcatalog.kernel_vocab_life1 import (ACTIONS, ACTIVITIES, BASE_DRIFT, BEDROOM, BODY_KEYS, BODY_RANGE,  # noqa
                                             CATALOG, CLOTHING, GARMENT_ASSETS, HOUSE, INGREDIENTS, INSTANT,
                                             LOCATIONS, MAIN_FOODS, MENU, MONEY, PEOPLE, PLACE, PLACES, RECIPES,
                                             RENDER, ROUTES, SNACKS, VOCAB_VERSION, activity_allowed_at, bed, guard,
                                             label, next_hop, owner_of, place_id, public_location, route_seconds,
                                             snack_ok, validate, validate_garment_assets)

EDIBLE = edible(sys.modules[__name__])

__all__ = ["ACTIONS", "ACTIVITIES", "BASE_DRIFT", "BEDROOM", "BODY_KEYS", "BODY_RANGE", "CATALOG", "CLOTHING", "EDIBLE",
           "GARMENT_ASSETS", "HOUSE", "INGREDIENTS", "INSTANT", "LOCATIONS", "MAIN_FOODS", "MENU", "MONEY", "PEOPLE",
           "PLACE", "PLACES", "RECIPES", "RENDER", "ROUTES", "SNACKS", "VOCAB_VERSION", "activity_allowed_at", "bed",
           "edible", "guard", "label", "next_hop", "owner_of", "place_id", "public_location", "route_seconds",
           "snack_ok", "validate", "validate_garment_assets"]
