"""L0 habits derived from the persona, versioned, visible and overridable (v1.1 §3.2 + v1.1.1 F3).

A habit is a bounded everyday tendency (go to bed in her window, eat ready food when hungry, rest when worn
out), never a script for her day. derive() turns a controlled persona profile into habit records by one fixed
rule; the model never rewrites them on a wake. Each record keeps where it came from (profile_ref and
profile_version, never the persona text), the derivation rule version and its own version. A new profile
version is validated first and then replaces the old records; an owner override is its own version and a
later persona update never silently overwrites it. What L0 may do with a habit is only its allowed_actions,
through the kernel, as origin=routine.
"""
import json

from .store import dumps

DERIVATION_RULE_VERSION = "habits-derive/p1a-1"
KINDS = ("sleep", "eat_ready_food", "rest")
ALLOWED = {"sleep": ("move", "sleep"), "eat_ready_food": ("eat",), "rest": ("rest",)}
NEVER = ("say", "cook", "accept_invite", "promise", "table_move")  # L0 never speaks, commits or cooks for her
DEFAULTS = {"sleep": "23:00-07:00", "hunger_eat_at": 75.0, "rest_at_energy": 20.0, "rest_seconds": 1200}


class HabitError(ValueError):
    pass


def _window(text):
    try:
        lo, hi = text.split("-")
        mins = []
        for part in (lo, hi):
            h, m = part.split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            mins.append(h * 60 + m)
    except (AttributeError, ValueError):
        raise HabitError(f"bad time window: {text!r}")
    if mins[0] == mins[1]:
        raise HabitError("empty time window")
    return mins


def _pct(v, name):
    v = float(v)
    if not 0 <= v <= 100:
        raise HabitError(f"{name} out of 0..100")
    return v


def derive(profile, vocab):
    """Habit records for one housemate from her persona profile (synthetic dict in P1a). Deterministic."""
    for k in ("actor_id", "profile_ref", "profile_version"):
        if not profile.get(k):
            raise HabitError(f"profile needs {k}")
    p = dict(DEFAULTS, **{k: v for k, v in profile.items() if v is not None})
    rest = int(p["rest_seconds"])
    if not 0 < rest <= vocab.ACTIONS["rest"]["duration_rule"].get("max", rest):
        raise HabitError("rest_seconds outside the catalog")
    common = {"actor_id": profile["actor_id"], "profile_ref": profile["profile_ref"],
              "profile_version": str(profile["profile_version"]), "derivation_rule_version": DERIVATION_RULE_VERSION}
    out = [dict(common, kind="sleep", time_window=_window(p["sleep"]), preconditions={}),
           dict(common, kind="eat_ready_food", time_window=None,
                preconditions={"hunger_at_least": _pct(p["hunger_eat_at"], "hunger_eat_at"), "food": "ready_edible"}),
           dict(common, kind="rest", time_window=None,
                preconditions={"energy_at_most": _pct(p["rest_at_energy"], "rest_at_energy"), "seconds": rest})]
    for h in out:
        h["habit_id"] = f"{h['actor_id']}:{h['kind']}"
        h["allowed_actions"] = list(ALLOWED[h["kind"]])
        h["interruption_policy"] = "her_choice_replaces"  # her own decision supersedes a routine action
        h["enabled"] = True
        check(h, vocab)
    return out


def check(h, vocab):
    if h["kind"] not in KINDS:
        raise HabitError("unknown habit kind")
    acts = set(h["allowed_actions"])
    if not acts <= set(ALLOWED[h["kind"]]) or acts & set(NEVER) or not acts <= set(vocab.ACTIONS):
        raise HabitError("habit may only use its plain catalog actions")
    if h["kind"] == "sleep" and not h.get("time_window"):
        raise HabitError("sleep needs a window")


