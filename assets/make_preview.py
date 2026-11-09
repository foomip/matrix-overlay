#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""Render assets/screenshot.png with the app's *own* renderer, headless.

Why generate it: a store listing needs a screenshot, and a hand-made mock would
be a lie about what the overlay looks like. This runs the same Cairo painter and
the same world simulation `--self-test` exercises, so the asset is what the app
draws (the GL path rasterises the identical cell batches on GPU). No compositor,
no display, nothing on anyone's screen.

    /usr/bin/python3 assets/make_preview.py [--frames N] [--out PATH]

Deterministic: the seed is fixed, the font is the config default, and the cell
geometry comes from config.py, so the asset only changes when the look changes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cairo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from matrix_overlay import cli  # noqa: E402
from matrix_overlay.atlas import AtlasKey, GlyphAtlas  # noqa: E402
from matrix_overlay.config import load_default, replace_config  # noqa: E402
from matrix_overlay.render import Renderer  # noqa: E402
from matrix_overlay.surface import MonitorInfo  # noqa: E402
from matrix_overlay.world import World, WorldCanvas  # noqa: E402

GEOMS = [(1600, 900, 1)]        # one synthetic output, store-friendly ratio
SEED = 20261109                 # fixed, so regenerating is a no-op diff


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=200)
    ap.add_argument("--out", default=str(REPO / "assets/screenshot.png"))
    ns = ap.parse_args(argv[1:])

    cfg = replace_config(load_default(), **{"world.seed": SEED, "outputs.mode": "all"})
    infos = [MonitorInfo(index=i, model="preview", x=0, y=0, width=w, height=h,
                         scale=scale)
             for i, (w, h, scale) in enumerate(GEOMS)]
    canvas = WorldCanvas.from_geoms([m.geom() for m in infos],
                                    cell_size=cfg.world.cell_size)
    key = AtlasKey(font=cfg.look.font, cell_size=cfg.world.cell_size, scale=1,
                   render_scale=cfg.world.render_scale, glyph_set=cfg.look.glyph_set,
                   custom_glyphs=cfg.look.custom_glyphs)
    atlas = GlyphAtlas(key)
    world = World(canvas, cli._params_for(cfg, atlas))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, infos[0].width, infos[0].height)
    ctx = cairo.Context(surf)
    rend = Renderer(crop=canvas.crops[0], canvas=canvas, atlas=atlas, scale=1,
                    glow_enabled=cfg.look.glow.enabled)
    for _ in range(ns.frames):
        rend.apply(ctx, world.step().per_output[0])
    surf.flush()
    surf.write_to_png(ns.out)
    print(f"wrote {ns.out}: {infos[0].width}x{infos[0].height}, {len(rend.screen)} "
          f"lit cells, unsupported glyphs: {len(atlas.unsupported)}")
    return 0 if not atlas.unsupported else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
