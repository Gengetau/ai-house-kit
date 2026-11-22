"""The eleven AI小屋 rooms (keys = world-kernel LOCATIONS)."""
import random

import furniture as F
from engine import Box, C, Scene, fill_para, mat, shade, mix, blob

T = 6       # wall thickness
FL = 6      # floor slab


def new_scene(key, W, D, H=84, top=0):
    ox = D + T + 12
    w = W + D + 2 * T + 24
    oy = H + T + 14 + top
    h = int(oy + (W + D) / 2 + FL + 14)
    sc = Scene(key, w, h, ox, oy)
    sc.Wd, sc.Dd, sc.Hw = W, D, H
    return sc


def planks(base, seam, W, D, along="x", width=8, run=48, seed=1):
    rnd = random.Random(seed)
    shades = [shade(C(base), k) for k in (0.0, 0.05, -0.05, 0.1)]
    sc = C(seam)
    cache = {}

    def f(s, t):
        a, b = (s * W, t * D) if along == "x" else (t * D, s * W)
        row = int(b // width)
        if b % width < 1.0:
            return sc
        off = (row * 17) % run
        seg = int((a + off) // run)
        if (a + off) % run < 1.0:
            return sc
        key = (row, seg)
        if key not in cache:
            cache[key] = rnd.choice(shades)
        return cache[key]
    return f


def tiles(c1, c2, W, D, size=12, grout="b9c6d6"):
    a, b, g = C(c1), C(c2), C(grout)

    def f(s, t):
        x, y = s * W, t * D
        if x % size < 1 or y % size < 1:
            return g
        return a if (int(x // size) + int(y // size)) % 2 else b
    return f


def grass(W, D, seed=2):
    rnd = random.Random(seed)
    cols = [C("6a9270"), C("7da57a"), C("5d8565"), C("8bb27f")]
    cache = {}

    def f(s, t):
        k = (int(s * W / 3), int(t * D / 3))
        if k not in cache:
            cache[k] = rnd.choice(cols)
        return cache[k]
    return f


def wallfn(paper, H, wains=None, stripe=True, rail="8a5a40"):
    p = C(paper)
    p2 = shade(p, -0.06)
    wa = C(wains) if wains else None
    ra = C(rail)

    def mk(length):
        def f(s, t):
            z = t * (H + FL) - FL
            u = s * length
            if wa is not None:
                if 15 <= z < 18:
                    return ra
                if z < 15:
                    return shade(wa, -0.25) if u % 12 < 1 else wa
            if stripe and u % 8 < 2:
                return p2
            return p
        return f
    return mk


def shell(sc, floor_fn, wall_mk, floor_side="7a4d36", wall_top="f1e5ca", wall_cap=None):
    W, D, H = sc.Wd, sc.Dd, sc.Hw
    iso, cv = sc.iso, sc.bg
    capm = mat(wall_cap or "d9cdb6")
    lw = Box(iso, -T, 0, -FL, T, D, H + FL)
    rw = Box(iso, 0, -T, -FL, W, T, H + FL)
    co = Box(iso, -T, -T, -FL, T, T, H + FL)
    O, A, B = lw.face("right")
    fill_para(cv, O, A, B, wall_mk(D))
    O, A, B = rw.face("left")
    fill_para(cv, O, A, B, wall_mk(W))
    for b in (lw, rw, co):
        O, A, B = b.face("top")
        fill_para(cv, O, A, B, C(wall_top))
    O, A, B = lw.face("left")
    fill_para(cv, O, A, B, capm["left"])
    O, A, B = rw.face("right")
    fill_para(cv, O, A, B, capm["right"])
    fl = Box(iso, 0, 0, -FL, W, D, FL)
    fm = mat(floor_side)
    fl.draw(cv, fm, faces=("left", "right"))
    O, A, B = fl.face("top")
    fill_para(cv, O, A, B, floor_fn)
    # thin dark seam where floor meets the walls
    sc.lw, sc.rw = Box(iso, -T, 0, 0, T, D, H), Box(iso, 0, -T, 0, W, T, H)


def wpaint(sc, wall, u, z, w, h, color, glow=False):
    b = sc.lw if wall == "L" else sc.rw
    b.paint(sc.bg, "right" if wall == "L" else "left", u, z, w, h, color, glow)


def window(sc, wall, u, z, w, h, night=False, frame="f4f1ea", view="sky", curtain=None):
    wpaint(sc, wall, u - 2, z - 2, w + 4, h + 4, C(frame))
    b = sc.lw if wall == "L" else sc.rw
    face = "right" if wall == "L" else "left"
    O, A, B = b.face(face)
    length = b.d if wall == "L" else b.w
    rnd = random.Random(int(u * 7 + z))
    if night:
        top, bot = C("1d2447"), C("3a3f73")
    else:
        top, bot = C("8cc9e6"), C("d7eef0")
    stars = {(int(rnd.uniform(0, w)), int(rnd.uniform(0, h))) for _ in range(int(w * h / 40))}

    def f(s, t):
        uu, zz = s * length - u, t * b.h - z
        k = zz / h
        c = mix(bot, top, k)
        if view == "city" and zz < h * 0.45:
            col = int(uu // 4)
            hh = (col * 37 % 7 + 3) / 10 * h * 0.45
            if zz < hh:
                c = C("2b2f4a") if night else C("9aa3b0")
                if night and int(uu) % 3 == 1 and int(zz) % 4 == 1 and (col + int(zz)) % 3:
                    c = C("f6d98b")
        elif view == "garden" and zz < h * 0.35:
            c = C("3f6553") if night else C("6a9270")
            if zz < h * 0.35 - 3 - (int(uu) * 13 % 5):
                c = C("2f4a40") if night else C("5d8565")
        if night and (int(uu), int(zz)) in stars and zz > h * 0.5:
            c = C("f6d98b")
        return c
    fill_para(sc.bg, O, A, B, f, u / length, (u + w) / length, (z + FL) / b.h * 0 + (z) / b.h, (z + h) / b.h, glow=night)
    wpaint(sc, wall, u + w / 2 - 1, z, 2, h, C(frame))
    wpaint(sc, wall, u, z + h / 2 - 1, w, 2, C(frame))
    wpaint(sc, wall, u - 3, z - 4, w + 6, 2, C("d9cdb6"))
    if curtain:
        cc = C(curtain)
        wpaint(sc, wall, u - 6, z - 4, 6, h + 10, cc)
        wpaint(sc, wall, u + w, z - 4, 6, h + 10, cc)
        wpaint(sc, wall, u - 6, z - 4, 1, h + 10, shade(cc, -0.25))
        wpaint(sc, wall, u + w + 5, z - 4, 1, h + 10, shade(cc, -0.25))
        wpaint(sc, wall, u - 8, z + h + 5, w + 16, 2, C("8a5a40"))


def frame_pic(sc, wall, u, z, w, h, colors=("87b7ac", "f6d98b", "e387bf"), frame="b6813f"):
    wpaint(sc, wall, u - 1, z - 1, w + 2, h + 2, C(frame))
    wpaint(sc, wall, u, z, w, h, C(colors[0]))
    wpaint(sc, wall, u, z, w, h * 0.4, C(colors[1]))
    wpaint(sc, wall, u + w * 0.55, z + h * 0.45, w * 0.25, h * 0.3, C(colors[2]))


def clock(sc, wall, u, z):
    wpaint(sc, wall, u, z, 8, 8, C("b6813f"))
    wpaint(sc, wall, u + 1, z + 1, 6, 6, C("f4f1ea"))
    wpaint(sc, wall, u + 4, z + 4, 1, 3, C("252b3d"))
    wpaint(sc, wall, u + 4, z + 4, 2, 1, C("252b3d"))


def wall_shelf(sc, wall, u, z, w, items=("c45e64", "5386b7", "e4b960", "6a9270")):
    wpaint(sc, wall, u, z, w, 2, C("8a5a40"))
    step = w / (len(items) + 1)
    for i, c in enumerate(items):
        wpaint(sc, wall, u + step * (i + 0.6), z + 2, 3, 5 + (i % 2) * 2, C(c))


def neon(sc, wall, u, z, text_cells, night):
    col = C("66d5d0")
    for (cu, cz, cw, ch) in text_cells:
        wpaint(sc, wall, u + cu, z + cz, cw, ch, col, glow=True)


def string_lights(sc, wall, u, z, w, night):
    cols = ["f6d98b", "e387bf", "66d5d0", "edad69"]
    for i in range(int(w // 6)):
        zz = z - abs(((i * 6) % 24) - 12) / 4
        wpaint(sc, wall, u + i * 6, zz, 2, 2, C(cols[i % 4]), glow=True)


def depth_of(x, y, w, d, bias=0.0):
    return x + w / 2 + y + d / 2 + bias


# ---------------------------------------------------------------- rooms

def living(night=False):
    sc = new_scene("living", 160, 144)
    shell(sc, planks("b0744c", "7a4a33", 160, 144), wallfn("efe1c4", 84, wains="c79a6b"))
    window(sc, "L", 14, 30, 44, 34, night, view="garden", curtain="c4705a")
    window(sc, "L", 84, 30, 30, 34, night, view="garden", curtain="c4705a")
    frame_pic(sc, "R", 62, 44, 26, 18)
    clock(sc, "R", 102, 56)
    string_lights(sc, "R", 6, 76, 150, night)
    F.rug(sc.bg, sc.iso, 44, 30, 64, 46, "d5804c", "e4b960", "a05439")
    o = sc.obj("bookshelf", "客厅书架：大家的漫画和游戏攻略", depth_of(8, 0, 24, 9))
    F.bookshelf(o, 8, 0, la=26, h=50, seed=11)
    o = sc.obj("sofa", "米白色大沙发，大家都爱窝在这儿", depth_of(44, 0, 64, 20))
    F.sofa(o, 44, 0, la=64)
    o = sc.obj("piano", "立式钢琴", depth_of(118, 0, 34, 16))
    F.piano(o, 118, 0)
    o = sc.obj("tv", "电视和游戏机", depth_of(0, 56, 10, 40))
    F.tv_stand(o, 0, 56, la=40, facing="x")
    o = sc.obj("plant", "大叶植物", depth_of(4, 112, 8, 8))
    F.plant(o, 4, 112, size=1.3, seed=8)
    o = sc.obj("lamp", "落地灯", depth_of(30, 4, 6, 6, -2))
    F.floor_lamp(o, 34, 6)
    o = sc.obj("table", "茶几，上面有两只马克杯", depth_of(56, 40, 36, 20))
    F.coffee_table(o, 56, 40, la=36, lb=20)
    o = sc.obj("armchair", "单人扶手椅", depth_of(20, 96, 20, 20))
    F.armchair(o, 20, 96, facing="x")
    o = sc.obj("game_box", "桌游收纳箱", depth_of(132, 40, 12, 10))
    F.chest(o, 128, 36, facing="y", color="5386b7", gold=False)
    wall_shelf(sc, "L", 120, 56, 20, items=("6a9270", "f6d98b", "c45e64"))
    frame_pic(sc, "R", 38, 50, 16, 12, colors=("f4c6dd", "87b7ac", "f4f1ea"))
    o = sc.obj("side", "小边几，放着遥控器和纸巾", depth_of(44, 118, 10, 10))
    F.side_table(o, 44, 118, items=("f4f1ea", "3e3b4c"))
    o = sc.obj("cush1", "坐垫", depth_of(88, 84, 10, 10))
    F.cushion(o, 88, 84, "f6d98b")
    o = sc.obj("cush2", "坐垫", depth_of(104, 68, 10, 10))
    F.cushion(o, 104, 68, "87b7ac")
    o = sc.obj("mags", "一摞杂志和游戏攻略", depth_of(122, 58, 10, 8))
    F.floor_books(o, 122, 58, n=4, seed=2)
    o = sc.obj("trash", "垃圾桶", depth_of(148, 124, 6, 6))
    F.trash_can(o, 148, 124)
    # anchors (x, y, z) foot points in world units
    for i, ax in enumerate((54, 70, 86, 100)):
        sc.anchor(f"sofa{i}", ax, 13, 8, "sit",
                  ["resting", "idle", "phone", "drama", "movie_night", "tea_time", "socializing"], on="sofa")
    sc.anchor("armchair", 30, 104, 8, "sit", ["reading", "tea_time", "resting", "movie_night"], on="armchair")
    sc.anchor("game0", 30, 72, 0, "sit", ["gaming", "movie_night", "board_game"])
    sc.anchor("game1", 30, 88, 0, "sit", ["gaming", "board_game"])
    sc.anchor("rug0", 74, 70, 0, "sit", ["board_game", "movie_night", "tea_time"])
    sc.anchor("rug1", 98, 56, 0, "sit", ["board_game", "tea_time"])
    sc.anchor("piano", 134, 24, 0, "stand", ["music"])
    sc.anchor("stand0", 116, 92, 0, "stand", ["chores_clean", "idle", "walking"])
    sc.anchor("stand1", 70, 112, 0, "stand", ["chores_clean", "idle", "browsing"])
    return sc


def kitchen(night=False):
    sc = new_scene("kitchen", 160, 128)
    shell(sc, tiles("efe1c4", "d9cdb6", 160, 128, size=12, grout="c8b89c"),
          wallfn("f4ecd8", 84, wains=None, stripe=False))
    # tiled splash band on the right wall
    for u in range(0, 160, 6):
        for z in range(18, 34, 6):
            wpaint(sc, "R", u, z, 5, 5, C("bfe0d8") if (u // 6 + z // 6) % 2 else C("e6f2ee"))
    window(sc, "R", 58, 46, 26, 22, night, view="garden")
    window(sc, "L", 30, 34, 40, 32, night, view="garden", curtain="e4b960")
    wall_shelf(sc, "L", 86, 52, 30, items=("f4f1ea", "e4b960", "c45e64", "87b7ac"))
    clock(sc, "L", 98, 66)
    o = sc.obj("fridge", "冰箱，门上贴着大家的便签", depth_of(2, 0, 16, 14))
    F.fridge(o, 2, 0)
    o = sc.obj("counter1", "料理台", depth_of(20, 0, 34, 14))
    F.counter(o, 20, 0, la=34)
    o = sc.obj("sink", "水槽", depth_of(54, 0, 20, 14))
    F.sink(o, 54, 0)
    o = sc.obj("stove", "灶台，锅里炖着东西", depth_of(74, 0, 18, 14))
    F.stove(o, 74, 0)
    o = sc.obj("counter2", "料理台和调料架", depth_of(92, 0, 40, 14))
    F.counter(o, 92, 0, la=40)
    F.L(o, 92, 0).box(4, 3, 18, 5, 5, 8, F.mat("c45e64"))
    F.L(o, 92, 0).box(12, 4, 18, 4, 4, 6, F.mat("e4b960"))
    F.L(o, 92, 0).box(26, 2, 18, 10, 8, 3, F.mat("d3a273"))
    o = sc.obj("cabinet", "吊柜", depth_of(96, 0, 36, 8, -6))
    F.wall_cabinet(o, 96, 0, la=36)
    o = sc.obj("cabinet2", "吊柜", depth_of(20, 0, 32, 8, -6))
    F.wall_cabinet(o, 20, 0, la=32)
    # dining set
    for i, a in enumerate((52, 70, 88)):
        o = sc.obj(f"chairf{i}", "餐椅", depth_of(a, 48, 11, 11))
        F.chair(o, a, 48)
    o = sc.obj("dtable", "六人餐桌", depth_of(48, 58, 54, 28))
    t = F.table(o, 48, 58, la=54, lb=28)
    F.tea_set(o, 70, 66, 14)
    for i, a in enumerate((52, 70, 88)):
        o = sc.obj(f"chairn{i}", "餐椅", depth_of(a, 88, 11, 11))
        F.chair(o, a, 88, back_far=False)
    o = sc.obj("plant", "香草盆栽", depth_of(4, 104, 8, 8))
    F.plant(o, 4, 104, size=0.9, seed=21)
    F.rug(sc.bg, sc.iso, 44, 44, 62, 58, "c4705a", "d5804c", "8a3d4b")
    wpaint(sc, "L", 120, 40, 18, 14, C("6b4a33"))
    wpaint(sc, "L", 121, 41, 16, 12, C("3e4a44"))
    for k, (u, z, w) in enumerate(((123, 50, 8), (123, 47, 11), (123, 44, 6))):
        wpaint(sc, "L", u, z, w, 1, C("eef2f4"))
    o = sc.obj("trash", "分类垃圾桶", depth_of(140, 4, 14, 6))
    F.trash_can(o, 140, 4, color="6a9270")
    F.trash_can(o, 148, 4, color="5386b7")
    o = sc.obj("veg", "今天买的菜", depth_of(140, 100, 10, 10))
    F.basket(o, 140, 100, color="d3a273")
    o = sc.obj("dtable2", "餐桌上的杯子", depth_of(48, 58, 54, 28, 0.2))
    F.mug(o, 56, 62, 14, "f4f1ea")
    F.mug(o, 92, 78, 14, "e387bf")
    F.mug(o, 86, 62, 14, "87b7ac")
    for i, a in enumerate((57, 75, 93)):
        sc.anchor(f"dinef{i}", a, 54, 9, "sit", ["eating", "tea_time", "socializing", "phone"], on=f"chairf{i}")
    for i, a in enumerate((57, 75, 93)):
        sc.anchor(f"dinen{i}", a, 92, 9, "sit", ["eating", "tea_time", "socializing"], on=f"chairn{i}")
    sc.anchor("stove", 83, 24, 0, "stand", ["cooking"])
    sc.anchor("counter", 104, 24, 0, "stand", ["cooking", "chores_clean"])
    sc.anchor("sink", 64, 24, 0, "stand", ["chores_dishes", "cooking"])
    sc.anchor("fridge", 12, 24, 0, "stand", ["idle", "eating"])
    sc.anchor("stand", 130, 70, 0, "stand", ["idle", "phone", "chores_clean"])
    return sc


def study(night=False):
    sc = new_scene("study", 144, 128)
    shell(sc, planks("8a5a40", "5e3a2c", 144, 128, width=6, run=36, seed=4),
          wallfn("dcd3be", 84, wains="7a4d36", stripe=True, rail="5e3a2c"))
    window(sc, "L", 24, 32, 36, 34, night, view="garden", curtain="3f6553")
    frame_pic(sc, "L", 80, 46, 18, 22, colors=("6a9270", "d7eef0", "e4b960"))
    clock(sc, "L", 110, 60)
    for i, a in enumerate((6, 34, 62)):
        o = sc.obj(f"shelf{i}", "书房大书墙，按门类整理好", depth_of(a, 0, 26, 9))
        F.bookshelf(o, a, 0, la=26, h=62, seed=30 + i, rows=5)
    o = sc.obj("desk1", "书桌", depth_of(96, 0, 34, 14))
    F.desk(o, 96, 0, la=34)
    F.stack_books(o, 112, 3, 15)
    o = sc.obj("chair1", "书桌椅", depth_of(106, 18, 11, 11))
    F.chair(o, 106, 18, back_far=False)
    F.rug(sc.bg, sc.iso, 40, 50, 52, 40, "6a9270", "87b7ac", "3f6553")
    o = sc.obj("desk2", "第二张书桌（谁都能用）", depth_of(0, 60, 14, 32))
    F.desk(o, 0, 60, la=32, facing="x", lamp=True)
    o = sc.obj("chair2", "椅子", depth_of(18, 70, 11, 11))
    F.chair(o, 18, 70, facing="x", back_far=False)
    o = sc.obj("armchair", "阅读椅", depth_of(56, 58, 20, 20))
    F.armchair(o, 56, 58, color="8a3d4b")
    o = sc.obj("stool", "小边几和红茶", depth_of(80, 64, 8, 8))
    F.stool(o, 80, 64, color="a47052")
    F.tea_set(o, 81, 65, 12)
    o = sc.obj("easel", "画架", depth_of(110, 70, 12, 8))
    F.easel(o, 112, 70)
    o = sc.obj("lamp", "落地灯", depth_of(130, 20, 6, 6))
    F.floor_lamp(o, 130, 22)
    o = sc.obj("plant", "绿萝", depth_of(4, 104, 8, 8))
    F.plant(o, 4, 104, size=1.0, seed=33)
    o = sc.obj("books1", "没放回去的书", depth_of(86, 40, 10, 8))
    F.floor_books(o, 86, 40, n=3, seed=4)
    o = sc.obj("books2", "一摞书", depth_of(8, 22, 10, 8))
    F.floor_books(o, 8, 22, n=5, seed=7)
    o = sc.obj("cush", "读书坐垫", depth_of(88, 104, 10, 10))
    F.cushion(o, 88, 104, "6a9270")
    o = sc.obj("trash", "废纸篓", depth_of(134, 40, 6, 6))
    F.trash_can(o, 134, 40, color="a47052")
    sc.anchor("desk1", 111, 24, 9, "sit", ["working", "reading"], on="chair1")
    sc.anchor("desk2", 24, 75, 9, "sit", ["working", "drawing", "reading"], on="chair2")
    sc.anchor("armchair", 66, 68, 8, "sit", ["reading", "tea_time", "resting"], on="armchair")
    sc.anchor("easel", 118, 88, 0, "stand", ["drawing"])
    sc.anchor("shelf", 46, 20, 0, "stand", ["reading", "chores_clean", "idle"])
    sc.anchor("rug", 70, 100, 0, "sit", ["reading", "phone", "idle"])
    return sc


def bath(night=False):
    sc = new_scene("bath", 128, 112)
    shell(sc, tiles("eef2f4", "cfe3ea", 128, 112, size=10, grout="b9c6d6"),
          wallfn("e3eef0", 84, wains="87b7ac", stripe=False, rail="53878a"), wall_top="eef2f4")
    window(sc, "R", 70, 52, 22, 18, night, view="sky", frame="eef2f4")
    # mirror over the basin on the left wall
    wpaint(sc, "L", 34, 36, 20, 26, C("b6813f"))
    wpaint(sc, "L", 36, 38, 16, 22, C("cfe9f0"))
    wpaint(sc, "L", 38, 50, 3, 8, C("eef2f4"))
    wall_shelf(sc, "L", 70, 46, 24, items=("e387bf", "87b7ac", "f6d98b"))
    o = sc.obj("bathtub", "浴缸，热水刚放好", depth_of(60, 0, 44, 24))
    F.bathtub(o, 60, 0, la=46, lb=24)
    o = sc.obj("basin", "洗手台", depth_of(0, 36, 12, 18))
    F.basin(o, 0, 36, facing="x")
    o = sc.obj("washer", "洗衣机", depth_of(0, 70, 14, 16))
    F.washer(o, 0, 70, facing="x")
    o = sc.obj("basket", "脏衣篮", depth_of(18, 90, 10, 10))
    F.basket(o, 18, 92)
    o = sc.obj("towels", "毛巾架", depth_of(20, 0, 16, 4))
    F.towel_rack(o, 20, 0)
    o = sc.obj("plant", "小盆栽", depth_of(110, 0, 8, 8))
    F.plant(o, 112, 4, size=0.7, seed=41)
    o = sc.obj("stool", "浴室小凳", depth_of(44, 8, 8, 8))
    F.stool(o, 44, 8, color="87b7ac")
    o = sc.obj("slippers", "拖鞋", depth_of(40, 56, 8, 8))
    F.slippers(o, 40, 56, color="87b7ac")
    o = sc.obj("trash", "垃圾桶", depth_of(4, 100, 6, 6))
    F.trash_can(o, 4, 100, color="eef2f4")
    F.rug(sc.bg, sc.iso, 60, 34, 40, 18, "e387bf", "f4c6dd", "c45e8f")
    sc.anchor("tub", 82, 12, 6, "bath", ["bathing"], on="bathtub")
    sc.anchor("basin", 18, 45, 0, "stand", ["chores_clean", "idle"])
    sc.anchor("washer", 20, 78, 0, "stand", ["laundry"])
    sc.anchor("mat", 80, 46, 0, "stand", ["bathing", "chores_clean"])
    sc.anchor("stand0", 60, 84, 0, "stand", ["chores_clean", "laundry", "idle"])
    sc.anchor("stand1", 96, 70, 0, "stand", ["idle"])
    return sc


def fence_fn(H):
    a, b, gap = C("c79a6b"), C("a86f4c"), C("8a5a40")
    sky = None

    def mk(length):
        def f(s, t):
            u = s * length
            z = t * (H + FL) - FL
            if u % 10 < 1:
                return gap
            if 26 <= z < 29 or 8 <= z < 11:
                return C("7a4d36")
            return a if int(u // 10) % 2 else b
        return f
    return mk


def yard(night=False):
    sc = new_scene("yard", 160, 144, H=40, top=36)
    shell(sc, grass(160, 144), fence_fn(40), floor_side="6b4a33", wall_top="d3a273", wall_cap="a86f4c")
    # stepping stones
    for i, (x, y) in enumerate(((20, 120), (36, 104), (52, 90), (70, 80), (90, 74))):
        b = Box(sc.iso, x, y, 0, 10, 8, 0)
        O, A, B = b.face("top")
        fill_para(sc.bg, O, A, B, C("b9c6d6"))
    o = sc.obj("tree", "院子里的老树", depth_of(10, 10, 6, 6))
    F.tree(o, 12, 12, seed=51)
    o = sc.obj("flowers1", "花圃，开着各色小花", depth_of(40, 0, 60, 12))
    F.flower_bed(o, 40, 0, la=60, seed=52)
    o = sc.obj("flowers2", "花圃", depth_of(0, 40, 12, 50))
    F.flower_bed(o, 0, 40, la=50, facing="x", seed=53)
    o = sc.obj("bench", "长椅，晴天可以在这晒太阳", depth_of(110, 4, 34, 12))
    F.bench(o, 110, 4, la=34)
    o = sc.obj("ttable", "户外小茶桌", depth_of(70, 60, 20, 20))
    F.table(o, 70, 60, la=20, lb=20, top=F.mat("eee7da"))
    F.tea_set(o, 74, 64, 14)
    o = sc.obj("tchair1", "折叠椅", depth_of(58, 64, 11, 11))
    F.chair(o, 56, 64, facing="x", color=F.mat("eee7da"))
    o = sc.obj("tchair2", "折叠椅", depth_of(94, 64, 11, 11))
    F.chair(o, 94, 64, facing="x", back_far=False, color=F.mat("eee7da"))
    o = sc.obj("rack", "晾衣架", depth_of(110, 96, 36, 6))
    F.laundry_rack(o, 110, 96, la=36, facing="x")
    o = sc.obj("pot1", "盆栽", depth_of(140, 30, 8, 8))
    F.plant(o, 140, 30, size=0.8, seed=54, pot="d5804c")
    o = sc.obj("pot2", "盆栽", depth_of(150, 52, 8, 8))
    F.plant(o, 150, 52, size=0.6, seed=55, pot="c4705a", leaf=("a05439", "e387bf", "f6d98b"))
    o = sc.obj("can", "洒水壶", depth_of(102, 18, 6, 6))
    F.trash_can(o, 102, 18, color="6a9270")
    o = sc.obj("ball", "皮球", depth_of(40, 96, 6, 6))
    F.cushion(o, 40, 96, "c45e64", s=5)
    sc.anchor("bench0", 120, 12, 9, "sit", ["resting", "phone", "idle", "tea_time"], on="bench")
    sc.anchor("bench1", 134, 12, 9, "sit", ["resting", "phone", "tea_time"], on="bench")
    sc.anchor("chair1", 62, 70, 9, "sit", ["tea_time", "drawing"], on="tchair1")
    sc.anchor("chair2", 99, 70, 9, "sit", ["tea_time"], on="tchair2")
    sc.anchor("garden", 70, 22, 0, "stand", ["gardening"])
    sc.anchor("garden2", 22, 64, 0, "stand", ["gardening"])
    sc.anchor("rack", 104, 110, 0, "stand", ["laundry"])
    sc.anchor("lawn", 40, 120, 0, "sit", ["resting", "idle", "drawing"])
    return sc


# ------------------------------------------------------------- bedrooms

def bedroom(key, night, W, D, paper, wains, floor, blanket, extras, curtain="c4705a",
            view="garden", stripe=True, bed_at=(0, 30), bed_size=(44, 26), frame=None):
    sc = new_scene(key, W, D)
    shell(sc, floor, wallfn(paper, 84, wains=wains, stripe=stripe))
    window(sc, "R", W * 0.45, 36, 30, 30, night, view=view, curtain=curtain)
    bx, by = bed_at
    la, lb = bed_size
    o = sc.obj("bed", "床", depth_of(bx, by, la, lb, -4))
    F.bed(o, bx, by, la=la, lb=lb, facing="x", blanket=blanket, frame=frame)
    sc.bed_obj = o
    sc.anchor("bed", bx + 10, by + lb / 2 + 2, 14, "sleep", ["sleeping", "resting"], on="bed")
    o = sc.obj("nightstand", "床头柜和台灯", depth_of(bx + 2, by - 13, 11, 11))
    F.nightstand(o, bx + 2, by - 13)
    o = sc.obj("slippers", "拖鞋", depth_of(bx + la + 3, by + lb - 8, 6, 7))
    F.slippers(o, bx + la + 3, by + lb - 8, color=blanket)
    extras(sc)
    return sc


def bedroom_0(night=False):
    def ex(sc):
        frame_pic(sc, "L", 76, 46, 20, 16, colors=("f4c6dd", "f6d98b", "6a9270"))
        wall_shelf(sc, "L", 8, 68, 24, items=("e387bf", "f4f1ea", "e4b960"))
        F.rug(sc.bg, sc.iso, 46, 44, 40, 30, "f4c6dd", "fbe3ee", "e387bf")
        o = sc.obj("wardrobe", "衣柜，围裙挂在最外面", depth_of(8, 0, 26, 12))
        F.wardrobe(o, 8, 0, la=26, color=F.mat("d49c67"))
        o = sc.obj("desk", "书桌，摊着记账本", depth_of(88, 0, 32, 14))
        F.desk(o, 88, 0, la=32)
        F.stack_books(o, 100, 3, 15, cols=("e387bf", "f4f1ea"))
        o = sc.obj("dchair", "椅子", depth_of(98, 18, 11, 11))
        F.chair(o, 98, 18, back_far=False, color=F.mat("f4c6dd"))
        o = sc.obj("plant", "小盆栽", depth_of(116, 90, 8, 8))
        F.plant(o, 114, 88, size=0.9, seed=61, pot="e387bf")
        o = sc.obj("basket", "针线篮", depth_of(60, 82, 10, 10))
        F.basket(o, 60, 82, color="d3a273")
        o = sc.obj("side", "小茶几", depth_of(116, 40, 10, 10))
        F.side_table(o, 116, 40, items=("f4f1ea", "e387bf"))
        o = sc.obj("cush", "粉色坐垫", depth_of(50, 64, 10, 10))
        F.cushion(o, 50, 64, "f4c6dd")
        o = sc.obj("trash", "垃圾桶", depth_of(6, 100, 6, 6))
        F.trash_can(o, 6, 100, color="f4c6dd")
        sc.anchor("desk", 103, 24, 9, "sit", ["working", "reading", "phone"], on="dchair")
        sc.anchor("rug", 66, 60, 0, "sit", ["phone", "resting", "idle", "reading"])
        sc.anchor("wardrobe", 22, 22, 0, "stand", ["idle", "chores_clean"])
        sc.anchor("stand", 90, 80, 0, "stand", ["idle", "drawing"])
    return bedroom("bedroom_0", night, 128, 112, "f9ead8", "e2b8a8",
                   planks("c8935f", "8a5a40", 128, 112, seed=6), "f4c6dd", ex, curtain="e387bf")


def bedroom_1(night=False):
    def ex(sc):
        frame_pic(sc, "L", 78, 44, 22, 18, colors=("87b7ac", "edad69", "f4f1ea"))
        for i, a in enumerate((6, 32)):
            o = sc.obj(f"shelf{i}", "书柜里的私人藏书", depth_of(a, 0, 26, 9))
            F.bookshelf(o, a, 0, la=26, h=56, seed=70 + i, rows=4)
        F.rug(sc.bg, sc.iso, 50, 40, 44, 34, "8a3d4b", "a05454", "5e2a35")
        o = sc.obj("ttable", "小圆茶桌和红茶", depth_of(70, 52, 16, 16))
        F.table(o, 70, 52, la=16, lb=16, top=F.mat("f4f1ea"))
        F.tea_set(o, 72, 54, 14)
        o = sc.obj("armchair", "天鹅绒扶手椅", depth_of(48, 52, 20, 20))
        F.armchair(o, 46, 50, facing="x", color="8a3d4b")
        o = sc.obj("vase", "橙花（系着黑缎带）", depth_of(112, 6, 8, 8))
        F.plant(o, 112, 6, size=0.7, seed=72, pot="252b3d", leaf=("a05439", "d5804c", "edad69"))
        o = sc.obj("lamp", "落地灯", depth_of(104, 90, 6, 6))
        F.floor_lamp(o, 106, 92)
        o = sc.obj("desk", "写字台，摊着读书笔记", depth_of(0, 74, 14, 30))
        F.desk(o, 0, 74, la=30, facing="x", color=F.mat("684a46"))
        F.stack_books(o, 4, 82, 15, cols=("8a3d4b", "f4f1ea", "252b3d"))
        o = sc.obj("dchair", "椅子", depth_of(18, 84, 11, 11))
        F.chair(o, 18, 84, facing="x", back_far=False, color=F.mat("8a3d4b"))
        o = sc.obj("books", "待读的书", depth_of(40, 104, 10, 8))
        F.floor_books(o, 40, 102, n=4, seed=5, cols=("8a3d4b", "252b3d", "e4b960", "f4f1ea", "6a9270"))
        sc.anchor("desk", 24, 89, 9, "sit", ["working", "reading", "drawing"], on="dchair")
        sc.anchor("armchair", 56, 60, 8, "sit", ["reading", "tea_time", "resting", "phone"], on="armchair")
        sc.anchor("shelf", 30, 20, 0, "stand", ["reading", "idle", "chores_clean"])
        sc.anchor("ttable", 92, 64, 0, "stand", ["tea_time", "idle"])
        sc.anchor("stand", 80, 92, 0, "stand", ["working", "idle"])
    return bedroom("bedroom_1", night, 128, 112, "f1e5ca", "8a5a40",
                   planks("a86f4c", "684a46", 128, 112, seed=7), "f4f1ea", ex, curtain="8a3d4b",
                   view="sky", bed_at=(0, 34))


def bedroom_2(night=False):
    def ex(sc):
        window(sc, "L", 70, 32, 36, 34, night, view="sky", frame="dfeaf2", curtain="5e6a8a")
        for i in range(9):
            wpaint(sc, "L", 70 + (i * 13) % 34, 32 + (i * 7) % 30, 2, 2, C("f4f9fb"), glow=night)
        # silver nest: pillows and furs heaped round the bed
        o = sc.bed_obj
        o.label = "软蓬蓬的靠枕和暖毯大床"
        iso = sc.iso
        for i, (X, Y, Z, r) in enumerate(((6, 34, 20, 7), (8, 54, 20, 7), (20, 30, 18, 6), (36, 66, 16, 6),
                                          (50, 64, 16, 6), (56, 34, 14, 5))):
            cx, cy = iso.P(X, Y, Z)
            blob(o.cv, cx, cy, r + 2, r, [C("aab8c8"), C("dfe8ef"), C("f4f9fb")], seed=90 + i)
        o = sc.obj("crystal", "水晶摆件（微弱发光）", depth_of(140, 100, 10, 10))
        cx, cy = iso.P(146, 106, 10)
        for k, (dx, h) in enumerate(((-4, 14), (0, 22), (4, 16), (7, 10))):
            for yy in range(h):
                w = max(1, 3 - yy * 3 // h)
                for xx in range(w):
                    o.cv.set(int(cx + dx + xx), int(cy - yy), C("8cc9d6") if xx else C("dfeaf2"), glow=True)
        o = sc.obj("case1", "收藏柜：宝石和银器", depth_of(56, 0, 26, 10))
        F.display_case(o, 56, 0, la=26)
        o = sc.obj("case2", "收藏柜：古书和鳞片", depth_of(84, 0, 26, 10))
        F.display_case(o, 84, 0, la=26, items=("e4b960", "eef2f4", "5386b7", "bc9cd5"))
        o = sc.obj("chest", "宝箱（别碰）", depth_of(124, 4, 20, 12))
        F.chest(o, 124, 4, color="5e6a8a")
        o = sc.obj("chest2", "又一个宝箱", depth_of(0, 104, 12, 20))
        F.chest(o, 0, 104, facing="x", color="8a7aa8")
        F.rug(sc.bg, sc.iso, 60, 50, 56, 44, "dfeaf2", "c9d8e6", "8c9cb8")
        o = sc.obj("coins", "金币堆", depth_of(120, 30, 10, 10))
        F.snack_pile(o, 126, 34)
        o = sc.obj("cush1", "银色靠垫", depth_of(104, 60, 10, 10))
        F.cushion(o, 104, 60, "dfeaf2")
        o = sc.obj("cush2", "紫色靠垫", depth_of(66, 90, 10, 10))
        F.cushion(o, 66, 90, "8a7aa8")
        sc.anchor("case", 78, 22, 0, "stand", ["idle", "chores_clean"])
        sc.anchor("rug", 86, 72, 0, "sit", ["reading", "phone", "idle", "resting"])
        sc.anchor("chest", 130, 26, 0, "stand", ["idle"])
        sc.anchor("stand", 40, 110, 0, "stand", ["working", "idle"])
    return bedroom("bedroom_2", night, 160, 128, "cfdce8", "8c9cb8",
                   tiles("dfe8ef", "c9d6e2", 160, 128, size=16, grout="aab8c8"), "eef2f4", ex,
                   curtain="8c9cb8", view="sky", stripe=False, bed_at=(0, 30), bed_size=(54, 36),
                   frame=F.mat("b9c6d6"))


def bedroom_3(night=False):
    def ex(sc):
        frame_pic(sc, "L", 20, 44, 18, 24, colors=("c45e64", "252b3d", "66d5d0"), frame="252b3d")
        frame_pic(sc, "L", 44, 50, 14, 18, colors=("8071b7", "e4b960", "e387bf"), frame="252b3d")
        string_lights(sc, "L", 4, 76, 100, night)
        o = sc.obj("desk", "电竞桌，双屏", depth_of(56, 0, 40, 14))
        F.desk(o, 56, 0, la=40, monitor=True, lamp=False, color=F.mat("3e3b4c"))
        o = sc.obj("gchair", "电竞椅", depth_of(70, 18, 12, 12))
        F.gaming_chair(o, 70, 30, color="c45e64")
        o = sc.obj("snacks", "零食堆", depth_of(100, 4, 10, 10))
        F.snack_pile(o, 102, 4)
        o = sc.obj("shelf", "手办架", depth_of(0, 88, 10, 24))
        F.shelf_low(o, 0, 86, la=22, facing="x", items=("c45e64", "66d5d0", "e4b960"))
        F.rug(sc.bg, sc.iso, 50, 50, 40, 30, "51436f", "6a5a8a", "352b4a")
        o = sc.obj("bean", "懒人沙发", depth_of(84, 72, 14, 14))
        F.cushion(o, 84, 72, "c45e64", s=14)
        o = sc.obj("manga", "漫画堆", depth_of(20, 70, 10, 8))
        F.floor_books(o, 20, 70, n=5, seed=3)
        o = sc.obj("trash", "垃圾桶（满了）", depth_of(100, 30, 6, 6))
        F.trash_can(o, 100, 30, color="3e3b4c")
        o = sc.obj("guitar", "电吉他", depth_of(100, 88, 8, 3))
        F.guitar(o, 100, 88)
        sc.anchor("gchair", 76, 36, 11, "sit", ["gaming", "working", "phone"], on="gchair")
        sc.anchor("rug", 70, 70, 0, "sit", ["phone", "drama", "resting", "idle"])
        sc.anchor("snacks", 100, 24, 0, "stand", ["eating", "idle"])
        sc.anchor("stand", 40, 92, 0, "stand", ["idle"])
    return bedroom("bedroom_3", night, 112, 104, "5a4f74", "352b4a",
                   planks("6b5a5a", "3e3346", 112, 104, seed=8), "51436f", ex, curtain="252b3d",
                   view="city", stripe=True, bed_at=(0, 34), bed_size=(42, 24))


def bedroom_4(night=False):
    def ex(sc):
        for i, (u, z) in enumerate(((10, 70), (28, 62), (46, 74), (64, 60), (84, 70), (100, 64), (18, 52), (92, 50))):
            wpaint(sc, "L", u, z, 2, 2, C("f6d98b"), glow=True)
            wpaint(sc, "L", u - 1, z + 0.5, 4, 1, C("f6d98b"), glow=True)
        string_lights(sc, "R", 4, 78, 120, night)
        o = sc.obj("telescope", "望远镜（夜里看星星用）", depth_of(80, 24, 12, 12))
        F.telescope(o, 82, 24)
        o = sc.obj("catbed", "猫窝，其实她更爱沙发", depth_of(90, 70, 16, 16))
        F.cat_bed(o, 90, 70)
        o = sc.obj("shelf", "矮柜，放着星星罐", depth_of(20, 0, 24, 10))
        F.shelf_low(o, 20, 0, la=24, items=("f6d98b", "bc9cd5", "66d5d0"))
        o = sc.obj("plant", "小盆栽", depth_of(4, 96, 8, 8))
        F.plant(o, 4, 96, size=0.8, seed=81, pot="bc9cd5")
        F.rug(sc.bg, sc.iso, 50, 50, 44, 30, "bc9cd5", "d9c6ea", "8071b7")
        o = sc.obj("cush", "星星抱枕", depth_of(52, 56, 10, 10))
        F.cushion(o, 52, 56, "f6d98b")
        o = sc.obj("side", "小圆桌，放着星星罐", depth_of(110, 40, 10, 10))
        F.side_table(o, 110, 40, items=("f6d98b", "bc9cd5"))
        o = sc.obj("books", "星座图鉴", depth_of(30, 74, 10, 8))
        F.floor_books(o, 30, 74, n=3, seed=6, cols=("8071b7", "f6d98b", "bc9cd5", "51436f", "66d5d0"))
        sc.anchor("catbed", 98, 78, 4, "sleep", ["sleeping", "resting"], on="catbed")
        sc.anchor("telescope", 74, 40, 0, "stand", ["idle", "hobby"])
        sc.anchor("rug", 68, 66, 0, "sit", ["phone", "resting", "idle", "drama"])
        sc.anchor("stand", 40, 92, 0, "stand", ["idle", "chores_clean"])
    return bedroom("bedroom_4", night, 128, 112, "d9c6ea", "8071b7",
                   planks("b48a6a", "7a5a50", 128, 112, seed=9), "bc9cd5", ex, curtain="51436f",
                   view="sky", bed_at=(0, 36))


def bedroom_5(night=False):
    def ex(sc):
        # waves on the left wall
        for u in range(0, 112, 8):
            wpaint(sc, "L", u, 64, 5, 2, C("8cc9d6"))
            wpaint(sc, "L", u + 4, 66, 4, 2, C("8cc9d6"))
        frame_pic(sc, "L", 70, 40, 22, 16, colors=("5386b7", "8cc9e6", "eef2f4"))
        o = sc.obj("aquarium", "大水族箱", depth_of(56, 0, 28, 12))
        F.aquarium(o, 56, 0)
        o = sc.obj("snackshelf", "零食柜（满的）", depth_of(90, 0, 24, 10))
        F.shelf_low(o, 90, 0, la=24, items=("e4b960", "c45e64", "f6d98b", "66d5d0"))
        o = sc.obj("snacks", "地上的零食", depth_of(96, 30, 10, 10))
        F.snack_pile(o, 98, 30)
        o = sc.obj("plush", "鲸鱼抱枕", depth_of(70, 74, 12, 10))
        F.cat_bed(o, 70, 74, color="5386b7")
        F.rug(sc.bg, sc.iso, 50, 46, 44, 32, "5386b7", "8cc9d6", "334b7c")
        o = sc.obj("cush", "海浪坐垫", depth_of(52, 70, 10, 10))
        F.cushion(o, 52, 70, "8cc9d6")
        o = sc.obj("books", "海洋图鉴", depth_of(30, 74, 10, 8))
        F.floor_books(o, 30, 74, n=3, seed=2, cols=("5386b7", "8cc9d6", "eef2f4", "334b7c", "e4b960"))
        o = sc.obj("trash", "零食袋", depth_of(110, 50, 6, 6))
        F.trash_can(o, 110, 50, color="e4b960")
        sc.anchor("aquarium", 70, 22, 0, "stand", ["idle", "hobby"])
        sc.anchor("snacks", 104, 22, 0, "stand", ["eating", "idle"])
        sc.anchor("rug", 64, 62, 0, "sit", ["phone", "eating", "resting", "drama", "gaming"])
        sc.anchor("stand", 40, 92, 0, "stand", ["idle", "chores_clean"])
    return bedroom("bedroom_5", night, 128, 112, "cfe6ef", "5386b7",
                   planks("b88a62", "7a5a40", 128, 112, seed=10), "5386b7", ex, curtain="334b7c",
                   view="garden", stripe=False, bed_at=(0, 36))


ROOMS = {"living": living, "kitchen": kitchen, "study": study, "bath": bath, "yard": yard,
         "bedroom_0": bedroom_0, "bedroom_1": bedroom_1, "bedroom_2": bedroom_2,
         "bedroom_3": bedroom_3, "bedroom_4": bedroom_4, "bedroom_5": bedroom_5}
