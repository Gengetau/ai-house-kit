"""Cross-section (doll-house) overview of AI小屋: 2F five bedrooms, 1F five rooms, the yard. v2 (2026-11-02):
bigger cells for the 48px residents and much more furniture and small things per room."""
import random

from engine import Canvas, C, shade, mix, blob

CELL_H = 150          # v3 (owner 11-02: fill the page height): taller rooms, sky above, front garden below
DROP = 34             # wall decorations sit this much lower than in the 100px-tall v2 rooms
SKY = 64
FRONT = 112
WALL = 5
SLAB = 8
FLOOR_T = 5

FLOOR2 = [("bedroom_0", "卧室 A", 150, "f9ead8", "e2b8a8"), ("bedroom_1", "卧室 B", 150, "f1e5ca", "8a5a40"),
          ("bedroom_2", "卧室 C", 176, "dce8df", "6a9270"), ("bedroom_3", "卧室 D", 128, "5a4f74", "352b4a"),
          ("bedroom_4", "卧室 E", 150, "d9c6ea", "8071b7")]
FLOOR1 = [("living", "客厅", 214, "efe1c4", "c79a6b"), ("kitchen", "厨房", 186, "f4ecd8", "bfe0d8"),
          ("study", "书房", 150, "dcd3be", "7a4d36"), ("bath", "浴室", 96, "e3eef0", "87b7ac"),
          ("bedroom_5", "卧室 F", 108, "cfe6ef", "5386b7")]
YARD_W = 176


def R(cv, x, y, w, h, c, glow=False):
    cv.rect(int(x), int(y), int(w), int(h), C(c) if isinstance(c, str) else c, glow)


def boxf(cv, x, y, w, h, c, line="2a2035", glow=False):
    base = C(c) if isinstance(c, str) else c
    R(cv, x - 1, y - 1, w + 2, h + 2, line)
    R(cv, x, y, w, h, base, glow)
    R(cv, x, y, w, 1, shade(base, 0.25), glow)
    if h > 2:
        R(cv, x + w - 1, y + 1, 1, h - 1, shade(base, -0.2), glow)


