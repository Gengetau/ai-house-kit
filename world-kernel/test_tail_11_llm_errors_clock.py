"""Tail 11: newcomer-facing model-connection errors (Chinese reason + next step) and virtual-clock restart.

Part 1 is table-driven over `explain_llm_error`, then one test per surface (setup wizard "test connection",
`doctor --live-ping`, chat fallback note) against a local stub HTTP server.
Part 2 checks that `up --virtual-clock` resumes from the latest house time after a restart.
Fake keys are built at runtime.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import io
import json
import socket
import ssl
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ai_house import cli  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import JST, HouseDaemon, OffsetWallClock  # noqa: E402
from ai_house.llm import LLMAPIError, LLMDeadlineError, explain_llm_error  # noqa: E402

PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
FAKE_KEY = "sk-" + "x" * 20


def _http(code: int, msg: str, err_code: str = "", param: str = "", detail: str = "") -> LLMAPIError:
    body = "; ".join(p for p in (msg, f"param={param}" if param else "", f"code={err_code}" if err_code else "") if p)
    return LLMAPIError(code, f"HTTP {code}: {body}", detail=body or detail)


EMPTY = LLMAPIError(502, "HTTP 502: model returned an empty reply", detail="empty_reply")

# (label, error, substrings that must be in the detailed text, original text that must survive in it)
CASES: List[Tuple[str, Any, List[str], str]] = [
    ("401", _http(401, f"Incorrect API key provided: {FAKE_KEY}", "invalid_api_key"), ["Key 不对或没有权限", "AI_HOUSE_API_KEY", "设置页"], "Incorrect API key provided"),
    ("403", _http(403, "Forbidden"), ["Key 不对或没有权限", "AI_HOUSE_API_KEY"], "Forbidden"),
    ("404 model_not_found", _http(404, "The model `x` does not exist", "model_not_found"), ["模型名不存在", "AI_HOUSE_MODEL"], "model_not_found"),
    ("model_not_found without status", "The model `x` does not exist; code=model_not_found", ["模型名不存在", "AI_HOUSE_MODEL"], "does not exist"),
    ("429", _http(429, "Rate limit reached"), ["请求太频繁", "额度"], "Rate limit reached"),
    ("500", _http(500, "Internal server error"), ["服务方出错", "稍后再试"], "Internal server error"),
    ("503", _http(503, "overloaded"), ["服务方出错", "稍后再试"], "overloaded"),
    ("timeout", TimeoutError("timed out"), ["太慢", "AI_HOUSE_BASE_URL", "代理", "网络"], "TimeoutError: timed out"),
    ("timeout in URLError", urllib.error.URLError(socket.timeout("timed out")), ["太慢", "AI_HOUSE_BASE_URL"], "timed out"),
    ("deadline", LLMDeadlineError("LLM 总时限 5.0s 已耗尽，已停止重试与回退"), ["太慢", "AI_HOUSE_BASE_URL"], "LLM 总时限"),
    (
        "connection refused",
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
        ["地址或端口不对", "本地模型没有启动", "AI_HOUSE_BASE_URL"],
        "Connection refused",
    ),
    ("connection refused text", "URLError: <urlopen error [Errno 111] Connection refused>", ["地址或端口不对", "本地模型"], "[Errno 111]"),
    (
        "dns",
        urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
        ["接口地址写错", "没有联网", "AI_HOUSE_BASE_URL"],
        "Name or service not known",
    ),
    (
        "tls",
        urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")),
        ["证书", "https"],
        "CERTIFICATE_VERIFY_FAILED",
    ),
    (
        "400 parameter",
        _http(400, "Unsupported parameter: 'max_tokens' is not supported with this model", "unsupported_parameter", param="max_tokens"),
        ["模型拒绝了请求里的某个参数"],
        "Unsupported parameter: 'max_tokens'",
    ),
    ("empty reply", EMPTY, ["模型返回了空回复", "重试"], "model returned an empty reply"),
]

# kept as today: no Chinese hint, the original text only
UNKNOWN: List[Tuple[Any, str]] = [
    (_http(418, "I am a teapot"), "HTTP 418: I am a teapot"),
    (_http(400, "Bad request body"), "HTTP 400: Bad request body"),
    (ValueError("weird failure"), "ValueError: weird failure"),
]


class ExplainLlmErrorTests(unittest.TestCase):
    def test_table_detail_mode(self) -> None:
        for label, err, needles, original in CASES:
            with self.subTest(label):
                out = explain_llm_error(err, secrets=[FAKE_KEY])
                for n in needles:
                    self.assertIn(n, out)
                self.assertIn(original, out)  # the provider's own text stays after the hint
                self.assertIn("原始信息：", out)
                self.assertLess(out.index(needles[0]), out.index(original))
                self.assertNotIn(FAKE_KEY, out)
                self.assertNotIn("x" * 12, out)

    def test_table_short_mode_has_no_raw_text(self) -> None:
        for label, err, needles, original in CASES:
            with self.subTest(label):
                out = explain_llm_error(err, secrets=[FAKE_KEY], with_detail=False)
                self.assertIn(needles[0], out)
                self.assertNotIn(original, out)
                for bad in ("HTTP ", "http", "Errno", "原始信息", FAKE_KEY, "xxxx", "127.0.0.1"):
                    self.assertNotIn(bad, out)
                self.assertLess(len(out), 60)

    def test_unknown_errors_keep_todays_text(self) -> None:
        for err, text in UNKNOWN:
            with self.subTest(text):
                self.assertEqual(explain_llm_error(err), text)
                short = explain_llm_error(err, with_detail=False)
                self.assertNotIn(text, short)
                self.assertIn("原因不明", short)

    def test_config_error_text_is_kept_in_both_modes(self) -> None:
        err = LLMAPIError(0, "配置错误：该住客配置了 base_url 但没有自己的 api_key")
        self.assertEqual(explain_llm_error(err), str(err))
        self.assertEqual(explain_llm_error(err, with_detail=False), str(err))

    def test_key_is_masked_even_for_unknown_and_string_errors(self) -> None:
        self.assertNotIn(FAKE_KEY, explain_llm_error(f"weird: {FAKE_KEY}", secrets=[FAKE_KEY]))
        self.assertNotIn(FAKE_KEY, explain_llm_error(f"weird: {FAKE_KEY}"))  # sk-* pattern even without the key itself
        short_key = "plainkey" + "9" * 12  # not sk-shaped: needs the explicit secret
        out = explain_llm_error(_http(401, f"bad key {short_key}"), secrets=[short_key])
        self.assertNotIn(short_key, out)
        self.assertNotIn(short_key[:10], out)

    def test_http_error_string_is_classified(self) -> None:
        out = explain_llm_error("HTTP 429: slow down")
        self.assertIn("限", out)
        self.assertIn("HTTP 429: slow down", out)


# ------------------------------------------------------------------------------------------ surfaces
class Stub:
    """Local OpenAI-compatible endpoint answering every POST with (status, doc)."""

    def __init__(self, status: int, doc: Dict[str, Any]) -> None:
        self.hits = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.hits += 1
                out = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def _err_doc(message: str, code: str) -> Dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


class Base(unittest.TestCase):
    def tmpdir(self) -> str:
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return t.name

    def stub(self, status: int, doc: Dict[str, Any]) -> Stub:
        s = Stub(status, doc)
        self.addCleanup(s.close)
        return s

    def daemon(self, rt: Optional[str] = None, env: Optional[Dict[str, str]] = None, **kw: Any) -> HouseDaemon:
        d = HouseDaemon(rt or self.tmpdir(), pack_dir=PACK, env=env or {}, allow_onboarding=False, **kw)
        self.addCleanup(d.close)
        return d


class SurfaceTests(Base):
    def test_setup_wizard_test_connection(self) -> None:
        d = self.daemon(virtual_clock=True)
        bad_key = self.stub(401, _err_doc(f"Incorrect API key provided: {FAKE_KEY}", "invalid_api_key"))
        res = d.test_llm_settings({"base_url": bad_key.base_url, "model": "m", "api_key": FAKE_KEY})
        self.assertFalse(res["ok"])
        for n in ("Key 不对或没有权限", "AI_HOUSE_API_KEY", "HTTP 401", "invalid_api_key"):
            self.assertIn(n, res["error"])
        self.assertNotIn(FAKE_KEY, res["error"])

        no_model = self.stub(404, _err_doc("The model `x` does not exist", "model_not_found"))
        res = d.test_llm_settings({"base_url": no_model.base_url, "model": "x", "api_key": FAKE_KEY})
        self.assertIn("模型名不存在", res["error"])
        self.assertIn("AI_HOUSE_MODEL", res["error"])
        self.assertIn("code=model_not_found", res["error"])

        res = d.test_llm_settings({"base_url": _closed_port_url(), "model": "m", "api_key": FAKE_KEY})
        self.assertIn("地址或端口不对", res["error"])
        self.assertIn("Errno", res["error"])
        self.assertNotIn(FAKE_KEY, res["error"])

    def test_doctor_live_ping(self) -> None:
        stub = self.stub(404, _err_doc("The model `x` does not exist", "model_not_found"))
        env = {"AI_HOUSE_WORLD_PACK": "life1", "AI_HOUSE_BASE_URL": stub.base_url, "AI_HOUSE_API_KEY": FAKE_KEY, "AI_HOUSE_MODEL": "x"}
        buf = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=False), contextlib.redirect_stdout(buf):
            rc = cli.main(["doctor", "--data-dir", self.tmpdir(), "--live-ping", "--json"])
        self.assertEqual(rc, 1)
        live = next(c for c in json.loads(buf.getvalue())["checks"] if c["name"] == "llm_live_ping")
        self.assertFalse(live["ok"])
        for n in ("模型名不存在", "AI_HOUSE_MODEL", "HTTP 404", "code=model_not_found"):
            self.assertIn(n, live["detail"])
        self.assertNotIn(FAKE_KEY, live["detail"])

    def test_chat_fallback_note_is_short_chinese_only(self) -> None:
        stub = self.stub(401, _err_doc(f"Incorrect API key provided: {FAKE_KEY}", "invalid_api_key"))
        d = self.daemon(
            env={"AI_HOUSE_OWNER_TOKEN": "owner-tail11", "AI_HOUSE_API_KEY": FAKE_KEY, "AI_HOUSE_BASE_URL": stub.base_url, "AI_HOUSE_MODEL": "m"},
            virtual_clock=True,
        )
        res = d.converse("private:sora", "你好呀", owner_speaker("房主"))
        txt = " ".join(str(x.get("text") or "") for x in res.get("replies") or [])
        self.assertIn("接口暂不可用 Key 不对或没有权限，已自动切换本地演示回复", txt)
        for bad in ("HTTP", "401", "invalid_api_key", "Incorrect", "127.0.0.1", FAKE_KEY, "xxxx"):
            self.assertNotIn(bad, txt)
        self.assertGreaterEqual(stub.hits, 1)


# ------------------------------------------------------------------------------------------ virtual clock
def _latest_event_ts(rt: str) -> float:
    with contextlib.closing(sqlite3.connect(str(Path(rt) / "life1.db"))) as c:
        return float(c.execute("SELECT MAX(MAX(recorded_at), MAX(occurred_at)) FROM events").fetchone()[0])


class VirtualClockRestartTests(Base):
    START = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()

    def test_fresh_virtual_clock_starts_at_fixed_point(self) -> None:
        d = self.daemon(virtual_clock=True)
        self.assertEqual(d.clock.now(), self.START)

    def test_restart_resumes_from_latest_house_time(self) -> None:
        rt = self.tmpdir()
        d1 = self.daemon(rt, virtual_clock=True)
        for _ in range(6):
            d1.fast_forward(240)  # 24 virtual hours
        reached = d1.clock.now()
        self.assertGreaterEqual(reached, self.START + 24 * 3600)
        d1.close()

        d2 = self.daemon(rt, virtual_clock=True)
        resumed = d2.clock.now()
        self.assertGreater(resumed, self.START + 20 * 3600)  # not back at 2026-11-21 09:00
        self.assertLessEqual(resumed, reached + 1)  # never ahead of where the house actually was
        self.assertEqual(resumed, _latest_event_ts(rt))
        d2.fast_forward(15)
        self.assertGreater(d2.clock.now(), resumed)

    def test_explicit_later_start_ts_still_wins(self) -> None:
        rt = self.tmpdir()
        d1 = self.daemon(rt, virtual_clock=True)
        d1.fast_forward(60)
        d1.close()
        later = self.START + 30 * 86400
        d2 = self.daemon(rt, virtual_clock=True, start_ts=later)
        self.assertEqual(d2.clock.now(), later)

    def test_real_clock_mode_is_unchanged(self) -> None:
        rt = self.tmpdir()
        d1 = self.daemon(rt, virtual_clock=True)
        d1.fast_forward(60)
        d1.close()
        d2 = self.daemon(rt)  # real clock on a data dir that has (virtual) history: still the wall clock + offset file
        self.assertIsInstance(d2.clock, OffsetWallClock)
        self.assertLess(abs(d2.clock.now() - dt.datetime.now().timestamp()), 5.0)

    def test_help_says_restart_continues(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["up", "--help"])
        flat = "".join(buf.getvalue().split())
        self.assertIn("重启后从小屋上次到达的时间继续", flat)
        self.assertNotIn("重启独立运行", flat)


if __name__ == "__main__":
    unittest.main()
