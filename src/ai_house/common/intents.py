"""意图与承诺 V1 (2026-10-20, owner; docs/intents-v1.md): what a housemate means to do, alone or with someone.

Found in the memory extraction she already runs (no extra model call): the model proposes intent / intent_done /
intent_drop ops with a quote, the host checks the quote is in that batch and who said it, and decides. A joint
plan needs everyone in it to have spoken; a plan only the owner made is not tracked (the world never keeps the
owner's books). Shown, when INTENTS_SHOW=1, as at most 3 lines on her 此刻 card and as the due ones in the world
prompt; done only with evidence (a quote in chat, a validated world scene, the console); quietly expires.
Lives in world.db; world.py and memory.py reach it through their INTENTS hook (set by server.py)."""

import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import sys
from pathlib import Path

if __package__:  # inside a package (the open-source kit) the two files live beside this one
    from . import world_shim as W, qq_identity as ID
else:
    import world as W
    import qq_identity as ID  # #79 phase 2 / A-05 (2026-11-04): who said a line, by its QQ number only

log = logging.getLogger("intents")

ON = os.environ.get("INTENTS", "0") == "1"
SHOW = os.environ.get("INTENTS_SHOW", "0") == "1"
EVIDENCE_GATE = os.environ.get("INTENTS_EVIDENCE_GATE", "1") == "1"
PRESENCE_ON = os.environ.get("INTENTS_PRESENCE", "1") == "1"  # infer the owner's room from chat; off: nothing inferred or queued
OPEN = ("proposed", "accepted")
ENDED = ("completed", "declined", "expired", "cancelled", "missed")
MAX_OPEN = 20  # per housemate, open intents she is in
PER_DAY = 8  # new ones she starts a day
LATE_S = 12 * 3600  # after the window closes
IDLE_S = 7 * 86400  # no time given and nothing heard of it
KEEP_S = 30 * 86400  # ended ones stay this long (they can be restored)
SOON_S = 3 * 3600  # the card shows what is due within this
OWNER = W.R.OWNER

SCHEMA = """
create table if not exists intents(
    id integer primary key, actor text not null, party text not null, agreed text not null,
    kind text not null, text text not null, due_from real, due_to real, status text not null,
    source text not null, refs text not null, visibility text not null, confidence real not null,
    nudged integer not null default 0, created real not null, updated real not null, last_action real not null);
create index if not exists intents_status on intents(status, actor);
"""

PROMPT = """

另外整理「意图与承诺」（她自己真的打算做、或和别人说好要一起做的事），用下面三种 op，没有就不写：
- {{"op":"intent","text":"一句话，写名字，不超过 40 字","kind":"plan|promise|joint","with":["一起的人的名字"],"agreed":["明确说了好/答应了的人的名字"],"due":"YYYY-MM-DD HH:MM 或 YYYY-MM-DD 或空","quote":"聊天里提出或答应这件事的那句原话（原样摘一段）"}}
  plan=「{me}」自己的打算；promise=「{me}」答应了别人要做的事；joint=和别人约好一起做。
- {{"op":"intent_done","id":编号,"quote":"说明已经做了的那句原话"}}
- {{"op":"intent_drop","id":编号,"why":"declined|cancelled","quote":"说明不做了/被婉拒的那句原话"}}
要求：
- 只记「{me}」自己要做、或「{me}」参与的事。房主自己答应的事、房主的计划不记成意图（照常记进记忆就好）。
- 必须是认真的打算：玩笑、反话、客套（“改天一起吃饭啊”没人接话）、假设、游戏里的事都不算。
- quote 必须是聊天记录里真实出现过的原话片段，不要改写。agreed 只包含明确接受、同意或承诺的人；沉默、复读、转移话题、客套、模糊回答、疑问句、附条件且条件未成立都不是同意。拿不准就别写进 agreed（会保持「提议中」），不要推断。
- 「已有的意图」里有的不要重复写 intent。只有当事人明确说已经做完了，才写 intent_done；问“做完了吗”、说“快做完了”“应该好了”、时间到了，都不算完成。不做了、被婉拒用 intent_drop。
- 日期按聊天开头的日本时间换算成具体日期；没说时间就留空。
"""

SCHEMA_NOTE = (',"intents_done":[{"id":惦记的事的编号,"beat":序号}]（只在这段剧本里真的把那件事做了时写，'
               '没做就空数组）')


