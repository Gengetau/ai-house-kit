"""pure life1 mapping plus transactional body-effect application for K4.

No transport/outbox polling. Only the kernel changes the six residents' bodies.
K4 must authenticate the bridge, process records in sequence and tick due jobs
before calling apply. quantity counts servings, or 15-minute blocks for walking.
"""
import hashlib
import json

from .city_visits import MAPPINGS_VERSION, SUPPORTED_MAPPINGS_VERSIONS
from .kernel import Refused
from .store import dumps


def body_delta(vocab, name, quantity=1):
    """Existing life1 rules only; base drift stays with Kernel._integrate."""
    if type(quantity) is not int or quantity <= 0:
        raise Refused("BAD_CITY_EFFECT_QUANTITY")
    if not vocab.VOCAB_VERSION.startswith("life1-"):
        raise Refused("CITY_EFFECT_VOCAB_MISMATCH")
    if name in ("snack", "drink_coffee", "drink_tea"):
        # coffee/bottled_tea are edible food items: the existing eat action
        # has a shared once effect, with no item-specific stimulant rule.
        rule = vocab.ACTIONS["eat"]["effects"].get("once", {})
        scale = quantity
    elif name == "walk_15m":
        rule = vocab.ACTIVITIES["stroll"]["effects"].get("rate", {})
        scale = 15 * quantity
    else:
        raise Refused("UNKNOWN_CITY_EFFECT")
    if set(rule) - set(vocab.BODY_KEYS):
        raise Refused("CITY_EFFECT_BODY_FIELD_MISMATCH")
    return {key: value * scale for key, value in rule.items()}


class CityBodyEffects:
    def __init__(self, kernel):
        self.k, self.s = kernel, kernel.s

    def apply(self, visit_id, effect_id, name, occurred_at_ms, *, quantity=1):
        """Apply once or durably reject; effect_id is the unmodified effects key.

        Same ID/different payload raises rather than silently applying it twice.
        Known refusals are retained in city_body_effects and a private event.
        Transient pending-job or transaction faults leave no consumed ID.
        """
        if any(not isinstance(x, str) or not x or len(x) > 512
               for x in (visit_id, effect_id, name)):
            raise Refused("BAD_CITY_EFFECT_ID")
        if type(occurred_at_ms) is not int or not 0 <= occurred_at_ms <= int(self.s.clock.now() * 1000):
            raise Refused("BAD_CITY_EFFECT_TIME")
        # Reject non-JSON quantities before hashing/auditing arbitrary inputs.
        if type(quantity) is not int or quantity <= 0:
            raise Refused("BAD_CITY_EFFECT_QUANTITY")
        payload = dict(visit_id=visit_id, effect_id=effect_id, name=name,
                       occurred_at_ms=occurred_at_ms, quantity=quantity)
        digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
        with self.k.city_visits._tx("city_body_effect") as db:
            old = db.execute("SELECT * FROM city_body_effects WHERE effect_id=?", (effect_id,)).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise Refused("CITY_EFFECT_CONFLICT")
                return json.loads(old["result"])
            visit = self.k.city_visits.get(visit_id)
            if visit is None:
                raise Refused("UNKNOWN_CITY_VISIT")
            actor_id = visit["actor_id"]  # Never trust an actor supplied by the bridge payload.
            reason, delta = None, {}
            auth = self.k.city_visits.authority(actor_id)
            if (visit["state"] not in ("active", "returning", "settling", "settled")
                    or auth["domain"] != "city" or auth["visit_id"] != visit_id):
                reason = "CITY_BODY_EFFECT_NOT_ACTIVE"
            elif visit["mappings_version"] not in SUPPORTED_MAPPINGS_VERSIONS:
                reason = "VERSION_MISMATCH"
            elif visit["vocab_version"] != self.k.V.VOCAB_VERSION:
                reason = "CITY_EFFECT_VOCAB_MISMATCH"
            elif occurred_at_ms < visit["created_at_ms"]:
                reason = "BAD_CITY_EFFECT_TIME"
            elif db.execute("SELECT 1 FROM effects WHERE effect_key=?", (effect_id,)).fetchone():
                reason = "CITY_EFFECT_KEY_CONFLICT"
            else:
                try:
                    delta = body_delta(self.k.V, name, quantity)
                except Refused as exc:
                    reason = exc.reason
            if not reason and db.execute("SELECT 1 FROM jobs WHERE state='pending' AND due_at<=? LIMIT 1",
                                         (self.s.clock.now(),)).fetchone():
                raise Refused("CITY_BODY_JOBS_DUE")
            event_id = self.k._emit(
                "city_body_effect_rejected" if reason else "city_body_effect_applied", actor_id, None,
                self.k.actor(actor_id)["location"], dict(payload, reason=reason, delta={} if reason else delta),
                scope="self:" + actor_id, occurred=occurred_at_ms / 1000, observers=(),
                source_ref=effect_id, source_kind="city_bridge")
            if not reason:
                actor = self.k._actor(db, actor_id)
                self.k._integrate(db, actor, self.s.clock.now())
                body = json.loads(actor["body"])
                lo, hi = self.k.V.BODY_RANGE
                for key, change in delta.items():
                    body[key] = min(hi, max(lo, body.get(key, 50.0) + change))
                if not self.k._effect(effect_id, event_id):
                    raise Refused("CITY_EFFECT_KEY_CONFLICT")
                self.k._set_actor(db, actor_id, body=dumps(body))
            result = dict(status="rejected" if reason else "applied", reason=reason,
                          effect_id=effect_id, event_id=event_id)
            db.execute("INSERT INTO city_body_effects VALUES(?,?,?,?,?,?,?)",
                       (effect_id, visit_id, digest, result["status"], reason, event_id, dumps(result)))
            return result
