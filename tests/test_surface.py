# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""M4a RED: layer-shell wiring and monitor selection.

The C5 call *order* is a correctness requirement on cosmic-comp: EXCLUSIVE
keyboard mode is only honoured for TOP/OVERLAY layers, and keyboard mode must
be committed before the surface is realised or Esc never arrives (T1).
Injected doubles keep this testable with no compositor at all.
"""
from __future__ import annotations

import pytest

from matrix_overlay.surface import (
    LayerShellConfigError,
    MonitorInfo,
    configure_layer_surface,
    select_monitors,
    unmatched_entries,
    veil_css,
    window_title,
)


class Enum:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeLayerShell:
    """Records the exact call sequence; mimics the GIR surface we use."""

    def __init__(self, *, protocol=4, omit=()):
        self.calls: list[tuple] = []
        self.Layer = Enum(BACKGROUND=0, BOTTOM=1, TOP=2, OVERLAY=3)
        self.Edge = Enum(LEFT=0, RIGHT=1, TOP=2, BOTTOM=3)
        self.KeyboardMode = Enum(NONE=0, EXCLUSIVE=1, ON_DEMAND=2)
        self._protocol = protocol
        self._omit = set(omit)

    def _rec(self, name):
        def fn(*a):
            self.calls.append((name, a))
        return fn

    def __getattr__(self, name):
        if name in self._omit:
            raise AttributeError(name)
        if name.startswith(("init_for", "set_", "auto_", "get_", "is_")):
            if name == "get_protocol_version":
                return lambda: self._protocol
            return self._rec(name)
        raise AttributeError(name)

    @property
    def sequence(self):
        return [n for n, _a in self.calls]

    def args(self, name):
        return [a for n, a in self.calls if n == name]


class FakeDisplay:
    def __init__(self, infos):
        self._infos = infos

    def get_n_monitors(self):
        return len(self._infos)

    def get_monitor(self, i):
        return self._infos[i].monitor


class FakeMonitor:
    def __init__(self, index, model, x, y, w, h, scale=1):
        self.index = index
        self._model = model
        self._geo = Enum(x=x, y=y, width=w, height=h)
        self._scale = scale

    def get_geometry(self):
        return self._geo

    def get_scale_factor(self):
        return self._scale

    def get_model(self):
        return self._model


def infos():
    return [
        MonitorInfo(index=0, model="DELL S2722DGM", x=0, y=0, width=2560, height=1440,
                    scale=1, monitor=FakeMonitor(0, "DELL S2722DGM", 0, 0, 2560, 1440, 1)),
        MonitorInfo(index=1, model="DELL S2817Q", x=2560, y=0, width=2560, height=1440,
                    scale=2, monitor=FakeMonitor(1, "DELL S2817Q", 2560, 0, 2560, 1440, 2)),
    ]


# ------------------------------------------------------------------ CSS ----


def test_veil_css_targets_only_our_window_name():
    css = veil_css(0.72)
    assert "#matrix-veil" in css
    assert "rgba(0, 0, 0, 0.72)" in css
    assert "*" not in css, "must not leak into other widgets in the session"


def test_veil_is_transparent_for_the_child_area():
    css = veil_css(0.72)
    assert "drawingarea" in css.lower() or "#matrix-canvas" in css
    assert "transparent" in css


@pytest.mark.parametrize("alpha,want", [(0.0, "0.0"), (1.0, "0.95"), (0.4, "0.4")])
def test_veil_alpha_is_clamped_to_the_schema_range(alpha, want):
    assert want in veil_css(alpha)


def test_window_title_carries_the_compositor_diagnostics():
    assert window_title("DP-5") == "matrix-overlay:DP-5"


# ------------------------------------------------------- layer shell order ---


def test_configure_uses_the_c5_sequence():
    ls, win, mon = FakeLayerShell(), object(), object()
    configure_layer_surface(ls, win, mon, mode="exclusive", namespace="matrix-overlay")
    seq = ls.sequence
    assert seq[0] == "init_for_window", "must precede realise"
    assert seq.index("set_layer") < seq.index("set_keyboard_mode")
    assert seq.index("set_monitor") < seq.index("set_keyboard_mode")
    assert seq.index("set_keyboard_mode") == len(seq) - 1, "last: before first commit"
    assert all(a[2] is True for a in ls.args("set_anchor")), "all edges anchored"
    assert len(ls.args("set_anchor")) == 4


def test_overlay_layer_is_mandatory_with_exclusive_keyboard():
    """D1.2a: EXCLUSIVE is only honoured on TOP/OVERLAY, so a BACKGROUND layer
    would silently kill the Escape contract."""
    ls = FakeLayerShell()
    configure_layer_surface(ls, object(), object(), mode="exclusive")
    assert ls.args("set_layer")[0][1] == ls.Layer.OVERLAY


def test_exclusive_zone_is_zero_and_auto_zone_is_off():
    ls = FakeLayerShell()
    configure_layer_surface(ls, object(), object(), mode="exclusive")
    assert ls.args("set_exclusive_zone")[0][1] == 0, "panels must not be resized"
    assert "auto_exclusive_zone_enable" not in ls.sequence


def test_namespace_is_set_for_compositor_debugging():
    ls = FakeLayerShell()
    configure_layer_surface(ls, object(), object(), mode="exclusive", namespace="matrix-overlay")
    assert ls.args("set_namespace")[0][1] == "matrix-overlay"


def test_on_demand_needs_protocol_version_4():
    """D1.4: ON_DEMAND is undefined below protocol 4 - refuse, do not silently
    degrade to NONE."""
    ls = FakeLayerShell(protocol=3)
    with pytest.raises(LayerShellConfigError):
        configure_layer_surface(ls, object(), object(), mode="on_demand")


def test_on_demand_allowed_at_protocol_4():
    ls = FakeLayerShell(protocol=4)
    configure_layer_surface(ls, object(), object(), mode="on_demand")
    assert ls.args("set_keyboard_mode")[0][1] == ls.KeyboardMode.ON_DEMAND


def test_unknown_mode_is_rejected():
    with pytest.raises(LayerShellConfigError):
        configure_layer_surface(FakeLayerShell(), object(), object(), mode="yolo")


def test_old_library_falls_back_to_deprecated_interactivity():
    """0.8.2 has set_keyboard_mode; the shim covers a downgrade (D1.4)."""
    ls = FakeLayerShell(omit=("set_keyboard_mode",))
    configure_layer_surface(ls, object(), object(), mode="exclusive")
    assert ls.args("set_keyboard_interactivity")[0][1] is True
    assert "set_keyboard_mode" not in ls.sequence


def test_mode_none_never_requests_keyboard_grab():
    ls = FakeLayerShell()
    configure_layer_surface(ls, object(), object(), mode="none")
    assert ls.args("set_keyboard_mode")[0][1] == ls.KeyboardMode.NONE


# ------------------------------------------------------------ monitor math ---


def test_monitor_info_is_logical_geometry_plus_scale():
    got = select_monitors(FakeDisplay(infos()), mode="all", listed=())
    assert [(m.index, m.x, m.width, m.scale) for m in got] == [(0, 0, 2560, 1),
                                                               (1, 2560, 2560, 2)]


def test_listed_matches_by_index_or_model_substring():
    """D5: GTK3 has no gdk_monitor_get_connector(); 'DP-5' can never match."""
    disp = FakeDisplay(infos())
    assert [m.index for m in select_monitors(disp, mode="listed", listed=("1",))] == [1]
    assert [m.index for m in select_monitors(disp, mode="listed", listed=("s2817q",))] == [1]
    assert [m.index for m in select_monitors(disp, mode="listed", listed=("DELL",))] == [0, 1]


def test_digit_entries_are_index_only_never_model_substrings():
    """Regression: '1' used to also match the model 'DELL S2817Q' by substring,
    silently selecting the wrong output."""
    mon0 = FakeMonitor(0, "MONITOR 1X", 0, 0, 1920, 1080, 1)
    mon1 = FakeMonitor(1, "OTHER PANEL", 1920, 0, 1920, 1080, 1)
    tricky = [
        MonitorInfo(index=0, model="MONITOR 1X", x=0, y=0, width=1920, height=1080,
                    scale=1, monitor=mon0),
        MonitorInfo(index=1, model="OTHER PANEL", x=1920, y=0, width=1920, height=1080,
                    scale=1, monitor=mon1),
    ]

    class Disp:
        def get_n_monitors(self):
            return len(tricky)

        def get_monitor(self, i):
            return tricky[i].monitor

    assert [m.index for m in select_monitors(Disp(), mode="listed", listed=("1",))] == [1]
    assert [m.index for m in select_monitors(Disp(), mode="listed", listed=("0",))] == [0]
    assert [m.index for m in select_monitors(Disp(), mode="listed", listed=("1x",))] == [0]
    assert unmatched_entries(Disp(), listed=("9",)) == ("9",)


def test_unmatched_listed_entries_are_reported_not_fatal():
    """D5: an entry like 'DP-5' can never match under GTK3 - say so loudly."""
    disp = FakeDisplay(infos())
    assert select_monitors(disp, mode="listed", listed=("DP-5",)) == []
    assert unmatched_entries(disp, listed=("DP-5",)) == ("DP-5",)
    assert unmatched_entries(disp, listed=("S2817Q", "0")) == ()


def test_geoms_are_derived_in_dependency_order():
    from matrix_overlay.world import WorldCanvas

    cv = WorldCanvas.from_geoms(
        [m.geom() for m in select_monitors(FakeDisplay(infos()), mode="all", listed=())],
        cell_size=18,
    )
    assert (cv.width, cv.height) == (5120, 1440)
    assert [c.name for c in cv.crops] == ["0:DELL S2722DGM", "1:DELL S2817Q"]
