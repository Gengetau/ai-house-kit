"""v0.2.1: public-city outing plugin against upstream-shaped replies (bot-game-mcp city tools, main@971484c).

Shapes mirrored here: successful tools answer `content[0].text` = JSON (no structuredContent);
failures answer `isError: true` with a text ending in `(code: X)`; `get_my_view` carries `me.in_city`,
`control.mine` and `here.people` (a list of `{id, name}` that never includes the viewer; `here` is null while
travelling); `enter_world` returns `resident_id`, `control.mine` and the same self view.
The fake city can lose the reply AFTER applying a call (a timeout with an unknown outcome), hand the resident to
another controller, and refuse. The local side uses the real world kernel: receipts and locations are real.
"""
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.builtin_plugins.city_mcp_outing import (
    CityControlError,
    CityMcpOutingExtension,
    CityOutingError,
    CityToolError,
    StdlibMcpCityClient,
)
from ai_house.daemon import HouseDaemon

KISSA = "ryokogai:kissa-tomoshibi"


def _ok(doc):
    return {"content": [{"type": "text", "text": json.dumps(doc, ensure_ascii=False)}]}


def _err(msg, code):
    return {"isError": True, "content": [{"type": "text", "text": f"❌ {msg}\n(code: {code})"}]}


class FakeCity:
    """One shared city; every token is a different resident with its own state."""

    def __init__(self):
        self.calls = []  # (token, method, tool)
        self.args = []  # (token, tool, arguments)
        self.res = {}
        self.fail_next = {}  # (token, tool) -> remaining failures
        self.fail_always = set()  # (token, tool)
        self.drop_reply = set()  # (token, tool): apply the call, then lose the reply (timeout, outcome unknown)
        self.crash_after = set()  # (token, tool): apply the call, then the whole process dies (SystemExit)
        self.hooks = {}  # (token, tool) -> callable run just before the call is handled
        self.controlled_elsewhere = set()
        self.include_self = False
        self.in_transit = set()
        self.ctx = {}  # token -> this MCP session has entered the city context (a fresh session has not)

    def _r(self, token):
        return self.res.setdefault(token, {"id": f"res_{len(self.res) + 1}", "name": "", "in_city": False, "place": None})

    def place_player(self, token, name, place):
        self._r(token).update(name=name, in_city=True, place=place)

    def count(self, tool, token=None):
        return sum(1 for t, _, n in self.calls if n == tool and (token is None or t == token))

    def _control(self, token, r):
        return {"epoch": 1, "mine": bool(r["in_city"] and token not in self.controlled_elsewhere)}

    def transport(self, method, params, token):
        tool = params.get("name") if method == "tools/call" else None
        out = self._handle(method, params, token, tool)
        if (token, tool) in self.drop_reply:
            self.drop_reply.discard((token, tool))
            raise TimeoutError("reply lost")
        if (token, tool) in self.crash_after:
            self.crash_after.discard((token, tool))
            raise SystemExit("process died")
        return out

    def _handle(self, method, params, token, tool):
        self.calls.append((token, method, tool))
        if method == "initialize":
            self.ctx[token] = False
            return {"protocolVersion": "2025-03-26"}
        args = params.get("arguments") or {}
        self.args.append((token, tool, args))
        hook = self.hooks.pop((token, tool), None)
        if hook:
            hook()
        in_ctx = self.ctx.get(token)  # was this session in the city context when the call arrived?
        if tool == "leave_world":
            self.ctx[token] = False  # upstream drops the session's city context first, whatever happens next
        key = (token, tool)
        if key in self.fail_always or self.fail_next.get(key, 0) > 0:
            if self.fail_next.get(key, 0) > 0:
                self.fail_next[key] -= 1
            return _err(f"{tool} refused", "PLACE_CLOSED")
        r = self._r(token)
        if tool == "enter_world":
            self.ctx[token] = True
            r.update(in_city=True, place=args.get("entry") or r["place"])  # re-entering keeps your place
            if "name" in args:  # upstream: name is only needed the first time; bio only changes when it is sent
                r["name"] = args["name"]
            if "bio" in args:
                r["bio"] = args["bio"]
            return _ok({
                "ok": True, "world_id": "city", "created": True, "resident_id": r["id"],
                "me": {"in_city": True}, "control": self._control(token, r),
            })
        if tool in ("world_act", "get_my_view", "leave_world", "world_wait") and not in_ctx:
            return _err("this session is not in the city", "NOT_IN_CITY")  # upstream routes such calls elsewhere
        if tool == "world_act":
            if args["action"]["type"] == "travel":
                r["place"] = args["action"]["to"]
            return _ok({"ok": True, "status": "accepted"})
        if tool == "world_wait":
            return _ok({"ok": True, "cursor": "cur_1", "events": []})
        if tool == "get_my_view":
            base = {"me": {"in_city": r["in_city"]}, "control": self._control(token, r)}
            if token in self.in_transit or not r["in_city"]:
                return _ok({**base, "here": None})
            people = [
                {"id": o["id"], "name": o["name"]}
                for t, o in self.res.items()
                if o["in_city"] and o["place"] == r["place"] and (t != token or self.include_self)
            ]
            return _ok({**base, "here": {"place": {"id": r["place"]}, "people": people}})
        if tool == "leave_world":  # upstream ends the principal's resident without asking which client controls it
            r["in_city"] = False
            return _ok({"ok": True})
        return _err("unknown tool", "NOT_FOUND")


