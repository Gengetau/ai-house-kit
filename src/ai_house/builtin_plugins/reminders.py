"""Built-in timed reminders plugin (`reminders.db` + urgent Outbox delivery, `0.3.0`)."""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    PluginTask,
    PluginTaskContext,
    ToolContext,
    ToolRegistration,
)

JST = dt.timezone(dt.timedelta(hours=9))
_TIME_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_TIME_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{1,2}):(\d{2})$")

REMINDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    scope TEXT NOT NULL,
    channel TEXT NOT NULL,
    target_channel TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    creator_role TEXT NOT NULL,
    creator_name TEXT NOT NULL,
    message TEXT NOT NULL,
    due_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    fired_at REAL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(state, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_scope ON reminders(scope, channel, creator_id, state);
"""


def _parse_due_at(now: float, *, in_minutes: Optional[float] = None, at_jst: str = "") -> float:
    ts = float(now)
    if in_minutes is not None:
        mins = float(in_minutes)
        if mins <= 0 or mins > 43200:
            raise ValueError("in_minutes 必须在 (0, 43200] 之间")
        return ts + mins * 60.0
    spec = str(at_jst or "").strip()
    if not spec:
        raise ValueError("请提供 in_minutes 或 at_jst（例如 '18:30' 或 '2026-11-23 09:00'）")
    cur_dt = dt.datetime.fromtimestamp(ts, JST)
    m_dt = _TIME_DT_RE.match(spec)
    if m_dt:
        yy, mo, dd, hh, mm = (int(x) for x in m_dt.groups())
        due_dt = dt.datetime(yy, mo, dd, hh, mm, tzinfo=JST)
        return due_dt.timestamp()
    m_hm = _TIME_HM_RE.match(spec)
    if m_hm:
        hh, mm = (int(x) for x in m_hm.groups())
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"无效时间: {at_jst!r}")
        cand = dt.datetime.combine(cur_dt.date(), dt.time(hh, mm), JST)
        if cand.timestamp() <= ts:
            cand = cand + dt.timedelta(days=1)
        return cand.timestamp()
    raise ValueError(f"无法解析提醒时间: {at_jst!r}")


class ReminderStore:
    """Thread-safe SQLite store for scheduled chat reminders in `runtime_dir/reminders.db`."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.db_path = Path(runtime_dir) / "reminders.db"

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db_path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def init(self) -> None:
        with contextlib.closing(self._connect()) as c:
            with c:
                c.executescript(REMINDERS_SCHEMA)

    @staticmethod
    def _creator_id(speaker: Any) -> str:
        ident = str(getattr(speaker, "identity_id", "") or "").strip()
        if ident:
            return ident
        if getattr(speaker, "is_owner", False):
            return "user:owner"
        return f"member:{getattr(speaker, 'name', 'anon') or 'anon'}"

    @staticmethod
    def _target_channel_for(channel: str) -> str:
        ch = str(channel or "").strip()
        if ch.startswith("qq-"):
            return "onebot_v11"
        return "webchat"

    def create(
        self,
        *,
        actor: str,
        scope: str,
        channel: str,
        speaker: Any,
        message: str,
        due_at: float,
        now: float,
        max_active_per_user: int = 20,
    ) -> Dict[str, Any]:
        clean_msg = str(message or "").strip()[:240]
        if not clean_msg:
            raise ValueError("提醒内容不能为空")
        sc = "private" if str(scope or "").strip().lower() == "private" else "group"
        ch = str(channel or ("private:" + actor if sc == "private" else "group")).strip()
        is_owner = bool(getattr(speaker, "is_owner", True))
        creator_role = "owner" if is_owner else "member"
        creator_id = self._creator_id(speaker)
        creator_name = str(getattr(speaker, "name", "") or ("房主" if is_owner else "成员"))[:48]
        target_ch = self._target_channel_for(ch)
        self.init()
        with contextlib.closing(self._connect()) as c:
            with c:
                cnt = int(
                    c.execute(
                        "SELECT COUNT(*) FROM reminders WHERE creator_id=? AND state='pending'",
                        (creator_id,),
                    ).fetchone()[0]
                )
                if cnt >= max(1, int(max_active_per_user or 20)):
                    raise ValueError(f"待触发提醒已达上限 ({max_active_per_user})")
                cur = c.execute(
                    "INSERT INTO reminders("
                    "actor, scope, channel, target_channel, creator_id, creator_role, creator_name, "
                    "message, due_at, state, created_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (actor, sc, ch, target_ch, creator_id, creator_role, creator_name, clean_msg, float(due_at), float(now)),
                )
                row = c.execute("SELECT * FROM reminders WHERE reminder_id=?", (cur.lastrowid,)).fetchone()
                return self._format_row(row)

    @staticmethod
    def _format_row(r: sqlite3.Row) -> Dict[str, Any]:
        due_jst = dt.datetime.fromtimestamp(float(r["due_at"]), JST).strftime("%Y-%m-%d %H:%M")
        return {
            "reminder_id": int(r["reminder_id"]),
            "actor": r["actor"],
            "scope": r["scope"],
            "channel": r["channel"],
            "target_channel": r["target_channel"],
            "creator_id": r["creator_id"],
            "creator_role": r["creator_role"],
            "creator_name": r["creator_name"],
            "message": r["message"],
            "due_at": float(r["due_at"]),
            "due_jst": due_jst,
            "state": r["state"],
            "created_at": float(r["created_at"]),
            "fired_at": r["fired_at"],
        }

    def list_visible(
        self,
        *,
        scope: Optional[str] = None,
        channel: Optional[str] = None,
        speaker: Any = None,
        state: Optional[str] = "pending",
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        if not self.db_path.exists():
            return []
        self.init()
        clauses: List[str] = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(str(state))
        if scope is not None:
            sc = str(scope).strip().lower()
            clauses.append("scope=?")
            params.append(sc)
            if channel:
                clauses.append("channel=?")
                params.append(str(channel).strip())
            if sc == "private" and speaker is not None and not getattr(speaker, "is_owner", False):
                return []
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        sql = f"SELECT * FROM reminders {where_sql}ORDER BY due_at ASC, reminder_id ASC LIMIT ?"
        params.append(max(1, min(int(limit or 40), 200)))
        with contextlib.closing(self._connect()) as c:
            rows = c.execute(sql, params).fetchall()
            return [self._format_row(r) for r in rows]

    def cancel(
        self,
        reminder_id: int,
        *,
        scope: Optional[str] = None,
        channel: Optional[str] = None,
        speaker: Any = None,
    ) -> Dict[str, Any]:
        rid = int(reminder_id)
        if not self.db_path.exists():
            raise ValueError(f"未找到提醒 #{rid}")
        self.init()
        is_owner = speaker is None or bool(getattr(speaker, "is_owner", False))
        caller_id = self._creator_id(speaker) if speaker is not None else ""
        with contextlib.closing(self._connect()) as c:
            with c:
                row = c.execute("SELECT * FROM reminders WHERE reminder_id=?", (rid,)).fetchone()
                if row is None:
                    raise ValueError(f"未找到提醒 #{rid}")
                if scope == "group" and row["scope"] != "group":
                    raise ValueError("群聊不能操作私聊提醒")
                if channel and row["channel"] != channel and scope == "group":
                    raise ValueError("不能跨频道操作其他群聊提醒")
                if not is_owner and row["creator_id"] != caller_id:
                    raise ValueError("只能取消自己创建的提醒")
                c.execute(
                    "UPDATE reminders SET state='cancelled' WHERE reminder_id=? AND state='pending'",
                    (rid,),
                )
                updated = c.execute("SELECT * FROM reminders WHERE reminder_id=?", (rid,)).fetchone()
                return self._format_row(updated)

    def fire_due(self, daemon: Any, *, now: float) -> List[Dict[str, Any]]:
        if not self.db_path.exists() or getattr(daemon, "needs_onboarding", True) or getattr(daemon, "authority", None) is None:
            return []
        self.init()
        ts = float(now)
        claimed: List[sqlite3.Row] = []
        with contextlib.closing(self._connect()) as c:
            with c:
                due_rows = c.execute(
                    "SELECT * FROM reminders WHERE state='pending' AND due_at<=? ORDER BY due_at ASC LIMIT 20",
                    (ts,),
                ).fetchall()
                for r in due_rows:
                    cur = c.execute(
                        "UPDATE reminders SET state='fired', fired_at=? WHERE reminder_id=? AND state='pending'",
                        (ts, r["reminder_id"]),
                    )
                    if cur.rowcount == 1:
                        claimed.append(r)

        dispatched: List[Dict[str, Any]] = []
        for r in claimed:
            text = f"⏰ 到点提醒：{r['message']}"
            if r["scope"] == "group" and r["creator_name"]:
                text = f"⏰ {r['creator_name']}，到点提醒：{r['message']}"
            payload: Dict[str, Any] = {
                "actor": r["actor"],
                "kind": r["scope"],
                "scope": r["scope"],
                "text": text,
                "urgent": True,
                "source": "reminder",
                "reminder_id": int(r["reminder_id"]),
            }
            if r["target_channel"] == "webchat":
                payload["target_channel"] = r["channel"]
            elif r["target_channel"] == "onebot_v11":
                ch_str = str(r["channel"] or "")
                if ch_str.startswith("qq-group:"):
                    payload["target"] = ch_str.split(":", 1)[1]
                elif r["creator_id"]:
                    payload["to_identity"] = r["creator_id"]
            if r["scope"] == "private":
                payload["to_identity"] = r["creator_id"] or "owner"
            did = daemon.enqueue_outbound(
                channel=r["target_channel"],
                payload=payload,
                event_id=int(r["reminder_id"]),
            )
            dispatched.append({"reminder_id": int(r["reminder_id"]), "delivery_id": did, "text": text})
        if dispatched:
            daemon.flush_outbox(reconcile=False)
        return dispatched


def _store_from_ctx(daemon: Any) -> ReminderStore:
    if daemon is None or getattr(daemon, "runtime_dir", None) is None:
        raise RuntimeError("daemon runtime_dir 不可用")
    return ReminderStore(daemon.runtime_dir)


def set_reminder(
    message: str,
    in_minutes: Optional[float] = None,
    at_jst: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    if ctx is None or ctx.daemon is None:
        raise ValueError("缺少 ToolContext")
    if ctx.scope == "private" and not getattr(ctx.speaker, "is_owner", False):
        raise ValueError("非房主不能设置私聊提醒")
    due_at = _parse_due_at(ctx.now, in_minutes=in_minutes, at_jst=at_jst)
    max_cap = int((ctx.plugin_config or {}).get("max_active_per_user") or 20)
    store = _store_from_ctx(ctx.daemon)
    item = store.create(
        actor=ctx.actor,
        scope=ctx.scope,
        channel=ctx.channel,
        speaker=ctx.speaker,
        message=message,
        due_at=due_at,
        now=ctx.now,
        max_active_per_user=max_cap,
    )
    return {"ok": True, "reminder": item}


def list_reminders(ctx: Optional[ToolContext] = None) -> Dict[str, Any]:
    if ctx is None or ctx.daemon is None:
        raise ValueError("缺少 ToolContext")
    store = _store_from_ctx(ctx.daemon)
    rows = store.list_visible(scope=ctx.scope, channel=ctx.channel, speaker=ctx.speaker, state="pending")
    return {"ok": True, "reminders": rows}


def cancel_reminder(reminder_id: int, ctx: Optional[ToolContext] = None) -> Dict[str, Any]:
    if ctx is None or ctx.daemon is None:
        raise ValueError("缺少 ToolContext")
    store = _store_from_ctx(ctx.daemon)
    item = store.cancel(reminder_id, scope=ctx.scope, channel=ctx.channel, speaker=ctx.speaker)
    return {"ok": True, "reminder": item}


def _task_due_tick(ctx: PluginTaskContext) -> None:
    if ctx.daemon is None or getattr(ctx.daemon, "runtime_dir", None) is None:
        return
    ReminderStore(ctx.daemon.runtime_dir).fire_due(ctx.daemon, now=ctx.now)


def _route_list(ctx: PluginRequestContext) -> Dict[str, Any]:
    store = _store_from_ctx(ctx.daemon)
    scope = (ctx.query.get("scope") or [None])[0]
    channel = (ctx.query.get("channel") or [None])[0]
    state = (ctx.query.get("state") or ["pending"])[0] or None
    return {"ok": True, "reminders": store.list_visible(scope=scope, channel=channel, state=state)}


def _route_cancel(ctx: PluginRequestContext) -> Dict[str, Any]:
    store = _store_from_ctx(ctx.daemon)
    body = ctx.body if isinstance(ctx.body, dict) else {}
    rid = body.get("reminder_id") or body.get("id")
    if rid is None:
        raise ValueError("缺少 reminder_id")
    return {"ok": True, "reminder": store.cancel(int(rid))}


_SET_SPEC = {
    "name": "set_reminder",
    "description": "为用户设置一个定时提醒（支持相对分钟数 in_minutes 或 JST 时刻 at_jst）。",
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "提醒事项内容"},
            "in_minutes": {"type": "number", "description": "多少分钟后提醒"},
            "at_jst": {"type": "string", "description": "JST 时间，如 18:30 或 2026-11-23 09:00"},
        },
        "required": ["message"],
    },
}

