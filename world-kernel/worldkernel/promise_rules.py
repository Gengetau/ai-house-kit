"""deterministic conditions and evidence judge. No model, no database writes."""
import itertools
import re

from . import vocab_life1 as _V
BOTS = frozenset(_V.ROSTER)
DEFAULT_NAMES = ("奈尔", "艾莎", "阿尔玛", "米娜", "由纪", "婉妲", "房主")  # the default pack's people and the owner
KINDS = {"arrived", "activity", "purchased", "brought_home", "handoff"}
NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
UNITS = r"[份包袋瓶个块杯盒]"
NUMBER = r"(?:[1-6一两二三四五六])"
TOGETHER_S = 600
# When a promise is made for the owner, the model's summary may call the owner by the chat nickname instead of
# 房主. Only the shape (替|帮|给)<short name>(买|带) is read that way, and only when the caller says the beneficiary
# is the owner. These are not names.
_NOT_A_NAME = re.compile(r"自己|忙|大家|别人|我们|你们|她们|他们|一起|顺便|顺路")
_ALIAS = re.compile(r"(?P<verb>替|帮|给)(?P<name>[^\s，。！？,.!?：:（）()买带]{1,4}?)(?=买|带)")
# Time words that may stand alone inside a note in brackets, such as （11月21日）or (周六 下午3点).
_DATE = r"(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{4}-\d\d-\d\d|今天|明天|大后天|后天|今晚|明晚|当天|下午|上午|晚上|早上|周末|" \
        r"(?:周|星期)[一二三四五六日天]|[一二三四五六七八九十\d]+点半?|\d{1,2}[:：]\d\d"
_DATE_NOTE = re.compile(r"[（(]\s*(?:" + _DATE + r")(?:[\s,，、]*(?:" + _DATE + r"))*\s*[）)]")


def compile_terms(text, vocab, party=None, beneficiary=None):
    """Closed grammar: unrecognised residue blocks automatic completion, never drops a clause. The names and
    the housemates it knows are those of the default pack; `bound(vocab)` gives the same for another pack.
    `beneficiary` (optional, e.g. ["owner"]) is who the promise is for; only then may the owner's chat nickname
    in `替<nickname>买…` count as the owner."""
    return _compile_terms(text, vocab, party, DEFAULT_NAMES, beneficiary)


