"""Real-run batch 1 (38.5 virtual hours, gpt-4o-mini): defects of the promise ("约定") loop, on the shared promise code.

The kit used to record promises by rule from the chat turn. Now a promise comes only from memory extraction and the
shared module (`ai_house.common.intents`, through `CommonIntents`) decides who said yes and what closes it. What is
still checked here is what the real run found and the shared code must keep:

1. an unrelated home event or another trip does not close a promise; only the named place / activity does;
2. a resident who declined in her reply is not recorded as having agreed (no accepted promise, no errand);
3. a single-character alias ("米") inside a name ("米娜") plus a "买" elsewhere is not a rice errand (`buy_words`);
4. "买不了" is glued onto the reply of an owner request for something the catalogue does not sell (`buy_words`).

The old cases about rule-based recording, small talk by keyword, diet memos and reminder rows are retired with the rule
code; see docs/_m2-cases-g1.md. A fake extraction model is the only "model"; nothing here uses the network.
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
from worldkernel import buy_words, vocab_life1  # noqa: E402
from test_m2_intents_adapter import line, standalone  # noqa: E402

T0 = dt.datetime(2026, 11, 21, 14, 0, tzinfo=JST).timestamp()
H = 3600.0
PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
NAMES = {k: str(v) for k, v in vocab_life1.NAMES.items()}
KERNEL_ENV = {"INTENTS_KERNEL_MATCH": "on", "INTENTS_EXPIRY_V2": "1", "INTENTS_DECIDE_CTX": "1",
              "WORLD_KERNEL_ERRANDS_FROM_INTENTS": "1"}


class _StoreCase(unittest.TestCase):
    """The shared promise code over the real life1 vocabulary (the one the long run used), as the house runs it.

    `chat()` feeds one memory batch to `apply_chat` with the ops a fake extraction model answered; `evidence()` puts a
    row of the kernel's evidence stream where `sweep` reads it.
    """

    STREAM = "test#1"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "world.db")
        self.ci = standalone(self.db, KERNEL_ENV, clock=lambda: T0)
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute("create table if not exists kernel_bind(k text primary key, v text)")
                c.execute("insert or replace into kernel_bind values('world', ?)", (self.STREAM,))
        self._n = 0

    def chat(self, consumer: str, said: List[Any], ops: List[Dict[str, Any]], *, now: float = T0,
             group: bool = True) -> Dict[str, int]:
        """said: [(who, text)] in order; who is a resident key or "owner". `consumer` is the resident whose memory runs."""
        i = self.ci.intents_module
        lines = []
        for k, (who, text) in enumerate(said, 1):
            if who == "owner":
                lines.append(line("房主", 1, text, k))
            else:
                lines.append(line(NAMES[who] + ("(你)" if who == consumer else ""), i.ID.ACTOR_QQ[who], text, k))
        return i.apply_chat(consumer, "group:1" if group else "private:1", not group, ops, lines, now=now,
                            times=[now] * len(lines))

    def rows(self, actor: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.ci.list_intents(actor=actor, limit=50)

    def evidence(self, body: Dict[str, Any]) -> str:
        self._n += 1
        key = f"{self.STREAM}#{self._n}"
        body = {"key": key, "plan_id": f"plan-{self._n}", **body}
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute("insert into intent_evidence values(?, ?, ?, ?, ?, 'queued')",
                          (key, self.STREAM, self._n, float(body["at"]), json.dumps(body)))
        return key

    def sweep(self, now: float) -> Any:
        return self.ci.sweep(now)


def plan_ops(text: str, quote: str, **kw: Any) -> List[Dict[str, Any]]:
    return [{"op": "intent", "text": text, "kind": "plan", "quote": quote, "scope": "world", "when": "vague", **kw}]


class UnrelatedHomeEventsDoNotCloseOutings(_StoreCase):
    TEA_PLAN_DONE = {"kind": "plan_finished", "actor": "wanda", "place": "living", "outcome": "done",
                     "members": ["wanda", "mina"]}

    def promise(self, text: str = "婉妲下午去公园", quote: str = "下午去公园") -> None:
        got = self.chat("wanda", [("wanda", f"我{quote}")], plan_ops(text, f"我{quote}"))
        self.assertEqual(got["intents"], 1, got)
        self.assertEqual([(r["state"], r["status"]) for r in self.rows("wanda")], [("open", "accepted")])

    def test_tea_at_home_does_not_close_an_outing(self) -> None:
        """Fails if a plan that finished at home (a tea party) closes a promise to go to a named place, or if the
        arrival at that place (the control, in the same scenario) does not close it."""
        for name, place in (("公园", "place:park"), ("书店", "place:bookshop"), ("图书馆", "place:library")):
            with self.subTest(place=place):
                self.setUp()
                self.promise(f"婉妲下午去{name}", f"下午去{name}")
                self.evidence({**self.TEA_PLAN_DONE, "at": T0 + 2 * H})
                self.sweep(T0 + 3 * H)
                self.assertEqual(self.rows("wanda")[0]["state"], "open")
                self.evidence({"kind": "arrived", "actor": "wanda", "place": place, "at": T0 + 4 * H})
                self.sweep(T0 + 5 * H)
                self.assertEqual(self.rows("wanda")[0]["state"], "completed")

    def test_shopping_trip_does_not_close_a_park_outing(self) -> None:
        """Fails if arriving at the supermarket and finishing that trip closes a promise to go to the park, or if the
        later arrival at the park does not close it."""
        self.promise()
        self.evidence({"kind": "arrived", "actor": "wanda", "place": "place:supermarket", "plan_id": "shopping-plan-1",
                       "at": T0 + 2 * H})
        self.evidence({"kind": "plan_finished", "actor": "wanda", "plan_id": "shopping-plan-1",
                       "place": "place:supermarket", "outcome": "done", "members": ["wanda"], "at": T0 + 2.5 * H})
        self.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "open")
        self.evidence({"kind": "arrived", "actor": "wanda", "place": "place:park", "plan_id": "walk-1", "at": T0 + 4 * H})
        self.sweep(T0 + 5 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "completed")

    def test_someone_elses_arrival_at_the_named_place_does_not_close_it(self) -> None:
        """Fails if the arrival of another resident at the park closes wanda's promise; the control is wanda's own
        arrival, which closes it and is named as the evidence."""
        self.promise()
        self.evidence({"kind": "arrived", "actor": "mina", "place": "place:park", "at": T0 + 2 * H})
        self.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "open")
        key = self.evidence({"kind": "arrived", "actor": "wanda", "place": "place:park", "at": T0 + 4 * H})
        self.sweep(T0 + 5 * H)
        row = self.rows("wanda")[0]
        self.assertEqual((row["state"], row["status"]), ("completed", "completed"))
        self.assertEqual(row["evidence_key"], key)

    def test_together_film_is_not_closed_by_a_different_activity_but_by_the_film(self) -> None:
        """Fails if a game played by both at home closes a joint promise to watch a film, or if the film night of both
        (the control) does not close it."""
        said = [("wanda", "下午一起看电影吧"), ("yuki", "好呀，一起去")]
        ops = [{"op": "intent", "text": "婉妲和由纪一起看电影", "kind": "joint", "with": ["由纪"],
                "agreed": ["由纪", "婉妲"], "quote": "下午一起看电影吧", "scope": "world", "when": "vague"}]
        self.assertEqual(self.chat("wanda", said, ops)["intents"], 1)
        self.assertEqual([r["status"] for r in self.rows("wanda")], ["accepted"])

        def activity(aid: str, plan: str, start: float) -> None:
            for who in ("wanda", "yuki"):
                self.evidence({"kind": "activity", "actor": who, "plan_id": plan, "activity": aid, "place": "living",
                               "started_at": start, "ended_at": start + 1200, "outcome": "succeeded", "at": start + 1200})

        activity("game_tournament", "plan-game", T0 + H)
        self.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "open")
        activity("movie_night", "plan-movie", T0 + 3 * H)
        self.sweep(T0 + 5 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "completed")


class DecliningReplyIsNotAnAgreement(_StoreCase):
    EGG = "谁方便的话，顺路帮我买两个鸡蛋回来吧，拜托啦"
    REFUSAL = "我现在在家发呆，没法出门买鸡蛋。要不我等会儿去厨房看看有什么食材？"

    def egg(self, name: str) -> List[Dict[str, Any]]:
        return [{"op": "intent", "text": f"{name}买两个鸡蛋", "kind": "promise", "for": ["房主"], "agreed": [name],
                 "quote": "顺路帮我买两个鸡蛋回来吧", "scope": "world", "when": "vague"}]

    def test_resident_who_refused_gets_no_errand_and_the_one_who_agreed_keeps_hers(self) -> None:
        """Fails if a promise is recorded for 阿尔玛 although the model lists her as agreed and her line refuses, if she
        gets an errand, or if 奈尔, who said yes to the same sentence (the control), does not get hers."""
        got = self.chat("alma", [("owner", self.EGG), ("alma", self.REFUSAL)], self.egg("阿尔玛"))
        self.assertEqual((got["intents"], got["rejected"]), (0, 1), got)
        got = self.chat("nell", [("owner", self.EGG), ("nell", "好呀，我下午去超市顺便带回来。"), ("alma", self.REFUSAL)],
                        self.egg("奈尔"))
        self.assertEqual(got["intents"], 1, got)
        self.assertEqual(self.rows("alma"), [])
        self.assertEqual([(r["actor"], r["status"]) for r in self.rows("nell")], [("nell", "accepted")])
        self.assertEqual(self.ci.errands("alma", T0 + 60), [])
        self.assertEqual([(e["item"], e["qty"]) for e in self.ci.errands("nell", T0 + 60)], [("egg", 2)])

    def test_a_refusal_never_makes_an_accepted_plan_or_promise(self) -> None:
        """Fails if 米娜's counter-question or 奈尔's "no way" is taken for a yes (an accepted row, or an errand-like
        promise), or if 婉妲's plain yes to the same kind of invitation (the control) is not accepted."""
        walk = [{"op": "intent", "text": "米娜和房主明天一起去散步", "kind": "joint", "with": ["房主"],
                 "agreed": ["房主", "米娜"], "quote": "明天一起去散步吧", "scope": "world", "when": "vague"}]
        self.chat("mina", [("owner", "米娜，明天一起去散步吧"), ("mina", "我明天上午有点事，能不能改成下午？")], walk)
        curry = [{"op": "intent", "text": "奈尔今晚做咖喱饭", "kind": "promise", "for": ["房主"], "agreed": ["奈尔"],
                  "quote": "今晚的晚饭能做咖喱饭吗", "scope": "world", "when": "vague"}]
        got = self.chat("nell", [("owner", "奈尔，今晚的晚饭能做咖喱饭吗？"), ("nell", "不好意思，今晚我没办法做饭。")], curry)
        self.assertEqual(got["rejected"], 1, got)
        self.assertEqual(self.rows("nell"), [])
        self.assertEqual([(r["status"], r["agreed"]) for r in self.rows("mina")], [("proposed", ["owner"])])
        yes = [{"op": "intent", "text": "婉妲和房主明天一起去散步", "kind": "joint", "with": ["房主"],
                "agreed": ["房主", "婉妲"], "quote": "明天一起去散步吧", "scope": "world", "when": "vague"}]
        self.chat("wanda", [("owner", "婉妲，明天一起去散步吧"), ("wanda", "好呀")], yes)
        self.assertEqual([(r["status"], sorted(r["agreed"])) for r in self.rows("wanda")],
                         [("accepted", ["owner", "wanda"])])
        for who in ("mina", "nell", "wanda"):
            self.assertEqual(self.ci.errands(who, T0 + 60), [])


