"""HTTP bridge client and cached weather provider for Toto (白帆市) City v1.

Pure Python stdlib (urllib / http.client) with injectable transport for offline
synthetic unit tests. Never logs or embeds bridge credentials in exceptions.
All retryable errors (503 RECOVERING, 429 RATE_LIMITED, 500 INTERNAL, transient
socket failures when enabled) reuse the exact original ``request_id`` and
``payload_hash``.
"""
import datetime as dt
import hashlib
import http.client
import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .store import dumps

BRIDGE_PROTOCOL = "city-bridge/1"
MAPPINGS_VERSION = 2
SUPPORTED_MAPPINGS_VERSIONS = (2, 3)
WEATHER_STALE_MS = 3 * 3600 * 1000
JST = dt.timezone(dt.timedelta(hours=9))

_MONTH_TEMP = (5.5, 6.5, 9.5, 14.5, 19.0, 22.0, 26.0, 27.5, 23.5, 18.0, 12.5, 8.0)
_AREA_ORDER = ("sakuraoka", "rurigai", "kazeminato")
QINGKONG_AREA_ORDER = ("hiyorizaka", "ryokogai", "kumominato")


def _utf16_key(s):
    return [ord(ch) for ch in s.encode("utf-16-le").decode("latin1")]


def stable_json(val):
    """Canonical JSON matching packages/engine/src/world/service.ts stableStringify."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return json.dumps(val, ensure_ascii=False, separators=(",", ":"))
    if isinstance(val, (list, tuple)):
        return "[" + ",".join(stable_json(x) for x in val) + "]"
    if isinstance(val, dict):
        items = []
        for k in sorted(val.keys(), key=lambda x: _utf16_key(str(x))):
            items.append(f"{json.dumps(str(k), ensure_ascii=False)}:{stable_json(val[k])}")
        return "{" + ",".join(items) + "}"
    raise TypeError(f"Unsupported JSON value type: {type(val).__name__}")


def bridge_payload_hash(body):
    """SHA-256 hex of canonical body excluding request_id and payload_hash."""
    if not isinstance(body, dict):
        raise ValueError("bridge request body must be a dict")
    rest = {k: v for k, v in body.items() if k not in ("request_id", "payload_hash")}
    return hashlib.sha256(stable_json(rest).encode("utf-8")).hexdigest()


def weather_text(code):
    code = int(code)
    if code == 0:
        return "晴"
    if code == 1:
        return "大致晴朗"
    if code == 2:
        return "多云间晴"
    if code == 3:
        return "阴"
    if code in (45, 48):
        return "有雾"
    if 51 <= code <= 57:
        return "毛毛雨"
    if 61 <= code <= 67:
        return "大雨" if code >= 65 else "下雨"
    if 71 <= code <= 77:
        return "下雪"
    if 80 <= code <= 82:
        return "阵雨"
    if code in (85, 86):
        return "阵雪"
    if code >= 95:
        return "雷雨"
    return "多云"


def city_fallback_weather(at_ms, area="sakuraoka"):
    """Deterministic non-rainy fallback identical to packages/engine/src/city/weather.ts."""
    at_ms = int(at_ms)
    d = dt.datetime.fromtimestamp(at_ms / 1000.0, JST)
    base = _MONTH_TEMP[d.month - 1]
    swing = math.cos(((d.hour - 14) / 24.0) * 2.0 * math.pi) * 3.5
    temp_c = math.floor((base + swing) * 10.0 + 0.5) / 10.0
    idx = _AREA_ORDER.index(area) if area in _AREA_ORDER else (QINGKONG_AREA_ORDER.index(area) if area in QINGKONG_AREA_ORDER else -1)
    code = (1, 2, 3, 2)[(d.day + idx) % 4]
    return {
        "area": area,
        "observed_at_ms": at_ms,
        "fetched_at_ms": at_ms,
        "code": code,
        "temp_c": temp_c,
        "precip_mm": 0.0,
        "rain": False,
        "source": "synthetic",
        "status": "fallback",
        "text": weather_text(code),
        "hourly": [],
    }


class CityBridgeError(Exception):
    """Structured bridge error; never contains credentials."""

    def __init__(self, code, message="", *, status=0, reason=None, data=None, retry_after_ms=None):
        super().__init__(f"{code}: {message}" if message else str(code))
        self.code = str(code)
        self.message = str(message or "")
        self.status = int(status or 0)
        self.reason = str(reason) if reason is not None else None
        self.data = dict(data) if isinstance(data, dict) else {}
        self.retry_after_ms = int(retry_after_ms) if retry_after_ms is not None else None

    @property
    def retryable(self):
        return self.code in ("RECOVERING", "RATE_LIMITED", "INTERNAL", "TRANSPORT_ERROR")


class CityTransportError(CityBridgeError):
    def __init__(self, message="bridge transport failure"):
        super().__init__("TRANSPORT_ERROR", message, status=0)


class CityRecoveringError(CityBridgeError):
    pass


class CityRateLimitedError(CityBridgeError):
    pass


class CityStaleEpochError(CityBridgeError):
    pass


class CityVisitNotActiveError(CityBridgeError):
    pass


class CityConflictError(CityBridgeError):
    pass


class CityVersionMismatchError(CityBridgeError):
    pass


class CityPreconditionError(CityBridgeError):
    pass


class CityNotFoundError(CityBridgeError):
    pass


_ERROR_CLASSES = {
    "RECOVERING": CityRecoveringError,
    "RATE_LIMITED": CityRateLimitedError,
    "STALE_DOMAIN_EPOCH": CityStaleEpochError,
    "VISIT_NOT_ACTIVE": CityVisitNotActiveError,
    "CONFLICT": CityConflictError,
    "VERSION_MISMATCH": CityVersionMismatchError,
    "PRECONDITION_FAILED": CityPreconditionError,
    "NOT_FOUND": CityNotFoundError,
}


def parse_bridge_error(status, payload, headers=None):
    headers = headers or {}
    if isinstance(payload, dict):
        raw_err = payload.get("error")
        if isinstance(raw_err, str) and raw_err:
            code = raw_err
        elif isinstance(raw_err, dict) and isinstance(raw_err.get("code"), str):
            code = raw_err["code"]
        elif isinstance(payload.get("code"), str):
            code = payload["code"]
        else:
            code = "RECOVERING" if status == 503 else "RATE_LIMITED" if status == 429 else "INTERNAL"
        message = str(payload.get("message") or "")
        reason = payload.get("reason")
        retry_ms = payload.get("retryAfterMs")
        extra = {k: v for k, v in payload.items() if k not in ("error", "message", "reason")}
    else:
        code = "RECOVERING" if status == 503 else "RATE_LIMITED" if status == 429 else f"HTTP_{status}"
        message = str(payload or "")[:200]
        reason = None
        retry_ms = None
        extra = {}
    if retry_ms is None and headers:
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra is not None:
            try:
                retry_ms = int(float(ra) * 1000)
            except (TypeError, ValueError):
                retry_ms = None
    cls = _ERROR_CLASSES.get(code, CityBridgeError)
    return cls(code, message, status=status, reason=reason, data=extra, retry_after_ms=retry_ms)


class CityBridgeHttpClient:
    """Server-to-server HTTP client for /city/bridge/v1."""

    def __init__(self, base_url, bridge_key, *, timeout_s=10.0, max_retries=4,
                 backoff_base_s=0.15, max_backoff_s=2.0, sleep_fn=None, transport=None):
        self.base_url = str(base_url).rstrip("/")
        self._bridge_key = str(bridge_key or "")
        self.timeout_s = float(timeout_s)
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self.max_backoff_s = float(max_backoff_s)
        self._sleep = sleep_fn or time.sleep
        self._transport = transport

    def _raw_http(self, method, url, headers, raw_body, timeout_s):
        req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status = int(resp.status)
                resp_headers = dict(resp.headers.items())
                text = resp.read().decode("utf-8", errors="replace")
                return status, resp_headers, text
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            resp_headers = dict(exc.headers.items()) if exc.headers else {}
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""
            return status, resp_headers, text
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise CityTransportError(f"{type(exc).__name__}: bridge connection failed") from None

    def request(self, method, path, *, query=None, body=None, retry_network=True, timeout_s=None):
        return self._request_with_policy(
            method,
            path,
            query=query,
            body=body,
            retry_network=retry_network,
            timeout_s=timeout_s,
            max_retries=None,
        )

    def _request_with_policy(
        self, method, path, *, query=None, body=None, retry_network=True, timeout_s=None, max_retries=None
    ):
        method = method.upper()
        rel = path if path.startswith("/") else f"/{path}"
        qs = ""
        if query:
            clean_q = {k: v for k, v in query.items() if v is not None}
            if clean_q:
                qs = "?" + urllib.parse.urlencode(clean_q)
        url = f"{self.base_url}{rel}{qs}"

        wire_body = None
        raw_bytes = None
        if body is not None:
            wire_body = dict(body)
            if "request_id" in wire_body and "payload_hash" not in wire_body:
                wire_body["payload_hash"] = bridge_payload_hash(wire_body)
            raw_bytes = stable_json(wire_body).encode("utf-8")

        headers = {"Accept": "application/json"}
        if self._bridge_key:
            headers["Authorization"] = f"Bearer {self._bridge_key}"
        if raw_bytes is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        eff_timeout = float(timeout_s) if timeout_s is not None else self.timeout_s
        eff_retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        attempt = 0
        while True:
            try:
                if self._transport is not None:
                    status, resp_headers, text = self._transport(
                        method, rel, dict(query or {}),
                        json.loads(raw_bytes.decode("utf-8")) if raw_bytes is not None else None,
                        dict(headers),
                    )
                else:
                    status, resp_headers, text = self._raw_http(method, url, headers, raw_bytes, eff_timeout)
            except CityTransportError:
                if not retry_network or attempt >= eff_retries:
                    raise
                delay = min(self.max_backoff_s, self.backoff_base_s * (2 ** attempt))
                attempt += 1
                self._sleep(delay)
                continue

            parsed = None
            if isinstance(text, (dict, list)):
                parsed = text
            elif isinstance(text, str) and text.strip():
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None

            if 200 <= status < 300:
                if not isinstance(parsed, dict):
                    raise CityTransportError("Malformed non-object JSON from city bridge")
                return parsed

            err = parse_bridge_error(status, parsed if parsed is not None else text, resp_headers)
            if err.code in ("RECOVERING", "RATE_LIMITED", "INTERNAL") and attempt < eff_retries:
                if err.retry_after_ms is not None:
                    delay = min(self.max_backoff_s, max(0.01, err.retry_after_ms / 1000.0))
                else:
                    delay = min(self.max_backoff_s, self.backoff_base_s * (2 ** attempt))
                attempt += 1
                self._sleep(delay)
                continue
            raise err

    # --- typed endpoint wrappers --------------------------------------------------------------------
    def get_catalog(self, *, retry_network=True, timeout_s=None, max_retries=None):
        return self._request_with_policy(
            "GET",
            "/catalog",
            retry_network=retry_network,
            timeout_s=timeout_s,
            max_retries=max_retries,
        )

    def get_weather(self):
        return self.request("GET", "/weather")

    def get_reconcile(self):
        return self.request("GET", "/reconcile")

    def get_visit(self, visit_id):
        return self.request("GET", f"/visits/{urllib.parse.quote(str(visit_id), safe='')}")

    def prepare_visit(self, body, *, retry_network=False):
        return self.request("POST", "/visits/prepare", body=body, retry_network=retry_network)

    def commit_visit(self, visit_id, *, request_id, transfer_id, domain_epoch, payload_hash=None, retry_network=False):
        body = {"request_id": request_id, "transfer_id": transfer_id, "domain_epoch": int(domain_epoch)}
        if payload_hash is not None:
            body["payload_hash"] = payload_hash
        return self.request(
            "POST",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/commit",
            body=body,
            retry_network=retry_network,
        )

    def abort_visit(self, visit_id, *, request_id, reason=None, payload_hash=None, retry_network=False):
        body = {"request_id": request_id}
        if reason is not None:
            body["reason"] = str(reason)[:120]
        if payload_hash is not None:
            body["payload_hash"] = payload_hash
        return self.request(
            "POST",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/abort",
            body=body,
            retry_network=retry_network,
        )

    def request_return(self, visit_id, *, request_id, reason=None, payload_hash=None, retry_network=False):
        body = {"request_id": request_id}
        if reason is not None:
            body["reason"] = str(reason)[:120]
        if payload_hash is not None:
            body["payload_hash"] = payload_hash
        return self.request(
            "POST",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/return-request",
            body=body,
            retry_network=retry_network,
        )

    def release_visit(self, visit_id, *, request_id, settlement_id, domain_epoch, payload_hash=None, retry_network=False):
        body = {
            "request_id": request_id,
            "settlement_id": str(settlement_id),
            "domain_epoch": int(domain_epoch),
        }
        if payload_hash is not None:
            body["payload_hash"] = payload_hash
        return self.request(
            "POST",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/release",
            body=body,
            retry_network=retry_network,
        )

    def get_outbox(self, *, after=0, limit=100, wait_ms=0):
        timeout_s = max(self.timeout_s, (int(wait_ms) / 1000.0) + 5.0)
        return self.request(
            "GET",
            "/outbox",
            query={"after": int(after), "limit": int(limit), "wait_ms": int(wait_ms)},
            timeout_s=timeout_s,
        )

    def visit_view(self, visit_id, *, domain_epoch):
        return self.request(
            "GET",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/view",
            query={"domain_epoch": int(domain_epoch)},
        )

    def visit_wait(self, visit_id, *, domain_epoch, cursor=None, wait_ms=0):
        timeout_s = max(self.timeout_s, (int(wait_ms) / 1000.0) + 5.0)
        return self.request(
            "GET",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/wait",
            query={"domain_epoch": int(domain_epoch), "cursor": cursor, "wait_ms": int(wait_ms)},
            timeout_s=timeout_s,
        )

    def visit_act(self, visit_id, *, request_id, domain_epoch, action, payload_hash=None, retry_network=True):
        body = {
            "request_id": request_id,
            "domain_epoch": int(domain_epoch),
            "action": dict(action),
        }
        if payload_hash is not None:
            body["payload_hash"] = payload_hash
        return self.request(
            "POST",
            f"/visits/{urllib.parse.quote(str(visit_id), safe='')}/act",
            body=body,
            retry_network=retry_network,
        )


class CityWeatherProvider:
    """K9 / F14: kernel weather backed by GET /city/bridge/v1/weather with 3h stale & identical fallback."""

    def __init__(self, store, client, *, home_area="sakuraoka", refresh_interval_ms=30 * 60 * 1000):
        self.s = store
        self.client = client
        self.home_area = home_area
        self.refresh_interval_ms = int(refresh_interval_ms)
        self._last_fetch_attempt_ms = None

    def sync(self, now_ms=None, *, force=False):
        now_ms = int(round(self.s.clock.now() * 1000)) if now_ms is None else int(now_ms)
        if (not force and self._last_fetch_attempt_ms is not None
                and 0 <= now_ms - self._last_fetch_attempt_ms < self.refresh_interval_ms):
            return True
        self._last_fetch_attempt_ms = now_ms
        try:
            resp = self.client.get_weather()
        except CityBridgeError:
            return False
        areas = resp.get("areas") if isinstance(resp, dict) else None
        if not isinstance(areas, list):
            return False
        with self.s.tx() as db:
            for snap in areas:
                if not isinstance(snap, dict) or not isinstance(snap.get("area"), str):
                    continue
                area = snap["area"]
                status = str(snap.get("status") or "ok")
                fetched = int(snap.get("fetched_at_ms") or resp.get("at_ms") or now_ms)
                prev = db.execute("SELECT last_ok_at_ms FROM city_weather_cache WHERE area=?", (area,)).fetchone()
                prev_ok = prev[0] if prev and prev[0] is not None else None
                last_ok = fetched if status == "ok" else prev_ok
                db.execute(
                    "INSERT INTO city_weather_cache(area,observed_at_ms,fetched_at_ms,last_ok_at_ms,code,temp_c,"
                    "precip_mm,rain,source,status,text,hourly_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(area) DO UPDATE SET observed_at_ms=excluded.observed_at_ms,"
                    " fetched_at_ms=excluded.fetched_at_ms, last_ok_at_ms=excluded.last_ok_at_ms,"
                    " code=excluded.code, temp_c=excluded.temp_c, precip_mm=excluded.precip_mm,"
                    " rain=excluded.rain, source=excluded.source, status=excluded.status,"
                    " text=excluded.text, hourly_json=excluded.hourly_json",
                    (
                        area,
                        int(snap.get("observed_at_ms") or fetched),
                        fetched,
                        last_ok,
                        int(snap.get("code") or 1),
                        float(snap.get("temp_c") if snap.get("temp_c") is not None else 15.0),
                        float(snap.get("precip_mm") or 0.0),
                        1 if snap.get("rain") else 0,
                        str(snap.get("source") or "open-meteo"),
                        status,
                        str(snap.get("text") or weather_text(int(snap.get("code") or 1))),
                        dumps(snap.get("hourly") if isinstance(snap.get("hourly"), list) else []),
                    ),
                )
        return True

    def area_snapshot(self, area, now_ms=None, *, sync=True):
        now_ms = int(round(self.s.clock.now() * 1000)) if now_ms is None else int(now_ms)
        online = self.sync(now_ms, force=sync) if sync else False
        with self.s.read() as db:
            row = db.execute("SELECT * FROM city_weather_cache WHERE area=?", (area,)).fetchone()
        if row is not None:
            last_ok = row["last_ok_at_ms"]
            age_ref = last_ok if last_ok is not None else row["fetched_at_ms"]
            if online and row["fetched_at_ms"] == now_ms:
                return {
                    "area": row["area"],
                    "observed_at_ms": row["observed_at_ms"],
                    "fetched_at_ms": row["fetched_at_ms"],
                    "code": row["code"],
                    "temp_c": float(row["temp_c"]),
                    "precip_mm": float(row["precip_mm"]),
                    "rain": bool(row["rain"]),
                    "source": row["source"],
                    "status": row["status"],
                    "text": row["text"],
                }
            if row["status"] != "fallback" and age_ref is not None and now_ms - int(age_ref) <= WEATHER_STALE_MS:
                return {
                    "area": row["area"],
                    "observed_at_ms": row["observed_at_ms"],
                    "fetched_at_ms": row["fetched_at_ms"],
                    "code": row["code"],
                    "temp_c": float(row["temp_c"]),
                    "precip_mm": float(row["precip_mm"]),
                    "rain": bool(row["rain"]),
                    "source": row["source"],
                    "status": "ok" if (online and row["status"] == "ok") else "stale",
                    "text": row["text"],
                }
        return city_fallback_weather(now_ms, area=area)

    def __call__(self, ts=None):
        now_ms = int(round((self.s.clock.now() if ts is None else float(ts)) * 1000))
        snap = self.area_snapshot(self.home_area, now_ms=now_ms, sync=True)
        return {
            "temp_c": snap["temp_c"],
            "rain": snap["rain"],
            "status": snap["status"],
            "source": snap["source"],
            "code": snap["code"],
            "area": snap["area"],
            "text": snap["text"],
        }
