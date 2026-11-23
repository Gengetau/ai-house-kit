"""Tail batch 5: reset_house channel hook, epoch guard, tolerant plugin config, daemon lock, album, reminders, HTTP 400."""
import base64
import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import owner_speaker
from ai_house.daemon import JST, HouseDaemon, make_http_server
from test_p1_onebot import OWNER, Harness, private_msg, wait_until

PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


class ResetKeepsOneBotInboundAlive(unittest.TestCase):
    """N1: reset_house deleted channel_spool.db without rebuilding it, so every later QQ message failed."""

    def test_inbound_message_after_reset_is_handled(self):
        h = Harness(self)
        h.fake.push(private_msg(OWNER, "重置之前"))
        self.assertTrue(wait_until(lambda: h.private_to(OWNER)))
        h.daemon.reset_house()
        # the inbound cache must be usable and empty right after the reset
        with contextlib.closing(sqlite3.connect(h.rt / "channel_spool.db")) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM inbound_spool").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM inbound_dedup").fetchone()[0], 0)
        h.daemon.complete_setup({"mode": "first_bot", "resident": {"name": "奈尔", "actor": "nell"}})
        h.daemon.chat_agent._llm_fn = h.model  # the chat agent is rebuilt by the reset; re-attach the fake model
        n = len(h.private_to(OWNER))
        h.fake.push(private_msg(OWNER, "重置之后"))
        self.assertTrue(wait_until(lambda: len(h.private_to(OWNER)) > n), "no reply after reset")
        self.assertEqual(h.ch.stats["dropped"], 0)
        def spool_states():
            with contextlib.closing(sqlite3.connect(h.rt / "channel_spool.db")) as c:
                return [r[0] for r in c.execute("SELECT state FROM inbound_spool")]

        self.assertTrue(wait_until(lambda: spool_states() == ["done"]), spool_states())



def _slow_daemon(tc, tmpname=None):
    """A daemon whose chat model blocks on `release` once it is asked to answer (signals `entered`)."""
    entered, release = threading.Event(), threading.Event()

    def slow_llm(_sys, usr):
        if "刚刚说：" in usr:
            entered.set()
            release.wait(timeout=5.0)
        return "慢慢想出来的回复SLOWREPLY", 10

    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    rt = Path(tmp.name) / "rt"
    t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
    d = HouseDaemon(rt, pack_dir=PACK, env={}, start_ts=t0, virtual_clock=True, allow_onboarding=False, llm_fn=slow_llm)
    tc.addCleanup(d.close)
    return d, rt, entered, release


class InFlightReplySurvivesNonResetMutations(unittest.TestCase):
    """Only a real reset may drop an in-flight reply; adding/updating a resident must not."""

    def _turn_during(self, mutate):
        d, rt, entered, release = _slow_daemon(self)
        holder = []
        th = threading.Thread(
            target=lambda: holder.append(d.converse("private:sora", "慢慢想", owner_speaker("房主"))), daemon=True
        )
        th.start()
        self.assertTrue(entered.wait(3.0))
        mutate(d)
        release.set()
        th.join(5.0)
        self.assertTrue(holder, "converse never returned")
        return d, holder[0], rt

    def test_add_resident_does_not_drop_the_inflight_reply(self):
        d, res, rt = self._turn_during(lambda d: d.add_resident({"name": "阿七", "actor": "anan7"}))
        self.assertTrue(res["ok"], res)
        self.assertNotIn("ignored", res)
        self.assertEqual(len(res["replies"]), 1)
        rows = d.list_chat("private:sora", limit=10)
        self.assertIn("SLOWREPLY", " ".join(r["text"] for r in rows))
        # the observation went to the live (rebuilt) memory store, not the closed one
        with contextlib.closing(sqlite3.connect(rt / "memory.db")) as c:
            lines = [r[0] for r in c.execute("SELECT line FROM seen WHERE actor='sora'")]
        self.assertTrue(any("SLOWREPLY" in x for x in lines), lines)

    def test_update_resident_does_not_drop_the_inflight_reply(self):
        d, res, rt = self._turn_during(lambda d: d.update_resident({"actor": "sora", "name": "索拉"}))
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["replies"]), 1)

    def test_a_real_reset_still_drops_the_inflight_reply(self):
        d, res, rt = self._turn_during(lambda d: d.reset_house())
        self.assertFalse(res["ok"])
        self.assertEqual(res["ignored"], "world_reset_during_turn")
        self.assertEqual(d.list_chat("private:sora", limit=10), [])



