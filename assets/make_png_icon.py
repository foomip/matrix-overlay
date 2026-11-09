#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""Render the icon PNGs from the same motif as the SVG, into assets/hicolor.

The rasters are *generated, not hand-tuned*, and they are committed on purpose:
a package build must not depend on a font or on cairo being present, and a
checked-in binary is reviewable by `git diff` against this script. The scalable
SVG carries the detailed version (real glyphs); the PNGs draw the same column
motif as blocks, because a 16 px column of katakana is mush on every platform.

    /usr/bin/python3 assets/make_png_icon.py [sizes...]

Palette comes from config.py so the icon cannot drift away from the app's look.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cairo  # python3-gi-cairo (a build-time-only dependency here)

ASSETS = Path(__file__).resolve().parent
ID = "tech.i2r.matrixoverlay"
DEFAULT_SIZES = (16, 24, 32, 48, 64, 128)

BG = (0.016, 0.027, 0.039)          # near-black, matches the dimmed desktop
VEIL = (0.0, 0.066, 0.02, 0.75)     # the veil laid under the rain
BODY = (0.0, 0.72, 0.12)            # #00b81f, config.py look.body_color
HEAD = (0.84, 1.0, 0.84)            # #d6ffd6, config.py look.head_color


def rounded_rect(ctx, x, y, w, h, r) -> None:
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -1.5708, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, 1.5708)
    ctx.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    ctx.arc(x + r, y + r, r, 3.1416, 4.7124)
    ctx.close_path()


def column(ctx, cx: float, top: float, cell: float, rows: int, head: int,
           ramp: float) -> None:
    """One rain column: a fading tail of glyph blocks with a lit head."""
    for row in range(rows):
        y = top + row * cell
        if row == head:
            # the glow the app draws around a head cell (look.glow)
            halo = cell * 1.45
            ctx.set_source_rgba(*HEAD, 0.22)
            ctx.rectangle(round(cx - halo / 2), round(y - cell * 0.34),
                          round(halo), round(halo))
            ctx.fill()
            ctx.set_source_rgba(*HEAD, 1.0)
        else:
            # distance below the head dims the tail; above it the tail is dimmer
            dist = abs(row - head)
            alpha = max(0.10, ramp / (1.0 + dist * 1.6))
            ctx.set_source_rgba(*BODY, alpha)
        w = cell * 0.72
        ctx.rectangle(round(cx - w / 2), round(y), round(w), round(cell * 0.78))
        ctx.fill()


def render(size: int, path: Path) -> None:
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surf)
    r = max(2.0, size * 0.11)
    rounded_rect(ctx, 0, 0, size, size, r)
    ctx.set_source_rgba(*BG, 1.0)
    ctx.fill()
    rounded_rect(ctx, 0, 0, size, size, r)
    ctx.set_source_rgba(*VEIL)
    ctx.fill()

    cell = size / 8.0
    # three columns, different head heights: the middle one runs longest
    column(ctx, size * 0.27, size * 0.10, cell, rows=6, head=5, ramp=0.9)
    column(ctx, size * 0.50, size * 0.06, cell, rows=7, head=6, ramp=1.0)
    column(ctx, size * 0.73, size * 0.14, cell, rows=5, head=2, ramp=0.8)
    surf.write_to_png(str(path))


def main(argv: list[str]) -> int:
    sizes = [int(a) for a in argv[1:]] or list(DEFAULT_SIZES)
    made = []
    for size in sizes:
        out = ASSETS / "hicolor" / f"{size}x{size}" / "apps" / f"{ID}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        render(size, out)
        made.append(out)
    for path in made:
        print(f"wrote {path.relative_to(ASSETS)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
