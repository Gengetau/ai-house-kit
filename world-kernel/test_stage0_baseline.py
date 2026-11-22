"""Regression tests for Stage 0 baseline fixes (PORTING-PLAN.md items 0.1-0.4)."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import cli
from ai_house.chat_agent import LLMAPIError, RoommateChatAgent
from ai_house.daemon import HouseDaemon, OffsetWallClock
from ai_house.memory import _openai_json_extractor
from worldkernel.store import VirtualClock

JST = dt.timezone(dt.timedelta(hours=9))


class _FixedClock:
    def __init__(self, ts: float):
        self._ts = float(ts)

    def now(self) -> float:
        return self._ts


class Stage0ClockTests(unittest.TestCase):
    def test_real_clock_fast_forward_overnight_sleeps_changes_outfit_and_cooks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 18, 0, tzinfo=JST).timestamp()
        clock = OffsetWallClock(rt / "clock_offset.json", base_clock=VirtualClock(t0))
        daemon = HouseDaemon(runtime_dir=rt, virtual_clock=False, clock=clock, env={})
        self.addCleanup(daemon.close)

        with contextlib.closing(sqlite3.connect(daemon.life_db)) as lc:
            init_outfits = lc.execute("SELECT count(*) FROM outfit_log").fetchone()[0]

        for _ in range(6):
            daemon.fast_forward(180)  # 18:00 -> next day 12:00 JST

        self.assertAlmostEqual(daemon.memory._now(), daemon.clock.now(), places=3)
        self.assertAlmostEqual(daemon.authority.life.clock.now(), daemon.clock.now(), places=3)

        with contextlib.closing(sqlite3.connect(daemon.life_db)) as lc:
            sleeps = lc.execute("SELECT count(*) FROM actions WHERE action_type='sleep'").fetchone()[0]
            outfits = lc.execute("SELECT count(*) FROM outfit_log").fetchone()[0]
            cooks = lc.execute("SELECT count(*) FROM actions WHERE action_type='cook'").fetchone()[0]

        self.assertGreaterEqual(sleeps, 6, f"expected >=6 sleep actions overnight, got {sleeps}")
        self.assertGreater(outfits, init_outfits, "expected wardrobe changes after overnight fast-forward")
        self.assertGreaterEqual(cooks, 1, "expected at least one cooked meal overnight/morning")

    def test_fast_forward_on_non_advance_clock_updates_life_and_memory_clocks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 18, 0, tzinfo=JST).timestamp()
        daemon = HouseDaemon(runtime_dir=rt, virtual_clock=False, clock=_FixedClock(t0), env={})
        self.addCleanup(daemon.close)

        for _ in range(5):
            daemon.fast_forward(180)  # 18:00 -> next day 09:00 JST

        self.assertIs(daemon.authority.life.clock, daemon.clock)
        self.assertIs(daemon.authority.store.clock, daemon.clock)
        self.assertIs(daemon.memory._clock, daemon.clock)
        with contextlib.closing(sqlite3.connect(daemon.life_db)) as lc:
            sleeps = lc.execute("SELECT count(*) FROM actions WHERE action_type='sleep'").fetchone()[0]
        self.assertGreaterEqual(sleeps, 6)

    def test_fast_forward_offset_persists_across_restart_and_clears_on_reset(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 9, 0, tzinfo=JST).timestamp()

        d1 = HouseDaemon(
            runtime_dir=rt,
            virtual_clock=False,
            clock=OffsetWallClock(rt / "clock_offset.json", base_clock=VirtualClock(t0)),
            env={},
        )
        for _ in range(6):
            d1.fast_forward(240)  # +24h
        d1.close()

        self.assertTrue((rt / "clock_offset.json").is_file())
        d2 = HouseDaemon(
            runtime_dir=rt,
            virtual_clock=False,
            clock=OffsetWallClock(rt / "clock_offset.json", base_clock=VirtualClock(t0)),
            env={},
        )
        self.addCleanup(d2.close)
        self.assertGreaterEqual(d2.get_state()["now"], t0 + 86400)

        d2.reset_house()
        self.assertFalse((rt / "clock_offset.json").exists())
        self.assertEqual(d2.clock.offset_s, 0.0)


class Stage0ErrandIntentTests(unittest.TestCase):
    def test_tool_promise_with_empty_or_chinese_item_falls_back_and_completes_shopping(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 21, 13, 30, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        # Case 1: Model returned errand intent without `item`
        d.intents.enqueue_chat(
            "nell",
            "帮我买 3 个洋葱",
            "好呀，我待会儿顺路买",
            where="group",
            intents=[{"text": "帮房主买 3 个洋葱", "kind": "errand", "qty": 3, "答应了的": ["nell"]}],
            now=d.clock.now(),
        )
        # Case 2: Model returned errand intent with Chinese label `鸡蛋` instead of canonical `egg`
        d.intents.enqueue_chat(
            "nell",
            "再带两个鸡蛋回来",
            "没问题，记下啦",
            where="group",
            intents=[{"text": "买鸡蛋", "kind": "errand", "item": "鸡蛋", "qty": 2, "答应了的": ["nell"]}],
            now=d.clock.now(),
        )

        rows = {r["item"]: r for r in d.intents.list_intents(actor="nell")}
        self.assertIn("onion", rows)
        self.assertEqual(rows["onion"]["qty"], 3)
        self.assertEqual(rows["onion"]["state"], "open")
        self.assertIn("egg", rows)
        self.assertEqual(rows["egg"]["qty"], 2)
        self.assertEqual(rows["egg"]["state"], "open")

        errands = d.intents.errands("nell", d.clock.now())
        self.assertEqual({e["item"] for e in errands}, {"onion", "egg"})

        d.fast_forward(60)  # Daytime supermarket errand runs inside 09:30-16:00 JST

        after = {r["item"]: r for r in d.intents.list_intents(actor="nell")}
        self.assertEqual(after["onion"]["state"], "completed")
        self.assertEqual(after["egg"]["state"], "completed")
        self.assertTrue(str(after["onion"]["evidence_key"]).startswith("ai-house/"))
        self.assertTrue(str(after["egg"]["evidence_key"]).startswith("ai-house/"))

    def test_record_promise_tool_schema_and_allergy_note_not_completed_by_cooking(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 21, 17, 45, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        specs = RoommateChatAgent(d.vocab)._builtin_tool_specs()
        rec_spec = next(s for s in specs if s["name"] == "record_promise")
        desc = rec_spec["parameters"]["properties"]["item"]["description"]
        self.assertIn("onion=洋葱", desc)
        self.assertIn("egg=鸡蛋", desc)

        d.intents.enqueue_chat(
            "nell",
            "记住我对花生过敏",
            "我记牢啦，绝不让你误食",
            where="private",
            intents=[{"text": "记得房主对花生过敏", "kind": "cook", "答应了的": ["nell"]}],
            now=d.clock.now(),
        )
        d.fast_forward(120)  # Crosses dinner cooking window 17:45 -> 19:45 JST
        allergy_rows = [r for r in d.intents.list_intents(actor="nell") if "花生过敏" in r["summary"]]
        self.assertTrue(allergy_rows)
        self.assertNotEqual(allergy_rows[0]["state"], "completed")


class Stage0ModelCompatTests(unittest.TestCase):
    def _start_mock_llm_server(self, mode: str, secret_key: str):
        recorded = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: D401
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(raw or "{}")
                recorded.append(payload)

                def _reply(status: int, doc: dict) -> None:
                    body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                if mode == "error_with_secret":
                    _reply(
                        400,
                        {
                            "error": {
                                "message": f"Invalid request using key {secret_key} and sk-mockLeak99887766",
                                "param": "model",
                                "code": "invalid_request_error",
                            }
                        },
                    )
                    return

                if "max_tokens" in payload or "temperature" in payload:
                    _reply(
                        400,
                        {
                            "error": {
                                "message": (
                                    "Unsupported parameter: 'max_tokens' is not supported with this model. "
                                    "Use 'max_completion_tokens' instead."
                                ),
                                "param": "max_tokens",
                                "code": "unsupported_parameter",
                            }
                        },
                    )
                    return

                if int(payload.get("max_completion_tokens") or 0) < 1024:
                    _reply(400, {"error": {"message": "max_completion_tokens too small"}})
                    return

                sys_txt = " ".join(str(m.get("content") or "") for m in payload.get("messages") or [])
                content = (
                    '{"ops": [{"op": "add", "kind": "preference", "content": "喜欢热茶"}]}'
                    if "extractor_json" in sys_txt
                    else "OK，没问题！"
                )
                _reply(
                    200,
                    {
                        "choices": [{"message": {"content": content}}],
                        "usage": {"total_tokens": 42, "completion_tokens": 18},
                    },
                )

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}", recorded

    def test_gpt5_compat_auto_adapts_test_llm_chat_and_memory_extractor(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        secret_key = "sk-localMockTestKey123456"
        base_url, recorded = self._start_mock_llm_server("gpt5_compat", secret_key)

        d = HouseDaemon(
            Path(tmp.name) / "rt",
            env={
                "AI_HOUSE_WORLD_PACK": "life1",
                "AI_HOUSE_BASE_URL": base_url,
                "AI_HOUSE_API_KEY": secret_key,
                "AI_HOUSE_MODEL": "gpt-5-mini",
            },
        )
        self.addCleanup(d.close)
        ping = d.test_llm_settings({"base_url": base_url, "model": "gpt-5-mini", "api_key": secret_key})
        self.assertTrue(ping["ok"], ping)
        self.assertEqual(ping["reply"], "OK，没问题！")
        self.assertGreaterEqual(len(recorded), 2)
        self.assertIn("max_tokens", recorded[0])
        self.assertNotIn("max_tokens", recorded[-1])
        self.assertNotIn("temperature", recorded[-1])
        self.assertGreaterEqual(recorded[-1]["max_completion_tokens"], 1024)

        before = len(recorded)
        res = d.send_chat("private:nell", "晚上好")
        replies = res.get("replies") or []
        self.assertTrue(replies)
        self.assertEqual(replies[0]["mode"], "live_api")
        self.assertEqual(replies[0]["text"], "OK，没问题！")
        self.assertGreaterEqual(len(recorded) - before, 2)
        self.assertNotIn("max_tokens", recorded[-1])
        self.assertNotIn("temperature", recorded[-1])

        ext = _openai_json_extractor(
            "extractor_json",
            "我喜欢喝热茶",
            api_key=secret_key,
            base_url=base_url,
            model="gpt-5-mini",
        )
        self.assertEqual(ext.get("ops")[0]["content"], "喜欢热茶")

    def test_http_400_error_surfaces_detail_and_redacts_secrets(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        secret_key = "sk-primarySecretKey654321"
        base_url, _recorded = self._start_mock_llm_server("error_with_secret", secret_key)

        rt = Path(tmp.name) / "rt"
        d = HouseDaemon(
            rt,
            env={
                "AI_HOUSE_WORLD_PACK": "life1",
                "AI_HOUSE_BASE_URL": base_url,
                "AI_HOUSE_API_KEY": secret_key,
                "AI_HOUSE_MODEL": "bad-model",
            },
        )
        self.addCleanup(d.close)
        ping = d.test_llm_settings({"base_url": base_url, "model": "bad-model", "api_key": secret_key})
        self.assertFalse(ping["ok"])
        self.assertIn("HTTP 400", ping["error"])
        self.assertIn("Invalid request", ping["error"])
        self.assertIn("[REDACTED]", ping["error"])
        self.assertNotIn(secret_key, ping["error"])
        self.assertNotIn("sk-mockLeak99887766", ping["error"])

        res = d.send_chat("private:nell", "你好")
        replies = res.get("replies") or []
        self.assertTrue(replies)
        self.assertEqual(replies[0]["mode"], "demo")
        self.assertIn("HTTP 400", replies[0]["text"])
        self.assertIn("[REDACTED]", replies[0]["text"])
        self.assertNotIn(secret_key, replies[0]["text"])
        self.assertNotIn("sk-mockLeak99887766", replies[0]["text"])

        with self.assertRaises(LLMAPIError) as ctx:
            _openai_json_extractor(
                "extractor_json",
                "test",
                api_key=secret_key,
                base_url=base_url,
                model="bad-model",
            )
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertNotIn(secret_key, str(ctx.exception))

        d.close()
        buf = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "AI_HOUSE_WORLD_PACK": "life1",
                    "AI_HOUSE_BASE_URL": base_url,
                    "AI_HOUSE_API_KEY": secret_key,
                    "AI_HOUSE_MODEL": "bad-model",
                },
                clear=False,
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = cli.main(["doctor", "--data-dir", str(rt), "--live-ping", "--json"])
        self.assertEqual(rc, 1)
        doc = json.loads(buf.getvalue())
        live = next(c for c in doc["checks"] if c["name"] == "llm_live_ping")
        self.assertFalse(live["ok"])
        self.assertIn("HTTP 400", live["detail"])
        self.assertIn("[REDACTED]", live["detail"])
        self.assertNotIn(secret_key, live["detail"])
        self.assertNotIn("sk-mockLeak99887766", live["detail"])


if __name__ == "__main__":
    unittest.main()
