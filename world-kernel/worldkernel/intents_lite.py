"""Full commitment & promise store (`CommitmentStore` / `IntentsLite`) with evidence gates (`0.3.0`).

Upgrades `worldkernel.intents_lite` with scenario classification (`errand`/`cook`/`memo`/`outing`/
`together`/`chat_promise`), owner-only proposal gating, multi-resident shared plans (`plan_ref`,
`party_json`, `agreed_json`), JST due windows, strict `intent_evidence` closing, and `ChatLink`
compatible `owner_chat_queue` payloads.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

JST = dt.timezone(dt.timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS house_intents (
    intent_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    summary TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chat_promise',
    state TEXT NOT NULL DEFAULT 'open',
    item TEXT,
    raw_item TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    completed_at REAL,
    evidence_key TEXT,
    proposer_id TEXT NOT NULL DEFAULT 'user:owner',
    party_json TEXT NOT NULL DEFAULT '[]',
    agreed_json TEXT NOT NULL DEFAULT '[]',
    due_from REAL,
    due_to REAL,
    plan_ref TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS intent_evidence (
    key TEXT PRIMARY KEY,
    stream TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    at REAL NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_chat_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL DEFAULT (unixepoch('now'))
);
"""

_BUY_VERBS = ("买", "补货", "带点", "带些", "捎", "采购", "缺")
_CN_NUMS = {"一": 1, "二": 2, "两": 2, "俩": 2, "三": 3}
_DEFAULT_ITEM_ALIASES = {
    "鸡蛋": "egg", "牛奶": "milk", "大米": "rice", "米": "rice", "咖喱块": "curry_roux",
    "猪肉": "pork", "鸡肉": "chicken", "牛肉": "beef", "鲜鱼": "fish", "鱼": "fish",
    "土豆": "potato", "胡萝卜": "carrot", "洋葱": "onion", "卷心菜": "cabbage",
    "包菜": "cabbage", "白菜": "cabbage", "豆腐": "tofu", "味噌": "miso",
    "面粉": "flour", "意面": "pasta", "面条": "noodle", "乌冬": "noodle",
    "乌冬面": "noodle", "炒面": "noodle", "奶酪": "cheese", "芝士": "cheese",
    "黄油": "butter", "南瓜": "pumpkin", "苦瓜": "bitter_melon", "纳豆": "natto",
    "青椒": "green_pepper", "面包": "bread", "饭团": "onigiri", "水果": "fruit",
    "布丁杯": "pudding_cup", "布丁": "pudding_cup", "巧克力": "chocolate",
    "薯片": "chips", "饼干": "cookie", "红茶": "black_tea", "咖啡": "coffee",
    "可乐": "cola", "蜜瓜苏打": "melon_soda",
}

_ALLERGY_MEMO_WORDS = ("过敏", "忌口", "不吃", "不能吃", "少糖", "少盐", "少辣", "别放")
# A clause that asks rather than tells ("你记得我对什么过敏吗？") is never a dietary instruction.
_QUESTION_CLAUSE_RE = re.compile(
    r"[？?]\s*$|吗|呢|(?:什么|啥)(?!都|也|的)|谁知道|是不是|是否|有没有|记不记得|哪(?:些|个|种)|怎么|多少|为什么|为啥"
)
_COOK_COMMIT_WORDS = ("做饭", "下厨", "我来做", "我煮", "给你做", "帮做", "做顿", "做一顿", "做晚饭", "煮晚饭", "做早饭", "做午饭")
_OUTING_WORDS = ("出门", "晴空都", "逛", "陪我去", "陪我出", "图书馆", "咖啡店", "喫茶", "超市", "车站", "公园", "书店", "便利店")
_TOGETHER_WORDS = ("一起", "约好", "约了", "大扫除", "桌游", "看电影", "开黑", "联机")
_DISH_WORDS = ("咖喱", "火锅", "寿喜锅", "乌冬", "拉面", "意面", "炒面", "蛋包饭", "味噌汤", "炒饭", "松饼", "司康")
_VALID_KINDS = frozenset({"errand", "cook", "memo", "outing", "together", "chat_promise"})
_PAST_OR_NEG_BUY_WORDS = ("买好了", "买好啦", "买完了", "买完啦", "买到了", "买回来了", "买过了", "不用买", "别买")

