"""聊天介入生活.

What the owner says to a roommate on QQ (her private chat or the 小屋 group) and what she agrees to there reach her
day in the house: shared-services' memory extraction (intents.queue_world, no extra model call) writes the batch's
owner lines, her own lines and the intents that changed into world.db owner_chat_queue; this extension drains it on
the authority's step and gives her one decision of her own (kind owner_chat) with that in front of her. She decides
what, if anything, she does in the house: go to bed as she said she would, invite the others to the hotpot they all
agreed on, or nothing. Nothing is done for her and nothing for the owner: he is not placed anywhere by chat.
"""
import json
import sqlite3
from pathlib import Path

from . import vocab_life1 as V
from .store import LockHeld

GAP_S = 900            # a chat alone (no intent of hers changed) asks her at most every 15 min
PER_HOUR = 12          # all chat decisions together, out of the life budget (40/h)
KEEP = 3               # the last chat batches she has in front of her


class ChatLink:
    def __init__(self, life, world_db=None):
        self.life, self.k, self.s = life, life.k, life.s
        self.world_db = world_db

    def step(self):
        try:  # review P2: a locked world.db or a bad row never stops her day (as the stock queues)
            rows = self.drain()
            if rows:
                self.decide(rows)
        except LockHeld:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[world-kernel chatlink] step failed: {type(e).__name__}: {e}", flush=True)

    def drain(self):
        """-> [(actor, body)] taken from world.db (marked taken at once: a crash loses a nudge, never repeats one)."""
        if not self.world_db or not Path(self.world_db).exists():
            return []
        c = sqlite3.connect(Path(self.world_db).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='owner_chat_queue'").fetchone():
                return []
            rows = c.execute("SELECT id, actor, body FROM owner_chat_queue WHERE state='queued' ORDER BY id LIMIT 64").fetchall()
            with c:
                c.executemany("UPDATE owner_chat_queue SET state='taken' WHERE id=?", [(r[0],) for r in rows])
            out = []
            for r in rows:
                try:
                    body = json.loads(r[2])
                except ValueError:
                    continue  # a bad row is already marked taken: it never comes back
                if r[1] in getattr(self.life, 'V', V).PEOPLE and isinstance(body, dict):
                    out.append((r[1], body))
            return out
        finally:
            c.close()

    def decide(self, rows):
        life, now = self.life, self.s.clock.now()
        latest = {}
        for a, body in rows:
            kept = (life.get(f"chat:{a}") or [])[-(KEEP - 1):] + [body]
            life.put(f"chat:{a}", kept)
            # what she agreed to asks her at once; a private chat with him asks her too (at most every GAP_S);
            # group talk with no plan of hers in it only stays in front of her (six of them would all stop at once)
            if body.get("intents") or body.get("where") == "private":
                latest[a] = latest.get(a, False) or bool(body.get("intents"))
        hour = int(now // 3600)
        spent = life.get(f"chat:asks:{hour}", 0)
        # a changed intent of hers first: that is what she said she would do
        for a, has_intent in sorted(latest.items(), key=lambda x: (not x[1], x[0])):
            if spent >= PER_HOUR:
                life.put(f"chat:missed:{a}", now)
                continue
            me = self.k.actor(a)
            if me["activity"] == "sleeping" or a in life.cog.plans or any(j["actor"] == a for j in life.cog.pending):
                continue  # asleep or deciding already: the chat stays in front of her for the next decision
            if not has_intent and now - life.get(f"chat:asked:{a}", 0) < GAP_S:
                continue
            life.put(f"chat:asked:{a}", now)
            if life.think(a, "owner_chat", gap=0) == "asked":
                spent += 1
        life.put(f"chat:asks:{hour}", spent)

    def context(self, a, kind):
        """Her recent QQ talk with the owner, for every decision of hers within two hours of it (she remembers)."""
        now = self.s.clock.now()
        chats = [b for b in self.life.get(f"chat:{a}") or [] if now - b.get("at", 0) < 7200]
        if not chats:
            return {}
        out = []
        for b in chats:
            item = {"在哪": "和房主的私聊" if b.get("where") == "private" else "小屋群聊",
                    "房主说": b.get("owner_said") or [], "你说": b.get("she_said") or []}
            if b.get("intents"):
                item["你的打算"] = [{"text": i["text"], "status": i["status"], "和谁": [p for p in i["party"] if p != a],
                                  "答应了的": i["agreed"]} | ({"时间": _span(i)} if _span(i) else {})
                                 for i in b["intents"]]
            out.append(item)
        return {"qq_chat": out}


def _span(i):
    import datetime as dt
    jst = dt.timezone(dt.timedelta(hours=9))
    f = lambda t: dt.datetime.fromtimestamp(t, jst).strftime("%m-%d %H:%M")
    if i.get("due_from") and i.get("due_to"):
        return f"{f(i['due_from'])} ~ {f(i['due_to'])}"
    if i.get("due_from"):
        return f(i["due_from"]) + " 起"
    return ""


def install(world_db=None):
    """Life(extensions=[chatlink.install(world_db)])."""
    return lambda life: ChatLink(life, world_db)
