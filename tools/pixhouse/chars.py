"""Built-in resident sprites drawn at 32x48 (chibi, front view).

Poses: stand / sit (front), each with an optional held item (book, phone, pad, ladle, cup) and four frames
(rest, breathe, blink, sway + item lift); plus 'sleep' (head on pillow, eyes closed).
Includes sprites for `life1` (nell, elsa, alma, mina, yuki, wanda) and `indie-studio-3` (sora, rin, kuro).
"""
import math

from engine import Canvas, C, shade, mix, INK

W, H = 32, 48
SKIN, SKIN_S, BLUSH = C("f7d7c2"), C("e4b19c"), C("f2a3a8")
WHITE, WHITE_S = C("f6f3ec"), C("d8d3c8")

LOOKS = {
    "nell": dict(hair=("8a5233", "5c331e", "b4774e"), eyes=("0f766e", "5fb3a8"), long=35,
                 dress=("7a4d36", "5e3a2c"), apron=True, collar=True, band="maid",
                 legs=C("f3ecda"), shoes=C("2b2520")),
    "elsa": dict(hair=("c97a3e", "945324", "e9a95c"), eyes=("2563eb", "6ba3f5"), long=38,
                 dress=("f3ecda", "d9cbab"), corset=("8a3d4b", "e4b960"), flower=True, hem=C("8a3d4b"),
                 legs=C("f3ecda"), shoes=C("6b3f2a"), skirt_to=43),
    "alma": dict(hair=("3a4a5e", "232d3b", "6b809b"), eyes=("0891b2", "5ccfe6"), long=36,
                 dress=("6f9068", "4d6847"), collar=True, hem=C("d9cbb8"),
                 legs=WHITE, shoes=C("5e3a2c")),
    "mina": dict(hair=("e11d48", "9f1239", "fb7185"), eyes=("d97706", "fbbf24"), long=0, twintails=True,
                 jacket=("3a3644", "e11d48"), skirt=("2b2733", "1d1a24"), headphones=True, fang=True,
                 legs=SKIN, shoes=C("eab308"), skirt_to=37),
    "yuki": dict(hair=("8b73c8", "5b4596", "c4b5fd"), hair_end=C("d8b4fe"), eyes=("7c3aed", "c4b5fd"), long=40,
                 dress=("f6f3ec", "dfd8ca"), collar=True, stars=True, bow=C("e11d48"),
                 legs=WHITE, shoes=C("5b4596")),
    "wanda": dict(hair=("d97706", "a15204", "f59e0b"), eyes=("b45309", "f59e0b"), long=34, fluffy=True,
                  ahoge=True, dress=("c79a6b", "9c7248"), apron=True, puff=True, bow=C("eab308"),
                  legs=WHITE, shoes=C("7a4d36")),
    "sora": dict(hair=("0284c7", "0369a1", "7dd3fc"), eyes=("0284c7", "7dd3fc"), long=34, ahoge=True,
                 dress=("5b4636", "3e2f24"), apron=True, collar=True,
                 legs=SKIN, shoes=C("3e2f24")),
    "rin": dict(hair=("db2777", "9d174d", "f472b6"), eyes=("be185d", "f9a8d4"), long=36,
                jacket=("3b3048", "db2777"), skirt=("2a2236", "1b1524"), headphones=True,
                legs=WHITE, shoes=C("9d174d"), skirt_to=38),
    "kuro": dict(hair=("312e81", "1e1b4b", "6366f1"), eyes=("4f46e5", "a5b4fc"), long=0,
                 jacket=("1e293b", "4f46e5"), skirt=("334155", "1e293b"),
                 legs=SKIN, shoes=C("4f46e5"), skirt_to=39),
}
ITEMS = ["none", "book", "phone", "pad", "ladle", "cup"]


def _px(cv, x, y, c):
    if c is not None and 0 <= x < W and 0 <= y < H:
        cv.set(int(x), int(y), c)


