"""Two independent switches: `AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS=1` (she can recall her OWN private-chat memories in any
group, told to keep them to herself) and `AI_HOUSE_MEMORY_LEAK_FREE_GROUPS` (listed groups skip the outbound leak
checks and may mention them). Both empty = the old strict isolation, byte for byte.

Nothing here calls a real model: the extractor and the chat model are fakes, the QQ side is a local fake OneBot server,
every number and name is made up.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import cli  # noqa: E402
from ai_house.chat_guard import ReplyGuard  # noqa: E402
from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon  # noqa: E402
from ai_house.memory import (  # noqa: E402
    JST,
    NO_MEMORY_NOTE,
    LEAK_FREE_GROUPS_ENV,
    PRIVATE_IN_GROUP_NOTE_FREE,
    PRIVATE_IN_GROUP_NOTE_HUSH,
    PRIVATE_IN_GROUPS_ENV,
    MemoryStore,
    parse_leak_free_groups,
)
from worldkernel.outbox import BlockedDelivery  # noqa: E402

from test_realrun_1_promises import PACK, T0, _CapturingModel  # noqa: E402

ENV = LEAK_FREE_GROUPS_ENV
READ = PRIVATE_IN_GROUPS_ENV
PRIVATE_IN_GROUP_NOTE = PRIVATE_IN_GROUP_NOTE_FREE
DAY1 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
DAY2 = dt.datetime(2026, 11, 22, 20, 0, tzinfo=JST).timestamp()
NAMES = {"nell": "奈尔", "yuki": "由纪"}

NELL_SECRET = "房主打算下个月辞职去开咖啡店，要求奈尔保密。"
YUKI_SECRET = "房主在阳台的花盆底下藏了一把备用钥匙。"
PLAN_NO_OVERLAP = "房主准备下个月去开一家咖啡店，叮嘱奈尔保密。"  # shares no word with 「你知道我最近的打算吗」
ASK_PLAN = "奈尔，你知道我打算辞职去开咖啡店的事吗？"
ASK_KEY = "由纪，你知道我把备用钥匙放哪了吗？"
OWNER_PRIVATE_LINE = "我打算下个月辞职去开咖啡店，千万别告诉别人哦。"
REPEAT = "你说过打算下个月辞职去开咖啡店，我都记着呢。"  # shares 12+ verbatim characters with the line above


class _Base(unittest.TestCase):
    def make(self, groups: str = "", read: Optional[bool] = None, **kw: Any) -> MemoryStore:
        """`groups` = AI_HOUSE_MEMORY_LEAK_FREE_GROUPS; `read` = AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS (default: on when
        `groups` is given, i.e. the combination that makes a group fully open)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {ENV: groups}
        if (bool(groups) if read is None else read):
            env[READ] = "1"
        st = MemoryStore(tmp.name, names=NAMES, env=env, **kw)
        self.addCleanup(st.close)
        return st

    def seed(self, st: MemoryStore) -> None:
        st.add("nell", "private", NELL_SECRET, channel="private:nell", about=["房主"], kind="event", now=DAY1)
        st.add("yuki", "private", YUKI_SECRET, channel="private:yuki", about=["房主"], kind="fact", now=DAY1)


# ---------------------------------------------------------------------------------------------------------------
# 1. configuration: one spelling, parsed in one place
# ---------------------------------------------------------------------------------------------------------------
class ConfigParsing(_Base):
    def test_valid_items_whitespace_case_and_duplicates(self) -> None:
        ok, bad = parse_leak_free_groups("  group , qq-group:123,\nqq-group: 456 ，GROUP, Qq-Group:123 ;qq-group :789")
        self.assertEqual(ok, ["group", "qq-group:123", "qq-group:456", "qq-group:789"])
        self.assertEqual(bad, [])

    def test_empty_and_none_mean_nothing(self) -> None:
        for raw in ("", "   ", None, ",,, ,"):
            self.assertEqual(parse_leak_free_groups(raw), ([], []))

    def test_malformed_items_are_rejected_not_guessed(self) -> None:
        raw = "private,private:nell,privateX,qq-group:,qq-group:abc,qq-group:-1,123,qq:123,qq-group:12x3,world,qq-group:123"
        ok, bad = parse_leak_free_groups(raw)
        self.assertEqual(ok, ["qq-group:123"])
        self.assertIn("private", bad)
        self.assertIn("private:nell", bad)
        self.assertIn("123", bad)
        self.assertIn("qq-group:abc", bad)
        self.assertIn("world", bad)

    def test_store_ignores_bad_items_and_keeps_good_ones(self) -> None:
        st = self.make("private:nell,qq-group:abc,group,nonsense")
        self.assertEqual(st.leak_free_groups, frozenset({"group"}))
        self.assertFalse(st.leak_free_in("private:nell"))
        self.assertFalse(st.leak_free_in("private"))
        self.assertFalse(st.leak_free_in("qq-group:abc"))

    def test_the_one_decision_point(self) -> None:
        st = self.make("group, qq-group:123")
        self.assertTrue(st.leak_free_in("group"))
        self.assertTrue(st.leak_free_in(""), "an empty channel is the web group, as in recall")
        self.assertTrue(st.leak_free_in("qq-group:123"))
        self.assertFalse(st.leak_free_in("qq-group:1234"))
        self.assertFalse(st.leak_free_in("qq-group:12"))
        self.assertFalse(st.leak_free_in("webchat"))
        self.assertFalse(st.leak_free_in("private:nell"))
        self.assertFalse(st.leak_free_in("onebot_v11"))

    def test_only_qq_group_listed_does_not_open_the_web_group(self) -> None:
        st = self.make("qq-group:123")
        self.assertFalse(st.leak_free_in("group"))
        self.assertFalse(st.leak_free_in(""))

    def test_default_is_closed_everywhere(self) -> None:
        st = self.make("")
        for ch in ("group", "", "qq-group:123", "private:nell"):
            self.assertFalse(st.leak_free_in(ch))
        self.assertEqual(st.leak_free_groups, frozenset())

    def test_read_from_the_env_the_store_was_given_like_the_other_memory_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {ENV: "qq-group:777"}):
                st = MemoryStore(tmp)  # env=None: the process environment, read once at construction
                self.addCleanup(st.close)
            self.assertTrue(st.leak_free_in("qq-group:777"))
            self.assertIn("777", ",".join(st.leak_free_groups))

    def test_one_spelling_across_recall_outbound_and_config(self) -> None:
        """The channel ids the daemon passes (`channel` in `_converse`, OneBot's `qq-group:<gid>`, send_gate's helper)
        are the very strings the config lists."""
        from ai_house.send_gate import group_channel_of

        st = self.make("group,qq-group:50001")
        self.assertEqual(group_channel_of("webchat", "group", is_webchat=True), "group")
        self.assertEqual(group_channel_of("webchat", "", is_webchat=True), "group")
        self.assertEqual(group_channel_of("onebot_v11", "50001", is_webchat=False), "qq-group:50001")
        self.assertTrue(st.leak_free_in(group_channel_of("onebot_v11", "50001", is_webchat=False)))
        self.assertFalse(st.leak_free_in(group_channel_of("onebot_v11", "50002", is_webchat=False)))
        self.assertFalse(st.leak_free_in(group_channel_of("some_other_channel", "50001", is_webchat=False)))


