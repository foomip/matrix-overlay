# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""M6-N3 RED: ctypes binding layer for libmwl.so.

Nothing here needs a compositor: dlopen + symbol wiring + event parsing + the
build-on-demand policy are all testable headless. The display-touching behaviour
lives in the live smoke test instead.
"""
from __future__ import annotations

import os
import time

import pytest

from matrix_overlay import mwl as mwlmod
from matrix_overlay.mwl import (ESCAPE_KEYSYM, Event, Mwl, MwlError, ensure_library,
                                library_path, parse_event)


# ------------------------------------------------------------------ events --

@pytest.mark.parametrize("line, want", [
    ("configure 0 2560 1440", Event("configure", (0, 2560, 1440))),
    ("key 9", Event("key", (9,))),
    ("closed 1", Event("closed", (1,))),
    ("button 0 272", Event("button", (0, 272))),
    ("kb_enter 0", Event("kb_enter", (0,))),
    ("mods 0 0 16 0", Event("mods", (0, 0, 16, 0))),
    ("gl_error 1286", Event("gl_error", (1286,))),
    ("output_removed 42", Event("output_removed", (42,))),
])
def test_parse_event_reads_kind_and_integers(line, want):
    assert parse_event(line) == want


def test_parse_event_keeps_the_text_after_the_integers():
    ev = parse_event("display_error 3 role")
    assert (ev.kind, ev.args, ev.text) == ("display_error", (3,), "role")


def test_parse_event_tolerates_blank_and_unknown():
    assert parse_event("") == Event("empty")
    assert parse_event("   ") == Event("empty")
    assert parse_event("future_event").args == ()


def test_event_sid_is_the_first_argument():
    assert Event("closed", (7,)).sid == 7
    assert Event("key", (9,)).sid == -1, "no surface id on a key event"


# --------------------------------------------------------------- library --

def test_library_path_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(mwlmod.ENV_LIB, str(tmp_path / "custom.so"))
    assert library_path() == tmp_path / "custom.so"


def test_library_path_defaults_inside_the_app_tree(tmp_path, monkeypatch):
    monkeypatch.delenv(mwlmod.ENV_LIB, raising=False)
    assert library_path().parent.name == "native"


def test_ensure_library_refuses_to_invent_a_missing_file(tmp_path):
    with pytest.raises(MwlError, match="not found"):
        ensure_library(tmp_path / "nope.so", rebuild=False)


def test_ensure_library_rebuilds_when_the_source_is_newer(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    source = native / "mwl.c"
    source.write_text("int main(void){return 0;}\n", encoding="utf-8")
    lib = native / "libmwl.so"
    lib.write_bytes(b"stale")
    (native / "build.sh").write_text("#!/bin/sh\ntouch libmwl.so\necho built\n",
                                     encoding="utf-8")
    os.chmod(native / "build.sh", 0o755)
    old = time.time() - 60
    os.utime(lib, (old, old))          # lib older than source
    assert ensure_library(lib).exists()


def test_ensure_library_reports_compiler_output(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    (native / "mwl.c").write_text("broken\n", encoding="utf-8")
    lib = native / "libmwl.so"
    script = native / "build.sh"
    script.write_text("#!/bin/sh\necho 'mwl.c:1:1: error: broken' >&2\nexit 1\n",
                      encoding="utf-8")
    os.chmod(script, 0o755)
    with pytest.raises(MwlError, match="error: broken"):
        ensure_library(lib)


def test_ensure_library_is_a_noop_when_the_artifact_is_current(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    source = native / "mwl.c"
    source.write_text("ok\n", encoding="utf-8")
    lib = native / "libmwl.so"
    lib.write_bytes(b"current")
    future = time.time() + 60
    os.utime(lib, (future, future))
    assert ensure_library(lib).read_bytes() == b"current"


# ------------------------------------------------------------- handle --

def test_constructing_a_handle_does_not_touch_the_display(tmp_path):
    """dlopen must not connect to the compositor: unit tests stay headless."""
    with pytest.raises(MwlError):
        Mwl(tmp_path / "absent.so", rebuild=False)


@pytest.mark.skipif(not mwlmod.DEFAULT_LIB.exists(), reason="libmwl.so not built")
def test_real_library_wires_every_symbol_the_bindings_need():
    handle = Mwl(mwlmod.DEFAULT_LIB, rebuild=False)
    for fn in ("mwl_init", "mwl_shutdown", "mwl_error", "mwl_versions", "mwl_gl_info",
               "mwl_output_count", "mwl_output_info", "mwl_surface_create",
               "mwl_surface_make_current", "mwl_surface_size", "mwl_surface_scale",
               "mwl_surface_geom", "mwl_surface_swap", "mwl_surface_frame_ready",
               "mwl_surface_destroy", "mwl_tex_create", "mwl_tex_destroy",
               "mwl_clear", "mwl_draw_quads", "mwl_fd", "mwl_dispatch",
               "mwl_next_event", "mwl_display_error_code"):
        assert hasattr(handle._lib, fn), fn


@pytest.mark.skipif(not mwlmod.DEFAULT_LIB.exists(), reason="libmwl.so not built")
class FakeLib:
    """Stand-in for the ctypes handle: fills the out-params so the marshalling in
    Mwl.outputs() is covered without a compositor (a generator-exhaustion bug once
    made the real call pass 3 of 8 arguments - invisible while tests used a Python
    fake for the whole handle)."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def mwl_output_count(self):
        return len(self.records)

    def mwl_output_info(self, idx, x, y, w, h, scale, name, namelen):
        self.calls.append(idx)
        rec = self.records[idx]
        for ref, value in zip((x, y, w, h, scale), rec[:5]):
            ref._obj.value = value
        name.value = rec[5].encode()
        return 0


@pytest.mark.skipif(not mwlmod.DEFAULT_LIB.exists(), reason="libmwl.so not built")
def test_outputs_reads_every_out_parameter(monkeypatch):
    handle = Mwl(mwlmod.DEFAULT_LIB, rebuild=False)
    lib = FakeLib([(2560, 0, 2560, 1440, 2, "DP-4"), (0, 0, 2560, 1440, 1, "DP-5")])
    monkeypatch.setattr(handle, "_lib", lib)
    monkeypatch.setattr(handle, "_initialised", True)

    outs = handle.outputs()

    assert lib.calls == [0, 1], "every live output must be queried"
    assert [(o.index, o.name, o.x, o.scale) for o in outs] == [
        (0, "DP-4", 2560, 2), (1, "DP-5", 0, 1)]
    assert all(o.width == 2560 and o.height == 1440 for o in outs)


def test_outputs_before_init_is_a_clear_error():
    handle = Mwl(mwlmod.DEFAULT_LIB, rebuild=False)
    with pytest.raises(MwlError, match="before init"):
        handle.outputs()


@pytest.mark.skipif(not mwlmod.DEFAULT_LIB.exists(), reason="libmwl.so not built")
def test_shutdown_without_init_is_inert():
    Mwl(mwlmod.DEFAULT_LIB, rebuild=False).shutdown()   # must not raise


@pytest.mark.skipif(not mwlmod.DEFAULT_LIB.exists(), reason="libmwl.so not built")
def test_escape_keycode_maps_to_the_escape_keysym():
    """The stop key is configurable, so a real Gdk keymap is used when one is
    reachable; the fallback keeps the documented default working regardless."""
    handle = Mwl(mwlmod.DEFAULT_LIB, rebuild=False)
    assert handle.keycode_to_keysym(mwlmod.XKB_ESCAPE_KEYCODE) == ESCAPE_KEYSYM
