"""Tail batch 10: promise tail defects (unbuyable errand, garbage errand, questions recorded as dietary memos).

Each item has a fail-before/pass-after case plus regressions for normal purchases and normal dietary memos.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402

T0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


class _MockModel:
    """Loopback OpenAI-compatible model: optionally calls `record_promise` once, then answers `reply`."""

    def __init__(self, tool_args: Optional[Dict[str, Any]], reply: str) -> None:
        self.tool_args = tool_args
        self.reply = reply
        self.tool_results: List[Dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                tool_msgs = [m for m in body.get("messages") or [] if m.get("role") == "tool"]
                for tm in tool_msgs:
                    outer.tool_results.append(json.loads(tm.get("content") or "{}"))
                msg: Dict[str, Any] = {"role": "assistant", "content": outer.reply}
                if outer.tool_args is not None and body.get("tools") and not tool_msgs:
                    msg = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "tc1",
                            "type": "function",
                            "function": {
                                "name": "record_promise",
                                "arguments": json.dumps(outer.tool_args, ensure_ascii=False),
                            },
                        }],
                    }
                raw = json.dumps({"choices": [{"message": msg}], "usage": {"total_tokens": 10}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class _DaemonCase(unittest.TestCase):
    def talk(self, owner_text: str, tool_args: Optional[Dict[str, Any]], reply: str = "好的，我记下了，晚点就去") -> Dict[str, Any]:
        """One fresh daemon per call so each subTest sees only its own rows."""
        srv = _MockModel(tool_args, reply)
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = HouseDaemon(
            tmp.name,
            pack_dir=PACK,
            env={"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"},
            start_ts=T0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(self.d.close)
        self.srv = srv
        return self.d.converse("private:sora", owner_text, owner_speaker("房主"))

    def rows(self) -> List[Dict[str, Any]]:
        return self.d.intents.list_intents(actor="sora")

    def expiry_notes(self) -> List[Dict[str, Any]]:
        return [m for m in self.d.list_chat("private:sora", limit=80) if "已经过期" in m["text"]]


class UnbuyableItemTests(_DaemonCase):
    def test_durian_reply_tells_owner_it_cannot_be_bought(self) -> None:
        res = self.talk("买点榴莲回来吧", {"summary": "买榴莲", "kind": "errand", "item": "榴莲"})
        reply = res["replies"][0]["text"]
        self.assertIn("好的，我记下了，晚点就去", reply)
        self.assertIn("榴莲", reply.split("晚点就去", 1)[1])
        self.assertIn("买不了", reply)
        row = [r for r in self.rows() if r["raw_item"] == "榴莲"][0]
        self.assertFalse(row["item"])

    def test_durian_without_tool_call_also_warns(self) -> None:
        res = self.talk("帮我买点榴莲", None, reply="好呀，我这就去买榴莲")
        self.assertIn("买不了", res["replies"][0]["text"])

    def test_catalog_purchase_is_not_warned(self) -> None:
        res = self.talk("买两盒牛奶回来", {"summary": "买牛奶", "kind": "errand", "item": "牛奶", "qty": 2})
        self.assertNotIn("买不了", res["replies"][0]["text"])
        milk = [r for r in self.rows() if r["item"] == "milk"]
        self.assertEqual(len(milk), 1)
        self.assertEqual((milk[0]["kind"], milk[0]["state"], milk[0]["qty"]), ("errand", "open", 2))

    def test_catalog_purchase_with_empty_item_still_resolves(self) -> None:
        res = self.talk("买两盒牛奶回来", {"summary": "答应买东西", "kind": "errand"})
        self.assertNotIn("买不了", res["replies"][0]["text"])
        milk = [r for r in self.rows() if r["item"] == "milk"]
        self.assertEqual(len(milk), 1)
        self.assertEqual(milk[0]["qty"], 2)

    def test_expired_promise_is_reported_to_owner_once(self) -> None:
        self.talk("买点榴莲回来吧", {"summary": "买榴莲", "kind": "errand", "item": "榴莲"})
        d = self.d
        d._run_post_step_jobs()
        self.assertEqual(self.expiry_notes(), [])
        d.clock.advance(25 * 3600)
        d.intents.sweep(d.clock.now())
        self.assertEqual([r["state"] for r in self.rows() if r["raw_item"] == "榴莲"], ["expired"])
        d._run_post_step_jobs()
        d._run_post_step_jobs()
        notes = self.expiry_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("榴莲", notes[0]["text"])
        self.assertEqual(notes[0]["sender"], "sora")

    def test_completed_promise_is_not_reported(self) -> None:
        self.talk("买两盒牛奶回来", {"summary": "买牛奶", "kind": "errand", "item": "milk", "qty": 2})
        d = self.d
        d.clock.advance(3600)
        d.intents.sweep(d.clock.now())
        d._run_post_step_jobs()
        self.assertEqual(self.expiry_notes(), [])


class GarbageErrandTests(_DaemonCase):
    def test_small_talk_with_empty_item_records_nothing(self) -> None:
        for said in ("你再给我讲讲买卖", "可以买单吗"):
            with self.subTest(said=said):
                self.talk(said, {"summary": "答应房主去超市买东西", "kind": "errand", "qty": 2})
                self.assertEqual(self.srv.tool_results[0].get("recorded"), False)
                self.assertEqual(self.rows(), [])

    def test_unknown_item_in_owner_sentence_still_recorded_with_raw_item(self) -> None:
        self.talk("帮我买点榴莲回来", {"summary": "答应房主去超市买东西", "kind": "errand"})
        self.assertEqual([(r["kind"], r["raw_item"]) for r in self.rows()], [("errand", "榴莲")])


class QuestionNotMemoTests(_DaemonCase):
    QUESTIONS = ("你记得我对什么过敏吗？", "谁知道我对什么过敏？", "我是不是对花生过敏")
    STATEMENTS = ("我对花生过敏", "我不吃香菜，记一下", "我对花生过敏，你记得吗？")

    def memos(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows() if r["kind"] == "memo"]

    def test_questions_create_no_memo_without_tool(self) -> None:
        for q in self.QUESTIONS:
            with self.subTest(q=q):
                self.talk(q, None, reply="我想想哦")
                self.assertEqual(self.memos(), [])

    def test_questions_create_no_memo_with_tool(self) -> None:
        for q in self.QUESTIONS:
            with self.subTest(q=q):
                self.talk(q, {"summary": "记住房主饮食禁忌", "kind": "memo"}, reply="我想想哦")
                self.assertEqual(self.srv.tool_results[0].get("recorded"), False)
                self.assertEqual(self.memos(), [])

    def test_statements_still_create_memo(self) -> None:
        for s in self.STATEMENTS:
            with self.subTest(s=s):
                self.talk(s, None, reply="记住啦")
                memos = self.memos()
                self.assertEqual(len(memos), 1)
                self.assertTrue(memos[0]["summary"].startswith("记住房主饮食禁忌"))
                self.assertEqual(memos[0]["state"], "open")

    def test_statement_memo_via_tool_still_recorded(self) -> None:
        self.talk("我对花生过敏", {"summary": "记得房主对花生过敏", "kind": "memo"}, reply="记住啦")
        self.assertEqual(len(self.memos()), 1)

    def test_queue_payload_has_no_memo_for_question(self) -> None:
        self.talk("你记得我对什么过敏吗？", None, reply="我想想哦")
        with contextlib.closing(sqlite3.connect(self.d.world_db)) as c:
            bodies = [json.loads(r[0]) for r in c.execute("SELECT body FROM owner_chat_queue")]
        self.assertTrue(bodies)
        self.assertFalse([i for b in bodies for i in b.get("intents") or [] if i.get("kind") == "memo"])


if __name__ == "__main__":
    unittest.main()
