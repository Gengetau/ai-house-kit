"""`CommonMemoryStore`: the `MemoryStore` interface on top of the shared memory module (`ai_house.common`).

Not wired into the daemon yet. The shared module speaks in "consumer + chat + rendered QQ lines" and keeps its
settings in module globals; the kit speaks in "actor + scope + channel + plain text" and has one store per house.
This class is the translation, and nothing else: every memory decision (what is stored, who may see it, what a
recall says) is made by the loaded copy of the shared module.

Own database `<data_dir>/memory-common.db`; it never touches the old `memory.db`. Besides the shared module's own
tables it keeps three small tables of its own: `kit_numbers` (the stable placeholder number of each housemate),
`kit_channels` (kit channel ids that have no number of their own) and `kit_origin` (which rows were written in the
kit's `world` scope).

Accepted for compatibility but without effect: `recency_half_life_days`, `min_keyword_overlap`; `recall(about=,
max_chars=)`. `add(trust=)` is kept only with `AI_HOUSE_MEMORY_TRUST=1` (the shared module's trust levels, off by
default); `add(expires_at=, ttl_s=)` gives the memory a time after which it is gone.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import threading
import time as _real_time
import zlib
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from .common import loader, roster as _roster
from .memory import (
    DEFAULT_RECALL_MAX_CHARS,
    KINDS,
    LEAK_FREE_GROUPS_ENV,
    PRIVATE_IN_GROUPS_ENV,
    MemoryRefusedError,
    MemoryStore,
    NarrowMemoryAPI,
    _clean_line,
    _clean_speaker,
    _clean_text,
    parse_leak_free_groups,
)

log = logging.getLogger("ai_house.memory_common")

OWNER_NUMBER = 1  # the owner, inside the database only
OWNER_PRIVATE_CHAT = f"private:{OWNER_NUMBER}"
WEB_GROUP_CHAT = "group:1"  # the kit's web group channel `group`
WORLD_LEAK_CHAT = "group:0"  # world-scope outbound checks: a group nobody can list as leak-free
HOUSEMATE_NUMBERS = (1000, 8999)
STRANGER_NUMBERS = (9000, 9999)
_REAL_QQ_MIN = 10000  # below this a number is ours: a claimed `qq:` id in that range is not believed
_CHANNEL_BASE = 10 ** 15  # chats made for channel ids that have no number of their own
_QQ_GROUP_RE = re.compile(r"qq-group:(\d{1,20})")
_QQ_ID_RE = re.compile(r"(?:qq|visitor:(?:qq|onebot|onebot_v11)):(\d{1,15})")
_RESIDENT_ID_RE = re.compile(r"resident:([A-Za-z0-9_-]{1,32})")
_HEADER_BAD_RE = re.compile(r"[\[\]|\r\n]")
_KIT_COLUMNS = ("id", "owner", "scope", "channel", "about", "kind", "trust", "text", "source", "status", "created",
                "updated", "last_used", "uses", "expires_at")

# Environment names of the shared module that a host may happen to have set. They are hidden while a copy is
# loaded, so the copy starts from the code defaults and only the kit's own settings below apply.
_HOST_ENV_PREFIXES = ("MEMORY_",)
_HOST_ENV_NAMES = ("CPA_BASE", "CPA_CONFIG")
_ENV_LOCK = threading.Lock()


def _load_clean(roster: Any) -> Any:
    with _ENV_LOCK:
        hidden = {k: os.environ.pop(k) for k in list(os.environ)
                  if k.startswith(_HOST_ENV_PREFIXES) or k in _HOST_ENV_NAMES}
        try:
            return loader.load(roster=roster)
        finally:
            os.environ.update(hidden)


class _KitClock:
    """Stands in for the `time` module inside a copy: `time()` is the call's own `now`, else the house clock, else
    the real time; `localtime` is always Japan time so no result depends on the host's zone."""

    JST_OFFSET_S = 9 * 3600

    def __init__(self, ambient: Callable[[], float]):
        self._ambient = ambient
        self._tl = threading.local()

    def time(self) -> float:
        now = getattr(self._tl, "now", None)
        return float(now) if now is not None else float(self._ambient())

    def localtime(self, ts: Optional[float] = None) -> _real_time.struct_time:
        return _real_time.gmtime((self.time() if ts is None else float(ts)) + self.JST_OFFSET_S)

    @contextlib.contextmanager
    def at(self, now: Optional[float]) -> Iterator[None]:
        prev = getattr(self._tl, "now", None)
        if now is not None:
            self._tl.now = float(now)
        try:
            yield
        finally:
            self._tl.now = prev

    def __getattr__(self, name: str) -> Any:
        return getattr(_real_time, name)


def _csv(raw: Any, everyone: Iterable[str]) -> set:
    text = str(raw or "").strip()
    if text.lower() in ("all", "*"):
        return set(everyone)
    return {x.strip() for x in text.split(",") if x.strip()}


def _flag(raw: Any, default: bool) -> bool:
    return default if raw is None or str(raw).strip() == "" else str(raw).strip() == "1"


