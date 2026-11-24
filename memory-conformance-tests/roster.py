"""Who is who in AI小屋 (2026-10-20, Drive audit P1): every housemate's ids and names in one place.

10-19 由纪旧名 became 由纪 and one hand-written name list was missed (the dispatcher took her for a human). The lists
in world.py, server.py and memory.py are now made from this table, so a rename is one line here.

actor     world key (world.db, 此刻, growth)        consumer  app-services token / memory owner
qq        her QQ account (stable)                   name      what she is called now
account   the QQ account's own nickname, when it is not her name (婉妲 posts as Wanda_c)
former    names she had before; old lines and scripts still carry them
nicknames what people (and the life script) also call her
"""

ROSTER = (
    {"actor": "wanda", "consumer": "wanda_c", "qq": 1000000037, "name": "婉妲", "account": ("Wanda_c",), "former": (),
     "nicknames": ()},
    {"actor": "nell", "consumer": "nell", "qq": 1000000074, "name": "奈尔", "account": (), "former": (),
     "nicknames": ()},
    {"actor": "elsa", "consumer": "elsa", "qq": 1000000111, "name": "艾莎", "account": (), "former": (),
     "nicknames": ("小艾",)},
    {"actor": "alma", "consumer": "alma", "qq": 1000000148, "name": "阿尔玛", "account": (), "former": (),
     "nicknames": ("Alma",)},
    {"actor": "mina", "consumer": "mina", "qq": 1000000185, "name": "米娜", "account": (), "former": (),
     "nicknames": ()},
    {"actor": "yuki", "consumer": "yuki", "qq": 1000000222, "name": "由纪", "account": (), "former": ("由纪旧名",),
     "nicknames": ("小由", "小由1")},
)
OWNER = {"actor": "owner", "consumer": "owner", "qq": 1000000259, "name": "房主"}

_BY = {r["actor"]: r for r in ROSTER} | {r["consumer"]: r for r in ROSTER}


def names(key="actor", order=None):
    """{actor or consumer: name}, in roster order or the given order of keys."""
    rows = ROSTER if order is None else [_BY[k] for k in order]
    return {r[key]: r["name"] for r in rows}


ACTORS = names()
CONSUMER_ACTOR = {r["consumer"]: r["actor"] for r in ROSTER if r["consumer"] != r["actor"]}
ACTOR_QQ = {r["actor"]: r["qq"] for r in ROSTER}
ALIASES = {r["actor"]: (r["name"], *r["account"], *r["former"], *r["nicknames"]) for r in ROSTER}
# The names a QQ line may be signed with besides her name -> her name (not nicknames: a human may use those)
SIGNED_AS = {n: r["name"] for r in ROSTER for n in (*r["account"], *r["former"])}
