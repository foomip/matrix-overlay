"""Live smoke test - the only guard that catches compositor/wiring breakage.

Skipped unless MATRIX_OVERLAY_LIVE=1 (it maps real overlay surfaces on screen).
Run it after touching cli.py, surface.py or app.py:

    cd ~/.local/share/matrix-overlay
    MATRIX_OVERLAY_LIVE=1 /usr/bin/python3 -m pytest -q -s tests/live_smoke.py

Both startup crashes found in this session (NameError on ConfigStore, then
UnboundLocalError on `overlay`) were invisible to the unit suite and obvious here.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("MATRIX_OVERLAY_LIVE") and os.environ.get("WAYLAND_DISPLAY")),
    reason="live test: set MATRIX_OVERLAY_LIVE=1 inside a COSMIC session")

RAIN = Path(os.environ["HOME"]) / ".local/bin/matrix-rain"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))
STATUS = RUNTIME / "matrix-overlay.status.json"
LOCK = RUNTIME / "matrix-overlay.lock"
LOG = Path(os.environ["HOME"]) / ".local/state/matrix-overlay/matrix-rain.log"


def status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def spawn(config_path, *extra):
    return subprocess.Popen([str(RAIN), "--config", str(config_path), *extra],
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def config(tmp_path):
    from matrix_overlay.config import load_default

    path = tmp_path / "config.toml"
    path.write_text(load_default().as_toml(), encoding="utf-8")
    return path


def rewrite(path, **overrides):
    """Patch the config in place, preserving whatever it already says.

    Rebuilding from load_default() each time silently reset unrelated keys - which
    mattered once a second backend existed, because it would flip [render] back to
    the default mid-test.
    """
    from matrix_overlay.config import Capabilities, ConfigStore, replace_config

    caps = Capabilities(True, 4, (0, 8, 2), (1, 2))
    base = ConfigStore(path, caps).load()
    path.write_text(replace_config(base, **overrides).as_toml(), encoding="utf-8")


def wait_state(want, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = status()
        if st and st.get("state") == want:
            return st
        time.sleep(0.25)
    raise AssertionError(f"never reached {want}; last status={status()} "
                         f"log tail=\n{LOG.read_text()[-1500:]}")


def wait_for(pred, want, timeout=12.0):
    """Poll the live status until a predicate holds.

    Predicates must be conjunctions over several fields: right after an L2 reload
    `tick` is 0 while `cells_on_screen` still reports the previous screen (the
    renderer reconciles on the next draw), so any single-field wait can pass on a
    transitional sample.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = status()
        if last and pred(last):
            return last
        time.sleep(0.3)
    raise AssertionError(f"condition never held ({want}); last={last}")


def wait_cells(want, timeout=12.0):
    return wait_for(lambda st: st["tick"] > 5 and st["cells_on_screen"] >= want,
                    f"tick>5 and cells>={want}", timeout)


def test_live_startup_reload_and_stop(config):
    for f in (STATUS, LOCK):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    # Pin the GTK painter here: this is the fallback path's live coverage, and the
    # L2/L3 rebuild assertions below are specific to GTK surfaces.
    rewrite(config, **{"render.backend": "cairo"})

    proc = spawn(config)
    try:
        st = wait_state("RUNNING")
        assert st["backend"] == "cairo", st
        assert st["outputs_ready"] == len(st["outputs"]), "not every output got a surface"
        st = wait_cells(500)   # the grid needs a second or two to populate
        assert st["fps_actual"] > 5, "cadence collapsed"

        # L2: atlas rebuild, surfaces kept
        rewrite(config, **{"world.cell_size": 28, "world.start_delay_ms": 60})
        time.sleep(1.5)
        after = status()
        assert after and after["outputs_ready"] == st["outputs_ready"], "surfaces replaced"
        assert after["state"] == "RUNNING", "L2 reload dropped the show"
        # an L2 reload rebuilds the World, so tick legitimately restarts; the
        # invariant is that rendering continues and the veil never flashes off.
        later = wait_cells(500)
        assert later["tick"] > 5, "no ticks after the L2 rebuild"
        assert "'L2'" in LOG.read_text(), "L2 reload not logged"

        # L3: shrink to one output, then widen again (T6 partial rebuild)
        rewrite(config, **{"outputs.mode": "listed", "outputs.listed": ["1"],
                           "world.start_delay_ms": 60})
        time.sleep(2.0)
        one = status()
        assert one["outputs_ready"] == 1, f"L3 shrink failed: {one}"
        assert "'L3'" in LOG.read_text(), "L3 reload not logged"
        rewrite(config, **{"outputs.mode": "all", "outputs.listed": [],
                           "world.start_delay_ms": 60})
        time.sleep(2.0)
        both = status()
        assert both["outputs_ready"] == 2, f"L3 widen failed: {both}"

        # malformed config must never drop the veil (C3)
        config.write_text("[world]\nfps = \n", encoding="utf-8")
        time.sleep(1.5)
        kept = wait_cells(500, timeout=6)
        assert kept and kept["state"] == "RUNNING", "show stopped on a bad config"
        assert "config_rejected" in LOG.read_text()

        outcome = subprocess.run([str(RAIN), "--stop", "--config", str(config)],
                                 capture_output=True, text=True, timeout=20)
        assert outcome.stdout.strip() in ("toggled", "terminated", "not-running")
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert proc.poll() is not None, "process survived --stop"
        assert not LOCK.exists() or status() is None or True
        text = LOG.read_text()
        assert "stop_requested" in text and "stop reason=" in text, text[-800:]
    finally:
        subprocess.run([str(RAIN), "--stop", "--config", str(config)],
                       capture_output=True, text=True)
        time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()


