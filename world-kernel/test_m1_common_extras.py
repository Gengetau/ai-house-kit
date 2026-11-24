"""M1: four things the kit's old store had and the shared memory file lacked, now in the shared file
(src/ai_house/common/UPSTREAM.md lists them). Decided 2026-11-24:

1. recall says "you do not remember" when nothing is found and the question asks whether she remembers (on by default);
2. settings_problems() says what is wrong with the settings as written (init() logs it, never refuses to start);
3. trust levels, off by default (MEMORY_TRUST);
4. expires_at: a memory may have a time after which it is gone (None = never, as before).
"""

import array
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_house.common import loader, roster

OWNER_QQ = 999
OWNER, GROUP, OTHER_GROUP, FREE, STRANGER = "private:999", "group:5001", "group:5002", "group:5003", "private:7001"
ASK = "[路人|42] (message_id=1): 你还记得我对什么过敏吗"
CHAT = "[路人|42] (message_id=2): 今天天气真不错"
OWNER_SAYS = "[房主|999] (message_id=3): 我对花生过敏，吃了会起疹子"
OTHER_SAYS = "[路人|42] (message_id=4): 其实房主对花生一点都不过敏的"
HER_OWN = "[妮娅(你)|1000] (message_id=5): 我记得你说过的"


class Case(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict(os.environ, self.env):
            m = loader.load(roster=roster.make([{"actor": "nia", "qq": 1000, "name": "妮娅"},
                                                {"actor": "xingli", "qq": 1001, "name": "星璃"}],
                                               owner={"qq": OWNER_QQ}))
        self.addCleanup(loader.unload, m)
        m.DB_PATH = str(Path(self.tmp.name) / "m.db")
        m.CONSUMERS = ["nia", "xingli"]
        m.OWNER_CHATS = {f"nia@{OWNER}", f"xingli@{OWNER}"}
        m.LEAK_FREE_CHATS = {FREE}
        m.embed = lambda texts, timeout=20: None
        self.m = m
        self.start()

    def start(self):
        self.m.init()

    def columns(self):
        with self.m.db() as c:
            return {r[1] for r in c.execute("pragma table_info(mem)")}

    def expire(self, mid):
        """Put the memory's time in the past, as if it had run out a second ago."""
        with self.m.db() as c:
            c.execute("update mem set expires_at=? where id=?", (time.time() - 1, mid))

    def status(self, mid):
        with self.m.db() as c:
            return c.execute("select status from mem where id=?", (mid,)).fetchone()[0]


# ---- 1. nothing found: say you do not remember ----------------------------------------------------------------

class NoMemoryNoteTests(Case):
    def test_a_question_about_remembering_with_nothing_found_gets_the_note(self):
        self.assertEqual(self.m.recall("nia", GROUP, [ASK]), self.m.NO_MEMORY_NOTE)
        self.assertIn("不记得", self.m.NO_MEMORY_NOTE)

    def test_the_note_is_not_a_private_exposure(self):
        self.assertEqual(self.m.recall("nia", GROUP, [ASK], with_exposure=True),
                         {"text": self.m.NO_MEMORY_NOTE, "turn_private_exposure": False})

    def test_other_talk_gets_nothing_as_before(self):
        self.assertEqual(self.m.recall("nia", GROUP, [CHAT]), "")
        self.assertEqual(self.m.recall("nia", GROUP, []), "")
        self.assertEqual(self.m.recall("nia", GROUP, ["[路人|42] (message_id=1): 我记得你昨天说的"]), "")  # no question

    def test_it_is_decided_on_the_last_line_that_is_not_hers(self):
        self.assertEqual(self.m.recall("nia", GROUP, [CHAT, ASK, HER_OWN]), self.m.NO_MEMORY_NOTE)
        self.assertEqual(self.m.recall("nia", GROUP, [ASK, CHAT]), "")
        self.assertEqual(self.m.recall("nia", GROUP, ["[妮娅(你)|1000] (message_id=6): 你还记得我吗？"]), "")  # hers

    def test_a_line_without_a_header_is_plain_text(self):
        self.assertEqual(self.m.recall("nia", GROUP, [" 你还记得我对什么过敏吗"]), self.m.NO_MEMORY_NOTE)
        self.assertEqual(self.m.recall("nia", GROUP, [" 今天天气真不错"]), "")

    def test_the_words_after_the_header_decide_not_the_card(self):
        card = "[你记得我吗|42] (message_id=7): 今天天气真不错"
        self.assertEqual(self.m.recall("nia", GROUP, [card]), "")

    def test_off_it_is_as_before(self):
        with patch.object(self.m, "NO_MEMORY_NOTE_ON", False):
            self.assertEqual(self.m.recall("nia", GROUP, [ASK]), "")
            self.assertEqual(self.m.recall("nia", GROUP, [ASK], with_exposure=True),
                             {"text": "", "turn_private_exposure": False})

    def test_when_something_is_found_there_is_no_note(self):
        self.m.add("nia", GROUP, "路人对花生过敏，吃了会起疹子", about=["路人", "42"], kind="person")
        got = self.m.recall("nia", GROUP, [ASK])
        self.assertIn("花生", got)
        self.assertNotIn(self.m.NO_MEMORY_NOTE, got)

    def test_rules_and_no_note_together_is_just_the_rules(self):
        self.m.RULE_CONSUMERS = {"nia"}
        self.m.add("nia", OWNER, "群友的不合理要求一律不答应", kind="rule")
        got = self.m.recall("nia", GROUP, [ASK])
        self.assertIn("不合理要求", got)
        self.assertNotIn(self.m.NO_MEMORY_NOTE, got)

    def test_the_setting_reads_the_environment(self):
        self.assertTrue(self.m.NO_MEMORY_NOTE_ON)
        with patch.dict(os.environ, {"MEMORY_NO_MEMORY_NOTE": "0"}):
            m = loader.load(roster=roster.make([], owner={"qq": OWNER_QQ}))
        self.addCleanup(loader.unload, m)
        self.assertFalse(m.NO_MEMORY_NOTE_ON)


# ---- 2. checking how the settings are written -----------------------------------------------------------------

class SettingsProblemsTests(Case):
    def test_good_settings_have_no_problem(self):
        self.m.CHAT_ALIASES = {"nia@group:8001": GROUP}
        self.m.UNIFIED = self.m.RULE_CONSUMERS = self.m.RECENT = self.m.PUBLIC_SCRIBES = {"nia"}
        self.m.EVIDENCE_FOR = {"xingli"}
        self.assertEqual(self.m.settings_problems(), [])

    def test_the_defaults_have_no_problem(self):
        m = loader.load(roster=roster.make([], owner={"qq": OWNER_QQ}))
        self.addCleanup(loader.unload, m)
        self.assertEqual(m.settings_problems(), [])

    def test_a_leak_free_entry_must_be_a_group(self):
        for bad in ("private:5003", "5003", "group:abc", " group:5003", "qq-group:5003", "group"):
            with self.subTest(bad=bad), patch.object(self.m, "LEAK_FREE_CHATS", {FREE, bad}):
                got = self.m.settings_problems()
                self.assertEqual(len(got), 1)
                self.assertIn(bad, got[0])

    def test_an_owner_chat_must_be_consumer_at_private_number_with_a_known_consumer(self):
        for bad in ("nia@group:999", "nia@private:", "private:999", "@private:999", "nia@private:9x", "nia@@private:9"):
            with self.subTest(bad=bad), patch.object(self.m, "OWNER_CHATS", {f"nia@{OWNER}", bad}):
                self.assertEqual(len(self.m.settings_problems()), 1)
        with patch.object(self.m, "OWNER_CHATS", {f"nia@{OWNER}", "mia@private:999"}):
            got = self.m.settings_problems()
            self.assertEqual(len(got), 1)
            self.assertIn("mia", got[0])

    def test_chat_aliases_must_be_well_formed(self):
        for key, value in (("nia@8001", GROUP), ("nia@group:8001", "5001"), ("group:8001", GROUP),
                           ("nia@group:x", GROUP), ("nia@group:8001", "group:")):
            with self.subTest(key=key, value=value), patch.object(self.m, "CHAT_ALIASES", {key: value}):
                self.assertEqual(len(self.m.settings_problems()), 1)

    def test_a_name_that_is_not_a_consumer_is_found_in_every_list(self):
        for name in ("UNIFIED", "RULE_CONSUMERS", "RECENT", "EVIDENCE_FOR", "PUBLIC_SCRIBES"):
            with self.subTest(name=name), patch.object(self.m, name, {"nia", "nai"}):
                got = self.m.settings_problems()
                self.assertEqual(len(got), 1)
                self.assertIn("nai", got[0])

    def test_nothing_to_compare_with_when_the_consumer_list_is_empty(self):
        with patch.multiple(self.m, CONSUMERS=[], UNIFIED={"nai"}):
            self.assertEqual(self.m.settings_problems(), [])

    def test_it_reads_the_module_not_the_environment(self):
        with patch.dict(os.environ, {"MEMORY_LEAK_FREE_CHATS": "private:1"}), \
                patch.object(self.m, "LEAK_FREE_CHATS", {FREE}):
            self.assertEqual(self.m.settings_problems(), [])
        with patch.object(self.m, "LEAK_FREE_CHATS", {"private:1"}):
            self.assertEqual(len(self.m.settings_problems()), 1)

    def test_init_logs_each_problem_and_carries_on(self):
        with patch.multiple(self.m, LEAK_FREE_CHATS={"private:1"}, UNIFIED={"nai"}):
            with self.assertLogs("memory", "WARNING") as logs:
                self.m.init()
            self.assertEqual(len(logs.records), 2)
            self.assertGreater(self.m.add("nia", GROUP, "路人说他周末要去爬山")[0], 0)
        with self.assertNoLogs("memory", "WARNING"):
            self.m.init()


# ---- 3. trust levels ------------------------------------------------------------------------------------------

def same_vector(texts, timeout=20):
    """Every text is the same point: any two memories are near-duplicates, so a merge is always possible."""
    return [array.array("f", [1.0, 0.0]) for _ in texts]


class TrustOffTests(Case):
    def test_off_there_is_no_column_and_the_output_is_as_before(self):
        self.assertFalse(self.m.TRUST)
        self.assertNotIn("trust", self.columns())
        mid, _ = self.m.add("nia", GROUP, "路人说他周末要去爬山", about=["路人", "42"], trust="hearsay")
        with self.m.db() as c:
            row = c.execute("select * from mem where id=?", (mid,)).fetchone()
        self.assertIn(f"#{mid} [事实 关于路人|42] ", self.m._fmt(row))
        self.assertNotIn("未核实", self.m._fmt(row))
        self.assertNotIn("未核实", self.m.recall("nia", GROUP, ["[路人|42] (message_id=1): 路人周末要去爬山吗"]))

    def test_off_a_merge_does_not_look_at_levels(self):
        self.m.embed = same_vector
        first, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", trust="verified")
        second, how = self.m.add("nia", GROUP, "路人周末改去海边了", trust="hearsay")
        self.assertEqual((first, how), (second, "merged"))

    def test_off_the_extractor_writes_no_level(self):
        self.m.llm = lambda system, user: {"ops": [{"op": "add", "text": "路人周末要去爬山"}]}
        self.m.observe("nia", GROUP, [CHAT])
        self.assertEqual(self.m.extract("nia", GROUP, 0)["added"], 1)
        self.assertNotIn("trust", self.columns())

    def test_the_setting_reads_the_environment(self):
        with patch.dict(os.environ, {"MEMORY_TRUST": "1"}):
            m = loader.load(roster=roster.make([], owner={"qq": OWNER_QQ}))
        self.addCleanup(loader.unload, m)
        self.assertTrue(m.TRUST)


class TrustOnTests(Case):
    env = {"MEMORY_TRUST": "1"}

    def trust_of(self, mid):
        with self.m.db() as c:
            return c.execute("select trust from mem where id=?", (mid,)).fetchone()[0]

    def test_the_column_is_added_to_an_old_database_and_a_memory_is_verified_unless_said(self):
        self.assertIn("trust", self.columns())
        self.m.init()  # twice is fine
        a, _ = self.m.add("nia", GROUP, "路人说他周末要去爬山")
        b, _ = self.m.add("nia", GROUP, "路人好像对猫过敏的样子", trust="group_stated")
        c, _ = self.m.add("nia", GROUP, "听说路人以前住在海边", trust="hearsay")
        d, _ = self.m.add("nia", GROUP, "路人喜欢喝咖啡加糖", trust="nonsense")
        self.assertEqual([self.trust_of(x) for x in (a, b, c, d)], ["verified", "group_stated", "hearsay", "verified"])

    def test_the_recall_line_says_it_is_unverified(self):
        self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", about=["路人", "42"])
        self.m.add("nia", GROUP, "路人最近在学吉他，每天练一小时", about=["路人", "42"], trust="group_stated")
        got = self.m.recall("nia", GROUP, ["[路人|42] (message_id=1): 路人周末要去爬山吗，还有他的吉他"])
        self.assertIn("[事实 关于路人|42] 路人周末要去爬山", got)
        self.assertIn("[群聊陈述·未核实 关于路人|42] 路人最近在学吉他", got)

    def test_a_private_memory_that_is_unverified_is_an_inference(self):
        mid, _ = self.m.add("nia", OWNER, "房主可能对花生过敏吧大概", trust="group_stated")
        with self.m.db() as c:
            row = c.execute("select * from mem where id=?", (mid,)).fetchone()
        self.assertIn("[推断·未核实]", self.m._fmt(row))

    def test_search_weighs_by_level(self):
        self.m.add("nia", GROUP, "路人喜欢吃草莓蛋糕", about=["路人"])
        self.m.add("nia", GROUP, "路人喜欢吃草莓蛋糕和饼干", about=["路人"], trust="hearsay")
        got = {r["trust"]: s for s, r in self.m.search("nia", GROUP, "路人喜欢吃草莓蛋糕", about=["路人"])}
        self.assertAlmostEqual(got["hearsay"] / got["verified"], 0.60, delta=0.12)
        self.assertGreater(got["verified"], got["hearsay"])

    def test_an_unverified_claim_against_a_verified_memory_is_pushed_down_and_marked(self):
        self.m.add("nia", GROUP, "苍空对花生过敏，不能吃", about=["苍空"])
        self.m.add("nia", GROUP, "群友说苍空对花生并不过敏，随便吃", about=["苍空"], trust="group_stated")
        self.m.add("nia", GROUP, "群友说凛音对花生不过敏，随便吃", about=["凛音"], trust="group_stated")  # someone else
        got = self.m.search("nia", GROUP, "苍空 花生 过敏")
        by_text = {r["text"]: (s, r) for s, r in got}
        verified, claim = by_text["苍空对花生过敏，不能吃"], by_text["群友说苍空对花生并不过敏，随便吃"]
        self.assertGreater(verified[0], claim[0] * 3)
        self.assertIn(" [群聊未核实传言·与已知事实冲突，勿采信 关于苍空] ", self.m._fmt(claim[1]))
        other = [r for s, r in got if "凛音" in r["text"]]
        self.assertTrue(all("勿采信" not in self.m._fmt(r) and "群聊陈述·未核实" in self.m._fmt(r) for r in other))
        self.assertNotIn("勿采信", self.m._fmt(verified[1]))

    def test_a_weaker_text_never_merges_over_a_stronger_row(self):
        self.m.embed = same_vector
        a, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友")
        b, how = self.m.add("nia", GROUP, "路人周末改去海边了", trust="group_stated")
        self.assertNotEqual(a, b)
        self.assertEqual(how, "added")
        with self.m.db() as c:
            self.assertEqual(c.execute("select text from mem where id=?", (a,)).fetchone()[0], "路人周末要去爬山，约了朋友")
        same, how = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", trust="hearsay")  # the same words are the same
        self.assertEqual((same, how), (a, "same"))

    def test_a_merge_takes_the_stronger_level_and_never_lowers_one(self):
        self.m.embed = same_vector
        a, _ = self.m.add("nia", GROUP, "路人周末可能要去爬山", trust="hearsay")
        b, how = self.m.add("nia", GROUP, "路人周末要去爬山，已经订了票", trust="group_stated")
        self.assertEqual((b, how, self.trust_of(a)), (a, "merged", "group_stated"))
        c, how = self.m.add("nia", GROUP, "房主说路人周末确定去爬山")
        self.assertEqual((c, how, self.trust_of(a)), (a, "merged", "verified"))
        d, how = self.m.add("nia", GROUP, "路人周末爬山的事有点变化", trust="group_stated")
        self.assertEqual(how, "added")  # a weaker text next to a verified row is a row of its own
        self.assertEqual((self.trust_of(a), self.trust_of(d)), ("verified", "group_stated"))


class TrustExtractorTests(Case):
    env = {"MEMORY_TRUST": "1"}

    def run_extract(self, chat, lines, ops, private=0):
        self.m.llm = lambda system, user: {"ops": ops}
        self.m.observe("nia", chat, lines)
        return self.m.extract("nia", chat, private)

    def rows(self):
        with self.m.db() as c:
            return [dict(r) for r in c.execute("select id, text, trust, status from mem order by id")]

    ADD = {"op": "add", "text": "房主对花生过敏，吃了会起疹子", "about": ["房主", "999"], "kind": "fact"}

    def test_what_the_owner_said_in_private_is_verified(self):
        self.run_extract(OWNER, [OWNER_SAYS], [self.ADD], private=1)
        self.assertEqual([r["trust"] for r in self.rows()], ["verified"])

    def test_in_a_group_what_only_the_owner_said_is_verified(self):
        self.run_extract(GROUP, [OWNER_SAYS, HER_OWN], [self.ADD])
        self.assertEqual([r["trust"] for r in self.rows()], ["verified"])

    def test_in_a_group_what_others_said_is_not(self):
        self.run_extract(GROUP, [OWNER_SAYS, OTHER_SAYS], [self.ADD])
        self.assertEqual([r["trust"] for r in self.rows()], ["group_stated"])

    def test_a_line_without_a_header_is_nobodys_word(self):
        self.run_extract(GROUP, ["房主对花生过敏，吃了会起疹子"], [self.ADD])
        self.assertEqual([r["trust"] for r in self.rows()], ["group_stated"])

    def test_with_the_evidence_layer_the_lines_an_op_names_decide(self):
        with patch.multiple(self.m, EVIDENCE=True):
            self.m.init()
            self.run_extract(GROUP, [OWNER_SAYS, OTHER_SAYS],
                             [dict(self.ADD, src=[1]), dict(self.ADD, text="路人觉得房主对花生不过敏，没事", src=[2])])
        self.assertEqual([r["trust"] for r in self.rows()], ["verified", "group_stated"])

    def test_another_one_cannot_rewrite_a_verified_memory_but_the_owner_can(self):
        mid, _ = self.m.add("nia", GROUP, "房主对花生过敏，吃了会起疹子", about=["房主", "999"])
        self.run_extract(GROUP, [OTHER_SAYS], [{"op": "update", "id": mid, "text": "房主对花生一点都不过敏"}])
        rows = self.rows()
        self.assertEqual([(r["text"], r["trust"]) for r in rows],
                         [("房主对花生过敏，吃了会起疹子", "verified"), ("房主对花生一点都不过敏", "group_stated")])
        self.run_extract(GROUP, [OWNER_SAYS], [{"op": "update", "id": mid, "text": "房主对花生和虾都过敏"}])
        self.assertEqual(self.rows()[0]["text"], "房主对花生和虾都过敏")
        self.assertEqual(len(self.rows()), 2)

    def test_the_notebook_import_stays_as_it_was(self):
        self.m.llm = lambda system, user: {"ops": [self.ADD]}
        self.m.import_notebook("nia", GROUP, "房主对花生过敏")
        self.assertEqual([r["trust"] for r in self.rows()], ["verified"])


# ---- 4. expiry ------------------------------------------------------------------------------------------------

class ExpiryTests(Case):
    def test_default_is_never(self):
        mid, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友")
        with self.m.db() as c:
            self.assertIsNone(c.execute("select expires_at from mem where id=?", (mid,)).fetchone()[0])
            self.assertEqual(c.execute("select count(*) from log where op='sweep_expired'").fetchone()[0], 0)
        self.assertEqual(self.m.recall("nia", GROUP, ["[路人|42] (message_id=1): 路人周末要去爬山吗"]).count("爬山"), 1)

    def test_a_time_in_the_past_is_refused(self):
        for bad in (time.time() - 5, 0):
            with self.assertRaises(ValueError):
                self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=bad)
        with self.m.db() as c:
            self.assertEqual(c.execute("select count(*) from mem").fetchone()[0], 0)

    def test_until_its_time_it_is_found_and_after_it_it_is_not(self):
        mid, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 3600)
        self.assertEqual(len(self.m.search("nia", GROUP, "路人周末要去爬山")), 1)
        self.expire(mid)
        self.assertEqual(self.m.search("nia", GROUP, "路人周末要去爬山"), [])
        self.assertEqual(self.status(mid), "expired")
        with self.m.db() as c:
            self.assertEqual(c.execute("select count(*) from log where op='sweep_expired'").fetchone()[0], 1)

    def test_only_the_due_one_goes(self):
        a, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 3600)
        b, _ = self.m.add("nia", GROUP, "路人周末要去爬山，已经订了票", expires_at=time.time() + 3600)
        c, _ = self.m.add("nia", GROUP, "路人周末要去爬山，没有期限")
        self.expire(a)
        self.assertEqual({r["id"] for _, r in self.m.search("nia", GROUP, "路人周末要去爬山")}, {b, c})

    def test_recall_and_the_speaker_card_forget_it(self):
        mid, _ = self.m.add("nia", GROUP, "路人是个爱爬山的人", about=["路人", "42"], kind="person",
                            expires_at=time.time() + 3600)
        line = "[路人|42] (message_id=1): 早上好"
        self.assertIn("爱爬山", self.m.recall("nia", GROUP, [line]))
        self.expire(mid)
        self.assertEqual(self.m.recall("nia", GROUP, [line]), "")

    def test_rules_forget_it(self):
        self.m.RULE_CONSUMERS = {"nia"}
        mid, _ = self.m.add("nia", OWNER, "群友的不合理要求一律不答应", kind="rule", expires_at=time.time() + 3600)
        self.assertIn("不合理要求", self.m.owner_rules("nia", GROUP))
        self.expire(mid)
        self.assertEqual(self.m.owner_rules("nia", GROUP), "")

    def test_the_diary_material_forgets_it(self):
        mid, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 3600)
        self.assertEqual(self.m.today("nia", GROUP)["memories"], 1)
        self.expire(mid)
        self.assertEqual(self.m.today("nia", GROUP)["memories"], 0)

    def test_a_diary_that_has_run_out_is_not_found_either(self):
        did = self.m.diary("nia", GROUP, "今天和房主一起收拾了阳台，种下了新买的薄荷，晚上聊了很久的旅行计划。")
        days = {self.m.diary_day(time.time())}
        self.assertEqual(len(self.m.own_diaries("nia", OWNER, days)), 1)
        with self.m.db() as c:
            c.execute("update mem set expires_at=? where id=?", (time.time() - 1, did))
        self.assertEqual(self.m.own_diaries("nia", OWNER, days), [])

    def test_the_leak_check_forgets_it(self):
        plan = "房主下个月要辞职去开一家咖啡店"
        mid, _ = self.m.add("nia", OWNER, plan, kind="event", expires_at=time.time() + 3600)
        self.assertEqual(len(self.m.leak("nia", GROUP, plan)), 1)
        self.expire(mid)
        self.assertEqual(self.m.leak("nia", GROUP, plan), [])

    def test_it_can_no_longer_be_changed_or_forgotten(self):
        a, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 3600)
        b, _ = self.m.add("nia", GROUP, "路人最近在学吉他，每天练一小时", expires_at=time.time() + 3600)
        self.expire(a)
        self.expire(b)
        with self.assertRaises(ValueError):
            self.m.update("nia", GROUP, a, "路人周末改去海边了")
        with self.assertRaises(ValueError):
            self.m.forget("nia", GROUP, b)

    def test_an_expired_memory_is_not_merged_into_or_found_as_the_same(self):
        self.m.embed = same_vector
        a, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 3600)
        self.expire(a)
        b, how = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友")
        self.assertEqual(how, "added")
        self.assertNotEqual(a, b)
        c, how = self.m.add("nia", GROUP, "路人周末改去海边了")
        self.assertEqual((c, how), (b, "merged"))

    def test_a_merge_takes_a_new_time_and_keeps_the_old_one_otherwise(self):
        self.m.embed = same_vector
        t1, t2 = time.time() + 3600, time.time() + 7200
        a, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=t1)

        def expires():
            with self.m.db() as c:
                return c.execute("select expires_at from mem where id=?", (a,)).fetchone()[0]

        self.assertEqual(self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=t2)[1], "same")  # later: kept
        self.assertEqual(expires(), t2)
        self.assertEqual(self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=t1)[1], "same")  # never earlier
        self.assertEqual(expires(), t2)
        self.assertEqual(self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友")[1], "same")  # nor dropped
        self.assertEqual(expires(), t2)
        self.assertEqual(self.m.add("nia", GROUP, "路人周末要去海边，约了朋友")[1], "merged")  # new words, no time: kept
        self.assertEqual(expires(), t2)
        self.assertEqual(self.m.add("nia", GROUP, "路人改成周末去露营了", expires_at=t1)[1], "merged")  # a given time wins
        self.assertEqual(expires(), t1)

    def test_a_memory_with_no_time_does_not_get_one_from_a_repeat(self):
        a, _ = self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友")
        self.assertEqual(self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 60)[1], "same")
        with self.m.db() as c:
            self.assertIsNone(c.execute("select expires_at from mem where id=?", (a,)).fetchone()[0])

    def test_an_old_database_gets_the_column(self):
        with self.m.db() as c:
            c.executescript("drop index if exists mem_expiry; alter table mem drop column expires_at;")
        self.assertNotIn("expires_at", self.columns())
        self.m.init()
        self.assertIn("expires_at", self.columns())
        self.assertGreater(self.m.add("nia", GROUP, "路人周末要去爬山，约了朋友", expires_at=time.time() + 60)[0], 0)


if __name__ == "__main__":
    unittest.main()
