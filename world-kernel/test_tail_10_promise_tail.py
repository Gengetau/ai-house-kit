"""Tail batch 10: promise tail defects (unbuyable errand, garbage errand, questions recorded as dietary memos).

After M2 a chat turn records nothing by itself: promises come from memory extraction (`d.memory.extract`) through the
shared promise module. What stays here is the reply tail "不过「X」我这边买不了，没法替你带回来。" (the words in
`worldkernel.buy_words`), the rule that chit-chat is never read as a purchase request, and the quiet expiry.
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import Speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402
from worldkernel import buy_words  # noqa: E402

T0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


class _MockModel:
    """Loopback OpenAI-compatible model that answers every chat request with `reply` (no tools)."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                msg = {"role": "assistant", "content": outer.reply}
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
    def talk(self, owner_text: str, reply: str = "好的，我记下了，晚点就去") -> Dict[str, Any]:
        """One fresh daemon per call so each subTest sees only its own rows."""
        srv = _MockModel(reply)
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


TAIL = "买不了，没法替你带回来"


class UnbuyableItemTests(_DaemonCase):
    def test_durian_reply_tells_owner_it_cannot_be_bought(self) -> None:
        """Fails if the reply to an owner who asks for something outside the catalogue lacks the tail
        "不过「榴莲」我这边买不了，没法替你带回来。" after her own words, or if the tail names another thing. (The
        reminder row and "只先记个提醒" are gone: nothing is recorded by the turn.)"""
        res = self.talk("帮我买点榴莲回来")
        reply = res["replies"][0]["text"]
        self.assertTrue(reply.startswith("好的，我记下了，晚点就去"), reply)
        self.assertTrue(reply.endswith("不过「榴莲」我这边买不了，没法替你带回来。"), reply)
        self.assertNotIn("提醒", reply)
        self.assertEqual(self.rows(), [])

    def test_durian_with_a_reply_that_names_the_item_also_warns(self) -> None:
        """Fails if a reply that itself says "我这就去买榴莲" is not followed by the tail."""
        res = self.talk("帮我买点榴莲", reply="好呀，我这就去买榴莲")
        self.assertIn(TAIL, res["replies"][0]["text"])

    def test_a_reply_that_already_declines_gets_no_second_no(self) -> None:
        """Fails if the tail is added after a reply that already says she cannot do it, or if the declining reply
        loses its own words. The control: the same request with an accepting reply gets the tail."""
        said = "帮我买点榴莲"
        res = self.talk(said, reply="榴莲我买不到，抱歉")
        self.assertNotIn(TAIL, res["replies"][0]["text"])
        self.assertIn("榴莲我买不到", res["replies"][0]["text"])
        res = self.talk(said, reply="好呀，我这就去")
        self.assertIn(TAIL, res["replies"][0]["text"])

    def test_catalog_purchase_is_not_warned(self) -> None:
        """Fails if a request for a catalogue item ("牛奶") gets the "买不了" tail, or if the control (a thing outside
        the catalogue, in the same house and with the same reply) does not get it."""
        res = self.talk("买两盒牛奶回来")
        self.assertNotIn("买不了", res["replies"][0]["text"])
        self.assertEqual(buy_words.detect_errand_item(self.d.vocab, "买两盒牛奶回来"), ("milk", 2))
        self.assertEqual(buy_words.unbuyable_noun(self.d.vocab, "买两盒牛奶回来"), "")
        res = self.talk("帮我买点榴莲回来")
        self.assertIn(TAIL, res["replies"][0]["text"])

    def test_catalog_request_with_a_wordy_reply_is_not_warned(self) -> None:
        """Fails if the catalogue item is lost when the reply is long and names other things (the tail must come
        only from the owner's own request)."""
        res = self.talk("买两盒牛奶回来", reply="好的，我先看看冰箱里还有什么，再去超市买牛奶")
        self.assertNotIn("买不了", res["replies"][0]["text"])

    def test_an_expired_promise_is_quiet_and_listed_as_expired(self) -> None:
        """Fails if the owner gets a chat message when a promise runs out (D5: no expiry notice any more), or if the
        promise does not reach `expired` by time. The control inside the scenario: the promise is open before."""
        self.talk("顺便帮家里买2个鸡蛋", reply="好，我去买2个鸡蛋")
        d = self.d
        now = d.clock.now()
        ops = [{"op": "intent", "text": "苍空买2个鸡蛋", "kind": "promise", "for": ["房主"], "agreed": ["苍空"],
                "scope": "world", "when": "vague", "quote": "顺便帮家里买2个鸡蛋"}]
        d.memory.extract(force=True, now=now + 5, llm_fn=lambda system, user: {"ops": ops})
        self.assertEqual([r["state"] for r in self.rows()], ["open"])
        before = len(d.list_chat("private:sora", limit=200))
        d.clock.advance(40 * 24 * 3600)  # an open "vague" promise lasts 30 days
        d.intents.sweep(d.clock.now())
        d._run_post_step_jobs()
        d._run_post_step_jobs()
        self.assertEqual([r["state"] for r in self.rows()], ["expired"])
        self.assertEqual(self.expiry_notes(), [])
        self.assertEqual(len(d.list_chat("private:sora", limit=200)), before)


class GarbageErrandTests(_DaemonCase):
    SMALL_TALK = ("你再给我讲讲买卖", "可以买单吗")

    def test_small_talk_is_not_read_as_a_purchase_request(self) -> None:
        """Fails if "买卖" / "买单" small talk is read as a purchase request (a noun comes out, or the reply gets the
        "买不了" tail), or if the turn records anything. The control: a real request yields its noun and the tail."""
        for said in self.SMALL_TALK:
            with self.subTest(said=said):
                self.assertEqual(buy_words.extract_buy_request_noun(said), "")
                self.assertEqual(buy_words.extract_raw_buy_noun(said), "")
                res = self.talk(said)
                self.assertNotIn("买不了", res["replies"][0]["text"])
                self.assertEqual(self.rows(), [])
        self.assertEqual(buy_words.extract_buy_request_noun("帮我买点榴莲回来"), "榴莲")
        res = self.talk("帮我买点榴莲回来")
        self.assertIn(TAIL, res["replies"][0]["text"])

    def test_non_owner_asking_for_something_gets_no_tail(self) -> None:
        """Fails if the tail is added to a reply to a group member (only the owner's request counts). The control:
        the owner's same request in the same house gets it."""
        member = Speaker(role="member", name="路人", identity_id="qq:1")
        res = self.talk("帮我买点榴莲回来", reply="好的")
        self.assertIn(TAIL, res["replies"][0]["text"])
        res = self.d.converse("group", "帮我买点榴莲回来", member, actor="sora")
        self.assertTrue(res["replies"], res)  # she did answer; only the tail is missing
        for r in res["replies"]:
            self.assertNotIn(TAIL, r["text"])


if __name__ == "__main__":
    unittest.main()