class CityTestBase(unittest.TestCase):
    def setUp(self):
        self.city = FakeCity()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rt = Path(tmp.name) / "rt"
        self.submits = []  # (actor, to, kernel status): the receipts the plugin saw
        self.preempt = set()  # actors who get a real conflicting kernel action just before the plugin's move
        self.door_open = False  # CityMcpOutingExtension now carries its own real kernel door hook; True only for ordinary-plan test
        self.daemon = None
        self.boot()

    def boot(self):
        if self.daemon is not None:
            self.daemon.close()
        self.daemon = HouseDaemon(runtime_dir=self.rt, virtual_clock=True, env={})
        self.addCleanup(self.daemon.close)
        life = self.daemon.authority.life
        self.life = life
        self.kernel = life.k
        # Happy-path city trips run through the real kernel gate (`CityMcpOutingExtension.is_door_move`).
        # `self.door_open = True` is reserved for simulating an unrelated ordinary outing plan (`ordinary-plan`).
        real_door = self.kernel.city_visits.is_door_move
        self.kernel.city_visits.is_door_move = lambda a, here, args: True if self.door_open else real_door(a, here, args)
        self.clients = {t: StdlibMcpCityClient("https://city.example.com/mcp", token=t, transport=self.city.transport)
                        for t in ("tok_nell", "tok_yuki")}
        self.ext = CityMcpOutingExtension(life, {"nell": self.clients["tok_nell"], "yuki": self.clients["tok_yuki"]}, house_tag="樱丘洋馆")
        life.extensions.append(self.ext)
        real_submit = life.submit

        def recording_submit(actor, action_type, args, tag):
            if actor in self.preempt and action_type == "move" and args.get("to") in ("away", "living"):
                self.preempt.discard(actor)
                real_submit(actor, "wait", {"seconds": 120}, "preempt")  # a real, running kernel action valid inside or away
            res = real_submit(actor, action_type, args, tag)
            if action_type == "move":
                self.submits.append((actor, args.get("to"), res.get("status")))
            return res

        life.submit = recording_submit

    def accepted(self, actor, to):
        return any(a == actor and t == to and s in ("accepted", "running", "succeeded") for a, t, s in self.submits)

    def location(self, actor):
        return self.kernel.actor(actor)["location"]


