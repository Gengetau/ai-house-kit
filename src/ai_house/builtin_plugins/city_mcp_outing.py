"""Public Qingkong City outing extension (`city_mcp_outing`).

Lets a locally hosted roommate take a trip into a shared public `bot-game-mcp` city
(`world_id="city"`) over Streamable HTTP MCP, using only the Python standard library.

What it does today (and nothing more):
- `enter_world` (never claims control away from another client), `world_act` `travel`,
  `get_my_view` (to notice who is in the same place: `here.people`, and to read `control.mine`), `leave_world`.
- On coming home it writes an audience-scoped memory (`materials`) of whom they met.

Experimental extras supported (matching live bot-game-mcp main@971484c): `say`, `drift_book`, `take_book`,
and cursor-based `world_wait`. Bringing gifts home into wardrobe/closet is NOT yet provided.

Isolation: every roommate gets their own city credential and their own MCP session.
Either set `CITY_FEDERATION_TOKEN_<ACTOR_ID>` per roommate, or set one shared
`CITY_FEDERATION_TOKEN` together with `CITY_FEDERATION_ACTORS=<exactly one actor id>`.

Safety rules (the upstream `leave_world` ends the resident of the credential's principal and does not check
which client controls it):
- Every state change is written to the local store BEFORE the remote call that causes it.
- A cleanup (`leave_world`) is only sent after a fresh read says `control.mine is true`. If control is someone
  else's, or cannot be confirmed, nothing is sent and the trip record stays, flagged `needs_check`. (A leftover
  record of a departure that never got her out is dropped, without sending anything, once a check shows the
  resident is not ours.) The read and the leave are two calls: control can still change in between.
- A remote call whose outcome is unknown (timeout, reset) is never compensated blindly and never repeated:
  the record stays until a check settles it. Only a definite city answer (`isError`, JSON-RPC error) counts as "did not happen".
- The roommate is only moved locally after the city accepted the trip, and the local kernel's receipt
  (accepted / running / succeeded vs failed) decides whether the trip counts.
- The check after a restart (a fresh MCP session has no city context) is `enter_world(claim=false)`; a side effect
  is that a resident who had dropped out of the city is put back before being released. The same goes for every
  check after a `leave_world` attempt: upstream drops the session's city context first, whatever the outcome, so a
  second `leave_world` can follow a lost first reply.
- `owns()` (which stops the life layer from running her day) is only true while she is really out.
- Not covered yet: following the kernel move to its end (arrival / cancelled), and the kernel only lets a roommate out
  through an outing plan or a CityVisits door move, which this plugin does not use yet.

Public data: the display name and bio sent to the city are visible to every player there.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from worldkernel import materials

log = logging.getLogger("ai_house.city_mcp_outing")

DEFAULT_FEDERATION_PLACES = (
    "ryokogai:kissa-tomoshibi",
    "ryokogai:shiorido",
    "kumominato:muginohi",
    "kumominato:southern-terrace",
)

# transport(method, params, token) -> JSON-RPC `result` dict; lets tests see which credential was used.
Transport = Callable[[str, Dict[str, Any], str], Dict[str, Any]]


class CityOutingError(RuntimeError):
    """A trip step failed; the roommate's local state was left recoverable."""


class CityToolError(CityOutingError):
    """The city answered a tool call with `isError: true`: a definite refusal, not an unknown outcome."""

    def __init__(self, tool: str, detail: str, code: str = ""):
        super().__init__(f"{tool} failed: {detail}")
        self.tool = tool
        self.detail = detail
        self.code = code


class CityRpcError(CityOutingError):
    """The city answered a JSON-RPC call with an `error` object: also a definite answer."""


class CityControlError(CityOutingError):
    """Control of the resident is not ours (or cannot be confirmed): nothing was touched remotely."""


_CODE_RE = re.compile(r"\(code:\s*([A-Z_]+)\)")


def _is_definite(exc: BaseException) -> bool:
    """True when the city itself answered (so the call did not happen); anything else (timeout, reset,
    garbled reply) leaves the outcome unknown."""
    return isinstance(exc, (CityToolError, CityRpcError))


