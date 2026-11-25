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
- `record_completions()`: the memory side of a finished promise. The kernel closes a promise (`intents.status` becomes
  `completed`) and writes nothing to memory, so from the next day recall would only say she had promised. Each
  completed row gets ONE memory of hers, in the audience the promise was made in (a private promise: her private chat
  with the owner; a group promise: that group). Done once per row: the kit-owned table `kit_intent_done_memos` in
  `world.db` holds a marker per (id, created); the shared `intents` table is not touched. A house that already has
  completed rows when the table is first made marks them without writing (no flood of old news).
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
import datetime as _dt
import json
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

_JST = _dt.timezone(_dt.timedelta(hours=9))
# the owner asks for something anew in the batch (so a repeated item is a real second errand, never a restatement)
_NEW_REQUEST = re.compile(r"再|又|另外|还要|还想|还得|多买|多带|第二|两次|重新")
_OWNER_NAME_BAD = re.compile(r"\(你\)|（你）|·头衔.*$")
# A bracketed note that only says a relative day ("（后天）"). It goes stale the next day, so it is cut from a promise's
# text when the text also has an absolute date.
_REL_DAY_NOTE = re.compile(r"[（(]\s*(?:原话说?|即|也就是)?\s*[“\"「]?\s*(?:今天|明天|后天|大后天|昨天|前天|今晚|明晚)"
                           r"\s*[”\"」]?\s*[）)]")
_ABS_DATE = re.compile(r"\d{1,2}月\d{1,2}[日号]|\d{4}-\d\d-\d\d")


def strip_stale_day_notes(text: Any) -> Any:
    """Remove a bracketed relative-day note from a promise text when an absolute date is still in it."""
    if not isinstance(text, str):
        return text
    rest = _REL_DAY_NOTE.sub("", text)
    return rest if rest != text and _ABS_DATE.search(rest) else text


# "the next time I go out" wording: that time is the next shopping trip, not a condition to wait for
_OUTING_WORDS = re.compile(r"下次出门|下回出门|出门时|出门的时候|出去时|出去的时候|下次去超市|下次路过超市|下次")
# a real condition in the text: with any of these an "after" promise stays as the model wrote it
_REAL_CONDITION = re.compile(r"等|之后|以后|如果|要是|假如|只要|一旦|再说|有空|发工资|回来后|之前|才")


def _outing_after_to_vague(op: dict) -> dict:
    """An undated "after" intent whose only condition is an outing phrase becomes "vague", so the next trip runs it."""
    text = op.get("text")
    if (op.get("when") == "after" and not op.get("due") and isinstance(text, str)
            and _OUTING_WORDS.search(text) and not _REAL_CONDITION.search(text)):
        return dict(op, when="vague")
    return op


