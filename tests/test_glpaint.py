"""M6-N2 RED: pure GL packing helpers (no display, no GL context).

Contracts under test:
  * the atlas texture is a deterministic grid of (glyph, level) cells with
    non-overlapping UV rects - one upload per atlas, zero per-frame text work
  * Cairo ARGB32 is little-endian BGRA, premultiplied; uploading it as RGBA
    needs an R/B swap and must preserve G/A exactly
  * cell geometry is computed in *device* pixels (logical * output scale),
    including the negative-x seam case the Cairo renderer already handles
  * the quad batch interleaves (pos, uv, colour) as 6 vertices per quad and
    grows without losing content
  * glow quads are offset by the baked pad, so the halo stays centred
"""
from __future__ import annotations

import pathlib

import pytest

from matrix_overlay.glpaint import (AtlasLayout, CellGeom, QuadBatch, clear_colour,
                                    emit_cells, emit_glow, pack_atlas, pack_cell,
                                    swizzle_bgra_to_rgba)
from matrix_overlay.glpaint import CellBatch, emit_indexed, uv_table
from matrix_overlay.world import Draw

CELL = 8


class FakeSurface:
    """Duck-typed cairo.ImageSurface: stride-padded ARGB32 (i.e. BGRA in memory)."""

    def __init__(self, width=CELL, height=CELL, fill=(0, 0, 0, 0), pad_bytes=0):
        self._w, self._h = width, height
        self._stride = width * 4 + pad_bytes
        row = b"".join(bytes(fill) for _ in range(width)) + bytes(pad_bytes)
        self._data = row * height

    def get_data(self):
        return memoryview(self._data)

    def get_stride(self):
        return self._stride

    def get_width(self):
        return self._w

    def get_height(self):
        return self._h


def cell_value(glyph: int, level: int) -> tuple[int, int, int, int]:
    """Distinct per cell so we can prove where each byte landed."""
    return (10 + glyph, 20 + level, 30 + glyph * level % 200, 255)


def layout(n_glyphs=4, buckets=2, cell_px=CELL, max_width=1024) -> AtlasLayout:
    return AtlasLayout(n_glyphs=n_glyphs, buckets=buckets, cell_px=cell_px,
                       max_width=max_width)


# ------------------------------------------------------------------ layout --

def test_layout_size_is_a_whole_number_of_cell_rows():
    lay = layout(n_glyphs=4, buckets=2, cell_px=CELL)
    assert lay.width == lay.cols * CELL
    assert lay.height == lay.rows * CELL
    assert lay.cols * lay.rows >= 4 * 2, "every (glyph, level) pair needs a slot"


def test_layout_wraps_columns_when_too_wide():
    lay = AtlasLayout(n_glyphs=64, buckets=8, cell_px=36, max_width=4 * 36)
    assert lay.cols == 4
    assert lay.width == 4 * 36
    assert lay.width * lay.height * 4 >= 64 * 8 * 36 * 36 * 4


def test_uv_rects_stay_inside_the_unit_square():
    lay = layout()
    for g in range(lay.n_glyphs):
        for lvl in range(lay.buckets):
            u0, v0, u1, v1 = lay.uv(g, lvl)
            assert 0.0 <= u0 < u1 <= 1.0
            assert 0.0 <= v0 < v1 <= 1.0


def test_uv_rects_never_overlap():
    """A shared texel between two glyphs would bleed neighbouring glyphs."""
    lay = layout(n_glyphs=7, buckets=3, cell_px=CELL)
    seen: set[tuple[int, int]] = set()
    for g in range(lay.n_glyphs):
        for lvl in range(lay.buckets):
            x0, y0, w, h = lay.px_rect(g, lvl)
            assert (w, h) == (CELL, CELL)
            texels = {(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h)}
            assert not (texels & seen), f"cell ({g},{lvl}) overlaps an earlier cell"
            seen |= texels


# ----------------------------------------------------------------- swizzle --

