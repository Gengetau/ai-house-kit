"""What is in the old house goes into the kernel at genesis.

The old world keeps the house's things in world.db `items` (a name somebody wrote, a place, a count) and every
owner edit from the console as an `item_owner` event. The kernel starts from its own STOCK, so without this the
owner's 75 kinds / 329 pieces of snacks would be gone from the house at the 11-07 cutover. Here:

- category(name): the catalog item a freely written name is. Ordered keyword rules, specific before generic
  (「巧克力最中雪糕」 is ice cream, 「芥末牛肉薯片」 chips, 「水果果冻杯」 dessert). A food no rule knows is
  `other_snack` (其它零食) and keeps its own name in the source; the report lists every such name.
- legacy_items(world_db): read-only, the rows with something left, each with the owner events that put it there.
- migrate(rows, ...): one kernel resource per old row (`<item>@legacy:<old id>`, so two rows never merge), its count,
  and a source saying where it came from (old row id, the name as written, place, unit, who last changed it, the
  owner's event ids). Not copied, each counted: things (the kernel has its own anchors) and the house's own staples
  STOCK already fills (eggs, rice, bread, ...) unless the owner gave them.

Genesis runs once per kernel db (store meta `seeded`): a restart never copies again.
"""
import json
import hashlib
import os
import re
import sqlite3
import time

OTHER = "other_snack"
PREFIX = "legacy_item:"
# (catalog item, keywords): the first rule with a keyword in the name wins, so the order matters
RULES = (
    ("ice_cream", ("冰淇淋", "冰激凌", "雪糕", "哈根达斯", "雪见大福", "Papico", "冰棍")),
    ("dessert", ("果冻", "棉花糖", "年轮蛋糕", "铜锣烧", "蛋糕", "大福", "羊羹")),
    ("pudding_cup", ("布丁",)),
    ("rice_cracker", ("米饼", "仙贝", "米果", "雪饼", "煎饼", "柿种", "柿の種")),
    ("candy", ("软糖", "糖果", "硬糖", "奶糖", "棒棒糖", "嗨啾", "Hi-Chew", "Puccho", "グミ")),
    ("chips", ("薯片", "薯条", "玉米片", "玉米脆", "虾条", "干脆面", "膨化", "品客", "Pringles", "Jagabee", "じゃがりこ")),
    ("cup_noodle", ("杯面", "泡面", "方便面", "乌冬", "炒面", "拉面")),
    ("chocolate", ("巧克力", "百奇", "Pocky", "KitKat", "竹笋之里", "蘑菇之山", "Meltykiss", "黑雷")),
    ("cookie", ("饼干", "曲奇", "小圆饼", "奥利奥", "Oreo", "威化", "薄酥卷", "派之实", "CountryMa")),
    ("melon_soda", ("蜜瓜汽水", "蜜瓜苏打")),
    ("cola", ("可乐",)),
    ("soda", ("汽水", "雪碧", "苏打", "弹珠")),
    ("bottled_tea", ("午后红茶", "午後の紅茶", "乌龙茶", "绿茶", "麦茶", "冰红茶")),
    ("black_tea", ("红茶茶叶",)),
    ("bottled_tea", ("奶茶",)),
    ("ready_steak", ("牛排",)),
    ("coffee", ("咖啡",)),
    ("juice", ("果汁", "可尔必思", "Calpis", "乳酸菌", "草莓牛奶", "果奶")),
    ("onigiri", ("饭团",)),
    ("bread", ("吐司", "面包")),
    ("fruit", ("水果", "草莓", "苹果", "香蕉", "橘子", "葡萄", "蜜瓜")),
    # ingredients come after every snack rule (「幸福黄油薯片」 is chips, not butter)
    ("milk", ("牛奶",)), ("egg", ("鸡蛋",)), ("tofu", ("豆腐",)), ("miso", ("味噌",)), ("natto", ("纳豆",)),
    ("pork", ("猪肉",)), ("chicken", ("鸡肉",)), ("beef", ("牛肉",)), ("fish", ("鱼",)), ("cheese", ("奶酪", "芝士")),
    ("butter", ("黄油",)), ("onion", ("洋葱",)), ("cabbage", ("卷心菜", "包菜")), ("potato", ("土豆", "马铃薯")),
    ("carrot", ("胡萝卜",)), ("curry_roux", ("咖喱",)), ("flour", ("面粉",)), ("pasta", ("意面", "意大利面")),
    ("noodle", ("面条",)), ("pumpkin", ("南瓜",)), ("green_pepper", ("青椒",)), ("bitter_melon", ("苦瓜",)),
)
EXACT = {"米": "rice", "大米": "rice", "白米": "rice"}
INGREDIENT_START = next(i for i, (item, _) in enumerate(RULES) if item == 'milk')
EXACT.update({word: item for item, words in RULES[INGREDIENT_START:] for word in words})
EXACT['红茶'] = 'black_tea'
# the old world's places (world.py ITEM_PLACES) and the room each one is in; food lives in the kernel kitchen
ROOM = {"fridge": "kitchen", "pantry": "kitchen", "snacks": "living", "living": "living", "study": "study",
        "yard": "yard", "bath": "bath"}


