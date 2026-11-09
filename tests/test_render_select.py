# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""M2 RED: render-node auto-detection and EGL vendor pinning, host-portable.

The original pinning (plan 1.1, measured at M0) was correct but *widow-shaped*:
the default render_node was the literal /dev/dri/renderD129 and the EGL vendor was
derived from it, so an AMD-only box (grogu: one card, renderD128 only, no
10_nvidia.json) was pointed at a nonexistent node and pinned to a nonexistent
glvnd vendor file.

Everything here runs against the fake sysfs + glvnd fixtures in conftest.py, so
both hosts' topologies are asserted without either machine's hardware.
"""
from __future__ import annotations

import dataclasses

import pytest

from matrix_overlay import cli
from matrix_overlay.config import load_default
from conftest import (AMD_PCI, GROGU_PCI, GROGU_TOPOLOGY, NVIDIA_PCI,
                      SysfsCard, WIDOW_TOPOLOGY)


def plan(cfg, sysfs):
    return cli.render_plan(cfg, sysfs=sysfs)


def cfg_at(node):
    return cli.default_flat_with(render_node=node).cfg


# ------------------------------------------------------------- hybrid box ---


def test_auto_picks_the_gpu_that_drives_a_connected_output(glvnd, mk_sysfs):
    """widow: renderD128 (AMD, no connected outputs) must NOT win just because
    it is the lower node number."""
    glvnd()
    p = plan(cfg_at(""), mk_sysfs("widow"))
    assert p.source["node"] == "/dev/dri/renderD129"
    assert p.source["reason"] == "auto_connected"
    assert p.source["pci"] == NVIDIA_PCI
    assert p.env["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("10_nvidia.json")
    assert "DRI_PRIME" not in p.env


def test_auto_does_not_regress_widows_measured_pinning(glvnd, mk_sysfs):
    """The M0 measurement stands: widow's connected outputs hang off the NVIDIA
    function, and the off-display XTX cost 17.3 fps. Auto must keep NVIDIA."""
    glvnd()
    p = plan(load_default(), mk_sysfs("widow-default"))
    assert p.source["node"] == "/dev/dri/renderD129"


# ------------------------------------------------------------- AMD-only box ---


def test_amd_only_box_finds_its_only_node_and_pins_mesa(glvnd, mk_sysfs):
    glvnd(nvidia=False)
    p = plan(cfg_at(""), mk_sysfs("grogu", GROGU_TOPOLOGY))
    assert p.source["node"] == "/dev/dri/renderD128"
    assert p.source["vendor"] == "amd"
    assert p.env["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("50_mesa.json")
    assert p.env["MESA_LOADER_DRIVER_OVERRIDE"] == "radeonsi"
    assert not any("off_display" in w for w in p.warnings)


def test_missing_explicit_node_falls_back_to_auto_and_says_so(glvnd, mk_sysfs):
    """grogu with widow's old default: must self-heal, not hard-fail."""
    glvnd(nvidia=False)
    p = plan(cfg_at("/dev/dri/renderD129"), mk_sysfs("grogu-missing",
                                                     GROGU_TOPOLOGY))
    assert p.source["node"] == "/dev/dri/renderD128"
    assert p.source["requested"] == "/dev/dri/renderD129"
    assert p.source["reason"] == "auto_connected"
    assert any("render_node_missing" in w for w in p.warnings)


def test_a_vendor_json_that_does_not_exist_is_never_pinned(glvnd, mk_sysfs):
    """glvnd pinned to an absent file loads no vendor at all: that is the
    EGL_NOT_INITIALIZED (0x3001) signature at native/mwl.c:532."""
    glvnd(nvidia=False, mesa=False)
    p = plan(cfg_at(""), mk_sysfs("grogu-nojson", GROGU_TOPOLOGY))
    assert "__EGL_VENDOR_LIBRARY_FILENAMES" not in p.env
    assert any("glvnd_vendor_json_missing" in w for w in p.warnings)


def test_explicit_egl_vendor_still_wins(glvnd, mk_sysfs):
    glvnd()
    flat = cli.default_flat_with(render_node="",
                                 egl_vendor="/somewhere/else.json").cfg
    p = plan(flat, mk_sysfs("vendor-pin"))
    assert p.env["__EGL_VENDOR_LIBRARY_FILENAMES"] == "/somewhere/else.json"
    assert p.source["json"] == "/somewhere/else.json"


# --------------------------------------------------------------- tie-breaks ---


