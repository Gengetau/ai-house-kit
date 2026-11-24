"""Real-model re-run 6: the nightly diary when the model endpoint fails now and then.

Evidence (gemini house): 5 of 8 diary calls failed (HTTP_502/503), the diary timeout (25s) sat right at the average
latency (23s), and after one failure the diary was written from the template at once. In the all-gemini house three
diaries failing in a row opened the circuit breaker (shared with chat), and the next six diaries failed in 0 ms with
CIRCUIT_OPEN and were all written from the template.

1. with a real model configured, a failure keeps the diary for a later tick (at most RETRY_MAX_TRIES real tries, inside
   the night); only then the template, with one log line. No model: template at once, as before.
2. a CIRCUIT_OPEN "failure" never reached the endpoint: it is not one of the tries, and does not push the diary to the
   template; diary failures do not feed the circuit breaker that chat shares.
3. the template reads like a diary: the whole day, first person, whole sentences.
4. the diary timeout is 60s and no outer limit cuts it back.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import diary as diary_mod
from ai_house import llm as llm_mod
from ai_house.daemon import HouseDaemon
from ai_house.diary import DiaryService, finish_diary_text
from ai_house.llm import LLMAPIError, ModelRouter

JST = dt.timezone(dt.timedelta(hours=9))
NAMES = {"nell": "奈尔", "kai": "凯", "elsa": "艾莎", "yuki": "由纪", "sora": "空", "rin": "凛", "mio": "澪"}
DAY = "2026-11-21"
GOOD = "今天上午在厨房忙了一阵，中午和大家一起吃了饭，晚上泡了汤，心里特别踏实，准备睡觉了。晚安。"  # a finished diary


def at(h: int, m: int = 0, day: int = 21) -> float:
    return dt.datetime(2026, 11, day, h, m, tzinfo=JST).timestamp()


def make_world(tmp: str, actors: List[str], facts: Optional[List[Tuple[int, int, str]]] = None) -> Tuple[str, str]:
    """A world.db with a `script` table holding the day's kernel lines for every actor; returns (life_db, world_db)."""
    world = str(Path(tmp) / "world.db")
    c = sqlite3.connect(world)
    c.execute(
        "create table script(id integer primary key, ts real not null, actors text not null, scene integer, "
        "text text not null, source text not null, created real not null, knowers text)"
    )
    rows = facts or [(9, 0, "{n}吃了早饭"), (12, 10, "{n}吃了咖喱饭"), (21, 0, "{n}泡了汤")]
    for a in actors:
        for h, m, t in rows:
            c.execute(
                "insert into script(ts, actors, text, source, created) values(?,?,?,?,?)",
                (at(h, m), json.dumps([a]), t.format(n=NAMES[a]), "kernel", at(h, m)),
            )
    c.commit()
    c.close()
    return str(Path(tmp) / "no-life.db"), world


def make_service(tmp: str, actors: List[str], facts: Optional[List[Tuple[int, int, str]]] = None) -> DiaryService:
    life, world = make_world(tmp, actors, facts)
    vocab = SimpleNamespace(NAMES=NAMES, SELVES={}, ROSTER=list(actors))
    return DiaryService(life, world, vocab)


def rows_of(svc: DiaryService) -> Dict[str, Dict[str, Any]]:
    return {r["actor"]: r for r in svc.list_diaries(limit=50)}


class _Model:
    """A diary model that fails the way the endpoint did; `plan` is consumed one call at a time per actor."""

    def __init__(self, plan: Optional[Dict[str, List[Any]]] = None, default: Any = GOOD) -> None:
        self.plan = {k: list(v) for k, v in (plan or {}).items()}
        self.default = default
        self.calls: List[str] = []

    def __call__(self, system: str, user: str, *, actor: str = "") -> Tuple[str, int]:
        self.calls.append(actor)
        queue = self.plan.get(actor)
        step = queue.pop(0) if queue else self.default
        if isinstance(step, BaseException):
            raise step
        return step, 0


def e502() -> LLMAPIError:
    return LLMAPIError(502, "Bad Gateway sk-should-never-be-logged-123456")


def circuit_open() -> LLMAPIError:
    return LLMAPIError(503, "Endpoint circuit breaker open", detail="circuit_open")


class _Log:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(str(msg))


