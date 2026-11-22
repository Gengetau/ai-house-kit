"""Built-in demo LifeExtension + tool: shared living-room bookshelf notes (`book_nook`)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..plugin_api import ConfigField, LifeExtension, PluginDefinition, PluginManifest, ToolContext, ToolRegistration

_BOOK_TOPICS = (
    "《海边小站气象手记》：记着晴空都北环线的旧车站故事",
    "《铸铁锅与慢火咖喱》：洋葱要小火炒足十五分钟才会甜",
    "《合租屋阳台的香草栽培》：迷迭香和薄荷其实比想象中好养",
    "《雨天窗边速写集》：伞骨收拢时的水滴声音很安静",
)


class BookNookExtension(LifeExtension):
    """Records a short public bookshelf note whenever a roommate finishes reading."""

    shops = False

    def __init__(self, life: Any, max_notes: int = 12):
        super().__init__(life)
        self.max_notes = max(1, min(int(max_notes or 12), 50))
        self._seen_event_id = int(self.life.get("book_nook:cursor") or 0)

    @classmethod
    def from_env(cls, life: Any, env: Optional[Dict[str, str]] = None) -> "BookNookExtension":
        import os

        eff = os.environ if env is None else env
        try:
            max_notes = int((eff.get("AI_HOUSE_BOOK_NOOK_MAX_NOTES") or "12").strip())
        except ValueError:
            max_notes = 12
        return cls(life, max_notes=max_notes)

    def step(self) -> None:
        with self.life.s.read() as db:
            rows = db.execute(
                "SELECT event_id, actor_id, payload, occurred_at FROM events "
                "WHERE event_id>? AND kind='action_succeeded' ORDER BY event_id ASC LIMIT 32",
                (self._seen_event_id,),
            ).fetchall()
        if not rows:
            return
        notes: List[Dict[str, Any]] = list(self.life.get("book_nook:notes") or [])
        changed = False
        max_eid = self._seen_event_id
        for r in rows:
            max_eid = max(max_eid, int(r["event_id"]))
            if "reading" in str(r["payload"] or ""):
                topic = _BOOK_TOPICS[max_eid % len(_BOOK_TOPICS)]
                actor = str(r["actor_id"])
                notes.append({
                    "actor": actor,
                    "name": self.life.names.get(actor, actor),
                    "note": topic,
                    "at": float(r["occurred_at"]),
                })
                changed = True
        with self.life.s.tx():
            self.life.put("book_nook:cursor", max_eid)
            if changed:
                self.life.put("book_nook:notes", notes[-self.max_notes:])
        self._seen_event_id = max_eid

    def context(self, actor_id: str, decision_kind: str) -> Optional[Dict[str, Any]]:
        notes = self.life.get("book_nook:notes") or []
        return {"bookshelf_notes": notes[-3:]} if notes else None

    def recent_notes(self) -> List[Dict[str, Any]]:
        return list(self.life.get("book_nook:notes") or [])


def browse_bookshelf(ctx: ToolContext) -> Dict[str, Any]:
    """Context-aware chat tool handler returning recent living-room bookshelf notes."""
    nook = ctx.daemon._book_nook() if ctx.daemon is not None and hasattr(ctx.daemon, "_book_nook") else None
    return {"notes": nook.recent_notes() if nook is not None else []}


TOOL_SPEC = {
    "name": "browse_bookshelf",
    "description": "翻看合租屋客厅书架上大家留下的公开读书便签。",
    "parameters": {"type": "object", "properties": {}},
}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="book_nook",
        name="客厅书架便签",
        kind="extension",
        version="0.3.0",
        description="室友读书结束后自动记录公开读书便签，并向对话提供书架工具与生活上下文。",
        default_enabled=True,
        config_fields=(
            ConfigField(
                key="max_notes",
                label="保留便签条数",
                field_type="int",
                default=12,
                min_val=1,
                max_val=50,
                env_var="AI_HOUSE_BOOK_NOOK_MAX_NOTES",
                description="客厅书架最多保留的最新公开读书便签条数（1–50）。",
            ),
        ),
    ),
    extension_factory=BookNookExtension.from_env,
    tools=(ToolRegistration(spec=TOOL_SPEC, handler=browse_bookshelf, plugin_id="book_nook", takes_context=True),),
)