def init():
    with W.db() as c:
        c.executescript(SCHEMA)
        for name, spec in (("beneficiary", "text not null default '[]'"),
                           ("scope", "text not null default 'world'"),
                           ("when_kind", "text not null default ''"), ("plan_ref", "text")):
            if name not in {r[1] for r in c.execute("pragma table_info(intents)")}:
                try:
                    c.execute(f"alter table intents add column {name} {spec}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        c.executescript(KERNEL_SCHEMA)


def _party_key(who, worn=frozenset()):
    """A world key or 'owner' from a name the model wrote in with/agreed; None for anyone else.
    A-05: a name is only a name. One the bridge marked 「同名的另一个人」 is someone else; and when someone else in
    this batch shows a housemate's or the owner's name, that name cannot say who was meant: the op is refused
    (ValueError) instead of guessing the owner or the housemate."""
    raw = str(who or "")
    if "同名的另一个人" in raw:
        return None
    who = re.sub(r"\(你\)|（你）|·头衔.*$", "", raw).strip()
    key = "owner" if ID.is_owner_name(who) else W._key(who)
    if key and key in worn:
        raise ValueError("someone else in this chat wears that name")
    return key


def _speakers(lines):
    """(who, words) per line, in order. A-05: who comes from the QQ number only ('owner', a housemate's key, or None
    for anyone else whatever the card says; no fallback to the card). Words are the message itself, without the
    bridge's header and with a JSON body (MSG_STRUCTURED=json) decoded: the evidence checks read what was said."""
    out = []
    for line in lines:
        p = ID.parse(line)
        if not p:
            out.append((None, line or ""))
            continue
        out.append((ID.who(p[1]), ID.body(p[3])))
    return out


def _norm(t):
    return re.sub(r"[\s，。！？、,.!?~…「」“”\"'：:（）()]+", "", str(t or ""))


def _quoted(quote, spoken):
    """Who said the line the quote comes from, or None when it is not in this batch."""
    q = _norm(quote)
    if len(q) < 4:
        return None
    for who, text in spoken:
        if who and q in _norm(text):
            return who
    return None


# 2026-10-20 婉妲's review (#4): the model's word is not enough for "she agreed" or "it is done"; the lines must say so.
YES = re.compile(r"好[呀啊的吧哒嘞耶滴]?|行[啊呀吧]?|可以|没问题|一起|走[吧起]|去[吧呀啊]|当然|嗯嗯|ok|OK|算我一个|我也去|答应|说定|定了|就这么|约好|安排上")
NO = re.compile(r"不行|不了|不去|没空|算了|改天吧|再说吧|下次吧|不要|不想|去不了|来不了|不约|取消|那就算|没说好|还没说|"
                r"没答应|不确定|再看看|看情况")
FICTION = re.compile(r"梦里|梦见|做梦|假如|假设|如果|要是|想象|幻想|游戏里|剧本里")
ASKING = re.compile(r"[?？]\s*$|吗\s*$|吗[?？。！!～~]*$|了没|好了没|没有\s*$")
NOT_YET = re.compile(r"应该|快(?!乐|递|点)|准备|打算|待会|等会|一会|马上|还没|没做|没买|会去|要去")
DONE = re.compile(r"了|完|过|好啦|搞定|回来")

# The legacy batch has no authenticated reply IDs or per-message times. Accept only a direct
# adjacent reply with an unambiguous affirmative, or an explicit quotation of the proposal.
# Unknown relations stay proposed. These lexical checks do not claim general semantic entailment.
UNREAL = re.compile(r"玩笑|反话|才怪|差不多|大概|也许|可能|似乎|好像|算是|未必|不一定|了吧")
REPORTED = re.compile(r"(?:她|他|别人|有人).{0,4}(?:说|答应|同意)|(?:说|问|表示)[：:「“\"']")
DENIED_DONE = re.compile(r"没有|没买|没做|没完成|未完成|未买|未做|并不|并未|不是|不曾|尚未")
NO_EVIDENCE = re.compile(r"不参加|不同意|不答应|不愿意|不能|不会|别算我|不是我|再考虑|再想想")
YES_START = re.compile(r"^(?:好[呀啊的吧哒嘞耶滴]?|行[啊呀吧]?|可以|没问题|当然|嗯嗯|[oO][kK]|"
                       r"算我一个|我也去|我参加|答应|说定|定了|就这么|约好|安排上|一起|走[吧起])"
                       r"(?=$|[\s，。！、,.!～~]|我|明|后|周|下|三|四|五|六|七|八|九|十)")
# A greeting before the yes is not part of the answer; a later question that doubts the deal still is.
GREETING = re.compile(r"(?:(?:早上好|早安|上午好|中午好|下午好|晚上好|晚安|你好)|早(?=[\s，。！、,.!～~呀啊哦]|$))[\s，。！、,.!～~呀啊哦]*")
DOUBT = re.compile(r"确定|真的|当真|非要|一定要|还是算了|要不算了")
SENTENCE = re.compile(r"[^。！!？?\n]*(?:[。！!？?\n]|$)")


def _unreal(text):
    return bool(FICTION.search(text) or UNREAL.search(text) or REPORTED.search(text)
                or any(re.search(re.escape(name) + r'(?:说|答应|同意)', text) for name in W.ACTORS.values()))


def _topic_bits(text):
    """Conservative lexical link: ignore names, times and generic intention/affirmation words."""
    t = str(text)
    for name in sorted([*W.ACTORS.values(), *W.ACTORS, OWNER['name'], '房主'], key=len, reverse=True):
        t = t.replace(name, ' ')
    t = re.sub(r"明天|后天|今天|今晚|明晚|下午|晚上|早上|周末|一起|我们|你们|她们|打算|准备|"
               r"答应|约好|说好|已经|真的|完成|好了|好呀|可以|没问题|[一二三四五六七八九十0-9]+[点时分月日号]|"
               r"周[一二三四五六日天]", ' ', t)
    return {s[i:i + 2] for s in re.findall(r"[\u4e00-\u9fffA-Za-z]+", t) for i in range(len(s) - 1)}


def _same_topic(a, b):
    return bool(_topic_bits(a) & _topic_bits(b))


def _bound_quote(quote, spoken, proposal=False, own_plan=None):
    """Return one evidence position, not a speaker from one match and text from another."""
    q = _norm(quote)
    hits = [i for i, (who, text) in enumerate(spoken) if who and len(q) >= 4 and q in _norm(text)]
    if proposal:
        hits = [i for i in hits if q in _norm(re.sub(r'[「“\"]([^」”\"]+)[」”\"]', '', spoken[i][1]))]
    if own_plan:
        hits = [i for i in hits if spoken[i][0] == own_plan]
    # A direct affirmative may repeat the quoted words. Anchor to the preceding proposal,
    # not to that reply; any other multiple match remains ambiguous.
    if proposal and len(hits) == 2 and hits[1] == hits[0] + 1 and YES_START.match(spoken[hits[1]][1].strip()):
        hits = hits[:1]
    if len(hits) != 1:
        raise ValueError('missing or ambiguous quote')
    return hits[0]


def _bound_yes(who, quote_at, spoken):
    proposer, proposal = spoken[quote_at]
    if who == proposer:
        return True

    def greet_off(t):
        t = t.strip()
        m = GREETING.match(t)
        while m and m.end():
            t = t[m.end():]
            m = GREETING.match(t)
        return t

    def side_questions_off(text):
        """After a plain first-sentence yes, drop later questions that are about something else."""
        rest = greet_off(text)
        sents = [x for x in SENTENCE.findall(rest) if x.strip()]
        if len(sents) < 2 or re.search(r'[「“"]', text) or re.search(r'[?？]', sents[0]) \
                or not YES_START.match(sents[0].strip()):
            return text
        # "要不要" is an offer, not a refusal; any other 不 in a question keeps it.
        keep = [x for x in sents[1:] if not re.search(r'[?？]', x) or _same_topic(proposal, x) or DOUBT.search(x)
                or '不' in x.replace('要不要', '') or NO.search(x.replace('要不要', ''))
                or NO_EVIDENCE.search(x) or _unreal(x)]
        return text[:len(text) - len(rest)] + sents[0] + ''.join(keep)

    for i, (speaker, text) in enumerate(spoken):
        if speaker != who or i <= quote_at:
            continue
        text = side_questions_off(text)
        if _unreal(text) or NO.search(text) or NO_EVIDENCE.search(text):
            continue
        # A later, explicit quote of this particular proposal can identify the reply without time metadata.
        cited = re.findall(r'[「“\"]([^」”\"]+)[」”\"]', text)
        cites_proposal = any(_norm(c) == _norm(proposal) for c in cited)
        reply = text
        if cites_proposal:
            for c in cited:
                reply = reply.replace(c, '')
            reply = reply.strip('「」“”\" ：:，,')
        elif i != quote_at + 1:
            continue
        if re.search(r'[?？]', reply):
            continue
        reply = greet_off(reply)
        yes = YES_START.match(reply)
        if not yes:
            continue
        first = SENTENCE.match(reply).group()
        for tail in (first[yes.end():], reply[yes.end():]):
            tail = tail.strip(' ，。！、,.!～~')
            if not tail or _same_topic(proposal, tail):
                return True
            # Object-free scheduling / participation continuation, not an unrelated affirmative sentence.
            tail = re.sub(r'^(?:我们|我|那|就)', '', tail)
            if re.fullmatch(r'(?:明天|后天|今天|周[一二三四五六日天])?[一二三四五六七八九十0-9]+点', tail):
                return True
            if re.fullmatch(r'(?:(?:明天|后天|今天|周[一二三四五六日天])?[一二三四五六七八九十0-9]*点?)?'
                            r'(?:出门|出发|见|见面|集合|去|参加|一起去)', tail):
                return True
    return False


def _agrees(text):
    t = str(text or "").strip()
    return bool(YES.search(t)) and not NO.search(t) and not ASKING.search(t)


def _said_yes(who, quote_by, spoken):
    """She proposed it (the quote is hers) or said a plain yes somewhere in this batch."""
    return who == quote_by or any(w == who and _agrees(t) for w, t in spoken)


def _quote_line(quote, spoken):
    q = _norm(quote)
    return next((t for w, t in spoken if w and len(q) >= 4 and q in _norm(t)), "")


def _sim(a, b):
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / len(ga | gb)


def _due(v, now):
    """(due_from, due_to) from 'YYYY-MM-DD HH:MM' (an hour before to two after) or 'YYYY-MM-DD' (that day)."""
    v = str(v or "").strip()
    for fmt, before, after in (("%Y-%m-%d %H:%M", 3600, 7200), ("%Y-%m-%d", None, None)):
        try:
            d = datetime.datetime.strptime(v, fmt).replace(tzinfo=W.JST)
        except ValueError:
            continue
        t = d.timestamp()
        lo, hi = (t - before, t + after) if before else (t, t + 86400)
        if hi < now - 86400 or lo > now + 60 * 86400:
            return None, None  # a slip of the model (last year, next year): kept without a time
        return lo, hi
    return None, None


def _row(r):
    return {**dict(r), "party": json.loads(r["party"]), "agreed": json.loads(r["agreed"]),
            "refs": json.loads(r["refs"])}


def _bots(party):
    return [p for p in party if p != "owner"]


def _ev(c, kind, r, **data):
    W._event(c, kind, _bots(json.loads(r["party"]) if isinstance(r["party"], str) else r["party"]),
             {"id": r["id"], **data})


def _set(c, r, now, **kw):
    kw.update(updated=now)
    c.execute(f"update intents set {', '.join(f'{k}=?' for k in kw)} where id=?", (*kw.values(), r["id"]))


def _add_ref(r, ref):
    return json.dumps((json.loads(r["refs"]) + [ref])[-6:], ensure_ascii=False)


def _same_audience(r, actor, chat, private):
    """A semantic match cannot join distinct disclosure domains (including state transitions)."""
    if not W.DM_AUDIENCE:
        return True
    if r["visibility"] != ("private" if private else "group"):
        return False
    if private:
        return r["actor"] == actor  # each bot's owner DM is a separate conversation
    refs = [f for f in json.loads(r["refs"]) if "kernel" not in f]
    if any(ref.get("visibility", "group") != "group" for ref in refs):
        return False
    chats = [ref.get("chat") for ref in refs if "chat" in ref or "script" not in ref]
    return bool(chats) and all(source == chat and str(source).startswith("group:") for source in chats)


def _expiry_deadline(c, r):
    """Use the same explicit/plan window for expiry and owner correction eligibility."""
    d = dict(r)
    v2 = os.environ.get("INTENTS_EXPIRY_V2") == "1"
    if v2 and d.get("plan_ref"):
        ended = c.execute("select min(at) from intent_evidence where json_extract(body,'$.kind')='plan_finished'"
                          " and json_extract(body,'$.plan_id')=?"
                          " and stream=(select v from kernel_bind where k='world')",
                          (d["plan_ref"],)).fetchone()[0]
        if ended is not None:
            d["plan_ended_at"] = ended
    return _deadline(d, v2)


def expire(c, now):
    """Open ones whose time has long passed, or that nobody has mentioned in a week, end quietly."""
    v2 = os.environ.get("INTENTS_EXPIRY_V2") == "1"
    rows = c.execute("select * from intents where status in ('proposed','accepted')").fetchall()
    for r in rows:
        deadline = _expiry_deadline(c, r)
        if now > deadline:
            status = "missed" if v2 and r["status"] == "accepted" and (
                r["due_to"] is not None or r["plan_ref"]) else "expired"
            _set(c, r, now, status=status)
            _ev(c, "intent_" + status, r)
    c.execute("delete from intents where status in ('completed','declined','expired','cancelled','missed') and updated<?",
              (now - KEEP_S,))


def _mine(c, actor, private=True):
    rows = c.execute("select * from intents where status in ('proposed','accepted') and party like ? order by id",
                     (f'%"{actor}"%',)).fetchall()
    return [r for r in rows if private or r["visibility"] == "group"]


def _when(r, now):
    if r["due_from"] is None:
        return ""
    lo = W.jst(r["due_from"])
    day = W._day_start(now)
    name = ("今天" if day <= r["due_from"] < day + 86400 else "明天" if day + 86400 <= r["due_from"] < day + 2 * 86400
            else f"{lo.month}月{lo.day}日")
    return name if r["due_to"] - r["due_from"] >= 86400 else f"{name} {W.jst(r['due_from'] + 3600):%H:%M}"


def _who_text(r, actor):
    others = [p for p in json.loads(r["party"]) if p != actor]
    return "、".join(OWNER["name"] if p == "owner" else W.ACTORS[p] for p in others)


def open_text(consumer, private, now=None):
    """Her open intents for the memory prompt (numbered), or '' when there are none / the feature is off."""
    if not ON:
        return ""
    now = time.time() if now is None else now
    try:
        actor = W.actor_of(consumer)
    except ValueError:
        return ""
    with W.db() as c:
        expire(c, now)
        rows = _mine(c, actor, private)
    return "\n".join(f"#{r['id']} [{'约好了' if r['status'] == 'accepted' else '提议中'}] {r['text']}"
                     + (f"（和{_who_text(r, actor)}）" if _who_text(r, actor) else "")
                     + (f"（{_when(r, now)}）" if _when(r, now) else "")
                     + ("（到时间了：聊天里说做完了就写 intent_done；说不做了就写 intent_drop）"
                        if _deadline(dict(r), True) - now < 86400 else "") for r in rows)


def prompt(me):
    if not ON:
        return ""
    extra = ""
    if os.environ.get("INTENTS_EXTRACT_V2") == "1":
        extra = ('\nintent 另写 for（替谁做的名字数组，不是共同执行者）、scope（world 或 chat）、'
                 'when（exact/day/vague/after）。只有聊天里能完成的事用 chat；after 附原条件，不推断已成立。'
                 '受益人提出请求时，必须同时有她明确答应的原话；不得把受益人算进 with。')
    return PROMPT.format(me=me) + extra


def apply_chat(consumer, chat, private, ops, lines, now=None, times=None, ids=None):
    """The intent ops of one memory batch, checked against the batch's lines. -> counts.
    times / ids: when each line was seen and its row (memory seen.ts / seen.id, same order as lines); without them
    the batch time and the line's place in the batch stand in."""
    done = {"intents": 0, "merged": 0, "ended": 0, "rejected": 0}
    now = time.time() if now is None else now
    if ON and ops:
        _apply_ops(consumer, chat, private, ops, lines, now, done, times)
    try:  # 聊天介入生活 (owner 11-06): the world kernel hears what the owner said to her and what she agreed to
        queue_world(consumer, chat, private, lines, now, times, ids)
    except Exception as e:  # noqa: BLE001  never holds the memories back
        log.warning("intent %s world queue: %s", consumer, e)
    return done


def _apply_ops(consumer, chat, private, ops, lines, now, done, times=None):
    try:
        actor = W.actor_of(consumer)
    except ValueError:
        return done
    spoken = _speakers(lines)
    talked = {w for w, _ in spoken if w}
    # A-05: housemates / the owner whose name someone else shows in this batch (she herself is always herself)
    worn = ID.worn(lines) - {actor}
    with W.db() as c:
        expire(c, now)
        for op in ops[:10]:
            try:
                if op.get("op") == "intent":
                    how = _new(c, actor, chat, private, op, spoken, talked, now, worn, times)
                else:
                    how = _end(c, actor, chat, private, op, spoken, talked, now)
                done[how] += 1
            except (ValueError, TypeError, KeyError) as e:
                done["rejected"] += 1
                log.info("intent %s op rejected (%s): %s", consumer, e, json.dumps(op, ensure_ascii=False)[:200])
    return done


WORLD_QUEUE = ("create table if not exists owner_chat_queue (id integer primary key, actor text not null,"
               " body text not null, state text not null, created real not null)")


def queue_world(consumer, chat, private, lines, now, times=None, ids=None):
    """聊天介入生活 (owner 2026-11-06 「在ai小屋的群聊和各自的私聊通过聊天介入她们生活」): after a memory batch, when
    the owner (by QQ number, never a display name) spoke in it or one of her intents changed in it, the batch's owner
    lines, her own lines and those intents go to world.db for the kernel's next step, which gives her one decision of
    her own about it. Only with the kernel as the authority; no model call here. Nothing in the house is done for
    the owner: what happens is what she decides."""
    if os.environ.get("WORLD_KERNEL") != "authority":
        return False
    try:
        actor = W.actor_of(consumer)
    except ValueError:
        return False
    spoken = _speakers(lines)
    clip = lambda t: re.sub(r"\s+", " ", str(t or "")).strip()[:160]
    if PRESENCE_ON:
        try:  # owner card 11-06 「算，明确说在哪就进哪」: his own explicit words put him in a room / take him out
            presence_from_chat(spoken, chat, now, times, reader=actor, ids=ids)
        except Exception as e:  # noqa: BLE001
            log.warning("chat presence: %s", e)
    owner = [clip(t) for w, t in spoken if w == "owner" and clip(t)][-4:]
    mine = [clip(t) for w, t in spoken if w == actor and clip(t)][-4:]
    with W.db() as c:
        c.execute(WORLD_QUEUE)
        touched = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "status": r["status"],
                    "party": json.loads(r["party"]), "agreed": json.loads(r["agreed"]),
                    "due_from": r["due_from"], "due_to": r["due_to"]}
                   for r in c.execute("select * from intents where updated>=? and status in ('proposed','accepted')",
                                      (now - 1,))
                   if actor in json.loads(r["party"])]
        if not owner and not touched:
            return False
        body = {"where": "private" if private else "group", "chat": "private" if private else str(chat),
                "owner_said": owner, "she_said": mine, "intents": touched, "at": now}
        c.execute("insert into owner_chat_queue(actor, body, state, created) values (?,?,?,?)",
                  (actor, json.dumps(body, ensure_ascii=False), "queued", now))
        c.execute("delete from owner_chat_queue where state!='queued' and created<?", (now - 86400,))
    return True


