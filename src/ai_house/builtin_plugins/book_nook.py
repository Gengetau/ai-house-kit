"""Built-in demo LifeExtension + tool: shared living-room bookshelf notes (`book_nook`)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..plugin_api import LifeExtension

_BOOK_TOPICS = (
    "《海边小站气象手记》：记着晴空都北环线的旧车站故事",
    "《铸铁锅与慢火咖喱》：洋葱要小火炒足十五分钟才会甜",
    "《合租屋阳台的香草栽培》：迷迭香和薄荷其实比想象中好养",
    "《雨天窗边速写集》：伞骨收拢时的水滴声音很安静",
)


class BookNookExtension(LifeExtension):
    """Records a short public bookshelf note whenever a roommate finishes reading."""

    shops = False

    def __init__(self, life: Any):
        super().__init__(life)
        self._seen_event_id = int(self.life.get("book_nook:cursor") or 0)

    @classmethod
    def from_env(cls, life: Any, env: Optional[Dict[str, str]] = None) -> "BookNookExtension":
        return cls(life)

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
                self.life.put("book_nook:notes", notes[-12:])
        self._seen_event_id = max_eid

    def context(self, actor_id: str, decision_kind: str) -> Optional[Dict[str, Any]]:
        notes = self.life.get("book_nook:notes") or []
        return {"bookshelf_notes": notes[-3:]} if notes else None

    def recent_notes(self) -> List[Dict[str, Any]]:
        return list(self.life.get("book_nook:notes") or [])


TOOL_SPEC = {
    "name": "browse_bookshelf",
    "description": "翻看合租屋客厅书架上大家留下的公开读书便签。",
    "parameters": {"type": "object", "properties": {}},
}
