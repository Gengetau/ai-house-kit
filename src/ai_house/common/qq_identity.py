"""身份按号码 (#79 phase 2 / audit A-05, 2026-11-04): who said a rendered QQ line, told by its QQ number only.

Every bridge renders a message as "[名字|QQ号] [管理员]|[bot] (message_id=…): 正文". Phase 1 (#80) made the bridges
mark a stranger who wears a housemate's or the owner's name. This is the downstream half for world.py, memory.py,
intents.py and the shared judge: WHO comes from the number and roster.py, the card is only shown. A number nobody
knows stays someone else whatever the card says, "(你)" counts only on her own number, and a name alone never makes
anyone the owner or a housemate.

The header is the bridge's own: with MSG_STRUCTURED (all six bots since 10-28) NAME_SANITIZE keeps [ ] | out of
names, so the first "]" closes the header and the number in it is the sender's.
"""

import json
import re

if __package__:  # inside a package (the open-source kit) the roster lives beside this file
    from . import roster as R
else:
    import roster as R

OWNER_QQ = int(R.OWNER["qq"])
QQ_ACTOR = {int(r["qq"]): r["actor"] for r in R.ROSTER}
ACTOR_QQ = {r["actor"]: int(r["qq"]) for r in R.ROSTER}
NAMES = {r["actor"]: r["name"] for r in R.ROSTER}
OWNER_LABEL = "房主"
# What the owner is called. On any other number such a card is someone else. The roster may list more names.
OWNER_ALIASES = tuple(dict.fromkeys(("房主", "owner", *R.OWNER.get("aliases", ()))))

_FULL = re.compile(r"^\[([^\[\]|\r\n]+)\|(\d{1,15})\]((?: \[(?:管理员|bot)\])*) \(message_id=[^)\r\n]*\):(?: |$)")
# Hand-made lines without the message_id tail (fixtures, old notes). Never read from a line that has a message_id
# anywhere: a card that smuggled "|number]" in front of the real header must not pass for that number.
_SHORT = re.compile(r"^\[([^\[\]|\r\n]+)\|(\d{1,15})\](?: |$)")
_SELF_MARK = re.compile(r"[（(]\s*你\s*[)）]")
_TITLE = re.compile(r"·头衔.*$")
_NOTE = re.compile(r"（同名的另一个人，不是[^（()）]*）")
# the bridge's note (and a model's copy of it, maybe with ASCII parentheses): who the person is NOT. The name in it
# never spans another parenthesis (R79-04): an open fake note in a card cannot swallow the bridge's note after it.
_NOTE_WHO = re.compile(r"[（(]同名的另一个人，不是(?:小屋的)?([^（()）]*)[）)]")
_FENCE = re.compile(r"^⟦([0-9a-f]{6})⟧(.*)⟦/\1⟧$", re.S)


def _key(name):
    """A name for comparing: no title, no (…) labels (nested or full-width), no spaces, case folded."""
    name = _TITLE.sub("", str(name or ""))
    while True:
        stripped = re.sub(r"[（(][^()（）]*[)）]", "", name)
        if stripped == name:
            return re.sub(r"[\s()（）]+", "", stripped).casefold()
        name = stripped


_WEARS = {}  # name key -> the housemate's actor key, or "owner"
for _r in R.ROSTER:
    for _n in (_r["name"], *_r["account"], *_r["former"], *_r["nicknames"]):
        _WEARS.setdefault(_key(_n), _r["actor"])
for _n in OWNER_ALIASES:
    _WEARS.setdefault(_key(_n), "owner")
_WEARS.pop("", None)


def parse(line):
    """(card, uid, tags, raw words) of a bridge-rendered line, or None when it does not open with a sender header."""
    s = str(line or "")
    m = _FULL.match(s)
    if m:
        return m.group(1).strip(), int(m.group(2)), tuple(re.findall(r"\[(管理员|bot)\]", m.group(3))), s[m.end():]
    if "(message_id=" in s:
        return None
    m = _SHORT.match(s)
    return (m.group(1).strip(), int(m.group(2)), (), s[m.end():]) if m else None


def who(uid, me=None):
    """"self" (me's own number), "owner", a housemate's actor key, or None for anyone else."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    if me is not None and uid == ACTOR_QQ.get(me):
        return "self"
    if uid == OWNER_QQ:
        return "owner"
    return QQ_ACTOR.get(uid)


def speaker(line, me=None):
    """who() of the line's sender; None also when the line has no header."""
    p = parse(line)
    return who(p[1], me) if p else None


def actor_of(consumer):
    """The roster actor key of a app-services consumer (wanda_c is wanda), or None when she is not in the roster."""
    a = R.CONSUMER_ACTOR.get(consumer, consumer)
    return a if a in ACTOR_QQ else None


def is_self(line, me):
    """Did she say this herself: her own QQ number, whatever the card says. For someone not in the roster there is
    no number to check, and the bridge's "(你)" is all there is."""
    p = parse(line)
    if not p:
        return False
    if me in ACTOR_QQ:
        return p[1] == ACTOR_QQ[me]
    return _TITLE.sub("", p[0]).endswith("(你)")


def name_of(card):
    """What the card says, without the title, "(你)" and the bridge's same-name note."""
    return _NOTE.sub("", _SELF_MARK.sub("", _TITLE.sub("", str(card or "")))).strip()


def named(card):
    """The housemate's actor key, or "owner", whose name the card itself shows (title, (…) labels and notes left
    out); None when it is nobody's. A name only, never who someone is: that is the number."""
    return _WEARS.get(_key(card))


