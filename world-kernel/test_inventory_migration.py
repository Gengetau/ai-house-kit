"""what is left in the old house (world.db items, the owner's console adds) comes into the kernel
at genesis, once, each row with where it came from; names written freely map to catalog kinds."""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, JST, STOCK  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.legacy import LEGACY_SCHEMA as LEGACY  # noqa: E402
from worldkernel import inventory, vocab_life1 as V  # noqa: E402

# the owner's V2 list (10-30) as the 11-04 backfill wrote it, 75 kinds
V2 = {
    "chips": "湖池屋海苔盐薯片 神海苔盐薯片 披萨薯片 卡乐比原味薯片 浓汤薯片 幸福黄油薯片 坚脆黑胡椒薯片 沙拉味薯条杯 芝士味薯条杯 "
             "Jagabee薯条 品客酸奶油洋葱 芥末牛肉薯片 卡辣姆久辣薯片 DonTacos玉米片 河童虾条 焦糖玉米脆 干脆面 Potelong薯条",
    "chocolate": "草莓百奇 原味百奇 杏仁百奇 原味KitKat 抹茶KitKat 帆船巧克力 竹笋之里 蘑菇之山 黑雷巧克力 Meltykiss",
    "cookie": "派之实 小圆饼 动物饼干 奥利奥 CountryMa'am 月光黄油曲奇 玛丽饼干 露蒙德薄酥卷 爱丽丝威化卷 Lotus焦糖饼干",
    "candy": "金熊软糖 Pure葡萄软糖 Kororo软糖 明治果汁软糖 Fettuccine软糖 忍者饭软糖 Puccho软糖 嗨啾",
    "rice_cracker": "HappyTurn米饼 什锦米饼 雪の宿雪饼 柿种辣米果 饭团米饼",
    "dessert": "香草棉花糖 迷你年轮蛋糕 铜锣烧 水果果冻杯",
    "pudding_cup": "布丁",
    "ice_cream": "雪见大福 哈根达斯迷你杯 Papico冰淇淋 巧克力最中雪糕",
    "cup_noodle": "合味道原味杯面 合味道海鲜杯面 咚兵卫乌冬 UFO炒面 一平炒面",
    "cola": "可口可乐", "soda": "雪碧 三矢汽水 弹珠汽水", "melon_soda": "蜜瓜汽水",
    "bottled_tea": "午後の紅茶 乌龙茶 伊藤园绿茶", "juice": "可尔必思 葡萄果汁",
}
OLD = """
CREATE TABLE IF NOT EXISTS items(id integer primary key, name text not null, place text not null, qty integer not null,
  unit text not null, kind text not null, owner text not null default '', holder text not null default '',
  note text not null default '', updated real not null, by text not null default '');
CREATE TABLE IF NOT EXISTS events(id integer primary key, ts real not null, kind text not null, actors text not null,
  data text not null);
"""
# id, name, place, qty, unit, kind, owner, by
ROWS = [(1, "牛奶", "fridge", 0, "盒", "food", "", "elsa"),
        (2, "鸡蛋", "fridge", 3, "个", "food", "", "nell"),          # a staple the kernel fridge has: not copied
        (8, "布丁", "fridge", 6, "个", "food", "", "owner"),        # the owner's 6 merged into an old row
        (13, "红茶茶叶", "study", 2, "罐", "food", "elsa", "nell"),  # elsa's own
        (14, "游戏机", "living", 1, "台", "thing", "", "seed"),      # a thing: the kernel has its anchor
        (21, "草莓百奇", "snacks", 8, "盒", "food", "", "owner"),
        (23, "海苔仙贝", "snacks", 1, "袋", "food", "", "wanda"),    # she bought it herself
        (24, "湖池屋海苔盐薯片", "snacks", 6, "袋", "food", "", "owner"),
        (25, "披萨薯片", "snacks", 6, "袋", "food", "", "owner"),
        (26, "原味酸奶", "fridge", 1, "盒", "food", "", "alma"),   # no rule knows it
        (27, "午後の紅茶", "fridge", 6, "瓶", "food", "", "owner")]
# the console's item_owner events (world.owner_item): a merge names the row, a new row's add does not
EVENTS = [(4337, {"op": "del", "id": 11}),
          (4338, {"op": "add", "id": 21, "name": "草莓百奇", "qty": 8}),
          (4339, {"op": "add", "id": 8, "name": "布丁", "qty": 6}),
          (4340, {"op": "add", "id": None, "name": "湖池屋海苔盐薯片", "qty": 6, "place": "snacks", "note": None}),
          (4341, {"op": "add", "id": None, "name": "披萨薯片", "qty": 6, "place": "snacks", "note": None}),
          (4342, {"op": "add", "id": None, "name": "午後の紅茶", "qty": 6, "place": "fridge", "note": None}),
          (4343, {"op": "add", "id": None, "name": "披萨薯片", "qty": 1, "place": "fridge", "note": None})]  # another place
