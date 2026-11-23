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
_COOK_COMMIT_WORDS = ("做饭", "下厨", "我来做", "我煮", "给你做", "帮做", "做顿", "做一顿", "做晚饭", "煮晚饭", "做早饭", "做午饭")
_OUTING_WORDS = ("出门", "晴空都", "逛", "图书馆", "咖啡店", "喫茶", "超市", "车站")
_TOGETHER_WORDS = ("一起", "约好", "约了", "大扫除", "桌游", "看电影", "开黑", "联机")
_DISH_WORDS = ("咖喱", "火锅", "寿喜锅", "乌冬", "拉面", "意面", "炒面", "蛋包饭", "味噌汤", "炒饭", "松饼", "司康")
_VALID_KINDS = frozenset({"errand", "cook", "memo", "outing", "together", "chat_promise"})


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


def parse_due_window(text: str, now: float) -> Tuple[Optional[float], Optional[float]]:
    """Parse relative JST due phrases (`今晚`, `明天`, `明早`, `明晚`, `周末`, `等会儿`)."""
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
                "SELECT intent_id, actor, summary, kind, item, qty, created_at, expires_at, "
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
            "SELECT intent_id, actor, summary, kind, state, item, qty, created_at, expires_at, "
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
        iid = str(row["intent_id"])
        item = str(row["item"] or "")
        req_qty = max(1, int(row["qty"] or 1))
        summary = str(row["summary"] or "")

        is_errand = intent_kind == "errand" or bool(item) or any(v in summary for v in _BUY_VERBS)
        if is_errand:
            if not item and not any(v in summary for v in _BUY_VERBS):
                return None
            accum_qty = 0
            last_hit: Optional[Tuple[float, str]] = None
            for key, body_raw in ev_rows:
                try:
                    ev = json.loads(body_raw)
                except ValueError:
                    continue
                ev_at = float(ev.get("at") or 0)
                if ev_at < created - 1.0:
                    continue
                kind = ev.get("kind")
                if kind in ("purchased", "brought_home"):
                    fp = str(ev.get("fingerprint") or "")
                    if ev.get("intent_ref") == iid or ev.get("intent_id") == iid or fp.startswith(f"{iid}:"):
                        return ev_at or created, key
                    if item and ev.get("actor") in party and ev.get("item") == item:
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
            if ev_at < created - 1.0:
                continue
            ev_actor = ev.get("actor")
            ev_kind = str(ev.get("kind") or "")
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
                elif ev_kind == "plan_finished" and ev.get("plan_kind") == "cook":
                    return ev_at or created, key
            elif intent_kind in ("outing", "together"):
                if ev_actor in party and ev_kind in ("arrived", "departed", "invite_settled", "plan_finished"):
                    if ev_kind == "invite_settled" or ev_kind == "plan_finished":
                        return ev_at or created, key
                    if intent_kind == "outing" or ev_kind == "arrived":
                        return ev_at or created, key
            else:
                if ev_actor == actor and ev_kind == "activity":
                    act = str(ev.get("activity") or "")
                    label = ""
                    if self.vocab and act in getattr(self.vocab, "ACTIVITIES", {}):
                        label = str(self.vocab.ACTIVITIES[act].get("label") or "")
                    if any(
                        w and w in summary
                        for w in (act, label, "看书" if act == "reading" else "", "散步" if act == "stroll" else "")
                    ):
                        return ev_at or created, key
                elif ev_actor == actor and ev_kind in ("arrived", "plan_finished"):
                    if any(w in summary for w in ("出门", "去", "晴空都", "逛")):
                        return ev_at or created, key

        for sid, ts, actors_raw, text in script_rows:
            if float(ts) < created - 1.0:
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
            if "睡" in t and "睡" in summary and intent_kind == "chat_promise":
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
                    "SELECT intent_id, actor, summary, kind, item, qty, created_at, party_json "
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

    def _detect_errand_item(self, text: str) -> Tuple[Optional[str], int]:
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

    def _classify_kind(self, raw_kind: str, owner_text: str, summary: str, *, has_item: bool, party_len: int) -> str:
        if has_item:
            return "errand"
        combined = f"{owner_text}\n{summary}"
        if any(w in combined for w in _ALLERGY_MEMO_WORDS) and not any(w in combined for w in _COOK_COMMIT_WORDS):
            return "memo"
        if any(v in combined for v in _BUY_VERBS):
            return "errand"
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
        norm_text = re.sub(r"\s+", "", clean_owner) or str(clean_owner)
        digest = hashlib.sha1(norm_text.encode("utf-8")).hexdigest()[:10]
        plan_ref = f"plan:{int(ts // 300)}:{digest}"

        due_from, due_to = parse_due_window(clean_owner, ts)

        extracted: List[Dict[str, Any]] = [dict(it) for it in (intents or ()) if isinstance(it, dict)]
        if extracted:
            for it in extracted:
                resolved = self._resolve_item_id(it.get("item"))
                if resolved:
                    it["item"] = resolved
                    it["kind"] = "errand"
                    continue
                ctx_text = f"{clean_owner}\n{it.get('text') or ''}"
                if it.get("kind") == "errand" or any(v in ctx_text for v in _BUY_VERBS):
                    det_item, det_qty = self._detect_errand_item(ctx_text)
                    if det_item:
                        it["item"] = det_item
                        it["kind"] = "errand"
                        if not it.get("qty"):
                            it["qty"] = det_qty
                        continue
                it.pop("item", None)
            if clean_owner:
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
            combined = f"{clean_owner}\n{clean_reply}"
            buy_item, buy_qty = self._detect_errand_item(clean_owner)
            if buy_item:
                extracted.append({
                    "text": f"和房主约好采购：{clean_owner[:72]}",
                    "kind": "errand",
                    "item": buy_item,
                    "qty": buy_qty,
                    "答应了的": list(base_agreed),
                })
            if any(w in clean_owner for w in _ALLERGY_MEMO_WORDS) and not any(w in clean_owner for w in _COOK_COMMIT_WORDS):
                extracted.append({
                    "text": f"记住房主饮食禁忌：{clean_owner[:80]}",
                    "kind": "memo",
                    "答应了的": list(base_agreed),
                })
            else:
                keywords = (
                    "早点睡", "去睡", "做饭", "火锅", "咖喱", "喝茶", "下午茶", "打游戏",
                    "出门", "买", "看书", "散步", "一起", "晴空都",
                )
                cook_or_life = tuple(k for k in keywords if k != "买")
                has_buy_phrase = any(v in clean_owner for v in _BUY_VERBS)
                if (not buy_item and not has_buy_phrase) and any(k in combined for k in cook_or_life):
                    extracted.append({
                        "text": f"和房主聊到：{clean_owner[:80]}",
                        "kind": "chat_promise",
                        "答应了的": list(base_agreed),
                    })

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
                party_len=len(item_party),
            )
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
                        "intent_id, actor, summary, kind, state, item, qty, created_at, expires_at, "
                        "proposer_id, party_json, agreed_json, due_from, due_to, plan_ref, updated_at"
                        ") VALUES(?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            iid,
                            actor,
                            item["text"],
                            eff_kind,
                            item.get("item") or None,
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
    "parse_due_window",
]