ROOM_WORDS = {"客厅": "living", "厨房": "kitchen", "书房": "study", "院子": "yard", "庭院": "yard",
              "浴室": "bath", "洗澡间": "bath"}
HOME = re.compile(r"^(?:我|房主我?)?(?:已经)?(?:到家|回家|回来|回到家)(?:了|啦|喽|咯)")
IN_ROOM = re.compile(r"^(?:我|房主我?)(?:现在)?(?:在|去|进|到|来)(?:了)?(客厅|厨房|书房|院子|庭院|浴室|洗澡间|(\S{1,6}?)的?(?:房间|卧室))")
OUT = re.compile(r"^(?:我|房主我?)(?:先)?(?:出门|出去|走|离开家?)(?:了|啦|喽|咯)")
NOT_SURE = re.compile(r"[吗嘛呢？?]|没|不|别|要是|如果|假如|等会|待会|一会|明天|晚点|马上|准备|打算|想")
# #171 R171-01: one row per statement of his about where he is (applied or not); said_at is when its first reader's
# bridge saw it (memory seen.ts), the nearest thing to when he said it. One row per line a roommate read (her memory
# seen row), naming the statement it was: a batch replayed after a crash finds its lines there.
PRESENCE = ("create table if not exists owner_chat_statements(id integer primary key autoincrement, chat text not null,"
            " key text not null, op text not null, room text, said_at real not null, queued_at real not null,"
            " applied integer not null default 0)",
            "create table if not exists owner_chat_reads(reader text not null, chat text not null, line text not null,"
            " statement integer not null, read_at real not null, seen_at real, primary key(reader, chat, line))")
