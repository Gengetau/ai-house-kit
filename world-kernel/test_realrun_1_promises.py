"""Real-run batch 1 (38.5 virtual hours, gpt-4o-mini): defects of the promise ("约定") loop.

Every case below is a sentence or event pair taken from that run's evidence. Each defect has fail-before/pass-after cases
and a regression case showing that the genuine promise next to it is still recorded.

1. small talk was recorded as 「和房主聊到：…」 because the keyword scan also read the resident's reply, and unrelated
   home events (a tea plan) later closed those rows as "completed";
2. a resident who declined in her reply ("没法出门买鸡蛋") still got an errand and later an expiry notice;
3. a single-character alias ("米") inside a name ("米娜") plus a "买" elsewhere in the sentence became a rice errand;
4. "买不了" was glued onto the reply after the fact, and a follow-up got "我会尽快去买的" for something unbuyable.
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
from worldkernel import vocab_life1  # noqa: E402
from worldkernel.intents_lite import CommitmentStore  # noqa: E402

T0 = dt.datetime(2026, 11, 21, 14, 0, tzinfo=JST).timestamp()
H = 3600.0
PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


class _StoreCase(unittest.TestCase):
    """A `CommitmentStore` over the real life1 vocabulary (the one the long run used)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "world.db")
        self.store = CommitmentStore(self.db, vocab=vocab_life1.load_pack())
        self.store.init()
        self._n = 0

    def say(self, actor: str, owner_text: str, reply: str, *, now: float = T0, group: bool = False,
            intents: Optional[List[Dict[str, Any]]] = None, party: Optional[List[str]] = None) -> None:
        self.store.enqueue_chat(
            actor, owner_text, reply, where="group" if group else "private", now=now,
            channel="group" if group else f"private:{actor}", intents=intents, party=party,
        )

    def rows(self, actor: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.store.list_intents(actor=actor)

    def evidence(self, body: Dict[str, Any]) -> None:
        self._n += 1
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', ?, ?, ?)",
                    (f"k{self._n}", self._n, float(body.get("ended_at") or body["at"]), json.dumps(body)),
                )


class SmallTalkIsNotAPromise(_StoreCase):
    # (resident, what the owner said, what the resident answered) -- all recorded by the long run, none is a plan
    CHATTER = [
        ("nell", "今天过得怎么样？晚饭好吃吗？", "今天过得很平静，晚饭的咖喱饭味道不错哦！"),
        ("yuki", "好无聊啊，你们平时都喜欢干嘛？", "我喜欢在院子里看星星，或者一起去河岸散步。"),
        ("wanda", "好无聊啊，你们平时都喜欢干嘛？", "不如来一场游戏大战吧！或者我们可以一起喝下午茶。"),
        ("nell", "奈尔，说说你最近有什么开心的事，还有你最喜欢的一样东西吧。", "和大家一起做饭的时光总是很愉快。"),
        ("kai", "凯，欢迎搬进来！介绍一下你自己吧。", "我喜欢户外、登山和摄影。如果你有兴趣，咱们可以一起去！"),
        ("elsa", "大家知道我最近有什么打算吗？", "我想，你的打算可能与我们一起享受下午茶有关吧。"),
        ("nell", "今天晚饭大家想吃什么？", "我今天准备了咖喱饭，如果大家有其他想吃的，我也很乐意一起讨论。"),
        ("nell", "中午好，午饭吃了吗？", "我今天中午吃了咖喱饭，味道不错哦。"),
        ("kai", "快到晚上十点了，大家今天辛苦了。", "今天真不错。明天一起去公园玩吧！"),
        ("wanda", "这两天大家辛苦了，能说说这两天最开心的一件事吗？", "最开心的事就是和大家一起分享下午茶的甜点。"),
        ("nell", "奈尔，咖喱饭好吃吗？你今天都做了什么？", "咖喱饭很好吃，今天我在厨房整理了一下。"),
        ("mina", "米娜，昨天说好的逛街，后来去了吗？", "我昨天没去，下次我们一起去吧！"),
    ]

    def test_chatter_records_nothing(self) -> None:
        for actor, said, reply in self.CHATTER:
            with self.subTest(said=said):
                self.setUp()
                self.say(actor, said, reply)
                self.assertEqual(self.rows(actor), [], [r["summary"] for r in self.rows(actor)])

    def test_keyword_only_in_the_reply_records_nothing(self) -> None:
        for kw in ("一起", "咖喱", "喝茶", "做饭", "散步", "出门"):
            with self.subTest(kw=kw):
                self.say("nell", "你今天心情怎么样", f"挺好的，{kw}真开心")
                self.assertEqual(self.rows("nell"), [])

    def test_real_invitations_and_requests_are_still_recorded(self) -> None:
        cases = [
            ("nell", "奈尔，今晚的晚饭能做咖喱饭吗？", "好的，房主，我会做咖喱饭的！"),
            ("wanda", "婉妲，下午要不要一起喝下午茶呀？", "当然好呀！我已经准备好了甜点和茶。"),
            ("yuki", "明天下午我们一起去公园玩好不好？", "听起来很不错呢，公园的阳光和微风会让人心情愉悦。"),
            ("nell", "你今晚早点睡哦", "好的，我今晚早点睡"),
            ("mina", "明天一起去散步吧", "好呀"),
        ]
        for actor, said, reply in cases:
            with self.subTest(said=said):
                self.setUp()
                self.say(actor, said, reply)
                rows = self.rows(actor)
                self.assertEqual(len(rows), 1, said)
                self.assertTrue(rows[0]["summary"].startswith("和房主聊到"))
                self.assertEqual(rows[0]["state"], "open")