def category(name):
    """(catalog item, matched) for a freely written food name; (other_snack, False) when no rule knows it."""
    n = "".join(str(name or "").split())
    if n in EXACT:
        return EXACT[n], True
    low = n.lower()
    if n in ('杏仁豆腐', '牛肉干', '鲷鱼烧', '炒面面包'):
        return OTHER, False
    for item, words in RULES[:INGREDIENT_START]:
        if any(w.lower() in low for w in words):
            return item, True
    return OTHER, False


def legacy_items(world_db):
    """Read-only look at world.db items with qty > 0 (oldest row first), each with `owner_events`: the ids of the
    owner's console adds that put it there. [] when the file is not there."""
    if not world_db or not os.path.exists(world_db):
        return []
    c = sqlite3.connect(f"file:{world_db}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=1")
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "items" not in tables:  # a world.db without a house inventory (test fixtures, a fresh old world)
            return []
        rows = [dict(r) for r in c.execute("SELECT id, name, place, qty, unit, kind, owner, holder, note, by FROM items"
                                           " WHERE qty > 0 ORDER BY id")]
        adds = []
        for e in (c.execute("SELECT id, data FROM events WHERE kind='item_owner' ORDER BY id")
                  if "events" in tables else ()):
            try:
                d = json.loads(e["data"])
            except (TypeError, ValueError):
                continue
            if isinstance(d, dict) and d.get("op") == "add":
                adds.append((e["id"], d))
    finally:
        c.close()
    for r in rows:
        # a merge into an existing row names its id; a new row's add event has no id (world.owner_item), only
        # the name and place it was written with
        r["owner_events"] = [eid for eid, d in adds if d.get("id") == r["id"] or (
            d.get("id") is None and str(d.get("name") or "").strip()[:12] == r["name"] and d.get("place") == r["place"])]
        r["owner_qty"] = sum(d.get("qty", 0) for eid, d in adds
                             if eid in r["owner_events"] and type(d.get("qty")) is int and d['qty'] > 0)
    return rows


def source_of(resource):
    """The provenance dict of a migrated resource (None for anything else)."""
    s = (resource or {}).get("source") or ""
    return json.loads(s[len(PREFIX):]) if s.startswith(PREFIX) else None


def migrate(rows, stock, vocab, roster):
    """(kernel resources, report) for the old rows. Deterministic: the same rows give the same resources."""
    items = vocab.CATALOG["items"]
    out = []
    report = {"rows": 0, "qty": 0, "by_item": {}, "owner_rows": 0, "owner_qty": 0, "other": [], "skipped": []}
    for r in rows:
        qty = int(r.get("qty") or 0)
        if qty <= 0:
            continue
        if r.get("kind") != "food":
            report["skipped"].append({"id": r["id"], "name": r["name"], "qty": qty, "why": "thing"})
            continue
        item, matched = category(r["name"])
        given = bool(r.get("owner_events"))
        owner = r.get("owner") if r.get("owner") in roster else None
        if item in stock and not given and not owner:  # the house's own staple: the kernel fridge already has it
            report["skipped"].append({"id": r["id"], "name": r["name"], "qty": qty, "why": "stock"})
            continue
        if item in stock and given and not owner:
            # Console set also writes by=owner. Copy only explicit additions above the kernel's STOCK.
            qty = min(qty, r.get('owner_qty', 0))
            if qty <= 0:
                report['skipped'].append(dict(id=r['id'], name=r['name'], qty=r['qty'], why='stock'))
                continue
        if item not in items:  # a rule pointing outside the catalog would be a bug; never seed an unknown item
            raise ValueError(f"inventory rule maps {r['name']!r} to unknown item {item!r}")
        if owner:  # hers (the old owner column): only she uses it, where she keeps it
            location = vocab.bed(owner) if r.get("place") == "room" else ROOM.get(r.get("place"), "kitchen")
        else:
            location = "kitchen"
        src = {"legacy_item": r["id"], "name": r["name"], "place": r.get("place"), "unit": r.get("unit"),
               "by": r.get("by") or "", "owner_events": list(r.get("owner_events") or ()), "matched": matched}
        out.append({"id": f"{item}@legacy:{r['id']}", "kind": "food", "item": item, "location": location,
                    "holder": owner, "qty": qty, "source": PREFIX + json.dumps(src, ensure_ascii=False, sort_keys=True)})
        report["rows"] += 1
        report["qty"] += qty
        report["by_item"][item] = report["by_item"].get(item, 0) + qty
        if given:
            report["owner_rows"] += 1
            report["owner_qty"] += qty
        if not matched:
            report["other"].append(r["name"])
    return out, report


