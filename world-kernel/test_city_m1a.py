"""Offline synthetic unit & fault tests for (life1 Toto / 白帆市 City bridge adapter).

Covers S1 and F1-F14 + shared budget pool graceful degradation + 6-roommate rollout
using Store(synthetic=True) and an in-memory spec-compliant mock bridge transport
(zero network, zero paid calls, runs in ai-house-cc:20261007.2 --network none).
"""
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import tempfile
import unittest

import jsonschema

from worldkernel import admin, city_effects, city_projection, outings
from worldkernel.city_bridge import (
    ALL_HOUSE_ACTORS,
    DEFAULT_CITY_PLACE_TARGETS,
    CityBridgeAdapter,
    DeterministicCityModel,
    LiveCityDecisionModel,
    normalize_city_actions,
)
from worldkernel.city_client import (
    BRIDGE_PROTOCOL,
    MAPPINGS_VERSION,
    SUPPORTED_MAPPINGS_VERSIONS,
    _AREA_ORDER,
    _ERROR_CLASSES,
    CityBridgeError,
    CityBridgeHttpClient,
    CityTransportError,
    bridge_payload_hash,
    city_fallback_weather,
)
from worldkernel import city_visits
from worldkernel.kernel import Refused
from worldkernel.legacy import Projector, offline_world
from worldkernel.life import JST, ROSTER, Life
from worldkernel.statuscard import cards
from worldkernel.store import Store, VirtualClock

T0 = int(dt.datetime(2026, 11, 18, 13, 0, tzinfo=JST).timestamp())


