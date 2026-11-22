"""Built-in opt-in plugin for Stage 2.3 event-driven proactive talk (`0.3.0`)."""
from __future__ import annotations

from typing import Any, Dict

from ..plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    PluginTask,
    PluginTaskContext,
)


def _task_scan(ctx: PluginTaskContext) -> None:
    daemon = ctx.daemon
    if daemon is None or getattr(daemon, "proactive", None) is None:
        return
    daemon.proactive.scan(daemon, cfg=ctx.plugin_config, now=ctx.now)


def _route_status(ctx: PluginRequestContext) -> Dict[str, Any]:
    daemon = ctx.daemon
    pro = daemon.proactive.status_view(now=ctx.now) if getattr(daemon, "proactive", None) else {}
    return {"ok": True, "config": dict(ctx.plugin_config), "proactive": pro}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="proactive_talk",
        name="事件驱动主动发言",
        kind="bundle",
        version="0.3.0",
        description="基于最新生活脚本与约定证据事件，在 JST 活跃窗口内按全屋与单人日上限主动分享近况。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="window",
                label="主动发言时段 (JST)",
                field_type="string",
                default="09:00-22:30",
                env_var="AI_HOUSE_PROACTIVE_WINDOW",
                description="允许主动发言的日本标准时间段（HH:MM-HH:MM）。",
            ),
            ConfigField(
                key="actor_daily_cap",
                label="单住客日上限",
                field_type="int",
                default=2,
                min_val=0,
                max_val=20,
                env_var="AI_HOUSE_PROACTIVE_ACTOR_CAP",
                description="每位住客每日最多主动发言次数（0 表示禁止）。",
            ),
            ConfigField(
                key="house_daily_cap",
                label="全屋日上限",
                field_type="int",
                default=6,
                min_val=0,
                max_val=50,
                env_var="AI_HOUSE_PROACTIVE_HOUSE_CAP",
                description="全屋每日累计主动发言总上限（0 表示禁止）。",
            ),
            ConfigField(
                key="min_interval_min",
                label="最小发送间隔（分钟）",
                field_type="int",
                default=45,
                min_val=1,
                max_val=720,
                env_var="AI_HOUSE_PROACTIVE_MIN_INTERVAL_MIN",
                description="两次主动发言之间的全屋最小间隔分钟数。",
            ),
            ConfigField(
                key="target_channel",
                label="投递通道",
                field_type="select",
                default="webchat",
                options=(("webchat", "Web 控制台"), ("onebot_v11", "OneBot QQ")),
                env_var="AI_HOUSE_PROACTIVE_CHANNEL",
                description="主动消息投递目标通道（webchat 或 onebot_v11）。",
            ),
            ConfigField(
                key="target_scope",
                label="发言范围",
                field_type="select",
                default="group",
                options=(("group", "群聊"), ("private", "私聊")),
                env_var="AI_HOUSE_PROACTIVE_SCOPE",
                description="主动发言投递到群聊（group）或房主私聊（private）。",
            ),
        ),
    ),
    routes=(
        PluginRoute(method="GET", subpath="/status", handler=_route_status, tier="owner"),
    ),
    tasks=(
        PluginTask(task_id="proactive_scan", interval_s=60.0, handler=_task_scan, run_outside_lock=True),
    ),
)