class _Base(unittest.TestCase):
    def service(self, actors: List[str], facts: Optional[List[Tuple[int, int, str]]] = None) -> DiaryService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return make_service(tmp.name, actors, facts)


# ---------------------------------------------------------------------------------------------------------------
# 1. one failure must not turn the diary into the template
# ---------------------------------------------------------------------------------------------------------------
class RetryBeforeTemplate(_Base):
    def test_a_failure_is_not_written_as_a_template_and_the_next_tick_writes_the_real_diary(self) -> None:
        svc = self.service(["nell"])
        model, log = _Model({"nell": [e502()]}), _Log()
        t0 = at(22, 40)
        self.assertEqual(svc.run_due(t0, model, log=log), [])
        self.assertEqual(rows_of(svc), {}, "a failed call must leave the diary for later, not write the template")
        self.assertTrue(any("nell" in x and "稍后" in x for x in log.lines), log.lines)
        saved = svc.run_due(t0 + diary_mod.RETRY_DELAY_S + 1, model, log=log)
        self.assertEqual([s["mode"] for s in saved], ["live_api"])
        self.assertEqual(rows_of(svc)["nell"]["mode"], "live_api")
        self.assertEqual(model.calls, ["nell", "nell"])

    def test_it_waits_for_the_next_scheduled_try_instead_of_hammering_the_model(self) -> None:
        svc = self.service(["nell"])
        model = _Model({"nell": [e502()]})
        t0 = at(22, 40)
        svc.run_due(t0, model)
        svc.run_due(t0 + 5, model)
        svc.run_due(t0 + diary_mod.RETRY_DELAY_S - 1, model)
        self.assertEqual(len(model.calls), 1)

    def test_all_tries_failing_ends_in_the_template_with_one_log_line_giving_the_reason(self) -> None:
        svc = self.service(["nell"])
        model, log = _Model({"nell": [e502()] * 10}), _Log()
        t = at(22, 40)
        for _ in range(diary_mod.RETRY_MAX_TRIES):
            self.assertEqual(rows_of(svc), {})
            svc.run_due(t, model, log=log)
            t += diary_mod.RETRY_DELAY_S + 1
        self.assertEqual(len(model.calls), diary_mod.RETRY_MAX_TRIES)
        row = rows_of(svc)["nell"]
        self.assertEqual(row["mode"], "demo")
        fell = [x for x in log.lines if "模板" in x]
        self.assertEqual(len(fell), 1, log.lines)
        self.assertIn("HTTP_502", fell[0])
        self.assertIn("nell", fell[0])
        self.assertNotIn("sk-should-never", "\n".join(log.lines), "the error text may hold a key; log only the code")
        svc.run_due(t + 10 * 3600, model, log=log)  # nothing more to do, nothing more tried
        self.assertEqual(len(model.calls), diary_mod.RETRY_MAX_TRIES)

    def test_a_late_failure_goes_straight_to_the_template_when_the_night_is_nearly_over(self) -> None:
        svc = self.service(["nell"])
        model, log = _Model({"nell": [e502()] * 5}), _Log()
        now = at(2, 25, day=22)  # 02:25: the next try would be after the diary window (closes 03:00)
        svc.run_due(now, model, log=log)
        self.assertEqual(rows_of(svc)["nell"]["mode"], "demo")
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(any("模板" in x for x in log.lines))

    def test_the_retry_window_ends_before_the_diary_window(self) -> None:
        cutoff = dt.datetime(2026, 11, 22, *diary_mod.RETRY_UNTIL, tzinfo=JST)
        self.assertLess(cutoff.hour * 60 + cutoff.minute, diary_mod.LATE_UNTIL_H * 60)

    def test_without_a_model_the_template_is_written_at_once_as_before(self) -> None:
        svc = self.service(["nell"])
        log = _Log()
        saved = svc.run_due(at(22, 40), None, log=log)
        self.assertEqual([s["mode"] for s in saved], ["demo"])
        self.assertEqual(rows_of(svc)["nell"]["mode"], "demo")
        self.assertEqual([x for x in log.lines if "稍后" in x or "模板" in x], [])

    def test_a_reply_that_is_too_short_is_not_an_interface_failure(self) -> None:
        # unchanged behaviour: the model answered, the answer is just unusable -> template now
        svc = self.service(["nell"])
        model = _Model(default="好。")
        svc.run_due(at(22, 40), model)
        self.assertEqual(rows_of(svc)["nell"]["mode"], "demo")
        self.assertEqual(len(model.calls), 1)

    def test_a_diary_is_written_once_however_many_ticks_follow(self) -> None:
        svc = self.service(["nell"])
        model = _Model({"nell": [e502()]})
        t = at(22, 40)
        svc.run_due(t, model)
        for i in range(1, 6):
            svc.run_due(t + i * (diary_mod.RETRY_DELAY_S + 1), model)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(svc.list_diaries(limit=50)), 1)

    def test_memory_gets_the_diary_once(self) -> None:
        from ai_house.memory import MemoryStore

        svc = self.service(["nell"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mem = MemoryStore(tmp.name, names=NAMES)
        self.addCleanup(mem.close)
        svc.memory = mem
        model = _Model({"nell": [e502()]})
        t = at(22, 40)
        for i in range(4):
            svc.run_due(t + i * (diary_mod.RETRY_DELAY_S + 1), model)
        self.assertEqual(len(mem.list_entries("nell", "world", kind="diary")), 1)

    def test_one_residents_failure_does_not_hold_up_the_others(self) -> None:
        svc = self.service(["nell", "kai", "elsa"])
        model = _Model({"kai": [e502()]})
        saved = svc.run_due(at(22, 40), model)
        self.assertEqual(sorted(s["actor"] for s in saved), ["elsa", "nell"])
        self.assertEqual(rows_of(svc)["nell"]["mode"], "live_api")
        self.assertNotIn("kai", rows_of(svc))
        later = svc.run_due(at(22, 40) + diary_mod.RETRY_DELAY_S + 1, model)
        self.assertEqual([s["actor"] for s in later], ["kai"])
        self.assertEqual(rows_of(svc)["kai"]["mode"], "live_api")
        self.assertEqual(model.calls.count("nell"), 1)

    def test_an_unexpected_error_for_one_resident_does_not_stop_the_loop(self) -> None:
        svc = self.service(["nell", "kai"])
        model = _Model({"nell": [ValueError("boom")]})
        saved = svc.run_due(at(22, 40), model)
        self.assertEqual([s["actor"] for s in saved], ["kai"])

    def test_on_saved_is_called_for_each_new_diary_only(self) -> None:
        svc = self.service(["nell", "kai"])
        model = _Model({"kai": [e502()]})
        seen: List[str] = []
        svc.run_due(at(22, 40), model, on_saved=lambda saved, job: seen.append(saved["actor"]))
        svc.run_due(at(22, 41), model, on_saved=lambda saved, job: seen.append(saved["actor"]))
        self.assertEqual(seen, ["nell"])

    def test_generate_text_still_falls_back_at_once_for_direct_callers(self) -> None:
        svc = self.service(["nell"])
        job = {"day": DAY, "actor": "nell", "facts": ["09:00 奈尔吃了早饭"]}
        text, mode = svc.generate_text(job, llm_fn=_Model({"nell": [e502()]}))
        self.assertEqual(mode, "demo")


# ---------------------------------------------------------------------------------------------------------------
# 2. circuit breaker: "open" failures neither use up the tries nor go to the template
# ---------------------------------------------------------------------------------------------------------------
class CircuitOpenIsNotATry(_Base):
    def test_circuit_open_does_not_use_up_a_try_and_is_not_written_as_a_template(self) -> None:
        svc = self.service(["nell"])
        model = _Model({"nell": [circuit_open()] * 6 + [e502()]})
        t = at(22, 40)
        for _ in range(6):  # more than RETRY_MAX_TRIES blocked answers
            svc.run_due(t, model)
            self.assertEqual(rows_of(svc), {})
            t += diary_mod.BREAKER_WAIT_S + 1
        svc.run_due(t, model)  # a real failure: try 1 of 3
        self.assertEqual(rows_of(svc), {})
        t += diary_mod.RETRY_DELAY_S + 1
        saved = svc.run_due(t, model)  # the endpoint is back
        self.assertEqual([s["mode"] for s in saved], ["live_api"])

    def test_a_blocked_diary_waits_for_the_breaker_to_close(self) -> None:
        svc = self.service(["nell"])
        model = _Model({"nell": [circuit_open()]})
        t = at(22, 40)
        svc.run_due(t, model)
        svc.run_due(t + 5, model)
        self.assertEqual(len(model.calls), 1)
        self.assertGreaterEqual(diary_mod.BREAKER_WAIT_S, llm_mod.BREAKER_OPEN_S)
        svc.run_due(t + diary_mod.BREAKER_WAIT_S + 1, model)
        self.assertEqual(rows_of(svc)["nell"]["mode"], "live_api")

    def test_blocked_until_the_night_is_over_still_ends_in_the_template(self) -> None:
        svc = self.service(["nell"])
        log = _Log()
        model = _Model({"nell": [circuit_open()] * 3})
        svc.run_due(at(2, 30, day=22), model, log=log)  # nothing left of the night to wait in
        self.assertEqual(rows_of(svc)["nell"]["mode"], "demo")
        self.assertTrue(any("模板" in x and "circuit_open" in x for x in log.lines), log.lines)

    def test_first_residents_fail_and_open_the_breaker_the_later_ones_still_get_a_real_diary(self) -> None:
        """The all-gemini night: 7 diaries in a row, the first ones fail 502 and open the breaker (3 failures, 30s),
        the rest are refused by the breaker. Nobody may end up with the template; all get real diaries later."""
        actors = ["nell", "kai", "elsa", "yuki", "sora", "rin", "mio"]
        svc = self.service(actors)
        state = {"fails": 0, "open_until": 0.0, "now": 0.0, "down": True}

        def model(system: str, user: str, *, actor: str = "") -> Tuple[str, int]:
            now = state["now"]
            if state["open_until"] > now:
                raise circuit_open()
            if state["down"]:
                state["fails"] += 1
                if state["fails"] >= llm_mod.BREAKER_THRESHOLD:
                    state.update(fails=0, open_until=now + llm_mod.BREAKER_OPEN_S)
                raise e502()
            return GOOD, 0

        t = at(22, 40)
        state["now"] = t
        svc.run_due(t, model)
        self.assertEqual(rows_of(svc), {})  # nobody got the template
        state["down"] = False  # the endpoint recovers
        t += diary_mod.BREAKER_WAIT_S + 1
        state["now"] = t
        svc.run_due(t, model)
        t += diary_mod.RETRY_DELAY_S + 1
        state["now"] = t
        svc.run_due(t, model)
        got = rows_of(svc)
        self.assertEqual(sorted(got), sorted(actors))
        self.assertEqual({r["mode"] for r in got.values()}, {"live_api"})


# ---------------------------------------------------------------------------------------------------------------
# 2b. the router: diary failures do not open the breaker chat shares
# ---------------------------------------------------------------------------------------------------------------
class RouterBreaker(unittest.TestCase):
    def router(self) -> ModelRouter:
        r = ModelRouter({"AI_HOUSE_API_KEY": "sk-test-aaaaaaaaaa", "AI_HOUSE_BASE_URL": "http://127.0.0.1:9/v1",
                         "AI_HOUSE_MODEL": "m"}, retry_delay_s=0.0)
        self.addCleanup(r.close)
        self.sent: List[str] = []
        self.fail = True
        return r

    def patch(self, r: ModelRouter) -> None:
        def attempt(ep: Any, payload: Any, timeout: float, deadline: Any, **kw: Any) -> Dict[str, Any]:
            self.sent.append("x")
            if self.fail:
                raise LLMAPIError(502, "Bad Gateway")
            return {"choices": [{"message": {"content": "好的"}}], "usage": {}}

        r._attempt_once = attempt  # type: ignore[method-assign]

    def test_repeated_diary_failures_do_not_open_the_breaker_for_chat(self) -> None:
        r = self.router()
        self.patch(r)
        for _ in range(4):
            with self.assertRaises(LLMAPIError) as cm:
                r.complete_text("s", "u", purpose="diary", actor="nell")
            self.assertNotEqual(cm.exception.detail, "circuit_open")
        self.assertEqual(len(self.sent), 8, "each diary call tried twice (1 retry) and none was refused by the breaker")
        self.fail = False
        text, _ = r.complete_text("s", "u", purpose="chat", actor="nell")
        self.assertEqual(text, "好的")

    def test_chat_failures_still_open_the_breaker_and_the_diary_sees_it_as_circuit_open(self) -> None:
        r = self.router()
        self.patch(r)
        for _ in range(3):
            with self.assertRaises(LLMAPIError):
                r.complete_text("s", "u", purpose="chat", actor="nell")
        before = len(self.sent)
        with self.assertRaises(LLMAPIError) as cm:
            r.complete_text("s", "u", purpose="diary", actor="nell")
        self.assertEqual(cm.exception.detail, "circuit_open")
        self.assertEqual(len(self.sent), before, "the endpoint was not touched")


# ---------------------------------------------------------------------------------------------------------------
# 3. the template reads like a diary
# ---------------------------------------------------------------------------------------------------------------
class TemplateDiary(_Base):
    FACTS = [
        "05:30 婉妲在自己房间换上了格子睡衣套装（睡觉）",
        "05:40 婉妲在自己房间睡了 30 分钟",
        "07:10 婉妲从自己房间去了厨房兼餐厅",
        "07:20 婉妲吃了早饭（奈尔做的）",
        "09:00 婉妲读了 40 分钟的书",
        "10:15 婉妲整理了客厅",
        "12:11 婉妲吃了咖喱饭（奈尔做的）",
        "13:30 婉妲洗了衣服",
        "15:00 婉妲和奈尔一起看电影",
        "16:20 婉妲去了超市",
        "18:31 婉妲做了咖喱饭",
        "19:40 婉妲泡了汤",
        "21:30 婉妲换上了睡衣",
        "22:10 婉妲回到自己房间睡觉了",
    ]

    def demo(self, facts: Optional[List[str]] = None, name: str = "婉妲") -> str:
        names = dict(NAMES, wanda=name)
        vocab = SimpleNamespace(NAMES=names, SELVES={}, ROSTER=["wanda"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = DiaryService(str(Path(tmp.name) / "no-life.db"), str(Path(tmp.name) / "world.db"), vocab)
        text, mode = svc.generate_text({"day": DAY, "actor": "wanda", "facts": facts or self.FACTS}, llm_fn=None)
        self.assertEqual(mode, "demo")
        return text

    def test_it_covers_the_day_not_just_the_first_four_lines(self) -> None:
        text = self.demo()
        late = [k for k in ("咖喱饭", "泡了汤", "睡觉了", "看电影", "超市") if k in text]
        self.assertGreaterEqual(len(late), 2, text)
        self.assertIn("早", text)
        self.assertTrue(any(k in text for k in ("泡了汤", "睡觉了", "做了咖喱饭")), text)  # something from the evening
        picked = [f for f in self.FACTS if f[6:] in text or f[6:].replace("婉妲", "我") in text]
        self.assertGreaterEqual(len(picked), 5, text)
        self.assertLessEqual(len(picked), 6, text)

    def test_it_is_first_person_and_does_not_call_itself_by_name(self) -> None:
        text = self.demo()
        self.assertNotIn("婉妲", text)
        self.assertIn("我", text)

    def test_other_peoples_names_stay(self) -> None:
        text = self.demo(["09:00 婉妲和奈尔一起看电影", "12:00 婉妲吃了咖喱饭（奈尔做的）", "20:00 婉妲泡了汤"])
        self.assertIn("奈尔", text)
        self.assertNotIn("婉妲", text)

    def test_it_ends_in_a_whole_sentence_and_goes_through_finish_diary_text(self) -> None:
        text = self.demo()
        self.assertTrue(text.endswith("。"), text[-12:])
        self.assertEqual(finish_diary_text(text, diary_mod.DEMO_MAX_CHARS), text)
        self.assertLessEqual(len(text), diary_mod.DEMO_MAX_CHARS)
        self.assertTrue(text.startswith(f"{DAY} 日记："))

    def test_long_facts_still_fit_and_end_whole(self) -> None:
        facts = [f"{8 + i:02d}:00 婉妲在厨房兼餐厅里和奈尔、由纪一起做了一道要花很长时间的特别复杂的菜肴" for i in range(12)]
        text = self.demo(facts)
        self.assertLessEqual(len(text), diary_mod.DEMO_MAX_CHARS)
        self.assertTrue(text.endswith("。"), text[-12:])
        self.assertGreaterEqual(text.count("我"), 3)

    def test_a_day_that_only_reached_the_morning_does_not_pretend_to_be_over(self) -> None:
        text = self.demo(["07:10 婉妲吃了早饭", "09:00 婉妲读了书", "10:30 婉妲整理了客厅"])
        self.assertNotIn("忙完这一天", text)
        self.assertNotIn("准备早点休息", text)
        self.assertTrue(text.endswith("。"))

    def test_a_full_day_may_say_it_is_time_to_rest(self) -> None:
        text = self.demo()
        self.assertIn("休息", text)

    def test_few_facts_are_all_kept(self) -> None:
        text = self.demo(["09:00 婉妲吃了早饭", "20:00 婉妲泡了汤"])
        self.assertIn("早饭", text)
        self.assertIn("泡了汤", text)

    def test_a_fact_that_does_not_start_with_the_name_is_kept_as_is(self) -> None:
        text = self.demo(["09:00 奈尔给婉妲倒了茶", "20:00 婉妲泡了汤"])
        self.assertIn("奈尔给", text)


# ---------------------------------------------------------------------------------------------------------------
# 4. the diary timeout
# ---------------------------------------------------------------------------------------------------------------
class DiaryTimeout(unittest.TestCase):
    def test_diary_profile_timeout_is_60_and_nothing_else_moved(self) -> None:
        temperature, max_tokens, timeout, retries = llm_mod.DEFAULT_TASK_PROFILES["diary"]
        self.assertEqual((temperature, max_tokens, timeout, retries), (0.7, 800, 60.0, 1))

    def test_other_purposes_keep_their_timeouts(self) -> None:
        got = {k: v[2] for k, v in llm_mod.DEFAULT_TASK_PROFILES.items()}
        self.assertEqual(got, {"chat": 25.0, "life": 25.0, "memory_extract": 45.0, "diary": 60.0, "ping": 12.0})

    def test_no_outer_limit_cuts_the_single_request_back_below_the_timeout(self) -> None:
        r = ModelRouter({"AI_HOUSE_API_KEY": "sk-test-aaaaaaaaaa"})
        self.addCleanup(r.close)
        self.assertEqual(r.resolve_profile("diary").timeout_s, 60.0)
        self.assertGreaterEqual(r.resolve_total_budget("diary"), r.resolve_profile("diary").timeout_s)

    def test_a_slow_diary_reply_gets_the_whole_60_seconds(self) -> None:
        r = ModelRouter({"AI_HOUSE_API_KEY": "sk-test-aaaaaaaaaa", "AI_HOUSE_BASE_URL": "http://127.0.0.1:9/v1"})
        self.addCleanup(r.close)
        seen: List[float] = []

        def fake_send(url: str, key: str, payload: Any, timeout_s: float, deadline: Any, **kw: Any) -> Dict[str, Any]:
            seen.append(llm_mod._request_timeout(timeout_s, deadline))
            return {"choices": [{"message": {"content": "好"}}], "usage": {}}

        old = llm_mod._send_with_length_retry
        llm_mod._send_with_length_retry = fake_send  # type: ignore[assignment]
        self.addCleanup(setattr, llm_mod, "_send_with_length_retry", old)
        r.complete_text("s", "u", purpose="diary", actor="nell")
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 59.0, seen)


# ---------------------------------------------------------------------------------------------------------------
# 5. the daemon drives it (virtual clock)
# ---------------------------------------------------------------------------------------------------------------
class DaemonRetry(unittest.TestCase):
    def test_the_daemon_retries_a_failed_diary_on_a_later_tick(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls: List[str] = []
        state = {"fail": True}

        def llm(system: str, user: str, **kw: Any) -> Tuple[str, int]:
            if "睡前小日记" in system:
                calls.append(system)
                if state["fail"]:
                    raise e502()
                return GOOD, 5
            return "今天客厅茶香很暖，大家各自忙得开心。", 5

        t0 = at(17, 50, day=23)
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": "rr6-owner-token-01", "AI_HOUSE_QUIET_HOURS": "off"},
            llm_fn=llm,
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        d.clock.advance(at(18, 0, day=23) - d.clock.now())
        d.fast_forward(6)
        d.clock.advance(at(22, 40, day=23) - d.clock.now())
        d.fast_forward(1)
        self.assertTrue(calls, "the diary was due and the model was asked")
        self.assertEqual(d.diary.list_diaries(limit=20), [], "a failed call must not write the template")
        state["fail"] = False
        d.fast_forward(diary_mod.RETRY_DELAY_S // 60 + 2)
        rows = d.diary.list_diaries(limit=20)
        self.assertTrue(rows)
        self.assertEqual({r["mode"] for r in rows}, {"live_api"})


if __name__ == "__main__":
    unittest.main()