class MockCityBridgeEngine:
    """In-memory spec-faithful /city/bridge/v1 engine for fast deterministic fault injection."""

    def __init__(self, clock):
        self.clock = clock
        self.bridge_key = "test-bridge-secret"
        self.protocol = BRIDGE_PROTOCOL
        self.mappings_version = 3
        self.catalog_version = "toto-p3.0"
        self.receipts = {}
        self.visits = {}
        self.aborted_unknown = set()
        self.last_epoch = {a: 0 for a in ROSTER}
        self.outbox = []
        self.seq = 0
        self.bridge_balance = 0
        self.purses = {a: {"balance": 0, "held": 0} for a in ROSTER}
        self.rain = {"sakuraoka": False, "rurigai": False, "kazeminato": False}
        self.weather_offline = False
        self.catalog_offline = False
        self.weather_last_ok_ms = int(clock.now() * 1000)
        self.bridge_503_count = 0
        self.bridge_429_count = 0
        self.outbox_503_count = 0
        self.drop_next_commit_response = False
        self.drop_next_prepare_response = False
        self.drop_next_release_response = False
        self.purchase_unknown_sku = None
        self.reconcile_ok = True
        self.request_log = []
        self.external_act_counts = {}
        self.house_qq_ids = {f"qq_{a}" for a in ROSTER}

        self.places = [
            {
                "id": "sakuraoka:station",
                "name": "樱丘站",
                "area": "sakuraoka",
                "type": "station",
                "hours": None,
                "rain": "none",
                "homeward": {"fare_yen": 0, "minutes": 12},
                "goods": [],
            },
            {
                "id": "sakuraoka:hoshikawa-riverbank",
                "name": "星川河岸",
                "area": "sakuraoka",
                "type": "riverside",
                "hours": None,
                "rain": "closed",
                "homeward": {"fare_yen": 0, "minutes": 9},
                "goods": [],
            },
            {
                "id": "rurigai:station",
                "name": "琉璃街站",
                "area": "rurigai",
                "type": "station",
                "hours": None,
                "rain": "none",
                "homeward": {"fare_yen": 200, "minutes": 25},
                "goods": [],
            },
            {
                "id": "rurigai:kissa-tomoshibi",
                "name": "喫茶「帆」",
                "area": "rurigai",
                "type": "cafe",
                "hours": {"open": "08:00", "close": "21:00"},
                "rain": "none",
                "homeward": {"fare_yen": 200, "minutes": 31},
                "goods": [
                    {"sku": "coffee", "type": "coffee", "name": "混合咖啡", "price_yen": 450},
                    {"sku": "milk_tea", "type": "milk_tea", "name": "皇家奶茶", "price_yen": 500},
                    {"sku": "cheesecake", "type": "cheesecake", "name": "烤芝士蛋糕", "price_yen": 520},
                    {"sku": "coffee_beans", "type": "coffee_beans", "name": "咖啡豆", "price_yen": 900},
                ],
            },
            {
                "id": "rurigai:shiorido",
                "name": "书店「书签堂」",
                "area": "rurigai",
                "type": "shop",
                "hours": {"open": "10:00", "close": "21:00"},
                "rain": "none",
                "homeward": {"fare_yen": 200, "minutes": 30},
                "goods": [
                    {"sku": "paperback", "type": "paperback", "name": "文库本", "price_yen": 800},
                    {"sku": "postcard", "type": "postcard", "name": "明信片", "price_yen": 150},
                ],
            },
            {
                "id": "kazeminato:muginohi",
                "name": "面包店「麦穗」",
                "area": "kazeminato",
                "type": "shop",
                "hours": {"open": "08:00", "close": "20:00"},
                "rain": "none",
                "homeward": {"fare_yen": 260, "minutes": 36},
                "goods": [
                    {"sku": "melon_pan", "type": "melon_pan", "name": "菠萝包", "price_yen": 230},
                ],
            },
        ]

    def now_ms(self):
        return int(round(self.clock.now() * 1000))

    def push_outbox(self, visit_id, kind, payload, record_id=None, occurred_at_ms=None):
        self.seq += 1
        v = self.visits[visit_id]
        v["record_seq"] += 1
        rid = record_id or f"{visit_id}:e{v['record_seq']}"
        rec = {
            "seq": self.seq,
            "visit_id": visit_id,
            "kind": kind,
            "record_id": rid,
            "occurred_at_ms": self.now_ms() if occurred_at_ms is None else int(occurred_at_ms),
            "payload": dict({"actor_id": v["actor_id"]}, **payload),
        }
        self.outbox.append(rec)
        return rec

    def _with_receipt(self, op, visit_id, body, fn):
        req_id = body.get("request_id")
        ph = body.get("payload_hash")
        if req_id in self.receipts:
            saved = self.receipts[req_id]
            if saved["payload_hash"] != ph or saved["op"] != op or saved["visit_id"] != visit_id:
                return 409, {"error": "CONFLICT", "message": "conflict", "reason": "request_conflict"}
            rep = dict(saved["receipt"], replayed=True)
            return 200, rep
        status, res = fn()
        if status != 200:
            return status, res
        rcpt = {
            "receipt_id": f"rcpt_{len(self.receipts) + 1}",
            "request_id": req_id,
            "visit_id": visit_id,
            "state": res["state"],
            "accepted_at_ms": self.now_ms(),
            "payload": res["payload"],
        }
        self.receipts[req_id] = {"op": op, "visit_id": visit_id, "payload_hash": ph, "receipt": rcpt}
        return 200, rcpt

    def transport(self, method, path, query, body, headers):
        self.request_log.append((method, path, query, body))
        if path == "/mcp/enter_world":
            caller = (body or {}).get("caller_qq_id", "")
            if caller in self.house_qq_ids:
                return 403, {}, json.dumps({
                    "error": "ENTRY_VIA_HOME_ONLY",
                    "message": "House residents can only enter Toto via home bridge",
                })
            return 200, {}, json.dumps({"ok": True})
        if path == "/mcp/external_act":
            bot_id = (body or {}).get("bot_id", "ext_b")
            self.external_act_counts[bot_id] = self.external_act_counts.get(bot_id, 0) + 1
            if self.external_act_counts[bot_id] > 60:
                return 429, {"Retry-After": "1"}, json.dumps({
                    "error": "RATE_LIMITED",
                    "message": "hourly act limit reached",
                    "retryAfterMs": 1000,
                })
            return 200, {}, json.dumps({"ok": True, "count": self.external_act_counts[bot_id]})
        if headers.get("Authorization") != f"Bearer {self.bridge_key}":
            return 401, {}, json.dumps({"error": "UNAUTHENTICATED", "message": "bad key"})
        if method == "POST" and self.bridge_429_count > 0:
            self.bridge_429_count -= 1
            return 429, {"Retry-After": "0.05"}, json.dumps({
                "error": "RATE_LIMITED",
                "message": "transient rate limit",
                "retryAfterMs": 50,
            })
        if method == "POST" and self.bridge_503_count > 0:
            self.bridge_503_count -= 1
            return 503, {}, json.dumps({"error": "RECOVERING", "message": "recovering"})

        if method == "GET" and path == "/catalog":
            if self.catalog_offline:
                raise CityTransportError("catalog endpoint timeout")
            return 200, {}, json.dumps({
                "protocol": self.protocol,
                "city_catalog_version": self.catalog_version,
                "city_text_version": "toto-text-1.1",
                "fare_rule_version": "sim-2026-11",
                "mappings": {"version": self.mappings_version, "items": [], "effects": []},
                "handover_node": "sakuraoka:ai-house:entrance",
                "item_types": [],
                "places": self.places,
                "transit": [],
            })

        if method == "GET" and path == "/weather":
            if self.weather_offline:
                raise CityTransportError("weather endpoint offline")
            now = self.now_ms()
            self.weather_last_ok_ms = now
            areas = []
            for area_id, is_rain in self.rain.items():
                areas.append({
                    "area": area_id,
                    "observed_at_ms": now,
                    "fetched_at_ms": now,
                    "code": 61 if is_rain else 1,
                    "temp_c": 13.0 if is_rain else 18.5,
                    "precip_mm": 2.5 if is_rain else 0.0,
                    "rain": is_rain,
                    "source": "open-meteo",
                    "status": "ok",
                    "text": "下雨" if is_rain else "大致晴朗",
                    "hourly": [],
                })
            return 200, {}, json.dumps({"at_ms": now, "attribution": "Open-Meteo", "areas": areas})

        if method == "GET" and path == "/reconcile":
            return 200, {}, json.dumps({
                "reports": [{"report_id": 1, "ran_at_ms": self.now_ms(), "trigger": "test", "ok": self.reconcile_ok, "findings": []}]
            })

        if method == "GET" and path == "/outbox":
            if self.outbox_503_count > 0:
                self.outbox_503_count -= 1
                return 503, {}, json.dumps({"error": "RECOVERING", "message": "outbox recovering"})
            after = int(query.get("after", 0))
            limit = int(query.get("limit", 50))
            rows = [r for r in self.outbox if r["seq"] > after]
            page = rows[:limit]
            more = len(rows) > limit
            next_after = page[-1]["seq"] if page else after
            return 200, {}, json.dumps({"records": page, "next_after": next_after, "more": more})

        if method == "POST" and path == "/visits/prepare":
            vid = body["visit_id"]
            def _do_prepare():
                if vid in self.aborted_unknown:
                    return 409, {"error": "CONFLICT", "message": "aborted", "reason": "visit_aborted"}
                if body.get("mappings_version") != self.mappings_version:
                    return 422, {"error": "VERSION_MISMATCH", "message": "mappings", "reason": "mappings_version"}
                dest = body["plan"]["destination"]
                if not any(p["id"] == dest for p in self.places):
                    return 422, {"error": "PRECONDITION_FAILED", "message": "no place", "reason": "no_such_place"}
                items = []
                for it in body["carry"].get("items", []):
                    k_item = it["kernel"]["item"]
                    eng_type = {"cookie": "cookie", "onigiri": "onigiri", "bottled_tea": "green_tea"}.get(k_item)
                    if not eng_type:
                        return 422, {"error": "PRECONDITION_FAILED", "message": "unmapped", "reason": "item_not_mapped"}
                    items.append({"ref": it["ref"], "engine_type": eng_type, "qty": it["qty"], "kernel": it["kernel"]})
                self.visits[vid] = {
                    "visit_id": vid,
                    "actor_id": body["actor_id"],
                    "state": "prepared",
                    "protocol": body["protocol"],
                    "vocab_version": body.get("vocab_version"),
                    "city_catalog_version": self.catalog_version,
                    "mappings_version": self.mappings_version,
                    "carry_yen": int(body["carry"]["yen"]),
                    "carry_items": items,
                    "held_items": [],
                    "item_moves": [],
                    "orders": [],
                    "plan": body["plan"],
                    "transfer_id": None,
                    "domain_epoch": None,
                    "place": None,
                    "settlement": None,
                    "record_seq": 0,
                }
                return 200, {
                    "state": "prepared",
                    "payload": {
                        "city_catalog_version": self.catalog_version,
                        "mappings_version": self.mappings_version,
                        "carry_yen": int(body["carry"]["yen"]),
                        "items": [{"ref": x["ref"], "engine_type": x["engine_type"], "qty": x["qty"]} for x in items],
                        "plan": body["plan"],
                    },
                }
            st, payload = self._with_receipt("prepare", vid, body, _do_prepare)
            if st == 200 and self.drop_next_prepare_response:
                self.drop_next_prepare_response = False
                raise CityTransportError("dropped prepare response after commit")
            return st, {}, json.dumps(payload)

        if path.startswith("/visits/"):
            parts = path.strip("/").split("/")
            vid = parts[1]
            sub = parts[2] if len(parts) > 2 else None

            if method == "GET" and sub is None:
                v = self.visits.get(vid)
                if not v:
                    if vid in self.aborted_unknown:
                        rcpts = [
                            dict({"op": meta["op"]}, **meta["receipt"])
                            for meta in self.receipts.values()
                            if meta["visit_id"] == vid and meta["op"] != "act"
                        ]
                        return 200, {}, json.dumps({"visit_id": vid, "state": "aborted", "known": False, "receipts": rcpts})
                    return 404, {}, json.dumps({"error": "NOT_FOUND", "message": "not found", "reason": "no_such_visit"})
                rcpts = [
                    dict({"op": meta["op"]}, **meta["receipt"])
                    for meta in self.receipts.values()
                    if meta["visit_id"] == vid and meta["op"] != "act"
                ]
                return 200, {}, json.dumps({
                    "visit_id": vid,
                    "actor_id": v["actor_id"],
                    "state": v["state"],
                    "protocol": v["protocol"],
                    "city_catalog_version": v["city_catalog_version"],
                    "mappings_version": v["mappings_version"],
                    "vocab_version": v["vocab_version"],
                    "transfer_id": v["transfer_id"],
                    "domain_epoch": v["domain_epoch"],
                    "carry_yen": v["carry_yen"],
                    "plan": v["plan"],
                    "return_by_ms": v["plan"]["return_by_ms"],
                    "return_reason": v.get("return_reason"),
                    "settlement": v["settlement"],
                    "times": {
                        "prepared_at_ms": self.now_ms(),
                        "committed_at_ms": self.now_ms() if v["domain_epoch"] else None,
                        "returning_at_ms": None,
                        "settled_at_ms": self.now_ms() if v["settlement"] else None,
                        "released_at_ms": None,
                        "aborted_at_ms": None,
                    },
                    "receipts": rcpts,
                })

            if method == "POST" and sub == "abort":
                def _do_abort():
                    v = self.visits.get(vid)
                    if not v:
                        self.aborted_unknown.add(vid)
                        return 200, {"state": "aborted", "payload": {"known": False}}
                    if v["state"] == "aborted":
                        return 200, {"state": "aborted", "payload": {"known": True}}
                    if v["state"] != "prepared":
                        return 409, {"error": "CONFLICT", "message": "already committed", "reason": "already_committed"}
                    v["state"] = "aborted"
                    return 200, {"state": "aborted", "payload": {"known": True}}
                st, payload = self._with_receipt("abort", vid, body, _do_abort)
                return st, {}, json.dumps(payload)

            if method == "POST" and sub == "commit":
                def _do_commit():
                    v = self.visits.get(vid)
                    if not v:
                        return 404, {"error": "NOT_FOUND", "message": "missing", "reason": "no_such_visit"}
                    epoch = int(body["domain_epoch"])
                    tid = body["transfer_id"]
                    if v["state"] != "prepared":
                        if v["transfer_id"] == tid and v["domain_epoch"] == epoch:
                            for meta in self.receipts.values():
                                if meta["visit_id"] == vid and meta["op"] == "commit":
                                    return 200, {"state": meta["receipt"]["state"], "payload": meta["receipt"]["payload"]}
                        return 409, {"error": "CONFLICT", "message": "already", "reason": "already_committed"}
                    if epoch <= self.last_epoch[v["actor_id"]]:
                        return 409, {"error": "STALE_DOMAIN_EPOCH", "message": "stale", "last_epoch": self.last_epoch[v["actor_id"]]}
                    self.last_epoch[v["actor_id"]] = epoch
                    v["state"] = "active"
                    v["transfer_id"] = tid
                    v["domain_epoch"] = epoch
                    v["place"] = "sakuraoka:ai-house:entrance"
                    self.bridge_balance -= v["carry_yen"]
                    self.purses[v["actor_id"]]["balance"] += v["carry_yen"]
                    carried_out = []
                    for idx, it in enumerate(v["carry_items"], start=1):
                        eid = f"it_{vid}_{idx}"
                        v["held_items"].append({
                            "item_id": eid,
                            "type": it["engine_type"],
                            "qty": it["qty"],
                            "origin_ref": it["ref"],
                        })
                        carried_out.append({"ref": it["ref"], "engine_type": it["engine_type"], "qty": it["qty"], "engine_item_id": eid})
                    dest = v["plan"]["destination"]
                    p = next(x for x in self.places if x["id"] == dest)
                    return 200, {
                        "state": "active",
                        "payload": {
                            "transfer_id": tid,
                            "domain_epoch": epoch,
                            "purse_yen": v["carry_yen"],
                            "return_reserve_yen": p["homeward"]["fare_yen"],
                            "items": carried_out,
                            "trip": {"trip_id": f"trip_{vid}_1", "to": dest, "ends_at_ms": self.now_ms() + 1500000},
                        },
                    }
                st, payload = self._with_receipt("commit", vid, body, _do_commit)
                if st == 200 and self.drop_next_commit_response:
                    self.drop_next_commit_response = False
                    raise CityTransportError("socket dropped after commit")
                return st, {}, json.dumps(payload)

            if method == "GET" and sub == "view":
                v = self.visits.get(vid)
                if not v:
                    return 404, {}, json.dumps({"error": "NOT_FOUND", "message": "not found"})
                epoch = int(query.get("domain_epoch", 0))
                if v["state"] in ("closed", "reconcile", "aborted") or epoch != v["domain_epoch"]:
                    return 409, {}, json.dumps({"error": "STALE_DOMAIN_EPOCH", "message": "stale"})
                if v["state"] not in ("active", "returning"):
                    return 409, {}, json.dumps({"error": "VISIT_NOT_ACTIVE", "message": "not active"})
                p = next((x for x in self.places if x["id"] == v["place"]), self.places[0])
                reserve = p["homeward"]["fare_yen"]
                purse = self.purses[v["actor_id"]]
                spendable = max(0, purse["balance"] - purse["held"] - reserve)
                return 200, {}, json.dumps({
                    "visit_id": vid,
                    "state": v["state"],
                    "world_id": "city",
                    "at_ms": self.now_ms(),
                    "location": {"place": v["place"]},
                    "wallet": {"yen": purse["balance"], "held_yen": purse["held"], "spendable_yen": spendable, "return_reserve_yen": reserve},
                    "items": list(v["held_items"]),
                    "here": {"place": p, "goods": p["goods"], "people": [{"id": "res_bot_b", "name": "外部旅人B"}]},
                    "can": ["say", "buy", "consume", "give", "travel", "go_home"],
                    "control": {"epoch": epoch, "mine": True},
                })

            if method == "POST" and sub == "act":
                def _do_act():
                    v = self.visits.get(vid)
                    if not v:
                        return 404, {"error": "NOT_FOUND", "message": "not found"}
                    epoch = int(body["domain_epoch"])
                    if v["state"] in ("closed", "reconcile", "aborted") or epoch != v["domain_epoch"]:
                        return 409, {"error": "STALE_DOMAIN_EPOCH", "message": "stale"}
                    if v["state"] != "active":
                        return 409, {"error": "VISIT_NOT_ACTIVE", "message": "not active"}
                    act = body["action"]
                    atype = act["type"]
                    p = next((x for x in self.places if x["id"] == v["place"]), self.places[0])
                    reserve = p["homeward"]["fare_yen"]
                    purse = self.purses[v["actor_id"]]
                    if atype == "say":
                        return 200, {"state": "active", "payload": {"status": "completed", "message_id": 1, "heard_by": 1}}
                    if atype == "buy":
                        sku = act["sku"]
                        qty = int(act.get("qty", 1))
                        good = next((g for g in p["goods"] if g["sku"] == sku), None)
                        if not good:
                            return 422, {"error": "PRECONDITION_FAILED", "message": "no good", "reason": "no_such_good"}
                        cost = good["price_yen"] * qty
                        avail = purse["balance"] - purse["held"]
                        if avail - reserve < cost:
                            reason = "return_fare_reserved" if avail >= cost else "insufficient_funds"
                            return 422, {"error": "PRECONDITION_FAILED", "message": reason, "reason": reason}
                        ord_id = f"ord_{vid}_{len(v['orders']) + 1}"
                        if self.purchase_unknown_sku == sku:
                            purse["held"] += cost
                            v["orders"].append({"order_id": ord_id, "what": good["name"], "yen": cost, "place": p["id"], "status": "pending"})
                            return 200, {"state": "active", "payload": {"status": "pending", "reason": "outcome_unknown", "order_id": ord_id, "held_yen": cost, "purse_yen": purse["balance"]}}
                        purse["balance"] -= cost
                        it_id = f"it_buy_{vid}_{len(v['held_items']) + 1}"
                        v["held_items"].append({"item_id": it_id, "type": good["type"], "qty": qty, "origin_ref": None})
                        self.push_outbox(vid, "purchase", {"what": good["name"], "yen": -cost, "purse_yen": purse["balance"], "order_id": ord_id, "place": p["id"], "sku": sku, "type": good["type"], "qty": qty})
                        return 200, {"state": "active", "payload": {"status": "completed", "order_id": ord_id, "item": {"item_id": it_id, "type": good["type"], "name": good["name"], "qty": qty}, "paid_yen": cost, "purse_yen": purse["balance"]}}
                    if atype == "consume":
                        target = act.get("item_id") or act.get("item")
                        held = next((i for i in v["held_items"] if i["item_id"] == target or i["type"] == target), None)
                        if not held:
                            return 422, {"error": "PRECONDITION_FAILED", "message": "missing", "reason": "not_enough_items"}
                        v["held_items"].remove(held)
                        keff = {"cookie": "snack", "onigiri": "snack", "melon_pan": "snack", "cheesecake": "snack", "coffee": "drink_coffee", "milk_tea": "drink_tea", "green_tea": "drink_tea"}.get(held["type"])
                        ekind = "drank" if held["type"] in ("coffee", "milk_tea", "green_tea") else "ate"
                        eff_rec = self.push_outbox(vid, "body_effect", {"effect_id": f"{vid}:eff_{v['record_seq'] + 1}", "engine_kind": ekind, "engine_type": held["type"], "qty": held["qty"], "kernel_effect": keff}, record_id=f"{vid}:eff_{v['record_seq'] + 1}")
                        v["item_moves"].append({"engine_item_id": held["item_id"], "origin_ref": held["origin_ref"], "type": held["type"], "qty": held["qty"], "fate": "consumed", "effect_id": eff_rec["record_id"]})
                        return 200, {"state": "active", "payload": {"status": "completed", "consumed": {"type": held["type"], "qty": held["qty"]}, "effect_id": eff_rec["record_id"]}}
                    if atype == "give":
                        target = act.get("item_id") or act.get("item")
                        held = next((i for i in v["held_items"] if i["item_id"] == target or i["type"] == target), None)
                        if not held:
                            return 422, {"error": "PRECONDITION_FAILED", "message": "missing", "reason": "not_enough_items"}
                        v["held_items"].remove(held)
                        v["item_moves"].append({"engine_item_id": held["item_id"], "origin_ref": held["origin_ref"], "type": held["type"], "qty": held["qty"], "fate": "given", "to": "外部旅人B"})
                        return 200, {"state": "active", "payload": {"status": "completed", "given": {"type": held["type"], "qty": held["qty"]}, "to": {"id": act["to"], "name": "外部旅人B"}}}
                    if atype == "go_home":
                        v["state"] = "returning"
                        self.push_outbox(vid, "return_started", {"reason": "self", "from": v["place"]})
                        return 200, {"state": "returning", "payload": {"status": "accepted", "returning": True}}
                    return 400, {"error": "BAD_REQUEST", "message": "unknown"}
                st, payload = self._with_receipt("act", vid, body, _do_act)
                return st, {}, json.dumps(payload)

            if method == "POST" and sub == "return-request":
                def _do_return():
                    v = self.visits.get(vid)
                    if not v:
                        return 404, {"error": "NOT_FOUND", "message": "not found"}
                    started = False
                    if v["state"] == "active":
                        v["state"] = "returning"
                        started = True
                        self.push_outbox(vid, "return_started", {"reason": f"kernel:{body.get('reason', 'requested')}", "from": v["place"]})
                    return 200, {"state": v["state"], "payload": {"started": started}}
                st, payload = self._with_receipt("return-request", vid, body, _do_return)
                return st, {}, json.dumps(payload)

            if method == "POST" and sub == "release":
                def _do_release():
                    v = self.visits.get(vid)
                    if not v:
                        return 404, {"error": "NOT_FOUND", "message": "not found"}
                    if v["state"] in ("closed", "reconcile"):
                        return 409, {"error": "CONFLICT", "message": "already released", "reason": "already_released"}
                    if v["state"] != "settled":
                        return 409, {"error": "VISIT_NOT_ACTIVE", "message": "not settled", "reason": "not_settled"}
                    if body.get("settlement_id") != v["settlement"]["settlement_id"]:
                        return 409, {"error": "CONFLICT", "message": "mismatch", "reason": "settlement_mismatch"}
                    home_epoch = int(body["domain_epoch"])
                    if home_epoch <= v["domain_epoch"]:
                        return 409, {"error": "STALE_DOMAIN_EPOCH", "message": "stale"}
                    final_yen = v["settlement"]["purse_final_yen"]
                    self.purses[v["actor_id"]]["balance"] -= final_yen
                    self.bridge_balance += final_yen
                    self.last_epoch[v["actor_id"]] = max(self.last_epoch[v["actor_id"]], home_epoch)
                    v["state"] = "reconcile" if v["settlement"]["pending"] else "closed"
                    return 200, {
                        "state": v["state"],
                        "payload": {
                            "settlement_id": v["settlement"]["settlement_id"],
                            "purse_final_yen": final_yen,
                            "home_epoch": home_epoch,
                            "reconcile_ok": True,
                        },
                    }
                st, payload = self._with_receipt("release", vid, body, _do_release)
                if st == 200 and self.drop_next_release_response:
                    self.drop_next_release_response = False
                    raise CityTransportError("socket dropped after release")
                return st, {}, json.dumps(payload)

        return 404, {}, json.dumps({"error": "NOT_FOUND", "message": "not found"})

    def simulate_arrival_at_destination(self, visit_id, fare_yen=200):
        v = self.visits[visit_id]
        dest = v["plan"]["destination"]
        eta = self.now_ms() + 13 * 60 * 1000
        self.purses[v["actor_id"]]["balance"] -= fare_yen
        self.push_outbox(visit_id, "visit_progress", {"phase": "boarded", "line": "北环快线", "from": "sakuraoka:station", "to": "rurigai:station", "eta_ms": eta, "fare_yen": fare_yen, "text": "乘车中"})
        self.push_outbox(visit_id, "purchase", {"what": "北环快线车票", "yen": -fare_yen, "purse_yen": self.purses[v["actor_id"]]["balance"]})
        self.clock.advance(15 * 60)
        v["place"] = dest
        self.push_outbox(visit_id, "body_effect", {"effect_id": f"{visit_id}:walk1", "engine_kind": "walked", "minutes": 15, "kernel_effect": "walk_15m"}, record_id=f"{visit_id}:walk1")
        self.push_outbox(visit_id, "visit_progress", {"phase": "arrived_place", "place": dest, "text": f"在{dest}"})
        self.push_outbox(visit_id, "decision_wake", {"reason": "arrived", "place": dest})

    def simulate_return_and_settle(self, visit_id, return_fare_yen=200):
        v = self.visits[visit_id]
        if v["state"] == "active":
            v["state"] = "returning"
            self.push_outbox(visit_id, "return_started", {"reason": "return_by", "from": v["place"]})
        self.purses[v["actor_id"]]["balance"] -= return_fare_yen
        self.clock.advance(20 * 60)
        purse = self.purses[v["actor_id"]]
        final_yen = purse["balance"] - purse["held"]

        inbound = {
            "cookie": {"kind": "food", "item": "cookie"},
            "onigiri": {"kind": "food", "item": "onigiri"},
            "green_tea": {"kind": "food", "item": "bottled_tea"},
            "coffee_beans": {"kind": "food", "item": "coffee"},
            "melon_pan": {"kind": "food", "item": "bread"},
            "cream_bun": {"kind": "food", "item": "bread"},
            "cheesecake": {"kind": "food", "item": "dessert"},
        }
        items = []
        for it in list(v["held_items"]):
            if it["type"] in ("coffee", "milk_tea"):
                fate = "disposed"
                kern = None
            elif it["origin_ref"] is not None:
                fate = "back"
                kern = None
            elif it["type"] in inbound:
                fate = "back"
                kern = inbound[it["type"]]
            else:
                fate = "locker"
                kern = None
            entry = {"engine_item_id": it["item_id"], "origin_ref": it["origin_ref"], "type": it["type"], "qty": it["qty"], "fate": fate}
            if kern is not None:
                entry["kernel"] = kern
            items.append(entry)
        for mv in v["item_moves"]:
            items.append({
                "engine_item_id": mv["engine_item_id"],
                "origin_ref": mv["origin_ref"],
                "type": mv["type"],
                "qty": mv["qty"],
                "fate": mv["fate"],
            })
        pending = [o for o in v["orders"] if o["status"] == "pending"]
        settlement = {
            "settlement_id": f"set_{visit_id}",
            "visit_id": visit_id,
            "actor_id": v["actor_id"],
            "cutoff_seq": self.seq,
            "carry_yen": v["carry_yen"],
            "purse_final_yen": final_yen,
            "items": items,
            "consumed": [
                {"type": m["type"], "qty": m["qty"], "effect_id": m.get("effect_id"), "origin_ref": m["origin_ref"]}
                for m in v["item_moves"]
                if m["fate"] == "consumed"
            ],
            "given": [
                {"type": m["type"], "qty": m["qty"], "origin_ref": m["origin_ref"], "to": m.get("to", "")}
                for m in v["item_moves"]
                if m["fate"] == "given"
            ],
            "pending": [{"order_id": o["order_id"], "what": o["what"], "yen": o["yen"], "place": o["place"]} for o in pending],
            "spend_summary": [],
            "settled_at_ms": self.now_ms(),
        }
        v["state"] = "settled"
        v["settlement"] = settlement
        self.push_outbox(visit_id, "visit_settled", settlement, record_id=f"{visit_id}:settled")
        return settlement

    def resolve_pending_failed_after_close(self, visit_id, order_id):
        v = self.visits[visit_id]
        ord_row = next(o for o in v["orders"] if o["order_id"] == order_id)
        ord_row["status"] = "failed"
        yen = ord_row["yen"]
        self.purses[v["actor_id"]]["held"] -= yen
        self.purses[v["actor_id"]]["balance"] -= yen
        self.bridge_balance += yen
        all_clear = not any(o["status"] == "pending" for o in v["orders"])
        if all_clear and v["state"] == "reconcile":
            v["state"] = "closed"
        return self.push_outbox(
            visit_id,
            "order_refunded",
            {"actor_id": v["actor_id"], "order_id": order_id, "yen": yen, "all_pending_cleared": all_clear},
            record_id=f"{visit_id}:refund:{order_id}",
        )


