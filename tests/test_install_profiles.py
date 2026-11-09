"""M4 RED: install profiles and path resolution.

The app must work from three roots without any code branch:
  dotfile  $HOME/.local/share/matrix-overlay      (widow, grogu today)
  system   /usr/lib/matrix-overlay + /usr/bin     (the deb profile, M5)
  flatpak  /app/lib/matrix-overlay                (frozen profile)
so no path may be hardcoded, the config chain must have a system-wide layer, and
the native library must be findable when the install tree is read-only.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from matrix_overlay import cli, config as config_mod, mwl as mwl_mod

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ config chain ---


def test_explicit_flag_beats_the_env_and_both_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MATRIX_OVERLAY_CONFIG", str(tmp_path / "env.toml"))
    monkeypatch.setattr(cli, "SYSTEM_CONFIG", tmp_path / "etc.toml")
    opts = cli.parse_args(["--config", str(tmp_path / "flag.toml")])
    assert cli.config_path(opts) == tmp_path / "flag.toml"


def test_env_config_beats_user_and_system(tmp_path, monkeypatch):
    user = tmp_path / "user/.config/matrix-overlay/config.toml"
    user.parent.mkdir(parents=True)
    user.write_text("schema_version = 1\n", encoding="utf-8")
    system = tmp_path / "etc/matrix-overlay/config.toml"
    system.parent.mkdir(parents=True)
    system.write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user/.config"))
    monkeypatch.setattr(cli, "SYSTEM_CONFIG", system)
    monkeypatch.setenv("MATRIX_OVERLAY_CONFIG", str(tmp_path / "env.toml"))
    assert cli.config_path(cli.Options()) == tmp_path / "env.toml"


def test_user_config_wins_over_the_system_one(tmp_path, monkeypatch):
    user = tmp_path / "xdg/matrix-overlay/config.toml"
    user.parent.mkdir(parents=True)
    user.write_text("schema_version = 1\n", encoding="utf-8")
    system = tmp_path / "etc/matrix-overlay/config.toml"
    system.parent.mkdir(parents=True)
    system.write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(cli, "SYSTEM_CONFIG", system)
    assert cli.config_path(cli.Options()) == user


def test_the_system_config_is_used_when_the_user_has_none(tmp_path, monkeypatch):
    """The distro layer: /etc/matrix-overlay/config.toml is what a package ships,
    and it must apply when the user never edited anything."""
    system = tmp_path / "etc/matrix-overlay/config.toml"
    system.parent.mkdir(parents=True)
    system.write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-absent"))
    monkeypatch.setattr(cli, "SYSTEM_CONFIG", system)
    assert cli.config_path(cli.Options()) == system


def test_no_config_anywhere_points_at_the_user_path(tmp_path, monkeypatch):
    """Defaults stay defaults: the user path is returned, and its absence means
    SPEC is authoritative (no implicit empty file)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(cli, "SYSTEM_CONFIG", tmp_path / "etc-absent.toml")
    assert cli.config_path(cli.Options()) == tmp_path / "xdg/matrix-overlay/config.toml"


def test_xdg_config_home_without_a_trailing_slash_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cli.user_config_path() == tmp_path / "xdg/matrix-overlay/config.toml"


def test_home_is_the_fallback_for_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.user_config_path() == tmp_path / ".config/matrix-overlay/config.toml"


# --------------------------------------------------------------- state dir ---


def test_state_dir_follows_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.state_dir() == tmp_path / "state/matrix-overlay"