def _clean_ops(ops: Any) -> Any:
    """Copy of `ops` where each intent op's text has no stale day note and an outing-only "after" is "vague"; other ops
    and the caller's dicts stay as they are."""
    if not isinstance(ops, (list, tuple)):
        return ops
    out = []
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "intent" and isinstance(op.get("text"), str):
            text = strip_stale_day_notes(op["text"])
            if text != op["text"]:
                op = dict(op, text=text)
            op = _outing_after_to_vague(op)
        out.append(op)
    return out


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
                now = k.get("now", a[0] if a else None)
                try:
                    ops = _clean_ops(ops)
                except Exception:  # noqa: BLE001  # on doubt the texts stay as the model wrote them
                    pass
                try:
                    kept, dropped = self._without_restated(i, consumer, chat, private, ops, lines, now)
                except Exception:  # noqa: BLE001  # this guard never holds the memories back: on doubt nothing is dropped
                    kept, dropped = ops, 0
                done = orig_apply(consumer, chat, private, kept, lines, *a, **k)
                if dropped and isinstance(done, dict):
                    done["merged"] = done.get("merged", 0) + dropped  # counted the way a merge into an old row is
                return done
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

    def _day_start(self, now: Optional[float]) -> tuple:
        from .diary import DIARY_DAY_STARTS_H
        t = float(now) if now is not None else float(self._ambient_now())
        local = _dt.datetime.fromtimestamp(t - DIARY_DAY_STARTS_H * 3600, _JST)
        return t, _dt.datetime.combine(local.date(), _dt.time(hour=DIARY_DAY_STARTS_H), _JST).timestamp()

    def _without_restated(self, i: Any, consumer: str, chat: Any, private: bool, ops: Any, lines: Any,
                          now: Optional[float]) -> tuple:
        """Drop the intent ops that only restate a promise of hers that was completed today. -> (ops kept, n dropped).

        The shared `_new` merges into open rows only, so once the kernel closed "buy a box of tea" a later chat in which
        she repeats the old errand ("the tea isn't bought yet") registered it a second time. An op is dropped only when
        ALL of these hold; on any doubt it is kept (a second real errand is worse to lose than a duplicate to see):
          1. its terms compile to something the kernel understands (not `unsupported`);
          2. the op's quote is a line of HERS (a restatement), not a line of the owner or anybody else;
          3. no line of the owner in this batch asks for something anew (再 / 又 / 另外 / 还要 / 多买 ...);
          4. a promise of the same actor, party, beneficiary and audience was completed today (house day, 05:00 JST)
             and its terms are equal to the op's.
        A clearly new request ("再买一盒") fails 2 or 3 and registers as before."""
        if not (i.ON and ops):
            return ops, 0
        actor = i.W.actor_of(consumer)
        spoken = i._speakers(lines)
        if any(who == "owner" and _NEW_REQUEST.search(words or "") for who, words in spoken):
            return ops, 0
        _, start = self._day_start(now)
        with self._conn() as c:
            done = [r for r in c.execute("select * from intents where status='completed' and actor=? and updated>=?",
                                         (actor, start)) if i._same_audience(r, actor, chat, private)]
        if not done:
            return ops, 0
        kept, dropped = [], 0
        for op in ops:
            if self._restates(i, actor, op, spoken, done):
                dropped += 1
            else:
                kept.append(op)
        return kept, dropped

    def _restates(self, i: Any, actor: str, op: Any, spoken: Any, done: Any) -> bool:
        if not isinstance(op, dict) or op.get("op") != "intent" or op.get("with"):
            return False
        if i._quoted(op.get("quote"), spoken) != actor:
            return False
        names = op.get("for") or []
        keys = [i._party_key(n) for n in ([names] if isinstance(names, str) else names)]
        if any(k is None for k in keys):
            return False
        bene = sorted(set(keys) - {actor})
        terms = self._rules.compile_terms(str(op.get("text") or ""), self.vocab, [actor], bene)
        if not terms or terms == [{"kind": "unsupported"}]:
            return False
        for r in done:
            if json.loads(r["party"]) != [actor] or json.loads(r["beneficiary"] or "[]") != bene:
                continue
            if self._rules.compile_terms(r["text"], self.vocab, [actor], bene) == terms:
                return True
        return False

    def done_today_lines(self, actor: str, now: Optional[float] = None, private: bool = False, n: int = 3) -> List[str]:
        """Finished facts for a chat turn: promises of hers that the kernel (or the chat) closed today, newest `n`, as
        "答应房主的「…」已经办完了（HH:MM）". A private promise's text is only given to her own private chat with the
        owner, never to a group turn. Off with INTENTS=0; independent of INTENTS_SHOW (it nags about nothing open)."""
        if not self._i.ON:
            return []
        t, start = self._day_start(now)
        try:
            with self._conn() as c:
                found = c.execute("select * from intents where status='completed' and updated>=? and updated<=? "
                                  "order by updated desc, id desc", (start, t)).fetchall()
        except sqlite3.Error:
            return []
        out: List[tuple] = []
        for r in found:
            if actor not in json.loads(r["party"]):
                continue
            if r["visibility"] == "private" and not (private and r["actor"] == actor):
                continue
            who = "答应房主的" if "owner" in json.loads(r["beneficiary"] or "[]") else "约好的"
            hm = _dt.datetime.fromtimestamp(float(r["updated"]), _JST).strftime("%H:%M")
            out.append((float(r["updated"]), f"{who}「{r['text']}」已经办完了（{hm}）"))
            if len(out) >= n:
                break
        return [text for _, text in sorted(out)]

    def open_lines(self, actor: str, now: Optional[float] = None, private: bool = False, n: int = 5) -> List[str]:
        """Promises of hers that are agreed and not done, with the day already worked out by the house (JST), so she
        never does the date arithmetic herself. Dated ones by start, then undated. Same visibility rule as
        `done_today_lines`; off with INTENTS=0; never raises on a read error."""
        if not self._i.ON:
            return []
        t = float(now) if now is not None else float(self._ambient_now())
        try:
            with self._conn() as c:
                found = c.execute("select * from intents where status='accepted' order by id").fetchall()
        except sqlite3.Error:
            return []
        today = _dt.datetime.fromtimestamp(t, _JST).replace(hour=0, minute=0, second=0, microsecond=0)
        dated: List[tuple] = []
        undated: List[str] = []
        for r in found:
            if actor not in json.loads(r["party"]):
                continue
            if r["visibility"] == "private" and not (private and r["actor"] == actor):
                continue
            who = "答应房主的" if "owner" in json.loads(r["beneficiary"] or "[]") else "约好的"
            head = f"{who}「{r['text']}」还没办，"
            if r["due_from"] is None:
                undated.append(head + "没有约具体哪天")
                continue
            if r["due_to"] is not None and float(r["due_to"]) < t - 86400:
                continue
            lo = _dt.datetime.fromtimestamp(float(r["due_from"]), _JST)
            days = (lo.date() - today.date()).days
            rel = ("昨天，已经过了" if days == -1 else "今天" if days == 0 else "明天" if days == 1 else "后天" if days == 2
                   else "大后天" if days == 3 else f"{days}天后" if days > 3 else f"{-days}天前，已经过了")
            at = ""
            if r["due_to"] is not None and float(r["due_to"]) - float(r["due_from"]) < 86400:  # an exact time: `_when`
                at = " " + _dt.datetime.fromtimestamp(float(r["due_from"]) + 3600, _JST).strftime("%H:%M")
            dated.append((float(r["due_from"]), head + f"约在{lo.month}月{lo.day}日{at}（{rel}）"))
        dated.sort(key=lambda x: x[0])
        return ([text for _, text in dated] + undated)[:max(0, n)]

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
            self._init_done_memos()
            self._carry_memos()

    def _init_done_memos(self) -> None:
        """The marker table of `record_completions`. Made once; the first time it is made, whatever is already completed
        is marked as seen (those were finished before the house wrote such memories)."""
        with self._conn() as c:
            fresh = not c.execute("select 1 from sqlite_master where type='table' and name='kit_intent_done_memos'").fetchone()
            c.execute("create table if not exists kit_intent_done_memos(intent_id integer not null, created real not null,"
                      " written_at real not null, outcome text not null, primary key(intent_id, created))")
            if fresh and c.execute("select 1 from sqlite_master where type='table' and name='intents'").fetchone():
                c.execute("insert or ignore into kit_intent_done_memos select id, created, ?, 'before' from intents "
                          "where status='completed'", (self._ambient_now(),))

    def record_completions(self, now: Optional[float] = None) -> int:
        """Write one memory for each promise that is completed and has none yet. -> rows written. Never raises.

        Same audience as the promise: private -> `private` scope (her private chat with the owner, so recall keeps
        every rule that already covers private memories); group -> the group the promise was made in (the web group
        when its refs name none, as for a promise the kernel made among housemates). Kind `event`, never merged into an
        older row. The time in the text is the completion time on the house clock (`updated`), in JST. A write that
        fails for another reason than a refusal is left unmarked and tried again on the next call. INTENTS=0: nothing."""
        mem = self._memory
        if mem is None or not self.world_db or not self._i.ON:
            return 0
        try:
            with self._conn() as c:
                pending = c.execute(
                    "select * from intents where status='completed' and not exists (select 1 from kit_intent_done_memos m "
                    "where m.intent_id=intents.id and m.created=intents.created) order by updated, id").fetchall()
        except sqlite3.Error as exc:
            log.warning("promise completions not read: %s", type(exc).__name__)
            return 0
        wrote = 0
        for r in pending:
            try:
                outcome = self._write_completion(mem, r)
            except Exception as exc:  # noqa: BLE001  # memory trouble must never reach the step loop; tried again later
                log.warning("promise completion memory not written (intent %s): %s: %s", r["id"], type(exc).__name__, exc)
                continue
            try:
                with self._conn() as c:
                    c.execute("insert or ignore into kit_intent_done_memos values (?,?,?,?)",
                              (r["id"], r["created"], self._ambient_now() if now is None else float(now), outcome))
            except sqlite3.Error as exc:
                log.warning("promise completion marker not written (intent %s): %s", r["id"], type(exc).__name__)
                continue
            wrote += outcome == "written"
        return wrote

    def _write_completion(self, mem: Any, r: sqlite3.Row) -> str:
        actor = str(r["actor"])
        done_at = float(r["updated"])
        t = _dt.datetime.fromtimestamp(done_at, _JST)
        name = str((getattr(mem, "names", None) or {}).get(actor) or actor)
        owner_asked = "owner" in json.loads(r["beneficiary"] or "[]")
        text = f"{t.month}月{t.day}日 {t:%H:%M} {name}把{'答应房主的' if owner_asked else '约好的'}事办完了：{r['text']}"
        if r["visibility"] == "private":
            scope, channel = "private", ""
        else:
            scope = "group"
            chats = [str(f["chat"]) for f in json.loads(r["refs"]) if isinstance(f, dict) and str(f.get("chat", "")).startswith("group:")]
            channel = mem._channel_of_chat(chats[0], actor) if chats else "group"
        try:
            mem.add(actor, scope, text, channel=channel, about="房主" if owner_asked else "", kind="event",
                    source="promise_done", merge=False, now=done_at)
        except ValueError as exc:  # refused by the store (a name no longer living here, text it will not keep): not retried
            log.info("promise completion memory refused (intent %s): %s", r["id"], exc)
            return "refused"
        return "written"

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