class CityBridgeM1ATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "life1_synth.db")
        self.world_db = os.path.join(self.tmp.name, "world_synth.db")
        offline_world(self.world_db)
        self.clock = VirtualClock(T0)
        self.engine = MockCityBridgeEngine(self.clock)
        self._boot_seq = 0
        self._boot()
        with self.s.tx() as db:
            for a in ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
            db.execute(
                "INSERT INTO resources(resource_id,kind,item,location,holder,qty,source)"
                " VALUES('nell_cookie_1','food','cookie',NULL,'nell',2,'test_seed')"
            )
            db.execute(
                "INSERT INTO resources(resource_id,kind,item,location,holder,qty,source)"
                " VALUES('nell_onigiri_1','food','onigiri',NULL,'nell',1,'test_seed')"
            )

    def _boot(self, *, policy="abort", model_handler=None, allowed=None):
        self._boot_seq += 1
        self.s = Store(self.db_path, self.clock, synthetic=True, seed=24700 + self._boot_seq)
        self.s.acquire()
        self.client = CityBridgeHttpClient(
            "http://127.0.0.1:17700/city/bridge/v1",
            self.engine.bridge_key,
            sleep_fn=lambda _: None,
            transport=self.engine.transport,
        )
        self.life = Life(self.s, extensions=[outings.install])
        self.k = self.life.k
        self.city_model = DeterministicCityModel(model_handler)
        self.adapter = CityBridgeAdapter(
            self.life,
            self.client,
            city_model=self.city_model,
            allowed_actors=allowed or ("nell",),
            recover_preparing_policy=policy,
            intercept_plans=True,
        )
        self.life.extensions.append(self.adapter)
        self.proj = Projector(self.life, self.world_db)

    def _cards(self):
        with contextlib.closing(sqlite3.connect(self.world_db)) as w:
            w.row_factory = sqlite3.Row
            return cards(self.s.db, w, self.clock.now())

    def _restart_kernel(self, *, downtime_s=30, policy="abort", model_handler=None, allowed=None):
        stopped = self.clock.now()
        self.s.release()
        self.s.close()
        self.clock.advance(downtime_s)
        self._boot(policy=policy, model_handler=model_handler, allowed=allowed)
        self.k.resume(stopped)
        self.adapter.recover()

    def tearDown(self):
        if self.s.epoch is not None:
            self.s.release()
        self.s.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ S1
    def test_s1_nell_full_vertical_slice_and_reconcile(self):
        def nell_brain(ctx):
            reason = ctx["wake"]["reason"]
            if reason == "arrived":
                return [
                    {"type": "say", "text": "你好呀，这家的咖啡很香。"},
                    {"type": "buy", "sku": "coffee", "qty": 1},
                    {"type": "consume", "item": "coffee", "qty": 1},
                    {"type": "buy", "sku": "coffee_beans", "qty": 1},
                ]
            if reason == "heard_chat":
                return [{"type": "go_home"}]
            return []

        self.city_model.handler = nell_brain

        # Create confirmed home outing plan linked to visit (K5)
        now = self.clock.now()
        with self.s.tx() as db:
            db.execute(
                "INSERT INTO plans(plan_id,host,kind,what,location,start_at,end_at,members,state,created_at,updated_at)"
                " VALUES('plan_s1','nell','outing','cafe','place:rurigai',?,?,?,'confirmed',?,?)",
                (now, now + 7200, json.dumps(["nell"]), now, now),
            )

        start_bal = self.adapter.wallet.available("nell")
        self.assertEqual(40000, start_bal)

        # Step 1-2: T1 departure with ¥20,000 + 1 cookie
        vis = self.adapter.depart(
            "nell",
            "rurigai:kissa-tomoshibi",
            carry_yen=20000,
            carry_items=[{"resource_id": "nell_cookie_1", "qty": 1}],
            visit_id="vis_s1",
            plan_id="plan_s1",
        )
        self.assertEqual("preparing", vis["state"])
        self.assertEqual(20000, self.adapter.wallet.available("nell"))
        # Balance itself unchanged during hold
        bal_row = self.s.db.execute("SELECT balance FROM wallets WHERE account='nell'").fetchone()[0]
        self.assertEqual(40000, bal_row)
        # Cookie 1 of 2 held in custody -> free_qty is 1, admin cannot reduce stock below held
        self.assertEqual(
            "would_break_reservation",
            admin.correction(self.k, "admin:world", "nell_cookie_1", 0, "probe")["reason"],
        )

        # Walk living -> away (60s), then prepare -> commit (T2)
        self.clock.advance(60)
        self.k.tick()
        vis = self.adapter.advance_handover("vis_s1")
        self.assertEqual("active", vis["state"])
        self.assertEqual({"actor_id": "nell", "domain": "city", "domain_epoch": 1, "visit_id": "vis_s1"},
                         {k: self.k.city_visits.authority("nell")[k] for k in ("actor_id", "domain", "domain_epoch", "visit_id")})

        # Verify derived action request_ids and K7 transit / cafe projection
        self.engine.push_outbox(
            "vis_s1",
            "visit_progress",
            {"phase": "boarded", "line": "北环快线", "from": "sakuraoka:station", "to": "rurigai:station", "eta_ms": self.engine.now_ms() + 780000, "fare_yen": 200, "text": "乘车中"},
        )
        self.adapter.poll_outbox_once()
        self.proj.run()
        card_transit = self._cards()["residents"]["nell"]["city_outing"]
        self.assertIn("乘车中", card_transit["status_text"])

        # Arrive at cafe -> decision_wake triggers say + buy coffee + drink coffee + buy coffee_beans
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", body=json.dumps({"energy": 80.0, "hunger": 80.0}), body_at=self.clock.now())
        self.engine.simulate_arrival_at_destination("vis_s1", fare_yen=200)
        # Also buy a postcard (unmapped -> locker) via a second wake + encounter
        self.adapter.poll_outbox_once()
        self.proj.run()
        card_here = self._cards()["residents"]["nell"]["city_outing"]
        self.assertIn("喫茶「帆」", card_here["status_text"])

        # Verify deterministic derived request_ids visit:vis_s1:d1:a1..a4
        req_ids = [
            r[0]
            for r in self.s.db.execute(
                "SELECT request_id FROM city_decision_actions WHERE visit_id='vis_s1' ORDER BY action_seq"
            ).fetchall()
        ]
        self.assertEqual(
            ["visit:vis_s1:d1:a1", "visit:vis_s1:d1:a2", "visit:vis_s1:d1:a3", "visit:vis_s1:d1:a4"],
            req_ids,
        )
        # Body updated by walk_15m (-0.75 energy) and drink_coffee (-60 hunger) + 15m base drift
        body_mid = self.k.actor("nell")["body"]
        self.assertLess(body_mid["hunger"], 30.0)

        # Encounter with Bot B + Bot B speaks -> wake -> nell decides go_home
        enc_rec = self.engine.push_outbox(
            "vis_s1",
            "encounter",
            {"with": {"name": "外部旅人B"}, "place": "rurigai:kissa-tomoshibi", "place_name": "喫茶「帆」", "area": "rurigai"},
        )
        self.engine.push_outbox(
            "vis_s1",
            "decision_wake",
            {"reason": "heard_chat", "from": {"name": "外部旅人B"}, "place": "rurigai:kissa-tomoshibi"},
        )
        self.adapter.poll_outbox_once()
        # Duplicate encounter replay is idempotent (still 1 row in city_encounters and 1 material)
        self.adapter._handle_encounter(enc_rec)
        with self.s.read() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM city_encounters WHERE visit_id='vis_s1'").fetchone()[0])
            self.assertEqual(
                1,
                db.execute("SELECT COUNT(*) FROM materials WHERE source_kind='city_encounter' AND actor_id='nell'").fetchone()[0],
            )
        self.assertEqual("returning", self.k.city_visits.get("vis_s1")["state"])

        # Also add an unmapped item in engine held_items to verify locker -> normal closed!
        self.engine.visits["vis_s1"]["held_items"].append(
            {"item_id": "it_postcard_1", "type": "postcard", "qty": 1, "origin_ref": None}
        )
        self.engine.simulate_return_and_settle("vis_s1", return_fare_yen=200)
        self.adapter.poll_outbox_once()

        # T3 + release completed; finish door walk away -> living
        self.clock.advance(60)
        self.k.tick()
        final_vis = self.k.city_visits.get("vis_s1")
        self.assertEqual("closed", final_vis["state"])
        self.assertEqual(2, self.k.city_visits.authority("nell")["domain_epoch"])
        self.assertEqual("home", self.k.city_visits.authority("nell")["domain"])
        self.assertEqual("living", self.k.actor("nell")["location"])
        self.assertEqual("done", self.life.plans.plan("plan_s1")["state"])

        # Money exact conservation: spent 200 (out) + 450 (coffee) + 900 (beans) + 200 (back) = 1750
        self.assertEqual(40000 - 1750, self.adapter.wallet.available("nell"))
        self.assertEqual(-1750, self.engine.bridge_balance)

        # Carried cookie returned & unlocked; bought coffee_beans mapped to kitchen food/coffee; postcard in locker
        self.assertEqual(2, self.k.resource("nell_cookie_1")["qty"])
        with self.s.read() as db:
            self.assertEqual(2, self.k._free_qty(db, "nell_cookie_1"))
            bought_coffee = db.execute(
                "SELECT item, qty, location FROM resources WHERE resource_id LIKE 'city:vis_s1:%'"
            ).fetchone()
            self.assertEqual(("coffee", 1, "kitchen"), tuple(bought_coffee))
            locker = json.loads(
                db.execute("SELECT locker_items FROM city_visit_carry WHERE visit_id='vis_s1'").fetchone()[0]
            )
            self.assertEqual("postcard", locker[0]["type"])

        rec = self.adapter.reconcile()
        self.assertTrue(rec["ok"], rec)

    # ------------------------------------------------------------------ F1 & F2
    def test_f1_f2_idempotent_replays_and_payload_conflict(self):
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=20000, auto_snack=False, visit_id="vis_f1")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_f1")

        carry = self.adapter._carry_row("vis_f1")
        prep_body = self.adapter._build_prepare_wire_body(self.k.city_visits.get("vis_f1"), carry)
        # F1: replay prepare & commit & act with exact same request_id
        r1 = self.client.prepare_visit(prep_body)
        self.assertTrue(r1.get("replayed"))
        r2 = self.client.commit_visit("vis_f1", request_id=carry["commit_request_id"], transfer_id="tr:vis_f1:1", domain_epoch=1)
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(-20000, self.engine.bridge_balance)

        a1 = self.client.visit_act("vis_f1", request_id="visit:vis_f1:d1:a1", domain_epoch=1, action={"type": "say", "text": "早安"})
        a2 = self.client.visit_act("vis_f1", request_id="visit:vis_f1:d1:a1", domain_epoch=1, action={"type": "say", "text": "早安"})
        self.assertEqual(a1["receipt_id"], a2["receipt_id"])
        self.assertTrue(a2.get("replayed"))

        # F2: same request_id with different content -> 409 CONFLICT
        with self.assertRaises(CityBridgeError) as cm:
            self.client.visit_act("vis_f1", request_id="visit:vis_f1:d1:a1", domain_epoch=1, action={"type": "say", "text": "篡改内容"})
        self.assertEqual("CONFLICT", cm.exception.code)

    # ------------------------------------------------------------------ F3
    def test_f3_crash_after_t1_before_prepare_abort_and_continue(self):
        before_bal = self.adapter.wallet.available("nell")
        before_cookie = self.k.resource("nell_cookie_1")["qty"]
        self.adapter.depart(
            "nell",
            "rurigai:kissa-tomoshibi",
            carry_yen=20000,
            carry_items=[{"resource_id": "nell_cookie_1", "qty": 1}],
            visit_id="vis_f3_abort",
        )
        # Crash kernel before prepare is sent, restart with default policy='abort'
        self._restart_kernel(downtime_s=65, policy="abort")
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("aborted", self.k.city_visits.get("vis_f3_abort")["state"])
        self.assertEqual(before_bal, self.adapter.wallet.available("nell"))
        self.assertEqual(before_cookie, self.k.resource("nell_cookie_1")["qty"])
        self.assertEqual("living", self.k.actor("nell")["location"])

        # Second visit: crash after T1, restart with policy='continue' -> seamlessly reaches 'active'
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=20000, auto_snack=False, visit_id="vis_f3_cont")
        self._restart_kernel(downtime_s=65, policy="continue")
        self.assertEqual("active", self.k.city_visits.get("vis_f3_cont")["state"])

    # ------------------------------------------------------------------ F4 & 503 retry
    def test_f4_dropped_commit_response_and_503_recovering_retry(self):
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=20000, auto_snack=False, visit_id="vis_f4")
        self.clock.advance(60)
        self.k.tick()
        self.engine.bridge_503_count = 2
        self.engine.drop_next_commit_response = True
        vis = self.adapter.advance_handover("vis_f4")
        self.assertEqual("active", vis["state"])
        # Purse credited exactly once!
        self.assertEqual(20000, self.engine.purses["nell"]["balance"])
        self.assertEqual(-20000, self.engine.bridge_balance)

    # ------------------------------------------------------------------ F5, F6 & hot drink disposed
    def test_f5_f6_carried_consumed_given_hot_drink_disposed_and_offline_return(self):
        # Visit A: eat carried cookie + buy hot coffee & leave it unconsumed (disposed) + buy melon_pan & go offline (F5)
        def plan_a(ctx):
            if ctx["wake"]["reason"] == "arrived":
                return [
                    {"type": "consume", "item": "cookie", "qty": 1},
                    {"type": "buy", "sku": "coffee", "qty": 1},
                ]
            return []

        self.city_model.handler = plan_a
        self.adapter.depart(
            "nell",
            "rurigai:kissa-tomoshibi",
            carry_yen=20000,
            carry_items=[{"resource_id": "nell_cookie_1", "qty": 1}],
            visit_id="vis_f6a",
        )
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_f6a")
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", body=json.dumps({"energy": 80.0, "hunger": 90.0}), body_at=self.clock.now())
        self.engine.simulate_arrival_at_destination("vis_f6a")
        self.adapter.poll_outbox_once()
        hunger_after_cookie = self.k.actor("nell")["body"]["hunger"]
        self.assertLess(hunger_after_cookie, 35.0)

        # Adapter goes offline until return_by passes -> engine auto-settles with unconsumed coffee -> disposed
        self.engine.simulate_return_and_settle("vis_f6a")
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("closed", self.k.city_visits.get("vis_f6a")["state"])
        # Carried cookie deducted from 2 -> 1; unconsumed coffee disposed (NOT eaten, zero extra hunger drop!)
        self.assertEqual(1, self.k.resource("nell_cookie_1")["qty"])
        self.assertGreater(self.k.actor("nell")["body"]["hunger"], hunger_after_cookie)

        # Visit B: give carried onigiri to Bot B -> fate='given' deducts home stock 1 -> 0
        self.city_model.handler = lambda ctx: (
            [{"type": "give", "to": "res_bot_b", "item": "onigiri", "qty": 1}, {"type": "go_home"}]
            if ctx["wake"]["reason"] == "arrived"
            else []
        )
        self.adapter.depart(
            "nell",
            "rurigai:kissa-tomoshibi",
            carry_yen=10000,
            carry_items=[{"resource_id": "nell_onigiri_1", "qty": 1}],
            visit_id="vis_f6b",
        )
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_f6b")
        self.engine.simulate_arrival_at_destination("vis_f6b")
        self.adapter.poll_outbox_once()
        self.engine.simulate_return_and_settle("vis_f6b")
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual(0, self.k.resource("nell_onigiri_1")["qty"])
        self.assertTrue(self.adapter.reconcile()["ok"])

    # ------------------------------------------------------------------ F7 & F8
    def test_f7_f8_walk_blocks_dedupe_and_stale_epoch_fencing(self):
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=10000, auto_snack=False, visit_id="vis_f8")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_f8")

        # F8a: while in city, home commands for nell are fenced out
        res = self.k.submit({
            "idempotency_key": "home_while_out",
            "principal": "channel:nell",
            "actor_id": "nell",
            "origin": "self",
            "action_type": "rest",
            "args": {"seconds": 600},
            "domain_epoch": 1,
        })
        self.assertEqual("failed", res["status"])

        # F8b: stale domain_epoch on bridge act is rejected with STALE_DOMAIN_EPOCH
        with self.assertRaises(CityBridgeError) as cm:
            self.client.visit_act("vis_f8", request_id="visit:vis_f8:stale1", domain_epoch=99, action={"type": "say", "text": "x"})
        self.assertEqual("STALE_DOMAIN_EPOCH", cm.exception.code)

        # F7: 30 minutes walked -> quantity=2 -> energy -1.5 (not -22.5!), duplicate replay ignored
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", body=json.dumps({"energy": 80.0, "hunger": 40.0}), body_at=self.clock.now())
        rec = self.engine.push_outbox(
            "vis_f8",
            "body_effect",
            {"effect_id": "vis_f8:walk30", "engine_kind": "walked", "minutes": 30, "kernel_effect": "walk_15m"},
            record_id="vis_f8:walk30",
        )
        self.adapter.poll_outbox_once()
        self.assertAlmostEqual(78.5, self.k.actor("nell")["body"]["energy"], places=3)
        # Duplicate replay of same record_id does nothing
        self.adapter._handle_body_effect(rec)
        self.assertAlmostEqual(78.5, self.k.actor("nell")["body"]["energy"], places=3)

        # Return & close -> domain_epoch becomes 2; stale epoch=1 on city & home both rejected
        self.engine.simulate_return_and_settle("vis_f8", return_fare_yen=0)
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        with self.assertRaises(CityBridgeError) as cm2:
            self.client.visit_act("vis_f8", request_id="visit:vis_f8:after_home", domain_epoch=1, action={"type": "say", "text": "x"})
        self.assertEqual("STALE_DOMAIN_EPOCH", cm2.exception.code)
        stale_home = self.k.submit({
            "idempotency_key": "stale_home_after_city",
            "principal": "channel:nell",
            "actor_id": "nell",
            "origin": "self",
            "action_type": "rest",
            "args": {"seconds": 600},
            "domain_epoch": 1,
        })
        self.assertEqual("STALE_DOMAIN_EPOCH", stale_home["reason"])

    # ------------------------------------------------------------------ F9, F10, F11, F12
    def test_f9_f10_f11_f12_pending_order_outage_crash_before_release_and_fare_reserve(self):
        # F12a: carry below round-trip transit reserve (200 + 200 + 200 margin = 600) is refused BEFORE T1
        with self.assertRaises(Refused) as cm_low:
            self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=300, auto_snack=False, visit_id="vis_low")
        self.assertEqual("carry_below_transit_reserve", cm_low.exception.reason)
        self.assertIsNone(self.k.city_visits.get("vis_low"))

        # F12 + F9: carry ¥1,200 to Rurigai (out ¥200, return reserve ¥200 -> ¥800 spendable)
        # Try buying coffee_beans (¥900 -> blocked by return_fare_reserved!), then buy cheesecake (¥520 -> purchaseUnknown pending!)
        def brain_f9(ctx):
            if ctx["wake"]["reason"] == "arrived":
                return [
                    {"type": "buy", "sku": "coffee_beans", "qty": 1},
                    {"type": "buy", "sku": "cheesecake", "qty": 1},
                ]
            return []

        self.city_model.handler = brain_f9
        self.engine.purchase_unknown_sku = "cheesecake"
        start_bal = self.adapter.wallet.available("nell")
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=1200, duration_s=1800, auto_snack=False, visit_id="vis_f9")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_f9")
        self.engine.simulate_arrival_at_destination("vis_f9", fare_yen=200)
        self.adapter.poll_outbox_once()

        # Action 1 (coffee_beans ¥900) rejected due to return_fare_reserved; Action 2 (cheesecake ¥520) pending
        act_states = [
            r[0]
            for r in self.s.db.execute(
                "SELECT state FROM city_decision_actions WHERE visit_id='vis_f9' ORDER BY action_seq"
            ).fetchall()
        ]
        self.assertEqual(["rejected", "completed"], act_states)

        # F10: engine unreachable for 30 minutes past return_by -> status unconfirmed, no refund or premature home move
        self.clock.advance(2000)
        self.engine.bridge_503_count = 3
        self.engine.outbox_503_count = 5
        self.adapter.step()
        proj = city_projection.resolve_projection(self.s, "nell", now=self.clock.now())
        self.assertTrue(proj["unconfirmed"])
        self.assertIn("状态待确认", proj["status_text"])
        self.assertEqual("away", self.k.actor("nell")["location"])
        self.assertEqual(start_bal - 1200, self.adapter.wallet.available("nell"))

        # Engine recovers & settles; drop release response once to simulate crash between T3 and release (F11b)!
        self.engine.bridge_503_count = 0
        self.engine.outbox_503_count = 0
        self.engine.simulate_return_and_settle("vis_f9", return_fare_yen=200)
        self.engine.drop_next_release_response = True
        # Force _confirm_release to defer by also returning 503 on fallback
        self.engine.bridge_503_count = 5
        self.adapter.poll_outbox_once()
        self.assertEqual("reconcile", self.k.city_visits.get("vis_f9")["state"])
        self.assertEqual(0, self.adapter._carry_row("vis_f9")["release_confirmed"])
        # Net delta so far: carry 1200, out 200, back 200, pending held 520 -> purse_final 280 -> net -920
        self.assertEqual(start_bal - 920, self.adapter.wallet.available("nell"))

        # Kernel restarts while release_confirmed=0 -> recovers release without double-booking T3!
        self.engine.bridge_503_count = 0
        self._restart_kernel(downtime_s=65)
        self.assertEqual(1, self.adapter._carry_row("vis_f9")["release_confirmed"])
        self.assertEqual(start_bal - 920, self.adapter.wallet.available("nell"))
        self.assertEqual("living", self.k.actor("nell")["location"])

        # Post-close pending order resolved as failed -> order_refunded refunds +¥520 and closes reconcile -> closed!
        ord_id = self.engine.visits["vis_f9"]["orders"][0]["order_id"]
        self.engine.resolve_pending_failed_after_close("vis_f9", ord_id)
        self.adapter.poll_outbox_once()
        self.assertEqual("closed", self.k.city_visits.get("vis_f9")["state"])
        self.assertEqual(start_bal - 400, self.adapter.wallet.available("nell"))

        # F11a: engine settles while kernel is completely stopped -> kernel restarts, drains outbox, applies T3 once & releases
        self.clock.advance(60)
        self.k.tick()
        bal_before_f11 = self.adapter.wallet.available("nell")
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=5000, auto_snack=False, visit_id="vis_f11a")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_f11a")
        self.engine.simulate_arrival_at_destination("vis_f11a", fare_yen=200)
        self.engine.simulate_return_and_settle("vis_f11a", return_fare_yen=200)
        self._restart_kernel(downtime_s=30)
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("closed", self.k.city_visits.get("vis_f11a")["state"])
        self.assertEqual(1, self.adapter._carry_row("vis_f11a")["release_confirmed"])
        self.assertEqual(bal_before_f11 - 400, self.adapter.wallet.available("nell"))
        self.assertEqual("living", self.k.actor("nell")["location"])

    # ------------------------------------------------------------------ F13 & F14
    def test_f13_f14_rain_umbrella_rule_and_weather_3h_fallback(self):
        # F13: Rurigai raining, strip nell's umbrellas -> refused BEFORE T1 (zero hold, stays in living)
        self.engine.rain["rurigai"] = True
        with self.s.tx() as db:
            db.execute("DELETE FROM garments WHERE actor='nell' AND (garment_id LIKE '%umbrella%' OR label LIKE '%伞%')")
            # Parasol (阳伞) must NOT count as rain gear
            self.life.wardrobe.add(
                db,
                "nell",
                {"id": "nell_parasol", "label": "蕾丝阳伞", "category": "accessory", "temp_c": [15, 35], "occasions": ["out"], "like": 3},
                "test:parasol",
            )
        before_bal = self.adapter.wallet.available("nell")
        with self.assertRaises(Refused) as cm:
            self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=10000, visit_id="vis_rain_no")
        self.assertEqual("rain_requires_owned_umbrella", cm.exception.reason)
        self.assertEqual(before_bal, self.adapter.wallet.available("nell"))
        self.assertEqual("living", self.k.actor("nell")["location"])
        self.assertIsNone(self.k.city_visits.get("vis_rain_no"))

        # Riverside closed in rain even if she has an umbrella
        self.engine.rain["sakuraoka"] = True
        with self.s.tx() as db:
            self.life.wardrobe.add(
                db,
                "nell",
                {"id": "nell_umbrella", "label": "透明雨伞", "category": "accessory", "temp_c": [0, 35], "occasions": ["out"], "like": 4},
                "test:umbrella",
            )
        with self.assertRaises(Refused) as cm_closed:
            self.adapter.depart("nell", "sakuraoka:hoshikawa-riverbank", carry_yen=5000, visit_id="vis_river_rain")
        self.assertEqual("place_closed_by_rain", cm_closed.exception.reason)

        # Indoor cafe in rain WITH owned umbrella -> equips umbrella and departs!
        vis = self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=10000, auto_snack=False, visit_id="vis_rain_ok")
        self.assertEqual("preparing", vis["state"])
        self.assertIn("nell_umbrella", self.life.wardrobe.current("nell")["items"])
        # Cleanly abort vis_rain_ok
        self.clock.advance(60)
        self.k.tick()
        self.adapter._abort_with_engine(self.k.city_visits.get("vis_rain_ok"), self.adapter._carry_row("vis_rain_ok"), "test_done")
        self.clock.advance(60)
        self.k.tick()

        # F14: weather endpoint offline -> within 3h = 'stale', past 3h = 'fallback' matching engine formula
        self.engine.weather_offline = True
        self.clock.advance(2 * 3600)
        w_stale = self.adapter.weather(self.clock.now())
        self.assertEqual("stale", w_stale["status"])
        self.assertTrue(w_stale["rain"])

        self.clock.advance(2 * 3600)  # Total 4h > 3h
        w_fb = self.adapter.weather(self.clock.now())
        expected_fb = city_fallback_weather(int(round(self.clock.now() * 1000)), "sakuraoka")
        self.assertEqual("fallback", w_fb["status"])
        self.assertFalse(w_fb["rain"])
        self.assertEqual(expected_fb["temp_c"], w_fb["temp_c"])
        self.assertEqual(expected_fb["code"], w_fb["code"])
        self.assertEqual("sakuraoka", w_fb["area"])

    # ------------------------------------------------------------------ F15, F16 & K5 Plan Routing
    def test_f15_f16_rate_limit_isolation_entry_via_home_only_and_k5_plan_routing(self):
        # F16: Roommate QQ key calling enter_world directly is rejected with 403 ENTRY_VIA_HOME_ONLY (non-retryable)
        with self.assertRaises(CityBridgeError) as cm_f16:
            self.client.request("POST", "/mcp/enter_world", body={"caller_qq_id": "qq_nell", "world_id": "city"})
        self.assertEqual("ENTRY_VIA_HOME_ONLY", cm_f16.exception.code)
        self.assertEqual(403, cm_f16.exception.status)
        self.assertFalse(cm_f16.exception.retryable)

        # F15: External Bot B hits 61st world_act -> RATE_LIMITED + retryAfterMs; Nell's bridge visit via K5 plan is unaffected!
        for _ in range(60):
            r = self.client.request("POST", "/mcp/external_act", body={"bot_id": "ext_bot_b"})
            self.assertTrue(r["ok"])
        self.client.max_retries = 0
        with self.assertRaises(CityBridgeError) as cm_f15:
            self.client.request("POST", "/mcp/external_act", body={"bot_id": "ext_bot_b"})
        self.assertEqual("RATE_LIMITED", cm_f15.exception.code)
        self.assertEqual(1000, cm_f15.exception.retryAfterMs if hasattr(cm_f15.exception, "retryAfterMs") else cm_f15.exception.retry_after_ms)
        self.client.max_retries = 4

        # K5: Confirmed plan to 'place:rurigai' created via normal Kernel.submit(invite/respond_invite)
        # (never direct SQL INSERT into plans) is routed through CityBridgeAdapter (not old Outings!)
        self.adapter.enable_actor("elsa")
        now = self.clock.now()
        inv_res = self.k.submit(
            {
                "idempotency_key": "k5:f15:invite",
                "principal": "channel:nell",
                "actor_id": "nell",
                "origin": "self",
                "action_type": "invite",
                "args": {
                    "to": ["elsa"],
                    "kind": "outing",
                    "what": "cafe_time",
                    "location": "place:rurigai",
                    "start_at": now + 60,
                    "end_at": now + 5460,
                },
            }
        )
        self.assertEqual("succeeded", inv_res["status"], inv_res)
        plan_k5_id = inv_res["plan_id"]
        inv_id = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == plan_k5_id)
        resp_res = self.k.submit(
            {
                "idempotency_key": "k5:f15:respond",
                "principal": "channel:elsa",
                "actor_id": "elsa",
                "origin": "self",
                "action_type": "respond_invite",
                "args": {"inv_id": inv_id, "answer": "yes"},
            }
        )
        self.assertEqual("succeeded", resp_res["status"], resp_res)
        self.assertEqual("confirmed", self.life.plans.plan(plan_k5_id)["state"])
        self.clock.advance(60)
        self.city_model.handler = lambda ctx: [{"type": "say", "text": "顺利进城啦"}, {"type": "go_home"}]
        self.life.step()
        # Verify Outings did NOT create legacy journeys, while CityBridgeAdapter created city visits for both members!
        self.assertIsNone(self.life.get(f"journey:{plan_k5_id}:nell"))
        self.assertIsNone(self.life.get(f"journey:{plan_k5_id}:elsa"))
        vis_nell = self.k.city_visits.current("nell")
        vis_elsa = self.k.city_visits.current("elsa")
        self.assertIsNotNone(vis_nell)
        self.assertIsNotNone(vis_elsa)
        self.assertEqual("rurigai:kissa-tomoshibi", vis_nell["plan"]["destination"])
        self.assertEqual("rurigai:kissa-tomoshibi", vis_elsa["plan"]["destination"])
        vid = vis_nell["visit_id"]
        vid_elsa = vis_elsa["visit_id"]
        self.clock.advance(60)
        # Also test transient 429 retry with exact same request_id on bridge commit/act
        self.engine.bridge_429_count = 1
        self.life.step()
        self.assertEqual("active", self.k.city_visits.get(vid)["state"])
        self.assertEqual("active", self.k.city_visits.get(vid_elsa)["state"])
        self.engine.simulate_arrival_at_destination(vid, fare_yen=200)
        self.engine.simulate_arrival_at_destination(vid_elsa, fare_yen=200)
        self.engine.bridge_429_count = 1
        self.life.step()
        self.assertEqual("returning", self.k.city_visits.get(vid)["state"])
        self.assertEqual("returning", self.k.city_visits.get(vid_elsa)["state"])
        self.engine.simulate_return_and_settle(vid, return_fare_yen=200)
        self.engine.simulate_return_and_settle(vid_elsa, return_fare_yen=200)
        self.life.step()
        self.clock.advance(60)
        self.life.step()
        self.assertEqual("closed", self.k.city_visits.get(vid)["state"])
        self.assertEqual("closed", self.k.city_visits.get(vid_elsa)["state"])
        self.assertEqual("done", self.life.plans.plan(plan_k5_id)["state"])
        self.assertEqual("living", self.k.actor("nell")["location"])
        self.assertEqual("living", self.k.actor("elsa")["location"])
        self.assertTrue(self.adapter.reconcile()["ok"])

    # ------------------------------------------------------------------ Shared Budget & 6-Roommate Rollout
    def test_shared_budget_graceful_degradation_and_six_roommate_rollout(self):
        self.engine.rain = {"sakuraoka": False, "rurigai": False, "kazeminato": False}
        # Before rollout, elsa is gated off
        with self.assertRaises(Refused) as cm:
            self.adapter.depart("elsa", "rurigai:shiorido", carry_yen=5000, auto_snack=False)
        self.assertEqual("actor_not_enabled_for_city", cm.exception.reason)

        # Exhaust shared 40/h life.budget pool -> nell still travels, degrades wake gracefully, and settles home!
        for _ in range(40):
            self.assertIsNotNone(self.life.budget.reserve("elsa", "home_test", units=1))
        self.assertIsNone(self.life.budget.reserve("nell", "probe", units=1))

        self.city_model.handler = lambda ctx: [{"type": "say", "text": "不该被调用"}]
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=5000, auto_snack=False, visit_id="vis_deg")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_deg")
        self.engine.simulate_arrival_at_destination("vis_deg")
        self.adapter.poll_outbox_once()
        self.assertEqual(0, self.city_model.calls)
        self.assertEqual(1, self.adapter._carry_row("vis_deg")["budget_exhausted"])
        self.engine.simulate_return_and_settle("vis_deg")
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("closed", self.k.city_visits.get("vis_deg")["state"])

        # Advance >1h so hourly window clears, open all 6 roommates!
        self.clock.advance(3700)
        self.adapter.enable_all_roommates()
        self.assertEqual(set(ALL_HOUSE_ACTORS), self.adapter.allowed_actors)
        self.city_model.handler = lambda ctx: [{"type": "go_home"}] if ctx["wake"]["reason"] == "arrived" else []

        for actor in ALL_HOUSE_ACTORS:
            vid = f"vis_all_{actor}"
            self.adapter.depart(actor, "kazeminato:muginohi", carry_yen=5000, auto_snack=False, visit_id=vid)
        self.clock.advance(60)
        self.k.tick()
        for actor in ALL_HOUSE_ACTORS:
            vid = f"vis_all_{actor}"
            self.adapter.advance_handover(vid)
            self.assertEqual("active", self.k.city_visits.get(vid)["state"])
            self.engine.simulate_arrival_at_destination(vid, fare_yen=260)
        self.adapter.poll_outbox_once()
        for actor in ALL_HOUSE_ACTORS:
            vid = f"vis_all_{actor}"
            self.engine.simulate_return_and_settle(vid, return_fare_yen=260)
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        for actor in ALL_HOUSE_ACTORS:
            self.assertEqual("closed", self.k.city_visits.get(f"vis_all_{actor}")["state"])
            self.assertEqual("living", self.k.actor(actor)["location"])
        self.assertTrue(self.adapter.reconcile()["ok"])

        # Also verify rolling 24h 300-call shared pool cap degrades wake gracefully without blocking deterministic return!
        self.clock.advance(25 * 3600)  # Fresh 24h window & morning life-day (14:00 JST next day)
        with self.s.tx() as db:
            for a in ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
        for i in range(10):
            for _ in range(30):
                self.assertIsNotNone(self.life.budget.reserve("elsa", "home_24h_fill", units=1))
            if i < 9:
                self.clock.advance(3700)  # Spread 300 calls across ~9.25 hours (< 24h, never > 40/h)
        # Now hourly count is 30 (<40), but rolling 24h count is 300 (==300 cap)!
        self.assertIsNone(self.life.budget.reserve("nell", "probe_24h", units=1))
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", location="living", activity="idle", action_id=None)
        calls_before = self.city_model.calls
        self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=5000, duration_s=1800, auto_snack=False, visit_id="vis_24h_deg")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_24h_deg")
        self.engine.simulate_arrival_at_destination("vis_24h_deg")
        self.adapter.poll_outbox_once()
        self.assertEqual(calls_before, self.city_model.calls)
        self.assertEqual(1, self.adapter._carry_row("vis_24h_deg")["budget_exhausted"])
        self.engine.simulate_return_and_settle("vis_24h_deg")
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        self.assertEqual("closed", self.k.city_visits.get("vis_24h_deg")["state"])
        self.assertTrue(self.adapter.reconcile()["ok"])

    # ------------------------------------------------------------------ R0 mappings v2/v3 & D0 schema contract
    def test_mappings_version_v2_and_v3_dual_support_and_mismatch_refusal(self):
        self.assertEqual((2, 3), SUPPORTED_MAPPINGS_VERSIONS)
        self.assertEqual((2, 3), city_visits.SUPPORTED_MAPPINGS_VERSIONS)
        self.assertEqual(2, MAPPINGS_VERSION)

        # Unsupported versions refused BEFORE T1 (zero hold, actor stays in living)
        before_bal = self.adapter.wallet.available("nell")
        for bad_ver in (0, 1, 4, True, "3", None):
            self.engine.mappings_version = bad_ver
            with self.assertRaises(Refused) as cm:
                self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=5000, auto_snack=False, visit_id=f"vis_bad_{bad_ver}")
            self.assertEqual("VERSION_MISMATCH", cm.exception.reason)
            self.assertEqual(before_bal, self.adapter.wallet.available("nell"))
            self.assertEqual("living", self.k.actor("nell")["location"])

        # Both mappings_version=2 and mappings_version=3 complete full visit lifecycle cleanly
        for ver in (2, 3):
            self.engine.mappings_version = ver
            vid = f"vis_mv{ver}"
            self.city_model.handler = lambda ctx: (
                [{"type": "buy", "sku": "coffee", "qty": 1}, {"type": "consume", "item": "coffee", "qty": 1}, {"type": "go_home"}]
                if ctx["wake"]["reason"] == "arrived"
                else []
            )
            vis = self.adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=5000, auto_snack=False, visit_id=vid)
            self.assertEqual(ver, vis["mappings_version"])
            self.clock.advance(60)
            self.k.tick()
            vis = self.adapter.advance_handover(vid)
            self.assertEqual("active", vis["state"])
            self.assertEqual(ver, self.engine.visits[vid]["mappings_version"])
            self.engine.simulate_arrival_at_destination(vid, fare_yen=200)
            self.adapter.poll_outbox_once()
            self.engine.simulate_return_and_settle(vid, return_fare_yen=200)
            self.adapter.poll_outbox_once()
            self.clock.advance(60)
            self.k.tick()
            self.assertEqual("closed", self.k.city_visits.get(vid)["state"])
            self.assertTrue(self.adapter.reconcile()["ok"])

    def test_r0_d0_schema_and_forbidden_legacy_tokens_contract(self):
        # Load actual published D0 bridge-v1.schema.json (synced from bot-game-mcp main @ e7b7c32,
        # overridable via CITY_BRIDGE_SCHEMA_PATH for direct cross-repo validation).
        schema_path = os.environ.get("CITY_BRIDGE_SCHEMA_PATH") or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "docs", "city", "bridge-v1.schema.json")
        )
        self.assertTrue(os.path.isfile(schema_path), f"Missing D0 bridge schema: {schema_path}")
        with open(schema_path, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
        jsonschema.Draft202012Validator.check_schema(schema)
        defs = schema["$defs"]
        validators = {}

        def _validator(def_name):
            if def_name not in validators:
                self.assertIn(def_name, defs, f"Missing $defs/{def_name} in {schema_path}")
                validators[def_name] = jsonschema.Draft202012Validator(
                    {"$ref": f"#/$defs/{def_name}", "$defs": defs}
                )
            return validators[def_name]

        def assert_valid(def_name, value, label=None):
            errs = list(_validator(def_name).iter_errors(value))
            if errs:
                detail = "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errs[:5])
                self.fail(f"{label or def_name} failed $defs/{def_name}: {detail}")

        def assert_invalid(def_name, value, label=None):
            self.assertFalse(
                _validator(def_name).is_valid(value),
                f"{label or def_name} unexpectedly passed $defs/{def_name}",
            )

        # 1) Schema metadata & R0 enums vs kernel constants
        self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
        self.assertEqual(schema["x-protocol"], BRIDGE_PROTOCOL)
        self.assertIn(schema["x-mappings-version"], SUPPORTED_MAPPINGS_VERSIONS)
        self.assertIn("city_text_version", defs["Catalog"]["properties"])

        r0_areas = tuple(defs["AreaId"]["enum"])
        self.assertEqual(("sakuraoka", "rurigai", "kazeminato"), r0_areas)
        self.assertEqual(r0_areas, _AREA_ORDER)
        self.assertIn(self.adapter.weather.home_area, r0_areas)
        self.assertEqual("sakuraoka", self.adapter.weather.home_area)
        for area_key, target_place in DEFAULT_CITY_PLACE_TARGETS.items():
            # an alias is an area key or (11-20 hotfix) a life1 "city"-tier place that names a 白帆市 place
            if area_key not in r0_areas:
                self.assertEqual("city", self.k.V.PLACES[area_key]["tier"], area_key)
            assert_valid("PlaceId", target_place, f"DEFAULT_CITY_PLACE_TARGETS[{area_key}]")
            self.assertIn(target_place.split(":", 1)[0], r0_areas)
        self.assertEqual(set(r0_areas) | {"city"}, set(city_projection.AREA_LABELS.keys()))

        # KernelEffectName enum matches city_effects.body_delta
        schema_effects = tuple(defs["KernelEffectName"]["enum"])
        self.assertEqual(("snack", "drink_coffee", "drink_tea", "walk_15m"), schema_effects)
        for eff in schema_effects:
            delta = city_effects.body_delta(self.k.V, eff, quantity=1)
            self.assertIsInstance(delta, dict)
            self.assertTrue(delta, f"Empty body_delta for {eff}")
        with self.assertRaises(Refused) as cm_eff:
            city_effects.body_delta(self.k.V, "caffeine_boost_invented", quantity=1)
        self.assertEqual("UNKNOWN_CITY_EFFECT", cm_eff.exception.reason)

        # VisitState & ErrorCode enums align with kernel state machine and HTTP client
        schema_states = set(defs["VisitState"]["enum"])
        for st in set(city_visits.TRANSITIONS.keys()) - {"preparing"}:
            self.assertIn(st, schema_states)
        schema_error_codes = set(defs["ErrorCode"]["enum"])
        for err_code in _ERROR_CLASSES:
            self.assertIn(err_code, schema_error_codes)

        # 2) Live wire-level validation of real kernel requests & responses against loaded schema
        validated_defs = set()
        base_transport = self.engine.transport

        def validating_transport(method, path, query, body, headers):
            status, resp_headers, raw_text = base_transport(method, path, query, body, headers)
            parsed = json.loads(raw_text) if isinstance(raw_text, str) and raw_text.strip() else raw_text
            if method == "POST" and path == "/visits/prepare":
                assert_valid("PrepareRequest", body, "POST /visits/prepare req")
                validated_defs.add("PrepareRequest")
                if 200 <= status < 300:
                    assert_valid("PrepareReceipt", parsed, "POST /visits/prepare resp")
                    validated_defs.add("PrepareReceipt")
            elif method == "POST" and path.endswith("/commit"):
                assert_valid("CommitRequest", body, f"POST {path} req")
                validated_defs.add("CommitRequest")
                if 200 <= status < 300:
                    assert_valid("CommitReceipt", parsed, f"POST {path} resp")
                    validated_defs.add("CommitReceipt")
            elif method == "POST" and path.endswith("/abort"):
                assert_valid("AbortRequest", body, f"POST {path} req")
                validated_defs.add("AbortRequest")
                if 200 <= status < 300:
                    assert_valid("AbortReceipt", parsed, f"POST {path} resp")
                    validated_defs.add("AbortReceipt")
            elif method == "POST" and path.endswith("/return-request"):
                assert_valid("ReturnRequest", body, f"POST {path} req")
                validated_defs.add("ReturnRequest")
                if 200 <= status < 300:
                    assert_valid("ReturnRequestReceipt", parsed, f"POST {path} resp")
                    validated_defs.add("ReturnRequestReceipt")
            elif method == "POST" and path.endswith("/release"):
                assert_valid("ReleaseRequest", body, f"POST {path} req")
                validated_defs.add("ReleaseRequest")
                if 200 <= status < 300:
                    assert_valid("ReleaseReceipt", parsed, f"POST {path} resp")
                    validated_defs.add("ReleaseReceipt")
            elif method == "POST" and path.endswith("/act"):
                assert_valid("ActRequest", body, f"POST {path} req")
                validated_defs.add("ActRequest")
                if 200 <= status < 300:
                    assert_valid("ActReceipt", parsed, f"POST {path} resp")
                    validated_defs.add("ActReceipt")
            elif method == "GET" and path == "/catalog" and 200 <= status < 300:
                assert_valid("Catalog", parsed, "GET /catalog resp")
                validated_defs.add("Catalog")
            elif method == "GET" and path == "/weather" and 200 <= status < 300:
                assert_valid("WeatherResponse", parsed, "GET /weather resp")
                validated_defs.add("WeatherResponse")
            elif method == "GET" and path == "/reconcile" and 200 <= status < 300:
                assert_valid("ReconcileResponse", parsed, "GET /reconcile resp")
                validated_defs.add("ReconcileResponse")
            elif method == "GET" and path == "/outbox" and 200 <= status < 300:
                assert_valid("OutboxPage", parsed, "GET /outbox resp")
                validated_defs.add("OutboxPage")
                for rec in parsed.get("records", []):
                    assert_valid("OutboxRecord", rec, f"outbox #{rec.get('seq')} {rec.get('kind')}")
                    validated_defs.add(f"Outbox:{rec.get('kind')}")
            elif method == "GET" and path.endswith("/view") and 200 <= status < 300:
                assert_valid("VisitView", parsed, f"GET {path} resp")
                validated_defs.add("VisitView")
            elif method == "GET" and path.startswith("/visits/") and path.count("/") == 2 and 200 <= status < 300:
                assert_valid("VisitDescription", parsed, f"GET {path} resp")
                validated_defs.add("VisitDescription")
            if status >= 400 and isinstance(parsed, dict):
                assert_valid("Error", parsed, f"{method} {path} HTTP {status}")
                validated_defs.add("Error")
            return status, resp_headers, raw_text

        self.client._transport = validating_transport
        self.engine.mappings_version = schema["x-mappings-version"]
        self.engine.request_log.clear()

        # Flow A: full visit with carried snack, in-city actions, encounter, settlement, release, reconcile
        self.city_model.handler = lambda ctx: (
            [
                {"type": "say", "text": "白帆市的下午茶很安静呢。"},
                {"type": "buy", "sku": "coffee", "qty": 1},
                {"type": "consume", "item": "coffee", "qty": 1},
                {"type": "give", "to": "res_bot_b", "item": "cookie", "qty": 1},
                {"type": "go_home"},
            ]
            if ctx["wake"]["reason"] == "arrived"
            else []
        )
        self.adapter.depart(
            "nell",
            "rurigai:kissa-tomoshibi",
            carry_yen=5000,
            carry_items=[{"resource_id": "nell_cookie_1", "qty": 1}],
            auto_snack=False,
            visit_id="vis_d0_full",
        )
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_d0_full")
        self.engine.simulate_arrival_at_destination("vis_d0_full", fare_yen=200)
        self.engine.push_outbox(
            "vis_d0_full",
            "encounter",
            {
                "with": {"name": "外部旅人B"},
                "place": "rurigai:kissa-tomoshibi",
                "place_name": "喫茶「帆」",
                "area": "rurigai",
            },
        )
        self.adapter.poll_outbox_once()
        self.proj.run()
        card_blob = json.dumps(self._cards()["residents"]["nell"]["city_outing"], ensure_ascii=False)
        settlement = self.engine.simulate_return_and_settle("vis_d0_full", return_fare_yen=200)
        assert_valid("Settlement", settlement, "vis_d0_full settlement")
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()
        self.assertTrue(self.adapter.reconcile()["ok"])

        # Flow B: kernel deadline return-request -> ReturnRequest / ReturnRequestReceipt
        self.city_model.handler = lambda ctx: []
        self.adapter.depart("nell", "kazeminato:muginohi", carry_yen=5000, auto_snack=False, duration_s=3600, visit_id="vis_d0_ret")
        self.clock.advance(60)
        self.k.tick()
        self.adapter.advance_handover("vis_d0_ret")
        self.engine.simulate_arrival_at_destination("vis_d0_ret", fare_yen=260)
        self.adapter.poll_outbox_once()
        self.clock.advance(3600)
        self.adapter._enforce_return_deadlines()
        self.engine.simulate_return_and_settle("vis_d0_ret", return_fare_yen=260)
        self.adapter.poll_outbox_once()
        self.clock.advance(60)
        self.k.tick()

        # Flow C: prepare rejected by engine (422) -> Error + kernel AbortRequest / AbortReceipt
        self.adapter.depart("nell", "kazeminato:muginohi", carry_yen=5000, auto_snack=False, visit_id="vis_d0_abrt")
        self.clock.advance(60)
        self.k.tick()
        saved_places = list(self.engine.places)
        self.engine.places = [p for p in saved_places if p["id"] != "kazeminato:muginohi"]
        try:
            aborted_vis = self.adapter.advance_handover("vis_d0_abrt")
        finally:
            self.engine.places = saved_places
        self.assertEqual("aborted", aborted_vis["state"])
        self.clock.advance(60)
        self.k.tick()

        # Verify all 6 POST requests, GET responses, and Error were validated against loaded schema
        for required_def in (
            "Catalog", "WeatherResponse", "ReconcileResponse", "OutboxPage", "VisitView", "VisitDescription",
            "PrepareRequest", "PrepareReceipt", "CommitRequest", "CommitReceipt",
            "AbortRequest", "AbortReceipt", "ReturnRequest", "ReturnRequestReceipt",
            "ReleaseRequest", "ReleaseReceipt", "ActRequest", "ActReceipt", "Error",
            "Outbox:visit_progress", "Outbox:purchase", "Outbox:body_effect",
            "Outbox:encounter", "Outbox:decision_wake", "Outbox:return_started", "Outbox:visit_settled",
        ):
            self.assertIn(required_def, validated_defs, f"Schema validation did not cover {required_def}")

        # Every normalized action type produced by kernel conforms to $defs/Action & $defs/ActRequest
        normalized_samples = normalize_city_actions(
            [
                {"type": "travel", "to": "rurigai:station"},
                {"type": "say", "text": "你好"},
                {"type": "buy", "sku": "coffee", "qty": 2},
                {"type": "consume", "item": "coffee", "qty": 1},
                {"type": "give", "to": "res_bot_b", "item_id": "it_1", "qty": 1},
                {"type": "store_locker", "item_id": "it_2", "qty": 1},
                {"type": "take_locker", "item": "paperback", "qty": 1},
                {"type": "go_home"},
            ],
            max_actions=8,
        )
        self.assertEqual(8, len(normalized_samples))
        for idx, act in enumerate(normalized_samples, start=1):
            assert_valid("Action", act, f"Action[{act['type']}]")
            act_env = {"request_id": f"visit:vis_d0_act:d1:a{idx}", "domain_epoch": 1, "action": act}
            act_env["payload_hash"] = bridge_payload_hash(act_env)
            assert_valid("ActRequest", act_env, f"ActRequest[{act['type']}]")

        # Kernel fallback weather for every schema AreaId conforms to $defs/WeatherSnapshot
        fallback_snaps = [city_fallback_weather(int(self.clock.now() * 1000), a) for a in r0_areas]
        for snap in fallback_snaps:
            assert_valid("WeatherSnapshot", snap, f"fallback_weather({snap['area']})")

        # 3) Negative counter-examples prove loaded schema rejects legacy areas & invalid wire payloads
        for removed in (
            "oldtown", "ikebukuro", "shinjuku", "asakusa", "shibuya",
            "kichijoji", "jinbocho", "shimokitazawa", "tokyo",
        ):
            self.assertNotIn(removed, _AREA_ORDER)
            self.assertNotIn(removed, city_projection.AREA_LABELS)
            assert_invalid("AreaId", removed, f"legacy AreaId {removed}")
        assert_invalid("WeatherSnapshot", dict(fallback_snaps[0], area="oldtown"), "legacy weather area")

        prep_body = next(b for m, p, _q, b in self.engine.request_log if m == "POST" and p == "/visits/prepare")
        assert_invalid("PrepareRequest", dict(prep_body, mappings_version=2), "PrepareRequest mappings_version=2")
        assert_invalid("PrepareRequest", dict(prep_body, mappings_version=4), "PrepareRequest mappings_version=4")
        assert_invalid("PrepareRequest", dict(prep_body, protocol="city-bridge/2"), "PrepareRequest bad protocol")
        assert_invalid("PrepareRequest", dict(prep_body, request_id="short"), "PrepareRequest short request_id")
        assert_invalid("PrepareRequest", dict(prep_body, carry={"yen": -1, "items": []}), "PrepareRequest neg yen")
        assert_invalid("CommitRequest", {"request_id": "visit:x:commit", "transfer_id": "tr:vis_1:1", "domain_epoch": 0})
        assert_invalid("ReleaseRequest", {"request_id": "visit:x:release", "domain_epoch": 2})
        assert_invalid("ActRequest", {"request_id": "visit:x:d1:a1", "domain_epoch": 1, "action": {"type": "dance"}})
        assert_invalid(
            "BodyEffectPayload",
            {"actor_id": "nell", "effect_id": "v:e1", "engine_kind": "drank", "kernel_effect": "caffeine_boost"},
        )

        # 4) LiveCityDecisionModel prompt & forbidden legacy token zero-hit probe across kernel outputs
        captured = {}
        def fake_chat(system, user):
            captured["system"] = system
            captured["user"] = user
            return '{"actions": []}', 10
        live_model = LiveCityDecisionModel(fake_chat, selves={"nell": "奈尔"})
        live_model({"actor": "nell", "wake": {"reason": "arrived"}, "view": {"world_id": "city"}})
        self.assertIn("在白帆市外出", captured["system"])

        weather_blob = json.dumps(fallback_snaps, ensure_ascii=False)
        redacted_blob = city_projection.redact_text(
            "sakuraoka:ai-house:entrance / oldtown:ai-house:entrance / 樱丘西2-13-4 101号室 / 某都某区旧町西2-13-4"
        )
        combined = "\n".join([captured["system"], card_blob, weather_blob, redacted_blob]).lower()
        forbidden_tokens = (
            "tokyo", "asia/tokyo", "oldtown", "ikebukuro", "shinjuku", "arakawa",
            "jinbocho", "shimokitazawa", "asakusa", "shibuya", "kichijoji",
            "mun:13", "jreast", "saikyo", "yamanote", "shonan-shinjuku",
            "oldtown:ai-house:entrance", "sakuraoka:ai-house:entrance",
            "东京", "旧町", "池袋", "新宿", "旧川", "神保町", "下北泽", "下北沢",
            "浅草", "涩谷", "渋谷", "吉祥寺", "某区", "丰岛区", "新宿区",
            "千代田区", "世田谷区", "埼京线", "山手线", "湘南新宿线", "jr 东日本",
        )
        for tok in forbidden_tokens:
            self.assertNotIn(tok.lower(), combined, f"Forbidden legacy token leaked: {tok}")

    def test_hotfix_life1_city_tier_is_qingkong_and_routes_through_the_bridge(self):
        """11-20 hotfix: life1's "city" tier names 白帆市 places, every one is a bridge alias, the free-time context
        lists them, and an ordinary invite to one (place:muginohi) becomes a city visit that pays and settles."""
        city_tier = {k for k, p in self.k.V.PLACES.items() if p["tier"] == "city"}
        self.assertEqual({"kissa_tomoshibi", "shiorido", "minami_park", "muginohi", "southern_terrace", "kamitsuki"}, city_tier)
        for k in city_tier:
            target = DEFAULT_CITY_PLACE_TARGETS[k]
            self.assertEqual(target, self.adapter._plan_city_destination({"location": "place:" + k},
                                                                          require_place_prefix_for_alias=True))
            self.assertFalse(any(bad in self.k.V.PLACES[k]["label"] for bad in ("吉祥寺", "新宿", "秋叶原", "上野", "立川", "下北泽")))
        self.assertFalse(any("镇" in p["label"] for p in self.k.V.PLACES.values()))
        self.adapter.enable_actor("elsa")
        ctx = self.adapter.context("nell", "free_time")
        for k in city_tier:
            self.assertIn(k, ctx["places"])
        self.city_model.handler = lambda ctx: [{"type": "go_home"}] if ctx["wake"]["reason"] == "arrived" else []
        self.clock.advance(80 * 60)
        with self.s.tx() as db:
            for a in ("nell", "elsa"):
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
        wallet0 = {a: self.adapter.wallet.available(a) for a in ("nell", "elsa")}
        now0 = self.clock.now()

        def _sub(key, actor, action_type, args):
            return self.k.submit({"idempotency_key": key, "principal": f"channel:{actor}", "actor_id": actor,
                                  "origin": "self", "domain_epoch": self.k.city_visits.authority(actor)["domain_epoch"],
                                  "action_type": action_type, "args": args})

        inv = _sub("hotfix:inv", "nell", "invite", {"to": ["elsa"], "kind": "outing", "what": "shopping",
                                                    "location": "place:muginohi", "start_at": now0 + 600,
                                                    "end_at": now0 + 5400})
        self.assertEqual("succeeded", inv["status"], inv)
        pid = inv["plan_id"]
        self.assertTrue(self.adapter.claims_plan(self.life.plans.plan(pid)))
        inv_id = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid)
        self.assertEqual("succeeded", _sub("hotfix:yes", "elsa", "respond_invite", {"inv_id": inv_id, "answer": "yes"})["status"])
        self.adapter._outings.shopping.targets = {}
        self.clock.advance(600)
        self.life.step()
        if self.k.city_visits.current("nell") is None or self.k.city_visits.current("elsa") is None:
            self.clock.advance(60)
            self.life.step()
        visits = {a: self.k.city_visits.current(a) for a in ("nell", "elsa")}
        for a, v in visits.items():
            self.assertIsNotNone(v, a)
            self.assertEqual("kazeminato:muginohi", v["plan"]["destination"])
            self.assertIsNone(self.life.get(f"journey:{pid}:{a}"))
        self.clock.advance(60)
        self.life.step()
        for v in visits.values():
            self.engine.simulate_arrival_at_destination(v["visit_id"], fare_yen=260)
        self.life.step()
        for v in visits.values():
            self.engine.simulate_return_and_settle(v["visit_id"], return_fare_yen=260)
        self.life.step()
        self.clock.advance(60)
        self.life.step()
        for a in ("nell", "elsa"):
            self.assertIsNone(self.k.city_visits.current(a), a)
            self.assertEqual(wallet0[a] - 520, self.adapter.wallet.available(a), a)
            self.assertNotEqual("city", self.k.city_visits.authority(a)["domain"], a)

    def test_k5_normal_invite_confirm_routes_toto_destinations_and_preserves_legacy_outings(self):
        """Regression: supported Toto destinations ('place:rurigai', 'place:kazeminato',
        and explicit public catalog places) pass normal Kernel.submit(invite/respond_invite) without
        raw SQL plan inserts and trigger CityBridgeAdapter handover, while 4c distant real-world
        places (since the 11-20 hotfix a kernel-only place, 'place:bookshop') remain on legacy Outings and old IDs
        never re-enter the city contract."""
        self.adapter.enable_actor("elsa")
        self.city_model.handler = lambda ctx: [{"type": "go_home"}] if ctx["wake"]["reason"] == "arrived" else []

        def _invite(key, actor, to, loc, start_at, end_at, kind="outing", what="cafe_time", plan_id=None):
            args = {
                "to": to,
                "kind": kind,
                "what": what,
                "location": loc,
                "start_at": start_at,
                "end_at": end_at,
            }
            if plan_id is not None:
                args = {"to": to, "plan_id": plan_id}
            return self.k.submit(
                {
                    "idempotency_key": key,
                    "principal": f"channel:{actor}",
                    "actor_id": actor,
                    "origin": "self",
                    "domain_epoch": self.k.city_visits.authority(actor)["domain_epoch"],
                    "action_type": "invite",
                    "args": args,
                }
            )

        def _respond(key, actor, inv_id, answer="yes"):
            return self.k.submit(
                {
                    "idempotency_key": key,
                    "principal": f"channel:{actor}",
                    "actor_id": actor,
                    "origin": "self",
                    "domain_epoch": self.k.city_visits.authority(actor)["domain_epoch"],
                    "action_type": "respond_invite",
                    "args": {"inv_id": inv_id, "answer": answer},
                }
            )

        # 0) Cold-boot explicit catalog place ('place:rurigai:shiorido') BEFORE any depart(),
        #    starting from bedrooms AND with low kitchen stock (Shopping must NOT hijack confirmed members!)
        #    Advance past lunch grace (14:20 JST) so Shopping.step() is actively looking for an errand runner.
        self.clock.advance(80 * 60)
        with self.s.tx() as db:
            self.k._set_actor(db, "nell", location=self.k.V.bed("nell"), activity="idle", action_id=None)
            self.k._set_actor(db, "elsa", location=self.k.V.bed("elsa"), activity="idle", action_id=None)
            self.k._set_actor(db, "alma", location="living", activity="idle", action_id=None)
            db.execute("UPDATE resources SET qty=1 WHERE resource_id='egg'")
        now0 = self.clock.now()
        inv_cold = _invite("k5:cold:inv", "nell", ["elsa"], "place:rurigai:shiorido", now0 + 600, now0 + 5400, what="shopping")
        self.assertEqual("succeeded", inv_cold["status"], inv_cold)
        pid_cold = inv_cold["plan_id"]
        inv_cold_id = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid_cold)
        self.assertEqual("succeeded", _respond("k5:cold:yes", "elsa", inv_cold_id, "yes")["status"])
        self.assertEqual("confirmed", self.life.plans.plan(pid_cold)["state"])
        self.assertEqual(now0 + 600, self.adapter.hold_until("nell", now0))

        # At start_at, both walk from bedrooms -> living, then depart into Toto on the next minute
        self.clock.advance(600)
        self.life.step()
        shop_cur = self.life.get("shopping:current")
        if shop_cur is not None:
            self.assertNotIn(shop_cur["actor"], ("nell", "elsa"))
        if self.k.city_visits.current("nell") is None or self.k.city_visits.current("elsa") is None:
            self.clock.advance(60)
            self.life.step()
        self.assertIsNone(self.life.get(f"journey:{pid_cold}:nell"))
        self.assertIsNone(self.life.get(f"journey:{pid_cold}:elsa"))
        v_cold_nell = self.k.city_visits.current("nell")
        v_cold_elsa = self.k.city_visits.current("elsa")
        self.assertIsNotNone(v_cold_nell)
        self.assertIsNotNone(v_cold_elsa)
        self.assertEqual("rurigai:shiorido", v_cold_nell["plan"]["destination"])
        self.assertEqual("rurigai:shiorido", v_cold_elsa["plan"]["destination"])
        # Once departed, status card 'next' no longer lists the already-started city plan
        self.proj.run()
        self.assertFalse(any("琉璃街" in x for x in self._cards()["residents"]["nell"]["next"]))
        self.adapter._outings.shopping.targets = {}
        self.clock.advance(60)
        self.life.step()
        self.engine.simulate_arrival_at_destination(v_cold_nell["visit_id"], fare_yen=200)
        self.engine.simulate_arrival_at_destination(v_cold_elsa["visit_id"], fare_yen=200)
        self.life.step()
        self.engine.simulate_return_and_settle(v_cold_nell["visit_id"], return_fare_yen=200)
        self.engine.simulate_return_and_settle(v_cold_elsa["visit_id"], return_fare_yen=200)
        self.life.step()
        self.clock.advance(60)
        self.life.step()
        self.assertEqual("done", self.life.plans.plan(pid_cold)["state"])
        self.clock.advance(300)

        # 1) Both DEFAULT_CITY_PLACE_TARGETS ('place:rurigai', 'place:kazeminato') pass invite -> respond_invite -> CityBridgeAdapter
        for idx, (loc, expected_dest, expected_area_label, fare) in enumerate(
            (
                ("place:rurigai", "rurigai:kissa-tomoshibi", "琉璃街", 200),
                ("place:kazeminato", "kazeminato:muginohi", "风港", 260),
            ),
            start=1,
        ):
            with self.s.tx() as db:
                for a in ROSTER:
                    self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
            now = self.clock.now()
            inv = _invite(f"k5:inv:{idx}", "nell", ["elsa"], loc, now + 600, now + 7200)
            self.assertEqual("succeeded", inv["status"], (loc, inv))
            pid = inv["plan_id"]
            pending = [p for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid]
            self.assertEqual(1, len(pending))
            ans = _respond(f"k5:yes:{idx}", "elsa", pending[0]["inv_id"], "yes")
            self.assertEqual("succeeded", ans["status"], (loc, ans))
            self.assertEqual("confirmed", self.life.plans.plan(pid)["state"])

            # Status card 'next' uses localized Toto area label (never raw 'place:rurigai' / 'place:kazeminato')
            self.proj.run()
            nxt_text = " ".join(self._cards()["residents"]["nell"]["next"])
            self.assertIn(expected_area_label, nxt_text)
            self.assertNotIn(loc, nxt_text)

            # Overlapping city invite while confirmed is refused with outing_schedule_conflict
            overlap = _invite(f"k5:overlap:{idx}", "nell", ["elsa"], "place:kazeminato", now + 1200, now + 4800)
            self.assertEqual("failed", overlap["status"])
            self.assertEqual("outing_schedule_conflict", overlap["reason"])

            # Advance to start_at and run full city lifecycle via Life.step()
            self.clock.advance(600)
            self.life.step()
            if self.k.city_visits.current("nell") is None or self.k.city_visits.current("elsa") is None:
                self.clock.advance(60)
                self.life.step()
            self.assertIsNone(self.life.get(f"journey:{pid}:nell"))
            self.assertIsNone(self.life.get(f"journey:{pid}:elsa"))
            v_nell = self.k.city_visits.current("nell")
            v_elsa = self.k.city_visits.current("elsa")
            self.assertIsNotNone(v_nell)
            self.assertIsNotNone(v_elsa)
            self.assertEqual(expected_dest, v_nell["plan"]["destination"])
            self.assertEqual(expected_dest, v_elsa["plan"]["destination"])

            self.clock.advance(60)
            self.life.step()
            self.assertEqual("active", self.k.city_visits.get(v_nell["visit_id"])["state"])
            self.assertEqual("active", self.k.city_visits.get(v_elsa["visit_id"])["state"])
            self.engine.simulate_arrival_at_destination(v_nell["visit_id"], fare_yen=fare)
            self.engine.simulate_arrival_at_destination(v_elsa["visit_id"], fare_yen=fare)
            self.life.step()
            # Settle nell first while elsa is still out -> nell is no longer blocked by Trips.validate on later plans
            self.engine.simulate_return_and_settle(v_nell["visit_id"], return_fare_yen=fare)
            self.life.step()
            self.clock.advance(60)
            self.life.step()
            self.assertEqual("closed", self.k.city_visits.get(v_nell["visit_id"])["state"])
            self.assertEqual("running", self.life.plans.plan(pid)["state"])
            self.engine.simulate_return_and_settle(v_elsa["visit_id"], return_fare_yen=fare)
            self.life.step()
            self.clock.advance(60)
            self.life.step()
            self.assertEqual("closed", self.k.city_visits.get(v_elsa["visit_id"])["state"])
            self.assertEqual("done", self.life.plans.plan(pid)["state"])
            self.assertTrue(self.adapter.reconcile()["ok"])
            self.clock.advance(300)

        # 2) free_time context & guardrails: actor gating, cross-life-day, hidden entrance / legacy / bogus rejections
        nell_places = self.life.life_context("nell", "free_time")["places"]
        self.assertEqual("琉璃街", nell_places.get("rurigai"))
        self.assertEqual("风港", nell_places.get("kazeminato"))
        alma_places = self.life.life_context("alma", "free_time")["places"]
        self.assertNotIn("rurigai", alma_places)
        self.assertNotIn("kazeminato", alma_places)

        now = self.clock.now()
        r_non_allowed_invitee = _invite("k5:gate:invitee", "nell", ["alma"], "place:rurigai", now + 600, now + 5400)
        self.assertEqual("failed", r_non_allowed_invitee["status"])
        self.assertEqual("actor_not_enabled_for_city", r_non_allowed_invitee["reason"])

        r_non_allowed_host = _invite("k5:gate:host", "alma", ["nell"], "place:kazeminato", now + 600, now + 5400)
        self.assertEqual("failed", r_non_allowed_host["status"])
        self.assertEqual("actor_not_enabled_for_city", r_non_allowed_host["reason"])

        # Follow-up invite on an open city plan to a non-enabled roommate is refused on respond_invite('yes')
        r_open = _invite("k5:gate:open", "nell", ["elsa"], "place:rurigai", now + 3600, now + 7200)
        self.assertEqual("succeeded", r_open["status"])
        r_add_alma = _invite("k5:gate:add_alma", "nell", ["alma"], None, None, None, plan_id=r_open["plan_id"])
        if r_add_alma["status"] == "succeeded":
            alma_inv = next(p["inv_id"] for p in self.life.plans.pending_for("alma") if p["plan_id"] == r_open["plan_id"])
            r_alma_yes = _respond("k5:gate:alma_yes", "alma", alma_inv, "yes")
            self.assertEqual("failed", r_alma_yes["status"])
            self.assertEqual("actor_not_enabled_for_city", r_alma_yes["reason"])
        self.life.plans.set_state(r_open["plan_id"], "cancelled", "cleanup")

        r_overnight = _invite("k5:gate:overnight", "nell", ["elsa"], "place:rurigai", now + 600, now + 26 * 3600)
        self.assertEqual("failed", r_overnight["status"])
        self.assertEqual("CITY_RETURN_MUST_BE_SAME_LIFE_DAY", r_overnight["reason"])

        for bad_idx, bad_loc in enumerate(
            (
                "place:oldtown",
                "place:ikebukuro",
                "place:tokyo",
                "place:sakuraoka:ai-house:entrance",
                "sakuraoka:ai-house:entrance",
                "place:oldtown:ai-house:entrance",
                "place:foo:bar",
                "rurigai",
            )
        ):
            r_bad = _invite(f"k5:bad:{bad_idx}", "nell", ["elsa"], bad_loc, now + 600, now + 5400)
            self.assertEqual("failed", r_bad["status"], (bad_loc, r_bad))
            self.assertEqual("unknown_location", r_bad["reason"], (bad_loc, r_bad))

        r_bad_kind = _invite("k5:badkind", "nell", ["elsa"], "place:rurigai", now + 600, now + 5400, kind="activity", what="reading")
        self.assertEqual("failed", r_bad_kind["status"])
        self.assertEqual("unknown_location", r_bad_kind["reason"])

        self.adapter.intercept_plans = False
        try:
            for idx_off, loc_off in enumerate(("place:rurigai", "place:kazeminato")):
                r_off = _invite(f"k5:off:{idx_off}", "nell", ["elsa"], loc_off, now + 600, now + 5400)
                self.assertEqual("failed", r_off["status"])
                self.assertEqual("unknown_location", r_off["reason"])
        finally:
            self.adapter.intercept_plans = True

        # 3) 4c kernel-only place ('place:bookshop'; 11-20 the real-world city tier became 白帆市) stays on legacy Outings (NOT intercepted into city contract)
        next_day_13 = dt.datetime.fromtimestamp(now, JST).replace(hour=13, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        self.clock.advance(max(0, next_day_13.timestamp() - 600 - self.clock.now()))
        with self.s.tx() as db:
            for a in ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
        t_start = self.clock.now() + 600
        inv_bookshop = _invite("k5:bookshop:inv", "nell", ["elsa"], "place:bookshop", t_start, t_start + 5 * 3600, what="shopping")
        self.assertEqual("succeeded", inv_bookshop["status"], inv_bookshop)
        pid_bookshop = inv_bookshop["plan_id"]
        inv_bookshop_id = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid_bookshop)
        self.assertEqual("succeeded", _respond("k5:bookshop:yes", "elsa", inv_bookshop_id, "yes")["status"])
        self.assertFalse(self.adapter.claims_plan(self.life.plans.plan(pid_bookshop)))
        visits_before = len(self.engine.visits)
        self.clock.advance(600)
        self.life.step()
        self.assertEqual(visits_before, len(self.engine.visits))
        self.assertIsNone(self.k.city_visits.current("nell"))
        self.assertIsNone(self.k.city_visits.current("elsa"))
        self.assertEqual("running", self.life.get(f"journey:{pid_bookshop}:nell"))
        self.assertEqual("running", self.life.get(f"journey:{pid_bookshop}:elsa"))

    def test_k5_omitted_end_at_unifies_2h_city_window_and_preserves_4h_legacy_outings(self):
        """Regression for review item 1: city plans without end_at consistently reserve and execute
        a 2h (7200s) window across invite, respond_invite, departure, and legacy Outings conflict checks,
        while legacy Outings plans without end_at retain their 4h (14400s) window."""
        self.adapter.enable_actor("elsa")
        self.city_model.handler = lambda ctx: [{"type": "go_home"}] if ctx["wake"]["reason"] == "arrived" else []

        def _invite_no_end(key, actor, to, loc, start_at, *, end_at=None, kind="outing", what="cafe_time"):
            args = {
                "to": to,
                "kind": kind,
                "what": what,
                "location": loc,
                "start_at": start_at,
            }
            if end_at is not None:
                args["end_at"] = end_at
            return self.k.submit(
                {
                    "idempotency_key": key,
                    "principal": f"channel:{actor}",
                    "actor_id": actor,
                    "origin": "self",
                    "domain_epoch": self.k.city_visits.authority(actor)["domain_epoch"],
                    "action_type": "invite",
                    "args": args,
                }
            )

        def _respond(key, actor, inv_id, answer="yes"):
            return self.k.submit(
                {
                    "idempotency_key": key,
                    "principal": f"channel:{actor}",
                    "actor_id": actor,
                    "origin": "self",
                    "domain_epoch": self.k.city_visits.authority(actor)["domain_epoch"],
                    "action_type": "respond_invite",
                    "args": {"inv_id": inv_id, "answer": answer},
                }
            )

        with self.s.tx() as db:
            for a in ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)

        # 1) Confirm first city trip to Rurigai omitting end_at (effective 2h window: [t1, t1 + 7200))
        now = self.clock.now()
        t1 = now + 600
        r1 = _invite_no_end("k5:noend:inv1", "nell", ["elsa"], "place:rurigai", t1)
        self.assertEqual("succeeded", r1["status"], r1)
        pid1 = r1["plan_id"]
        self.assertIsNone(self.life.plans.plan(pid1)["end_at"])
        inv1 = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid1)
        self.assertEqual("succeeded", _respond("k5:noend:yes1", "elsa", inv1)["status"])
        self.assertEqual("confirmed", self.life.plans.plan(pid1)["state"])

        # Genuinely overlapping invite (+1h, inside 2h window) is still refused
        r_overlap = _invite_no_end("k5:noend:overlap1h", "nell", ["elsa"], "place:kazeminato", t1 + 3600)
        self.assertEqual("failed", r_overlap["status"])
        self.assertEqual("outing_schedule_conflict", r_overlap["reason"])

        # Non-overlapping Kazeminato invite ~3h after t1 (1h gap after t1 + 7200) MUST succeed via normal entry!
        t2 = t1 + 3 * 3600
        r2 = _invite_no_end("k5:noend:inv2", "nell", ["elsa"], "place:kazeminato", t2, what="shopping")
        self.assertEqual("succeeded", r2["status"], r2)
        pid2 = r2["plan_id"]
        inv2 = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid2)
        ans2 = _respond("k5:noend:yes2", "elsa", inv2)
        self.assertEqual("succeeded", ans2["status"], ans2)
        self.assertEqual("confirmed", self.life.plans.plan(pid2)["state"])

        # Also verify a legacy Outing proposed after a 2h city plan (+3h on another pair) is NOT blocked by 4h!
        self.adapter.enable_actor("yuki")
        r_city_xl = _invite_no_end("k5:noend:xl_city", "yuki", ["elsa"], "place:rurigai", t1 + 6 * 3600)
        self.assertEqual("succeeded", r_city_xl["status"], r_city_xl)
        pid_xl_city = r_city_xl["plan_id"]
        inv_xl_city = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid_xl_city)
        self.assertEqual("succeeded", _respond("k5:noend:xl_yes", "elsa", inv_xl_city)["status"])
        r_leg_after_city = _invite_no_end(
            "k5:noend:leg_after_city", "yuki", ["alma"], "place:bookshop", t1 + 9 * 3600, what="shopping"
        )
        self.assertEqual("succeeded", r_leg_after_city["status"], r_leg_after_city)
        self.life.plans.set_state(r_leg_after_city["plan_id"], "cancelled", "cleanup")
        self.life.plans.set_state(pid_xl_city, "cancelled", "cleanup")

        # Execute first trip (Rurigai) at t1 -> return_by_ms is t1 + 7200s, settles cleanly
        self.clock.advance(t1 - self.clock.now())
        self.life.step()
        if self.k.city_visits.current("nell") is None or self.k.city_visits.current("elsa") is None:
            self.clock.advance(60)
            self.life.step()
        v1_nell = self.k.city_visits.current("nell")
        v1_elsa = self.k.city_visits.current("elsa")
        self.assertIsNotNone(v1_nell)
        self.assertIsNotNone(v1_elsa)
        self.assertEqual(int((t1 + 7200) * 1000), v1_nell["plan"]["return_by_ms"])
        self.clock.advance(60)
        self.life.step()
        self.engine.simulate_arrival_at_destination(v1_nell["visit_id"], fare_yen=200)
        self.engine.simulate_arrival_at_destination(v1_elsa["visit_id"], fare_yen=200)
        self.life.step()
        self.engine.simulate_return_and_settle(v1_nell["visit_id"], return_fare_yen=200)
        self.engine.simulate_return_and_settle(v1_elsa["visit_id"], return_fare_yen=200)
        self.life.step()
        self.clock.advance(60)
        self.life.step()
        self.assertEqual("done", self.life.plans.plan(pid1)["state"])

        # Execute second trip (Kazeminato) at t2 -> return_by_ms is t2 + 7200s, settles cleanly
        self.clock.advance(t2 - self.clock.now())
        self.life.step()
        if self.k.city_visits.current("nell") is None or self.k.city_visits.current("elsa") is None:
            self.clock.advance(60)
            self.life.step()
        v2_nell = self.k.city_visits.current("nell")
        v2_elsa = self.k.city_visits.current("elsa")
        self.assertIsNotNone(v2_nell)
        self.assertIsNotNone(v2_elsa)
        self.assertEqual(int((t2 + 7200) * 1000), v2_nell["plan"]["return_by_ms"])
        self.clock.advance(60)
        self.life.step()
        self.engine.simulate_arrival_at_destination(v2_nell["visit_id"], fare_yen=260)
        self.engine.simulate_arrival_at_destination(v2_elsa["visit_id"], fare_yen=260)
        self.life.step()
        self.engine.simulate_return_and_settle(v2_nell["visit_id"], return_fare_yen=260)
        self.engine.simulate_return_and_settle(v2_elsa["visit_id"], return_fare_yen=260)
        self.life.step()
        self.clock.advance(60)
        self.life.step()
        self.assertEqual("done", self.life.plans.plan(pid2)["state"])

        # 2) Legacy Outings plans without end_at preserve their 4h (14400s) rule against both legacy & city invites
        next_day_11 = dt.datetime.fromtimestamp(self.clock.now(), JST).replace(
            hour=11, minute=0, second=0, microsecond=0
        ) + dt.timedelta(days=1)
        self.clock.advance(max(0, next_day_11.timestamp() - 600 - self.clock.now()))
        with self.s.tx() as db:
            for a in ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
        t_leg = self.clock.now() + 600
        r_leg = _invite_no_end("k5:noend:leg1", "nell", ["elsa"], "place:bookshop", t_leg, what="shopping")
        self.assertEqual("succeeded", r_leg["status"], r_leg)
        pid_leg = r_leg["plan_id"]
        inv_leg = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid_leg)
        self.assertEqual("succeeded", _respond("k5:noend:leg1_yes", "elsa", inv_leg)["status"])

        # +3h after legacy Outing is STILL inside its 4h window -> refused for both legacy and city!
        r_leg_conflict = _invite_no_end("k5:noend:leg_conf", "nell", ["elsa"], "place:bookshop", t_leg + 3 * 3600, what="shopping")
        self.assertEqual("failed", r_leg_conflict["status"])
        self.assertEqual("outing_schedule_conflict", r_leg_conflict["reason"])
        r_city_conflict = _invite_no_end("k5:noend:city_conf", "nell", ["elsa"], "place:rurigai", t_leg + 3 * 3600)
        self.assertEqual("failed", r_city_conflict["status"])
        self.assertEqual("outing_schedule_conflict", r_city_conflict["reason"])

        # +4h10m after legacy Outing is outside its 4h window -> city invite succeeds!
        r_city_after_4h = _invite_no_end("k5:noend:city_ok4h", "nell", ["elsa"], "place:rurigai", t_leg + 4 * 3600 + 600)
        self.assertEqual("succeeded", r_city_after_4h["status"], r_city_after_4h)

    def test_catalog_timeout_cold_boot_rate_limit_and_post_recovery_public_place_invite(self):
        """Regression for review item 2: /catalog timeout does not trigger synchronous retries on
        CityBridgeAdapter construction or double-poll on every Life.step(); area aliases still work while
        /catalog is down, explicit public catalog places fail fast without retries while unreachable, and
        automatically become invitable once /catalog recovers."""
        self.s.release()
        self.s.close()
        self.engine.catalog_offline = True
        self.engine.request_log.clear()
        sleep_calls = []

        self._boot_seq += 1
        self.s = Store(os.path.join(self.tmp.name, "cold_timeout.db"), self.clock, synthetic=True, seed=27800)
        self.s.acquire()
        self.client = CityBridgeHttpClient(
            "http://127.0.0.1:17700/city/bridge/v1",
            self.engine.bridge_key,
            sleep_fn=sleep_calls.append,
            transport=self.engine.transport,
        )
        self.life = Life(self.s, extensions=[outings.install])
        self.k = self.life.k
        self.city_model = DeterministicCityModel()
        self.adapter = CityBridgeAdapter(
            self.life,
            self.client,
            city_model=self.city_model,
            allowed_actors=("nell", "elsa"),
            intercept_plans=True,
        )
        self.life.extensions.append(self.adapter)

        def _catalog_reqs():
            return sum(1 for m, p, _, _ in self.engine.request_log if m == "GET" and p == "/catalog")

        # 1) Cold-start construction makes 0 /catalog requests; two empty Life.step()s make at most 1 non-retrying probe!
        c0 = _catalog_reqs()
        self.life.step()
        c1 = _catalog_reqs()
        self.clock.advance(60)
        self.life.step()
        c2 = _catalog_reqs()
        self.assertEqual((0, 1, 1), (c0, c1, c2), f"Expected (0, 1, 1) /catalog probes, got {(c0, c1, c2)}")
        self.assertEqual([], sleep_calls, "Catalog warm probe must not trigger synchronous backoff sleeps")

        # 2) While /catalog is unreachable: area alias ('place:rurigai') succeeds with 0 extra /catalog calls,
        #    and an explicit public place that is no life1 target ('place:rurigai:suzuran-closet') fails fast within cooldown with 0 extra calls!
        now = self.clock.now()
        r_alias = self.k.submit(
            {
                "idempotency_key": "cat:down:alias",
                "principal": "channel:nell",
                "actor_id": "nell",
                "origin": "self",
                "action_type": "invite",
                "args": {"to": ["elsa"], "kind": "outing", "what": "cafe_time", "location": "place:rurigai", "start_at": now + 3600},
            }
        )
        self.assertEqual("succeeded", r_alias["status"], r_alias)
        self.life.plans.set_state(r_alias["plan_id"], "cancelled", "cleanup")
        self.assertEqual(1, _catalog_reqs())

        r_explicit_down = self.k.submit(
            {
                "idempotency_key": "cat:down:suzuran",
                "principal": "channel:nell",
                "actor_id": "nell",
                "origin": "self",
                "action_type": "invite",
                "args": {
                    "to": ["elsa"],
                    "kind": "outing",
                    "what": "shopping",
                    "location": "place:rurigai:suzuran-closet",
                    "start_at": now + 3600,
                },
            }
        )
        self.assertEqual("failed", r_explicit_down["status"])
        self.assertEqual("unknown_location", r_explicit_down["reason"])
        self.assertEqual(1, _catalog_reqs())

        # 3) Full outage (outbox + /catalog both down) skips /catalog completely while /outbox is down,
        #    then recovers immediately on the first Life.step() after engine comes back!
        self.engine.outbox_503_count = 5
        self.clock.advance(300)
        self.life.step()
        self.assertEqual(1, _catalog_reqs())  # /outbox failed -> 0 extra /catalog calls!
        self.engine.catalog_offline = False
        self.clock.advance(10)
        self.life.step()
        self.assertEqual(2, _catalog_reqs())  # Immediate recovery on /outbox recovery!

        # 4) Explicit public place ('place:rurigai:shiorido') is now invitable via normal invite -> respond_invite!
        now_rec = self.clock.now()
        r_explicit_up = self.k.submit(
            {
                "idempotency_key": "cat:up:shiorido",
                "principal": "channel:nell",
                "actor_id": "nell",
                "origin": "self",
                "action_type": "invite",
                "args": {
                    "to": ["elsa"],
                    "kind": "outing",
                    "what": "shopping",
                    "location": "place:rurigai:shiorido",
                    "start_at": now_rec + 600,
                },
            }
        )
        self.assertEqual("succeeded", r_explicit_up["status"], r_explicit_up)
        self.assertEqual(2, _catalog_reqs())
        pid_up = r_explicit_up["plan_id"]
        inv_up = next(p["inv_id"] for p in self.life.plans.pending_for("elsa") if p["plan_id"] == pid_up)
        r_yes = self.k.submit(
            {
                "idempotency_key": "cat:up:yes",
                "principal": "channel:elsa",
                "actor_id": "elsa",
                "origin": "self",
                "action_type": "respond_invite",
                "args": {"inv_id": inv_up, "answer": "yes"},
            }
        )
        self.assertEqual("succeeded", r_yes["status"], r_yes)
        self.assertEqual("confirmed", self.life.plans.plan(pid_up)["state"])
        self.life.step()
        self.assertEqual(2, _catalog_reqs())

        # 5) Also verify direct invite after /catalog-only timeout cooldown expires warms on demand without Life.step()!
        self.adapter._places_by_id.clear()
        self.adapter._catalog_cache = None
        self.engine.catalog_offline = True
        self.clock.advance(300)
        r_probe_fail = self.k.submit(
            {
                "idempotency_key": "cat:direct:fail",
                "principal": "channel:nell",
                "actor_id": "nell",
                "origin": "self",
                "action_type": "invite",
                "args": {"to": ["elsa"], "kind": "outing", "what": "shopping", "location": "place:rurigai:shiorido", "start_at": self.clock.now() + 3600},
            }
        )
        self.assertEqual("failed", r_probe_fail["status"])
        self.assertEqual(3, _catalog_reqs())
        self.engine.catalog_offline = False
        self.clock.advance(300)
        r_probe_ok = self.k.submit(
            {
                "idempotency_key": "cat:direct:ok",
                "principal": "channel:nell",
                "actor_id": "nell",
                "origin": "self",
                "action_type": "invite",
                "args": {"to": ["elsa"], "kind": "outing", "what": "shopping", "location": "place:rurigai:shiorido", "start_at": self.clock.now() + 7200},
            }
        )
        self.assertEqual("succeeded", r_probe_ok["status"], r_probe_ok)
        self.assertEqual(4, _catalog_reqs())

        # 6) Verify real _raw_http socket timeout cap (<= 1.5s, never 10.0s) and 0 retries on 503 RECOVERING
        raw_timeouts = []
        raw_sleeps = []

        def _fake_raw_http(method, url, headers, raw_body, timeout_s):
            raw_timeouts.append((method, url, timeout_s))
            if len(raw_timeouts) == 1:
                raise CityTransportError("simulated socket timeout")
            return 503, {}, json.dumps({"error": "RECOVERING", "message": "booting"})

        raw_client = CityBridgeHttpClient(
            "http://127.0.0.1:17700/city/bridge/v1",
            self.engine.bridge_key,
            timeout_s=10.0,
            max_retries=4,
            sleep_fn=raw_sleeps.append,
        )
        raw_client._raw_http = _fake_raw_http
        self.adapter.client = raw_client
        self.adapter._places_by_id.clear()
        self.adapter._catalog_cache = None
        self.clock.advance(300)
        self.assertFalse(self.adapter._warm_catalog())
        self.assertEqual(1, len(raw_timeouts))
        self.assertLessEqual(raw_timeouts[0][2], 1.5)
        self.clock.advance(300)
        self.assertFalse(self.adapter._warm_catalog())
        self.assertEqual(2, len(raw_timeouts))
        self.assertEqual([], raw_sleeps)
        self.adapter.client = self.client


if __name__ == "__main__":
    unittest.main()