def test_swizzle_swaps_red_and_blue_and_keeps_green_alpha():
    bgra = bytes([7, 8, 9, 10, 11, 12, 13, 14])
    assert swizzle_bgra_to_rgba(bgra) == bytes([9, 8, 7, 10, 13, 12, 11, 14])


def test_swizzle_is_length_preserving_and_involutive():
    src = bytes(range(64))
    out = swizzle_bgra_to_rgba(src)
    assert len(out) == len(src)
    assert swizzle_bgra_to_rgba(out) == src


# -------------------------------------------------------------------- pack --

def test_pack_cell_lands_at_its_uv_offset():
    lay = layout()
    dst = bytearray(lay.width * lay.height * 4)
    x0, y0, _w, _h = lay.px_rect(2, 1)
    surf = FakeSurface(fill=(1, 2, 3, 255))
    pack_cell(dst, lay, 2, 1, surf)
    off = (y0 * lay.width + x0) * 4
    assert dst[off:off + 4] == bytes([3, 2, 1, 255]), "R/B swapped, G/A kept"


def test_pack_cell_respects_source_stride_padding():
    lay = layout()
    dst = bytearray(lay.width * lay.height * 4)
    surf = FakeSurface(pad_bytes=8, fill=(5, 6, 7, 255))
    pack_cell(dst, lay, 0, 0, surf)
    for row in range(CELL):
        line = dst[row * lay.width * 4:(row * 1 + 1) * lay.width * 4]
        assert line[:CELL * 4] == bytes([7, 6, 5, 255]) * CELL, f"row {row} skewed"


def test_pack_cell_leaves_neighbours_untouched():
    lay = layout()
    dst = bytearray(lay.width * lay.height * 4)
    x0, y0, _w, _h = lay.px_rect(1, 0)
    pack_cell(dst, lay, 1, 0, FakeSurface(fill=(9, 9, 9, 255)))
    other_x, other_y, _ow, _oh = lay.px_rect(0, 0)
    off = (other_y * lay.width + other_x) * 4
    assert dst[off:off + 4] == bytes(4), "neighbouring cell was written"


def test_pack_atlas_packs_every_supplied_cell():
    lay = layout(n_glyphs=4, buckets=2)
    cells = {(g, lvl): FakeSurface(fill=cell_value(g, lvl))
             for g in range(4) for lvl in range(2)}
    blob, n = pack_atlas(lay, cells)
    assert n == 8
    assert len(blob) == lay.width * lay.height * 4
    g, lvl = 3, 1
    x0, y0, _w, _h = lay.px_rect(g, lvl)
    b, gr, r, a = cell_value(g, lvl)
    off = (y0 * lay.width + x0) * 4
    assert blob[off:off + 4] == bytes((r, gr, b, a))


# ------------------------------------------------------------------ geometry --

def geom(scale=2, col0=10, row0=3, x_px=180, y_px=54, cell=18) -> CellGeom:
    return CellGeom(col0=col0, row0=row0, x_px=x_px, y_px=y_px,
                    cell_logical=cell, scale=scale)


def test_cell_geom_converts_to_device_pixels():
    """Matches render.Renderer.rect_of, then scaled to device pixels."""
    g = geom(scale=2)          # col0=10 row0=3 x_px=180 y_px=54 cell=18
    assert g.rect(0, 0) == (0.0, 0.0, 36.0)          # (10*18-180)*2, (3*18-54)*2
    assert g.rect(1, 1) == (36.0, 36.0, 36.0)        # (11*18-180)*2, (4*18-54)*2


def test_cell_geom_keeps_the_negative_seam_x():
    """Cells physically clipped by the bezel must keep negative x; the GPU clips."""
    x, _y, _s = geom(scale=1, col0=0, x_px=6).rect(0, 0)
    assert x == -6


def test_cell_geom_scale_one_is_identity_for_whole_cells():
    g = CellGeom(col0=0, row0=0, x_px=0, y_px=0, cell_logical=18, scale=1)
    assert g.rect(4, 7) == (72.0, 126.0, 18.0)