def ell(cv, cx, cy, rx, ry, c, fn=None):
    for y in range(int(cy - ry - 1), int(cy + ry + 2)):
        for x in range(int(cx - rx - 1), int(cx + rx + 2)):
            dx, dy = (x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry
            if dx * dx + dy * dy <= 1.0:
                _px(cv, x, y, fn(x, y) if fn else c)


def rect(cv, x0, y0, x1, y1, c):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            _px(cv, x, y, c)


def poly(cv, pts, c, fn=None):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    for y in range(int(min(ys)), int(max(ys)) + 1):
        for x in range(int(min(xs)), int(max(xs)) + 1):
            px, py, inside = x + 0.5, y + 0.5, False
            j = len(pts) - 1
            for i in range(len(pts)):
                xi, yi = pts[i]
                xj, yj = pts[j]
                if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                    inside = not inside
                j = i
            if inside:
                _px(cv, x, y, fn(x, y) if fn else c)


def hair_col(L, y, top=6, bottom=40):
    base, dark, light = (C(h) for h in L["hair"])
    end = L.get("hair_end")
    if end is not None:
        t = max(0.0, min(1.0, (y - 14) / max(1, bottom - 14)))
        base = mix(base, end, t)
        dark = shade(base, -0.25)
    return base, dark, light


# ------------------------------------------------------------ layers

def back_layer(cv, L, dy, head_only=False, sway=0):
    """Wings, tail, long back hair, twintails: everything behind the body."""
    if L.get("wings") and not head_only:
        m, d = C("dde5ee"), C("aab8c8")
        for s in (-1, 1):
            cx = 16
            w = sway  # wings lift and fold a little
            pts = [(cx + s * 4, 27), (cx + s * 15, 16 - w), (cx + s * (16 + w * 0.5), 22 - w), (cx + s * 14, 30),
                   (cx + s * 9, 35)]
            poly(cv, pts, m)
            for k in range(3):
                x0, y0, x1, y1 = cx + s * 5, 28, cx + s * (12 + k), 20 + k * 5 - w
                for t in range(12):
                    _px(cv, round(x0 + (x1 - x0) * t / 11), round(y0 + (y1 - y0) * t / 11), d)
    tail = None if head_only else L.get("tail")
    if tail == "cat":
        base = hair_col(L, 40)[0]
        for t in range(16):
            a = t / 15
            x = 21 + a * 8 + math.sin(a * 3.0) * 1.5 + sway * a * a * 3
            y = 40 - a * 14
            ell(cv, x, y, 1.6, 1.6, base if t < 13 else shade(base, 0.25))
    elif tail == "dragon":
        base, dk = C("dfe5ee"), C("b4bfcf")
        for t in range(18):
            a = t / 17
            x = 20 + a * 10
            y = 41 + math.sin(a * 2.6) * 2 - a * 2 - sway * a * a * 3
            r = 2.6 - a * 1.7
            ell(cv, x, y, r, r, base if t % 3 else dk)
    if L.get("long"):
        n = L["long"]
        rx = 11 if L.get("fluffy") else 10
        def fn(x, y):
            b, d, l = hair_col(L, y, bottom=n)
            return d if (x in (6, 7, 24, 25) and y > 22) or (x * 3 + y * 5) % 23 == 0 else b
        ell(cv, 16, 15 + dy, rx, 10, None, fn)
        poly(cv, [(16 - rx + 0.5, 15 + dy), (16 + rx - 0.5, 15 + dy), (16 + rx - 1, n - 2),
                  (16 + rx - 4, n), (16, n - 1), (16 - rx + 4, n), (16 - rx + 1, n - 2)], None, fn)
        if L.get("fluffy"):
            for k in range(5):
                ell(cv, 7 + k * 4.5, n - 1, 2.6, 2.2, None, fn)
    if L.get("twintails"):
        b, d, l = hair_col(L, 20)
        for s in (-1, 1):
            cx = 16 + s * 12 + (sway * 0.6 if sway else 0)
            ell(cv, cx, 24 + dy, 3.6, 10.5, None,
                lambda x, y, cx=cx, s=s: d if (x - cx) * s > 1.6 else (l if (x - cx) * s < -1.5 and y < 22 + dy else b))
            ell(cv, cx + s * 0.5 + sway, 33 + dy, 2.4, 3, b)


def arms(cv, L, item, sit):
    sleeve = WHITE if L.get("puff") else (C(L["jacket"][0]) if L.get("jacket") else C(L["dress"][0]))
    sl_d = shade(sleeve, -0.18)
    if item == "none":
        for s in (-1, 1):
            x0 = 16 + s * 6
            poly(cv, [(x0 - 1.5 * (s < 0) - 0.5 * (s > 0), 25), (x0 + s * 2.5, 26), (x0 + s * 3, 32), (x0 + s * 0.5, 32)],
                 sleeve)
            ell(cv, x0 + s * 1.8, 33.5, 1.4, 1.4, SKIN)
    else:
        for s in (-1, 1):
            x0 = 16 + s * 6
            poly(cv, [(x0, 25), (x0 + s * 2.6, 26), (x0 + s * 2, 31), (x0 - s * 1.5, 32), (x0 - s * 1.5, 30)], sleeve)
            rect(cv, min(x0 + s * 1, x0 + s * 2), 28, max(x0 + s * 1, x0 + s * 2), 30, sl_d)
    if L.get("puff"):
        for s in (-1, 1):
            ell(cv, 16 + s * 6.5, 26.5, 2.6, 2, WHITE)
            _px(cv, 16 + s * 6.5, 28, WHITE_S)


def body(cv, L, item, sit):
    d, dd = (C(c) for c in L.get("dress", ("2e2a3a", "201d2b")))
    skirt_to = L.get("skirt_to", 41)
    if sit:
        skirt_to = min(skirt_to, 41)
    # legs + shoes
    legs, shoes = L["legs"], L["shoes"]
    if sit:
        for x0 in (12, 17):
            rect(cv, x0, skirt_to, x0 + 2, skirt_to + 1, legs)
            rect(cv, x0 - (1 if x0 == 12 else 0), skirt_to + 2, x0 + 2 + (1 if x0 == 17 else 0), skirt_to + 3, shoes)
    else:
        for x0 in (12, 17):
            rect(cv, x0, skirt_to, x0 + 2, 44, legs)
            rect(cv, x0 - (1 if x0 == 12 else 0), 45, x0 + 2 + (1 if x0 == 17 else 0), 47, shoes)
            _px(cv, x0 + (0 if x0 == 12 else 2), 45, shade(shoes, 0.35))
    if L.get("jacket"):
        jk, red = C(L["jacket"][0]), C(L["jacket"][1])
        sk, skd = (C(c) for c in L["skirt"])
        poly(cv, [(10.5, 31), (21.5, 31), (23, skirt_to), (9, skirt_to)], sk)
        for x in range(10, 23, 3):
            for y in range(32, skirt_to + 1):
                _px(cv, x, y, skd)
        poly(cv, [(11, 24.5), (21, 24.5), (22, 33), (10, 33)], jk)
        rect(cv, 15, 25, 16, 32, C("f6f3ec"))
        rect(cv, 15, 26, 16, 26, red)
        rect(cv, 10, 32, 22, 32, red)
        rect(cv, 11, 25, 11, 32, red)
        rect(cv, 20, 25, 20, 32, red)
    else:
        poly(cv, [(11, 24.5), (21, 24.5), (21.5, 31), (10.5, 31)], d)
        def skirt(x, y):
            t = (y - 31) / max(1, skirt_to - 31)
            if x < 16 - 5.5 - t * 3.5 + 1.2 or x > 16 + 5.5 + t * 3.5 - 1.2:
                return dd
            return dd if (x - 16) % 5 == 0 and y > 33 else d
        poly(cv, [(10.5, 30.5), (21.5, 30.5), (16 + 9, skirt_to + 1), (16 - 9, skirt_to + 1)], None, skirt)
        if L.get("hem") is not None:
            rect(cv, 8, skirt_to, 24, skirt_to, L["hem"])
    if L.get("apron"):
        poly(cv, [(13, 27), (19, 27), (21, skirt_to - 1), (11, skirt_to - 1)], WHITE)
        rect(cv, 12, 30, 20, 30, WHITE_S)
        for x in range(11, 22, 2):
            _px(cv, x, skirt_to, WHITE)
        rect(cv, 13, 25, 19, 26, WHITE)
    if L.get("corset"):
        cc, gold = (C(c) for c in L["corset"])
        poly(cv, [(12, 27), (20, 27), (20.5, 32), (16, 34), (11.5, 32)], cc)
        for y in (28, 30, 32):
            _px(cv, 15, y, gold)
            _px(cv, 16, y, gold)
        rect(cv, 12, 25, 19, 25, WHITE)
    if L.get("stars"):
        for (x, y) in ((11, 34), (19, 33), (14, 37), (21, 38), (10, 39), (17, 40), (13, 28), (19, 29)):
            if y <= skirt_to:
                _px(cv, x, y, C("f6f3ec"))
    if L.get("collar"):
        rect(cv, 13, 24, 19, 25, WHITE)
        _px(cv, 16, 25, WHITE_S)
    if L.get("bow") is not None:
        poly(cv, [(13, 25), (16, 26.5), (13, 28)], L["bow"])
        poly(cv, [(19.5, 25), (16.5, 26.5), (19.5, 28)], L["bow"])
        rect(cv, 15, 26, 16, 27, shade(L["bow"], 0.3))
    arms(cv, L, item, sit)
    if L.get("headphones"):
        hp = C("2b2733")
        rect(cv, 11, 24, 21, 24, hp)
        for x0 in (10, 20):
            rect(cv, x0, 23, x0 + 2, 26, hp)
        _px(cv, 11, 24, C("c9473f"))
        _px(cv, 21, 24, C("c9473f"))
    # neck
    rect(cv, 14, 23, 17, 24, SKIN_S)


def held(cv, item, lift=0):
    if lift:
        tmp = Canvas(W, H)
        held(tmp, item)
        for i, p in enumerate(tmp.p):
            if p:
                x, y = i % W, i // W
                _px(cv, x, y - lift, p)
        return
    if item == "book":
        rect(cv, 11, 28, 20, 33, C("8a3d4b"))
        rect(cv, 12, 28, 19, 28, C("f6f3ec"))
        rect(cv, 15, 29, 16, 33, C("6b2e3a"))
        _px(cv, 13, 31, C("e4b960"))
    elif item == "phone":
        rect(cv, 14, 28, 17, 33, C("2b2733"))
        rect(cv, 15, 29, 16, 31, C("66d5d0"))
    elif item == "pad":
        rect(cv, 11, 29, 20, 32, C("3e3b4c"))
        _px(cv, 13, 30, C("c9473f"))
        _px(cv, 18, 30, C("66d5d0"))
        _px(cv, 19, 31, C("f2cf55"))
    elif item == "ladle":
        for t in range(8):
            _px(cv, 20 + t * 0.5, 31 - t, C("9aa3b0"))
        ell(cv, 24.5, 22.5, 1.6, 1.2, C("9aa3b0"))
        rect(cv, 11, 30, 14, 33, C("c4705a"))
    elif item == "cup":
        rect(cv, 14, 29, 17, 32, C("f6f3ec"))
        rect(cv, 14, 29, 17, 29, C("a86f4c"))
        _px(cv, 18, 30, C("f6f3ec"))
    if item != "none":
        for x in (11, 20):
            ell(cv, x + 0.5, 31.5, 1.3, 1.3, SKIN)


def head(cv, L, dy, closed=False):
    hb, hd, hl = hair_col(L, 10)
    # ears behind the head
    if L.get("ears") == "cat":
        for s in (-1, 1):
            poly(cv, [(16 + s * 4, 9 + dy), (16 + s * 9.5, 1 + dy), (16 + s * 10, 11 + dy)], hb)
            poly(cv, [(16 + s * 5.5, 9 + dy), (16 + s * 9, 3.5 + dy), (16 + s * 9, 9 + dy)], L["ear_in"])
    if L.get("fins"):
        fin, fd = C("4f86d6"), C("335fa8")
        for s in (-1, 1):
            poly(cv, [(16 + s * 8, 13 + dy), (16 + s * 15, 8 + dy), (16 + s * 13, 14 + dy), (16 + s * 15, 18 + dy),
                      (16 + s * 8, 17 + dy)], fin)
            _px(cv, 16 + s * 11, 14 + dy, fd)
            _px(cv, 16 + s * 12, 13 + dy, fd)
    # face
    ell(cv, 16, 16 + dy, 8.6, 8.2, SKIN)
    # hair cap + bangs
    def cap(x, y):
        b, d, l = hair_col(L, y)
        return l if (y - dy in (9, 10) and x in (11, 12, 13, 19, 20)) else (d if (x * 3 + y * 5) % 23 == 0 else b)
    ell(cv, 16, 13 + dy, 9.6, 8.4, None, lambda x, y: cap(x, y) if y - dy <= 11 else None)
    for x in range(7, 26):
        bottom = 12 + (2 if x % 4 == 2 else 1 if x % 4 in (1, 3) else 0)
        if x in (15, 16):
            bottom = 11
        for y in range(9, bottom + 1):
            _px(cv, x, y + dy, cap(x, y + dy))
        _px(cv, x, bottom + 1 + dy, SKIN_S if 9 <= x <= 23 else None)
    for s in (-1, 1):  # side locks
        for y in range(11, 25):
            w = 2 if y < 22 else 1
            for k in range(w + 1):
                _px(cv, 16 + s * (8 + k - 1 if s > 0 else 8 + k - 1), y + dy, cap(16 + s * 8, y + dy))
    # eyes
    iris, iris_l = (C(c) for c in L["eyes"])
    iris_r = tuple(C(c) for c in L["eyes_r"]) if L.get("eyes_r") else (iris, iris_l)
    for i, x0 in enumerate((10, 19)):
        ir, il = (iris, iris_l) if i == 0 else iris_r
        if closed:
            rect(cv, x0, 18 + dy, x0 + 2, 18 + dy, INK)
            _px(cv, x0 + (0 if i == 0 else 2), 17 + dy, INK)
            continue
        rect(cv, x0, 16 + dy, x0 + 2, 16 + dy, INK)
        _px(cv, x0 - 1 if i == 0 else x0 + 3, 16 + dy, INK)
        _px(cv, x0, 17 + dy, shade(ir, -0.35))
        _px(cv, x0 + 1, 17 + dy, WHITE)
        _px(cv, x0 + 2, 17 + dy, shade(ir, -0.35) if not L.get("slit_pupil") else ir)
        rect(cv, x0, 18 + dy, x0 + 2, 18 + dy, ir)
        _px(cv, x0 + 1, 19 + dy, il)
        if L.get("slit_pupil"):
            _px(cv, x0 + 1, 18 + dy, shade(ir, -0.5))
    # blush + mouth
    for x in (10, 11, 21, 22):
        _px(cv, x, 20 + dy, BLUSH)
    _px(cv, 15, 21 + dy, C("c46a6a"))
    _px(cv, 16, 21 + dy, C("c46a6a"))
    if L.get("fang"):
        _px(cv, 17, 21 + dy, WHITE)
    # accessories
    if L.get("band") == "maid":
        for x in range(10, 23):
            _px(cv, x, 7 + dy if 12 <= x <= 20 else 8 + dy, WHITE)
        for x in range(11, 22, 2):
            _px(cv, x, 6 + dy if 12 <= x <= 20 else 7 + dy, WHITE)
    if L.get("flower"):
        fx, fy = 8, 10 + dy
        for (dx, dy2) in ((0, -1), (-1, 0), (1, 0), (0, 1)):
            ell(cv, fx + dx * 1.6, fy + dy2 * 1.6, 1.3, 1.3, C("ef8f3a"))
        _px(cv, fx, fy, C("f6d26a"))
        rect(cv, 9, 12 + dy, 9, 15 + dy, C("2a2530"))
        rect(cv, 10, 12 + dy, 10, 14 + dy, C("2a2530"))
    if L.get("star_pin"):
        sx, sy = 23, 9 + dy
        for (dx, dy2, c) in ((0, -2, "66d5d0"), (2, 0, "e387bf"), (0, 2, "8071b7"), (-2, 0, "f6d98b")):
            _px(cv, sx + dx, sy + dy2, C(c))
            _px(cv, sx + dx // 2, sy + dy2 // 2, C(c))
        _px(cv, sx, sy, C("fff6c8"))
    if L.get("horns"):
        hc, hs = C("eef6fb"), C("b9c6d6")
        for s in (-1, 1):
            for t in range(8):
                x = 16 + s * (4 + t * 0.6)
                y = 7 - t + dy
                _px(cv, x, y, hc)
                if t < 6:
                    _px(cv, x + s, y, hc)
                    _px(cv, x + s, y + 1, hs)
    if L.get("ahoge"):
        b = hair_col(L, 4)[0]
        for (x, y) in ((16, 4), (17, 3), (18, 2), (18, 1), (17, 1)):
            _px(cv, x, y + dy, b)


def sprite(actor, pose="stand", item="none", frame=0):
    """frame 0 rest, 1 breath (head down 1px, tail/wings sway one way), 2 blink, 3 breath + sway the other way
    and the held item lifted (sip, page turn, button press)."""
    L = LOOKS[actor]
    cv = Canvas(W, H)
    dy = 1 if frame in (1, 3) else 0
    sway = {1: 1, 3: -1}.get(frame, 0)
    if pose == "sleep":
        back_layer(cv, L, 0, head_only=True)
        head(cv, L, 0, closed=True)
        out = Canvas(W, H)
        out.p = [p if i // W <= 25 else None for i, p in enumerate(cv.p)]
        return out.outlined()
    sit = pose == "sit"
    back_layer(cv, L, dy, sway=sway)
    body(cv, L, item, sit)
    held(cv, item, lift=1 if frame == 3 else 0)
    head(cv, L, dy, closed=frame == 2)
    return cv.outlined()


FRAMES = 4


def atlas(actor):
    """Rows: stand, sit; columns: ITEMS x FRAMES; plus one sleep cell at the end of row 0."""
    cols = len(ITEMS) * FRAMES + 1
    at = Canvas(cols * W, 2 * H)
    cells = []
    for r, pose in enumerate(("stand", "sit")):
        for i, it in enumerate(ITEMS):
            for f in range(FRAMES):
                c = (i * FRAMES + f)
                at.blit(sprite(actor, pose, it, f), c * W, r * H)
                cells.append({"name": f"{pose}_{it}_{f}", "x": c * W, "y": r * H, "w": W, "h": H})
    at.blit(sprite(actor, "sleep"), (cols - 1) * W, 0)
    cells.append({"name": "sleep", "x": (cols - 1) * W, "y": 0, "w": W, "h": H})
    return at, {"cell_size": [W, H], "cells": cells, "anchor": {"stand": [16, 47], "sit": [16, 41], "sleep": [16, 25]}}
