"""Regenerate the site's raster marks from the same four-quadrant mark the inline
SVG favicon draws.

No Pillow, and no font: a PNG is a zlib stream behind a fixed header, and the
seven letters this needs are a 5x7 grid each. Adding an image library and a
font file to the deployment to draw one social card once would be a poor trade.
"""
import pathlib
import struct
import zlib

OUT = pathlib.Path("src/report/static")

BG     = (0x0A, 0x0A, 0x0A)
INK    = (0xE7, 0xEB, 0xF2)
DIM    = (0x6B, 0x71, 0x80)
BLUE   = (0x23, 0x40, 0xBE)
YELLOW = (0xF3, 0xC2, 0x18)
RED    = (0xE1, 0x36, 0x2C)
GREY   = (0xCB, 0xD3, 0xE0)

# 5 wide, 7 tall. Only the letters "TO SCALE" needs.
GLYPHS = {
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    " ": ("00000",) * 7,
}


def png(pixels, w, h):
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in pixels)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def mark(size, bg=BG):
    """The mark: a disc quartered into owns / owed / left over, on the page's
    own ground. Supersampled 3x so the circle's edge is not a staircase."""
    ss = 3
    n = size * ss
    cx = cy = n / 2.0
    r = n / 2.0
    rows = []
    for y in range(n):
        row = []
        for x in range(n):
            dx, dy = x + 0.5 - cx, y + 0.5 - cy
            if dx * dx + dy * dy > r * r:
                row.append(bg)
            elif dx >= 0 and dy < 0:
                row.append(BLUE)
            elif dx >= 0:
                row.append(YELLOW)
            elif dy >= 0:
                row.append(RED)
            else:
                row.append(GREY)
        rows.append(row)
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            acc = [0, 0, 0]
            for j in range(ss):
                for i in range(ss):
                    p = rows[y * ss + j][x * ss + i]
                    acc[0] += p[0]
                    acc[1] += p[1]
                    acc[2] += p[2]
            row.append(tuple(v // (ss * ss) for v in acc))
        out.append(row)
    return out


def blit(canvas, tile, ox, oy):
    for y, row in enumerate(tile):
        for x, px in enumerate(row):
            canvas[oy + y][ox + x] = px


def text(canvas, s, ox, oy, cell, colour, gap=1):
    """Draw `s` at `cell` pixels per grid square. Returns the width drawn."""
    x = ox
    for ch in s.upper():
        grid = GLYPHS[ch]
        for gy, line in enumerate(grid):
            for gx, on in enumerate(line):
                if on != "1":
                    continue
                for j in range(cell):
                    for i in range(cell):
                        canvas[oy + gy * cell + j][x + gx * cell + i] = colour
        x += (5 + gap) * cell
    return x - ox


def rect(canvas, x0, y0, w, h, colour):
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            canvas[y][x] = colour


def og_image(w=1200, h=630):
    """The social card: the mark, the wordmark, and a balance sheet in three
    bands. A placeholder, but a placeholder that says which site it is."""
    rows = [[BG] * w for _ in range(h)]

    # Left half: who this is.
    blit(rows, mark(150), 100, 150)
    text(rows, "TO SCALE", 100, 370, 11, INK)

    # Right half: the drawing in miniature -- one column of what is owned
    # against one of who has a claim on it, the two the same height because
    # they are the same money counted twice. The site's whole argument, at
    # thumbnail size.
    base, height = 520, 340
    col_w = 170
    for x, bands in ((740, ((140, GREY), (200, BLUE))),
                     (960, ((215, RED), (125, YELLOW)))):
        y = base - height
        for band, colour in bands:
            rect(rows, x, y, col_w, band, colour)
            y += band

    # The baseline both columns stand on.
    rect(rows, 100, base + 6, w - 200, 3, DIM)
    return png(rows, w, h)


def ico(size=32):
    """A one-image .ico wrapping a PNG. Every browser that still asks for an
    .ico has understood PNG-in-ICO since 2007."""
    data = png(mark(size), size, size)
    return (struct.pack("<HHH", 0, 1, 1)
            + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), 22)
            + data)


def main():
    (OUT / "favicon.ico").write_bytes(ico(32))
    (OUT / "apple-touch-icon.png").write_bytes(png(mark(180), 180, 180))
    (OUT / "logo.png").write_bytes(png(mark(512), 512, 512))
    (OUT / "og-image.png").write_bytes(og_image())
    for f in ("favicon.ico", "apple-touch-icon.png", "logo.png", "og-image.png"):
        print(f, (OUT / f).stat().st_size)


if __name__ == "__main__":
    main()