SAME_LINE_S = 1800  # how far apart two roommates' bridges can see the same line (one busy replying waits for it)
PAGE_MOVES = ("enter", "move")  # his page's enter/move takes the QQ session out (avatar._enter); its leave does not
AVATAR_QUEUE = ("CREATE TABLE IF NOT EXISTS owner_avatar_queue (cmd_id TEXT PRIMARY KEY, body TEXT NOT NULL,"
                " state TEXT NOT NULL, receipt TEXT, created_at REAL NOT NULL)")  # = worldkernel.avatar.QUEUE_SCHEMA


def presence_of(text):
    """('enter', room) / ('leave', None) for the owner's own plain statement of where he is now, else None.
    Only first-person, present, plain statements: a question, a negation, a plan or a maybe moves nobody."""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t or len(t) > 30 or NOT_SURE.search(t):
        return None
    if OUT.match(t):
        return ("leave", None)
    m = IN_ROOM.match(t)
    if m:
        if m.group(2):
            who = _party_key(m.group(2))
            return ("enter", f"room_{who}") if who and who != "owner" else None
        return ("enter", ROOM_WORDS[m.group(1)])
    if HOME.match(t):
        return ("enter", "living")
    return None


def _said_at(times, i, now):
    try:
        t = float(times[i])
    except (TypeError, ValueError, IndexError):
        return now
    return t if 0 < t <= now + 60 else now


def presence_from_chat(spoken, chat, now, times=None, reader=None, ids=None):
    """His explicit statements of this batch about where he is; the newest new one goes to the avatar inbox (session
    qqchat). #171 R171-01 (婉妲 review): six roommates read the same group line in their own batches, minutes apart,
    each one once and in order. A line she read before (same memory row: her batch replayed) is what it was then. A
    line with the same words, in the same chat, within SAME_LINE_S of a statement she has not read yet, is that
    statement (a copy, never applied again); otherwise it is a new statement, even with the same words. Every
    statement is kept, applied or not, so a late copy always finds it. The last new one of the batch is applied only
    when nothing newer is known about where he is: a statement she read after it in this batch, a later statement of
    his she has not read in this chat (in another chat, any later one), or his page's enter/move (not failed) after
    it."""
    if not PRESENCE_ON:
        return None
    said = [(i, t, presence_of(t)) for i, (w, t) in enumerate(spoken) if w == "owner"]
    said = [(i, _said_at(times, i, now), t, p) for i, t, p in said if p]
    if not said:
        return None
    reader = reader or ""
    # a private chat has one reader: his DMs to nell and to wanda share the chat string private:<owner> (review 3 P2)
    chat = f"{reader}@{chat}" if str(chat).startswith("private:") else chat
    with W.db() as c:
        for ddl in PRESENCE:
            c.execute(ddl)
        _add_seen_at(c)
        # statements go by said_at, reads by read_at (>= said_at): a read may outlive its statement for a while; it
        # then only marks that line as read (autoincrement ids are never reused), which is what it was
        c.execute("delete from owner_chat_statements where said_at<?", (now - 86400,))
        c.execute("delete from owner_chat_reads where read_at<?", (now - 86400,))
        order = []  # (statement id, new?) in her reading order
        for i, at, text, (op, room) in said:
            line = str(ids[i]) if ids and i < len(ids) and ids[i] is not None else f"{at:.3f}#{i}"
            r = c.execute("select statement from owner_chat_reads where reader=? and chat=? and line=?",
                          (reader, chat, line)).fetchone()
            if r:
                order.append((r[0], False))
                continue
            key = hashlib.sha1(f"{chat}|{_norm(text)}".encode()).hexdigest()[:32]
            copy = c.execute("select id from owner_chat_statements s where chat=? and key=? and said_at between ? and ?"
                             " and not exists (select 1 from owner_chat_reads r where r.reader=? and r.statement=s.id)"
                             " order by said_at, id limit 1",
                             (chat, key, at - SAME_LINE_S, at + SAME_LINE_S, reader)).fetchone()
            sid = copy[0] if copy else c.execute(
                "insert into owner_chat_statements(chat, key, op, room, said_at, queued_at) values (?,?,?,?,?,?)",
                (chat, key, op, room, at, now)).lastrowid
            c.execute("insert into owner_chat_reads(reader, chat, line, statement, read_at, seen_at) values"
                      " (?,?,?,?,?,?)", (reader, chat, line, sid, now, at))
            order.append((sid, not copy))
        new = [k for k, (_, is_new) in enumerate(order) if is_new]
        if not new:
            return None
        sid = order[new[-1]][0]
        after = {s for s, _ in order[new[-1] + 1:]} - {sid}
        key, at, op, room = c.execute("select key, said_at, op, room from owner_chat_statements where id=?",
                                      (sid,)).fetchone()
        # R171-01b (婉妲 review 2): her reading order only says what came first inside one chat; her chats are
        # extracted in their own order (a DM can go first, a backlog can wait), so across chats time decides: for a
        # statement she read in another chat, the earlier of said_at and when her own bridge saw it (its first
        # reader's bridge may have seen it late; hers and this line's time come from the same clock)
        mine = {r[0] for r in c.execute("select statement from owner_chat_reads where reader=? and chat=?",
                                        (reader, chat))} - after
        c.execute(AVATAR_QUEUE)
        # what she read before this line in this chat is older than it for her, whatever its first reader's clock
        # said; what she read after it is newer
        newer = bool(after) or any(s not in mine and min(said_at, seen if seen is not None else said_at) > at
                                   for s, said_at, seen in c.execute(
            "select s.id, s.said_at, (select min(r.seen_at) from owner_chat_reads r where r.reader=? and"
            " r.statement=s.id) from owner_chat_statements s where s.said_at>? and s.id!=?", (reader, at, sid))) or any(
            _page_move(b) for b, in c.execute("select body from owner_avatar_queue where cmd_id not like 'chat-%'"
                                              " and created_at>? and state!='failed'", (at,)))
        if newer:
            log.info("chat presence %s said %.0fs ago: older than what is known, not applied", op, now - at)
            return None
        c.execute("update owner_chat_statements set applied=1 where id=?", (sid,))
        cmd_id = f"chat-{key[:8]}-{int(at)}-{sid}"
        body = {"op": op, "cmd_id": cmd_id, "session": "qqchat", "via": "chat", "said_at": round(at, 3)} \
            | ({"location": room} if room else {})
        c.execute("INSERT OR IGNORE INTO owner_avatar_queue VALUES(?,?,'queued',NULL,?)",
                  (cmd_id, json.dumps(body, ensure_ascii=False, sort_keys=True), now))
    return body


