# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""Shared test fixtures.

T14 guard: the asdf shims shadow python3/pytest on PATH (verified: `command -v
pytest` -> ~/.asdf/shims/pytest). That interpreter has no `gi`, so every
GTK-touching test would fail with a misleading ModuleNotFoundError. Run tests as
`/usr/bin/python3 -m pytest` only; bail out loudly if we are not on it.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import textwrap
from pathlib import Path

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


# ------------------------------------------------- fake sysfs GPU topologies ---
# The render-node tests must not depend on the machine running them (the M0
# tests pinned widow's real /sys and could not pass anywhere else). Shapes are
# mirrored from real measurements:
#   widow  renderD129 -> 0000:04:00.0 vendor 0x10de, card0 DP-5/DP-6 connected
#          renderD128 -> 0000:11:00.0 vendor 0x1002, card1 nothing connected
#   grogu  renderD128 -> 0000:66:00.0 vendor 0x1002, card0 HDMI-A-1 connected


@dataclasses.dataclass(frozen=True)
class SysfsCard:
    """One DRM card as sysfs shows it: card dir, PCI function, render nodes,
    connectors and VRAM (mem_info_vram_total, bytes)."""

    name: str                                # "card0"
    pci: str                                 # "0000:04:00.0"
    vendor: str                              # "0x10de"
    renders: tuple[int, ...]                 # (129,) -> renderD129
    connectors: tuple[tuple[str, str], ...]  # (("DP-5", "connected"), ...)
    vram_total: int = 0


NVIDIA_PCI = "0000:04:00.0"
AMD_PCI = "0000:11:00.0"
GROGU_PCI = "0000:66:00.0"

WIDOW_TOPOLOGY = (
    SysfsCard("card0", NVIDIA_PCI, "0x10de", (129,),
              (("DP-4", "disconnected"), ("DP-5", "connected"),
               ("DP-6", "connected"), ("HDMI-A-2", "disconnected")),
              0),                             # no mem_info_* from the nvidia driver
    SysfsCard("card1", AMD_PCI, "0x1002", (128,),
              (("DP-1", "disconnected"), ("HDMI-A-1", "disconnected"),
               ("Writeback-1", "unknown")), 25_753_026_560),   # measured value
)

GROGU_TOPOLOGY = (
    SysfsCard("card0", GROGU_PCI, "0x1002", (128,), (("HDMI-A-1", "connected"),),
              536_870_912),   # iGPU: shared/visible aperture, order of magnitude only
)


def build_sysfs(root: Path, cards: tuple[SysfsCard, ...]):
    """Materialise a fake /sys/class/drm + /sys/bus/pci/devices tree."""
    from matrix_overlay.cli import Sysfs

    drm, pci = root / "drm", root / "pci"
    drm.mkdir(parents=True)
    pci.mkdir(parents=True)
    for card in cards:
        func = pci / card.pci
        func.mkdir()
        (func / "vendor").write_text(card.vendor, encoding="utf-8")
        (func / "class").write_text("0x030000", encoding="utf-8")
        card_dir = drm / card.name
        card_dir.mkdir()
        os.symlink(func, card_dir / "device")   # real sysfs: cardN/device -> func
        if card.vram_total > 0:
            # amdgpu exposes mem_info_vram_total on the PCI function; the NVIDIA
            # driver exposes no mem_info_* at all there (verified on widow), so
            # an absent file is the realistic NVIDIA shape, not a fixture gap.
            (func / "mem_info_vram_total").write_text(f"{card.vram_total}\n",
                                                     encoding="utf-8")
        for render in card.renders:
            node = drm / f"renderD{render}"
            node.mkdir()
            os.symlink(func, node / "device")
        for conn, status in card.connectors:
            conn_dir = drm / f"{card.name}-{conn}"
            conn_dir.mkdir()
            (conn_dir / "status").write_text(status + "\n", encoding="utf-8")
    return Sysfs(drm=drm, pci=pci)


@pytest.fixture
def mk_sysfs(tmp_path):
    """Unique fake-sysfs root per test: topologies never bleed into each other."""

    def make(name, cards=WIDOW_TOPOLOGY):
        return build_sysfs(tmp_path / name, cards)

    return make


@pytest.fixture
def glvnd(tmp_path, monkeypatch):
    """Choose which glvnd vendor json files exist (grogu has no 10_nvidia.json)."""

    def setup(*, nvidia=True, mesa=True):
        out = {}
        for kind, exists, name in (("nvidia", nvidia, "10_nvidia.json"),
                                   ("mesa", mesa, "50_mesa.json")):
            path = tmp_path / "glvnd" / name
            path.parent.mkdir(exist_ok=True)
            if exists:
                path.write_text("{}", encoding="utf-8")
            out[kind] = str(path)
        monkeypatch.setattr("matrix_overlay.cli.NVIDIA_JSON", out["nvidia"])
        monkeypatch.setattr("matrix_overlay.cli.MESA_JSON", out["mesa"])
        return out

    return setup
