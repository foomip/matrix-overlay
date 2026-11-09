"""M2 RED: backend-aware dependency gating.

Two facts were conflated. The GL backend is a native Wayland client: it needs
libmwl.so (or the toolchain to build it) and *nothing* from GObject
Introspection's GtkLayerShell. The cairo fallback is GTK3 + gtk-layer-shell and
does. Yet run() and the wrapper's preflight both required GtkLayerShell
unconditionally, so an AMD-only box without gir1.2-gtklayershell-0.1 died in the
shim (exit 3) before EGL was ever attempted - which is exactly the grogu failure,
and the log line blamed the GPU.

Everything decided here is pure Python so the decision table is testable; the
wrapper just execs `--preflight`.
"""
from __future__ import annotations

import pytest

from matrix_overlay import cli
from matrix_overlay.config import Capabilities


# --------------------------------------------------- what each backend needs ---


def test_gl_backend_does_not_require_gtk_layer_shell():
    assert "GtkLayerShell" not in cli.required_typelibs("gl", gl_available=True)
    assert "Gtk" in cli.required_typelibs("gl", gl_available=True)


def test_cairo_backend_requires_gtk_layer_shell():
    assert "GtkLayerShell" in cli.required_typelibs("cairo", gl_available=True)


def test_gl_failure_pulls_the_layer_shell_requirement_back_in():
    """The fallback ladder is the reason the requirement is conditional, not the
    backend name alone: if GL is unusable, layer-shell is mandatory again."""
    assert "GtkLayerShell" in cli.required_typelibs("gl", gl_available=False)


# ------------------------------------------------------------ native caps ---


VERSIONS = "layer_shell=4 compositor=4 seat=5 xdg_output=yes outputs=2"


def test_parse_versions_reads_the_native_string():
    parsed = cli.parse_versions(VERSIONS)
    assert parsed["layer_shell"] == "4"
    assert parsed["xdg_output"] == "yes"


def test_caps_from_native_does_not_claim_a_gtk_layer_shell_version():
    caps = cli.caps_from_native(VERSIONS, (1, 2))
    assert caps.layer_shell_supported is True
    assert caps.protocol_version == 4
    assert caps.monitor_scales == (1, 2)
    assert caps.lib_version == cli.NATIVE_LIB


def test_native_caps_cannot_trip_the_gtk_layer_shell_blocker(mkstore):
    """diagnose()'s keyboard-mode blocker is about libgtk-layer-shell, which the
    native path does not use."""
    caps = cli.caps_from_native(VERSIONS, (1,))
    out = cli.diagnose(mkstore("[input]\nkeyboard_mode = \"exclusive\"\n"), caps)
    assert not any("keyboard_mode" in b for b in out["blockers"])
    assert out["supported"] is True


def test_native_caps_without_layer_shell_are_a_blocker():
    caps = cli.caps_from_native("layer_shell=0 compositor=4 seat=5", (1,))
    assert caps.layer_shell_supported is False
    assert caps.protocol_version == 0


# --------------------------------------------------------- build blockers ---


def test_gl_is_usable_when_the_library_is_already_built(tmp_path, monkeypatch):
    from matrix_overlay import mwl as mwl_mod

    lib = tmp_path / "libmwl.so"
    lib.write_bytes(b"\x7fELF")
    monkeypatch.setattr(mwl_mod, "library_path", lambda *a, **k: lib)
    ok, why = cli.gl_usable()
    assert ok is True
    assert "built" in why


def test_missing_headers_are_reported_as_a_build_blocker(monkeypatch):
    """The exact grogu state: gcc and wayland-scanner exist, EGL/GLES headers do
    not, so native/build.sh cannot compile anything."""
    monkeypatch.setattr(cli, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli, "HEADER_PATHS", ("/nope/EGL/egl.h", "/nope/GLES2/gl2.h"))
    monkeypatch.setattr(cli, "PROTOCOL_XMLS", ())
    from matrix_overlay import mwl as mwl_mod

    monkeypatch.setattr(mwl_mod, "library_path",
                        lambda *a, **k: __import__("pathlib").Path("/nope/libmwl.so"))
    ok, why = cli.gl_usable()
    assert ok is False
    assert "EGL/egl.h" in why and "GLES2/gl2.h" in why


def test_missing_compiler_is_reported(monkeypatch):
    monkeypatch.setattr(cli, "which", lambda name: None)
    monkeypatch.setattr(cli, "HEADER_PATHS", ())
    monkeypatch.setattr(cli, "PROTOCOL_XMLS", ())
    from matrix_overlay import mwl as mwl_mod

    monkeypatch.setattr(mwl_mod, "library_path",
                        lambda *a, **k: __import__("pathlib").Path("/nope/libmwl.so"))
    ok, why = cli.gl_usable()
    assert ok is False
    assert "gcc" in why and "wayland-scanner" in why


# ------------------------------------------------------------ preflight ---


@pytest.fixture
def probes(monkeypatch):
    """Drive gl_usable / _typelib_ok from the test."""
    def setup(*, gl=(True, "built"), typelibs=None):
        monkeypatch.setattr(cli, "gl_usable", lambda: gl)
        libs = typelibs if typelibs is not None else {}

        def fake(ns, version):
            return libs.get(ns, True)

        monkeypatch.setattr(cli, "_typelib_ok", fake)
        return fake
    return setup


def preflight_rc(opts=None):
    return cli.preflight(cli.Options() if opts is None else opts)


def test_preflight_passes_for_gl_without_touching_layer_shell(probes, capsys):
    probes(gl=(True, "built"), typelibs={"GtkLayerShell": False})
    assert preflight_rc() == cli.EXIT_OK
    assert "GtkLayerShell" not in capsys.readouterr().out


def test_preflight_falls_through_to_cairo_when_gl_cannot_build(probes):
    probes(gl=(False, "missing headers: EGL/egl.h"),
           typelibs={"GtkLayerShell": True})
    assert preflight_rc() == cli.EXIT_OK


def test_preflight_names_both_backends_when_nothing_can_run(probes, capsys):
    probes(gl=(False, "missing headers: EGL/egl.h"),
           typelibs={"GtkLayerShell": False})
    assert preflight_rc() == cli.EXIT_NO_LAYER_SHELL
    err = capsys.readouterr().err
    assert "gl:missing headers" in err
    assert "cairo" in err and "gir1.2-gtklayershell-0.1" in err


def test_preflight_mode_is_parseable():
    assert cli.parse_args(["--preflight"]).mode == "preflight"


NATIVE_REPORT = {"library": "/tmp/libmwl.so", "built": True, "versions": VERSIONS,
                 "outputs": [{"index": 0, "name": "HDMI-A-1", "x": 0, "y": 0,
                              "width": 1920, "height": 1080, "scale": 1}],
                 "gl": "GL_RENDERER=Radeon 760M (radeonsi, gfx1103, ACO)"}


def test_diagnose_mode_works_without_gtk_layer_shell_on_the_gl_path(monkeypatch,
                                                                    capsys):
    """--diagnose on grogu (no gir1.2-gtklayershell-0.1) must still report the
    native stack instead of dying in the GTK import."""
    monkeypatch.setattr(cli, "gl_usable", lambda: (True, "built"))
    monkeypatch.setattr(cli, "gl_status", lambda: dict(NATIVE_REPORT))

    def boom(*args, **kwargs):
        raise AssertionError("the gl path must not import GtkLayerShell")

    monkeypatch.setattr(cli, "init_gtk", boom)
    rc = cli.main(["--diagnose"])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "Radeon 760M" in out          # the native GL probe answered
    assert '"supported": true' in out    # from the native layer-shell version