class CliValidationAndTemplate(unittest.TestCase):
    def _up(self, value: str) -> tuple:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / ".env"
        env_file.write_text(f"{ENV}={value}\n", encoding="utf-8")
        err = io.StringIO()
        with patch.dict(os.environ, {}, clear=False), contextlib.redirect_stderr(err):
            os.environ.pop(ENV, None)
            with patch.object(cli, "HouseDaemon", side_effect=cli.DaemonLockError("stop here")) as daemon_cls:
                rc = cli.main(["up", "--env-file", str(env_file), "--data-dir", str(Path(tmp.name) / "rt"), "--port", "0"])
            os.environ.pop(ENV, None)
        return rc, err.getvalue(), daemon_cls

    def test_up_refuses_to_start_on_a_malformed_entry(self) -> None:
        for bad in ("private:nell", "private", "qq-group:abc,group", "12345", "group;qq-group:", "world"):
            with self.subTest(value=bad):
                rc, err, daemon_cls = self._up(bad)
                self.assertEqual(rc, 2)
                self.assertIn(ENV, err)
                daemon_cls.assert_not_called()

    def test_up_accepts_empty_and_well_formed_values(self) -> None:
        for good in ("", "group", " group , qq-group:123 ,group", "qq-group: 99"):
            with self.subTest(value=good):
                rc, err, daemon_cls = self._up(good)
                daemon_cls.assert_called_once()  # got past the check and tried to build the daemon
                self.assertNotIn(ENV, err)

    def test_doctor_reports_the_same_problem(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / ".env"
        env_file.write_text(f"{ENV}=private:nell\n", encoding="utf-8")
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(out):
            os.environ.pop(ENV, None)
            cli.main(["doctor", "--env-file", str(env_file), "--data-dir", str(Path(tmp.name) / "rt"), "--json"])
        import json

        checks = {c["name"]: c for c in json.loads(out.getvalue())["checks"]}
        self.assertFalse(checks["leak_free_groups"]["ok"])

    def test_init_template_carries_the_setting_with_a_comment(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file = Path(tmp.name) / ".env"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["init", "--env-file", str(env_file), "--data-dir", str(Path(tmp.name) / "rt")])
        self.assertEqual(rc, 0)
        lines = env_file.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{ENV}=", lines)
        self.assertIn(f"{READ}=0", lines)
        idx = lines.index(f"{READ}=0")
        self.assertTrue(lines[idx - 1].startswith("#"), "one line of explanation above the settings")
        self.assertEqual(lines.index(f"{ENV}="), idx + 1)
        vals, errs, _ = cli._parse_dotenv_file(env_file)
        self.assertEqual(errs, [])
        self.assertEqual(vals.get(ENV, ""), "")


# ---------------------------------------------------------------------------------------------------------------
# 2. default (empty) = exactly the old behaviour
# ---------------------------------------------------------------------------------------------------------------
class DefaultIsUnchanged(_Base):
    BASE = "status='active' AND (expires_at IS NULL OR expires_at>?)"

    def test_read_sql_is_byte_identical_to_the_old_one(self) -> None:
        st = self.make("")
        want_group = (
            f"{self.BASE} AND ((owner IN (?, 'shared') AND scope='group' AND channel=?) "
            "OR (owner IN (?, 'shared') AND scope='world'))"
        )
        self.assertEqual(st._visible_sql("nell", "group", "qq-group:1", 5.0), (want_group, [5.0, "nell", "qq-group:1", "nell"]))
        want_uni = (
            f"{self.BASE} AND ((owner=? AND scope='group') OR (owner='shared' AND scope='group' AND channel=?) "
            "OR (owner IN (?, 'shared') AND scope='world'))"
        )
        uni = self.make("", unified_groups=True)
        self.assertEqual(uni._visible_sql("nell", "group", "qq-group:1", 5.0), (want_uni, [5.0, "nell", "qq-group:1", "nell"]))
        want_priv = (
            f"{self.BASE} AND ((owner=? AND scope='private' AND channel=?) "
            "OR (owner IN (?, 'shared') AND scope IN ('group', 'world')))"
        )
        self.assertEqual(st._visible_sql("nell", "private", "private:nell", 5.0), (want_priv, [5.0, "nell", "private:nell", "nell"]))

    def test_group_recall_never_shows_private_memory(self) -> None:
        st = self.make("")
        self.seed(st)
        for ch in ("group", "qq-group:123", ""):
            got = st.recall("nell", "group", ASK_PLAN, channel=ch, now=DAY2)
            self.assertNotIn("咖啡店", got)
            self.assertNotIn(PRIVATE_IN_GROUP_NOTE, got)
            self.assertEqual(got, NO_MEMORY_NOTE)

    def test_outbound_check_still_blocks_with_or_without_a_channel(self) -> None:
        st = self.make("")
        self.seed(st)
        for ch in ("", "group", "qq-group:123"):
            self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, channel=ch, now=DAY2))
        self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, now=DAY2))

    def test_reply_guard_without_a_listed_group_blocks_even_with_an_actor(self) -> None:
        st = self.make("")
        g = ReplyGuard({}, leak_free=st.leak_free_in)
        g.note_private(OWNER_PRIVATE_LINE, actor="nell")
        self.assertEqual(g.check("group", REPEAT, actor="nell"), "private_content")
        self.assertEqual(g.check("group", REPEAT), "private_content")
        self.assertIsNone(g.check(f"private:nell", REPEAT, actor="nell"))