MAX_DELIVERY_CHARS = 32768
MAX_DELIVERY_LINES = 256
MAX_DELIVERY_QTY = 999
DELIVERY_PREFIX = 'food_delivery:'


def parse_food_list(text):
    """Full explicit quantity lists, bounded with counts. Supports name ×N[unit] and Markdown tables.

    Caller knows this is an actual delivery, not a wish list. A partial final line at the character boundary
    is discarded, never reinterpreted as a smaller quantity. No raw message is persisted.
    """
    cut = text[:MAX_DELIVERY_CHARS]
    if len(text) > len(cut) and not cut.endswith('\n'):
        cut = cut.rsplit('\n', 1)[0] if '\n' in cut else ''
    lines = cut.splitlines()
    truncated_chars = len(text) - len(cut)
    report = dict(truncated_chars=truncated_chars, truncated_lines=max(0, len(text.splitlines()) - min(len(lines), MAX_DELIVERY_LINES)),
                  rejected_lines=int(bool(truncated_chars)), rows=0, qty=0, unmatched=0)
    entries = []
    for line in lines[:MAX_DELIVERY_LINES]:
        line = line.strip()
        if not line:
            continue
        if line.startswith('|'):
            cells = [s.strip() for s in line.strip('|').split('|')]
            match = re.fullmatch(r'([0-9]+)', cells[1]) if len(cells) >= 2 else None
            name, number, unit = (cells[0], match[1], cells[2] if len(cells) > 2 else '') if match else ('', '', '')
        else:
            line = re.sub(r'^\s*(?:[-*•]\s+|[0-9]+[.、)）]\s*)', '', line)
            match = re.fullmatch(r'(.+?)\s*[×xX*]\s*([0-9]+)\s*([^0-9\W_]{0,4})\s*', line)
            name, number, unit = match.groups() if match else ('', '', '')
        name = name.strip().strip('*').strip()
        if not name or len(name) > 128 or not number or len(number) > 3 or not 1 <= int(number) <= MAX_DELIVERY_QTY:
            report['rejected_lines'] += 1
            continue
        item, matched = category(name)
        entries.append(dict(name=name, item=item, matched=matched, qty=int(number), unit=unit or '件'))
        report['qty'] += int(number)
        report['unmatched'] += int(not matched)
    report['rows'] = len(entries)
    return entries, report


def receive_food(kernel, principal, channel, message_id, text):
    """Authenticated explicit owner delivery. Source message is a stable idempotency key.

    Trusted adapter supplies cmd:owner; never infer it from a QQ nickname/tag or let a model submit it.
    Resources, fact events and receipt commit together. Return bounds/rejection counts to the caller.
    """
    if principal != 'cmd:owner':
        return dict(status='failed', reason='owner_only')
    if (not isinstance(channel, str) or not channel or len(channel) > 128 or
            not isinstance(message_id, (str, int)) or isinstance(message_id, bool) or
            not str(message_id) or len(str(message_id)) > 128 or not isinstance(text, str)):
        return dict(status='failed', reason='bad_delivery_source')
    key = hashlib.sha256(json.dumps([channel, str(message_id)], ensure_ascii=False).encode()).hexdigest()
    digest = hashlib.sha256(text.encode()).hexdigest()
    entries, report = parse_food_list(text)
    with kernel.s.tx() as db:
        db.execute('CREATE TABLE IF NOT EXISTS food_deliveries (source_key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)')
        prior = db.execute('SELECT digest, receipt FROM food_deliveries WHERE source_key=?', (key,)).fetchone()
        if prior:
            return json.loads(prior['receipt']) if prior['digest'] == digest else dict(status='failed', reason='delivery_conflict')
        if not entries:
            return dict(report, status='failed', reason='no_counted_food')
        for i, entry in enumerate(entries):
            rid = f'delivery:{key}:{i}'
            src = dict(entry, giver='owner', channel=channel, message_id=str(message_id))
            eid = kernel._emit('food_delivered', None, None, 'kitchen',
                               dict(entry, resource_id=rid, giver='owner', channel=channel, message_id=str(message_id)),
                               scope='room:kitchen', occurred=kernel.s.clock.now(), source_kind='owner_input',
                               source_ref=f'{channel}:{message_id}')
            src['event_id'] = eid
            db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES(?,'food',?,'kitchen',?,?)",
                       (rid, entry['item'], entry['qty'], DELIVERY_PREFIX + json.dumps(src, ensure_ascii=False, sort_keys=True)))
        receipt = dict(report, status='succeeded', source_key=key)
        db.execute('INSERT INTO food_deliveries VALUES(?,?,?)', (key, digest, json.dumps(receipt, sort_keys=True)))
    return receipt


