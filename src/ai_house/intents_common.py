"""`CommonIntents`: the house's promise ("约定") code, on top of the shared promise module (`ai_house.common.intents`).

The shared module speaks the private deployment's language: module-level functions that read the house database
through a `world` module, take their switches from the environment and their clock from `time`. The kit has one
house per object and a house clock of its own. This class is that translation and nothing else: **every promise
decision (what is recorded, who has said yes, what closes it, when it ends) is made by the loaded copy of the shared
module**; this file only configures that copy, carries old data over, and renames fields for the page.

What it does
- holds the `intents` and `world_shim` modules of one loader copy. Given the house's memory store it uses that
  store's copy (and sets `memory_module.INTENTS`, so the extraction hook calls `open_text / prompt / apply_chat`);
  the store tells it whenever it reloads its copy (`attach_intents`). Without a store (kernel tests build an
  `Authority` alone) it loads a copy of its own, with a roster made from the vocabulary.
- configuration: house clock (calls without `now`), the house's effective env mapping (not the process env), the
  switches `ON / SHOW / EVIDENCE_GATE / PRESENCE_ON`, `DB_PATH`, `HOME_CHAT`, and the kit's own `promise_rules` bound to
  the house's pack (`bound(vocab)`).
- `init()`: carries an existing `world.db` over (see there), then lets the shared module make its tables.
- pass-through: `sweep`, `kernel_context`, `errands`, `request_reopen`, `card_lines`, `listing`.
- `list_intents`: `listing()` in the row shape the web page reads.

Names. The shared `_party_key` knows the owner as "房主", "owner" and the aliases of the roster; the roster made here
has the owner as "房主" without aliases. The house, though, shows the owner on every line by `speaker.name`, which is
whatever the sender called themselves ("房主" by default). A model that writes that name into `with` / `for` /
`agreed` would be taken for nobody. So, only inside the batch being applied, the card of every line whose number is
the owner's counts as the owner's name, unless a line of someone else in the same batch wears the same card.

What `list_intents` shows. `listing()` has no viewer argument: a promise made in the owner's private chat always
shows the placeholder "（房主私聊里的约定）" in place of its text (and its refs reduced to "private"), everything else
(group promises, with their text and evidence) as it is. The only caller is `GET /api/intents`, which needs the
`private` tier; the owner sees exactly this, a caller without the tier sees nothing. This is narrower than what the
earlier endpoint showed (it showed the owner's private-chat summaries), not wider.
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time as _real_time
from typing import Any, Dict, List, Mapping, Optional

from .common import loader, roster as _roster
from .memory_common import OWNER_NUMBER, _KitClock, _load_clean

log = logging.getLogger("ai_house.intents_common")

WEB_GROUP_CHAT = "group:1"  # the kit's web group (as `memory_common.WEB_GROUP_CHAT`)

# private statuses -> the states the page and the endpoint speak
_STATE = {"proposed": "open", "accepted": "open", "completed": "completed", "expired": "expired",
          "missed": "expired", "declined": "cancelled", "cancelled": "cancelled"}

_OWNER_NAME_BAD = re.compile(r"\(你\)|（你）|·头衔.*$")


class _OsProxy:
    """Stands in for the `os` module inside a copy: `environ` is the house's effective mapping."""

    def __init__(self, environ: Mapping[str, str]):
        import os as _os

        self._os = _os
        self.environ = environ

    def __getattr__(self, name: str) -> Any:
        return getattr(self._os, name)


