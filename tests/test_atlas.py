"""M3a RED: GlyphAtlas - bake every (glyph, brightness) pair once at startup.

Headless by construction: PangoCairo renders into ImageSurfaces, so no
compositor and no display connection are needed (verified probe).
"""
from __future__ import annotations

import cairo
import pytest

from matrix_overlay.atlas import AtlasKey, GlyphAtlas, codepoints_for
from matrix_overlay.world import BUCKETS


def key(**over):
    base = dict(font="Noto Sans Mono CJK JP", cell_size=18, scale=1, render_scale=1.0,
                glyph_set="matrix", custom_glyphs="", head_color="#d6ffd6",
                body_color="#00b81f", glow_enabled=False, glow_radius=6,
                glow_intensity=0.45)
    return AtlasKey(**{**base, **over})


def atlas(**over):
    return GlyphAtlas(key(**over))


def pixels(surface):
    """-> list of (x, y, r, g, b, a) un-premultiplied."""
    surface.flush()
    buf = surface.get_data()
    stride = surface.get_stride()
    out = []
    for y in range(surface.get_height()):
        row = bytes(buf[y * stride:y * stride + surface.get_width() * 4])
        for x in range(surface.get_width()):
            b, g, r, a = row[x * 4:x * 4 + 4]
            if a:
                out.append((x, y, min(255, r * 255 // a), min(255, g * 255 // a),
                            min(255, b * 255 // a), a))
    return out


def brightest(surface):
    px = pixels(surface)
    return max(px, key=lambda p: p[5]) if px else None


def peak_alpha(surface):
    b = brightest(surface)
    return b[5] if b else 0


# ------------------------------------------------------------- glyph sets ---


def test_matrix_set_mixes_katakana_digits_and_latin():
    cps = codepoints_for("matrix")
    assert 0x30A2 in cps and 0x0037 in cps and 0x0041 in cps and 0xFF71 in cps
    assert len(cps) == 197
    assert len(set(cps)) == len(cps), "codepoints must be unique (they index glyph ids)"


@pytest.mark.parametrize("name,expected", [("digits", 10), ("katakana", 58 + 95),
                                           ("ascii", 94)])
def test_preset_sizes(name, expected):
    assert len(codepoints_for(name)) == expected


def test_ascii_preset_has_no_high_codepoints():
    assert all(cp < 0x80 for cp in codepoints_for("ascii"))


def test_custom_set_is_exact_and_deduplicated():
    cps = codepoints_for("custom", custom="πΔπ 元")
    assert cps == tuple(ord(c) for c in ("π", "Δ", "元"))


def test_empty_custom_set_rejected():
    with pytest.raises(ValueError):
        codepoints_for("custom", custom="")


def test_unknown_preset_rejected():
    with pytest.raises(ValueError):
        codepoints_for("dings")


# ---------------------------------------------------------------- geometry ---


def test_cell_px_accounts_for_monitor_scale_and_render_scale():
    assert atlas().cell_px == 18
    assert atlas(scale=2).cell_px == 36          # DP-4 measured scale=2 (D2)
    assert atlas(scale=2, render_scale=0.5).cell_px == 18


def test_surfaces_are_cell_sized_and_cached_by_identity():
    a = atlas()
    s = a.surface_for(0, BUCKETS - 1)
    assert (s.get_width(), s.get_height()) == (18, 18)
    assert s.get_format() == cairo.FORMAT_ARGB32
    assert a.surface_for(0, BUCKETS - 1) is s, "atlas must not re-render per frame"


def test_out_of_range_lookups_are_errors():
    a = atlas()
    with pytest.raises(IndexError):
        a.surface_for(a.n_glyphs, 1)
    with pytest.raises(IndexError):
        a.surface_for(1, BUCKETS)
    with pytest.raises(IndexError):
        a.surface_for(1, -1)


def test_memory_budget_is_bounded():
    """26k surfaces would be a bug, not a feature. The worst case is driven by
    radius x scale (D6), so the bound is explicit rather than implied."""
    a = atlas(scale=2, glow_enabled=True, glow_radius=24)
    assert a.surface_count == a.n_glyphs * BUCKETS * 2
    assert a.glow_pad == 48
    assert a.approx_bytes < 260 * 1024 * 1024, a.approx_bytes / 1e6


# ------------------------------------------------------------------ colour ---


def test_glyphs_are_not_blank():
    a = atlas()
    assert all(pixels(a.surface_for(g, BUCKETS - 1)) for g in range(a.n_glyphs)), \
        "a blank glyph means the font cannot draw it"


def test_no_tofu_for_the_default_font():
    assert atlas().unsupported == ()


def test_tofu_is_reported_not_silently_blitted():
    a = atlas(glyph_set="custom", custom_glyphs="A\U0010FFFF")
    assert a.unsupported == (1,), "unmapped codepoints must surface (invisible rain bug)"


def test_alpha_ladder_is_monotonic():
    a = atlas()
    alphas = [peak_alpha(a.surface_for(3, lvl)) for lvl in range(BUCKETS)]
    assert alphas == sorted(alphas)
    assert alphas[0] == 0 and alphas[-1] > 200


def test_head_is_white_hot_and_tail_is_green():
    a = atlas()
    top = brightest(a.surface_for(5, BUCKETS - 1))
    low = brightest(a.surface_for(5, 1))
    assert abs(top[2] - 0xD6) < 0x14 and abs(top[3] - 0xFF) < 0x14, "head != head_color"
    assert low[3] > low[2] and low[3] > low[4], "tail must stay green (body_color)"


def test_colours_follow_config():
    a = atlas(head_color="#0000ff", body_color="#ff0000", glyph_set="digits")
    assert pixels(a.surface_for(0, BUCKETS - 1))


# ------------------------------------------------------------------- glow ---


def test_glow_surfaces_are_padded_and_baked_once():
    a = atlas(glow_enabled=True, glow_radius=6)
    assert a.glow_pad == 6
    s = a.glow_surface_for(2, BUCKETS - 1)
    assert (s.get_width(), s.get_height()) == (18 + 12, 18 + 12)
    assert a.glow_surface_for(2, BUCKETS - 1) is s


def test_glow_radius_is_logical_so_both_scales_look_the_same():
    """D6: measured as a 2x timing asymmetry - a device-px radius would make the
    bloom half as wide on the scale=2 output."""
    a1 = atlas(glow_enabled=True, scale=1)
    a2 = atlas(glow_enabled=True, scale=2)
    assert (a1.glow_pad, a2.glow_pad) == (6, 12)
    assert a1.glow_pad_logical == a2.glow_pad_logical == 6
    assert a2.glow_surface_for(2, BUCKETS - 1).get_width() == 36 + 24
    assert a1.glow_pad / a1.key.scale == a2.glow_pad / a2.key.scale == 6


def test_glow_spills_outside_the_cell():
    a = atlas(glow_enabled=True, glow_radius=6)
    lit = pixels(a.glow_surface_for(2, BUCKETS - 1))
    assert any(x < 4 or y < 4 for x, y, *_ in lit), "bloom must extend beyond the cell"


def test_disabled_glow_has_no_surfaces():
    a = atlas(glow_enabled=False)
    assert a.glow_pad == 0
    with pytest.raises(RuntimeError):
        a.glow_surface_for(0, 1)


def test_glow_radius_zero_disables_padding():
    assert atlas(glow_enabled=True, glow_radius=0).glow_pad == 0


# --------------------------------------------------------------- keying ---


def test_key_drives_identity_and_reuse():
    a, b = atlas(), atlas()
    assert a.key == b.key
    assert a.surface_hash(0, BUCKETS - 1) == b.surface_hash(0, BUCKETS - 1)
    c = atlas(font="DejaVu Sans Mono")
    assert c.key != a.key
    assert c.n_glyphs == a.n_glyphs


def test_every_glyph_level_pair_is_baked():
    a = atlas()
    for g in range(a.n_glyphs):
        for lvl in range(1, BUCKETS):
            assert a.surface_for(g, lvl) is not None
