"""M1 step 2b: clock, host time zone, host environment, settings and the extraction schedule of `CommonMemoryStore`.

Run it under any host zone (`TZ=UTC`, `TZ=Asia/Tokyo`): nothing here may depend on it.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_m1_adapter_support import (
    ASK_PLAN, NAMES, OWNER_SAYS, SECRET_PLAN, T0, FakeModel, StoreCase, add_op, quiet_resource_warnings,
)

from ai_house.memory_common import CommonMemoryStore  # noqa: E402

GROUP = "qq-group:111"
OTHER = "qq-group:222"


def setUpModule():
    quiet_resource_warnings()


def settings(mod):
    """Every plain upper-case setting of a loaded copy, but the database path."""
    plain = (int, float, str, bool, set, frozenset, list, tuple, dict, type(None))
    return {k: (sorted(v) if isinstance(v, (set, frozenset)) else v) for k, v in vars(mod).items()
            if k.isupper() and isinstance(v, plain) and k not in ("DB_PATH", "TOOLS")}  # TOOLS holds functions


class ClockTests(StoreCase):
    def test_now_and_the_house_clock_decide_every_time(self):
        st = self.make()
        mid, _ = st.add("nia", "private", SECRET_PLAN, now=T0)
        self.assertEqual(st.list_entries("nia", "private")[0]["created"], T0)
        st.observe("nia", "private", OWNER_SAYS, now=T0 + 10)
        self.assertEqual(self.rows(st, "select ts from seen")[0]["ts"], T0 + 10)
        st.recall("nia", "private", ASK_PLAN, now=T0 + 99)
        self.assertEqual(st.list_entries("nia", "private")[0]["last_used"], T0 + 99)
        # no `now`: the house clock (an object with now(), or a plain callable), then the real time
        st._clock = type("C", (), {"now": lambda self: T0 + 500})()
        st.observe("nia", "private", "第二句话说点别的", now=None)
        self.assertEqual(self.rows(st, "select ts from seen order by id desc")[0]["ts"], T0 + 500)
        st._clock = lambda: T0 + 600
        st.recall("nia", "private", ASK_PLAN)
        self.assertEqual(st.list_entries("nia", "private")[0]["last_used"], T0 + 600)
        st._clock = None
        before = time.time()
        st.observe("nia", "private", "第三句话也说点别的")
        self.assertGreaterEqual(self.rows(st, "select ts from seen order by id desc")[0]["ts"], before)
        # the explicit now beats the clock
        st._clock = lambda: T0 + 700
        st.recall("nia", "private", ASK_PLAN, now=T0 + 5)
        self.assertEqual(st.list_entries("nia", "private")[0]["last_used"], T0 + 5)

    def test_the_extraction_is_due_by_the_house_clock(self):
        model = FakeModel([add_op(SECRET_PLAN)])
        st = self.make(llm_fn=model)
        st.observe("nia", "private", OWNER_SAYS, now=T0)
        self.assertEqual(st.extract(now=T0 + 10)["buckets"], 0)
        st._clock = lambda: T0 + 1799
        self.assertEqual(st.extract()["buckets"], 0)
        self.assertEqual(model.calls, [])
        st._clock = lambda: T0 + 1800
        res = st.extract()
        self.assertEqual((res["buckets"], res["lines"], res["added"]), (1, 1, 1))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.rows(st, "select created from mem")[0]["created"], T0 + 1800)
        self.assertEqual(st.extract()["buckets"], 0)  # nothing new

    def test_results_do_not_depend_on_the_host_zone(self):
        if not hasattr(time, "tzset"):
            self.skipTest("no tzset")
        shown, prompts = {}, {}
        saved = os.environ.get("TZ")

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

        self.addCleanup(restore)
        for tz in ("UTC", "Asia/Tokyo", "America/Los_Angeles", "Pacific/Kiritimati"):
            os.environ["TZ"] = tz
            time.tzset()
            model = FakeModel()
            st = self.make(llm_fn=model)
            st.add("nia", "private", SECRET_PLAN, now=T0)
            st.observe("nia", "private", OWNER_SAYS, reply="好的。", now=T0)
            st.extract(force=True, now=T0 + 5)
            shown[tz] = st.recall("nia", "private", ASK_PLAN, now=T0 + 6)
            prompts[tz] = model.calls[0][1]
        self.assertEqual(len(set(shown.values())), 1, shown)
        self.assertEqual(len(set(prompts.values())), 1)
        self.assertIn("（记于 11-24）", shown["UTC"])  # 02:00 JST, still the 23rd by UTC
        self.assertIn("—— 2026-11-24 02:00 (Tue) JST ——", prompts["UTC"])

    def test_a_diary_belongs_to_the_japanese_day(self):
        st = self.make()
        st.add("nia", "world", "2026-11-24 日记：凌晨两点还没睡，窗外在下雨，和房主聊起了春天的旅行。", kind="diary", now=T0)
        got = st.recall("nia", "private", "昨天的日记 凌晨还没睡 下雨", now=T0 + 3600)
        self.assertIn("春天的旅行", got)
        self.assertIn("（11-23）", got)  # before 05:00 JST a diary still belongs to the evening before


class HostEnvironmentTests(StoreCase):
    POLLUTION = {
        "MEMORY_UNIFIED": "nia,xingli", "MEMORY_PRIVATE_IN_GROUPS": "1", "MEMORY_LEAK_FREE_CHATS": "group:111",
        "MEMORY_DB": "/nonexistent/dir/memory.db", "MEMORY_CONSUMERS": "somebody", "MEMORY_NAMES": "nia=别人",
        "MEMORY_RECALL_K": "1", "MEMORY_RECALL_MIN": "0.99", "MEMORY_EXTRACT_EVERY_S": "1", "MEMORY_EXTRACT_BATCH": "1",
        "MEMORY_EXTRACT_PER_HOUR": "1", "MEMORY_MAX_ACTIVE": "1", "MEMORY_RULES": "nia", "MEMORY_RECENT": "nia",
        "MEMORY_URGENT": "1", "MEMORY_EVIDENCE": "1", "MEMORY_EVIDENCE_FOR": "nia", "MEMORY_PUBLIC_SCRIBES": "nia",
        "MEMORY_DIARY_PRIVATE": "0", "MEMORY_PUBLIC_EXPOSURE_GATE": "0", "MEMORY_OWNER_CHATS": "nia@private:5",
        "MEMORY_CHAT_ALIASES": "nia@group:9=group:111", "MEMORY_EMBED_URL": "http://embed.invalid", "MEMORY_MODEL": "m",
        "CPA_BASE": "http://model.invalid", "CPA_CONFIG": "/nonexistent/cpa.yaml",
    }

    def test_variables_of_the_shared_module_in_the_host_do_not_reach_the_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self.make(tmp + "/a")
            with mock.patch.dict(os.environ, self.POLLUTION):
                dirty = CommonMemoryStore(tmp + "/b", names=NAMES)  # reads the real environment, as the daemon does
                self.addCleanup(dirty.close)
                self.assertEqual(os.environ["MEMORY_UNIFIED"], "nia,xingli")  # put back after the copy was made
                self.assertEqual(os.environ["CPA_BASE"], "http://model.invalid")
            self.assertEqual(settings(dirty._mod), settings(clean._mod))
            self.assertEqual(dirty._mod.search.__defaults__, clean._mod.search.__defaults__)
            self.assertEqual(dirty._mod.UNIFIED, set())
            self.assertEqual(dirty._mod.LEAK_FREE_CHATS, set())
            self.assertEqual(dirty._mod.RECALL_K, 6)
            self.assertEqual(dirty._mod.search.__defaults__[1], 6)
            self.assertEqual(dirty._mod.DB_PATH, str(dirty.db_path))
            self.assertTrue(dirty.db_path.exists())
            self.assertFalse(dirty.vector_enabled)  # MEMORY_EMBED_URL is not read
            dirty.add("nia", "private", SECRET_PLAN, now=T0)
            self.assertEqual(dirty.recall("nia", "group", ASK_PLAN, channel="qq-group:111", now=T0), "")
            # the group is checked (it is not listed here; the host's MEMORY_LEAK_FREE_CHATS did not exempt anything)
            self.assertEqual(len(dirty.check_outbound_leak("nia", "group", SECRET_PLAN, channel="qq-group:111", now=T0)), 1)
            self.assertFalse(dirty.leak_free_in("qq-group:111"))
            self.assertEqual(len(dirty.list_entries("nia", "private")), 1)  # MEMORY_MAX_ACTIVE=1 did not trim anything
            dirty.add("nia", "private", "房主喜欢喝热的豆浆。", now=T0)
            dirty.add("nia", "private", "房主养了一只叫团子的猫。", now=T0)
            self.assertEqual(len(dirty.list_entries("nia", "private")), 3)

    def test_a_reload_also_ignores_the_host(self):
        st = self.make()
        with mock.patch.dict(os.environ, self.POLLUTION):
            st.names = {"nia": "妮娅", "xingli": "星璃", "mira": "米拉"}
        self.assertEqual(st._mod.UNIFIED, set())
        self.assertEqual(st._mod.CONSUMERS, ["mira", "nia", "xingli"])
        self.assertEqual(st._mod.NAMES["nia"], "妮娅")


class SettingsTests(StoreCase):
    def test_kit_settings_become_the_modules_constants_and_the_search_default(self):
        env = {"AI_HOUSE_MEMORY_RECALL_K": "2", "AI_HOUSE_MEMORY_MIN_SCORE": "0.45", "AI_HOUSE_MEMORY_EXTRACT_EVERY_S": "60",
               "AI_HOUSE_MEMORY_EXTRACT_BATCH": "5", "AI_HOUSE_MEMORY_EXTRACT_PER_HOUR": "7", "AI_HOUSE_MEMORY_MAX_ACTIVE": "50"}
        mod = self.make(env=env)._mod
        self.assertEqual((mod.RECALL_K, mod.RECALL_MIN, mod.EXTRACT_EVERY_S, mod.EXTRACT_BATCH, mod.EXTRACT_PER_HOUR,
                          mod.MAX_ACTIVE), (2, 0.45, 60, 5, 7, 50))
        self.assertEqual(mod.search.__defaults__[1], 2)  # `def search(..., k=RECALL_K)` was bound at load time
        by_arg = self.make(recall_k=3, min_score=0.6, extract_every_s=90, extract_batch=9, max_active=200)._mod
        self.assertEqual((by_arg.RECALL_K, by_arg.RECALL_MIN, by_arg.EXTRACT_EVERY_S, by_arg.EXTRACT_BATCH,
                          by_arg.MAX_ACTIVE), (3, 0.6, 90, 9, 200))
        self.assertEqual(by_arg.search.__defaults__[1], 3)
        default = self.make()._mod  # nothing given: the shared module's own defaults, not the old store's
        self.assertEqual((default.RECALL_K, default.RECALL_MIN, default.EXTRACT_BATCH), (6, 0.5, 60))

    def test_the_recall_count_limits_what_is_shown(self):
        st = self.make(env={"AI_HOUSE_MEMORY_RECALL_K": "2"})
        for i, what in enumerate(("红茶", "绿茶", "乌龙茶", "普洱茶")):
            st.add("nia", "private", f"房主最近很喜欢喝{what}，每天都要喝一杯{what}。", now=T0 + i)
        got = st.recall("nia", "private", "房主最近喜欢喝什么茶", now=T0 + 9)
        self.assertEqual(got.count("- #"), 2)
        self.assertEqual(st._mod.RECALL_K, 2)
        wide = st.recall("nia", "private", "房主最近喜欢喝什么茶", k=4, now=T0 + 9)
        self.assertEqual(wide.count("- #"), 4)
        self.assertEqual(st._mod.RECALL_K, 2)  # a call's own count does not stay

    def test_the_other_switches_have_an_environment_entry_and_a_code_default(self):
        base = self.make()._mod
        self.assertEqual((base.RULE_CONSUMERS, base.RECENT, base.URGENT, base.EVIDENCE, base.PUBLIC_SCRIBES), (set(),) * 2 + (False, False, set()))
        self.assertTrue(base.PUBLIC_EXPOSURE_GATE)
        self.assertFalse(base.DIARY_PRIVATE)  # the kit's own default: her diaries come to mind in every chat
        self.assertTrue(self.make(env={"AI_HOUSE_MEMORY_DIARY_PRIVATE": "1"})._mod.DIARY_PRIVATE)
        env = {"AI_HOUSE_MEMORY_RULES": "nia", "AI_HOUSE_MEMORY_RECENT": "all", "AI_HOUSE_MEMORY_RECENT_K": "99",
               "AI_HOUSE_MEMORY_RECENT_WINDOW_S": "60", "AI_HOUSE_MEMORY_URGENT": "1", "AI_HOUSE_MEMORY_URGENT_GAP_S": "10",
               "AI_HOUSE_MEMORY_EVIDENCE": "1", "AI_HOUSE_MEMORY_EVIDENCE_FOR": "nia", "AI_HOUSE_MEMORY_EVIDENCE_CHARS": "100",
               "AI_HOUSE_MEMORY_PUBLIC_SCRIBES": "xingli", "AI_HOUSE_MEMORY_DIARY_PRIVATE": "1",
               "AI_HOUSE_MEMORY_PUBLIC_EXPOSURE_GATE": "0"}
        mod = self.make(env=env)._mod
        self.assertEqual((mod.RULE_CONSUMERS, mod.RECENT, mod.RECENT_K, mod.RECENT_WINDOW_S), ({"nia"}, {"nia", "xingli"}, 40, 60))
        self.assertEqual((mod.URGENT, mod.URGENT_GAP_S, mod.EVIDENCE, mod.EVIDENCE_FOR, mod.EVIDENCE_CHARS), (True, 10, True, {"nia"}, 100))
        self.assertEqual((mod.PUBLIC_SCRIBES, mod.DIARY_PRIVATE, mod.PUBLIC_EXPOSURE_GATE), ({"xingli"}, True, False))

    def test_a_rule_the_owner_gives_in_private_needs_the_rules_switch(self):
        off, on = self.make(), self.make(env={"AI_HOUSE_MEMORY_RULES": "nia"})
        off.add("nia", "private", "房主要求：群友的不合理要求一律不答应。", kind="rule", now=T0)
        self.assertEqual(off.list_entries("nia", "private")[0]["kind"], "promise")
        on.add("nia", "private", "房主要求：群友的不合理要求一律不答应。", kind="rule", now=T0)
        self.assertEqual(on.list_entries("nia", "private")[0]["kind"], "rule")
        self.assertIn("一律不答应", on.recall("nia", "group", "群友的不合理要求", channel="qq-group:111", now=T0))

    def test_the_evidence_layer_can_be_switched_on(self):
        st = self.make(env={"AI_HOUSE_MEMORY_EVIDENCE": "1"})
        st.observe("nia", "private", OWNER_SAYS, now=T0)
        self.assertEqual(self.rows(st, "select count(*) n from seen_kw_docsize")[0]["n"], 1)


class ScheduleTests(StoreCase):
    def seed(self, st, chat, n=1, at=T0, text="周末谁想去爬山呀"):
        for i in range(n):
            st.observe("nia", "group", f"{text}{i}", channel=chat, speaker="阿甲", speaker_id="qq:20001", now=at)

    def test_a_chat_is_due_when_it_has_enough_lines_or_is_old_enough(self):
        model = FakeModel()
        st = self.make(llm_fn=model, extract_batch=3)
        self.seed(st, GROUP, 2)
        self.assertEqual(st.extract(now=T0 + 1)["buckets"], 0)
        self.seed(st, GROUP, 1, at=T0 + 2)
        self.assertEqual(st.extract(now=T0 + 3)["buckets"], 1)  # three lines
        self.seed(st, OTHER, 1, at=T0 + 4)
        self.assertEqual(st.extract(now=T0 + 1803)["buckets"], 0)
        self.assertEqual(st.extract(now=T0 + 1804)["buckets"], 1)  # the oldest line is 1800 s old
        self.assertEqual(len(model.calls), 2)

    def test_force_extracts_what_is_not_due(self):
        model = FakeModel()
        st = self.make(llm_fn=model)
        self.seed(st, GROUP, 1)
        self.seed(st, OTHER, 1)
        self.assertEqual(st.extract(actor="nia", scope="group", channel=GROUP, force=True, now=T0 + 1)["buckets"], 1)
        self.assertEqual(st.extract(actor="xingli", force=True, now=T0 + 1)["buckets"], 0)
        self.assertEqual(st.extract(scope="private", force=True, now=T0 + 1)["buckets"], 0)
        self.assertEqual(st.extract(force=True, now=T0 + 1)["buckets"], 1)
        self.assertEqual(len(model.calls), 2)

    def test_at_most_the_hourly_count_of_model_calls(self):
        model = FakeModel()
        st = self.make(llm_fn=model, env={"AI_HOUSE_MEMORY_EXTRACT_PER_HOUR": "1"})
        self.seed(st, GROUP)
        self.seed(st, OTHER)
        late = T0 + 2000
        self.assertEqual([st.extract(now=late)["buckets"], st.extract(now=late + 1)["buckets"]], [1, 0])
        self.assertEqual(st.extract(now=late + 3601)["buckets"], 1)
        self.assertEqual(len(model.calls), 2)

    def test_a_failing_model_is_left_alone_for_a_while_and_force_says_why(self):
        model = FakeModel(fail=RuntimeError("model down"))
        st = self.make(llm_fn=model)
        self.seed(st, GROUP)
        late = T0 + 2000
        self.assertEqual(st.extract(now=late)["buckets"], 0)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(st.extract(now=late + 239)["buckets"], 0)
        self.assertEqual(len(model.calls), 1)  # still backing off (240 s after the first failure)
        st.extract(now=late + 240)
        self.assertEqual(len(model.calls), 2)
        st.extract(now=late + 240 + 100)
        self.assertEqual(len(model.calls), 2)  # 480 s after the second
        with self.assertRaises(RuntimeError):
            st.extract(force=True, now=late + 241)
        self.assertEqual(self.rows(st, "select count(*) n from seen where done=0")[0]["n"], 1)  # lines kept
        model.fail = None
        self.assertEqual(st.extract(now=late + 1200)["buckets"], 0)  # the forced failure counted: 960 s after it
        self.assertEqual(st.extract(now=late + 1201)["buckets"], 1)
        self.assertEqual(self.rows(st, "select count(*) n from seen where done=0")[0]["n"], 0)

    def test_urgent_lines_do_not_wait(self):
        text = "记住下周三要交稿"
        calm = self.make(llm_fn=FakeModel())
        calm.observe("nia", "group", text, channel=GROUP, speaker="阿甲", now=T0)
        self.assertEqual(calm.extract(now=T0 + 5)["buckets"], 0)
        model = FakeModel()
        st = self.make(llm_fn=model, env={"AI_HOUSE_MEMORY_URGENT": "1"})
        st.observe("nia", "group", text, channel=GROUP, speaker="阿甲", now=T0)
        self.assertEqual(st.extract(now=T0 + 5)["buckets"], 1)
        st.observe("nia", "group", text, channel=GROUP, speaker="阿甲", now=T0 + 6)
        self.assertEqual(st.extract(now=T0 + 7)["buckets"], 0)  # the gap between urgent rounds is 300 s
        self.assertEqual(st.extract(now=T0 + 306)["buckets"], 1)

    def test_the_owner_going_quiet_in_private_is_urgent_too(self):
        st = self.make(llm_fn=FakeModel(), env={"AI_HOUSE_MEMORY_URGENT": "1"})
        st.observe("nia", "private", OWNER_SAYS, now=T0)
        self.assertEqual(st.extract(now=T0 + 89)["buckets"], 0)
        self.assertEqual(st.extract(now=T0 + 90)["buckets"], 1)

    def test_the_schedule_survives_a_names_reload(self):
        model = FakeModel()
        st = self.make(llm_fn=model, env={"AI_HOUSE_MEMORY_EXTRACT_PER_HOUR": "1"})
        self.seed(st, GROUP)
        self.seed(st, OTHER)
        self.assertEqual(st.extract(now=T0 + 2000)["buckets"], 1)
        st.names = {"nia": "妮娅", "xingli": "星璃", "mira": "米拉"}
        self.assertEqual(st.extract(now=T0 + 2001)["buckets"], 0)  # the hourly count was carried over

    def test_allow_shared_lets_a_group_extraction_write_shared_memories(self):
        ops = [add_op("阿甲是群里的摄影爱好者。", about=["阿甲"], scope="public")]
        plain, shared = self.make(llm_fn=FakeModel(ops)), self.make(llm_fn=FakeModel(ops))
        for st in (plain, shared):
            self.seed(st, GROUP)
        plain.extract(force=True, now=T0 + 1)
        shared.extract(force=True, allow_shared=True, now=T0 + 1)
        self.assertEqual(plain.list_entries("nia", "group")[0]["owner"], "nia")
        self.assertEqual(shared.list_entries("nia", "group")[0]["owner"], "shared")
        self.assertEqual(shared._mod.PUBLIC_SCRIBES, set())  # only for that call

    def test_extract_says_how_many_lines_it_took(self):
        st = self.make(llm_fn=FakeModel([add_op("阿甲想周末去爬山。", about=["阿甲"])]))
        self.seed(st, GROUP, 3)
        res = st.extract(force=True, now=T0 + 1)
        self.assertEqual(res, {"buckets": 1, "lines": 3, "added": 1, "merged": 0, "updated": 0, "invalidated": 0,
                               "rejected": 0, "refused": 0, "expired": 0})
        self.assertEqual(st.extract(force=True, now=T0 + 2)["buckets"], 0)


if __name__ == "__main__":
    unittest.main()