def _add_seen_at(c):
    """owner_chat_reads from before R171-01b gets its seen_at column; two roommates' first batches may race here."""
    if "seen_at" in {r[1] for r in c.execute("pragma table_info(owner_chat_reads)")}:
        return
    try:
        c.execute("alter table owner_chat_reads add column seen_at real")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _page_move(body):
    try:
        return json.loads(body).get("op") in PAGE_MOVES
    except (TypeError, ValueError, AttributeError):
        return False


def _new(c, actor, chat, private, op, spoken, talked, now, worn=frozenset(), times=None):
    text = W._clean(op.get("text"), 80)
    quote = W._clean(op.get("quote"), 200)
    if len(text) < 4 or W._private(text) or W._private(quote):
        raise ValueError("empty or private")
    names = op.get("with") or []
    partners = {_party_key(n, worn) for n in (names if isinstance(names, list) else [names])} - {None, actor}
    v2 = os.environ.get("INTENTS_EXTRACT_V2") == "1"
    beneficiaries = []
    if v2:
        names_for = op.get("for") or []
        if isinstance(names_for, str):
            names_for = [names_for]
        if not isinstance(names_for, list):
            raise ValueError("bad beneficiary")
        resolved_for = [_party_key(n, worn) for n in names_for]
        if any(p is None for p in resolved_for):
            raise ValueError("unknown beneficiary")
        beneficiaries = sorted(set(resolved_for) - {actor})
        if names_for and not beneficiaries:
            raise ValueError("unknown beneficiary")
    party = sorted({actor} | partners)
    quote_at = _bound_quote(quote, spoken, proposal=True,
                            own_plan=actor if not partners and not beneficiaries else None) if EVIDENCE_GATE else None
    said_by = spoken[quote_at][0] if EVIDENCE_GATE else _quoted(quote, spoken)
    if not said_by or said_by not in party + beneficiaries:
        raise ValueError("quote not said by anyone in it")
    line = spoken[quote_at][1] if EVIDENCE_GATE else _quote_line(quote, spoken)
    if NO.search(line) or FICTION.search(line) or (EVIDENCE_GATE and _unreal(line)):
        raise ValueError("the quote turns it down, or it is a dream / a what-if")
    if EVIDENCE_GATE and not _same_topic(text, line):
        raise ValueError('proposal quote cannot be linked to this matter')
    if actor not in talked:
        raise ValueError("she did not say a word about it")  # someone else's plan (the owner's, too) is not hers
    kind = op.get("kind") if op.get("kind") in ("plan", "promise", "joint") else "joint"
    if beneficiaries:
        kind = "promise"
    elif not partners:
        kind = "plan"
    elif kind == "plan":
        kind = "joint"
    if kind == "plan" and said_by != actor:
        raise ValueError("her own plan quoted from someone else")
    want = op.get("agreed") or []
    agreed = sorted(p for p in ({actor} | {_party_key(n, worn) for n in (want if isinstance(want, list) else [want])})
                    & set(party) & talked if (_bound_yes(p, quote_at, spoken) if EVIDENCE_GATE
                                             else _said_yes(p, said_by, spoken)))
    if kind == "promise" and actor not in agreed:
        raise ValueError("a promise she did not say yes to")
    status = "accepted" if kind == "promise" or set(agreed) == set(party) else "proposed"
    lo, hi = _due(op.get("due"), now)
    said_at = now
    if quote_at is not None and times and quote_at < len(times) and type(times[quote_at]) in (int, float):
        said_at = min(now, times[quote_at])
    ref = {"chat": "private" if private else chat, "quote": quote[:120], "at": now, "said_at": said_at}
    scope = op.get("scope", "world") if v2 else "world"
    when_kind = op.get("when", "") if v2 else ""
    if scope not in ("world", "chat") or when_kind not in ("", "exact", "day", "vague", "after"):
        raise ValueError("invalid intent classification")
    if not when_kind and hi is not None:
        when_kind = "day" if hi - lo >= 86400 else "exact"
    key = json.dumps(party)
    for r in c.execute("select * from intents where status in ('proposed','accepted') and party=?", (key,)):
        if not _same_audience(r, actor, chat, private):
            continue
        if json.loads(r["beneficiary"]) != beneficiaries or r["scope"] != scope:
            continue
        if _sim(_norm(r["text"]), _norm(text)) >= 0.5:  # the same plan again (or a roommate's copy of it)
            both = sorted(set(json.loads(r["agreed"])) | set(agreed))
            st = "accepted" if r["status"] == "accepted" or status == "accepted" or set(both) == set(party) else "proposed"
            _set(c, r, now, agreed=json.dumps(both), status=st, refs=_add_ref(r, ref), last_action=now,
                 due_from=lo if lo is not None else r["due_from"], due_to=hi if hi is not None else r["due_to"], when_kind=when_kind or r["when_kind"])
            if st != r["status"]:
                _ev(c, "intent_accepted", r)
            return "merged"
    if len(_mine(c, actor)) >= MAX_OPEN:
        raise ValueError("too many open intents")
    if scope == "chat" or when_kind in ("vague", "after"):
        unusual = [r for r in _mine(c, actor) if r["scope"] == "chat" or r["when_kind"] in ("vague", "after")]
        if len(unusual) >= 8:
            raise ValueError("too many long-lived intents")
    if c.execute("select count(*) from intents where actor=? and created>=?",
                 (actor, W._day_start(now))).fetchone()[0] >= PER_DAY:
        raise ValueError("enough new intents today")
    try:
        conf = max(0.0, min(1.0, float(op.get("confidence", 0.7))))
    except (TypeError, ValueError):
        conf = 0.7
    iid = c.execute("insert into intents(actor, party, agreed, kind, text, due_from, due_to, status, source, refs, "
                    "visibility, confidence, created, updated, last_action) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (actor, key, json.dumps(agreed), kind, text, lo, hi, status, "chat",
                     json.dumps([ref], ensure_ascii=False), "private" if private else "group",
                     conf if status == "accepted" else min(conf, 0.5), now, now, now)).lastrowid
    c.execute("update intents set beneficiary=?, scope=?, when_kind=? where id=?",
              (json.dumps(beneficiaries), scope, when_kind, iid))
    W._event(c, "intent_new", _bots(party), {"id": iid, "kind": kind, "status": status, "text": text})
    return "intents"


