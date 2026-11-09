# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""GlyphAtlas: bake every (glyph, brightness) pair once, blit forever.

Plan 2.4 / C4: per-frame Pango or Cairo text calls must be zero. Rendering uses
PangoCairo into ImageSurfaces, which needs no display connection - so this
module is unit-testable with no compositor at all.

Each glyph is centred inside its cell rather than laid out on the font's
advance width: fontconfig fallback swaps faces mid-set (measured: katakana falls
back to Noto even when DejaVu is requested), and centring makes the grid immune
to mismatched advances.
"""
from __future__ import annotations

import dataclasses
import hashlib

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

from .world import BUCKETS  # noqa: E402

NOTDEF = 0x10FFFF
GLYPH_SIZE_RATIO = 0.8
_HALF_KATA = (0xFF66, 0xFF9F)
_KATA = (0x30A1, 0x30FF)
_DIGITS = (0x0030, 0x0039)
_UPPER = (0x0041, 0x005A)
_SYMBOLS = "!?<>[]()"


def _range(a: int, b: int) -> list[int]:
    return list(range(a, b + 1))


def codepoints_for(glyph_set: str, custom: str = "") -> tuple[int, ...]:
    """Glyph ids index into this tuple, so it must be unique and ordered."""
    if glyph_set == "custom":
        chars = [c for c in custom if not c.isspace()]
        cps: list[int] = []
        for ch in chars:
            if ord(ch) not in cps:
                cps.append(ord(ch))
        if not cps:
            raise ValueError("glyph_set='custom' needs a non-empty custom_glyphs")
        return tuple(cps)
    if glyph_set == "matrix":
        cps = _range(*_KATA) + _range(*_HALF_KATA) + _range(*_DIGITS) + _range(*_UPPER)
        cps += [ord(c) for c in _SYMBOLS]
    elif glyph_set == "katakana":
        cps = _range(*_HALF_KATA) + _range(*_KATA)
    elif glyph_set == "ascii":
        cps = _range(0x21, 0x7E)
    elif glyph_set == "digits":
        cps = _range(*_DIGITS)
    else:
        raise ValueError(f"unknown glyph_set {glyph_set!r}")
    return tuple(dict.fromkeys(cps))


def _rgb(spec: str) -> tuple[float, float, float]:
    s = spec.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a: tuple[float, float, float], b: tuple[float, float, float], t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


@dataclasses.dataclass(frozen=True)
class AtlasKey:
    """Everything that invalidates the atlas (reload class L2, plan C3)."""

    font: str
    cell_size: int
    scale: int = 1
    render_scale: float = 1.0
    glyph_set: str = "matrix"
    custom_glyphs: str = ""
    head_color: str = "#d6ffd6"
    body_color: str = "#00b81f"
    glow_enabled: bool = False
    glow_radius: int = 6
    glow_intensity: float = 0.45

    @property
    def cell_px(self) -> int:
        return max(4, round(self.cell_size * self.scale * self.render_scale))

    @property
    def glow_px(self) -> int:
        """`glow_radius` is a *logical* length (like cell_size), so the baked halo
        must grow with the monitor scale - otherwise the bloom is half the size on
        a scale=2 output and double on scale=1 (measured timing asymmetry)."""
        return max(0, round(self.glow_radius * self.scale * self.render_scale))


def _surface(px: int) -> cairo.ImageSurface:
    return cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)


def _digest(surface: cairo.ImageSurface) -> str:
    surface.flush()
    return hashlib.sha256(bytes(memoryview(surface.get_data())[:])).hexdigest()


def _render_base(ch: str, font: str, size_px: float, cell: int) -> cairo.ImageSurface:
    """White glyph, alpha only - the mask every brightness level is tinted from."""
    surf = _surface(cell)
    cr = cairo.Context(surf)
    layout = Pango.Layout(PangoCairo.create_context(cr))
    fd = Pango.FontDescription.from_string(font)
    fd.set_absolute_size(size_px * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_text(ch, -1)
    _ink, logic = layout.get_pixel_extents()
    cr.set_source_rgb(1.0, 1.0, 1.0)
    cr.move_to(round((cell - logic.width) / 2), round((cell - logic.height) / 2))
    PangoCairo.show_layout(cr, layout)
    surf.flush()
    return surf


def _has_ink(surface: cairo.ImageSurface) -> bool:
    surface.flush()
    data = bytes(memoryview(surface.get_data()))
    return any(data[3::4])


def _tinted(base: cairo.ImageSurface, cell: int, colour, ratio: float) -> cairo.ImageSurface:
    surf = _surface(cell)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*colour)
    cr.mask(cairo.SurfacePattern(base))  # colour is the source, glyph alpha is the mask
    if ratio >= 1.0:
        surf.flush()
        return surf
    out = _surface(cell)
    ocr = cairo.Context(out)
    ocr.set_source_surface(surf, 0, 0)
    ocr.paint_with_alpha(max(0.0, ratio))
    out.flush()
    return out


def _halo(tinted: cairo.ImageSurface, cell: int, pad: int, intensity: float):
    """Bloom baked once: downscale to a quarter cell, then stretch it back over
    the padded size - the stretch is what spreads the halo, so spill scales with
    `pad`. Never computed per frame (trap T8)."""
    size = cell + 2 * pad
    small = max(2, cell // 4)
    down = _surface(small)
    dcr = cairo.Context(down)
    dcr.scale(small / cell, small / cell)
    dcr.set_source_surface(tinted, 0, 0)
    dcr.get_source().set_filter(cairo.FILTER_BEST)
    dcr.paint()
    out = _surface(size)
    cr = cairo.Context(out)
    pat = cairo.SurfacePattern(down)
    pat.set_filter(cairo.FILTER_BEST)
    shrink = cairo.Matrix()  # pycairo mutates in place; Matrix.scale() returns None
    shrink.scale(small / size, small / size)
    pat.set_matrix(shrink)
    cr.set_source(pat)
    cr.paint_with_alpha(max(0.005, intensity))
    out.flush()
    return out


class GlyphAtlas:
    def __init__(self, key: AtlasKey) -> None:
        self.key = key
        self.codepoints = codepoints_for(key.glyph_set, key.custom_glyphs)
        self.n_glyphs = len(self.codepoints)
        self.cell_px = key.cell_px
        self.glow_pad = key.glow_px if key.glow_enabled else 0
        self.glow_pad_logical = key.glow_radius * key.render_scale if key.glow_enabled else 0
        size_px = self.cell_px * GLYPH_SIZE_RATIO
        head, body = _rgb(key.head_color), _rgb(key.body_color)

        notdef = _digest(_render_base(chr(NOTDEF), key.font, size_px, self.cell_px))
        self._surfaces: dict[tuple[int, int], cairo.ImageSurface] = {}
        self._glow: dict[tuple[int, int], cairo.ImageSurface] = {}
        self._hashes: dict[tuple[int, int], str] = {}
        blank: list[int] = []
        for gid, cp in enumerate(self.codepoints):
            base = _render_base(chr(cp), key.font, size_px, self.cell_px)
            if _digest(base) == notdef or not _has_ink(base):
                blank.append(gid)
            for level in range(BUCKETS):
                ratio = level / (BUCKETS - 1)
                tinted = (_tinted(base, self.cell_px, _mix(body, head, ratio), ratio)
                          if level else _surface(self.cell_px))
                self._surfaces[(gid, level)] = tinted
                self._hashes[(gid, level)] = _digest(tinted)
                if self.glow_pad:
                    self._glow[(gid, level)] = (_halo(tinted, self.cell_px, self.glow_pad,
                                                      key.glow_intensity) if level
                                                else _surface(self.cell_px + 2 * self.glow_pad))
        self.unsupported = tuple(blank)

    # ------------------------------------------------------------ access ---

    def surface_for(self, glyph: int, level: int) -> cairo.ImageSurface:
        self._check(glyph, level)
        return self._surfaces[(glyph, level)]

    def glow_surface_for(self, glyph: int, level: int) -> cairo.ImageSurface:
        if not self.glow_pad:
            raise RuntimeError("glow is disabled for this atlas")
        self._check(glyph, level)
        return self._glow[(glyph, level)]

    def surface_hash(self, glyph: int, level: int) -> str:
        self._check(glyph, level)
        return self._hashes[(glyph, level)]

    def _check(self, glyph: int, level: int) -> None:
        if not 0 <= glyph < self.n_glyphs:
            raise IndexError(f"glyph id {glyph} out of range 0..{self.n_glyphs - 1}")
        if not 0 <= level < BUCKETS:
            raise IndexError(f"brightness level {level} out of range 0..{BUCKETS - 1}")

    @property
    def surface_count(self) -> int:
        per = BUCKETS * (2 if self.glow_pad else 1)
        return self.n_glyphs * per

    @property
    def approx_bytes(self) -> int:
        total = 0
        for store in (self._surfaces, self._glow):
            for surf in store.values():
                total += surf.get_stride() * surf.get_height()
        return total


def level_from_brightness(brightness: float) -> int:
    """Renderer-side helper: Draw.brightness is already quantised (C4)."""
    return max(0, min(BUCKETS - 1, int(round(brightness * (BUCKETS - 1)))))