# ---------------------------------------------------------------------------------------------------------------
# 3. reads in a listed group
# ---------------------------------------------------------------------------------------------------------------
class ReadInListedGroup(_Base):
    def test_she_sees_her_own_private_memory_marked_as_such(self) -> None:
        st = self.make("group,qq-group:123")
        self.seed(st)
        for ch in ("group", "qq-group:123"):
            got = st.recall("nell", "group", ASK_PLAN, channel=ch, now=DAY2)
            self.assertIn("咖啡店", got)
            self.assertIn("[私密·", got)
            self.assertIn(PRIVATE_IN_GROUP_NOTE, got)
            self.assertIn("小屋自己的群", PRIVATE_IN_GROUP_NOTE)

    def test_she_never_sees_another_roommates_private_memory(self) -> None:
        st = self.make("group")
        self.seed(st)
        self.assertNotIn("花盆", st.recall("nell", "group", ASK_KEY, channel="group", now=DAY2))
        self.assertNotIn("花盆", st.recall("nell", "group", "房主在阳台的花盆底下藏了备用钥匙", channel="group", now=DAY2))
        self.assertIn("花盆", st.recall("yuki", "group", ASK_KEY, channel="group", now=DAY2))
        self.assertNotIn("咖啡店", st.recall("yuki", "group", ASK_PLAN, channel="group", now=DAY2))
        hits = st.search("nell", "group", "钥匙 花盆 咖啡店 辞职 阳台", channel="group", now=DAY2)
        self.assertTrue(all(r["owner"] != "yuki" for _, r in hits))
        self.assertTrue(all(r["scope"] != "private" or r["channel"] == "private:nell" for _, r in hits))

    def test_switch_one_reads_in_any_group_and_the_note_tone_follows_switch_two(self) -> None:
        st = self.make("group")
        self.seed(st)
        free = st.recall("nell", "group", ASK_PLAN, channel="group", now=DAY2)
        self.assertIn(PRIVATE_IN_GROUP_NOTE_FREE, free)
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE_HUSH, free)
        for ch in ("qq-group:123", "other-room"):
            got = st.recall("nell", "group", ASK_PLAN, channel=ch, now=DAY2)
            self.assertIn("咖啡店", got)
            self.assertIn("[私密·", got)
            self.assertIn(PRIVATE_IN_GROUP_NOTE_HUSH, got)
            self.assertIn("别在这里说出来", PRIVATE_IN_GROUP_NOTE_HUSH)
            self.assertNotIn(PRIVATE_IN_GROUP_NOTE_FREE, got)

    def test_all_kinds_and_trust_levels_are_returned(self) -> None:
        st = self.make("", read=True)
        st.add("nell", "private", "房主觉得楼下的邻居半夜总在练习小号，心里很烦。", channel="private:nell", kind="claim", trust="hearsay", now=DAY1)
        got = st.recall("nell", "group", "楼下的邻居半夜练习小号", channel="group", now=DAY2)
        self.assertIn("邻居", got)
        self.assertIn("[私密·", got)

    def test_without_switch_one_nothing_is_read_even_when_switch_two_lists_the_group(self) -> None:
        for st in (self.make("group", read=False), self.make("", read=False)):
            self.seed(st)
            for ch in ("group", "qq-group:123"):
                got = st.recall("nell", "group", ASK_PLAN, channel=ch, now=DAY2)
                self.assertNotIn("咖啡店", got)

    def test_the_note_appears_only_when_a_private_row_was_really_brought_out(self) -> None:
        st = self.make("group")
        self.seed(st)
        st.add("nell", "group", "凯和由纪周末要一起去逛书店。", channel="group", kind="event", now=DAY1)
        got = st.recall("nell", "group", "周末谁要去逛书店？", channel="group", now=DAY2)
        self.assertIn("书店", got)
        self.assertNotIn("咖啡店", got)
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE, got)
        self.assertNotIn("[私密·", got)

    def test_other_rows_keep_their_old_labels(self) -> None:
        st = self.make("group")
        st.add("nell", "group", "凯和由纪周末要一起去逛书店。", channel="group", kind="event", now=DAY1)
        got = st.recall("nell", "group", "周末谁要去逛书店？", channel="group", now=DAY2)
        self.assertIn("[事件", got)
        self.assertNotIn("私聊", got)

    def test_list_entries_stays_exact_scope(self) -> None:
        st = self.make("group")
        self.seed(st)
        self.assertEqual(st.list_entries("nell", "group", channel="group"), [])