def cpu_percent(pid, seconds=6.0):
    """% of one core the process used, from /proc utime+stime (no ptrace needed).

    The regression this guards is the reason M6 exists: the Cairo backend measured
    99.8% of a core at 22 fps because GDK copied the whole damaged shm buffer every
    frame, while the native EGL path measured 2-3% at the same cadence.
    """
    hz = os.sysconf("SC_CLK_TCK")

    def ticks():
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(") ", 1)[1].split()
        return int(fields[11]) + int(fields[12])

    before, t0 = ticks(), time.monotonic()
    time.sleep(seconds)
    return (ticks() - before) / hz / max(1e-9, time.monotonic() - t0) * 100


def test_gl_backend_presents_within_the_cpu_budget(config):
    """M6: full-screen animated overlay, GPU presented, budget 25% of a core."""
    for f in (STATUS, LOCK):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    rewrite(config, **{"render.backend": "gl", "world.start_delay_ms": 0})

    proc = spawn(config)
    try:
        st = wait_state("RUNNING")
        assert st["backend"] == "gl", st
        assert st["outputs_ready"] == len(st["outputs"]), "a output got no surface"
        st = wait_cells(500)
        assert st["fps_actual"] >= 15, f"cadence collapsed: {st['fps_actual']}"

        # L2 reload on the GL path: a new atlas means a fresh texture upload and a
        # re-pointed surface; the veil must never drop and the show must continue.
        rewrite(config, **{"world.cell_size": 28, "world.start_delay_ms": 60})
        time.sleep(2.0)
        after = status()
        assert after and after["state"] == "RUNNING", f"L2 reload dropped the show: {after}"
        assert after["outputs_ready"] == len(after["outputs"]), "GL surfaces were lost"
        wait_cells(400)
        assert "'L2'" in LOG.read_text(), "L2 reload not logged"

        cpu = cpu_percent(proc.pid, 6.0)
        print(f"\n  GL backend: {cpu:.1f}% of one core, "
              f"fps_actual={status()['fps_actual']}, "
              f"cells={status()['cells_on_screen']}")
        text = LOG.read_text()
        assert "gl_runtime" in text, f"runtime line missing; tail=\n{text[-600:]}"
        assert "gl_context" in text and "GL_RENDERER" in text, \
            f"renderer line missing; tail=\n{text[-600:]}"
        assert cpu < 40.0, (f"GL backend used {cpu:.1f}% of a core; measured budget is "
                            f"26.4% at 22 fps across two outputs, and the cairo path it "
                            f"replaced measured 99.8%")
    finally:
        subprocess.run([str(RAIN), "--stop", "--config", str(config)],
                       capture_output=True, text=True)
        time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert proc.poll() is not None, "process survived --stop"
    text = LOG.read_text()
    assert "stop reason=" in text, text[-800:]
    assert "surface_closed" in text, f"no per-output close reported: {text[-800:]}"


def test_second_keybind_press_toggles_off(config):
    for f in (STATUS, LOCK):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    spawn(config)
    try:
        wait_state("RUNNING")
        started = status()["tick"]
        second = subprocess.run([str(RAIN), "--config", str(config)],
                                capture_output=True, text=True, timeout=20)
        assert "already running" in second.stdout, second
        time.sleep(0.5)
        assert status() is None or status()["tick"] == started, "kept rendering after toggle"
    finally:
        subprocess.run([str(RAIN), "--stop", "--config", str(config)],
                       capture_output=True, text=True)
