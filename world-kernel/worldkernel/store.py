"""SQLite store, clocks and the single-writer lock of the P1a world kernel (v1.1 §2/§12/§15).

The state tables are the authority; events are written in the same transaction as the facts, observers
and view rows they explain. There is no checkpoint table and no promise to rebuild state from events.
Times are UTC epoch seconds; JST is only for display.
"""
import json
import os
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS city_visits (
  visit_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, state TEXT NOT NULL,
  transfer_id TEXT NOT NULL UNIQUE, begin_hash TEXT NOT NULL, domain_epoch INTEGER NOT NULL, protocol TEXT NOT NULL,
  vocab_version TEXT NOT NULL, city_catalog_version TEXT, mappings_version INTEGER NOT NULL DEFAULT 1, plan TEXT NOT NULL,
  departure_action_id TEXT, settlement_id TEXT, receipt_ids TEXT NOT NULL DEFAULT '{}',
  created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS city_visit_open_actor ON city_visits(actor_id)
  WHERE state NOT IN ('closed','aborted');
CREATE TABLE IF NOT EXISTS actor_domains (
  actor_id TEXT PRIMARY KEY, domain TEXT NOT NULL CHECK(domain IN ('home','city')),
  domain_epoch INTEGER NOT NULL, visit_id TEXT, updated_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_visit_requests (
  request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS city_visit_receipts (
  receipt_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS city_visit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, visit_id TEXT NOT NULL, state TEXT NOT NULL,
  event_id INTEGER NOT NULL, recorded_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_visit_projections (
  visit_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
  last_record_id TEXT, last_kind TEXT, occurred_at_ms INTEGER,
  payload TEXT NOT NULL DEFAULT '{}', engine_unreachable INTEGER NOT NULL DEFAULT 0,
  unreachable_since_ms INTEGER, updated_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS home_action_epochs (
  action_id TEXT PRIMARY KEY, domain_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_body_effects (
  effect_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT, event_id INTEGER NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS city_visit_carry (
  visit_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, plan_id TEXT,
  carry_yen INTEGER NOT NULL, carry_items TEXT NOT NULL DEFAULT '[]', source_hold_ref TEXT NOT NULL,
  prepare_request_id TEXT NOT NULL, commit_request_id TEXT NOT NULL,
  abort_request_id TEXT NOT NULL, release_request_id TEXT NOT NULL,
  decision_seq INTEGER NOT NULL DEFAULT 0, decisions_used INTEGER NOT NULL DEFAULT 0,
  budget_exhausted INTEGER NOT NULL DEFAULT 0, release_confirmed INTEGER NOT NULL DEFAULT 0,
  locker_items TEXT NOT NULL DEFAULT '[]', settlement_json TEXT,
  created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_item_custody (
  custody_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, ref TEXT NOT NULL,
  resource_id TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, engine_type TEXT NOT NULL,
  qty INTEGER NOT NULL, reservation_id TEXT NOT NULL, state TEXT NOT NULL,
  settled_at_ms INTEGER, UNIQUE(visit_id, ref));
CREATE TABLE IF NOT EXISTS city_settlements (
  settlement_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL,
  carry_yen INTEGER NOT NULL, purse_final_yen INTEGER NOT NULL, net_delta INTEGER NOT NULL,
  final_state TEXT NOT NULL, payload TEXT NOT NULL, settled_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_outbox_seen (
  record_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, visit_id TEXT NOT NULL,
  kind TEXT NOT NULL, occurred_at_ms INTEGER NOT NULL, processed_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_encounters (
  record_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  other_name TEXT NOT NULL, place TEXT NOT NULL, place_name TEXT NOT NULL, area TEXT,
  occurred_at_ms INTEGER NOT NULL, event_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_visit_purchases (
  record_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  what TEXT NOT NULL, yen INTEGER NOT NULL, purse_yen INTEGER NOT NULL,
  payload TEXT NOT NULL, occurred_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS city_pending_refunds (
  record_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  order_id TEXT NOT NULL, yen INTEGER NOT NULL, event_id INTEGER NOT NULL,
  processed_at_ms INTEGER NOT NULL, UNIQUE(visit_id, order_id));
CREATE TABLE IF NOT EXISTS city_weather_cache (
  area TEXT PRIMARY KEY, observed_at_ms INTEGER NOT NULL, fetched_at_ms INTEGER NOT NULL,
  last_ok_at_ms INTEGER, code INTEGER NOT NULL, temp_c REAL NOT NULL, precip_mm REAL NOT NULL,
  rain INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL,
  hourly_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS city_decision_actions (
  request_id TEXT PRIMARY KEY, visit_id TEXT NOT NULL, decision_seq INTEGER NOT NULL,
  action_seq INTEGER NOT NULL, domain_epoch INTEGER NOT NULL, action_json TEXT NOT NULL,
  state TEXT NOT NULL, receipt_json TEXT, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS actors (
  actor_id TEXT PRIMARY KEY, simulated INTEGER NOT NULL, profile_ref TEXT, generation INTEGER NOT NULL DEFAULT 0,
  location TEXT, activity TEXT, action_id TEXT, body TEXT NOT NULL DEFAULT '{}', body_at REAL,
  availability TEXT NOT NULL DEFAULT 'free', version INTEGER NOT NULL DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS resources (
  resource_id TEXT PRIMARY KEY, kind TEXT NOT NULL, item TEXT NOT NULL, location TEXT, holder TEXT,
  qty INTEGER NOT NULL, capacity INTEGER, version INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservations (
  reservation_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL, action_id TEXT NOT NULL, qty INTEGER NOT NULL,
  consume INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS reservations_live ON reservations(resource_id, state);
CREATE TABLE IF NOT EXISTS commands (
  command_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, principal TEXT NOT NULL, actor_id TEXT,
  origin TEXT NOT NULL, request_digest TEXT NOT NULL, status TEXT NOT NULL, action_id TEXT, result TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, synthetic INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, actor_id TEXT NOT NULL, origin TEXT NOT NULL,
  goal_id TEXT, guide_id TEXT, action_type TEXT NOT NULL, args TEXT NOT NULL, state TEXT NOT NULL,
  generation INTEGER NOT NULL, interrupt_policy TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
  due_at REAL, finished_at REAL, reason TEXT, superseded_by TEXT);
CREATE INDEX IF NOT EXISTS actions_actor ON actions(actor_id, state);
CREATE TABLE IF NOT EXISTS effects (effect_key TEXT PRIMARY KEY, event_id INTEGER NOT NULL, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, actor_id TEXT, action_id TEXT,
  occurred_at REAL NOT NULL, recorded_at REAL NOT NULL, location TEXT, payload TEXT NOT NULL,
  knowers TEXT NOT NULL, disclosure_scope TEXT NOT NULL, source_ref TEXT, source_kind TEXT NOT NULL,
  trust TEXT NOT NULL, retention_class TEXT NOT NULL, synthetic INTEGER NOT NULL, runtime_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, observer_id TEXT NOT NULL,
  observed_at REAL NOT NULL, mode TEXT NOT NULL, location TEXT, UNIQUE(event_id, observer_id));
CREATE TABLE IF NOT EXISTS view_events (
  audience_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id INTEGER, payload TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (audience_id, seq));
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, due_at REAL NOT NULL, state TEXT NOT NULL,
  created_at REAL NOT NULL, done_at REAL, UNIQUE(kind, ref));
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, due_at);
CREATE TABLE IF NOT EXISTS owner_presence (
  session_id TEXT PRIMARY KEY, state TEXT NOT NULL, location TEXT, lease_until REAL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
  delivery_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL, channel TEXT NOT NULL, payload TEXT NOT NULL,
  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, external_ref TEXT, created_at REAL NOT NULL,
  updated_at REAL NOT NULL, runtime_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, status TEXT NOT NULL, external_ref TEXT,
  received_at REAL NOT NULL, runtime_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scene_offers (
  guide_id TEXT PRIMARY KEY, rule_key TEXT NOT NULL, rule_version TEXT NOT NULL, location TEXT NOT NULL,
  window_start REAL NOT NULL, window_end REAL NOT NULL, opportunity TEXT NOT NULL, constraints TEXT NOT NULL,
  invite_hint TEXT, source_refs TEXT NOT NULL, audience_scope TEXT NOT NULL, expires_at REAL NOT NULL,
  state TEXT NOT NULL, takers TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, UNIQUE(rule_key, window_start));
CREATE TABLE IF NOT EXISTS decision_points (
  point_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
  reason TEXT NOT NULL, outcome TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS budget_reservations (
  reservation_id TEXT PRIMARY KEY, actor_id TEXT, purpose TEXT NOT NULL, pool TEXT NOT NULL, units INTEGER NOT NULL,
  state TEXT NOT NULL, created_at REAL NOT NULL, settled_at REAL, used_tokens INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS budget_window ON budget_reservations(pool, created_at);
CREATE TABLE IF NOT EXISTS materials (
  material_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, source_ref TEXT NOT NULL, source_kind TEXT NOT NULL,
  observed_at REAL, knowers TEXT NOT NULL, disclosure_scope TEXT NOT NULL, body TEXT, state TEXT NOT NULL,
  trust TEXT NOT NULL, retention_class TEXT NOT NULL, synthetic INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS summaries (
  summary_id TEXT PRIMARY KEY, scene TEXT NOT NULL, audience TEXT NOT NULL, source_from INTEGER NOT NULL,
  source_to INTEGER NOT NULL, summary_version TEXT NOT NULL, source_event_ids TEXT NOT NULL, text TEXT NOT NULL,
  triggers TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(scene, audience, source_from, source_to, summary_version));
CREATE TABLE IF NOT EXISTS habits (
  habit_id TEXT NOT NULL, habit_version INTEGER NOT NULL, actor_id TEXT NOT NULL, kind TEXT NOT NULL,
  profile_ref TEXT NOT NULL, profile_version TEXT NOT NULL, derivation_rule_version TEXT NOT NULL,
  allowed_actions TEXT NOT NULL, time_window TEXT, preconditions TEXT NOT NULL, interruption_policy TEXT NOT NULL,
  enabled INTEGER NOT NULL, override_source TEXT, created_at REAL NOT NULL, superseded_at REAL,
  PRIMARY KEY (habit_id, habit_version));
CREATE TABLE IF NOT EXISTS audits (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, detail TEXT NOT NULL, at REAL NOT NULL,
  runtime_epoch INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS receipts_once ON receipts(delivery_id, status, COALESCE(external_ref,''));
"""


class VirtualClock:
    """Test/replay clock: time only moves when told to. Fake model latency also moves it (v1.1 §17)."""

    def __init__(self, start):
        self.t = float(start)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)
        return self.t


class WallClock:
    def now(self):
        return time.time()


class LockHeld(RuntimeError):
    """Another live scheduler holds this world (v1.1 §12: no second scheduler on the same DB)."""


def dumps(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ReadOnlyTx(LockHeld):
    """A write was attempted inside read(). A read transaction never holds the writer fence and is never
    upgraded: start the write in its own tx() (which may itself wrap reads) instead."""


class Store:
    LOCK_LEASE = 60.0

    def __init__(self, path, clock, world_id="ai-house/p1a", synthetic=True, holder=None, stream_epoch="1", seed=None):
        """seed: make generated ids reproducible (a recorded replay must come out byte-identical)."""
        self.path, self.clock, self.world_id, self.synthetic = path, clock, world_id, synthetic
        self._rng = random.Random(seed) if seed is not None else None
        self.faults = {}  # test-only injection points: name -> callable that raises
        self.holder = holder or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        # Old K1 visits retain their original mapping generation. Never relabel
        # an in-flight v1 transfer as v2 when opening an existing database.
        if "mappings_version" not in {r[1] for r in self.db.execute("PRAGMA table_info(city_visits)")}:
            self.db.execute("ALTER TABLE city_visits ADD COLUMN mappings_version INTEGER NOT NULL DEFAULT 1")
        self._in_tx, self._tx_name = None, None  # None, "read" or "write"
        with self.tx(control=True):
            self._meta_default("schema_version", str(SCHEMA_VERSION))
            self._meta_default("world_id", world_id)
            self._meta_default("synthetic", "1" if synthetic else "0")
            self._meta_default("stream_epoch", str(stream_epoch))
            self._meta_default("runtime_epoch", "0")
            stored = self.meta("synthetic")
            if stored != ("1" if synthetic else "0") or self.meta("world_id") != world_id:
                raise RuntimeError("world_id/synthetic flag does not match this database (v1.1 R6 isolation)")
        self.epoch = None

    def new_id(self, prefix, n=12):
        bits = self._rng.getrandbits(4 * n) if self._rng else int(uuid.uuid4().hex[:n], 16)
        return f"{prefix}-{bits:0{n}x}"

    # --- transactions -------------------------------------------------------------------------------
    @contextmanager
    def tx(self, control=False):
        """One short write transaction. Nested use joins the outer one. Never call a model inside.

        Every write transaction is fenced (v1.1 §2 single writer): inside the same BEGIN IMMEDIATE that holds
        SQLite's write lock it checks that this store still is the scheduler (lock_holder and runtime_epoch
        unchanged since acquire) and extends the lease, so a stale scheduler that lost the world to a successor
        cannot commit anything, and no takeover can slip in between check and write. A lease that lapsed
        while nobody took over is still ours: a successor always moves runtime_epoch. control=True is for
        the store's own setup and lock bookkeeping only."""
        if self._in_tx == "write":
            yield self.db
            return
        if self._in_tx == "read":
            raise ReadOnlyTx("write inside read(): a read transaction is not fenced and cannot be upgraded")
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = "write"
        try:
            if not control:
                self._fence()
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            try:
                self.fault("before_commit")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            self.fault("after_commit")
        finally:
            self._in_tx = None
            self._tx_name = None

    def fault(self, point):
        """Test hook: raise at a named point of the transaction named by name_tx() (v1.1 §12 failure cases)."""
        f = self.faults.get(f"{getattr(self, '_tx_name', None)}:{point}")
        if f:
            f()

    def name_tx(self, name):
        """Label the transaction being written (command_acceptance, action_completion, ...) for fault points."""
        if self._in_tx == "write" and not getattr(self, "_tx_name", None):
            self._tx_name = name

    @contextmanager
    def read(self):
        """Explicit read transaction: snapshot and cursor come from the same boundary (v1.1 §10).

        Read-only for real: tx() inside it raises ReadOnlyTx, and SQLite's query_only refuses any raw write
        that skips tx(), so nothing written under a read boundary can commit unfenced. It always rolls back.
        Inside a write transaction it joins that one (already fenced)."""
        if self._in_tx:
            yield self.db
            return
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("BEGIN")
        self._in_tx = "read"
        try:
            yield self.db
        finally:
            self.db.execute("ROLLBACK")
            self.db.execute("PRAGMA query_only=OFF")
            self._in_tx = None

    # --- meta / lock --------------------------------------------------------------------------------
    def meta(self, key, default=None):
        r = self.db.execute("SELECT value FROM runtime_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO runtime_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def _meta_default(self, key, value):
        self.db.execute("INSERT OR IGNORE INTO runtime_meta(key,value) VALUES(?,?)", (key, value))

    def acquire(self):
        """Take the scheduler lock and start a new runtime_epoch. Raises LockHeld if a live holder exists."""
        now = self.clock.now()
        with self.tx(control=True):
            holder, until = self.meta("lock_holder"), float(self.meta("lock_until", "0"))
            if holder and holder != self.holder and until > now:
                raise LockHeld(f"world locked by {holder} until {until}")
            self.epoch = int(self.meta("runtime_epoch", "0")) + 1
            self.set_meta("runtime_epoch", self.epoch)
            self.set_meta("lock_holder", self.holder)
            self.set_meta("lock_until", now + self.LOCK_LEASE)
        return self.epoch

    def renew(self):
        with self.tx(control=True):
            if self.meta("lock_holder") != self.holder or int(self.meta("runtime_epoch")) != self.epoch:
                raise LockHeld("lock lost")
            self.set_meta("lock_until", self.clock.now() + self.LOCK_LEASE)

    def release(self):
        with self.tx(control=True):
            if self.meta("lock_holder") == self.holder and int(self.meta("runtime_epoch")) == self.epoch:
                self.set_meta("lock_until", 0)
        self.epoch = None

    def _fence(self):
        if self.epoch is None:
            raise LockHeld("acquire() the world before writing")
        if self.meta("lock_holder") != self.holder or int(self.meta("runtime_epoch", "0")) != self.epoch:
            self.epoch = None  # lost for good: this object never writes again, a fresh acquire() is needed
            raise LockHeld("lock lost to another scheduler")
        self.set_meta("lock_until", self.clock.now() + self.LOCK_LEASE)

    def require_lock(self):
        """Cheap early refusal; the authoritative check is _fence() inside every write transaction."""
        if self.epoch is None:
            raise LockHeld("acquire() the world before writing")

    def close(self):
        self.db.close()