_NUMERAL_CHARS = "一二两俩三四五六七八九十0123456789"
_MEASURE_CHARS = "节包袋瓶盒斤个颗根把块份只串支条箱罐件双杯"
# "买卖/买单/买账" are not purchases; "买一下X/买下X" is the verb plus a complement ("下午茶", "下酒菜" stay nouns).
# "带" only counts with a quantity right after it ("带瓶可乐"), never as "带一下…" ("带一下路/孩子").
_BUY_VERB = (
    r"(?:买(?![卖单账])(?:一下|下(?![午酒饭班面载水]))?|带(?!一?下)(?=[点些一二两俩三四五六七八九十\d"
    + _MEASURE_CHARS + r"])|捎|采购|补货)"
)
_QTY = (
    r"\s*(?:[一二两俩三四五六七八九十\d]+\s*[" + _MEASURE_CHARS + r"]?|点(?!心)|些|[个把包瓶盒袋份条杯根只斤罐箱支颗串])?\s*"
)
_BOUGHT_VERBISH = r"(?:买|带|捎|采购|补货|补|缺)(?:一下|下)?"
_NOUN = r"([\u4e00-\u9fffA-Za-z0-9]{1,12})"
_LENIENT_BUY_RE = re.compile(_BUY_VERB + _QTY + _NOUN)
_REQUEST_MARKERS = (
    r"(?:帮我|帮忙|帮(?=买|带|捎)|替我|给我(?=买|带|捎)|麻烦|拜托|劳驾|请|顺便|顺路|顺带|记得|记着|务必|一定要|"
    r"能不能|能否|可以|可不可以|你去|你来|你先|你再|你(?=买|带|捎)|再|抽空|有空|路过|"
    r"去[^，,。.！!？?；;\n]{0,8}?(?=买|带|捎|采购))"
)
_REQUEST_BUY_RE = re.compile(
    _REQUEST_MARKERS + r"[^，,。.！!？?；;\n]{0,8}?" + _BUY_VERB + _QTY + _NOUN
)
# The resident promises the purchase themselves ("我这就去买榴莲" / "好，我去买").
_COMMIT_BUY_RE = re.compile(
    r"我(?:会|就|这就|马上|待会儿?|等下|等会儿?|一会儿?|明天|今天|下午|晚上|一定|先|去|来|帮你|替你|顺便|顺路|记得)*"
    + _BUY_VERB + _QTY + r"(?:" + _NOUN + r")?"
)
_SELF_BUY_RE = re.compile(r"(?<![帮替给为])我[^，,。.！!？?；;\n你]{0,3}?" + _BUY_VERB)
_FORGET_RE = re.compile(r"(?:别|不要|不能|千万别|莫)忘(?:了|记)?")
_POLITE_RE = re.compile(r"帮我|帮忙|麻烦|拜托|请|能不能|能否|可以|可不可以|记得")
_QUESTION_RE = re.compile(
    r"为什么|为啥|什么|啥|是不是|有没有|谁|怎么|多少|哪(?:里|个|儿|家|种|些)?|几(?:个|瓶|包|袋|盒|斤)|要不要|是否|买不买|买没买|该不该"
)
_PAST_BUY_RE = re.compile(
    r"[买带捎](?:了|过)|[买带捎采购补货](?:好|完|到)(?:了|啦)|买回来了|带回来了|"
    r"刚(?:才|刚)?(?:买|带)|已经(?:买|带)|早就(?:买|带)|"
    r"(?:昨天|昨晚|前天|上周|上个月|刚才|刚刚|之前|上次|曾经)[^，,。.！!？?；;\n]{0,6}?(?:买|带|捎|采购)"
)
_NEG_BUY_RE = re.compile(
    r"(?:不用|不必|无需|不需要|用不着|不要|不想|没有?|不)(?:再|要|用|去|来|给)?(?:买|带|捎|采购|补)|"
    r"别(?:再|去|要|乱)?(?:买|带|捎|采购)|[买带捎]不(?:起|到|了|着|成)"
)
_NOUN_CUT_RE = re.compile(r"回来|回去|上来|下来|过来|上去|进来|给我|给你|给他|给她|还有|以及|然后|[和跟与及、吧呢啊呀哦嘛吗啦了啰喔哟噢哈呗来去就再]")
_NOUN_REJECT_RE = re.compile(
    r"^(?:这|那|哪|什么|啥|它|他|她|自己|别的|其他|其它|东西|一点|[买带捎卖]|好吃|好喝|吃的|喝的|用的)|^(?:[点些单账]|一?下)$"
)
_ABSTRACT_NOUNS = frozenset({
    "钱", "觉", "时间", "运气", "命", "心", "力", "气", "自由", "教训", "开心", "安心", "放心", "明白", "面子", "单", "账",
})
# A lone measure word is not an item ("麻烦买一瓶"); "包" is also a real thing when no numeral precedes it ("买个包").
_MEASURE_NOUN_OK = frozenset({"包"})
# "买包子": the bare measure belongs to the noun.
_MEASURE_COMPOUNDS = (
    "包子", "包菜", "盒饭", "盒子", "瓶子", "袋子", "杯子", "杯面", "罐头", "罐子", "箱子", "条幅", "条纹", "个人", "根茎", "支架",
)
# Things one carries, passes on or leads rather than buys ("请带把伞", "捎个话", "带个头").
_CARRY_NOT_BUY = (
    "伞", "钥匙", "手机", "钱包", "身份证", "证件", "头", "话", "路", "队", "人", "孩子", "娃", "口信", "外套", "衣服",
    "口罩", "充电器", "电脑", "书包", "耳机", "眼镜", "脑子",
)
_BUY_VERB_FIND_RE = re.compile(r"买|带|捎|采购|补货")
# A request marker in the clause right before a verb-initial purchase clause ("路过便利店，捎份报纸").
_PREV_CLAUSE_MARKER_RE = re.compile(r"路过|顺路|顺便|顺带|麻烦|拜托|劳驾|帮我|帮忙")
_OUTING_DEST_MAP = (
    (("超市", "杂货", "买菜"), ("supermarket", "shop", "grocery", "market", "shotengai", "超市", "商店街")),
    (("商店街",), ("shotengai", "商店街")),
    (("公园", "绿地"), ("park", "公园", "绿地")),
    (("河岸", "星川"), ("riverside", "河岸", "星川")),
    (("图书馆",), ("library", "图书馆")),
    (("喫茶", "帆"), ("kissa", "tomoshibi", "cafe", "喫茶", "咖啡", "帆")),
    (("咖啡",), ("cafe", "coffee", "kissa", "tomoshibi", "咖啡", "喫茶")),
    (("书签堂", "栞堂"), ("shiorido", "bookshop", "bookstore", "书签堂", "栞堂", "书店")),
    (("书店",), ("bookshop", "bookstore", "book", "shiorido", "书店", "书签堂", "栞堂")),
    (("便利店",), ("konbini", "convenience", "便利店")),
    (("面包房", "面包店", "面包"), ("bakery", "muginohi", "面包", "麦穗")),
    (("神社",), ("shrine", "神社")),
    (("钱汤", "澡堂", "泡澡"), ("sento", "钱汤", "澡堂")),
    (("拉面",), ("ramen", "拉面")),
    (("车站", "云港", "棱光街"), ("station", "harbor", "port", "车站", "云港", "棱光街")),
)
_TOGETHER_ACTIVITY_HINTS = (
    (("大扫除", "打扫", "整理", "家务"), ("housework", "kitchen_help", "laundry")),
    (("桌游", "游戏", "开黑", "联机", "对战"), ("gaming", "game_tournament")),
    (("看电影", "电影"), ("movie_night",)),
    (("火锅",), ("hotpot_night",)),
    (("观星", "星星"), ("stargazing_together", "stargazing")),
    (("茶会", "喝茶", "下午茶"), ("tea_party", "tea_time")),
    (("采购", "买东西"), ("shopping_together", "shopping")),
    (("一起",), ("being_together",)),
)
_PLAN_SUCCESS_OUTCOMES = frozenset({"done", "early_home"})
_HOME_PLACES = frozenset({"kitchen", "living", "study", "yard", "bath", "home"})


def _is_home_place(place: Any) -> bool:
    p = str(place or "").strip().lower()
    return p in _HOME_PLACES or p.startswith("room_")


def _plan_place_fits_outing(ev: Mapping[str, Any], summary: str) -> bool:
    """A finished plan can only be the outing when it happened away from home, at the named place if one was named.

    `plan_finished` carries no plan kind, so a tea plan in the living room used to close every open "去…" promise.
    A plan without a place stays acceptable (older exporters did not write one).
    """
    place = str(ev.get("place") or "").strip().lower()
    if not place:
        return True
    if _is_home_place(place):
        return False
    for sum_toks, ev_toks in _OUTING_DEST_MAP:
        if any(st in summary for st in sum_toks):
            return any(et in place for et in ev_toks)
    return True


def _plan_activity_fits_together(ev: Mapping[str, Any], summary: str, ev_rows: Sequence[Tuple[str, str]]) -> bool:
    """A finished shared plan closes a "一起<活动>" promise only when its activity is not known to be a different one."""
    specific = [acts for words, acts in _TOGETHER_ACTIVITY_HINTS if words != ("一起",) and any(w in summary for w in words)]
    if not specific:
        return True
    acts = {str(ev.get("activity") or "").lower()} - {""}
    pid = str(ev.get("plan_id") or "")
    if not acts and pid:
        for _key, body_raw in ev_rows:
            if pid not in body_raw:
                continue
            try:
                other = json.loads(body_raw)
            except ValueError:
                continue
            if str(other.get("plan_id") or "") == pid and other.get("activity"):
                acts.add(str(other["activity"]).lower())
    if not acts:
        return True
    return any(a in group for a in acts for group in specific)


def _clauses(text: str) -> List[str]:
    parts = re.split(r"(?<=[，,。.！!？?；;\n…~～])", str(text or ""))
    return [p.strip() for p in parts if p and p.strip()]



def diet_memo_blocked(text: str) -> bool:
    """True when every clause naming a dietary restriction is a question, so no "remember this" memo is due.

    「我对花生过敏，你记得吗？」 still has a telling clause and is not blocked.
    """
    diet = [c for c in _clauses(text) if any(w in c for w in _ALLERGY_MEMO_WORDS)]
    return bool(diet) and all(_QUESTION_CLAUSE_RE.search(c) for c in diet)