def _bad_env_for_all_plugins(d_tmp_defs):
    env = {}
    for defn in d_tmp_defs.values():
        for fld in defn.manifest.config_fields:
            if fld.env_var and fld.field_type in ("int", "float", "bool"):
                env[fld.env_var] = "@@bad@@"
    return env


class InvalidPluginEnvNeverRaisesAtRuntime(unittest.TestCase):
    """D5: a malformed env value must degrade to the default at runtime, with one warning, never an exception."""

    def setUp(self):
        from ai_house.plugin_api import PluginRegistry

        self.bad_env = _bad_env_for_all_plugins(PluginRegistry().discover().definitions)
        self.assertIn("AI_HOUSE_GROUP_REPLY_PROB", self.bad_env)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs = []
        self.d = HouseDaemon(
            Path(tmp.name) / "rt", pack_dir=PACK, env=dict(self.bad_env), start_ts=1.8e9, virtual_clock=True,
            allow_onboarding=False, llm_fn=lambda s, u: ("你好呀", 1),
        )
        self.addCleanup(self.d.close)
        self.d.log = self.logs.append

    def test_group_converse_with_bad_env(self):
        res = self.d.converse("group", "大家好", owner_speaker("房主"))
        self.assertTrue(res["ok"], res)

    def test_group_behavior_view_and_mute_with_bad_env(self):
        self.assertTrue(self.d.mutate_group_behavior({"op": "mute"})["ok"])
        self.assertTrue(self.d.get_group_behavior_view()["ok"])

    def test_enabling_a_plugin_is_not_blocked_by_a_bad_env_value(self):
        defn = self.d.registry.definitions["group_behavior"]
        entry = self.d.settings_store.update_plugin(defn.manifest, enabled=True, values={}, env=self.d.env)
        self.assertTrue(entry["enabled"])

    def test_saving_an_invalid_value_is_still_rejected_strictly(self):
        defn = self.d.registry.definitions["group_behavior"]
        with self.assertRaises(ValueError):
            self.d.settings_store.update_plugin(defn.manifest, enabled=True, values={"reply_prob": "abc"}, env={})

    def test_warning_is_logged_once_not_per_call(self):
        for _ in range(3):
            self.d.converse("group", "大家好", owner_speaker("房主"))
            self.d.get_group_behavior_view()
        warns = [x for x in self.logs if "AI_HOUSE_GROUP_REPLY_PROB" in x]
        self.assertEqual(len(warns), 1, self.logs)

    def test_album_diary_art_paths_with_bad_env(self):
        from ai_house.builtin_plugins import album_diary_art as ada

        stored = {"image_gen": {"enabled": True, "values": {}}, "vision_understand": {"enabled": True, "values": {}}}
        self.d.settings_store.get_plugins_map = lambda: stored  # only activates the plugins; config resolution stays real
        saved = {"actor": "sora", "day": "2026-11-22", "text": "今天很开心", "name": "索拉"}
        ada.maybe_illustrate_diary(self.d, saved, {}, 1.8e9, cfg={"auto_illustrate_diary": True})
        ada.archive_chat_images(["http://127.0.0.1:9/x.png"], scope="group", channel="group", actor="sora", daemon=self.d, now=1.8e9)
        warns = [x for x in self.logs if "config warning" in x]
        self.assertTrue(warns, "expected the bad vision/image env to be reported once")

    def test_plugin_route_with_bad_env(self):
        defn = self.d.registry.definitions["album_diary_art"]
        self.d.settings_store.update_plugin(defn.manifest, enabled=True, values={}, env={})
        hit = self.d.find_plugin_route("GET", "/api/plugins/album_diary_art/items")
        self.assertIsNotNone(hit)
        res = self.d.execute_plugin_route(hit[0], hit[1], method="GET", path="/api/plugins/album_diary_art/items", query={}, body=None)
        self.assertTrue(res["ok"], res)

    def test_enabled_tool_runs_with_bad_env(self):
        defn = self.d.registry.definitions["album_diary_art"]
        self.d.settings_store.update_plugin(defn.manifest, enabled=True, values={}, env={})
        self.d._active_tools = self.d.registry.active_tools(self.d.env, stored_plugins=self.d.settings_store.get_plugins_map())
        res = self.d.converse("private:sora", "看看相册", owner_speaker("房主"))
        self.assertTrue(res["ok"], res)



