"""M2 step B: `CommonIntents` (ai_house.intents_common) puts the shared promise module in the house.

No network, no real model: the memory model is a fake function that answers with the ops it was given. Names, numbers
and the pack are the kit's own. Every test says, in its docstring, what would make it fail.
"""
import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from ai_house.intents_common import CommonIntents  # noqa: E402
from ai_house.memory_common import CommonMemoryStore  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402

INDIE = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
T0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
OWNER_LINE = "顺便帮家里买2个鸡蛋"


def setUpModule():
    import warnings

    warnings.filterwarnings("ignore", category=ResourceWarning)


def egg_ops(name: str) -> list:
    return [{"op": "intent", "text": f"{name}买2个鸡蛋", "kind": "promise", "for": ["房主"], "agreed": [name],
             "scope": "world", "when": "vague", "quote": OWNER_LINE}]


def fake_model(ops):
    calls = []

    def model(system, user):
        calls.append((system, user))
        return {"ops": ops}

    model.calls = calls
    return model


def rows(path, sql, *params):
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.row_factory = sqlite3.Row
        return c.execute(sql, params).fetchall()


def queue_size(path):
    with contextlib.closing(sqlite3.connect(path)) as c:
        if not c.execute("select 1 from sqlite_master where name='owner_chat_queue'").fetchone():
            return 0
        return c.execute("select count(*) from owner_chat_queue").fetchone()[0]


def columns(path, table):
    return [r[1] for r in rows(path, f"pragma table_info({table})")]


def line(card, number, text, n=1):
    return f"[{card}|{number}] (message_id={n}): {text}"


def standalone(db, env=None, clock=None, **kw):
    env = {"INTENTS": "1", "WORLD_KERNEL": "authority", "INTENTS_EXTRACT_V2": "1", **(env or {})}
    ci = CommonIntents(db, vocab=V, env=env, clock=clock, **kw)
    ci.init()
    return ci


class Dir(unittest.TestCase):
    def tmp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return t.name

    def daemon(self, pack=None, env=None):
        d = HouseDaemon(self.tmp(), pack_dir=pack, env=env or {}, start_ts=T0, virtual_clock=True,
                        allow_onboarding=False)
        self.addCleanup(d.close)
        return d