_LIST_SPEC = {
    "name": "list_reminders",
    "description": "查看当前会话范围内尚未触发的待办提醒（严格隔离私聊与群聊）。",
    "parameters": {"type": "object", "properties": {}},
}

_CANCEL_SPEC = {
    "name": "cancel_reminder",
    "description": "取消指定编号的提醒（群友只能取消自己在当前群创建的提醒）。",
    "parameters": {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "integer", "description": "提醒编号 reminder_id"},
        },
        "required": ["reminder_id"],
    },
}

PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="reminders",
        name="定时提醒助手",
        kind="tool",
        version="0.3.0",
        description="提供隔离作用域的定时提醒持久化、到点走 Outbox 加急推送与列表取消接口。",
        default_enabled=True,
        config_fields=(
            ConfigField(
                key="max_active_per_user",
                label="单用户待触发上限",
                field_type="int",
                default=20,
                min_val=1,
                max_val=100,
                env_var="AI_HOUSE_REMINDERS_MAX_ACTIVE",
                description="同一用户同时可保留的 pending 定时提醒条数上限。",
            ),
        ),
    ),
    tools=(
        ToolRegistration(spec=_SET_SPEC, handler=set_reminder, plugin_id="reminders", takes_context=True),
        ToolRegistration(spec=_LIST_SPEC, handler=list_reminders, plugin_id="reminders", takes_context=True),
        ToolRegistration(spec=_CANCEL_SPEC, handler=cancel_reminder, plugin_id="reminders", takes_context=True),
    ),
    routes=(
        PluginRoute(method="GET", subpath="/list", handler=_route_list, tier="private"),
        PluginRoute(method="POST", subpath="/cancel", handler=_route_cancel, tier="private"),
    ),
    tasks=(
        PluginTask(task_id="reminders_due_tick", interval_s=15.0, handler=_task_due_tick, run_outside_lock=True),
    ),
)
