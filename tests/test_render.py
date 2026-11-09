# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""M3b RED: dirty-cell Cairo compositor.

The contracts under test (plan C4, D1.6, T5):
  * a draw clears its own cell first - Cairo OVER is not idempotent for alpha<1
  * the veil is CSS-only: no full-surface pass, ever (op-count bound)
  * every op is clip-safe, with a pending backlog so clipped work is not lost
  * geometry is device pixels; damage is reported in logical pixels
"""
from __future__ import annotations

import cairo
import pytest

from matrix_overlay.atlas import AtlasKey, GlyphAtlas
from matrix_overlay.render import Renderer
from matrix_overlay.world import BUCKETS, Crop, Draw, Geom, OutputFrame, Pos, World, \
    WorldCanvas, Params

CELL = 18


class CrSpy:
    """Delegating context: real pixels are drawn, ops and their args are recorded."""

    def __init__(self, real):
        self.__dict__["_real"] = real
        self.__dict__["ops"] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if callable(attr):
            def call(*a):
                self.ops.append((name, a))
                return attr(*a)
            return call
        return attr

    @property
    def names(self):
        return [n for n, _a in self.ops]

    @property
    def paints(self):
        return sum(1 for n in self.names if n in ("paint", "paint_with_alpha", "mask", "fill"))

    def operators(self):
        return [a[0] for n, a in self.ops if n == "set_operator"]


def surface(w=512, h=128):
    return cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)


def atlas(glow=False, scale=1):
    return GlyphAtlas(AtlasKey(font="DejaVu Sans Mono", cell_size=CELL, scale=scale,
                               glow_enabled=glow))


def crop(scale=1):
    cv = WorldCanvas.from_geoms((Geom("DP-5", 0, 0, 2560, 1440),
                                 Geom("DP-4", 2560, 0, 2560, 1440)), cell_size=CELL)
    return cv, cv.crops[0]


def renderer(*, glow=False, scale=1, w=512, h=128, max_pending=4096):
    cv, cr0 = crop(scale)
    atlas_obj = atlas(glow=glow, scale=scale)
    rend = Renderer(crop=cr0, canvas=cv, atlas=atlas_obj, scale=scale,
                    glow_enabled=glow, max_pending=max_pending)
    surf = surface(w, h)
    ctx = cairo.Context(surf)
    return rend, surf, ctx


def draw(gcol=1, row=2, glyph=3, brightness=1.0, head=True):
    return Draw(col=gcol, row=row, global_col=gcol, glyph=glyph, brightness=brightness,
                head=head)


def alpha_at(surf, x, y):
    surf.flush()
    buf = bytes(memoryview(surf.get_data()))
    stride = surf.get_stride()
    return buf[y * stride + x * 4 + 3]


def rect_alpha_box(surf, x, y, w, h):
    return [alpha_at(surf, px, py) for py in range(y, y + h) for px in range(x, x + w)]


# ------------------------------------------------------------ basic blit ---


def test_draw_blits_at_the_cells_rect():
    rend, surf, ctx = renderer()
    rend.apply(CrSpy(ctx), OutputFrame("DP-5", draw=(draw(gcol=1, row=2),), clear=()))
    assert max(rect_alpha_box(surf, CELL, 2 * CELL, CELL, CELL)) > 0
    assert alpha_at(surf, 0, 0) == 0, "neighbouring cell must be untouched"


def test_draw_erases_previous_content_instead_of_blending_onto_it():
    """Invariant 1: a stale glyph under a fresh dim one reads too bright, so the
    cell is cleared first - the result must equal a blit onto an empty surface."""
    frame = OutputFrame("DP-5", draw=(draw(gcol=1, row=2, head=False,
                                           brightness=1.0 / (BUCKETS - 1)),), clear=())
    rend, surf, ctx = renderer()
    ctx.set_source_rgb(0, 1, 0)
    ctx.rectangle(CELL, 2 * CELL, CELL, CELL)
    ctx.fill()
    rend.apply(CrSpy(ctx), frame)
    dirty = max(rect_alpha_box(surf, CELL, 2 * CELL, CELL, CELL))
    clean_rend, clean_surf, clean_ctx = renderer()
    clean_rend.apply(CrSpy(clean_ctx), frame)
    pristine = max(rect_alpha_box(clean_surf, CELL, 2 * CELL, CELL, CELL))
    assert pristine > 0
    assert dirty == pristine, f"content blended onto the old glyph: {dirty} vs {pristine}"


def test_repeated_apply_of_the_same_frame_is_idempotent():
    """Cairo OVER accumulates alpha: without a per-cell clear a churned glyph
    would get brighter every frame it was redrawn."""
    rend, surf, ctx = renderer()
    frame = OutputFrame("DP-5", draw=(draw(gcol=0, row=0, brightness=1.0 / (BUCKETS - 1),
                                            head=False),),
                        clear=())
    once = CrSpy(ctx)
    rend.apply(once, frame)
    peak_once = max(rect_alpha_box(surf, 0, 0, CELL, CELL))
    for _ in range(5):
        rend.apply(CrSpy(ctx), frame)
    peak_many = max(rect_alpha_box(surf, 0, 0, CELL, CELL))
    assert peak_once > 0
    assert abs(peak_many - peak_once) <= 1, f"alpha accumulated: {peak_once} -> {peak_many}"


def test_op_count_is_bounded_by_cells_not_pixels():
    rend, surf, ctx = renderer()
    cells = 20
    frame = OutputFrame("DP-5", draw=tuple(draw(gcol=i % 4, row=i // 4, brightness=0.5)
                                           for i in range(cells)),
                        clear=())
    spy = CrSpy(ctx)
    rend.apply(spy, frame)
    assert spy.paints <= 2 * cells, f"{spy.paints} ops for {cells} cells: a full-frame pass"


def test_clear_blanks_only_the_named_cell():
    rend, surf, ctx = renderer()
    ctx.set_source_rgb(0, 1, 0)
    ctx.rectangle(0, 0, CELL * 2, CELL)
    ctx.fill()
    rend.apply(CrSpy(ctx), OutputFrame("DP-5", draw=(), clear=(Pos(0, 0),)))
    assert max(rect_alpha_box(surf, 0, 0, CELL, CELL)) == 0
    assert alpha_at(surf, CELL + 2, 2) == 255


def test_renderer_never_paints_a_background():
    """Dim lives in window CSS (C4). Any full-surface op here is the 2.65 GB/s
    path from plan 1.4, so bound the ops and check the untouched rows."""
    rend, surf, ctx = renderer(w=CELL * 8, h=CELL * 8)
    spy = CrSpy(ctx)
    rend.apply(spy, OutputFrame("DP-5", draw=tuple(draw(gcol=i, row=1) for i in range(8)),
                               clear=()))
    assert spy.paints <= 2 * 8
    assert max(rect_alpha_box(surf, 0, 5 * CELL, CELL * 8, CELL)) == 0, \
        "the renderer painted the veil"


# --------------------------------------------------------------- geometry ---


def test_plan_returns_logical_rects_and_apply_paints_them():
    """Damage is *planned* from the surface's queue() (before the draw) and painted
    by apply(); plan/apply must agree on the rect for the same cell."""
    rend, surf, ctx = renderer(scale=2, w=1024, h=256)
    frame = OutputFrame("DP-5", draw=(draw(gcol=3, row=1),), clear=())
    assert rend.plan(frame) == [(3 * CELL, 1 * CELL, CELL, CELL)]
    assert rend.apply(CrSpy(ctx), frame) is None
    assert max(rect_alpha_box(surf, 3 * CELL, 1 * CELL, CELL, CELL)) > 0


def test_merge_rows_collapses_a_row_into_one_rect():
    """~1600 per-cell damage rects/frame measured ~10 fps live; per-row bands fix it."""
    from matrix_overlay.render import merge_rows

    cells = [(c * CELL, 2 * CELL, CELL, CELL) for c in range(40)]
    merged = merge_rows(cells)
    assert len(merged) == 1
    assert merged[0] == (0, 2 * CELL, 40 * CELL, CELL)
    two_rows = merge_rows(cells + [(0, 3 * CELL, CELL, CELL), (5 * CELL, 3 * CELL, CELL, CELL)])
    assert len(two_rows) == 2
    assert merge_rows([]) == []


def test_plan_covers_cleared_and_drawn_cells():
    rend, _surf, _ctx = renderer()
    frame = OutputFrame("DP-5", draw=(draw(gcol=1, row=1, head=False),),
                        clear=(Pos(2, 2), Pos(1, 1)))
    rects = rend.plan(frame)
    assert len(rects) == 2, "a cell both cleared and redrawn is reported once"
    assert {(r[0], r[1]) for r in rects} == {(CELL, CELL), (2 * CELL, 2 * CELL)}


def test_plan_widens_head_rects_for_the_bloom():
    tight = renderer()[0].plan(
        OutputFrame("DP-5", draw=(draw(gcol=1, row=1, head=True),), clear=()))
    glowy = renderer(glow=True)[0].plan(
        OutputFrame("DP-5", draw=(draw(gcol=1, row=1, head=True),), clear=()))
    assert tight == [(CELL, CELL, CELL, CELL)]
    x, y, w, h = glowy[0]
    assert (w, h) == (CELL + 12, CELL + 12) and (x, y) == (CELL - 6, CELL - 6)


def test_atlas_is_highres_for_the_monitor_scale_but_draws_in_logical_space():
    """D2: DP-4 reports scale=2. The atlas bakes a 36px cell, the pattern matrix
    shrinks it by 1/2 into an 18px *logical* rect - crisp glyphs, and the renderer
    never thinks in device pixels because GTK3 already scaled the draw context."""
    seen = []
    rend, surf, ctx = renderer(scale=2, w=1024, h=256)
    frame = OutputFrame("DP-5", draw=(draw(gcol=2, row=1),), clear=())
    seen.extend(rend.plan(frame))
    rend.apply(CrSpy(ctx), frame)
    assert rend.atlas.cell_px == 36
    assert rend.blit_scale == pytest.approx(0.5)
    assert seen[0] == (2 * CELL, 1 * CELL, CELL, CELL)
    assert max(rect_alpha_box(surf, 2 * CELL, 1 * CELL, CELL, CELL)) > 0
    assert alpha_at(surf, 2 * CELL - 2, CELL) == 0


def test_crop_pixel_offset_clips_at_the_seam():
    cv = WorldCanvas.from_geoms((Geom("A", 0, 0, 2560, 1440), Geom("B", 2560, 0, 2560, 1440)),
                                cell_size=CELL)
    b = cv.crops[1]
    assert b.local_x(b.col0) == pytest.approx(-4), "global col 142 straddles the seam"


# ------------------------------------------------------------- clip safety ---


def test_cells_outside_the_clip_are_deferred_not_dropped():
    rend, surf, ctx = renderer(w=CELL * 4, h=CELL * 4)
    ctx.rectangle(0, 0, CELL * 2, CELL * 2)  # clip to top-left 2x2 cells
    ctx.clip()
    frame = OutputFrame("DP-5", draw=(draw(gcol=0, row=0, glyph=7), draw(gcol=3, row=3, glyph=9)),
                        clear=())
    rend.apply(CrSpy(ctx), frame)
    assert max(rect_alpha_box(surf, 0, 0, CELL, CELL)) > 0
    assert max(rect_alpha_box(surf, 3 * CELL, 3 * CELL, CELL, CELL)) == 0, \
        "drew outside the clip"
    assert rend.pending_draws == {(3, 3)}, "clipped work must be held, not lost"
    # now let the clip cover everything and feed an empty frame: backlog drains
    rend.apply(CrSpy(cairo.Context(surf)), OutputFrame("DP-5", draw=(), clear=()))
    assert max(rect_alpha_box(surf, 3 * CELL, 3 * CELL, CELL, CELL)) > 0
    assert rend.pending_draws == set()


def test_pending_clear_cannot_be_lost_either():
    rend, surf, ctx = renderer(w=CELL * 4, h=CELL * 4)
    rend.apply(CrSpy(ctx), OutputFrame("DP-5", draw=(draw(gcol=2, row=2),), clear=()))
    lit_before = max(rect_alpha_box(surf, 2 * CELL, 2 * CELL, CELL, CELL))
    assert lit_before > 0
    clipped = cairo.Context(surf)
    clipped.rectangle(0, 0, CELL, CELL)
    clipped.clip()
    rend.apply(CrSpy(clipped), OutputFrame("DP-5", draw=(), clear=(Pos(2, 2),)))
    assert max(rect_alpha_box(surf, 2 * CELL, 2 * CELL, CELL, CELL)) == lit_before
    assert rend.pending_clears == {(2, 2)}
    rend.apply(CrSpy(cairo.Context(surf)), OutputFrame("DP-5", draw=(), clear=()))
    assert max(rect_alpha_box(surf, 2 * CELL, 2 * CELL, CELL, CELL)) == 0


def test_backlog_overflow_requests_a_resync():
    calls = []
    rend, surf, ctx = renderer(w=CELL * 2, h=CELL * 2, max_pending=64)
    rend.on_resync = lambda: calls.append(1)
    clipped = cairo.Context(surf)
    clipped.rectangle(0, 0, 1, 1)
    clipped.clip()
    big = OutputFrame("DP-5", draw=tuple(draw(gcol=i, row=i) for i in range(300)), clear=())
    for _ in range(5):
        rend.apply(CrSpy(clipped), big)
    assert calls, "backlog must trigger a full re-sync rather than grow forever"
    assert len(rend.pending_draws) <= rend.max_pending, "backlog is not capped"


# ------------------------------------------------------------------ glow ---


def test_glow_is_additive_on_heads_only():
    rend, surf, ctx = renderer(glow=True, w=CELL * 4, h=CELL * 4)
    spy = CrSpy(ctx)
    rend.apply(spy, OutputFrame("DP-5", draw=(draw(gcol=0, row=0, brightness=1.0, head=True),
                                              draw(gcol=1, row=0, brightness=0.5, head=False)),
                                clear=()))
    assert cairo.Operator.ADD in spy.operators()
    assert max(rect_alpha_box(surf, 0, 0, CELL, CELL)) > 0
    assert spy.operators().count(cairo.Operator.ADD) == 1, "glow on non-head cells"


def outer_alpha(surf, x, y, cell, pad):
    """Sum of alpha in the padded ring around a cell: the bloom's footprint."""
    surf.flush()
    buf = bytes(memoryview(surf.get_data()))
    stride = surf.get_stride()
    total = 0
    for py in range(y - pad, y + cell + pad):
        for px in range(x - pad, x + cell + pad):
            if x <= px < x + cell and y <= py < y + cell:
                continue
            if px < 0 or py < 0:
                continue
            total += buf[py * stride + px * 4 + 3]
    return total