# ---------------------------------------------------------------------------------------------------------------
# 4. the private-only helpers inside recall
# ---------------------------------------------------------------------------------------------------------------
class RecallHelpersInListedGroup(_Base):
    def hear(self, st: MemoryStore, line: str = OWNER_PRIVATE_LINE, actor: str = "nell", at: float = DAY2) -> None:
        st.observe(actor, "private", line, channel=f"private:{actor}", speaker="房主", speaker_role="owner",
                   reply="好的，我替你保密。", now=at)

    def test_what_the_owner_just_said_and_is_not_extracted_yet_is_known_in_the_group(self) -> None:
        st = self.make("group")
        self.hear(st)
        got = st.recall("nell", "group", ASK_PLAN, channel="group", now=DAY2 + 60)
        self.assertIn("咖啡店", got)
        self.assertIn("[私密·刚说的]", got)
        self.assertIn(PRIVATE_IN_GROUP_NOTE, got)
        self.assertNotIn("替你保密", got, "only the owner's own line is brought out, not her stock reply")

    def test_pending_lines_need_switch_one_and_a_related_question(self) -> None:
        st = self.make("group")
        self.hear(st)
        self.assertIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:9", now=DAY2 + 60), "any group with switch one")
        self.assertNotIn("咖啡店", st.recall("nell", "group", "今晚想吃点什么好呢，有人要点外卖吗", channel="group", now=DAY2 + 60))
        for off in (self.make("", read=False), self.make("group", read=False)):
            self.hear(off)
            self.assertNotIn("咖啡店", off.recall("nell", "group", ASK_PLAN, channel="group", now=DAY2 + 60))

    def test_pending_lines_of_another_roommate_and_questions_are_not_used(self) -> None:
        st = self.make("group")
        self.hear(st, "我在阳台花盆下藏了备用钥匙，别告诉别人。", actor="yuki")
        self.hear(st, "你觉得我辞职去开咖啡店好不好？", actor="nell")
        got = st.recall("nell", "group", ASK_PLAN, channel="group", now=DAY2 + 60)
        self.assertNotIn("花盆", got)
        self.assertNotIn("好不好", got)

    def test_extracted_lines_are_not_shown_twice(self) -> None:
        st = self.make("group", llm_fn=lambda _s, _u: {"ops": [
            {"op": "add", "kind": "event", "text": NELL_SECRET, "about": ["房主"], "src": [1]}]},
            extract_every_s=1, extract_batch=1)
        self.hear(st, at=DAY1)
        st.extract("nell", scope="private", force=True, now=DAY1 + 3600)
        got = st.recall("nell", "group", ASK_PLAN, channel="group", now=DAY2)
        self.assertEqual(got.count("咖啡店"), 1)
        self.assertNotIn("刚说的", got)

    def test_bare_do_you_know_question_gets_the_latest_private_notes(self) -> None:
        """「你知道我最近的打算吗」 has no content word: in a private chat the latest few notes are used; with switch one
        on the same happens in a group, whoever asks."""
        q = "你知道我最近的打算吗？"
        for groups, channel, note in (("group", "group", PRIVATE_IN_GROUP_NOTE_FREE), ("", "qq-group:5", PRIVATE_IN_GROUP_NOTE_HUSH)):
            with self.subTest(channel=channel):
                st = self.make(groups, read=True)
                st.add("nell", "private", PLAN_NO_OVERLAP, channel="private:nell", about=["房主"], kind="event", now=DAY1)
                st.add("yuki", "private", YUKI_SECRET, channel="private:yuki", about=["房主"], kind="fact", now=DAY1)
                self.hear(st, "我想学做咖喱饭，周末练习。")
                self.assertEqual(st.search("nell", "group", q, channel=channel, now=DAY2), [], "nothing matches the bare question")
                got = st.recall("nell", "group", q, channel=channel, now=DAY2 + 60)
                self.assertIn("咖啡店", got)
                self.assertIn("咖喱", got)
                self.assertIn(note, got)
                self.assertNotIn("不记得", got, "she must not be told 'you do not remember' while being shown what she remembers")
                self.assertNotIn("花盆", got)

    def test_the_fallback_needs_switch_one(self) -> None:
        q = "你知道我最近的打算吗？"
        for st in (self.make("group", read=False), self.make("", read=False)):
            st.add("nell", "private", PLAN_NO_OVERLAP, channel="private:nell", about=["房主"], kind="event", now=DAY1)
            got = st.recall("nell", "group", q, channel="group", now=DAY2)
            self.assertNotIn("咖啡店", got)
            self.assertEqual(got, NO_MEMORY_NOTE)

    def test_nothing_known_still_says_so_in_a_group(self) -> None:
        st = self.make("group")
        got = st.recall("nell", "group", "你记得我对什么过敏吗？", channel="group", now=DAY2)
        self.assertEqual(got, NO_MEMORY_NOTE)

    def test_no_contradiction_when_group_memory_exists_but_no_private(self) -> None:
        st = self.make("group")
        st.add("nell", "group", "房主对花生过敏，这是大家在群里都知道的。", channel="group", kind="fact", now=DAY1)
        got = st.recall("nell", "group", "你记得我对什么过敏吗？", channel="group", now=DAY2)
        self.assertIn("花生", got)
        self.assertNotIn(NO_MEMORY_NOTE, got)
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE_FREE, got)
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE_HUSH, got)


# ---------------------------------------------------------------------------------------------------------------
# 5. outbound checks
# ---------------------------------------------------------------------------------------------------------------
class OutboundMemoryCheck(_Base):
    def test_listed_group_is_exempt_unlisted_is_not(self) -> None:
        st = self.make("group,qq-group:123")
        self.seed(st)
        st.observe("nell", "private", OWNER_PRIVATE_LINE, channel="private:nell", speaker="房主", speaker_role="owner", now=DAY2)
        self.assertEqual(st.check_outbound_leak("nell", "group", REPEAT, channel="group", now=DAY2), [])
        self.assertEqual(st.check_outbound_leak("nell", "group", REPEAT, channel="qq-group:123", now=DAY2), [])
        self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, channel="qq-group:124", now=DAY2))
        self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, channel="", now=DAY2))
        self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, now=DAY2))
        self.assertTrue(st.check_outbound_leak("nell", "world", REPEAT, channel="group", now=DAY2), "only group replies are exempt")

    def test_with_switch_one_on_the_memory_is_in_the_prompt_everywhere_but_only_leak_free_groups_may_say_it(self) -> None:
        st = self.make("qq-group:123", read=True)
        self.seed(st)
        a = st.recall("nell", "group", ASK_PLAN, channel="qq-group:123", now=DAY2)
        b = st.recall("nell", "group", ASK_PLAN, channel="qq-group:124", now=DAY2)
        self.assertIn("咖啡店", a)
        self.assertIn("咖啡店", b)
        self.assertIn(PRIVATE_IN_GROUP_NOTE_FREE, a)
        self.assertIn(PRIVATE_IN_GROUP_NOTE_HUSH, b)
        self.assertEqual(st.check_outbound_leak("nell", "group", REPEAT, channel="qq-group:123", now=DAY2), [])
        self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, channel="qq-group:124", now=DAY2))

    def test_switch_one_off_switch_two_on_is_just_an_exemption(self) -> None:
        st = self.make("qq-group:123", read=False)
        self.seed(st)
        self.assertNotIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:123", now=DAY2))
        self.assertEqual(st.check_outbound_leak("nell", "group", REPEAT, channel="qq-group:123", now=DAY2), [])
        self.assertTrue(st.check_outbound_leak("nell", "group", REPEAT, channel="qq-group:124", now=DAY2))