# Words that make a plain chat line look like a plan. Only the owner's own words count: the resident's reply is
# full of "一起/咖喱" while answering small talk, and the owner's question about a past event is not a plan either.
_CHAT_PLAN_KEYWORDS = (
    "早点睡", "去睡", "做饭", "火锅", "咖喱", "喝茶", "下午茶", "打游戏", "出门", "看书", "散步", "一起", "晴空都",
    "逛街", "逛逛", "陪我去", "陪我出", "陪我逛",
)
# A question that is really an invitation or a request ("要不要一起…", "能做咖喱饭吗", "…好不好", "…怎么样").
_INVITE_MARKER_RE = re.compile(
    r"要不要|想不想|能不能|能否|可不可以|行不行|好不好|好吗|可以吗|怎么样|如何|不如|能[^，,。.！!？?；;\n]{0,8}吗"
)
# Looking back ("昨天说好的逛街，后来去了吗") is a question about the past, not a new plan.
_RETROSPECT_RE = re.compile(r"了吗|了没|过吗|有没有|后来|昨天|昨晚|前天|刚才|刚刚|上次|之前|是不是")


def owner_proposes_plan(owner_text: str) -> bool:
    """True when some clause of what the owner said is a plan/invitation (not a question about habits or the past)."""
    for clause in _clauses(owner_text):
        if not any(k in clause for k in _CHAT_PLAN_KEYWORDS) or _RETROSPECT_RE.search(clause):
            continue
        if _QUESTION_CLAUSE_RE.search(clause) and not _INVITE_MARKER_RE.search(clause):
            continue
        return True
    return False


_DECLINE_RE = re.compile(
    r"没法|没办法|无法|不能|不可以|不行|做不到|办不到|去不了|买不了|来不了|做不了|不方便|没空|没时间|"
    r"改天|改成|改到|换成|拒绝|不想|不去|不买|不做|不会(?=去|买|做|帮|带)|"
    # "买不到/带不了/帮不上/去不成": the item or the errand is out of reach
    r"买不到|带不了|带不到|帮不上|去不成|买不成|来不成|做不成|"
    # "今天不出门 / 暂时没打算出门 / 不打算去": she is staying in, so she is not going to fetch anything
    r"不出门|[不没]打算(?:出门|出去|去|买|带)|"
    # "大概率不出门 / 应该不会去 / 估计不买": a hedge is still a no
    r"(?:大概率|大概|多半|八成|应该|估计|恐怕)(?:会)?不(?:会)?(?:出门|出去|去|买|带)"
)
_ACCEPT_RE = re.compile(
    r"好的|好呀|好啊|好哒|好嘞|好哦|好吧|行啊|没问题|可以|当然|我会|我去|我来|交给我|包在我|答应|马上|这就|一定|收到|乐意|愿意|嗯嗯"
)


def reply_declines(reply_text: str) -> bool:
    """True when the resident's reply turns the request down ("没法出门买鸡蛋", "能不能改成下午？", "今天大概率不出门",
    "榴莲我买不到").

    The first clause that either declines or accepts decides, so "好的，我会买，不过现在没法出门" is still an
    agreement while "我现在在家发呆，没法出门买鸡蛋" is not. A reply that does neither is not read as a refusal.
    """
    for clause in _clauses(reply_text):
        if _DECLINE_RE.search(clause):
            return True
        if _ACCEPT_RE.search(clause):
            return False
    return False


# 「陪我去商店街买衣服」: the owner shops herself and asks for company, she does not ask the resident to buy anything.
_COMPANION_BUY_RE = re.compile(r"(?:陪我|陪着我|陪我一起|跟我|和我|与我)[^，,。.！!？?；;\n]{0,12}?" + _BUY_VERB)


def _clause_blocked(clause: str) -> bool:
    """Past tense, questions, negations and 「陪我去…买」 never form a purchase errand."""
    c = _FORGET_RE.sub("记得", clause)
    if _PAST_BUY_RE.search(c) or _NEG_BUY_RE.search(c) or _QUESTION_RE.search(c) or _COMPANION_BUY_RE.search(c):
        return True
    if "吗" in c and not _POLITE_RE.search(c):
        return True
    return False


def _single_char_is_bought(text: str, label: str) -> bool:
    """A one-character item word ("米", "鱼") counts only as the direct object of a buy verb and as a whole word.

    Anywhere else it is part of a longer word or a name ("米娜", "鱼缸"): the old "label in text" test turned
    「米娜，明天陪我去逛街买衣服」 into a rice errand.
    """
    return bool(re.search(
        _BOUGHT_VERBISH + _QTY + re.escape(label) + r"(?:(?![\u4e00-\u9fff])|(?=[回来去吧啊呀哦嘛吗啦了的]))", text
    ))


def _live_buy_text(text: str) -> str:
    """The clauses of `text` that are not past/question/negated."""
    return "，".join(c for c in _clauses(text) if not _clause_blocked(c))


def _clean_buy_noun(raw: str) -> str:
    noun = _NOUN_CUT_RE.split(str(raw or ""), maxsplit=1)[0]
    noun = re.sub(r"的+$", "", noun).strip()
    if not noun or noun in _ABSTRACT_NOUNS or _NOUN_REJECT_RE.match(noun):
        return ""
    return noun[:24]


def _match_buy_noun(m: "re.Match[str]") -> str:
    """Item of a purchase match, or "" when the captured word is not something to buy."""
    raw = m.group(1) or ""
    if not raw:
        return ""
    head = m.string[m.start():m.start(1)].rstrip()
    lead, before_lead = head[-1:], head[-2:-1]
    if lead in _MEASURE_CHARS and before_lead not in _NUMERAL_CHARS and (lead + raw).startswith(_MEASURE_COMPOUNDS):
        raw = lead + raw
    noun = _clean_buy_noun(raw)
    if not noun:
        return ""
    if len(noun) == 1 and noun in _MEASURE_CHARS and (lead in _NUMERAL_CHARS or noun not in _MEASURE_NOUN_OK):
        return ""
    verbs = _BUY_VERB_FIND_RE.findall(head)
    if verbs and verbs[-1] in ("带", "捎") and noun.startswith(_CARRY_NOT_BUY):
        return ""
    return noun


def extract_raw_buy_noun(text: str) -> str:
    """Noun of the first live (non-past, non-question, non-negated) purchase clause; no marker required.

    Callers that already know the speaker asked for an errand (an LLM `errand` intent) use this directly;
    free chat goes through `extract_buy_request_noun`, which also demands an explicit request/commitment.
    """
    for clause in _clauses(text):
        if _clause_blocked(clause):
            continue
        m = _LENIENT_BUY_RE.search(clause)
        if m:
            noun = _match_buy_noun(m)
            if noun:
                return noun
    return ""


