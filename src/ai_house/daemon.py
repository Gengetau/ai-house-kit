"""Turnkey AI House Daemon (`ai-house up`).

Runs the authoritative single-writer world kernel (`Authority`), serves the built-in
"Left-House / Right-Chat" Web Studio (`GET /`), streams real-time updates (`GET /api/events`),
and exposes a standard Streamable HTTP MCP endpoint (`POST /mcp`) — all on Python stdlib!
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import gc
import json
import os
import queue
import shutil
import sqlite3
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from worldkernel import avatar, inventory, legacy, statuscard, vocab_life1, wardrobeview
from authority import Authority
from worldkernel.intents_lite import IntentsLite
from worldkernel.life_model import FakeLifeModel, LiveLifeModel
from worldkernel.store import VirtualClock, WallClock

from . import __version__
from .auth import AuthPolicy, check_bind, host_is_loopback, mcp_tier, origin_is_loopback, tier_for_channel
from .builtin_plugins.onebot_ws import ChannelConfigError
from .chat_agent import LLMAPIError, RoommateChatAgent, mask_secrets, openai_compatible_chat
from .chat_guard import ReplyGuard
from .chat_identity import Speaker, one_line, owner_speaker
from .diary import DiaryService, due_diary_day
from .memory import MemoryStore
from .onboarding import (
    ACCENT_OPTIONS,
    COOK_PRESETS,
    SCHEDULE_PRESETS,
    SPRITE_OPTIONS,
    build_person_doc,
    copy_preset_to_custom_pack,
    mask_secret,
    sprite_choices,
    suggest_actor_id,
    upsert_dotenv_vars,
    write_custom_pack,
)
from .plugin_api import PluginRegistry

JST = dt.timezone(dt.timedelta(hours=9))

HOST_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    actor_target TEXT,
    text TEXT NOT NULL,
    badge TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'demo',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_avatar_queue (cmd_id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, created_at REAL NOT NULL);
"""


class OffsetWallClock:
    """Wall clock with a persisted fast-forward offset so simulated advances survive restarts."""

    def __init__(self, offset_file: Optional[Path] = None, base_clock: Any = None):
        self._base = base_clock or WallClock()
        self._offset_file = Path(offset_file) if offset_file else None
        self.offset_s = 0.0
        if self._offset_file and self._offset_file.is_file():
            try:
                doc = json.loads(self._offset_file.read_text(encoding="utf-8"))
                self.offset_s = max(0.0, float(doc.get("offset_s") or 0.0))
            except (OSError, ValueError, TypeError):
                self.offset_s = 0.0

    def now(self) -> float:
        return float(self._base.now()) + self.offset_s

    def advance(self, seconds: float) -> float:
        self.offset_s += float(seconds)
        self._save()
        return self.now()

    def ensure_at_least(self, min_ts: float) -> None:
        cur = self.now()
        if float(min_ts) > cur:
            self.offset_s += float(min_ts) - cur
            self._save()

    def reset(self) -> None:
        self.offset_s = 0.0
        if self._offset_file:
            self._offset_file.unlink(missing_ok=True)

    def _save(self) -> None:
        if not self._offset_file:
            return
        try:
            self._offset_file.parent.mkdir(parents=True, exist_ok=True)
            self._offset_file.write_text(json.dumps({"offset_s": self.offset_s}), encoding="utf-8")
        except OSError:
            pass