class ReplyGuardInListedGroup(unittest.TestCase):
    PERSONA = "你是苍空，一个住在小屋里的安静的作曲人，说话慢悠悠的，喜欢在夜里弹琴。"

    def guard(self, groups: str, tmp: str = "/srv/ai-house/runtime") -> ReplyGuard:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        st = MemoryStore(d.name, env={ENV: groups})
        self.addCleanup(st.close)
        g = ReplyGuard(
            {"AI_HOUSE_OWNER_TOKEN": "tok-live-secret-0123456789"},
            runtime_dir=tmp,
            protected_texts=[self.PERSONA],
            leak_free=st.leak_free_in,
        )
        g.note_private(OWNER_PRIVATE_LINE, actor="nell")
        return g

    def test_private_overlap_is_exempt_only_in_a_listed_group_for_the_same_roommate(self) -> None:
        g = self.guard("group,qq-group:123")
        self.assertIsNone(g.check("group", REPEAT, actor="nell"))
        self.assertIsNone(g.check("qq-group:123", REPEAT, actor="nell"))
        self.assertEqual(g.check("qq-group:124", REPEAT, actor="nell"), "private_content")
        self.assertEqual(g.check("group", REPEAT, actor="yuki"), "private_content", "what she heard is not yuki's to repeat")
        self.assertEqual(g.check("group", REPEAT), "private_content", "no actor named: no exemption")
        self.assertEqual(g.check("webchat", REPEAT, actor="nell"), "private_content")

    def test_what_the_owner_told_someone_else_is_still_guarded_for_her(self) -> None:
        g = self.guard("group")
        g.note_private("今天晚上我想在天台一个人看流星雨，不要告诉任何人。", actor="yuki")
        self.assertEqual(g.check("group", "你昨晚是不是想在天台一个人看流星雨，不要告诉任何人？", actor="nell"), "private_content")

    def test_every_other_check_still_applies_in_a_listed_group(self) -> None:
        g = self.guard("group")
        cases = {
            "cq_code": "好的 [CQ:image,file=a.png] 这是图片",
            "secret": "令牌是 tok-live-secret-0123456789 请收好",
            "internal_path": "日志在 /home/ubuntu/app/run.log 里",
            "internal_path2": "把 .env 文件发给我",
            "internal_path3": "配置在 /srv/ai-house/runtime/x 下面",
            "system_prompt": self.PERSONA + "对，就是这样。",
        }
        want = {"cq_code": "cq_code", "secret": "secret", "internal_path": "internal_path", "internal_path2": "internal_path",
                "internal_path3": "internal_path", "system_prompt": "system_prompt"}
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertEqual(g.check("group", text, actor="nell"), want[name])
                # and mixed with the exempt private overlap the other reason still wins
                self.assertEqual(g.check("group", text + REPEAT, actor="nell"), want[name])
        self.assertEqual(g.check("group", "今天天气不错，大家早上好呀。", actor="nell"), None)


# ---------------------------------------------------------------------------------------------------------------
# 6. fences: reading is not writing, and a listed group's memories do not flow out
# ---------------------------------------------------------------------------------------------------------------
class WriteAccessIsNotWidened(_Base):
    def test_a_listed_group_cannot_rewrite_or_void_a_private_memory(self) -> None:
        st = self.make("group")
        mid, _ = st.add("nell", "private", NELL_SECRET, channel="private:nell", kind="event", now=DAY1)
        for ch in ("group", "qq-group:1"):
            with self.assertRaises(ValueError):
                st.update("nell", "group", mid, "房主其实没有打算辞职，只是随口一说。", channel=ch, now=DAY2)
            with self.assertRaises(ValueError):
                st.invalidate("nell", "group", mid, reason="记错了", channel=ch, now=DAY2)
        row = st.list_entries("nell", "private", channel="private:nell")[0]
        self.assertEqual(row["text"], NELL_SECRET)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(st._peek_writable("nell", "group", "group", mid, False))
        self.assertIsNone(st._peek_writable("nell", "group", "group", mid, True))

    def test_extraction_in_a_listed_group_cannot_edit_private_rows_and_never_sees_them(self) -> None:
        prompts: List[str] = []
        holder: Dict[str, int] = {}

        def fake(sys_p: str, usr_p: str) -> Dict[str, Any]:
            prompts.append(usr_p)
            return {"ops": [
                {"op": "update", "id": holder["id"], "text": "房主说其实没有这回事。", "src": [1]},
                {"op": "invalidate", "id": holder["id"], "reason": "记错了"},
                {"op": "add", "kind": "event", "text": "凯在群里说周末想去逛书店。", "about": ["凯"], "src": [1]},
            ]}

        st = self.make("group", llm_fn=fake, extract_every_s=1, extract_batch=1)
        holder["id"], _ = st.add("nell", "private", NELL_SECRET, channel="private:nell", kind="event", now=DAY1)
        st.observe("nell", "group", "周末我想去逛书店", channel="group", speaker="凯", speaker_role="member", now=DAY1)
        st.extract("nell", scope="group", channel="group", force=True, now=DAY1 + 60)
        self.assertTrue(prompts)
        self.assertNotIn("咖啡店", prompts[0], "the extractor is not shown private rows")
        self.assertEqual(st.list_entries("nell", "private", channel="private:nell")[0]["text"], NELL_SECRET)
        grp = st.list_entries("nell", "group", channel="group")
        self.assertEqual([r["scope"] for r in grp], ["group"])
        self.assertIn("书店", grp[0]["text"])

    def test_memories_formed_in_a_listed_group_are_still_that_groups_group_memories(self) -> None:
        st = self.make("group")
        st.observe("nell", "group", "我打算下个月辞职", channel="group", speaker="房主", speaker_role="owner",
                   reply="我记着呢", now=DAY1)
        with st._connect() as c:
            row = c.execute("SELECT scope, channel FROM seen WHERE actor='nell'").fetchone()
        self.assertEqual((row["scope"], row["channel"]), ("group", "group"))


class UnifiedGroupFence(_Base):
    SECRET_IN_G = "房主在小屋群里说过自己打算辞职去开咖啡店。"

    def make_uni(self, groups: str) -> MemoryStore:
        st = self.make(groups, unified_groups=True)
        st.add("nell", "group", self.SECRET_IN_G, channel="group", kind="event", now=DAY1)  # formed in the listed group
        st.add("nell", "group", "凯周末想去逛书店，大家都听到了。", channel="qq-group:900", kind="event", now=DAY1)  # unlisted
        return st

    def test_default_unified_still_crosses_groups_as_before(self) -> None:
        st = self.make_uni("")
        self.assertIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:900", now=DAY2))

    def test_listed_group_memories_do_not_show_in_an_unlisted_group(self) -> None:
        st = self.make_uni("group")
        self.assertNotIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:900", now=DAY2))
        self.assertNotIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:901", now=DAY2))

    def test_they_still_show_in_the_listed_group_in_private_and_in_other_listed_groups(self) -> None:
        st = self.make_uni("group,qq-group:55")
        self.assertIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="group", now=DAY2))
        self.assertIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:55", now=DAY2))
        self.assertIn("咖啡店", st.recall("nell", "private", ASK_PLAN, channel="private:nell", now=DAY2))

    def test_unlisted_group_rows_keep_crossing_into_other_groups_and_into_listed_ones(self) -> None:
        st = self.make_uni("group")
        self.assertIn("书店", st.recall("nell", "group", "周末谁要去逛书店？", channel="qq-group:901", now=DAY2))
        self.assertIn("书店", st.recall("nell", "group", "周末谁要去逛书店？", channel="group", now=DAY2))

    def test_without_unified_groups_channels_were_already_separate(self) -> None:
        st = self.make("group")
        st.add("nell", "group", self.SECRET_IN_G, channel="group", kind="event", now=DAY1)
        self.assertNotIn("咖啡店", st.recall("nell", "group", ASK_PLAN, channel="qq-group:900", now=DAY2))

    def test_shared_group_rows_are_channel_bound_so_they_cannot_leave_either(self) -> None:
        st = self.make("group", unified_groups=True)
        st.add("nell", "group", self.SECRET_IN_G, channel="group", kind="event", shared=True, now=DAY1)
        self.assertNotIn("咖啡店", st.recall("yuki", "group", ASK_PLAN, channel="qq-group:900", now=DAY2))
        self.assertIn("咖啡店", st.recall("yuki", "group", ASK_PLAN, channel="group", now=DAY2))

    def test_world_scope_is_read_everywhere_so_nothing_from_a_group_is_written_there(self) -> None:
        """Group turns are observed as scope `group` and extracted into `group`; only the diary writes `world`."""
        st = self.make("group")
        st.observe("nell", "group", "房主说他打算辞职去开咖啡店", channel="group", speaker="房主", speaker_role="owner", now=DAY1)
        with st._connect() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT DISTINCT scope FROM seen")], ["group"])