def test_glow_spill_is_the_same_logical_size_on_both_scales():
    """D6 regression: the bloom is authored in logical px, so a head cell must
    light the same logical ring at scale 1 and at scale 2."""
    foot = {}
    for scale in (1, 2):
        rend, surf, ctx = renderer(glow=True, scale=scale, w=CELL * 6, h=CELL * 6)
        rend.apply(CrSpy(ctx), OutputFrame("DP-5", clear=(),
                                           draw=(draw(gcol=2, row=2, brightness=1.0),)))
        foot[scale] = outer_alpha(surf, 2 * CELL, 2 * CELL, CELL, 6)
    assert foot[1] > 0 and foot[2] > 0, f"no bloom footprint: {foot}"
    ratio = foot[2] / foot[1]
    assert 0.5 < ratio < 2.0, f"logical spill differs by {ratio:.2f}x: {foot}"


def test_disabled_glow_never_uses_add():
    rend, surf, ctx = renderer(glow=False, w=CELL * 4, h=CELL * 4)
    spy = CrSpy(ctx)
    rend.apply(spy, OutputFrame("DP-5", draw=(draw(gcol=0, row=0, head=True),), clear=()))
    assert cairo.Operator.ADD not in spy.operators()


# ------------------------------------------------------------- full sync ---


