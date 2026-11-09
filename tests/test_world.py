# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""M2 RED: pure simulation. world.py must not import Gtk/Gdk (plan 1.5).

Encodes: D3 (continuous column grid + row-phase alignment across the seam),
C4 (fixed dt, derived brightness, per-cell dirty bound, determinism),
and the L1 live-params contract.
"""
from __future__ import annotations

import dataclasses

import pytest
from matrix_overlay.world import Geom, Params, World, WorldCanvas

# Both outputs measured at M0: DP-5 2560x1440+0+0 scale 1, DP-4 2560x1440+2560+0 scale 2.
GEOMS = (
    Geom("DP-5", 0, 0, 2560, 1440),
    Geom("DP-4", 2560, 0, 2560, 1440),
)


def canvas(cell=18):
    return WorldCanvas.from_geoms(GEOMS, cell_size=cell)


def params(**over):
    base = dict(fps=30, cell_size=18, fall_speed=480.0, fall_speed_jitter=0.0,
                stream_density=1.0, tail_length=14, fade_rate=1.6, mutate_rate=0.0,
                seed=42, n_glyphs=96)
    return Params(**{**base, **over})


def world(**over):
    p = over.pop("params", None) or params(**over)
    return World(canvas(p.cell_size), p)


def drawn(frame):
    return [c for out in frame.per_output for c in out.draw]


# ------------------------------------------------------------ canvas math ---


def test_canvas_is_the_bbox_of_enabled_monitors():
    cv = canvas()
    assert (cv.width, cv.height) == (5120, 1440)
    # floor, not ceil: the trailing partial column (centre off-screen) is dropped
    # so that the two crops partition the grid exactly - no gap, no overlap.
    assert cv.cols == 284 and cv.rows == 80
    assert len(cv.crops) == 2


def test_seam_has_no_dead_and_no_doubled_column():
    """D3(a): every global column resolves to exactly one output."""
    cv = canvas()
    resolved = [cv.local_of(gc, 0) for gc in range(cv.cols)]
    assert all(r is not None for r in resolved), "dead column at the seam"
    assert len({(r[0], r[1]) for r in resolved}) == cv.cols, "duplicated column"


def test_crop_roundtrip_is_identity():
    cv = canvas()
    for gc in range(cv.cols):
        idx, lc, lr = cv.local_of(gc, 3)
        crop = cv.crops[idx]
        assert crop.to_global(lc, lr) == (gc, 3)


def test_bbox_with_gap_and_vertical_offset_has_dead_space_only():
    cv = WorldCanvas.from_geoms(
        (Geom("A", 0, 0, 1920, 1080), Geom("B", 3840, 240, 1080, 1920)),
        cell_size=20,
    )
    assert cv.width == 4920 and cv.height == 2160
    assert {c.name for c in cv.crops} == {"A", "B"}
    assert cv.crops[1].row0 > 0, "B is 240px lower"


# ----------------------------------------------------------- determinism ---


def test_same_seed_is_bit_identical():
    a, b = world(), world()
    for _ in range(200):
        a.step()
        b.step()
    assert a.digest() == b.digest()


def test_different_seed_diverges():
    a, b = world(params=params(seed=1)), world(params=params(seed=2))
    for _ in range(30):
        a.step()
        b.step()
    assert a.digest() != b.digest()


def test_seed_zero_uses_entropy():
    a, b = world(params=params(seed=0)), world(params=params(seed=0))
    for _ in range(5):
        a.step()
        b.step()
    assert a.digest() != b.digest(), "seed 0 must mean 'random per launch' (plan 2.3)"


def test_first_frame_is_reproducible():
    assert dataclasses.asdict(world().step()) == dataclasses.asdict(world().step())


# ------------------------------------------------- D3: seam continuity ---


def global_cells(frame, cv):
    out = set()
    for of in frame.per_output:
        crop = next(c for c in cv.crops if c.name == of.name)
        out |= {crop.to_global(c.col, c.row) + (c.glyph,) for c in of.draw}
    return out


def test_column_behaviour_is_independent_of_which_monitor_owns_it():
    """D3: the world is keyed by global column, so reordering the crop
    partition (or plugging the monitors into the opposite ports) must not
    change a single cell. This is what 'no visible seam' actually means."""
    cv_a = canvas()
    cv_b = WorldCanvas.from_geoms(tuple(reversed(GEOMS)), cell_size=18)
    wa, wb = World(cv_a, params()), World(cv_b, params())
    for _ in range(60):
        fa, fb = wa.step(), wb.step()
        assert global_cells(fa, cv_a) == global_cells(fb, cv_b)
    assert wa.digest() == wb.digest()


def test_speed_distribution_is_continuous_across_the_seam():
    """Neighbours either side of the seam come from one distribution."""
    cv = canvas()
    w = World(cv, params(fall_speed_jitter=0.5))
    seam = max(c.col0 for c in cv.crops)
    speeds = {c: w.column(c).speed_rows_per_step for c in range(seam - 6, seam + 6)}
    base = 480.0 / 30 / 18
    assert all(base * 0.5 <= s <= base * 1.5 for s in speeds.values())
    assert len(set(round(s, 6) for s in speeds.values())) > 1, "jitter must vary speed"
    per_side = [{speeds[c] for c in range(seam - 6, seam)},
                {speeds[c] for c in range(seam, seam + 6)}]
    assert per_side[0] and per_side[1]


def test_neighbouring_columns_are_not_synchronised():
    cv = canvas()
    w = World(cv, params())
    for _ in range(40):
        w.step()
    a = {c.glyph for out in w.step().per_output for c in out.draw if out.name == "DP-5"}
    assert len(a) > 8, "one shared world must not reuse a single glyph per row band"


# ------------------------------------------------------------ motion math ---


def test_fixed_dt_makes_head_velocity_exact():
    p = params(fps=30, fall_speed=480.0, cell_size=18)
    w = world(params=p)
    before = w.column(0).head_row
    w.step()
    assert w.column(0).head_row - before == pytest.approx(480.0 / 30 / 18)


def test_tick_counter_advances_exactly_one_per_step():
    """Regression: the counter once stopped advancing while frames still rendered,
    which silently broke rate accounting and the guard's measurements."""
    w = world()
    assert w.tick == 0
    for expected in range(1, 6):
        frame = w.step()
        assert (w.tick, frame.tick) == (expected, expected)