class SettingsPageShowsConfigWarnings(unittest.TestCase):
    def test_settings_js_renders_config_warnings_as_text_nodes(self):
        js = (ROOT / "src" / "ai_house" / "web" / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("config_warnings", js)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)



class DaemonLockIsExclusive(unittest.TestCase):
    def _rt(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / "rt"

    def _open(self, rt):
        return HouseDaemon(rt, pack_dir=PACK, env={}, start_ts=1.8e9, virtual_clock=True, allow_onboarding=False)

    def _sleeper(self):
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_live_foreign_lock_refuses_start_with_a_clear_message(self):
        rt = self._rt()
        rt.mkdir(parents=True)
        proc = self._sleeper()
        (rt / ".daemon.lock").write_text(json.dumps({"pid": proc.pid, "started_at": time.time()}), encoding="utf-8")
        with self.assertRaises(Exception) as cm:
            self._open(rt)
        msg = str(cm.exception)
        self.assertIn(str(proc.pid), msg)
        self.assertIn("已有", msg)
        # the other daemon's lock is left alone and nothing was started on its directory
        self.assertEqual(json.loads((rt / ".daemon.lock").read_text(encoding="utf-8"))["pid"], proc.pid)
        self.assertFalse((rt / "world.db").exists())

    def test_stale_lock_is_taken_over(self):
        import subprocess

        rt = self._rt()
        rt.mkdir(parents=True)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (rt / ".daemon.lock").write_text(json.dumps({"pid": dead.pid, "started_at": 1}), encoding="utf-8")
        d = self._open(rt)
        self.addCleanup(d.close)
        self.assertEqual(json.loads((rt / ".daemon.lock").read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_garbage_lock_is_taken_over(self):
        rt = self._rt()
        rt.mkdir(parents=True)
        (rt / ".daemon.lock").write_text("not json", encoding="utf-8")
        d = self._open(rt)
        self.addCleanup(d.close)
        self.assertEqual(json.loads((rt / ".daemon.lock").read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_close_removes_lock_and_same_process_can_reopen(self):
        rt = self._rt()
        d1 = self._open(rt)
        self.assertTrue((rt / ".daemon.lock").is_file())
        d1.close()
        self.assertFalse((rt / ".daemon.lock").exists())
        d2 = self._open(rt)
        self.addCleanup(d2.close)
        self.assertTrue((rt / ".daemon.lock").is_file())

    def test_refused_start_leaves_holder_intact_and_stale_takeover_works_afterwards(self):
        rt = self._rt()
        proc = self._sleeper()
        rt.mkdir(parents=True)
        (rt / ".daemon.lock").write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        with self.assertRaises(Exception):
            self._open(rt)
        proc.kill()
        proc.wait()
        d = self._open(rt)  # holder is gone now: the stale lock can be taken over
        self.addCleanup(d.close)



TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TINY_DATA_URI = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")


def _http_get(url, token=""):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _album_daemon(tc, *, env=None, llm_fn=None):
    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    full_env = {"AI_HOUSE_OWNER_TOKEN": "owner-tok-t5", "AI_HOUSE_ENABLED_PLUGINS": "album_diary_art"}
    full_env.update(env or {})
    d = HouseDaemon(
        Path(tmp.name) / "rt", pack_dir=PACK, env=full_env, start_ts=dt.datetime(2026, 11, 22, 15, 0, tzinfo=JST).timestamp(),
        virtual_clock=True, allow_onboarding=False, llm_fn=llm_fn,
    )
    tc.addCleanup(d.close)
    return d


class AlbumDefaultsAndArchiveTiming(unittest.TestCase):
    def test_api_album_does_not_inline_images_unless_asked(self):
        d = _album_daemon(self)
        d.album.add_item(scope="world", actor="sora", channel="diary", kind="diary_art", raw_bytes=TINY_PNG, now=1.0)
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        st, body = _http_get(f"{base}/api/album?scope=world", token="owner-tok-t5")
        self.assertEqual(st, 200)
        item = json.loads(body)["items"][0]
        self.assertNotIn("data_uri", item)
        self.assertTrue(item["media_url"].startswith("/api/album/media?path="))
        st, body = _http_get(f"{base}/api/album?scope=world&inline=1", token="owner-tok-t5")
        self.assertTrue(json.loads(body)["items"][0]["data_uri"].startswith("data:image/png;base64,"))

    def test_chat_image_is_archived_after_the_reply_not_before(self):
        seen_during_reply = []
        holder = {}

        def llm(_sys, usr):
            if "刚刚说：" in usr:
                seen_during_reply.append(len(holder["d"].album.list_items(scope="private", limit=10, include_data_uri=False)))
            return "收到啦", 1

        d = holder["d"] = _album_daemon(self, llm_fn=llm)
        res = d.converse("private:sora", "看这张图", owner_speaker("房主"), attachments={"images": [TINY_DATA_URI]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen_during_reply, [0], "the archive must not run before the model is asked")
        self.assertTrue(d.drain_post_reply_jobs(5.0))
        items = d.album.list_items(scope="private", limit=10, include_data_uri=False)
        self.assertEqual([it["kind"] for it in items], ["chat_image"])
        self.assertEqual(items[0]["actor"], "sora")

    def test_archive_job_is_dropped_when_the_house_is_reset_meanwhile(self):
        d, rt, entered, release = _slow_daemon(self)
        d.settings_store.update_plugin(
            d.registry.definitions["album_diary_art"].manifest, enabled=True, values={}, env={}
        )
        holder = []
        th = threading.Thread(
            target=lambda: holder.append(
                d.converse("private:sora", "看图", owner_speaker("房主"), attachments={"images": [TINY_DATA_URI]})
            ),
            daemon=True,
        )
        th.start()
        self.assertTrue(entered.wait(3.0))
        d.reset_house()
        release.set()
        th.join(5.0)
        self.assertTrue(d.drain_post_reply_jobs(5.0))
        self.assertEqual(d.album.list_items(scope="private", limit=10, include_data_uri=False), [])
        self.assertFalse((rt / "media" / "album" / "private").exists() and any((rt / "media" / "album" / "private").iterdir()))


class AlbumStoreCleansUpOrphans(unittest.TestCase):
    def test_failed_insert_leaves_no_orphan_file(self):
        from ai_house.album import AlbumStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = AlbumStore(Path(tmp.name))
        with contextlib.closing(sqlite3.connect(store.db_path)) as c:
            with c:
                c.execute(
                    "CREATE TRIGGER no_insert BEFORE INSERT ON album_items BEGIN SELECT RAISE(ABORT, 'disk says no'); END"
                )
        with self.assertRaises(sqlite3.Error):
            store.add_item(scope="group", actor="sora", channel="group", raw_bytes=TINY_PNG, now=1.0)
        media = Path(tmp.name) / "media" / "album" / "group"
        self.assertEqual(list(media.iterdir()) if media.exists() else [], [])


class SaveToAlbumReferencesConversationImages(unittest.TestCase):
    def _ctx(self, d, *, scope, channel, actor="sora", attachments=None):
        from ai_house.plugin_api import ToolContext

        return ToolContext(
            actor=actor, scope=scope, channel=channel, speaker=owner_speaker("房主"), now=5.0, daemon=d,
            attachments=attachments,
        )

    def test_attachment_index_saves_the_current_message_image_in_the_current_scope(self):
        from ai_house.builtin_plugins.album_diary_art import _tool_save_to_album

        d = _album_daemon(self)
        ctx = self._ctx(d, scope="private", channel="private:sora", attachments={"images": [TINY_DATA_URI]})
        res = _tool_save_to_album(image_index=1, title="便签", ctx=ctx)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["scope"], "private")
        self.assertEqual(len(d.album.list_items(scope="private", limit=10, include_data_uri=False)), 1)
        self.assertEqual(d.album.list_items(scope="group", limit=10, include_data_uri=False), [])
        bad = _tool_save_to_album(image_index=2, ctx=ctx)
        self.assertFalse(bad["ok"])
        self.assertFalse(_tool_save_to_album(image_index=1, ctx=self._ctx(d, scope="private", channel="private:sora"))["ok"])

    def test_media_rel_is_scope_checked(self):
        from ai_house.builtin_plugins.album_diary_art import _tool_save_to_album

        d = _album_daemon(self)
        priv = d.album.add_item(scope="private", actor="sora", channel="private:sora", kind="chat_image", raw_bytes=TINY_PNG + b"1", now=1.0)
        other_priv = d.album.add_item(scope="private", actor="rin", channel="private:rin", kind="chat_image", raw_bytes=TINY_PNG + b"2", now=1.0)
        world = d.album.add_item(scope="world", actor="sora", channel="diary", kind="diary_art", raw_bytes=TINY_PNG + b"3", now=1.0)
        # the same private chat may re-save its own archived image
        ok = _tool_save_to_album(media_rel=priv["media_rel"], title="再存一次", ctx=self._ctx(d, scope="private", channel="private:sora"))
        self.assertTrue(ok["ok"], ok)
        # a group conversation must not reach into a private album, nor may another resident's private chat
        for ctx in (self._ctx(d, scope="group", channel="group"), self._ctx(d, scope="private", channel="private:rin", actor="rin")):
            res = _tool_save_to_album(media_rel=priv["media_rel"], ctx=ctx)
            self.assertFalse(res["ok"], res)
            self.assertEqual(res["status"], "forbidden")
        self.assertFalse(_tool_save_to_album(media_rel=other_priv["media_rel"], ctx=self._ctx(d, scope="private", channel="private:sora"))["ok"])
        # public (world) art is visible everywhere
        self.assertTrue(_tool_save_to_album(media_rel=world["media_rel"], ctx=self._ctx(d, scope="group", channel="group"))["ok"])
        # path tricks never escape
        self.assertFalse(_tool_save_to_album(media_rel="media/album/private/../../../world.db", ctx=self._ctx(d, scope="private", channel="private:sora"))["ok"])
        self.assertFalse(_tool_save_to_album(source="not a data uri", ctx=self._ctx(d, scope="private", channel="private:sora"))["ok"])

    def test_model_tool_call_reaches_the_current_message_images_end_to_end(self):
        import http.server

        observed = []

        class Chat(http.server.BaseHTTPRequestHandler):
            turn = 0

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                for msg in body.get("messages") or []:
                    if msg.get("role") == "tool":
                        observed.append(json.loads(msg["content"]))
                type(self).turn += 1
                if type(self).turn == 1:
                    msg = {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "save_to_album", "arguments": json.dumps({"image_index": 1, "title": "便签"})}}]}
                else:
                    msg = {"role": "assistant", "content": "已经收进相册啦"}
                raw = json.dumps({"choices": [{"message": msg}], "usage": {"total_tokens": 5}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Chat)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        d = _album_daemon(self)
        d.update_llm_settings({"base_url": f"http://127.0.0.1:{srv.server_port}", "api_key": "sk-t5-local-test", "model": "m"})
        res = d.converse("private:sora", "帮我收藏这张", owner_speaker("房主"), attachments={"images": [TINY_DATA_URI]})
        self.assertEqual(res["replies"][0]["mode"], "live_api", res)
        self.assertTrue(observed and observed[0].get("ok"), observed)
        self.assertTrue(d.drain_post_reply_jobs(5.0))
        kinds = sorted(it["kind"] for it in d.album.list_items(scope="private", limit=10, include_data_uri=False))
        self.assertIn("photo", kinds)


class ReminderPerPersonChannelCap(unittest.TestCase):
    def test_one_person_cannot_fill_a_shared_channel(self):
        from ai_house.builtin_plugins.reminders import set_reminder
        from ai_house.chat_identity import member_speaker
        from ai_house.plugin_api import ToolContext

        d = _album_daemon(self)
        now = d.clock.now()
        big = {"max_active_per_user": 100}

        def ctx(name, uid):
            return ToolContext(actor="sora", scope="group", channel="group", speaker=member_speaker(name, uid), now=now, daemon=d, plugin_config=big)

        hog = ctx("阿晴", "8002")
        added = 0
        with self.assertRaises(ValueError):
            for i in range(40):
                set_reminder(f"hog-{i}", in_minutes=30 + i, ctx=hog)
                added += 1
        self.assertLess(added, 30, "one person filled the whole channel")
        # someone else can still set reminders in the same channel
        set_reminder("别人的", in_minutes=45, ctx=ctx("阿明", "8003"))


class BadQueryNumbersAre400(unittest.TestCase):
    def test_non_numeric_limit_is_a_400_not_a_dropped_connection(self):
        d = _album_daemon(self)
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        for path in ("/api/memories?limit=abc", "/api/diaries?limit=abc", "/api/album?limit=abc", "/api/intents?limit=abc", "/api/outbox?limit=abc", "/api/usage?days=abc"):
            st, body = _http_get(base + path, token="owner-tok-t5")
            self.assertEqual(st, 400, path)
            self.assertFalse(json.loads(body)["ok"], path)
            st, _ = _http_get(base + path.replace("abc", "3"), token="owner-tok-t5")
            self.assertEqual(st, 200, path)
        # authorization still comes first
        st, _ = _http_get(base + "/api/memories?limit=abc")
        self.assertEqual(st, 401)


if __name__ == "__main__":
    unittest.main()