def test_frame_buffer_composes_deltas_instead_of_accumulating_them():
    """Livelock regression: queued frames were appended, so each draw grew heavier
    until the main loop never yielded. Composition bounds the work at one frame."""
    from matrix_overlay.render import FrameBuffer
    from matrix_overlay.world import Draw

    buf = FrameBuffer("DP-5")
    d = lambda col, row, glyph: Draw(col=col, row=row, global_col=col, glyph=glyph,  # noqa: E731
                                     brightness=0.5, head=False)
    buf.add(OutputFrame("DP-5", draw=(d(1, 1, 7),), clear=()))
    for i in range(50):  # 50 further deltas touching the same cells
        buf.add(OutputFrame("DP-5", draw=(d(1, 1, 100 + i),), clear=()))
    frame = buf.take()
    assert len(frame.draw) == 1, "buffer must compose, not queue"
    assert frame.draw[0].glyph == 149, "last write must win"
    assert buf.take() is None


def test_frame_buffer_clear_after_draw_wins_and_draw_after_clear_survives():
    from matrix_overlay.render import FrameBuffer
    from matrix_overlay.world import Draw, Pos

    d = lambda col, row: Draw(col=col, row=row, global_col=col, glyph=1,  # noqa: E731
                              brightness=1.0, head=True)
    clear_then_draw = FrameBuffer("x")
    clear_then_draw.add(OutputFrame("x", draw=(), clear=(Pos(2, 3),)))
    clear_then_draw.add(OutputFrame("x", draw=(d(2, 3),), clear=()))
    f = clear_then_draw.take()
    assert (len(f.draw), len(f.clear)) == (1, 0)

    draw_then_clear = FrameBuffer("x")
    draw_then_clear.add(OutputFrame("x", draw=(d(2, 3),), clear=()))
    draw_then_clear.add(OutputFrame("x", draw=(), clear=(Pos(2, 3),)))
    f = draw_then_clear.take()
    assert (len(f.draw), len(f.clear)) == (0, 1), "a cleared cell must not be repainted"


