"""M1 RED: config parsing, validation, clamping and hot-reload classification.

Contract sources: plan C3 (reload classes L1-L4, last-good on rejection,
config is read-only to the app), 2.3 schema, T7 (pointer_passthrough is a
protocol no-op), T8/D1.7 ([perf] guard), D1.4 (on_demand needs protocol >= 4).
"""
from __future__ import annotations

import pytest

from matrix_overlay.config import (
    ATLAS,
    LIVE,
    RESET,
    TOPOLOGY,
    Capabilities,
    ConfigStore,
    change_classes,
    load_default,
)


# ---------------------------------------------------------------- loading ---


def test_render_backend_exists_and_is_a_choice():
    """M6: two presentation backends coexist, with gl (native EGL) as the default."""
    from matrix_overlay.config import SPEC_BY_KEY

    spec = SPEC_BY_KEY["render.backend"]
    assert spec.choices == ("cairo", "gl")
    assert load_default().render.backend == "gl"


def test_missing_file_yields_defaults_without_warnings(mkstore, warnings_of):
    store = mkstore(exists=False)
    cfg = store.load()
    assert cfg.world.fps == 22
    assert cfg.look.dim_alpha == pytest.approx(0.72)
    assert warnings_of(store) == []


def test_defaults_match_approved_schema(mkstore):
    cfg = mkstore(exists=False).load()
    assert cfg.schema_version == 1
    assert (cfg.world.cell_size, cfg.world.tail_length) == (18, 14)
    assert cfg.world.start_delay_ms == 120
    assert cfg.look.glyph_set == "matrix"
    assert cfg.input.keyboard_mode == "exclusive"
    assert cfg.input.stop_key == "Escape"
    assert cfg.perf.guard is True
    # M2: "" means auto-detect. A hardcoded node is a widow-shaped default that
    # breaks every other host (grogu has only renderD128).
    assert cfg.render.render_node == ""


def test_auto_render_node_default_loads_without_warnings(mkstore, warnings_of):
    store = mkstore("[render]\nrender_node = \"\"\n")
    assert store.load().render.render_node == ""
    assert warnings_of(store) == []


def test_serialized_defaults_round_trip_without_warnings(mkstore, warnings_of):
    store = mkstore(load_default().as_toml())
    cfg = store.load()
    assert cfg.world.fps == 22
    assert cfg.render.render_node == ""
    assert warnings_of(store) == []


def test_malformed_toml_keeps_last_good(mkstore, warnings_of):
    store = mkstore('[world]\nfps = 24\n')
    good = store.load()
    assert good.world.fps == 24
    store.path.write_text("[world]\nfps = \n", encoding="utf-8")
    kept = store.load()
    assert kept == good
    assert store.rejected is True
    assert "config_rejected" in warnings_of(store)


# --------------------------------------------------------------- clamping ---


@pytest.mark.parametrize(
    "body, attr, expected, code",
    [
        ('[world]\nfps = 200\n', "world.fps", 60, "clamped"),
        ('[world]\nfps = 1\n', "world.fps", 10, "clamped"),
        ('[world]\ncell_size = 99\n', "world.cell_size", 32, "clamped"),
        ('[world]\ncell_size = 4\n', "world.cell_size", 10, "clamped"),
        ('[world]\nfall_speed = -5\n', "world.fall_speed", 60.0, "clamped"),
        ('[world]\ntail_length = 100\n', "world.tail_length", 40, "clamped"),
        ('[look]\ndim_alpha = 1.5\n', "look.dim_alpha", 0.95, "clamped"),
        ('[perf]\nframe_budget_ratio = 2.0\n', "perf.frame_budget_ratio", 0.95, "clamped"),
        ('[perf]\nmin_render_scale = 0.1\n', "perf.min_render_scale", 0.5, "clamped"),
        ('[world]\nfps = "thirty"\n', "world.fps", 22, "bad_type"),
        ('[look]\nhead_color = "chartreuse"\n', "look.head_color", "#d6ffd6", "bad_color"),
        ('[look]\nglyph_set = "nope"\n', "look.glyph_set", "matrix", "bad_choice"),
        ('[render]\nbackend = "vulkan"\n', "render.backend", "gl", "bad_choice"),
    ],
)
def test_out_of_range_is_clamped_with_warning(mkstore, warnings_of, body, attr, expected, code):
    store = mkstore(body)
    cfg = store.load()
    got = cfg
    for part in attr.split("."):
        got = getattr(got, part)
    assert got == expected
    assert code in warnings_of(store)


def test_unknown_keys_are_ignored_not_fatal(mkstore, warnings_of):
    store = mkstore("[world]\nfps = 24\nbogus_key = 1\n\n[nonsense]\nx = 2\n")
    cfg = store.load()
    assert cfg.world.fps == 24
    assert "unknown_key" in warnings_of(store)


# ------------------------------------------------------- schema versioning ---


def test_absent_schema_version_is_treated_as_one(mkstore):
    assert mkstore("[world]\nfps = 45\n").load().schema_version == 1