def test_state_dir_falls_back_to_home_local_state(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.state_dir() == tmp_path / ".local/state/matrix-overlay"


# ------------------------------------------------- native library ladder -----


def test_prebuilt_library_in_the_tree_wins(tmp_path, monkeypatch):
    """The deb profile ships libmwl.so next to the sources: no compiler needed,
    and nothing is ever written into /usr."""
    monkeypatch.delenv(mwl_mod.ENV_LIB, raising=False)
    tree = tmp_path / "usr/lib/matrix-overlay"
    native = tree / "native"
    native.mkdir(parents=True)
    (native / "libmwl.so").write_bytes(b"\x7fELF")
    found = mwl_mod.candidate_library(tree / "native" / "libmwl.so",
                                      state_dir=tmp_path / "state/matrix-overlay")
    assert found == native / "libmwl.so"


def test_a_read_only_tree_falls_back_to_a_writable_state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(mwl_mod.ENV_LIB, raising=False)
    ro = tmp_path / "usr/lib/matrix-overlay/native"
    ro.mkdir(parents=True)
    (ro / "mwl.c").write_text("int x;\n", encoding="utf-8")
    os.chmod(ro, 0o555)
    try:
        found = mwl_mod.candidate_library(ro / "libmwl.so",
                                          state_dir=tmp_path / "state/matrix-overlay")
    finally:
        os.chmod(ro, 0o755)
    assert found == tmp_path / "state/matrix-overlay/native/libmwl.so"


def test_sources_are_copied_into_the_writable_dir_and_built_there(tmp_path,
                                                                  monkeypatch):
    """build.sh compiles next to itself, so a read-only install tree needs the
    sources staged in the writable location first."""
    ro = tmp_path / "usr/lib/matrix-overlay/native"
    ro.mkdir(parents=True)
    (ro / "mwl.c").write_text("int x;\n", encoding="utf-8")
    (ro / "mwl.h").write_text("int y;\n", encoding="utf-8")
    (ro / "build.sh").write_text("#!/bin/sh\ntouch libmwl.so\n", encoding="utf-8")
    os.chmod(ro / "build.sh", 0o755)
    os.chmod(ro, 0o555)
    try:
        native = mwl_mod.stage_build_dir(ro, state_dir=tmp_path / "state/matrix-overlay")
    finally:
        os.chmod(ro, 0o755)
    assert native == tmp_path / "state/matrix-overlay/native"
    assert (native / "mwl.c").exists() and (native / "build.sh").exists()


def test_ensure_library_stages_from_the_install_tree_when_it_is_read_only(tmp_path,
                                                                          monkeypatch):
    """End to end: a packaged tree with no prebuilt .so and no write permission
    must still build - into the state dir, from a copy of the install sources."""
    ro = tmp_path / "usr/lib/matrix-overlay/native"
    ro.mkdir(parents=True)
    (ro / "mwl.c").write_text("int x;\n", encoding="utf-8")
    (ro / "build.sh").write_text("#!/bin/sh\ntouch libmwl.so\n", encoding="utf-8")
    os.chmod(ro / "build.sh", 0o755)
    monkeypatch.setattr(mwl_mod, "DEFAULT_LIB", ro / "libmwl.so")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    os.chmod(ro, 0o555)
    try:
        lib = mwl_mod.ensure_library()
    finally:
        os.chmod(ro, 0o755)
    staged = tmp_path / "state/matrix-overlay/native"
    assert lib == staged / "libmwl.so" and lib.exists()
    assert (staged / "mwl.c").exists()
    assert mwl_mod.is_stale(lib, staged) is False      # stamped: no rebuild loop


def test_a_stamp_stops_a_rebuild_loop_for_copied_sources(tmp_path):
    """Copied sources get a fresh mtime, so an mtime-only rule would rebuild on
    every launch forever. With a content stamp, identical sources are current."""
    native = tmp_path / "native"
    native.mkdir()
    (native / "mwl.c").write_text("int x;\n", encoding="utf-8")
    lib = native / "libmwl.so"
    lib.write_bytes(b"built")
    mwl_mod.write_stamp(native, lib)
    old = 1000000000.0
    os.utime(lib, (old, old))
    (native / "mwl.c").touch()             # newer than the .so, same content
    assert mwl_mod.is_stale(lib, native) is False


def test_a_changed_source_is_stale_despite_the_stamp(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    (native / "mwl.c").write_text("int x;\n", encoding="utf-8")
    lib = native / "libmwl.so"
    lib.write_bytes(b"built")
    mwl_mod.write_stamp(native, lib)
    (native / "mwl.c").write_text("int x; int z;\n", encoding="utf-8")
    assert mwl_mod.is_stale(lib, native) is True


# ------------------------------------------------------------- installer -----


def run_installer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(REPO / "install.sh"), *args],
                          capture_output=True, text=True)


