"""Tiny pure-Python PNG writer/reader (8-bit RGBA out; RGBA/RGB/gray/palette in)."""
import struct
import zlib


def write_png(path, w, h, px):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = px[y * w:(y + 1) * w]
        for c in row:
            raw.extend(c if c else (0, 0, 0, 0))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    i = 8
    idat = b""
    plte = trns = None
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        t = data[i + 4:i + 8]
        d = data[i + 8:i + 8 + n]
        i += 12 + n
        if t == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", d)
            assert il == 0
        elif t == b"PLTE":
            plte = [tuple(d[j:j + 3]) for j in range(0, len(d), 3)]
        elif t == b"tRNS":
            trns = d
        elif t == b"IDAT":
            idat += d
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    bits = bd * ch
    stride = (w * bits + 7) // 8
    bpp = max(1, bits // 8)
    out = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 255
        prev = line
        for x in range(w):
            if bd == 8:
                s = line[x * ch:(x + 1) * ch]
            else:
                bit = x * bd
                v = (line[bit // 8] >> (8 - bd - bit % 8)) & ((1 << bd) - 1)
                s = [v]
            if ct == 6:
                px = tuple(s)
            elif ct == 2:
                px = tuple(s) + (255,)
            elif ct == 0:
                px = (s[0], s[0], s[0], 255)
            elif ct == 4:
                px = (s[0], s[0], s[0], s[1])
            else:
                k = s[0]
                a = trns[k] if trns and k < len(trns) else 255
                px = plte[k] + (a,)
            out.append(px if px[3] else None)
    return w, h, out