def noted(card):
    """Every housemate (actor key) and "owner" that a same-name note on this text says the person is NOT; empty
    without a note. Every note counts, not just the first (R79-04): a card can bring a fake note of its own in front
    of the one the bridge appends. Only ever used to refuse, so a note never makes anyone the owner or a housemate."""
    return {_WEARS.get(_key(m.group(1))) for m in _NOTE_WHO.finditer(str(card or ""))} - {None}


def wearing(card):
    """The housemates (actor keys) and "owner" whose name this card shows, itself or in any same-name note on it;
    empty when none. R79-01: a card 「房主」 arrives as 「·（同名的另一个人，不是房主）」. R79-04: 「房主(同名的另一个人，
    不是米娜)」 arrives as 「·(同名的另一个人，不是米娜)（同名的另一个人，不是房主）」, and both names count. Only ever
    used to say someone ELSE wears a name."""
    return ({named(card)} | noted(card)) - {None}


def unnamed(text, actor=None, names=()):
    """The text without the same-name notes. Where a note says the person is not `actor`, the name in front of it goes
    too: from where one of her `names` starts (a title, (…) and spaces may follow it) up to the note. What is left
    names her only where she is really meant (#79 R79-03: a picture prompt never gives her look to a namesake).
    Notes right after one another (only spaces between) belong to one name (R79-04: a card's own fake note can sit
    between the name and the bridge's note)."""
    s, out, last = str(text or ""), [], 0
    keys = {_key(n) for n in names} - {""}
    runs = []  # [start, end, everyone the notes say the person is not], one per name
    for m in _NOTE_WHO.finditer(s):
        if runs and not s[runs[-1][1]:m.start()].strip():
            runs[-1][1:] = [m.end(), runs[-1][2] | noted(m.group(0))]
        else:
            runs.append([m.start(), m.end(), noted(m.group(0))])
    for start, end, not_ in runs:
        head = s[last:start]
        cut = len(head)
        if actor is not None and actor in not_:
            for i in range(len(head) - 1, max(-1, len(head) - 41), -1):  # cards are at most 24 characters
                if _key(head[i:]) in keys:
                    cut = i
        out.append(head[:cut])
        last = end
    out.append(s[last:])
    return " ".join(out)


def is_owner_name(name):
    return "owner" in wearing(name)


def display(card, uid=None):
    """Someone else's card in a summary, with the bridge's note when it shows a housemate's or the owner's name.
    Every note already on the card is kept: NAME_SANITIZE turns a card 「房主」 into 「·」 before the bridge notes it,
    and a card can carry a note of its own in front of the bridge's (R79-04)."""
    shown = name_of(card) or (str(uid) if uid else "?")
    notes = _NOTE.findall(str(card or ""))
    hit = named(shown)
    if hit and hit not in noted(card):
        notes.append("（同名的另一个人，不是房主）" if hit == "owner" else f"（同名的另一个人，不是小屋的{NAMES[hit]}）")
    return shown + "".join(notes)


def label(line, me=None, self_label="（她自己）"):
    """How a summary names the sender, by number: her own lines self_label, the owner and the housemates their fixed
    names, anyone else the card (noted by display()). None when the line has no header."""
    p = parse(line)
    if not p:
        return None
    if me is not None and is_self(line, me):
        return self_label
    k = who(p[1])
    if k == "owner":
        return OWNER_LABEL
    if k:
        return NAMES[k]
    return display(p[0], p[1])


def body(raw):
    """The words after the header: a JSON string (MSG_STRUCTURED=json) decoded, a fence or the "┆" lines undone."""
    s = str(raw or "")
    if s.startswith('"'):
        for t in (s, s + '"'):  # observe keeps 400 characters: the closing quote may be cut off
            try:
                v = json.loads(t)
            except ValueError:
                continue
            if isinstance(v, str):
                return v
        return s
    m = _FENCE.match(s)
    return (m.group(2) if m else s).replace("\n  ┆ ", "\n")


def short(line, me=None, self_label="（她自己）"):
    """"who: words" of one line for a summary; the line as it is when it has no header."""
    p = parse(line)
    return f"{label(line, me, self_label)}: {body(p[3])}" if p else str(line or "")


def verified(line, me=None):
    """The line with only its header's card told by number: "(你)" stays only on her own number (and is added there),
    and anyone else showing a housemate's or the owner's name gets the same-name note. Number, tags and words are
    left alone; the owner's and the housemates' cards too."""
    s = str(line or "")
    m = _FULL.match(s) or (None if "(message_id=" in s else _SHORT.match(s))
    if not m:
        return s
    card, uid = m.group(1), int(m.group(2))
    if me is not None and is_self(s, me):
        bare = _TITLE.sub("", card)
        new = card if bare.endswith("(你)") else bare.rstrip() + "(你)" + card[len(bare):]
    elif who(uid) is None and wearing(card):
        title = _TITLE.search(card)
        new = display(card, uid) + (title.group(0) if title else "")
    else:
        new = _SELF_MARK.sub("", card).strip() or str(uid)
    return s[:m.start(1)] + new + s[m.end(1):]


def worn(lines):
    """Housemates (actor keys) and "owner" whose name someone ELSE shows in these lines: there a name alone cannot
    say who was meant."""
    out = set()
    for line in lines:
        p = parse(line)
        if p:
            out |= {hit for hit in wearing(p[0]) if who(p[1]) != hit}
    return out