def _end(c, actor, chat, private, op, spoken, talked, now):
    kind = op.get("op")
    if kind not in ("intent_done", "intent_drop"):
        raise ValueError("not an intent op")
    r = c.execute("select * from intents where id=? and status in ('proposed','accepted')",
                  (int(op.get("id")),)).fetchone()
    if not r or actor not in json.loads(r["party"]):
        raise ValueError("no such open intent of hers")
    if r["visibility"] == "private" and not private:
        raise ValueError("a private one is not ended from a group")
    if not _same_audience(r, actor, chat, private):
        raise ValueError("intent evidence belongs to another audience")
    quote = W._clean(op.get("quote"), 200)
    quote_at = _bound_quote(quote, spoken) if EVIDENCE_GATE else None
    said_by = spoken[quote_at][0] if EVIDENCE_GATE else _quoted(quote, spoken)
    # Only a drop may also come from who the promise is for (the owner cancelling it); "done" stays with the party.
    allowed = json.loads(r["party"]) + (json.loads(r["beneficiary"] or "[]") if kind == "intent_drop" else [])
    if not said_by or said_by not in allowed:
        raise ValueError("no evidence from anyone in it")
    line = spoken[quote_at][1] if EVIDENCE_GATE else _quote_line(quote, spoken)
    if EVIDENCE_GATE and (_unreal(line) or re.search(r'[?？]', line)):
        raise ValueError('fiction, reported speech or uncertainty is not evidence')
    if ASKING.search(line.strip()):
        raise ValueError("a question is not evidence")
    if kind == "intent_done" and (NOT_YET.search(line) or not DONE.search(line)):
        raise ValueError("not said to be done")
    if EVIDENCE_GATE and kind == "intent_done" and (DENIED_DONE.search(line) or NO.search(line)
                                                   or not _same_topic(r['text'], line)):
        raise ValueError('completion is denied or cannot be linked to this matter')
    status = "completed" if kind == "intent_done" else ("declined" if op.get("why") == "declined" else "cancelled")
    ref = {"chat": "private" if private else chat, "quote": W._clean(op.get("quote"), 120), "at": now, "end": status}
    _set(c, r, now, status=status, refs=_add_ref(r, ref), last_action=now)
    _ev(c, f"intent_{status}", r, by=said_by)
    return "ended"


def card_lines(actor, now=None, n=3):
    """At most n lines for her 此刻 card: what she agreed on and is due soon (or has no time), from group chats."""
    if not (ON and SHOW):
        return []
    now = time.time() if now is None else now
    with W.db() as c:
        expire(c, now)
        rows = [r for r in _mine(c, actor, private=False) if r["status"] == "accepted"
                and (r["due_from"] is None or r["due_from"] <= now + SOON_S and now <= r["due_to"])]
    rows.sort(key=lambda r: (r["due_from"] is None, r["due_from"] or 0))
    out = []
    for r in rows[:n]:
        bits = [b for b in (_who_text(r, actor) and "和" + _who_text(r, actor), _when(r, now)) if b]
        out.append(r["text"] + (f"（{'，'.join(bits)}）" if bits else ""))
    return out


def due_for(c, members, now):
    """Agreed intents of these members that are due now, for the world prompt: [(id, text, party)]."""
    if not (ON and SHOW):
        return []
    expire(c, now)
    out = []
    for r in c.execute("select * from intents where status='accepted' and visibility='group' and "
                       "(due_from is null or (due_from<=? and due_to>=?)) order by due_from is null, due_from",
                       (now, now)):
        party = json.loads(r["party"])
        if any(p in members for p in party):
            out.append((r["id"], r["text"], party))
    return out[:4]


def prompt_lines(due, members):
    if not due:
        return ""
    lines = [f"- #{i} {'、'.join(OWNER['name'] if p == 'owner' else W.ACTORS[p] for p in party)}：{text}"
             + ("（涉及房主：绝不推断、模拟或代替房主的出席、同意、取消、完成，也不因为到期去催；只能等房主自己说）"
                if "owner" in party else "")
             + ("" if all(p in members or p == "owner" for p in party) else "（有人不在这儿，这次做不成，最多想起来）")
             for i, text, party in due]
    return ("\n她们惦记着的事（时间只作参考，不是催办；只在当事人醒着、有空、情境自然时才考虑。真的做了才写进剧本并在 "
            "intents_done 里标出；没做就算了，别硬凑，错过也不用内疚）：\n" + "\n".join(lines))


def world_done(c, members, got, ids, due, now):
    """intents_done from a validated advance: only due ones whose housemates were all in this scene, no owner."""
    marks = got.get("intents_done") if isinstance(got, dict) else None
    if not (due and isinstance(marks, list)):
        return 0
    ok = {i: party for i, _, party in due if "owner" not in party and all(p in members for p in party)}
    n = 0
    for m in marks[:4]:
        try:
            iid, beat = int(m.get("id")), max(0, min(len(ids) - 1, int(m.get("beat", len(ids) - 1))))
        except (AttributeError, TypeError, ValueError):
            continue
        r = c.execute("select * from intents where id=? and status='accepted'", (iid,)).fetchone()
        if iid not in ok or not r:
            continue
        _set(c, r, now, status="completed", refs=_add_ref(r, {"script": ids[beat], "at": now, "end": "completed"}),
             last_action=now)
        _ev(c, "intent_completed", r, by="world", script=ids[beat])
        n += 1
    return n


def nudge(c, actor, now):
    """Her joint plan with a roommate who is elsewhere is due: one wish to reach her (outreach still waits until
    she is awake and free). Only plans with a time, from group chats, once each."""
    if not (ON and SHOW):
        return 0
    n = 0
    for r in c.execute("select * from intents where actor=? and status='accepted' and visibility='group' and "
                       "nudged=0 and due_from<=? and due_to>=?", (actor, now, now)).fetchall():
        for p in _bots(json.loads(r["party"])):
            if p != actor and not W._together(c, actor, p):
                n += bool(W._add_contact(c, actor, p, f"之前说好的：{r['text']}", "intent", now))
        _set(c, r, now, nudged=1)
    return n


def listing(actor=None, status=None, now=None):
    """For the console (read-only): private ones keep their text to themselves."""
    now = time.time() if now is None else now
    with W.db() as c:
        rows = c.execute("select * from intents order by updated desc limit 200").fetchall()
        progress = {r["id"]: json.loads(r["progress"]) for r in c.execute("select id,progress from intent_match")}
        reopenable = {r["id"]: _can_reopen(c, r, now) for r in rows}
    out = []
    for r in rows:
        x = _row(r)
        x["end_reason"] = _end_reason(x)
        x["can_reopen"] = reopenable[r["id"]]
        x["progress"] = [] if x["visibility"] == "private" else progress.get(r["id"], [])
        if actor and actor not in x["party"] or status and x["status"] != status:
            continue
        if x["visibility"] == "private":
            x["text"] = "（房主私聊里的约定）"
            x["refs"] = [{"chat": "private", "at": f.get("at"), **({"end": f["end"]} if f.get("end") else {})}
                         for f in x["refs"]]
        elif W.DM_AUDIENCE:
            # Legacy mixed rows remain untouched in storage; redact unsafe refs at the public read boundary.
            x["refs"] = [f if f.get("visibility", "group") == "group" and
                             (str(f.get("chat", "")).startswith("group:") or
                              (("script" in f or "kernel" in f) and "chat" not in f and "quote" not in f)) else
                         {"chat": "private", "at": f.get("at"), **({"end": f["end"]} if f.get("end") else {})}
                         for f in x["refs"]]
        out.append(x)
    return out


# #191 phase 1. Schema is additive; plans/invitations are never altered.
KERNEL_SCHEMA = """
create table if not exists intent_evidence(
 key text primary key, stream text not null, event_id integer not null, at real not null,
 body text not null, state text not null default 'queued');
create index if not exists evidence_stream on intent_evidence(stream,event_id);
create table if not exists intent_match(
 id integer primary key, fingerprint text not null, terms text not null, progress text not null,
 candidates text not null, suppressed integer not null default 0);
create table if not exists intent_ops_queue(
 key text primary key, intent integer not null, expected_updated real not null,
 created real not null, state text not null default 'queued');
create table if not exists intent_plan_claim(
 stream text not null, plan text not null, intent integer, reason text not null,
 primary key(stream,plan));
"""