class UnrelatedHomeEventsDoNotCloseOutings(_StoreCase):
    TEA_PLAN_DONE = {"kind": "plan_finished", "actor": "wanda", "plan_id": "plan-388b6e7163a3", "place": "living",
                     "outcome": "done", "members": ["wanda", "mina"]}

    def test_tea_at_home_does_not_close_an_outing(self) -> None:
        for said in ("明天下午我们一起去公园玩好不好？", "婉妲，今天下午一起去街上逛逛怎么样？", "下午出门走走吧"):
            with self.subTest(said=said):
                self.setUp()
                self.say("wanda", said, "好呀，一起去吧")
                self.assertEqual([r["kind"] for r in self.rows("wanda")], ["outing"])
                self.evidence({**self.TEA_PLAN_DONE, "at": T0 + 2 * H})
                self.store.sweep(T0 + 3 * H)
                self.assertEqual(self.rows("wanda")[0]["state"], "open")

    def test_shopping_trip_does_not_close_a_park_outing(self) -> None:
        self.say("wanda", "明天下午我们一起去公园玩好不好？", "好呀")
        self.evidence({"kind": "plan_finished", "actor": "wanda", "plan_id": "shopping-plan-1",
                       "place": "place:supermarket", "outcome": "done", "members": ["wanda"], "at": T0 + 2 * H})
        self.store.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "open")

    def test_arriving_at_the_named_place_still_closes(self) -> None:
        self.say("wanda", "明天下午我们一起去公园玩好不好？", "好呀")
        self.evidence({"kind": "arrived", "actor": "wanda", "place": "place:park", "at": T0 + 2 * H})
        self.store.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "completed")

    def test_finished_plan_away_from_home_still_closes_an_unnamed_outing(self) -> None:
        self.say("wanda", "下午出门走走吧", "好呀")
        self.evidence({"kind": "plan_finished", "actor": "wanda", "plan_id": "plan-x", "place": "place:takaosan",
                       "outcome": "done", "members": ["wanda", "kai"], "at": T0 + 2 * H})
        self.store.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "completed")

    def test_together_tea_is_not_closed_by_a_different_activity_but_by_tea(self) -> None:
        self.say("wanda", "婉妲，下午要不要一起喝下午茶呀？", "当然好呀")
        self.assertEqual(self.rows("wanda")[0]["kind"], "together")
        self.evidence({"kind": "invite_settled", "actor": "wanda", "plan_id": "plan-game", "activity": "gaming",
                       "place": "living", "members": ["wanda", "mina"], "at": T0 + H})
        self.evidence({"kind": "plan_finished", "actor": "wanda", "plan_id": "plan-game", "place": "living",
                       "outcome": "done", "members": ["wanda", "mina"], "at": T0 + 2 * H})
        self.store.sweep(T0 + 3 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "open")
        self.evidence({"kind": "invite_settled", "actor": "wanda", "plan_id": "plan-tea", "activity": "tea_party",
                       "place": "living", "members": ["wanda", "mina"], "at": T0 + 3 * H})
        self.evidence({"kind": "plan_finished", "actor": "wanda", "plan_id": "plan-tea", "place": "living",
                       "outcome": "done", "members": ["wanda", "mina"], "at": T0 + 4 * H})
        self.store.sweep(T0 + 5 * H)
        self.assertEqual(self.rows("wanda")[0]["state"], "completed")