class CarryOverTests(Dir):
    def old_db(self):
        path = os.path.join(self.tmp(), "world.db")
        with contextlib.closing(sqlite3.connect(path)) as c:
            with c:
                c.executescript("""
                create table intent_evidence(key text primary key, stream text not null, event_id integer not null,
                    at real not null, body text not null);
                create table owner_chat_queue(id integer primary key autoincrement, actor text not null,
                    body text not null, state text not null default 'queued', created_at real not null default 5);
                create table house_intents(intent_id text primary key, actor text not null, summary text not null,
                    kind text not null default 'chat_promise', state text not null default 'open', item text,
                    raw_item text not null default '', qty integer not null default 1, created_at real not null,
                    expires_at real not null, completed_at real, evidence_key text, updated_at real not null default 0);
                insert into intent_evidence values('k1','s',1,10,'{"kind":"purchased"}'),('k2','s',2,11,'{}');
                insert into owner_chat_queue(actor, body, state, created_at) values('nell','{}','taken',77);
                insert into house_intents(intent_id, actor, summary, kind, state, created_at, expires_at) values
                  ('chat:1:0','nell','记住房主饮食禁忌：我对虾仁过敏，一点都别放','memo','open',1,9e9),
                  ('chat:1:1','nell','记住房主饮食禁忌：不吃香菜','memo','completed',1,9e9),
                  ('chat:1:2','nell','顺便帮家里买2个鸡蛋','errand','open',1,9e9),
                  ('chat:1:3','someone_gone','记住房主饮食禁忌：不吃芹菜','memo','open',1,9e9);
                """)
        return path

    def test_old_columns_are_carried_once_and_the_memo_becomes_a_memory(self):
        """Fails if the `state` column is not added, history is not marked consumed, `created_at` is not renamed (the
        shared module inserts `created`), the memo is not carried or is carried again at a restart, or an open
        errand / a closed memo is carried too."""
        path = self.old_db()
        store = CommonMemoryStore(self.tmp(), names={"nell": "奈尔"}, env={})
        self.addCleanup(store.close)
        ci = CommonIntents(path, vocab=V, env={"INTENTS": "1"}, memory=store)
        ci.init()
        self.assertIn("state", columns(path, "intent_evidence"))
        self.assertEqual({r["state"] for r in rows(path, "select state from intent_evidence")}, {"consumed"})
        self.assertIn("created", columns(path, "owner_chat_queue"))
        self.assertNotIn("created_at", columns(path, "owner_chat_queue"))
        self.assertEqual(rows(path, "select actor, state, created from owner_chat_queue")[0]["created"], 77)
        with contextlib.closing(sqlite3.connect(path)) as c:  # the shared module's own insert works
            with c:
                c.execute("insert into owner_chat_queue(actor, body, state, created) values('nell','{}','queued',1)")
        mems = store.list_entries("nell", "private")
        self.assertEqual(len(mems), 1)
        self.assertIn("虾仁过敏", mems[0]["text"])
        self.assertEqual(mems[0]["kind"], "preference")
        self.assertEqual(store.list_entries("nell", "group"), [])  # not visible outside the private chat
        # a new evidence row after the carry-over is judged: a restart must not mark it consumed
        with contextlib.closing(sqlite3.connect(path)) as c:
            with c:
                c.execute("insert into intent_evidence values('k3','s',3,12,'{}','queued')")
        again = CommonIntents(path, vocab=V, env={"INTENTS": "1"}, memory=store)
        again.init()
        again.init()
        self.assertEqual(len(store.list_entries("nell", "private")), 1)
        self.assertEqual(rows(path, "select state from intent_evidence where key='k3'")[0]["state"], "queued")
        # the old table stays on disk, unread and unchanged
        old = rows(path, "select intent_id, state from house_intents order by intent_id")
        self.assertEqual([(r["intent_id"], r["state"]) for r in old],
                         [("chat:1:0", "open"), ("chat:1:1", "completed"), ("chat:1:2", "open"), ("chat:1:3", "open")])
        # the memo of someone who no longer lives here is not carried, and does not make her a housemate again
        self.assertEqual([m for m in store.list_entries("nell", "private") if "芹菜" in m["text"]], [])
        self.assertNotIn("someone_gone", store._residents)

    def test_without_a_memory_store_nothing_is_marked_so_a_later_start_carries_the_memo(self):
        """Fails if the carry-over is recorded as done although no memory store could take the memo."""
        path = self.old_db()
        standalone(path)
        self.assertEqual(rows(path, "select name from sqlite_master where name='intents_carry'"), [])
        store = CommonMemoryStore(self.tmp(), names={"nell": "奈尔"}, env={})
        self.addCleanup(store.close)
        CommonIntents(path, vocab=V, env={"INTENTS": "1"}, memory=store).init()
        self.assertEqual(len(store.list_entries("nell", "private")), 1)

    def test_an_empty_world_db_and_a_fresh_house_just_work(self):
        """Fails if init() assumes the old tables exist."""
        path = os.path.join(self.tmp(), "w.db")
        ci = standalone(path)
        ci.init()
        self.assertIn("created", columns(path, "intents") + columns(path, "intents"))
        self.assertIn("state", columns(path, "intent_evidence"))