def queue_food(world_db, principal, channel, message_id, text):
    """Console/CLI adapter: durable input only, never writes physical inventory outside the kernel."""
    if principal != 'cmd:owner':
        return dict(status='failed', reason='owner_only')
    if (not isinstance(channel, str) or not 1 <= len(channel) <= 128 or
            not isinstance(message_id, (str, int)) or isinstance(message_id, bool) or
            not 1 <= len(str(message_id)) <= 128 or not isinstance(text, str) or len(text) > 1048576):
        return dict(status='failed', reason='bad_delivery_source')
    key = hashlib.sha256(json.dumps([channel, str(message_id)], ensure_ascii=False).encode()).hexdigest()
    digest = hashlib.sha256(text.encode()).hexdigest()
    c = sqlite3.connect(world_db, timeout=5)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')  # serialize same-source submissions from concurrent console requests
            c.execute('CREATE TABLE IF NOT EXISTS food_input_queue (source_key TEXT PRIMARY KEY, channel TEXT NOT NULL, message_id TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT)')
            row = c.execute('SELECT digest,state,receipt FROM food_input_queue WHERE source_key=?', (key,)).fetchone()
            if row:
                if row[0] != digest:
                    return dict(status='failed', reason='delivery_conflict')
                return json.loads(row[2]) if row[2] else dict(status='queued', source_key=key)
            c.execute("INSERT INTO food_input_queue VALUES(?,?,?,?,?,'queued',NULL)",
                      (key, channel, str(message_id), text, digest))
        return dict(status='queued', source_key=key)
    finally:
        c.close()


def process_food_queue(kernel, world_db):
    """Scheduler owns all physical writes. Replay covers a crash between kernel commit and input ack."""
    c = sqlite3.connect(f'file:{world_db}?mode=rw', uri=True, timeout=5)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='food_input_queue'").fetchone():
            return
        rows = c.execute("SELECT source_key,channel,message_id,body FROM food_input_queue WHERE state='queued' ORDER BY rowid LIMIT 16").fetchall()
        for key, channel, mid, body in rows:
            receipt = receive_food(kernel, 'cmd:owner', channel, mid, body)
            with c:
                c.execute('UPDATE food_input_queue SET state=?,receipt=? WHERE source_key=?',
                          (receipt['status'], json.dumps(receipt, sort_keys=True), key))
    finally:
        c.close()


# ---------------------------------------------------------------- the house's stock as people see it
# After the 11-07 cutover world.db `items` is frozen; the kernel's resources are the stock. Every food resource is in
# the kernel kitchen (or held by her), so for the bots' card and the console each one gets a shelf like the old list
# had: the old row's place when it came from there, else by what it is.
SHELVES = {"fridge": "冰箱", "pantry": "厨房柜子", "snacks": "零食柜", "living": "客厅", "study": "书房", "yard": "院子",
           "bath": "浴室", "room": "卧室"}
DRY = {"rice", "flour", "pasta", "noodle", "curry_roux", "potato", "onion", "black_tea", "coffee", "bread", "cup_noodle"}
COLD_SNACKS = {"ice_cream", "pudding_cup", "dessert", "fruit", "onigiri"}
OWNER_STOCK_PREFIX = "owner_stock:"
OWNER_GIFT_PREFIX = "owner_gift:"  # one piece the owner handed her in the house (房主化身), see hand_over()


def _src(source):
    """(origin, provenance dict) of a resource's source string."""
    s = source or ""
    for prefix, origin in ((PREFIX, "legacy"), (DELIVERY_PREFIX, "delivery"), (OWNER_STOCK_PREFIX, "owner"),
                           (OWNER_GIFT_PREFIX, "gift")):
        if s.startswith(prefix):
            try:
                d = json.loads(s[len(prefix):])
            except ValueError:
                d = {}
            return origin, d if isinstance(d, dict) else {}
    if s.startswith("event:"):
        return "event", {"event_id": s[6:]}
    return ("genesis" if s == "life1_genesis" else "other"), {}


def shelf(item, location, cls, src_place=None):
    """Where in the house the old list would have put it (a SHELVES key). The old place only names a kitchen shelf
    (fridge/pantry/snacks): the kernel keeps all shared food in the kitchen, so an old 「客厅」 row is not shown as
    being in the living room; a row the kernel has elsewhere is shown where the kernel has it."""
    if location and location.startswith("room_"):
        return "room"
    if location and location != "kitchen" and location in SHELVES:
        return location
    if src_place in ("fridge", "pantry", "snacks"):
        return src_place
    if cls == "snack":
        return "fridge" if item in COLD_SNACKS else "pantry" if item in DRY else "snacks"
    if cls in ("ingredient", "drink"):
        return "pantry" if item in DRY else "fridge"
    return "fridge"  # cooked dishes left over, anything else