class CommonMemoryStore:
    """Same calls as `ai_house.memory.MemoryStore`, answered by a private copy of the shared memory module."""

    # Reused from the old store so the rules cannot drift apart: validation, the two switches, the model and vector
    # plug-ins. They only read plain attributes set in __init__.
    _valid_actor = MemoryStore.__dict__["_valid_actor"]
    _valid_scope = MemoryStore.__dict__["_valid_scope"]
    _clean_channel = MemoryStore.__dict__["_clean_channel"]
    _resolve_channel = MemoryStore.__dict__["_resolve_channel"]
    private_readable_in = MemoryStore.private_readable_in
    leak_free_in = MemoryStore.leak_free_in
    _init_embedder = MemoryStore._init_embedder
    _embed = MemoryStore._embed
    _build_extractor_fn = MemoryStore._build_extractor_fn
    vector_enabled = MemoryStore.vector_enabled

    def __init__(
        self,
        data_dir: Union[str, Path],
        *,
        clock: Any = None,
        llm_fn: Optional[Callable[[str, str], Any]] = None,
        model_router: Any = None,
        embed_plugin: Any = None,
        names: Optional[Mapping[str, str]] = None,
        env: Optional[Mapping[str, str]] = None,
        unified_groups: bool = False,
        private_in_groups: bool = False,
        leak_free_groups: Iterable[str] = (),
        recency_half_life_days: Optional[float] = None,
        recall_k: Optional[int] = None,
        min_score: Optional[float] = None,
        min_keyword_overlap: Optional[float] = None,
        extract_every_s: Optional[int] = None,
        extract_batch: Optional[int] = None,
        max_active: Optional[int] = None,
    ):
        # the shared module's own variable names (and the old store's fallback to `MEMORY_EMBED_URL`) are not read
        self._env = {k: v for k, v in dict(os.environ if env is None else env).items()
                     if not k.startswith(_HOST_ENV_PREFIXES) and k not in _HOST_ENV_NAMES}
        base = Path(data_dir)
        if base.suffix.lower() in (".db", ".sqlite", ".sqlite3") and not base.is_dir():
            base = base.parent  # a path to the old database is never opened: this store has its own file
        self.db_path = base / "memory-common.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._llm_fn = llm_fn
        self.model_router = model_router
        self.unified_groups = bool(unified_groups or self._env.get("AI_HOUSE_MEMORY_UNIFIED_GROUPS") == "1")
        self.private_in_groups = bool(private_in_groups or self._env.get(PRIVATE_IN_GROUPS_ENV) == "1")
        groups_ok, groups_bad = parse_leak_free_groups(
            ",".join([
                str(self._env.get(LEAK_FREE_GROUPS_ENV) or ""),
                *([leak_free_groups] if isinstance(leak_free_groups, str) else [str(x) for x in leak_free_groups]),
            ])
        )
        if groups_bad:
            log.warning("%s: ignoring invalid entries (%d)", LEAK_FREE_GROUPS_ENV, len(groups_bad))
        self.leak_free_groups: frozenset = frozenset(groups_ok)
        self.recency_half_life_days = recency_half_life_days
        self.min_keyword_overlap = min_keyword_overlap
        self._numeric = self._read_numeric(recall_k, min_score, extract_every_s, extract_batch, max_active)
        self._embedder = self._init_embedder(embed_plugin)

        self._cfg_lock = threading.RLock()
        self._extract_lock = threading.RLock()
        self._k_lock = threading.Lock()
        self._msg_lock = threading.Lock()
        self._tl = threading.local()
        self._names: Dict[str, str] = dict(names or {})
        self._residents: set = set(self._names)
        self._numbers_by_key: Dict[str, int] = {}
        self._mod: Any = None
        self._msg_seq = 0
        self._closed = False
        self._ensure_adapter_tables()
        self._rebuild()
        with self._adapter_conn() as c:
            self._msg_seq = int(c.execute("select coalesce(max(id), 0) from seen").fetchone()[0])

    # ---- settings ----------------------------------------------------------------------------------------------

    def _read_numeric(self, recall_k, min_score, every_s, batch, max_active) -> Dict[str, Any]:
        """Module constants that have a kit counterpart: env, else the constructor argument, else the shared
        module's own default (left out here). The kit's old defaults are not carried over."""
        e = self._env

        def pick(env_name: str, arg: Any, cast: Callable[[Any], Any], low: Any) -> Any:
            raw = e.get(env_name)
            if raw is None or str(raw).strip() == "":
                raw = arg
            return None if raw is None else max(low, cast(raw))

        out = {
            "RECALL_K": pick("AI_HOUSE_MEMORY_RECALL_K", recall_k, int, 1),
            "RECALL_MIN": pick("AI_HOUSE_MEMORY_MIN_SCORE", min_score, float, float("-inf")),
            "EXTRACT_EVERY_S": pick("AI_HOUSE_MEMORY_EXTRACT_EVERY_S", every_s, int, 1),
            "EXTRACT_BATCH": pick("AI_HOUSE_MEMORY_EXTRACT_BATCH", batch, int, 1),
            "EXTRACT_PER_HOUR": pick("AI_HOUSE_MEMORY_EXTRACT_PER_HOUR", None, int, 1),
            "MAX_ACTIVE": pick("AI_HOUSE_MEMORY_MAX_ACTIVE", max_active, int, 10),
        }
        return {k: v for k, v in out.items() if v is not None}

    def _switches(self, residents: Sequence[str]) -> Dict[str, Any]:
        """The shared module's other switches: its code defaults, unless an `AI_HOUSE_` + original name variable
        says otherwise. Same parsing and clamps as the module's own."""
        e = self._env
        get = lambda name: e.get("AI_HOUSE_" + name)  # noqa: E731
        out: Dict[str, Any] = {}

        def put(attr: str, env_name: str, cast: Callable[[Any], Any]) -> None:
            raw = get(env_name)
            if raw is not None and str(raw).strip() != "":
                out[attr] = cast(raw)

        put("RULE_CONSUMERS", "MEMORY_RULES", lambda v: _csv(v, residents))
        put("PUBLIC_SCRIBES", "MEMORY_PUBLIC_SCRIBES", lambda v: _csv(v, residents))
        put("RECENT", "MEMORY_RECENT", lambda v: _csv(v, residents))
        put("RECENT_SHADOW", "MEMORY_RECENT_SHADOW", lambda v: str(v).strip() == "1")
        put("RECENT_WINDOW_S", "MEMORY_RECENT_WINDOW_S", lambda v: max(0, int(v)))
        put("RECENT_K", "MEMORY_RECENT_K", lambda v: max(0, min(40, int(v))))
        put("URGENT", "MEMORY_URGENT", lambda v: str(v).strip() == "1")
        put("URGENT_GAP_S", "MEMORY_URGENT_GAP_S", lambda v: max(0, int(v)))
        put("EVIDENCE", "MEMORY_EVIDENCE", lambda v: str(v).strip() == "1")
        put("EVIDENCE_FOR", "MEMORY_EVIDENCE_FOR", lambda v: {x.strip() for x in str(v).split(",") if x.strip()})
        put("EVIDENCE_CHARS", "MEMORY_EVIDENCE_CHARS", lambda v: max(0, int(v)))
        put("EVIDENCE_RAW", "MEMORY_EVIDENCE_RAW", lambda v: max(0, min(4, int(v))))
        put("EVIDENCE_MIN_AGE_S", "MEMORY_EVIDENCE_MIN_AGE_S", lambda v: max(0, int(v)))
        put("EVIDENCE_VER_CHARS", "MEMORY_EVIDENCE_VERSION_CHARS", lambda v: max(0, int(v)))
        put("DIARY_PRIVATE", "MEMORY_DIARY_PRIVATE", lambda v: str(v).strip() == "1")
        put("TRUST", "MEMORY_TRUST", lambda v: str(v).strip() == "1")
        put("NO_MEMORY_NOTE_ON", "MEMORY_NO_MEMORY_NOTE", lambda v: str(v).strip() == "1")
        put("PUBLIC_EXPOSURE_GATE", "MEMORY_PUBLIC_EXPOSURE_GATE", lambda v: str(v).strip() == "1")
        return out

    def _configure(self, mod: Any, residents: Sequence[str]) -> None:
        """Everything the kit decides, written onto a fresh copy (the host's `MEMORY_*` variables were hidden while
        it loaded, so what is not set here is the code default)."""
        mod.time = _KitClock(self._ambient_now)
        mod.DB_PATH = str(self.db_path)
        mod.EMBED_URL = mod.CPA_BASE = mod.CPA_CONFIG = mod.MODEL = ""
        mod.CONSUMERS = list(residents)
        mod.OWNER_CHATS = {f"{k}@{OWNER_PRIVATE_CHAT}" for k in residents}
        mod.NAMES = {k: self._names.get(k, k) for k in residents}
        mod.CHAT_ALIASES = {}
        mod.UNIFIED = set(residents) if self.unified_groups else set()
        mod.PRIVATE_IN_GROUPS = bool(self.private_in_groups)
        mod.LEAK_FREE_CHATS = {self._chat_of_channel(mod, ch) for ch in self.leak_free_groups}
        # the kit's default: a housemate's own diaries come to mind in every chat (AI_HOUSE_MEMORY_DIARY_PRIVATE=1:
        # only in the owner's private chat, which is the shared module's own default)
        mod.DIARY_PRIVATE = False
        for attr, value in {**self._numeric, **self._switches(residents)}.items():
            setattr(mod, attr, value)
        # `search(k=RECALL_K)` binds its default when the file is read; point it at the setting in force
        code = mod.search.__code__
        names = code.co_varnames[:code.co_argcount]
        defaults = list(mod.search.__defaults__ or ())
        offset = len(names) - len(defaults)
        if "k" in names[offset:]:
            defaults[names.index("k") - offset] = mod.RECALL_K
            mod.search.__defaults__ = tuple(defaults)
        mod.llm = self._make_llm(mod)
        mod.embed = self._make_embed()
        self._narrow_world_view(mod)
        mod.init()

    def _narrow_world_view(self, mod: Any) -> None:
        """A recall for the kit's `world` scope (no chat at all: the house's own doings) may see the memories saved
        as `world` and her own diaries, and nothing that belongs to a chat. The shared module has no such place, so
        while such a recall runs (this thread only) its visibility rule is replaced by that narrower one and lines
        from other chats are left out."""
        visible, recent, note = mod._visible_sql, mod.recent_block, mod._private_note

        def _visible_sql(consumer: str, chat: str) -> Tuple[str, List[Any]]:
            if not getattr(self._tl, "world_view", False):
                return visible(consumer, chat)
            where = "id in (select mem_id from kit_origin where scope='world')"
            if mod.DIARY_PRIVATE:
                return f"status='active' and kind!='diary' and {where}", []
            return f"status='active' and ({where} or (kind='diary' and owner=?))", [consumer]

        def recent_block(consumer: str, chat: str, text: str) -> str:
            return "" if getattr(self._tl, "world_view", False) else recent(consumer, chat, text)

        def _private_note(consumer: str, chat: str) -> str:  # nothing private is shown there
            return "" if getattr(self._tl, "world_view", False) else note(consumer, chat)

        mod._visible_sql, mod.recent_block, mod._private_note = _visible_sql, recent_block, _private_note

    def _make_llm(self, mod: Any) -> Callable[..., Dict[str, Any]]:
        def llm(system: str, user: str, timeout: float = 300) -> Dict[str, Any]:
            fn = self._build_extractor_fn(getattr(self._tl, "llm_fn", None), actor=getattr(self._tl, "actor", ""))
            if fn is None:
                raise mod.Blocked("no memory model is configured")
            try:
                return fn(system, user)
            except MemoryRefusedError as exc:
                raise mod.Blocked(str(exc)) from exc

        return llm

    def _make_embed(self) -> Callable[..., Any]:
        def embed(texts: Sequence[str], timeout: float = 20) -> Any:
            # recall() puts a space before the question (see there); an embedder that matches on the exact text
            # must get it as it was asked
            return self._embed([str(t).strip() for t in texts])

        return embed

    # ---- residents, numbers, the loaded copy ------------------------------------------------------------------

    @property
    def names(self) -> Dict[str, str]:
        return self._names

    @names.setter
    def names(self, value: Optional[Mapping[str, str]]) -> None:
        new = dict(value or {})
        with self._cfg_lock:
            if new == self._names:
                return
            self._names = new
            self._residents |= set(new)
            self._rebuild()

    def _ambient_now(self) -> float:
        clock = self._clock
        if clock is not None:
            return float(clock.now() if hasattr(clock, "now") else clock())
        return _real_time.time()

    def _now(self, now: Optional[float] = None) -> float:
        return float(now) if now is not None else self._ambient_now()

    @contextlib.contextmanager
    def _adapter_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("pragma journal_mode=wal")
            conn.execute("pragma busy_timeout=10000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_adapter_tables(self) -> None:
        with self._adapter_conn() as c:
            c.executescript("""
            create table if not exists kit_numbers(key text primary key, num integer not null unique);
            create table if not exists kit_channels(id integer primary key autoincrement, channel text not null unique);
            create table if not exists kit_origin(mem_id integer primary key, scope text not null);
            """)

    def _numbers(self, keys: Sequence[str]) -> Dict[str, int]:
        """Placeholder number of each housemate, kept in the database so a later housemate never moves an earlier
        one. First choice is a hash of the key; a taken number moves on to the next free one."""
        lo, hi = HOUSEMATE_NUMBERS
        span = hi - lo + 1
        with self._adapter_conn() as c:
            have = {r["key"]: int(r["num"]) for r in c.execute("select key, num from kit_numbers")}
            taken = set(have.values())
            for key in sorted(keys):
                if key in have:
                    continue
                n = lo + zlib.crc32(key.encode("utf-8")) % span
                while n in taken:
                    n = lo + (n - lo + 1) % span
                c.execute("insert into kit_numbers(key, num) values (?, ?)", (key, n))
                have[key] = n
                taken.add(n)
        self._numbers_by_key = {k: have[k] for k in keys}
        return self._numbers_by_key

    def _rebuild(self) -> None:
        """(Re)load the copy for the current housemates and names. The database stays; the scheduling state of the
        old copy (hourly extraction count, failures, urgent times) is carried over."""
        with self._cfg_lock:
            keys = sorted(self._residents)
            numbers = self._numbers(keys)
            rows = [{"actor": k, "qq": numbers[k], "name": self._names.get(k, k)} for k in keys]
            made = _roster.make(rows, owner={"qq": OWNER_NUMBER, "name": "房主"}, name="roster_common")
            old, new = self._mod, _load_clean(made)
            try:
                self._configure(new, keys)
            except BaseException:
                loader.unload(new)
                raise
            if old is not None:
                new._extract_times.extend(old._extract_times)
                new._extract_fails.update(old._extract_fails)
                new._urgent_times.update(old._urgent_times)
                loader.unload(old)
            self._mod = new
            self._closed = False

    def _use(self, actor: str) -> Any:
        """The loaded copy, after making sure `actor` is one of its housemates (a new one costs a reload)."""
        if actor not in self._residents:
            with self._cfg_lock:
                if actor not in self._residents:
                    self._residents.add(actor)
                    self._rebuild()
        return self._mod

    def _number_of(self, actor: str) -> int:
        return self._numbers_by_key[actor]

    # ---- chats ---------------------------------------------------------------------------------------------------

    def _chat_of_channel(self, mod: Any, channel: str) -> str:
        """The shared module's group chat for a kit group channel id: `group` is group:1, `qq-group:<n>` is
        group:<n>; any other id gets a number of its own, kept in the database."""
        ch = channel or "group"
        if ch == "group":
            return WEB_GROUP_CHAT
        m = _QQ_GROUP_RE.fullmatch(ch)
        if m and m.group(1) == str(int(m.group(1))) and 2 <= int(m.group(1)) < _CHANNEL_BASE:
            return f"group:{int(m.group(1))}"
        with self._adapter_conn() as c:
            c.execute("insert or ignore into kit_channels(channel) values (?)", (ch,))
            row = c.execute("select id from kit_channels where channel=?", (ch,)).fetchone()
        return f"group:{_CHANNEL_BASE + int(row['id'])}"

    def _channel_of_chat(self, chat: str, actor: str) -> str:
        kind, _, num = chat.partition(":")
        if kind == "private":
            return f"private:{actor}"
        n = int(num or 0)
        if chat == WEB_GROUP_CHAT:
            return "group"
        if n >= _CHANNEL_BASE:
            with self._adapter_conn() as c:
                row = c.execute("select channel from kit_channels where id=?", (n - _CHANNEL_BASE,)).fetchone()
            if row:
                return str(row["channel"])
        return f"qq-group:{n}"

    def _chat_for(self, mod: Any, scope: str, resolved_channel: str) -> str:
        if scope == "private":
            return OWNER_PRIVATE_CHAT
        if scope == "group":
            return self._chat_of_channel(mod, resolved_channel)
        return WEB_GROUP_CHAT  # world

    # ---- observe -------------------------------------------------------------------------------------------------

    def _next_message_id(self) -> int:
        with self._msg_lock:
            self._msg_seq += 1
            return self._msg_seq

    def _render(self, card: str, number: int, body: str) -> str:
        card = _HEADER_BAD_RE.sub("", str(card or "")).strip()[:24] or "?"
        return f"[{card}|{number}] (message_id={self._next_message_id()}): {body}"

    def _speaker_number(self, actor: str, role: str, speaker: str, speaker_id: str) -> int:
        if role == "owner":
            return OWNER_NUMBER
        if role == "self":
            return self._number_of(actor)
        sid = str(speaker_id or "").strip()
        m = _QQ_ID_RE.fullmatch(sid)
        if m and int(m.group(1)) >= _REAL_QQ_MIN:
            return int(m.group(1))
        m = _RESIDENT_ID_RE.fullmatch(sid)
        if m and m.group(1) in self._numbers_by_key:
            return self._numbers_by_key[m.group(1)]
        lo, hi = STRANGER_NUMBERS
        return lo + zlib.crc32(str(speaker or "").encode("utf-8")) % (hi - lo + 1)

    def observe(
        self,
        actor: str,
        scope: str,
        text: str,
        *,
        channel: str = "",
        speaker: str = "",
        speaker_role: Optional[str] = None,
        speaker_id: str = "",
        reply: str = "",
        now: Optional[float] = None,
    ) -> int:
        """Record one turn as rendered lines: what was said, and her reply as her own line. -> lines kept."""
        actor = self._valid_actor(actor)
        scope = self._valid_scope(scope)
        resolved = self._resolve_channel(actor, scope, channel)
        role = str(
            speaker_role if speaker_role is not None
            else ("owner" if scope == "private" else "world" if scope == "world" else "member")
        ).strip().lower()
        if role == "bot":
            return 0
        if scope == "private" and role not in ("owner", "self"):
            return 0
        mod = self._use(actor)
        chat = self._chat_for(mod, scope, resolved)
        lines: List[str] = []
        clean_in = _clean_line(text, single_line=(role != "owner"))
        if clean_in and not mod.SECRETS.search(clean_in):
            card = _clean_speaker(speaker, role)
            if role == "world" and not speaker:
                lines.append(clean_in)
            else:
                lines.append(self._render(card, self._speaker_number(actor, role, speaker, speaker_id), clean_in))
        clean_rep = _clean_line(reply, single_line=True)
        if clean_rep and not mod.SECRETS.search(clean_rep):
            me = _clean_speaker(self._names.get(actor, actor), "self")
            lines.append(self._render(me, self._number_of(actor), clean_rep))
        if not lines:
            return 0
        with mod.time.at(self._now(now)):
            return int(mod.observe(actor, chat, lines))

    # ---- recall and the outbound check ---------------------------------------------------------------------------

    def recall(
        self,
        actor: str,
        scope: str,
        text: str,
        *,
        channel: str = "",
        about: Sequence[str] = (),
        k: Optional[int] = None,
        max_chars: int = DEFAULT_RECALL_MAX_CHARS,
        now: Optional[float] = None,
    ) -> str:
        actor = self._valid_actor(actor)
        scope = self._valid_scope(scope)
        resolved = self._resolve_channel(actor, scope, channel)
        mod = self._use(actor)
        # `world` is asked from a place where nothing was ever said (see _narrow_world_view for what it may see)
        chat = WORLD_LEAK_CHAT if scope == "world" else self._chat_for(mod, scope, resolved)
        # a leading space: text from a chat member must never parse as a sender header of its own
        lines = [" " + str(text or "")]
        self._tl.world_view = scope == "world"
        try:
            with mod.time.at(self._now(now)):
                if k is None:
                    return str(mod.recall(actor, chat, lines))
                with self._k_lock:  # the shared recall reads its count from the module global
                    before, mod.RECALL_K = mod.RECALL_K, max(1, int(k))
                    try:
                        return str(mod.recall(actor, chat, lines))
                    finally:
                        mod.RECALL_K = before
        finally:
            self._tl.world_view = False

    def check_outbound_leak(
        self,
        actor: str,
        scope: str,
        reply_text: str,
        *,
        channel: str = "",
        now: Optional[float] = None,
    ) -> List[int]:
        actor = self._valid_actor(actor)
        scope = self._valid_scope(scope)
        if scope == "private":
            return []
        mod = self._use(actor)
        chat = WORLD_LEAK_CHAT  # a place that is never exempt: `world`, no channel given, or one that is not a group
        if scope == "group" and str(channel or "").strip():
            ch = self._clean_channel(channel)
            if ch and not ch.lower().startswith("private"):
                chat = self._chat_of_channel(mod, ch)
                # exempt only when the id as given is listed (a malformed id is never cleaned into a listed one)
                if chat in mod.LEAK_FREE_CHATS and not self.leak_free_in(channel):
                    chat = WORLD_LEAK_CHAT
        with mod.time.at(self._now(now)):
            return [int(i) for i in mod.leak(actor, chat, reply_text)]

    # ---- add / list / invalidate -------------------------------------------------------------------------------

    def add(
        self,
        actor: str,
        scope: str,
        text: str,
        *,
        channel: str = "",
        about: Union[str, Sequence[str], None] = "",
        kind: str = "fact",
        trust: Optional[str] = None,
        shared: bool = False,
        expires_at: Optional[float] = None,
        ttl_s: Optional[float] = None,
        source: str = "self",
        merge: bool = True,
        now: Optional[float] = None,
    ) -> Tuple[int, str]:
        actor = self._valid_actor(actor)
        scope = self._valid_scope(scope)
        resolved = self._resolve_channel(actor, scope, channel)
        mod = self._use(actor)
        ts = self._now(now)
        if expires_at is None and ttl_s is not None:
            if float(ttl_s) <= 0:
                raise ValueError("ttl_s 必须大于 0")
            expires_at = ts + float(ttl_s)
        if trust is None and kind == "claim":  # the old store's kind for what somebody merely said
            trust = "group_stated"
        with mod.time.at(ts):
            if kind == "diary":
                # the housemate's own. Written from the house group, so the shared module keeps it as her own
                # (not private) while its diary switch is off, and she finds it in every chat; with the switch on
                # the module keeps it private and shows it in the owner's private chat only.
                return int(mod.diary(actor, WEB_GROUP_CHAT, text)), "added"
            chat = self._chat_for(mod, scope, resolved)
            shared_row = scope == "world" or (scope == "group" and bool(shared))
            mid, how = mod.add(actor, chat, text, about=about if about is not None else "", kind=kind,
                               scope="public" if shared_row else "own", source=source, merge=merge,
                               trust=trust, expires_at=expires_at)
        if scope == "world" and how == "added":
            with self._adapter_conn() as c:
                c.execute("insert or replace into kit_origin(mem_id, scope) values (?, 'world')", (int(mid),))
        return int(mid), how

    def _world_ids(self, ids: Sequence[int]) -> set:
        if not ids:
            return set()
        with self._adapter_conn() as c:
            return {int(r["mem_id"]) for r in c.execute(
                f"select mem_id from kit_origin where mem_id in ({','.join('?' * len(ids))})", list(ids))}

    def _kit_view(self, row: Mapping[str, Any], world: bool) -> Tuple[str, str]:
        """(kit scope, kit channel) of a row of the shared module's `mem` table."""
        if world or row["kind"] == "diary":
            return "world", "world"
        if str(row["chat"]).startswith("private:"):
            return "private", f"private:{row['owner']}"
        return "group", self._channel_of_chat(str(row["chat"]), str(row["owner"]))

    def _kit_row(self, row: Mapping[str, Any], world: bool) -> Dict[str, Any]:
        scope, channel = self._kit_view(row, world)
        return {
            "id": int(row["id"]), "owner": "shared" if row["owner"] == "public" else row["owner"], "scope": scope,
            "channel": channel, "about": row["about"], "kind": row["kind"],
            "trust": row["trust"] if "trust" in row.keys() else "", "text": row["text"],
            "source": row["source"], "status": row["status"], "created": row["created"], "updated": row["updated"],
            "last_used": row["last_used"], "uses": row["uses"],
            "expires_at": row["expires_at"] if "expires_at" in row.keys() else None,
        }

    def list_entries(
        self,
        actor: Optional[str] = None,
        scope: str = "world",
        *,
        channel: str = "",
        kind: Optional[str] = None,
        limit: int = 50,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        sc = self._valid_scope(scope)
        cap = max(1, min(int(limit), 200))
        with self._mod.time.at(self._now(now)):
            self._mod._sweep()  # what is past its time is not listed either
        clauses = ["status='active'"]
        params: List[Any] = []
        origin = "id in (select mem_id from kit_origin where scope='world')"
        if sc == "private":
            act = self._valid_actor(actor or "")
            self._resolve_channel(act, sc, channel)
            clauses += ["owner=?", f"chat='{OWNER_PRIVATE_CHAT}'", "kind!='diary'", f"not {origin}"]
            params.append(act)
        elif sc == "group":
            clauses += ["chat like 'group:%'", "kind!='diary'", f"not {origin}"]
            if actor:
                act = self._valid_actor(actor)
                clauses.append("owner in (?, 'public')")
                params.append(act)
            if channel:
                ch = (self._resolve_channel(act, sc, channel) if actor
                      else self._clean_channel(channel)) or "group"
                clauses.append("chat=?")
                params.append(self._chat_of_channel(self._mod, ch))
        else:
            if actor:
                act = self._valid_actor(actor)
                clauses.append(f"((kind='diary' and owner=?) or ({origin} and owner='public'))")
                params.append(act)
            else:
                clauses.append(f"(kind='diary' or {origin})")
        if kind:
            clauses.append("kind=?")
            params.append(str(kind).strip())
        with self._adapter_conn() as c:
            rows = c.execute(f"select * from mem where {' and '.join(clauses)} order by updated desc, id desc limit ?",
                             [*params, cap]).fetchall()
        world = self._world_ids([int(r["id"]) for r in rows])
        return [self._kit_row(r, int(r["id"]) in world) for r in rows]

    def invalidate(
        self,
        actor: str,
        scope: str,
        mem_id: Union[int, str],
        *,
        channel: str = "",
        reason: str = "invalidated",
        allow_shared: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Take a memory out of recall for good (the shared module marks it `deleted`; the reason goes to its log)."""
        actor = self._valid_actor(actor)
        scope = self._valid_scope(scope)
        resolved = self._resolve_channel(actor, scope, channel)
        if isinstance(mem_id, bool):
            raise ValueError("记忆编号无效")
        try:
            mid = int(str(mem_id).lstrip("#"))
        except ValueError as exc:
            raise ValueError("记忆编号无效") from exc
        ts = self._now(now)
        clean_reason = _clean_text(reason, limit=80) or "invalidated"
        mod = self._use(actor)
        # the owner's private chat may edit all of her own rows and every public one: the kit's narrower rule is
        # checked here, on the row as the kit sees it
        chat = self._chat_for(mod, "group", resolved) if scope == "group" else OWNER_PRIVATE_CHAT
        missing = ValueError(f"未找到可作废的记忆 #{mid}")
        with mod.time.at(ts):
            try:
                row = mod._own_row(actor, chat, mid)
            except ValueError:
                raise missing from None
            kit_scope, _ = self._kit_view(row, bool(self._world_ids([mid])))
            shared_row = row["owner"] == "public"
            if scope == "private" and shared_row:
                raise missing
            if scope in ("group", "world") and (kit_scope != scope or (shared_row and not allow_shared)):
                raise missing
            mod.forget(actor, chat, mid)
            with self._adapter_conn() as c:
                mod._log(c, actor, "invalidate", {"id": mid, "reason": clean_reason})
        out = self._kit_row(row, kit_scope == "world")
        out.update(status="invalidated", invalidated_reason=clean_reason, updated=ts)
        return out

    forget = invalidate

    # ---- extract -------------------------------------------------------------------------------------------------

    def _undone(self, key: Tuple[str, str, int]) -> int:
        with self._adapter_conn() as c:
            return int(c.execute("select count(*) from seen where consumer=? and chat=? and private=? and done=0",
                                 key).fetchone()[0])

    def extract(
        self,
        actor: Optional[str] = None,
        *,
        scope: Optional[str] = None,
        channel: Optional[str] = None,
        force: bool = False,
        llm_fn: Optional[Callable[[str, str], Any]] = None,
        allow_shared: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """One round of what the shared module's background loop does, run by the caller: for every chat with new
        lines that is due (enough lines, or the oldest is old enough, or urgent) and not backing off, within the
        hourly cap, ask the model. `force` skips all of those checks and raises the first failure."""
        ts = self._now(now)
        totals: Dict[str, Any] = {"buckets": 0, "lines": 0, "added": 0, "merged": 0, "updated": 0, "invalidated": 0,
                                  "rejected": 0, "refused": 0, "expired": 0}
        act = self._valid_actor(actor) if actor is not None else None
        sc = self._valid_scope(scope) if scope is not None else None
        mod = self._use(act) if act else self._mod
        want_chat: Optional[str] = None
        if channel is not None and sc != "private":
            ch = self._clean_channel(channel)
            want_chat = OWNER_PRIVATE_CHAT if ch.lower().startswith("private") else self._chat_of_channel(mod, ch)

        def wanted(r: Any) -> bool:
            if act is not None and r["consumer"] != act:
                return False
            if sc == "private" and not r["private"]:
                return False
            if sc == "group" and not r["chat"].startswith("group:"):
                return False
            if sc == "world" and r["chat"] != WEB_GROUP_CHAT:
                return False
            return want_chat is None or r["chat"] == want_chat

        with self._extract_lock:
            mod = self._mod
            due = [r for r in mod._due() if wanted(r)]
            if mod.URGENT:
                due = sorted(due, key=lambda r: not mod._urgent_due(r, ts))
                active = {(r["consumer"], r["chat"], r["private"]) for r in due}
                for key, last in list(mod._urgent_times.items()):
                    if key not in active and ts - last >= mod.URGENT_GAP_S:
                        mod._urgent_times.pop(key, None)
            for r in due:
                key = (r["consumer"], r["chat"], r["private"])
                if r["consumer"] not in mod.CONSUMERS or key in mod._extracting:
                    continue
                urgent = False
                if not force:
                    urgent = mod._urgent_due(r, ts)
                    if not urgent and r["n"] < mod.EXTRACT_BATCH and ts - r["first"] < mod.EXTRACT_EVERY_S:
                        continue
                    if ts < mod._extract_fails.get(key, (0, 0))[1]:
                        continue
                    while mod._extract_times and ts - mod._extract_times[0] > 3600:
                        mod._extract_times.popleft()
                    if len(mod._extract_times) >= mod.EXTRACT_PER_HOUR:
                        break
                if self._build_extractor_fn(llm_fn, actor=key[0]) is None:
                    continue  # no model: the lines stay for when there is one
                if not force:
                    mod._extract_times.append(ts)
                    if urgent:
                        mod._urgent_times[key] = ts
                mod._extracting.add(key)
                scribes = mod.PUBLIC_SCRIBES
                before = self._undone(key)
                try:
                    with mod.time.at(ts):
                        if urgent:
                            with mod.db() as c:
                                mod._log(c, key[0], "extract_urgent",
                                         {"chat": key[1], "private": bool(key[2]),
                                          "reason": "keyword" if r["urgent"] else "owner_quiet",
                                          "hour_used": len(mod._extract_times)})
                        if allow_shared and key[1].startswith("group:") and not (
                                self.private_in_groups or key[1] in mod.LEAK_FREE_CHATS):
                            mod.PUBLIC_SCRIBES = set(scribes) | {key[0]}
                        self._tl.llm_fn, self._tl.actor = llm_fn, key[0]
                        res = mod.extract(*key)
                    mod._extract_fails.pop(key, None)
                    if res is not None:
                        totals["buckets"] += 1
                        totals["lines"] += max(0, before - self._undone(key))
                        for stat in ("added", "merged", "updated", "rejected", "refused"):
                            totals[stat] += int(res.get(stat, 0))
                        totals["invalidated"] += int(res.get("forgot", 0))
                except Exception as exc:  # noqa: BLE001  # the lines stay undone; retried after a back-off
                    n_fail = mod._extract_fails.get(key, (0, 0))[0] + 1
                    mod._extract_fails[key] = (n_fail, ts + min(120 * 2 ** n_fail, 3600))
                    log.warning("memory extract %s failed (%d in a row): %s", key, n_fail, type(exc).__name__)
                    if force:
                        raise
                finally:
                    mod.PUBLIC_SCRIBES = scribes
                    self._tl.llm_fn = self._tl.actor = None
                    mod._extracting.discard(key)
        return totals

    # ---- the rest ------------------------------------------------------------------------------------------------

    def narrow_api(self) -> NarrowMemoryAPI:
        return NarrowMemoryAPI(self)  # type: ignore[arg-type]

    def close(self) -> None:
        """Checkpoint the log and drop the copy from the import table (a copy already in use keeps working)."""
        with self._cfg_lock:
            if self.db_path.exists():
                try:
                    with self._adapter_conn() as c:
                        c.execute("pragma wal_checkpoint(passive)")
                except sqlite3.Error:
                    pass
            if self._mod is not None and not self._closed:
                loader.unload(self._mod)
            self._closed = True


__all__ = ["CommonMemoryStore", "OWNER_NUMBER", "OWNER_PRIVATE_CHAT"]