class DecliningReplyIsNotAnAgreement(_StoreCase):
    EGG = "谁方便的话，顺路帮我买两个鸡蛋回来吧，拜托啦"

    def test_resident_who_refused_gets_no_errand(self) -> None:
        self.say("alma", self.EGG, "我现在在家发呆，没法出门买鸡蛋。要不我等会儿去厨房看看有什么食材？", group=True)
        self.assertEqual(self.rows("alma"), [])

    def test_resident_who_agreed_to_the_same_sentence_keeps_hers(self) -> None:
        self.say("alma", self.EGG, "我现在在家发呆，没法出门买鸡蛋。", group=True, party=["alma", "nell"])
        self.say("nell", self.EGG, "好呀，我下午去超市顺便带回来。", group=True, party=["alma", "nell"])
        self.assertEqual(self.rows("alma"), [])
        eggs = self.rows("nell")
        self.assertEqual([(r["kind"], r["item"], r["qty"]) for r in eggs], [("errand", "egg", 2)])

    def test_refusal_also_blocks_the_llm_tool_intent_and_other_plans(self) -> None:
        self.say("alma", "帮我买两个鸡蛋", "抱歉，我现在去不了。", intents=[
            {"text": "买鸡蛋", "kind": "errand", "item": "egg", "qty": 2, "答应了的": ["alma"]}])
        self.assertEqual(self.rows("alma"), [])
        self.say("mina", "米娜，明天一起去散步吧", "我明天上午有点事，能不能改成下午？")
        self.assertEqual(self.rows("mina"), [])
        self.say("nell", "奈尔，今晚的晚饭能做咖喱饭吗？", "不好意思，今晚我没办法做饭。")
        self.assertEqual(self.rows("nell"), [])

    def test_agreement_with_a_caveat_is_still_an_agreement(self) -> None:
        self.say("nell", "帮我买一盒牛奶回来好吗？", "好的，我会买的，不过现在还没法出门，下午去。")
        self.assertEqual([(r["item"], r["state"]) for r in self.rows("nell")], [("milk", "open")])

    def test_diet_memo_is_not_a_promise_and_survives_a_refusing_tone(self) -> None:
        self.say("nell", "我对花生过敏，你记一下哦。", "抱歉，我没法保证每次都想起来，但我会尽量。")
        self.assertEqual([r["kind"] for r in self.rows("nell")], ["memo"])