class EndToEndTests(Dir):
    def run_errand(self, pack, actor, name):
        d = self.daemon(pack)
        self.assertIsInstance(d.intents, CommonIntents)
        d.memory.observe(actor, "private", OWNER_LINE, speaker_role="owner", speaker="房主", reply="好的", now=T0)
        self.assertEqual(d.intents.errands(actor, T0 + 1), [])  # nothing is recorded by the chat turn itself
        self.assertEqual(queue_size(d.world_db), 0)
        model = fake_model(egg_ops(name))
        d.memory.extract(force=True, now=T0 + 5, llm_fn=model)
        self.assertIn("意图", model.calls[0][0])  # the extraction prompt carries the promise instructions
        got = d.intents.errands(actor, T0 + 10)
        self.assertEqual([(e["item"], e["qty"], e["beneficiary"], e["account"]) for e in got],
                         [("egg", 2, ["owner"], actor)])
        queued = rows(d.world_db, "select actor, state, body from owner_chat_queue")
        self.assertEqual([(r["actor"], r["state"]) for r in queued], [(actor, "queued")])
        self.assertIn(OWNER_LINE, json.loads(queued[0]["body"])["owner_said"])
        listed = d.intents.list_intents(actor=actor)
        self.assertEqual([(r["state"], r["status"]) for r in listed], [("open", "accepted")])
        for _ in range(12):  # the kernel sends her shopping and its purchase receipt closes the promise
            d.fast_forward(240)
            if d.intents.list_intents(actor=actor)[0]["status"] == "completed":
                break
        self.assertEqual(d.intents.list_intents(actor=actor)[0]["state"], "completed")
        self.assertEqual(d.intents.errands(actor, d.clock.now()), [])

    def test_owner_private_chat_to_accepted_promise_to_purchase_on_the_default_pack(self):
        """Fails if the daemon records at chat time, extraction does not reach `apply_chat`, `errands` does not
        see the promise, the owner's line does not reach `owner_chat_queue`, or a kernel purchase does not close it."""
        self.run_errand(None, "nell", "奈尔")

    def test_the_same_on_the_bundled_indie_studio_pack(self):
        """Fails if `promise_rules` is still bound to the default six people (it never completes anything here)."""
        self.run_errand(INDIE, "sora", "苍空")

    def test_the_chat_turn_alone_records_nothing(self):
        """Fails if the daemon still records a promise by rule (or queues the owner's line) when the owner speaks."""
        d = self.daemon()
        d.converse("private:nell", OWNER_LINE, owner_speaker())
        self.assertEqual(d.intents.list_intents(), [])
        self.assertEqual(queue_size(d.world_db), 0)
        self.assertFalse(hasattr(d.intents, "enqueue_chat"))
        self.assertFalse(hasattr(d.intents, "cancel"))

    def test_the_hook_follows_a_reload_of_the_memory_copy(self):
        """Fails if the extraction hook of a reloaded memory copy no longer reaches the promise code (new names make
        the store load a new copy)."""
        d = self.daemon()
        first = d.memory._mod
        self.assertIs(first.INTENTS, d.intents.intents_module)
        other = next(k for k in d.memory.names if k != "nell")
        d.memory.names = {**d.memory.names, other: "新名字"}
        self.assertIsNot(d.memory._mod, first)
        self.assertIs(d.memory._mod.INTENTS, d.intents.intents_module)
        self.assertIs(d.intents.intents_module, __import__("ai_house.common.loader", fromlist=["x"]).part(d.memory._mod, "intents"))
        d.memory.observe("nell", "private", OWNER_LINE, speaker_role="owner", speaker="房主", reply="好的", now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model(egg_ops("奈尔")))
        self.assertEqual(len(d.intents.errands("nell", T0 + 10)), 1)

    def test_daemon_defaults_and_a_deployers_value_wins(self):
        """Fails if a default is missing, the memory store does not extract urgently by default, or a deployer's
        own value is overwritten."""
        d = self.daemon()
        env = d.intents._env
        for k, v in {"INTENTS": "1", "INTENTS_SHOW": "0", "INTENTS_EVIDENCE_GATE": "1", "INTENTS_KERNEL_MATCH": "on",
                     "INTENTS_EXTRACT_V2": "1", "INTENTS_EXPIRY_V2": "1", "INTENTS_DECIDE_CTX": "1",
                     "WORLD_KERNEL_ERRANDS_FROM_INTENTS": "1", "INTENTS_PRESENCE": "0",
                     "WORLD_KERNEL": "authority"}.items():
            self.assertEqual(env.get(k), v, k)
        self.assertTrue(d.memory._mod.URGENT)
        i = d.intents.intents_module
        self.assertEqual((i.ON, i.SHOW, i.EVIDENCE_GATE, i.PRESENCE_ON), (True, False, True, False))
        self.assertEqual(d.intents.world_module.HOME_CHAT, "group:1")
        d2 = self.daemon(env={"INTENTS_SHOW": "1", "AI_HOUSE_MEMORY_URGENT": "0", "INTENTS_EVIDENCE_GATE": "0"})
        self.assertEqual(d2.intents._env["INTENTS_SHOW"], "1")
        self.assertFalse(d2.memory._mod.URGENT)
        self.assertEqual((d2.intents.intents_module.SHOW, d2.intents.intents_module.EVIDENCE_GATE), (True, False))

    def test_a_setting_left_blank_in_the_env_file_counts_as_unset(self):
        """Fails if a blank value (what `NAME=` in .env gives) turns a default-on switch off: with the urgent
        extraction or the promise code off, the owner's errand would never reach the world."""
        d = self.daemon(env={"AI_HOUSE_MEMORY_URGENT": "", "INTENTS": " ", "INTENTS_SHOW": "", "INTENTS_PRESENCE": "",
                             "INTENTS_EXTRACT_V2": "", "WORLD_KERNEL": "shadow"})
        self.assertTrue(d.memory._mod.URGENT)
        i = d.intents.intents_module
        self.assertEqual((i.ON, i.SHOW, i.PRESENCE_ON), (True, False, False))
        self.assertEqual(d.intents._env["INTENTS_EXTRACT_V2"], "1")
        self.assertEqual(d.intents._env["WORLD_KERNEL"], "authority")  # the kit has no other mode


