"""房主化身走进小屋 (— the pixel house page as a place the owner can walk into).

The console never writes the kernel. It queues the owner's avatar commands into world.db (owner_avatar_queue, the
same inbox shape as food_input_queue); the authority drains the queue at the start of its step, so the
kernel keeps one writer and a restart loses nothing. Every command is cmd:owner and comes only from the console's
owner session (readonly logins cannot POST); a housemate or a model can never submit one.

What the owner can do: enter a room, move, keep the session alive, leave; say something in the room he is in;
hand a roommate in that room one snack from the kitchen stock. The people in that room perceive it (kernel
observers) and get one decision of their own to answer in the house: a line, stopping what they do, eating it.
Nothing here sends QQ. Owner words are only what he typed; the world never makes up an owner action.
"""
import json
import sqlite3
from pathlib import Path

from . import inventory
from . import vocab_life1 as V
from .store import LockHeld, dumps

OPS = ("enter", "move", "heartbeat", "leave", "say", "give")
LEASE_S = 300          # heartbeats arrive with the authority's 60 s step, so a 90 s lease would flap
CHAT_LEASE_S = 2 * 3600  # 「我到家了」 said on QQ: no page heartbeats
REACT_GAP_S = 90       # one reaction decision per roommate per 90 s at most
REACT_PER_HOUR = 10    # all reactions together (each = a decision + her line); the life budget (40/h) is shared
RESPONDERS = 2         # at most two of the people in the room answer one thing he did (the gift's receiver always)
TEXT_MAX = 200
QUEUE_MAX_AGE_S = 600  # review: a command that waited this long (the authority was down) is not applied late
KINDS = {"owner_entered": "owner_entered", "owner_input_received": "owner_said", "owner_gave": "owner_gave"}


class AvatarError(ValueError):
    pass


def _log(msg):
    print(f"[world-kernel avatar] {msg}", flush=True)


def rooms():
    return [k for k, loc in V.LOCATIONS.items() if loc.get("kind") == "room"]


def check(body, vocab=None):
    _V = vocab or V
    """Validate one console command; returns the clean dict (raises AvatarError with words for the page)."""
    if not isinstance(body, dict) or body.get("op") not in OPS:
        raise AvatarError("不认识的操作")
    cmd_id, session = body.get("cmd_id"), body.get("session")
    for v in (cmd_id, session):
        if not isinstance(v, str) or not 1 <= len(v) <= 64 or not v.replace("-", "").replace("_", "").isalnum():
            raise AvatarError("缺少编号")
    out = {"op": body["op"], "cmd_id": cmd_id, "session": session}
    if body.get("via") == "chat" and body["op"] in ("enter", "move", "leave"):
        out["via"] = "chat"
        said = body.get("said_at")  # when he said it on QQ; his chat lease runs from then
        if isinstance(said, (int, float)) and not isinstance(said, bool) and said > 0:
            out["said_at"] = said
    if body["op"] in ("enter", "move"):
        if body.get("location") not in [k for k, loc in _V.LOCATIONS.items() if loc.get("kind") == "room"]:
            raise AvatarError("没有这个房间")
        out["location"] = body["location"]
    elif body["op"] == "say":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            raise AvatarError(f"要说的话不能是空的，也不能超过 {TEXT_MAX} 字")
        out["text"] = text.strip()
    elif body["op"] == "give":
        if body.get("to") not in _V.PEOPLE or not isinstance(body.get("resource_id"), str) \
                or not 1 <= len(body["resource_id"]) <= 200:
            raise AvatarError("要选给谁、给什么")
        out["to"], out["resource_id"] = body["to"], body["resource_id"]
    return out


QUEUE_SCHEMA = ("CREATE TABLE IF NOT EXISTS owner_avatar_queue (cmd_id TEXT PRIMARY KEY, body TEXT NOT NULL,"
                " state TEXT NOT NULL, receipt TEXT, created_at REAL NOT NULL)")


def queue(world_db, principal, body, now):
    """Console adapter (world.py avatar '<json>'): durable input only, never touches the kernel."""
    if principal != "cmd:owner":
        return {"status": "failed", "reason": "owner_only"}
    if isinstance(body, dict):  # review P3: only shared-services' chat path writes via/said_at, never the page
        body = {k: v for k, v in body.items() if k not in ("via", "said_at")}
    cmd = check(body)
    c = sqlite3.connect(world_db, timeout=5)
    try:
        with c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(QUEUE_SCHEMA)
            row = c.execute("SELECT body, state, receipt FROM owner_avatar_queue WHERE cmd_id=?",
                            (cmd["cmd_id"],)).fetchone()
            if row:
                if json.loads(row[0]) != cmd:
                    return {"status": "failed", "reason": "cmd_conflict"}
                return json.loads(row[2]) if row[2] else {"status": "queued", "cmd_id": cmd["cmd_id"]}
            c.execute("INSERT INTO owner_avatar_queue VALUES(?,?,'queued',NULL,?)",
                      (cmd["cmd_id"], json.dumps(cmd, ensure_ascii=False, sort_keys=True), now))
            # keep the inbox small: answered commands older than a day go
            c.execute("DELETE FROM owner_avatar_queue WHERE state!='queued' AND created_at<?", (now - 86400,))
        return {"status": "queued", "cmd_id": cmd["cmd_id"]}
    finally:
        c.close()