def stock_rows(kernel_db, since=None, empty=False, vocab=None):
    """Read-only: the kernel's food, one dict per resource (qty > 0 unless empty=True), oldest kind first.

    {id, item, name, label, qty, held, unit, shelf, location, holder, by, origin, eaten_today, cooked_today,
    given_today}.
    by: who brought it in ('owner', a roommate id, '' = the house had it); for a row carried over from the old list
    'owner' when one of his console adds put it there, else whoever last changed the old row (the old list kept no
    buyer). eaten_today / cooked_today: pieces eaten / cooked with since `since` (None = not counted).
    given_today: pieces of it the owner handed someone in the house since `since` (None = not counted).
    Raises FileNotFoundError when the kernel db is not there (before the first authority start).
    """
    if vocab is None:
        from . import vocab_life1 as vocab
    if not kernel_db or not os.path.exists(kernel_db):
        raise FileNotFoundError(kernel_db)
    items = vocab.CATALOG["items"]
    c = sqlite3.connect(f"file:{kernel_db}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=1")
        rows = [dict(r) for r in c.execute(
            "SELECT r.resource_id, r.item, r.location, r.holder, r.qty, r.source,"
            " (SELECT COALESCE(SUM(v.qty),0) FROM reservations v WHERE v.resource_id=r.resource_id AND v.state='held')"
            " AS held FROM resources r WHERE r.kind='food'" + ("" if empty else " AND r.qty>0") + " ORDER BY r.rowid")]
        eaten, cooked, given = {}, {}, {}
        if since is not None:
            for rid, cook, n in c.execute(
                    "SELECT v.resource_id, a.action_type='cook', SUM(v.qty) FROM reservations v JOIN actions a ON"
                    " a.action_id=v.action_id WHERE v.consume=1 AND v.state='used' AND a.finished_at>=?"
                    " GROUP BY v.resource_id, a.action_type='cook'", (since,)):
                (cooked if cook else eaten)[rid] = n
            for g in _gifts(c, since):
                given[g["from"]] = given.get(g["from"], 0) + 1
        actor_of = {}
        ids = [_src(r["source"])[1].get("event_id") for r in rows if r["source"].startswith("event:")]
        for i in range(0, len(ids), 500):
            part = [int(x) for x in ids[i:i + 500] if str(x).isdigit()]
            if part:
                actor_of.update({str(e[0]): e[1] for e in c.execute(
                    f"SELECT event_id, actor_id FROM events WHERE event_id IN ({','.join('?' * len(part))})", part)})
    finally:
        c.close()
    out = []
    for r in rows:
        origin, src = _src(r["source"])
        meta = items.get(r["item"], {})
        label = meta.get("label", r["item"])
        if origin == "legacy":
            by = "owner" if src.get("owner_events") else src.get("by") or ""
        elif origin in ("delivery", "owner", "gift"):
            by = "owner"
        elif origin == "event":
            by = actor_of.get(str(src.get("event_id")), "") or ""
            origin = "cooked" if "@act-" in r["resource_id"] else "bought"
        else:
            by = ""
        out.append({"id": r["resource_id"], "item": r["item"], "name": str(src.get("name") or label)[:40],
                    "label": label, "qty": r["qty"], "held": r["held"],
                    "unit": str(src.get("unit") or "")[:4] or "份",
                    "shelf": shelf(r["item"], r["location"], meta.get("class"), src.get("place")),
                    "location": r["location"], "holder": r["holder"] or "", "by": by, "origin": origin,
                    "eaten_today": eaten.get(r["resource_id"], 0) if since is not None else None,
                    "cooked_today": cooked.get(r["resource_id"], 0) if since is not None else None,
                    "given_today": given.get(r["resource_id"], 0) if since is not None else None})
    return out