def _deadline(r, v2=True):
    if not v2:
        return r["due_to"] + LATE_S if r["due_to"] is not None else r["last_action"] + IDLE_S
    if r.get("scope") == "chat":
        return r["last_action"] + 30 * 86400
    if r.get("plan_ended_at") is not None and r["due_to"] is None:
        return r["plan_ended_at"] + LATE_S
    if r["due_to"] is not None:
        return r["due_to"] + (86400 if r.get("when_kind") == "day" else LATE_S)
    return r["last_action"] + (30 if r.get("when_kind") in ("vague", "after") else 14) * 86400


def _rules():
    # Server already adds /srv/world-kernel in authority mode; local worktrees use their sibling.
    path = Path(__file__).resolve().parent.parent / "world-kernel"
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
    from worldkernel import promise_rules, vocab_life1
    return promise_rules, vocab_life1


def _structured(r, now):
    rules, vocab = _rules()
    d = _row(r)
    d["beneficiary"] = json.loads(d.get("beneficiary") or "[]")
    times = [f.get("said_at", f.get("at", d["created"])) for f in d["refs"] if "chat" in f]
    d["said_at"] = min(times) if times else d["created"]
    d["deadline"] = _deadline(d, os.environ.get("INTENTS_EXPIRY_V2") == "1")
    d["terms"] = rules.compile_terms(d["text"], vocab, d["party"], d["beneficiary"])
    return d