def receipt(world_db, cmd_id):
    """What became of a queued command (the page asks after the next step), or None."""
    try:
        c = sqlite3.connect(Path(world_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='owner_avatar_queue'").fetchone():
            return None
        r = c.execute("SELECT state, receipt FROM owner_avatar_queue WHERE cmd_id=?", (cmd_id,)).fetchone()
        return (json.loads(r[1]) if r[1] else {"status": r[0]}) if r else None
    finally:
        c.close()


class Avatar:
    """Life extension: drains the owner's inbox, applies it in the kernel, wakes the people who perceived it."""

    def __init__(self, life, world_db=None):
        self.life, self.k, self.s, self.V = life, life.k, life.s, getattr(life, 'V', V)
        self.world_db = world_db
        with self.s.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS avatar_applied (cmd_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")

    # ------------------------------------------------------------------ the step
    def step(self):
        # review P2: like the two stock queues, a locked world.db or a bad row never stops her day
        for name, part in (("drain", self.drain), ("react", self.react)):
            try:
                part()
            except LockHeld:
                raise
            except Exception as e:  # noqa: BLE001
                _log(f"{name} failed: {type(e).__name__}: {e}")

    def drain(self):
        if not self.world_db or not Path(self.world_db).exists():
            return
        c = sqlite3.connect(Path(self.world_db).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='owner_avatar_queue'").fetchone():
                return
            rows = c.execute("SELECT cmd_id, body, created_at FROM owner_avatar_queue WHERE state='queued'"
                             " ORDER BY rowid LIMIT 32").fetchall()
            now = self.s.clock.now()
            for cmd_id, body, created in rows:
                try:
                    done = self._applied(cmd_id)  # applied before a crash: that receipt, however old
                    if done:
                        got = done
                    elif not isinstance(created, (int, float)) or now - created > QUEUE_MAX_AGE_S:
                        got = {"status": "failed", "reason": "expired"}
                    else:
                        got = self.apply(json.loads(body))
                except AvatarError as e:
                    got = {"status": "failed", "reason": str(e)}
                except LockHeld:
                    raise
                except Exception as e:  # noqa: BLE001  a row that can never apply must not stay queued
                    _log(f"{cmd_id} failed: {type(e).__name__}: {e}")
                    got = {"status": "failed", "reason": "error:" + type(e).__name__}
                with c:
                    c.execute("UPDATE owner_avatar_queue SET state=?, receipt=? WHERE cmd_id=?",
                              (got["status"], json.dumps(got, ensure_ascii=False, sort_keys=True), cmd_id))
        finally:
            c.close()

    def _applied(self, cmd_id):
        with self.s.read() as db:
            r = db.execute("SELECT receipt FROM avatar_applied WHERE cmd_id=?", (cmd_id,)).fetchone()
        return json.loads(r[0]) if r else None

    # ------------------------------------------------------------------ one command
    def apply(self, cmd, principal="cmd:owner"):
        """Apply one checked command in one kernel transaction; replay after a crash returns the first receipt."""
        if principal != "cmd:owner":
            return {"status": "failed", "reason": "owner_only"}
        cmd = check(cmd, vocab=self.V)
        with self.s.tx() as db:
            prior = db.execute("SELECT receipt FROM avatar_applied WHERE cmd_id=?", (cmd["cmd_id"],)).fetchone()
            if prior:
                return json.loads(prior[0])
            got = getattr(self, "_" + cmd["op"])(db, cmd)
            db.execute("INSERT INTO avatar_applied VALUES(?,?)", (cmd["cmd_id"], dumps(got)))
        return got

    def _where(self, db, session):
        now = self.s.clock.now()
        r = db.execute("SELECT location FROM owner_presence WHERE session_id=? AND state='present' AND lease_until>?",
                       (session, now)).fetchone()
        return r[0] if r else None

    def _lease(self, cmd):
        if cmd.get("via") != "chat":
            return LEASE_S
        # 「我到家了」 read 25 min after he said it gives him 1 h 35 min, not a fresh two hours
        return CHAT_LEASE_S - max(0.0, self.s.clock.now() - cmd.get("said_at", self.s.clock.now()))

    def _enter(self, db, cmd):
        lease = self._lease(cmd)
        if lease < 60:
            return {"status": "failed", "reason": "expired"}
        here = self._where(db, cmd["session"])
        if here == cmd["location"]:
            return self.k.owner_presence(cmd["session"], "heartbeat", lease=lease)
        if here:
            return self._move(db, cmd)
        # another tab of his that is still "present" elsewhere: that one left (one owner, one place)
        for r in db.execute("SELECT session_id FROM owner_presence WHERE state='present' AND session_id!=?",
                            (cmd["session"],)).fetchall():
            self.k.owner_presence(r[0], "leave")
        return self.k.owner_presence(cmd["session"], "enter", cmd["location"], lease=lease)

    def _move(self, db, cmd):
        lease = self._lease(cmd)
        if lease < 60:
            return {"status": "failed", "reason": "expired"}
        here = self._where(db, cmd["session"])
        if not here:
            return self._enter(db, cmd)
        if here == cmd["location"]:
            return self.k.owner_presence(cmd["session"], "heartbeat", lease=lease)
        return self.k.owner_presence(cmd["session"], "move", cmd["location"], lease=lease)

    def _heartbeat(self, db, cmd):
        return self.k.owner_presence(cmd["session"], "heartbeat", lease=LEASE_S)

    def _leave(self, db, cmd):
        if cmd.get("via") == "chat":  # 「我出门了」: he is out, whichever way he had come in
            gone = [r[0] for r in db.execute("SELECT session_id FROM owner_presence WHERE state='present'"
                                             " AND lease_until>?", (self.s.clock.now(),)).fetchall()]
            for s in gone:
                got = self.k.owner_presence(s, "leave")
            return got if gone else {"status": "succeeded", "note": "not_in_house"}
        if not self._where(db, cmd["session"]):
            return {"status": "succeeded", "note": "not_in_house"}
        return self.k.owner_presence(cmd["session"], "leave")

    def _say(self, db, cmd):
        loc = self._where(db, cmd["session"])
        if not loc:
            return {"status": "failed", "reason": "not_in_house"}
        now = self.s.clock.now()
        self.k.owner_presence(cmd["session"], "heartbeat", lease=LEASE_S)
        eid = self.k._emit("owner_input_received", None, None, loc, {"input": "talk", "text": cmd["text"]},
                           scope=f"room:{loc}", occurred=now, source_kind="owner_input",
                           source_ref=f"avatar:{cmd['cmd_id']}")
        return {"status": "succeeded", "event_id": eid}

    def _give(self, db, cmd):
        loc = self._where(db, cmd["session"])
        if not loc:
            return {"status": "failed", "reason": "not_in_house"}
        to = db.execute("SELECT location, activity FROM actors WHERE actor_id=?", (cmd["to"],)).fetchone()
        if not to or to[0] != loc:
            return {"status": "failed", "reason": "not_here"}
        if to[1] == "sleeping":
            return {"status": "failed", "reason": "asleep"}
        # the stock side (what may be handed over, one piece off the row, her new row) is inventory's
        r, why = inventory.giveable(self.k, db, cmd["resource_id"], V)
        if why:
            return {"status": "failed", "reason": why}
        now = self.s.clock.now()
        label = inventory.stock_name(r, V)
        gift = f"gift:{cmd['cmd_id']}"
        self.k.owner_presence(cmd["session"], "heartbeat", lease=LEASE_S)
        eid = self.k._emit("owner_gave", None, None, loc,
                           {"to": cmd["to"], "item": r["item"], "label": label, "resource_id": gift,
                            "from": r["resource_id"]},
                           scope=f"room:{loc}", occurred=now, source_kind="owner_input",
                           source_ref=f"avatar:{cmd['cmd_id']}")
        inventory.hand_over(self.k, db, r, cmd["to"], loc, gift, eid, vocab=V)
        return {"status": "succeeded", "event_id": eid, "resource_id": gift}

    # ------------------------------------------------------------------ their answer
    def react(self):
        """One decision each for the roommates who perceived something of the owner since the last step."""
        life = self.life
        now = self.s.clock.now()
        cursor = life.get("avatar:cursor", 0)
        with self.s.read() as db:
            rows = db.execute("SELECT e.event_id, e.kind, e.payload, o.observer_id FROM events e JOIN observations o"
                              " ON o.event_id=e.event_id WHERE e.event_id>? AND e.kind IN (?,?,?)"
                              " ORDER BY e.event_id", (cursor, *KINDS)).fetchall()
            top = db.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]
        want = {}
        rank = {"owner_entered": 0, "owner_said": 1, "owner_gave": 2}
        for r in rows:
            a = r["observer_id"]
            if a not in self.V.PEOPLE:
                continue
            kind = KINDS[r["kind"]]
            if kind == "owner_gave" and json.loads(r["payload"]).get("to") != a:
                continue  # she saw him hand it to someone else: that one is for the receiver to answer
            if a not in want or rank[kind] >= rank[want[a]]:
                want[a] = kind
        life.put("avatar:cursor", max(cursor, top))
        if not want:
            return
        hour = int(now // 3600)
        spent = life.get(f"avatar:reacts:{hour}", 0)
        # the gift's receiver first, then whoever answered him least recently
        order = sorted(want.items(), key=lambda x: (-rank[x[1]], life.get(f"avatar:react:{x[0]}", 0), x[0]))
        answered = 0
        for a, kind in order:
            if spent >= REACT_PER_HOUR or (answered >= RESPONDERS and kind != "owner_gave"):
                break
            me = self.k.actor(a)
            if me["activity"] == "sleeping" or a in life.cog.plans or any(j["actor"] == a for j in life.cog.pending):
                continue
            if now - life.get(f"avatar:react:{a}", 0) < REACT_GAP_S and kind == "owner_entered":
                continue
            life.put(f"avatar:react:{a}", now)
            if life.think(a, kind, gap=0) == "asked":
                spent += 1
                answered += 1
        life.put(f"avatar:reacts:{hour}", spent)

    def context(self, a, kind):
        if not kind.startswith("owner_"):
            return {}
        with self.s.read() as db:
            loc = db.execute("SELECT location FROM actors WHERE actor_id=?", (a,)).fetchone()[0]
            here = "owner" in self.k._present(db, loc)
        return {"owner_here": here}


def install(world_db=None):
    """Life(extensions=[avatar.install(world_db)])."""
    return lambda life: Avatar(life, world_db)


# ---------------------------------------------------------------------- the page's read (read-only, no Store)
STAGE_KINDS = ("said", "owner_entered", "owner_left_room", "owner_exited_avatar", "owner_input_received", "owner_gave")


def stage(life_db, now, hours=3, limit=120):
    """What the pixel house page shows: where the owner's avatar is, what was said and handed over in the house
    (room talk and anything to/from the owner; a quiet word between two roommates stays theirs), and the snacks
    in the kitchen he could hand over. Opens the db read-only."""
    p = Path(life_db)
    if not p.exists():
        return {"ok": False, "reason": "no_kernel"}
    db = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        # one owner, one place: a session still present comes first (a chat enter that puts his old tab out writes
        # both rows in the same step, with the same updated_at)
        pres = db.execute("SELECT session_id, state, location, lease_until FROM owner_presence ORDER BY"
                          " (state='present' AND lease_until>?) DESC, updated_at DESC LIMIT 1", (now,)).fetchone()
        owner = None
        if pres and pres["state"] == "present" and (pres["lease_until"] or 0) > now:
            owner = {"session": pres["session_id"], "location": pres["location"]}
        events = []
        for r in db.execute(f"SELECT event_id, kind, actor_id, location, occurred_at, payload, disclosure_scope"
                            f" FROM events WHERE occurred_at>? AND kind IN ({','.join('?' * len(STAGE_KINDS))})"
                            f" ORDER BY event_id DESC LIMIT ?", (now - hours * 3600, *STAGE_KINDS, limit)):
            scope = r["disclosure_scope"] or ""
            if r["kind"] == "said" and not (scope.startswith("room:") or "owner" in scope.split(":", 1)[-1].split(",")):
                continue
            pl = json.loads(r["payload"])
            e = {"id": r["event_id"], "kind": r["kind"], "who": r["actor_id"] or "owner", "room": r["location"],
                 "at": r["occurred_at"]}
            if r["kind"] in ("said", "owner_input_received"):
                e["text"] = pl.get("text")
                if pl.get("to"):
                    e["to"] = pl["to"]
            elif r["kind"] == "owner_gave":
                e.update(to=pl.get("to"), label=pl.get("label"))
            events.append(e)
        snacks = inventory.giveable_snacks(db, V)  # the kernel stock, by the name it has there
        people = {r["actor_id"]: {"room": r["location"], "activity": r["activity"]}
                  for r in db.execute("SELECT actor_id, location, activity FROM actors WHERE simulated=1")}
        return {"ok": True, "now": now, "owner": owner, "events": list(reversed(events)),
                "snacks": snacks, "people": people, "rooms": rooms()}
    finally:
        db.close()