def win(cv, x, y, w, h, night, view="garden", curtain=None):
    R(cv, x - 2, y - 2, w + 4, h + 4, "f4f1ea")
    top, bot = (C("1d2447"), C("3a3f73")) if night else (C("8cc9e6"), C("d7eef0"))
    for yy in range(h):
        R(cv, x, y + yy, w, 1, mix(top, bot, yy / max(1, h - 1)), glow=night)
    if view == "garden":
        R(cv, x, y + h - 5, w, 5, "3f6553" if night else "6a9270", glow=night)
    if night:
        rnd = random.Random(x * 3 + y)
        for _ in range(max(1, w * h // 45)):
            cv.set(x + rnd.randrange(w), y + rnd.randrange(max(1, h - 6)), C("f6d98b"), True)
    R(cv, x + w // 2, y, 1, h, "f4f1ea")
    R(cv, x, y + h // 2, w, 1, "f4f1ea")
    R(cv, x - 3, y + h + 2, w + 6, 2, "d9cdb6")
    if curtain:
        R(cv, x - 6, y - 4, 5, h + 9, curtain)
        R(cv, x + w + 1, y - 4, 5, h + 9, curtain)
        R(cv, x - 6, y - 4, 1, h + 9, shade(C(curtain), -0.3))
        R(cv, x + w + 5, y - 4, 1, h + 9, shade(C(curtain), -0.3))
        R(cv, x - 8, y - 5, w + 16, 1, "7a4d36")


def frame(cv, x, y, w, h, cols=("87b7ac", "f6d98b", "e387bf"), fr="b6813f"):
    R(cv, x - 1, y - 1, w + 2, h + 2, fr)
    R(cv, x, y, w, h, cols[0])
    R(cv, x, y, w, h * 2 // 5, cols[1])
    R(cv, x + w * 3 // 5, y + h // 2, max(1, w // 4), max(1, h // 3), cols[2])


def ceiling_lamp(cv, x, y0, c="f6d98b"):
    R(cv, x, y0, 1, 20, "3e3b4c")
    boxf(cv, x - 4, y0 + 20, 9, 4, c, glow=True)
    R(cv, x - 2, y0 + 24, 5, 1, shade(C(c), 0.3), glow=True)


def bed(cv, x, fy, w, blanket, frame_c="a86f4c", pillow="eee7da"):
    boxf(cv, x, fy - 22, 5, 22, frame_c)
    boxf(cv, x + w - 4, fy - 14, 4, 14, frame_c)
    boxf(cv, x + 5, fy - 10, w - 9, 6, frame_c)
    boxf(cv, x + 5, fy - 14, w - 9, 4, "f6f3ec")
    boxf(cv, x + 6, fy - 18, 12, 5, pillow)
    boxf(cv, x + 18, fy - 16, w - 22, 6, blanket)
    R(cv, x + 18, fy - 13, w - 22, 1, shade(C(blanket), -0.15))


def shelf(cv, x, fy, w, h, seed=1, wood="a86f4c"):
    rnd = random.Random(seed)
    boxf(cv, x, fy - h, w, h, wood)
    rows = max(2, h // 12)
    rh = (h - 3) // rows
    for r in range(rows):
        y0 = fy - h + 2 + r * rh
        R(cv, x + 2, y0, w - 4, rh - 2, "4a2e24")
        xx = x + 2
        while xx < x + w - 3:
            bw = rnd.choice((1, 2, 2))
            bh = rnd.randint(max(3, rh - 6), rh - 2)
            if rnd.random() < 0.1:
                xx += 2
                continue
            R(cv, xx, y0 + rh - 2 - bh, bw, bh,
              rnd.choice(["c45e64", "5386b7", "6a9270", "e4b960", "8071b7", "eee7da", "d5804c", "53878a"]))
            xx += bw


def plant(cv, x, fy, s=1.0, seed=3, pot="c4705a"):
    boxf(cv, x, fy - 8, 8, 8, pot)
    blob(cv, x + 4, fy - 8 - 8 * s, 6 * s, 8 * s, [C("3f6553"), C("6a9270"), C("a9c58a")], seed=seed)


def floor_lamp(cv, x, fy, c="f6d98b"):
    boxf(cv, x, fy - 2, 7, 2, "3e3b4c")
    R(cv, x + 3, fy - 30, 1, 28, "3e3b4c")
    boxf(cv, x, fy - 37, 8, 7, c, glow=True)


def desk(cv, x, fy, w, monitor=False, lamp=True, wood="a86f4c", chair="d49c67"):
    boxf(cv, x, fy - 17, w, 3, wood)
    boxf(cv, x + 1, fy - 14, 3, 14, shade(C(wood), -0.2))
    boxf(cv, x + w - 11, fy - 14, 10, 14, wood)
    R(cv, x + w - 9, fy - 9, 6, 1, shade(C(wood), -0.4))
    if monitor:
        boxf(cv, x + 4, fy - 33, 20, 13, "2b2733")
        R(cv, x + 5, fy - 32, 18, 11, "66d5d0", glow=True)
        R(cv, x + 7, fy - 29, 8, 3, "8071b7", glow=True)
        R(cv, x + 13, fy - 20, 2, 3, "2b2733")
    elif lamp:
        R(cv, x + 4, fy - 26, 1, 9, "3e3b4c")
        boxf(cv, x + 2, fy - 30, 6, 4, "f6d98b", glow=True)
        boxf(cv, x + 12, fy - 20, 8, 3, "c45e64")
        boxf(cv, x + 13, fy - 23, 7, 3, "5386b7")
    boxf(cv, x + w // 2 - 5, fy - 10, 11, 2, chair)
    R(cv, x + w // 2 - 4, fy - 8, 1, 8, shade(C(chair), -0.3))
    R(cv, x + w // 2 + 4, fy - 8, 1, 8, shade(C(chair), -0.3))


def rug(cv, x, fy, w, c1, c2):
    for i in range(w):
        cv.set(x + i, fy - 1, C(c1) if (i // 3) % 2 else C(c2))


def wardrobe(cv, x, fy, w=24, h=56, c="d49c67"):
    boxf(cv, x, fy - h, w, h, c)
    R(cv, x + w // 2, fy - h + 2, 1, h - 4, shade(C(c), -0.35))
    R(cv, x + w // 2 - 3, fy - h // 2, 1, 5, "e4b960")
    R(cv, x + w // 2 + 2, fy - h // 2, 1, 5, "e4b960")
    boxf(cv, x - 1, fy - h - 3, w + 2, 2, shade(C(c), -0.15))


def sofa(cv, x, fy, w, c="e9dcc0", cushion="f4d27a"):
    boxf(cv, x, fy - 24, w, 10, c)
    boxf(cv, x, fy - 14, w, 12, c)
    boxf(cv, x - 2, fy - 19, 6, 17, shade(C(c), -0.08))
    boxf(cv, x + w - 4, fy - 19, 6, 17, shade(C(c), -0.08))
    boxf(cv, x + 8, fy - 27, 10, 8, cushion)
    boxf(cv, x + w - 18, fy - 27, 10, 8, "e387bf")
    R(cv, x + 2, fy - 2, 2, 2, "3e3b4c")
    R(cv, x + w - 4, fy - 2, 2, 2, "3e3b4c")


def mugs(cv, x, y):
    boxf(cv, x, y - 3, 3, 3, "f6f3ec")
    boxf(cv, x + 6, y - 3, 3, 3, "c45e64")


def room_items(cv, key, x, y0, fy, w, night):
    ceiling = y0 - DROP + 1
    if key == "bedroom_0":
        win(cv, x + 70, y0 + 16, 24, 22, night, curtain="e387bf")
        frame(cv, x + 112, y0 + 24, 14, 11, ("f4c6dd", "f6d98b", "6a9270"))
        ceiling_lamp(cv, x + 50, ceiling, "fbe3ee")
        rug(cv, x + 50, fy, 50, "f4c6dd", "e387bf")
        bed(cv, x + 6, fy, 44, "f4c6dd")
        wardrobe(cv, x + 56, fy, 22, 52)
        R(cv, x + 60, fy - 40, 6, 12, "f6f3ec")
        desk(cv, x + 100, fy, 44)
        boxf(cv, x + 84, fy - 9, 10, 9, "d3a273")
        R(cv, x + 86, fy - 11, 5, 2, "e387bf")
    elif key == "bedroom_1":
        win(cv, x + 82, y0 + 16, 24, 22, night, view="sky", curtain="8a3d4b")
        ceiling_lamp(cv, x + 60, ceiling)
        rug(cv, x + 50, fy, 60, "8a3d4b", "a05454")
        shelf(cv, x + 4, fy, 24, 64, 7)
        shelf(cv, x + 30, fy, 20, 50, 8)
        bed(cv, x + 106, fy, 40, "f6f3ec")
        boxf(cv, x + 56, fy - 16, 16, 16, "8a3d4b")
        boxf(cv, x + 56, fy - 26, 5, 10, "8a3d4b")
        boxf(cv, x + 76, fy - 15, 14, 2, "f4f1ea")
        R(cv, x + 82, fy - 13, 2, 13, "7a4d36")
        mugs(cv, x + 77, fy - 15)
        boxf(cv, x + 94, fy - 12, 5, 12, "252b3d")
        for (dx, dy) in ((0, -16), (3, -18), (5, -15), (1, -20)):
            R(cv, x + 93 + dx, fy + dy, 2, 2, "ef8f3a")
    elif key == "bedroom_2":
        win(cv, x + 88, y0 + 14, 26, 24, night, view="sky", curtain="5e6a8a")
        for i in range(10):
            cv.set(x + 88 + (i * 7) % 26, y0 + 14 + (i * 5) % 24, C("f4f9fb"), night)
        ceiling_lamp(cv, x + 60, ceiling, "dfeaf2")
        rug(cv, x + 40, fy, 80, "dfeaf2", "aab8c8")
        bed(cv, x + 6, fy, 58, "eef2f4", frame_c="b9c6d6")
        for i, (dx, r) in enumerate(((10, 5), (24, 4), (52, 5))):
            blob(cv, x + dx, fy - 19, r, 3, [C("aab8c8"), C("dfe8ef"), C("f4f9fb")], seed=4 + i)
        for i in range(2):
            boxf(cv, x + 70 + i * 22, fy - 44, 20, 44, "b9c6d6")
            for r in range(3):
                R(cv, x + 72 + i * 22, fy - 41 + r * 14, 16, 10, "dfeaf2")
                for k in range(3):
                    R(cv, x + 73 + i * 22 + k * 5, fy - 37 + r * 14, 3, 4 + (k + r) % 2 * 2,
                      ["e4b960", "8cc9d6", "bc9cd5", "eef2f4"][(k + r + i) % 4])
        boxf(cv, x + 118, fy - 14, 24, 14, "5e6a8a")
        R(cv, x + 129, fy - 9, 2, 3, "e4b960")
        boxf(cv, x + 146, fy - 10, 20, 10, "8a7aa8")
        for i in range(6):
            boxf(cv, x + 120 + i * 3, fy - 17 - (i % 2) * 2, 2, 2, "e4b960")
        for (dx, h) in ((0, 14), (3, 20), (6, 12)):
            R(cv, x + 160 + dx, fy - 10 - h, 2, h, "8cc9d6", glow=True)
            R(cv, x + 160 + dx, fy - 10 - h, 1, h, "dfeaf2", glow=True)
    elif key == "bedroom_3":
        R(cv, x + 50, y0 + 12, 30, 28, "252b3d")
        for i in range(0, 30, 5):
            R(cv, x + 50 + i, y0 + 12, 1, 28, "352b4a")
        frame(cv, x + 8, y0 + 18, 14, 18, ("c45e64", "252b3d", "66d5d0"), fr="252b3d")
        frame(cv, x + 28, y0 + 24, 12, 14, ("8071b7", "e4b960", "e387bf"), fr="252b3d")
        for i in range(14):
            cv.set(x + 6 + i * 8, y0 - DROP + 9 + abs(i % 4 - 2), C(["f6d98b", "e387bf", "66d5d0"][i % 3]), True)
        rug(cv, x + 40, fy, 44, "51436f", "6a5a8a")
        bed(cv, x + 4, fy, 40, "51436f", frame_c="3e3b4c")
        desk(cv, x + 74, fy, 46, monitor=True, wood="3e3b4c", chair="c45e64")
        for i, c in enumerate(("e4b960", "c45e64", "66d5d0", "f6d98b")):
            boxf(cv, x + 50 + i * 5, fy - 4 - i % 2 * 3, 4, 4 + i % 2 * 3, c)
    elif key == "bedroom_4":
        win(cv, x + 96, y0 + 14, 26, 24, night, view="sky", curtain="51436f")
        for i in range(9):
            for (dx, dy) in ((0, 0), (-1, 1), (1, 1), (0, 2)):
                cv.set(x + 10 + i * 9 + dx, y0 + 12 + (i % 3) * 6 + dy, C("f6d98b"), True)
        ceiling_lamp(cv, x + 70, ceiling, "e6d6f5")
        rug(cv, x + 50, fy, 50, "bc9cd5", "8071b7")
        bed(cv, x + 6, fy, 44, "bc9cd5")
        R(cv, x + 104, fy - 26, 1, 26, "3e3b4c")
        R(cv, x + 112, fy - 26, 1, 26, "3e3b4c")
        boxf(cv, x + 100, fy - 34, 20, 6, "e4b960")
        boxf(cv, x + 60, fy - 7, 22, 7, "bc9cd5")
        R(cv, x + 62, fy - 8, 18, 2, "f6f3ec")
        boxf(cv, x + 128, fy - 16, 18, 16, "d49c67")
        boxf(cv, x + 132, fy - 23, 6, 7, "dfeaf2")
        R(cv, x + 134, fy - 20, 2, 2, "f6d98b", glow=True)
    elif key == "living":
        win(cv, x + 130, y0 + 14, 30, 26, night, curtain="c4705a")
        win(cv, x + 178, y0 + 14, 22, 26, night, curtain="c4705a")
        frame(cv, x + 60, y0 + 22, 24, 16)
        for i in range(20):
            cv.set(x + 6 + i * 10, y0 - DROP + 9 + abs(i % 4 - 2), C(["f6d98b", "e387bf", "66d5d0", "edad69"][i % 4]), True)
        ceiling_lamp(cv, x + 90, ceiling)
        rug(cv, x + 46, fy, 80, "d5804c", "e4b960")
        shelf(cv, x + 4, fy, 24, 62, 11)
        floor_lamp(cv, x + 32, fy)
        sofa(cv, x + 46, fy, 62)
        boxf(cv, x + 112, fy - 12, 22, 2, "a86f4c")
        R(cv, x + 114, fy - 10, 2, 10, "7a4d36")
        R(cv, x + 130, fy - 10, 2, 10, "7a4d36")
        mugs(cv, x + 116, fy - 12)
        boxf(cv, x + 140, fy - 12, 30, 12, "7a4d36")
        boxf(cv, x + 143, fy - 32, 24, 18, "2b2733")
        R(cv, x + 144, fy - 31, 22, 16, "4f7fc2", glow=True)
        R(cv, x + 147, fy - 27, 8, 5, "8cc9d6", glow=True)
        boxf(cv, x + 160, fy - 15, 6, 3, "f6f3ec")
        boxf(cv, x + 176, fy - 30, 30, 30, "3e3b4c")
        R(cv, x + 177, fy - 18, 28, 3, "f6f3ec")
        for i in range(178, 205, 3):
            cv.set(x + i, fy - 18, C("252b3d"))
    elif key == "kitchen":
        for i in range(0, 110, 6):
            for j in range(0, 18, 6):
                R(cv, x + 26 + i, fy - 40 + j, 5, 5, "bfe0d8" if (i // 6 + j // 6) % 2 else "e6f2ee")
        win(cv, x + 150, y0 + 14, 24, 22, night, curtain="e4b960")
        ceiling_lamp(cv, x + 160, ceiling)
        boxf(cv, x + 4, fy - 58, 20, 58, "eef2f4")
        R(cv, x + 5, fy - 38, 18, 1, "b9c6d6")
        R(cv, x + 20, fy - 34, 1, 8, "737d94")
        R(cv, x + 8, fy - 52, 4, 4, "e387bf")
        R(cv, x + 14, fy - 50, 3, 3, "f6d98b")
        boxf(cv, x + 26, fy - 20, 110, 20, "d9cdb6")
        boxf(cv, x + 26, fy - 22, 110, 2, "eee7da")
        for i in range(5):
            R(cv, x + 30 + i * 22, fy - 16, 18, 13, shade(C("d9cdb6"), 0.1))
            R(cv, x + 37 + i * 22, fy - 12, 4, 1, "737d94")
        R(cv, x + 56, fy - 22, 18, 1, "8cc9d6")
        R(cv, x + 64, fy - 28, 1, 6, "9aa3b0")
        R(cv, x + 90, fy - 22, 14, 1, "3e3b4c")
        R(cv, x + 92, fy - 22, 4, 1, "e07a4c", glow=True)
        boxf(cv, x + 96, fy - 28, 9, 6, "9aa3b0")
        boxf(cv, x + 112, fy - 28, 5, 6, "c45e64")
        boxf(cv, x + 119, fy - 26, 4, 4, "e4b960")
        boxf(cv, x + 26, fy - 70, 50, 16, "d9cdb6")
        boxf(cv, x + 86, fy - 70, 50, 16, "d9cdb6")
        for i in range(4):
            R(cv, x + 38 + i * 25, fy - 60, 1, 4, "737d94")
        boxf(cv, x + 144, fy - 18, 38, 3, "a86f4c")
        R(cv, x + 146, fy - 15, 2, 15, "7a4d36")
        R(cv, x + 178, fy - 15, 2, 15, "7a4d36")
        mugs(cv, x + 150, fy - 18)
        boxf(cv, x + 164, fy - 21, 8, 3, "f6f3ec")
    elif key == "study":
        win(cv, x + 108, y0 + 14, 26, 24, night, curtain="3f6553")
        ceiling_lamp(cv, x + 80, ceiling)
        rug(cv, x + 50, fy, 50, "6a9270", "87b7ac")
        shelf(cv, x + 4, fy, 24, 76, 31, wood="7a4d36")
        shelf(cv, x + 30, fy, 24, 76, 32, wood="7a4d36")
        boxf(cv, x + 60, fy - 18, 18, 18, "8a3d4b")
        boxf(cv, x + 60, fy - 30, 5, 12, "8a3d4b")
        boxf(cv, x + 80, fy - 12, 8, 2, "a86f4c")
        mugs(cv, x + 81, fy - 12)
        desk(cv, x + 92, fy, 44)
        R(cv, x + 142, fy - 36, 1, 36, "7a4d36")
        boxf(cv, x + 137, fy - 42, 11, 14, "f6f3ec")
        R(cv, x + 139, fy - 38, 6, 5, "87b7ac")
    elif key == "bath":
        boxf(cv, x + 4, fy - 18, 50, 18, "f4f1ea")
        R(cv, x + 6, fy - 18, 46, 2, "8cc9d6")
        R(cv, x + 50, fy - 26, 1, 8, "9aa3b0")
        boxf(cv, x + 58, fy - 22, 16, 22, "eef2f4")
        R(cv, x + 61, fy - 17, 10, 10, "737d94")
        R(cv, x + 62, fy - 16, 8, 8, "8cc9d6")
        boxf(cv, x + 80, fy - 16, 12, 16, "a86f4c")
        boxf(cv, x + 78, fy - 19, 16, 3, "f4f1ea")
        R(cv, x + 80, fy - 44, 12, 18, "b6813f")
        R(cv, x + 81, fy - 43, 10, 16, "cfe9f0")
        win(cv, x + 22, y0 + 14, 16, 14, night, view="sky")
        R(cv, x + 56, fy - 46, 18, 2, "8a5a40")
        for i, c in enumerate(("e387bf", "87b7ac", "f6d98b")):
            boxf(cv, x + 58 + i * 6, fy - 52, 3, 5, c)
    elif key == "bedroom_5":
        win(cv, x + 72, y0 + 14, 22, 22, night, curtain="334b7c")
        for i in range(0, w - 4, 8):
            R(cv, x + i, y0 + 46, 5, 1, "8cc9d6")
            R(cv, x + i + 4, y0 + 47, 4, 1, "8cc9d6")
        ceiling_lamp(cv, x + 50, ceiling, "cfe6ef")
        rug(cv, x + 40, fy, 40, "5386b7", "8cc9d6")
        bed(cv, x + 4, fy, 38, "5386b7")
        boxf(cv, x + 46, fy - 14, 36, 14, "7a4d36")
        boxf(cv, x + 46, fy - 36, 36, 22, "53878a", glow=True)
        R(cv, x + 47, fy - 35, 34, 2, "8cc9d6", glow=True)
        for (dx, dy, c) in ((6, 28, "ed9a5c"), (18, 22, "f6d98b"), (27, 30, "e387bf")):
            R(cv, x + 46 + dx, fy - dy, 3, 2, c, glow=True)
        R(cv, x + 56, fy - 26, 1, 10, "6a9270", glow=True)
        boxf(cv, x + 86, fy - 18, 18, 18, "d49c67")
        for i, c in enumerate(("e4b960", "c45e64", "66d5d0")):
            boxf(cv, x + 88 + i * 5, fy - 25, 4, 7, c)


def front_garden(cv, W, ground, H, x0, W2, night):
    """The lawn in front of the house (below the cross-section): path, hedges, flowers, mailbox, lamp post."""
    g1, g2 = (C("2f4a3e"), C("3f6553")) if night else (C("6a9270"), C("86ad7c"))
    R(cv, 0, ground, W, H - ground, g1)
    rnd = random.Random(31)
    for _ in range(W * (H - ground) // 9):
        xx, yy = rnd.randrange(W), ground + rnd.randrange(H - ground)
        cv.set(xx, yy, g2)
    R(cv, 0, ground, W, 3, shade(g1, -0.25))
    # foundation under the house
    R(cv, x0, ground, W2 + 5, 8, "8a7a6a")
    for i in range(x0, x0 + W2 + 5, 14):
        R(cv, i, ground + 1, 1, 7, "6b5e52")
    R(cv, x0, ground + 4, W2 + 5, 1, "6b5e52")
    # gravel path along the front
    py = ground + 46
    R(cv, 0, py, W, 16, "b8a68c" if not night else "6e6458")
    for _ in range(W // 2):
        cv.set(rnd.randrange(W), py + rnd.randrange(16), C("d3c4a8") if not night else C("857a6c"))
    R(cv, 0, py, W, 1, shade(g1, -0.3))
    R(cv, 0, py + 16, W, 1, shade(g1, -0.3))
    # stepping stones from the path to the house
    for k, sx in enumerate((x0 + 120, x0 + 460, x0 + 760)):
        for j in range(3):
            boxf(cv, sx + (j % 2) * 4, ground + 12 + j * 11, 14, 5, "b9c6d6" if not night else "707a8c")
    # hedges and flower beds between the house and the path
    leaf = [C("2f4a3e"), C("3f6553"), C("6a9270")] if night else [C("3f6553"), C("6a9270"), C("a9c58a")]
    for k in range(0, W2, 46):
        if any(abs(x0 + k - sx) < 30 for sx in (x0 + 120, x0 + 460, x0 + 760)):
            continue
        blob(cv, x0 + k + 14, ground + 22, 15, 9, leaf, seed=60 + k)
        for j in range(5):
            cv.set(x0 + k + 5 + j * 4, ground + 16 + (j * 7) % 9,
                   C(["e387bf", "f6d98b", "c45e64", "bc9cd5", "f4f1ea"][(k // 46 + j) % 5]))
    # front flowers along the bottom
    for k in range(6, W, 11):
        yy = py + 26 + (k * 7) % 12
        R(cv, k, yy, 1, 4, leaf[1])
        cv.set(k, yy - 1, C(["e387bf", "f6d98b", "f4f1ea", "c45e64"][(k // 11) % 4]))
    # mailbox
    mx = x0 + W2 + 24
    R(cv, mx + 4, ground + 20, 2, 26, "5e3a2c")
    boxf(cv, mx, ground + 12, 12, 9, "c45e64")
    R(cv, mx + 2, ground + 15, 8, 1, "8a3d4b")
    # lamp posts (glow at night)
    for lx in (x0 + 300, x0 + 640, W - 30):
        R(cv, lx, py - 30, 2, 30, "3e3b4c")
        R(cv, lx - 2, py - 31, 6, 2, "3e3b4c")
        boxf(cv, lx - 2, py - 39, 6, 7, "f6d98b" if night else "eee7da", glow=night)
        if night:
            for yy in range(-10, 11):
                for xx in range(-14, 15):
                    if (xx * xx) / 196 + (yy * yy) / 100 <= 1 and (xx + yy) % 2 == 0:
                        c = cv.get(lx + xx, py - 36 + yy)
                        if c and not cv.glow[(py - 36 + yy) * cv.w + lx + xx]:
                            cv.set(lx + xx, py - 36 + yy, mix(c, C("f6d98b"), 0.25))
    # a cat napping on the path
    cx = x0 + 540
    R(cv, cx, py + 6, 10, 4, "eef2f4" if not night else "aab0c0")
    R(cv, cx + 8, py + 3, 4, 4, "eef2f4" if not night else "aab0c0")
    cv.set(cx + 8, py + 2, C("e387bf"))
    cv.set(cx + 11, py + 2, C("e387bf"))
    R(cv, cx - 3, py + 8, 3, 1, "eef2f4" if not night else "aab0c0")
    # lower lawn: bushes, a bench, a little pond, a bicycle
    by = py + 50
    for k in range(20, W, 70):
        blob(cv, k, by + (k * 3) % 10, 12, 7, leaf, seed=90 + k)
    bx = x0 + 380
    boxf(cv, bx, by - 8, 34, 3, "a86f4c")
    boxf(cv, bx, by - 18, 34, 3, "a86f4c")
    R(cv, bx + 2, by - 5, 2, 8, "5e3a2c")
    R(cv, bx + 30, by - 5, 2, 8, "5e3a2c")
    pxc, pyc = x0 + 820, by - 2
    for yy in range(-8, 9):
        for xx in range(-26, 27):
            d = (xx * xx) / 676 + (yy * yy) / 64
            if d <= 1:
                c = C("5386b7") if d < 0.75 else C("b9c6d6")
                if night:
                    c = shade(c, -0.35)
                cv.set(pxc + xx, pyc + yy, c)
    cv.set(pxc - 6, pyc - 2, C("f4f9fb"))
    cv.set(pxc + 9, pyc + 2, C("f4f9fb"))
    R(cv, pxc - 12, pyc - 1, 4, 2, "e387bf")
    kx = x0 + 120
    for r_ in ((kx, by - 4), (kx + 22, by - 4)):
        for yy in range(-5, 6):
            for xx in range(-5, 6):
                if 16 <= xx * xx + yy * yy <= 25:
                    cv.set(r_[0] + xx, r_[1] + yy, C("3e3b4c"))
    R(cv, kx, by - 9, 22, 1, "c45e64")
    R(cv, kx + 2, by - 12, 1, 8, "c45e64")
    R(cv, kx + 18, by - 12, 1, 8, "c45e64")
    R(cv, kx + 16, by - 13, 6, 1, "3e3b4c")
    R(cv, kx, by - 12, 5, 1, "3e3b4c")


def wall_decor(cv, key, x, fy0, fy, w, paper):
    """Fill bare upper walls: a shelf with things, a frame, a clock, wherever the wall is still plain."""
    p = C(paper)
    plain = {p, shade(p, -0.05)}

    def clear(rx, ry, rw, rh):
        for yy in range(ry - 2, ry + rh + 2):
            for xx in range(rx - 2, rx + rw + 2):
                if xx < x + 3 or xx >= x + w - 3 or cv.get(xx, yy) not in plain:
                    return False
        return True

    rnd = random.Random(sum(map(ord, key)))
    pal = [["c45e64", "5386b7", "e4b960", "6a9270"], ["e387bf", "f4f1ea", "bc9cd5", "f6d98b"],
           ["87b7ac", "edad69", "8071b7", "f4f1ea"]][len(key) % 3]
    placed = []
    plan = [("shelf", 30, 12, fy - 74), ("frame", 18, 14, fy0 + 46), ("clock", 8, 8, fy0 + 40),
            ("frame2", 14, 18, fy - 92), ("plant", 12, 12, fy - 92), ("shelf2", 24, 12, fy0 + 70)]
    for kind, rw, rh, ry in plan:
        cands = list(range(x + 6, x + w - rw - 6, 4))
        rnd.shuffle(cands)
        for rx in cands:
            if any(abs(rx - px) < 26 and abs(ry - py_) < 30 for px, py_ in placed):
                continue
            if not clear(rx, ry, rw, rh):
                continue
            if kind.startswith("shelf"):
                R(cv, rx, ry + rh - 2, rw, 2, "8a5a40")
                R(cv, rx + 2, ry + rh, 1, 3, "5e3a2c")
                R(cv, rx + rw - 3, ry + rh, 1, 3, "5e3a2c")
                ix = rx + 1
                for i in range(5):
                    bw, bh = 2 + (i + len(key)) % 3, 5 + (i * 3 + len(key)) % 5
                    if ix + bw > rx + rw - 1:
                        break
                    boxf(cv, ix, ry + rh - 2 - bh, bw, bh, pal[i % 4])
                    ix += bw + 2
            elif kind.startswith("frame"):
                frame(cv, rx, ry, rw, rh, (pal[0], pal[1], pal[2]), fr=["b6813f", "5e3a2c", "f4f1ea"][len(key) % 3])
            elif kind == "clock":
                for yy in range(-4, 5):
                    for xx in range(-4, 5):
                        d = xx * xx + yy * yy
                        if d <= 16:
                            cv.set(rx + 4 + xx, ry + 4 + yy, C("5e3a2c") if d > 9 else C("f4f1ea"))
                R(cv, rx + 4, ry + 1, 1, 3, "3e3b4c")
                R(cv, rx + 4, ry + 4, 2, 1, "3e3b4c")
            elif kind == "plant":
                R(cv, rx, ry + rh - 1, rw, 1, "8a5a40")
                boxf(cv, rx + 3, ry + 6, 6, 5, "c4705a")
                blob(cv, rx + 6, ry + 3, 5, 4, [C("3f6553"), C("6a9270"), C("a9c58a")], seed=len(key))
                for i in range(4):
                    R(cv, rx + 2 + i * 3, ry + 9, 1, 4 + i % 2 * 3, "6a9270")
            placed.append((rx, ry))
            break


def build(night=False):
    W2 = sum(c[2] for c in FLOOR2) + WALL * (len(FLOOR2) + 1)
    W = W2 + YARD_W + 30
    roof_h = 56
    H = SKY + roof_h + 2 * (CELL_H + SLAB) + FRONT
    cv = Canvas(W, H)
    sky_top, sky_bot = (C("1d2447"), C("3a3f73")) if night else (C("bfe3ef"), C("eef6f2"))
    for y in range(H):
        R(cv, 0, y, W, 1, mix(sky_top, sky_bot, y / H))
    rnd = random.Random(7)
    if night:
        for _ in range(140):
            cv.set(rnd.randrange(W), rnd.randrange(H - FRONT), C("f6d98b"), True)
        for yy in range(-7, 8):
            for xx in range(-7, 8):
                if xx * xx + yy * yy <= 49 and (xx + 3) ** 2 + (yy - 2) ** 2 > 30:
                    cv.set(W - 60 + xx, 40 + yy, C("f6e7b8"), True)
    else:
        for k in range(6):
            cx, cy = rnd.randrange(40, W - 40), rnd.randrange(12, SKY + 40)
            for i in range(3):
                blob(cv, cx + i * 9, cy - (i % 2) * 3, 9, 5, [C("e8f2f6"), C("f4f9fb"), C("ffffff")], seed=k * 3 + i,
                     density=1)
        for yy in range(-9, 10):
            for xx in range(-9, 10):
                if xx * xx + yy * yy <= 81:
                    cv.set(W - 60 + xx, 36 + yy, C("fbe6a2") if xx * xx + yy * yy > 50 else C("fff4c8"))
    x0 = 14
    roof_y = SKY
    f2y = roof_y + roof_h
    f1y = f2y + CELL_H + SLAB
    ground = f1y + CELL_H + SLAB
    front_garden(cv, W, ground, H, x0, W2, night)
    for i in range(roof_h):
        inset = int((roof_h - i) * 1.25)
        c = C("8a3d4b") if (i // 5) % 2 else C("a05454")
        R(cv, x0 - 8 + inset, roof_y + i, W2 + 16 - 2 * inset, 1, c)
        if i % 5 == 4:
            for xx in range(x0 - 8 + inset, x0 + W2 + 8 - inset, 9):
                cv.set(xx + (i // 5) % 2 * 4, roof_y + i, shade(c, -0.3))
    R(cv, x0 - 10, f2y - 4, W2 + 20, 4, "5e2a35")
    R(cv, x0 + W2 - 90, roof_y + 2, 14, 26, "8a5a40")
    R(cv, x0 + W2 - 92, roof_y, 18, 4, "5e3a2c")
    win(cv, x0 + W2 // 2 - 10, roof_y + 28, 20, 16, night)
    cells = []
    for row, fy0 in ((FLOOR2, f2y), (FLOOR1, f1y)):
        x = x0
        R(cv, x0, fy0, W2, CELL_H + SLAB, "c79a6b")
        for key, label, w, paper, wains in row:
            x += WALL
            p = C(paper)
            R(cv, x, fy0, w, CELL_H, p)
            for i in range(0, w, 8):
                R(cv, x + i, fy0, 2, CELL_H - 26, shade(p, -0.05))
            R(cv, x, fy0, w, 4, shade(p, -0.12))             # crown moulding
            R(cv, x, fy0 + 4, w, 1, shade(p, 0.1))
            wa = C(wains)
            if key != "kitchen":
                R(cv, x, fy0 + CELL_H - FLOOR_T - 22, w, 22, wa)
                for i in range(0, w, 12):
                    R(cv, x + i, fy0 + CELL_H - FLOOR_T - 22, 1, 22, shade(wa, -0.2))
                R(cv, x, fy0 + CELL_H - FLOOR_T - 23, w, 2, shade(wa, -0.35))
            R(cv, x, fy0 + CELL_H - FLOOR_T, w, FLOOR_T, "a86f4c")
            R(cv, x, fy0 + CELL_H - FLOOR_T, w, 1, "d3a273")
            for i in range(0, w, 16):
                R(cv, x + i, fy0 + CELL_H - FLOOR_T + 1, 1, FLOOR_T - 1, "7a4d36")
            fy = fy0 + CELL_H - FLOOR_T
            room_items(cv, key, x, fy0 + DROP, fy, w, night)
            wall_decor(cv, key, x, fy0, fy, w, paper)
            cells.append({"key": key, "label": label, "x": x, "y": fy0, "w": w, "h": CELL_H, "floor": fy,
                          "spots": [int(14 + i * (w - 28) / 5) for i in range(6)]})
            x += w
        R(cv, x0, fy0 + CELL_H, W2 + WALL, SLAB, "7a4d36")
        R(cv, x0, fy0 + CELL_H, W2 + WALL, 1, "a86f4c")
        x = x0
        for key, label, w, paper, wains in row:
            R(cv, x, fy0, WALL, CELL_H, "c79a6b")
            R(cv, x + 1, fy0, 1, CELL_H, "d9b48a")
            x += WALL + w
        R(cv, x, fy0, WALL, CELL_H, "c79a6b")
    yx = x0 + W2 + WALL + 10
    yf = ground
    for i in range(0, YARD_W, 8):
        c = "c79a6b" if (i // 8) % 2 else "a86f4c"
        R(cv, yx + i, yf - 30, 7, 30, c)
        R(cv, yx + i + 2, yf - 33, 3, 3, c)
    R(cv, yx, yf - 22, YARD_W, 2, "7a4d36")
    R(cv, yx, yf - 10, YARD_W, 2, "7a4d36")
    R(cv, yx + 26, yf - 56, 7, 56, "7a4d36")
    blob(cv, yx + 29, yf - 70, 26, 20, [C("3f6553"), C("6a9270"), C("a9c58a")], seed=12)
    boxf(cv, yx + 70, yf - 13, 36, 3, "a86f4c")
    R(cv, yx + 72, yf - 10, 2, 10, "7a4d36")
    R(cv, yx + 102, yf - 10, 2, 10, "7a4d36")
    boxf(cv, yx + 70, yf - 26, 36, 3, "a86f4c")
    boxf(cv, yx + 118, yf - 14, 18, 2, "eee7da")
    R(cv, yx + 126, yf - 12, 2, 12, "9aa3b0")
    mugs(cv, yx + 120, yf - 14)
    R(cv, yx + 142, yf - 40, 1, 40, "9aa3b0")
    R(cv, yx + 172, yf - 40, 1, 40, "9aa3b0")
    R(cv, yx + 142, yf - 40, 31, 1, "9aa3b0")
    for i, c in enumerate(("f6f3ec", "87b7ac", "e387bf", "f6d98b")):
        boxf(cv, yx + 146 + i * 7, yf - 39, 5, 10 - i % 2 * 3, c)
    for i in range(10):
        blob(cv, yx + 40 + i * 5, yf - 3, 3, 3, [C("3f6553"), C("6a9270"), C("a9c58a")], seed=20 + i)
        cv.set(yx + 40 + i * 5, yf - 6, C(["e387bf", "f6d98b", "c45e64", "bc9cd5"][i % 4]))
    cells.append({"key": "yard", "label": "院子", "x": yx, "y": yf - CELL_H, "w": YARD_W, "h": CELL_H,
                  "floor": yf, "spots": [int(14 + i * (YARD_W - 28) / 5) for i in range(6)]})
    return cv, cells
