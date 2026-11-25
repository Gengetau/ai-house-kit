"""M1 step 2a: `CommonMemoryStore` (ai_house.memory_common) answers the kit's calls with the shared memory module.

No network, no real model: the memory model is a fake function. Names and numbers are made up.
"""

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from test_m1_adapter_support import (
    ASK_PLAN, NAMES, NOTHING_FOUND, OWNER_SAYS, REPEAT_PLAN, SECRET_PLAN, T0, FakeModel, StoreCase, add_op,
    quiet_resource_warnings,
)

from ai_house.memory_common import CommonMemoryStore  # noqa: E402

GROUP = "qq-group:111"
LISTED = "qq-group:222"


def setUpModule():
    quiet_resource_warnings()


def copies(name: str):
    return [k for k in sys.modules if k.startswith(name)]


class IsolationTests(StoreCase):
    def test_two_stores_see_nothing_of_each_other_and_close_drops_the_copy(self):
        a, b = self.make(names={"nia": "妮娅"}), self.make(names={"xingli": "星璃"})
        self.assertIsNot(a._mod, b._mod)
        a.add("nia", "private", SECRET_PLAN, now=T0)
        self.assertIn("咖啡店", a.recall("nia", "private", ASK_PLAN, now=T0))
        self.assertEqual(b.recall("xingli", "private", ASK_PLAN, now=T0), NOTHING_FOUND)
        self.assertEqual(b.recall("nia", "private", ASK_PLAN, now=T0), NOTHING_FOUND)
        self.assertEqual(b.list_entries("nia", "private"), [])
        self.assertNotEqual(a.db_path, b.db_path)
        name_a, name_b = a._mod.__copy__, b._mod.__copy__
        self.assertTrue(copies(name_a))
        a.close()
        self.assertEqual(copies(name_a), [])
        self.assertTrue(copies(name_b))  # the other house keeps its copy
        b.close()
        self.assertEqual(copies(name_b), [])

    def test_the_old_database_is_never_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "memory.db"
            old.write_bytes(b"not a database")
            for given in (tmp, str(old)):
                st = self.make(given)
                st.add("nia", "private", SECRET_PLAN, now=T0)
                self.assertEqual(st.db_path, Path(tmp) / "memory-common.db")
            self.assertEqual(old.read_bytes(), b"not a database")

    def test_memories_and_numbers_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = CommonMemoryStore(tmp, names=NAMES, env={})
            first.add("nia", "private", SECRET_PLAN, now=T0)
            numbers = dict(first._numbers_by_key)
            first.close()
            second = self.make(tmp)
            self.assertEqual(second._numbers_by_key, numbers)
            self.assertIn("咖啡店", second.recall("nia", "private", ASK_PLAN, now=T0))
        for n in numbers.values():
            self.assertTrue(1000 <= n <= 8999)
        self.assertEqual(len(set(numbers.values())), len(numbers))