class NamesAndNounsAreNotIngredients(unittest.TestCase):
    def detect(self, text: str) -> Optional[str]:
        return buy_words.detect_errand_item(vocab_life1, text)[0]

    def test_mina_is_not_rice(self) -> None:
        """Fails if the name 米娜 plus a 买 elsewhere ("买衣服") is read as a rice errand, or if a plain request for rice
        (the control) is not read as one."""
        self.assertIsNone(self.detect("米娜，明天上午陪我去逛街买衣服，可以吗？"))
        self.assertEqual(self.detect("米娜，帮我买点米回来"), "rice")

    def test_single_char_alias_needs_to_be_the_thing_bought(self) -> None:
        """Fails if a one-character item word inside a name or another word ("米娜", "鱼缸", "米价") is taken for the
        item; the controls are the same words as real purchases."""
        none_of_these = [
            "米娜，明天上午陪我去逛街买衣服，可以吗？",
            "帮米娜买点东西",
            "买鱼缸",
            "今天去买鱼香肉丝的材料之前先看看米价",
            "你今天买衣服了吗，米娜说你去了",
        ]
        for text in none_of_these:
            with self.subTest(text=text):
                self.assertNotIn(self.detect(text), ("rice", "fish"))
        self.assertEqual(self.detect("帮我买条鱼"), "fish")

    def test_buying_rice_or_fish_still_works(self) -> None:
        """Fails if a genuine request for rice or fish is not recognised."""
        for text, item in (("帮我买点米回来", "rice"), ("买米吧", "rice"), ("记得买两袋米", "rice"),
                           ("缺米了，补点", "rice"), ("帮我买条鱼", "fish"), ("路过超市买鱼回来", "fish"),
                           ("帮我买大米", "rice")):
            with self.subTest(text=text):
                self.assertEqual(self.detect(text), item)

    def test_resident_names_are_not_matched_even_for_longer_aliases(self) -> None:
        """Fails if a resident's name takes part in item matching; the control is the same sentence without the name."""
        self.assertIn("米娜", vocab_life1.NAMES.values())
        self.assertIsNone(self.detect("米娜帮我买点东西"))
        self.assertEqual(self.detect("帮我买点米"), "rice")


