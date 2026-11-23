"""Stage 4.1: `ai-house doctor --onebot-ping` against a fake OneBot v11 forward-WebSocket server.

All account numbers and tokens below are made up; nothing here touches a real QQ account.
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

from ai_house import cli  # noqa: E402
from ai_house.onebot_probe import probe_onebot  # noqa: E402

BOT_ID = 90001
OWNER_ID = 10001
WS_TOKEN = "-".join(["fake", "onebot", "probe", "x" * 8])


class FakeProtocolEnd:
    """Answers get_version_info / get_login_info like a OneBot v11 implementation; records what it was asked."""

    def __init__(self, token: str = WS_TOKEN, mode: str = "ok") -> None:
        self.token = token
        self.mode = mode  # ok | logged_out | silent | retcode_1403 | drop
        self.actions: List[Dict[str, Any]] = []
        self.auth_headers: List[str] = []
        self.server = serve(self._handler, "127.0.0.1", 0, process_request=self._check)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()

    def _check(self, connection: Any, request: Any) -> Optional[Any]:
        got = request.headers.get("Authorization") or ""
        self.auth_headers.append(got)
        if self.token and got != f"Bearer {self.token}":
            return connection.respond(401, "unauthorized\n")
        return None

    def _handler(self, ws: Any) -> None:
        try:
            # a real protocol end pushes a lifecycle event first; the probe has to skip it
            ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": BOT_ID}))
            for raw in ws:
                d = json.loads(raw)
                self.actions.append(d)
                if self.mode == "silent":
                    continue
                if self.mode == "drop":
                    ws.close()
                    return
                echo, action = d.get("echo"), d.get("action")
                if self.mode == "retcode_1403":
                    ws.send(json.dumps({"status": "failed", "retcode": 1403, "data": None, "echo": echo}))
                elif action == "get_version_info":
                    ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo,
                                        "data": {"app_name": "FakeEnd", "app_version": "1.2.3", "protocol_version": "v11"}}))
                elif action == "get_login_info":
                    if self.mode == "logged_out":
                        ws.send(json.dumps({"status": "failed", "retcode": 100, "data": None, "echo": echo}))
                    else:
                        ws.send(json.dumps({"status": "ok", "retcode": 0, "echo": echo,
                                            "data": {"user_id": BOT_ID, "nickname": "室友小号"}}))
        except ConnectionClosed:
            pass


class ProbeTests(unittest.TestCase):
    def fake(self, **kw: Any) -> FakeProtocolEnd:
        srv = FakeProtocolEnd(**kw)
        self.addCleanup(srv.close)
        return srv

    def test_ok_reports_implementation_and_never_sends_a_message_or_prints_ids(self) -> None:
        srv = self.fake()
        res = probe_onebot(srv.url, WS_TOKEN, owner_ids=[str(OWNER_ID)], timeout_s=5)
        self.assertEqual((res["ok"], res["code"]), (True, "ok"), res)
        self.assertIn("FakeEnd 1.2.3", res["detail"])
        self.assertNotIn(str(BOT_ID), json.dumps(res, ensure_ascii=False))
        self.assertNotIn(WS_TOKEN, json.dumps(res, ensure_ascii=False))
        self.assertEqual(sorted(a["action"] for a in srv.actions), ["get_login_info", "get_version_info"])
        self.assertEqual(srv.auth_headers, [f"Bearer {WS_TOKEN}"])

    def test_wrong_and_missing_token_are_told_apart(self) -> None:
        srv = self.fake()
        wrong = probe_onebot(srv.url, WS_TOKEN + "-typo", timeout_s=5)
        self.assertEqual((wrong["ok"], wrong["code"]), (False, "unauthorized"), wrong)
        self.assertIn("不一致", wrong["detail"])
        self.assertNotIn("typo", wrong["detail"])
        missing = probe_onebot(srv.url, "", timeout_s=5)
        self.assertEqual(missing["code"], "unauthorized")
        self.assertIn("是空的", missing["detail"])
        self.assertEqual(srv.actions, [], "nothing may be sent on a refused connection")

    def test_token_rejected_inside_the_protocol(self) -> None:
        srv = self.fake(token="", mode="retcode_1403")
        res = probe_onebot(srv.url, WS_TOKEN, timeout_s=5)
        self.assertEqual((res["ok"], res["code"]), (False, "unauthorized"), res)

    def test_connected_but_not_logged_in(self) -> None:
        srv = self.fake(mode="logged_out")
        res = probe_onebot(srv.url, WS_TOKEN, timeout_s=5)
        self.assertEqual((res["ok"], res["code"]), (False, "not_logged_in"), res)
        self.assertIn("还没有登录", res["detail"])

    def test_owner_number_equal_to_the_logged_in_account_is_a_config_mistake(self) -> None:
        srv = self.fake()
        res = probe_onebot(srv.url, WS_TOKEN, owner_ids=[str(OWNER_ID), str(BOT_ID)], timeout_s=5)
        self.assertEqual((res["ok"], res["code"]), (False, "owner_is_bot"), res)
        self.assertNotIn(str(BOT_ID), res["detail"])

    def test_port_that_never_answers_times_out_instead_of_hanging(self) -> None:
        srv = self.fake(mode="silent")
        res = probe_onebot(srv.url, WS_TOKEN, timeout_s=1.0)
        self.assertEqual((res["ok"], res["code"]), (False, "no_answer"), res)

    def test_connection_dropped_before_the_answer(self) -> None:
        srv = self.fake(mode="drop")
        res = probe_onebot(srv.url, WS_TOKEN, timeout_s=3.0)
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], ("dropped", "no_answer"), res)

    def test_nothing_listening_gives_the_docker_hint(self) -> None:
        with contextlib.closing(socket.socket()) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        res = probe_onebot(f"ws://127.0.0.1:{port}", WS_TOKEN, timeout_s=2.0)
        self.assertEqual((res["ok"], res["code"]), (False, "unreachable"), res)
        self.assertIn("host.docker.internal", res["detail"])

    def test_plain_http_port_is_not_mistaken_for_onebot(self) -> None:
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        res = probe_onebot(f"ws://127.0.0.1:{httpd.server_port}", "", timeout_s=3.0)
        self.assertEqual((res["ok"], res["code"]), (False, "bad_handshake"), res)

    def test_bad_scheme(self) -> None:
        self.assertEqual(probe_onebot("http://127.0.0.1:3001")["code"], "bad_url")


class DoctorCliTests(unittest.TestCase):
    def run_doctor(self, env: Dict[str, str], *flags: str) -> Dict[str, Any]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / ".env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
        out = io.StringIO()
        clean = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("ONEBOT_", "AI_HOUSE_"))}
        with mock.patch.dict("os.environ", clean, clear=True), contextlib.redirect_stdout(out):
            rc = cli.main(["doctor", "--env-file", str(env_file), "--data-dir", str(Path(tmp.name) / "rt"), "--json", *flags])
        doc = json.loads(out.getvalue())
        doc["rc"] = rc
        doc["raw"] = out.getvalue()
        return doc

    def test_doctor_onebot_ping_passes_against_a_logged_in_protocol_end(self) -> None:
        srv = FakeProtocolEnd()
        self.addCleanup(srv.close)
        doc = self.run_doctor(
            {"ONEBOT_WS_URL": srv.url, "ONEBOT_ACCESS_TOKEN": WS_TOKEN, "ONEBOT_OWNER_QQ": str(OWNER_ID)}, "--onebot-ping")
        live = next(c for c in doc["checks"] if c["name"] == "onebot_live_ping")
        self.assertTrue(live["ok"], live)
        self.assertEqual(doc["rc"], 0, doc)
        self.assertNotIn(WS_TOKEN, doc["raw"])
        self.assertNotIn(str(BOT_ID), doc["raw"])

    def test_doctor_fails_with_a_reason_when_the_token_is_wrong(self) -> None:
        srv = FakeProtocolEnd()
        self.addCleanup(srv.close)
        doc = self.run_doctor(
            {"ONEBOT_WS_URL": srv.url, "ONEBOT_ACCESS_TOKEN": WS_TOKEN + "-typo", "ONEBOT_OWNER_QQ": str(OWNER_ID)},
            "--onebot-ping")
        live = next(c for c in doc["checks"] if c["name"] == "onebot_live_ping")
        self.assertFalse(live["ok"])
        self.assertIn("ONEBOT_ACCESS_TOKEN", live["detail"])
        self.assertEqual(doc["rc"], 1)
        self.assertNotIn("typo", doc["raw"])

    def test_without_the_flag_nothing_is_connected(self) -> None:
        srv = FakeProtocolEnd()
        self.addCleanup(srv.close)
        doc = self.run_doctor({"ONEBOT_WS_URL": srv.url, "ONEBOT_ACCESS_TOKEN": WS_TOKEN, "ONEBOT_OWNER_QQ": str(OWNER_ID)})
        self.assertEqual([c for c in doc["checks"] if c["name"] == "onebot_live_ping"], [])
        self.assertEqual(srv.auth_headers, [])
        self.assertIn("--onebot-ping", next(c for c in doc["checks"] if c["name"] == "onebot_ws")["detail"])

    def test_flag_without_onebot_config_is_reported(self) -> None:
        doc = self.run_doctor({}, "--onebot-ping")
        live = next(c for c in doc["checks"] if c["name"] == "onebot_live_ping")
        self.assertFalse(live["ok"])

    def test_invalid_config_is_not_pinged(self) -> None:
        srv = FakeProtocolEnd()
        self.addCleanup(srv.close)
        doc = self.run_doctor({"ONEBOT_WS_URL": srv.url, "ONEBOT_ACCESS_TOKEN": WS_TOKEN, "ONEBOT_OWNER_QQ": ""}, "--onebot-ping")
        self.assertEqual([c for c in doc["checks"] if c["name"] == "onebot_live_ping"], [])
        self.assertEqual(srv.auth_headers, [])
        self.assertEqual(doc["rc"], 1)


if __name__ == "__main__":
    unittest.main()
