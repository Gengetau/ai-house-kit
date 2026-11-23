"""Tail 7a regression tests: a second review of the third-party plugin host proxy.

Covers: charging channel sends (`record_sent`), no private-room reads through the proxy, the env view as a whitelist,
quota calls bound to the proxy's own plugin id, and the quota day/limit edge cases. Every test here failed before
the 7a fix (see the commit message). Fake credentials are assembled at run time so secret scanners stay quiet.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog"), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import BOT, OWNER, Speaker, member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon  # noqa: E402
from ai_house.llm import JST, ModelRouter  # noqa: E402
from ai_house.plugin_api import (  # noqa: E402
    ChannelPlugin,
    ConfigField,
    PluginDefinition,
    PluginHostProxy,
    PluginManifest,
    PluginRegistry,
    public_env_view,
)
from worldkernel.outbox import NotSent  # noqa: E402

PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")


def _fake(*parts: str) -> str:
    return "-".join(parts)


class _Clock:
    def __init__(self, t: float = 1700000000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


class _Chan(ChannelPlugin):
    channel_id = "tp_chan"

    def start(self, host: Any) -> None:  # pragma: no cover - never started in these tests
        return None


def _register(d: HouseDaemon, pid: str, *, channel: bool, env_vars: Dict[str, str] | None = None) -> PluginHostProxy:
    """Register a third-party definition (entry-point sourced) on the daemon and hand back its proxy."""
    fields = tuple(
        ConfigField(key=k.lower()[:20], label=k, env_var=v, secret=True) for k, v in (env_vars or {}).items()
    )
    d.registry.register_definition(
        PluginDefinition(
            manifest=PluginManifest(plugin_id=pid, name=pid, kind="channel" if channel else "bundle", config_fields=fields),
            channel_factory=(lambda: _Chan()) if channel else None,
            source="entrypoint",
        )
    )
    return PluginHostProxy(d, pid)


class _DaemonCase(unittest.TestCase):
    def _daemon(self, env: Dict[str, str] | None = None) -> HouseDaemon:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            Path(tmp.name) / "rt", pack_dir=PACK, env=dict(env or {}), virtual_clock=True,
            allow_onboarding=False, llm_fn=lambda _s, _u: ("好的，收到。", 4),
        )
        self.addCleanup(d.close)
        return d


# --------------------------------------------------------------------------------------------------------------------
# 1. channel sends can be charged through the proxy
# --------------------------------------------------------------------------------------------------------------------
class RecordSentThroughProxy(_DaemonCase):
    def setUp(self) -> None:
        self.d = self._daemon({"AI_HOUSE_DIRECT_HOURLY_CAP": "3"})
        self.proxy = _register(self.d, "tp_chan", channel=True)
        self.chan = _Chan()

    def _authorize(self, did: str = "") -> str:
        return self.proxy.authorize_send(self.chan, kind="private", target="u1", actor="sora", text="你好", delivery_id=did)

    def test_without_record_the_hourly_cap_never_trips(self) -> None:
        for i in range(12):
            self._authorize(f"d-{i}")  # authorize only judges; never raises while nothing was charged

    def test_authorize_then_record_reaches_the_cap(self) -> None:
        for i in range(3):
            key = self._authorize(f"d-{i}")
            self.assertTrue(self.proxy.record_sent(f"d-{i}", key))
        with self.assertRaises(NotSent) as cm:
            self._authorize("d-3")
        self.assertEqual(cm.exception.reason, "hourly_cap")
        # a different target has its own hourly budget
        self.proxy.authorize_send(self.chan, kind="private", target="u2", actor="sora", text="嗨", delivery_id="x1")

    def test_record_without_delivery_id_still_counts_each_send(self) -> None:
        for _ in range(3):
            key = self._authorize()
            self.proxy.record_sent("", key)
        with self.assertRaises(NotSent):
            self._authorize()

    def test_same_delivery_id_is_charged_once(self) -> None:
        key = self._authorize("dup")
        self.assertTrue(self.proxy.record_sent("dup", key))
        self.assertFalse(self.proxy.record_sent("dup", key))

    def test_cannot_charge_another_channels_budget(self) -> None:
        with self.assertRaises(PermissionError):
            self.proxy.record_sent("evil-1", "direct:onebot_v11:12345")
        with self.assertRaises(PermissionError):
            self.proxy.record_sent("evil-2", "outbox:whatever")
        with self.assertRaises(PermissionError):
            PluginHostProxy(self.d, "").record_sent("e3", "direct::x")  # an id-less proxy has no channel to charge


# --------------------------------------------------------------------------------------------------------------------
# 2. no private-room reads through the proxy
# --------------------------------------------------------------------------------------------------------------------
class PrivateRoomAccess(_DaemonCase):
    SECRET_TEXT = "这是房主的私密消息暗号蓝鲸"

    def setUp(self) -> None:
        self.d = self._daemon()
        self.actor = self.d.vocab.ROSTER[0]
        self.room = f"private:{self.actor}"
        self.d.converse(self.room, self.SECRET_TEXT, owner_speaker("房主"))
        now = self.d.clock.now()
        self.d.memory.add(self.actor, "private", "房主偷偷喜欢蓝鲸这个暗号", kind="fact", now=now)
        self.d.album.add_item(scope="private", actor=self.actor, raw_bytes=b"\x89PNG-fake-private", title="私密相片", now=now)
        self.d.album.add_item(scope="world", actor=self.actor, raw_bytes=b"\x89PNG-fake-world", title="公开相片", now=now)
        self.plain = _register(self.d, "tp_plain", channel=False)
        self.chan = _register(self.d, "tp_chan", channel=True)

    def test_plain_plugin_cannot_list_private_chat(self) -> None:
        for room in (self.room, f" {self.room}", self.room.upper()):
            with self.assertRaises(PermissionError, msg=room):
                self.plain.list_chat(room)
        self.assertIsInstance(self.plain.list_chat("group"), list)  # public rooms stay readable

    def test_plain_plugin_cannot_read_private_memories(self) -> None:
        for scope in ("private", " Private "):
            with self.assertRaises(PermissionError, msg=scope):
                self.plain.list_memories(actor=self.actor, scope=scope)
        with self.assertRaises(PermissionError):
            self.plain.list_memories(actor=self.actor, scope="group", channel=self.room)
        self.assertIsInstance(self.plain.list_memories(scope="world"), list)

    def test_plain_plugin_cannot_read_private_album(self) -> None:
        with self.assertRaises(PermissionError):
            self.plain.list_album_view(scope="private", actor=self.actor)
        with self.assertRaises(PermissionError):
            self.plain.album.list_items(scope="private")
        priv = self.d.album.list_items(scope="private", include_data_uri=False)[0]
        self.assertIsNone(self.plain.album.get_item(priv["album_id"]))
        self.assertIsNone(self.plain.album.get_by_media_rel(priv["media_rel"]))
        pub = self.d.album.list_items(scope="world", include_data_uri=False)[0]
        self.assertEqual(self.plain.album.get_item(pub["album_id"])["title"], "公开相片")
        self.assertEqual(len(self.plain.album.list_items(scope="world")), 1)

    def test_channel_plugin_may_read_private_scopes_it_carries(self) -> None:
        # built-in channels run with the real daemon; a third-party channel is the only proxy user that needs this
        self.assertIn(self.SECRET_TEXT, [m["text"] for m in self.chan.list_chat(self.room)])
        self.assertEqual(len(self.chan.list_memories(actor=self.actor, scope="private")), 1)
        self.assertTrue(self.chan.list_album_view(scope="private", actor=self.actor)["ok"])
        self.assertEqual(len(self.chan.album.list_items(scope="private")), 1)

    def test_bot_speaker_cannot_converse_in_a_private_room_and_gets_no_history(self) -> None:
        bot = Speaker(BOT, "b", "u1")
        for proxy in (self.chan, self.plain):
            with self.assertRaises(PermissionError):
                proxy.converse(self.room, "hi", bot)
            with self.assertRaises(PermissionError):
                proxy.converse(f"  {self.room}", "hi", bot, actor=self.actor)
        # a bot may still talk to a public room (the daemon ignores it there)
        res = self.chan.converse("group", "hi", bot)
        self.assertEqual(res.get("ignored"), "bot_speaker")

    def test_forged_owner_and_members_still_refused_and_bound_owner_still_works(self) -> None:
        with self.assertRaises(PermissionError):
            self.chan.converse(self.room, "hi", Speaker(OWNER, "房主", "u-x", "user:owner"))
        with self.assertRaises(PermissionError):
            self.chan.converse(self.room, "hi", member_speaker("x", uid="u2"))
        self.d.mutate_identity({"op": "bind", "platform": "tp_chan", "external_id": "u-owner", "identity_id": "user:owner"})
        spk = self.chan.resolve_speaker("u-owner", fallback_name="Boss")
        res = self.chan.converse(self.room, "你好呀", spk)
        self.assertTrue(res["ok"])
        self.assertTrue(res["replies"])


# --------------------------------------------------------------------------------------------------------------------
# 3. env view
# --------------------------------------------------------------------------------------------------------------------
class EnvWhitelist(unittest.TestCase):
    def setUp(self) -> None:
        pat = _fake("ghp", "x" * 12)
        self.env = {
            # secrets the old name/value regexes missed
            "GITHUB_PAT": pat,
            "SENTRY_DSN": "https://" + "abc123" + "@o1.sentry.example/42",
            "DB_PASS": _fake("db", "pw", "x" * 8),
            "PGPASSWD": _fake("pg", "pw", "x" * 8),
            "MY_CREDENTIALS": _fake("cred", "x" * 8),
            "JWT_SIGNING": _fake("jwt", "x" * 8),
            "API_URL": "https://h.example/v1?api_key=" + "x" * 10,
            "SIG_URL": "https://h.example/v1/f?sig=" + "y" * 10,
            "AMZ_URL": "https://h.example/o?X-Amz-Signature=" + "z" * 10,
            "BASIC_URL": "https://user:" + "pw" * 4 + "@h.example/x",
            "USER_ONLY_URL": "https://" + "w" * 8 + "@h.example/x",
            "INNOCENT_NAME": "ghp_" + "q" * 24,
            "PEM_BLOB": "-----BEGIN " + "PRIVATE" + " KEY-----abc",
            "SESSION_STORE": "x",
            "SSL_CERT": "x",
            "OPENAI_API_KEY": _fake("sk", "x" * 12),
            "AI_HOUSE_API_KEY": _fake("sk", "main", "x" * 12),
            "AI_HOUSE_OWNER_TOKEN": _fake("own", "x" * 12),
            "AI_HOUSE_MIRROR": "https://u:" + "p" * 6 + "@m.example",
            "AI_HOUSE_BAD_QUERY": "https://m.example/x?token=" + "t" * 6,
            "AI_HOUSE_SIG_QUERY": "https://m.example/x?a=1&signature=" + "t" * 6,
            "AI_HOUSE_PASS": "x",
            "AI_HOUSE_SESSION_ID": "x",
            # harmless: visible
            "TZ": "Asia/Tokyo",
            "AI_HOUSE_THEME": "tea",
            "AI_HOUSE_MAX_TOKENS": "512",
            "AI_HOUSE_AUTHOR": "somebody",
            "AI_HOUSE_PASSTHROUGH": "1",
            "AI_HOUSE_KEYBOARD_LAYOUT": "us",
            "AI_HOUSE_ENDPOINT": "https://m.example/v1?model=x&max_tokens=5&design=2",
            # not declared by anybody and not AI_HOUSE_*: whitelist hides them
            "HTTPS_PROXY": "http://proxy.local:3128",
            "PATH": "/usr/bin",
            "MYPLUG_ENDPOINT": "https://mine.example/",
            "MYPLUG_SECRET_THING": _fake("mine", "x" * 8),
        }
        self.reg = PluginRegistry()
        for pid, env_vars in (
            ("myplug", {"ENDPOINT": "MYPLUG_ENDPOINT", "SECRET_THING": "MYPLUG_SECRET_THING", "STOLEN": "AI_HOUSE_API_KEY"}),
            ("other", {}),
        ):
            self.reg.register_definition(
                PluginDefinition(
                    manifest=PluginManifest(
                        plugin_id=pid, name=pid,
                        config_fields=tuple(ConfigField(key=k.lower(), label=k, env_var=v, secret=True) for k, v in env_vars.items()),
                    ),
                    source="entrypoint",
                )
            )
        self.daemon = types.SimpleNamespace(env=self.env, registry=self.reg)

    def test_known_misses_are_hidden(self) -> None:
        view = PluginHostProxy(self.daemon, "other").env
        for name in (
            "GITHUB_PAT", "SENTRY_DSN", "DB_PASS", "PGPASSWD", "MY_CREDENTIALS", "JWT_SIGNING", "API_URL", "SIG_URL",
            "AMZ_URL", "BASIC_URL", "USER_ONLY_URL", "INNOCENT_NAME", "PEM_BLOB", "SESSION_STORE", "SSL_CERT",
            "OPENAI_API_KEY", "AI_HOUSE_API_KEY", "AI_HOUSE_OWNER_TOKEN", "AI_HOUSE_MIRROR", "AI_HOUSE_BAD_QUERY",
            "AI_HOUSE_SIG_QUERY", "AI_HOUSE_PASS", "AI_HOUSE_SESSION_ID",
        ):
            self.assertNotIn(name, view, name)
        flat = " ".join(view.values())
        for needle in ("ghp_", "abc123", "api_key=", "sig=", "Signature=", "sk-"):
            self.assertNotIn(needle, flat)

    def test_harmless_variables_stay_visible(self) -> None:
        view = PluginHostProxy(self.daemon, "other").env
        for name in ("TZ", "AI_HOUSE_THEME", "AI_HOUSE_MAX_TOKENS", "AI_HOUSE_AUTHOR", "AI_HOUSE_PASSTHROUGH",
                     "AI_HOUSE_KEYBOARD_LAYOUT", "AI_HOUSE_ENDPOINT"):
            self.assertEqual(view.get(name), self.env[name], name)

    def test_only_declared_variables_beyond_the_harmless_set(self) -> None:
        other = PluginHostProxy(self.daemon, "other").env
        mine = PluginHostProxy(self.daemon, "myplug").env
        for name in ("HTTPS_PROXY", "PATH", "MYPLUG_ENDPOINT", "MYPLUG_SECRET_THING"):
            self.assertNotIn(name, other, name)
        self.assertEqual(mine["MYPLUG_ENDPOINT"], "https://mine.example/")
        self.assertEqual(mine["MYPLUG_SECRET_THING"], self.env["MYPLUG_SECRET_THING"])  # its own declared secret
        self.assertNotIn("HTTPS_PROXY", mine)
        # declaring a host credential in a manifest does not unlock it
        self.assertNotIn("AI_HOUSE_API_KEY", mine)

    def test_view_is_read_only_and_public_helper_matches(self) -> None:
        view = PluginHostProxy(self.daemon, "other").env
        with self.assertRaises(TypeError):
            view["X"] = "1"  # type: ignore[index]
        self.assertEqual(dict(public_env_view(self.env)), dict(view))


# --------------------------------------------------------------------------------------------------------------------
# 4. quota calls are bound to the proxy's own plugin id
# --------------------------------------------------------------------------------------------------------------------
class FactoryAndConfigEnv(_DaemonCase):
    """The same whitelist applies to what a third-party factory is handed and to config resolved from the env."""

    HOST_KEY = _fake("sk", "host", "main", "x" * 12)
    OWN_KEY = _fake("tp", "own", "y" * 12)

    def _env(self) -> Dict[str, str]:
        return {"AI_HOUSE_API_KEY": self.HOST_KEY, "TP_OWN_API_KEY": self.OWN_KEY, "AI_HOUSE_MODEL": "m",
                "HTTPS_PROXY": "http://" + "u:p" + "@proxy.example:3128"}

    def _defn(self, source: str, seen: List[Dict[str, str]], axis: str) -> PluginDefinition:
        def factory(env: Any = None) -> Any:
            seen.append(dict(env or {}))
            return _Chan() if axis == "channel" else None

        fields = (
            ConfigField(key="own_key", label="own", env_var="TP_OWN_API_KEY", secret=True),
            ConfigField(key="stolen", label="alias", env_var="AI_HOUSE_API_KEY", secret=True),
        )
        return PluginDefinition(
            manifest=PluginManifest(plugin_id=f"tp_{source}_{axis}", name="tp", kind="bundle", default_enabled=True,
                                    config_fields=fields),
            channel_factory=factory if axis == "channel" else None,
            provider_factory=factory if axis == "provider" else None,
            source=source,
        )

    def test_third_party_factories_never_receive_host_secrets(self) -> None:
        for axis in ("channel", "provider"):
            with self.subTest(axis=axis):
                seen: List[Dict[str, str]] = []
                reg = PluginRegistry()
                reg.register_definition(self._defn("entrypoint", seen, axis))
                if axis == "channel":
                    for _pid, build in reg.active_channels(env=self._env()):
                        build()
                else:
                    reg.active_providers(env=self._env())
                self.assertEqual(len(seen), 1)
                self.assertNotIn(self.HOST_KEY, " ".join(seen[0].values()))
                self.assertNotIn("HTTPS_PROXY", seen[0])
                self.assertEqual(seen[0].get("TP_OWN_API_KEY"), self.OWN_KEY, "its own declared variable stays visible")
                self.assertEqual(seen[0].get("AI_HOUSE_MODEL"), "m")

    def test_builtin_factories_keep_the_full_environment(self) -> None:
        seen: List[Dict[str, str]] = []
        reg = PluginRegistry()
        reg.register_definition(self._defn("builtin", seen, "channel"))
        for _pid, build in reg.active_channels(env=self._env()):
            build()
        self.assertEqual(seen[0].get("AI_HOUSE_API_KEY"), self.HOST_KEY)

    def test_manifest_cannot_alias_a_host_secret_into_its_config(self) -> None:
        d = self._daemon(self._env())
        d.registry.register_definition(self._defn("entrypoint", [], "channel"))
        pid = "tp_entrypoint_channel"
        for label, cfg in (
            ("proxy.config()", dict(PluginHostProxy(d, pid).config())),
            ("registry.resolve_config", d.registry.resolve_config(pid, env=d.env)),
            ("registry.safe_resolve_config", d.registry.safe_resolve_config(pid, env=d.env)[0]),
            ("daemon.plugin_config", dict(d.plugin_config(pid))),
        ):
            with self.subTest(path=label):
                self.assertNotIn(self.HOST_KEY, " ".join(str(v) for v in cfg.values()))
                self.assertEqual(cfg.get("own_key"), self.OWN_KEY)

    def test_builtin_config_still_resolves_host_namespace_variables(self) -> None:
        reg = PluginRegistry()
        reg.register_definition(self._defn("builtin", [], "channel"))
        self.assertEqual(reg.resolve_config("tp_builtin_channel", env=self._env()).get("stolen"), self.HOST_KEY)


class _Host:
    def __init__(self, router: ModelRouter) -> None:
        self.model_router = router


class QuotaBoundToOwnId(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clk = _Clock()
        self.router = ModelRouter({}, runtime_dir=tmp.name, clock=self.clk)
        self.addCleanup(self.router.close)
        self.a = PluginHostProxy(_Host(self.router), "plugin_a")

    def _victim_slot_is_free(self) -> bool:
        err = self.router.check_plugin_quota("image_gen", speaker_id="o", is_owner=True, daily_limit=1)
        return err is None

    def test_plugin_cannot_reserve_another_plugins_quota(self) -> None:
        for _ in range(5):
            err = self.a.check_plugin_quota("image_gen", speaker_id="x", is_owner=True, daily_limit=1)
            self.assertTrue(err)  # refused, never a free pass
        self.assertTrue(self._victim_slot_is_free())

    def test_plugin_cannot_settle_or_fabricate_records_for_another_plugin(self) -> None:
        with self.assertRaises(PermissionError):
            self.a.record_plugin_call("image_gen", speaker_id="x", is_owner=True, ok=True)
        self.assertTrue(self._victim_slot_is_free())

    def test_refusal_does_not_leak_the_other_plugins_usage(self) -> None:
        self.router.check_plugin_quota("web_search", speaker_id="o", is_owner=True, daily_limit=7)
        err = self.a.check_plugin_quota("web_search", daily_limit=7) or ""
        self.assertTrue(err)
        self.assertNotIn("7", err)
        self.assertNotIn("/", err)

    def test_own_quota_still_works_with_or_without_explicit_id(self) -> None:
        self.assertIsNone(self.a.check_plugin_quota("plugin_a", speaker_id="x", daily_limit=2))
        self.a.record_plugin_call("plugin_a", speaker_id="x", ok=True)
        self.assertIsNone(self.a.check_plugin_quota(speaker_id="x", daily_limit=2))
        self.a.record_plugin_call(speaker_id="x", ok=True)
        self.assertTrue(self.a.check_plugin_quota(speaker_id="x", daily_limit=2))  # 2/2 used
        # and it is accounted under plugin_a only
        self.assertTrue(self._victim_slot_is_free())

    def test_id_less_proxy_has_no_quota(self) -> None:
        anon = PluginHostProxy(_Host(self.router), "")
        self.assertTrue(anon.check_plugin_quota("image_gen", daily_limit=1))
        with self.assertRaises(PermissionError):
            anon.record_plugin_call("image_gen", ok=True)


# --------------------------------------------------------------------------------------------------------------------
# 5. quota edge cases (midnight, daily_limit=0)
# --------------------------------------------------------------------------------------------------------------------
class QuotaEdges(unittest.TestCase):
    def _router(self, t: float) -> tuple[ModelRouter, _Clock]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clk = _Clock(t)
        r = ModelRouter({}, runtime_dir=tmp.name, clock=clk)
        self.addCleanup(r.close)
        return r, clk

    def test_reservation_settled_after_midnight_counts_for_the_day_it_was_reserved(self) -> None:
        t0 = dt.datetime(2026, 11, 22, 23, 59, 0, tzinfo=JST).timestamp()
        r, clk = self._router(t0)
        self.assertIsNone(r.check_plugin_quota("p", speaker_id="o", is_owner=True, daily_limit=2))
        clk.t += 120  # 00:01 the next day
        r.record_plugin_call("p", speaker_id="o", is_owner=True, ok=True, model="m", base_url="x", actor="a")
        got = 0
        while r.check_plugin_quota("p", speaker_id="o", is_owner=True, daily_limit=2) is None and got < 5:
            got += 1
        self.assertEqual(got, 2, "the new day must start with the full budget")
        # the usage ledger still shows the call on the day it was reserved
        by_day = r.get_usage_summary(period="all")["by_day"]
        self.assertEqual({row["day"]: row["calls"] for row in by_day}, {"2026-11-22": 1})

    def test_same_day_accounting_is_unchanged(self) -> None:
        t0 = dt.datetime(2026, 11, 22, 10, 0, 0, tzinfo=JST).timestamp()
        r, _ = self._router(t0)
        for _ in range(2):
            self.assertIsNone(r.check_plugin_quota("p", speaker_id="o", is_owner=True, daily_limit=2))
            r.record_plugin_call("p", speaker_id="o", is_owner=True, ok=True)
        self.assertTrue(r.check_plugin_quota("p", speaker_id="o", is_owner=True, daily_limit=2))

    def test_daily_limit_zero_means_no_calls_and_none_means_default(self) -> None:
        r, _ = self._router(1700000000.0)
        for lim in (0, -3):
            err = r.check_plugin_quota("p0", speaker_id="o", is_owner=True, daily_limit=lim)
            self.assertTrue(err, lim)
        n = 0
        while r.check_plugin_quota("pd", speaker_id="o", is_owner=True, daily_limit=None) is None and n < 100:  # type: ignore[arg-type]
            n += 1
        self.assertEqual(n, 60)
        n = 0
        while r.check_plugin_quota("pe", speaker_id="o", is_owner=True) is None and n < 100:
            n += 1
        self.assertEqual(n, 60)


if __name__ == "__main__":
    unittest.main()
