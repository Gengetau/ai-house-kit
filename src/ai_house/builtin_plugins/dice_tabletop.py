"""Built-in demo tool plugin: sharehouse tabletop dice & daily fortune (`roll_dice`)."""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict

_FORTUNES = ("大吉 · 今天厨艺爆棚", "中吉 · 适合在客厅喝茶看书", "小吉 · 散步或许会遇见好风景", "吉 · 安稳惬意的一天")


def roll_dice(sides: int = 6, count: int = 1, seed_hint: str = "") -> Dict[str, Any]:
    s = max(2, min(int(sides or 6), 100))
    n = max(1, min(int(count or 1), 5))
    rng = random.Random(seed_hint) if seed_hint else random.Random()
    rolls = [rng.randint(1, s) for _ in range(n)]
    return {"ok": True, "sides": s, "rolls": rolls, "total": sum(rolls)}


def daily_fortune(actor: str, day: str = "") -> Dict[str, Any]:
    idx = int(hashlib.sha256(f"{day}:{actor}".encode("utf-8")).hexdigest()[:6], 16) % len(_FORTUNES)
    return {"ok": True, "actor": actor, "fortune": _FORTUNES[idx]}


TOOL_SPEC = {
    "name": "roll_dice",
    "description": "在群聊或桌游互动时掷骰子（例如 1d6、2d20）。",
    "parameters": {
        "type": "object",
        "properties": {
            "sides": {"type": "integer", "minimum": 2, "maximum": 100},
            "count": {"type": "integer", "minimum": 1, "maximum": 5},
        },
    },
}