class CommonIntents:
    def __init__(
        self,
        world_db: Any,
        *,
        vocab: Any = None,
        env: Optional[Mapping[str, str]] = None,
        memory: Any = None,
        clock: Any = None,
    ):
        import os

        self.world_db = str(world_db) if world_db else ""
        if vocab is None:
            from worldkernel import vocab_life1 as vocab  # the default pack
        self.vocab = vocab
        self._env: Mapping[str, str] = os.environ if env is None else env  # the mapping itself: later changes count
        self._memory = memory
        self._clock = clock
        self._tl = threading.local()
        self._lock = threading.RLock()
        self._own: Any = None  # the memory module of a copy this object made itself
        self._i: Any = None
        self._w: Any = None
        self._mem_mod: Any = None
        self._rules: Any = None
        self._own_clock = _KitClock(self._ambient_now)
        from worldkernel import promise_rules

        self._rules = promise_rules.bound(self.vocab)
        if memory is not None:
            memory.attach_intents(self._bind)  # calls _bind now and after each reload of the store's copy
        else:
            self._bind(self._make_own_copy())

    # ---- the copy ------------------------------------------------------------------------------------------------

    def _ambient_now(self) -> float:
        clock = self._clock
        if clock is not None:
            return float(clock.now() if hasattr(clock, "now") else clock())
        return _real_time.time()

    def _make_own_copy(self) -> Any:
        keys = sorted(str(k) for k in (getattr(self.vocab, "ROSTER", None) or ()))
        names = getattr(self.vocab, "NAMES", None) or {}
        rows = [{"actor": k, "qq": 1000 + n, "name": str(names.get(k, k))} for n, k in enumerate(keys)]
        made = _roster.make(rows, owner={"qq": OWNER_NUMBER, "name": "房主"}, name="roster_intents")
        mem = _load_clean(made)
        self._own = mem
        return mem

    def _bind(self, mem_mod: Any) -> None:
        """Take (and configure) the `intents` / `world_shim` modules of the copy `mem_mod` belongs to."""
        with self._lock:
            i = loader.part(mem_mod, "intents")
            w = loader.part(mem_mod, "world_shim")
            self._configure(mem_mod, i, w)
            self._i, self._w, self._mem_mod = i, w, mem_mod

    def _configure(self, mem_mod: Any, i: Any, w: Any) -> None:
        env = self._env
        clock = mem_mod.time if self._memory is not None else self._own_clock
        proxy = _OsProxy(env)
        for m in (i, w):
            m.time = clock
            m.os = proxy
        w.DB_PATH = self.world_db
        w.HOME_CHAT = str(env.get("WORLD_HOME_CHAT") or "") or WEB_GROUP_CHAT
        w.INTENTS = i
        i.ON = str(env.get("INTENTS", "0")) == "1"
        i.SHOW = str(env.get("INTENTS_SHOW", "0")) == "1"
        i.EVIDENCE_GATE = str(env.get("INTENTS_EVIDENCE_GATE", "1")) == "1"
        i.PRESENCE_ON = str(env.get("INTENTS_PRESENCE", "1")) == "1"
        i._rules = lambda: (self._rules, self.vocab)
        self._install_owner_names(i)
        if self._memory is not None or mem_mod is self._own:
            mem_mod.INTENTS = i  # the extraction hook of the memory module reaches the promise code through this

    def _install_owner_names(self, i: Any) -> None:
        ID = i.ID
        orig_apply, orig_key, tl = i.apply_chat, i._party_key, self._tl

        def owner_cards(lines: Any) -> frozenset:
            mine, others = set(), set()
            for line in lines or ():
                p = ID.parse(line)
                if not p:
                    continue
                key = ID._key(ID.name_of(p[0]))
                (mine if ID.who(p[1]) == "owner" else others).add(key)
            return frozenset(mine - others - {""})

        def apply_chat(consumer, chat, private, ops, lines, *a, **k):
            tl.owner_cards = owner_cards(lines)
            try:
                return orig_apply(consumer, chat, private, ops, lines, *a, **k)
            finally:
                tl.owner_cards = frozenset()

        def party_key(who, worn=frozenset()):
            key = orig_key(who, worn)
            cards = getattr(tl, "owner_cards", None)
            if key is None and cards and "同名的另一个人" not in str(who or ""):
                if ID._key(_OWNER_NAME_BAD.sub("", str(who or "")).strip()) in cards:
                    return "owner"
            return key

        i.apply_chat, i._party_key = apply_chat, party_key

    @property
    def intents_module(self) -> Any:
        return self._i

    @property
    def world_module(self) -> Any:
        return self._w

    @property
    def show(self) -> bool:
        return bool(self._i.ON and self._i.SHOW)

    def close(self) -> None:
        with self._lock:
            if self._own is not None:
                loader.unload(self._own)
                self._own = None

    # ---- schema carry-over -----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _conn(self):
        c = sqlite3.connect(self.world_db, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("pragma busy_timeout=10000")
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _columns(c: sqlite3.Connection, table: str) -> set:
        return {r[1] for r in c.execute(f"pragma table_info({table})")}

    def init(self) -> None:
        """Idempotent. First what an existing house's `world.db` needs so the shared module can run on it:
        (a) `intent_evidence` without `state` gets the column (default 'queued') and the rows that were there are
        marked 'consumed' once, so history is not judged again; (b) `owner_chat_queue` with `created_at` gets that
        column renamed `created` (the name the shared module writes; ChatLink reads neither); then the shared
        module's own `init()`; then (c) once, the standing diet memos of the old table `house_intents` become
        entries in the house's memory (no memory store: left for a later start). The other rows of `house_intents`
        stay on disk, unread."""
        if not self.world_db:
            return
        with self._lock:
            with self._conn() as c:
                if c.execute("select 1 from sqlite_master where type='table' and name='intent_evidence'").fetchone():
                    if "state" not in self._columns(c, "intent_evidence"):
                        c.execute("alter table intent_evidence add column state text not null default 'queued'")
                        c.execute("update intent_evidence set state='consumed'")
                if c.execute("select 1 from sqlite_master where type='table' and name='owner_chat_queue'").fetchone():
                    cols = self._columns(c, "owner_chat_queue")
                    if "created_at" in cols and "created" not in cols:
                        c.execute("alter table owner_chat_queue rename column created_at to created")
            self._w.init()
            self._i.init()
            self._carry_memos()

    def _carry_memos(self) -> None:
        if self._memory is None:
            return
        with self._conn() as c:
            c.execute("create table if not exists intents_carry(key text primary key, at real not null)")
            if c.execute("select 1 from intents_carry where key='house_memos'").fetchone():
                return
            if not c.execute("select 1 from sqlite_master where type='table' and name='house_intents'").fetchone():
                return
            rows = c.execute("select actor, summary from house_intents where kind='memo' and state='open'"
                             " order by created_at").fetchall()
        living = {str(k) for k in (getattr(self.vocab, "ROSTER", None) or ())}
        for r in rows:
            if living and str(r["actor"]) not in living:
                continue  # a memo of someone who no longer lives here
            text = re.sub(r"^记住", "", str(r["summary"] or "")).strip()
            try:
                self._memory.add(str(r["actor"]), "private", text, about="房主", kind="preference", source="carried")
            except Exception:  # noqa: BLE001  # too short, refused, or anything else: not carried, the house still starts
                continue
        with self._conn() as c:
            c.execute("insert or ignore into intents_carry values('house_memos', ?)", (self._ambient_now(),))

    # ---- pass-through ----------------------------------------------------------------------------------------------

    def sweep(self, now: Optional[float] = None) -> Any:
        return self._i.sweep(now)

    def kernel_context(self, actor: str, now: Optional[float] = None) -> Any:
        return self._i.kernel_context(actor, now)

    def errands(self, actor: str, now: Optional[float] = None) -> Any:
        return self._i.errands(actor, now)

    def request_reopen(self, iid: Any, expected_updated: Any, key: str, now: Optional[float] = None) -> Any:
        return self._i.request_reopen(iid, expected_updated, key, now)

    def card_lines(self, actor: str, now: Optional[float] = None, n: int = 3) -> List[str]:
        """What her 此刻 card says about promises: nothing unless `INTENTS_SHOW=1` (the default is off)."""
        return self._i.card_lines(actor, now, n)

    def listing(self, actor: Optional[str] = None, status: Optional[str] = None, now: Optional[float] = None) -> Any:
        return self._i.listing(actor, status, now)

    # ---- the page -------------------------------------------------------------------------------------------------

    def list_intents(self, actor: Optional[str] = None, limit: int = 30, *, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """`listing()` as rows for `GET /api/intents` and the 约定 page. `state` is open / completed / expired /
        cancelled (a private status name also works); `status` carries the private status through."""
        i = self._i
        rows = i.listing(actor)
        out: List[Dict[str, Any]] = []
        for x in rows:
            mapped = _STATE.get(x["status"], x["status"])
            if state and state not in (mapped, x["status"]):
                continue
            item, qty = "", 0
            if x["visibility"] != "private":  # a private row's text is a placeholder: nothing to read an item from
                try:
                    terms = self._rules.compile_terms(x["text"], self.vocab, x["party"])
                except Exception:  # noqa: BLE001
                    terms = []
                bought = next((t for t in terms if t.get("kind") == "purchased"), None)
                if bought:
                    item, qty = str(bought.get("item") or ""), int(bought.get("qty") or 0)
            kernel = next((f["kernel"] for f in reversed(x["refs"]) if isinstance(f, dict) and f.get("kernel")), "")
            out.append({
                "intent_id": str(x["id"]),
                "actor": x["actor"],
                "state": mapped,
                "status": x["status"],
                "created_at": x["created"],
                "updated_at": x["updated"],
                "summary": x["text"],
                "item": item,
                "qty": qty,
                "evidence_key": kernel,
                "party": x["party"],
                "agreed": x["agreed"],
                "due_from": x["due_from"],
                "due_to": x["due_to"],
                "visibility": x["visibility"],
                "end_reason": x.get("end_reason", ""),
                "can_reopen": bool(x.get("can_reopen")),
            })
            if len(out) >= max(1, int(limit)):
                break
        return out


__all__ = ["CommonIntents"]
