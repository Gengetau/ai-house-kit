"""今天的小屋: worldkernel.daynote over a temp life1.db + world.db with a fake chat (no model, no network).

Run: python3 -m unittest -v test_daynote
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_daylog  # noqa: E402
from worldkernel import daynote  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.legacy import LEGACY_SCHEMA  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
DAY = dt.date(2026, 11, 7)
M = dt.datetime(2026, 11, 7, tzinfo=JST).timestamp()
H = 3600
# a note that only says what the facts below say
GOOD = ("上午十一点，奈尔在一楼客厅看了一个小时的书。下午三点，奈尔出门去了超市，买了鸡蛋。傍晚六点，艾莎做了味噌汤。"
        "晚上九点，由纪去浴室洗澡，前后大约半个小时。这一天里，奈尔在客厅看书，又去超市买东西；艾莎做了汤；"
        "由纪晚上在浴室洗了澡。上午的书看了一个小时，超市的鸡蛋买了六个，晚上的澡洗了半个小时。")


class FileDay(test_daylog.Day):
    """test_daylog's helpers, writing into the authority's own life1.db file."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.n = 900

    def done(self):
        self.db.commit()
        self.db.close()


class Chat:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return (json.dumps({"text": self.answer}, ensure_ascii=False) if self.answer is not None else ""), 321


