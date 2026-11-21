"""Virtual call budget of P1a (v1.1 §16): count first, then call.

A reservation is written before any (fake) model call and counts until it is settled or released. A call
whose outcome is unknown (crash, timeout) keeps counting: cancelled is not the same as not billed. Limits
mirror the owner's 10-23 P1b numbers (60 total for the first sample, 20/hour, 100/day) so the replay
shows the same degradation the real trial would; P1a spends nothing.
"""

DEFAULT_LIMITS = {"total": 60, "per_hour": 20, "per_day": 100}
COUNTING = ("reserved", "settled", "unknown")


class Budget:
    def __init__(self, store, pool="world/fake", limits=None):
        self.s, self.pool = store, pool
        self.limits = dict(DEFAULT_LIMITS, **(limits or {}))

    def _used(self, db, since=None):
        q = "SELECT COALESCE(SUM(units),0) FROM budget_reservations WHERE pool=? AND state IN (?,?,?)"
        args = [self.pool, *COUNTING]
        if since is not None:
            q += " AND created_at>?"
            args.append(since)
        return db.execute(q, args).fetchone()[0]

    def used(self):
        """Units counted so far (reserved, settled or unknown)."""
        with self.s.read() as db:
            return self._used(db)

    def remaining(self):
        now = self.s.clock.now()
        with self.s.read() as db:
            return min(self.limits["total"] - self._used(db), self.limits["per_hour"] - self._used(db, now - 3600),
                       self.limits["per_day"] - self._used(db, now - 86400))

    def reserve(self, actor_id, purpose, units=1):
        """Reservation id, or None when any window is full (the caller degrades, it does not wait)."""
        now = self.s.clock.now()
        with self.s.tx() as db:
            left = min(self.limits["total"] - self._used(db), self.limits["per_hour"] - self._used(db, now - 3600),
                       self.limits["per_day"] - self._used(db, now - 86400))
            if left < units:
                return None
            rid = self.s.new_id("bud")
            db.execute("INSERT INTO budget_reservations(reservation_id,actor_id,purpose,pool,units,state,created_at)"
                       " VALUES(?,?,?,?,?,?,?)", (rid, actor_id, purpose, self.pool, units, "reserved", now))
            return rid

    def settle(self, rid, used_tokens=None):
        with self.s.tx() as db:
            db.execute("UPDATE budget_reservations SET state='settled', settled_at=?, used_tokens=? WHERE reservation_id=?"
                       " AND state IN ('reserved','unknown')", (self.s.clock.now(), used_tokens, rid))

    def unknown(self, rid, note):
        with self.s.tx() as db:
            db.execute("UPDATE budget_reservations SET state='unknown', note=? WHERE reservation_id=? AND state='reserved'",
                       (note, rid))

    def release(self, rid):
        """Only for a call that provably never started."""
        with self.s.tx() as db:
            db.execute("UPDATE budget_reservations SET state='released', settled_at=? WHERE reservation_id=?"
                       " AND state='reserved'", (self.s.clock.now(), rid))