def _compile_terms(text, vocab, party, names, beneficiary=None):
    from .inventory import RULES
    t = str(text)
    people = len(party) if party is not None else sum(name in t for name in names)
    for name in sorted(list(vocab.PEOPLE), key=len, reverse=True):
        t = t.replace(name, "")
    # Canonical names in person files need not be called 'name'.
    for name in names:
        t = t.replace(name, "")
    t = _DATE_NOTE.sub("", t)
    t = re.sub(r"(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{4}-\d\d-\d\d(?: \d\d:\d\d)?|今天|明天|大后天|后天|今晚|明晚|当天|下午|上午|"
               r"晚上|早上|周末|(?:周|星期)[一二三四五六日天]|[一二三四五六七八九十\d]+点(?:半)?|下次|改天|有空时|"
               r"出门的时候|出门时|出门|出去的时候|出去时|等下|等会儿|等会|待会儿|待会|一会儿", "", t)
    if beneficiary is not None and list(beneficiary) == ["owner"]:
        alias = _ALIAS.search(t)
        if alias and not _NOT_A_NAME.search(alias.group("name")):
            t = t[:alias.start("name")] + t[alias.end("name"):]
    # A pronoun right after 帮/替/给/答应 is the person it is done for; anywhere else it stays residue.
    t = re.sub(r"(帮|替|给|答应)[他你]", r"\1", t)
    t = re.sub(r"我们|我|她|打算|准备|答应|约好|说好|帮|替|给|要|会|再|顺便|顺路|去一趟|一下|吧|呢", "", t)
    terms = []
    handoff = bool(re.search(r"交给|送给|一起吃|拿给", str(text)))
    # Remove delivery clause only after recording the unsupported condition.
    if handoff:
        terms.append({"kind": "handoff"})
        t = re.sub(r"(?:交|送|拿)(?:到)?(?:手里|给)?|吃", "", t)
    brought = bool(re.search(r"带回(?:来|家)?|拿回(?:来|家)?", t))
    t = re.sub(r"带回(?:来|家)?|拿回(?:来|家)?", "", t)
    # `带<item>回来` without 买: bringing a shop item home means buying it (the judge still needs both events).
    # The words in between stay in the text, so anything that is not an ingredient is residue.
    bring = None if brought or re.search(r"买到?|采购|补买", t) else re.search(r"带(?P<what>[^带]+?)回(?:来|家)", t)
    if bring:
        brought = True
        t = t[:bring.start()] + bring.group("what") + t[bring.end():]
    aliases = {"park": "公园", "bookshop": "书店", "cafe": "咖啡店", "shrine": "神社",
               "library": "图书馆", "supermarket": "超市"}
    locations = []
    for pid, spec in vocab.PLACES.items():
        labels = [pid, spec.get("label", ""), spec.get("name", ""), aliases.get(pid, "")]
        for label in sorted(filter(None, labels), key=len, reverse=True):
            token = re.search(r"(?:去|到)" + re.escape(label), t)
            if token:
                terms.append({"kind": "arrived", "place": "place:" + pid})
                locations.append("place:" + pid)
                t = t[:token.start()] + t[token.end():]
                break
    for room, label in {"yard":"院子", "living":"客厅", "study":"书房", "kitchen":"厨房", "bath":"浴室"}.items():
        token = re.search(r"(?:去|到|在)" + label, t)
        if token and room in vocab.LOCATIONS:
            locations.append(room)
            t = t[:token.start()] + t[token.end():]
    buy = re.search(r"买到?|采购|补买", t)
    if buy or bring:
        if buy:
            t = t[:buy.start()] + t[buy.end():]
        aliases = {}
        for item, words in RULES:
            if item in vocab.INGREDIENTS and item != "other_snack":
                for word in words:
                    aliases[word] = item
        for item, spec in vocab.INGREDIENTS.items():
            if item != "other_snack":
                aliases[item] = item
                aliases[spec.get("label", item)] = item
        found = []
        for word in sorted(aliases, key=len, reverse=True):
            pat = r"(?:(?P<n>" + NUMBER + r")" + UNITS + r"?)?" + re.escape(word)
            m = re.search(pat, t)
            if not m:
                continue
            if m.start() and t[m.start() - 1] in "做煮烧炒烤炖熬":
                return [{"kind": "unsupported"}]  # `做咖喱` is cooking (another action), not buying curry roux
            number = m.group("n") or "1"
            n = int(number) if number.isdigit() else NUM[number]
            item = aliases[word]
            found.append({"kind": "purchased", "item": item, "qty": n})
            t = t[:m.start()] + t[m.end():]
        terms += found
        if brought:
            terms += [dict(x, kind="brought_home") for x in found]
        if not found:
            return [{"kind": "unsupported"}]
    elif brought:
        return [{"kind": "unsupported"}]
    # Resolve shared labels in their location/participant context before consuming
    # words. No external destination means home variants, not an unmentioned cinema.
    t = t.replace("一起", "")
    def words(aid, spec):
        label = re.sub(r"[（(].*?[）)]", "", spec.get("label", "")).strip().removeprefix("一起")
        return (label, aid)
    def rank(entry):
        aid, spec = entry
        group = spec.get("party") or {}
        return (-len(words(aid, spec)[0]),
                0 if (people > 1) == (group.get("min", 1) > 1) else 1, aid)
    activity_count = 0
    for aid, spec in sorted(vocab.ACTIVITIES.items(), key=rank):
        where = spec.get("where", ())
        if locations and not all(loc in where for loc in locations):
            continue
        if not locations and not any(loc in vocab.LOCATIONS and vocab.LOCATIONS[loc]["kind"] == "room"
                                     or loc == "own_room" for loc in where):
            continue
        for word in words(aid, spec):
            if word and word in t:
                term = {"kind": "activity", "activity": aid, "min_s": min(600, spec["min"])}
                if locations:
                    term["place"] = locations[0]
                terms.append(term)
                activity_count += 1
                t = t.replace(word, "", 1)
                break
    if any(not loc.startswith("place:") for loc in locations) and not activity_count:
        return [{"kind":"unsupported"}]
    t = re.sub(r"买|去|到|做|一起|和|与|、|及|然后|再|并且|回来|带|回家|[，。！？,.!?：:\s]", "", t)
    if t or not terms or len(terms) > 12:
        return [{"kind": "unsupported"}]
    return terms