def extract_buy_request_noun(owner_text: str, reply_text: str = "") -> str:
    """Raw item only for an explicit request to the resident or an explicit promise by the resident."""
    prev = ""
    for clause in _clauses(owner_text):
        before, prev = prev, clause
        if _clause_blocked(clause) or _SELF_BUY_RE.search(clause):
            continue
        m = _REQUEST_BUY_RE.search(_FORGET_RE.sub("记得", clause))
        if m is None and before and _PREV_CLAUSE_MARKER_RE.search(before) and not _BUY_VERB_FIND_RE.search(before):
            if not (_clause_blocked(before) or _SELF_BUY_RE.search(before)):
                m = _LENIENT_BUY_RE.match(clause)
        if m:
            noun = _match_buy_noun(m)
            if noun:
                return noun
    promised = False
    for clause in _clauses(reply_text):
        if _clause_blocked(clause):
            continue
        m = _COMMIT_BUY_RE.search(clause)
        if m:
            promised = True
            noun = _match_buy_noun(m)
            if noun:
                return noun
    return extract_raw_buy_noun(owner_text) if promised else ""


_extract_raw_buy_noun = extract_raw_buy_noun


def _clean_actors(values: Iterable[str], fallback: str = "") -> List[str]:
    out: List[str] = []
    for v in values:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    if fallback and fallback not in out:
        out.insert(0, fallback)
    return out


