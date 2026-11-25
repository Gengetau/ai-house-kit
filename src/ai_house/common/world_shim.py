"""The few names of the private deployment's world module that the shared promise code (`intents.py`) uses.

There the promise code does `import world as W`: a module of several thousand lines that runs the housemates' days.
The kit has its own kernel for that and does not carry the file. What `intents.py` needs from it is small and is
here: opening the house database, who is who, the day boundary, an event log, and two helpers of the feature by
which housemates reach each other. The bodies are the private file's own, with the differences listed in
`UPSTREAM.md` (no journal mode is set, the connection closes at the end of its `with`, no house group is written
in as a default, and `_together` knows no rooms until the caller gives it some).

Like `memory.py`, this is one module per house: load a copy with `loader.load()` and set `DB_PATH` and `HOME_CHAT`
on it.
"""

import datetime
import json
import os
import re
import sqlite3
import time

if __package__:
    from . import roster as R
else:  # the private layout: plain top-level modules
    import roster as R

DB_PATH = os.environ.get("WORLD_DB", "")  # the house's world database; set by the caller
DM_AUDIENCE = os.environ.get("WORLD_DM_AUDIENCE", "1") == "1"  # a private chat's promises stay with that chat
HOME_CHAT = os.environ.get("WORLD_HOME_CHAT", "")  # the house's own group, as a chat id (`group:<n>`); set by the caller
JST = datetime.timezone(datetime.timedelta(hours=9))  # Japan has no DST
ACTORS = R.ACTORS
CONSUMER_ACTOR = R.CONSUMER_ACTOR
CONTACTS_PER_DAY = int(os.environ.get("WORLD_CONTACTS_PER_DAY", "0"))
INTENTS = None  # the promise module of this house, once it is switched on
LOCATIONS = {}  # room -> (.., .., shared?) in the private file; empty here, so nobody is ever "together" (see nudge)
NAME_KEY = {**{v: k for k, v in ACTORS.items()}, **{n: r["actor"] for r in R.ROSTER for n in r["former"]}}
PRIVATE = re.compile(r"\d{5,}|密码|验证码|身份证|银行卡|住址|电话")


def jst(ts=None):
    return datetime.datetime.fromtimestamp(time.time() if ts is None else ts, JST)


class _Conn(sqlite3.Connection):
    """`with db() as c:` commits or rolls back, and then closes (sqlite3's own `with` leaves the connection open)."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=_Conn)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=10000")
    return conn


def init():
    """The tables of the world module that the promise code writes to. The rest of that module's tables belong to
    features the kit does not have."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.executescript("""
        create table if not exists events(
            id integer primary key, ts real not null, kind text not null, actors text not null,
            data text not null);
        create table if not exists contacts(
            id integer primary key, actor text not null, target text not null, about text not null,
            created real not null, status text not null default 'pending', via text, done real,
            origin text not null default 'bot');
        create index if not exists contacts_actor on contacts(actor, status);
        """)


def _event(c, kind, actors, data):
    c.execute("insert into events(ts, kind, actors, data) values (?,?,?,?)",
              (time.time(), kind, json.dumps(actors), json.dumps(data, ensure_ascii=False)[:4000]))


def actor_of(consumer):
    a = CONSUMER_ACTOR.get(consumer, consumer)
    if a not in ACTORS:
        raise ValueError(f"unknown world actor {consumer}")
    return a


def _together(c, a, b):
    """Both awake in the same shared room: the world writes what they do together."""
    ra = c.execute("select location, activity from reality where actor=?", (a,)).fetchone()
    rb = c.execute("select location, activity from reality where actor=?", (b,)).fetchone()
    return bool(ra and rb and ra["location"] == rb["location"]
                and LOCATIONS.get(ra["location"], (None, None, False))[2]
                and "sleeping" not in (ra["activity"], rb["activity"]))


def _add_contact(c, actor, target, about, origin, now):
    """Store one wish to reach another bot. None when it can't be kept (bad target, daily cap, duplicate)."""
    about = _clean(about, 120)
    if target not in ACTORS or target == actor or len(about) < 2 or _private(about):
        return None
    if CONTACTS_PER_DAY and c.execute("select count(*) from contacts where actor=? and created>=?",
                                      (actor, _day_start(now))).fetchone()[0] >= CONTACTS_PER_DAY:
        return None
    if c.execute("select 1 from contacts where actor=? and target=? and status='pending'", (actor, target)).fetchone():
        return None  # one open wish per pair is enough
    cid = c.execute("insert into contacts(actor, target, about, created, origin) values (?,?,?,?,?)",
                    (actor, target, about, now, origin)).lastrowid
    _event(c, "contact", [actor, target], {"id": cid, "about": about, "origin": origin})
    return cid


def _clean(t, n):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(t or "")).strip()[:n]


def _key(who):
    """An actor key from what the model wrote: the key itself, or her name."""
    return who if who in ACTORS else NAME_KEY.get(str(who or "").strip())


def _private(text):
    return bool(PRIVATE.search(text))


def _day_start(now):
    d = jst(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