# ---------------------------------------------------------------------------------------------------------------
# 7. HouseDaemon end to end (web group)
# ---------------------------------------------------------------------------------------------------------------
class _DaemonCase(unittest.TestCase):
    def boot(self, groups: str = "", read: Optional[bool] = None) -> _CapturingModel:
        srv = _CapturingModel("好的，我替你保密。")
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"}
        if groups:
            env[ENV] = groups
        if bool(groups) if read is None else read:
            env[READ] = "1"
        self.d = HouseDaemon(tmp.name, pack_dir=PACK, env=env, start_ts=T0, virtual_clock=True, allow_onboarding=False)
        self.addCleanup(self.d.close)
        return srv

    def tell_in_private(self, srv: _CapturingModel, line: str = OWNER_PRIVATE_LINE) -> None:
        srv.reply = "好的，我替你保密。"
        res = self.d.converse("private:sora", line, owner_speaker("房主"))
        self.assertEqual(res["replies"][0]["mode"] != "blocked", True)

    def ask_in_group(self, srv: _CapturingModel, text: str = "苍空，你知道我最近的打算吗？", reply: str = REPEAT, who: Any = None) -> Dict[str, Any]:
        srv.reply = reply
        res = self.d.converse("group", text, who or owner_speaker("房主"))
        self.assertEqual(len(res["replies"]), 1, res)
        return res["replies"][0]


class DaemonWebGroupEndToEnd(_DaemonCase):
    def test_listed_group_prompt_carries_it_and_the_retelling_is_delivered(self) -> None:
        srv = self.boot("group")
        self.tell_in_private(srv)
        n = len(srv.systems)
        rep = self.ask_in_group(srv)
        prompt = srv.systems[n]
        self.assertIn("咖啡店", prompt)
        self.assertIn("[私密·", prompt)
        self.assertIn(PRIVATE_IN_GROUP_NOTE, prompt)
        self.assertNotEqual(rep["mode"], "blocked", rep)
        self.assertEqual(rep["text"], REPEAT)
        rows = self.d.list_chat("group")
        self.assertEqual(rows[-1]["text"], REPEAT)

    def test_same_flow_without_the_setting_is_held_back(self) -> None:
        srv = self.boot("")
        self.tell_in_private(srv)
        n = len(srv.systems)
        rep = self.ask_in_group(srv)
        self.assertNotIn("咖啡店", srv.systems[n])
        self.assertNotIn(PRIVATE_IN_GROUP_NOTE, srv.systems[n])
        self.assertEqual(rep["mode"], "blocked")
        self.assertIn("已被拦下", rep["text"])

    def test_listing_only_a_qq_group_leaves_the_web_group_hushed(self) -> None:
        srv = self.boot("qq-group:50001")  # switch one on, but the web group is not leak-free
        self.tell_in_private(srv)
        n = len(srv.systems)
        rep = self.ask_in_group(srv)
        self.assertIn("咖啡店", srv.systems[n])
        self.assertIn(PRIVATE_IN_GROUP_NOTE_HUSH, srv.systems[n])
        self.assertEqual(rep["mode"], "blocked")

    def test_four_combinations_in_the_web_group(self) -> None:
        # (leak-free list, switch one) -> (private in prompt, note, reply delivered)
        cases = (
            ("", False, False, None, False),            # both off: as before
            ("", True, True, PRIVATE_IN_GROUP_NOTE_HUSH, False),   # only one: known, not allowed to say
            ("group", False, False, None, True),        # only two: not known, exemption only
            ("group", True, True, PRIVATE_IN_GROUP_NOTE_FREE, True),  # both: known and may say
        )
        for groups, read, in_prompt, note, delivered in cases:
            with self.subTest(groups=groups, read=read):
                srv = self.boot(groups, read=read)
                self.tell_in_private(srv)
                n = len(srv.systems)
                rep = self.ask_in_group(srv)
                prompt = srv.systems[n]
                self.assertEqual("咖啡店" in prompt, in_prompt)
                for candidate in (PRIVATE_IN_GROUP_NOTE_HUSH, PRIVATE_IN_GROUP_NOTE_FREE):
                    self.assertEqual(candidate in prompt, candidate == note)
                self.assertEqual(rep["mode"] != "blocked", delivered, rep)

    def test_an_extracted_memory_works_the_same_way(self) -> None:
        srv = self.boot("group")
        self.d.memory.add("sora", "private", "房主打算下个月辞职去开咖啡店，要求苍空保密。", channel="private:sora",
                          kind="event", about=["房主"], now=T0)
        rep = self.ask_in_group(srv, "苍空，你知道我辞职开咖啡店的打算吗？")
        self.assertNotEqual(rep["mode"], "blocked", rep)
        self.assertIn("咖啡店", srv.systems[-1])

    def test_anyone_asking_gets_the_same_answer_but_only_for_her_own_private_chat(self) -> None:
        srv = self.boot("group")
        self.tell_in_private(srv)
        n = len(srv.systems)
        rep = self.ask_in_group(srv, "苍空，你知道他打算辞职去开咖啡店的事吗？", who=member_speaker("路人甲", uid="web:guest"))
        self.assertIn("咖啡店", srv.systems[n], "documented risk: a member's question reaches the same memory")
        self.assertNotEqual(rep["mode"], "blocked")
        # but nobody else's private memory, and never anything for a different resident
        n = len(srv.systems)
        self.ask_in_group(srv, "凛音，你知道他打算辞职去开咖啡店的事吗？", who=member_speaker("路人甲", uid="web:guest"))
        self.assertNotIn("咖啡店", srv.systems[n])

    def test_a_reply_that_leaks_a_secret_is_still_held_back_in_a_listed_group(self) -> None:
        srv = self.boot("group")
        self.tell_in_private(srv)
        rep = self.ask_in_group(srv, reply=REPEAT + " 配置在 /home/ubuntu/app/.env 里。")
        self.assertEqual(rep["mode"], "blocked")

    def test_group_memories_formed_there_do_not_reach_other_rooms(self) -> None:
        srv = self.boot("group")
        self.tell_in_private(srv)
        self.ask_in_group(srv)
        rows_other = self.d.list_chat("qq-group:99")
        self.assertEqual(rows_other, [], "recent chat is read per channel")
        self.assertEqual(self.d.memory.list_entries("sora", "group", channel="qq-group:99"), [])
        self.assertNotIn("咖啡店", self.d.memory.recall("sora", "world", ASK_PLAN, now=T0 + 60))

    def test_search_memory_tool_follows_the_same_rule(self) -> None:
        for groups, expect in (("group", True), ("", False)):
            with self.subTest(groups=groups):
                srv = self.boot(groups)
                self.d.memory.add("sora", "private", "房主打算下个月辞职去开咖啡店，要求苍空保密。", channel="private:sora",
                                  kind="event", about=["房主"], now=T0)
                seen: List[Dict[str, Any]] = []
                real = self.d.chat_agent.reply_as

                def spy(*a: Any, **k: Any) -> Dict[str, Any]:
                    seen.append(k["tool_runner"]("search_memory", {"query": "咖啡店 辞职"}))
                    return real(*a, **k)

                with patch.object(self.d.chat_agent, "reply_as", spy):
                    srv.reply = "嗯嗯。"
                    self.d.converse("group", "苍空，在吗", owner_speaker("房主"))
                self.assertEqual("咖啡店" in str(seen[0]), expect, seen[0])
                self.assertEqual("[私密·" in str(seen[0]), expect)


