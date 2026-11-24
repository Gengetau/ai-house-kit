"""Real-run batch 7 / round 3 (everyone on gemini-4-hydrogen): two defects found in the transcript.

1. Declines that the keyword list did not know ("今天大概率不出门", "暂时没打算出门", "榴莲我买不到") were recorded as an
   agreement with the owner (an errand that later expired), or got a second "买不了" sentence glued on by the daemon.
2. A reply cut by the output limit (`finish_reason=length`, partial text) was posted as the resident's words. The model's
   thinking tokens eat into `max_tokens`: the usage ledger shows completion_tokens=13 for a call whose total-minus-prompt is
   exactly the 400 limit. The router only handled "length with empty text".
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
from ai_house.daemon import HouseDaemon  # noqa: E402
from ai_house.llm import ModelRouter, _trim_to_last_sentence  # noqa: E402
from ai_house.daemon import JST  # noqa: E402
from worldkernel.buy_words import reply_declines  # noqa: E402
from test_m2_intents_adapter import line, standalone  # noqa: E402

# Shared with test_realrun_8_round4 / test_realrun_9_leftovers (they import these from here, not from realrun_1).
T0 = dt.datetime(2026, 11, 21, 14, 0, tzinfo=JST).timestamp()
H = 3600.0
PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


# ---------------------------------------------------------------------------------------------------------------------
# helpers (also used by test_realrun_8_round4 and test_realrun_9_leftovers)

NAMES = {"alma": "阿尔玛", "elsa": "艾莎", "mina": "米娜", "nell": "奈尔", "wanda": "婉妲", "yuki": "由纪"}


class _PromiseCase(unittest.TestCase):
    """The promise code a house runs (`CommonIntents` on a fresh world.db), fed the way the memory store feeds it.

    A promise exists only when an extraction answer (here: a made-up one) is applied to the chat lines of that batch.
    `say` plays one batch for every resident who answered: the owner's line, then each answer, and for each of them the
    extraction op "she agreed", exactly as a model that always claims agreement would write it. What is then recorded
    is decided by the shared module's own checks of what the lines say.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "world.db")
        # the switches the daemon sets for a house (so `errands` and the evidence gate work as in a running house)
        self.ci = standalone(self.db, {"INTENTS_EVIDENCE_GATE": "1", "INTENTS_KERNEL_MATCH": "on",
                                       "INTENTS_EXPIRY_V2": "1", "INTENTS_DECIDE_CTX": "1",
                                       "WORLD_KERNEL_ERRANDS_FROM_INTENTS": "1"}, clock=lambda: T0)
        self.i = self.ci.intents_module
        self._n = 0
        with contextlib.closing(sqlite3.connect(self.db)) as c:  # what the kernel's bridge keeps in a running house
            with c:
                c.execute("create table if not exists kernel_bind(k text primary key, v text not null)")
                c.execute("insert or replace into kernel_bind values('world', 'ev')")

    def say(self, owner_text: str, replies: Dict[str, str], *, group: bool = False, now: float = T0,
            text: Optional[str] = None, kind: str = "promise", due: str = "", when: str = "vague") -> None:
        lines = [line("房主", 1, owner_text, 1)]
        for k, (actor, reply) in enumerate(replies.items()):
            lines.append(line(f"{NAMES[actor]}", self.i.ID.ACTOR_QQ[actor], reply, k + 2))
        for actor in replies:
            op = {"op": "intent", "text": text or owner_text, "kind": kind, "for": ["房主"],
                  "agreed": [NAMES[actor]], "scope": "world", "when": when, "quote": owner_text}
            if due:
                op["due"] = due
            self.i.apply_chat(actor, "group:1" if group else "private:1", not group, [op], lines, now=now,
                              times=[now] * len(lines))

    def rows(self, actor: Optional[str] = None) -> List[Dict[str, Any]]:
        """The `intents` table as the shared module wrote it (`listing` hides private rows' text)."""
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            c.row_factory = sqlite3.Row
            got = c.execute("SELECT * FROM intents ORDER BY id").fetchall()
        return [dict(r) for r in got if actor is None or r["actor"] == actor]

    def errands(self, actor: str, now: float = T0 + 60) -> List[Dict[str, Any]]:
        return self.ci.errands(actor, now)

    def evidence(self, body: Dict[str, Any]) -> None:
        self._n += 1
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute("INSERT INTO intent_evidence(key, stream, event_id, at, body, state) VALUES(?, 'ev', ?, ?, ?, 'queued')",
                          (f"k{self._n}", self._n, float(body.get("ended_at") or body["at"]), json.dumps(body)))


