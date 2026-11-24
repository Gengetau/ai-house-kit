"""Unit tests for v0.2 P3-P6 turnkey features:
- Authority eff_env isolation (Promises + DayNote without mutating os.environ)
- RoommateChatAgent env isolation, primary->fallback failover, and bounded OpenAI tool loop
- Lock-free converse() keeping /api/state non-blocking during stalled LLM calls
- IntentsLite errand detection & evidence-backed completion
- DiaryService evening personal diaries isolated from private DMs + memory recall
- HTTP API endpoints (/api/diaries, /api/intents, /api/memories, /api/memories/forget) & token tiers
"""
from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from authority import Authority
from worldkernel import legacy, vocab_life1
from worldkernel.life_model import FakeLifeModel
from worldkernel.store import VirtualClock

from ai_house.chat_identity import member_speaker, owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server

JST = dt.timezone(dt.timedelta(hours=9))
OWNER = "owner_" + "a" * 24
READER = "reader_" + "b" * 24


class _MockDualOpenAIServer:
    """Loopback OpenAI-compatible HTTP server with /fail/v1 (503) and /ok/v1 (tool loop + text)."""

    def __init__(self):
        self.calls = []
        self.tool_rounds_seen = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                outer.calls.append((self.path, self.headers.get("Authorization"), body))
                if self.path.startswith("/fail/"):
                    raw = b'{"error":{"message":"upstream overloaded"}}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                msgs = body.get("messages") or []
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                # Trigger 1 round of multi-tool calls if tools were offered and no tool output yet
                if body.get("tools") and not tool_msgs:
                    outer.tool_rounds_seen += 1
                    resp_doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "tc_1", "type": "function", "function": {"name": "inspect_house", "arguments": "{}"}},
                                    {"id": "tc_2", "type": "function", "function": {"name": "search_memory", "arguments": json.dumps({"query": "秘密"})}},
                                    {"id": "tc_3", "type": "function", "function": {"name": "record_promise", "arguments": json.dumps({"summary": "答应顺路买2个鸡蛋", "kind": "errand", "item": "egg", "qty": 2})}},
                                    {"id": "tc_4", "type": "function", "function": {"name": "pick_mood_sticker", "arguments": json.dumps({"mood": "开心"})}},
                                    {"id": "tc_5", "type": "function", "function": {"name": "roll_dice", "arguments": json.dumps({"sides": 6, "count": 1})}},
                                    {"id": "tc_6", "type": "function", "function": {"name": "browse_bookshelf", "arguments": "{}"}},
                                ],
                            }
                        }],
                        "usage": {"total_tokens": 42},
                    }
                else:
                    resp_doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "好呀，我已经看过冰箱库存和书架笔记了，待会儿顺路买2个鸡蛋回来！",
                            }
                        }],
                        "usage": {"total_tokens": 28},
                    }
                raw = json.dumps(resp_doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class P3P6UnitTests(unittest.TestCase):
    def test_authority_eff_env_isolation_without_global_os_environ(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        tdir = Path(tmp)
        t0 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
        wdb1 = str(tdir / "w1.db")
        wdb2 = str(tdir / "w2.db")
        legacy.offline_world(wdb1, roster=vocab_life1.ROSTER)
        legacy.offline_world(wdb2, roster=vocab_life1.ROSTER)
        dummy_chat = lambda s, u: ("{}", 5)

        with mock.patch.dict(os.environ, {}, clear=True):
            a1 = Authority(
                str(tdir / "l1.db"),
                wdb1,
                dummy_chat,
                vocab_life1.SELVES,
                clock=VirtualClock(t0),
                model=FakeLifeModel(vocab=vocab_life1),
                env={"INTENTS": "1", "WORLD_KERNEL_DAY_NOTE": "1"},
            )
            self.addCleanup(a1.close)
            self.assertIsNotNone(a1.promises)
            self.assertIsNotNone(a1.note)

        with mock.patch.dict(os.environ, {"INTENTS": "1", "WORLD_KERNEL_DAY_NOTE": "1"}, clear=True):
            a2 = Authority(
                str(tdir / "l2.db"),
                wdb2,
                dummy_chat,
                vocab_life1.SELVES,
                clock=VirtualClock(t0),
                model=FakeLifeModel(vocab=vocab_life1),
                env={},
            )
            self.addCleanup(a2.close)
            self.assertIsNone(a2.promises)
            self.assertIsNone(a2.note)

    def test_chat_agent_env_isolation_failover_and_tool_loop(self):
        mock_srv = _MockDualOpenAIServer()
        self.addCleanup(mock_srv.close)
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        # 1) Host OPENAI_API_KEY must not leak when daemon receives explicit env={}
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-host-should-not-leak"}, clear=True):
            isolated = HouseDaemon(runtime_dir=Path(tmp) / "rt_iso", virtual_clock=True, env={})
            self.addCleanup(isolated.close)
            self.assertFalse(isolated.chat_agent.live_llm_enabled)

        # 2) Primary endpoint fails -> falls back to AI_HOUSE_FALLBACK_* and runs tool loop
        env = {
            "AI_HOUSE_API_KEY": "sk-primary",
            "AI_HOUSE_BASE_URL": f"http://127.0.0.1:{mock_srv.port}/fail",
            "AI_HOUSE_MODEL": "primary-model",
            "AI_HOUSE_FALLBACK_API_KEY": "sk-fallback",
            "AI_HOUSE_FALLBACK_BASE_URL": f"http://127.0.0.1:{mock_srv.port}/ok",
            "AI_HOUSE_FALLBACK_MODEL": "fallback-model",
            "AI_HOUSE_TOOLS_LOOP": "1",
        }
        d = HouseDaemon(runtime_dir=Path(tmp) / "rt_live", virtual_clock=True, env=env)
        self.addCleanup(d.close)
        # changed: the memory text is not password-like (the stricter secret filter refuses those)
        d.memory.add("nell", "private", "房主的私人保险柜位置是个秘密（不能泄露）", kind="fact", source="chat")
        res = d.send_chat("private:nell", "帮我看看冰箱，顺便记住一会儿去买2个鸡蛋")
        rep = res["replies"][0]
        self.assertEqual(rep["mode"], "live_api")
        self.assertEqual(mock_srv.tool_rounds_seen, 1)
        self.assertTrue(any(p.startswith("/fail/") for p, _, _ in mock_srv.calls))
        self.assertTrue(any(p.startswith("/ok/") and a == "Bearer sk-fallback" for p, a, _ in mock_srv.calls))
        intents = d.intents.list_intents(actor="nell")
        self.assertTrue(any(it["item"] == "egg" and it["qty"] == 2 and it["state"] == "open" for it in intents))

        # Member message in group cannot trigger record_promise to create an owner errand
        before_cnt = len(d.intents.list_intents(actor="nell"))
        member = member_speaker("群友路人", "qq:999")
        d.converse("group", "买点东西吧", member, actor="nell")
        after_cnt = len(d.intents.list_intents(actor="nell"))
        self.assertEqual(after_cnt, before_cnt)
        # Group search_memory call never sees private memory in tool output
        tool_payloads = [
            m["content"]
            for _, _, b in mock_srv.calls
            for m in b.get("messages", [])
            if m.get("role") == "tool" and m.get("tool_call_id") == "tc_2"
        ]
        self.assertTrue(tool_payloads)
        # positive control: the same memory is found by the same tool call in the private chat
        self.assertIn("保险柜", tool_payloads[0])
        self.assertNotIn("保险柜", tool_payloads[-1])

    def test_converse_does_not_block_get_state_while_llm_stalls(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_llm(sys_p: str, usr_p: str):
            if "刚刚说" in usr_p:
                entered.set()
                release.wait(timeout=4.0)
            return "慢思考回复完成啦", 10

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        d = HouseDaemon(runtime_dir=Path(tmp) / "rt", virtual_clock=True, env={}, llm_fn=slow_llm)
        self.addCleanup(d.close)
        err_holder = []
        t = threading.Thread(
            target=lambda: d.converse("private:nell", "你在干嘛呢？", owner_speaker()),
            daemon=True,
        )
        t.start()
        try:
            self.assertTrue(entered.wait(timeout=2.0), "slow llm_fn was never invoked")
            t0 = time.monotonic()
            snap = d.get_state()
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.35, f"get_state blocked for {elapsed:.3f}s during stalled LLM call")
            self.assertIn("nell", snap["residents"])
        finally:
            release.set()
            t.join(timeout=3.0)
        self.assertFalse(err_holder)

    def test_intents_errand_lifecycle_completes_on_evidence(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        start = dt.datetime(2026, 11, 21, 13, 30, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=Path(tmp) / "rt", virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)
        d.send_chat("group", "奈尔，今天有空帮家里买2个鸡蛋好不好？")
        rows = [r for r in d.intents.list_intents(actor="nell") if r["item"] == "egg"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["state"], "open")
        self.assertEqual(rows[0]["qty"], 2)
        errands = d.intents.errands("nell", d.clock.now())
        self.assertTrue(any(e["item"] == "egg" for e in errands))
        egg_errand = next(e for e in errands if e["item"] == "egg")
        self.assertIn("fingerprint", egg_errand)
        self.assertEqual(egg_errand["account"], "nell")
        self.assertIsInstance(egg_errand["beneficiary"], list)
        # Exercise live daytime Shopping/Outings pipeline (09:30-16:00 JST) without KeyError
        d.fast_forward(30)

        done_rows = [r for r in d.intents.list_intents(actor="nell") if r["item"] == "egg"]
        self.assertEqual(done_rows[0]["state"], "completed")
        self.assertTrue(str(done_rows[0]["evidence_key"]).startswith("ai-house/"))
        self.assertIsNotNone(done_rows[0]["completed_at"])

    def test_diary_service_evening_generation_isolation_and_recall(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        start = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=Path(tmp) / "rt", virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)
        d.send_chat("private:nell", "我的备用钥匙藏在庭院蓝花盆底下，千万别对别人提")
        # changed: the private line only becomes a memory through extraction (scripted model); used as positive control below
        d.memory.extract("nell", force=True, now=start + 60, llm_fn=lambda sys_p, usr_p: {"ops": [{
            "op": "add", "text": "房主的备用钥匙藏在庭院蓝花盆底下，不想让别人知道", "about": ["房主"], "kind": "fact"}]})
        # Advance slightly through daytime so FakeLifeModel creates real daylog/script entries
        d.fast_forward(60)
        d.fast_forward(60)
        # Advance clock to 22:45 JST of same day and trigger step_once -> post-step diary generation
        night_ts = dt.datetime(2026, 11, 21, 22, 45, tzinfo=JST).timestamp()
        d.clock.t = night_ts
        d.step_once()

        diaries = d.diary.list_diaries()
        self.assertTrue(diaries, "evening diaries should be written after 22:30 JST")
        for item in diaries:
            self.assertEqual(item["mode"], "demo")
            self.assertNotIn("蓝花盆", item["text"])
            self.assertNotIn("备用钥匙", item["text"])

        # changed: recall matches on content words (no vector service), so the question must share them with the
        # diary; "昨晚日记写了什么" shares only one bigram with it and finds nothing
        recalled = d.memory.recall("nell", "world", "日记写了什么", now=night_ts + 3600)
        self.assertIn("日记：早上", recalled)
        # positive control for the isolation checks above: the private line itself IS recalled in the private chat
        self.assertIn("蓝花盆", d.memory.recall("nell", "private", "备用钥匙藏在哪里", now=night_ts + 3600))

    def test_new_http_endpoints_and_token_tiers(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        start = dt.datetime(2026, 11, 21, 22, 45, tzinfo=JST).timestamp()
        d = HouseDaemon(
            runtime_dir=Path(tmp) / "rt",
            virtual_clock=True,
            start_ts=start,
            env={"AI_HOUSE_OWNER_TOKEN": OWNER, "AI_HOUSE_READ_TOKEN": READER},
        )
        self.addCleanup(d.close)
        d.send_chat("private:nell", "记住今天帮买2个鸡蛋，还有我的秘密配方是两勺蜂蜜")
        d.memory.add("nell", "private", "房主的秘密配方是两勺蜂蜜", kind="fact", source="chat", now=start)
        # changed: a diary must be long enough
        d.memory.add("nell", "world", "今天去图书馆翻到了一本气象手记，下午在窗边读了很久，晚上把有趣的几页抄在了本子上。",
                     kind="diary", source="diary", now=start)
        d.step_once()

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def req(method, path, body=None, token=None):
            h = {}
            if token:
                h["Authorization"] = f"Bearer {token}"
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                h["Content-Type"] = "application/json"
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request(method, path, body=data, headers=h)
                r = conn.getresponse()
                return r.status, json.loads(r.read().decode("utf-8"))
            finally:
                conn.close()

        st, _ = req("GET", "/api/diaries")
        self.assertEqual(st, 401)
        st, doc = req("GET", "/api/diaries", token=READER)
        self.assertEqual(st, 200)
        self.assertTrue(doc["ok"])

        st, doc = req("GET", "/api/intents", token=READER)
        self.assertEqual(st, 403)
        st, doc = req("GET", "/api/intents", token=OWNER)
        self.assertEqual(st, 200)
        self.assertTrue(any(i["item"] == "egg" for i in doc["intents"]))

        st, doc = req("GET", "/api/memories?scope=world&actor=nell", token=READER)
        self.assertEqual(st, 200)
        self.assertTrue(any("气象手记" in m["text"] for m in doc["memories"]))

        st, _ = req("GET", "/api/memories?scope=private&actor=nell", token=READER)
        self.assertEqual(st, 403)
        st, doc = req("GET", "/api/memories?scope=private&actor=nell", token=OWNER)
        self.assertEqual(st, 200)
        priv_mems = [m for m in doc["memories"] if "蜂蜜" in m["text"]]
        self.assertTrue(priv_mems)
        mid = priv_mems[0]["id"]
        self.assertIn("蜂蜜", d.memory.recall("nell", "private", "蜂蜜", now=start + 10))  # positive control

        st, _ = req("POST", "/api/memories/forget", {"actor": "nell", "scope": "private", "id": mid}, token=READER)
        self.assertEqual(st, 403)
        st, doc = req("POST", "/api/memories/forget", {"actor": "nell", "scope": "private", "id": mid}, token=OWNER)
        self.assertEqual(st, 200)
        self.assertEqual(d.memory.recall("nell", "private", "蜂蜜", now=start + 10), "")

    def test_v2_console_static_assets_and_api_endpoints(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        start = dt.datetime(2026, 11, 21, 12, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            runtime_dir=Path(tmp) / "rt",
            virtual_clock=True,
            start_ts=start,
            env={"AI_HOUSE_OWNER_TOKEN": OWNER, "AI_HOUSE_READ_TOKEN": READER},
        )
        self.addCleanup(d.close)
        d.step_once()

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def req_raw(method, path, body=None, token=None):
            h = {}
            if token:
                h["Authorization"] = f"Bearer {token}"
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                h["Content-Type"] = "application/json"
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request(method, path, body=data, headers=h)
                r = conn.getresponse()
                return r.status, r.getheader("Content-Type", ""), r.read()
            finally:
                conn.close()

        # Static web assets serve cleanly without auth token; traversal attempts return 404
        st, ct, html_bytes = req_raw("GET", "/")
        self.assertEqual(st, 200)
        html_txt = html_bytes.decode("utf-8")
        self.assertIn("AI House Studio", html_txt)
        self.assertIn("/web/css/app.css", html_txt)
        self.assertIn("/web/js/main.js", html_txt)

        for path, ctype_prefix in (
            ("/web/css/app.css", "text/css"),
            ("/web/js/main.js", "text/javascript"),
            ("/web/img/house/overview/cells.json", "application/json"),
            ("/web/img/house/overview/house_day.png", "image/png"),
            ("/web/img/house/chars/nell.png", "image/png"),
            ("/web/img/house/chars/sora.png", "image/png"),
        ):
            st, ct, raw = req_raw("GET", path)
            self.assertEqual(st, 200, path)
            self.assertTrue(ct.startswith(ctype_prefix), (path, ct))
            if ctype_prefix == "image/png":
                self.assertTrue(raw.startswith(b"\x89PNG"), path)

        for bad_path in ("/web/../daemon.py", "/web/..%2fdaemon.py", "/web/missing.js"):
            st, _, _ = req_raw("GET", bad_path)
            self.assertEqual(st, 404, bad_path)

        # /api/auth tier introspection
        st, _, _ = req_raw("GET", "/api/auth")
        self.assertEqual(st, 401)
        st, _, raw = req_raw("GET", "/api/auth", token=READER)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw)["tiers"], {"read": True, "write": False, "private": False, "owner": False})
        st, _, raw = req_raw("GET", "/api/auth", token=OWNER)
        self.assertEqual(json.loads(raw)["tiers"], {"read": True, "write": True, "private": True, "owner": True})

        # /api/wardrobe & /api/house/stage & /api/stock
        st, _, _ = req_raw("GET", "/api/wardrobe?actor=nell")
        self.assertEqual(st, 401)
        st, _, raw = req_raw("GET", "/api/wardrobe?actor=nell", token=READER)
        self.assertEqual(st, 200)
        wd = json.loads(raw)
        self.assertEqual(wd["actor"], "nell")
        self.assertIn("garments", wd)

        st, _, raw = req_raw("GET", "/api/house/stage", token=READER)
        self.assertEqual(st, 200)
        stage = json.loads(raw)
        self.assertTrue(stage["ok"])
        self.assertIn("living", stage["rooms"])

        st, _, before_raw = req_raw("GET", "/api/stock", token=READER)
        self.assertEqual(st, 200)
        before_cookies = sum(r["qty"] for r in json.loads(before_raw)["rows"] if r["item"] == "cookie")

        st, _, add_raw = req_raw("POST", "/api/stock", {"item": "cookie", "qty": 3}, token=OWNER)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(add_raw)["ok"])

        st, _, after_raw = req_raw("GET", "/api/stock", token=READER)
        after_cookies = sum(r["qty"] for r in json.loads(after_raw)["rows"] if r["item"] == "cookie")
        self.assertEqual(after_cookies, before_cookies + 3)

    def test_v2_console_works_with_indie_studio_3_pack(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        d = HouseDaemon(
            runtime_dir=Path(tmp) / "rt",
            pack_dir=ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3",
            virtual_clock=True,
            env={},
        )
        self.addCleanup(d.close)
        d.step_once()
        wd = d.get_wardrobe("sora")
        self.assertEqual(wd["actor"], "sora")
        self.assertEqual(wd["name"], "苍空")
        stage = d.get_house_stage()
        self.assertTrue(stage["ok"])
        self.assertIn("sora", stage["people"])

    def test_onboarding_first_bot_invite_second_llm_config_and_reset(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = Path(tmp_dir.name)
        env_file = tmp / ".env"
        env_file.write_text("# test env\nAI_HOUSE_OWNER_TOKEN=" + OWNER + "\n", encoding="utf-8")
        writer_tok = "writer_" + "c" * 24

        d = HouseDaemon(
            runtime_dir=tmp / "rt",
            virtual_clock=True,
            allow_onboarding=True,
            env_file=env_file,
            env={"AI_HOUSE_OWNER_TOKEN": OWNER, "AI_HOUSE_WRITE_TOKEN": writer_tok},
        )
        self.addCleanup(d.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def call(method, path, body=None, token=OWNER):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            hdrs = {"Authorization": f"Bearer {token}"}
            data = None
            if body is not None:
                hdrs["Content-Type"] = "application/json"
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            conn.close()
            return resp.status, json.loads(raw)

        # 1) Unconfigured house starts with empty roster and needs_onboarding=True
        st, state0 = call("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertTrue(state0["needs_onboarding"])
        self.assertEqual(state0["roster"], [])

        # Non-owner write token cannot inspect or mutate owner setup routes
        st, _ = call("GET", "/api/setup", token=writer_tok)
        self.assertEqual(st, 403)
        st, _ = call("POST", "/api/setup", {"mode": "first_bot", "resident": {"name": "越权"}}, token=writer_tok)
        self.assertEqual(st, 403)

        st, setup0 = call("GET", "/api/setup", token=OWNER)
        self.assertEqual(st, 200)
        self.assertTrue(setup0["needs_onboarding"])

        # 2) Owner configures the first resident only (no 6 pre-stuffed bots)
        st, res1 = call("POST", "/api/setup", {
            "mode": "first_bot",
            "resident": {"name": "小满", "bio": "第一位入住的室友", "sprite": "sora", "accent": "#B4532D"},
            "llm": {"mode": "demo"},
        })
        self.assertEqual(st, 200)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["state"]["roster"], ["bot1"])
        self.assertFalse(res1["state"]["needs_onboarding"])
        self.assertEqual(res1["state"]["residents"]["bot1"]["wallet_yen"], 40000)
        self.assertEqual(res1["state"]["house_wallet_yen"], 200000)

        # Chat, step, and wardrobe all work for bot1
        st, chat1 = call("POST", "/api/chat", {"channel": "private:bot1", "text": "欢迎搬进小屋！"})
        self.assertEqual(st, 200)
        self.assertTrue(chat1["ok"])
        self.assertEqual(chat1["replies"][0]["actor"], "bot1")
        st, wd1 = call("GET", "/api/wardrobe?actor=bot1")
        self.assertEqual(st, 200)
        self.assertGreater(len(wd1["garments"]), 0)

        # 3) Invite a second resident live without losing bot1's chat
        st, res2 = call("POST", "/api/residents", {"name": "知夏", "bio": "第二位室友", "sprite": "elsa"})
        self.assertEqual(st, 200)
        self.assertEqual(res2["state"]["roster"], ["bot1", "bot2"])
        self.assertEqual(res2["state"]["residents"]["bot2"]["wallet_yen"], 40000)
        st, ch_after = call("GET", "/api/chat?channel=private:bot1")
        self.assertEqual(st, 200)
        self.assertTrue(any("欢迎搬进小屋" in m["text"] for m in ch_after["messages"]))

        # 4) Edit resident profile + update LLM settings cleanly (secret masked in GET /api/setup)
        st, upd = call("POST", "/api/residents/update", {"actor": "bot1", "name": "小满", "bio": "更新后的一句话简介", "sprite": "rin"})
        self.assertEqual(st, 200)
        self.assertEqual(upd["state"]["residents"]["bot1"]["bio"], "更新后的一句话简介")
        self.assertEqual(upd["state"]["residents"]["bot1"]["sprite"], "rin")

        secret_key = "sk-test-secret-123456"
        st, llm_res = call("POST", "/api/settings/llm", {"base_url": "https://api.example.invalid/v1", "model": "demo-llm", "api_key": secret_key})
        self.assertEqual(st, 200)
        self.assertEqual(llm_res["state"]["llm_mode"], "live_api")
        self.assertEqual(llm_res["state"]["model_name"], "demo-llm")
        st, setup_after = call("GET", "/api/setup")
        self.assertEqual(st, 200)
        self.assertNotIn(secret_key, json.dumps(setup_after))
        self.assertTrue(setup_after["llm"]["api_key_set"])
        self.assertIn("AI_HOUSE_MODEL=demo-llm", env_file.read_text(encoding="utf-8"))

        # 5) Reset house removes runtime DBs cleanly (including on Windows) and restores onboarding state
        st, rst = call("POST", "/api/setup/reset", {})
        self.assertEqual(st, 200)
        self.assertTrue(rst["needs_onboarding"])
        self.assertEqual(rst["state"]["roster"], [])
        self.assertFalse((tmp / "rt" / "life1.db").exists())
        self.assertFalse((tmp / "rt" / "custom-pack").exists())


if __name__ == "__main__":
    unittest.main()