class HabitBook:
    def __init__(self, kernel):
        self.k, self.s = kernel, kernel.s

    def _active(self, db, habit_id):
        return db.execute("SELECT * FROM habits WHERE habit_id=? AND superseded_at IS NULL", (habit_id,)).fetchone()

    def _write(self, db, h, version, override, now):
        db.execute("INSERT INTO habits(habit_id,habit_version,actor_id,kind,profile_ref,profile_version,"
                   "derivation_rule_version,allowed_actions,time_window,preconditions,interruption_policy,enabled,"
                   "override_source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (h["habit_id"], version, h["actor_id"], h["kind"], h["profile_ref"], h["profile_version"],
                    h["derivation_rule_version"], dumps(h["allowed_actions"]),
                    dumps(h["time_window"]) if h["time_window"] else None, dumps(h["preconditions"]),
                    h["interruption_policy"], 1 if h["enabled"] else 0, override, now))

    def install(self, profile):
        """Validate all of a new profile's habits first, then replace. Owner overrides stay (audited)."""
        new = derive(profile, self.k.V)  # raises before anything is written
        now, out = self.s.clock.now(), {}
        with self.s.tx() as db:
            for h in new:
                cur = self._active(db, h["habit_id"])
                if cur and cur["override_source"]:
                    self.k.audit("habit_override_kept", habit=h["habit_id"], version=cur["habit_version"],
                                 profile_version=h["profile_version"])
                    out[h["kind"]] = ("kept_override", cur["habit_version"])
                    continue
                if cur and self._same(cur, h):
                    out[h["kind"]] = ("unchanged", cur["habit_version"])
                    continue
                v = (cur["habit_version"] + 1) if cur else 1
                if cur:
                    db.execute("UPDATE habits SET superseded_at=? WHERE habit_id=? AND habit_version=?",
                               (now, h["habit_id"], cur["habit_version"]))
                self._write(db, h, v, None, now)
                out[h["kind"]] = ("installed", v)
        return out

    @staticmethod
    def _same(row, h):
        return (row["profile_ref"] == h["profile_ref"] and row["profile_version"] == h["profile_version"]
                and row["derivation_rule_version"] == h["derivation_rule_version"]
                and json.loads(row["time_window"] or "null") == h["time_window"]
                and json.loads(row["preconditions"]) == h["preconditions"] and bool(row["enabled"]) == h["enabled"])

    def override(self, principal, actor_id, kind, reason, time_window=None, preconditions=None, enabled=None):
        """The owner's change in the console: a new traceable version, same rules as derived habits."""
        if principal != "cmd:owner":
            return {"status": "failed", "reason": "owner_only"}
        now = self.s.clock.now()
        with self.s.tx() as db:
            cur = self._active(db, f"{actor_id}:{kind}")
            if not cur:
                return {"status": "failed", "reason": "no_such_habit"}
            h = self._row(cur)
            if time_window is not None:
                h["time_window"] = _window(time_window)
            if preconditions:
                bad = set(preconditions) - set(h["preconditions"])
                if bad:
                    return {"status": "failed", "reason": f"unknown_precondition:{sorted(bad)[0]}"}
                for k, v in preconditions.items():
                    h["preconditions"][k] = _pct(v, k) if k.endswith(("_least", "_most")) else v
            if enabled is not None:
                h["enabled"] = bool(enabled)
            check(h, self.k.V)
            db.execute("UPDATE habits SET superseded_at=? WHERE habit_id=? AND habit_version=?",
                       (now, cur["habit_id"], cur["habit_version"]))
            self._write(db, h, cur["habit_version"] + 1, f"owner:{reason}", now)
            self.k.audit("habit_override", habit=cur["habit_id"], version=cur["habit_version"] + 1)
            return {"status": "succeeded", "habit_version": cur["habit_version"] + 1}

    def clear_override(self, principal, profile):
        """Owner hands the habit back to the persona: re-derive from this profile and replace the override."""
        if principal != "cmd:owner":
            return {"status": "failed", "reason": "owner_only"}
        new = derive(profile, self.k.V)
        now = self.s.clock.now()
        with self.s.tx() as db:
            for h in new:
                cur = self._active(db, h["habit_id"])
                if cur and cur["override_source"]:
                    db.execute("UPDATE habits SET superseded_at=? WHERE habit_id=? AND habit_version=?",
                               (now, h["habit_id"], cur["habit_version"]))
                    self._write(db, h, cur["habit_version"] + 1, None, now)
        return {"status": "succeeded"}

    @staticmethod
    def _row(r):
        return {"habit_id": r["habit_id"], "actor_id": r["actor_id"], "kind": r["kind"], "profile_ref": r["profile_ref"],
                "profile_version": r["profile_version"], "derivation_rule_version": r["derivation_rule_version"],
                "habit_version": r["habit_version"], "allowed_actions": json.loads(r["allowed_actions"]),
                "time_window": json.loads(r["time_window"]) if r["time_window"] else None,
                "preconditions": json.loads(r["preconditions"]), "interruption_policy": r["interruption_policy"],
                "enabled": bool(r["enabled"]), "override_source": r["override_source"]}

    def listing(self, actor_id=None):
        """What the console shows: content, source versions, enabled, override. No persona text."""
        with self.s.read() as db:
            q = "SELECT * FROM habits WHERE superseded_at IS NULL"
            rows = db.execute(q + (" AND actor_id=?" if actor_id else "") + " ORDER BY habit_id",
                              (actor_id,) if actor_id else ()).fetchall()
        return [self._row(r) for r in rows]

    def history(self, habit_id):
        with self.s.read() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM habits WHERE habit_id=? ORDER BY habit_version",
                                                     (habit_id,))]