def _fingerprint(d):
    return hashlib.sha256(json.dumps({k: d.get(k) for k in (
        "text", "party", "beneficiary", "scope", "when_kind", "due_from", "due_to", "said_at", "terms")},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _end_reason(x):
    if x["status"] == "completed":
        return "内核记录：履约条件已满足" if any("kernel" in f for f in x["refs"]) else "聊天里说做完了"
    if x["status"] in ("expired", "missed"):
        return "窗口过了" if x.get("due_to") is not None or x.get("plan_ref") else "没人再提"
    return ""


def _can_reopen(c, r, now):
    # Owner correction withdraws a completion, never another person's refusal or consent.
    if not r or r["status"] != "completed" or set(json.loads(r["agreed"])) != set(json.loads(r["party"])):
        return False
    # Touching an undated agreement renews its idle window, but cannot change an agreed date.
    return now <= _expiry_deadline(c, dict(r, last_action=now))


def request_reopen(iid, expected_updated, key, now=None):
    """Trusted owner adapter only. Queue receipt does not itself change an intent."""
    now = time.time() if now is None else now
    if not isinstance(key, str) or not 1 <= len(key) <= 100:
        raise ValueError("bad operation key")
    with W.db() as c:
        prior = c.execute("select * from intent_ops_queue where key=?", (key,)).fetchone()
        if prior:
            if prior["intent"] != int(iid) or prior["expected_updated"] != float(expected_updated):
                raise ValueError("operation key conflict")
            return {"queued": True, "state": prior["state"]}
        r = c.execute("select * from intents where id=?", (int(iid),)).fetchone()
        if not r or r["updated"] != float(expected_updated) or not _can_reopen(c, r, now):
            raise ValueError("只能撤回仍在有效期内、全员已同意的已完成约定")
        c.execute("insert into intent_ops_queue values(?,?,?,?, 'queued')",
                  (key, int(iid), float(expected_updated), now))
    return {"queued": True}


def sweep(now=None):
    """Intents' only kernel writer. Receipt consumption + progress + completion share one write tx."""
    if not ON:
        return 0
    now = time.time() if now is None else now
    mode = os.environ.get("INTENTS_KERNEL_MATCH", "off")
    n = 0
    with W.db() as c:
        c.execute("begin immediate")
        for op in c.execute("select * from intent_ops_queue where state='queued' order by created").fetchall():
            r = c.execute("select * from intents where id=?", (op["intent"],)).fetchone()
            valid = r and r["updated"] == op["expected_updated"] and _can_reopen(c, r, now)
            if valid:
                _set(c, r, now, status="accepted", last_action=now)
                # A withdrawn false completion may not complete again from the very same old receipts.
                c.execute("update intent_match set suppressed=1 where id=?", (r["id"],))
                _ev(c, "intent_reopened", r)
            c.execute("update intent_ops_queue set state=? where key=?", ("done" if valid else "stale", op["key"]))
        if mode not in ("shadow", "on"):
            expire(c, now)
            return 0
        rules, _ = _rules()
        bind = c.execute("select v from kernel_bind where k='world'").fetchone()
        if not bind:
            raise ValueError("missing kernel stream binding")
        evidence = [json.loads(r["body"]) for r in c.execute(
            "select body from intent_evidence where stream=? order by event_id", (bind[0],))]
        rows = c.execute("select * from intents where status in ('proposed','accepted')").fetchall()
        matches, groups = {}, {}
        # Claims outlive OPEN rows and process restarts. Ambiguous evidence stays
        # ambiguous even after one agreement expires or is completed in chat.
        claims = {r["plan"]: r["intent"] for r in c.execute(
            "select plan,case when reason='released' then null else intent end as intent"
            " from intent_plan_claim where stream=?", (bind[0],))}
        for r in c.execute("select id,plan_ref from intents where plan_ref is not null"):
            c.execute("insert or ignore into intent_plan_claim values(?,?,?,'bound')",
                      (bind[0], r["plan_ref"], r["id"]))
            claims.setdefault(r["plan_ref"], r["id"])
        # A failed attempt does not cancel the agreement. Retire its plan forever,
        # but free the still-open agreement to bind a later attempt in its own window.
        if mode == "on":
            for r in rows:
                plan = r["plan_ref"]
                selected = [e for e in evidence if plan and e.get("plan_id") == plan]
                if not any(e["kind"] == "plan_finished" and e.get("outcome") in (
                        "done", "early_home", "lapsed", "cancelled", "missed") for e in selected):
                    continue
                d = _structured(r, now)
                d["fingerprint"] = _fingerprint(d)
                if rules.judge(d, selected, now)["complete"]:
                    continue
                _set(c, r, now, plan_ref=None)
                c.execute("update intent_plan_claim set reason='released' where stream=? and plan=?",
                          (bind[0], plan))
                claims[plan] = None
            rows = c.execute("select * from intents where status in ('proposed','accepted')").fetchall()
        # An accepted kernel invitation can link exactly one structurally identical chat agreement.
        for e in evidence:
            settled = e["kind"] == "invite_settled" or (e["kind"] == "invite_answered"
                                                        and e.get("answer") == "yes" and not e.get("pending_count"))
            if (mode != "on" or not settled
                    or not e.get("plan_id") or e["plan_id"] in claims):
                continue
            linked = []
            for r in rows:
                d = _structured(r, now)
                if (d.get("plan_ref") or d["scope"] != "world" or not d["terms"]
                        or d["id"] in claims.values()
                        or set(d["party"]) != set(e.get("members", [])) or "owner" in d["party"]
                        or not d["said_at"] <= e["at"] <= d["deadline"]):
                    continue
                start, end = e.get("plan_start"), e.get("plan_end")
                if start is None:
                    continue
                end = start + 4 * 3600 if end is None else end
                if end < start:
                    continue
                if (start < max(d["said_at"], (d.get("due_from") or d["said_at"]) - 7200)
                        or start > (d.get("due_to") or d["deadline"])
                        or end < (d.get("due_from") or d["said_at"])):
                    continue
                selected = [x for x in evidence if x.get("plan_id") == e["plan_id"]]
                # A backlog can contain the whole failed attempt before the first sweep.
                # Do not bind it now and let expire() end the agreement before a retry is examined.
                if any(x["kind"] == "plan_finished" and x.get("outcome") in (
                        "done", "early_home", "lapsed", "cancelled", "missed") for x in selected):
                    candidate = dict(d, status="accepted", plan_ref=e["plan_id"], fingerprint=_fingerprint(d))
                    if not rules.judge(candidate, selected, now)["complete"]:
                        continue
                if all((t["kind"] == "arrived" and t["place"] == e.get("place")) or
                       (t["kind"] == "activity" and t["activity"] == e.get("activity")
                        and (not t.get("place") or t["place"] == e.get("place"))) for t in d["terms"]):
                    linked.append(r)
            if len(linked) == 1:
                r = linked[0]
                _set(c, r, now, plan_ref=e["plan_id"], status="accepted", agreed=r["party"])
                c.execute("insert into intent_plan_claim values(?,?,?,'bound')", (bind[0],e["plan_id"],r["id"]))
                claims[e["plan_id"]] = r["id"]
            elif len(linked) > 1:
                c.execute("insert into intent_plan_claim values(?,?,null,'ambiguous')", (bind[0],e["plan_id"]))
                claims[e["plan_id"]] = None
        rows = c.execute("select * from intents where status in ('proposed','accepted')").fetchall()
        for r in rows:
            d = _structured(r, now)
            fingerprint = _fingerprint(d)
            d["fingerprint"] = fingerprint
            prior = c.execute("select * from intent_match where id=?", (r["id"],)).fetchone()
            if prior and prior["fingerprint"] == fingerprint and prior["suppressed"]:
                continue
            ended = [e["at"] for e in evidence if e["kind"] == "plan_finished"
                     and d.get("plan_ref") and e.get("plan_id") == d["plan_ref"]]
            if ended:
                d["plan_ended_at"] = min(ended)
                d["deadline"] = _deadline(d)
            exact = any(x["kind"] in ("purchased", "brought_home") for x in d["terms"])
            plans = {e.get("plan_id") for e in evidence if e.get("plan_id")}
            if d.get("plan_ref"):
                plans &= {d["plan_ref"]}
            evaluations = []
            partial_progress = []
            for plan in sorted(plans):
                if not exact and plan in claims and claims[plan] != r["id"]:
                    continue
                selected = [e for e in evidence if e.get("plan_id") == plan]
                result = rules.judge(d, selected, now)
                partial_progress.extend(dict(p,plan_id=plan) for p in result["progress"])
                if result["complete"]:
                    evaluations.append((plan, result))
            if exact:
                # Errands are precisely scoped to (intent,item,actor,beneficiary); cross-trip partials accumulate.
                result = rules.judge(d, evidence, now)
                evaluations = [("exact", result)] if result["complete"] else []
            progress = rules.judge(d, evidence, now)["progress"] if exact else partial_progress
            candidates = [p for p, _ in evaluations]
            c.execute("insert into intent_match values(?,?,?,?,?,0) on conflict(id) do update set "
                      "fingerprint=excluded.fingerprint, terms=excluded.terms, progress=excluded.progress,"
                      "candidates=excluded.candidates, suppressed=0 where "
                      "intent_match.fingerprint!=excluded.fingerprint or intent_match.terms!=excluded.terms or "
                      "intent_match.progress!=excluded.progress or intent_match.candidates!=excluded.candidates or "
                      "intent_match.suppressed!=0",
                      (r["id"], fingerprint, json.dumps(d["terms"]), json.dumps(progress), json.dumps(candidates)))
            if len(evaluations) == 1:
                plan, result = evaluations[0]
                matches[r["id"]] = (r, plan, result)
            # Persist all candidate collisions, including multi-plan ambiguity.
            if not exact:
                for plan, _ in evaluations:
                    groups.setdefault(plan, []).append(r["id"])
        if mode == "on":
            for plan, ids in groups.items():
                if len(ids) > 1 and plan not in claims:
                    c.execute("insert into intent_plan_claim values(?,?,null,'ambiguous')", (bind[0],plan))
                    claims[plan] = None
            for iid, (r, plan, result) in matches.items():
                if plan != "exact" and len(groups[plan]) != 1:
                    continue
                if plan != "exact":
                    if plan in claims and claims[plan] != iid:
                        continue
                    c.execute("insert or ignore into intent_plan_claim values(?,?,?,'fulfilled')", (bind[0],plan,iid))
                    claims[plan] = iid
                # Still in this same immediate transaction: cancellation/edit cannot race this write.
                refs = [{"kernel": key, "kind": "fulfilled", "at": now, "end": "completed"}
                        for key in result["keys"]]
                _set(c, r, now, status="completed", last_action=now,
                     refs=json.dumps([f for f in json.loads(r["refs"]) if "kernel" not in f][-6:] + refs,
                                     ensure_ascii=False))
                _ev(c, "intent_completed", r, by="kernel")
                n += 1
        c.execute("update intent_evidence set state='consumed' where stream=? and state!='consumed'", (bind[0],))
        expire(c, now)
    return n


def kernel_context(actor, now=None):
    """Only house-group sources; owner/private/other-group contents never enter cross-roommate decisions."""
    if not ON or os.environ.get("INTENTS_DECIDE_CTX") != "1":
        return []
    now = time.time() if now is None else now
    with W.db() as c:
        rows = _mine(c, actor, private=False)
    out = []
    for r in rows:
        refs = [f for f in json.loads(r["refs"]) if "kernel" not in f and "script" not in f]
        if (r["scope"] != "world" or not refs or _deadline(dict(r), True) < now
                or any(f.get("chat") != W.HOME_CHAT for f in refs)):
            continue
        d = _structured(r, now)
        out.append({k: d[k] for k in ("id", "party", "beneficiary", "status", "terms", "due_from", "due_to")}
                   | {"text": d["text"][:30]})
    out.sort(key=lambda x: (0 if x["due_to"] is not None and x["due_to"] <= now + 86400
                           else 1 if x["due_to"] is None else 2, x["due_to"] or 0))
    return out[:4]


def errands(actor, now=None):
    """Read-only list of unfulfilled, unreserved personal purchases; account is always her own wallet."""
    if not ON or os.environ.get("WORLD_KERNEL_ERRANDS_FROM_INTENTS") != "1":
        return []
    now = time.time() if now is None else now
    rules, vocab = _rules()
    with W.db() as c:
        rows = _mine(c, actor)
        bind = c.execute("select v from kernel_bind where k='world'").fetchone()
        evidence = [json.loads(e["body"]) for e in c.execute(
            "select body from intent_evidence where stream=?", (bind[0] if bind else "",))]
        suppressed = dict(c.execute("select id,fingerprint from intent_match where suppressed=1"))
    out = []
    for r in rows:
        d = _structured(r, now)
        d["fingerprint"] = _fingerprint(d)
        if (suppressed.get(r["id"]) == d["fingerprint"] or d["status"] != "accepted" or d["scope"] != "world" or d["party"] != [actor]
                or d["deadline"] < now or d["when_kind"] == "after"
                or now < max(d["said_at"], (d["due_from"] or d["said_at"]) - 7200)
                or any(x["kind"] == "unsupported" for x in d["terms"])):
            continue
        for term in d["terms"]:
            if term["kind"] != "purchased":
                continue
            got = rules.judge(d, evidence, now)
            progress = next((p for p in got["progress"] if p["kind"] == "purchased" and p["item"] == term["item"]), {})
            qty = max(0, term["qty"] - progress.get("got", 0))
            item = term["item"]
            sells = vocab.PLACES["supermarket"].get("sells", [])
            if qty and item in vocab.INGREDIENTS and ("ingredients" in sells or item in sells
                    or "supermarket" in vocab.INGREDIENTS[item].get("where_to_buy", [])):
                out.append({"intent": d["id"], "item": item, "qty": qty,
                            "beneficiary": d["beneficiary"], "account": actor, "fingerprint": _fingerprint(d)})
    return out
