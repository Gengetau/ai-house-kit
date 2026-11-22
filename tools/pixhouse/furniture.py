"""Furniture library. Every piece draws into its own Obj layer.

facing='y': back towards low Y (against the right back wall), front face = 'left'.
facing='x': back towards low X (against the left back wall), front face = 'right'.
Local coords: a = along the piece, b = depth (0 = back), z up.
"""
from engine import Box, C, mat, shade, mix, blob, fill_para, INK


class L:
    def __init__(self, o, x, y, facing="y"):
        self.o, self.x, self.y, self.f = o, x, y, facing
        self.front = "left" if facing == "y" else "right"
        self.side = "right" if facing == "y" else "left"

    def B(self, a, b, z, la, lb, h):
        if self.f == "y":
            return Box(self.o.scene.iso, self.x + a, self.y + b, z, la, lb, h)
        return Box(self.o.scene.iso, self.x + b, self.y + a, z, lb, la, h)

    def box(self, a, b, z, la, lb, h, m, faces=("left", "right", "top"), glow=False):
        return self.B(a, b, z, la, lb, h).draw(self.o.cv, m, faces, glow)

    def paint_front(self, bx, u, v, uw, vh, c, glow=False):
        # u measured along the piece (a axis)
        bx.paint(self.o.cv, self.front, u, v, uw, vh, c, glow)

    def paint_top(self, bx, a, b, la, lb, c, glow=False):
        if self.f == "y":
            bx.paint(self.o.cv, "top", a, b, la, lb, c, glow)
        else:
            bx.paint(self.o.cv, "top", b, a, lb, la, c, glow)


WOOD = mat("a86f4c")
WOOD_D = mat("7a4d36")
WOOD_L = mat("d3a273")
WHITE = mat("eee7da")
METAL = mat("9aa3b0")
DARK = mat("3e3b4c")


def sofa(o, x, y, la=48, facing="y", color="e9dcc0", cushion="f4d27a"):
    m = mat(color)
    l = L(o, x, y, facing)
    l.box(0, 2, 0, la, 18, 8, m)                    # base
    l.box(0, 0, 0, la, 7, 20, m)                    # back
    l.box(0, 4, 0, 5, 16, 13, mat(shade(m["base"], -0.05)))
    l.box(la - 5, 4, 0, 5, 16, 13, mat(shade(m["base"], -0.05)))
    seat = l.B(5, 7, 8, la - 10, 13, 2)
    seat.draw(o.cv, mat(shade(m["base"], 0.06)))
    for i, cx in enumerate((8, la - 18)):
        l.box(cx, 2, 15, 10, 5, 8, mat(cushion))


def armchair(o, x, y, facing="y", color="c4705a"):
    m = mat(color)
    l = L(o, x, y, facing)
    l.box(0, 2, 0, 20, 18, 8, m)
    l.box(0, 0, 0, 20, 6, 20, m)
    l.box(0, 4, 0, 4, 16, 13, m)
    l.box(16, 4, 0, 4, 16, 13, m)
    l.box(4, 6, 8, 12, 14, 2, mat(shade(m["base"], 0.1)))


def coffee_table(o, x, y, la=28, lb=16, facing="y"):
    l = L(o, x, y, facing)
    for a, b in ((1, 1), (la - 3, 1), (1, lb - 3), (la - 3, lb - 3)):
        l.box(a, b, 0, 2, 2, 7, WOOD_D)
    t = l.box(0, 0, 7, la, lb, 2, WOOD)
    # mugs
    l.box(5, 4, 9, 3, 3, 3, WHITE)
    l.box(la - 10, lb - 8, 9, 3, 3, 3, mat("c45e64"))