def _overlap(rows, seconds):
    """One real interval per actor; require a shared interval, not only a chain of overlaps."""
    for chosen in itertools.product(*rows):
        places = {e.get("place") for e in chosen}
        if len({e.get("plan_id") for e in chosen}) == 1 and len(places) == 1 and None not in places and min(e["ended_at"] for e in chosen) - max(
                e["started_at"] for e in chosen) >= seconds:
            return list(chosen)
    return []


def judge(intent, evidence, now):
    """Cross-trip quantities accumulate only from trips proving the other conditions too."""
    return _judge_all(intent, evidence, now, BOTS)


def _judge_all(intent, evidence, now, bots):
    terms = intent.get("terms", [])
    other = [t for t in terms if t.get("kind") not in ("purchased", "brought_home")]
    if other and len(other) != len(terms):
        eligible, support = set(), []
        for plan in {e.get("plan_id") for e in evidence if e.get("plan_id")}:
            result = _judge(dict(intent, terms=other),
                            [e for e in evidence if e.get("plan_id") == plan], now, bots)
            if result["complete"]:
                eligible.add(plan)
                support.extend(result["keys"])
        evidence = [e for e in evidence if e.get("plan_id") in eligible]
        result = _judge(intent, evidence, now, bots)
        if result["complete"]:
            result["keys"] = sorted(set(result["keys"] + support))
        return result
    return _judge(intent, evidence, now, bots)