def test_heads_enter_from_above_not_mid_screen():
    w = world()
    assert all(w.column(c).head_row <= 1.0 for c in range(canvas().cols))
    frame = w.step()
    assert all(c.row < params().tail_length + 2 for c in drawn(frame)), \
        "rain must build downwards from the top edge"


def test_inactive_columns_do_not_draw():
    w = world(params=params(stream_density=0.0))
    assert all(not out.draw for out in w.step().per_output)


def test_column_retires_then_reactivates():
    p = params(stream_density=1.0, tail_length=4, fall_speed=2000.0)
    w = world(params=p)
    retired, back = set(), set()
    probe = range(20)
    for _ in range(600):
        w.step()
        for c in probe:
            if not w.column(c).active:
                retired.add(c)
            elif c in retired:
                back.add(c)
    assert retired, "a fast world must fully retire its columns"
    assert back == retired, "retired columns must respawn"


# ----------------------------------------------------------- cell output ---


def test_clear_matches_occupancy_and_incremental_state_equals_resync():
    """The strong contract: applying clear+draw frame by frame must land on
    exactly the same screen as a full re-sync from the world. Catches ghosts
    (cleared too late) and holes (cleared when not on screen)."""
    w = world()
    screen = {"DP-5": set(), "DP-4": set()}
    for _ in range(80):
        for out in w.step().per_output:
            s = screen[out.name]
            cleared = {(p.col, p.row) for p in out.clear}
            assert cleared <= s, f"{out.name}: cleared a cell that was not on screen"
            s -= cleared
            s |= {(c.col, c.row) for c in out.draw}
    for of in w.render_state():
        assert {(c.col, c.row) for c in of.draw} == screen[of.name], \
            f"{of.name}: incremental dirty-cell state drifted from authoritative state"