def tv_stand(o, x, y, la=36, facing="y", game=True):
    l = L(o, x, y, facing)
    st = l.box(0, 0, 0, la, 10, 10, WOOD_D)
    l.paint_front(st, 2, 2, la / 2 - 3, 6, shade(WOOD_D["left"], -0.2))
    l.paint_front(st, la / 2 + 1, 2, la / 2 - 3, 6, shade(WOOD_D["left"], -0.2))
    l.box(la / 2 - 2, 3, 10, 4, 3, 2, DARK)
    scr = l.box(3, 3, 12, la - 6, 2, 20, DARK)
    l.paint_front(scr, 1, 1, la - 8, 18, C("4f7fc2"), glow=True)
    l.paint_front(scr, 3, 10, 10, 7, C("8cc9d6"), glow=True)
    l.paint_front(scr, 16, 4, 8, 5, C("f6d98b"), glow=True)
    if game:
        l.box(la - 10, 3, 10, 7, 5, 3, WHITE)


def bookshelf(o, x, y, la=24, h=44, lb=9, facing="y", seed=3, rows=4):
    import random
    rnd = random.Random(seed)
    l = L(o, x, y, facing)
    sh = l.box(0, 0, 0, la, lb, h, WOOD)
    cols = ["c45e64", "5386b7", "6a9270", "e4b960", "8071b7", "d5804c", "eee7da", "53878a"]
    step = (h - 4) / rows
    for r in range(rows):
        z0 = 2 + r * step
        l.paint_front(sh, 2, z0, la - 4, step - 2, shade(WOOD["left"], -0.45))
        u = 2
        while u < la - 3:
            bw = rnd.choice((1, 2, 2, 3))
            bh = rnd.uniform(step * 0.55, step - 2.5)
            if rnd.random() < 0.12:
                u += bw
                continue
            l.paint_front(sh, u, z0, bw, bh, C(rnd.choice(cols)))
            u += bw


def rug(cv, iso, x, y, w, d, c1, c2, border=None):
    b = Box(iso, x, y, 0, w, d, 0)
    O, A, B = b.face("top")
    c1, c2 = C(c1), C(c2)
    bc = C(border) if border else shade(c1, -0.25)

    def f(s, t):
        if s < 3 / w or s > 1 - 3 / w or t < 3 / d or t > 1 - 3 / d:
            return bc
        k = int(s * w / 6) + int(t * d / 6)
        return c1 if k % 2 else c2
    fill_para(cv, O, A, B, f)


def plant(o, x, y, size=1.0, seed=5, pot="c4705a", leaf=("3f6553", "6a9270", "a9c58a")):
    iso = o.scene.iso
    Box(iso, x, y, 0, 8, 8, 8).draw(o.cv, mat(pot))
    cx, cy = iso.P(x + 4, y + 4, 8 + 10 * size)
    blob(o.cv, cx, cy, 8 * size, 10 * size, [C(c) for c in leaf], seed=seed)


def floor_lamp(o, x, y, shade_c="f6d98b"):
    iso = o.scene.iso
    Box(iso, x, y, 0, 6, 6, 2).draw(o.cv, DARK)
    Box(iso, x + 2, y + 2, 2, 2, 2, 30).draw(o.cv, DARK)
    Box(iso, x - 1, y - 1, 30, 8, 8, 9).draw(o.cv, mat(shade_c, top=0.3, right=-0.05), glow=True)


def table(o, x, y, la=40, lb=24, facing="y", top=None):
    l = L(o, x, y, facing)
    m = top or WOOD
    for a, b in ((2, 2), (la - 4, 2), (2, lb - 4), (la - 4, lb - 4)):
        l.box(a, b, 0, 2, 2, 12, WOOD_D)
    return l.box(0, 0, 12, la, lb, 2, m)


def chair(o, x, y, facing="y", back_far=True, color=None):
    """A chair; back_far=True puts the backrest at the far side (b=0)."""
    m = color or WOOD_L
    l = L(o, x, y, facing)
    for a, b in ((1, 1), (8, 1), (1, 8), (8, 8)):
        l.box(a, b, 0, 2, 2, 7, WOOD_D)
    l.box(0, 0, 7, 11, 11, 2, m)
    if back_far:
        l.box(0, 0, 9, 11, 2, 11, m)