def test_tie_break_prefers_more_vram_then_pci_order(glvnd, mk_sysfs):
    """Two equally-connected cards: VRAM decides; equal VRAM falls back to PCI
    order (never to the render node number, which is enumeration order)."""
    lower_pci = SysfsCard("card0", "0000:05:00.0", "0x1002", (128,),
                          (("DP-1", "connected"),), 1_073_741_824)
    higher_pci = SysfsCard("card1", AMD_PCI, "0x1002", (129,),
                           (("DP-9", "connected"),), 8_589_934_592)
    p = plan(cfg_at(""), mk_sysfs("tie", (lower_pci, higher_pci)))
    assert p.source["node"] == "/dev/dri/renderD129"        # more VRAM wins
    equal = (lower_pci, dataclasses.replace(higher_pci, vram_total=lower_pci.vram_total))
    p2 = plan(cfg_at(""), mk_sysfs("tie2", equal))
    assert p2.source["node"] == "/dev/dri/renderD128"       # then PCI order


def test_more_connected_outputs_beats_a_bigger_gpu(glvnd, mk_sysfs):
    dark = (
        SysfsCard("card1", AMD_PCI, "0x1002", (128,),
                  (("DP-1", "connected"), ("DP-2", "connected")),
                  25_769_803_776),
        dataclasses.replace(WIDOW_TOPOLOGY[0], connectors=(("DP-5", "connected"),)),
    )
    p = plan(cfg_at(""), mk_sysfs("counts", dark))
    assert p.source["node"] == "/dev/dri/renderD128"        # 2 outputs > 1 output


def test_off_display_gpu_is_chosen_only_when_nothing_is_connected(glvnd, mk_sysfs):
    dark = (dataclasses.replace(WIDOW_TOPOLOGY[0],
                                connectors=(("DP-5", "disconnected"),)),
            WIDOW_TOPOLOGY[1])
    p = plan(cfg_at(""), mk_sysfs("dark", dark))
    assert p.source["node"] == "/dev/dri/renderD128"        # more VRAM, no outputs
    assert p.source["reason"] == "auto_off_display"
    assert any("off_display_gpu" in w for w in p.warnings)
    assert p.env["DRI_PRIME"].startswith("pci-")


# -------------------------------------------------------------- degenerate ---


def test_no_drm_at_all_yields_no_node_and_no_vendor_pin(tmp_path, glvnd):
    from matrix_overlay.cli import Sysfs

    glvnd()
    p = plan(cfg_at(""), Sysfs(drm=tmp_path / "nope", pci=tmp_path / "nope"))
    assert p.source["node"] is None
    assert p.source["reason"] == "no_render_node"
    assert "__EGL_VENDOR_LIBRARY_FILENAMES" not in p.env
    assert any("no_render_node" in w for w in p.warnings)
    assert p.env["GDK_BACKEND"] == "wayland"


def test_single_gpu_box_sets_no_dri_prime(glvnd, mk_sysfs):
    """DRI_PRIME on a single-GPU box is at best a no-op and at worst a driver
    override aimed at a device that does not exist."""
    glvnd(nvidia=False)
    p = plan(cfg_at(""), mk_sysfs("grogu-single", GROGU_TOPOLOGY))
    assert "DRI_PRIME" not in p.env


def test_render_env_wrapper_keeps_the_old_two_tuple_contract(glvnd, mk_sysfs):
    """run()/diagnose() and the M0 tests call render_env(cfg) -> (env, warnings)."""
    glvnd()
    env, warnings = cli.render_env(load_default(), sysfs=mk_sysfs("widow2"))
    assert isinstance(env, dict) and isinstance(warnings, list)
    assert env["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("10_nvidia.json")


def test_render_source_reports_which_gpu_was_chosen_and_why(glvnd, mk_sysfs,
                                                            monkeypatch):
    monkeypatch.setattr(cli, "SYSFS", mk_sysfs("diag"))
    glvnd()
    payload = cli.render_source(load_default())
    assert payload["node"] == "/dev/dri/renderD129"
    assert payload["reason"] == "auto_connected"
    assert set(payload) >= {"node", "pci", "vendor", "json", "reason", "connected"}


def test_diagnose_carries_render_source(mkstore, mk_sysfs, glvnd, monkeypatch):
    """--diagnose used to print the env without the decision behind it."""
    from matrix_overlay.config import Capabilities

    monkeypatch.setattr(cli, "SYSFS", mk_sysfs("diagnose"))
    glvnd()
    store = mkstore("[world]\nfps = 22\n")
    out = cli.diagnose(store, Capabilities(True, 4, (0, 8, 2), (1, 2)))
    assert out["render_source"]["node"] == "/dev/dri/renderD129"
    assert out["render_source"]["vendor"] == "nvidia"
    assert out["render"]["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("10_nvidia.json")