class IsolationTests(Dir):
    def promise_in_group(self, ci, now=None):
        i = ci.intents_module
        n = i.ID.ACTOR_QQ["nell"]
        ops = [{"op": "intent", "text": "奈尔下午去公园散步", "kind": "plan", "quote": "我下午去公园散步"}]
        lines = [line("奈尔(你)", n, "我下午去公园散步")]
        return i.apply_chat("nell", "group:1", False, ops, lines, now=now, times=[now] if now else None)

    def test_two_houses_in_one_process_keep_their_own_clock_and_env(self):
        """Fails if the clock or the env mapping of one house leaks into the other (module globals shared, the
        process env read, or a calls-without-`now` default taken from the wall clock)."""
        a_t, b_t = [T0], [T0 + 5 * 86400]
        a = standalone(os.path.join(self.tmp(), "a.db"), {"INTENTS_SHOW": "1"}, clock=lambda: a_t[0])
        b = standalone(os.path.join(self.tmp(), "b.db"), {"INTENTS_SHOW": "0"}, clock=lambda: b_t[0])
        self.assertIsNot(a.intents_module, b.intents_module)
        self.assertEqual(self.promise_in_group(a)["intents"], 1)
        self.assertEqual(self.promise_in_group(b)["intents"], 1)
        self.assertEqual(rows(a.world_db, "select created from intents")[0]["created"], T0)
        self.assertEqual(rows(b.world_db, "select created from intents")[0]["created"], T0 + 5 * 86400)
        self.assertEqual(len(a.card_lines("nell")), 1)  # SHOW on in a only (and no `now` given)
        self.assertEqual(b.card_lines("nell"), [])
        a_t[0] += 3600  # the house clock moves, the other one does not
        self.assertEqual(len(a.listing()), 1)
        self.assertEqual(rows(a.world_db, "select count(*) n from intents")[0]["n"], 1)
        self.assertNotIn("INTENTS_SHOW", os.environ)  # the process env was never the source
        self.assertEqual(a.world_module.DB_PATH, a.world_db)
        self.assertEqual(b.world_module.DB_PATH, b.world_db)

    def test_the_process_env_is_not_read(self):
        """Fails if the module reads `os.environ` instead of the mapping it was given."""
        os.environ["INTENTS_EXPIRY_V2"] = "1"
        self.addCleanup(os.environ.pop, "INTENTS_EXPIRY_V2", None)
        ci = standalone(os.path.join(self.tmp(), "w.db"), {"INTENTS_EXPIRY_V2": "0"}, clock=lambda: T0)
        self.assertEqual(ci.intents_module.os.environ.get("INTENTS_EXPIRY_V2"), "0")
        self.assertIsNone(ci.intents_module.os.environ.get("INTENTS_NOT_THERE"))


class OwnerNameTests(Dir):
    def joint(self, lines_cards, with_name):
        ci = standalone(os.path.join(self.tmp(), "w.db"), clock=lambda: T0)
        i = ci.intents_module
        n = i.ID.ACTOR_QQ["nell"]
        lines = [line(card, number if number else n, text, k + 1) for k, (card, number, text) in enumerate(lines_cards(n))]
        ops = [{"op": "intent", "text": "奈尔和房主周末一起去公园玩", "kind": "joint", "with": [with_name],
                "agreed": [with_name, "奈尔"], "quote": "周末一起去公园玩吧"}]
        done = i.apply_chat("nell", "group:1", False, ops, lines, now=T0, times=[T0] * len(lines))
        return done, ci.listing()

    def test_the_name_the_house_shows_for_the_owner_resolves(self):
        """Fails if a model that writes the owner's display name (not 房主) into `with` / `agreed` is taken for
        nobody (the roster made here knows the owner only as 房主)."""
        done, listed = self.joint(lambda n: [("太郎", 1, "周末一起去公园玩吧"), ("奈尔(你)", n, "好呀，一起去")], "太郎")
        self.assertEqual(done["intents"], 1)
        self.assertEqual(listed[0]["party"], ["nell", "owner"])
        self.assertEqual(listed[0]["status"], "accepted")

    def test_a_stranger_wearing_the_same_name_does_not_become_the_owner(self):
        """Fails if the name is resolved although another number in the batch shows the same card."""
        done, listed = self.joint(
            lambda n: [("太郎", 1, "周末一起去公园玩吧"), ("太郎", 9001, "我也去"), ("奈尔(你)", n, "好呀，一起去")], "太郎")
        self.assertEqual(done["intents"], 0)
        self.assertEqual(listed, [])

    def test_the_default_name_still_works(self):
        """Fails if the wrapper breaks the shared lookup of 房主."""
        done, _ = self.joint(lambda n: [("房主", 1, "周末一起去公园玩吧"), ("奈尔(你)", n, "好呀，一起去")], "房主")
        self.assertEqual(done["intents"], 1)


