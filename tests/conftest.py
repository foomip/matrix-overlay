"""Shared test fixtures.

T14 guard: the asdf shims shadow python3/pytest on PATH (verified: `command -v
pytest` -> ~/.asdf/shims/pytest). That interpreter has no `gi`, so every
GTK-touching test would fail with a misleading ModuleNotFoundError. Run tests as
`/usr/bin/python3 -m pytest` only; bail out loudly if we are not on it.
"""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

EXPECTED_INTERPRETER = "/usr/bin/python3"

if not os.path.samefile(sys.executable, EXPECTED_INTERPRETER):
    pytest.exit(
        "tests must run under the system interpreter, which owns PyGObject.\n"
        f"  got: {sys.executable}\n"
        f"  use: {EXPECTED_INTERPRETER} -m pytest -q\n"
        "  (PATH python3/pytest resolve to ~/.asdf/shims, which have no `gi`)"
    )


@pytest.fixture
def caps():
    """Compositor capabilities as measured on this machine at M0."""
    from matrix_overlay.config import Capabilities

    return Capabilities(
        layer_shell_supported=True,
        protocol_version=4,
        lib_version=(0, 8, 2),
        monitor_scales=(1, 2),
    )


@pytest.fixture
def mkstore(tmp_path, caps):
    """Build a ConfigStore over an optional config.toml body."""

    def make(body=None, *, capabilities=None, exists=True):
        from matrix_overlay.config import ConfigStore

        path = tmp_path / "config.toml"
        if exists:
            path.write_text(textwrap.dedent(body or ""), encoding="utf-8")
        return ConfigStore(path, capabilities or caps)

    return make


@pytest.fixture
def warnings_of():
    """Normalise warning codes out of a store: 'code' or 'code:detail'."""

    def _of(store):
        return [w.split(":", 1)[0] for w in store.warnings]

    return _of