class Base(unittest.TestCase):
    def setUp(self):
        import authority
        self.authority = authority
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.wpath, self.kpath = os.path.join(d.name, "world.db"), os.path.join(d.name, "life1.db")
        c = sqlite3.connect(self.wpath)
        c.executescript(LEGACY_SCHEMA)
        for a in V.PEOPLE:
            c.execute("insert into reality values(?, 'living', '客厅', 'idle', '发呆', '平静', 50, 60, 30, 'free',"
                      " 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
        for ts, actors, text in ((M + 15 * H + 30, ["nell"], "奈尔到了超市"),        # told by her detail line
                                 (M + 18 * H, ["elsa"], "艾莎做了味噌汤"),
                                 (M - 4 * H, ["wanda"], "婉妲到了便利店"),          # the day before
                                 (M + 24 * H + 600, ["mina"], "米娜做了煎蛋卷")):  # the day after
            c.execute("insert into script(ts, actors, scene, text, source, created, knowers) values (?,?,?,?,?,?,?)",
                      (ts, json.dumps(actors), None, text, "kernel", ts, json.dumps(actors)))
        c.execute("insert into script(ts, actors, scene, text, source, created, knowers) values (?,?,?,?,?,?,?)",
                  (M + 19 * H, json.dumps(["alma"]), None, "阿尔玛写的一段旧剧本", "model", M, "[]"))
        c.commit()
        c.close()
        self.clock = VirtualClock(M + 10 * H)
        self.chat = Chat(GOOD)
        self.a = authority.Authority(self.kpath, self.wpath, self.chat, {}, clock=self.clock, model=FakeLifeModel(),
                                     migrate_items=False, day_note=True)
        self.addCleanup(self.a.store.close)
        self.addCleanup(self.a.store.release)
        self.note = self.a.note
        self.note.log = lambda m: None

    def day(self):
        d = FileDay(self.kpath)
        d.do("nell", "reading", "living", M + 11 * H, 60)
        d.move("nell", "living", "place:supermarket", M + 15 * H)
        d.buy("nell", "egg", 6, 280, "place:supermarket", M + 15 * H + 300)
        d.move("yuki", "living", "bath", M + 21 * H)
        d.do("yuki", "bathing", "bath", M + 21 * H + 60, 30)
        d.do("mina", "gaming", "living", M - 2 * H, 30)  # the day before: not in the facts
        d.done()

    def at(self, h, m=0, day=0):
        self.clock.t = M + day * 86400 + h * H + m * 60

    def rows(self):
        c = sqlite3.connect(self.kpath)
        try:
            return c.execute("select summary_id, scene, audience, text, triggers, summary_version, source_event_ids"
                             " from summaries where scene='day'").fetchall()
        finally:
            c.close()

    def budget(self):
        c = sqlite3.connect(self.kpath)
        try:
            return c.execute("select state, used_tokens from budget_reservations where pool=?",
                             (daynote.POOL,)).fetchall()
        finally:
            c.close()

    def result(self, day=DAY):
        return self.a.life.get(f"day_note:{day.isoformat()}:result")


class DueTests(Base):
    def test_from_half_past_eleven_until_three(self):
        due = {(23, 29, 0): None, (23, 30, 0): DAY, (23, 59, 0): DAY, (0, 5, 1): DAY, (2, 59, 1): DAY,
               (3, 0, 1): None, (12, 0, 1): None, (23, 30, 1): DAY + dt.timedelta(days=1)}
        for (h, m, d), want in due.items():
            self.at(h, m, d)
            self.assertEqual(self.note.due(), want, (h, m, d))

    def test_off_unless_switched_on(self):
        for env, chat, want in (({}, self.chat, False), ({"WORLD_KERNEL_DAY_NOTE": "0"}, self.chat, False),
                                ({"WORLD_KERNEL_DAY_NOTE": "1"}, self.chat, True),
                                ({"WORLD_KERNEL_DAY_NOTE": "1"}, None, False)):  # no model route: nothing to call
            self.a.store.release()
            with patch.dict(os.environ, env, clear=False):
                if "WORLD_KERNEL_DAY_NOTE" not in env:
                    os.environ.pop("WORLD_KERNEL_DAY_NOTE", None)
                a = self.authority.Authority(self.kpath, self.wpath, chat, {}, clock=self.clock,
                                             model=FakeLifeModel(), migrate_items=False)
            self.assertEqual(a.note is not None, want, env)
            a.close()
            self.a.store.acquire()


class WriteTests(Base):
    def test_one_note_a_night_from_that_days_facts(self):
        self.day()
        self.at(23, 30)
        self.assertEqual(self.note.step(), "written")
        self.assertEqual(len(self.chat.calls), 1)
        system, user = self.chat.calls[0]
        for line in ("\n11:00–12:00 奈尔在一楼客厅看书 1 小时（同在：米娜）\n",  # mina stayed there from the night before
                     "\n15:00–15:05 奈尔出门去超市，买了鸡蛋 ×6，共 ¥280（公账）\n", "\n18:00 艾莎做了味噌汤\n",
                     "\n21:00–21:31 由纪去浴室洗澡/泡澡 30 分钟\n"):
            self.assertIn(line, user)
        for other in ("婉妲", "煎蛋卷", "玩", "旧剧本", "奈尔到了超市"):  # other days, the old world, a told line
            self.assertNotIn(other, user)
        self.assertIn("2026 年 11 月 7 日（星期六）", user)
        (sid, scene, aud, text, trig, ver, ids), = self.rows()
        self.assertEqual((sid, scene, aud, text, json.loads(trig), ver),
                         ("day:2026-11-07", "day", "owner", GOOD, ["nightly"], daynote.VERSION))
        self.assertTrue(json.loads(ids))
        self.assertEqual(self.budget(), [("settled", 321)])
        # later that night and after midnight: nothing more
        for h, m, d in ((23, 31, 0), (23, 59, 0), (1, 0, 1), (2, 30, 1)):
            self.at(h, m, d)
            self.assertIsNone(self.note.step())
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(self.result()["outcome"], "written")

    def test_a_failed_call_is_not_retried(self):
        self.day()
        self.chat.error = TimeoutError("slow")
        self.at(23, 30)
        self.assertEqual(self.note.step(), "model_error")
        for h, m, d in ((23, 45, 0), (0, 30, 1), (2, 0, 1)):
            self.at(h, m, d)
            self.note.step()
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.budget(), [("unknown", None)])  # cost unknown: it keeps counting
        self.assertEqual((self.result()["outcome"], self.result()["error"]), ("model_error", "TimeoutError"))

    def test_a_refused_answer_is_not_kept_or_retried(self):
        self.day()
        self.chat.answer = GOOD.replace("由纪", "婉妲")
        self.at(23, 30)
        self.assertEqual(self.note.step(), "refused")
        self.at(23, 50)
        self.note.step()
        self.assertEqual((len(self.chat.calls), self.rows()), (1, []))
        self.assertEqual(self.result()["reason"], "person_not_in_facts:wanda")
        self.assertNotIn("text", self.result())  # the outcome never carries the note

    def test_attempt_survives_a_restart(self):
        self.day()
        self.chat.error = RuntimeError("crash")
        self.at(23, 30)
        self.note.step()
        self.a.store.release()
        chat = Chat(GOOD)
        a = self.authority.Authority(self.kpath, self.wpath, chat, {}, clock=self.clock, model=FakeLifeModel(),
                                     migrate_items=False, day_note=True)
        self.addCleanup(a.store.close)
        self.addCleanup(a.store.release)
        self.at(23, 40)
        self.assertIsNone(a.note.step())
        self.assertEqual(chat.calls, [])

    def test_too_few_facts_no_call(self):
        self.at(23, 30)
        self.assertEqual(self.note.step(), "too_few_facts")
        self.assertEqual((self.chat.calls, self.budget()), ([], []))

    def test_facts_that_cannot_be_read_are_said_and_not_retried(self):
        self.day()
        os.remove(self.wpath)  # world.db gone: the read-only open fails
        self.at(23, 30)
        self.assertEqual(self.note.step(), "facts_error")
        self.assertEqual((self.result()["outcome"], self.result()["error"]), ("facts_error", "OperationalError"))
        self.at(23, 40)
        self.assertIsNone(self.note.step())
        self.assertEqual((self.chat.calls, self.budget()), ([], []))

    def test_no_json_is_no_note(self):
        self.day()
        self.chat.answer = None  # an empty answer (or one cut off by max_tokens)
        self.at(23, 30)
        self.assertEqual(self.note.step(), "refused")
        self.assertEqual((self.result()["reason"], self.result()["answer_chars"]), ("no_text", 0))
        self.assertEqual(self.rows(), [])

    def test_no_budget_no_call(self):
        self.day()
        for _ in range(daynote.LIMITS["per_hour"]):
            self.note.budget.reserve("system", "day_note")
        self.at(23, 30)
        self.assertEqual(self.note.step(), "no_budget")
        self.assertEqual(self.chat.calls, [])

    def test_rewrite_in_place(self):
        self.day()
        self.at(23, 30)
        self.note.step()
        self.at(23, 45)
        self.assertEqual(self.note.write(DAY), "exists")  # without force: kept
        self.chat.answer = GOOD.replace("这一天里", "这天")
        self.clock.t += 3 * H  # the next morning, a manual rewrite
        self.assertEqual(self.note.write(DAY, trigger="manual", force=True), "written")
        (sid, _, _, text, trig, _, _), = self.rows()
        self.assertEqual((sid, text, json.loads(trig)), ("day:2026-11-07", GOOD.replace("这一天里", "这天"), ["manual"]))
        with self.a.store.read() as db:
            got = daynote.note(db, DAY)
        self.assertEqual(got["text"], GOOD.replace("这一天里", "这天"))
        self.assertEqual(got["at"], self.clock.now())

    def test_a_failing_note_never_stops_her_day(self):
        self.day()
        self.at(23, 30)
        with patch.object(self.note, "write", side_effect=sqlite3.OperationalError("database is locked")), \
                patch.object(self.authority, "log") as log:
            self.a.step()
        self.assertEqual(float(self.a.store.meta("last_step")), self.clock.now())
        self.assertIn("day note failed", log.call_args[0][0])
        self.at(23, 31)
        self.a.step()  # the night's attempt is used up: no second call
        self.assertEqual(self.chat.calls, [])

    def test_authority_step_writes_it(self):
        self.day()
        self.at(23, 30)
        self.a.step()
        self.assertEqual(len(self.chat.calls), 1)
        self.assertEqual(len(self.rows()), 1)