def test_newer_schema_version_rejects_file_values(mkstore, warnings_of):
    store = mkstore('schema_version = 99\n[world]\nfps = 55\n')
    cfg = store.load()
    assert cfg.world.fps == 22, "must not consume keys from an unknown schema"
    assert "schema_version_unsupported" in warnings_of(store)


# ------------------------------------------------------------ T7 / D1.4 ---


def test_pointer_passthrough_is_a_noop_and_forces_click_stop(mkstore, warnings_of):
    store = mkstore("[input]\npointer_passthrough = true\npointer_click_action = \"ignore\"\n")
    cfg = store.load()
    assert cfg.input.pointer_click_action == "stop", "must never trap the user (T7)"
    assert cfg.input.pointer_passthrough is False
    assert "unsupported_key" in warnings_of(store)


def test_on_demand_requires_protocol_4(tmp_path, mkstore, warnings_of):
    from matrix_overlay.config import ConfigStore

    body = "[input]\nkeyboard_mode = \"on_demand\"\n"
    caps_v3 = Capabilities(True, 3, (0, 8, 2), (1,))
    store = ConfigStore(tmp_path / "config.toml", caps_v3)
    store.path.write_text(body, encoding="utf-8")
    cfg = store.load()
    assert cfg.input.keyboard_mode == "exclusive"
    assert "needs_protocol_4" in warnings_of(store)
    assert mkstore(body).load().input.keyboard_mode == "on_demand"  # caps v4


def test_on_demand_allowed_on_protocol_4(mkstore):
    assert mkstore("[input]\nkeyboard_mode = \"on_demand\"\n").load().input.keyboard_mode == "on_demand"


def test_custom_glyph_set_without_glyphs_falls_back(mkstore, warnings_of):
    store = mkstore("[look]\nglyph_set = \"custom\"\ncustom_glyphs = \"\"\n")
    assert store.load().look.glyph_set == "matrix"
    assert "bad_choice" in warnings_of(store)


def test_listed_outputs_empty_falls_back_to_all(mkstore, warnings_of):
    store = mkstore("[outputs]\nmode = \"listed\"\nlisted = []\n")
    cfg = store.load()
    assert cfg.outputs.mode == "all"
    assert "bad_value" in warnings_of(store)


def test_unknown_render_node_warns_but_loads(mkstore, warnings_of):
    store = mkstore("[render]\nrender_node = \"/dev/dri/renderD999\"\n")
    assert store.load().render.render_node == "/dev/dri/renderD999"
    assert "unknown_render_node" in warnings_of(store)


# ------------------------------------------------ reload classification ---


def _patch(cfg, **overrides):
    from matrix_overlay.config import replace_config

    return replace_config(cfg, **overrides)


def test_from_flat_rejects_unknown_keys():
    """A silently ignored override is worse than a crash: it looks like a pass."""
    from matrix_overlay.config import from_flat, load_default, to_flat

    flat = to_flat(load_default())
    flat["world.fpsx"] = 24
    with pytest.raises(KeyError):
        from_flat(flat)


def test_bare_leaf_names_are_qualified_when_unambiguous():
    from matrix_overlay.config import load_default, replace_config

    cfg = replace_config(load_default(), **{"render_node": "/dev/dri/renderD128"})
    assert cfg.render.render_node == "/dev/dri/renderD128"


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"world.fps": 24}, {LIVE}),
        ({"look.dim_alpha": 0.5}, {LIVE}),
        ({"perf.guard": False}, {LIVE}),
        ({"world.cell_size": 22}, {ATLAS}),
        ({"look.font": "DejaVu Sans Mono"}, {ATLAS}),
        ({"look.glow.radius": 12}, {ATLAS}),
        # the painter is chosen per run, but a backend swap must recreate the layer
        # surfaces (a GL surface is not a GTK widget), so it is L2, not L1
        ({"render.backend": "cairo"}, {ATLAS}),
        ({"outputs.listed": ["S2722DGM"]}, {TOPOLOGY}),
        ({"outputs.mode": "listed"}, {TOPOLOGY}),
        ({"world.seed": 7}, {RESET}),
        ({"outputs.span_mode": "independent"}, {RESET}),
    ],
)
def test_single_key_classifies(mkstore, override, expected):
    old = mkstore(exists=False).load()
    new = _patch(old, **override)
    assert set(change_classes(old, new)) == expected


def test_mixed_change_union_and_dominant_class(mkstore):
    old = mkstore(exists=False).load()
    new = _patch(old, **{"world.fps": 24, "world.cell_size": 20, "outputs.mode": "listed"})
    classes = set(change_classes(old, new))
    assert classes == {LIVE, ATLAS, TOPOLOGY}
    from matrix_overlay.config import dominant_class

    assert dominant_class(classes) == TOPOLOGY


def test_no_change_classifies_empty(mkstore):
    old = mkstore(exists=False).load()
    assert set(change_classes(old, old)) == set()
