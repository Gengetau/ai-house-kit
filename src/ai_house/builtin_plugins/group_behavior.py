"""Built-in plugin manifest for Stage 2.2 group chat pacing, mute control & sleep silence (`0.3.0`)."""
from __future__ import annotations

from typing import Any, Dict

from ..plugin_api import ConfigField, PluginDefinition, PluginManifest, PluginRequestContext, PluginRoute


def _route_status(ctx: PluginRequestContext) -> Dict[str, Any]:
    daemon = ctx.daemon
    status = daemon.group_behavior.status_view(now=ctx.now) if getattr(daemon, "group_behavior", None) else {}
    return {"ok": True, "config": dict(ctx.plugin_config), **status}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="group_behavior",
        name="群聊节奏与闭麦护栏",
        kind="bundle",
        version="0.3.0",
        description="控制群聊回复概率、非房主连发上限、房主口令闭麦/开麦及住客熟睡静默。",
        default_enabled=True,
        config_fields=(
            ConfigField(
                key="reply_prob",
                label="非房主群聊回复概率",
                field_type="float",
                default=1.0,
                min_val=0.0,
                max_val=1.0,
                env_var="AI_HOUSE_GROUP_REPLY_PROB",
                description="群聊收到非房主成员消息时的回复概率（0.0–1.0；1.0 表示有问必答）。",
            ),
            ConfigField(
                key="max_streak",
                label="非房主连续回复上限",
                field_type="int",
                default=8,
                min_val=1,
                max_val=50,
                env_var="AI_HOUSE_GROUP_MAX_STREAK",
                description="未见房主插话前同一群聊最多连续回复轮数（超过后暂停直到超时重置或房主发言）。",
            ),
            ConfigField(
                key="streak_reset_s",
                label="连发计数重置间隔（秒）",
                field_type="int",
                default=300,
                min_val=10,
                max_val=3600,
                env_var="AI_HOUSE_GROUP_STREAK_RESET_S",
                description="群聊安静超过该秒数后自动清零连续回复计数（10–3600）。",
            ),
            ConfigField(
                key="mute_minutes",
                label="默认闭麦时长（分钟）",
                field_type="int",
                default=30,
                min_val=1,
                max_val=1440,
                env_var="AI_HOUSE_GROUP_MUTE_MINUTES",
                description="房主在群里说“闭麦”而未指定分钟数时的默认静默时长。",
            ),
            ConfigField(
                key="sleep_silence",
                label="住客熟睡时在群聊保持静默",
                field_type="bool",
                default=True,
                env_var="AI_HOUSE_GROUP_SLEEP_SILENCE",
                description="开启后熟睡中的住客不在群聊回复睡眠占位提示（私聊仍可查看未醒提示）。",
            ),
        ),
    ),
    routes=(
        PluginRoute(method="GET", subpath="/status", handler=_route_status, tier="owner"),
    ),
)