def stock_takes(kernel_db, since, limit=60):
    """Read-only: what was taken out of the stock since `since`, one dict per eat / cook action, newest first:
    {at, actor, kind: 'eat' | 'cook', items: [{id, qty}]}. Counted exactly like stock_rows' eaten_today /
    cooked_today (a used, consuming reservation of an action finished since then), so the two always agree. A piece
    the owner handed someone is one more take: {at, actor: 'owner', kind: 'gift', to, items: [{id, qty: 1}]},
    counted like given_today.
    Raises FileNotFoundError when the kernel db is not there."""
    if not kernel_db or not os.path.exists(kernel_db):
        raise FileNotFoundError(kernel_db)
    c = sqlite3.connect(f"file:{kernel_db}?mode=ro", uri=True, timeout=5)
    try:
        c.execute("PRAGMA query_only=1")
        got = {}
        for aid, actor, typ, at, rid, n in c.execute(
                "SELECT a.action_id, a.actor_id, a.action_type, a.finished_at, v.resource_id, SUM(v.qty) FROM"
                " reservations v JOIN actions a ON a.action_id=v.action_id WHERE v.consume=1 AND v.state='used'"
                " AND a.finished_at>=? GROUP BY a.action_id, v.resource_id ORDER BY a.finished_at DESC, a.action_id,"
                " v.resource_id", (since,)):
            t = got.setdefault(aid, {"at": at, "actor": actor, "kind": "cook" if typ == "cook" else "eat", "items": []})
            t["items"].append({"id": rid, "qty": n})
        takes = list(got.values()) + [{"at": g["at"], "actor": "owner", "kind": "gift", "to": g["to"],
                                       "items": [{"id": g["from"], "qty": 1}]} for g in _gifts(c, since)]
    finally:
        c.close()
    takes.sort(key=lambda t: -t["at"])  # stable: same-time takes keep their order
    return takes[:max(0, int(limit))]


def _gifts(c, since):
    """The owner's hand-overs (owner_gave facts) since `since` that took a piece out of a stock row."""
    out = []
    for at, payload in c.execute("SELECT occurred_at, payload FROM events WHERE kind='owner_gave' AND occurred_at>=?"
                                 " ORDER BY event_id", (since,)):
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        if isinstance(p, dict) and isinstance(p.get("from"), str):
            out.append({"at": at, "to": p.get("to"), "from": p["from"]})
    return out


# ---------------------------------------------------------------- the owner edits the stock from the console
# Same road as a food delivery: world.py (console/CLI, owner already authenticated there) only queues the request in
# world.db; the authority scheduler applies it inside the kernel's own transaction, once per request key.
OWNER_STOCK_OPS = ("add", "set", "del")
OWNER_STOCK_CLASSES = ("ingredient", "snack", "drink", "dish")
# a queued edit older than this is not applied (queued under authority, shadow rollback, authority
# again days later must not write a days-old count); its receipt says expired
OWNER_STOCK_MAX_AGE_S = 3600


def queue_owner_stock(world_db, principal, op, args, key):
    """Durable input only: {status: queued|<the receipt>, source_key}. Never touches the kernel db."""
    if principal != "cmd:owner":
        return dict(status="failed", reason="owner_only")
    if op not in OWNER_STOCK_OPS or not isinstance(args, dict) or not isinstance(key, str) or not 1 <= len(key) <= 128:
        return dict(status="failed", reason="bad_request")
    body = json.dumps(dict(op=op, args=args), ensure_ascii=False, sort_keys=True)
    if len(body) > 4096:
        return dict(status="failed", reason="bad_request")
    digest = hashlib.sha256(body.encode()).hexdigest()
    c = sqlite3.connect(world_db, timeout=5)
    try:
        with c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("CREATE TABLE IF NOT EXISTS owner_stock_queue (source_key TEXT PRIMARY KEY, body TEXT NOT NULL,"
                      " digest TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, created REAL)")
            row = c.execute("SELECT digest, receipt FROM owner_stock_queue WHERE source_key=?", (key,)).fetchone()
            if row:
                if row[0] != digest:
                    return dict(status="failed", reason="request_conflict")
                return json.loads(row[1]) if row[1] else dict(status="queued", source_key=key)
            c.execute("INSERT INTO owner_stock_queue VALUES(?,?,?,'queued',NULL,?)", (key, body, digest, time.time()))
        return dict(status="queued", source_key=key)
    finally:
        c.close()


def _qty(v, lo):
    if type(v) is not int or not lo <= v <= MAX_DELIVERY_QTY:
        raise _Bad("bad_qty")
    return v


class _Bad(Exception):
    pass


