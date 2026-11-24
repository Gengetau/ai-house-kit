"""Tail 7b: the world step after a reply is not starved by the reply's spent deadline; a deadline error raised before
any request left the process is "not called" (not a bad answer); an empty 200 does not open the circuit breaker;
`*_BUDGET_S=0` means the default.

Fake keys are assembled at run time (no secret-looking literals).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import unittest
from typing import Any, Dict, List, Tuple

from test_tail_4a_llm_deadline import EMPTY_STOP_DOC, OK_DOC, Base, FakeServer

import ai_house.daemon as daemon_mod
from ai_house.chat_identity import owner_speaker
from ai_house.llm import (
    DEFAULT_TOTAL_BUDGET_S,
    LLMAPIError,
    LLMDeadlineError,
    deadline_scope,
    remaining_budget,
    renewed_deadline_scope,
)

KEY = "sk-" + "x" * 10
LIFE_JSON = {"choices": [{"message": {"role": "assistant", "content": '{"steps": []}'}, "finish_reason": "stop"}],
             "usage": {"total_tokens": 5}}


def _is_chat(body: Dict[str, Any]) -> bool:
    return "刚刚说：" in json.dumps(body, ensure_ascii=False)


def _life_or_chat(n: int, body: Dict[str, Any], h: Any) -> Tuple[Any, Any]:
    return (200, OK_DOC) if _is_chat(body) else (200, LIFE_JSON)


def _hang_life(n: int, body: Dict[str, Any], h: Any) -> Tuple[Any, Any]:
    return (200, OK_DOC) if _is_chat(body) else ("hang", 60)


def _life_hits(srv: FakeServer) -> int:
    with srv.lock:
        return len([b for b in srv.requests if not _is_chat(b)])


class LifeLedger:
    """What the kernel recorded about her decisions."""

    def __init__(self, d: Any) -> None:
        self.path = d.life_db

    def rows(self, sql: str) -> List[Tuple[Any, ...]]:
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            return c.execute(sql).fetchall()

    def states(self) -> List[str]:
        return [r[0] for r in self.rows("SELECT state FROM cognition_requests WHERE purpose='decide'")]

    def budget_states(self) -> List[str]:
        return [r[0] for r in self.rows("SELECT state FROM budget_reservations")]

    def kinds(self, state: str) -> List[str]:
        return [r[0] for r in self.rows(
            f"SELECT json_extract(payload,'$.kind') FROM cognition_requests WHERE purpose='decide' AND state='{state}'")]


# --------------------------------------------------------------------------------------------- 1. world step
class StepAfterReplyTests(Base):
    def live_daemon(self, srv: FakeServer, **extra: str) -> Any:
        return self._daemon({"AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_API_KEY": KEY,
                             "AI_HOUSE_OWNER_TOKEN": "owner-" + "t" * 10, **extra})

    @staticmethod
    def slow_replies(d: Any, seconds: float) -> None:
        orig = d.chat_agent.reply_as

        def slow(*a: Any, **k: Any) -> Any:
            r = orig(*a, **k)
            time.sleep(seconds)  # the reply itself eats the turn's whole deadline
            return r

        d.chat_agent.reply_as = slow

    def _assert_step_ran_on_fresh_budget(self, d: Any, srv: FakeServer) -> None:
        led = LifeLedger(d)
        self.assertGreater(_life_hits(srv), 0, "the world step must still ask the life model after a slow reply")
        self.assertEqual(led.states().count("unknown"), 0, led.states())
        self.assertNotIn("unknown", led.budget_states())
        self.assertTrue({"ready", "done"} & set(led.states()), f"a valid life decision was obtained: {led.states()}")

    def test_private_turn_step_still_asks_life_model_after_reply_spent_the_deadline(self) -> None:
        srv = self.server(_life_or_chat)
        d = self.live_daemon(srv, AI_HOUSE_CONVERSE_BUDGET_S="1")
        self.slow_replies(d, 1.3)
        res = d.converse("private:" + d.vocab.ROSTER[0], "你好呀", owner_speaker("房主"))
        self.assertTrue(res["ok"])
        self._assert_step_ran_on_fresh_budget(d, srv)
        # and the next steps keep asking (nothing is stuck degraded). A chat turn no longer queues an extra
        # decision by itself, so the next one is due at the normal free-time cadence (about 2 h): step up to 3 h.
        before = _life_hits(srv)
        for _ in range(18):
            d.clock.advance(600)
            d.authority.step()
            if _life_hits(srv) > before:
                break
        self.assertGreater(_life_hits(srv), before, "no decision was asked within 3 virtual hours")

    def test_group_turn_with_slow_replies_does_not_starve_the_step(self) -> None:
        srv = self.server(_life_or_chat)
        d = self.live_daemon(srv, AI_HOUSE_CONVERSE_BUDGET_S="1")
        self.slow_replies(d, 0.8)
        names = [d.vocab.NAMES[a] for a in list(d.vocab.NAMES)[:2]]
        res = d.converse("group", f"{names[0]}、{names[1]}，你们都说一句", owner_speaker("房主"))
        self.assertTrue(res["ok"])
        self._assert_step_ran_on_fresh_budget(d, srv)

    def test_hanging_life_endpoint_keeps_the_step_bounded_by_one_life_budget(self) -> None:
        # the renewed scope must still be a scope: a hung endpoint cannot stretch the step to minutes
        srv = self.server(_hang_life)
        d = self.live_daemon(srv, AI_HOUSE_LLM_TOTAL_BUDGET_S="2", AI_HOUSE_CHAT_TIMEOUT_S="4")
        t = time.monotonic()
        res = d.converse("private:" + d.vocab.ROSTER[0], "你好呀", owner_speaker("房主"))
        el = time.monotonic() - t
        self.assertTrue(res["ok"])
        self.assertLessEqual(el, 2.0 * 3 + 6.0, f"turn took {el:.1f}s")

    def test_life_call_under_an_expired_deadline_is_not_called_and_next_step_asks_again(self) -> None:
        srv = self.server(_life_or_chat)
        d = self.live_daemon(srv)
        led = LifeLedger(d)
        with deadline_scope(0.01):
            time.sleep(0.05)
            d.authority.step()
        self.assertEqual(srv.hits, 0, "a spent deadline sends no request")
        self.assertEqual(led.states().count("unknown"), 0, led.states())
        self.assertNotIn("unknown", led.budget_states())
        self.assertEqual(d.authority.life.cog.budget.used(), 0, "nothing was called, nothing counts against the budget")
        self.assertIn("released", led.budget_states())
        # next step, no deadline: the same decisions are asked normally (not degraded for good)
        d.clock.advance(600)
        d.authority.step()
        self.assertGreater(srv.hits, 0)
        self.assertIn("free_time", led.kinds("ready") + led.kinds("done"),
                      led.rows("SELECT actor_id,state,json_extract(payload,'$.kind') FROM cognition_requests"))

    def test_deadline_error_after_a_request_was_sent_still_counts_as_unknown(self) -> None:
        # the guard is not blanket: a call that did reach the endpoint and then timed out keeps counting
        srv = self.server(_hang_life)
        d = self.live_daemon(srv, AI_HOUSE_LLM_TOTAL_BUDGET_S="1", AI_HOUSE_CHAT_TIMEOUT_S="4")
        led = LifeLedger(d)
        d.authority.step()
        self.assertGreater(_life_hits(srv), 0)
        self.assertIn("unknown", led.states())


class RequestSentFlagTests(Base):
    def env(self, srv: FakeServer, **extra: str) -> Dict[str, str]:
        return {"AI_HOUSE_API_KEY": KEY, "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "m", **extra}

    def test_spent_deadline_before_send_reports_request_not_sent(self) -> None:
        srv = self.server()
        r = self.router(self.env(srv))
        with deadline_scope(0.01):
            time.sleep(0.05)
            with self.assertRaises(LLMDeadlineError) as cm:
                r.complete_text("s", "u", purpose="life")
        self.assertIs(cm.exception.request_sent, False)
        self.assertEqual(srv.hits, 0)

    def test_deadline_after_a_hung_request_reports_request_sent(self) -> None:
        srv = self.server(lambda n, b, h: ("hang", 30))
        r = self.router(self.env(srv, AI_HOUSE_LLM_TOTAL_BUDGET_S="1", AI_HOUSE_CHAT_TIMEOUT_S="4"))
        with self.assertRaises(LLMDeadlineError) as cm:
            r.complete_text("s", "u", purpose="chat")
        self.assertIs(cm.exception.request_sent, True)

    def test_renewed_scope_ignores_a_spent_outer_deadline_but_deadline_scope_still_takes_the_earliest(self) -> None:
        with deadline_scope(0.01):
            time.sleep(0.05)
            self.assertLess(remaining_budget(), 0)
            with renewed_deadline_scope(30):
                self.assertGreater(remaining_budget(), 25)
                with deadline_scope(100):  # nesting inside a renewed scope: earliest wins as before
                    self.assertLess(remaining_budget(), 31)
                with deadline_scope(2):
                    self.assertLess(remaining_budget(), 2.1)
            self.assertLess(remaining_budget(), 0, "leaving the renewed scope restores the outer deadline")
        with deadline_scope(5):
            with deadline_scope(100):
                self.assertLess(remaining_budget(), 5.1)


# --------------------------------------------------------------------------------------------- 2. breaker
class EmptyReplyBreakerTests(Base):
    def env(self, srv: FakeServer) -> Dict[str, str]:
        return {"AI_HOUSE_API_KEY": KEY, "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "m"}

    def test_consecutive_empty_replies_do_not_open_the_breaker_and_chat_still_works(self) -> None:
        mode = {"empty": True}
        srv = self.server(lambda n, b, h: (200, EMPTY_STOP_DOC if mode["empty"] else OK_DOC))
        r = self.router(self.env(srv))
        for i in range(5):  # more than the breaker threshold
            before = srv.hits
            with self.assertRaises(LLMAPIError) as cm:
                r.complete_text("s", "u", purpose="memory_extract")
            self.assertEqual(cm.exception.detail, "empty_reply", f"call {i}: {cm.exception}")
            self.assertEqual(srv.hits - before, 2, f"call {i} must still reach the endpoint (no CIRCUIT_OPEN)")
        self.assertFalse([x for x in self.usage_rows(r) if x["error_code"] == "CIRCUIT_OPEN"])
        mode["empty"] = False
        self.assertEqual(r.complete_text("s", "u", purpose="chat")[0], "好的")

    def test_empty_reply_still_falls_back_and_counts_as_a_failed_call(self) -> None:
        f = self.server(lambda n, b, h: (200, OK_DOC))
        p = self.server(lambda n, b, h: (200, EMPTY_STOP_DOC))
        r = self.router({**self.env(p), "AI_HOUSE_FALLBACK_API_KEY": "sk-" + "f" * 10,
                         "AI_HOUSE_FALLBACK_BASE_URL": f.base_url})
        self.assertEqual(r.complete_text("s", "u", purpose="memory_extract")[0], "好的")
        self.assertEqual(f.hits, 1)
        self.assertTrue([x for x in self.usage_rows(r) if x["error_code"] == "EMPTY_REPLY"])

    def test_real_5xx_still_opens_the_breaker(self) -> None:
        srv = self.server(lambda n, b, h: (503, {"error": {"message": "overloaded"}}))
        r = self.router(self.env(srv))
        for _ in range(3):
            with self.assertRaises(LLMAPIError):
                r.complete_text("s", "u", purpose="chat")
        hits = srv.hits
        with self.assertRaises(LLMAPIError) as cm:
            r.complete_text("s", "u", purpose="chat")
        self.assertEqual(srv.hits, hits, "breaker open: no request leaves")
        self.assertEqual(cm.exception.detail, "circuit_open")

    def test_connection_failures_still_open_the_breaker(self) -> None:
        srv = self.server()
        url = srv.base_url
        srv.close()  # nothing listens any more
        r = self.router({"AI_HOUSE_API_KEY": KEY, "AI_HOUSE_BASE_URL": url, "AI_HOUSE_MODEL": "m"})
        for _ in range(3):
            with self.assertRaises(Exception):
                r.complete_text("s", "u", purpose="chat")
        with self.assertRaises(LLMAPIError) as cm:
            r.complete_text("s", "u", purpose="chat")
        self.assertEqual(cm.exception.detail, "circuit_open")


# --------------------------------------------------------------------------------------------- 3. budget = 0
class ZeroBudgetTests(Base):
    def router_with(self, **env: str) -> Any:
        return self.router({"AI_HOUSE_API_KEY": KEY, "AI_HOUSE_BASE_URL": "http://127.0.0.1:9", **env})

    def test_zero_negative_empty_or_garbage_total_budget_means_the_purpose_default(self) -> None:
        for raw in ("0", "0.0", "-3", "", "abc", "nan"):
            for purpose, default in DEFAULT_TOTAL_BUDGET_S.items():
                r = self.router_with(AI_HOUSE_LLM_TOTAL_BUDGET_S=raw)
                self.assertEqual(r.resolve_total_budget(purpose), default, (raw, purpose))
                r2 = self.router_with(**{f"AI_HOUSE_{purpose.upper()}_TOTAL_BUDGET_S": raw})
                self.assertEqual(r2.resolve_total_budget(purpose), default, (raw, purpose))

    def test_positive_values_still_apply_and_are_clamped(self) -> None:
        self.assertEqual(self.router_with(AI_HOUSE_LLM_TOTAL_BUDGET_S="7").resolve_total_budget("chat"), 7.0)
        self.assertEqual(self.router_with(AI_HOUSE_LIFE_TOTAL_BUDGET_S="9999").resolve_total_budget("life"), 600.0)
        self.assertEqual(self.router_with(AI_HOUSE_LLM_TOTAL_BUDGET_S="0.1").resolve_total_budget("chat"), 0.5)

    def test_converse_and_attachment_budget_zero_use_defaults(self) -> None:
        self.assertEqual(daemon_mod._env_budget("0", 45.0), 45.0)
        self.assertEqual(daemon_mod._env_budget("", 10.0), 10.0)
        self.assertEqual(daemon_mod._env_budget("-1", 10.0), 10.0)
        self.assertEqual(daemon_mod._env_budget("3", 10.0), 3.0)
        d = self._daemon({"AI_HOUSE_OWNER_TOKEN": "owner-" + "t" * 10, "AI_HOUSE_CONVERSE_BUDGET_S": "0"})
        seen: List[float] = []
        real = daemon_mod.deadline_scope

        @contextlib.contextmanager
        def spy(b: float):
            seen.append(float(b))
            with real(b) as eff:
                yield eff

        daemon_mod.deadline_scope = spy
        self.addCleanup(setattr, daemon_mod, "deadline_scope", real)
        d.converse("private:" + d.vocab.ROSTER[0], "你好呀", owner_speaker("房主"))
        self.assertEqual(seen[0], 45.0, "CONVERSE_BUDGET_S=0 must mean the default ceiling, not 1s")


if __name__ == "__main__":
    unittest.main()