def bed(o, x, y, la=40, lb=24, facing="x", blanket="8cc9d6", pillow="eee7da", frame=None, h=10):
    """facing='x': headboard against the left wall (low X), bed extends +X."""
    fr = frame or WOOD
    if facing == "x":
        iso = o.scene.iso
        Box(iso, x, y, 0, la, lb, h - 4).draw(o.cv, fr)
        Box(iso, x, y, 0, 4, lb, h + 12).draw(o.cv, fr)
        Box(iso, x + 4, y + 1, h - 4, la - 5, lb - 2, 5).draw(o.cv, WHITE)
        Box(iso, x + 6, y + 3, h + 1, 8, lb - 6, 3).draw(o.cv, mat(pillow))
        Box(iso, x + 16, y + 1, h - 3, la - 17, lb - 2, 5).draw(o.cv, mat(blanket))
    else:
        iso = o.scene.iso
        Box(iso, x, y, 0, lb, la, h - 4).draw(o.cv, fr)
        Box(iso, x, y, 0, lb, 4, h + 12).draw(o.cv, fr)
        Box(iso, x + 1, y + 4, h - 4, lb - 2, la - 5, 5).draw(o.cv, WHITE)
        Box(iso, x + 3, y + 6, h + 1, lb - 6, 8, 3).draw(o.cv, mat(pillow))
        Box(iso, x + 1, y + 16, h - 3, lb - 2, la - 17, 5).draw(o.cv, mat(blanket))


def wardrobe(o, x, y, la=24, lb=12, h=52, facing="y", color=None):
    m = color or WOOD
    l = L(o, x, y, facing)
    wd = l.box(0, 0, 0, la, lb, h, m)
    l.paint_front(wd, la / 2 - 0.5, 2, 1, h - 4, shade(m["left"], -0.35))
    l.paint_front(wd, la / 2 - 3, h / 2, 1, 4, C("e4b960"))
    l.paint_front(wd, la / 2 + 2, h / 2, 1, 4, C("e4b960"))


def desk(o, x, y, la=32, lb=14, facing="y", monitor=False, lamp=True, color=None):
    m = color or WOOD
    l = L(o, x, y, facing)
    l.box(0, 0, 0, 3, lb, 13, m)
    dr = l.box(la - 10, 0, 0, 10, lb, 13, m)
    l.paint_front(dr, 1, 7, 8, 1, shade(m["left"], -0.35))
    l.box(0, 0, 13, la, lb, 2, m)
    if monitor:
        l.box(la / 2 - 2, 3, 15, 4, 3, 3, DARK)
        s = l.box(la / 2 - 9, 2, 18, 18, 2, 11, DARK)
        l.paint_front(s, 1, 1, 16, 9, C("66d5d0"), glow=True)
        l.paint_front(s, 2, 5, 7, 3, C("8071b7"), glow=True)
        l.box(la / 2 - 6, 7, 15, 12, 4, 1, mat("737d94"))
    if lamp:
        l.box(2, 2, 15, 4, 4, 1, DARK)
        l.box(3, 3, 16, 2, 2, 8, DARK)
        l.box(1, 1, 22, 6, 6, 5, mat("f6d98b"), glow=True)