def test_frame_buffer_damage_rects_are_planned_before_the_draw():
    from matrix_overlay.render import FrameBuffer

    buf = FrameBuffer("DP-5")
    rend = renderer()[0]
    assert buf.rects(rend) == []
    buf.add(OutputFrame("DP-5", draw=(Draw(col=4, row=2, global_col=4, glyph=1,
                                           brightness=1.0, head=False),), clear=()))
    assert buf.rects(rend) == [(4 * CELL, 2 * CELL, CELL, CELL)]


def test_sync_repaints_the_authoritative_state():
    cv = WorldCanvas.from_geoms((Geom("DP-5", 0, 0, 2560, 1440),), cell_size=CELL)
    w = World(cv, Params(fps=30, cell_size=CELL, seed=5, stream_density=1.0,
                         fall_speed_jitter=0.0, mutate_rate=0.0))
    for _ in range(40):
        w.step()
    rend, surf, ctx = renderer(w=cv.width // CELL * CELL, h=cv.height // CELL * CELL)
    frame = w.render_state()[0]
    rend.sync(CrSpy(ctx), frame)
    assert rend.screen == {(c.col, c.row) for c in frame.draw}


def test_incremental_stream_matches_authoritative_render():
    """Drive a real world: incremental blits must equal a from-scratch repaint."""
    cv = WorldCanvas.from_geoms((Geom("DP-5", 0, 0, 2560, 1440),), cell_size=CELL)
    p = Params(fps=30, cell_size=CELL, seed=11, stream_density=1.0, fall_speed_jitter=0.0,
               mutate_rate=6.0, tail_length=10)
    w = World(cv, p)
    inc_surf = surface(cv.width, cv.rows * CELL)
    inc = Renderer(crop=cv.crops[0], canvas=cv, atlas=atlas(), scale=1,
                   glow_enabled=False)
    inc_cr = cairo.Context(inc_surf)
    for _ in range(30):
        inc.apply(CrSpy(inc_cr), w.step().per_output[0])
    ref_surf = surface(cv.width, cv.rows * CELL)
    ref = Renderer(crop=cv.crops[0], canvas=cv, atlas=atlas(), scale=1, glow_enabled=False)
    ref.sync(CrSpy(cairo.Context(ref_surf)), w.render_state()[0])
    assert bytes(memoryview(inc_surf.get_data())[:]) == \
        bytes(memoryview(ref_surf.get_data())[:]), "incremental render drifted from sync"