def receive_owner_stock(kernel, principal, key, op, args):
    """Apply one owner edit to kernel resources; the receipt (also for a refusal) is kept so a replay is a no-op.

    add {name, qty 1..999, unit?, place?, holder?, item?, kind?}: a new resource `owner:<key>` (food only; the name
        picks the catalog item like a delivery does, unknown names are 其它零食); in the kitchen, or her bedroom when
        place=room and holder is given.
    set {id, qty 0..999} or {id, delta -999..999}: that food resource's count, absolute or relative to what the
        kernel has when it applies it (the console's ±1: an edit queued for a minute must not undo what was eaten
        meanwhile). Never below what someone has in hand right now (in_use), never outside 0..999.
    del {id}: the count to 0 (the row stays: what was eaten from it is still history). Not while in use.
    """
    if principal != "cmd:owner":
        return dict(status="failed", reason="owner_only")
    V = kernel.V
    digest = hashlib.sha256(json.dumps(dict(op=op, args=args), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with kernel.s.tx() as db:
        db.execute("CREATE TABLE IF NOT EXISTS owner_stock_receipts (source_key TEXT PRIMARY KEY, digest TEXT NOT NULL,"
                   " receipt TEXT NOT NULL)")
        prior = db.execute("SELECT digest, receipt FROM owner_stock_receipts WHERE source_key=?", (key,)).fetchone()
        if prior:
            return json.loads(prior["receipt"]) if prior["digest"] == digest else dict(status="failed",
                                                                                      reason="request_conflict")
        now = kernel.s.clock.now()
        try:
            if op not in OWNER_STOCK_OPS or not isinstance(args, dict):
                raise _Bad("bad_request")
            if op == "add":
                name = " ".join(str(args.get("name") or "").split())[:40]
                if not name:
                    raise _Bad("bad_name")
                if (args.get("kind") or "food") != "food":
                    raise _Bad("not_food")
                qty = _qty(args.get("qty"), 1)
                place = args.get("place") if args.get("place") in SHELVES else None
                holder = args.get("holder") if args.get("holder") in kernel.roster else None
                if place == "room" and not holder:
                    raise _Bad("room_needs_holder")
                meta = V.CATALOG["items"].get(args.get("item")) or {}
                if meta.get("kind") == "food" and meta.get("class") in OWNER_STOCK_CLASSES:
                    item, matched = args["item"], True
                else:
                    item, matched = category(name)
                location = V.bed(holder) if place == "room" else "kitchen"
                rid = "owner:" + hashlib.sha256(key.encode()).hexdigest()[:24]  # never two keys, one id
                src = dict(name=name, item=item, matched=matched, place=place, unit=str(args.get("unit") or "")[:4],
                           by="owner", source_key=key)
                eid = kernel._emit("owner_stock_changed", None, None, location,
                                   dict(op="add", resource_id=rid, item=item, name=name, qty=qty, holder=holder),
                                   scope=f"room:{location}", occurred=now, source_kind="owner_input",
                                   source_ref=f"console:{key}")
                src["event_id"] = eid
                db.execute("INSERT INTO resources(resource_id,kind,item,location,holder,qty,source)"
                           " VALUES(?,'food',?,?,?,?,?)",
                           (rid, item, location, holder, qty,
                            OWNER_STOCK_PREFIX + json.dumps(src, ensure_ascii=False, sort_keys=True)))
                receipt = dict(status="succeeded", op=op, id=rid, item=item, qty=qty, matched=matched)
            else:
                rid = args.get("id")
                r = db.execute("SELECT * FROM resources WHERE resource_id=?", (rid,)).fetchone() \
                    if isinstance(rid, str) else None
                if not r or r["kind"] != "food":
                    raise _Bad("no_such_food")
                if op == "del":
                    qty = 0
                elif "delta" in args:
                    d = args["delta"]
                    if type(d) is not int or not d or abs(d) > MAX_DELIVERY_QTY:
                        raise _Bad("bad_qty")
                    qty = r["qty"] + d
                    if not 0 <= qty <= MAX_DELIVERY_QTY:
                        raise _Bad("bad_qty")
                else:
                    qty = _qty(args.get("qty"), 0)
                held = db.execute("SELECT COALESCE(SUM(qty),0) FROM reservations WHERE resource_id=? AND state='held'",
                                  (rid,)).fetchone()[0]
                if qty < held:
                    raise _Bad("in_use")
                db.execute("UPDATE resources SET qty=?, version=version+1 WHERE resource_id=?", (qty, rid))
                kernel._emit("owner_stock_changed", None, None, r["location"],
                             dict(op=op, resource_id=rid, item=r["item"], before=r["qty"], qty=qty),
                             scope=f"room:{r['location']}", occurred=now, source_kind="owner_input",
                             source_ref=f"console:{key}")
                receipt = dict(status="succeeded", op=op, id=rid, item=r["item"], before=r["qty"], qty=qty)
        except _Bad as e:
            receipt = dict(status="failed", op=op, reason=str(e))
        receipt["source_key"] = key
        db.execute("INSERT INTO owner_stock_receipts VALUES(?,?,?)", (key, digest, json.dumps(receipt, sort_keys=True)))
    return receipt


def process_owner_stock_queue(kernel, world_db):
    """Scheduler side of queue_owner_stock (same crash story as process_food_queue: the kernel receipt makes a
    replay after a crash between the two commits a no-op)."""
    c = sqlite3.connect(f"file:{world_db}?mode=rw", uri=True, timeout=5)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='owner_stock_queue'").fetchone():
            return
        rows = c.execute("SELECT source_key, body, created FROM owner_stock_queue WHERE state='queued' ORDER BY rowid"
                         " LIMIT 32").fetchall()
        for key, body, created in rows:
            try:
                b = json.loads(body)
            except ValueError:
                b = {}
            try:
                stale = time.time() - float(created) > OWNER_STOCK_MAX_AGE_S
            except (TypeError, ValueError):
                stale = True
            done = _owner_receipt(kernel, key) if stale else None  # applied before a long outage: that receipt wins
            if done:
                receipt = done
            elif stale:
                receipt = dict(status="failed", op=b.get("op"), reason="expired", source_key=key)
            else:
                try:
                    receipt = receive_owner_stock(kernel, "cmd:owner", key, b.get("op"), b.get("args"))
                except sqlite3.IntegrityError as e:  # a row that can never apply must not stay queued
                    receipt = dict(status="failed", op=b.get("op"), reason="conflict:" + str(e)[:80], source_key=key)
            with c:
                c.execute("UPDATE owner_stock_queue SET state=?, receipt=? WHERE source_key=?",
                          (receipt["status"], json.dumps(receipt, sort_keys=True), key))
    finally:
        c.close()


def _owner_receipt(kernel, key):
    """The kernel's own receipt for a request key, or None (read-only)."""
    with kernel.s.read() as db:
        try:
            r = db.execute("SELECT receipt FROM owner_stock_receipts WHERE source_key=?", (key,)).fetchone()
        except sqlite3.OperationalError:  # no edit was ever applied
            return None
    return json.loads(r[0]) if r else None


# ---------------------------------------------------------------- the owner hands her a snack in the house
# 房主化身走进小屋: the console queues his "give" in world.db (avatar.queue); the authority applies it in its own step
# (avatar.Avatar.apply, one kernel transaction). The stock side is here so it is counted like every other change:
# only a kitchen snack nobody has reserved, one piece off that row, a new row in her hand that says where it came
# from (stock_rows shows it as the owner's; given_today / stock_takes count the piece that left the row).
def giveable(kernel, db, rid, vocab=None):
    """(row, None) when one piece of resource `rid` can be handed over now, else (None, reason)."""
    vocab = vocab or kernel.V
    r = db.execute("SELECT * FROM resources WHERE resource_id=?", (rid,)).fetchone() if isinstance(rid, str) else None
    if not r or r["kind"] != "food" or r["holder"] is not None or r["location"] != "kitchen" \
            or r["item"] not in vocab.SNACKS:
        return None, "not_a_house_snack"
    if (kernel._free_qty(db, rid) or 0) < 1:
        return None, "none_left"
    return r, None


def stock_name(row, vocab):
    """The name the stock shows for a resource row: as written when it came in, else the catalog label."""
    label = vocab.CATALOG["items"].get(row["item"], {}).get("label", row["item"])
    return str(_src(row["source"])[1].get("name") or label)[:40]


def hand_over(kernel, db, row, to, location, gift_id, event_id, via="avatar", vocab=None):
    """Inside the caller's kernel transaction (after giveable): one piece of `row` leaves the kitchen stock and is
    hers (holder=to) in `location`, as resource `gift_id`. The fact (owner_gave, payload.from = row id) is the
    caller's; event_id ties the two together."""
    kernel._bump_resource(db, row["resource_id"], -1)
    _, src = _src(row["source"])
    prov = {"giver": "owner", "from": row["resource_id"], "event_id": event_id, "via": via,
            "name": stock_name(row, vocab or kernel.V), "unit": str(src.get("unit") or "")[:4]}
    db.execute("INSERT INTO resources(resource_id,kind,item,location,holder,qty,source) VALUES(?,'food',?,?,?,1,?)",
               (gift_id, row["item"], location, to,
                OWNER_GIFT_PREFIX + json.dumps(prov, ensure_ascii=False, sort_keys=True)))


def giveable_snacks(db, vocab):
    """Read side for the house page (any connection with row_factory=sqlite3.Row): the kitchen snacks he could hand
    over, one entry per name in the stock: {item, label, resource_id, qty}. qty = free pieces (minus reservations)
    of that name; resource_id = its oldest row with a free piece, the one a give takes from."""
    out = {}
    for r in db.execute("SELECT r.resource_id, r.item, r.qty, r.source, (SELECT COALESCE(SUM(v.qty),0) FROM"
                        " reservations v WHERE v.resource_id=r.resource_id AND v.state='held') AS held FROM"
                        " resources r WHERE r.kind='food' AND r.location='kitchen' AND r.holder IS NULL AND r.qty>0"
                        " ORDER BY r.rowid"):
        free = r["qty"] - r["held"]
        if r["item"] not in vocab.SNACKS or free <= 0:
            continue
        name = stock_name(r, vocab)
        s = out.setdefault((r["item"], name), {"item": r["item"], "label": name, "resource_id": r["resource_id"],
                                               "qty": 0})
        s["qty"] += free
    return sorted(out.values(), key=lambda s: (s["label"], s["item"]))