# ------------------------------------------------------------------- batch --

def test_batch_writes_six_interleaved_vertices_per_quad():
    b = QuadBatch(capacity_quads=4)
    b.add(1, 2, 3, 4, (0.0, 0.25, 0.5, 0.75), (1.0, 0.5, 0.25, 1.0))
    assert b.n_quads == 1 and b.n_verts == 6
    v = list(b.vertex_array())
    assert len(v) == 6 * 8
    assert v[0:8] == [1.0, 2.0, 0.0, 0.25, 1.0, 0.5, 0.25, 1.0]
    assert v[8:10] == [4.0, 2.0] and v[10:12] == [0.5, 0.25]   # top-right corner
    assert v[40:42] == [1.0, 6.0]                              # bottom-left corner


def test_batch_grows_without_losing_content():
    b = QuadBatch(capacity_quads=2)
    for i in range(50):
        assert b.add(i, 0, 1, 1, (0.0, 0.0, 1.0, 1.0))
    assert b.n_quads == 50 and b.capacity >= 50
    v = b.vertex_array()
    assert v[0] == 0.0
    assert v[40 * b.floats_per_quad] == 40.0, "quad 40 survived the reallocation"


def test_batch_reset_clears_count_not_capacity():
    b = QuadBatch(capacity_quads=8)
    b.add(0, 0, 1, 1, (0.0, 0.0, 1.0, 1.0))
    cap = b.capacity
    b.reset()
    assert b.n_quads == 0 and b.capacity == cap


def test_batch_default_colour_is_opaque_white():
    """Tint is baked into the atlas, so the vertex colour must be a no-op."""
    b = QuadBatch(capacity_quads=1)
    b.add(0, 0, 1, 1, (0.0, 0.0, 1.0, 1.0))
    assert list(b.vertex_array()[4:8]) == [1.0, 1.0, 1.0, 1.0]


# ------------------------------------------------- native expander parity --

NATIVE_LIB = pathlib.Path(__file__).resolve().parents[1] / "native" / "libmwl.so"
needs_native = pytest.mark.skipif(not NATIVE_LIB.exists(), reason="libmwl.so not built")


def _expand_native(batch, tbl, g, buckets, pad=0.0, heads_only=False, cap=64):
    """Call the pure C expander directly: no display, no GL context needed."""
    import ctypes

    from matrix_overlay.glpaint import as_float_ptr

    lib = ctypes.CDLL(str(NATIVE_LIB))
    lib.mwl_expand_cells.restype = ctypes.c_int
    lib.mwl_expand_cells.argtypes = [
        ctypes.POINTER(ctypes.c_float), ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_float), ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.c_int,
        ctypes.c_float, ctypes.c_int,
    ]
    out = (ctypes.c_float * (cap * 6 * 8))()
    iptr, n = batch.as_ctypes()
    got = lib.mwl_expand_cells(out, cap * 6, g.col0, g.row0, g.x_px, g.y_px,
                               g.cell_logical, g.scale, buckets,
                               as_float_ptr(tbl), len(tbl), iptr, n, pad,
                               1 if heads_only else 0)
    return out, int(got)


def parity_cells():
    """Mixed glyphs, levels and heads: exercises every branch of the expander."""
    return {(c, r): draw(c, r, glyph=(c * 3 + r) % 6, brightness=(r + 1) / 7.0,
                         head=(r % 3 == 0))
            for c in range(5) for r in range(4)}


@needs_native
def test_native_expander_matches_the_python_packer_vertex_for_vertex():
    """The production path packs indices and lets C build the vertices; this asserts
    it produces exactly what the pure Python reference produces."""
    lay = layout(n_glyphs=6, buckets=8)
    g = CellGeom(col0=2, row0=1, x_px=36, y_px=18, cell_logical=18, scale=2)

    ref = QuadBatch(64)
    emit_cells(ref, parity_cells(), g, lay)
    ib = CellBatch(64)
    assert emit_indexed(ib, parity_cells(), lay) == 20

    out, got = _expand_native(ib, uv_table(lay), g, lay.buckets)
    assert got == ref.n_verts
    assert list(out[:got * 8]) == pytest.approx(list(ref.vertex_array()))


