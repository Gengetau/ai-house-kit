"""Isometric pixel-art engine for the AI小屋 rooms.

World units: 1 floor tile = 16 units. Screen: x = ox + X - Y, y = oy + (X+Y)/2 - Z.
Visible box faces: top, left (plane Y=y+d) and right (plane X=x+w).
"""
import math
import random

from png import write_png


def C(h, a=255):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def mix(c1, c2, t):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3)) + (255,)


def shade(c, k):
    """k<0 darker (towards a cool ink), k>0 lighter (towards warm white)."""
    if k < 0:
        return mix(c, (40, 30, 52, 255), -k)
    return mix(c, (255, 246, 226, 255), k)


INK = C("2a2035")


def mat(base, line=None, top=0.18, left=0.0, right=-0.22):
    b = C(base) if isinstance(base, str) else base
    return {"top": shade(b, top), "left": shade(b, left), "right": shade(b, right),
            "base": b, "line": line or INK}


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.p = [None] * (w * h)
        self.glow = [False] * (w * h)

    def get(self, x, y):
        if 0 <= x < self.w and 0 <= y < self.h:
            return self.p[y * self.w + x]
        return None

    def set(self, x, y, c, glow=False):
        if c is None or not (0 <= x < self.w and 0 <= y < self.h):
            return
        if len(c) == 3:
            c = c + (255,)
        i = y * self.w + x
        if c[3] < 255 and self.p[i]:
            c = mix(self.p[i], c, c[3] / 255)
        self.p[i] = c
        self.glow[i] = glow

    def rect(self, x, y, w, h, c, glow=False):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set(xx, yy, c, glow)

    def blit(self, o, dx=0, dy=0):
        for y in range(o.h):
            for x in range(o.w):
                c = o.p[y * o.w + x]
                if c:
                    self.set(x + dx, y + dy, c, o.glow[y * o.w + x])

    def outlined(self, color=INK):
        out = Canvas(self.w, self.h)
        out.p = list(self.p)
        out.glow = list(self.glow)
        for y in range(self.h):
            for x in range(self.w):
                if self.p[y * self.w + x]:
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if self.get(x + dx, y + dy):
                        out.p[y * self.w + x] = color
                        break
        return out

    def bbox(self):
        xs, ys = [], []
        for y in range(self.h):
            for x in range(self.w):
                if self.p[y * self.w + x]:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return min(xs), min(ys), max(xs) + 1, max(ys) + 1

    def crop(self, x0, y0, x1, y1):
        o = Canvas(x1 - x0, y1 - y0)
        for y in range(y0, y1):
            for x in range(x0, x1):
                o.p[(y - y0) * o.w + (x - x0)] = self.get(x, y)
                if 0 <= x < self.w and 0 <= y < self.h:
                    o.glow[(y - y0) * o.w + (x - x0)] = self.glow[y * self.w + x]
        return o

    def night(self, light=None):
        """Night variant: darken + blue except glowing pixels; light (0..1 per pixel) warms what lamps reach."""
        o = Canvas(self.w, self.h)
        for i, c in enumerate(self.p):
            if not c:
                continue
            if self.glow[i]:
                o.p[i] = c
            else:
                d = (int(c[0] * 0.42 + 10), int(c[1] * 0.45 + 14), int(c[2] * 0.58 + 34), c[3])
                if light is not None and light[i] > 0.02:
                    k = min(1.0, light[i])
                    warm = (min(255, int(c[0] * 0.95 + 18)), int(c[1] * 0.82 + 8), int(c[2] * 0.6 + 4))
                    d = tuple(int(d[j] + (warm[j] - d[j]) * k) for j in range(3)) + (c[3],)
                o.p[i] = tuple(min(255, v) for v in d[:3]) + (c[3],)
            o.glow[i] = self.glow[i]
        return o

    def lit(self, light=None):
        """Night with the room lights on: slightly dimmer and warmer than day, brighter still near lamps."""
        o = Canvas(self.w, self.h)
        for i, c in enumerate(self.p):
            if not c:
                continue
            if self.glow[i]:
                o.p[i] = c
            else:
                k = 0.0 if light is None else min(1.0, light[i])
                f = 0.86 + 0.12 * k
                d = (c[0] * f + 6 + 10 * k, c[1] * (f - 0.03) + 3 + 4 * k, c[2] * (f - 0.1) + 2)
                o.p[i] = tuple(max(0, min(255, int(v))) for v in d) + (c[3],)
            o.glow[i] = self.glow[i]
        return o

    def scaled(self, k):
        o = Canvas(self.w * k, self.h * k)
        for y in range(o.h):
            for x in range(o.w):
                o.p[y * o.w + x] = self.p[(y // k) * self.w + x // k]
        return o

    def save(self, path):
        write_png(path, self.w, self.h, self.p)


def light_map(layers, w, h, radius=22):
    """Soft light from every glowing pixel of the given canvases (box blur via an integral image)."""
    m = [0.0] * (w * h)
    for cv in layers:
        for i, g in enumerate(cv.glow):
            c = cv.p[i]
            if g and c and c[0] > 170 and c[0] > c[2] + 50:  # warm lamp light only, not night windows/screens
                m[i] = 1.0
    integ = [[0.0] * (w + 1) for _ in range(h + 1)]
    for y in range(h):
        row = 0.0
        for x in range(w):
            row += m[y * w + x]
            integ[y + 1][x + 1] = integ[y][x + 1] + row
    out = [0.0] * (w * h)
    area = (2 * radius + 1) ** 2
    for y in range(h):
        y0, y1 = max(0, y - radius), min(h, y + radius + 1)
        for x in range(w):
            x0, x1 = max(0, x - radius), min(w, x + radius + 1)
            s = integ[y1][x1] - integ[y0][x1] - integ[y1][x0] + integ[y0][x0]
            out[y * w + x] = min(1.0, s / area * 9.0)
    return out


class Iso:
    def __init__(self, ox, oy):
        self.ox, self.oy = ox, oy

    def P(self, X, Y, Z=0):
        return (self.ox + X - Y, self.oy + (X + Y) / 2.0 - Z)


def fill_para(cv, O, A, B, color, s0=0.0, s1=1.0, t0=0.0, t1=1.0, glow=False):
    """Fill the part [s0,s1]x[t0,t1] of parallelogram O + sA + tB.
    color: tuple or fn(s, t) -> tuple|None."""
    det = A[0] * B[1] - A[1] * B[0]
    if abs(det) < 1e-9:
        return
    pts = [(O[0] + s * A[0] + t * B[0], O[1] + s * A[1] + t * B[1])
           for s in (s0, s1) for t in (t0, t1)]
    xa = int(math.floor(min(p[0] for p in pts))) - 1
    xb = int(math.ceil(max(p[0] for p in pts))) + 1
    ya = int(math.floor(min(p[1] for p in pts))) - 1
    yb = int(math.ceil(max(p[1] for p in pts))) + 1
    e = 1e-6
    for py in range(ya, yb):
        for px in range(xa, xb):
            dx, dy = px + 0.5 - O[0], py + 0.25 - O[1]
            s = (dx * B[1] - dy * B[0]) / det
            t = (A[0] * dy - A[1] * dx) / det
            if s0 - e <= s < s1 - e and t0 - e <= t < t1 - e:
                c = color(s, t) if callable(color) else color
                cv.set(px, py, c, glow)


class Box:
    def __init__(self, iso, x, y, z, w, d, h):
        self.iso, self.x, self.y, self.z, self.w, self.d, self.h = iso, x, y, z, w, d, h

    def face(self, which):
        P, x, y, z, w, d, h = self.iso.P, self.x, self.y, self.z, self.w, self.d, self.h
        if which == "top":
            O = P(x, y, z + h)
            A = _sub(P(x + w, y, z + h), O)
            B = _sub(P(x, y + d, z + h), O)
        elif which == "left":   # plane Y = y+d; s along X, t up
            O = P(x, y + d, z)
            A = _sub(P(x + w, y + d, z), O)
            B = _sub(P(x, y + d, z + h), O)
        else:                   # right: plane X = x+w; s along Y, t up
            O = P(x + w, y, z)
            A = _sub(P(x + w, y + d, z), O)
            B = _sub(P(x + w, y, z + h), O)
        return O, A, B

    def draw(self, cv, m, faces=("left", "right", "top"), glow=False):
        for f in faces:
            O, A, B = self.face(f)
            fill_para(cv, O, A, B, m[f], glow=glow)
        return self

    def paint(self, cv, which, u, v, uw, vh, color, glow=False):
        """Paint a rect on a face in world units (u along face s-axis, v along t-axis)."""
        O, A, B = self.face(which)
        su = {"top": self.w, "left": self.w, "right": self.d}[which]
        sv = {"top": self.d, "left": self.h, "right": self.h}[which]
        fill_para(cv, O, A, B, color, u / su, (u + uw) / su, v / sv, (v + vh) / sv, glow)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def blob(cv, cx, cy, rx, ry, cols, seed=1, density=0.9, glow=False):
    """Leafy/fluffy blob. cols = (dark, mid, light)."""
    rnd = random.Random(seed)
    for y in range(int(cy - ry), int(cy + ry) + 1):
        for x in range(int(cx - rx), int(cx + rx) + 1):
            nx, ny = (x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry
            r = nx * nx + ny * ny
            if r > 1:
                continue
            if r > 0.6 and rnd.random() > density:
                continue
            k = nx * 0.6 + ny * 0.8 + rnd.uniform(-0.35, 0.35)
            c = cols[2] if k < -0.35 else cols[1] if k < 0.35 else cols[0]
            cv.set(x, y, c, glow)


class Obj:
    """A furniture sprite: its own layer, outlined, with a depth for sorting."""

    def __init__(self, scene, oid, label, depth, fg=True):
        self.scene, self.id, self.label, self.depth = scene, oid, label, depth
        self.cv = Canvas(scene.W, scene.H)

    def box(self, x, y, z, w, d, h, m, faces=("left", "right", "top"), glow=False):
        return Box(self.scene.iso, x, y, z, w, d, h).draw(self.cv, m, faces, glow)


class Scene:
    def __init__(self, key, W, H, ox, oy):
        self.key, self.W, self.H = key, W, H
        self.iso = Iso(ox, oy)
        self.bg = Canvas(W, H)
        self.objs = []
        self.anchors = []

    def obj(self, oid, label, depth):
        o = Obj(self, oid, label, depth)
        self.objs.append(o)
        return o

    def anchor(self, aid, X, Y, Z, pose, acts, on=None):
        sx, sy = self.iso.P(X, Y, Z)
        depth = X + Y + 0.5
        if on:
            depth = next(o.depth for o in self.objs if o.id == on) + 0.5
        self.anchors.append({"id": aid, "x": int(round(sx)), "y": int(round(sy)),
                             "depth": round(depth, 2), "pose": pose, "activities": acts})
