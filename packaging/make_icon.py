"""Draw the app icon and write `app/web/favicon.ico` (used by the browser tab and the exe).

Pure stdlib: a small PNG encoder plus an ICO wrapper, so regenerating the icon needs no
image library. Run from anywhere:

    python packaging/make_icon.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 256
SUPERSAMPLE = 4
OUT = Path(__file__).resolve().parent.parent / "app" / "web" / "favicon.ico"

PINK_TOP = (255, 111, 174)
PINK_BOTTOM = (122, 43, 86)
INK = (26, 8, 17)


def _rounded_alpha(x: float, y: float, size: float, radius: float) -> float:
    """Coverage of a rounded square at (x, y) — 1 inside, 0 outside, soft at the corner."""
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    return 1.0 if distance <= radius - 0.5 else (0.0 if distance >= radius + 0.5 else radius + 0.5 - distance)


def _ring_alpha(x: float, y: float, cx: float, cy: float, outer: float, inner: float) -> float:
    distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    if distance > outer + 0.5 or distance < inner - 0.5:
        return 0.0
    if distance >= outer - 0.5 or distance <= inner + 0.5:
        # soft edge on either side of the ring
        outer_soft = max(0.0, min(1.0, outer + 0.5 - distance))
        inner_soft = max(0.0, min(1.0, distance - inner + 0.5))
        return min(outer_soft, inner_soft)
    return 1.0


def _bar_alpha(x: float, y: float, x0: float, y0: float, x1: float, y1: float, radius: float) -> float:
    cx = min(max(x, x0 + radius), x1 - radius)
    cy = min(max(y, y0 + radius), y1 - radius)
    if x0 - 0.5 <= x <= x1 + 0.5 and y0 - 0.5 <= y <= y1 + 0.5:
        distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        if distance <= radius - 0.5:
            return 1.0
        if distance >= radius + 0.5:
            return 0.0
        return radius + 0.5 - distance
    return 0.0


def render() -> list[list[tuple[int, int, int, int]]]:
    """A supersampled RGBA raster of the icon."""
    big = SIZE * SUPERSAMPLE
    rows: list[list[tuple[int, int, int, int]]] = []
    corner = 0.24 * big
    ring_cx, ring_cy = 0.375 * big, 0.52 * big
    ring_outer, ring_inner = 0.175 * big, 0.082 * big
    bar_x0, bar_x1 = 0.635 * big, 0.735 * big
    bar_y0, bar_y1 = 0.30 * big, 0.60 * big
    dot_cx = (bar_x0 + bar_x1) / 2
    dot_cy, dot_r = 0.705 * big, 0.052 * big

    for py in range(big):
        row: list[tuple[int, int, int, int]] = []
        y = py + 0.5
        blend = py / (big - 1)
        base = tuple(round(PINK_TOP[i] + (PINK_BOTTOM[i] - PINK_TOP[i]) * blend) for i in range(3))
        for px in range(big):
            x = px + 0.5
            alpha = _rounded_alpha(x, y, big, corner)
            if alpha <= 0:
                row.append((0, 0, 0, 0))
                continue
            r, g, b = base
            to_dot = ((x - dot_cx) ** 2 + (y - dot_cy) ** 2) ** 0.5
            mark = max(
                _ring_alpha(x, y, ring_cx, ring_cy, ring_outer, ring_inner),
                _bar_alpha(x, y, bar_x0, bar_y0, bar_x1, bar_y1, (bar_x1 - bar_x0) / 2),
                max(0.0, min(1.0, dot_r + 0.5 - to_dot)),
            )
            if mark > 0:
                r = round(r + (INK[0] - r) * mark)
                g = round(g + (INK[1] - g) * mark)
                b = round(b + (INK[2] - b) * mark)
            row.append((r, g, b, round(255 * alpha)))
        rows.append(row)
    return rows


def downsample(rows: list[list[tuple[int, int, int, int]]]) -> list[list[tuple[int, int, int, int]]]:
    """Box-filter the supersampled raster down to SIZE×SIZE."""
    out: list[list[tuple[int, int, int, int]]] = []
    n = SUPERSAMPLE * SUPERSAMPLE
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            r = g = b = a = 0
            for dy in range(SUPERSAMPLE):
                for dx in range(SUPERSAMPLE):
                    pr, pg, pb, pa = rows[y * SUPERSAMPLE + dy][x * SUPERSAMPLE + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            row.append((r // a, g // a, b // a, a // n) if a else (0, 0, 0, 0))
        out.append(row)
    return out


def png_bytes(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    raw = b"".join(b"\x00" + b"".join(struct.pack("4B", *px) for px in row) for row in rows)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico_bytes(png: bytes) -> bytes:
    """A one-image ICO carrying a PNG (supported by Windows Vista and later)."""
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> int:
    png = png_bytes(downsample(render()))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(ico_bytes(png))
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