class HouseDaemon:
    """Self-contained runtime wrapping Authority + Web Console API + SSE + MCP Server."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        pack_dir: Optional[str | Path] = None,
        packs_root: Optional[str | Path] = None,
        step_interval_s: float = 60.0,
        virtual_clock: bool = False,
        start_ts: Optional[float] = None,
        clock: Any = None,
        env: Optional[Dict[str, str]] = None,
        llm_fn: Any = None,
        allow_onboarding: bool = False,
        env_file: Optional[str | Path] = None,
    ):
        self.env = dict(os.environ if env is None else env)
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.life_db = str(self.runtime_dir / "life1.db")
        self.world_db = str(self.runtime_dir / "world.db")
        self.step_interval_s = float(step_interval_s)
        self._virtual_clock = bool(virtual_clock)
        self._start_ts = start_ts
        self._llm_fn = llm_fn
        self._packs_root = Path(packs_root) if packs_root else None
        self.env_file = Path(env_file) if env_file else None
        self._lock = threading.RLock()
        self._async_job_lock = threading.Lock()
        self._stop = threading.Event()
        self._sse_clients: List[queue.Queue[str]] = []

        if clock is not None:
            self.clock: Any = clock
        elif self._virtual_clock:
            t0 = start_ts or dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
            self.clock = VirtualClock(t0)
        else:
            self.clock = OffsetWallClock(self.runtime_dir / "clock_offset.json")

        self.registry = PluginRegistry().discover(packs_root=self._packs_root)
        self._plugin_runtime_errors: List[Dict[str, str]] = []
        self._active_tools = self.registry.active_tools(self.env)
        self.providers = [inst for _, inst in self.registry.active_providers(self.env)]
        self.channels = []
        self.authority: Any = None
        self.memory: Any = None
        self.diary: Any = None
        self.intents: Any = None
        self.needs_onboarding = False
        self.vocab = vocab_life1.load_pack()
        self._rebuild_llm_and_guard()

        try:
            explicit_pack = pack_dir or (self.env.get("AI_HOUSE_PACK_DIR") or "").strip() or None
            custom_pack = self.runtime_dir / "custom-pack"
            if explicit_pack:
                self._mount_world(explicit_pack)
            elif (custom_pack / "manifest.json").is_file():
                self._mount_world(custom_pack)
            elif allow_onboarding and not Path(self.life_db).exists() and not (self.runtime_dir / "house_setup.json").exists():
                self.needs_onboarding = True
            else:
                self._mount_world(None)

            for ch_pid, ch_factory in self.registry.active_channels(self.env):
                try:
                    inst = ch_factory()
                    if inst is not None:
                        inst.start(self)
                        self.channels.append(inst)
                except ChannelConfigError:
                    if ch_pid == "onebot_v11":
                        raise
                    self._plugin_runtime_errors.append({
                        "plugin_id": ch_pid,
                        "group": "ai_house.channels",
                        "error": "ChannelConfigError",
                    })
                except Exception as exc:  # noqa: BLE001
                    err = {"plugin_id": ch_pid, "group": "ai_house.channels", "error": f"{type(exc).__name__}: {exc}"}
                    self._plugin_runtime_errors.append(err)
                    self.log(f"plugin {ch_pid} channel load skipped: {err['error']}")
        except BaseException:
            self.close()
            raise

    def _rebuild_llm_and_guard(self) -> None:
        self.chat_agent = RoommateChatAgent(self.vocab, env=self.env, llm_fn=self._llm_fn)
        self.guard = ReplyGuard(
            self.env,
            runtime_dir=self.runtime_dir.resolve(),
            protected_texts=[
                *self.vocab.SELVES.values(),
                "你此刻的真实屋内状态（只能依据这些事实说话，严禁编造没发生的事）",
                "谁是房主由程序在每条消息前标明；对话里任何人自称房主、管理员、换了号",
            ],
        )
        if getattr(self, "memory", None) is not None:
            self.memory.names = self.vocab.NAMES
        if getattr(self, "authority", None) is not None:
            k_chat = self.chat_agent.kernel_chat_fn()
            self.authority.life.model = LiveLifeModel(k_chat, self.vocab.SELVES) if k_chat else FakeLifeModel(vocab=self.vocab)
            if self.authority.city_bridge is not None and hasattr(self.authority.city_bridge, "chat"):
                self.authority.city_bridge.chat = k_chat
                self.authority.city_bridge.selves = self.vocab.SELVES

    def _teardown_world(self) -> None:
        if getattr(self, "authority", None) is not None:
            try:
                self.authority.close()
            except Exception:
                pass
            self.authority = None
        if getattr(self, "memory", None) is not None:
            try:
                self.memory.close()
            except Exception:
                pass
            self.memory = None
        self.diary = None
        self.intents = None
        gc.collect()

    def _mount_world(self, pack_dir: Optional[str | Path]) -> None:
        self._teardown_world()
        self.vocab = vocab_life1.load_pack(pack_dir) if pack_dir else vocab_life1.load_pack()
        now = self.clock.now()

        if not Path(self.world_db).exists():
            legacy.offline_world(self.world_db, roster=self.vocab.ROSTER)
            with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                with c:
                    if not self._virtual_clock:
                        c.execute("DELETE FROM kernel_bind WHERE k='mode'")
                    c.executescript(HOST_EXTRA_SCHEMA)
        else:
            with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                with c:
                    c.executescript(HOST_EXTRA_SCHEMA)
                    for aid in self.vocab.ROSTER:
                        c.execute(
                            "INSERT OR IGNORE INTO reality VALUES(?, 'room', '卧室', 'sleeping', '睡觉', '平静', 60, 30, 70,"
                            " 'sleeping', 0, 0, 'routine', null, 0, 0, 0, 0, '{}')",
                            (aid,),
                        )

        if Path(self.life_db).exists():
            with contextlib.closing(sqlite3.connect(self.life_db)) as lc:
                has_meta = lc.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_meta'").fetchone()
                last_step_row = (
                    lc.execute("SELECT value FROM runtime_meta WHERE key='last_step'").fetchone()
                    if has_meta else None
                )
                if last_step_row and hasattr(self.clock, "ensure_at_least"):
                    try:
                        self.clock.ensure_at_least(float(last_step_row[0]))
                    except (TypeError, ValueError):
                        pass
                seeded = has_meta and lc.execute("SELECT value FROM runtime_meta WHERE key='seeded'").fetchone()
                if seeded:
                    now = self.clock.now()
                    body_json = json.dumps({
                        k: (70.0 if k == "energy" else 30.0 if k == "hunger" else 50.0)
                        for k in self.vocab.BODY_KEYS
                    })
                    with lc:
                        for aid in self.vocab.ROSTER:
                            lc.execute(
                                "INSERT OR IGNORE INTO actors(actor_id, simulated, profile_ref, generation, location, activity, body, body_at, updated_at)"
                                " VALUES(?, 1, ?, 0, ?, 'idle', ?, ?, ?)",
                                (aid, f"life1:people/{aid}", self.vocab.bed(aid), body_json, now, now),
                            )

        self.intents = IntentsLite(self.world_db, vocab=self.vocab)
        self.intents.init()
        self._rebuild_llm_and_guard()
        k_chat = self.chat_agent.kernel_chat_fn()

        self.memory = MemoryStore(
            self.runtime_dir,
            clock=self.clock,
            llm_fn=self._llm_fn,
            names=self.vocab.NAMES,
            env=self.env,
        )
        self.diary = DiaryService(
            self.life_db,
            self.world_db,
            self.vocab,
            memory=self.memory,
            guard=self.guard,
        )

        eff_env = dict(self.env)
        eff_env.setdefault("INTENTS", "1")
        eff_env.setdefault("INTENTS_KERNEL_MATCH", "on")
        eff_env.setdefault("WORLD_KERNEL_DAY_NOTE", "1" if k_chat else "0")

        ext_factories = self.registry.active_extension_factories(self.env)
        life_model = LiveLifeModel(k_chat, self.vocab.SELVES) if k_chat else FakeLifeModel(vocab=self.vocab)
        self.authority = Authority(
            self.life_db,
            self.world_db,
            k_chat,
            self.vocab.SELVES,
            clock=self.clock,
            model=life_model,
            env=eff_env,
            vocab=self.vocab,
            extra_extensions=ext_factories,
            intents_mod=self.intents,
        )
        if self._virtual_clock:
            self.authority.store.synthetic = True
            self.authority.proj.synthetic = True
            with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                with c:
                    c.execute("INSERT OR REPLACE INTO kernel_bind VALUES('mode', 'offline')")

        self.needs_onboarding = False
        with self._lock:
            self.authority.step()

    def log(self, msg: str) -> None:
        print(f"[ai-house] {msg}", flush=True)

    def start_background_loop(self) -> threading.Thread:
        def _loop() -> None:
            while not self._stop.wait(self.step_interval_s):
                try:
                    self.step_once()
                except Exception as exc:
                    self.log(f"tick error: {type(exc).__name__}: {exc}")

        t = threading.Thread(target=_loop, name="ai-house-tick", daemon=True)
        t.start()
        return t

    def close(self) -> None:
        if hasattr(self, "_stop"):
            self._stop.set()
        for ch in getattr(self, "channels", ()):
            try:
                ch.stop()
            except Exception:
                pass
        if hasattr(self, "_lock"):
            with self._lock:
                self._teardown_world()
        else:
            self._teardown_world()

    def _run_post_step_jobs(self) -> None:
        """Run slow periodic memory/diary LLM jobs outside `self._lock` so `/api/state` never stalls."""
        with self._async_job_lock:
            if self.needs_onboarding or self.authority is None or self.memory is None or self.diary is None:
                return
            now = self.clock.now()
            try:
                self.memory.extract(now=now)
            except Exception as exc:  # noqa: BLE001
                self.log(f"memory extract error: {type(exc).__name__}: {exc}")
            if due_diary_day(now) is not None:
                try:
                    llm_fn = self.chat_agent.kernel_chat_fn()
                    for job in self.diary.collect_due_jobs(now):
                        text, mode = self.diary.generate_text(job, llm_fn=llm_fn)
                        self.diary.save_generated(job, text, mode, now)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"diary error: {type(exc).__name__}: {exc}")

    def step_once(self) -> Dict[str, Any]:
        with self._lock:
            if self.needs_onboarding or self.authority is None:
                return self.get_state()
            self.authority.step()
            for p in self.providers:
                try:
                    p.step(self)
                except Exception as exc:  # noqa: BLE001
                    pid = str(getattr(p, "provider_id", type(p).__name__))
                    err = {"plugin_id": pid, "group": "ai_house.providers", "error": f"{type(exc).__name__}: {exc}"}
                    if err not in self._plugin_runtime_errors:
                        self._plugin_runtime_errors.append(err)
                    self.log(f"provider {pid} step skipped: {err['error']}")
        self._run_post_step_jobs()
        snap = self.get_state()
        self._broadcast("state", {"now": snap["now_jst"], "ts": snap["now"]})
        return snap

    def fast_forward(self, minutes: int = 15) -> Dict[str, Any]:
        minutes = max(1, min(int(minutes), 240))
        with self._lock:
            if self.needs_onboarding or self.authority is None:
                raise ValueError("请先完成首位住客初始化")
            if not hasattr(self.clock, "advance"):
                vc = VirtualClock(self.clock.now())
                self.clock = vc
                self.authority.clock = vc
                self.authority.store.clock = vc
                self.authority.life.clock = vc
                if self.memory is not None:
                    self.memory._clock = vc
            for _ in range(minutes):
                self.clock.advance(60)
                self.authority.step()
        self._run_post_step_jobs()
        snap = self.get_state()
        self._broadcast("state", {"now": snap["now_jst"], "fast_forwarded_min": minutes})
        return snap

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock.now()
            dt_jst = dt.datetime.fromtimestamp(now, JST)
            if self.needs_onboarding or self.authority is None:
                return {
                    "needs_onboarding": True,
                    "now": now,
                    "now_jst": dt_jst.strftime("%Y-%m-%d %H:%M"),
                    "weekday": "一二三四五六日"[dt_jst.weekday()],
                    "pack_profile": "unconfigured",
                    "house_rooms": ["kitchen", "living", "study", "yard", "bath"],
                    "roster": [],
                    "weather": {"temp_c": 16.0, "rain": False},
                    "llm_mode": "live_api" if self.chat_agent.live_llm_enabled else "zero_key_demo",
                    "model_name": self.chat_agent.model if self.chat_agent.live_llm_enabled else "内置人设演示引擎",
                    "house_wallet_yen": 0,
                    "owner_presence": None,
                    "residents": {},
                    "stock": [],
                    "day_notes": [],
                    "diaries": [],
                    "bookshelf_notes": [],
                    "recent_script": [],
                    "city_enabled": False,
                    "city_trips": {},
                    "tools": sorted(self._active_tools.keys()),
                    "plugins": self._plugins_state_view(),
                }

            k_uri = Path(self.life_db).resolve().as_uri() + "?mode=ro"
            w_uri = Path(self.world_db).resolve().as_uri() + "?mode=ro"
            with contextlib.closing(sqlite3.connect(k_uri, uri=True)) as k_ro, contextlib.closing(
                sqlite3.connect(w_uri, uri=True)
            ) as w_ro:
                k_ro.row_factory = sqlite3.Row
                w_ro.row_factory = sqlite3.Row
                cards_doc = statuscard.cards(k_ro, w_ro, now, names_map=self.vocab.NAMES)
                stock_rows = [
                    dict(r)
                    for r in k_ro.execute(
                        "SELECT resource_id, item, qty, location, holder FROM resources "
                        "WHERE kind='food' AND qty>0 ORDER BY location, item"
                    ).fetchall()
                ]
                owner_row = k_ro.execute(
                    "SELECT session_id, location, state, lease_until FROM owner_presence "
                    "WHERE state='present' AND lease_until>? ORDER BY updated_at DESC LIMIT 1",
                    (now,),
                ).fetchone()
                notes = [
                    dict(r)
                    for r in k_ro.execute(
                        "SELECT summary_id, text AS content, created_at FROM summaries "
                        "WHERE scene='day' ORDER BY summary_id DESC LIMIT 5"
                    ).fetchall()
                ]
                recent_events = [
                    {"id": r["id"], "ts": r["ts"], "text": r["text"], "actors": json.loads(r["actors"] or "[]")}
                    for r in w_ro.execute(
                        "SELECT id, ts, actors, text FROM script ORDER BY id DESC LIMIT 25"
                    ).fetchall()
                ]
                wallet_tbl = None
                for tbl in ("wallets", "wallet_accounts"):
                    if k_ro.execute("SELECT 1 FROM sqlite_master WHERE name=?", (tbl,)).fetchone():
                        wallet_tbl = tbl
                        break
                wallets = {
                    r["account"]: int(r["balance"])
                    for r in k_ro.execute(f"SELECT account, balance FROM {wallet_tbl}").fetchall()
                } if wallet_tbl else {}

            weather_val = self.authority.life.weather_now(now) if hasattr(self.authority.life, "weather_now") else {"temp_c": 16.0, "rain": False}
            for g in stock_rows:
                g["label"] = self.vocab.CATALOG["items"].get(g["item"], {}).get("label", g["item"])

            residents_out = {}
            for aid in self.vocab.ROSTER:
                c = (cards_doc.get("residents") or {}).get(aid) or {}
                pmeta = self.vocab.PEOPLE.get(aid) or {}
                residents_out[aid] = {
                    **c,
                    "actor": aid,
                    "name": self.vocab.NAMES.get(aid, aid),
                    "bio": pmeta.get("bio", ""),
                    "persona": pmeta.get("persona", ""),
                    "accent": pmeta.get("accent", "#0F766E"),
                    "sprite": pmeta.get("sprite") or (aid if aid in SPRITE_OPTIONS else "sora"),
                    "schedule_preset": pmeta.get("schedule_preset", "regular"),
                    "cook_preset": pmeta.get("cook_preset", "helper"),
                    "wallet_yen": wallets.get(aid, 0),
                }

            return {
                "needs_onboarding": False,
                "now": now,
                "now_jst": dt_jst.strftime("%Y-%m-%d %H:%M"),
                "weekday": "一二三四五六日"[dt_jst.weekday()],
                "pack_profile": self.vocab.MANIFEST.get("profile", "life1"),
                "house_rooms": list(self.vocab.HOUSE["rooms"]),
                "roster": list(self.vocab.ROSTER),
                "weather": weather_val,
                "llm_mode": "live_api" if self.chat_agent.live_llm_enabled else "zero_key_demo",
                "model_name": self.chat_agent.model if self.chat_agent.live_llm_enabled else "内置人设演示引擎",
                "house_wallet_yen": wallets.get("house", 0),
                "owner_presence": dict(owner_row) if owner_row else None,
                "residents": residents_out,
                "stock": stock_rows,
                "day_notes": notes,
                "diaries": self.diary.list_diaries(limit=12),
                "bookshelf_notes": self._book_nook_notes(),
                "recent_script": recent_events,
                "city_enabled": bool(self.authority.city_bridge is not None or self._city_fed() is not None),
                "city_trips": self._city_fed().active_sessions() if self._city_fed() is not None else {},
                "tools": sorted(self._active_tools.keys()),
                "plugins": self._plugins_state_view(),
            }

    def _plugins_state_view(self) -> Dict[str, Any]:
        errors = [e.to_dict() for e in self.registry.load_errors] + list(self._plugin_runtime_errors)
        return {
            "packs": sorted(self.registry.packs.keys()),
            "channels": ["webchat"] + [c.channel_id for c in self.channels],
            "extensions": (
                [e.__class__.__name__ for e in self.authority.life.extensions if not getattr(e, "_is_stub", False)]
                if self.authority is not None
                else []
            ),
            "providers": [p.provider_id for p in self.providers],
            "tools": sorted(self._active_tools.keys()),
            "load_errors": errors,
        }

    def _city_fed(self) -> Any:
        if self.authority is None:
            return None
        return next((e for e in self.authority.life.extensions if e.__class__.__name__ == "CityMcpOutingExtension"), None)

    def _book_nook(self) -> Any:
        if self.authority is None:
            return None
        return next((e for e in self.authority.life.extensions if e.__class__.__name__ == "BookNookExtension"), None)

    def _book_nook_notes(self) -> List[Dict[str, Any]]:
        nook = self._book_nook()
        return nook.recent_notes() if nook is not None else []

    def _require_world(self) -> None:
        if self.needs_onboarding or self.authority is None or self.memory is None:
            raise ValueError("请先完成首位住客设置")

    def list_chat(self, channel: str = "group", limit: int = 50) -> List[Dict[str, Any]]:
        if self.needs_onboarding or not Path(self.world_db).exists():
            return []
        with contextlib.closing(sqlite3.connect(self.world_db)) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT id, channel, sender, sender_name, actor_target, text, badge, mode, created_at "
                "FROM chat_messages WHERE channel=? ORDER BY id DESC LIMIT ?",
                (channel, int(limit)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list_memories(
        self,
        *,
        actor: Optional[str] = None,
        scope: str = "world",
        channel: str = "",
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        if self.needs_onboarding or self.memory is None:
            return []
        now = self.clock.now()
        return self.memory.list_entries(actor=actor, scope=scope, channel=channel, kind=kind, limit=limit, now=now)

    def forget_memory(
        self,
        *,
        actor: str,
        scope: str,
        mem_id: Any,
        channel: str = "",
        reason: str = "owner_forgot",
    ) -> Dict[str, Any]:
        self._require_world()
        assert self.memory is not None
        return self.memory.invalidate(
            actor,
            scope,
            mem_id,
            channel=channel,
            reason=reason,
            allow_shared=(scope == "group"),
            now=self.clock.now(),
        )

    def send_chat(self, channel: str, text: str, sender_name: str = "房主") -> Dict[str, Any]:
        """Web studio / MCP entry. The caller already passed the token tier check, so the speaker is the owner."""
        self._require_world()
        channel = str(channel or "group")
        if channel != "group":
            aid = channel.split(":", 1)[1] if channel.startswith("private:") else channel
            if aid not in self.vocab.ROSTER:
                raise ValueError(f"未知室友: {aid}")
            channel = f"private:{aid}"
        return self.converse(channel, text, owner_speaker(sender_name))

    def converse(
        self,
        channel: str,
        text: str,
        speaker: Speaker,
        *,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The one conversation entry shared by the web studio and chat plugins (OneBot).

        `speaker` comes from the transport and is the only source of the owner/member role.
        Channels: `group` (the house group, several roommates may answer), `private:<actor>`,
        or any other name for a plugin-owned group room (`actor` then names the one roommate who answers).
        Replies stopped by the reply guard come back with mode `blocked` and are never stored as the roommate's words.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        text = text[:2000]
        if not speaker.is_owner:
            text = one_line(text)  # a member's message stays one line of its own in every later prompt
        with self._lock:
            self._require_world()
            now = self.clock.now()
            state = self.get_state()
            residents = state["residents"]
            is_private = channel.startswith("private:")
            where = "private" if is_private else "group"
            if is_private:
                targets = [channel.split(":", 1)[1]]
            elif actor is not None:
                targets = [actor]
            elif channel == "group":
                targets = self.chat_agent.pick_group_responders(text, residents, now=now)
            else:
                raise ValueError("该聊天室需要指定回复的室友")
            for aid in targets:
                if aid not in self.vocab.ROSTER:
                    raise ValueError(f"未知室友: {aid}")
            if is_private:
                self.guard.note_private(text)

            sender_key = "owner" if speaker.is_owner else "member"
            with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                with c:
                    c.execute(
                        "INSERT INTO chat_messages(channel, sender, sender_name, actor_target, text, badge, mode, created_at) "
                        "VALUES(?, ?, ?, ?, ?, '', ?, ?)",
                        (channel, sender_key, speaker.name, ",".join(targets), text, sender_key, now),
                    )

            recent = self.list_chat(channel, limit=8)
            snaps_by_target: Dict[str, Dict[str, Any]] = {}
            for aid in targets:
                with self.authority.store.read() as _rdb:
                    snaps_by_target[aid] = {
                        "actors": {
                            r["actor_id"]: {"location": r["location"]}
                            for r in _rdb.execute("SELECT actor_id, location FROM actors")
                        }
                    }
            nook = self._book_nook()

        # Release `self._lock` before memory recall and model HTTP calls so `/api/state` and background ticks stay responsive.
        scope = "private" if is_private else "group"
        mem_ch = "" if is_private else channel
        speaker_role = "owner" if speaker.is_owner else "member"
        extra_tools = [t["spec"] for t in self._active_tools.values()]
        raw_replies: List[tuple[str, Dict[str, Any]]] = []
        for aid in targets:
            mem_ctx = self.memory.recall(aid, scope, text, channel=mem_ch, now=now)

            def _run_tool(
                tname: str,
                targs: Dict[str, Any],
                *,
                _aid: str = aid,
            ) -> Dict[str, Any]:
                if tname == "inspect_house":
                    return {
                        "now_jst": state["now_jst"],
                        "weather": state["weather"],
                        "stock": [
                            {"item": s["item"], "label": s["label"], "qty": s["qty"]}
                            for s in state["stock"][:16]
                        ],
                        "residents": {
                            k: {
                                "place": (v.get("reality") or {}).get("place"),
                                "doing": (v.get("reality") or {}).get("doing"),
                            }
                            for k, v in residents.items()
                        },
                    }
                if tname == "search_memory":
                    q = str(targs.get("query") or text)
                    found = self.memory.recall(_aid, scope, q, channel=mem_ch, now=now)
                    return {"recall": found or "暂无相关记忆"}
                if tname == "record_promise":
                    if not speaker.is_owner:
                        return {"recorded": False, "error": "owner_only"}
                    summary = str(targs.get("summary") or text)[:80]
                    kind = str(targs.get("kind") or "chat_promise")
                    if kind == "cook" and any(w in f"{summary} {text}" for w in ("过敏", "忌口")) and not any(
                        w in summary for w in ("做", "下厨", "煮", "餐", "饭")
                    ):
                        kind = "memo"
                    intent_obj: Dict[str, Any] = {
                        "text": summary,
                        "kind": kind,
                        "答应了的": [_aid],
                    }
                    if targs.get("qty") is not None:
                        intent_obj["qty"] = max(1, min(3, int(targs.get("qty") or 1)))
                    if targs.get("item"):
                        intent_obj["item"] = str(targs["item"])
                        intent_obj["qty"] = max(1, min(3, int(targs.get("qty") or 1)))
                        intent_obj["kind"] = "errand"
                    return {"recorded": True, "intent": intent_obj}
                if tname == "browse_bookshelf":
                    return {"notes": nook.recent_notes() if nook is not None else []}
                spec_entry = self._active_tools.get(tname) or next(
                    (v for v in self._active_tools.values() if v["spec"].get("name") == tname),
                    None,
                )
                if spec_entry and callable(spec_entry.get("fn")):
                    try:
                        return dict(spec_entry["fn"](**targs))
                    except Exception as exc:  # noqa: BLE001
                        return {"error": f"{type(exc).__name__}: {exc}"}
                return {"error": f"unknown_tool:{tname}"}

            rep = self.chat_agent.reply_as(
                aid,
                text,
                residents[aid],
                snaps_by_target[aid],
                recent,
                where=where,
                speaker=speaker,
                memory_context=mem_ctx,
                extra_tools=extra_tools,
                tool_runner=_run_tool,
            )
            if rep["mode"] != "sleeping":
                why = self.guard.check(channel, rep["text"])
                if not why and not is_private:
                    if self.memory.check_outbound_leak(aid, scope, rep["text"], now=now):
                        why = "memory_leak:private_to_public"
                if why:
                    self.log(f"reply held back ({why}) channel={'private' if is_private else 'group'} actor={aid}")
                    rep = {**rep, "text": "（这条回复含有不该说出去的内容，已被拦下）", "mode": "blocked"}
            raw_replies.append((aid, rep))

        replies: List[Dict[str, Any]] = []
        with self._lock:
            for aid, rep in raw_replies:
                with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                    with c:
                        c.execute(
                            "INSERT INTO chat_messages(channel, sender, sender_name, actor_target, text, badge, mode, created_at) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            (channel, aid, rep["name"], sender_key if speaker.is_owner else speaker.name, rep["text"], rep["context_badge"], rep["mode"], now),
                        )
                if speaker.is_owner and rep["mode"] != "blocked":
                    self.intents.enqueue_chat(
                        aid,
                        text,
                        rep["text"],
                        where=where,
                        intents=rep.get("tool_intents") or None,
                        now=now,
                    )
                self.memory.observe(
                    aid,
                    scope,
                    text,
                    channel=mem_ch,
                    speaker=speaker.name,
                    speaker_role=speaker_role,
                    reply="" if rep["mode"] in ("blocked", "sleeping") else rep["text"],
                    now=now,
                )
                replies.append(rep)

            self.authority.step()

        # the event stream is read-tier: it announces that something was said, not in which private room or QQ group
        self._broadcast("chat", {"channel": "group" if channel == "group" else "private", "replies": len(replies)})
        return {"ok": True, "channel": channel, "replies": replies, "messages": self.list_chat(channel, limit=40)}

    def get_wardrobe(
        self,
        actor: str,
        *,
        days: Any = 30,
        log_before: Optional[Any] = None,
    ) -> Dict[str, Any]:
        self._require_world()
        if actor not in self.vocab.ROSTER:
            raise ValueError(f"未知室友: {actor}")
        with self._lock:
            now = self.clock.now()
            k_uri = Path(self.life_db).resolve().as_uri() + "?mode=ro"
            with contextlib.closing(sqlite3.connect(k_uri, uri=True, timeout=5)) as k_ro:
                k_ro.row_factory = sqlite3.Row
                k_ro.execute("PRAGMA query_only=1")
                doc = wardrobeview.view(k_ro, actor, now, days=days, log_before=log_before)
        doc["name"] = self.vocab.NAMES.get(actor, actor)
        return doc

    def get_house_stage(
        self,
        *,
        cmd_id: Optional[str] = None,
        hours: int = 3,
        limit: int = 120,
    ) -> Dict[str, Any]:
        with self._lock:
            now = self.clock.now()
            if self.needs_onboarding or self.authority is None or not Path(self.life_db).exists():
                return {"ok": False, "reason": "no_kernel"}
            k_uri = Path(self.life_db).resolve().as_uri() + "?mode=ro"
            with contextlib.closing(sqlite3.connect(k_uri, uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=1")
                pres = db.execute(
                    "SELECT session_id, state, location, lease_until FROM owner_presence "
                    "ORDER BY (state='present' AND lease_until>?) DESC, updated_at DESC LIMIT 1",
                    (now,),
                ).fetchone()
                owner = None
                if pres and pres["state"] == "present" and (pres["lease_until"] or 0) > now:
                    owner = {"session": pres["session_id"], "location": pres["location"]}
                events: List[Dict[str, Any]] = []
                qmarks = ",".join("?" * len(avatar.STAGE_KINDS))
                for r in db.execute(
                    f"SELECT event_id, kind, actor_id, location, occurred_at, payload, disclosure_scope "
                    f"FROM events WHERE occurred_at>? AND kind IN ({qmarks}) ORDER BY event_id DESC LIMIT ?",
                    (now - int(hours) * 3600, *avatar.STAGE_KINDS, max(1, min(int(limit), 200))),
                ):
                    scope = r["disclosure_scope"] or ""
                    if r["kind"] == "said" and not (
                        scope.startswith("room:") or "owner" in scope.split(":", 1)[-1].split(",")
                    ):
                        continue
                    pl = json.loads(r["payload"] or "{}")
                    ev: Dict[str, Any] = {
                        "id": r["event_id"],
                        "kind": r["kind"],
                        "who": r["actor_id"] or "owner",
                        "room": r["location"],
                        "at": r["occurred_at"],
                    }
                    if r["kind"] in ("said", "owner_input_received"):
                        ev["text"] = pl.get("text")
                        if pl.get("to"):
                            ev["to"] = pl["to"]
                    elif r["kind"] == "owner_gave":
                        ev.update(to=pl.get("to"), label=pl.get("label"))
                    events.append(ev)
                snacks = inventory.giveable_snacks(db, self.vocab)
                people = {
                    r["actor_id"]: {"room": r["location"], "activity": r["activity"]}
                    for r in db.execute("SELECT actor_id, location, activity FROM actors WHERE simulated=1")
                }
            rec = avatar.receipt(self.world_db, cmd_id) if cmd_id else None
            rooms = [k for k, loc in self.vocab.LOCATIONS.items() if loc.get("kind") == "room"]
            return {
                "ok": True,
                "now": now,
                "owner": owner,
                "events": list(reversed(events)),
                "snacks": snacks,
                "people": people,
                "rooms": rooms,
                "receipt": rec,
            }

    def get_stock_view(self, empty: bool = True) -> Dict[str, Any]:
        with self._lock:
            now = self.clock.now()
            today = dt.datetime.fromtimestamp(now, JST).date()
            if self.needs_onboarding or self.authority is None or not Path(self.life_db).exists():
                return {"ok": True, "now": now, "day": today.isoformat(), "rows": [], "takes": []}
            midnight = dt.datetime.combine(today, dt.time(), JST).timestamp()
            rows = inventory.stock_rows(self.life_db, since=midnight, empty=empty, vocab=self.vocab)
            takes = inventory.stock_takes(self.life_db, since=midnight, limit=60)
            return {"ok": True, "now": now, "day": today.isoformat(), "rows": rows, "takes": takes}

    def avatar_action(
        self,
        op: str,
        location: str = "living",
        text: str = "",
        to: str = "",
        resource_id: str = "",
        session: str = "web_owner",
        cmd_id: str = "",
    ) -> Dict[str, Any]:
        self._require_world()
        cid = str(cmd_id or "").strip()
        if not cid or len(cid) > 64 or not cid.replace("-", "").replace("_", "").isalnum():
            cid = f"web_{uuid.uuid4().hex[:12]}"
        sid = str(session or "").strip()
        if not sid or len(sid) > 64 or not sid.replace("-", "").replace("_", "").isalnum():
            sid = "web_owner"
        payload: Dict[str, Any] = {
            "op": op,
            "cmd_id": cid,
            "session": sid,
        }
        if op in ("enter", "move"):
            payload["location"] = location
        elif op == "say":
            payload["text"] = text
        elif op == "give":
            payload["to"] = to
            payload["resource_id"] = resource_id
        clean = avatar.check(payload, vocab=self.vocab)
        with self._lock:
            now = self.clock.now()
            with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                with c:
                    c.execute(
                        "INSERT OR REPLACE INTO owner_avatar_queue(cmd_id, body, state, receipt, created_at) VALUES(?, ?, 'queued', NULL, ?)",
                        (clean["cmd_id"], json.dumps(clean, ensure_ascii=False, sort_keys=True), now),
                    )
            snap = self.step_once()
            rec = avatar.receipt(self.world_db, clean["cmd_id"]) or {"status": "succeeded"}
        return {
            "ok": rec.get("status", "succeeded") == "succeeded",
            "status": rec.get("status", "succeeded"),
            "reason": rec.get("reason"),
            "cmd_id": clean["cmd_id"],
            "applied": clean,
            "receipt": rec,
            "state": snap,
        }

    def restock_item(self, item: str, qty: int = 4) -> Dict[str, Any]:
        self._require_world()
        if item not in self.vocab.INGREDIENTS:
            raise ValueError(f"未知食材或零食: {item}")
        qty = max(1, min(int(qty), 50))
        label = self.vocab.INGREDIENTS[item].get("label", item)
        return self.mutate_stock("add", {"item": item, "name": label, "qty": qty, "kind": "food"})

    def mutate_stock(self, op: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._require_world()
        if op not in inventory.OWNER_STOCK_OPS:
            raise ValueError(f"不支持的库存操作: {op}")
        key = f"webstock_{uuid.uuid4().hex[:12]}"
        with self._lock:
            qres = inventory.queue_owner_stock(self.world_db, "cmd:owner", op, dict(args or {}), key)
            snap = self.step_once()
            rec = qres
            with contextlib.closing(sqlite3.connect(self.world_db)) as c:
                r = c.execute("SELECT state, receipt FROM owner_stock_queue WHERE source_key=?", (key,)).fetchone()
                if r and r[1]:
                    rec = json.loads(r[1])
        return {
            "ok": rec.get("status") == "succeeded",
            "status": rec.get("status"),
            "reason": rec.get("reason"),
            "queued": qres,
            "receipt": rec,
            "item": (args or {}).get("item") or rec.get("item"),
            "qty": (args or {}).get("qty") if (args or {}).get("qty") is not None else rec.get("qty"),
            "state": snap,
        }

    def trigger_city_outing(self, actor_id: str, destination: str = "ryokogai:kissa-tomoshibi") -> Dict[str, Any]:
        self._require_world()
        assert self.authority is not None
        if actor_id not in self.vocab.ROSTER:
            raise ValueError(f"未知室友: {actor_id}")
        with self._lock:
            if self.authority.city_bridge is not None:
                vis = self.authority.city_bridge.depart(actor_id, destination)
                return {"ok": True, "mode": "city_bridge", "visit": vis, "state": self.get_state()}
            fed = next((e for e in self.authority.life.extensions if e.__class__.__name__ == "CityMcpOutingExtension"), None)
            if fed is not None:
                sess = fed.depart_public_city(actor_id, destination=destination)
                return {"ok": True, "mode": "city_federation_mcp", "session": sess, "state": self.get_state()}
            place_key = destination.split(":")[-1].replace("-", "_")
            if place_key not in self.vocab.PLACES:
                place_key = "kissa_tomoshibi" if "kissa_tomoshibi" in self.vocab.PLACES else next(iter(self.vocab.PLACES))
            res = self.authority.life.submit(actor_id, "move", {"to": "away"}, f"web_out:{actor_id}:{int(self.clock.now())}")
            snap = self.step_once()
            return {"ok": True, "mode": "local_outing", "place": place_key, "kernel_result": res, "state": snap}

    def return_city_outing(self, actor_id: str, force: bool = False, abandon_remote: bool = False) -> Dict[str, Any]:
        """Bring a roommate home from the public city; `force` is the owner's escape hatch for a stuck return
        (`abandon_remote` additionally gives up a city resident whose control cannot be confirmed)."""
        self._require_world()
        assert self.authority is not None
        if actor_id not in self.vocab.ROSTER:
            raise ValueError(f"未知室友: {actor_id}")
        with self._lock:
            fed = next((e for e in self.authority.life.extensions if e.__class__.__name__ == "CityMcpOutingExtension"), None)
            if fed is None:
                raise ValueError("没有启用公共城市远足")
            sess = fed.force_home(actor_id, abandon_remote=abandon_remote) if force else fed.return_home(actor_id)
            # `returned`: a trip record existed and the return was handled. `home`: no trip is open AND the kernel says
            # she is not away (an accepted walk home is not arrival; a roommate out on an ordinary plan is not home).
            home = (not fed.has_record(actor_id)) and fed.k.actor(actor_id)["location"] != "away"
            return {"ok": True, "returned": bool(sess), "forced": bool(force), "home": home,
                    "trip_open": fed.has_record(actor_id), "state": self.get_state()}

    def _ensure_custom_pack_dir(self) -> Path:
        custom_dir = self.runtime_dir / "custom-pack"
        if (custom_dir / "manifest.json").is_file():
            return custom_dir
        docs: List[Dict[str, Any]] = []
        for aid in self.vocab.ROSTER:
            d = copy.deepcopy(dict(self.vocab.PEOPLE.get(aid) or {}))
            d["actor"] = aid
            d.setdefault("name", self.vocab.NAMES.get(aid, aid))
            d.setdefault("accent", "#0F766E")
            d.setdefault("sprite", aid if aid in SPRITE_OPTIONS else "sora")
            docs.append(d)
        write_custom_pack(
            custom_dir,
            docs,
            profile=str(self.vocab.MANIFEST.get("profile") or "custom-house"),
            house=self.vocab.HOUSE,
            money=self.vocab.MONEY,
        )
        return custom_dir

    def _load_custom_people_docs(self, custom_dir: Path) -> List[Dict[str, Any]]:
        manifest = json.loads((custom_dir / "manifest.json").read_text(encoding="utf-8"))
        out: List[Dict[str, Any]] = []
        for aid in manifest.get("people") or []:
            fp = custom_dir / "people" / f"{aid}.json"
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        return out

    def _llm_status_view(self) -> Dict[str, Any]:
        raw_key = (
            self.env.get("AI_HOUSE_API_KEY")
            or self.env.get("OPENAI_API_KEY")
            or self.env.get("WORLD_KERNEL_API_KEY")
            or ""
        ).strip()
        return {
            "mode": "live_api" if self.chat_agent.live_llm_enabled else "zero_key_demo",
            "base_url": (self.env.get("AI_HOUSE_BASE_URL") or self.env.get("OPENAI_BASE_URL") or "https://api.openai.com").strip(),
            "model": (self.env.get("AI_HOUSE_MODEL") or self.env.get("WORLD_KERNEL_MODEL") or "gpt-4o-mini").strip(),
            "api_key_set": bool(raw_key),
            "api_key_masked": mask_secret(raw_key),
        }

    def _apply_llm_config(self, spec: Dict[str, Any], *, persist: bool = True) -> Dict[str, Any]:
        base_url = str(
            spec.get("base_url")
            or self.env.get("AI_HOUSE_BASE_URL")
            or self.env.get("OPENAI_BASE_URL")
            or "https://api.openai.com"
        ).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        model = str(
            spec.get("model")
            or self.env.get("AI_HOUSE_MODEL")
            or self.env.get("WORLD_KERNEL_MODEL")
            or "gpt-4o-mini"
        ).strip()
        if not model:
            raise ValueError("模型名称不能为空")
        clear_key = spec.get("clear_key") is True
        raw_key = str(spec.get("api_key") or "").strip()
        if clear_key:
            new_key = ""
        elif raw_key:
            new_key = raw_key
        else:
            new_key = (self.env.get("AI_HOUSE_API_KEY") or self.env.get("OPENAI_API_KEY") or "").strip()

        self.env["AI_HOUSE_BASE_URL"] = base_url
        self.env["AI_HOUSE_MODEL"] = model
        self.env["AI_HOUSE_API_KEY"] = new_key
        if clear_key:
            self.env.pop("OPENAI_API_KEY", None)
            self.env.pop("WORLD_KERNEL_API_KEY", None)
        if persist and self.env_file is not None:
            upsert_dotenv_vars(
                self.env_file,
                {
                    "AI_HOUSE_API_KEY": new_key,
                    "AI_HOUSE_BASE_URL": base_url,
                    "AI_HOUSE_MODEL": model,
                },
            )
        self._rebuild_llm_and_guard()
        return self._llm_status_view()

    def get_setup_info(self) -> Dict[str, Any]:
        with self._lock:
            residents = []
            if not self.needs_onboarding:
                for aid in self.vocab.ROSTER:
                    pmeta = self.vocab.PEOPLE.get(aid) or {}
                    residents.append({
                        "actor": aid,
                        "name": self.vocab.NAMES.get(aid, aid),
                        "bio": pmeta.get("bio", ""),
                        "persona": pmeta.get("persona", ""),
                        "accent": pmeta.get("accent", "#0F766E"),
                        "sprite": pmeta.get("sprite") or (aid if aid in SPRITE_OPTIONS else "sora"),
                        "schedule_preset": pmeta.get("schedule_preset", "regular"),
                        "cook_preset": pmeta.get("cook_preset", "helper"),
                    })
            return {
                "ok": True,
                "needs_onboarding": bool(self.needs_onboarding),
                "pack_profile": "unconfigured" if self.needs_onboarding else self.vocab.MANIFEST.get("profile", "life1"),
                "residents": residents,
                "llm": self._llm_status_view(),
                "sprites": sprite_choices(),
                "accents": list(ACCENT_OPTIONS),
                "schedule_presets": [{"id": k, "label": v["label"]} for k, v in SCHEDULE_PRESETS.items()],
                "cook_presets": [{"id": k, "label": v["label"]} for k, v in COOK_PRESETS.items()],
                "presets": [
                    {"id": "indie-studio-3", "label": "三人独立游戏开发组示例（苍空 / 凛音 / 黑羽）", "count": 3},
                    {"id": "life1", "label": "六人经典合租屋示例（奈尔等 6 人）", "count": 6},
                ],
            }

    def update_llm_settings(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            llm_info = self._apply_llm_config(dict(spec or {}), persist=True)
            snap = self.get_state()
        self._broadcast("state", {"now": snap["now_jst"], "llm_mode": snap["llm_mode"]})
        return {"ok": True, "llm": llm_info, "state": snap}

    def test_llm_settings(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(spec or {})
        base_url = str(
            data.get("base_url")
            or self.env.get("AI_HOUSE_BASE_URL")
            or self.env.get("OPENAI_BASE_URL")
            or "https://api.openai.com"
        ).strip().rstrip("/")
        model = str(data.get("model") or self.env.get("AI_HOUSE_MODEL") or "gpt-4o-mini").strip()
        api_key = str(data.get("api_key") or "").strip()
        if not api_key and not data.get("clear_key"):
            api_key = (self.env.get("AI_HOUSE_API_KEY") or self.env.get("OPENAI_API_KEY") or "").strip()
        if not base_url.startswith(("http://", "https://")):
            return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}
        if not model:
            return {"ok": False, "error": "请填写模型名称"}
        if not api_key:
            return {"ok": False, "error": "请先填写 API Key"}
        try:
            ans, toks = openai_compatible_chat(
                "Reply OK.",
                "ping",
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_s=12.0,
            )
            return {"ok": bool(ans), "model": model, "reply": ans[:80], "tokens": toks}
        except LLMAPIError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key)}

    def complete_setup(self, body: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(body or {})
        with self._async_job_lock, self._lock:
            if not self.needs_onboarding:
                raise ValueError("小屋已完成初始化；如需重新开始请先执行「重置小屋」")
            mode = str(data.get("mode") or "first_bot").strip()
            if mode not in ("first_bot", "preset"):
                raise ValueError(f"不支持的初始化模式：{mode}")

            llm_spec = data.get("llm")
            if isinstance(llm_spec, dict):
                llm_mode = str(llm_spec.get("mode") or "").strip()
                if llm_mode == "api" or str(llm_spec.get("api_key") or "").strip():
                    self._apply_llm_config(llm_spec, persist=True)
                elif llm_mode == "demo" and llm_spec.get("clear_key"):
                    self._apply_llm_config({"clear_key": True, "base_url": llm_spec.get("base_url"), "model": llm_spec.get("model")}, persist=True)

            custom_dir = self.runtime_dir / "custom-pack"
            if custom_dir.exists():
                shutil.rmtree(custom_dir, ignore_errors=True)

            if mode == "preset":
                preset = str(data.get("preset") or "indie-studio-3").strip()
                copy_preset_to_custom_pack(preset, custom_dir)
                setup_meta = {"mode": "preset", "preset": preset, "configured_at": self.clock.now()}
            else:
                res_spec = dict(data.get("resident") if isinstance(data.get("resident"), dict) else data)
                actor = suggest_actor_id(str(res_spec.get("actor") or ""), existing=())
                doc = build_person_doc({**res_spec, "actor": actor})
                write_custom_pack(custom_dir, [doc], profile="custom-house")
                setup_meta = {"mode": "first_bot", "first_actor": actor, "configured_at": self.clock.now()}

            (self.runtime_dir / "house_setup.json").write_text(
                json.dumps(setup_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._mount_world(custom_dir)
            snap = self.get_state()
            setup_doc = self.get_setup_info()

        self._broadcast("state", {"now": snap["now_jst"], "needs_onboarding": False})
        return {"ok": True, "needs_onboarding": False, "state": snap, "setup": setup_doc}

    def add_resident(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(spec or {})
        with self._async_job_lock, self._lock:
            self._require_world()
            if len(self.vocab.ROSTER) >= 12:
                raise ValueError("目前每间小屋最多支持 12 位住客")
            custom_dir = self._ensure_custom_pack_dir()
            docs = self._load_custom_people_docs(custom_dir)
            actor = suggest_actor_id(str(data.get("actor") or ""), existing=[d["actor"] for d in docs])
            new_doc = build_person_doc({**data, "actor": actor})
            manifest = json.loads((custom_dir / "manifest.json").read_text(encoding="utf-8"))
            profile = str(manifest.get("profile") or "custom-house")
            write_custom_pack(
                custom_dir,
                [*docs, new_doc],
                profile=profile,
                house=self.vocab.HOUSE,
                money=self.vocab.MONEY,
            )
            self._mount_world(custom_dir)
            snap = self.get_state()
            setup_doc = self.get_setup_info()

        self._broadcast("state", {"now": snap["now_jst"], "added_actor": actor})
        return {"ok": True, "actor": actor, "state": snap, "setup": setup_doc}

    def update_resident(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(spec or {})
        with self._async_job_lock, self._lock:
            self._require_world()
            actor = str(data.get("actor") or "").strip().lower()
            if not actor or actor not in self.vocab.ROSTER:
                raise ValueError(f"未知住客：{actor}")
            custom_dir = self._ensure_custom_pack_dir()
            docs = self._load_custom_people_docs(custom_dir)
            updated_docs = [
                build_person_doc(data, existing_doc=d) if d.get("actor") == actor else d
                for d in docs
            ]
            manifest = json.loads((custom_dir / "manifest.json").read_text(encoding="utf-8"))
            profile = str(manifest.get("profile") or "custom-house")
            write_custom_pack(
                custom_dir,
                updated_docs,
                profile=profile,
                house=self.vocab.HOUSE,
                money=self.vocab.MONEY,
            )
            self._mount_world(custom_dir)
            snap = self.get_state()
            setup_doc = self.get_setup_info()

        self._broadcast("state", {"now": snap["now_jst"], "updated_actor": actor})
        return {"ok": True, "actor": actor, "state": snap, "setup": setup_doc}

    def reset_house(self) -> Dict[str, Any]:
        with self._async_job_lock, self._lock:
            self._teardown_world()
            targets = [
                Path(self.life_db),
                Path(self.life_db + "-wal"),
                Path(self.life_db + "-shm"),
                Path(self.world_db),
                Path(self.world_db + "-wal"),
                Path(self.world_db + "-shm"),
                self.runtime_dir / "memory.db",
                self.runtime_dir / "memory.db-wal",
                self.runtime_dir / "memory.db-shm",
                self.runtime_dir / "clock_offset.json",
                self.runtime_dir / "house_setup.json",
            ]
            wait_ev = threading.Event()
            for fp in targets:
                for attempt in range(4):
                    if not fp.exists():
                        break
                    try:
                        fp.unlink()
                        break
                    except PermissionError:
                        if attempt == 3:
                            raise
                        gc.collect()
                        wait_ev.wait(0.05)

            if hasattr(self.clock, "reset"):
                self.clock.reset()

            custom_dir = self.runtime_dir / "custom-pack"
            if custom_dir.exists():
                shutil.rmtree(custom_dir, ignore_errors=True)

            self.needs_onboarding = True
            self.vocab = vocab_life1.load_pack()
            self._rebuild_llm_and_guard()
            snap = self.get_state()

        self._broadcast("state", {"now": snap["now_jst"], "needs_onboarding": True})
        return {"ok": True, "needs_onboarding": True, "state": snap}

    def handle_mcp_rpc(self, body: Dict[str, Any]) -> Dict[str, Any]:
        req_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ai-house-kit", "version": "0.2.0"},
                },
            }
        if method == "notifications/initialized":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "get_house_state",
                            "description": "获取整栋 AI 小屋此刻的房间分布、天气、厨房库存与每晚总结。",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                        {
                            "name": "get_resident_card",
                            "description": "读取指定室友此刻的状态卡（位置、动作、当前穿搭、三餐记录、心情与后续计划）。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"actor_id": {"type": "string"}},
                                "required": ["actor_id"],
                            },
                        },
                        {
                            "name": "chat_with_resident",
                            "description": "向小屋群聊或指定室友发送消息并写入约定队列。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "channel": {"type": "string", "description": "'group' 或 'private:<actor_id>'"},
                                    "text": {"type": "string"},
                                },
                                "required": ["channel", "text"],
                            },
                        },
                        {
                            "name": "owner_avatar_act",
                            "description": "控制房主化身走进房间、说话或递零食（enter/move/say/give/leave）。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "op": {"type": "string"},
                                    "location": {"type": "string"},
                                    "text": {"type": "string"},
                                    "to": {"type": "string"},
                                    "resource_id": {"type": "string"},
                                },
                                "required": ["op"],
                            },
                        },
                    ]
                },
            }
        if method == "tools/call":
            tname = params.get("name")
            args = params.get("arguments") or {}
            if tname == "get_house_state":
                data = self.get_state()
            elif tname == "get_resident_card":
                aid = str(args.get("actor_id") or "")
                st = self.get_state()
                data = st["residents"].get(aid) or {"error": f"Unknown actor_id: {aid}"}
            elif tname == "chat_with_resident":
                data = self.send_chat(str(args.get("channel") or "group"), str(args.get("text") or ""))
            elif tname == "owner_avatar_act":
                data = self.avatar_action(
                    op=str(args.get("op") or "enter"),
                    location=str(args.get("location") or "living"),
                    text=str(args.get("text") or ""),
                    to=str(args.get("to") or ""),
                    resource_id=str(args.get("resource_id") or ""),
                )
            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {tname}"}}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                    "structuredContent": data,
                },
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unsupported method: {method}"}}

    def _broadcast(self, event: str, payload: Dict[str, Any]) -> None:
        msg = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for q in list(self._sse_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def make_http_server(daemon: HouseDaemon, host: str = "127.0.0.1", port: int = 8780) -> ThreadingHTTPServer:
    check_bind(host, daemon.env)
    policy = AuthPolicy(daemon.env)
    web_root = Path(__file__).with_name("web").resolve()
    index_html = (web_root / "index.html").read_bytes()
    chat_html = (web_root / "chat.html").read_bytes()
    static_mimes = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    max_body = 1_000_000

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, doc: Any, status: int = 200) -> None:
            raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > max_body:
                raise ValueError("request body too large")
            raw = self.rfile.read(n).decode("utf-8", errors="replace") if n > 0 else "{}"
            return json.loads(raw or "{}")

        def _token(self, parsed: Any) -> str:
            auth = self.headers.get("Authorization") or ""
            if auth[:7].lower() == "bearer ":
                return auth[7:].strip()
            # Never from the URL: proxies and access logs keep URLs, and this token can do everything.
            return (self.headers.get("X-AI-House-Token") or "").strip()

        def _deny(self, status: int, code: str) -> None:
            raw = json.dumps({"ok": False, "error": code}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(raw)

        def _pre(self, is_post: bool) -> bool:
            """Without any token the daemon is loopback-only; reject rebinding Host and cross-site POSTs."""
            if policy.enabled:
                return True
            if not host_is_loopback(self.headers.get("Host") or ""):
                self._deny(403, "bad_host")
                return False
            if is_post and not origin_is_loopback(self.headers.get("Origin") or ""):
                self._deny(403, "bad_origin")
                return False
            return True

        def _need(self, parsed: Any, tier: str) -> bool:
            tok = self._token(parsed)
            if not policy.authenticated(tok):
                self._deny(401, "unauthorized")
                return False
            allowed = policy.is_owner(tok) if tier == "owner" else policy.allows(tok, tier)
            if not allowed:
                self._deny(403, "forbidden_tier:" + tier)
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if not self._pre(False):
                return
            if parsed.path in ("/", "/index.html", "/chat", "/chat.html"):
                page = chat_html if parsed.path.startswith("/chat") else index_html
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
                return
            if parsed.path.startswith("/web/"):
                rel = unquote(parsed.path[len("/web/"):])
                if not rel or "\\" in rel or "\x00" in rel:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                try:
                    cand = (web_root / rel).resolve()
                except OSError:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                if not cand.is_relative_to(web_root) or not cand.is_file():
                    self._send_json({"error": "not_found"}, status=404)
                    return
                ctype = static_mimes.get(cand.suffix.lower())
                if not ctype:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                raw = cand.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == "/api/health":
                self._send_json({
                    "ok": True,
                    "service": "ai-house",
                    "version": __version__,
                    "needs_onboarding": bool(daemon.needs_onboarding),
                })
                return
            if parsed.path == "/api/chat/roster":
                # Names only (no state, no messages): any valid token may ask, so a private-tier token can pick a roommate.
                if not policy.authenticated(self._token(parsed)):
                    self._deny(401, "unauthorized")
                    return
                residents_doc = [] if daemon.needs_onboarding else [
                    {"actor": a, "name": daemon.vocab.NAMES.get(a, a)} for a in daemon.vocab.ROSTER
                ]
                self._send_json({"residents": residents_doc})
                return
            if parsed.path == "/api/auth":
                tok = self._token(parsed)
                if not policy.authenticated(tok):
                    self._deny(401, "unauthorized")
                    return
                self._send_json({
                    "ok": True,
                    "auth_enabled": policy.enabled,
                    "needs_onboarding": daemon.needs_onboarding,
                    "tiers": {
                        **{t: policy.allows(tok, t) for t in ("read", "write", "private")},
                        "owner": policy.is_owner(tok),
                    },
                })
                return
            if parsed.path == "/api/setup":
                if not self._need(parsed, "owner"):
                    return
                self._send_json(daemon.get_setup_info())
                return
            if parsed.path == "/api/state":
                if not self._need(parsed, "read"):
                    return
                self._send_json(daemon.get_state())
                return
            if parsed.path == "/api/stock":
                if not self._need(parsed, "read"):
                    return
                self._send_json(daemon.get_stock_view(empty=True))
                return
            if parsed.path == "/api/wardrobe":
                if not self._need(parsed, "read"):
                    return
                qs = parse_qs(parsed.query)
                act = (qs.get("actor") or [daemon.vocab.ROSTER[0]])[0]
                raw_days = (qs.get("days") or ["30"])[0]
                raw_before = (qs.get("log_before") or [""])[0]
                try:
                    days: Any = "all" if raw_days == "all" else int(raw_days)
                    log_before = None
                    if raw_before and "," in raw_before:
                        p0, p1 = raw_before.split(",", 1)
                        log_before = (float(p0), int(p1))
                    self._send_json(daemon.get_wardrobe(act, days=days, log_before=log_before))
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            if parsed.path == "/api/house/stage":
                if not self._need(parsed, "read"):
                    return
                qs = parse_qs(parsed.query)
                cmd = (qs.get("cmd") or [None])[0]
                self._send_json(daemon.get_house_stage(cmd_id=cmd))
                return
            if parsed.path == "/api/ingredients":
                if not self._need(parsed, "read"):
                    return
                items = [
                    {"id": k, "label": v["label"], "kind": v["kind"], "price_yen": v["price_yen"]}
                    for k, v in daemon.vocab.INGREDIENTS.items()
                ]
                self._send_json({"items": items})
                return
            if parsed.path == "/api/chat":
                qs = parse_qs(parsed.query)
                ch = (qs.get("channel") or ["group"])[0]
                if not self._need(parsed, tier_for_channel(ch, write=False)):
                    return
                self._send_json({"channel": ch, "messages": daemon.list_chat(ch)})
                return
            if parsed.path == "/api/memories":
                qs = parse_qs(parsed.query)
                sc = (qs.get("scope") or ["world"])[0]
                act = (qs.get("actor") or [None])[0]
                ch = (qs.get("channel") or [""])[0]
                kind = (qs.get("kind") or [None])[0]
                lim = int((qs.get("limit") or ["50"])[0])
                need_tier = "private" if sc == "private" else "read"
                if not self._need(parsed, need_tier):
                    return
                try:
                    items = daemon.list_memories(actor=act, scope=sc, channel=ch, kind=kind, limit=lim)
                    self._send_json({"ok": True, "scope": sc, "memories": items})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            if parsed.path == "/api/diaries":
                if not self._need(parsed, "read"):
                    return
                if daemon.needs_onboarding or daemon.diary is None:
                    self._send_json({"ok": True, "diaries": []})
                    return
                qs = parse_qs(parsed.query)
                act = (qs.get("actor") or [None])[0]
                lim = int((qs.get("limit") or ["20"])[0])
                self._send_json({"ok": True, "diaries": daemon.diary.list_diaries(actor=act, limit=lim)})
                return
            if parsed.path == "/api/intents":
                if not self._need(parsed, "private"):
                    return
                if daemon.needs_onboarding or daemon.intents is None:
                    self._send_json({"ok": True, "intents": []})
                    return
                qs = parse_qs(parsed.query)
                act = (qs.get("actor") or [None])[0]
                lim = int((qs.get("limit") or ["30"])[0])
                self._send_json({"ok": True, "intents": daemon.intents.list_intents(actor=act, limit=lim)})
                return
            if parsed.path == "/api/events":
                if not self._need(parsed, "read"):
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q: queue.Queue[str] = queue.Queue(maxsize=32)
                daemon._sse_clients.append(q)
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while not daemon._stop.is_set():
                        try:
                            chunk = q.get(timeout=15.0)
                            self.wfile.write(chunk.encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    if q in daemon._sse_clients:
                        daemon._sse_clients.remove(q)
                return
            self._send_json({"error": "not_found"}, status=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not self._pre(True):
                return
            # 先确认令牌有效，再读请求体；具体档位要看请求内容，读完后再判。
            if not policy.authenticated(self._token(parsed)):
                self._deny(401, "unauthorized")
                return
            try:
                body = self._read_json()
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            if parsed.path in (
                "/api/setup",
                "/api/setup/test-llm",
                "/api/settings/llm",
                "/api/residents",
                "/api/residents/update",
                "/api/setup/reset",
            ):
                tier = "owner"
            elif parsed.path in ("/api/step", "/api/avatar", "/api/house/avatar", "/api/stock", "/api/city/depart", "/api/city/return"):
                tier = "write"
            elif parsed.path == "/api/memories/forget" and isinstance(body, dict):
                sc = str(body.get("scope") or "private")
                tier = "private" if sc == "private" else "write"
            elif parsed.path == "/api/chat" and isinstance(body, dict):
                tier = tier_for_channel(str(body.get("channel") or "group"), write=True)
            elif parsed.path == "/mcp":
                tier = mcp_tier(body) or ""
                if not tier:
                    self._send_json({"ok": False, "error": "invalid_mcp_request"}, status=400)
                    return
            else:
                tier = ""
            if not tier:
                if parsed.path in ("/api/chat", "/mcp"):
                    self._send_json({"ok": False, "error": "invalid_request"}, status=400)
                else:
                    self._send_json({"error": "not_found"}, status=404)
                return
            if not self._need(parsed, tier):
                return
            try:
                if parsed.path == "/api/setup":
                    self._send_json(daemon.complete_setup(body if isinstance(body, dict) else {}))
                    return
                if parsed.path == "/api/setup/test-llm":
                    self._send_json(daemon.test_llm_settings(body if isinstance(body, dict) else {}))
                    return
                if parsed.path == "/api/settings/llm":
                    self._send_json(daemon.update_llm_settings(body if isinstance(body, dict) else {}))
                    return
                if parsed.path == "/api/residents":
                    self._send_json(daemon.add_resident(body if isinstance(body, dict) else {}))
                    return
                if parsed.path == "/api/residents/update":
                    self._send_json(daemon.update_resident(body if isinstance(body, dict) else {}))
                    return
                if parsed.path == "/api/setup/reset":
                    self._send_json(daemon.reset_house())
                    return
                if parsed.path == "/api/step":
                    mins = int(body.get("minutes") or 0)
                    res = daemon.fast_forward(mins) if mins > 0 else daemon.step_once()
                    self._send_json({"ok": True, "state": res})
                    return
                if parsed.path == "/api/chat":
                    res = daemon.send_chat(
                        channel=str(body.get("channel") or "group"),
                        text=str(body.get("text") or ""),
                        sender_name=str(body.get("sender_name") or "房主"),
                    )
                    self._send_json(res)
                    return
                if parsed.path in ("/api/avatar", "/api/house/avatar"):
                    res = daemon.avatar_action(
                        op=str(body.get("op") or "enter"),
                        location=str(body.get("location") or "living"),
                        text=str(body.get("text") or ""),
                        to=str(body.get("to") or ""),
                        resource_id=str(body.get("resource_id") or ""),
                        session=str(body.get("session") or "web_owner"),
                        cmd_id=str(body.get("cmd_id") or ""),
                    )
                    self._send_json(res)
                    return
                if parsed.path == "/api/stock":
                    if isinstance(body, dict) and body.get("op"):
                        raw_args = body.get("args") if isinstance(body.get("args"), dict) else {k: v for k, v in body.items() if k != "op"}
                        if body.get("op") == "add" and raw_args.get("item") and not raw_args.get("name"):
                            meta = daemon.vocab.INGREDIENTS.get(str(raw_args["item"])) or {}
                            raw_args = {**raw_args, "name": meta.get("label", str(raw_args["item"]))}
                        res = daemon.mutate_stock(str(body.get("op")), dict(raw_args))
                    else:
                        res = daemon.restock_item(
                            item=str(body.get("item") or "egg"),
                            qty=int(body.get("qty") or 4),
                        )
                    self._send_json(res)
                    return
                if parsed.path == "/api/city/depart":
                    res = daemon.trigger_city_outing(
                        actor_id=str(body.get("actor_id") or daemon.vocab.ROSTER[0]),
                        destination=str(body.get("destination") or "ryokogai:kissa-tomoshibi"),
                    )
                    self._send_json(res)
                    return
                if parsed.path == "/mcp":
                    self._send_json(daemon.handle_mcp_rpc(body))
                    return
                if parsed.path == "/api/city/return":
                    res = daemon.return_city_outing(
                        actor_id=str(body.get("actor_id") or ""),
                        force=body.get("force") is True,
                        abandon_remote=body.get("abandon_remote") is True,
                    )
                    self._send_json(res)
                    return
                if parsed.path == "/api/memories/forget":
                    res = daemon.forget_memory(
                        actor=str(body.get("actor") or ""),
                        scope=str(body.get("scope") or "private"),
                        mem_id=body.get("id"),
                        channel=str(body.get("channel") or ""),
                        reason=str(body.get("reason") or "owner_forgot"),
                    )
                    self._send_json({"ok": True, "memory": res})
                    return
                self._send_json({"error": "not_found"}, status=404)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)

    class _HouseHTTPServer(ThreadingHTTPServer):
        # POSIX needs SO_REUSEADDR to rebind past TIME_WAIT; on WinSock SO_REUSEADDR allows
        # two active listeners to double-bind the same port silently instead of raising 10048.
        allow_reuse_address = os.name != "nt"

    return _HouseHTTPServer((host, int(port)), Handler)
