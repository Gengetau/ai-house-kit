"""L0 routine of P1a (v1.1 §3.2): model-free needs check and plain everyday actions.

It may walk her toward bed in the sleep window, have her eat ready food she can see, or rest when worn out.
Everything it does is origin=routine, goes through the kernel like any command and is replaced by her own
decision. It never accepts invitations, starts commitments, cooks, or answers for anyone: when a need has
no plain answer it records a decision point marked would_call, so the cost of running without a model is
measurable. With a HabitBook the ranges come from her persona-derived, versioned habits (v1.1.1 F3): a
disabled habit does nothing, and a habit can only use its own allowed_actions. Without one, placeholder
defaults apply (unit tests and fixtures that do not model persona).
"""
import json

from .vocab import edible
from .guide import jst_minute

DEFAULT_HABITS = {
    "version": "habits/p1a-default-1",
    "source": "placeholder until persona-derived ranges (F3)",
    "sleep_jst": (23 * 60, 7 * 60),
    "eat_when_hunger": 75.0,
    "rest_when_energy": 20.0,
    "rest_seconds": 1200,
}


def in_window(minute, window):
    lo, hi = window
    return lo <= minute < hi if lo < hi else (minute >= lo or minute < hi)


class Routine:
    def __init__(self, kernel, habits=None, book=None):
        self.k, self.s = kernel, kernel.s
        self.habits, self.book = habits or {}, book

    def habit(self, actor_id):
        """Her current habit parameters, with the exact habit versions they came from."""
        if not self.book:
            h = dict(DEFAULT_HABITS, **self.habits.get(actor_id, {}))
            h.setdefault("refs", [h["version"]])
            h.setdefault("enabled", {"sleep": True, "eat_ready_food": True, "rest": True})
            h.setdefault("allowed", {"sleep": ("move", "sleep"), "eat_ready_food": ("eat",), "rest": ("rest",)})
            return h
        rows = {r["kind"]: r for r in self.book.listing(actor_id)}
        h = dict(DEFAULT_HABITS)
        if "sleep" in rows:
            h["sleep_jst"] = tuple(rows["sleep"]["time_window"])
        if "eat_ready_food" in rows:
            h["eat_when_hunger"] = rows["eat_ready_food"]["preconditions"]["hunger_at_least"]
        if "rest" in rows:
            h["rest_when_energy"] = rows["rest"]["preconditions"]["energy_at_most"]
            h["rest_seconds"] = rows["rest"]["preconditions"]["seconds"]
        h["refs"] = [f"{r['habit_id']}@{r['habit_version']}" for r in rows.values()]
        h["version"] = "habits:" + ",".join(sorted(h["refs"])) if rows else "habits:none"
        h["enabled"] = {k: (k in rows and rows[k]["enabled"]) for k in ("sleep", "eat_ready_food", "rest")}
        h["allowed"] = {k: tuple(rows[k]["allowed_actions"]) if k in rows else () for k in h["enabled"]}
        return h

    def _point(self, actor_id, kind, reason, outcome, refs=()):
        with self.s.tx() as db:
            db.execute("INSERT INTO decision_points(actor_id,at,kind,reason,outcome,refs) VALUES(?,?,?,?,?,?)",
                       (actor_id, self.s.clock.now(), kind, reason, outcome, json.dumps(list(refs))))

    def _would_call(self, actor_id, kind, reason):
        """One decision point per unmet need, not one per check: repeated checks while nothing changed for
        her (no new action) are the same moment of needing a model."""
        with self.s.read() as db:
            last = db.execute("SELECT kind, reason, outcome, at FROM decision_points WHERE actor_id=?"
                              " ORDER BY point_id DESC LIMIT 1", (actor_id,)).fetchone()
            acted = last and db.execute("SELECT 1 FROM actions WHERE actor_id=? AND created_at>=? LIMIT 1",
                                        (actor_id, last["at"])).fetchone()
        if last and (last["kind"], last["reason"], last["outcome"]) == (kind, reason, "would_call") and not acted:
            return
        self._point(actor_id, kind, reason, "would_call")

    def _submit(self, actor_id, kind, action_type, args, habit, family):
        if action_type not in habit["allowed"].get(family, ()):
            self._point(actor_id, kind, f"{action_type}_not_allowed_by_habit", "skipped")
            return {"skipped": f"{action_type}_not_allowed_by_habit"}
        now = self.s.clock.now()
        # same need, same step, same minute = the same command (a re-run check must not eat twice)
        key = f"routine:{actor_id}:{kind}:{action_type}:{json.dumps(args, sort_keys=True)}:{int(now // 60)}"
        res = self.k.submit({"idempotency_key": key, "principal": "routine",
                             "actor_id": actor_id, "origin": "routine", "action_type": action_type, "args": args,
                             "context_refs": list(habit["refs"])})
        self._point(actor_id, kind, action_type, "routine_action" if res["status"] in ("running", "accepted")
                    else f"routine_failed:{res.get('reason')}", [res.get("command_id")])
        return {"action": action_type, "result": res}

    def check(self, actor_id):
        """One needs check. Returns {"action": ...} | {"would_call": reason} | {} (nothing needed)."""
        if not self.k.city_visits.home_allowed(actor_id):
            return {}
        self.k.settle_bodies([actor_id])
        a = self.k.actor(actor_id)
        if not a or not a["simulated"] or a["action_id"]:
            return {}
        h, now = self.habit(actor_id), self.s.clock.now()
        body, minute = a["body"], jst_minute(now)
        if h["enabled"]["sleep"] and in_window(minute, h["sleep_jst"]):
            bed = self.k.V.bed(actor_id) if hasattr(self.k.V, "bed") else "exit_rooms"  # P1b: her own bedroom
            if a["location"] != bed:
                hop = self.k.V.next_hop(a["location"], bed)
                if hop:
                    return self._submit(actor_id, "sleep_route", "move", {"to": hop}, h, "sleep")
            else:
                wake = h["sleep_jst"][1]
                seconds = ((wake - minute) % 1440) * 60 or 60
                return self._submit(actor_id, "sleep", "sleep", {"seconds": min(seconds, 36000)}, h, "sleep")
        if h["enabled"]["eat_ready_food"] and body["hunger"] >= h["eat_when_hunger"]:
            food = self._ready_food(actor_id, a["location"], h)
            if food:
                return self._submit(actor_id, "hunger", "eat", {"item": food[1], "resource_id": food[0]}, h,
                                    "eat_ready_food")
            self._would_call(actor_id, "hunger", "hungry_no_ready_food")
            return {"would_call": "hungry_no_ready_food"}
        if h["enabled"]["rest"] and body["energy"] <= h["rest_when_energy"]:
            return self._submit(actor_id, "tired", "rest", {"seconds": h["rest_seconds"]}, h, "rest")
        return {}

    def _ready_food(self, actor_id, location, h):
        """Food lying in her room or in her hands. Raw ingredients are not a meal (edible flag)."""
        with self.s.read() as db:
            for r in db.execute("SELECT resource_id, item FROM resources WHERE kind='food' AND qty>0"
                                " AND (holder=? OR (holder IS NULL AND location=?)) ORDER BY resource_id",
                                (actor_id, location)).fetchall():
                if r["item"] in edible(self.k.V) and (self.k._free_qty(db, r["resource_id"]) or 0) > 0:
                    return r["resource_id"], r["item"]
        return None
