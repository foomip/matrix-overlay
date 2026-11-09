# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""glpaint: pure packing helpers for the GL backend - no display, no context.

Kept free of ctypes/GL so the whole geometry contract is unit-testable (plan 1.5
applies to M6 too): the Cairo renderer needed a clip + pending-backlog machine to
avoid painting off-screen cells, but the rasteriser clips for free, so this module
only has to answer three questions:

  1. where does glyph (g, level) live in the atlas texture?   -> AtlasLayout
  2. how do Cairo's premultiplied BGRA bytes become an RGBA upload?
                                                           -> swizzle/pack_*
  3. what vertices does one frame of `world` produce?      -> CellGeom, QuadBatch

Atlas cells are laid out one glyph per column, one brightness level per row, so a
level is a horizontal strip: the same ordering the Cairo `GlyphAtlas` exposes as
`_surfaces[(glyph, level)]`.
"""
from __future__ import annotations

import array
import ctypes
import dataclasses
import math
from collections.abc import Mapping
from typing import Any, Sequence

from .atlas import level_from_brightness
from .world import BUCKETS, Draw

VERTS_PER_QUAD = 6           # two triangles, no index buffer (ES2-friendly)
FLOATS_PER_VERTEX = 8        # x, y, u, v, r, g, b, a
FLOATS_PER_QUAD = VERTS_PER_QUAD * FLOATS_PER_VERTEX
MAX_ATLAS_WIDTH = 8192       # stays under the min guaranteed texture size
VEIL_RGB = (0.0, 0.0, 0.0)   # the dim is neutral black, as in the CSS veil
MAX_DIM_ALPHA = 0.95         # never go fully opaque: parity with veil_css()


# ------------------------------------------------------------------- layout --

@dataclasses.dataclass(frozen=True)
class AtlasLayout:
    """Grid placement of `n_glyphs * buckets` cells of `cell_px` in one texture."""

    n_glyphs: int
    buckets: int
    cell_px: int
    max_width: int = MAX_ATLAS_WIDTH

    def __post_init__(self) -> None:
        if self.n_glyphs <= 0:
            raise ValueError(f"n_glyphs must be > 0, got {self.n_glyphs}")
        if self.buckets <= 0:
            raise ValueError(f"buckets must be > 0, got {self.buckets}")
        if self.cell_px <= 0:
            raise ValueError(f"cell_px must be > 0, got {self.cell_px}")
        if self.cols * self.cell_px > self.max_width:  # pragma: no cover - guard
            raise ValueError("column count exceeds max_width")

    @property
    def cols(self) -> int:
        return max(1, min(self.n_glyphs, self.max_width // self.cell_px))

    @property
    def n_cells(self) -> int:
        return self.n_glyphs * self.buckets

    @property
    def rows(self) -> int:
        return max(1, math.ceil(self.n_cells / self.cols))

    @property
    def width(self) -> int:
        return self.cols * self.cell_px

    @property
    def height(self) -> int:
        return self.rows * self.cell_px

    def slot(self, glyph: int, level: int) -> int:
        if not 0 <= glyph < self.n_glyphs:
            raise IndexError(f"glyph {glyph} out of range 0..{self.n_glyphs - 1}")
        if not 0 <= level < self.buckets:
            raise IndexError(f"level {level} out of range 0..{self.buckets - 1}")
        return level * self.n_glyphs + glyph

    def px_rect(self, glyph: int, level: int) -> tuple[int, int, int, int]:
        s = self.slot(glyph, level)
        return ((s % self.cols) * self.cell_px, (s // self.cols) * self.cell_px,
                self.cell_px, self.cell_px)

    def uv(self, glyph: int, level: int) -> tuple[float, float, float, float]:
        x, y, w, h = self.px_rect(glyph, level)
        wide, high = self.width, self.height
        return (x / wide, y / high, (x + w) / wide, (y + h) / high)


# ------------------------------------------------------------------- packing --

def swizzle_bgra_to_rgba(src: bytes) -> bytes:
    """Cairo ARGB32 is stored little-endian as B,G,R,A; GL wants R,G,B,A.

    Slice assignment swaps the channels at C speed without numpy, and is its own
    inverse, which the round-trip test asserts.
    """
    if len(src) % 4:
        raise ValueError(f"source length {len(src)} is not a whole number of pixels")
    if not src:
        return b""
    ba = bytearray(src)
    ba[0::4], ba[2::4] = ba[2::4], ba[0::4]
    return bytes(ba)


def pack_cell(dst: bytearray, layout: AtlasLayout, glyph: int, level: int,
              surface: Any) -> int:
    """Blit one Cairo surface into the atlas, row by row (stride-aware).

    Duck-typed on get_data/get_stride/get_width/get_height so tests need no
    cairo, and so `GlyphAtlas`'s own surfaces drop straight in.
    """
    x0, y0, w, h = layout.px_rect(glyph, level)
    data = bytes(memoryview(surface.get_data()))
    stride = surface.get_stride()
    n = min(w, surface.get_width())
    m = min(h, surface.get_height())
    if n <= 0 or m <= 0:
        return 0
    written = 0
    row_bytes = n * 4
    for row in range(m):
        start = row * stride
        chunk = swizzle_bgra_to_rgba(data[start:start + row_bytes])
        off = ((y0 + row) * layout.width + x0) * 4
        dst[off:off + len(chunk)] = chunk
        written += len(chunk)
    return written


def pack_atlas(layout: AtlasLayout,
               cells: Mapping[tuple[int, int], Any]) -> tuple[bytes, int]:
    """Pack {(glyph, level): surface}. Returns (rgba bytes, cells packed)."""
    dst = bytearray(layout.width * layout.height * 4)
    packed = 0
    for (glyph, level), surf in cells.items():
        pack_cell(dst, layout, glyph, level, surf)
        packed += 1
    return bytes(dst), packed


def atlas_cells(atlas, glow: bool = False) -> dict[tuple[int, int], Any]:
    """Adapt a GlyphAtlas to the packing API (glow cells are the padded ones)."""
    out: dict[tuple[int, int], Any] = {}
    for glyph in range(atlas.n_glyphs):
        for level in range(BUCKETS):
            try:
                out[(glyph, level)] = (atlas.glow_surface_for(glyph, level) if glow
                                       else atlas.surface_for(glyph, level))
            except RuntimeError:  # pragma: no cover - glow asked while disabled
                return {}
    return out


# ------------------------------------------------------------------ geometry --

@dataclasses.dataclass(frozen=True)
class CellGeom:
    """Logical grid -> device pixels for one output.

    Same maths as render.Renderer.rect_of (so both backends agree on where a cell
    is), times the output scale. x may go negative at the bezel seam; the GPU
    clips it, exactly as the surface clips the Cairo path.
    """

    col0: int
    row0: int
    x_px: int
    y_px: int
    cell_logical: int
    scale: int = 1

    @classmethod
    def from_crop(cls, crop, scale: int) -> CellGeom:
        return cls(col0=crop.col0, row0=crop.row0, x_px=crop.x_px, y_px=crop.y_px,
                   cell_logical=crop.cell_size, scale=max(1, int(scale)))

    def rect(self, col: int, row: int) -> tuple[float, float, float]:
        k = max(1, self.scale)
        gx = ((self.col0 + col) * self.cell_logical - self.x_px) * k
        gy = ((self.row0 + row) * self.cell_logical - self.y_px) * k
        return (float(gx), float(gy), float(self.cell_logical * k))


# -------------------------------------------------------------------- batch --

class QuadBatch:
    """Append-only interleaved vertex buffer with geometric growth.

    `vertex_array()` copies (safe to hand to tests and to keep across frames);
    `as_ctypes()` does not, and is what the spike path uses.

    Kept as the reference implementation of the vertex layout: the production hot
    path builds a CellBatch and lets the native side expand it (see emit_indexed),
    and the parity test in tests/test_glpaint.py asserts the two produce identical
    vertices. Removing this would remove the oracle for the C code.
    """

    floats_per_quad = FLOATS_PER_QUAD

    def __init__(self, capacity_quads: int = 512) -> None:
        self.capacity = max(1, int(capacity_quads))
        self._v = array.array("f", bytes(4 * self.capacity * self.floats_per_quad))
        self.n_quads = 0

    @property
    def n_verts(self) -> int:
        return self.n_quads * VERTS_PER_QUAD

    @property
    def n_floats(self) -> int:
        return self.n_verts * FLOATS_PER_VERTEX

    def reset(self) -> None:
        self.n_quads = 0

    def ensure(self, extra: int = 1) -> None:
        need = self.n_quads + max(0, int(extra))
        if need <= self.capacity:
            return
        cap = self.capacity
        while cap < need:
            cap *= 2
        grown = array.array("f", bytes(4 * cap * self.floats_per_quad))
        grown[:len(self._v)] = self._v
        self._v = grown
        self.capacity = cap

    def add(self, x: float, y: float, w: float, h: float,
            uv: Sequence[float],
            color: Sequence[float] = (1.0, 1.0, 1.0, 1.0)) -> bool:
        self.ensure(1)
        u0, v0, u1, v1 = uv
        r, g, b, a = color
        x1, y1 = x + w, y + h
        v = self._v
        i = self.n_quads * self.floats_per_quad
        for px, py, pu, pv in ((x, y, u0, v0), (x1, y, u1, v0), (x1, y1, u1, v1),
                              (x, y, u0, v0), (x1, y1, u1, v1), (x, y1, u0, v1)):
            v[i] = px
            v[i + 1] = py
            v[i + 2] = pu
            v[i + 3] = pv
            v[i + 4] = r
            v[i + 5] = g
            v[i + 6] = b
            v[i + 7] = a
            i += FLOATS_PER_VERTEX
        self.n_quads += 1
        return True

    def vertex_array(self) -> array.array:
        return self._v[:self.n_floats]

    def as_ctypes(self) -> tuple[Any, int]:
        addr, _n = self._v.buffer_info()
        return ctypes.cast(addr, ctypes.POINTER(ctypes.c_float)), self.n_verts


# -------------------------------------------------------------------- emit --

def _visible(cells: Mapping[tuple[int, int], Draw]) -> list[tuple[int, int]]:
    """Sorted keys: identical output regardless of dict insertion order, which
    keeps the self-test digest meaningful."""
    return sorted(cells)


def as_float_ptr(arr: array.array) -> Any:
    """ctypes pointer into an array('f') buffer, without copying.

    The caller must keep `arr` alive while the native side reads it - GlTextures
    owns the uv tables for exactly that reason.
    """
    addr, _n = arr.buffer_info()
    return ctypes.cast(addr, ctypes.POINTER(ctypes.c_float))


def emit_cells(batch: QuadBatch, cells: Mapping[tuple[int, int], Draw],
               geom: CellGeom, layout: AtlasLayout) -> int:
    batch.ensure(len(cells))
    emitted = 0
    for pos in _visible(cells):
        d = cells[pos]
        level = level_from_brightness(d.brightness)
        if level <= 0 or d.glyph >= layout.n_glyphs:
            continue  # level 0 is stored blank: not worth a triangle pair
        x, y, size = geom.rect(*pos)
        batch.add(x, y, size, size, layout.uv(d.glyph, level))
        emitted += 1
    return emitted


def emit_glow(batch: QuadBatch, cells: Mapping[tuple[int, int], Draw],
              geom: CellGeom, layout: AtlasLayout, pad_px: float) -> int:
    """Head bloom, in the padded glow atlas, drawn additively.

    `pad_px` is in device pixels (atlas `glow_pad`), and the quad grows by it on
    every side so the halo stays centred on the cell.
    """
    batch.ensure(len(cells))
    emitted = 0
    for pos in _visible(cells):
        d = cells[pos]
        level = level_from_brightness(d.brightness)
        if not d.head or level <= 0 or d.glyph >= layout.n_glyphs:
            continue
        x, y, size = geom.rect(*pos)
        s = size + 2 * pad_px
        batch.add(x - pad_px, y - pad_px, s, s, layout.uv(d.glyph, level))
        emitted += 1
    return emitted


# ------------------------------------------------- indexed batches (native) --

INTS_PER_CELL = 5      # col, row, glyph, level, head


class CellBatch:
    """Five int32 per cell, expanded into vertices by the native layer.

    Why not QuadBatch: expanding 48 floats per cell in Python measured ~7 ms per
    surface per frame at ~1000 cells - after moving the pixels to the GPU, the
    vertex build itself had become the largest remaining cost. Five appends per
    cell instead of 48 float writes keeps Python out of the hot path.
    """

    ints_per_cell = INTS_PER_CELL

    def __init__(self, capacity_cells: int = 1024) -> None:
        self.capacity = max(1, int(capacity_cells))
        self._v = array.array("i", bytes(4 * self.capacity * self.ints_per_cell))
        self.n_cells = 0

    def reset(self) -> None:
        self.n_cells = 0

    def ensure(self, extra: int = 1) -> None:
        need = self.n_cells + max(0, int(extra))
        if need <= self.capacity:
            return
        cap = self.capacity
        while cap < need:
            cap *= 2
        grown = array.array("i", bytes(4 * cap * self.ints_per_cell))
        grown[:len(self._v)] = self._v
        self._v = grown
        self.capacity = cap

    def add(self, col: int, row: int, glyph: int, level: int, head: int = 0) -> bool:
        self.ensure(1)
        i = self.n_cells * self.ints_per_cell
        v = self._v
        v[i] = col
        v[i + 1] = row
        v[i + 2] = glyph
        v[i + 3] = level
        v[i + 4] = 1 if head else 0
        self.n_cells += 1
        return True

    def index_array(self) -> array.array:
        return self._v[:self.n_cells * self.ints_per_cell]

    def as_ctypes(self) -> tuple[Any, int]:
        addr, _n = self._v.buffer_info()
        return ctypes.cast(addr, ctypes.POINTER(ctypes.c_int)), self.n_cells


def uv_table(layout: AtlasLayout) -> array.array:
    """Four floats per (glyph, level) slot, indexed `glyph * buckets + level`.

    The native expander looks cells up here, so the stride is a contract between
    the two layers - it is passed to mwl_set_geom as uv_stride.
    """
    tbl = array.array("f", bytes(4 * layout.n_glyphs * layout.buckets * 4))
    for glyph in range(layout.n_glyphs):
        for level in range(layout.buckets):
            u0, v0, u1, v1 = layout.uv(glyph, level)
            i = (glyph * layout.buckets + level) * 4
            tbl[i] = u0
            tbl[i + 1] = v0
            tbl[i + 2] = u1
            tbl[i + 3] = v1
    return tbl


def emit_indexed(batch: CellBatch, cells: Mapping[tuple[int, int], Draw],
                 layout: AtlasLayout) -> int:
    """Pack the visible cells as indices. Returns how many were emitted."""
    batch.ensure(len(cells))
    emitted = 0
    for pos in _visible(cells):
        d = cells[pos]
        level = level_from_brightness(d.brightness)
        if level <= 0 or d.glyph >= layout.n_glyphs:
            continue  # level 0 is stored blank: no triangles needed
        batch.add(pos[0], pos[1], d.glyph, level, 1 if d.head else 0)
        emitted += 1
    return emitted


def clear_colour(dim_alpha: float, rgb: Sequence[float] = VEIL_RGB
                 ) -> tuple[float, float, float, float]:
    """Premultiplied clear colour == the old CSS veil, now done by the GPU.

    Replaces `surface.veil_css`: the dim is no longer a widget background, it is
    the clear value, so 'clearing' a cell reveals the dim exactly as before.
    """
    a = max(0.0, min(MAX_DIM_ALPHA, float(dim_alpha)))
    return (float(rgb[0]) * a, float(rgb[1]) * a, float(rgb[2]) * a, a)
