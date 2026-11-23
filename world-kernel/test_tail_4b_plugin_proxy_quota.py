"""Tail 4b regression tests: third-party plugin host proxy (channels/providers, secret leakage) and paid-plugin quotas.

Every test here failed against main @ 75d3129 (see commit message); none of them mocks importlib.metadata.
"""
from __future__ import annotations

import http.server
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog"), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.builtin_plugins.web_search import _tool_web_search, run_web_search  # noqa: E402
from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon  # noqa: E402
from ai_house.llm import ModelRouter  # noqa: E402
from ai_house.plugin_api import PluginHostProxy, ToolContext  # noqa: E402

PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
# 假值在运行时拼接，避免密钥扫描把测试夹具当成真实凭据
FAKE_API_CRED = "-".join(["sk", "tail4b", "very", "secret", "0001"])
FAKE_OWNER_CRED = "-".join(["owner", "tok", "123456"])


class _Clock:
    def __init__(self, t: float = 1700000000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


class _SearchServer:
    """Search upstream that sleeps a little so concurrent callers genuinely overlap."""

    def __init__(self, delay_s: float = 0.25) -> None:
        self.hits = 0
        self._lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                time.sleep(delay_s)
                with outer._lock:
                    outer.hits += 1
                raw = json.dumps({"results": [{"title": "t", "url": "http://a.example/", "content": "c"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/search"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class _Host:
    def __init__(self, router: ModelRouter) -> None:
        self.model_router = router


def _make_router(testcase: unittest.TestCase, env: Dict[str, str] | None = None) -> tuple[ModelRouter, _Clock]:
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    clk = _Clock()
    router = ModelRouter(env or {}, runtime_dir=tmp.name, clock=clk)
    testcase.addCleanup(router.close)
    return router, clk


def _write_dist(site: Path, dist: str, group: str, entry: str, module_name: str, source: str) -> None:
    di = site / f"{dist}-0.1.0.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n", encoding="utf-8")
    (di / "entry_points.txt").write_text(f"[{group}]\n{entry}\n", encoding="utf-8")
    (site / f"{module_name}.py").write_text(source, encoding="utf-8")


THIRD_PARTY_CHANNEL = '''
from ai_house.channel_spool import ChannelSpool
from ai_house.plugin_api import ChannelPlugin
from worldkernel.outbox import BlockedDelivery

INSTANCES = []


class TgLikeChannel(ChannelPlugin):
    """A channel written the way the built-in OneBot one is, but only against the documented host surface."""

    channel_id = "tp_chan"

    def __init__(self):
        self.host = None
        self.spool = None
        self.sent = []
        self.blocked = []
        self.errors = []
        INSTANCES.append(self)

    def start(self, host):
        self.host = host
        self.spool = ChannelSpool(host.plugin_dir)

    def stop(self):
        if self.spool is not None:
            self.spool.close()

    def inbound(self, uid, nick, text, *, private=True, actor="sora", message_id=""):
        if message_id and self.spool.check_and_mark_dedup("tp_chan", message_id, scope_key="t"):
            return "duplicate"
        speaker = self.host.resolve_speaker(uid, fallback_name=nick)
        channel = f"private:{actor}" if private else "group"
        res = self.host.converse(channel, text, speaker, **({} if private else {"actor": actor}))
        n = 0
        for rep in res.get("replies") or ():
            if rep.get("mode") in ("blocked", "demo", "sleeping") or not rep.get("text"):
                continue
            try:
                key = self.host.authorize_send(
                    self, kind="private" if private else "group", target=uid, actor=actor, text=rep["text"],
                )
            except BlockedDelivery as exc:
                self.blocked.append(exc.reason)
                continue
            self.sent.append({"to": uid, "text": rep["text"]})  # (the real wire send would happen here)
            self.host.record_sent("", key)  # tail 7a: charge the caps only after the send succeeded
            n += 1
        return n


def make():
    return TgLikeChannel()
'''

THIRD_PARTY_PROVIDER = '''
import json
from ai_house.plugin_api import StudioProvider

INSTANCES = []


class CursorProvider(StudioProvider):
    provider_id = "tp_prov"

    def __init__(self):
        self.seen = []
        INSTANCES.append(self)

    def step(self, host):
        state = host.get_state()
        cur = host.plugin_dir / "cursor.json"
        cur.write_text(json.dumps({"residents": sorted(state["residents"])}), encoding="utf-8")
        self.seen.append(str(host.plugin_dir))


def make():
    return CursorProvider()
'''


class _SiteMixin:
    def _install_site(self, tmp: Path) -> Path:
        site = tmp / "site"
        site.mkdir()
        old_path = list(sys.path)
        sys.path.insert(0, str(site))
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))  # type: ignore[attr-defined]
        return site

    def _daemon(self, tmp: Path, env: Dict[str, str], **kw: Any) -> HouseDaemon:
        d = HouseDaemon(
            tmp / "rt",
            pack_dir=PACK,
            env=env,
            virtual_clock=True,
            allow_onboarding=False,
            llm_fn=lambda _s, _u: ("好的，收到啦。", 8),
            **kw,
        )
        self.addCleanup(d.close)  # type: ignore[attr-defined]
        return d


class ThirdPartyChannelEndToEnd(_SiteMixin, unittest.TestCase):
    """N4: a real third-party channel discovered via .dist-info must be able to converse and send through the gate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        site = self._install_site(self.root)
        _write_dist(site, "tp-chan-pkg", "ai_house.channels", "tp_chan = tp_chan_mod:make", "tp_chan_mod", THIRD_PARTY_CHANNEL)
        self.addCleanup(lambda: sys.modules.pop("tp_chan_mod", None))
        self.d = self._daemon(
            self.root,
            {"AI_HOUSE_API_KEY": FAKE_API_CRED, "AI_HOUSE_PLUGIN_TP_CHAN_ENABLED": "1"},
        )
        import tp_chan_mod  # type: ignore

        self.assertEqual(len(tp_chan_mod.INSTANCES), 1, self.d._plugin_runtime_errors)
        self.chan = tp_chan_mod.INSTANCES[0]

    def test_channel_gets_proxy_and_can_converse_and_send_via_gate(self) -> None:
        self.assertIsInstance(self.chan.host, PluginHostProxy)
        # the owner binds the third-party account once (web UI `bind` op); only then is it the owner
        self.d.mutate_identity({"op": "bind", "platform": "tp_chan", "external_id": "u-owner", "identity_id": "user:owner"})
        sent_before = self.d.send_gate.stats() if hasattr(self.d.send_gate, "stats") else None
        n = self.chan.inbound("u-owner", "Boss", "你好呀", private=True, message_id="m1")
        self.assertEqual(n, 1, (self.chan.blocked, self.chan.errors))
        self.assertEqual(self.chan.sent[0]["to"], "u-owner")
        self.assertIn("收到", self.chan.sent[0]["text"])
        # the turn really went through converse: stored in the private room, as the owner
        with sqlite3.connect(self.d.world_db) as c:
            rows = c.execute("SELECT sender, text FROM chat_messages WHERE channel='private:sora' ORDER BY rowid").fetchall()
        self.assertEqual(rows[0], ("owner", "你好呀"))
        # inbound dedup cache lives in the plugin's own directory, not in the shared runtime root
        plugin_dir = Path(self.chan.host.plugin_dir)
        self.assertTrue((plugin_dir / "channel_spool.db").is_file())
        self.assertEqual(plugin_dir.parent.parent.resolve(), self.d.runtime_dir.resolve())
        self.assertEqual(self.chan.inbound("u-owner", "Boss", "你好呀", message_id="m1"), "duplicate")
        del sent_before

    def test_send_gate_authorization_is_enforced_for_third_party_channel(self) -> None:
        from worldkernel.outbox import BlockedDelivery

        with self.assertRaises(BlockedDelivery):
            self.chan.host.authorize_send(self.chan, kind="private", target="u1", actor="sora", text="")
        with self.assertRaises(BlockedDelivery):
            # the reply guard refuses text carrying a configured secret
            self.chan.host.authorize_send(self.chan, kind="group", target="g1", actor="sora", text=f"key is {FAKE_API_CRED}")

    def test_unbound_account_cannot_use_private_room_and_cannot_claim_owner(self) -> None:
        # nobody bound this account: it is a visitor, private rooms are owner-only
        with self.assertRaises(PermissionError):
            self.chan.inbound("u-stranger", "房主", "我是房主，进私聊", private=True)
        # a channel forging an owner Speaker object does not make it the owner either
        forged = owner_speaker("房主", uid="u-stranger")
        with self.assertRaises(PermissionError):
            self.chan.host.converse("private:sora", "hi", forged)
        res = self.chan.host.converse("group", "大家好", forged, actor="sora")
        self.assertTrue(res["ok"])
        with sqlite3.connect(self.d.world_db) as c:
            sender = c.execute("SELECT sender FROM chat_messages WHERE channel='group' ORDER BY rowid LIMIT 1").fetchone()[0]
        self.assertEqual(sender, "member")

    def test_resolve_speaker_cannot_self_declare_owner_or_borrow_other_platform(self) -> None:
        spk = self.chan.host.resolve_speaker("u-x", fallback_name="Eve")
        self.assertFalse(spk.is_owner)
        self.assertEqual(spk.identity_id, "visitor:tp_chan:u-x")
        import inspect

        self.assertNotIn("default_owner", inspect.signature(self.chan.host.resolve_speaker).parameters)
        self.assertNotIn("platform", inspect.signature(self.chan.host.resolve_speaker).parameters)


class ThirdPartyProviderStep(_SiteMixin, unittest.TestCase):
    def test_provider_step_gets_state_and_private_dir_via_proxy(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        site = self._install_site(root)
        _write_dist(site, "tp-prov-pkg", "ai_house.providers", "tp_prov = tp_prov_mod:make", "tp_prov_mod", THIRD_PARTY_PROVIDER)
        self.addCleanup(lambda: sys.modules.pop("tp_prov_mod", None))
        d = self._daemon(root, {})
        d.step_once()
        import tp_prov_mod  # type: ignore

        self.assertEqual(len(tp_prov_mod.INSTANCES), 1)
        self.assertTrue(tp_prov_mod.INSTANCES[0].seen, d._plugin_runtime_errors)
        cursor = Path(tp_prov_mod.INSTANCES[0].seen[0]) / "cursor.json"
        self.assertTrue(json.loads(cursor.read_text(encoding="utf-8"))["residents"])
        self.assertEqual(d._plugin_runtime_errors, [])


class HelloTeaExampleUnderProxy(_SiteMixin, unittest.TestCase):
    def test_example_plugin_task_persists_into_its_private_plugin_dir(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        site = self._install_site(root)
        di = site / "hello_tea_plugin-0.1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text("Metadata-Version: 2.1\nName: ai-house-hello-tea-plugin\nVersion: 0.1.0\n", encoding="utf-8")
        (di / "entry_points.txt").write_text("[ai_house.plugins]\nhello_tea = hello_tea_plugin:PLUGIN_DEF\n", encoding="utf-8")
        sys.path.insert(0, str(ROOT / "examples" / "hello-tea-plugin"))
        self.addCleanup(lambda: sys.modules.pop("hello_tea_plugin", None))
        d = self._daemon(root, {"AI_HOUSE_PLUGIN_HELLO_TEA_ENABLED": "1"})
        d.fast_forward(15)
        brew = d.runtime_dir / "plugins" / "hello_tea" / "brew.json"
        self.assertTrue(brew.is_file(), d._plugin_runtime_errors)
        self.assertGreaterEqual(json.loads(brew.read_text(encoding="utf-8"))["brew_count"], 1)
        self.assertEqual(d._plugin_runtime_errors, [])


class ProxyDoesNotLeak(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "AI_HOUSE_API_KEY": FAKE_API_CRED,
            "AI_HOUSE_OWNER_TOKEN": FAKE_OWNER_CRED,
            "SOME_PASSWORD": "pw-hunter2222",
            "MY_SECRET_THING": "s3cr3t-aaaaaa",
            "HTTP_COOKIE_JAR": "cookie-bbbbbb",
            "BASIC_AUTH_LINE": "auth-cccccc",
            "lower_token_x": "tok-dddddd",
            "MIRROR_URL": "https://user:pass-eeeeee@mirror.example/x",
            "TZ": "Asia/Tokyo",
            "AI_HOUSE_THEME": "tea",
        }
        self.d = HouseDaemon(
            Path(self.tmp.name) / "rt", pack_dir=PACK, env=self.env, virtual_clock=True,
            allow_onboarding=False, llm_fn=lambda _s, _u: ("好", 1),
        )
        self.addCleanup(self.d.close)
        self.proxy = PluginHostProxy(self.d)  # id-less: the leak checks must not depend on the new signature

    def test_env_is_a_secret_free_view(self) -> None:
        view = self.proxy.env
        self.assertEqual(view.get("TZ"), "Asia/Tokyo")
        self.assertEqual(view.get("AI_HOUSE_THEME"), "tea")
        for name in ("AI_HOUSE_API_KEY", "AI_HOUSE_OWNER_TOKEN", "SOME_PASSWORD", "MY_SECRET_THING",
                     "HTTP_COOKIE_JAR", "BASIC_AUTH_LINE", "lower_token_x", "MIRROR_URL"):
            self.assertNotIn(name, view)
        flat = json.dumps(dict(view))
        for secret in ("pass-eeeeee", FAKE_API_CRED, FAKE_OWNER_CRED, "pw-hunter2222"):
            self.assertNotIn(secret, flat)
        with self.assertRaises(TypeError):
            view["X"] = "1"  # type: ignore[index]

    def test_real_daemon_is_not_reachable_through_public_attributes(self) -> None:
        self.assertFalse(hasattr(self.proxy, "_daemon"))
        self.assertFalse(hasattr(self.proxy, "daemon"))
        self.assertFalse(hasattr(self.proxy, "__dict__"))  # slots: no instance dict to fish in
        for name in dir(self.proxy):
            if name.startswith("__"):
                continue
            try:
                val = getattr(self.proxy, name)
            except Exception:  # noqa: BLE001
                continue
            self.assertIsNot(val, self.d, name)
            self.assertNotIsInstance(val, HouseDaemon, name)

    def test_guard_view_does_not_expose_secret_list(self) -> None:
        g = self.proxy.guard
        self.assertFalse(hasattr(g, "_secrets"))
        self.assertIsNotNone(g.check("group", f"leak {FAKE_API_CRED}"))
        self.assertIsNone(g.check("group", "今天天气不错"))

    def test_album_is_read_only_view(self) -> None:
        album = self.proxy.album
        self.assertIsNotNone(album)
        for name in ("add_item", "reset", "init_schema", "db_path", "_conn", "delete_item"):
            self.assertFalse(hasattr(album, name), name)
        self.assertEqual(album.list_items(scope="world"), [])

    def test_config_only_returns_declared_fields_of_this_plugin(self) -> None:
        from ai_house.plugin_api import ConfigField, PluginDefinition, PluginManifest

        self.d.registry.register_definition(
            PluginDefinition(
                manifest=PluginManifest(
                    plugin_id="tp_leak", name="x", kind="bundle", default_enabled=True,
                    config_fields=(
                        ConfigField(key="greeting", label="g", default="hi"),
                        ConfigField(key="token", label="t", secret=True, env_var="TP_LEAK_TOKEN"),
                    ),
                ),
                source="entrypoint",
            )
        )
        self.d.env["TP_LEAK_TOKEN"] = "own-secret-value"
        cfg = PluginHostProxy(self.d, "tp_leak").config()
        self.assertEqual(set(cfg), {"greeting", "token"})
        self.assertEqual(cfg["token"], "own-secret-value")  # a plugin may read what it declared
        self.assertNotIn("AI_HOUSE_API_KEY", json.dumps(dict(cfg)))
        with self.assertRaises(TypeError):
            cfg["greeting"] = "x"  # type: ignore[index]


class PluginQuota(unittest.TestCase):
    def test_concurrent_requests_cannot_exceed_daily_limit(self) -> None:
        router, _ = _make_router(self)
        srv = _SearchServer(0.25)
        self.addCleanup(srv.close)
        cfg = {"endpoint_url": srv.url, "allow_loopback": True, "daily_limit": 3}
        for label, host, owner in (("owner-direct", _Host(router), True), ("visitor-proxy", None, False)):
            if host is None:
                host = PluginHostProxy(_Host(router), "web_search")
            before = srv.hits
            out: List[str] = []
            lk = threading.Lock()
            barrier = threading.Barrier(15)

            def go(i: int) -> None:
                barrier.wait()
                st = run_web_search("q", cfg=cfg, daemon=host, speaker_id=f"v{i}", is_owner=owner)["status"]
                with lk:
                    out.append(st)

            ts = [threading.Thread(target=go, args=(i,)) for i in range(15)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(out), 15, label)
            ok = out.count("ok")
            self.assertLessEqual(ok, 3, f"{label}: {out}")
            self.assertLessEqual(srv.hits - before, 3, label)
            self.assertEqual(out.count("quota_exceeded"), 15 - ok, label)
            if owner:
                self.assertEqual(ok, 3, label)  # nothing is lost either: exactly the limit is served
            # fresh day for the next scenario
            router._clock.t += 86400  # type: ignore[union-attr]

    def test_failed_upstream_call_refunds_daily_slot_but_counts_toward_hourly(self) -> None:
        router, _ = _make_router(self)
        srv = _SearchServer(0.0)
        self.addCleanup(srv.close)
        bad = {"endpoint_url": "http://127.0.0.1:1/x", "allow_loopback": True, "daily_limit": 2, "timeout_s": 1}
        good = dict(bad, endpoint_url=srv.url)
        # owner: failures never eat the daily budget
        for _ in range(5):
            self.assertEqual(run_web_search("q", cfg=bad, daemon=_Host(router), speaker_id="o", is_owner=True)["status"], "upstream_error")
        self.assertEqual(run_web_search("q", cfg=good, daemon=_Host(router), speaker_id="o", is_owner=True)["status"], "ok")
        self.assertEqual(run_web_search("q", cfg=good, daemon=_Host(router), speaker_id="o", is_owner=True)["status"], "ok")
        self.assertEqual(run_web_search("q", cfg=good, daemon=_Host(router), speaker_id="o", is_owner=True)["status"], "quota_exceeded")

    def test_visitor_hourly_limit_counts_failed_attempts(self) -> None:
        router, _ = _make_router(self)
        bad = {"endpoint_url": "http://127.0.0.1:1/x", "allow_loopback": True, "daily_limit": 5000, "timeout_s": 1}
        statuses = [
            run_web_search("q", cfg=bad, daemon=_Host(router), speaker_id="visitor:qq:9", is_owner=False)["status"]
            for _ in range(30)
        ]
        self.assertEqual(statuses.count("upstream_error"), 20, statuses)
        self.assertEqual(statuses.count("quota_exceeded"), 10, statuses)

    def test_invalid_source_does_not_leave_a_reserved_slot_behind(self) -> None:
        from ai_house.builtin_plugins.vision_understand import describe_image_source
        from ai_house.builtin_plugins.voice_io import transcribe_audio_source

        router, _ = _make_router(self)
        host = _Host(router)
        vcfg = {"api_key": "k-vision-1234", "base_url": "http://127.0.0.1:1", "daily_limit": 1}
        acfg = {"api_key": "k-voice-1234", "base_url": "http://127.0.0.1:1", "daily_limit": 1}
        for _ in range(3):  # a bad source never reaches the upstream and must not hold the only daily slot
            self.assertEqual(describe_image_source("", cfg=vcfg, daemon=host)["status"], "invalid_source")
            self.assertEqual(transcribe_audio_source("", cfg=acfg, daemon=host)["status"], "invalid_source")

    def test_visitors_cannot_consume_owner_reserve(self) -> None:
        router, _ = _make_router(self)
        got = 0
        for i in range(30):
            err = router.check_plugin_quota("paid", speaker_id=f"visitor:qq:{i}", is_owner=False, daily_limit=10, owner_reserve_ratio=0.5)
            if err is None:
                got += 1
                router.record_plugin_call("paid", speaker_id=f"visitor:qq:{i}", is_owner=False, ok=True)
        self.assertEqual(got, 5)
        for _ in range(5):
            self.assertIsNone(router.check_plugin_quota("paid", speaker_id="user:owner", is_owner=True, daily_limit=10, owner_reserve_ratio=0.5))
            router.record_plugin_call("paid", speaker_id="user:owner", is_owner=True, ok=True)
        self.assertIsNotNone(router.check_plugin_quota("paid", speaker_id="user:owner", is_owner=True, daily_limit=10, owner_reserve_ratio=0.5))

    def test_default_owner_reserve_keeps_some_daily_budget_for_owner(self) -> None:
        router, _ = _make_router(self)
        for i in range(100):
            if router.check_plugin_quota("paid", speaker_id=f"v{i}", is_owner=False, daily_limit=10) is None:
                router.record_plugin_call("paid", speaker_id=f"v{i}", is_owner=False, ok=True)
        self.assertIsNone(router.check_plugin_quota("paid", speaker_id="user:owner", is_owner=True, daily_limit=10))

    def test_owner_reserve_ratio_is_configurable_via_env(self) -> None:
        router, _ = _make_router(self, {"AI_HOUSE_PLUGIN_OWNER_RESERVE": "0.8"})
        got = 0
        for i in range(30):
            if router.check_plugin_quota("paid", speaker_id=f"v{i}", is_owner=False, daily_limit=10) is None:
                got += 1
                router.record_plugin_call("paid", speaker_id=f"v{i}", is_owner=False, ok=True)
        self.assertEqual(got, 2)

    def _tool_ctx(self, speaker: Any, daemon: Any, cfg: Dict[str, Any], channel: str = "qq-group:1") -> ToolContext:
        return ToolContext(actor="sora", scope="group", channel=channel, speaker=speaker, now=0.0, plugin_config=cfg, daemon=daemon)

    def test_visitor_cannot_bypass_hourly_limit_by_changing_display_name(self) -> None:
        router, _ = _make_router(self)
        bad = {"endpoint_url": "http://127.0.0.1:1/x", "allow_loopback": True, "daily_limit": 5000, "timeout_s": 1}
        host = _Host(router)
        # no identity, no external id: every nickname lands in one shared anonymous bucket
        res = [
            _tool_web_search(query="q", ctx=self._tool_ctx(member_speaker(f"昵称{i}"), host, bad))["status"]
            for i in range(25)
        ]
        self.assertEqual(res.count("quota_exceeded"), 5, res)
        # same external account with different nicknames: one bucket as well (fresh router so counters are clean)
        router2, _ = _make_router(self)
        host2 = _Host(router2)
        res2 = [
            _tool_web_search(query="q", ctx=self._tool_ctx(member_speaker(f"别名{i}", uid="12345"), host2, bad))["status"]
            for i in range(25)
        ]
        self.assertEqual(res2.count("quota_exceeded"), 5, res2)
        # a different external account has its own hourly budget
        other = _tool_web_search(query="q", ctx=self._tool_ctx(member_speaker("别名", uid="67890"), host2, bad))["status"]
        self.assertEqual(other, "upstream_error")


if __name__ == "__main__":
    unittest.main()