def _judge(intent, evidence, now, bots=BOTS):
    """All terms AND; returns complete + progress keys. plan_finished never satisfies a term."""
    party = intent.get("party", [])
    terms = intent.get("terms", [])
    result = {"complete": False, "keys": [], "progress": []}
    if (intent.get("status") != "accepted" or intent.get("scope", "world") != "world"
            or not party or not set(party) <= bots or not terms
            or any(x.get("kind") not in KINDS for x in terms)):
        return result
    lower = max(intent.get("said_at", intent["created"]), (intent.get("due_from") or intent["created"]) - 7200)
    upper = min(intent.get("deadline", now), now)
    ev = {e["key"]: e for e in evidence if lower <= e["at"] <= upper
          and e.get("actor") in party and (not e.get("intent_ref") or e["intent_ref"] == intent["id"])
          and (not intent.get("plan_ref") or e.get("plan_id") == intent["plan_ref"])
          and (not e.get("fingerprint") or e["fingerprint"] == intent.get("fingerprint"))}
    used, progress = set(), []
    satisfied = True
    for term in terms:
        kind = term["kind"]
        candidates = [e for e in ev.values() if e["kind"] == kind]
        if kind == "handoff":
            satisfied = False
            continue
        if kind in ("purchased", "brought_home"):
            got_all = True
            for actor in party:
                rows = [e for e in candidates if e.get("item") == term["item"] and e["actor"] == actor
                        and e.get("intent_ref") == intent["id"] and e.get("account") == actor
                        and sorted(e.get("beneficiary") or []) == sorted(intent.get("beneficiary") or [])]
                # One physical purchase per action/resource; delivery receipts never increase purchased qty.
                unique = {(e.get("action_id"), e.get("resource_id")): e for e in rows
                          if e.get("action_id") and e.get("resource_id")}
                rows = list(unique.values())
                if kind == "brought_home":
                    purchases = {(e.get("action_id"), e.get("resource_id")) for e in ev.values()
                                 if e["kind"] == "purchased" and e.get("intent_ref") == intent["id"]
                                 and e.get("item") == term["item"] and e["actor"] == actor}
                    rows = [e for e in rows if (e["action_id"], e["resource_id"]) in purchases]
                got = sum(e.get("qty", 0) for e in rows)
                progress.append({"kind": kind, "actor": actor, "item": term["item"], "got": got, "qty": term["qty"]})
                used.update(e["key"] for e in rows)
                got_all &= got >= term["qty"]
            satisfied &= got_all
        elif kind == "activity":
            by_actor = [[e for e in candidates if e["actor"] == a and e.get("activity") == term["activity"]
                         and (not term.get("place") or e.get("place") == term["place"])
                         and e.get("outcome") in ("succeeded", "cancelled")
                         and e.get("started_at") is not None and e.get("ended_at") is not None
                         and e["started_at"] >= lower and e["ended_at"] <= upper]
                        for a in party]
            chosen = _overlap(by_actor, TOGETHER_S if len(party) > 1 else term.get("min_s", 600))
            progress.extend({"kind": kind, "actor": actor, "activity": term["activity"],
                             "done": any(e["ended_at"]-e["started_at"] >= term.get("min_s",600) for e in rows)}
                            for actor, rows in zip(party,by_actor))
            used.update(e["key"] for e in chosen)
            satisfied &= bool(chosen)
        elif kind == "arrived":
            by_actor = [[e for e in candidates if e["actor"] == a and e.get("place") == term["place"]]
                        for a in party]
            progress.extend({"kind": kind, "actor": actor, "place": term["place"], "done": bool(rows)}
                            for actor,rows in zip(party,by_actor))
            if len(party) > 1:
                intervals = []
                for rows in by_actor:
                    current = []
                    for e in rows:
                        ends = [x for x in ev.values() if x["kind"] == "departed" and x["actor"] == e["actor"]
                                and x.get("plan_id") == e.get("plan_id") and x["at"] >= e["at"]]
                        if ends:
                            end = min(ends, key=lambda x: x["at"])
                            current.append(dict(e, started_at=e["at"], ended_at=end["at"], end_key=end["key"]))
                    intervals.append(current)
                chosen = _overlap(intervals, TOGETHER_S)
                used.update(e["end_key"] for e in chosen)
            else:
                chosen = by_actor[0][:1]
            used.update(e["key"] for e in chosen)
            satisfied &= bool(chosen)
    result.update(complete=bool(satisfied), keys=sorted(used), progress=progress)
    return result


class _Bound:
    """The two rules bound to one pack: its housemates may promise, its names are not residue."""

    def __init__(self, vocab):
        self.vocab = vocab
        self.bots = frozenset(vocab.ROSTER)
        self.names = tuple(str(n) for n in vocab.NAMES.values()) + ("房主",)

    def compile_terms(self, text, vocab=None, party=None, beneficiary=None):
        return _compile_terms(text, self.vocab if vocab is None else vocab, party, self.names, beneficiary)

    def judge(self, intent, evidence, now):
        return _judge_all(intent, evidence, now, self.bots)


def bound(vocab):
    """-> an object with `compile_terms(text, vocab=None, party=None, beneficiary=None)` and `judge(intent, evidence, now)` for the
    pack `vocab` (a vocabulary namespace: ROSTER, NAMES, PLACES, ...). The module-level functions stay the default
    pack's."""
    return _Bound(vocab)