class DaemonSendGateAndProactive(_DaemonCase):
    PLUGIN = SimpleNamespace(
        cfg=SimpleNamespace(groups=frozenset({"9001", "9002"}), owners=frozenset({"8001"}), max_per_hour=100),
        deliver=lambda delivery_id, payload: f"ref-{delivery_id}",
    )

    def seeded(self, groups: str) -> HouseDaemon:
        self.boot(groups)
        self.d.memory.add("sora", "private", "房主打算下个月辞职去开咖啡店，要求苍空保密。", channel="private:sora",
                          kind="event", about=["房主"], now=T0)
        self.d.guard.note_private(OWNER_PRIVATE_LINE, actor="sora")
        return self.d

    def direct(self, d: HouseDaemon, gid: str, text: str = REPEAT, actor: str = "sora") -> str:
        return d.send_gate.authorize_direct_send("onebot_v11", self.PLUGIN, kind="group", target=gid, actor=actor,
                                                 text=text, delivery_id=f"dir-{gid}-{abs(hash(text)) % 9999}")

    def test_direct_qq_reply_listed_group_passes_unlisted_is_blocked(self) -> None:
        d = self.seeded("qq-group:9001")
        self.assertTrue(self.direct(d, "9001"))
        with self.assertRaises(BlockedDelivery) as cm:
            self.direct(d, "9002")
        self.assertIn("guard:private_content", str(cm.exception))

    def test_direct_qq_reply_other_checks_still_apply_in_the_listed_group(self) -> None:
        d = self.seeded("qq-group:9001")
        with self.assertRaises(BlockedDelivery):
            self.direct(d, "9001", REPEAT + "文件在 /home/ubuntu/x 里")
        with self.assertRaises(BlockedDelivery):
            self.direct(d, "9001", REPEAT + "[CQ:face,id=1]")

    def test_direct_qq_reply_the_memory_check_is_what_stops_it_when_the_guard_never_heard_the_line(self) -> None:
        d = self.seeded("qq-group:9001")
        d.guard._private.clear()
        self.assertTrue(self.direct(d, "9001"))
        with self.assertRaises(BlockedDelivery) as cm:
            self.direct(d, "9002")
        self.assertIn("memory_leak", str(cm.exception))

    def test_without_the_setting_both_groups_are_blocked(self) -> None:
        d = self.seeded("")
        for gid in ("9001", "9002"):
            with self.assertRaises(BlockedDelivery):
                self.direct(d, gid)

    def test_another_roommates_reply_in_the_listed_group_is_still_blocked(self) -> None:
        d = self.seeded("qq-group:9001")
        with self.assertRaises(BlockedDelivery):
            self.direct(d, "9001", actor="rin")

    def test_outbox_delivery_to_a_qq_group(self) -> None:
        d = self.seeded("qq-group:9001")
        ref = d.send_gate.deliver_gated(
            "onebot_v11", self.PLUGIN, "ob-1", {"actor": "sora", "kind": "group", "group_id": "9001", "text": REPEAT},
            is_webchat=False)
        self.assertTrue(ref)
        with self.assertRaises(BlockedDelivery):
            d.send_gate.deliver_gated(
                "onebot_v11", self.PLUGIN, "ob-2", {"actor": "sora", "kind": "group", "group_id": "9002", "text": REPEAT},
                is_webchat=False)

    def test_outbox_delivery_to_the_web_group(self) -> None:
        for groups, passes in (("group", True), ("", False), ("qq-group:9001", False)):
            with self.subTest(groups=groups):
                d = self.seeded(groups)
                payload = {"actor": "sora", "kind": "group", "target": "group", "text": REPEAT}
                if passes:
                    self.assertTrue(d.send_gate.deliver_gated("webchat", None, f"wc-{groups}", payload, is_webchat=True))
                else:
                    with self.assertRaises(BlockedDelivery):
                        d.send_gate.deliver_gated("webchat", None, f"wc-{groups}", payload, is_webchat=True)

    def scan(self, d: HouseDaemon, text: str) -> Dict[str, Any]:
        from ai_house.proactive import ProactiveCandidate

        pe = d.proactive
        pe.anchor(d.world_db, now=T0)
        cand = ProactiveCandidate(source="script", event_id=1, ts=T0, actor="sora", text="在练琴")
        with patch.object(pe, "_collect_new_events", return_value=([cand], 5, 5)), \
                patch.object(pe, "_generate_message", return_value=(text, "injected_llm")):
            return pe.scan(d, cfg={"window": "00:00-23:59", "target_channel": "webchat", "target_scope": "group"}, now=T0 + 30)

    def test_proactive_message_to_the_web_group(self) -> None:
        d = self.seeded("group")
        res = self.scan(d, REPEAT)
        self.assertTrue(res["triggered"], res)

    def test_proactive_message_without_the_setting_is_blocked(self) -> None:
        d = self.seeded("")
        res = self.scan(d, REPEAT)
        self.assertFalse(res["triggered"])
        self.assertTrue(res["reason"].startswith("blocked:"), res)

    def test_proactive_memory_check_uses_the_real_channel(self) -> None:
        d = self.seeded("qq-group:9001")  # the web group is not listed
        d.guard._private.clear()
        res = self.scan(d, REPEAT)
        self.assertEqual(res["reason"], "blocked:memory_leak")


