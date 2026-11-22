"""Build the pixel house assets for ai-house-kit.

python3 tools/pixhouse/build.py OUT_DIR [room ...] [--preview]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Canvas, light_map  # noqa: E402
import rooms as R  # noqa: E402
import overview as OV  # noqa: E402
import chars as CH  # noqa: E402

ACTORS = list(CH.LOOKS.keys())
PREVIEW = ""


def load_chars(out):
    os.makedirs(os.path.join(out, "chars"), exist_ok=True)
    for a in ACTORS:
        at, lay = CH.atlas(a)
        at.save(os.path.join(out, "chars", f"{a}.png"))
        with open(os.path.join(out, "chars", f"{a}.json"), "w", encoding="utf-8") as f:
            json.dump(lay, f)
    return ACTORS


def pack(objs):
    items = []
    for o in objs:
        cv = o.cv.outlined()
        bb = cv.bbox()
        if not bb:
            continue
        items.append((o, cv.crop(*bb), bb))
    maxw = 512
    x = y = rowh = 0
    placed = []
    for o, spr, bb in items:
        if x + spr.w > maxw:
            x, y, rowh = 0, y + rowh + 1, 0
        placed.append((o, spr, bb, x, y))
        x += spr.w + 1
        rowh = max(rowh, spr.h)
    atlas = Canvas(maxw, max(1, y + rowh))
    meta = []
    for o, spr, bb, ax, ay in placed:
        atlas.blit(spr, ax, ay)
        meta.append({"id": o.id, "label": o.label, "x": bb[0], "y": bb[1], "w": spr.w, "h": spr.h,
                     "ax": ax, "ay": ay, "depth": round(o.depth, 2)})
    return atlas, meta


ITEM_FOR = {"reading": "book", "working": "book", "phone": "phone", "gaming": "pad", "cooking": "ladle",
            "tea_time": "cup", "eating": "cup", "browsing": "phone"}


def posed(actor, pose, act=None):
    item = ITEM_FOR.get(act, "none")
    if pose == "sleep":
        return CH.sprite(actor, "sleep"), 25
    if pose == "bath":
        return CH.sprite(actor, "stand").crop(0, 0, 32, 30), 29
    if pose == "sit":
        return CH.sprite(actor, "sit", item), 41
    return CH.sprite(actor, "stand", item if pose == "stand" and act in ITEM_FOR else "none"), 47


def preview(sc_day, objs_meta, atlas, bg, path, k=3, fill=True):
    cv = Canvas(bg.w, bg.h)
    cv.rect(0, 0, bg.w, bg.h, (246, 238, 226, 255))
    cv.blit(bg)
    draws = [(m["depth"], "o", m) for m in objs_meta]
    if fill:
        for i, a in enumerate(sc_day.anchors[:6]):
            draws.append((a["depth"], "c", (ACTORS[i], a)))
    draws.sort(key=lambda d: d[0])
    for _, kind, m in draws:
        if kind == "o":
            spr = atlas.crop(m["ax"], m["ay"], m["ax"] + m["w"], m["ay"] + m["h"])
            cv.blit(spr, m["x"], m["y"])
        else:
            actor, a = m
            spr, row = posed(actor, a["pose"], a["activities"][0])
            cv.blit(spr, a["x"] - 16, a["y"] - row)
    cv.scaled(k).save(path)


def build_room(key, out):
    d = os.path.join(out, "rooms", key)
    os.makedirs(d, exist_ok=True)
    res = {}
    for tag in ("day", "night", "lit"):
        night = tag != "day"
        sc = R.ROOMS[key](night=night)
        objs = sc.objs
        lm = light_map([sc.bg] + [o.cv for o in objs], sc.W, sc.H) if night else None
        dark = tag == "night" or (tag == "lit" and key == "yard")
        bg = sc.bg.night(lm) if dark else sc.bg.lit(lm) if tag == "lit" else sc.bg
        if night:
            for o in objs:
                o.cv = o.cv.night(lm) if dark else o.cv.lit(lm)
        atlas, meta = pack(objs)
        bg.save(os.path.join(d, f"bg_{tag}.png"))
        atlas.save(os.path.join(d, f"objs_{tag}.png"))
        if PREVIEW:
            os.makedirs(PREVIEW, exist_ok=True)
            preview(sc, meta, atlas, bg, os.path.join(PREVIEW, f"{key}-{tag}-3x.png"))
        res[tag] = (sc, meta)
    sc, meta = res["day"]
    room = {"key": key, "size": [sc.W, sc.H], "objects": meta, "anchors": sc.anchors}
    with open(os.path.join(d, "room.json"), "w", encoding="utf-8") as f:
        json.dump(room, f, ensure_ascii=False, indent=1)
    return room


def build_overview(out):
    d = os.path.join(out, "overview")
    os.makedirs(d, exist_ok=True)
    for tag in ("day", "night", "lit"):
        night = tag != "day"
        cv, cells = OV.build(night)
        img = cv.night() if tag == "night" else cv.lit() if tag == "lit" else cv
        img.save(os.path.join(d, f"house_{tag}.png"))
        if PREVIEW:
            pv = Canvas(img.w, img.h)
            pv.blit(img)
            for i, a in enumerate(ACTORS[:6]):
                c = cells[[0, 1, 2, 5, 6, 10][i]]
                pv.blit(CH.sprite(a), c["x"] + c["spots"][2] - 16, c["floor"] - 47)
            os.makedirs(PREVIEW, exist_ok=True)
            pv.scaled(2).save(os.path.join(PREVIEW, f"overview-{tag}-2x.png"))
    with open(os.path.join(d, "cells.json"), "w", encoding="utf-8") as f:
        json.dump({"size": [cv.w, cv.h], "cells": cells}, f, ensure_ascii=False, indent=1)


def main():
    global PREVIEW
    args = sys.argv[1:]
    want_preview = "--preview" in args
    args = [a for a in args if a != "--preview"]
    out = args[0]
    PREVIEW = os.environ.get("PIXHOUSE_PREVIEW", os.path.join(out, "preview") if want_preview else "")
    keys = args[1:] or list(R.ROOMS)
    load_chars(out)
    if keys == ["overview"]:
        build_overview(out)
        print("built overview")
        return
    build_overview(out)
    for k in keys:
        build_room(k, out)
        print("built", k)


if __name__ == "__main__":
    main()
