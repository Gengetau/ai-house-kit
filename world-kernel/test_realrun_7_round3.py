"""Real-run batch 7 / round 3 (everyone on gemini-4-hydrogen): two defects found in the transcript.

1. Declines that the keyword list did not know ("今天大概率不出门", "暂时没打算出门", "榴莲我买不到") were recorded as an
   agreement with the owner (an errand that later expired), or got a second "买不了" sentence glued on by the daemon.
2. A reply cut by the output limit (`finish_reason=length`, partial text) was posted as the resident's words. The model's
   thinking tokens eat into `max_tokens`: the usage ledger shows completion_tokens=13 for a call whose total-minus-prompt is
   exactly the 400 limit. The router only handled "length with empty text".
"""
from __future__ import annotations

import http.server
import json
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
from worldkernel.intents_lite import reply_declines  # noqa: E402

from test_realrun_1_promises import PACK, T0, JST, _DaemonCase, _StoreCase  # noqa: E402,F401

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


class SlackerRepliesMakeNoErrand(_StoreCase):
    def test_gemini_alma_overslept_and_is_not_going_out(self) -> None:
        self.say("alma", EGG, REAL_REPLIES[8][0], group=True)
        self.assertEqual(self.rows("alma"), [])

    def test_terra_alma_gives_only_a_conditional_maybe(self) -> None:
        self.say("alma", EGG, REAL_REPLIES[11][0], group=True)
        self.assertEqual(self.rows("alma"), [])
        self.say("alma", EGG, REAL_REPLIES[10][0], group=True)
        self.assertEqual(self.rows("alma"), [])

    def test_somebody_else_who_agrees_keeps_the_errand(self) -> None:
        self.say("alma", EGG, REAL_REPLIES[8][0], group=True, party=["alma", "nell"])
        self.say("nell", EGG, "好呀，我下午去超市顺便带回来。", group=True, party=["alma", "nell"])
        self.assertEqual(self.rows("alma"), [])
        self.assertEqual([(r["item"], r["qty"]) for r in self.rows("nell")], [("egg", 2)])

    def test_real_agreements_still_become_promises(self) -> None:
        self.say("nell", "奈尔，帮我买一盒牛奶回来好吗？", REAL_REPLIES[0][0])
        self.assertEqual([(r["item"], r["state"]) for r in self.rows("nell")], [("milk", "open")])
        self.say("wanda", "婉妲，下午要不要一起喝下午茶呀？", REAL_REPLIES[16][0], group=True)
        self.assertEqual([r["kind"] for r in self.rows("wanda")], ["together"])


class UnbuyableItemGetsOneSentenceNotTwo(_DaemonCase):
    def test_a_reply_that_says_it_cannot_buy_it_gets_no_second_sentence(self) -> None:
        self.boot(REAL_REPLIES[4][0])
        text = self.talk("艾莎，能帮我买个榴莲回来吗？")
        self.assertEqual(text, REAL_REPLIES[4][0])
        self.assertNotIn("只先记个提醒", text)

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
