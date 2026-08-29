#!/usr/bin/env python3
"""Generate the RingOut application icon (.ico) from the launcher character art.

Pure Python 3 (stdlib only): decodes the RGBA cutout shipped at
ModernGekko/assets/launcher/launcher-character.png, composites it onto a
rounded-square brand tile (the launcher's deep navy -> blue accent), resamples
to the Windows icon size set, and writes a multi-resolution .ico plus a 256px
PNG preview.

Usage:
    python3 tools/make_ringout_icon.py \
        --art ModernGekko/assets/launcher/launcher-character.png \
        --out ModernGekko/assets/ringout.ico \
        --preview /tmp/ringout-preview.png

The .ico layout follows the standard Windows container:

  * ICONDIR (6 bytes) + one ICONDIRENTRY (16 bytes) per image.
  * Sizes <= 128: 32-bpp BMP DIB (BGRA bottom-up XOR + 1-bpp AND mask).
  * Size 256:     PNG-compressed entry (required for 256 in the classic format).

The single resource group produced here is deliberately the FIRST (and only)
icon group in the binaries that embed it, so SDL3's Win32 class registration
(EnumResourceNames fallback) picks it up for the launcher window with no extra
launcher code, and DolphinNoGUI's PlatformWin32 loads it for the game window.
"""

import argparse
import io
import struct
import sys
import zlib
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Minimal PNG decoder (8-bit RGBA / RGB / grayscale / palette)
# ---------------------------------------------------------------------------