def test_install_dotfile_lays_out_the_expected_tree(tmp_path):
    proc = run_installer("--mode", "dotfile", "--home", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    app = tmp_path / ".local/share/matrix-overlay"
    assert (app / "matrix_overlay/cli.py").exists()
    assert (app / "bin/matrix-rain").exists()
    shim = tmp_path / ".local/bin/matrix-rain"
    assert shim.exists() and os.access(shim, os.X_OK)
    # build products and VCS noise never travel to the install:
    assert not (app / ".git").exists()
    assert not (app / "matrix_overlay/__pycache__").exists()


def test_uninstall_dotfile_removes_both_pieces(tmp_path):
    assert run_installer("--mode", "dotfile", "--home", str(tmp_path)).returncode == 0
    proc = run_installer("--mode", "dotfile", "--home", str(tmp_path), "--uninstall")
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / ".local/share/matrix-overlay").exists()
    assert not (tmp_path / ".local/bin/matrix-rain").exists()


def test_install_system_lays_out_the_fhs_tree_under_a_destdir(tmp_path):
    root = tmp_path / "root"
    proc = run_installer("--mode", "system", "--destdir", str(root))
    assert proc.returncode == 0, proc.stderr
    assert (root / "usr/bin/matrix-rain").exists()
    assert (root / "usr/lib/matrix-overlay/matrix_overlay/cli.py").exists()
    assert (root / "usr/lib/matrix-overlay/native/mwl.c").exists()
    etc = root / "etc/matrix-overlay/config.toml"
    assert etc.exists() and "schema_version" in etc.read_text(encoding="utf-8")


def test_the_fhs_tree_is_importable_from_its_own_root(tmp_path):
    """Proves the package is not secretly tied to $HOME: the installed layout
    answers --print-default-config when run from /usr/lib/... (here: a destdir)."""
    root = tmp_path / "root"
    assert run_installer("--mode", "system", "--destdir", str(root)).returncode == 0
    env = dict(os.environ, PYTHONPATH=str(root / "usr/lib/matrix-overlay"))
    proc = subprocess.run([sys.executable, "-m", "matrix_overlay",
                           "--print-default-config"], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "render_node" in proc.stdout


def test_no_code_hardcodes_the_dotfile_install_root():
    """The root is derived from __file__; a literal '.local/share' in the code is
    how a dotfile install leaks into the system profile."""
    offenders = []
    for path in (Path(__file__).resolve().parent.parent / "matrix_overlay").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".local/share/matrix-overlay" in text:
            offenders.append(path.name)
    assert offenders == []


def test_self_test_names_the_missing_optional_dependency(tmp_path, monkeypatch, capsys):
    """The cairo self-test is the headless render proof; without python3-gi-cairo
    it must exit with the diagnose code and say what to install, not traceback."""
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "cairo", None)
    rc = cli.self_test(cli.Options())
    assert rc == cli.EXIT_DIAGNOSE
    err = capsys.readouterr().err
    assert "python3-gi-cairo" in err and "preflight" in err


def test_config_module_default_matches_the_new_chain(tmp_path, monkeypatch):
    """SPEC is still the single source of defaults (plan C3): the chain picks a
    file, it does not invent a second defaults mechanism."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(cli, "SYSTEM_CONFIG", tmp_path / "absent.toml")
    path = cli.config_path(cli.Options())
    store = config_mod.ConfigStore(path, cli._neutral_caps())
    assert store.load() == config_mod.load_default()