class PrivacyTests(StoreCase):
    def test_what_the_owner_said_in_private_comes_back_in_private_only(self):
        model = FakeModel([add_op(SECRET_PLAN, kind="event")])
        st = self.make(llm_fn=model)
        self.assertEqual(st.observe("nia", "private", OWNER_SAYS, speaker_role="owner", reply="好，我替你保密。", now=T0), 2)
        self.assertEqual(st.extract(now=T0 + 2000)["added"], 1)
        got = st.recall("nia", "private", ASK_PLAN, now=T0 + 2001)
        self.assertIn(SECRET_PLAN, got)
        self.assertEqual(st.recall("nia", "group", ASK_PLAN, channel=GROUP, now=T0 + 2001), NOTHING_FOUND)
        self.assertEqual(st.recall("nia", "group", ASK_PLAN, now=T0 + 2001), NOTHING_FOUND)
        self.assertEqual(st.recall("xingli", "private", ASK_PLAN, now=T0 + 2001), NOTHING_FOUND)

    def unified(self, **kw):
        return self.make(unified_groups=True, private_in_groups=True, leak_free_groups=[LISTED], **kw)

    def test_private_in_groups_marks_the_note_and_the_hush_line_only_where_not_listed(self):
        st = self.unified()
        st.add("nia", "private", SECRET_PLAN, about=["房主"], kind="event", now=T0)
        hush = st.recall("nia", "group", ASK_PLAN, channel=GROUP, now=T0)
        self.assertIn("·私密", hush)
        self.assertIn("别在这里说出来", hush)
        free = st.recall("nia", "group", ASK_PLAN, channel=LISTED, now=T0)
        self.assertIn("·私密", free)
        self.assertIn("想提可以自然地提", free)
        self.assertNotIn("别在这里说出来", free)
        self.assertEqual(st.recall("xingli", "group", ASK_PLAN, channel=GROUP, now=T0), NOTHING_FOUND)

    def test_outbound_check_stops_a_repeat_in_unlisted_groups_only(self):
        st = self.unified()
        st.add("nia", "private", SECRET_PLAN, about=["房主"], kind="event", now=T0)
        hit = st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=GROUP, now=T0)
        self.assertEqual(len(hit), 1)
        self.assertGreater(hit[0], 0)
        self.assertEqual(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel="", now=T0), hit)  # web group
        self.assertEqual(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=LISTED, now=T0), [])
        self.assertEqual(st.check_outbound_leak("nia", "private", REPEAT_PLAN, now=T0), [])
        self.assertEqual(st.check_outbound_leak("nia", "world", REPEAT_PLAN, now=T0), hit)
        self.assertEqual(st.check_outbound_leak("nia", "group", "今天天气不错", channel=GROUP, now=T0), [])
        self.assertEqual(st.check_outbound_leak("xingli", "group", REPEAT_PLAN, channel=GROUP, now=T0), [])

    def test_a_world_recall_sees_world_memories_and_her_diaries_and_no_chat(self):
        diary = "今天和房主一起收拾了阳台，种下了新买的薄荷，晚上聊了很久的旅行计划。"
        for unified in (False, True):
            with self.subTest(unified=unified):
                slots = {}

                def embed(texts):  # a memory (ends with 。) gets one of three axes, anything else all three
                    return [[float(slots.setdefault(t, len(slots) % 3) == i) for i in range(3)] if t.endswith("。")
                            else [1.0, 1.0, 1.0] for t in texts]

                st = self.make(unified_groups=unified, private_in_groups=True, leak_free_groups=["group"],
                               env={"AI_HOUSE_MEMORY_RECENT": "all"}, embed_plugin=embed)
                st.add("nia", "world", "小屋的规矩是每周六晚上一起大扫除。", now=T0)
                st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
                st.add("nia", "group", "阿甲在网页群里说他周末要去爬山。", about=["阿甲"], now=T0)
                st.add("nia", "group", "阿乙在另一个群里说她要去游泳。", channel=GROUP, about=["阿乙"], now=T0)
                st.add("nia", "world", f"2026-11-24 日记：{diary}", kind="diary", source="diary", now=T0)
                st.observe("nia", "private", "房主悄悄说下个月想换一份工作", now=T0)  # a line not yet tidied
                self.assertIn("大扫除", st.recall("nia", "world", "每周六晚上一起大扫除吗", now=T0 + 60))
                self.assertIn("薄荷", st.recall("nia", "world", "今天种下了新买的薄荷", now=T0 + 60))
                # (the made-up vectors above make every memory match every question, and no two memories kept in
                # the same place look alike: what is left out is left out by what may be seen, not by the score)
                for ask in (ASK_PLAN, "阿甲周末要去爬山吗", "阿乙要去游泳吗", "下个月想换一份工作"):
                    got = st.recall("nia", "world", ask, now=T0 + 60)
                    for word in ("咖啡店", "爬山", "游泳", "换一份工作", "私密"):
                        self.assertNotIn(word, got, ask)
                self.assertIn("咖啡店", st.recall("nia", "group", ASK_PLAN, now=T0 + 60))  # the view is back to normal

    def test_no_channel_or_a_malformed_one_is_never_exempt_from_the_outbound_check(self):
        st = self.make(leak_free_groups=["group", LISTED])
        st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        self.assertEqual(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel="group", now=T0), [])
        self.assertEqual(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=LISTED, now=T0), [])
        for ch in ("", "  ", "qq-group:2 22", "private:nia"):
            self.assertEqual(len(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=ch, now=T0)), 1, repr(ch))
        self.assertEqual(len(st.check_outbound_leak("nia", "world", REPEAT_PLAN, channel=LISTED, now=T0)), 1)

    def test_the_two_switches_are_read_by_the_old_names(self):
        st = self.make(env={"AI_HOUSE_MEMORY_UNIFIED_GROUPS": "1", "AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS": "1",
                            "AI_HOUSE_MEMORY_LEAK_FREE_GROUPS": f"group, {LISTED}, private:9, nonsense"})
        self.assertEqual(st.leak_free_groups, frozenset({"group", LISTED}))
        self.assertTrue(st.leak_free_in("group") and st.leak_free_in(LISTED) and st.leak_free_in(""))
        self.assertFalse(st.leak_free_in(GROUP) or st.leak_free_in("private:nia"))
        self.assertTrue(st.private_readable_in(GROUP) and not st.private_readable_in("private:nia"))
        st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        self.assertIn("想提可以自然地提", st.recall("nia", "group", ASK_PLAN, channel="", now=T0))  # empty = web group
        self.assertIn("别在这里说出来", st.recall("nia", "group", ASK_PLAN, channel=GROUP, now=T0))

    def test_private_in_groups_works_without_unified(self):
        # The two switches stand apart (decided 2026-11-24; before, the shared module needed both).
        st = self.make(private_in_groups=True)
        st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        st.add("nia", "group", "阿甲在另一个群里说他周末要去爬山。", channel=LISTED, about=["阿甲"], now=T0)
        hush = st.recall("nia", "group", ASK_PLAN, channel=GROUP, now=T0)
        self.assertIn("·私密", hush)
        self.assertIn("别在这里说出来", hush)
        self.assertEqual(st.recall("nia", "group", "阿甲周末要去爬山吗", channel=GROUP, now=T0), "")  # not unified
        self.assertEqual(st.recall("xingli", "group", ASK_PLAN, channel=GROUP, now=T0), NOTHING_FOUND)
        self.assertEqual(len(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=GROUP, now=T0)), 1)

    def test_outbound_check_runs_in_unlisted_groups_with_both_switches_off(self):
        # decided 2026-11-24: a group that is not listed is checked whatever the two switches say
        st = self.make(leak_free_groups=[LISTED])
        st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        self.assertEqual(st.recall("nia", "group", ASK_PLAN, channel=GROUP, now=T0), NOTHING_FOUND)
        self.assertEqual(len(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=GROUP, now=T0)), 1)
        self.assertEqual(st.check_outbound_leak("nia", "group", REPEAT_PLAN, channel=LISTED, now=T0), [])
        self.assertEqual(st.check_outbound_leak("nia", "private", REPEAT_PLAN, now=T0), [])
        self.assertEqual(st.check_outbound_leak("xingli", "group", REPEAT_PLAN, channel=GROUP, now=T0), [])

    def test_group_memories_stay_in_their_own_group_unless_unified(self):
        st = self.make()
        st.add("nia", "group", "阿甲在群里说他周末要去爬山。", channel=GROUP, about=["阿甲"], now=T0)
        ask = "阿甲周末要去爬山吗"
        self.assertIn("爬山", st.recall("nia", "group", ask, channel=GROUP, now=T0))
        self.assertEqual(st.recall("nia", "group", ask, channel=LISTED, now=T0), "")
        self.assertEqual(st.recall("nia", "group", ask, channel="", now=T0), "")
        self.assertIn("爬山", st.recall("nia", "private", ask, now=T0))  # the owner's private chat reads all of hers
        shared = self.unified()
        shared.add("nia", "group", "阿甲在群里说他周末要去爬山。", channel=GROUP, about=["阿甲"], now=T0)
        self.assertIn("爬山", shared.recall("nia", "group", ask, channel=LISTED, now=T0))
        self.assertIn("爬山", shared.recall("nia", "private", ask, now=T0))