class NamesAndNounsAreNotIngredients(_StoreCase):
    def test_mina_is_not_rice(self) -> None:
        self.say("mina", "米娜，明天上午陪我去逛街买衣服，可以吗？", "好呀，我陪你去")
        self.assertEqual([r for r in self.rows("mina") if r["item"] == "rice"], [])
        self.assertEqual([r for r in self.rows("mina") if r["kind"] == "errand"], [])

    def test_single_char_alias_needs_to_be_the_thing_bought(self) -> None:
        none_of_these = [
            "米娜，明天上午陪我去逛街买衣服，可以吗？",
            "帮米娜买点东西",
            "买鱼缸",
            "今天去买鱼香肉丝的材料之前先看看米价",
            "你今天买衣服了吗，米娜说你去了",
        ]
        for text in none_of_these:
            with self.subTest(text=text):
                item, _ = self.store._detect_errand_item(text)
                self.assertNotIn(item, ("rice", "fish"))

    def test_buying_rice_or_fish_still_works(self) -> None:
        for text, item in (("帮我买点米回来", "rice"), ("买米吧", "rice"), ("记得买两袋米", "rice"),
                           ("缺米了，补点", "rice"), ("帮我买条鱼", "fish"), ("路过超市买鱼回来", "fish"),
                           ("帮我买大米", "rice")):
            with self.subTest(text=text):
                self.assertEqual(self.store._detect_errand_item(text)[0], item)

    def test_resident_names_are_not_matched_even_for_longer_aliases(self) -> None:
        names = vocab_life1.load_pack().NAMES
        self.assertIn("米娜", names.values())
        item, _ = self.store._detect_errand_item("米娜帮我买点东西")
        self.assertIsNone(item)


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


class ResidentKnowsItCannotBuyBeforeAnswering(_DaemonCase):
    def test_the_prompt_says_the_item_cannot_be_bought(self) -> None:
        srv = self.boot("好的，我再想想")
        self.talk("能帮我买个榴莲回来吗？")
        prompt = srv.systems[-1]
        self.assertIn("榴莲", prompt)
        self.assertIn("买不了", prompt)

    def test_a_purchasable_item_gets_no_such_hint(self) -> None:
        srv = self.boot("好的")
        self.talk("能帮我买两盒牛奶回来吗？")
        self.assertNotIn("买不了", srv.systems[-1])

    def test_already_declining_reply_is_not_given_a_second_contradicting_tail(self) -> None:
        self.boot("榴莲我买不了，只能先记个提醒。")
        text = self.talk("能帮我买个榴莲回来吗？")
        self.assertEqual(text.count("买不了"), 1, text)

    def test_followup_after_expiry_tells_her_it_expired_and_was_never_buyable(self) -> None:
        srv = self.boot("好的，我再想想")
        self.talk("能帮我买个榴莲回来吗？")
        d = self.d
        d.clock.advance(25 * H)
        d.intents.sweep(d.clock.now())
        self.assertEqual([r["state"] for r in d.intents.list_intents(actor="sora") if r["raw_item"] == "榴莲"], ["expired"])
        self.talk("榴莲的事怎么样了？")
        prompt = srv.systems[-1]
        self.assertIn("榴莲", prompt)
        self.assertIn("过期", prompt)
        self.assertIn("买不了", prompt)

    def test_followup_about_an_expired_catalog_errand_says_it_was_not_bought(self) -> None:
        srv = self.boot("好的")
        self.d.intents.enqueue_chat("sora", "帮我买两个鸡蛋回来", "好呀", where="private", now=self.d.clock.now())
        self.d.clock.advance(25 * H)
        self.d.intents.sweep(self.d.clock.now())
        self.talk("鸡蛋买了吗？")
        prompt = srv.systems[-1]
        self.assertIn("鸡蛋", prompt)
        self.assertIn("过期", prompt)
        self.assertIn("没买成", prompt)

    def test_unrelated_chat_gets_no_promise_hint(self) -> None:
        srv = self.boot("好的")
        self.talk("能帮我买个榴莲回来吗？")
        self.talk("今天天气怎么样？")
        self.assertNotIn("买不了", srv.systems[-1])
        self.assertNotIn("榴莲", srv.systems[-1])

    def test_expiry_notice_for_a_reminder_does_not_claim_a_promise_to_buy(self) -> None:
        self.boot("好的，我再想想")
        self.talk("能帮我买个榴莲回来吗？")
        d = self.d
        d.clock.advance(25 * H)
        d.intents.sweep(d.clock.now())
        d._run_post_step_jobs()
        notes = [m for m in d.list_chat("private:sora", limit=80) if "过期" in m["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("榴莲", notes[0]["text"])
        self.assertNotIn("答应你买", notes[0]["text"])


if __name__ == "__main__":
    unittest.main()