class ListIntentsTests(Dir):
    def put(self, path, status, text, visibility="group", refs="[]", actor="nell", party=None, created=100.0):
        with contextlib.closing(sqlite3.connect(path)) as c:
            with c:
                c.execute(
                    "insert into intents(actor, party, agreed, kind, text, due_from, due_to, status, source, refs,"
                    " visibility, confidence, created, updated, last_action) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (actor, json.dumps(party or [actor]), json.dumps(party or [actor]), "promise", text, None, None,
                     status, "chat", refs, visibility, 0.7, created, created, created))

    def test_rows_in_the_shape_the_page_reads(self):
        """Fails if a private status is mapped to the wrong page state, a field of days.js is missing or renamed,
        the item is not read from the promise text, the kernel key is not the evidence key, a private row's text
        leaks, or the `state` / `actor` / `limit` arguments do nothing."""
        path = os.path.join(self.tmp(), "w.db")
        ci = standalone(path)
        k = json.dumps([{"kernel": "world#7", "kind": "fulfilled", "at": 5, "end": "completed"}])
        self.put(path, "accepted", "奈尔买2个鸡蛋", created=101)
        self.put(path, "proposed", "奈尔和婉妲一起去公园", party=["nell", "wanda"], created=102)
        self.put(path, "completed", "奈尔买2个鸡蛋", refs=k, created=103)
        self.put(path, "expired", "奈尔去图书馆", created=104)
        self.put(path, "missed", "奈尔去书店", created=105)
        self.put(path, "declined", "奈尔去海边", created=106)
        self.put(path, "cancelled", "奈尔去山上", created=107)
        self.put(path, "accepted", "奈尔买2个鸡蛋", visibility="private", created=108,
                 refs=json.dumps([{"chat": "private", "quote": "顺便帮家里买2个鸡蛋", "at": 8}]))
        self.put(path, "accepted", "婉妲买2盒牛奶", actor="wanda", created=109)
        everything = ci.list_intents(limit=50)
        by = {(r["status"], r["summary"]): r for r in everything}
        want = {"accepted": "open", "proposed": "open", "completed": "completed", "expired": "expired",
                "missed": "expired", "declined": "cancelled", "cancelled": "cancelled"}
        for r in everything:
            self.assertEqual(r["state"], want[r["status"]], r["status"])
            for key in ("intent_id", "actor", "state", "status", "created_at", "summary", "item", "qty", "evidence_key"):
                self.assertIn(key, r)
            self.assertIsInstance(r["intent_id"], str)
        done = by[("completed", "奈尔买2个鸡蛋")]
        self.assertEqual((done["item"], done["qty"], done["evidence_key"], done["created_at"]), ("egg", 2, "world#7", 103))
        self.assertEqual((by[("accepted", "婉妲买2盒牛奶")]["item"], by[("accepted", "婉妲买2盒牛奶")]["qty"]), ("milk", 2))
        self.assertEqual(by[("proposed", "奈尔和婉妲一起去公园")]["item"], "")
        private = [r for r in everything if r["visibility"] == "private"][0]
        self.assertEqual(private["summary"], "（房主私聊里的约定）")
        self.assertEqual((private["item"], private["qty"], private["evidence_key"]), ("", 0, ""))
        self.assertNotIn("鸡蛋", json.dumps(private, ensure_ascii=False))
        self.assertTrue(all("wanda" in r["party"] for r in ci.list_intents(actor="wanda")))
        self.assertEqual(len(ci.list_intents(actor="wanda", limit=50)), 2)  # hers, and the joint one with nell
        self.assertEqual({r["state"] for r in ci.list_intents(state="open", limit=50)}, {"open"})
        self.assertEqual(len(ci.list_intents(state="cancelled", limit=50)), 2)
        self.assertEqual(len(ci.list_intents(state="declined", limit=50)), 1)  # a private status name works too
        self.assertEqual(len(ci.list_intents(limit=3)), 3)
        self.assertEqual(ci.list_intents(state="no-such-state"), [])
        newest_first = [r["created_at"] for r in everything]
        self.assertEqual(newest_first, sorted(newest_first, reverse=True))