def read_png(path: str) -> Tuple[int, int, List[bytes]]:
    """Decode a PNG into (width, height, rows) where each row is RGBA bytes."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")

    pos = 8
    width = height = bit_depth = color_type = 0
    palette: List[bytes] = []
    idat = bytearray()

    def chunk():
        nonlocal pos
        if pos + 8 > len(data):
            raise ValueError("truncated PNG")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        return ctype, body

    while pos < len(data):
        ctype, body = chunk()
        if ctype == b"IHDR":
            (width, height, bit_depth, color_type, _, _, _) = struct.unpack(
                ">IIBBBBB", body)
            if bit_depth != 8:
                raise ValueError(f"unsupported bit depth {bit_depth}")
        elif ctype == b"PLTE":
            palette = [body[i:i + 3] for i in range(0, len(body), 3)]
        elif ctype == b"tRNS" and color_type == 3:
            for i, alpha in enumerate(body):
                if i < len(palette):
                    palette[i] = palette[i] + bytes([alpha])
        elif ctype == b"IDAT":
            idat.extend(body)
        elif ctype == b"IEND":
            break

    raw = zlib.decompress(bytes(idat))

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bpp = channels
    stride = width * channels
    if len(raw) < (height * (stride + 1)):
        raise ValueError("PNG IDAT too short")

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        if pb <= pc:
            return b
        return c

    rows: List[bytes] = []
    prev = bytearray(width * channels)
    for y in range(height):
        ft = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ft == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ft == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ft == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif ft == 4:
                line[x] = (line[x] + paeth(a, b, c)) & 0xFF
        prev = line

        rgba = bytearray(width * 4)
        for x in range(width):
            if color_type == 6:          # RGBA
                rgba[4 * x:4 * x + 4] = line[4 * x:4 * x + 4]
            elif color_type == 2:        # RGB
                rgba[4 * x:4 * x + 3] = line[3 * x:3 * x + 3]
                rgba[4 * x + 3] = 0xFF
            elif color_type == 0:        # grayscale
                rgba[4 * x:4 * x + 3] = bytes([line[x]]) * 3
                rgba[4 * x + 3] = 0xFF
            elif color_type == 4:        # gray + alpha
                rgba[4 * x] = rgba[4 * x + 1] = rgba[4 * x + 2] = line[2 * x]
                rgba[4 * x + 3] = line[2 * x + 1]
            elif color_type == 3:        # palette
                idx = line[x]
                entry = palette[idx] if idx < len(palette) else b"\0\0\0\xFF"
                rgba[4 * x:4 * x + 4] = entry.ljust(4, b"\xFF")
        rows.append(bytes(rgba))
    return width, height, rows
# ---------------------------------------------------------------------------
# RGBA compositing / resampling (premultiplied math avoids dark fringes)
# ---------------------------------------------------------------------------

def premultiply(rows: List[bytes]) -> List[bytearray]:
    out = []
    for row in rows:
        p = bytearray(len(row))
        for i in range(0, len(row), 4):
            r, g, b, a = row[i], row[i + 1], row[i + 2], row[i + 3]
            scale = a / 255.0
            p[i] = round(r * scale)
            p[i + 1] = round(g * scale)
            p[i + 2] = round(b * scale)
            p[i + 3] = a
        out.append(p)
    return out


def unpremultiply(rows: List[bytearray]) -> List[bytes]:
    out = []
    for row in rows:
        p = bytearray(len(row))
        for i in range(0, len(row), 4):
            r, g, b, a = row[i], row[i + 1], row[i + 2], row[i + 3]
            if a > 0:
                scale = 255.0 / a
                p[i] = min(255, round(r * scale))
                p[i + 1] = min(255, round(g * scale))
                p[i + 2] = min(255, round(b * scale))
            p[i + 3] = a
        out.append(bytes(p))
    return out


def _lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def bilinear_resize(src: List[bytearray], sw: int, sh: int, tw: int,
                    th: int) -> List[bytearray]:
    out = []
    x_ratio = sw / tw
    y_ratio = sh / th
    for y in range(th):
        sy = y * y_ratio
        y0 = int(sy)
        y1 = min(y0 + 1, sh - 1)
        fy = sy - y0
        row = bytearray(tw * 4)
        for x in range(tw):
            sx = x * x_ratio
            x0 = int(sx)
            x1 = min(x0 + 1, sw - 1)
            fx = sx - x0
            for c in range(4):
                p00 = src[y0][4 * x0 + c]
                p10 = src[y0][4 * x1 + c]
                p01 = src[y1][4 * x0 + c]
                p11 = src[y1][4 * x1 + c]
                top = _lerp(p00, p10, fx)
                bot = _lerp(p01, p11, fx)
                row[4 * x + c] = _lerp(top, bot, fy)
        out.append(row)
    return out


def box_resize(src: List[bytearray], sw: int, sh: int, tw: int,
               th: int) -> List[bytearray]:
    """Integer box downscale; used as a cheap pre-filter before bilinear."""
    out = []
    x_step = sw / tw
    y_step = sh / th
    inv = 1.0 / (x_step * y_step)
    for yy in range(th):
        y0 = int(yy * y_step)
        y1 = min(int((yy + 1) * y_step), sh)
        row = bytearray(tw * 4)
        for xx in range(tw):
            x0 = int(xx * x_step)
            x1 = min(int((xx + 1) * x_step), sw)
            acc = [0.0] * 4
            for sy in range(y0, y1):
                for sx in range(x0, x1):
                    base = 4 * sx
                    for c in range(4):
                        acc[c] += src[sy][base + c]
            for c in range(4):
                row[4 * xx + c] = min(255, round(acc[c] * inv))
        out.append(row)
    return out


def resize(src: List[bytearray], sw: int, sh: int, tw: int,
           th: int) -> List[bytearray]:
    # Pre-scale in integer steps when the target is much smaller, then finish
    # bilinearly. Keeps 16px icons clean without a full mip chain.
    if sw <= tw and sh <= th:
        return bilinear_resize(src, sw, sh, tw, th)
    factor = max(1, min(sw // max(1, tw * 2), sh // max(1, th * 2)))
    if factor > 1:
        iw, ih = max(tw * 2, sw // factor), max(th * 2, sh // factor)
        src = box_resize(src, sw, sh, iw, ih)
        sw, sh = iw, ih
    return bilinear_resize(src, sw, sh, tw, th)
# ---------------------------------------------------------------------------
# Brand tile composition
# ---------------------------------------------------------------------------

def make_tile(size: int, radius_fraction: float = 0.225) -> List[bytearray]:
    """Opaque rounded square with a navy->blue diagonal gradient."""
    radius = int(size * radius_fraction)
    top = (15, 32, 84)      # deep navy
    bottom = (46, 91, 214)  # restrained blue accent
    rows = []
    for y in range(size):
        row = bytearray(size * 4)
        t = y / max(1, size - 1)
        cr = _lerp(top[0], bottom[0], t)
        cg = _lerp(top[1], bottom[1], t)
        cb = _lerp(top[2], bottom[2], t)
        for x in range(size):
            inside = True
            dx = dy = 0
            if radius > 0:
                dx = max(radius - x, x - (size - 1 - radius), 0)
                dy = max(radius - y, y - (size - 1 - radius), 0)
                if dx * dx + dy * dy > radius * radius:
                    inside = False
            if inside:
                row[4 * x:4 * x + 3] = bytes([cr, cg, cb])
                row[4 * x + 3] = 0xFF
            else:
                # 1-sample feather so small sizes do not go jaggy.
                edge = radius * radius
                alpha = max(0.0, min(1.0, (edge - (dx * dx + dy * dy))))
                row[4 * x:4 * x + 3] = bytes([cr, cg, cb])
                row[4 * x + 3] = round(alpha * 255)
        rows.append(row)
    return rows


def composite(art: List[bytearray], aw: int, ah: int, tile: List[bytearray],
              size: int, fill: float = 0.84, lift: float = 0.0) -> List[bytearray]:
    """Blend premultiplied art centered on the tile; both inputs premultiplied."""
    scale = (size * fill) / max(aw, ah)
    gw, gh = max(1, round(aw * scale)), max(1, round(ah * scale))
    art_scaled = resize(art, aw, ah, gw, gh)
    ox = (size - gw) // 2
    oy = (size - gh) // 2 + round(lift * size)

    out = [bytearray(row) for row in tile]  # copy (already premultiplied)
    for yy in range(gh):
        ty = oy + yy
        if ty < 0 or ty >= size:
            continue
        for xx in range(gw):
            tx = ox + xx
            if tx < 0 or tx >= size:
                continue
            sa = art_scaled[yy][4 * xx + 3] / 255.0
            if sa <= 0:
                continue
            src = art_scaled[yy]
            dst = out[ty]
            for c in range(3):
                dst[4 * tx + c] = round(src[4 * xx + c] + dst[4 * tx + c] * (1 - sa))
            dst[4 * tx + 3] = 255
    return out


# ---------------------------------------------------------------------------
# PNG writer (for the 256px ICO entry and the preview)
# ---------------------------------------------------------------------------

def write_png(width: int, height: int, rows: List[bytes], path: str) -> None:
    def chunk(ctype: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    idat = zlib.compress(raw, 9)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) +
           chunk(b"IEND", b""))
    open(path, "wb").write(png)


# ---------------------------------------------------------------------------
# ICO writer
# ---------------------------------------------------------------------------

def bmp_entry(size: int, rows: List[bytes]) -> bytes:
    """32-bpp bottom-up XOR bitmap + 1-bpp AND mask for one ICO image."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):  # bottom-up
        for x in range(size):
            r, g, b, a = rows[y][4 * x:4 * x + 4]
            xor += bytes([b, g, r, a])  # BGRA
    and_stride = ((size + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(size - 1, -1, -1):
        line = bytearray(and_stride)
        for x in range(size):
            if rows[y][4 * x + 3] < 128:  # transparent -> mask bit 1
                line[x // 8] |= 0x80 >> (x % 8)
        and_mask += line
    return header + bytes(xor) + bytes(and_mask)


def write_ico(path: str, images: List[Tuple[int, bytes]]) -> None:
    """images: [(size, encoded_image)]. size 256 must be PNG-encoded."""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = bytearray()
    offset = 6 + 16 * len(images)
    for size, body in images:
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(body),
                               offset)
        offset += len(body)
    with open(path, "wb") as f:
        f.write(header + bytes(entries))
        for _, body in images:
            f.write(body)


# ---------------------------------------------------------------------------

def _write_png_bytes(width: int, height: int, rows: List[bytes]) -> bytes:
    def chunk(ctype: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    idat = zlib.compress(raw, 9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) +
            chunk(b"IEND", b""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--art",
                    default="ModernGekko/assets/launcher/launcher-character.png")
    ap.add_argument("--out", default="ModernGekko/assets/ringout.ico")
    ap.add_argument("--preview", default="/tmp/ringout-preview.png")
    args = ap.parse_args()

    aw, ah, art_rows = read_png(args.art)
    print(f"art: {aw}x{ah} RGBA", file=sys.stderr)

    # Decode art is straight alpha; premultiply once for compositing.
    art_pm = premultiply(art_rows)
    tile_pm = make_tile(max(aw, ah))  # tile at art resolution: clean scaling
    composed_pm = composite(art_pm, aw, ah, tile_pm, max(aw, ah))
    composed = unpremultiply(composed_pm)   # straight alpha rows
    composed_pm = premultiply(composed)     # for resampling chain below
    canvas = max(aw, ah)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images: List[Tuple[int, bytes]] = []
    for size in sizes:
        rows_pm = resize(composed_pm, canvas, canvas, size, size)
        rows = unpremultiply(rows_pm)
        if size == 256:
            images.append((size, _write_png_bytes(size, size, rows)))
        else:
            images.append((size, bmp_entry(size, rows)))

    write_ico(args.out, images)
    preview_rows = unpremultiply(
        resize(composed_pm, canvas, canvas, 256, 256))
    write_png(256, 256, preview_rows, args.preview)
    print(f"wrote {args.out} with sizes {sizes}", file=sys.stderr)
    print(f"preview: {args.preview}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())