def _loads_list(raw: Any, fallback: Sequence[str] = ()) -> List[str]:
    if isinstance(raw, list):
        return _clean_actors(str(x) for x in raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return _clean_actors(str(x) for x in parsed)
        except ValueError:
            pass
    return _clean_actors(fallback)


# A part of the day named in the sentence: (start, end) as (hour, minute). Ends run a little past the hour a person
# would say (tea that began at 17:00 ends after 18:00), so an activity that is just finishing still counts.
_DAY_PARTS = (
    ("上午", (7, 0), (12, 30)),
    ("中午", (11, 0), (14, 0)),
    ("下午", (12, 0), (18, 30)),
    ("晚上", (18, 0), (23, 59)),
)
# 「下午茶」 is something to drink, not a time.
_DAY_PART_RE = re.compile(r"(今天|明天|后天)?(?:的)?(上午|中午|下午(?!茶)|晚上)")
_DAY_OFFSET = {"今天": 0, "明天": 1, "后天": 2}


def parse_due_window(text: str, now: float) -> Tuple[Optional[float], Optional[float]]:
    """Parse relative JST due phrases (`今晚`, `明天`, `明早`, `明晚`, `周末`, `等会儿`, and a part of the day:
    `下午`, `今天上午`, `明天下午`, `晚上`).

    A part of the day without a day word means the next one that is not over yet; said after it ended, it is
    tomorrow's. 「今天」 alone is the rest of today. No time word: no window (the caller's default time-to-live)."""
    body = str(text or "")
    cur = dt.datetime.fromtimestamp(float(now), JST)
    d0 = cur.date()

    def _ts(d: dt.date, hh: int, mm: int) -> float:
        return dt.datetime.combine(d, dt.time(hh, mm), JST).timestamp()

    if any(w in body for w in ("等会儿", "待会儿", "一会儿", "待会", "等会")):
        return float(now), float(now) + 4 * 3600.0
    if any(w in body for w in ("今晚", "今夜", "今天晚上")):
        start = max(float(now), _ts(d0, 18, 0))
        end = max(float(now) + 3600.0, _ts(d0, 23, 59))
        return start, end
    if any(w in body for w in ("明早", "明天早上", "明晨")):
        d1 = d0 + dt.timedelta(days=1)
        return _ts(d1, 7, 0), _ts(d1, 11, 30)
    if any(w in body for w in ("明晚", "明天晚上")):
        d1 = d0 + dt.timedelta(days=1)
        return _ts(d1, 18, 0), _ts(d1, 23, 59)
    m = _DAY_PART_RE.search(body)
    if m:
        _, (sh, sm), (eh, em) = next(p for p in _DAY_PARTS if p[0] == m.group(2))
        day = d0 + dt.timedelta(days=_DAY_OFFSET.get(m.group(1) or "今天", 0))
        start, end = _ts(day, sh, sm), _ts(day, eh, em)
        if day == d0:
            if not m.group(1) and float(now) >= end:  # 「下午一起喝茶」 at 20:00 means tomorrow afternoon
                day += dt.timedelta(days=1)
                return _ts(day, sh, sm), _ts(day, eh, em)
            return max(float(now), start), max(float(now) + 3600.0, end)
        return start, end
    if "明天" in body:
        d1 = d0 + dt.timedelta(days=1)
        return _ts(d1, 8, 0), _ts(d1, 23, 59)
    if "后天" in body:
        d2 = d0 + dt.timedelta(days=2)
        return _ts(d2, 8, 0), _ts(d2, 23, 59)
    if any(w in body for w in ("周末", "周六", "星期六")):
        days_ahead = (5 - d0.weekday()) % 7
        if days_ahead == 0 and cur.hour >= 20:
            days_ahead = 7
        sat = d0 + dt.timedelta(days=days_ahead)
        sun = sat + dt.timedelta(days=1)
        return _ts(sat, 9, 0), _ts(sun, 22, 0)
    if "今天" in body:
        return float(now), max(float(now) + 3600.0, _ts(d0, 23, 59))
    return None, None


class CommitmentStore:
    """Persistent commitment/memo ledger backed by `world.db` (`house_intents` + `intent_evidence`)."""

    def __init__(self, world_db: Any, vocab: Any = None) -> None:
        self.world_db = world_db
        self.vocab = vocab

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.world_db, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def init(self) -> None:
        if not self.world_db:
            return
        with contextlib.closing(self._conn()) as c:
            with c:
                c.executescript(SCHEMA)
                cols = {r[1] for r in c.execute("PRAGMA table_info(house_intents)").fetchall()}
                for name, ddl in (
                    ("item", "TEXT"),
                    ("raw_item", "TEXT NOT NULL DEFAULT ''"),
                    ("qty", "INTEGER NOT NULL DEFAULT 1"),
                    ("completed_at", "REAL"),
                    ("evidence_key", "TEXT"),
                    ("proposer_id", "TEXT NOT NULL DEFAULT 'user:owner'"),
                    ("party_json", "TEXT NOT NULL DEFAULT '[]'"),
                    ("agreed_json", "TEXT NOT NULL DEFAULT '[]'"),
                    ("due_from", "REAL"),
                    ("due_to", "REAL"),
                    ("plan_ref", "TEXT NOT NULL DEFAULT ''"),
                    ("updated_at", "REAL NOT NULL DEFAULT 0"),
                ):
                    if name not in cols:
                        c.execute(f"ALTER TABLE house_intents ADD COLUMN {name} {ddl}")
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intents_plan_ref ON house_intents(plan_ref)"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_intent_evidence_at ON intent_evidence(at, event_id)"
                )

    def _row_to_dict(self, r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        actor = str(d.get("actor") or "")
        d["party"] = _loads_list(d.pop("party_json", "[]"), fallback=[actor] if actor else ())
        d["agreed"] = _loads_list(d.pop("agreed_json", "[]"), fallback=[actor] if actor else ())
        return d

    def kernel_context(self, actor: str, now: float) -> List[Dict[str, Any]]:
        if not self.world_db:
            return []
        self.init()
        with contextlib.closing(self._conn()) as c:
            rows = c.execute(
                "SELECT intent_id, actor, summary, kind, item, raw_item, qty, created_at, expires_at, "
                "proposer_id, party_json, agreed_json, due_from, due_to, plan_ref "
                "FROM house_intents "
                "WHERE actor=? AND state='open' AND (kind='memo' OR expires_at>?) "
                "ORDER BY CASE WHEN kind='memo' THEN 1 ELSE 0 END, created_at DESC LIMIT 8",
                (actor, float(now)),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = self._row_to_dict(r)
                out.append(d)
            return out

    def errands(self, actor: str, now: float) -> List[Dict[str, Any]]:
        if not self.world_db:
            return []
        self.init()
        with contextlib.closing(self._conn()) as c:
            rows = c.execute(
                "SELECT intent_id, item, qty, party_json FROM house_intents "
                "WHERE actor=? AND state='open' AND kind='errand' AND expires_at>? "
                "AND item IS NOT NULL AND item!='' "
                "ORDER BY created_at ASC LIMIT 4",
                (actor, float(now)),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            ingredients = getattr(self.vocab, "INGREDIENTS", None) if self.vocab else None
            for r in rows:
                if ingredients is not None and r["item"] not in ingredients:
                    continue
                qty = max(1, min(3, int(r["qty"] or 1)))
                party = _loads_list(r["party_json"], fallback=[actor])
                beneficiary = [p for p in party if p != actor]
                out.append({
                    "intent": r["intent_id"],
                    "item": r["item"],
                    "qty": qty,
                    "account": actor,
                    "beneficiary": beneficiary,
                    "fingerprint": f"{r['intent_id']}:{r['item']}:{qty}",
                })
            return out

    def list_intents(
        self,
        actor: Optional[str] = None,
        limit: int = 30,
        *,
        state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.world_db:
            return []
        self.init()
        clauses: List[str] = []
        params: List[Any] = []
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if state:
            clauses.append("state=?")
            params.append(str(state).strip().lower())
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        sql = (
            "SELECT intent_id, actor, summary, kind, state, item, raw_item, qty, created_at, expires_at, "
            "completed_at, evidence_key, proposer_id, party_json, agreed_json, due_from, due_to, plan_ref, updated_at "
            f"FROM house_intents {where_sql}ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit or 30), 200)))
        with contextlib.closing(self._conn()) as c:
            rows = c.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    list_commitments = list_intents

    def cancel(
        self,
        intent_id: str,
        *,
        reason: str = "owner_cancelled",
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.world_db:
            raise ValueError("世界数据库尚未初始化")
        iid = str(intent_id or "").strip()
        if not iid:
            raise ValueError("缺少 intent_id")
        ts = float(now if now is not None else time.time())
        self.init()
        with contextlib.closing(self._conn()) as c:
            with c:
                row = c.execute(
                    "SELECT * FROM house_intents WHERE intent_id=?",
                    (iid,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"未找到约定 {iid}")
                if row["state"] == "open":
                    c.execute(
                        "UPDATE house_intents SET state='cancelled', evidence_key=?, updated_at=? WHERE intent_id=?",
                        (f"cancel:{str(reason)[:64]}", ts, iid),
                    )
                updated = c.execute("SELECT * FROM house_intents WHERE intent_id=?", (iid,)).fetchone()
                return self._row_to_dict(updated)

    def _match_evidence(
        self,
        row: sqlite3.Row,
        ev_rows: Sequence[Tuple[str, str]],
        script_rows: Sequence[Tuple[int, float, str, str]],
    ) -> Optional[Tuple[float, str]]:
        intent_kind = str(row["kind"] or "chat_promise")
        if intent_kind == "memo":
            return None

        actor = str(row["actor"])
        party = _loads_list(row["party_json"] if "party_json" in row.keys() else "[]", fallback=[actor])
        created = float(row["created_at"])
        expires = float(row["expires_at"]) if "expires_at" in row.keys() and row["expires_at"] is not None else float("inf")
        iid = str(row["intent_id"])
        item = str(row["item"] or "")
        raw_item = str(row["raw_item"] if "raw_item" in row.keys() and row["raw_item"] else "")
        plan_ref = str(row["plan_ref"] if "plan_ref" in row.keys() and row["plan_ref"] else "")
        req_qty = max(1, int(row["qty"] or 1))
        summary = str(row["summary"] or "")

        # An outing/together whose words mention buying ("陪我去逛街买衣服") is still an outing: it closes on arriving,
        # never on a purchase event (a summary-only "买" is an errand only for a kind that has no better home).
        buy_in_summary = intent_kind not in ("outing", "together") and any(v in summary for v in _BUY_VERBS)
        is_errand = intent_kind == "errand" or bool(item) or bool(raw_item) or buy_in_summary
        if is_errand:
            if not item and not raw_item and not buy_in_summary:
                return None
            accum_qty = 0
            last_hit: Optional[Tuple[float, str]] = None
            for key, body_raw in ev_rows:
                try:
                    ev = json.loads(body_raw)
                except ValueError:
                    continue
                ev_at = float(ev.get("at") or 0)
                if ev_at < created - 1.0 or ev_at > expires:
                    continue
                kind = ev.get("kind")
                ev_fp = str(ev.get("fingerprint") or "")
                ev_iid = str(ev.get("intent_ref") or ev.get("intent_id") or "")
                if ev_iid == iid or (ev_fp and ev_fp.startswith(f"{iid}:")):
                    return ev_at or created, key
                if kind in ("purchased", "brought_home"):
                    if ev.get("actor") not in party:
                        continue
                    ev_item = str(ev.get("item") or "").strip()
                    if item:
                        matched_item = ev_item == item
                    else:
                        # PR-B4: Out-of-catalog buy errand never closes on unrelated catalog items (e.g. onion).
                        matched_item = bool(raw_item and ev_item == raw_item)
                    if matched_item:
                        accum_qty += max(1, int(ev.get("qty") or 1))
                        last_hit = (ev_at or created, key)
                        if accum_qty >= req_qty:
                            return last_hit
            return None

        wanted_dishes = [d for d in _DISH_WORDS if d in summary]
        for key, body_raw in ev_rows:
            try:
                ev = json.loads(body_raw)
            except ValueError:
                continue
            ev_at = float(ev.get("ended_at") or ev.get("at") or 0)
            if ev_at < created - 1.0 or ev_at > expires:
                continue
            ev_actor = ev.get("actor")
            ev_kind = str(ev.get("kind") or "")
            ev_ok = str(ev.get("outcome") or "").strip().lower() in _PLAN_SUCCESS_OUTCOMES
            if intent_kind == "cook":
                if ev_actor not in party:
                    continue
                if ev_kind == "activity":
                    act = str(ev.get("activity") or "").lower()
                    if act in ("cook", "cooking", "bake", "prep_meal", "dinner", "lunch", "breakfast") or any(
                        w in act for w in ("cook", "做饭", "下厨")
                    ):
                        ev_text = f"{ev.get('dish') or ''} {ev.get('recipe') or ''} {ev.get('text') or ''}"
                        if wanted_dishes and any(d in ev_text for d in _DISH_WORDS):
                            if not any(d in ev_text for d in wanted_dishes):
                                continue
                        return ev_at or created, key
                elif ev_kind == "plan_finished" and ev.get("plan_kind") == "cook" and ev_ok:
                    return ev_at or created, key
            elif intent_kind in ("outing", "together"):
                # An agreed invitation (`invite_settled`) is not the act itself, and `plan_finished` only counts when
                # the plan actually succeeded. Outings close on arrival at the named place; togethers need a
                # finished shared plan or a succeeded activity inside a shared plan.
                if ev_actor not in party:
                    continue
                if ev_kind == "plan_finished" and ev_ok:
                    members = ev.get("members")
                    if intent_kind == "together" and isinstance(members, list) and len(set(map(str, members))) < 2:
                        continue
                    ev_ref = str(ev.get("intent_ref") or ev.get("intent_id") or "")
                    if intent_kind == "outing" and ev_ref != iid and not _plan_place_fits_outing(ev, summary):
                        continue
                    if intent_kind == "together" and not _plan_activity_fits_together(ev, summary, ev_rows):
                        continue
                    return ev_at or created, key
                if intent_kind == "together" and ev_kind == "activity":
                    shared_plan = str(ev.get("plan_id") or "")
                    if not shared_plan or shared_plan.startswith("action:"):
                        continue
                    if str(ev.get("outcome") or "").strip().lower() != "succeeded":
                        continue
                    act = str(ev.get("activity") or "").lower()
                    label = ""
                    if self.vocab and act in getattr(self.vocab, "ACTIVITIES", {}):
                        label = str(self.vocab.ACTIVITIES[act].get("label") or "")
                    related = bool(label and label in summary) or any(
                        any(w in summary for w in words) and act in acts
                        for words, acts in _TOGETHER_ACTIVITY_HINTS
                    )
                    if related:
                        return ev_at or created, key
                if intent_kind == "outing" and ev_kind == "arrived":
                    ev_desc = (
                        f"{ev.get('place') or ''} {ev.get('where') or ''} "
                        f"{ev.get('location') or ''} {ev.get('detail') or ''} {ev.get('text') or ''}"
                    ).lower()
                    ev_ref = str(ev.get("intent_ref") or ev.get("intent_id") or "")
                    ev_pref = str(ev.get("plan_ref") or "")
                    dest_ok = True
                    if ev_ref != iid and not (plan_ref and ev_pref == plan_ref):
                        for sum_toks, ev_toks in _OUTING_DEST_MAP:
                            if any(st in summary for st in sum_toks):
                                dest_ok = any(et in ev_desc for et in ev_toks)
                                break
                    if dest_ok:
                        return ev_at or created, key
            else:
                if ev_actor == actor and ev_kind == "activity":
                    act = str(ev.get("activity") or ev.get("act") or "").lower()
                    label = ""
                    if self.vocab and act in getattr(self.vocab, "ACTIVITIES", {}):
                        label = str(self.vocab.ACTIVITIES[act].get("label") or "")
                    if any(
                        w and w in summary
                        for w in (
                            act,
                            label,
                            "睡" if act == "sleep" else "",
                            "看书" if act == "reading" else "",
                            "散步" if act == "stroll" else "",
                        )
                    ):
                        return ev_at or created, key
                elif ev_actor == actor and (
                    ev_kind == "arrived"
                    or (ev_kind == "plan_finished" and ev_ok and not _is_home_place(ev.get("place")))
                ):
                    if any(w in summary for w in ("出门", "去", "晴空都", "逛")):
                        return ev_at or created, key

        for sid, ts, actors_raw, text in script_rows:
            if float(ts) < created - 1.0 or float(ts) > expires:
                continue
            try:
                actors = json.loads(actors_raw or "[]")
            except ValueError:
                actors = []
            if not any(a in party for a in actors):
                continue
            t = str(text or "")
            if any(w in summary for w in _ALLERGY_MEMO_WORDS):
                continue
            if intent_kind in ("cook", "chat_promise") and ("做了" in t or "做好了" in t) and any(
                w in summary for w in ("做饭", "下厨", "煮", "咖喱", "火锅", "晚饭", "午饭", "早饭")
            ):
                if wanted_dishes and any(d in t for d in _DISH_WORDS) and not any(d in t for d in wanted_dishes):
                    continue
                return float(ts), f"script:{sid}"
        return None

    def sweep(self, now: float) -> None:
        if not self.world_db:
            return
        self.init()
        ts = float(now)
        with contextlib.closing(self._conn()) as c:
            with c:
                open_rows = c.execute(
                    "SELECT intent_id, actor, summary, kind, item, raw_item, qty, created_at, expires_at, party_json, plan_ref "
                    "FROM house_intents WHERE state='open' ORDER BY created_at ASC"
                ).fetchall()
                if open_rows:
                    min_created = min(float(r["created_at"]) for r in open_rows) - 2.0
                    ev_rows = c.execute(
                        "SELECT key, body FROM intent_evidence WHERE at >= ? OR at = 0 ORDER BY at ASC, event_id ASC",
                        (min_created,),
                    ).fetchall()
                    has_script = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='script'").fetchone()
                    script_rows = (
                        c.execute(
                            "SELECT id, ts, actors, text FROM script WHERE source='kernel' AND ts >= ? ORDER BY ts ASC, id ASC",
                            (min_created,),
                        ).fetchall()
                        if has_script
                        else []
                    )
                    for r in open_rows:
                        hit = self._match_evidence(r, ev_rows, script_rows)
                        if hit:
                            c.execute(
                                "UPDATE house_intents SET state='completed', completed_at=?, evidence_key=?, updated_at=? "
                                "WHERE intent_id=? AND state='open'",
                                (hit[0], hit[1], ts, r["intent_id"]),
                            )
                c.execute(
                    "UPDATE house_intents SET state='expired', updated_at=? "
                    "WHERE state='open' AND kind!='memo' AND expires_at<=?",
                    (ts, ts),
                )

    def _resolve_item_id(self, raw_item: Any) -> Optional[str]:
        raw = str(raw_item or "").strip()
        if not raw:
            return None
        ingredients = getattr(self.vocab, "INGREDIENTS", None) if self.vocab else None
        if ingredients is not None:
            if raw in ingredients:
                return raw
            low = raw.lower()
            if low in ingredients:
                return low
            for item_id, spec in ingredients.items():
                lbl = str((spec or {}).get("label") or "").strip()
                short = lbl.split("（", 1)[0].strip()
                if raw in (lbl, short):
                    return item_id
            aliased = _DEFAULT_ITEM_ALIASES.get(raw)
            if aliased and aliased in ingredients:
                return aliased
            return None
        if re.fullmatch(r"[a-zA-Z0-9_]+", raw):
            return raw.lower()
        return _DEFAULT_ITEM_ALIASES.get(raw)

    def unbuyable_noun(self, owner_text: str) -> str:
        """The out-of-catalog thing the owner explicitly asks the resident to buy, or "" (nothing / buyable)."""
        text = str(owner_text or "")
        if not text or self._detect_errand_item(text)[0]:
            return ""
        noun = extract_buy_request_noun(text)
        return "" if not noun or self._resolve_item_id(noun) else noun

    def _item_words(self, item: str) -> List[str]:
        words = [w for w, i in _DEFAULT_ITEM_ALIASES.items() if i == item and len(w) >= 2]
        spec = (getattr(self.vocab, "INGREDIENTS", None) or {}).get(item) if self.vocab else None
        label = str((spec or {}).get("label") or "").strip()
        for w in (label, label.split("（", 1)[0].strip()):
            if len(w) >= 2 and w not in words:
                words.append(w)
        return words

    def reply_hints(self, actor: str, owner_text: str, now: float, *, window_s: float = 3 * 86400.0) -> List[str]:
        """Facts about her own promises that she must know *before* she answers the owner.

        - the owner asks for something she cannot buy: say so now, instead of a note glued to the reply afterwards;
        - the owner asks after a recent errand of hers that expired or is only a reminder: she must not say
          "我会尽快去买的" about something that is over or was never buyable.
        """
        if not self.world_db or not str(owner_text or "").strip():
            return []
        text = str(owner_text)
        hints: List[str] = []
        noun = self.unbuyable_noun(text)
        if noun:
            hints.append(
                f"房主这句话想让你买「{noun}」，但这个你买不了（你只能去超市采购常见的食材、零食和饮品，「{noun}」不在其中）。"
                "请如实告诉她你买不了，不要答应去买；最多说可以先记个提醒。"
            )
        ingredients = getattr(self.vocab, "INGREDIENTS", None) or {}
        now_f = float(now)
        seen: set = set()
        for r in self.list_intents(actor=actor, limit=40):
            if r["kind"] != "errand" or not (r.get("item") or r.get("raw_item")):
                continue
            state = str(r["state"])
            if state == "open" and float(r.get("expires_at") or 0) <= now_f:
                state = "expired"
            if state not in ("open", "expired"):
                continue
            if state == "expired" and now_f - float(r.get("expires_at") or 0) > window_s:
                continue
            if r.get("item"):
                words = self._item_words(str(r["item"]))
                label = str((ingredients.get(r["item"]) or {}).get("label") or r["item"]).split("（", 1)[0]
                unbuyable = False
            else:
                label = str(r["raw_item"])
                words = [label]
                unbuyable = True
            if label in seen or not any(w and w in text for w in words):
                continue
            seen.add(label)
            if unbuyable and state == "expired":
                hints.append(
                    f"你之前只是给房主记了个「{label}」的提醒，但这个你买不了，提醒已经过期，没有办成。"
                    "她问起时请如实说你买不了、已经过期，不要说会去买。"
                )
            elif unbuyable:
                hints.append(
                    f"你给房主记了个「{label}」的提醒，但这个你买不了，也不会自动去买。她问起时请如实说。"
                )
            elif state == "expired":
                hints.append(
                    f"你之前答应过帮房主买{label}，但一直没买成，约定已经过期了。"
                    "她问起时请如实说没买成、已经过期，不要当成还在办；她还要的话请她再说一声。"
                )
            else:
                hints.append(f"你之前答应过帮房主买{label}，还没有买回来，还在办。")
        return hints[:4]

    def _detect_errand_item(self, text: str) -> Tuple[Optional[str], int]:
        text = _live_buy_text(text)
        names = getattr(self.vocab, "NAMES", None) if self.vocab else None
        if isinstance(names, Mapping):
            # "米娜" is a person, not rice: names never take part in item matching.
            for nm in sorted({str(v) for v in names.values() if len(str(v)) >= 2}, key=len, reverse=True):
                text = text.replace(nm, " ")
        if not any(v in text for v in _BUY_VERBS):
            return None, 1
        ingredients = getattr(self.vocab, "INGREDIENTS", None) if self.vocab else None
        candidates = {
            k: v for k, v in _DEFAULT_ITEM_ALIASES.items()
            if ingredients is None or v in ingredients
        }
        if ingredients:
            for item_id, spec in ingredients.items():
                lbl = str((spec or {}).get("label") or "").strip()
                if lbl:
                    candidates[lbl] = item_id
                    short = lbl.split("（", 1)[0].strip()
                    if len(short) >= 2:
                        candidates.setdefault(short, item_id)
        for label, item_id in sorted(candidates.items(), key=lambda kv: -len(kv[0])):
            if label in text:
                if len(label) == 1 and not _single_char_is_bought(text, label):
                    continue
                m = re.search(
                    rf"([1-9]|[一二两俩三])\s*(?:个|盒|瓶|袋|份|斤|根|条|颗|包|块|束|片|杯|罐|听)?\s*{re.escape(label)}",
                    text,
                )
                if m:
                    tok = m.group(1)
                    qty = _CN_NUMS[tok] if tok in _CN_NUMS else max(1, min(3, int(tok)))
                else:
                    qty = 2
                return item_id, qty
        return None, 1

    def _classify_kind(
        self, raw_kind: str, owner_text: str, summary: str, *, has_item: bool, party_len: int, has_raw: bool = False
    ) -> str:
        if has_item or has_raw:
            return "errand"
        combined = f"{owner_text}\n{summary}"
        if (
            any(w in combined for w in _ALLERGY_MEMO_WORDS)
            and not any(w in combined for w in _COOK_COMMIT_WORDS)
            and not diet_memo_blocked(owner_text)
        ):
            return "memo"
        rk = str(raw_kind or "").strip().lower()
        if rk in _VALID_KINDS and rk != "chat_promise":
            return rk
        if any(w in combined for w in _COOK_COMMIT_WORDS):
            return "cook"
        if any(w in combined for w in _OUTING_WORDS):
            return "outing"
        if party_len > 1 or any(w in combined for w in _TOGETHER_WORDS):
            return "together"
        return rk if rk in _VALID_KINDS else "chat_promise"

    def enqueue_chat(
        self,
        actor: str,
        owner_text: str,
        reply_text: str = "",
        where: str = "private",
        intents: Optional[Sequence[Mapping[str, Any]]] = None,
        now: Optional[float] = None,
        *,
        channel: str = "",
        speaker_role: str = "owner",
        proposer_id: str = "user:owner",
        party: Optional[Sequence[str]] = None,
        agreed: Optional[Sequence[str]] = None,
    ) -> Optional[int]:
        """Record an owner chat turn into `world.db` (`owner_chat_queue` + `house_intents`).

        Non-owner speakers (`speaker_role != 'owner'`) can never create commitments or wake ChatLink.
        """
        if not self.world_db:
            return None
        if str(speaker_role or "owner").strip().lower() != "owner":
            return None

        ts = float(now if now is not None else time.time())
        clean_owner = str(owner_text or "")
        clean_reply = str(reply_text or "")
        base_party = _clean_actors(party or [actor], fallback=actor)
        base_agreed = _clean_actors(agreed or [actor], fallback=actor)
        scope_sig = str(channel or where or "private").strip() or "private"
        party_sig = ",".join(sorted(base_party))
        norm_text = re.sub(r"\s+", "", clean_owner) or str(clean_owner)
        digest = hashlib.sha1(f"{scope_sig}:{party_sig}|{norm_text}".encode("utf-8")).hexdigest()[:10]
        plan_ref = f"plan:{int(ts // 300)}:{digest}"

        due_from, due_to = parse_due_window(clean_owner, ts)

        live_owner = _live_buy_text(clean_owner)
        is_past_or_neg_buy = any(v in clean_owner for v in _BUY_VERBS) and not any(v in live_owner for v in _BUY_VERBS)
        extracted: List[Dict[str, Any]] = [dict(it) for it in (intents or ()) if isinstance(it, dict)]
        if extracted:
            for it in extracted:
                raw_arg = str(it.get("raw_item") or it.get("item") or "").strip()
                resolved = self._resolve_item_id(it.get("item"))
                if resolved:
                    it["item"] = resolved
                    it["raw_item"] = ""
                    it["kind"] = "errand"
                    continue
                ctx_text = f"{clean_owner}\n{it.get('text') or ''}"
                if it.get("kind") == "errand" or (not is_past_or_neg_buy and any(v in ctx_text for v in _BUY_VERBS)):
                    det_item, det_qty = self._detect_errand_item(ctx_text)
                    if det_item:
                        it["item"] = det_item
                        it["raw_item"] = ""
                        it["kind"] = "errand"
                        if not it.get("qty"):
                            it["qty"] = det_qty
                        continue
                    if raw_arg and raw_arg.lower() not in ("none", "null"):
                        inferred_raw = raw_arg
                    elif it.get("kind") == "errand":
                        # The extractor already judged this an errand; still skip past/negated/question clauses.
                        inferred_raw = extract_raw_buy_noun(ctx_text)
                    else:
                        inferred_raw = extract_buy_request_noun(clean_owner, clean_reply)
                    if inferred_raw:
                        it["raw_item"] = inferred_raw[:24]
                        it["kind"] = "errand"
                it.pop("item", None)
            if clean_owner and not is_past_or_neg_buy:
                buy_item, buy_qty = self._detect_errand_item(clean_owner)
                if buy_item and not any(x.get("item") == buy_item for x in extracted):
                    extracted.append({
                        "text": f"和房主约好采购：{clean_owner[:72]}",
                        "kind": "errand",
                        "item": buy_item,
                        "qty": buy_qty,
                        "答应了的": list(base_agreed),
                    })
        elif clean_owner:
            raw_buy = ""
            buy_item, buy_qty = self._detect_errand_item(clean_owner)
            if buy_item:
                extracted.append({
                    "text": f"和房主约好采购：{clean_owner[:72]}",
                    "kind": "errand",
                    "item": buy_item,
                    "qty": buy_qty,
                    "答应了的": list(base_agreed),
                })
            else:
                raw_buy = extract_buy_request_noun(clean_owner, clean_reply)
                if raw_buy:
                    extracted.append({
                        "text": f"待办采购（未在超市词表）：{clean_owner[:68]}",
                        "kind": "errand",
                        "raw_item": raw_buy,
                        "答应了的": list(base_agreed),
                    })
            if (
                any(w in clean_owner for w in _ALLERGY_MEMO_WORDS)
                and not any(w in clean_owner for w in _COOK_COMMIT_WORDS)
                and not diet_memo_blocked(clean_owner)
            ):
                extracted.append({
                    "text": f"记住房主饮食禁忌：{clean_owner[:80]}",
                    "kind": "memo",
                    "答应了的": list(base_agreed),
                })
            else:
                has_buy_phrase = bool(raw_buy) or any(v in live_owner for v in _BUY_VERBS)
                if (not buy_item and not has_buy_phrase) and owner_proposes_plan(clean_owner):
                    extracted.append({
                        "text": f"和房主聊到：{clean_owner[:80]}",
                        "kind": "chat_promise",
                        "答应了的": list(base_agreed),
                    })

        declined = reply_declines(clean_reply)
        enriched_intents: List[Dict[str, Any]] = []
        for it in extracted:
            item_party = _clean_actors(it.get("party") or base_party, fallback=actor)
            item_agreed = _clean_actors(it.get("agreed") or it.get("答应了的") or base_agreed, fallback=actor)
            summary = str(it.get("text") or "")[:180]
            eff_kind = self._classify_kind(
                str(it.get("kind") or "chat_promise"),
                clean_owner,
                summary,
                has_item=bool(it.get("item")),
                has_raw=bool(str(it.get("raw_item") or "").strip()),
                party_len=len(item_party),
            )
            if declined and eff_kind != "memo" and not (str(it.get("raw_item") or "").strip() and not it.get("item")):
                # She said no (or asked to move it): that is not a promise. Memos are facts about the owner and stay,
                # and an out-of-catalog errand is only a reminder to the owner, never a promise to buy.
                continue
            i_from = it.get("due_from") if it.get("due_from") is not None else (None if eff_kind == "memo" else due_from)
            i_to = it.get("due_to") if it.get("due_to") is not None else (None if eff_kind == "memo" else due_to)
            eff_ref = str(it.get("plan_ref") or plan_ref)
            enriched = {
                **it,
                "text": summary,
                "kind": eff_kind,
                "status": "open",
                "party": item_party,
                "agreed": item_agreed,
                "答应了的": item_agreed,
                "due_from": i_from,
                "due_to": i_to,
                "plan_ref": eff_ref,
            }
            enriched_intents.append(enriched)

        payload = {
            "at": ts,
            "where": where,
            "proposer_id": str(proposer_id or "user:owner"),
            "owner": clean_owner[:300],
            "her": clean_reply[:300],
            "owner_said": [clean_owner[:300]] if clean_owner else [],
            "she_said": [clean_reply[:300]] if clean_reply else [],
            "intents": enriched_intents,
        }

        self.init()
        with contextlib.closing(self._conn()) as c:
            with c:
                cur = c.execute(
                    "INSERT INTO owner_chat_queue(actor, body, state, created_at) VALUES(?, ?, 'queued', ?)",
                    (actor, json.dumps(payload, ensure_ascii=False), ts),
                )
                for idx, item in enumerate(enriched_intents):
                    iid = f"chat:{cur.lastrowid}:{idx}"
                    eff_kind = item["kind"]
                    if eff_kind == "memo":
                        exp = ts + 3650 * 86400.0
                    elif item.get("due_to") is not None:
                        exp = max(ts + 3600.0, float(item["due_to"]))
                    else:
                        ttl = float(item.get("expires_s") or 24 * 3600)
                        exp = ts + ttl

                    eff_ref = item["plan_ref"]
                    sib_rows = c.execute(
                        "SELECT intent_id, party_json, agreed_json FROM house_intents "
                        "WHERE plan_ref=? AND state='open'",
                        (eff_ref,),
                    ).fetchall()
                    merged_party = list(item["party"])
                    merged_agreed = list(item["agreed"])
                    for sr in sib_rows:
                        for a in _loads_list(sr["party_json"]):
                            if a not in merged_party:
                                merged_party.append(a)
                        for a in _loads_list(sr["agreed_json"]):
                            if a not in merged_agreed:
                                merged_agreed.append(a)

                    c.execute(
                        "INSERT OR REPLACE INTO house_intents("
                        "intent_id, actor, summary, kind, state, item, raw_item, qty, created_at, expires_at, "
                        "proposer_id, party_json, agreed_json, due_from, due_to, plan_ref, updated_at"
                        ") VALUES(?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            iid,
                            actor,
                            item["text"],
                            eff_kind,
                            item.get("item") or None,
                            str(item.get("raw_item") or "")[:24],
                            int(item.get("qty") or 1),
                            ts,
                            exp,
                            str(proposer_id or "user:owner"),
                            json.dumps(merged_party, ensure_ascii=False),
                            json.dumps(merged_agreed, ensure_ascii=False),
                            item.get("due_from"),
                            item.get("due_to"),
                            eff_ref,
                            ts,
                        ),
                    )
                    if sib_rows:
                        c.execute(
                            "UPDATE house_intents SET party_json=?, agreed_json=?, updated_at=? "
                            "WHERE plan_ref=? AND state='open'",
                            (
                                json.dumps(merged_party, ensure_ascii=False),
                                json.dumps(merged_agreed, ensure_ascii=False),
                                ts,
                                eff_ref,
                            ),
                        )
                return cur.lastrowid


IntentsLite = CommitmentStore

__all__ = [
    "CommitmentStore",
    "IntentsLite",
    "SCHEMA",
    "owner_proposes_plan",
    "parse_due_window",
    "reply_declines",
]