START = dt.datetime(2026, 11, 7, 10, 0, tzinfo=JST).timestamp()


def old_world(path, legacy=False):
    c = sqlite3.connect(path)
    if legacy:
        c.executescript(LEGACY)
        for a in V.PEOPLE:
            c.execute("insert into reality values(?, 'living', '客厅', 'idle', '发呆', '平静', 50, 60, 30, 'free',"
                      " 0, 0, 'routine', null, 0, 0, 0, 0, '{}')", (a,))
    c.executescript(OLD)
    for r in ROWS:
        c.execute("insert into items(id,name,place,qty,unit,kind,owner,updated,by) values(?,?,?,?,?,?,?,?,?)",
                  (*r[:7], 1793700000.0, r[7]))
    for eid, d in EVENTS:
        c.execute("insert into events(id,ts,kind,actors,data) values(?,?,?,?,?)",
                  (eid, 1793790000.0, "item_owner", "[]", json.dumps(d, ensure_ascii=False)))
    c.commit()
    c.close()


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def legacy_resources(store):
    with store.read() as c:
        return {r["resource_id"]: dict(r) for r in c.execute("SELECT * FROM resources WHERE source LIKE 'legacy_item:%'")}


class Names(unittest.TestCase):
    def test_every_kind_of_the_owner_v2_list_has_its_catalog_kind(self):
        names = [(n, item) for item, ns in V2.items() for n in ns.split()]
        self.assertEqual(len(names), 75)
        got = {n: inventory.category(n) for n, _ in names}
        self.assertEqual({n: g for n, g in got.items() if g != (dict(names)[n], True)}, {})
        for item in V2:
            self.assertIn(item, V.SNACKS, item)  # edible, not a main dish: a snack or a drink of the catalog

    def test_specific_before_generic(self):
        for name, item in (("巧克力最中雪糕", "ice_cream"), ("芥末牛肉薯片", "chips"), ("水果果冻杯", "dessert"),
                           ("草莓百奇", "chocolate"), ("午後の紅茶", "bottled_tea"), ("红茶茶叶", "black_tea"),
                           ("饭团米饼", "rice_cracker"), ("干脆面", "chips"), ("明治果汁软糖", "candy"),
                           ("葡萄果汁", "juice"), ("草莓牛奶", "juice"), ("海苔仙贝", "rice_cracker"), ("鸡蛋", "egg"),
                           ("米", "rice"), ("吐司", "bread"), ("草莓", "fruit"), (" 披萨 薯片 ", "chips")):
            self.assertEqual(inventory.category(name), (item, True), name)
        self.assertEqual(inventory.category("原味酸奶"), ("other_snack", False))


