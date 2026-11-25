"""Regression tests for Stage 0 baseline fixes (PORTING-PLAN.md items 0.1-0.4)."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import http.server
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import cli
from ai_house.chat_agent import LLMAPIError, RoommateChatAgent
from ai_house.daemon import HouseDaemon, OffsetWallClock, make_http_server
from ai_house.memory import _openai_json_extractor
from worldkernel import vocab_life1
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


def _owner_asks(d, actor, line, ops, now):
    """The owner says `line` to `actor` in private (she answers with a plain yes), then one forced memory extraction
    runs with a fake model that answers `ops`: the only way a promise comes to exist."""
    d.memory.observe(actor, "private", line, speaker_role="owner", speaker="房主", reply="好呀", now=now)
    d.memory.extract(force=True, now=now + 2, llm_fn=lambda system, user: {"ops": ops})


def _errand_op(text, quote):
    return {"op": "intent", "text": text, "kind": "promise", "for": ["房主"], "agreed": ["奈尔"], "scope": "world",
            "when": "vague", "quote": quote}


class Stage0ErrandIntentTests(unittest.TestCase):
    def test_two_agreed_errands_are_carried_out_by_the_kernel_and_closed_by_its_receipts(self):
        """Fails if an agreed errand (a quantity of 3, or a Chinese item label) is not read as a catalogue item, is not
        listed as an errand for her, is not carried out in the daytime shopping window, or is not closed with the
        kernel's own evidence key. (The `record_promise` tool that used to pass the item is gone, dropped item D2;
        the item is now read from the promise text.)"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 21, 13, 30, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        _owner_asks(d, "nell", "帮我买 3 个洋葱", [_errand_op("奈尔帮房主买3个洋葱", "帮我买 3 个洋葱")], d.clock.now())
        _owner_asks(d, "nell", "再带两个鸡蛋回来", [_errand_op("奈尔买2个鸡蛋", "再带两个鸡蛋回来")], d.clock.now() + 10)

        # a promise made in the owner's private chat is listed without its words, so the items are read from `errands`
        listed = d.intents.list_intents(actor="nell")
        self.assertEqual([r["state"] for r in listed], ["open", "open"])
        errands = d.intents.errands("nell", d.clock.now() + 30)
        self.assertEqual({(e["item"], e["qty"]) for e in errands}, {("onion", 3), ("egg", 2)})

        d.fast_forward(60)  # Daytime supermarket errand runs inside 09:30-16:00 JST

        self.assertEqual([r["state"] for r in d.intents.list_intents(actor="nell")], ["completed", "completed"])
        self.assertEqual(d.intents.errands("nell", d.clock.now() + 30), [])
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            refs = [json.loads(r[0]) for r in c.execute("SELECT refs FROM intents WHERE status='completed'")]
        keys = [next(f["kernel"] for f in ref if isinstance(f, dict) and f.get("kernel")) for ref in refs]
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(k.startswith("ai-house/") for k in keys), keys)

    def test_an_allergy_note_is_a_memory_that_cooking_does_not_touch(self):
        """Fails if the standing allergy note is turned into a promise (diet notes are memories now, dropped item D3),
        is changed or removed when she cooks dinner, or if no dinner was settled in the window (the control: the
        kernel did plan a meal for her, so 'not completed by cooking' is not just 'nothing happened'). The tool
        schema half of the old case went with the `record_promise` tool (dropped item D2)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 21, 17, 45, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        ops = [{"op": "add", "text": "房主对花生过敏", "kind": "preference", "about": ["房主"]}]
        _owner_asks(d, "nell", "记住我对花生过敏", ops, d.clock.now())
        notes = d.memory.list_entries("nell", "private")
        self.assertEqual([n["text"] for n in notes], ["房主对花生过敏"])
        self.assertEqual(d.intents.list_intents(actor="nell"), [])
        d.fast_forward(120)  # Crosses dinner cooking window 17:45 -> 19:45 JST
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            meals = [r[0] for r in c.execute("SELECT body FROM intent_evidence") if '"plan_kind": "meal"' in r[0]]
        self.assertTrue(meals, "no dinner was settled in the window")
        after = d.memory.list_entries("nell", "private")
        self.assertEqual([(n["id"], n["text"], n["status"]) for n in after], [(n["id"], n["text"], n["status"]) for n in notes])
        self.assertEqual(d.intents.list_intents(actor="nell"), [])

    def test_errand_not_closed_by_unrelated_arrival_and_handles_over_500_evidences(self):
        """Fails if an arrival at the park, or an egg purchase that belongs to no errand, closes an errand to buy eggs,
        if the errand is lost from `errands`, or if the errand's own purchase receipt is not seen behind 505 later unrelated evidence rows (the read window), or does
        not close it with that receipt's key. The errand is created through extraction (rule-based recording at chat
        time is gone, dropped item D1); the same promise being open before and completed after is the control."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        _owner_asks(d, "nell", "去超市买鸡蛋", [_errand_op("奈尔去超市买2个鸡蛋", "去超市买鸡蛋")], start + 10.0)

        with contextlib.closing(sqlite3.connect(d.world_db)) as c:  # the promise code reads only the bound kernel stream
            stream = c.execute("SELECT v FROM kernel_bind WHERE k='world'").fetchone()[0]

        def body(key, doc):  # the kernel's own rows carry their key and stream inside the body
            return json.dumps({**doc, "key": key, "stream": stream})

        # Case A: a park arrival and an egg purchase that belongs to no errand must NOT complete the errand
        errand = d.intents.errands("nell", start + 30.0)[0]
        self.assertEqual((errand["item"], errand["qty"]), ("egg", 2))
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, ?, ?, ?, ?)",
                    (
                        "ev-park-arrive",
                        stream,
                        1,
                        start + 20.0,
                        body("ev-park-arrive", {"kind": "arrived", "actor": "nell", "place": "place:park",
                                                "plan_id": "plan-park", "at": start + 20.0}),
                    ),
                )
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, ?, ?, ?, ?)",
                    (
                        "ev-buy-egg-stray",
                        stream,
                        2,
                        start + 25.0,
                        body("ev-buy-egg-stray", {"kind": "purchased", "actor": "nell", "item": "egg", "qty": 2,
                                                  "plan_id": "plan-park", "at": start + 25.0}),
                    ),
                )
        d.intents.sweep(start + 30.0)
        listed = d.intents.list_intents(actor="nell")
        self.assertEqual([r["state"] for r in listed], ["open"])
        self.assertEqual([(e["item"], e["qty"]) for e in d.intents.errands("nell", start + 30.0)], [("egg", 2)])

        # Case B: 505 unrelated evidence rows after the promise; the errand's own arrival at the supermarket and receipt behind them must still complete it
        arrival = {"kind": "arrived", "actor": "nell", "place": "place:supermarket", "plan_id": "plan-shop",
                   "at": start + 50.0}
        receipt = {"kind": "purchased", "actor": "nell", "item": "egg", "qty": 2, "plan_id": "plan-shop", "at": start + 60.0,
                   "intent_ref": errand["intent"], "account": errand["account"], "beneficiary": errand["beneficiary"],
                   "fingerprint": errand["fingerprint"], "action_id": "act-egg-1", "resource_id": "res-egg-1"}
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                for idx in range(505):
                    ev_ts = start + 31.0 + idx * 0.01
                    c.execute(
                        "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, ?, ?, ?, ?)",
                        (
                            f"ev-noise-{idx}",
                            stream,
                            10 + idx,
                            ev_ts,
                            body(f"ev-noise-{idx}", {"kind": "activity", "actor": "sora", "activity": "reading", "at": ev_ts}),
                        ),
                    )
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, ?, ?, ?, ?)",
                    ("ev-arrive-market", stream, 599, start + 50.0, body("ev-arrive-market", arrival)),
                )
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, ?, ?, ?, ?)",
                    ("ev-buy-egg-506", stream, 600, start + 60.0, body("ev-buy-egg-506", receipt)),
                )
        d.intents.sweep(start + 70.0)
        self.assertEqual([r["state"] for r in d.intents.list_intents(actor="nell")], ["completed"])
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            refs = json.loads(c.execute("SELECT refs FROM intents WHERE status='completed'").fetchone()[0])
        self.assertEqual(sorted(f["kernel"] for f in refs if isinstance(f, dict) and f.get("kernel")),
                         ["ev-arrive-market", "ev-buy-egg-506"])
        self.assertEqual(d.intents.errands("nell", start + 70.0), [])


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
        """Fails if the connection test, the chat turn, the life model call that follows the owner's line, or the memory
        extractor keeps sending `max_tokens` / `temperature` to a model that refuses them (or too small a completion
        budget). The life model call is now woken by the owner's line reaching the world at extraction time, not by the
        chat turn (dropped item D1); without that line no life call is made (checked by hand), so the assertion is real."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        secret_key = "sk-localMockTestKey123456"
        base_url, recorded = self._start_mock_llm_server("gpt5_compat", secret_key)

        # A fixed evening hour: on the wall clock this case failed whenever it ran while she was asleep (02:00-06:00).
        d = HouseDaemon(
            Path(tmp.name) / "rt",
            virtual_clock=True,
            start_ts=dt.datetime(2026, 11, 21, 20, 0, tzinfo=JST).timestamp(),
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
        self.assertGreaterEqual(len(recorded) - before, 1)
        self.assertNotIn("max_tokens", recorded[-1])
        self.assertNotIn("temperature", recorded[-1])

        # The owner's line reaches the world when the memory extraction runs; the life model then thinks about it,
        # and that call adapts to the model the same way (a chat turn alone no longer wakes the life model).
        before = len(recorded)
        ops = [{"op": "intent", "text": "奈尔晚上泡热茶", "kind": "promise", "for": ["房主"], "agreed": ["奈尔"],
                "scope": "world", "when": "vague", "quote": "晚上好"}]
        d.memory.extract(force=True, now=d.clock.now() + 1, llm_fn=lambda system, user: {"ops": ops})
        d.fast_forward(1)
        life = [r for r in recorded[before:] if "你现在的情况" in json.dumps(r["messages"], ensure_ascii=False)]
        self.assertTrue(life, "the life model was never asked after the owner's line reached the world")
        for r in life:
            self.assertNotIn("max_tokens", r)
            self.assertNotIn("temperature", r)
            self.assertGreaterEqual(r["max_completion_tokens"], 1024)

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
        self.assertIn("模型拒绝了请求里的某个参数", replies[0]["text"])  # chat bubble: short Chinese reason only
        self.assertNotIn("HTTP 400", replies[0]["text"])
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


class Stage0CliAndHealthTests(unittest.TestCase):
    def test_init_prints_abs_path_and_token_and_token_subcommand_reads_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_path = Path(tmp.name) / ".env"
        data_dir = Path(tmp.name) / "rt"

        out_init = io.StringIO()
        with contextlib.redirect_stdout(out_init):
            rc_init = cli.main(["init", "--env-file", str(env_path), "--data-dir", str(data_dir)])
        self.assertEqual(rc_init, 0)
        init_txt = out_init.getvalue()
        self.assertIn(str(env_path.resolve()), init_txt)
        self.assertIn("AI_HOUSE_OWNER_TOKEN", init_txt)

        env_lines = env_path.read_text(encoding="utf-8")
        expected_tok = ""
        for line in env_lines.splitlines():
            if line.startswith("AI_HOUSE_OWNER_TOKEN="):
                expected_tok = line.split("=", 1)[1].strip()
                break
        self.assertTrue(expected_tok)
        self.assertIn(expected_tok, init_txt)

        out_tok = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"AI_HOUSE_OWNER_TOKEN": ""}, clear=False),
            contextlib.redirect_stdout(out_tok),
        ):
            rc_tok = cli.main(["token", "--env-file", str(env_path)])
        self.assertEqual(rc_tok, 0)
        self.assertIn(str(env_path.resolve()), out_tok.getvalue())
        self.assertIn(expected_tok, out_tok.getvalue())

        out_json = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"AI_HOUSE_OWNER_TOKEN": ""}, clear=False),
            contextlib.redirect_stdout(out_json),
        ):
            rc_json = cli.main(["token", "--env-file", str(env_path), "--json"])
        self.assertEqual(rc_json, 0)
        doc = json.loads(out_json.getvalue())
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["owner_token"], expected_tok)
        self.assertEqual(doc["env_file"], str(env_path.resolve()))
        self.assertEqual(doc["source"], "dotenv")

        missing_env = Path(tmp.name) / "missing.env"
        err_missing = io.StringIO()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stderr(err_missing),
        ):
            rc_missing = cli.main(["token", "--env-file", str(missing_env)])
        self.assertEqual(rc_missing, 1)
        self.assertIn("AI_HOUSE_OWNER_TOKEN", err_missing.getvalue())

    def test_token_and_doctor_match_up_when_env_overrides_dotenv(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_path = Path(tmp.name) / "override.env"
        file_tok = "tok-from-dotenv-file-1111"
        env_tok = "tok-from-process-env-2222"
        env_path.write_text(f"AI_HOUSE_OWNER_TOKEN={file_tok}\nAI_HOUSE_PORT=8799\n", encoding="utf-8")

        out_plain = io.StringIO()
        out_json = io.StringIO()
        with mock.patch.dict("os.environ", {"AI_HOUSE_OWNER_TOKEN": env_tok}, clear=False):
            with contextlib.redirect_stdout(out_plain):
                rc_plain = cli.main(["token", "--env-file", str(env_path)])
            with contextlib.redirect_stdout(out_json):
                rc_json = cli.main(["token", "--env-file", str(env_path), "--json"])
            cli._load_dotenv(str(env_path))
            effective_after_load = os.environ.get("AI_HOUSE_OWNER_TOKEN")

        out_empty_env = io.StringIO()
        with mock.patch.dict("os.environ", {"AI_HOUSE_OWNER_TOKEN": ""}, clear=False):
            with contextlib.redirect_stdout(out_empty_env):
                rc_empty = cli.main(["token", "--env-file", str(env_path), "--json"])
            cli._load_dotenv(str(env_path))
            effective_when_empty = os.environ.get("AI_HOUSE_OWNER_TOKEN")

        self.assertEqual(rc_plain, 0)
        self.assertEqual(rc_json, 0)
        self.assertEqual(rc_empty, 0)
        self.assertEqual(effective_after_load, env_tok)
        self.assertEqual(effective_when_empty, file_tok)
        self.assertEqual(json.loads(out_empty_env.getvalue())["owner_token"], file_tok)
        self.assertIn(env_tok, out_plain.getvalue())
        self.assertNotIn(file_tok, out_plain.getvalue())
        doc = json.loads(out_json.getvalue())
        self.assertEqual(doc["owner_token"], env_tok)
        self.assertEqual(doc["source"], "env")

    def test_pack_aliases_life1_and_missing_pack_lists_available(self):
        v1 = vocab_life1.load_pack("life1")
        v2 = vocab_life1.load_pack("default")
        self.assertEqual(v1.MANIFEST.get("profile"), v2.MANIFEST.get("profile"))
        self.assertTrue(v1.ROSTER)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_path = Path(tmp.name) / "life1.env"
        data_dir = Path(tmp.name) / "rt"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["init", "--pack", "life1", "--env-file", str(env_path), "--data-dir", str(data_dir)])
        self.assertEqual(rc, 0)
        self.assertIn("AI_HOUSE_PACK_DIR=life1", env_path.read_text(encoding="utf-8"))

        with self.assertRaises(FileNotFoundError) as ctx:
            vocab_life1.load_pack("missing-pack-xyz")
        msg = str(ctx.exception)
        self.assertIn("life1", msg)
        self.assertIn("indie-studio-3", msg)

    def test_up_port_in_use_friendly_error_and_subcommand_help(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        busy_port = sock.getsockname()[1]

        missing_env = Path(tmp.name) / "none.env"
        data_dir = Path(tmp.name) / "rt"
        err_buf = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                {"AI_HOUSE_OWNER_TOKEN": "", "AI_HOUSE_PACK_DIR": "", "AI_HOUSE_HOST": "", "AI_HOUSE_PORT": ""},
                clear=False,
            ),
            contextlib.redirect_stderr(err_buf),
        ):
            rc = cli.main([
                "up",
                "--host", "127.0.0.1",
                "--port", str(busy_port),
                "--data-dir", str(data_dir),
                "--env-file", str(missing_env),
            ])
        self.assertEqual(rc, 2)
        self.assertIn("已被占用", err_buf.getvalue())

        for subcmd in ("up", "init", "token", "doctor", "plugins", "pack-check"):
            help_buf = io.StringIO()
            with (
                contextlib.redirect_stdout(help_buf),
                self.assertRaises(SystemExit) as sysexit,
            ):
                cli.main([subcmd, "--help"])
            self.assertEqual(sysexit.exception.code, 0)
            help_txt = help_buf.getvalue()
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in help_txt)
            self.assertTrue(has_cjk, f"missing Chinese help text for {subcmd}")

    def test_health_endpoint_unauthenticated_and_loopback_host_guarded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        d_auth = HouseDaemon(
            Path(tmp.name) / "rt_auth",
            allow_onboarding=True,
            env={"AI_HOUSE_OWNER_TOKEN": "tok-health-test-123456"},
        )
        self.addCleanup(d_auth.close)
        srv_auth = make_http_server(d_auth, host="127.0.0.1", port=0)
        self.addCleanup(srv_auth.server_close)
        self.addCleanup(srv_auth.shutdown)
        threading.Thread(target=srv_auth.serve_forever, daemon=True).start()

        with urllib.request.urlopen(f"http://127.0.0.1:{srv_auth.server_port}/api/health", timeout=3) as r:
            self.assertEqual(r.status, 200)
            h = json.loads(r.read().decode("utf-8"))
        self.assertTrue(h["ok"])
        self.assertEqual(h["service"], "ai-house")
        self.assertTrue(h["needs_onboarding"])

        with self.assertRaises(urllib.error.HTTPError) as unauth:
            urllib.request.urlopen(f"http://127.0.0.1:{srv_auth.server_port}/api/state", timeout=3)
        self.assertEqual(unauth.exception.code, 401)
        unauth.exception.close()

        d_open = HouseDaemon(Path(tmp.name) / "rt_open", allow_onboarding=True, env={})
        self.addCleanup(d_open.close)
        srv_open = make_http_server(d_open, host="127.0.0.1", port=0)
        self.addCleanup(srv_open.server_close)
        self.addCleanup(srv_open.shutdown)
        threading.Thread(target=srv_open.serve_forever, daemon=True).start()

        with urllib.request.urlopen(f"http://127.0.0.1:{srv_open.server_port}/api/health", timeout=3) as r2:
            self.assertEqual(r2.status, 200)

        conn = http.client.HTTPConnection("127.0.0.1", srv_open.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/health", headers={"Host": "evil.example.com"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