class _CapturingModel:
    """Loopback OpenAI-compatible model that remembers the system prompts it was shown."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.systems: List[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                msgs = body.get("messages") or []
                # only chat turns (the world step talks to the model too, with a different prompt)
                if any("刚刚说" in str(m.get("content") or "") for m in msgs if m.get("role") == "user"):
                    outer.systems.extend(str(m.get("content") or "") for m in msgs if m.get("role") == "system")
                raw = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": outer.reply}}],
                    "usage": {"total_tokens": 10},
                }).encode("utf-8")
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
    def boot(self, reply: str) -> _CapturingModel:
        srv = _CapturingModel(reply)
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
        return srv

    def talk(self, text: str) -> str:
        res = self.d.converse("private:sora", text, owner_speaker("房主"))
        return res["replies"][0]["text"]


class ReplyTailSaysItCannotBeBought(_DaemonCase):
    TAIL = "不过「榴莲」我这边买不了，没法替你带回来。"

    def test_a_request_for_something_not_sold_gets_the_tail_and_a_purchasable_one_does_not(self) -> None:
        """Fails if the reply to "能帮我买个榴莲回来吗？" (not in the catalogue) lacks the tail, if the milk request (the
        control, same daemon) gets any "买不了", or if unrelated chat afterwards gets the tail."""
        self.boot("好的，我再想想")
        text = self.talk("能帮我买个榴莲回来吗？")
        self.assertIn(self.TAIL, text)
        self.assertTrue(text.startswith("好的，我再想想"), text)
        self.assertNotIn("买不了", self.talk("能帮我买两盒牛奶回来吗？"))
        self.assertNotIn("买不了", self.talk("今天天气怎么样？"))

    def test_already_declining_reply_is_not_given_a_second_contradicting_tail(self) -> None:
        """Fails if a reply that already says "买不了" gets the tail as well (two sentences saying the same, one of
        them saying "没法替你带回来" after a promise); the control is the non-declining reply of the test above."""
        self.boot("榴莲我买不了，只能先记个提醒。")
        text = self.talk("能帮我买个榴莲回来吗？")
        self.assertEqual(text.count("买不了"), 1, text)
        self.assertNotIn(self.TAIL, text)


if __name__ == "__main__":
    unittest.main()