class ShownTests(Dir):
    def context(self, env):
        d = self.daemon(env=env)
        i = d.intents.intents_module
        n = d.memory._number_of("nell")
        ops = [{"op": "intent", "text": "奈尔下午去公园散步", "kind": "plan", "quote": "我下午去公园散步"}]
        got = i.apply_chat("nell", "group:1", False, ops, [line("奈尔(你)", n, "我下午去公园散步")], now=T0, times=[T0])
        self.assertEqual(got["intents"], 1)
        seen = []
        real = d.chat_agent.reply_as

        def spy(*a, **kw):
            seen.append(kw.get("memory_context") or "")
            return real(*a, **kw)

        d.chat_agent.reply_as = spy
        d.converse("group", "大家下午好", owner_speaker(), actor="nell")
        self.assertTrue(seen)
        return d, seen[-1]

    def test_card_lines_reach_the_chat_context_only_when_shown(self):
        """Fails if the chat context carries promises with INTENTS_SHOW off (the default), or none with it on."""
        d, ctx = self.context({})
        self.assertNotIn("公园散步", ctx)
        self.assertEqual(d.intents.card_lines("nell", T0), [])
        d, ctx = self.context({"INTENTS_SHOW": "1"})
        self.assertIn("奈尔下午去公园散步", ctx)
        self.assertEqual(d.intents.card_lines("nell", T0), ["奈尔下午去公园散步"])


class CancelTests(Dir):
    def test_cancel_answers_not_supported_and_the_list_still_works(self):
        """Fails if the route is gone (404), cancels something, answers ok, or the list endpoint breaks."""
        tok = "priv-intents-token-01"
        d = self.daemon(env={"AI_HOUSE_PRIVATE_TOKEN": tok})
        d.memory.observe("nell", "private", OWNER_LINE, speaker_role="owner", speaker="房主", reply="好的", now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model(egg_ops("奈尔")))
        server = make_http_server(d, "127.0.0.1", 0)
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, method="GET" if body is None else "POST",
                                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        status, out = call("/api/intents?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual([(r["actor"], r["state"]) for r in out["intents"]], [("nell", "open")])
        status, out = call("/api/intents/cancel", {"intent_id": out["intents"][0]["intent_id"]})
        self.assertEqual((status, out["ok"], out["code"]), (501, False, "not_supported"))
        self.assertIn("不支持", out["error"])
        status, out = call("/api/intents?limit=10")
        self.assertEqual([r["state"] for r in out["intents"]], ["open"])


class PresenceTests(Dir):
    def run_owner_line(self, presence):
        path = os.path.join(self.tmp(), "w.db")
        env = {"INTENTS_PRESENCE": presence} if presence is not None else {}
        ci = standalone(path, env, clock=lambda: T0)
        i = ci.intents_module
        n = i.ID.ACTOR_QQ["nell"]
        lines = [line("房主", 1, "我在厨房", 1), line("奈尔(你)", n, "好的", 2)]
        i.apply_chat("nell", "private:1", True, [], lines, times=[T0, T0])
        tables = {r["name"] for r in rows(path, "select name from sqlite_master where type='table'")}
        queued = rows(path, "select * from owner_avatar_queue") if "owner_avatar_queue" in tables else []
        chat = rows(path, "select * from owner_chat_queue")
        return queued, chat

    def test_presence_off_writes_nothing_to_the_avatar_queue(self):
        """Fails if the owner's room is read out of chat with the switch at 0; the control (switch at 1) shows the
        same line does queue a command, and the owner's line still reaches `owner_chat_queue` either way."""
        queued, chat = self.run_owner_line("0")
        self.assertEqual(queued, [])
        self.assertEqual(len(chat), 1)
        queued, chat = self.run_owner_line("1")
        self.assertEqual(len(queued), 1)
        self.assertIn('"kitchen"', queued[0]["body"])
        self.assertEqual(len(chat), 1)
        queued, _ = self.run_owner_line(None)  # the shared default is on; the daemon's is 0 (tested above)
        self.assertEqual(len(queued), 1)


if __name__ == "__main__":
    unittest.main()