class CheckTests(unittest.TestCase):
    LINES = [{"ts": M + 11 * H, "end": M + 12 * H, "text": "奈尔在一楼客厅阅读 1 小时", "with": ["elsa"], "events": [1],
              "kind": "activity"},
             {"ts": M + 18 * H, "end": None, "text": "艾莎做了味噌汤", "with": [], "events": [], "kind": "script"},
             {"ts": M + 21 * H, "end": M + 21.5 * H, "text": "由纪去浴室洗澡/泡澡 30 分钟", "with": [], "events": [2],
              "kind": "activity"}]
    BASE = ("上午奈尔在客厅读了一个小时的书，艾莎也在。傍晚艾莎做了味噌汤。晚上由纪去浴室洗了半小时澡。" * 4)

    def check(self, text):
        return daynote.check(text, self.LINES)

    def test_a_plain_note_passes(self):
        self.assertIsNone(self.check(self.BASE))  # 客厅 for 一楼客厅 is the same room

    def test_refusals(self):
        cases = {self.BASE[:60]: "too_short", self.BASE * 2: "too_long",
                 self.BASE + "奈尔说：「真好看。」": "quoted_speech",
                 self.BASE + "米娜也来了。": "person_not_in_facts:mina",
                 self.BASE + "房主也在。": "owner_not_in_facts",
                 self.BASE + "后来她们去了公园。": "place_not_in_facts",
                 self.BASE + "她们去了院子。": "place_not_in_facts",
                 self.BASE + "后来又去了日光。": "place_not_in_facts",
                 self.BASE + "大家都很开心。": "feeling",
                 self.BASE + "她好像累了。": "feeling",
                 self.BASE + "这是悠闲的一天。": "feeling",
                 self.BASE + "晚上很热闹。": "feeling",
                 self.BASE + "奈尔和艾莎聊了一会儿。": "speech",
                 self.BASE + "艾莎说汤做好了。": "speech",
                 "": "no_text"}
        for text, why in cases.items():
            self.assertEqual(self.check(text), why, text[-12:])

    def test_not_everything_that_looks_like_a_word_is_one(self):
        for extra in ("由纪大概泡了半小时。", "她读的是一本小说。"):
            self.assertIsNone(self.check(self.BASE + extra), extra)
        lines = self.LINES + [{"ts": M + 22 * H, "end": None, "text": "阿尔玛在小院子日光浴 20 分钟", "with": [],
                               "events": [], "kind": "activity"}]
        self.assertIsNone(daynote.check(self.BASE + "阿尔玛在院子里晒日光浴。", lines))

    def test_the_kitchen_of_a_day_with_cooking(self):
        # 「艾莎做了味噌汤」 (a script line) says no room: on such a day the kitchen counts as named
        self.assertIsNone(self.check(self.BASE + "艾莎在厨房做的汤。"))
        no_cooking = [x for x in self.LINES if x["kind"] != "script"]
        self.assertEqual(daynote.check(self.BASE.replace("傍晚艾莎做了味噌汤。", "") + "奈尔去了厨房。" * 3,
                                       no_cooking), "place_not_in_facts")

    def test_a_very_full_day_leaves_the_walks_out_first(self):
        walks = [{"ts": M + i * 60, "end": None, "text": f"奈尔从一楼客厅去了书房{i}", "with": [], "events": [],
                  "kind": "move"} for i in range(daynote.MAX_FACTS)]
        _, user = daynote.prompt(DAY, sorted(self.LINES + walks, key=lambda x: x["ts"]))
        self.assertNotIn("去了书房", user)
        self.assertIn("艾莎做了味噌汤", user)
        self.assertIn("略去了零碎的走动", user)

    def test_a_word_the_facts_have_is_fine(self):
        lines = self.LINES + [{"ts": M + 22 * H, "end": None, "text": "阿尔玛在小院子享受晚风", "with": [], "events": [],
                               "kind": "activity"}]
        self.assertIsNone(daynote.check(self.BASE + "阿尔玛在院子享受晚风。", lines))

    def test_reading_the_answer(self):
        self.assertEqual(daynote.read('好的：\n{"text": " 一段话 "}'), "一段话")
        self.assertEqual(daynote.read("一段话"), "")  # no JSON: no note (a preamble is never kept)
        self.assertEqual(daynote.read('{"text": 不是 json}'), "")
        self.assertEqual(daynote.read('{"text": "被截断的一段'), "")
        self.assertEqual(daynote.read('{"text": ["一段"]}'), "")
        self.assertEqual(daynote.read(""), "")

    def test_the_prompt_carries_who_was_there(self):
        _, user = daynote.prompt(DAY, self.LINES)
        self.assertIn("11:00–12:00 奈尔在一楼客厅阅读 1 小时（同在：艾莎）", user)
        self.assertIn("18:00 艾莎做了味噌汤\n", user)
        self.assertIn("150~300", user)


if __name__ == "__main__":
    unittest.main()