@needs_native
def test_native_glow_expansion_matches_the_python_packer():
    lay = layout(n_glyphs=6, buckets=8)
    glow_lay = AtlasLayout(n_glyphs=6, buckets=8, cell_px=lay.cell_px + 8)
    g = CellGeom(col0=1, row0=0, x_px=18, y_px=0, cell_logical=18, scale=1)

    ref = QuadBatch(64)
    emit_glow(ref, parity_cells(), g, glow_lay, pad_px=4)
    ib = CellBatch(64)
    emit_indexed(ib, parity_cells(), lay)

    out, got = _expand_native(ib, uv_table(glow_lay), g, glow_lay.buckets,
                              pad=4.0, heads_only=True)
    assert got == ref.n_verts
    assert list(out[:got * 8]) == pytest.approx(list(ref.vertex_array()))


@needs_native
def test_native_expander_drops_invisible_and_stale_cells():
    lay = layout(n_glyphs=4, buckets=8)
    g = CellGeom(col0=0, row0=0, x_px=0, y_px=0, cell_logical=18, scale=1)
    batch = CellBatch(8)
    batch.add(0, 0, 1, 0, 0)        # level 0: blank cell, no triangles
    batch.add(1, 0, 99, 3, 0)       # glyph outside the atlas
    batch.add(2, 0, 1, 3, 0)        # the only visible one
    out, got = _expand_native(batch, uv_table(lay), g, lay.buckets)
    assert got == 6, "exactly one quad survives the filters"
    assert out[0] == 36.0 and out[1] == 0.0     # col 2 * 18px


def test_uv_table_and_cell_batch_match_the_native_contract():
    """Documented ABI: uv[glyph * buckets + level] -> 4 floats; cells are 5 int32."""
    lay = layout(n_glyphs=3, buckets=8)
    tbl = uv_table(lay)
    assert len(tbl) == 3 * 8 * 4
    b = CellBatch(2)
    b.add(1, 2, 3, 4, 1)
    assert list(b.index_array()) == [1, 2, 3, 4, 1]


# ------------------------------------------------------------------- emit --

def draw(col, row, glyph=1, brightness=1.0, head=False) -> Draw:
    return Draw(col=col, row=row, global_col=col, glyph=glyph,
                brightness=brightness, head=head)


def test_emit_cells_one_quad_per_visible_cell():
    lay = layout(n_glyphs=4, buckets=8)
    b = QuadBatch(capacity_quads=4)
    cells = {(0, 0): draw(0, 0), (1, 2): draw(1, 2, brightness=0.5)}
    assert emit_cells(b, cells, geom(scale=1), lay) == 2
    assert b.n_quads == 2


def test_emit_cells_skips_invisible_level_zero():
    lay = layout(n_glyphs=4, buckets=8)
    b = QuadBatch(capacity_quads=4)
    # brightness low enough to quantise to level 0 (atlas stores those blank)
    assert emit_cells(b, {(0, 0): draw(0, 0, brightness=0.0)}, geom(), lay) == 0
    assert b.n_quads == 0


def test_emit_cells_is_deterministic():
    lay = layout(n_glyphs=4, buckets=8)
    cells = {(c, r): draw(c, r, glyph=(c + r) % 4)
             for c in range(3) for r in range(3)}
    a, b = QuadBatch(4), QuadBatch(4)
    emit_cells(a, cells, geom(scale=1), lay)
    emit_cells(b, dict(reversed(list(cells.items()))), geom(scale=1), lay)
    assert list(a.vertex_array()) == list(b.vertex_array())