class Genesis(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wpath, self.kpath = os.path.join(self.d, "world.db"), os.path.join(self.d, "life1.db")
        self.clock = VirtualClock(START)

    def life(self, seed_items):
        s = Store(self.kpath, self.clock, world_id="ai-house/life1-test", synthetic=True, holder="test")
        s.acquire()
        return Life(s, model=None, seed_items=seed_items)

    def test_rows_come_in_once_with_count_kind_place_and_where_from(self):
        old_world(self.wpath)
        before = sha(self.wpath)
        life = self.life(inventory.legacy_items(self.wpath))
        self.assertEqual(sha(self.wpath), before)  # read-only
        got = legacy_resources(life.s)
        self.assertEqual({k: (v["item"], v["qty"], v["location"], v["holder"]) for k, v in got.items()}, {
            "pudding_cup@legacy:8": ("pudding_cup", 6, "kitchen", None),
            "black_tea@legacy:13": ("black_tea", 2, "study", "elsa"),
            "chocolate@legacy:21": ("chocolate", 8, "kitchen", None),
            "rice_cracker@legacy:23": ("rice_cracker", 1, "kitchen", None),
            "chips@legacy:24": ("chips", 6, "kitchen", None),
            "chips@legacy:25": ("chips", 6, "kitchen", None),
            "other_snack@legacy:26": ("other_snack", 1, "kitchen", None),
            "bottled_tea@legacy:27": ("bottled_tea", 6, "kitchen", None)})
        self.assertEqual(inventory.source_of(got["chips@legacy:24"]), {
            "legacy_item": 24, "name": "湖池屋海苔盐薯片", "place": "snacks", "unit": "袋", "by": "owner",
            "owner_events": [4340], "matched": True})
        self.assertEqual(inventory.source_of(got["chips@legacy:25"])["owner_events"], [4341])  # not the fridge one
        self.assertEqual(inventory.source_of(got["pudding_cup@legacy:8"])["owner_events"], [4339])
        self.assertEqual(inventory.source_of(got["chocolate@legacy:21"])["owner_events"], [4338])
        self.assertEqual(inventory.source_of(got["rice_cracker@legacy:23"])["owner_events"], [])
        self.assertEqual(inventory.source_of(got["other_snack@legacy:26"])["name"], "原味酸奶")
        # the kernel's own fridge is as before: STOCK, plus nothing for the egg row it already covers
        self.assertEqual(life.k.resource("egg")["qty"], STOCK["egg"])
        self.assertEqual(life.k.resource("pudding_cup")["qty"], STOCK["pudding_cup"])
        self.assertIsNone(life.k.resource("egg@legacy:2"))
        rep = life.get("genesis:inventory")
        self.assertEqual((rep["rows"], rep["qty"], rep["owner_rows"], rep["owner_qty"]), (8, 36, 5, 32))
        self.assertEqual(rep["by_item"]["chips"], 12)
        self.assertEqual(rep["other"], ["原味酸奶"])
        self.assertEqual([(s["id"], s["why"]) for s in rep["skipped"]], [(2, "stock"), (14, "thing")])

    def test_a_restart_does_not_copy_again(self):
        old_world(self.wpath)
        life = self.life(inventory.legacy_items(self.wpath))
        first = legacy_resources(life.s)
        life.s.release()
        again = self.life(inventory.legacy_items(self.wpath))
        self.assertEqual(legacy_resources(again.s), first)
        with again.s.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM resources WHERE source LIKE 'legacy_item:%'").fetchone()[0], 8)

    def test_she_can_eat_the_owner_chips(self):
        old_world(self.wpath)
        life = self.life(inventory.legacy_items(self.wpath))
        life.submit("nell", "move", {"to": "kitchen"}, "t")
        for _ in range(5):
            self.clock.advance(60)
            life.k.tick()
        self.assertEqual(life.k.actor("nell")["location"], "kitchen")
        r = life.submit("nell", "eat", {"item": "chips", "resource_id": "chips@legacy:24"}, "t")
        self.assertIn(r["status"], ("running", "accepted"), r)
        for _ in range(60):
            self.clock.advance(60)
            life.k.tick()
        self.assertEqual(life.k.resource("chips@legacy:24")["qty"], 5)
        self.assertEqual(life.k.resource("chips@legacy:25")["qty"], 6)

    def test_no_world_db_or_no_items_table(self):
        self.assertEqual(inventory.legacy_items(os.path.join(self.d, "missing.db")), [])
        sqlite3.connect(self.wpath).executescript(LEGACY)
        self.assertEqual(inventory.legacy_items(self.wpath), [])


class AuthoritySwitch(unittest.TestCase):
    def start(self, **kw):
        import authority
        return authority.Authority(self.kpath, self.wpath, None, {}, clock=self.clock, model=FakeLifeModel(), **kw)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wpath, self.kpath = os.path.join(self.d, "world.db"), os.path.join(self.d, "life1.db")
        self.clock = VirtualClock(START)
        old_world(self.wpath, legacy=True)

    def test_on_by_default_and_once(self):
        os.environ.pop("WORLD_KERNEL_MIGRATE_ITEMS", None)
        a = self.start()
        self.assertEqual(len(legacy_resources(a.store)), 8)
        a.step()
        a.store.release()
        b = self.start()
        self.assertEqual(len(legacy_resources(b.store)), 8)
        self.assertEqual(sum(r["qty"] for r in legacy_resources(b.store).values()), 36)
        b.store.release()

    def test_switch_off(self):
        a = self.start(migrate_items=False)
        self.assertEqual(legacy_resources(a.store), {})
        self.assertIsNone(a.life.get("genesis:inventory"))
        a.store.release()

    def test_env_off(self):
        os.environ["WORLD_KERNEL_MIGRATE_ITEMS"] = "0"
        try:
            a = self.start()
        finally:
            os.environ.pop("WORLD_KERNEL_MIGRATE_ITEMS", None)
        self.assertEqual(legacy_resources(a.store), {})
        a.store.release()


if __name__ == "__main__":
    unittest.main()