class _CapturingModel:
    """Loopback OpenAI-compatible model that remembers the system prompts it was shown on chat turns."""

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
    def boot(self, reply: str, start_ts: float = T0) -> _CapturingModel:
        srv = _CapturingModel(reply)
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = HouseDaemon(
            tmp.name,
            pack_dir=PACK,
            env={"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"},
            start_ts=start_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(self.d.close)
        return srv

    def talk(self, text: str) -> str:
        res = self.d.converse("private:sora", text, owner_speaker("房主"))
        return res["replies"][0]["text"]

EGG = "谁方便的话，顺路帮我买两个鸡蛋回来吧，拜托啦"

# Every "owner asks a resident to do something -> resident answers" pair of the evidence runs (gemini / mixed / terra /
# mini), with the verdict the rule must give: True = declines, False = not a refusal.
REAL_REPLIES = [
    # 0-3 buy milk
    ("好的，我记下了，晚点出门时顺便帮你买一盒牛奶回来。我现在正要去厨房看看，你待会儿也记得好好吃早饭哦。", False),
    ("好的，房主，我会帮你买一盒牛奶的！如果还有其他需要的东西，随时告诉我哦。", False),
    ("好呀，我记下了，一盒牛奶。", False),
    ("好的，等方便出门时，我去买一盒回来。", False),
    # 4-7 buy durian
    ("抱歉，常去的超市里只有常见食材和零食，榴莲我买不到。如果你需要的话，我可以先帮你记个提醒，或者改带别的水果？", True),
    ("抱歉，榴莲我这边买不了，平时去超市只采购得到常见的水果和日常食材。如果你怕自己忘了，我可以先帮你记个提醒，或者换种别的水果我顺路带回来？", True),
    ("榴莲我买不了，我只能采购超市里常见的食材、零食和饮品。不过可以先帮你记个提醒。", True),
    ("房主，我现在正前往一楼，稍后可以考虑这件事。不过，榴莲的气味确实让人难以忽视。", False),
    # 8-11 two eggs
    ("早啊，我刚睡过头，人还在卧室发呆开机呢，今天大概率不出门。说起来厨房里不是还有十来个鸡蛋吗，是不够用还是准备做什么大菜？", True),
    ("我现在在家发呆，没法出门买鸡蛋。要不我等会儿去厨房看看有什么食材？", True),
    ("我现在在卧室发呆，暂时没打算出门。要是等会儿出去，可以顺路帮你买两个。", True),
    ("我现在在卧室发呆，暂时没打算出门；待会儿要是出去，可以顺路帮你买两个。", True),
    # 12-15 curry
    ("好的，没问题。厨房里正好还有咖喱块、胡萝卜和肉，等我在房间休息一会儿就去准备，今晚一定会做一锅香喷喷的咖喱饭，到时候记得按时过来吃哦。", False),
    ("好的，房主，我会做咖喱饭的！如果有其他想要的配菜，记得告诉我哦。", False),
    ("好呀，今晚我来做咖喱饭，等我回家就准备。", False),
    ("当然可以，今晚我来做咖喱饭。现在还没到饭点，等晚些时候再准备吧。", False),
    # 16-19 afternoon tea
    ("好呀好呀，我正抱着手机犯馋呢！家里红茶、牛奶和水果都还剩好多，现在正好两点了，你快下楼来客厅，我们一起泡热奶茶配黄油烤面包吧！", False),
    ("当然好呀！我已经准备好了甜点和茶，期待和你们一起享受这个美好的下午时光！你们想吃点什么呢？", False),
    ("当然要啦，这正合我意！我看家里正好还有红茶、面包和黄油，要不我们叫上客厅的大家一起泡壶热茶烤点面包？你要是想吃别的甜点也可以告诉我哦！", False),
    ("好呀！下午茶配点甜食最棒了，你想吃什么？", False),
    # 20-27 park tomorrow
    ("好呀，正好最近想出去透透气。明天下午带本小速写本去公园散散步，顺便找找画画的灵感。", False),
    ("好耶，必须算我一个！我现在在院子里吹风都觉得好惬意，明天咱们带壶红茶和小甜点去公园野餐吧，刚好也能和凯多聊聊", False),
    ("听起来很不错呢，公园的阳光和微风会让人心情愉悦。大家要记得带上相机哦，可能会有很多灵感涌现出来。", False),
    ("哇，公园玩耍听起来太棒了！阳光、草地，还有可能的甜点，绝对是个完美的下午！大家都期待一起去吗？", False),
    ("好呀，明天下午一起去公园走走吧，说不定能找到画画的灵感。", False),
    ("好呀，明天下午一起去公园走走吧，或许能找到画画的灵感。", False),
    ("好呀！去公园走走，顺便带点甜食当下午茶吧？凯也可以带上相机！", False),
    ("好耶，必须算我一个！", False),
    # 28-31 shopping with the owner tomorrow morning
    ("明天上午啊……对我这种夜猫子来说早起简直是要命的Boss战！不过既然是你开口叫我，那肯定讲义气陪你去逛街，明早要是没动静记得来用力敲我门哦！", False),
    ("我明天上午有点事，能不能改成下午？我可以帮你挑好看的衣服哦！", True),
    ("啊明天上午啊，虽然早起对我这个夜猫子来说简直是地狱级难度，但既然是你叫我帮忙，那肯定没问题啦！我今晚尽量早点收手睡觉，明天准时陪你去挑衣服！", False),
    ("明天上午啊，有点考验我这个夜猫子！不过陪你逛街没问题，几点出发呀？", False),
    # 32 a private invitation answered by the sleeping notice
    ("（婉妲已经在卧室睡着了，手机屏幕亮了一下没有立刻回复……）", False),
]


class RefusalPhrases(unittest.TestCase):
    def test_every_real_reply_gets_the_verdict_a_human_would_give(self) -> None:
        for text, want in REAL_REPLIES:
            with self.subTest(text=text[:40]):
                self.assertEqual(reply_declines(text), want)

    def test_more_ways_of_saying_no(self) -> None:
        for text in (
            "我今天不出门。", "我暂时没打算出门。", "我不打算出去了。", "这个我买不到。", "太重了，我带不了。",
            "这事我帮不上忙。", "明天我去不成了。", "估计不去了，抱歉。", "应该不会出门吧。", "大概率不买哦。",
            "估计不出门。", "我今天大概率不去超市。",
        ):
            with self.subTest(text=text):
                self.assertTrue(reply_declines(text))

    def test_the_first_stance_still_decides(self) -> None:
        for text in (
            "好的，我会买，不过现在没法出门。",
            "好的，我会买的，不过现在还没法出门，下午去。",
            "可以呀，大概率不会忘，我下午去买。",
            "好呀，我下午出门，不买就不回来。",
        ):
            with self.subTest(text=text):
                self.assertFalse(reply_declines(text))
        self.assertTrue(reply_declines("我现在在家发呆，没法出门买鸡蛋。"))

    def test_plain_chat_with_no_stance_is_not_a_refusal(self) -> None:
        for text in ("早啊，今天天气不错。", "我记得你对花生过敏。", "我在厨房，不出声地看书。", ""):
            with self.subTest(text=text):
                self.assertFalse(reply_declines(text))


class SlackerRepliesMakeNoErrand(_PromiseCase):
    """Defect 1 on the new path: an extraction answer that says "she agreed" is checked against the lines of the batch.

    The model here always claims agreement (see `_PromiseCase.say`); the shared module must still refuse it when her
    reply does not say yes. Each case has its positive control in the same scenario: the same request, answered with a
    real yes, does become an errand for the same resident.
    """

    YES = "好呀，我下午去超市顺便带回来。"
    EGGS = "买2个鸡蛋"

    def agrees_later(self, actor: str, expect_item: str = "egg") -> None:
        """Control: the same owner asks again and she says yes -> one accepted promise and an errand for 2 eggs."""
        later = T0 + 600
        self.say(EGG, {actor: self.YES}, group=True, now=later, text=NAMES[actor] + self.EGGS)
        self.assertEqual([r["status"] for r in self.rows(actor)], ["accepted"])
        self.assertEqual([(e["item"], e["qty"]) for e in self.errands(actor, later + 60)], [(expect_item, 2)])

    def test_gemini_alma_overslept_and_is_not_going_out(self) -> None:
        """Fails if a reply that only says she is not going out ("今天大概率不出门") is recorded as agreeing to fetch
        eggs although the model claims she agreed, or if the control (the same request answered "好呀") gives nothing."""
        self.say(EGG, {"alma": REAL_REPLIES[8][0]}, group=True, text="阿尔玛" + self.EGGS)
        self.assertEqual(self.rows("alma"), [])
        self.assertEqual(self.errands("alma"), [])
        self.agrees_later("alma")

    def test_terra_alma_gives_only_a_conditional_maybe(self) -> None:
        """Fails if "暂时没打算出门；待会儿要是出去，可以顺路帮你买两个" (a maybe, not a yes) in either of its two real
        wordings is recorded, or if the control (a real yes to the same request) gives nothing."""
        self.say(EGG, {"alma": REAL_REPLIES[11][0]}, group=True, text="阿尔玛" + self.EGGS)
        self.assertEqual(self.rows("alma"), [])
        self.say(EGG, {"alma": REAL_REPLIES[10][0]}, group=True, text="阿尔玛" + self.EGGS, now=T0 + 60)
        self.assertEqual(self.rows("alma"), [])
        self.assertEqual(self.errands("alma", T0 + 120), [])
        self.agrees_later("alma")

    def test_somebody_else_who_agrees_keeps_the_errand(self) -> None:
        """Fails if alma's refusal is recorded, if nell's yes to the same request is lost, if nell's promise shows up
        for alma (or the other way round), or if the errand is not 2 eggs for nell alone."""
        self.say(EGG, {"alma": REAL_REPLIES[8][0]}, group=True, text="阿尔玛" + self.EGGS)
        self.say(EGG, {"nell": self.YES}, group=True, text="奈尔" + self.EGGS, now=T0 + 30)
        self.assertEqual(self.rows("alma"), [])
        self.assertEqual([(r["status"], r["party"]) for r in self.rows("nell")], [("accepted", '["nell"]')])
        self.assertEqual([(e["item"], e["qty"]) for e in self.errands("nell", T0 + 90)], [("egg", 2)])
        self.assertEqual(self.errands("alma", T0 + 90), [])

    def test_real_agreements_still_become_promises(self) -> None:
        """Fails if real agreements to buy milk (three of the evidence runs' wordings) are not recorded as accepted
        errands for 1 box of milk, or if the shared gate lets a refusal of the same request ("我现在在家发呆，没法出门")
        become one in the same scenario."""
        for k in (0, 2, 3):
            with self.subTest(reply=REAL_REPLIES[k][0][:20]):
                self.setUp()
                self.say("奈尔，帮我买一盒牛奶回来好吗？", {"nell": REAL_REPLIES[k][0]}, text="奈尔买1盒牛奶")
                self.assertEqual([(r["status"], r["visibility"]) for r in self.rows("nell")], [("accepted", "private")])
                self.assertEqual([(e["item"], e["qty"]) for e in self.errands("nell")], [("milk", 1)])
        self.setUp()
        self.say("奈尔，帮我买一盒牛奶回来好吗？", {"nell": REAL_REPLIES[9][0]}, text="奈尔买1盒牛奶")
        self.assertEqual(self.rows("nell"), [])


class UnbuyableItemGetsOneSentenceNotTwo(_DaemonCase):
    def test_a_reply_that_says_it_cannot_buy_it_gets_no_second_sentence(self) -> None:
        self.boot(REAL_REPLIES[4][0])
        text = self.talk("艾莎，能帮我买个榴莲回来吗？")
        self.assertEqual(text, REAL_REPLIES[4][0])
        self.assertNotIn("我这边买不了", text)

    def test_a_vague_reply_still_gets_the_honest_tail(self) -> None:
        self.boot("好的，我再想想")
        text = self.talk("能帮我买个榴莲回来吗？")
        self.assertIn("买不了", text)


# ---------------------------------------------------------------------------------------------------------------------
# 2. truncated replies

CUT = "中午好，房主。我还在高尾山这边观光，这会儿还没吃午饭呢，早上吃过和风早餐还不算饿。房主也记得按时吃午饭呀，别忙起来"
CUT_TRIMMED = "中午好，房主。我还在高尾山这边观光，这会儿还没吃午饭呢，早上吃过和风早餐还不算饿。"
FULL = CUT + "了就忘了喝水哦。"


class _LimitServer:
    """Loopback OpenAI-compatible endpoint whose answers depend on the output limit it was given.

    Below `need` tokens it answers `finish_reason=length` with `partial` (the thinking ate the budget); at or above
    `need` it answers `finish_reason=stop` with `full`. With `script`, request n answers `script[n]` (the last one repeats)
    with `finish_reason=length`. Every request body is recorded.
    """

    def __init__(self, *, need: int, partial: str = CUT, full: str = FULL, tool_calls: Optional[List[Dict[str, Any]]] = None,
                 script: Optional[List[str]] = None, refuse_above: int = 0) -> None:
        self.need, self.partial, self.full, self.tool_calls, self.script = need, partial, full, tool_calls, script
        self.refuse_above = refuse_above  # a strict server: a limit above this is answered with HTTP 400
        self.requests: List[Dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                outer.requests.append(body)
                lim = int(body.get("max_completion_tokens") or body.get("max_tokens") or 0)
                if outer.refuse_above and lim > outer.refuse_above:
                    raw = json.dumps({"error": {"message": "max_tokens is too large"}}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                cut = lim < outer.need
                text = outer.partial if cut else outer.full
                if outer.script is not None:
                    cut = True
                    text = outer.script[min(len(outer.requests) - 1, len(outer.script) - 1)]
                msg: Dict[str, Any] = {"role": "assistant", "content": text}
                if outer.tool_calls:
                    msg["tool_calls"] = outer.tool_calls
                raw = json.dumps({
                    "choices": [{"index": 0, "message": msg, "finish_reason": "length" if cut else "stop"}],
                    "usage": {"prompt_tokens": 1700, "completion_tokens": 13, "total_tokens": 1700 + (lim if cut else 300)},
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def limits(self) -> List[int]:
        return [int(b.get("max_completion_tokens") or b.get("max_tokens") or 0) for b in self.requests]

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class _RouterCase(unittest.TestCase):
    def router_for(self, srv: _LimitServer, model: str = "gemini-4-hydrogen") -> ModelRouter:
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = ModelRouter({"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": model},
                        runtime_dir=tmp.name, retry_delay_s=0.0)
        self.addCleanup(r.close)
        return r


class TruncatedPartialTextIsRetriedWithMoreRoom(_RouterCase):
    def test_partial_text_with_finish_reason_length_is_retried_once_with_a_bigger_limit(self) -> None:
        srv = _LimitServer(need=1000)
        r = self.router_for(srv)
        text, _ = r.complete_text("s", "u", purpose="chat")
        self.assertEqual(text, FULL)
        self.assertEqual(len(srv.requests), 2)
        self.assertEqual(srv.limits()[0], 400)
        self.assertGreaterEqual(srv.limits()[1], 4096)

    def test_the_endpoint_is_remembered_so_the_next_call_does_not_pay_twice(self) -> None:
        srv = _LimitServer(need=1000)
        r = self.router_for(srv)
        for i in range(3):
            self.assertEqual(r.complete_text("s", f"u{i}", purpose="chat")[0], FULL)
        self.assertEqual(len(srv.requests), 4, srv.limits())
        self.assertTrue(all(lim >= 4096 for lim in srv.limits()[1:]), srv.limits())

    def test_the_memory_is_per_endpoint_and_model_and_is_dropped_with_the_config(self) -> None:
        srv = _LimitServer(need=1000)
        r = self.router_for(srv)
        r.complete_text("s", "u1", purpose="chat")
        srv.requests.clear()
        r.update_env({"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "gemini-4-hydrogen"})
        r.complete_text("s", "u2", purpose="chat")
        self.assertEqual(srv.limits()[0], 400)

    def test_an_answer_that_fits_is_left_alone_and_costs_one_request(self) -> None:
        srv = _LimitServer(need=100)
        r = self.router_for(srv)
        self.assertEqual(r.complete_text("s", "u", purpose="chat")[0], FULL)
        self.assertEqual(len(srv.requests), 1)

    def test_a_retry_that_yields_nothing_falls_back_to_the_first_partial_text(self) -> None:
        # the second answer is empty+length (the thinking ate everything): better the cleaned-up first text than an error
        srv = _LimitServer(need=10 ** 9, script=[CUT, ""])
        r = self.router_for(srv)
        res = r.complete_messages([{"role": "user", "content": "u"}], purpose="chat")
        self.assertEqual(res.content, CUT_TRIMMED)
        self.assertTrue(res.truncated)

    def test_the_bigger_request_changes_the_limit_and_nothing_else(self) -> None:
        # the endpoint accepted this request shape and answered with text: the second try must not rename the limit
        # parameter or drop the temperature (a strict server would refuse it, and the resident would sound different)
        srv = _LimitServer(need=1000)
        r = self.router_for(srv)
        r.complete_text("s", "u", purpose="chat")
        first, second = srv.requests
        self.assertEqual({k for k in first if k != "max_tokens"}, {k for k in second if k != "max_tokens"})
        self.assertIn("max_tokens", second)
        self.assertEqual(first.get("temperature"), second.get("temperature"))
        self.assertEqual(first["messages"], second["messages"])

    def test_a_bigger_request_that_is_refused_keeps_the_first_partial_text(self) -> None:
        # a server that will not give more room: the owner still gets the complete sentences of the first answer
        srv = _LimitServer(need=10 ** 9, refuse_above=2000)
        r = self.router_for(srv)
        res = r.complete_messages([{"role": "user", "content": "u"}], purpose="chat")
        self.assertEqual(res.content, CUT_TRIMMED)
        self.assertTrue(res.truncated)
        self.assertEqual(len(srv.requests), 2)
        # nothing was learned, so the next call starts from the profile's limit again
        r.complete_messages([{"role": "user", "content": "u2"}], purpose="chat")
        self.assertEqual(srv.limits()[2], 400)


class StillTruncatedFallsBackToTheLastCompleteSentence(_RouterCase):
    def test_chat_reply_is_cut_back_to_its_last_full_sentence(self) -> None:
        srv = _LimitServer(need=10 ** 9)
        r = self.router_for(srv)
        res = r.complete_messages([{"role": "user", "content": "u"}], purpose="chat")
        self.assertEqual(res.content, CUT_TRIMMED)
        self.assertEqual(res.message["content"], CUT_TRIMMED)  # the tool loop reads `message`, not `content`
        self.assertTrue(res.truncated)
        self.assertEqual(len(srv.requests), 2)

    def test_the_real_examples(self) -> None:
        self.assertEqual(_trim_to_last_sentence("早！你对什么过敏、最近有"), "早！")
        self.assertEqual(_trim_to_last_sentence(CUT), CUT_TRIMMED)
        wanda = "好耶，必须算我一个！我现在在院子里吹风都觉得好惬意，明天咱们带壶红茶和小甜点去公园野餐吧，刚好也能和凯多聊聊"
        self.assertEqual(_trim_to_last_sentence(wanda), "好耶，必须算我一个！")

    def test_trim_edge_cases(self) -> None:
        self.assertEqual(_trim_to_last_sentence("我没有句号只有逗号，所以原样"), "我没有句号只有逗号，所以原样")
        self.assertEqual(_trim_to_last_sentence("已经完整了。"), "已经完整了。")
        self.assertEqual(_trim_to_last_sentence("说好了……下次再"), "说好了……")
        self.assertEqual(_trim_to_last_sentence("他说：「好的。」然后我们"), "他说：「好的。」")
        self.assertEqual(_trim_to_last_sentence("Pi is 3.14 and then"), "Pi is 3.14 and then")
        self.assertEqual(_trim_to_last_sentence("Done. Then we"), "Done.")
        self.assertEqual(_trim_to_last_sentence(""), "")

    def test_nothing_to_fall_back_to_means_the_text_is_returned_as_is(self) -> None:
        srv = _LimitServer(need=10 ** 9, partial="早安，房主，我正要去厨房")
        r = self.router_for(srv)
        res = r.complete_messages([{"role": "user", "content": "u"}], purpose="chat")
        self.assertEqual(res.content, "早安，房主，我正要去厨房")
        self.assertTrue(res.truncated)

    def test_structured_purposes_are_not_cut_by_sentence(self) -> None:
        # a truncated JSON object has no "last full sentence"; cutting it at some 。 inside a string would be worse
        part = '{"action": "chat", "say": "好的。我去", "x'
        for purpose in ("life", "memory_extract"):
            with self.subTest(purpose=purpose):
                srv = _LimitServer(need=10 ** 9, partial=part)
                r = self.router_for(srv)
                res = r.complete_messages([{"role": "user", "content": "u"}], purpose=purpose)
                self.assertEqual(res.content, part)
                self.assertTrue(res.truncated)

    def test_diary_is_prose_and_is_cut_back_like_chat(self) -> None:
        srv = _LimitServer(need=10 ** 9, partial="今天很开心。晚饭吃了咖喱，然后")
        r = self.router_for(srv)
        self.assertEqual(r.complete_messages([{"role": "user", "content": "u"}], purpose="diary").content, "今天很开心。")

    def test_tool_calls_are_never_touched(self) -> None:
        tc = [{"id": "c1", "type": "function", "function": {"name": "record_promise", "arguments": '{"text": "买'}}]
        srv = _LimitServer(need=10 ** 9, partial="好的。我来记一下，", tool_calls=tc)
        r = self.router_for(srv)
        res = r.complete_messages(
            [{"role": "user", "content": "u"}], purpose="chat",
            tools=[{"name": "record_promise", "description": "d", "parameters": {"type": "object", "properties": {}}}],
        )
        self.assertEqual(res.message["content"], "好的。我来记一下，")
        self.assertEqual(res.message["tool_calls"], tc)
        self.assertEqual(len(srv.requests), 2, "tool-call answers cut by the limit also get one bigger retry")


class TruncatedReplyNeverReachesTheOwner(unittest.TestCase):
    def test_the_resident_does_not_say_half_a_sentence(self) -> None:
        half = "早啊，房主。我刚醒，正要去厨房弄点吃的。你今天也要好好吃饭，别忙起来"
        srv = _LimitServer(need=10 ** 9, partial=half)
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            tmp.name, pack_dir=PACK,
            env={"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"},
            start_ts=T0, virtual_clock=True, allow_onboarding=False,
        )
        self.addCleanup(d.close)
        res = d.converse("private:sora", "早上好", owner_speaker("房主"))
        self.assertEqual(res["replies"][0]["text"], "早啊，房主。我刚醒，正要去厨房弄点吃的。")


if __name__ == "__main__":
    unittest.main()
