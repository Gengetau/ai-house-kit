"""Built-in demo tool plugin: sharehouse tabletop dice & daily fortune (`roll_dice`)."""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict

from ..plugin_api import ConfigField, PluginDefinition, PluginManifest, ToolRegistration

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


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="dice_tabletop",
        name="桌面骰子小工具",
        kind="tool",
        version="0.3.0",
        description="提供无状态 roll_dice 掷骰互动工具，适合桌游与群聊随机抽签。",
        default_enabled=True,
        config_fields=(
            ConfigField(
                key="default_sides",
                label="默认骰子面数",
                field_type="int",
                default=6,
                min_val=2,
                max_val=100,
                env_var="AI_HOUSE_DICE_SIDES",
                description="未指定面数时默认使用的骰子面数（2–100）。",
            ),
        ),
    ),
    tools=(ToolRegistration(spec=TOOL_SPEC, handler=roll_dice, plugin_id="dice_tabletop"),),
)
