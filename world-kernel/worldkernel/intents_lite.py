"""Built-in zero-dependency intents store & lightweight chat promise extractor.

Replaces the closed-source host `intents.py` module when running ai-house-kit standalone so
`INTENTS=1` and `owner_chat_queue` work out-of-the-box without external private services.
"""
import contextlib
import json
import re
import sqlite3
import time

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
    evidence_key TEXT
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


class IntentsLite:
    def __init__(self, world_db, vocab=None):
        self.world_db = world_db
        self.vocab = vocab

    def _conn(self):
        c = sqlite3.connect(self.world_db, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def init(self):
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
                ):
                    if name not in cols:
                        c.execute(f"ALTER TABLE house_intents ADD COLUMN {name} {ddl}")

    def kernel_context(self, actor, now):
        if not self.world_db:
            return []
        self.init()
        with contextlib.closing(self._conn()) as c:
            rows = c.execute(
                "SELECT intent_id, summary, kind, item, qty, created_at, expires_at FROM house_intents "
                "WHERE actor=? AND state='open' AND expires_at>? ORDER BY created_at DESC LIMIT 8",
                (actor, float(now)),
            ).fetchall()
            return [dict(r) for r in rows]

    def errands(self, actor, now):
        if not self.world_db:
            return []
        self.init()
        with contextlib.closing(self._conn()) as c:
            rows = c.execute(
                "SELECT intent_id, item, qty FROM house_intents "
                "WHERE actor=? AND state='open' AND expires_at>? AND item IS NOT NULL AND item!='' "
                "ORDER BY created_at ASC LIMIT 4",
                (actor, float(now)),
            ).fetchall()
            out = []
            ingredients = getattr(self.vocab, "INGREDIENTS", None) if self.vocab else None
            for r in rows:
                if ingredients is not None and r["item"] not in ingredients:
                    continue
                qty = max(1, min(3, int(r["qty"] or 1)))
                out.append({
                    "intent": r["intent_id"],
                    "item": r["item"],
                    "qty": qty,
                    "account": actor,
                    "beneficiary": [],
                    "fingerprint": f"{r['intent_id']}:{r['item']}:{qty}",
                })
            return out

    def list_intents(self, actor=None, limit=30):
        if not self.world_db:
            return []
        self.init()
        with contextlib.closing(self._conn()) as c:
            if actor:
                rows = c.execute(
                    "SELECT intent_id, actor, summary, kind, state, item, qty, created_at, expires_at, completed_at, evidence_key "
                    "FROM house_intents WHERE actor=? ORDER BY created_at DESC LIMIT ?",
                    (actor, int(limit)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT intent_id, actor, summary, kind, state, item, qty, created_at, expires_at, completed_at, evidence_key "
                    "FROM house_intents ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]

    def _match_evidence(self, row, ev_rows, script_rows):
        actor = row["actor"]
        created = float(row["created_at"])
        iid = row["intent_id"]
        item = row["item"] or ""
        summary = row["summary"] or ""
        for key, body_raw in ev_rows:
            try:
                ev = json.loads(body_raw)
            except ValueError:
                continue
            if float(ev.get("at") or 0) < created - 1.0:
                continue
            kind = ev.get("kind")
            if kind == "purchased":
                if ev.get("intent_ref") == iid or (item and ev.get("actor") == actor and ev.get("item") == item):
                    return float(ev.get("at") or created), key
            elif ev.get("actor") == actor:
                if kind == "activity":
                    act = str(ev.get("activity") or "")
                    label = ""
                    if self.vocab and act in getattr(self.vocab, "ACTIVITIES", {}):
                        label = str(self.vocab.ACTIVITIES[act].get("label") or "")
                    if any(w and w in summary for w in (act, label, "看书" if act == "reading" else "", "散步" if act == "stroll" else "")):
                        return float(ev.get("ended_at") or ev.get("at") or created), key
                elif kind in ("arrived", "plan_finished"):
                    if any(w in summary for w in ("出门", "去", "晴空都", "逛")):
                        return float(ev.get("at") or created), key
        for sid, ts, actors_raw, text in script_rows:
            if float(ts) < created - 1.0:
                continue
            try:
                actors = json.loads(actors_raw or "[]")
            except ValueError:
                actors = []
            if actor not in actors:
                continue
            t = str(text or "")
            if ("做了" in t or "做好了" in t) and not any(w in summary for w in ("过敏", "忌口", "不吃")) and any(
                w in summary for w in ("做饭", "下厨", "煮", "咖喱", "火锅", "晚饭", "午饭", "早饭")
            ):
                return float(ts), f"script:{sid}"
            if "睡" in t and "睡" in summary:
                return float(ts), f"script:{sid}"
        return None

    def sweep(self, now):
        if not self.world_db:
            return
        self.init()
        ts = float(now)
        with contextlib.closing(self._conn()) as c:
            with c:
                open_rows = c.execute(
                    "SELECT intent_id, actor, summary, kind, item, created_at FROM house_intents "
                    "WHERE state='open' ORDER BY created_at ASC"
                ).fetchall()
                if open_rows:
                    ev_rows = c.execute(
                        "SELECT key, body FROM intent_evidence ORDER BY event_id ASC LIMIT 500"
                    ).fetchall()
                    has_script = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='script'").fetchone()
                    script_rows = (
                        c.execute("SELECT id, ts, actors, text FROM script WHERE source='kernel' ORDER BY id DESC LIMIT 300").fetchall()
                        if has_script else []
                    )
                    for r in open_rows:
                        hit = self._match_evidence(r, ev_rows, script_rows)
                        if hit:
                            c.execute(
                                "UPDATE house_intents SET state='completed', completed_at=?, evidence_key=? "
                                "WHERE intent_id=? AND state='open'",
                                (hit[0], hit[1], r["intent_id"]),
                            )
                c.execute("UPDATE house_intents SET state='expired' WHERE state='open' AND expires_at<=?", (ts,))

    def _resolve_item_id(self, raw_item):
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

    def _detect_errand_item(self, text):
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

    def enqueue_chat(self, actor, owner_text, reply_text="", where="private", intents=None, now=None):
        """Record a chat turn into world.db owner_chat_queue so ChatLink wakes the roommate's house decision."""
        if not self.world_db:
            return None
        ts = float(now if now is not None else time.time())
        extracted = [dict(it) for it in (intents or ()) if isinstance(it, dict)]
        if extracted:
            for it in extracted:
                resolved = self._resolve_item_id(it.get("item"))
                if resolved:
                    it["item"] = resolved
                    it["kind"] = "errand"
                    continue
                ctx_text = f"{owner_text}\n{it.get('text') or ''}"
                if it.get("kind") == "errand" or any(v in ctx_text for v in _BUY_VERBS):
                    det_item, det_qty = self._detect_errand_item(ctx_text)
                    if det_item:
                        it["item"] = det_item
                        it["kind"] = "errand"
                        if not it.get("qty"):
                            it["qty"] = det_qty
                        continue
                it.pop("item", None)
            if owner_text:
                buy_item, buy_qty = self._detect_errand_item(owner_text)
                if buy_item and not any(x.get("item") == buy_item for x in extracted):
                    extracted.append({
                        "text": f"和房主约好采购：{owner_text[:72]}",
                        "kind": "errand",
                        "item": buy_item,
                        "qty": buy_qty,
                        "答应了的": [actor],
                    })
        elif owner_text:
            combined = f"{owner_text}\n{reply_text}"
            buy_item, buy_qty = self._detect_errand_item(owner_text)
            if buy_item:
                extracted.append({
                    "text": f"和房主约好采购：{owner_text[:72]}",
                    "kind": "errand",
                    "item": buy_item,
                    "qty": buy_qty,
                    "答应了的": [actor],
                })
            # Fast heuristic promise detector when no extra LLM extractor call is configured
            keywords = ("早点睡", "去睡", "做饭", "火锅", "咖喱", "喝茶", "下午茶", "打游戏", "出门", "买", "看书", "散步")
            cook_or_life = tuple(k for k in keywords if k != "买")
            if any(k in combined for k in cook_or_life) or (not buy_item and "买" in combined):
                extracted.append({
                    "text": f"和房主聊到：{owner_text[:80]}",
                    "kind": "chat_promise",
                    "答应了的": [actor],
                })
        payload = {
            "at": ts,
            "where": where,
            "owner": owner_text[:300],
            "her": reply_text[:300],
            "intents": extracted,
        }
        self.init()
        with contextlib.closing(self._conn()) as c:
            with c:
                cur = c.execute(
                    "INSERT INTO owner_chat_queue(actor, body, state, created_at) VALUES(?, ?, 'queued', ?)",
                    (actor, json.dumps(payload, ensure_ascii=False), ts),
                )
                for idx, item in enumerate(extracted):
                    iid = f"chat:{cur.lastrowid}:{idx}"
                    ttl = float(item.get("expires_s") or 24 * 3600)
                    c.execute(
                        "INSERT OR REPLACE INTO house_intents(intent_id, actor, summary, kind, state, item, qty, created_at, expires_at) "
                        "VALUES(?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                        (
                            iid,
                            actor,
                            str(item.get("text") or "")[:180],
                            str(item.get("kind") or "chat_promise"),
                            item.get("item") or None,
                            int(item.get("qty") or 1),
                            ts,
                            ts + ttl,
                        ),
                    )
                return cur.lastrowid