# ---------------------------------------------------------------------------------------------------------------
# 8. QQ group end to end (fake OneBot server)
# ---------------------------------------------------------------------------------------------------------------
import test_p1_onebot as ob  # noqa: E402


class QqGroupEndToEnd(unittest.TestCase):
    def rule(self, system: str, said: str) -> Optional[str]:
        return REPEAT if "最近的打算" in said else None

    def run_flow(self, groups: str, read_only: bool = False) -> "ob.Harness":
        extra = {ENV: groups, READ: "1"} if groups else {}
        if read_only:
            extra = {READ: "1"}
        h = ob.Harness(self, model=ob.Model(self.rule), extra_env=extra)
        h.fake.push(ob.private_msg(ob.OWNER, OWNER_PRIVATE_LINE))
        self.assertTrue(ob.wait_until(lambda: h.private_to(ob.OWNER)), "private reply never arrived")
        h.fake.push(ob.group_msg(ob.GROUP, ob.OWNER, [ob.at(ob.BOT), ob.txt(" 你知道我最近的打算吗")]))
        return h

    def test_listed_qq_group_gets_the_retelling(self) -> None:
        h = self.run_flow(f"qq-group:{ob.GROUP}")
        self.assertTrue(ob.wait_until(lambda: h.fake.sent("send_group_msg")), "nothing was sent to the group")
        self.assertIn("咖啡店", ob.message_text(h.fake.sent("send_group_msg")[0]))
        group_prompts = [s for s, u in h.model.calls if "最近的打算" in u]
        self.assertTrue(group_prompts and "[私密·" in group_prompts[-1])
        rows = h.chat_rows(f"qq-group:{ob.GROUP}")
        self.assertNotIn("blocked", [r["mode"] for r in rows])

    def test_the_same_flow_without_the_setting_is_held_back(self) -> None:
        h = self.run_flow("")
        self.assertTrue(ob.wait_until(lambda: any(r["mode"] == "blocked" for r in h.chat_rows(f"qq-group:{ob.GROUP}"))))
        self.assertEqual(h.fake.sent("send_group_msg"), [])
        group_prompts = [s for s, u in h.model.calls if "最近的打算" in u]
        self.assertTrue(group_prompts and "咖啡店" not in group_prompts[-1])

    def test_listing_another_qq_group_hushes_this_one(self) -> None:
        h = self.run_flow(f"qq-group:{ob.OTHER_GROUP}")
        self.assertTrue(ob.wait_until(lambda: any(r["mode"] == "blocked" for r in h.chat_rows(f"qq-group:{ob.GROUP}"))))
        self.assertEqual(h.fake.sent("send_group_msg"), [])
        group_prompts = [s_ for s_, u in h.model.calls if "最近的打算" in u]
        self.assertTrue("咖啡店" in group_prompts[-1] and PRIVATE_IN_GROUP_NOTE_HUSH in group_prompts[-1])

    def test_only_switch_one_knows_but_cannot_say(self) -> None:
        h = self.run_flow("", read_only=True)
        self.assertTrue(ob.wait_until(lambda: any(r["mode"] == "blocked" for r in h.chat_rows(f"qq-group:{ob.GROUP}"))))
        self.assertEqual(h.fake.sent("send_group_msg"), [])
        group_prompts = [s_ for s_, u in h.model.calls if "最近的打算" in u]
        self.assertIn(PRIVATE_IN_GROUP_NOTE_HUSH, group_prompts[-1])

    def test_only_switch_two_exempts_but_does_not_make_her_know(self) -> None:
        h = ob.Harness(self, model=ob.Model(self.rule), extra_env={ENV: f"qq-group:{ob.GROUP}"})
        h.fake.push(ob.private_msg(ob.OWNER, OWNER_PRIVATE_LINE))
        self.assertTrue(ob.wait_until(lambda: h.private_to(ob.OWNER)))
        h.fake.push(ob.group_msg(ob.GROUP, ob.OWNER, [ob.at(ob.BOT), ob.txt(" 你知道我最近的打算吗")]))
        self.assertTrue(ob.wait_until(lambda: h.fake.sent("send_group_msg")))
        group_prompts = [s_ for s_, u in h.model.calls if "最近的打算" in u]
        self.assertNotIn("咖啡店", group_prompts[-1])


class SharedOrWorldNeverFromPrivateAwareGroups(_Base):
    """Extraction writes into the bucket's own scope; `shared` is only possible when the caller allows it. In a group
    that can carry private content (switch one on, or leak-free) it is not allowed, whatever the caller asks."""

    def extract_shared(self, groups: str, read: bool, channel: str) -> List[Dict[str, Any]]:
        op = {"op": "add", "kind": "event", "text": "房主说他打算下个月去开一家咖啡店。", "about": ["房主"], "shared": True, "src": [1]}
        st = self.make(groups, read=read, llm_fn=lambda _s, _u: {"ops": [op]}, extract_every_s=1, extract_batch=1)
        st.observe("nell", "group", "我打算下个月去开一家咖啡店", channel=channel, speaker="房主", speaker_role="owner", now=DAY1)
        st.extract("nell", scope="group", channel=channel, force=True, allow_shared=True, now=DAY1 + 60)
        with st._connect() as c:
            return [dict(r) for r in c.execute("SELECT owner, scope FROM mem")]

    def test_default_still_allows_shared_when_the_caller_asks(self) -> None:
        self.assertEqual(self.extract_shared("", False, "group"), [{"owner": "shared", "scope": "group"}])

    def test_never_shared_or_world_when_switch_one_is_on(self) -> None:
        self.assertEqual(self.extract_shared("", True, "group"), [{"owner": "nell", "scope": "group"}])

    def test_never_shared_in_a_leak_free_group_even_with_switch_one_off(self) -> None:
        self.assertEqual(self.extract_shared("group", False, "group"), [{"owner": "nell", "scope": "group"}])
        self.assertEqual(self.extract_shared("group", False, "qq-group:5"), [{"owner": "shared", "scope": "group"}])


if __name__ == "__main__":
    unittest.main()