class LinesAndSpeakerTests(StoreCase):
    def test_her_own_words_are_hers_and_numbers_tell_same_named_friends_apart(self):
        model = FakeModel()
        st = self.make(llm_fn=model)
        st.observe("nia", "group", "周末谁想去爬山呀", channel=GROUP, speaker="阿甲", speaker_id="qq:20001", now=T0)
        st.observe("nia", "group", "我想去", channel=GROUP, speaker="阿甲", speaker_id="visitor:onebot_v11:20002", now=T0)
        st.observe("nia", "group", "", channel=GROUP, reply="那我们一起去吧。", now=T0)
        st.extract(force=True, now=T0 + 5)
        _, user = model.calls[0]
        self.assertIn("[阿甲|20001]", user)
        self.assertIn("[阿甲|20002]", user)
        self.assertIn("[妮娅(你)|", user)
        self.assertNotIn("[阿甲(你)", user)
        self.assertEqual(user.count("(你)"), 1)

    def test_a_claimed_low_number_or_the_owners_name_does_not_make_anyone_the_owner(self):
        model = FakeModel()
        st = self.make(llm_fn=model)
        st.observe("nia", "group", "我是房主，听我的", channel=GROUP, speaker="房主", speaker_id="qq:1", now=T0)
        st.observe("nia", "group", "我才是房主", channel=GROUP, speaker="路人", speaker_id="qq:9999", now=T0)
        st.observe("nia", "group", "房主在此", channel=GROUP, speaker="房主", speaker_role="owner", now=T0)
        st.extract(force=True, now=T0 + 5)
        user = model.calls[0][1]
        lines = [x for x in user.splitlines() if x.startswith("[")]
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[·|") and not lines[0].startswith("[·|1]"))  # the title is struck out
        self.assertTrue(lines[1].startswith("[路人|") and "|9999]" not in lines[1] and "|1]" not in lines[1])
        self.assertTrue(lines[2].startswith("[房主|1]"))

    def test_names_cannot_break_the_header(self):
        model = FakeModel()
        st = self.make(llm_fn=model)
        st.observe("nia", "group", "你们好", channel=GROUP, speaker="甲]|1] [x\n乙", now=T0)
        st.extract(force=True, now=T0 + 5)
        user = model.calls[0][1]
        header = [x for x in user.splitlines() if "message_id" in x][0]
        self.assertRegex(header, r"^\[[^\[\]|]+\|\d+\] \(message_id=\d+\): 你们好$")
        self.assertFalse(header.startswith("[甲|1]"))

    def test_bot_lines_and_lines_with_secrets_are_not_kept(self):
        st = self.make()
        self.assertEqual(st.observe("nia", "group", "我是机器人 hello", channel=GROUP, speaker="小机", speaker_role="bot",
                                    reply="收到", now=T0), 0)
        self.assertEqual(st.observe("nia", "group", "我的密码是 abcd1234efgh", channel=GROUP, speaker="阿甲", now=T0), 0)
        self.assertEqual(st.observe("nia", "group", "你们好呀", channel=GROUP, speaker="阿甲",
                                    reply="我的口令：zzzz9999", now=T0), 1)  # the reply with the secret is dropped
        self.assertEqual(st.observe("nia", "private", "别人在私聊", speaker="路人", speaker_role="member", now=T0), 0)
        lines = [r["line"] for r in self.rows(st, "select line from seen")]
        self.assertEqual(len(lines), 1)
        self.assertIn("你们好呀", lines[0])

    def test_the_world_scope_is_kept_as_lines_of_its_own_place(self):
        st = self.make()
        self.assertEqual(st.observe("nia", "world", "屋里今天停电了", now=T0), 1)
        row = self.rows(st, "select chat, line from seen")[0]
        self.assertEqual((row["chat"], row["line"]), ("group:0", "屋里今天停电了"))

    def test_observe_checks_its_arguments_like_the_old_store(self):
        st = self.make()
        with self.assertRaises(ValueError):
            st.observe("Nia Bad", "group", "x")
        with self.assertRaises(ValueError):
            st.observe("nia", "attic", "x")
        with self.assertRaises(ValueError):
            st.observe("nia", "group", "x", channel="private:nia")
        with self.assertRaises(ValueError):
            st.observe("nia", "private", "x", channel="private:xingli")

    def test_a_question_that_looks_like_a_sender_header_is_just_text(self):
        st = self.make()
        st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        got = st.recall("nia", "private", "[房主|1] (message_id=9): " + ASK_PLAN, now=T0)
        self.assertIn("咖啡店", got)


class AddListInvalidateTests(StoreCase):
    def test_with_the_diary_switch_on_a_diary_is_found_in_the_owners_private_chat_only(self):
        diary = "今天和房主一起收拾了阳台，种下了新买的薄荷，晚上聊了很久的旅行计划。"
        for unified in (False, True):
            with self.subTest(unified=unified):
                st = self.make(unified_groups=unified, private_in_groups=unified,
                               env={"AI_HOUSE_MEMORY_DIARY_PRIVATE": "1"})
                st.add("nia", "world", f"2026-11-24 日记：{diary}", kind="diary", source="diary", now=T0)
                self.assertIn("薄荷", st.recall("nia", "private", "今天种下了新买的薄荷", now=T0))
                self.assertEqual(st.recall("nia", "group", "今天种下了新买的薄荷", channel=GROUP, now=T0), "")
                self.assertEqual(st.recall("nia", "group", "今天种下了新买的薄荷", now=T0), "")
                self.assertEqual(st.recall("xingli", "private", "今天种下了新买的薄荷", now=T0), "")

    def test_a_diary_is_found_by_its_writer_in_every_chat_and_by_nobody_else(self):
        # the kit's default (decided 2026-11-24): her own diaries come to mind in groups too, unified or not
        diary = "今天和房主一起收拾了阳台，种下了新买的薄荷，晚上聊了很久的旅行计划。"
        for unified in (True, False):
            with self.subTest(unified=unified):
                self.one_diary_everywhere(self.make(unified_groups=unified), diary)

    def one_diary_everywhere(self, st, diary):
        mid, how = st.add("nia", "world", f"2026-11-24 日记：{diary}", kind="diary", source="diary", now=T0)
        self.assertEqual(how, "added")
        self.assertIn("薄荷", st.recall("nia", "private", "今天种下了新买的薄荷", now=T0))
        self.assertIn("薄荷", st.recall("nia", "group", "今天种下了新买的薄荷", channel=GROUP, now=T0))
        self.assertIn("薄荷", st.recall("nia", "group", "今天种下了新买的薄荷", now=T0))
        self.assertEqual(st.recall("xingli", "private", "今天种下了新买的薄荷", now=T0), "")
        self.assertEqual(st.recall("xingli", "group", "今天种下了新买的薄荷", channel=GROUP, now=T0), "")
        self.assertEqual(st.recall("xingli", "group", "今天种下了新买的薄荷", now=T0), "")
        row = st.list_entries("nia", "world", now=T0)[0]
        self.assertEqual((row["id"], row["kind"], row["scope"], row["channel"], row["owner"]),
                         (mid, "diary", "world", "world", "nia"))
        self.assertEqual(st.list_entries("nia", "private"), [])
        self.assertEqual(st.list_entries("nia", "group"), [])
        out = st.invalidate("nia", "world", mid, reason="写错了", now=T0 + 5)
        self.assertEqual((out["id"], out["status"], out["invalidated_reason"], out["updated"]), (mid, "invalidated", "写错了", T0 + 5))
        self.assertEqual(st.list_entries("nia", "world"), [])
        self.assertEqual(st.recall("nia", "private", "今天种下了新买的薄荷", now=T0), "")
        with self.assertRaises(ValueError):
            st.invalidate("nia", "world", mid)

    def test_a_world_memory_is_hers_unless_it_is_saved_as_shared(self):
        st = self.make()
        own, _ = st.add("nia", "world", "妮娅今天把阳台的薄荷移到了窗边。", now=T0)
        both, _ = st.add("nia", "world", "小屋的规矩是每周六晚上一起大扫除。", shared=True, now=T0)
        self.assertIn("薄荷", st.recall("nia", "world", "阳台的薄荷移到了窗边吗", now=T0 + 60))
        self.assertNotIn("薄荷", st.recall("xingli", "world", "阳台的薄荷移到了窗边吗", now=T0 + 60))
        self.assertNotIn("薄荷", st.recall("xingli", "group", "阳台的薄荷移到了窗边吗", now=T0 + 60))
        self.assertNotIn("薄荷", st.recall("xingli", "private", "阳台的薄荷移到了窗边吗", now=T0 + 60))
        for who in ("nia", "xingli"):
            self.assertIn("大扫除", st.recall(who, "world", "每周六晚上一起大扫除吗", now=T0 + 60), who)
        self.assertEqual([(r["id"], r["owner"]) for r in st.list_entries("nia", "world")], [(both, "shared"), (own, "nia")])
        self.assertEqual([(r["id"], r["owner"]) for r in st.list_entries("xingli", "world")], [(both, "shared")])
        with self.assertRaises(ValueError):
            st.invalidate("xingli", "world", own)
        with self.assertRaises(ValueError):
            st.invalidate("nia", "world", both)  # a shared one needs allow_shared
        self.assertEqual(st.invalidate("nia", "world", own)["scope"], "world")

    def test_list_entries_has_the_old_keys_and_the_old_scopes(self):
        st = self.make()
        p, _ = st.add("nia", "private", SECRET_PLAN, about=["房主"], kind="event", now=T0)
        g, _ = st.add("nia", "group", "阿甲在群里说他周末要去爬山。", channel=GROUP, now=T0 + 1)
        s, _ = st.add("nia", "group", "群里约好每周五晚上一起看电影。", channel=GROUP, shared=True, kind="promise", now=T0 + 2)
        w, _ = st.add("nia", "world", "阳台的薄荷已经长出了新叶子。", now=T0 + 3)
        keys = {"id", "owner", "scope", "channel", "about", "kind", "trust", "text", "source", "status", "created",
                "updated", "last_used", "uses", "expires_at"}
        priv = st.list_entries("nia", "private", now=T0)
        self.assertEqual([r["id"] for r in priv], [p])
        self.assertEqual(set(priv[0]), keys)
        self.assertEqual((priv[0]["scope"], priv[0]["channel"], priv[0]["about"], priv[0]["created"]),
                         ("private", "private:nia", "房主", T0))
        grp = st.list_entries("nia", "group", channel=GROUP)
        self.assertEqual([(r["id"], r["owner"], r["scope"], r["channel"]) for r in grp],
                         [(s, "shared", "group", GROUP), (g, "nia", "group", GROUP)])
        self.assertEqual(st.list_entries("nia", "group", channel=LISTED), [])
        self.assertEqual([r["id"] for r in st.list_entries(None, "group")], [s, g])
        wor = st.list_entries("nia", "world")
        self.assertEqual([(r["id"], r["owner"], r["scope"], r["channel"]) for r in wor], [(w, "nia", "world", "world")])
        self.assertEqual([r["id"] for r in st.list_entries("nia", "world", kind="diary")], [])
        self.assertEqual([r["id"] for r in st.list_entries("nia", "group", kind="promise", channel=GROUP)], [s])
        self.assertEqual(len(st.list_entries("nia", "group", limit=1)), 1)
        with self.assertRaises(ValueError):
            st.list_entries(None, "private")
        with self.assertRaises(ValueError):
            st.list_entries("nia", "attic")

    def test_invalidate_keeps_to_the_scope_and_the_shared_switch(self):
        st = self.make()
        p, _ = st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        g, _ = st.add("nia", "group", "阿甲在群里说他周末要去爬山。", channel=GROUP, now=T0)
        s, _ = st.add("nia", "group", "群里约好每周五晚上一起看电影。", channel=GROUP, shared=True, now=T0)
        w, _ = st.add("nia", "world", "阳台的薄荷已经长出了新叶子。", now=T0)
        with self.assertRaises(ValueError):
            st.invalidate("nia", "group", s, channel=GROUP)  # a shared row needs allow_shared
        with self.assertRaises(ValueError):
            st.invalidate("nia", "group", p, channel=GROUP)  # a private memory is not reachable from a group
        with self.assertRaises(ValueError):
            st.invalidate("nia", "group", g, channel=LISTED)
        with self.assertRaises(ValueError):
            st.invalidate("nia", "world", g)
        with self.assertRaises(ValueError):
            st.invalidate("nia", "group", w, channel=GROUP, allow_shared=True)
        with self.assertRaises(ValueError):
            st.invalidate("nia", "private", s)  # shared rows are not reachable from the private scope
        with self.assertRaises(ValueError):
            st.invalidate("xingli", "private", p)
        with self.assertRaises(ValueError):
            st.invalidate("nia", "private", True)
        self.assertEqual(st.invalidate("nia", "group", s, channel=GROUP, allow_shared=True)["status"], "invalidated")
        self.assertEqual(st.invalidate("nia", "group", f"#{g}", channel=GROUP)["id"], g)
        self.assertEqual(st.invalidate("nia", "private", p, channel="private:nia")["scope"], "private")
        self.assertEqual(st.invalidate("nia", "world", w, allow_shared=True)["scope"], "world")
        for scope, ch in (("private", ""), ("group", GROUP), ("world", "")):
            self.assertEqual(st.list_entries("nia", scope, channel=ch), [])
        self.assertEqual(st.forget, st.invalidate)

    def test_the_owner_can_invalidate_her_group_memory_from_the_private_scope(self):
        st = self.make()
        g, _ = st.add("nia", "group", "阿甲在群里说他周末要去爬山。", channel=GROUP, now=T0)
        self.assertEqual(st.invalidate("nia", "private", g)["scope"], "group")

    def test_add_returns_how_and_refuses_short_or_secret_text(self):
        st = self.make()
        self.assertEqual(st.add("nia", "private", SECRET_PLAN, now=T0)[1], "added")
        mid, how = st.add("nia", "private", SECRET_PLAN, now=T0)
        self.assertEqual(how, "same")
        self.assertEqual(st.add("nia", "private", SECRET_PLAN, merge=False, now=T0)[1], "added")
        with self.assertRaises(ValueError):
            st.add("nia", "private", "太短", now=T0)
        with self.assertRaises(ValueError):
            st.add("nia", "private", "我的密码是 abcd1234efgh 请记住", now=T0)
        with self.assertRaises(ValueError):
            st.add("nia", "group", "阿甲说了一件事。", channel="private:nia")
        # accepted without effect: no trust levels, no expiry in the shared module
        self.assertEqual(st.add("nia", "private", "房主喜欢喝热的豆浆。", trust="hearsay", ttl_s=60, expires_at=T0 + 9, now=T0)[1], "added")
        self.assertIsNone(st.list_entries("nia", "private")[0]["expires_at"])

    def test_channels_without_a_number_get_one_that_lasts(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = CommonMemoryStore(tmp, names=NAMES, env={})
            st.add("nia", "group", "阿甲在这个频道说他周末要去爬山。", channel="telegram:abc", now=T0)
            st.add("nia", "group", "阿乙在网页群里说他要早睡。", channel="group", now=T0 + 1)
            st.add("nia", "group", "阿丙在群里说他要去游泳。", channel="qq-group:1", now=T0 + 2)
            st.close()
            again = self.make(tmp)
            got = {r["channel"]: r["text"] for r in again.list_entries("nia", "group")}
            self.assertEqual(set(got), {"telegram:abc", "group", "qq-group:1"})
            self.assertIn("爬山", again.recall("nia", "group", "阿甲周末要去爬山吗", channel="telegram:abc", now=T0))
            self.assertEqual(again.recall("nia", "group", "阿甲周末要去爬山吗", channel="group", now=T0), "")
            self.assertEqual(again.recall("nia", "group", "阿丙要去游泳吗", channel="group", now=T0), "")

    def test_a_vector_plugin_is_used_and_may_fail(self):
        def plugin(texts):
            return [[float(len(t) % 7 + 1), 1.0, float(sum(map(ord, t)) % 5)] for t in texts]

        st = self.make(embed_plugin=plugin)
        self.assertTrue(st.vector_enabled)
        mid, _ = st.add("nia", "private", SECRET_PLAN, now=T0)
        self.assertIsNotNone(self.rows(st, "select vec from mem where id=?", mid)[0]["vec"])
        self.assertIn("咖啡店", st.recall("nia", "private", SECRET_PLAN, now=T0))
        down = self.make(embed_plugin=lambda texts: None)
        down.add("nia", "private", SECRET_PLAN, now=T0)
        self.assertIn("咖啡店", down.recall("nia", "private", ASK_PLAN, now=T0))
        self.assertFalse(self.make().vector_enabled)

    def test_a_vector_plugin_gets_the_question_as_it_was_asked(self):
        got = []

        def plugin(texts):
            got.extend(texts)
            return [[1.0, 0.0] for _ in texts]

        st = self.make(embed_plugin=plugin)
        st.add("nia", "private", SECRET_PLAN, now=T0)
        del got[:]
        st.recall("nia", "private", ASK_PLAN, now=T0)
        self.assertEqual(got, [ASK_PLAN])  # no space put before it on the way


class WorldPlaceTests(StoreCase):
    """`world` memories (the house's own doings, said in no chat) are kept apart from every chat's memories."""

    RULE = "小屋的规矩是每周六晚上一起大扫除。"
    MINT = "妮娅今天把阳台的薄荷移到了窗边。"
    ASK_MINT = "阳台的薄荷移到了窗边吗"

    def test_the_same_words_as_world_and_in_the_web_group_stay_two_memories(self):
        for first, second in (("world", "group"), ("group", "world")):
            with self.subTest(first=first):
                st = self.make()
                a, how_a = st.add("nia", first, self.RULE, now=T0)
                b, how_b = st.add("nia", second, self.RULE, now=T0 + 1)
                self.assertEqual((how_a, how_b), ("added", "added"))
                self.assertEqual([r["id"] for r in st.list_entries("nia", first)], [a])
                self.assertEqual([r["id"] for r in st.list_entries("nia", second)], [b])
                st.invalidate("nia", second, b)  # taking one out leaves the other
                self.assertEqual([r["id"] for r in st.list_entries("nia", first)], [a])
                self.assertEqual(st.list_entries("nia", second), [])

    def test_a_world_memory_comes_to_mind_in_every_group_and_in_private_and_stays_hers(self):
        for unified in (False, True):
            with self.subTest(unified=unified):
                st = self.make(unified_groups=unified)
                st.add("nia", "world", self.MINT, now=T0)
                st.add("nia", "world", self.RULE, shared=True, now=T0)
                for scope, channel in (("group", ""), ("group", GROUP), ("private", ""), ("world", "")):
                    self.assertIn("薄荷", st.recall("nia", scope, self.ASK_MINT, channel=channel, now=T0 + 60), scope)
                    self.assertNotIn("薄荷", st.recall("xingli", scope, self.ASK_MINT, channel=channel, now=T0 + 60))
                    self.assertIn("大扫除", st.recall("xingli", scope, "每周六晚上一起大扫除吗", channel=channel,
                                                   now=T0 + 60), scope)

    def test_a_group_cannot_reword_or_remove_a_world_memory(self):
        st = self.make()
        mid, _ = st.add("nia", "world", self.MINT, now=T0)
        for channel in ("", GROUP):
            with self.assertRaises(ValueError):
                st.invalidate("nia", "group", mid, channel=channel)
            with self.assertRaises(ValueError):
                st.update("nia", "group", mid, "妮娅把薄荷送人了。", channel=channel)
        self.assertEqual([r["text"] for r in st.list_entries("nia", "world")], [self.MINT])

    def test_lines_are_tidied_in_the_place_they_were_noted(self):
        model = FakeModel([add_op("屋里今天停电了一个小时。", about=[])])
        st = self.make(llm_fn=model)
        st.observe("nia", "world", "屋里今天停电了一个小时", now=T0)
        st.observe("nia", "group", "大家晚上好，今天有人去爬山吗", speaker="阿甲", now=T0)
        self.assertEqual(st.extract("nia", scope="world", force=True, now=T0 + 5)["buckets"], 1)
        self.assertIn("停电", model.calls[0][1])
        self.assertNotIn("爬山", model.calls[0][1])
        self.assertEqual([r["scope"] for r in st.list_entries("nia", "world")], ["world"])
        self.assertEqual(st.list_entries("nia", "group"), [])
        self.assertEqual(self.rows(st, "select count(*) n from seen where done=0 and chat='group:1'")[0]["n"], 1)
        self.assertEqual(st.extract("nia", scope="group", force=True, now=T0 + 9)["buckets"], 1)
        self.assertIn("爬山", model.calls[-1][1])
        self.assertNotIn("停电了一个小时", model.calls[-1][1].split("聊天记录")[-1])

    def test_a_database_from_when_world_rows_sat_in_the_web_group_is_put_right_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = self.make(tmp.name)
        w, _ = st.add("nia", "world", self.MINT, now=T0)
        g, _ = st.add("nia", "group", "阿甲在网页群里说他周末要去爬山。", about=["阿甲"], now=T0)
        st.close()
        conn = sqlite3.connect(str(st.db_path))  # the way such a database looked: a marker table, one shared chat
        with conn:
            conn.execute("create table kit_origin(mem_id integer primary key, scope text not null)")
            conn.execute("insert into kit_origin values (?, 'world')", (w,))
            conn.execute("update mem set chat='group:1' where id=?", (w,))
        conn.close()
        for _ in range(2):
            st = self.make(tmp.name)
            self.assertEqual([r["id"] for r in st.list_entries("nia", "world")], [w])
            self.assertEqual([r["id"] for r in st.list_entries("nia", "group")], [g])
            self.assertEqual(self.rows(st, "select name from sqlite_master where name='kit_origin'"), [])
            st.close()


class OldInterfaceTests(StoreCase):
    """Methods the old store had that callers and tools may still use."""

    def test_update_rewords_a_memory_from_its_own_scope_only(self):
        st = self.make()
        gid, _ = st.add("nia", "group", "阿甲周末要去爬山。", channel=GROUP, about=["阿甲"], now=T0)
        pid, _ = st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        out = st.update("nia", "group", gid, "阿甲周末改去游泳了。", channel=GROUP, now=T0 + 5)
        self.assertEqual((out["id"], out["text"], out["scope"], out["channel"]), (gid, "阿甲周末改去游泳了。", "group", GROUP))
        self.assertEqual(self.rows(st, "select prev from mem where id=?", gid)[0]["prev"], "阿甲周末要去爬山。")
        with self.assertRaises(ValueError):  # nothing said in private is written into a row a group can read
            st.update("nia", "private", gid, "房主私下说阿甲其实不想去。", now=T0 + 6)
        with self.assertRaises(ValueError):
            st.update("nia", "group", pid, "房主的打算改了。", channel=GROUP, now=T0 + 6)
        with self.assertRaises(ValueError):
            st.update("xingli", "private", pid, "房主的打算改了。", now=T0 + 6)
        st.update("nia", "private", pid, "房主打算明年春天再辞职去开咖啡店。", expires_at=T0 + 3600, now=T0 + 7)
        self.assertEqual(st.list_entries("nia", "private", now=T0 + 8)[0]["expires_at"], T0 + 3600)
        self.assertEqual(st.invalidate("nia", "private", gid, now=T0 + 9)["status"], "invalidated")  # taking out is allowed

    def test_search_sweep_and_backfill(self):
        st = self.make()
        st.add("nia", "private", "房主对花生过敏。", about=["房主"], ttl_s=600, now=T0)
        st.add("nia", "group", "阿甲周末要去爬山。", channel=GROUP, about=["阿甲"], now=T0)
        hits = st.search("nia", "private", "我对什么过敏", now=T0 + 1)
        self.assertEqual([(r["scope"], r["text"]) for _, r in hits], [("private", "房主对花生过敏。")])
        self.assertGreaterEqual(hits[0][0], 0.5)
        self.assertEqual(st.search("xingli", "private", "我对什么过敏", now=T0 + 1), [])
        self.assertEqual(st.sweep_expired(now=T0 + 599), 0)
        self.assertEqual(st.sweep_expired(now=T0 + 601), 1)
        self.assertEqual(st.search("nia", "private", "我对什么过敏", now=T0 + 602), [])
        self.assertEqual(st.backfill_vectors(), 0)  # no vector service
        with_vectors = self.make(data_dir=str(st.db_path.parent), embed_plugin=lambda texts: [[1.0, 0.5] for _ in texts])
        self.assertEqual(with_vectors.backfill_vectors(), 1)
        self.assertEqual(with_vectors.backfill_vectors(), 0)


class NoVectorServiceTests(StoreCase):
    """A house with no vector service (the kit's default) still remembers when asked in plain words."""

    def test_a_plain_question_finds_the_fact(self):
        st = self.make()
        self.assertFalse(st.vector_enabled)
        st.add("nia", "private", "房主对花生过敏。", about=["房主"], now=T0)
        self.assertIn("花生", st.recall("nia", "private", "你还记得我对什么过敏吗？", now=T0))
        self.assertEqual(st.recall("nia", "private", "你记得我喜欢什么颜色吗？", now=T0), NOTHING_FOUND)
        self.assertEqual(st.recall("nia", "private", "今天天气真不错", now=T0), "")

    def test_what_the_owner_just_said_in_private_comes_to_mind_in_a_group_only_with_the_switch(self):
        said = "下个月我打算辞职去开咖啡店"
        for on in (True, False):
            with self.subTest(private_in_groups=on):
                st = self.make(private_in_groups=on)
                st.observe("nia", "private", said, now=T0)  # not yet tidied into a memory
                got = st.recall("nia", "group", "房主打算辞职去开咖啡店吗", channel=GROUP, now=T0 + 60)
                if on:
                    self.assertIn("咖啡店", got)
                    self.assertIn("别在群里说出来", got)
                else:
                    self.assertNotIn("咖啡店", got)
                self.assertNotIn("咖啡店", st.recall("xingli", "group", "房主打算辞职去开咖啡店吗", channel=GROUP, now=T0 + 60))


class DecidedExtrasTests(StoreCase):
    """The four things the old store had that went into the shared file (decided 2026-11-24), through the adapter."""

    def test_a_question_about_what_she_remembers_that_finds_nothing_gets_the_note(self):
        from legacy_memory_store import NO_MEMORY_NOTE  # the earlier store's wording, carried over word for word
        self.assertEqual(NOTHING_FOUND, NO_MEMORY_NOTE)
        st = self.make()
        self.assertEqual(st.recall("nia", "private", ASK_PLAN, now=T0), NOTHING_FOUND)
        self.assertEqual(st.recall("nia", "group", ASK_PLAN, channel=GROUP, now=T0), NOTHING_FOUND)
        self.assertEqual(st.recall("nia", "private", "今天晚饭吃咖喱好不好", now=T0), "")  # not such a question
        st.add("nia", "private", SECRET_PLAN, kind="event", now=T0)
        self.assertNotIn("没有找到", st.recall("nia", "private", ASK_PLAN, now=T0))
        off = self.make(env={"AI_HOUSE_MEMORY_NO_MEMORY_NOTE": "0"})
        self.assertEqual(off.recall("nia", "private", ASK_PLAN, now=T0), "")

    def test_a_memory_with_a_time_limit_is_gone_after_it(self):
        st = self.make()
        st.add("nia", "private", "房主这周五要去医院复查。", kind="event", ttl_s=3600, now=T0)
        st.add("nia", "private", "房主下周要去看牙医。", kind="event", expires_at=T0 + 7200, now=T0)
        st.add("nia", "private", "房主养了一只叫团子的猫。", now=T0)
        ask = "[房主|1] (message_id=9): 这周五去医院复查、下周看牙医、团子猫"
        both = st.recall("nia", "private", ask, now=T0 + 60)
        self.assertIn("复查", both)
        self.assertIn("牙医", both)
        self.assertEqual({r["expires_at"] for r in st.list_entries("nia", "private", now=T0 + 60)}, {T0 + 3600, T0 + 7200, None})
        later = st.recall("nia", "private", ask, now=T0 + 3601)
        self.assertNotIn("复查", later)
        self.assertIn("牙医", later)
        self.assertEqual(len(st.list_entries("nia", "private", now=T0 + 7201)), 1)  # listing leaves them out too
        cat = "[房主|1] (message_id=9): 房主养了一只叫团子的猫吗"
        self.assertIn("团子", st.recall("nia", "private", cat, now=T0 + 10 ** 7))  # no limit: kept for good
        with self.assertRaises(ValueError):
            st.add("nia", "private", "房主明天要早起赶飞机。", ttl_s=0, now=T0)
        with self.assertRaises(ValueError):
            st.add("nia", "private", "房主明天要早起赶飞机。", expires_at=T0 - 1, now=T0)

    def test_trust_levels_are_off_unless_switched_on(self):
        off = self.make()
        off.add("nia", "group", "群友说房主其实不喜欢吃咖喱。", channel=GROUP, kind="claim", trust="hearsay", now=T0)
        got = off.recall("nia", "group", "房主不喜欢吃咖喱吗", channel=GROUP, now=T0)
        self.assertIn("咖喱", got)
        self.assertNotIn("未核实", got)
        self.assertEqual(off.list_entries("nia", "group", channel=GROUP)[0]["trust"], "")
        on = self.make(env={"AI_HOUSE_MEMORY_TRUST": "1"})
        on.add("nia", "group", "群友说房主其实不喜欢吃咖喱。", channel=GROUP, kind="claim", now=T0)
        on.add("nia", "group", "房主周末要去爬山。", channel=GROUP, now=T0)
        self.assertIn("群聊陈述·未核实", on.recall("nia", "group", "房主不喜欢吃咖喱吗", channel=GROUP, now=T0))
        self.assertNotIn("未核实", on.recall("nia", "group", "房主周末要去爬山吗", channel=GROUP, now=T0))
        self.assertEqual({r["trust"] for r in on.list_entries("nia", "group", channel=GROUP)}, {"group_stated", "verified"})

    def test_with_trust_on_the_claim_kind_is_kept_and_a_conflicting_hit_is_flagged(self):
        for env, kind, flagged in (({}, "fact", False), ({"AI_HOUSE_MEMORY_TRUST": "1"}, "claim", True)):
            st = self.make(env=env)
            st.add("nia", "group", "房主对花生严重过敏，绝对不能吃", channel=GROUP, about=["房主"], trust="verified", now=T0 - 86400)
            st.add("nia", "group", "群友说房主其实对花生不过敏，随便吃", channel=GROUP, about=["房主"], kind="claim", now=T0)
            hits = st.search("nia", "group", "房主能吃花生吗", channel=GROUP, min_score=0.05, now=T0)
            self.assertEqual(sorted({r["kind"] for _, r in hits}), sorted({"fact", kind}))
            self.assertEqual(any(r.get("_conflict_verified") for _, r in hits), flagged)


class NamesAndModelTests(StoreCase):
    def test_assigning_names_keeps_what_was_written(self):
        model = FakeModel()
        st = self.make(llm_fn=model, names={"nia": "妮娅"})
        st.add("nia", "private", SECRET_PLAN, now=T0)
        before, numbers = st._mod, dict(st._numbers_by_key)
        name = before.__copy__
        st.names = {"nia": "尼娅", "xingli": "星璃"}
        self.assertIsNot(st._mod, before)
        self.assertEqual(copies(name), [])  # the old copy is gone
        self.assertEqual(st._mod.NAMES, {"nia": "尼娅", "xingli": "星璃"})
        self.assertEqual(st._numbers_by_key["nia"], numbers["nia"])
        self.assertIn("咖啡店", st.recall("nia", "private", ASK_PLAN, now=T0))
        st.observe("nia", "private", OWNER_SAYS, reply="好的。", now=T0)
        st.extract(force=True, now=T0 + 5)
        self.assertIn("[尼娅(你)|", model.calls[0][1])
        before = st._mod
        st.names = dict(st.names)  # the same names again: nothing to reload
        self.assertIs(st._mod, before)

    def test_a_housemate_the_names_did_not_list_is_still_served(self):
        st = self.make(names={})
        st.add("nia", "private", SECRET_PLAN, now=T0)
        self.assertIn("咖啡店", st.recall("nia", "private", ASK_PLAN, now=T0))
        self.assertEqual(st.recall("xingli", "private", ASK_PLAN, now=T0), NOTHING_FOUND)

    def test_the_narrow_api_passes_everything_through(self):
        model = FakeModel([add_op(SECRET_PLAN)])
        st = self.make(llm_fn=model)
        api = st.narrow_api()
        self.assertEqual(api.observe("nia", "private", OWNER_SAYS, speaker_role="owner", now=T0), 1)
        self.assertEqual(api.extract(actor="nia", force=True, now=T0 + 5)["added"], 1)
        self.assertIn("咖啡店", api.recall("nia", "private", ASK_PLAN, k=3, now=T0 + 6))

    def test_without_a_model_the_lines_wait(self):
        st = self.make()
        st.observe("nia", "private", OWNER_SAYS, now=T0)
        self.assertEqual(st.extract(force=True, now=T0 + 5)["buckets"], 0)
        self.assertEqual(self.rows(st, "select count(*) n from seen where done=0")[0]["n"], 1)

    def test_a_model_that_answers_nothing_leaves_the_line_out_not_the_queue_stuck(self):
        calls = []

        def empty(system, user):
            calls.append(user)
            return ""

        st = self.make(llm_fn=empty)
        st.observe("nia", "private", OWNER_SAYS, now=T0)
        res = st.extract(force=True, now=T0 + 5)
        self.assertEqual(res["refused"], 1)
        self.assertEqual(len(calls), 2)  # one line is asked twice before it is left out
        self.assertEqual(self.rows(st, "select count(*) n from seen where done=0")[0]["n"], 0)

    def test_the_model_router_is_used_when_it_has_an_endpoint(self):
        class Result:
            content = '{"ops": [{"op": "add", "text": "房主喜欢喝热的豆浆。", "about": ["房主"], "kind": "preference"}]}'
            completion_tokens = 20

        class Router:
            def __init__(self):
                self.seen = []

            def has_live_endpoint(self, *, purpose, actor=""):
                return True

            def complete_messages(self, messages, *, purpose, actor=""):
                self.seen.append((purpose, actor))
                return Result()

        router = Router()
        st = self.make(model_router=router)
        st.observe("nia", "private", "我喜欢喝热的豆浆。", now=T0)
        self.assertEqual(st.extract(force=True, now=T0 + 5)["added"], 1)
        self.assertEqual(router.seen, [("memory_extract", "nia")])
        self.assertIn("豆浆", st.recall("nia", "private", "房主喜欢喝热的豆浆吗", now=T0 + 6))


class ThreadTests(StoreCase):
    def test_each_thread_stamps_its_own_now(self):
        st = self.make()
        st.observe("nia", "private", "先让它加载一下。", now=T0)
        barrier = threading.Barrier(2)

        def work(tag: str, now: float):
            barrier.wait()
            for i in range(10):
                st.observe("nia", "private", f"{tag}说的第{i}句话", now=now)

        threads = [threading.Thread(target=work, args=("甲", T0 + 100)), threading.Thread(target=work, args=("乙", T0 + 200))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for r in self.rows(st, "select ts, line from seen where line like '%说的第%'"):
            self.assertEqual(r["ts"], T0 + 100 if "甲说" in r["line"] else T0 + 200)


if __name__ == "__main__":
    unittest.main()
