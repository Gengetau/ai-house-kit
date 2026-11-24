"""Real-run round 5 (terra and gemini reruns of 11-21..11-22): the leftovers the earlier rounds only wrote down.

Every sentence, event and number below is taken from that run's evidence.

1. Saturday 09:00 the owner asked 婉妲 「今天下午一起去街上逛逛怎么样？」 while she slept. The reply was the sleeping placeholder
   (mode `sleeping`: 「（婉妲已经在卧室睡着了，手机屏幕亮了一下没有立刻回复……）」), yet `house_intents` got an outing row for her:
   `HouseDaemon._converse` called `enqueue_chat` for every reply that was not `blocked`.
2. A `memo` row of 「我对花生过敏…我的生日是3月14日」 showed `expires_at` 11-18, three days before `created_at`. It is NOT a
   defect: the analysis script printed `%m-%d` without the year, and the memo's expiry is created_at + 3650 days =
   2036-11-18. These cases pin that (no due phrase is read out of 「3月14日」; a memo never expires before it was made).
3. A diary fact list said 「9:17 艾莎去一楼客厅花艺 45 分钟（和凯、奈尔一起）」 when 凯 was listening to music and 奈尔 came into
   the room for the last 8 of the 45 minutes (she was in the study before): the list put everyone who shared the room under
   「一起」. A meal got the same fix in b815dda (same dish at the same table).
4. gemini: `[world-kernel authority] day note 2026-11-22: model_error error=LLMDeadlineError facts=132`, two days running. The
   day note was asked through the `life` route (25 s per request, 45 s in all, shared circuit breaker), the diary through
   `diary` (60 s / 90 s, 92960e0), and a failed note was never asked again.
5. `brought_home` was written twice for every purchase (when she walks into the living room with the bags and again when
   the five-minute unpacking step ends).
No test calls a model.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_daylog  # noqa: E402
import test_daynote  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon  # noqa: E402
from test_life_outings import Base as OutingsBase  # noqa: E402
from test_realrun_1_promises import H as HOUR, JST, PACK, _CapturingModel, _StoreCase  # noqa: E402
from test_realrun_3_daylog import DAY, NAMES, _Files  # noqa: E402
from worldkernel import daynote  # noqa: E402

M = test_daylog.M  # 2026-11-07 00:00 JST


def _at(day: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=JST).timestamp()


# ---------------------------------------------------------------------------------------------------------------
# 1. nothing is recorded as promised by a resident who was asleep
# ---------------------------------------------------------------------------------------------------------------
INVITE = "婉妲，今天下午一起去街上逛逛怎么样？"
PLACEHOLDER = "（婉妲已经在卧室睡着了，手机屏幕亮了一下没有立刻回复……）"


class SleepingResidentPromisesNothing(_StoreCase):
    def test_record_intents_off_keeps_the_owner_line_for_when_she_wakes_but_no_promise(self) -> None:
        self.store.enqueue_chat("wanda", INVITE, "", where="private", now=_at(21, 9), channel="private:wanda",
                                record_intents=False)
        self.assertEqual(self.rows("wanda"), [])
        with contextlib.closing(sqlite3.connect(self.db)) as c:
            queued = c.execute("SELECT actor, body FROM owner_chat_queue").fetchall()
        self.assertEqual(len(queued), 1)  # ChatLink still has the owner's line in front of her when she gets up
        body = json.loads(queued[0][1])
        self.assertEqual((body["owner"], body["her"], body["intents"]), (INVITE, "", []))

    def test_default_is_unchanged(self) -> None:
        self.say("wanda", INVITE, "好呀，一起去吧", now=_at(21, 9))
        self.assertEqual([r["kind"] for r in self.rows("wanda")], ["outing"])


class SleepingResidentThroughTheDaemon(unittest.TestCase):
    def boot(self):
        srv = _CapturingModel("好呀，一起去吧")
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            tmp.name,
            pack_dir=PACK,
            env={"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"},
            start_ts=dt.datetime(2026, 11, 20, 22, 0, tzinfo=JST).timestamp(),
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        d.fast_forward(180)  # 01:00 Saturday: everybody is asleep
        self.assertEqual(d.get_state()["residents"]["sora"]["reality"]["availability"], "sleeping")
        return d

    def test_a_sleeping_placeholder_records_no_promise(self) -> None:
        d = self.boot()
        res = d.converse("private:sora", "今天下午一起去街上逛逛怎么样？", owner_speaker("房主"))
        self.assertEqual([r["mode"] for r in res["replies"]], ["sleeping"])
        self.assertIn("睡着了", res["replies"][0]["text"])
        self.assertEqual(d.intents.list_intents(actor="sora"), [])
        # what the owner said is still with her (the queue ChatLink reads), without the placeholder as "her answer"
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            bodies = [json.loads(r[0]) for r in c.execute("SELECT body FROM owner_chat_queue")]
        self.assertEqual([(b["owner"], b["her"], b["intents"]) for b in bodies],
                         [("今天下午一起去街上逛逛怎么样？", "", [])])

    def test_asking_her_to_wake_up_still_gets_a_real_answer_and_a_promise(self) -> None:
        d = self.boot()
        res = d.converse("private:sora", "醒醒，今天下午一起去街上逛逛怎么样？", owner_speaker("房主"))
        self.assertNotEqual(res["replies"][0]["mode"], "sleeping")
        self.assertEqual([r["kind"] for r in d.intents.list_intents(actor="sora")], ["outing"])


# ---------------------------------------------------------------------------------------------------------------
# 2. a memo never expires before it was made (it was a display artefact, see the module note)
# ---------------------------------------------------------------------------------------------------------------
MEMO_SAID = "奈尔早安！我对花生过敏，你记一下哦。还有，我的生日是3月14日。"


class MemoHasNoDueInThePast(_StoreCase):
    def test_the_real_sentence_makes_one_memo_that_outlives_its_creation(self) -> None:
        now = _at(21, 9)
        self.say("nell", MEMO_SAID, "好的，记下了", now=now)
        (row,) = self.rows("nell")
        self.assertEqual((row["kind"], row["state"]), ("memo", "open"))
        self.assertGreater(row["expires_at"], row["created_at"])
        self.assertEqual(row["expires_at"], now + 3650 * 86400.0)
        self.assertEqual(dt.datetime.fromtimestamp(row["expires_at"], JST).year, 2036)
        self.assertEqual((row["due_from"], row["due_to"]), (None, None))
        self.store.sweep(now + 400 * 86400.0)  # a memo stays open
        self.assertEqual(self.rows("nell")[0]["state"], "open")

    def test_a_calendar_date_is_not_a_due_phrase(self) -> None:
        from worldkernel.intents_lite import parse_due_window
        for text in ("我的生日是3月14日", "我的生日是3月14日，你记一下", "11月18日是我的纪念日"):
            with self.subTest(text=text):
                self.assertEqual(parse_due_window(text, _at(21, 9)), (None, None))


# ---------------------------------------------------------------------------------------------------------------
# 3. the diary list says 「和X一起」 only for the same thing at the same time
# ---------------------------------------------------------------------------------------------------------------
def _flower_day(*, kai_does: str = "listening_music") -> "test_daylog.Day":
    """艾莎 arranges flowers in the living room 09:18-10:03. 凯 is there from 09:21 (doing `kai_does`). 奈尔 comes in from the
    study at 09:55 (8 minutes of the 45) and does housework."""
    d = test_daylog.Day()
    d.move("elsa", "kitchen", "living", M + 9 * HOUR + 17 * 60)
    d.do("elsa", "flower_arranging", "living", M + 9 * HOUR + 18 * 60, 45)
    d.move("kai", "kitchen", "living", M + 9 * HOUR + 21 * 60)
    d.do("kai", kai_does, "living", M + 9 * HOUR + 22 * 60, 40)
    d.do("nell", "reading", "study", M + 8 * HOUR + 55 * 60, 55)
    d.move("nell", "study", "living", M + 9 * HOUR + 55 * 60)
    d.do("nell", "housework", "living", M + 9 * HOUR + 56 * 60, 30)
    return d


class DiaryTogetherMeansTheSameThing(unittest.TestCase):
    def line(self, f: _Files, actor: str, needle: str) -> str:
        got = [x for x in f.facts(actor) if needle in x]
        self.assertEqual(len(got), 1, f.facts(actor))
        return got[0]

    def test_the_real_flower_morning(self) -> None:
        f = _Files(self, _flower_day())
        line = self.line(f, "elsa", "花艺")
        self.assertIn("艾莎去一楼客厅花艺 45 分钟", line)
        self.assertNotIn("一起", line)           # 凯 listened to music, 奈尔 was in the study until 09:55
        self.assertNotIn("奈尔", line)           # in the room for 8 of the 45 minutes: not there
        self.assertIn("同屋还有凯", line)         # said as sharing the room, not as doing it together

    def test_the_same_activity_at_the_same_time_is_together(self) -> None:
        f = _Files(self, _flower_day(kai_does="flower_arranging"))
        line = self.line(f, "elsa", "花艺")
        self.assertIn("和凯一起", line)
        self.assertNotIn("奈尔", line)
        self.assertIn("和艾莎一起", self.line(f, "kai", "花艺"))

    def test_same_activity_in_another_room_or_at_another_time_is_not_together(self) -> None:
        d = test_daylog.Day()
        d.do("elsa", "reading", "study", M + 14 * HOUR, 60)
        d.do("kai", "reading", "living", M + 14 * HOUR, 60)          # the same activity, another room
        d.do("nell", "reading", "study", M + 16 * HOUR, 60)          # the same room, two hours later
        f = _Files(self, d)
        line = self.line(f, "elsa", "看书")
        self.assertNotIn("一起", line)
        self.assertNotIn("凯", line)
        self.assertNotIn("奈尔", line)

    def test_a_long_gaming_session_with_the_room_busy_with_other_things(self) -> None:
        # 米娜 20:17 gaming 2 h 18 min in the living room; 婉妲 was there on her phone, 艾莎 did housework for half an hour
        d = test_daylog.Day()
        d.do("mina", "gaming", "living", M + 20 * HOUR + 18 * 60, 138)
        d.do("wanda", "eating_snack", "living", M + 20 * HOUR + 4 * 60, 150, snack="fruit")
        d.do("elsa", "housework", "living", M + 20 * HOUR + 31 * 60, 31)
        d.move("elsa", "living", "bath", M + 21 * HOUR + 2 * 60)
        f = _Files(self, d)
        line = self.line(f, "mina", "打游戏")
        self.assertNotIn("一起", line)
        self.assertNotIn("艾莎", line)           # 31 of 138 minutes
        self.assertIn("同屋还有婉妲", line)

    def test_walks_outfits_and_shopping_are_not_done_together_with_whoever_stood_there(self) -> None:
        d = test_daylog.Day()
        d.move("mina", "room_mina", "kitchen", M + 19 * HOUR + 10 * 60)
        d.clothes("mina", ["hoodie"], "home", "living", M + 16 * HOUR + 14 * 60)
        d.do("mina", "reading", "living", M + 16 * HOUR, 5)
        for who in ("alma", "elsa", "kai"):
            d.do(who, "reading", "living", M + 16 * HOUR, 120)            # in the living room when she changes
            d.move(who, "living", "kitchen", M + 19 * HOUR + 9 * 60)
        f = _Files(self, d)
        for needle in ("去了厨房", "换上了"):
            line = self.line(f, "mina", needle)
            self.assertNotIn("一起", line)
            for other in ("阿尔玛", "艾莎", "凯"):
                self.assertNotIn(other, line)

    def test_the_console_timeline_still_shows_who_was_in_the_room(self) -> None:
        from worldkernel import daylog
        d = _flower_day()
        self.addCleanup(d.db.close)
        got = [x for x in daylog.entries(d.db, M, M + 24 * HOUR, M + 24 * HOUR, names=NAMES) if x["actor"] == "elsa"]
        flower = [x for x in got if x["kind"] == "activity"][0]
        self.assertEqual(flower["with"], ["kai", "nell"])  # unchanged: the week page's 「同在」


# ---------------------------------------------------------------------------------------------------------------
# 4. the day note: the diary's time limits, and a few more tries
# ---------------------------------------------------------------------------------------------------------------
class DayNoteAsksLikeTheDiary(unittest.TestCase):
    def test_the_daemon_asks_for_the_day_note_through_the_diary_route(self) -> None:
        srv = _CapturingModel("好的")
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            tmp.name,
            pack_dir=PACK,
            env={"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"},
            start_ts=dt.datetime(2026, 11, 20, 22, 0, tzinfo=JST).timestamp(),
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        seen = []

        def fake(system, user, *, purpose="chat", actor="", timeout_s=None):
            seen.append((purpose, system))
            return "", 0

        with mock.patch.object(d.model_router, "complete_text", side_effect=fake):
            d.fast_forward(120)  # to 00:00: the note of the 20th is due from 23:30
        notes = [p for p, s in seen if s.startswith(daynote.SYSTEM[:12])]
        self.assertTrue(notes, [(p, s[:20]) for p, s in seen])
        self.assertEqual(set(notes), {"diary"})
        self.assertIn("life", {p for p, _ in seen})  # her own decisions keep their route

    def test_limits_leave_room_for_the_first_call_and_two_tries(self) -> None:
        self.assertGreaterEqual(daynote.LIMITS["per_hour"], 3)
        self.assertGreaterEqual(daynote.LIMITS["per_day"], 3)


class DayNoteRetry(test_daynote.Base):
    def failing(self) -> None:
        self.day()
        self.note.retries = 2
        self.chat.error = TimeoutError("slow")

    def test_a_slow_model_gets_three_tries_ten_minutes_apart(self) -> None:
        self.failing()
        self.at(23, 30)
        self.assertEqual(self.note.step(), "model_error")
        self.at(23, 35)
        self.assertIsNone(self.note.step())          # too soon
        self.at(23, 40)
        self.assertEqual(self.note.step(), "model_error")
        self.at(23, 45)
        self.assertIsNone(self.note.step())
        self.at(23, 50)
        self.assertEqual(self.note.step(), "model_error")   # the third and last
        for h, m, d in ((0, 10, 1), (1, 0, 1), (2, 29, 1)):
            self.at(h, m, d)
            self.assertIsNone(self.note.step())
        self.assertEqual(len(self.chat.calls), 3)
        self.assertEqual([s for s, _ in self.budget()], ["unknown"] * 3)   # none of them was refused for budget
        self.assertEqual(self.rows(), [])

    def test_the_try_that_works_writes_it_and_ends_the_tries(self) -> None:
        self.failing()
        self.at(23, 30)
        self.assertEqual(self.note.step(), "model_error")
        self.chat.error = None
        self.at(23, 41)
        self.assertEqual(self.note.step(), "written")
        self.at(23, 55)
        self.assertIsNone(self.note.step())
        self.assertEqual(len(self.chat.calls), 2)
        self.assertEqual(len(self.rows()), 1)

    def test_no_try_after_half_past_two(self) -> None:
        self.failing()
        self.at(2, 25, 1)   # the night's first call comes late (the house was down)
        self.assertEqual(self.note.step(), "model_error")
        self.at(2, 40, 1)
        self.assertIsNone(self.note.step())
        self.assertEqual(len(self.chat.calls), 1)

    def test_the_tries_survive_a_restart(self) -> None:
        self.failing()
        self.at(23, 30)
        self.note.step()
        self.a.store.release()
        chat = test_daynote.Chat(test_daynote.GOOD)
        a = self.authority.Authority(self.kpath, self.wpath, chat, {}, clock=self.clock,
                                     model=test_daynote.FakeLifeModel(), migrate_items=False, day_note=True,
                                     day_note_retries=2)
        self.addCleanup(a.store.close)
        self.addCleanup(a.store.release)
        a.note.log = lambda m: None
        self.at(23, 35)
        self.assertIsNone(a.note.step())
        self.at(23, 41)
        self.assertEqual(a.note.step(), "written")

    def test_a_refused_answer_and_unreadable_facts_are_still_not_retried(self) -> None:
        self.day()
        self.note.retries = 2
        self.chat.answer = test_daynote.GOOD.replace("由纪", "婉妲")
        self.at(23, 30)
        self.assertEqual(self.note.step(), "refused")
        self.at(23, 45)
        self.assertIsNone(self.note.step())
        self.assertEqual(len(self.chat.calls), 1)

    def test_without_retries_a_failed_call_is_still_final(self) -> None:
        self.day()
        self.chat.error = TimeoutError("slow")
        self.at(23, 30)
        self.note.step()
        self.at(23, 45)
        self.assertIsNone(self.note.step())
        self.assertEqual(len(self.chat.calls), 1)

    def test_the_note_can_use_a_route_of_its_own(self) -> None:
        note_chat = test_daynote.Chat(test_daynote.GOOD)
        self.a.store.release()
        a = self.authority.Authority(self.kpath, self.wpath, self.chat, {}, clock=self.clock,
                                     model=test_daynote.FakeLifeModel(), migrate_items=False, day_note=True,
                                     note_chat=note_chat)
        self.addCleanup(a.store.close)
        self.addCleanup(a.store.release)
        a.note.log = lambda m: None
        self.day()
        self.at(23, 30)
        self.assertEqual(a.note.step(), "written")
        self.assertEqual((len(note_chat.calls), len(self.chat.calls)), (1, 0))


# ---------------------------------------------------------------------------------------------------------------
# 5. a purchase is brought home once
# ---------------------------------------------------------------------------------------------------------------
class BroughtHomeOnce(OutingsBase):
    def test_every_purchase_has_one_brought_home(self) -> None:
        self.o.shopping.targets = {"egg": 14, "milk": 14}
        self.o.step()
        pid = self.l.get("shopping:current")["plan_id"]
        self.finish(pid)
        bought = {json.loads(r[0])["item"]: r[1] for r in self.s.db.execute(
            "SELECT payload, action_id FROM events WHERE kind='purchased'")}
        self.assertEqual(set(bought), {"egg", "milk"})
        home = [json.loads(r[0]) for r in self.s.db.execute("SELECT payload FROM events WHERE kind='brought_home'")]
        self.assertEqual(sorted(h["item"] for h in home), ["egg", "milk"], home)
        self.assertEqual({h["action_id"] for h in home}, set(bought.values()))
        self.assertTrue(all(h["qty"] > 0 for h in home))
        # and the goods really did reach the kitchen shelf
        self.assertEqual(self.s.db.execute(
            "SELECT COUNT(*) FROM resources WHERE resource_id LIKE 'bought:%' AND holder IS NOT NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
