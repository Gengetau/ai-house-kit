"""Words about buying, for the chat: what the owner asked to have bought, and what the reply does about it.

Moved unchanged from the kit's earlier promise code (`intents_lite.py`, now gone). Nothing here touches a promise:
these are lookups in the house's catalog (`vocab`) and the rules that read a line of chat, used by the daemon to say,
before her reply, that the thing asked for cannot be bought (`unbuyable_noun`) and to leave the "买不了" tail off a
reply that already says no (`reply_declines`). Functions that need the catalog take it as `vocab` (a pack namespace
with `INGREDIENTS` and `NAMES`); `None` falls back to the built-in item words.
"""
from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Tuple


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
# A request marker in the clause right before a verb-initial purchase clause ("路过便利店，捎份报纸").

def _clauses(text: str) -> List[str]:
    parts = re.split(r"(?<=[，,。.！!？?；;\n…~～])", str(text or ""))
    return [p.strip() for p in parts if p and p.strip()]



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


def resolve_item_id(vocab: Any, raw_item: Any) -> Optional[str]:
    raw = str(raw_item or "").strip()
    if not raw:
        return None
    ingredients = getattr(vocab, "INGREDIENTS", None) if vocab else None
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


def detect_errand_item(vocab: Any, text: str) -> Tuple[Optional[str], int]:
    """(item id, quantity) of the catalog item the line asks to have bought, or (None, 1)."""
    text = _live_buy_text(text)
    names = getattr(vocab, "NAMES", None) if vocab else None
    if isinstance(names, Mapping):
        # "米娜" is a person, not rice: names never take part in item matching.
        for nm in sorted({str(v) for v in names.values() if len(str(v)) >= 2}, key=len, reverse=True):
            text = text.replace(nm, " ")
    if not any(v in text for v in _BUY_VERBS):
        return None, 1
    ingredients = getattr(vocab, "INGREDIENTS", None) if vocab else None
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


def unbuyable_noun(vocab: Any, owner_text: str) -> str:
    """The out-of-catalog thing the owner explicitly asks the resident to buy, or "" (nothing / buyable)."""
    text = str(owner_text or "")
    if not text or detect_errand_item(vocab, text)[0]:
        return ""
    noun = extract_buy_request_noun(text)
    return "" if not noun or resolve_item_id(vocab, noun) else noun