def test_emit_glow_shifts_quad_by_minus_pad():
    lay = AtlasLayout(n_glyphs=4, buckets=8, cell_px=CELL + 8)
    body = QuadBatch(4)
    glow = QuadBatch(4)
    cells = {(0, 0): draw(0, 0, brightness=1.0, head=True)}
    emit_cells(body, cells, geom(scale=1), layout(n_glyphs=4, buckets=8))
    emit_glow(glow, cells, geom(scale=1), lay, pad_px=4)
    bx, by = list(body.vertex_array())[0], list(body.vertex_array())[1]
    gx, gy = list(glow.vertex_array())[0], list(glow.vertex_array())[1]
    assert (gx, gy) == (bx - 4.0, by - 4.0)


def test_emit_glow_only_emits_heads():
    lay = AtlasLayout(n_glyphs=4, buckets=8, cell_px=CELL + 8)
    b = QuadBatch(4)
    cells = {(0, 0): draw(0, 0, head=True), (1, 0): draw(1, 0, head=False),
             (2, 0): draw(2, 0, head=True)}
    assert emit_glow(b, cells, geom(scale=1), lay, pad_px=4) == 2


# --------------------------------------------------- indexed batches (C path) --

def test_uv_table_is_laid_out_for_glyph_times_stride():
    """The C expander indexes uv[glyph * buckets + level]; the table must agree."""
    lay = layout(n_glyphs=4, buckets=8)
    tbl = uv_table(lay)
    assert len(tbl) == 4 * 8 * 4
    g, lvl = 2, 5
    expect = lay.uv(g, lvl)
    got = tuple(tbl[(g * 8 + lvl) * 4:(g * 8 + lvl) * 4 + 4])
    assert got == pytest.approx(expect)


def test_cell_batch_is_five_ints_per_cell():
    b = CellBatch(capacity_cells=4)
    b.add(1, 2, 3, 4, 1)
    assert b.n_cells == 1 and len(b.index_array()) == 5
    assert list(b.index_array()) == [1, 2, 3, 4, 1]


def test_cell_batch_grows_without_losing_content():
    b = CellBatch(capacity_cells=2)
    for i in range(30):
        b.add(i, 0, 1, 2, 0)
    assert b.n_cells == 30 and b.capacity >= 30
    assert b.index_array()[29 * 5] == 29


def test_emit_indexed_skips_invisible_and_out_of_range_cells():
    lay = layout(n_glyphs=4, buckets=8)
    b = CellBatch(4)
    cells = {(0, 0): draw(0, 0, glyph=1, brightness=1.0),
             (1, 0): draw(1, 0, glyph=1, brightness=0.0),      # level 0 -> invisible
             (2, 0): draw(2, 0, glyph=99, brightness=1.0)}     # glyph not in atlas
    assert emit_indexed(b, cells, lay) == 1
    assert list(b.index_array()) == [0, 0, 1, 7, 0]


def test_emit_indexed_records_head_and_is_deterministic():
    lay = layout(n_glyphs=4, buckets=8)
    cells = {(c, r): draw(c, r, glyph=c, head=(r == 0))
             for c in range(3) for r in range(2)}
    a, b = CellBatch(4), CellBatch(4)
    emit_indexed(a, cells, lay)
    emit_indexed(b, dict(reversed(list(cells.items()))), lay)
    assert list(a.index_array()) == list(b.index_array())
    heads = [a.index_array()[i + 4] for i in range(0, a.n_cells * 5, 5)]
    assert heads == [1, 0, 1, 0, 1, 0], "row 0 cells are the heads (sorted by col, row)"


# ------------------------------------------------------------------- veil --

def test_clear_colour_is_premultiplied_black_clamped_to_the_max():
    assert clear_colour(0.72) == (0.0, 0.0, 0.0, 0.72)
    assert clear_colour(1.9) == (0.0, 0.0, 0.0, 0.95)
    assert clear_colour(-3) == (0.0, 0.0, 0.0, 0.0)


def test_clear_colour_multiplies_a_custom_veil_tint():
    r, g, b, a = clear_colour(0.5, rgb=(0.2, 0.4, 0.6))
    assert (r, g, b) == pytest.approx((0.1, 0.2, 0.3)), "premultiplied by alpha"
    assert a == 0.5