def counter(o, x, y, la=32, lb=14, facing="y", top="eee7da", body="d9cdb6"):
    l = L(o, x, y, facing)
    bm = mat(body)
    c = l.box(0, 0, 0, la, lb, 16, bm)
    n = int(la // 12)
    for i in range(n):
        u = 1 + i * la / n
        l.paint_front(c, u, 2, la / n - 2, 11, shade(bm["left"], 0.12))
        l.paint_front(c, u + la / n / 2 - 2, 10, 3, 1, C("737d94"))
    l.box(0, 0, 16, la, lb, 2, mat(top))
    return l


def stove(o, x, y, facing="y"):
    l = counter(o, x, y, 18, 14, facing, top="3e3b4c", body="b9c6d6")
    for a, b in ((3, 3), (11, 3), (3, 8), (11, 8)):
        l.paint_top(l.B(0, 0, 18, 18, 14, 0), a, b, 4, 3, C("737d94"))
    l.paint_top(l.B(0, 0, 18, 18, 14, 0), 3, 3, 4, 3, C("e07a4c"), glow=True)
    # pot
    l.box(10, 2, 18, 6, 6, 5, METAL)


def sink(o, x, y, facing="y"):
    l = counter(o, x, y, 20, 14, facing)
    l.paint_top(l.B(0, 0, 18, 20, 14, 0), 4, 3, 12, 8, C("8cc9d6"))
    l.box(9, 0, 18, 2, 2, 7, METAL)


def fridge(o, x, y, facing="y"):
    l = L(o, x, y, facing)
    f = l.box(0, 0, 0, 16, 14, 46, mat("eef2f4"))
    l.paint_front(f, 0, 30, 16, 1, C("b9c6d6"))
    l.paint_front(f, 13, 33, 1, 8, C("737d94"))
    l.paint_front(f, 13, 20, 1, 8, C("737d94"))
    l.paint_front(f, 3, 36, 3, 3, C("e387bf"))
    l.paint_front(f, 7, 37, 3, 2, C("f6d98b"))


def wall_cabinet(o, x, y, la=32, facing="y", z=34):
    l = L(o, x, y, facing)
    c = l.box(0, 0, z, la, 8, 14, mat("d9cdb6"))
    n = int(la // 12)
    for i in range(n):
        l.paint_front(c, 1 + i * la / n, 1, la / n - 2, 12, shade(mat("d9cdb6")["left"], 0.12))


def bathtub(o, x, y, la=40, lb=22, facing="y"):
    l = L(o, x, y, facing)
    t = l.box(0, 0, 0, la, lb, 14, mat("f4f1ea"))
    l.paint_top(t, 3, 3, la - 6, lb - 6, C("8cc9d6"))
    l.paint_top(t, 6, 5, 8, 2, C("eef2f4"))
    l.box(la - 6, 0, 14, 2, 2, 6, METAL)


def washer(o, x, y, facing="y"):
    l = L(o, x, y, facing)
    w = l.box(0, 0, 0, 16, 14, 18, mat("eef2f4"))
    bx = l.B(0, 0, 0, 16, 14, 18)
    l.paint_front(bx, 3, 3, 10, 9, C("737d94"))
    l.paint_front(bx, 4, 4, 8, 7, C("8cc9d6"))
    l.paint_front(bx, 2, 14, 3, 2, C("66d5d0"), glow=True)


def basin(o, x, y, facing="y"):
    l = L(o, x, y, facing)
    c = l.box(0, 0, 0, 18, 12, 14, mat("a86f4c"))
    l.box(0, 0, 14, 18, 12, 3, mat("f4f1ea"))
    l.paint_top(l.B(0, 0, 17, 18, 12, 0), 4, 3, 10, 7, C("b9c6d6"))
    l.box(8, 0, 17, 2, 2, 4, METAL)


def basket(o, x, y, color="d49c67"):
    Box(o.scene.iso, x, y, 0, 10, 10, 9).draw(o.cv, mat(color))
    Box(o.scene.iso, x + 1, y + 1, 9, 8, 8, 2).draw(o.cv, mat("eee7da"))


def stool(o, x, y, color="e4b960"):
    Box(o.scene.iso, x + 3, y + 3, 0, 2, 2, 10).draw(o.cv, DARK)
    Box(o.scene.iso, x, y, 10, 8, 8, 2).draw(o.cv, mat(color))


def piano(o, x, y, facing="y"):
    l = L(o, x, y, facing)
    p = l.box(0, 0, 0, 34, 12, 26, mat("3e3b4c"))
    l.box(0, 10, 12, 34, 6, 3, mat("3e3b4c"))
    k = l.B(0, 10, 12, 34, 6, 3)
    l.paint_top(k, 1, 1, 32, 4, C("f4f1ea"))
    for i in range(2, 33, 3):
        l.paint_top(k, i, 1, 1, 2, C("252b3d"))


def easel(o, x, y):
    iso = o.scene.iso
    Box(iso, x, y + 4, 0, 2, 2, 30).draw(o.cv, WOOD_D)
    Box(iso, x + 8, y + 4, 0, 2, 2, 30).draw(o.cv, WOOD_D)
    cvs = Box(iso, x - 1, y + 2, 12, 12, 2, 16).draw(o.cv, mat("f4f1ea"))
    cvs.paint(o.cv, "left", 2, 3, 8, 6, C("87b7ac"))
    cvs.paint(o.cv, "left", 3, 8, 4, 5, C("e387bf"))
    cvs.paint(o.cv, "left", 6, 9, 3, 3, C("f6d98b"))


def aquarium(o, x, y, facing="y"):
    l = L(o, x, y, facing)
    l.box(0, 0, 0, 28, 12, 12, WOOD_D)
    t = l.box(0, 0, 12, 28, 12, 16, mat("8cc9d6", top=0.2), glow=True)
    l.paint_front(t, 1, 1, 26, 12, C("53878a"), glow=True)
    l.paint_front(t, 1, 1, 26, 2, C("e4b960"))
    l.paint_front(t, 5, 6, 3, 2, C("ed9a5c"), glow=True)
    l.paint_front(t, 15, 8, 3, 2, C("f6d98b"), glow=True)
    l.paint_front(t, 20, 4, 3, 2, C("e387bf"), glow=True)
    l.paint_front(t, 9, 1, 1, 6, C("6a9270"), glow=True)
    l.paint_front(t, 23, 1, 1, 8, C("6a9270"), glow=True)


def telescope(o, x, y):
    iso = o.scene.iso
    for dx, dy in ((0, 0), (8, 0), (4, 7)):
        Box(iso, x + dx, y + dy, 0, 2, 2, 18).draw(o.cv, DARK)
    t = Box(iso, x + 1, y - 4, 18, 6, 18, 6).draw(o.cv, mat("e4b960"))


def cat_bed(o, x, y, color="bc9cd5"):
    iso = o.scene.iso
    Box(iso, x, y, 0, 16, 16, 5).draw(o.cv, mat(color))
    Box(iso, x + 3, y + 3, 4, 10, 10, 2).draw(o.cv, mat("f4f1ea"))


def shelf_low(o, x, y, la=24, facing="y", items=("e4b960", "c45e64", "5386b7")):
    l = L(o, x, y, facing)
    s = l.box(0, 0, 0, la, 10, 16, WOOD_L)
    l.paint_front(s, 2, 2, la - 4, 5, shade(WOOD_L["left"], -0.4))
    l.paint_front(s, 2, 9, la - 4, 5, shade(WOOD_L["left"], -0.4))
    for i, c in enumerate(items):
        l.box(2 + i * (la - 4) / len(items), 2, 16, 5, 5, 5 + (i % 2) * 3, mat(c))


def bench(o, x, y, la=32, facing="y"):
    l = L(o, x, y, facing)
    for a in (2, la - 4):
        l.box(a, 2, 0, 2, 8, 7, WOOD_D)
    l.box(0, 0, 7, la, 12, 2, WOOD)
    l.box(0, 0, 9, la, 2, 12, WOOD)


def flower_bed(o, x, y, la=40, lb=12, facing="y", seed=4):
    import random
    rnd = random.Random(seed)
    l = L(o, x, y, facing)
    bx = l.box(0, 0, 0, la, lb, 5, mat("9a6a4a"))
    l.paint_top(bx, 1, 1, la - 2, lb - 2, C("5b4033"))
    iso = o.scene.iso
    for i in range(int(la // 5)):
        a = 2 + i * 5 + rnd.uniform(-1, 1)
        b = rnd.uniform(2, lb - 3)
        X, Y = (x + a, y + b) if facing == "y" else (x + b, y + a)
        cx, cy = iso.P(X, Y, 9)
        blob(o.cv, cx, cy, 3, 4, [C("3f6553"), C("6a9270"), C("a9c58a")], seed=seed + i)
        col = C(rnd.choice(["e387bf", "f6d98b", "c45e64", "bc9cd5", "eef2f4"]))
        o.cv.set(int(cx), int(cy - 3), col)
        o.cv.set(int(cx) + 1, int(cy - 3), col)
        o.cv.set(int(cx), int(cy - 2), col)


def tree(o, x, y, seed=9):
    iso = o.scene.iso
    Box(iso, x, y, 0, 6, 6, 34).draw(o.cv, WOOD_D)
    cx, cy = iso.P(x + 3, y + 3, 52)
    blob(o.cv, cx, cy, 20, 18, [C("3f6553"), C("6a9270"), C("a9c58a")], seed=seed)


def laundry_rack(o, x, y, la=36, facing="y", cloths=("eef2f4", "87b7ac", "e387bf", "f6d98b")):
    l = L(o, x, y, facing)
    l.box(0, 4, 0, 2, 2, 34, METAL)
    l.box(la - 2, 4, 0, 2, 2, 34, METAL)
    l.box(0, 4, 32, la, 2, 2, METAL)
    for i, c in enumerate(cloths):
        l.box(4 + i * (la - 8) / len(cloths), 4, 18 + (i % 2) * 3, 6, 1, 14 - (i % 2) * 3, mat(c))


def gaming_chair(o, x, y, facing="y", color="c45e64"):
    l = L(o, x, y, facing)
    l.box(4, 4, 0, 4, 4, 8, DARK)
    l.box(0, 0, 8, 12, 12, 3, mat(color))
    l.box(0, 0, 11, 12, 3, 18, mat(color))


def snack_pile(o, x, y):
    iso = o.scene.iso
    for i, (dx, dy, c) in enumerate(((0, 0, "e4b960"), (5, 2, "c45e64"), (2, 6, "66d5d0"), (7, 7, "f6d98b"))):
        Box(iso, x + dx, y + dy, 0, 5, 3, 7 - i).draw(o.cv, mat(c))


def chest(o, x, y, facing="y", color="8a7aa8", gold=True):
    l = L(o, x, y, facing)
    m = mat(color)
    c = l.box(0, 0, 0, 20, 12, 12, m)
    l.box(0, 0, 12, 20, 12, 4, mat(shade(m["base"], 0.1)))
    if gold:
        l.paint_front(c, 9, 7, 2, 3, C("e4b960"))
        l.box(3, 3, 16, 3, 3, 2, mat("e4b960"))
        l.box(12, 5, 16, 4, 3, 1, mat("e4b960"))


def display_case(o, x, y, la=24, facing="y", items=("eef2f4", "8cc9d6", "e4b960", "bc9cd5")):
    l = L(o, x, y, facing)
    c = l.box(0, 0, 0, la, 10, 40, mat("b9c6d6"))
    for r in range(3):
        z = 3 + r * 12
        l.paint_front(c, 2, z, la - 4, 10, C("dfeaf2"))
        for i, col in enumerate(items):
            l.paint_front(c, 3 + i * (la - 6) / len(items), z, 3, 4 + (i + r) % 3 * 2, C(col))


def towel_rack(o, x, y, facing="y"):
    l = L(o, x, y, facing)
    l.box(0, 2, 0, 2, 2, 30, METAL)
    l.box(14, 2, 0, 2, 2, 30, METAL)
    l.box(0, 2, 28, 16, 2, 2, METAL)
    l.box(2, 1, 14, 5, 3, 14, mat("e387bf"))
    l.box(8, 1, 12, 5, 3, 16, mat("87b7ac"))


def guitar(o, x, y):
    iso = o.scene.iso
    Box(iso, x, y, 0, 8, 3, 10).draw(o.cv, mat("d5804c"))
    Box(iso, x + 3, y, 10, 2, 2, 16).draw(o.cv, WOOD_D)


def board_game(o, cx, cy, z, iso):
    pass


def tea_set(o, x, y, z):
    iso = o.scene.iso
    Box(iso, x, y, z, 5, 5, 4).draw(o.cv, mat("f4f1ea"))
    Box(iso, x + 7, y + 1, z, 3, 3, 2).draw(o.cv, mat("f4f1ea"))
    Box(iso, x + 1, y + 7, z, 3, 3, 2).draw(o.cv, mat("f4f1ea"))


def stack_books(o, x, y, z, cols=("c45e64", "5386b7", "e4b960")):
    iso = o.scene.iso
    for i, c in enumerate(cols):
        Box(iso, x + (i % 2), y, z + i * 2, 7, 5, 2).draw(o.cv, mat(c))


def nightstand(o, x, y, color=None, lamp="f6d98b", clock=True):
    m = color or WOOD
    iso = o.scene.iso
    b = Box(iso, x, y, 0, 11, 11, 12)
    b.draw(o.cv, m)
    b.paint(o.cv, "left", 2, 5, 7, 1, shade(m["left"], -0.35))
    b.paint(o.cv, "left", 5, 7, 1, 1, C("e4b960"))
    Box(iso, x + 2, y + 2, 12, 4, 4, 1).draw(o.cv, DARK)
    Box(iso, x + 3, y + 3, 13, 2, 2, 5).draw(o.cv, DARK)
    Box(iso, x + 1, y + 1, 18, 6, 6, 5).draw(o.cv, mat(lamp, top=0.3, right=-0.05), glow=True)
    if clock:
        Box(iso, x + 7, y + 6, 12, 3, 2, 3).draw(o.cv, mat("e387bf"))


def cushion(o, x, y, color="f6d98b", z=0, s=10):
    iso = o.scene.iso
    Box(iso, x, y, z, s, s, 3).draw(o.cv, mat(color, top=0.25))
    cx, cy = iso.P(x + s / 2, y + s / 2, z + 3)
    o.cv.set(int(cx), int(cy), shade(C(color), -0.3))


def slippers(o, x, y, color="e387bf"):
    iso = o.scene.iso
    Box(iso, x, y, 0, 6, 3, 2).draw(o.cv, mat(color))
    Box(iso, x, y + 4, 0, 6, 3, 2).draw(o.cv, mat(color))


def side_table(o, x, y, color=None, items=("f4f1ea",)):
    m = color or WOOD_L
    iso = o.scene.iso
    Box(iso, x + 4, y + 4, 0, 2, 2, 11).draw(o.cv, WOOD_D)
    Box(iso, x, y, 11, 10, 10, 2).draw(o.cv, m)
    for i, c in enumerate(items):
        Box(iso, x + 2 + i * 4, y + 3, 13, 3, 3, 3 + (i % 2) * 2).draw(o.cv, mat(c))


def floor_books(o, x, y, n=3, seed=1, cols=("c45e64", "5386b7", "e4b960", "6a9270", "8071b7")):
    iso = o.scene.iso
    for i in range(n):
        c = cols[(seed + i * 3) % len(cols)]
        Box(iso, x + (i * seed) % 3, y + (i % 2), i * 2, 8, 6, 2).draw(o.cv, mat(c))


def trash_can(o, x, y, color="9aa3b0"):
    iso = o.scene.iso
    Box(iso, x, y, 0, 6, 6, 8).draw(o.cv, mat(color))
    Box(iso, x + 1, y + 1, 8, 4, 4, 1).draw(o.cv, mat("737d94"))


def mug(o, x, y, z, color="f4f1ea"):
    Box(o.scene.iso, x, y, z, 3, 3, 3).draw(o.cv, mat(color))