def _env_key(actor_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", actor_id.upper())


class StdlibMcpCityClient:
    """Dependency-free JSON-RPC 2.0 Streamable HTTP MCP client; one instance = one credential + one session."""

    USER_AGENT = "ai-house-kit/0.2 (+https://github.com/Gengetau/ai-house-kit)"

    def __init__(self, mcp_url: str, token: str = "", timeout_s: float = 8.0, transport: Optional[Transport] = None):
        self.mcp_url = mcp_url.rstrip("/")
        self.token = token.strip()
        self.timeout_s = float(timeout_s)
        self._transport = transport
        self.session_id: Optional[str] = None
        self._initialized = False
        self.entered = False  # this session has called enter_world (a fresh session after a restart has not)

    def reset_session(self) -> None:
        """Forget the MCP session; the next call initializes again and the resident must be re-entered."""
        self.session_id = None
        self._initialized = False
        self.entered = False

    def _open(self, req: urllib.request.Request) -> Any:
        try:
            return urllib.request.urlopen(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # the server no longer knows this session id
                self.reset_session()
            raise

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self._transport is not None:
            return self._transport(method, params or {}, self.token)
        payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": method, "params": params or {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self.USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            self.mcp_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self._open(req) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            raw = resp.read().decode("utf-8", errors="replace").strip()
            # Either plain application/json or SSE `data: {...}`
            for line in reversed(raw.splitlines()):
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
            doc = json.loads(raw) if raw else {}
            if "error" in doc:
                raise CityRpcError(f"MCP error: {doc['error']}")
            return doc.get("result") or {}

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "ai-house-kit", "version": "0.2.1"},
        })
        self._initialized = True

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool. Raises CityToolError when the city reports `isError`."""
        self.ensure_initialized()
        if name == "leave_world":
            # Upstream drops this session's city context before it does anything else, so whatever the outcome
            # (answer, error, lost reply) the session is no longer "in the city": the next check must re-enter.
            self.entered = False
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = ""
        for block in res.get("content") or ():
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                break
        if res.get("isError"):
            m = _CODE_RE.search(text)
            code = m.group(1) if m else ""
            if code == "NOT_IN_CITY":  # the city no longer has this session in its city context: look again by re-entering
                self.entered = False
            raise CityToolError(name, (text.splitlines() or [""])[0][:160] or "unknown error", code)
        if name == "enter_world":
            self.entered = True
        if isinstance(res.get("structuredContent"), dict):
            return res["structuredContent"]
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"text": text}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        return res


class CityMcpOutingExtension:
    """Life extension: one MCP client (credential + session) per roommate; the roster decides who may go.

    Trip record (`city_fed:sessions[actor]`), kept in the local store across restarts:
    `phase` entering|entered|out, `remote_in` (the city accepted us), `local_moved` (kernel accepted the move),
    `remote_left`, `needs_check` + `check_reason` (outcome unknown or cleanup failed), `control_lost`
    (the city resident is controlled by someone else: nothing is sent until the owner decides).
    """

    shops = False
    RETRY_S = 60
    MAX_RETURN_FAILURES = 5
    OK_RECEIPTS = ("accepted", "running", "succeeded")

    def __init__(
        self,
        life: Any,
        clients: Mapping[str, StdlibMcpCityClient],
        *,
        entry: str = "kumominato:station",
        house_tag: str = "樱丘小屋",
    ):
        tokens = [c.token for c in clients.values() if c.token]
        if len(set(tokens)) != len(tokens):
            raise ValueError("each roommate needs a different city token; one token may not serve two roommates")
        self.life = life
        self.k = life.k
        self.s = life.s
        self.clients: Dict[str, StdlibMcpCityClient] = dict(clients)
        self.entry = entry
        self.house_tag = house_tag
        self._active_depart_tag: Dict[str, str] = {}
        self._active_return_tag: Dict[str, str] = {}

    @classmethod
    def from_env(cls, life: Any, env: Optional[Dict[str, str]] = None) -> Optional["CityMcpOutingExtension"]:
        eff = os.environ if env is None else env
        url = eff.get("CITY_FEDERATION_MCP_URL", "").strip()
        if not url:
            return None
        tag = eff.get("CITY_FEDERATION_HOUSE_TAG", "樱丘小屋").strip() or "樱丘小屋"
        roster = tuple(getattr(life, "roster", ()) or getattr(life, "names", {}))
        clients: Dict[str, StdlibMcpCityClient] = {}
        for actor in roster:
            tok = eff.get(f"CITY_FEDERATION_TOKEN_{_env_key(actor)}", "").strip()
            if tok:
                clients[actor] = StdlibMcpCityClient(url, token=tok)
        shared = eff.get("CITY_FEDERATION_TOKEN", "").strip()
        if shared:
            listed = [a.strip() for a in eff.get("CITY_FEDERATION_ACTORS", "").split(",") if a.strip() in roster]
            if len(listed) == 1 and listed[0] not in clients:
                clients[listed[0]] = StdlibMcpCityClient(url, token=shared)
            else:
                log.warning(
                    "CITY_FEDERATION_TOKEN ignored: a shared token needs CITY_FEDERATION_ACTORS naming exactly one roommate; "
                    "give each roommate their own CITY_FEDERATION_TOKEN_<ACTOR_ID> instead"
                )
        return cls(life, clients, house_tag=tag)

    # -- state ---------------------------------------------------------------------------------
    def active_sessions(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.life.get("city_fed:sessions") or {})

    def owns(self, actor_id: str) -> bool:
        """True only while the roommate is really out (the kernel accepted her move). The life layer stops running
        her day for as long as this is true, so a leftover record of a trip that never started must not count."""
        sess = self.active_sessions().get(actor_id)
        return bool(sess and sess.get("local_moved"))

    def has_record(self, actor_id: str) -> bool:
        return actor_id in self.active_sessions()

    def pending_checks(self) -> Dict[str, Dict[str, Any]]:
        """Trip records waiting for a check or for the owner (outcome unknown, cleanup failed, control lost)."""
        return {a: s for a, s in self.active_sessions().items() if s.get("needs_check") or s.get("control_lost")}

    def context(self, actor_id: str, kind: str) -> Optional[Dict[str, Any]]:
        last = self.life.get(f"city_fed:last_memo:{actor_id}")
        return {"public_city_memo": last} if last else None

    def _save(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        with self.s.tx():
            self.life.put("city_fed:sessions", sessions)

    def _put(self, actor_id: str, sess: Dict[str, Any]) -> None:
        sessions = self.active_sessions()
        sessions[actor_id] = sess
        self._save(sessions)

    def _drop(self, actor_id: str) -> None:
        sessions = self.active_sessions()
        if sessions.pop(actor_id, None) is not None:
            self._save(sessions)

    def _client(self, actor_id: str) -> StdlibMcpCityClient:
        client = self.clients.get(actor_id)
        if client is None:
            raise CityOutingError(f"no city credential configured for {actor_id}")
        return client

    def _profile(self, actor_id: str) -> Dict[str, str]:
        name = self.life.names.get(actor_id, actor_id)
        return {
            "name": f"{name}·{self.house_tag}"[:16],  # public: other players see this
            "bio": str(self.life.V.PEOPLE.get(actor_id, {}).get("bio") or "来自远方合租屋的旅人")[:80],  # public too
            "client_id": re.sub(r"[^\w.-]", "-", f"aihouse-{actor_id}")[:60],
        }

    # -- control and local receipts ------------------------------------------------------------
    def _verify_control(self, actor_id: str, client: StdlibMcpCityClient) -> str:
        """'mine' | 'not_mine' | 'gone' | 'unknown'. Reads only; never claims. `gone` = the city says the resident is
        not in the city at all (`me.in_city` false), so there is nothing to leave. A fresh session has no city
        context, so it is probed with enter_world(claim=false), which also reports `control.mine`."""
        try:
            if client.entered:
                doc = client.call_tool("get_my_view", {})
            else:
                # a recovery probe sends NO profile: upstream updates an existing resident's bio before it decides
                # who controls her, so a check must never be able to overwrite what another client published
                doc = client.call_tool("enter_world", {"world_id": "city", "client_id": self._profile(actor_id)["client_id"],
                                                       "claim": False})
        except Exception as exc:  # noqa: BLE001
            log.warning("control check for %s failed: %s", actor_id, exc)
            return "unknown"
        me = doc.get("me") if isinstance(doc, dict) else None
        if isinstance(me, dict) and me.get("in_city") is False:
            return "gone"
        ctl = doc.get("control") if isinstance(doc, dict) else None
        mine = ctl.get("mine") if isinstance(ctl, dict) else None
        if mine is True:
            return "mine"
        if mine is False:
            return "not_mine"
        return "unknown"

    def _safe_leave(self, actor_id: str, client: StdlibMcpCityClient) -> str:
        """Leave the city only if the resident is confirmed ours. Returns left|gone|not_mine|unknown|leave_failed."""
        verdict = self._verify_control(actor_id, client)
        if verdict == "gone":
            return "gone"
        if verdict != "mine":
            return verdict
        try:
            client.call_tool("leave_world", {})
        except Exception as exc:  # noqa: BLE001
            log.warning("city leave_world for %s failed: %s", actor_id, exc)
            return "leave_failed"
        return "left"

    def _local_move(self, actor_id: str, to: str, tag: str) -> Dict[str, Any]:
        """Submit a kernel move and read the receipt. Raises when the kernel did not accept it."""
        actor = self.k.actor(actor_id)
        if actor["location"] == to and not actor["action_id"]:
            return {"status": "succeeded", "already_there": True}
        bucket = self._active_depart_tag if to == "away" else self._active_return_tag
        bucket[actor_id] = tag
        try:
            res = self.life.submit(actor_id, "move", {"to": to}, tag)
        finally:
            bucket.pop(actor_id, None)
        status = res.get("status") if isinstance(res, dict) else None
        if status not in self.OK_RECEIPTS:
            reason = (res or {}).get("reason") if isinstance(res, dict) else None
            raise CityOutingError(f"kernel did not accept the move to {to}: {status}/{reason}")
        return res

    def is_door_move(self, actor_id: str, here: str, args: Mapping[str, Any]) -> bool:
        """Real kernel outing door hook (`Outings.guard`): authorizes ONLY this plugin's active/persisted
        federation depart (`room -> away`) and return (`away -> living`) moves."""
        if not isinstance(args, dict):
            return False
        to = args.get("to")
        locs = getattr(self.k.V, "LOCATIONS", {})
        is_depart = here in locs and locs[here].get("kind") == "room" and to == "away"
        is_return = here == "away" and to == "living"
        if not (is_depart or is_return):
            return False
        if not self.k.city_visits.home_allowed(actor_id):
            return False
        if self.life.get(f"outing:active:{actor_id}"):
            return False
        sess = self.active_sessions().get(actor_id) or {}
        if is_depart:
            tag = self._active_depart_tag.get(actor_id) or str(sess.get("local_tag") or "")
        else:
            tag = self._active_return_tag.get(actor_id) or str(sess.get("home_tag") or "")
        if not tag:
            return False
        prefix = f"life:{actor_id}:{tag}:move:"
        with self.s.read() as db:
            rows = db.execute(
                "SELECT c.principal, c.idempotency_key FROM actions a JOIN commands c ON c.command_id=a.command_id "
                "WHERE a.actor_id=? AND a.state IN ('accepted', 'running')",
                (actor_id,),
            ).fetchall()
        return any(r["principal"] == "routine" and str(r["idempotency_key"]).startswith(prefix) for r in rows)

    def _departure_gate(self, actor_id: str, actor: Mapping[str, Any]) -> str:
        """Ask the kernel's own outing guard (read-only) whether a plain move out is allowed right now.
        Returns '' when it is, otherwise the guard's reason (or why it could not be asked). Called before the first
        write to the public city, so a departure the kernel will refuse never touches the city at all."""
        if not self.k.city_visits.home_allowed(actor_id):
            return "city_visit_active"
        if self.life.get(f"outing:active:{actor_id}"):
            return "outing_plan_active"
        try:
            why = self.k.V.guard("move", {"to": "away"}, {"actor_id": actor_id, "location": actor["location"]}, [])
        except Exception as exc:  # noqa: BLE001
            return f"guard_unavailable:{type(exc).__name__}"
        non_door = [r for r in why if r != "outing_plan_required"]
        if non_door:
            return str(non_door[0])
        if self not in getattr(self.life, "extensions", ()):
            return str(why[0]) if why else ""
        return ""

    def _move_state(self, actor_id: str, tag: str) -> Optional[str]:
        """State of the kernel action that THIS plugin submitted under `tag` (None: no such action). Matching is by
        the command's idempotency key, so another action of hers (a meal, a walk) is never mistaken for it."""
        prefix = f"life:{actor_id}:{tag}:move:"
        with self.s.read() as db:
            rows = db.execute(
                "SELECT a.state, c.idempotency_key FROM actions a JOIN commands c ON c.command_id=a.command_id"
                " WHERE a.actor_id=? ORDER BY a.rowid DESC LIMIT 200",
                (actor_id,),
            ).fetchall()
        for r in rows:
            if str(r[1]).startswith(prefix):
                return str(r[0])
        return None

    def _out_move_failed(self, actor_id: str, sess: Dict[str, Any]) -> bool:
        """The kernel accepted this trip's move out but later ended it as failed/cancelled, so she never left home.
        Turn the record back into a leftover and settle the city side the careful way (no "came back" memory)."""
        tag = str(sess.get("local_tag") or "")
        if not tag or sess.get("home_tag") or not sess.get("local_moved"):
            return False
        state = self._move_state(actor_id, tag)
        if state is None or state in self.OK_RECEIPTS or self.k.actor(actor_id)["location"] == "away":
            return False
        sess.update(local_moved=False, phase="entered", retry_at=0, needs_check=True,
                    check_reason=f"the kernel ended her move out as {state}; she never left home")
        self._put(actor_id, sess)
        self.reconcile(actor_id)
        return True

    def _flag(self, actor_id: str, sess: Dict[str, Any], reason: str, *, control_lost: bool = False) -> None:
        now = self.s.clock.now()
        sess["needs_check"] = True
        sess["check_reason"] = reason[:200]
        sess["retry_at"] = now + self.RETRY_S
        if control_lost:
            sess["control_lost"] = True
        self._put(actor_id, sess)
        log.error("city trip of %s needs a check: %s", actor_id, reason)

    def _rollback(self, actor_id: str, sess: Dict[str, Any], client: StdlibMcpCityClient, why: str) -> None:
        """Undo a half-finished departure, but only through a confirmed-ours leave. Otherwise keep the record."""
        result = self._safe_leave(actor_id, client)
        if result in ("left", "gone"):
            self._drop(actor_id)
            return
        self._flag(actor_id, sess, f"{why}; remote cleanup: {result}", control_lost=(result == "not_mine"))

    # -- trip ----------------------------------------------------------------------------------
    def depart_public_city(self, actor_id: str, destination: str = "ryokogai:kissa-tomoshibi", duration_s: int = 3600) -> Dict[str, Any]:
        """Send `actor_id` to the public city. The roommate only becomes `away` locally after the city said yes."""
        sessions = self.active_sessions()
        if actor_id in sessions:
            old = sessions[actor_id]
            raise CityOutingError(
                f"{actor_id} already has a public-city trip record ({old.get('phase')}"
                + (f", needs check: {old.get('check_reason')}" if old.get("needs_check") else "") + ")"
            )
        client = self._client(actor_id)
        actor = self.k.actor(actor_id)
        if actor["location"] == "away":
            raise CityOutingError(f"{actor_id} is already away")
        if actor["action_id"]:
            raise CityOutingError(f"{actor_id} is busy with something else right now")
        gate = self._departure_gate(actor_id, actor)
        if gate:  # known to fail locally: refuse before the first write to the public city, leave no record
            raise CityOutingError(f"the kernel does not let {actor_id} out right now ({gate}); nothing was sent to the city")
        now = self.s.clock.now()
        prof = self._profile(actor_id)
        sess: Dict[str, Any] = {
            "actor": actor_id,
            "phase": "entering",
            "resident_id": "",
            "display_name": prof["name"],
            "destination": destination,
            "request_id": f"cfed:{actor_id}:{int(now)}:go",
            "started_at": now,
            "return_at": now + max(600, int(duration_s)),
            "last_poll": now,
            "encounters": [],
            "met_ids": [],
            "remote_in": False,
            "local_moved": False,
            "local_pending": False,
            "local_tag": "",
            "home_tag": "",
            "remote_left": False,
            "needs_check": False,
            "control_lost": False,
            "last_error": "",
        }
        self._put(actor_id, sess)  # written BEFORE the first remote call, so a crash leaves a trace
        try:
            entered = client.call_tool("enter_world", {"world_id": "city", **prof, "entry": self.entry, "claim": False})
        except Exception as exc:
            if _is_definite(exc):
                self._drop(actor_id)  # the city said no: nothing happened
                raise
            self._flag(actor_id, sess, f"enter_world outcome unknown ({type(exc).__name__})")
            raise CityOutingError("enter_world outcome unknown; trip record kept until it is checked") from exc
        ctl = entered.get("control")
        mine = ctl.get("mine") if isinstance(ctl, dict) else None
        if mine is not True:
            self._flag(actor_id, sess, "resident is controlled by another client" if mine is False else "control unknown after enter_world",
                       control_lost=True)
            raise CityControlError(f"{actor_id}'s city resident is not confirmed ours; not taking over, nothing else sent")
        sess.update(phase="entered", remote_in=True, resident_id=str(entered.get("resident_id") or ""))
        self._put(actor_id, sess)
        try:
            act_res = client.call_tool("world_act", {
                "request_id": sess["request_id"],
                "action": {"type": "travel", "to": destination},
            })
            if act_res.get("ok") is False:
                raise CityOutingError(f"travel was refused: {str(act_res.get('reason') or act_res.get('error') or 'unknown')[:120]}")
        except Exception as exc:
            self._rollback(actor_id, sess, client, f"travel failed ({type(exc).__name__})")
            raise
        try:
            local_tag = f"cityfed:go:{actor_id}:{int(now)}"
            sess.update(phase="leaving_home", local_pending=True, local_tag=local_tag)  # saved first: a crash now is recognisable
            self._put(actor_id, sess)
            receipt = self._local_move(actor_id, "away", local_tag)
        except Exception as exc:
            sess["local_pending"] = False
            self._rollback(actor_id, sess, client, f"local move refused ({type(exc).__name__})")
            raise
        sess.update(phase="out", local_moved=True, local_pending=False, local_action=str(receipt.get("action_id") or ""))
        self._put(actor_id, sess)
        return sess

    def reconcile(self, actor_id: str) -> Optional[str]:
        """Settle a record the roommate never really left home for (outcome unknown or cleanup failed).
        Returns 'adopted' (the kernel had accepted this trip's move out), 'released' (we left the city ourselves),
        'gone' (the resident was not in the city) or None when no such record exists. Raises when control is someone
        else's (record kept, flagged control_lost) or cannot be confirmed yet."""
        sess = self.active_sessions().get(actor_id)
        if not sess or sess.get("local_moved"):
            return None
        if sess.get("local_pending"):
            # crashed between "kernel accepted her move out" and the record saying so. Only the kernel action this
            # plugin submitted under the recorded tag counts; her location or some other action proves nothing.
            state = self._move_state(actor_id, str(sess.get("local_tag") or "")) if sess.get("local_tag") else None
            if state in self.OK_RECEIPTS:
                sess.update(phase="out", local_moved=True, local_pending=False, needs_check=False,
                            check_reason="adopted after a restart: the kernel had accepted this trip's move out")
                self._put(actor_id, sess)
                return "adopted"
            sess["local_pending"] = False  # no such move: she never left home for this trip
            self._put(actor_id, sess)
        client = self._client(actor_id)
        verdict = self._verify_control(actor_id, client)
        if verdict == "gone":
            self._drop(actor_id)
            return "gone"
        if verdict == "mine":
            try:
                client.call_tool("leave_world", {})
            except Exception as exc:  # noqa: BLE001
                self._bump(actor_id, sess, f"leave_world failed: {exc}")
                raise
            self._drop(actor_id)
            return "released"
        if verdict == "not_mine":
            # keep the record: the owner decides (force_home, abandon_remote); nothing is sent on someone else's behalf
            self._flag(actor_id, sess, "control of the city resident is not ours; nothing sent", control_lost=True)
            raise CityControlError(f"{actor_id}'s city resident is controlled by someone else; record kept for the owner")
        self._bump(actor_id, sess, "control still unknown")
        raise CityControlError(f"cannot confirm control of {actor_id}'s city resident yet")

    def _audit_abandon(self, actor_id: str, sess: Dict[str, Any], reason: str) -> None:
        """Keep a trace (a list, one entry per abandonment) of a city resident the owner gave up on."""
        key = f"city_fed:abandoned:{actor_id}"
        notes = list(self.life.get(key) or [])
        notes.append({"at": self.s.clock.now(), "resident_id": sess.get("resident_id", ""), "reason": reason[:200]})
        with self.s.tx():
            self.life.put(key, notes)
        log.error("force_home for %s: abandoning a city resident that could not be released, on the owner's order (%s)", actor_id, reason[:120])

    def _bump(self, actor_id: str, sess: Dict[str, Any], why: str) -> None:
        sess["last_error"] = why[:200]
        sess["needs_check"] = True
        sess["check_reason"] = sess.get("check_reason") or why[:200]
        sess["retry_at"] = self.s.clock.now() + self.RETRY_S
        sess["return_failures"] = int(sess.get("return_failures", 0)) + 1
        if sess["return_failures"] >= self.MAX_RETURN_FAILURES:
            sess["stuck"] = True  # stop retrying on our own; the owner decides (force_home)
            log.error("trip of %s failed %d times; giving up automatic retries (force_home needed)", actor_id, sess["return_failures"])
        self._put(actor_id, sess)
        log.warning("trip step for %s failed, will retry: %s", actor_id, sess["last_error"])

    def return_home(self, actor_id: str, greeting_note: str = "") -> Optional[Dict[str, Any]]:
        """Leave the city, then come home. On failure the record stays (with `last_error`) and is retried;
        a lost or unconfirmed control stops everything and keeps the record."""
        sessions = self.active_sessions()
        sess = sessions.get(actor_id)
        if not sess:
            return None
        now = self.s.clock.now()
        if not sess.get("local_moved"):  # never left home: only a remote leftover to settle
            if self.reconcile(actor_id) != "adopted":
                return sess
            sess = self.active_sessions()[actor_id]
        if self._out_move_failed(actor_id, sess):
            return self.active_sessions().get(actor_id)
        if sess.get("control_lost"):
            raise CityControlError(f"{actor_id}'s city resident is controlled by someone else; waiting for the owner (force_home)")
        try:
            if not sess.get("remote_left"):
                client = self._client(actor_id)
                verdict = self._verify_control(actor_id, client)
                if verdict == "not_mine":
                    self._flag(actor_id, sess, "control of the city resident is no longer ours; nothing sent", control_lost=True)
                    raise CityControlError("control lost; not leaving on someone else's behalf")
                if verdict not in ("mine", "gone"):
                    raise CityOutingError("control of the city resident could not be confirmed")
                if verdict == "mine":
                    client.call_tool("leave_world", {})
                sess["remote_left"] = True
                self._put(actor_id, sess)  # never ask the city to leave twice
            arrived = self._go_home(actor_id, sess, now)
        except CityControlError:
            raise
        except Exception as exc:
            self._bump(actor_id, sess, f"{type(exc).__name__}: {exc}")
            raise
        if not arrived:  # the kernel accepted the walk home; she is not home until it says so (step checks again)
            sess["retry_at"] = now + 30
            self._put(actor_id, sess)
            return sess
        self._drop(actor_id)
        summary_text = greeting_note or (
            f"刚从公共晴空都（{sess['destination']}）远足回来"
            + (f"，路上遇到了 {'、'.join(sess['encounters'][:3])}" if sess.get("encounters") else "")
        )
        with self.s.tx():
            self.life.put(f"city_fed:last_memo:{actor_id}", {"at": now, "summary": summary_text})
        materials.observe(
            self.k,
            actor_id,
            f"cityfed:{actor_id}:{int(now)}",
            "public",
            summary_text,
            source_kind="city_federation",
            knowers=[actor_id],
        )
        return sess

    def force_home(self, actor_id: str, abandon_remote: bool = False) -> Optional[Dict[str, Any]]:
        """Owner escape hatch for a stuck trip. The city resident is only left when confirmed ours; if it is someone
        else's nothing is sent; if control cannot be confirmed the record stays unless the owner passes
        `abandon_remote=True` (an audit note is kept)."""
        sess = self.active_sessions().get(actor_id)
        if not sess:
            return None
        if not sess.get("local_moved"):  # never went out: settle the remote leftover the careful way
            try:
                if self.reconcile(actor_id) != "adopted":
                    return sess
                sess = self.active_sessions()[actor_id]
            except Exception as exc:  # noqa: BLE001
                if not abandon_remote:
                    raise
                self._audit_abandon(actor_id, sess, f"reconcile failed: {exc}")
                self._drop(actor_id)
                return sess
        if sess.get("remote_in") and not sess.get("remote_left"):
            client = self.clients.get(actor_id)
            verdict = self._verify_control(actor_id, client) if client is not None else "unknown"
            abandon = False
            if verdict == "mine":
                try:
                    client.call_tool("leave_world", {})
                except Exception as exc:  # noqa: BLE001
                    if not abandon_remote:
                        self._bump(actor_id, sess, f"force_home: leave_world failed: {exc}")
                        raise
                    abandon = True
            elif verdict in ("not_mine", "gone"):
                log.warning("force_home for %s: the city resident is %s, not sending leave_world", actor_id, verdict)
            elif not abandon_remote:
                raise CityControlError("cannot confirm control of the city resident; the trip record is kept "
                                       "(pass abandon_remote=True to send the roommate home anyway)")
            else:
                abandon = True
            if abandon:
                self._audit_abandon(actor_id, sess, "owner abandoned a city resident that could not be released")
            sess["remote_left"] = True
            self._put(actor_id, sess)
        if sess.get("local_moved"):
            try:
                if sess.get("control_lost") or sess.get("stuck"):  # the owner has decided; automatic checks may resume
                    sess.update(control_lost=False, stuck=False, return_failures=0)
                if not self._go_home(actor_id, sess, self.s.clock.now()):
                    self._put(actor_id, sess)
                    return sess  # accepted, still walking: the record stays until the kernel says she is home
            except Exception as exc:
                self._bump(actor_id, sess, f"force_home: {exc}")
                raise
        self._drop(actor_id)
        return sess

    def _go_home(self, actor_id: str, sess: Dict[str, Any], now: float) -> bool:
        """Walk her home through the kernel. True only once the kernel says she arrived: an accepted move is not
        arrival. The move is recorded under `home_tag` before it is submitted, so a restart finds it again; a move
        that failed or vanished while she is still away is submitted anew."""
        tag = str(sess.get("home_tag") or "")
        if tag:
            state = self._move_state(actor_id, tag)
            if state == "succeeded" or (state is None and self.k.actor(actor_id)["location"] != "away"):
                return True
            if state in ("accepted", "running"):
                return False
        elif self.k.actor(actor_id)["location"] == "living" and not self.k.actor(actor_id)["action_id"]:
            return True
        tag = f"cityfed:back:{actor_id}:{int(now)}"
        sess.update(phase="returning", home_tag=tag)
        self._put(actor_id, sess)
        res = self._local_move(actor_id, "living", tag)
        return bool(res.get("already_there")) or res.get("status") == "succeeded"

    def step(self) -> None:
        sessions = self.active_sessions()
        if not sessions:
            return
        now = self.s.clock.now()
        for actor_id, sess in list(sessions.items()):
            try:
                self._step_one(actor_id, sess, now)
            except Exception as exc:  # one roommate's trouble must not stop the others
                log.warning("city outing step for %s failed: %s", actor_id, exc)

    def _step_one(self, actor_id: str, sess: Dict[str, Any], now: float) -> None:
        if sess.get("control_lost") or sess.get("stuck"):
            return  # waiting for the owner; nothing is sent on its own
        if sess.get("home_tag") and sess.get("local_moved"):
            # the walk home was accepted: keep asking the kernel until it says she arrived (or the move failed)
            if now >= float(sess.get("retry_at", 0)):
                self.return_home(actor_id)
            return
        if not sess.get("local_moved"):
            # a record that never reached "she is out" is a leftover (flagged, or a crash mid-departure)
            if now >= float(sess.get("retry_at", 0)):
                self.reconcile(actor_id)
            return
        if self._out_move_failed(actor_id, sess):
            return
        if now >= float(sess.get("return_at", 0)):
            if now >= float(sess.get("retry_at", 0)):
                self.return_home(actor_id)
            return
        if now - float(sess.get("last_poll", 0)) < 120:
            return
        client = self._client(actor_id)
        if not client.entered:  # fresh session (restart, expiry): look before touching anything
            verdict = self._verify_control(actor_id, client)
            if verdict == "not_mine":
                self._flag(actor_id, sess, "control of the city resident is no longer ours", control_lost=True)
                return
            if verdict != "mine":
                return
        view = client.call_tool("get_my_view", {})
        ctl = view.get("control") if isinstance(view, dict) else None
        me = view.get("me") if isinstance(view, dict) else None
        dropped_out = isinstance(me, dict) and me.get("in_city") is False  # the city let go of us; the return settles it
        if isinstance(ctl, dict) and ctl.get("mine") is False and not dropped_out:
            self._flag(actor_id, sess, "control of the city resident is no longer ours", control_lost=True)
            return
        here = view.get("here") if isinstance(view, dict) else None  # null while on the train
        if isinstance(here, dict):
            for p in here.get("people") or ():
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                pid = str(p.get("id") or "")
                if pid and pid == sess.get("resident_id"):
                    continue
                if pid and pid in sess["met_ids"]:
                    continue
                if pid:
                    sess["met_ids"].append(pid)
                if str(p["name"]) not in sess["encounters"]:
                    sess["encounters"].append(str(p["name"]))
        sess["last_poll"] = now
        self._put(actor_id, sess)

    # -- experimental live city actions (matching bot-game-mcp main@971484c) -------------------
    def _require_live_city(self, actor_id: str) -> Tuple[StdlibMcpCityClient, Dict[str, Any]]:
        sess = self.active_sessions().get(actor_id)
        if not sess or not sess.get("local_moved") or sess.get("control_lost"):
            raise CityOutingError(f"{actor_id} is not currently active in the public city")
        client = self._client(actor_id)
        if self._verify_control(actor_id, client) != "mine":
            raise CityControlError(f"control of {actor_id}'s city resident is not confirmed ours")
        return client, sess

    def say(self, actor_id: str, text: str) -> Dict[str, Any]:
        clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).strip()[:140]
        if not clean:
            raise ValueError("say text cannot be empty")
        client, _ = self._require_live_city(actor_id)
        rid = f"cfed:{actor_id}:say:{uuid.uuid4().hex[:8]}"
        return client.call_tool("world_act", {"request_id": rid, "action": {"type": "say", "text": clean}})

    def drift_book(self, actor_id: str, item_id: str, note: str = "") -> Dict[str, Any]:
        client, _ = self._require_live_city(actor_id)
        rid = f"cfed:{actor_id}:drift:{uuid.uuid4().hex[:8]}"
        act: Dict[str, Any] = {"type": "drift_book", "item_id": str(item_id)}
        if note:
            act["note"] = str(note).strip()[:160]
        return client.call_tool("world_act", {"request_id": rid, "action": {"type": "drift_book", **{k: v for k, v in act.items() if k != "type"}}})

    def take_book(self, actor_id: str, item_id: str) -> Dict[str, Any]:
        client, _ = self._require_live_city(actor_id)
        rid = f"cfed:{actor_id}:take:{uuid.uuid4().hex[:8]}"
        return client.call_tool("world_act", {"request_id": rid, "action": {"type": "take_book", "item_id": str(item_id)}})

    def world_wait(self, actor_id: str, cursor: Optional[str] = None, timeout_ms: int = 2000) -> Dict[str, Any]:
        client, _ = self._require_live_city(actor_id)
        args: Dict[str, Any] = {"timeout_ms": max(100, min(int(timeout_ms), 15000))}
        if cursor is not None:
            args["cursor"] = str(cursor)
        return client.call_tool("world_wait", args)
