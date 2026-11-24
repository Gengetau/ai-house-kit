"""M1 step 3: `ai_house.memory_migrate` carries a house's memories from `memory.db` (old `MemoryStore`) into
`memory-common.db` (`CommonMemoryStore`).

The old house is built with the real old store through its public calls; what comes out is checked through the new
store's public calls. No network, no real model. Run under any host zone (`TZ=UTC`, `TZ=Asia/Tokyo`).
"""

import array
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from test_m1_adapter_support import NAMES, NOTHING_FOUND, T0, FakeModel, add_op, quiet_resource_warnings

from ai_house import memory_migrate
from ai_house.memory import MemoryStore
from ai_house.memory_common import CommonMemoryStore
from ai_house.memory_migrate import describe, id_map, migrate

G1 = "qq-group:111"
G2 = "qq-group:222"
NOW = T0 + 5000          # the moment of the migration
_BASE = "房主的书架上依次放着" + "、".join(f"第{n}本旧书" for n in range(1, 20))
LONG_A = _BASE + "甲版。"      # 120 < length <= 140: whole in the old store, cut at 120 by the new one
LONG_B = _BASE + "乙版。"      # differs only after the 120th character: the same row once cut
SECRETISH = "金库小盒子密码藏在书房床底"   # the old store lets it in, the shared module's filter does not


def setUpModule():
    quiet_resource_warnings()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def now(self) -> float:
        return self.t


def fake_vectors(texts):
    out = []
    for t in texts:
        h = zlib.crc32(t.encode("utf-8"))
        out.append([float((h >> i) & 7) + 1.0 for i in range(0, 24, 3)])
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table(path: Path, sql: str) -> List[tuple]:
    con = sqlite3.connect(str(path))
    try:
        return [tuple(r) for r in con.execute(sql)]
    finally:
        con.close()


def build_old(data_dir: str, *, vectors: bool = False) -> Dict[str, Any]:
    """A house that has been running on the old store. Returns the texts and ids by name."""
    clk = Clock(T0)
    old = MemoryStore(data_dir, clock=clk, names=NAMES, env={}, embed_plugin=fake_vectors if vectors else None)
    ids: Dict[str, int] = {}
    t: Dict[str, str] = {}

    def put(key, actor, scope, text, **kw):
        kw.setdefault("now", T0 + len(ids) * 10)
        mid, _ = old.add(actor, scope, text, **kw)
        ids[key], t[key] = mid, text
        return mid

    put("peanut", "nia", "private", "房主对花生过敏，妮娅要记得别点花生菜。", about=["房主"], kind="fact")
    put("rule", "nia", "private", "房主交代：晚上十一点后不要打电话叫醒他。", about=["房主"], kind="rule")
    put("blackout", "nia", "group", "群里有人说下周三要停电。", channel=G1, kind="claim", trust="hearsay", about=["群友"])
    put("cat", "nia", "group", "二号群的猫叫团子，爱睡在窗台。", channel=G2, kind="fact")
    put("cake", "xingli", "group", "星璃在一号群里答应周五带蛋糕。", channel=G1, kind="promise")
    put("movie", "nia", "group", "一号群约好每周六晚上一起看电影。", channel=G1, kind="event", shared=True)
    put("hotpot", "nia", "group", "网页群里大家喜欢吃火锅。", kind="preference")
    put("clock", "nia", "world", "小屋的钟在整点会敲三下。", kind="fact", shared=True)
    put("vote", "xingli", "group", "二号群的投票结果是喝茶。", channel=G2, kind="fact", trust="group_stated")
    put("diary_nia", "nia", "world", "2026-11-23 日记：今天下了一整天的雨，我在窗边读了很久的书，心里很安静。",
        kind="diary", source="diary")
    put("diary_xl", "xingli", "world", "2026-11-23 日记：傍晚和大家一起煮了汤，月亮出来的时候我们坐在院子里看了很久。",
        kind="diary", source="diary")
    put("eggplant", "nia", "private", "房主今晚想吃的是红烧茄子。", ttl_s=100000)
    put("gone", "nia", "private", "房主说周二的雨伞借给邻居了。", ttl_s=60)
    put("wrong", "nia", "private", "房主养了一只叫大黄的狗。")
    old.invalidate("nia", "private", ids["wrong"], reason="说错了", now=T0 + 500)
    put("long_a", "nia", "private", LONG_A, kind="fact")
    put("long_b", "nia", "private", LONG_B, kind="fact")
    put("vault", "nia", "private", SECRETISH, kind="fact")
    put("tea", "xingli", "private", "房主喜欢的茶是铁观音，不加糖。", about=["房主"], kind="preference", trust="group_stated")
    # use: recalling bumps the use count and the last-used time
    old.recall("nia", "private", "妮娅，你记得房主对花生过敏的事吗？", now=T0 + 700)
    old.recall("nia", "private", "妮娅，你记得房主对花生过敏的事吗？", now=T0 + 800)
    # the old store keeps the line it heard, until the next tidy-up
    old.observe("nia", "private", "我今天想去海边看日出。", speaker_role="owner", reply="好呀，我陪你去。", now=T0 + 900)
    old.observe("nia", "group", "周末谁想去爬山呀", channel=G1, speaker="阿明", speaker_id="qq:20001", now=T0 + 910)
    old.observe("xingli", "group", "我想学做蛋挞", channel=G2, speaker="小周", speaker_id="qq:20002", now=T0 + 920)
    old.observe("nia", "world", "院子里的桂花开了。", now=T0 + 930)
    old.observe("nia", "private", "这一句已经整理过了。", speaker_role="owner", now=T0 + 940)
    old.close()
    # one line the old store has already tidied into memories (what its own extract run would have marked)
    con = sqlite3.connect(str(old.db_path))
    with con:
        con.execute("update seen set done=1 where line like ?", ("%已经整理过%",))
    con.close()
    return {"ids": ids, "text": t, "old": old}


class MigrateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.house = build_old(self.dir)
        self.ids, self.text = self.house["ids"], self.house["text"]
        self.old_path = Path(self.dir) / "memory.db"
        self.new_path = Path(self.dir) / "memory-common.db"

    def run_migrate(self, **kw) -> Dict[str, Any]:
        kw.setdefault("env", {})
        kw.setdefault("names", NAMES)
        kw.setdefault("now", NOW)
        return migrate(self.dir, **kw)

    def store(self, **kw) -> CommonMemoryStore:
        kw.setdefault("names", NAMES)
        kw.setdefault("env", {})
        st = CommonMemoryStore(self.dir, **kw)
        self.addCleanup(st.close)
        return st

    def old_rows(self) -> Dict[int, sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.old_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return {r["id"]: r for r in con.execute("select * from mem")}
        finally:
            con.close()

    def entries(self, st: CommonMemoryStore) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for actor in NAMES:
            out += st.list_entries(actor, "private", limit=200, now=NOW)
            out += st.list_entries(actor, "world", limit=200, now=NOW)
        out += st.list_entries(None, "group", limit=200, now=NOW)
        out += st.list_entries(None, "world", limit=200, now=NOW)
        seen, uniq = set(), []
        for e in out:
            if e["id"] not in seen:
                seen.add(e["id"])
                uniq.append(e)
        return uniq

    def by_text(self, st: CommonMemoryStore) -> Dict[str, Dict[str, Any]]:
        return {e["text"]: e for e in self.entries(st)}


class CarryTests(MigrateCase):
    def test_every_shown_memory_is_found_where_the_old_store_found_it_and_nowhere_else(self):
        rep = self.run_migrate()
        self.assertTrue(rep["wrote"])
        st = self.store()
        T = self.text
        ask_peanut = "妮娅，你记得房主对花生过敏的事吗？"
        self.assertIn(T["peanut"], st.recall("nia", "private", ask_peanut, now=NOW))
        self.assertNotIn(T["peanut"], st.recall("xingli", "private", ask_peanut, now=NOW))         # not hers
        self.assertNotIn(T["peanut"], st.recall("nia", "group", ask_peanut, channel=G1, now=NOW))    # private stays private
        self.assertNotIn(T["peanut"], st.recall("nia", "group", ask_peanut, now=NOW))
        # her group memory: in that group, not in the other one, not for the housemate who was not there
        ask_cat = "妮娅，你记得二号群的猫叫什么吗？"
        self.assertIn(T["cat"], st.recall("nia", "group", ask_cat, channel=G2, now=NOW))
        self.assertNotIn(T["cat"], st.recall("nia", "group", ask_cat, channel=G1, now=NOW))
        self.assertIn(T["cat"], st.recall("nia", "private", ask_cat, now=NOW))      # as in the old store: her private chat sees her groups
        self.assertNotIn(T["cat"], st.recall("xingli", "private", ask_cat, now=NOW))
        ask_cake = "星璃，你记得周五要带什么吗？答应了蛋糕的事"
        self.assertIn(T["cake"], st.recall("xingli", "group", ask_cake, channel=G1, now=NOW))
        self.assertNotIn(T["cake"], st.recall("xingli", "group", ask_cake, channel=G2, now=NOW))
        self.assertNotIn(T["cake"], st.recall("nia", "group", ask_cake, channel=G1, now=NOW))      # hers, not nia's
        # shared in a group: every housemate, that group only
        ask_movie = "你们记得每周六晚上一起看什么吗？约好看电影的事"
        for who in NAMES:
            self.assertIn(T["movie"], st.recall(who, "group", ask_movie, channel=G1, now=NOW))
            self.assertNotIn(T["movie"], st.recall(who, "group", ask_movie, channel=G2, now=NOW))
        # the web group
        self.assertIn(T["hotpot"], st.recall("nia", "group", "大家喜欢吃什么？火锅的事", now=NOW))
        # world: the shared one and the diaries, as a world recall sees them
        self.assertIn(T["clock"], st.recall("nia", "world", "小屋的钟整点会怎样敲？", now=NOW))
        # a diary: her own, in every chat; nobody else's
        ask_diary = "你还记得那天下了一整天的雨，窗边读书的日记吗"
        self.assertIn("下了一整天的雨", st.recall("nia", "group", ask_diary, channel=G1, now=NOW))
        self.assertNotIn("下了一整天的雨", st.recall("xingli", "group", ask_diary, channel=G1, now=NOW))
        # the old store's invalidated, expired and the one the new filter refuses are nowhere
        for key in ("wrong", "gone", "vault"):
            for scope, ch in (("private", ""), ("group", G1), ("group", "")):
                self.assertNotIn(T[key], st.recall("nia", scope, T[key], channel=ch, now=NOW))
        self.assertFalse([e for e in self.entries(st) if e["text"] in (T["wrong"], T["gone"], T["vault"])])

    def test_the_outbound_check_knows_a_carried_private_memory(self):
        self.run_migrate()
        st = self.store()
        said = "房主对花生过敏，所以别点花生菜。"
        peanut = [e["id"] for e in st.list_entries("nia", "private", now=NOW) if e["text"] == self.text["peanut"]]
        self.assertEqual(st.check_outbound_leak("nia", "group", said, channel=G1, now=NOW), peanut)
        listed = self.store(leak_free_groups=[G2])
        self.assertEqual(listed.check_outbound_leak("nia", "group", said, channel=G2, now=NOW), [])
        self.assertEqual(st.check_outbound_leak("nia", "private", said, now=NOW), [])

    def test_a_carried_row_is_what_the_adapter_itself_would_have_made(self):
        """Save the same memories through the adapter in a fresh house and compare the rows it keeps."""
        self.run_migrate()
        ref_dir = tempfile.TemporaryDirectory()
        self.addCleanup(ref_dir.cleanup)
        ref = CommonMemoryStore(ref_dir.name, names=NAMES, env={})
        self.addCleanup(ref.close)
        old = self.old_rows()
        for i, r in old.items():
            if r["status"] != "active" or (r["expires_at"] and r["expires_at"] <= NOW) or r["text"] == SECRETISH:
                continue
            ref.add(r["owner"] if r["owner"] != "shared" else "nia", r["scope"], r["text"], channel=r["channel"],
                    about=r["about"], kind=r["kind"], shared=r["owner"] == "shared", source=r["source"],
                    now=r["created"])
        cols = "owner, scope, chat, about, kind, text, source, source_chat, sensitivity, status"
        mine = sorted(table(self.new_path, f"select {cols} from mem where status='active'"))
        want = sorted(table(Path(ref.db_path), f"select {cols} from mem where status='active'"))
        self.assertEqual(mine, want)
        self.assertEqual(sorted(table(self.new_path, "select * from kit_origin where scope='world' order by mem_id")
                                and [("world",)]), [("world",)])

    def test_times_counts_expiry_and_kind_are_kept(self):
        self.run_migrate()
        st = self.store()
        got = self.by_text(st)
        old = self.old_rows()
        for key in ("peanut", "blackout", "cat", "cake", "movie", "hotpot", "clock", "vote", "diary_nia", "diary_xl",
                    "eggplant", "tea"):
            r, e = old[self.ids[key]], got[self.text[key]]
            self.assertEqual((e["created"], e["updated"], e["last_used"], e["uses"], e["expires_at"]),
                             (r["created"], r["updated"], r["last_used"], r["uses"], r["expires_at"]), key)
        self.assertGreaterEqual(got[self.text["peanut"]]["uses"], 2)
        self.assertEqual(got[self.text["eggplant"]]["expires_at"], T0 + 10 * 11 + 100000)
        self.assertEqual(got[self.text["peanut"]]["kind"], "fact")
        self.assertEqual(got[self.text["cake"]]["kind"], "promise")
        self.assertEqual(got[self.text["cat"]]["source"], old[self.ids["cat"]]["source"])
        # the short-lived one is still short-lived: gone after its time
        later = T0 + 10 * 11 + 100001
        self.assertFalse([e for e in st.list_entries("nia", "private", now=later) if e["text"] == self.text["eggplant"]])

    def test_scopes_and_channels_come_out_as_the_old_store_listed_them(self):
        self.run_migrate()
        st = self.store()
        got = self.by_text(st)
        want = {"peanut": ("nia", "private", "private:nia"), "cat": ("nia", "group", G2), "cake": ("xingli", "group", G1),
                "movie": ("shared", "group", G1), "hotpot": ("nia", "group", "group"),
                "diary_nia": ("nia", "world", "world"), "diary_xl": ("xingli", "world", "world")}
        for key, (owner, scope, channel) in want.items():
            e = got[self.text[key]]
            self.assertEqual((e["owner"], e["scope"], e["channel"]), (owner, scope, channel), key)

    def test_kinds_the_new_store_has_no_place_for_are_reported(self):
        rep = self.run_migrate()
        self.assertEqual(rep["memories"]["kind_changed"], {"claim->fact": 1, "rule->promise": 1})

    def test_two_old_rows_that_become_one_new_row_are_merged_and_say_so(self):
        rep = self.run_migrate()
        a, b = self.ids["long_a"], self.ids["long_b"]
        self.assertEqual(rep["memories"]["merged_into_another_row"], 1)
        self.assertEqual(sorted(rep["memories"]["shortened_to_new_limit"]), [a])
        m = id_map(self.dir)
        self.assertEqual(m[a], m[b])
        old = self.old_rows()
        row = table(self.new_path, f"select created, updated, last_used, uses from mem where id={m[a]}")[0]
        self.assertEqual(row[0], min(old[a]["created"], old[b]["created"]))
        self.assertEqual(row[1], max(old[a]["updated"], old[b]["updated"]))
        self.assertEqual(row[3], old[a]["uses"] + old[b]["uses"])
        self.assertEqual(len(table(self.new_path, "select id from mem where kind='fact' and text like '房主的书架%'")), 1)

    def test_the_old_store_is_never_changed(self):
        before, listing = sha(self.old_path), sorted(os.listdir(self.dir))
        self.run_migrate()
        self.run_migrate()
        self.run_migrate(dry_run=True)
        self.assertEqual(sha(self.old_path), before)
        con = memory_migrate._open_old(self.old_path)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("delete from mem")
        con.close()
        self.assertEqual(sha(self.old_path), before)
        self.assertFalse([n for n in os.listdir(self.dir) if n.startswith("memory.db") and n not in listing])


class ReportTests(MigrateCase):
    def test_counts_per_outcome_and_no_memory_text_anywhere(self):
        rep = self.run_migrate()
        m = rep["memories"]
        self.assertEqual(m["in_old_file"], 18)
        self.assertEqual(m["copied"], 14)                       # 18 - invalidated - expired - refused - 1 merged
        self.assertEqual(m["merged_into_another_row"], 1)
        self.assertEqual(m["already_there"], 0)
        self.assertEqual(m["skipped"], {"secret_or_instruction": [self.ids["vault"]]})
        self.assertEqual(m["not_carried"]["invalidated"], {"total": 1, "by_kind": {"fact": 1}})
        self.assertEqual(m["not_carried"]["expired"], {"total": 1, "by_kind": {"fact": 1}})
        self.assertEqual(rep["pending_lines"]["copied"], 5)
        self.assertEqual(rep["pending_lines"]["not_carried"]["already_tidied"]["total"], 1)
        self.assertEqual(rep["evidence_links"]["carried"], 0)
        blob = json.dumps(rep, ensure_ascii=False) + "\n".join(describe(rep))
        for r in self.old_rows().values():
            self.assertNotIn(r["text"], blob)
        con = sqlite3.connect(f"file:{self.old_path}?mode=ro", uri=True)
        lines = [r[0] for r in con.execute("select line from seen")]
        con.close()
        for line in lines:
            self.assertNotIn(line, blob)
        self.assertIn("没有被改动", blob)
        self.assertIn(f"旧编号 {self.ids['vault']}", blob)

    def test_a_refused_row_is_not_in_the_new_file_but_is_in_the_map(self):
        self.run_migrate()
        st = self.store()
        self.assertFalse(table(self.new_path, f"select 1 from mem where text='{SECRETISH}'"))
        self.assertEqual(table(self.new_path, "select state, reason from kit_migrated where state='skipped'"),
                         [("skipped", "secret_or_instruction")])
        with self.assertRaises(ValueError):
            st.add("nia", "private", SECRETISH)   # the new store itself says no

    def test_no_old_file_means_nothing_to_do_and_no_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            rep = migrate(d, env={}, now=NOW)
            self.assertFalse(rep["old_found"])
            self.assertEqual(os.listdir(d), [])
            self.assertIn("没有找到旧记忆库", describe(rep)[0])

    def test_the_command_line_prints_sentences_and_a_dry_run_changes_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(memory_migrate.main([self.dir, "--dry-run", "--names", "nia=妮娅,xingli=星璃"]), 0)
        self.assertIn("试运行", out.getvalue())
        self.assertIn("旧记忆库里共 18 条记忆", out.getvalue())
        self.assertFalse(self.new_path.exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(memory_migrate.main([self.dir]), 0)
        self.assertIn("这次搬进新库 14 条", out.getvalue())
        self.assertTrue(self.new_path.exists())


class AgainTests(MigrateCase):
    def dump(self) -> Dict[str, List[tuple]]:
        return {t: table(self.new_path, f"select * from {t} order by 1, 2") for t in
                ("mem", "seen", "log", "kit_numbers", "kit_channels", "kit_origin")} | {
            "map": table(self.new_path, "select what, old_id, new_id, state, outcome, reason, old_trust, "
                                        "trust_applied from kit_migrated order by 1, 2")}

    def test_the_second_run_changes_nothing(self):
        self.run_migrate()
        before, listing, main_bytes = self.dump(), sorted(os.listdir(self.dir)), sha(self.new_path)
        rep = self.run_migrate(now=NOW + 99)
        self.assertEqual(self.dump(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), listing)          # no backup either
        self.assertEqual(sha(self.new_path), main_bytes)
        self.assertFalse(rep["wrote"])
        m = rep["memories"]
        self.assertEqual((m["copied"], m["merged_into_another_row"], m["already_there"]), (0, 0, 15))
        self.assertEqual(m["skipped_count"], 1)
        self.assertEqual((rep["pending_lines"]["copied"], rep["pending_lines"]["already_there"]), (0, 5))

    def test_a_row_added_to_the_old_store_later_is_carried_by_the_next_run_only(self):
        self.run_migrate()
        old = MemoryStore(self.dir, clock=Clock(NOW), names=NAMES, env={})
        old.add("nia", "private", "房主最近在学吉他，已经会弹三首歌了。", now=NOW + 5)
        old.close()
        rep = self.run_migrate(now=NOW + 10)
        self.assertEqual((rep["memories"]["copied"], rep["memories"]["already_there"]), (1, 15))
        self.assertEqual(len(table(self.new_path, "select 1 from mem where text like '房主最近在学吉他%'")), 1)

    def test_an_existing_new_file_is_copied_aside_once_per_writing_run_and_its_rows_stay(self):
        st = CommonMemoryStore(self.dir, names=NAMES, env={})
        mid, _ = st.add("nia", "private", "房主的生日在三月，妮娅记得送花。", now=T0)
        st.close()
        before = table(self.new_path, "select * from mem")
        rep = self.run_migrate()
        baks = [n for n in os.listdir(self.dir) if ".bak-" in n]
        self.assertEqual(baks, [rep["backup"]])
        self.assertEqual(table(Path(self.dir) / baks[0], "select * from mem"), before)
        self.assertIn(before[0], table(self.new_path, "select * from mem"))
        self.run_migrate(now=NOW + 5)
        self.assertEqual([n for n in os.listdir(self.dir) if ".bak-" in n], baks)

    def test_a_memory_the_new_store_already_has_is_not_touched_and_not_counted_as_copied(self):
        text = self.text["peanut"]
        st = CommonMemoryStore(self.dir, names=NAMES, env={})
        st.add("nia", "private", text, now=T0 + 3)
        st.close()
        row = table(self.new_path, "select created, updated, uses from mem")[0]
        rep = self.run_migrate()
        self.assertEqual(rep["memories"]["matched_existing_row"], 1)
        self.assertEqual(table(self.new_path, f"select created, updated, uses from mem where text='{text}'")[0], row)


class ResumeTests(MigrateCase):
    def clean_run_dump(self) -> Dict[str, List[tuple]]:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        build_old(d.name)
        migrate(d.name, env={}, names=NAMES, now=NOW)
        p = Path(d.name) / "memory-common.db"
        return self.snapshot(p)

    @staticmethod
    def snapshot(p: Path) -> Dict[str, List[tuple]]:
        return {"mem": table(p, "select * from mem order by id"), "seen": table(p, "select * from seen order by id"),
                "map": table(p, "select what, old_id, new_id, state, outcome, reason, old_trust, trust_applied "
                                "from kit_migrated order by 1, 2"),
                "origin": table(p, "select * from kit_origin order by 1")}

    def interrupted(self, method: str, after: int, *, real_call_first: bool) -> None:
        real = getattr(CommonMemoryStore, method)
        calls = {"n": 0}

        def wrapper(self_, *a, **kw):
            calls["n"] += 1
            if calls["n"] == after and not real_call_first:
                raise RuntimeError("cut off")
            out = real(self_, *a, **kw)
            if calls["n"] == after:
                raise RuntimeError("cut off")
            return out

        with mock.patch.object(CommonMemoryStore, method, wrapper):
            with self.assertRaises(RuntimeError):
                self.run_migrate()

    def test_a_run_cut_off_before_or_after_a_write_is_finished_by_the_next_run(self):
        want = self.clean_run_dump()
        for method, after, first in (("add", 5, False), ("add", 5, True), ("add", 1, True), ("add", 11, True),
                                     ("observe", 2, False), ("observe", 2, True), ("observe", 4, True)):
            with self.subTest(method=method, after=after, written=first):
                shutil_dir = tempfile.TemporaryDirectory()
                self.addCleanup(shutil_dir.cleanup)
                self.dir = shutil_dir.name
                build_old(self.dir)
                self.new_path = Path(self.dir) / "memory-common.db"
                self.interrupted(method, after, real_call_first=first)
                rep = self.run_migrate(now=NOW + 1)
                self.assertTrue(rep["wrote"])
                self.assertEqual(self.snapshot(self.new_path), want)
                self.assertEqual(rep["memories"]["waiting"], 0)
                again = self.snapshot(self.new_path)
                self.run_migrate(now=NOW + 2)
                self.assertEqual(self.snapshot(self.new_path), again)


class DryRunTests(MigrateCase):
    def test_a_dry_run_writes_nothing_anywhere_and_reports_what_a_real_run_reports(self):
        listing, before = sorted(os.listdir(self.dir)), sha(self.old_path)
        dry = self.run_migrate(dry_run=True)
        self.assertEqual(sorted(os.listdir(self.dir)), listing)
        self.assertFalse(self.new_path.exists())
        self.assertTrue(dry["dry_run"] and not dry["wrote"])
        real = self.run_migrate()
        for r in (dry, real):
            for k in ("dry_run", "wrote", "backup"):
                r.pop(k)
        self.assertEqual(dry, real)
        self.assertEqual(sha(self.old_path), before)

    def test_a_dry_run_leaves_an_existing_new_file_alone(self):
        st = CommonMemoryStore(self.dir, names=NAMES, env={})
        st.add("nia", "private", "房主的生日在三月，妮娅记得送花。", now=T0)
        st.close()

        def listing():
            # SQLite itself may drop an already-emptied -wal/-shm when the last reader closes: not the tool's writing
            return sorted(f for f in os.listdir(self.dir) if not f.endswith(("-wal", "-shm")))

        before, main_bytes = listing(), sha(self.new_path)
        rep = self.run_migrate(dry_run=True)
        self.assertEqual(listing(), before)
        self.assertEqual(sha(self.new_path), main_bytes)
        self.assertEqual(rep["memories"]["copied"], 14)


class PendingLineTests(MigrateCase):
    def test_lines_not_yet_tidied_are_kept_with_their_times_and_tidied_by_the_new_store(self):
        self.run_migrate()
        st = self.store(llm_fn=None)
        rows = table(self.new_path, "select consumer, ts, line, done, private from seen order by id")
        self.assertEqual(len(rows), 5)   # nia private: owner line + her own reply, nia group, nia world; xingli group
        self.assertTrue(all(r[3] == 0 for r in rows))
        self.assertEqual(sorted(r[1] for r in rows), [T0 + 900, T0 + 900, T0 + 910, T0 + 920, T0 + 930])
        self.assertFalse([r for r in rows if "已经整理过" in r[2]])
        model = FakeModel([add_op("房主想去海边看日出。", kind="event")])
        out = st.extract("nia", scope="private", force=True, llm_fn=model, now=NOW + 3000)
        self.assertEqual(out["added"], 1)
        self.assertIn("我今天想去海边看日出", model.calls[0][1])
        self.assertIn("好呀，我陪你去", model.calls[0][1])
        self.assertNotIn("已经整理过", model.calls[0][1])
        self.assertIn("房主想去海边看日出", st.recall("nia", "private", "我今天想去哪里看日出？海边", now=NOW + 3001))


class VectorTests(unittest.TestCase):
    def test_vectors_are_carried_as_they_are_and_missing_ones_stay_empty_until_backfilled(self):
        with tempfile.TemporaryDirectory() as d:
            build_old(d, vectors=True)
            old = sqlite3.connect(f"file:{Path(d) / 'memory.db'}?mode=ro", uri=True)
            old_vec = {r[0]: r[1] for r in old.execute("select text, vec from mem where vec is not null")}
            old.close()
            self.assertTrue(old_vec)
            rep = migrate(d, env={}, names=NAMES, now=NOW)
            new_vec = {r[0]: r[1] for r in table(Path(d) / "memory-common.db", "select text, vec from mem")}
            for text, vec in old_vec.items():
                if text in new_vec:
                    self.assertEqual(new_vec[text], vec)
            self.assertGreater(rep["memories"]["vectors"]["carried_this_run"], 5)
            self.assertEqual(rep["memories"]["vectors"]["left_empty"], 0)
        with tempfile.TemporaryDirectory() as d:
            build_old(d)
            rep = migrate(d, env={}, names=NAMES, now=NOW)
            self.assertEqual(rep["memories"]["vectors"]["carried_this_run"], 0)
            self.assertEqual(rep["memories"]["vectors"]["left_empty"], 15)
            self.assertIn("backfill_vectors", "\n".join(describe(rep)))
            st = CommonMemoryStore(d, names=NAMES, env={}, embed_plugin=lambda texts: fake_vectors(texts))
            try:
                self.assertEqual(st.backfill_vectors(), 14)
            finally:
                st.close()


class TrustTests(MigrateCase):
    def trust_of(self, st):
        got = self.by_text(st)
        return {k: got[self.text[k]]["trust"] for k in ("peanut", "blackout", "vote", "tea")}

    def test_with_the_switch_on_the_old_levels_come_along(self):
        rep = self.run_migrate(env={"AI_HOUSE_MEMORY_TRUST": "1"})
        st = self.store(env={"AI_HOUSE_MEMORY_TRUST": "1"})
        self.assertEqual(self.trust_of(st), {"peanut": "verified", "blackout": "hearsay", "vote": "group_stated",
                                             "tea": "group_stated"})
        self.assertTrue(rep["memories"]["trust"]["switch_on"])
        self.assertEqual(rep["memories"]["trust"]["waiting_for_the_switch"], 0)

    def test_with_the_switch_off_they_are_kept_and_a_later_run_with_it_on_writes_them(self):
        rep = self.run_migrate()
        self.assertEqual(rep["memories"]["trust"]["levels_below_verified"], 3)
        self.assertEqual(rep["memories"]["trust"]["waiting_for_the_switch"], 3)
        self.assertEqual(sorted(r[0] for r in table(self.new_path, "select old_trust from kit_migrated "
                                                                   "where old_trust!='verified' and what='mem'")),
                         ["group_stated", "group_stated", "hearsay"])
        self.assertIn("AI_HOUSE_MEMORY_TRUST=1", "\n".join(describe(rep)))
        # the house runs a while on the new store with the switch off ...
        st = CommonMemoryStore(self.dir, names=NAMES, env={})
        st.add("nia", "private", "房主新养了一盆绿萝，放在窗边。", now=NOW + 1)
        st.close()
        # ... then turns it on: the column appears (all 'verified'), the next migration run writes the old levels
        on = {"AI_HOUSE_MEMORY_TRUST": "1"}
        st = CommonMemoryStore(self.dir, names=NAMES, env=on)
        self.addCleanup(st.close)
        self.assertEqual(self.trust_of(st), {"peanut": "verified", "blackout": "verified", "vote": "verified",
                                             "tea": "verified"})
        rep = self.run_migrate(env=on, now=NOW + 50)
        self.assertTrue(rep["wrote"])
        self.assertEqual(rep["memories"]["trust"]["waiting_for_the_switch"], 0)
        self.assertEqual(rep["memories"]["copied"], 0)
        self.assertEqual(self.trust_of(st), {"peanut": "verified", "blackout": "hearsay", "vote": "group_stated",
                                             "tea": "group_stated"})
        self.assertEqual(self.by_text(st)["房主新养了一盆绿萝，放在窗边。"]["trust"], "verified")


class SettingsTests(MigrateCase):
    def test_the_houses_own_settings_decide_how_a_diary_and_a_private_memory_are_kept(self):
        self.run_migrate(env={"AI_HOUSE_MEMORY_DIARY_PRIVATE": "1"})
        st = self.store(env={"AI_HOUSE_MEMORY_DIARY_PRIVATE": "1"})
        ask_diary = "你还记得那天下了一整天的雨，窗边读书的日记吗"
        self.assertNotIn("下了一整天的雨", st.recall("nia", "group", ask_diary, channel=G1, now=NOW))
        self.assertIn("下了一整天的雨", st.recall("nia", "private", ask_diary, now=NOW))

    def test_private_in_groups_shows_the_carried_private_memory_in_a_group_with_the_hush_note(self):
        self.run_migrate(env={"AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS": "1"})
        st = self.store(env={"AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS": "1"})
        got = st.recall("nia", "group", "妮娅，你记得房主对花生过敏的事吗？", channel=G1, now=NOW)
        self.assertIn(self.text["peanut"], got)
        self.assertIn("别在这里说出来", got)


if __name__ == "__main__":
    unittest.main()


class HerOwnWorldMemoryTests(unittest.TestCase):
    def test_a_world_memory_of_one_housemate_stays_hers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = MemoryStore(tmp.name, names=NAMES, env={}, clock=Clock(T0))
        old.add("nia", "world", "妮娅今天把阳台的薄荷移到了窗边。", now=T0)
        old.add("nia", "world", "小屋的规矩是每周六晚上一起大扫除。", shared=True, now=T0)
        ask = "阳台的薄荷移到了窗边吗"
        self.assertIn("薄荷", old.recall("nia", "world", ask, now=T0 + 60))
        self.assertNotIn("薄荷", old.recall("xingli", "world", ask, now=T0 + 60))
        old.close()
        self.assertEqual(migrate(tmp.name, env={}, names=NAMES, now=NOW)["memories"]["copied"], 2)
        st = CommonMemoryStore(tmp.name, names=NAMES, env={}, clock=Clock(NOW))
        self.addCleanup(st.close)
        self.assertIn("薄荷", st.recall("nia", "world", ask, now=NOW + 60))
        for scope in ("world", "group", "private"):
            self.assertNotIn("薄荷", st.recall("xingli", scope, ask, now=NOW + 60), scope)
        for who in ("nia", "xingli"):
            self.assertIn("大扫除", st.recall(who, "world", "每周六晚上一起大扫除吗", now=NOW + 60), who)


class DamagedOldRowTests(unittest.TestCase):
    def test_an_expiry_that_is_not_a_time_does_not_stop_the_run(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = MemoryStore(tmp.name, names=NAMES, env={}, clock=Clock(T0))
        mid, _ = old.add("nia", "private", "房主的生日在三月，妮娅记得送花。", now=T0)
        old.add("nia", "private", "房主养了一只叫团子的橘猫。", now=T0)
        old.close()
        with contextlib.closing(sqlite3.connect(Path(tmp.name) / "memory.db")) as c, c:
            c.execute("update mem set expires_at='soon' where id=?", (mid,))
        rep = migrate(tmp.name, env={}, names=NAMES, now=NOW)
        self.assertEqual(rep["memories"]["copied"], 2)
        st = CommonMemoryStore(tmp.name, names=NAMES, env={}, clock=Clock(NOW))
        self.addCleanup(st.close)
        rows = st.list_entries("nia", "private", now=NOW)
        self.assertEqual([r["expires_at"] for r in rows], [None, None])  # as in the old store: it never came due