class CityOutingTests(CityTestBase):
    # -- isolation ------------------------------------------------------------------------------
    def test_two_roommates_use_their_own_credential_and_session(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.ext.depart_public_city("yuki", KISSA, duration_s=1800)
        for tok, actor in (("tok_nell", "nell"), ("tok_yuki", "yuki")):
            self.assertEqual(self.city.count("enter_world", tok), 1)
            self.assertEqual(sum(1 for t, m, _ in self.city.calls if t == tok and m == "initialize"), 1)
            entered = next(a for t, n, a in self.city.args if t == tok and n == "enter_world")
            self.assertEqual(entered["client_id"], f"aihouse-{actor}")
        self.assertEqual({t for t, _, _ in self.city.calls}, {"tok_nell", "tok_yuki"})
        self.assertTrue(self.ext.owns("nell") and self.ext.owns("yuki"))

        # nell going home must not end yuki's trip
        self.daemon.fast_forward(3)  # both have walked out; a return while still walking is refused by the kernel
        self.ext.return_home("nell")
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertEqual(self.city.count("leave_world", "tok_yuki"), 0)
        self.assertTrue(self.city.res["tok_yuki"]["in_city"])
        self.assertTrue(self.ext.owns("yuki"))
        self.assertTrue(self.ext.owns("nell"))  # the walk home was accepted, she has not arrived yet
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.owns("nell"))
        self.assertTrue(self.ext.owns("yuki"))

    def test_one_token_cannot_serve_two_roommates(self):
        mk = lambda: StdlibMcpCityClient("https://city.example.com/mcp", token="same", transport=self.city.transport)
        with self.assertRaises(ValueError):
            CityMcpOutingExtension(self.life, {"nell": mk(), "yuki": mk()})

    def test_from_env_shared_token_only_serves_one_named_roommate(self):
        life = self.life
        base = {"CITY_FEDERATION_MCP_URL": "https://city.example.com/mcp", "CITY_FEDERATION_TOKEN": "tok_shared"}
        ext = CityMcpOutingExtension.from_env(life, env=dict(base))
        self.assertEqual(ext.clients, {})
        with self.assertRaises(CityOutingError):
            ext.depart_public_city("nell", KISSA)
        ext = CityMcpOutingExtension.from_env(life, env={**base, "CITY_FEDERATION_ACTORS": "nell,yuki"})
        self.assertEqual(ext.clients, {})  # two names + one token: refused, not guessed
        ext = CityMcpOutingExtension.from_env(life, env={**base, "CITY_FEDERATION_ACTORS": "nell"})
        self.assertEqual(set(ext.clients), {"nell"})
        ext = CityMcpOutingExtension.from_env(life, env={
            "CITY_FEDERATION_MCP_URL": "https://city.example.com/mcp",
            "CITY_FEDERATION_TOKEN_NELL": "tok_a", "CITY_FEDERATION_TOKEN_YUKI": "tok_b",
        })
        self.assertEqual(set(ext.clients), {"nell", "yuki"})
        self.assertIsNone(CityMcpOutingExtension.from_env(life, env={}))

    def test_only_public_profile_fields_are_sent(self):
        self.ext.depart_public_city("nell", KISSA)
        entered = next(a for t, n, a in self.city.args if t == "tok_nell" and n == "enter_world")
        self.assertEqual(entered["name"], "奈尔·樱丘洋馆")
        self.assertLessEqual(len(entered["name"]), 16)
        self.assertLessEqual(len(entered["bio"]), 80)
        self.assertEqual(set(entered), {"world_id", "name", "bio", "entry", "client_id", "claim"})
        self.assertIs(entered["claim"], False)

    # -- a normal trip, against the real kernel --------------------------------------------------
    def test_trip_there_and_back_follows_the_kernel_receipts(self):
        sess = self.ext.depart_public_city("nell", KISSA, duration_s=600)
        self.assertTrue(sess["local_moved"] and sess["remote_in"])
        self.assertTrue(self.accepted("nell", "away"))
        self.daemon.fast_forward(3)
        self.assertEqual(self.location("nell"), "away")  # the kernel really walked her out
        self.daemon.fast_forward(12)  # return time reached: the step sends her home
        self.daemon.fast_forward(3)
        self.assertFalse(self.ext.owns("nell"))
        self.assertTrue(self.accepted("nell", "living"))
        self.assertNotEqual(self.location("nell"), "away")  # home (her routine may already have moved her on)
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)

    # -- departure: city side -----------------------------------------------------------------------
    def test_enter_is_error_keeps_roommate_home_and_leaves_no_record(self):
        self.city.fail_always.add(("tok_nell", "enter_world"))
        with self.assertRaises(CityToolError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertFalse(self.accepted("nell", "away"))
        self.assertFalse(self.ext.owns("nell"))  # a definite refusal: nothing happened, nothing to settle
        self.assertEqual(self.city.count("world_act", "tok_nell"), 0)
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)

    def test_enter_with_control_elsewhere_stops_and_keeps_a_record_without_leaving(self):
        self.city.controlled_elsewhere.add("tok_nell")
        self.city.place_player("tok_nell", "x", None)
        with self.assertRaises(CityControlError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertEqual(self.city.count("world_act", "tok_nell"), 0)
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)  # not ours to end
        self.assertFalse(self.accepted("nell", "away"))
        pending = self.ext.pending_checks()
        self.assertTrue(pending["nell"]["control_lost"])
        self.assertFalse(self.ext.owns("nell"))  # never went out: her day must keep running
        self.assertTrue(self.ext.has_record("nell"))
        with self.assertRaises(CityOutingError):  # no second attempt over the unresolved record
            self.ext.depart_public_city("nell", KISSA)
        self.assertEqual(self.city.count("enter_world", "tok_nell"), 1)
        # a check does NOT delete the record: the resident is someone else's, the owner decides
        with self.assertRaises(CityControlError):
            self.ext.reconcile("nell")
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)
        self.assertTrue(self.ext.has_record("nell"))
        self.assertTrue(self.ext.pending_checks()["nell"]["control_lost"])
        self.assertFalse(self.ext.owns("nell"))
        # only the owner's explicit word lets it go, and it leaves an audit note
        self.ext.force_home("nell", abandon_remote=True)
        self.assertFalse(self.ext.has_record("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)
        self.assertIsNotNone(self.daemon.authority.life.get("city_fed:abandoned:nell"))

    def test_enter_reply_lost_is_not_compensated_or_repeated_and_survives_a_restart(self):
        self.city.drop_reply.add(("tok_nell", "enter_world"))  # the city entered her, the answer never came
        with self.assertRaises(CityOutingError):
            self.ext.depart_public_city("nell", KISSA)
        rec = self.ext.active_sessions()["nell"]
        self.assertTrue(rec["needs_check"])
        self.assertEqual((self.city.count("enter_world", "tok_nell"), self.city.count("world_act", "tok_nell"),
                          self.city.count("leave_world", "tok_nell")), (1, 0, 0))
        self.assertFalse(self.accepted("nell", "away"))
        with self.assertRaises(CityOutingError):
            self.ext.depart_public_city("nell", KISSA)  # never a second departure while this is open
        self.assertEqual(self.city.count("enter_world", "tok_nell"), 1)

        # restart: new daemon, new client objects with no city context; the record is still there
        self.boot()
        self.assertEqual(self.ext.active_sessions()["nell"]["check_reason"], rec["check_reason"])
        self.assertEqual(self.ext.reconcile("nell"), "released")  # probe(claim=false) said mine, then left once
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertFalse(self.city.res["tok_nell"]["in_city"])
        self.assertFalse(self.ext.owns("nell"))
        entered = [a for t, n, a in self.city.args if t == "tok_nell" and n == "enter_world"]
        self.assertIs(entered[-1]["claim"], False)
        self.assertEqual(self.ext.active_sessions(), {})  # nothing left over: a new departure is not blocked by the record

    def test_step_settles_an_unknown_enter_on_its_own(self):
        self.city.drop_reply.add(("tok_nell", "enter_world"))
        with self.assertRaises(CityOutingError):
            self.ext.depart_public_city("nell", KISSA)
        self.daemon.fast_forward(3)
        self.assertFalse(self.ext.owns("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)

    def test_travel_is_error_rolls_back_through_a_confirmed_leave(self):
        self.city.fail_always.add(("tok_nell", "world_act"))
        with self.assertRaises(CityToolError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertFalse(self.accepted("nell", "away"))
        self.assertFalse(self.ext.owns("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertFalse(self.city.res["tok_nell"]["in_city"])

    def test_travel_reply_lost_rolls_back_without_moving_her(self):
        self.city.drop_reply.add(("tok_nell", "world_act"))  # travel happened remotely, answer lost
        with self.assertRaises(TimeoutError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertFalse(self.accepted("nell", "away"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertFalse(self.ext.owns("nell"))

    def test_rollback_that_also_fails_leaves_a_record_that_settles_later(self):
        self.city.fail_always.add(("tok_nell", "world_act"))
        self.city.fail_next[("tok_nell", "leave_world")] = 1
        with self.assertRaises(CityToolError):
            self.ext.depart_public_city("nell", KISSA)
        rec = self.ext.active_sessions()["nell"]
        self.assertTrue(rec["needs_check"])
        self.assertIn("leave_failed", rec["check_reason"])
        self.assertTrue(self.city.res["tok_nell"]["in_city"])  # she is still there
        with self.assertRaises(CityOutingError):
            self.ext.depart_public_city("nell", KISSA)
        self.daemon.fast_forward(3)  # the step retries the cleanup (the city has recovered)
        self.assertFalse(self.ext.owns("nell"))
        self.assertFalse(self.city.res["tok_nell"]["in_city"])

    def test_rollback_stops_when_control_changed_hands_during_the_trip_request(self):
        self.city.fail_always.add(("tok_nell", "world_act"))
        self.city.hooks[("tok_nell", "world_act")] = lambda: self.city.controlled_elsewhere.add("tok_nell")
        with self.assertRaises(CityToolError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)  # never ended someone else's resident
        self.assertTrue(self.ext.active_sessions()["nell"]["control_lost"])
        self.assertTrue(self.city.res["tok_nell"]["in_city"])

    # -- departure: local kernel side ------------------------------------------------------------------
    def test_kernel_refusing_the_move_leaves_the_city_and_keeps_her_home(self):
        self.preempt.add("nell")  # she starts something else in the kernel right before the move
        with self.assertRaises(CityOutingError) as ctx:
            self.ext.depart_public_city("nell", KISSA)
        self.assertIn("kernel did not accept", str(ctx.exception))
        self.assertIn(("nell", "away", "failed"), self.submits)  # the real receipt
        self.assertFalse(self.accepted("nell", "away"))
        self.assertFalse(self.ext.owns("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertFalse(self.city.res["tok_nell"]["in_city"])
        self.daemon.fast_forward(3)
        self.assertNotEqual(self.location("nell"), "away")

    def test_busy_roommate_is_refused_before_any_city_call(self):
        self.kernel.submit({"idempotency_key": "t:busy", "principal": "routine", "actor_id": "nell",
                            "origin": "routine", "action_type": "move", "args": {"to": "kitchen"}})
        with self.assertRaises(CityOutingError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertEqual(self.city.calls, [])
        self.assertFalse(self.ext.owns("nell"))

    def test_real_kernel_gate_stops_the_departure_before_any_city_write(self):
        """Without an active registered door hook, the real kernel says `outing_plan_required` before any city
        write; and even while this plugin is active, an unapproved plain move to `away` is still blocked."""
        self.door_open = False
        self.life.extensions.remove(self.ext)
        for _ in range(2):
            with self.assertRaises(CityOutingError) as ctx:
                self.ext.depart_public_city("nell", KISSA)
            self.assertIn("outing_plan_required", str(ctx.exception))
            self.assertEqual(self.city.calls, [])
            self.assertFalse(self.ext.has_record("nell"))
            self.assertFalse(self.ext.owns("nell"))
        self.assertEqual(self.submits, [])  # not even a local move was attempted
        self.life.extensions.append(self.ext)
        plain = self.daemon.authority.life.submit("nell", "move", {"to": "away"}, "unauthorized-plain")
        self.assertEqual(plain["status"], "failed")
        self.assertIn("outing_plan_required", str(plain["reason"]))
        self.daemon.fast_forward(3)
        self.assertNotEqual(self.location("nell"), "away")

    def test_the_gate_check_does_not_hide_an_existing_record(self):
        """An old record can still be settled while new departures are refused."""
        self.city.controlled_elsewhere.add("tok_nell")
        with self.assertRaises(CityControlError):
            self.ext.depart_public_city("nell", KISSA)  # door open: leaves a control_lost record
        self.door_open = False
        self.assertTrue(self.ext.has_record("nell"))
        self.ext.force_home("nell", abandon_remote=True)
        self.assertFalse(self.ext.has_record("nell"))

    # -- meeting people: here.people ---------------------------------------------------------------
    def test_encounter_comes_from_here_people_and_never_includes_self(self):
        self.city.place_player("tok_other", "小葵·海风寮", KISSA)
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.city.include_self = True  # even if a server listed the viewer, we skip them by id
        self.daemon.fast_forward(3)
        sess = self.ext.active_sessions()["nell"]
        self.assertEqual(sess["encounters"], ["小葵·海风寮"])
        self.assertEqual(len(sess["met_ids"]), 1)
        self.daemon.fast_forward(30)
        self.assertFalse(self.ext.owns("nell"))
        self.assertIn("小葵·海风寮", self.daemon.authority.life.get("city_fed:last_memo:nell")["summary"])

    def test_here_is_null_while_travelling(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.city.in_transit.add("tok_nell")
        self.daemon.fast_forward(3)
        self.assertEqual(self.ext.active_sessions()["nell"]["encounters"], [])

    def test_two_roommates_meet_only_who_is_where_they_are(self):
        self.city.place_player("tok_other", "小葵·海风寮", KISSA)
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.ext.depart_public_city("yuki", "kumominato:muginohi", duration_s=1800)
        self.daemon.fast_forward(3)
        sessions = self.ext.active_sessions()
        self.assertEqual(sessions["nell"]["encounters"], ["小葵·海风寮"])
        self.assertEqual(sessions["yuki"]["encounters"], [])

    # -- going home ----------------------------------------------------------------------------------
    def test_return_home_failure_is_raised_kept_and_retried(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.assertTrue(self.accepted("nell", "away"))
        self.city.fail_next[("tok_nell", "leave_world")] = 1
        with self.assertRaises(CityToolError):
            self.ext.return_home("nell")
        sess = self.ext.active_sessions()["nell"]
        self.assertIn("leave_world", sess["last_error"])
        self.assertFalse(self.accepted("nell", "living"))  # not home until the city let go
        self.daemon.fast_forward(32)
        self.daemon.fast_forward(2)
        self.assertFalse(self.ext.owns("nell"))
        self.assertTrue(self.accepted("nell", "living"))
        self.assertIsNotNone(self.daemon.authority.life.get("city_fed:last_memo:nell"))

    def test_leave_reply_lost_is_settled_by_looking_not_by_leaving_twice(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.city.drop_reply.add(("tok_nell", "leave_world"))  # she left the city, the answer was lost
        self.daemon.fast_forward(3)
        with self.assertRaises(TimeoutError):
            self.ext.return_home("nell")
        self.assertTrue(self.ext.active_sessions()["nell"]["needs_check"])
        self.assertFalse(self.accepted("nell", "living"))
        self.ext.return_home("nell")
        # The lost leave dropped the session's city context, so the check had to re-enter (claim=false) and
        # found her ours again: the second leave_world is the price. Never sent when control is not ours.
        enters = [a for t, n, a in self.city.args if t == "tok_nell" and n == "enter_world"]
        self.assertEqual(len(enters), 2)
        self.assertIs(enters[1]["claim"], False)
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 2)
        self.assertFalse(self.city.res["tok_nell"]["in_city"])
        self.assertTrue(self.accepted("nell", "living"))
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.owns("nell"))

    def test_control_lost_during_the_trip_stops_every_cleanup_and_keeps_the_record(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=600)
        self.city.controlled_elsewhere.add("tok_nell")  # another client took the resident over
        with self.assertRaises(CityControlError):
            self.ext.return_home("nell")
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)
        self.assertTrue(self.city.res["tok_nell"]["in_city"])  # the other client's session is untouched
        self.assertTrue(self.ext.active_sessions()["nell"]["control_lost"])
        self.assertFalse(self.accepted("nell", "living"))
        before = len(self.city.calls)
        self.daemon.fast_forward(30)  # the step does not retry or compensate on its own
        self.assertEqual(len(self.city.calls), before)
        self.assertTrue(self.ext.owns("nell"))
        # the owner decides: she comes home, the other client's resident is still left alone
        res = self.daemon.return_city_outing("nell", force=True)
        self.assertTrue(res["forced"] and res["returned"])
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.owns("nell"))
        self.assertNotEqual(self.location("nell"), "away", self.submits)  # the kernel walked her back in

    def test_polling_notices_a_control_change_and_stops(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.city.controlled_elsewhere.add("tok_nell")
        self.daemon.fast_forward(3)
        self.assertTrue(self.ext.active_sessions()["nell"]["control_lost"])
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)

    def test_one_roommates_failed_return_does_not_block_another(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=600)
        self.ext.depart_public_city("yuki", KISSA, duration_s=600)
        self.city.fail_always.add(("tok_nell", "leave_world"))
        self.daemon.fast_forward(12)
        self.assertTrue(self.ext.owns("nell"))
        self.assertFalse(self.ext.owns("yuki"))
        self.assertIn("leave_world", self.ext.active_sessions()["nell"]["last_error"])

    def test_kernel_refusing_the_walk_home_keeps_the_record_after_the_city_let_go(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.daemon.fast_forward(3)
        self.preempt.add("nell")
        with self.assertRaises(CityOutingError):
            self.ext.return_home("nell")
        rec = self.ext.active_sessions()["nell"]
        self.assertTrue(rec["remote_left"])  # recorded at once: never ask the city to leave twice
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertIn(("nell", "living", "failed"), self.submits)
        self.daemon.fast_forward(5)  # the step retries only the local half
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)

    # -- lost MCP session / stuck return ------------------------------------------------------------
    def test_lost_session_reenters_without_claiming_before_leaving(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.daemon.fast_forward(3)
        self.ext.clients["nell"].reset_session()  # e.g. the house restarted: new MCP session, no city context
        self.ext.return_home("nell")
        self.assertTrue(self.accepted("nell", "living"))
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.owns("nell"))
        enters = [a for t, n, a in self.city.args if t == "tok_nell" and n == "enter_world"]
        self.assertEqual(len(enters), 2)
        self.assertIs(enters[1]["claim"], False)
        self.assertNotIn("entry", enters[1])
        self.assertEqual(enters[1]["client_id"], enters[0]["client_id"])
        self.assertFalse(self.city.res["tok_nell"]["in_city"])  # really left, not just "returned"

    def test_lost_session_while_polling_reenters(self):
        self.city.place_player("tok_other", "小葵·海风寮", KISSA)
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.ext.clients["nell"].reset_session()
        self.daemon.fast_forward(3)
        self.assertEqual(self.ext.active_sessions()["nell"]["encounters"], ["小葵·海风寮"])

    def test_http_404_forgets_the_session(self):
        c = StdlibMcpCityClient("https://city.example.com/mcp", token="tok")
        c.session_id, c._initialized, c.entered = "sid", True, True
        err = urllib.error.HTTPError("https://city.example.com/mcp", 404, "Not Found", {}, None)
        with mock.patch("ai_house.builtin_plugins.city_mcp_outing.urllib.request.urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                c._rpc("tools/call", {"name": "leave_world", "arguments": {}})
        self.assertIsNone(c.session_id)
        self.assertFalse(c._initialized)
        self.assertFalse(c.entered)

    def test_stuck_return_stops_retrying_and_force_home_needs_the_owners_word(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=600)
        self.city.fail_always.add(("tok_nell", "leave_world"))
        self.daemon.fast_forward(25)
        sess = self.ext.active_sessions()["nell"]
        self.assertTrue(sess["stuck"])
        self.assertEqual(sess["return_failures"], self.ext.MAX_RETURN_FAILURES)
        attempts = self.city.count("leave_world", "tok_nell")
        self.daemon.fast_forward(10)
        self.assertEqual(self.city.count("leave_world", "tok_nell"), attempts)  # no more automatic retries
        self.assertFalse(self.accepted("nell", "living"))
        with self.assertRaises(CityToolError):  # a plain force still will not pretend the city let go
            self.daemon.return_city_outing("nell", force=True)
        self.assertTrue(self.ext.owns("nell"))
        res = self.daemon.return_city_outing("nell", force=True, abandon_remote=True)
        self.assertTrue(res["forced"] and res["returned"])
        self.assertTrue(self.accepted("nell", "living"))
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.owns("nell"))
        self.assertIsNotNone(self.daemon.authority.life.get("city_fed:abandoned:nell"))  # the audit note

    def test_force_home_refuses_when_control_cannot_be_confirmed(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.city.fail_always.add(("tok_nell", "get_my_view"))
        with self.assertRaises(CityControlError):
            self.ext.force_home("nell")
        self.assertTrue(self.ext.owns("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)

    # -- the process dies in the middle of a departure ------------------------------------------------
    def test_crash_during_enter_is_picked_up_after_restart_without_anyone_flagging_it(self):
        self.city.crash_after.add(("tok_nell", "enter_world"))  # the city entered her, then we died
        with self.assertRaises(SystemExit):
            self.ext.depart_public_city("nell", KISSA)
        rec = self.ext.active_sessions()["nell"]
        self.assertFalse(rec["needs_check"])  # nobody got to flag it: the record alone must be enough
        self.assertFalse(self.ext.owns("nell"))
        self.boot()
        self.assertTrue(self.ext.has_record("nell"))
        self.daemon.fast_forward(3)  # the step settles the leftover on its own
        self.assertFalse(self.ext.has_record("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertFalse(self.city.res["tok_nell"]["in_city"])

    def test_crash_after_the_kernel_accepted_the_move_adopts_the_trip_instead_of_tearing_it_down(self):
        real_put = self.ext._put

        def dying_put(actor_id, sess):
            if sess.get("local_moved"):
                raise SystemExit("process died before saving 'she is out'")
            return real_put(actor_id, sess)

        self.ext._put = dying_put
        with self.assertRaises(SystemExit):
            self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.assertTrue(self.accepted("nell", "away"))  # the kernel really has her walking out
        rec = self.ext.active_sessions()["nell"]
        self.assertTrue(rec["local_pending"] and not rec["local_moved"])
        self.boot()
        self.daemon.fast_forward(3)
        rec = self.ext.active_sessions()["nell"]
        self.assertTrue(rec["local_moved"])
        self.assertTrue(self.ext.owns("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)  # the city side was not torn down under her
        self.assertEqual(self.location("nell"), "away")
        self.ext.return_home("nell")  # and the normal way home still works
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.has_record("nell"))

    def test_a_record_is_adopted_only_for_this_trips_own_move(self):
        """Another kernel action of hers (or her being somewhere else) must not be taken for this trip's move out."""
        real_put = self.ext._put

        def dying_put(actor_id, sess):
            if sess.get("local_moved"):
                raise SystemExit("process died before saving 'she is out'")
            return real_put(actor_id, sess)

        self.ext._put = dying_put
        with self.assertRaises(SystemExit):
            self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.ext._put = real_put
        rec = self.ext.active_sessions()["nell"]
        rec["local_tag"] = "cityfed:go:nell:someone-elses-move"  # the recorded move is not the action in the kernel
        real_put("nell", rec)
        self.assertIsNone(self.ext._move_state("nell", "cityfed:go:nell:someone-elses-move"))
        self.assertEqual(self.ext.reconcile("nell"), "released")  # not adopted: the city side is settled the careful way
        self.assertFalse(self.ext.has_record("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)

    def test_a_move_out_the_kernel_later_fails_does_not_leave_her_marked_as_away(self):
        """Accepted is not done: if the kernel's own settle-time guard ends the move as failed she never left; the
        record must not keep her day stopped, the city side is settled, and no 'came back' memory is written."""
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.assertTrue(self.ext.owns("nell"))
        real_ext_door = self.ext.is_door_move
        self.ext.is_door_move = lambda *_: False  # the door closes before the kernel settles the move
        self.daemon.fast_forward(3)
        self.ext.is_door_move = real_ext_door
        self.assertNotEqual(self.location("nell"), "away")
        self.daemon.fast_forward(3)
        self.assertFalse(self.ext.has_record("nell"))
        self.assertFalse(self.ext.owns("nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertFalse(self.city.res["tok_nell"]["in_city"])
        self.assertIsNone(self.daemon.authority.life.get("city_fed:last_memo:nell"))

    def test_experimental_city_actions_require_control_and_reach_city(self):
        with self.assertRaises(CityOutingError):
            self.ext.say("nell", "还没出门不能说话")
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.daemon.fast_forward(3)
        res_say = self.ext.say("nell", "咖啡好香")
        self.assertTrue(res_say["ok"])
        self.ext.drift_book("nell", "item_book_1", note="下雨天读完了")
        self.ext.take_book("nell", "item_book_2")
        wres = self.ext.world_wait("nell", cursor="cur_0", timeout_ms=500)
        self.assertEqual(wres["cursor"], "cur_1")
        acts = [a["action"] for t, n, a in self.city.args if t == "tok_nell" and n == "world_act"]
        self.assertEqual([a["type"] for a in acts], ["travel", "say", "drift_book", "take_book"])
        self.city.controlled_elsewhere.add("tok_nell")
        with self.assertRaises(CityControlError):
            self.ext.say("nell", "失去控制后禁止代发")

    def test_a_session_the_city_forgot_is_looked_at_again_instead_of_getting_stuck(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.daemon.fast_forward(3)
        self.city.ctx["tok_nell"] = False  # NOT_IN_CITY for this session although we never left
        self.daemon.fast_forward(40)  # past return_at
        self.daemon.fast_forward(15)
        self.assertFalse(self.ext.has_record("nell"))
        self.assertFalse(self.city.res["tok_nell"]["in_city"])
        rec = self.daemon.authority.life.get("city_fed:last_memo:nell")
        self.assertIsNotNone(rec)

    def test_a_recovery_check_never_sends_the_profile(self):
        """Upstream updates an existing resident's bio BEFORE it decides who controls her, so a restart check that
        re-enters with a profile could overwrite what another client published."""
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.daemon.fast_forward(3)
        first = [a for t, n, a in self.city.args if t == "tok_nell" and n == "enter_world"]
        self.assertIn("name", first[0])  # the departure itself is her own entry
        self.city.res["tok_nell"]["bio"] = "published by another client"
        self.city.controlled_elsewhere.add("tok_nell")
        self.ext.clients["nell"].reset_session()  # restart: a fresh session has no city context
        self.daemon.fast_forward(40)  # past return_at: the return verifies control first
        probes = [a for t, n, a in self.city.args if t == "tok_nell" and n == "enter_world"][1:]
        self.assertTrue(probes)
        for a in probes:
            self.assertNotIn("name", a)
            self.assertNotIn("bio", a)
            self.assertNotIn("entry", a)
            self.assertIs(a["claim"], False)
        self.assertEqual(self.city.res["tok_nell"]["bio"], "published by another client")
        self.assertTrue(self.ext.pending_checks()["nell"]["control_lost"])
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 0)

    def test_home_is_not_claimed_without_the_kernels_word(self):
        """No federation record is not evidence of arrival: a roommate out on an ordinary plan is still away."""
        res = self.daemon.return_city_outing("nell")  # never went anywhere
        self.assertFalse(res["returned"])
        self.assertTrue(res["home"])  # she is at home, the kernel says so
        self.door_open = True
        self.daemon.authority.life.submit("nell", "move", {"to": "away"}, "ordinary-plan")
        self.daemon.fast_forward(3)
        self.assertEqual(self.location("nell"), "away")
        self.assertFalse(self.ext.has_record("nell"))
        for _ in range(2):  # and asking again does not change the answer
            res = self.daemon.return_city_outing("nell")
            self.assertFalse(res["returned"])
            self.assertFalse(res["home"])
            self.assertFalse(res["trip_open"])
        self.assertEqual(self.city.calls, [])  # the city was never touched

    def test_an_accepted_walk_home_is_not_arrival(self):
        self.ext.depart_public_city("nell", KISSA, duration_s=1800)
        self.daemon.fast_forward(3)
        self.ext.return_home("nell")
        self.assertTrue(self.accepted("nell", "living"))
        self.assertTrue(self.ext.has_record("nell"))  # accepted, not yet home
        self.assertTrue(self.ext.active_sessions()["nell"]["remote_left"])
        self.assertIsNone(self.daemon.authority.life.get("city_fed:last_memo:nell"))  # no "came back" memo yet
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.daemon.fast_forward(15)  # the step keeps asking the kernel; the city is not asked to leave again
        self.assertFalse(self.ext.has_record("nell"))
        self.assertIsNotNone(self.daemon.authority.life.get("city_fed:last_memo:nell"))
        self.assertEqual(self.city.count("leave_world", "tok_nell"), 1)
        self.assertNotEqual(self.location("nell"), "away")

    def test_a_leftover_record_does_not_stop_her_day(self):
        self.city.controlled_elsewhere.add("tok_nell")
        with self.assertRaises(CityControlError):
            self.ext.depart_public_city("nell", KISSA)
        self.assertTrue(self.ext.has_record("nell"))
        self.assertFalse(self.ext.owns("nell"))  # life.py skips a roommate's whole day while an extension owns her


class EnginePinTests(unittest.TestCase):
    FULL = "971484c13004e14ee6e91e9a9a75c0bd5ec65020"

    def test_compose_and_dockerfile_pin_the_full_upstream_commit(self):
        import re
        for rel in ("compose.yaml", "deploy/city-engine/Dockerfile"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(f"BOT_GAME_MCP_REF{':' if rel.endswith('yaml') else '='} {self.FULL}".replace("= ", "="), text, rel)
            # no abbreviated copy of the commit left anywhere in the file
            for m in re.finditer(r"971484c[0-9a-f]*", text):
                self.assertEqual(m.group(0), self.FULL, rel)


if __name__ == "__main__":
    unittest.main()