def test_brightness_is_monotonic_behind_the_head():
    w = world()
    for _ in range(60):
        frame = w.step()
    for out in frame.per_output:
        by_col: dict[int, list] = {}
        for c in out.draw:
            by_col.setdefault(c.col, []).append(c)
        for col, cells in by_col.items():
            cells.sort(key=lambda c: c.row)  # downwards: tail ... head
            vals = [c.brightness for c in cells]
            assert vals == sorted(vals), f"col {col}: brighter below than above"
            heads = [c for c in cells if c.head]
            assert all(c.brightness == pytest.approx(1.0) for c in heads)


def test_glyphs_are_stable_without_churn():
    w = world(params=params(mutate_rate=0.0))
    seen: dict[tuple[str, int, int], int] = {}
    for _ in range(50):
        for out in w.step().per_output:
            for c in out.draw:
                key = (out.name, c.col, c.row)
                if key in seen:
                    assert seen[key] == c.glyph
                seen[key] = c.glyph


def test_churn_replaces_glyphs_in_place():
    w = world(params=params(mutate_rate=40.0))
    seen: dict[tuple[str, int, int], int] = {}
    changed = 0
    for _ in range(60):
        for out in w.step().per_output:
            for c in out.draw:
                key = (out.name, c.col, c.row)
                if key in seen and seen[key] != c.glyph:
                    changed += 1
                seen[key] = c.glyph
    assert changed > 20, "mutate_rate must flicker glyphs in place (the movie look)"


# ------------------------------------------------------- C4: work bound ---


def test_dirty_work_is_per_cell_not_per_pixel():
    """Steady-state work is bounded by cells, not by buffer pixels - which is what
    makes the measured scale=2 output (4x the pixels of DP-5) affordable (D2).

    Measured: ~5.4 redrawn cells per column per frame at cell 18 / 480 px/s /
    30 fps / 8 brightness buckets, i.e. bucketing cuts a 14-row tail to ~40%.
    Ceiling is the visible tail itself, so bound it at (tail + 3) per column.
    """
    w = world()
    p = params()
    for _ in range(60):  # warm up
        w.step()
    peak = 0
    for _ in range(40):
        frame = w.step()
        for out in frame.per_output:
            span = next(c.col_span for c in canvas().crops if c.name == out.name)
            peak = max(peak, (len(out.draw) + len(out.clear)) / span)
            assert len(out.draw) + len(out.clear) <= (p.tail_length + 3) * span, \
                "dirty-cell bound violated: full-frame work would pin the CPU"
    assert peak < p.tail_length, f"work grew past the tail: {peak:.2f} cells/column"


# ------------------------------------------------------------ L1 hot path ---


def test_apply_params_keeps_state_and_tick():
    w = world()
    for _ in range(50):
        w.step()
    tick, active = w.tick, sum(1 for c in range(canvas().cols) if w.column(c).active)
    w.apply_params(params(fall_speed=1200.0, stream_density=0.2))
    assert w.tick == tick
    assert sum(1 for c in range(canvas().cols) if w.column(c).active) >= 0
    assert w.params.fall_speed == 1200.0
    head = w.column(5).head_row
    w.step()
    assert w.column(5).head_row - head == pytest.approx(1200.0 / 30 / 18)


def test_independent_span_mode_is_explicitly_unimplemented():
    with pytest.raises(NotImplementedError):
        world(params=params(span_mode="independent"))


def test_world_has_no_gui_imports():
    """Plan 1.5: world.py stays import-free of the GUI stack (checked on the AST,
    not the text - the docstring legitimately names Gtk/Gdk/cairo)."""
    import ast
    import pathlib

    import matrix_overlay.world as m

    tree = ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & {"gi", "Gtk", "Gdk", "cairo", "Pango", "tkinter"}, roots
