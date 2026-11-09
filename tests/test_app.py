"""M4b RED: lifecycle, tick scheduling, hot-reload dispatch, perf guard.

Everything compositor-facing is injected, so the state machine in plan 1.3 is
fully unit-testable: IDLE -> STARTING -> RUNNING -> STOPPING -> EXITED, plus the
CONFIG_RELOAD self-transition and the T8 degradation ladder.
"""
from __future__ import annotations

import dataclasses

import pytest

from matrix_overlay.app import (
    EXITED,
    IDLE,
    RUNNING,
    STARTING,
    STOPPING,
    Overlay,
)
from matrix_overlay.atlas import AtlasKey
from matrix_overlay.config import Capabilities, ConfigStore
from matrix_overlay.surface import MonitorInfo
from matrix_overlay.world import BUCKETS


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def step(self, s):
        self.t += s


class FakeScheduler:
    """Deterministic GLib.timeout_add double: absolute deadlines, no clock rewind."""

    def __init__(self):
        self.tasks: list[dict] = []
        self.now = 0.0

    def _add(self, ms, fn, once):
        task = {"ms": max(0, int(ms)), "fn": fn, "next": self.now + max(0, int(ms)),
                "once": once, "cancelled": False}
        self.tasks.append(task)
        return task

    def every(self, ms, fn):
        return self._add(ms, fn, False)

    def once(self, ms, fn):
        return self._add(ms, fn, True)

    def cancel(self, task):
        task["cancelled"] = True

    def advance(self, ms, clock=None):
        target = self.now + ms
        while True:
            live = [t for t in self.tasks if not t["cancelled"]]
            if not live:
                break
            nxt = min(t["next"] for t in live)
            if nxt >= target:
                break
            self._elapsed(nxt, clock)
            for task in [t for t in self.tasks if not t["cancelled"] and t["next"] <= self.now]:
                if task["once"]:
                    self.tasks.remove(task)
                if task["fn"]() is False:
                    task["cancelled"] = True
                else:
                    task["next"] = self.now + task["ms"]
        self._elapsed(target, clock)

    def _elapsed(self, when, clock):
        if when > self.now and clock is not None:
            clock.step((when - self.now) / 1000.0)
        self.now = max(self.now, when)


class FakeAtlas:
    def __init__(self, key):
        self.key = key
        self.cell_px = key.cell_px
        self.glow_pad = key.glow_radius if key.glow_enabled else 0
        self.n_glyphs = 40

    def surface_for(self, g, lvl):
        return (g, lvl)

    def glow_surface_for(self, g, lvl):
        return (g, lvl)


class FakeSurface:
    def __init__(self, info, crop, trace=None, **kw):
        self.info = info
        self.crop = crop
        self.shown = 0
        self.destroyed = 0
        self.frames: list = []
        self.syncs = 0
        self.dims: list = []
        self.presents = 0
        self._trace = trace
        self.kw = kw

    def show(self):
        self.shown += 1

    def destroy(self):
        self.destroyed += 1

    def queue(self, frame):
        self.frames.append(frame)

    def present(self):
        """Mirror of the real contract: cairo's is a no-op, gl's submits + swaps."""
        self.presents += 1
        if self._trace is not None:
            self._trace.append("present")

    def request_resync(self):
        self.syncs += 1

    def set_dim(self, alpha):
        self.dims.append(alpha)

    @property
    def screen_cells(self):
        return len(self.frames)


class FakeRuntime:
    """GL backend double: counts pumps and the shutdown the app owes us."""

    def __init__(self, trace=None):
        self.pumps = 0
        self.shutdowns = 0
        self._trace = trace

    def pump(self):
        self.pumps += 1
        if self._trace is not None:
            self._trace.append("pump")
        return []

    def shutdown(self):
        self.shutdowns += 1

    def describe(self):
        return "layer_shell=4 | GL_RENDERER=Fake"


def monitors(*scales):
    out = []
    x = 0
    for i, s in enumerate(scales):
        out.append(MonitorInfo(index=i, model=f"M{i}", x=x, y=0, width=1800, height=1440,
                              scale=s))
        x += 1800
    return out


def make(body="", *, scaled_monitors=(1, 2), start_delay=None, runtime=None,
         trace=None):
    clock = FakeClock()
    sched = FakeScheduler()
    made: list[list] = [[], []]  # [atlas keys], [surfaces]

    def atlas_factory(key: AtlasKey):
        made[0].append(key)
        return FakeAtlas(key)

    def surface_factory(info, crop, **kw):
        surf = FakeSurface(info, crop, trace=trace, **kw)
        made[1].append(surf)
        return surf

    tmp = pytest.MonkeyPatch()
    import pathlib
    import tempfile

    d = pathlib.Path(tempfile.mkdtemp())
    path = d / "config.toml"
    if start_delay is not None:
        body = (body + f"\n[world]\nstart_delay_ms = {start_delay}\n")
    path.write_text(body, encoding="utf-8")
    caps = Capabilities(True, 4, (0, 8, 2), scaled_monitors)
    store = ConfigStore(path, caps)
    logs: list[str] = []
    ov = Overlay(
        store=store,
        caps=caps,
        monitors=lambda: monitors(*scaled_monitors),
        atlas_factory=atlas_factory,
        surface_factory=surface_factory,
        scheduler=sched,
        clock=clock,
        key_resolver=lambda name: {"Escape": 0xFF1B, "q": 0x71}.get(name, 0),
        runtime=runtime,
        log=logs.append,
        on_finished=lambda reason: logs.append(f"finished:{reason}"),
    )
    return ov, made, logs, path


# ------------------------------------------------------------ state machine ---


def test_fresh_overlay_is_idle_and_tickless():
    ov, _made, _logs, _p = make()
    assert ov.state == IDLE
    assert ov.tick() is False


def test_start_shows_the_veil_before_the_first_frame():
    """The dim must land on frame 0; the atlas bake must not delay it (T10)."""
    ov, made, _logs, _p = make(start_delay=1000)
    ov.start()
    assert ov.state == STARTING
    assert [s.shown for s in made[1]] == [1, 1], "both surfaces shown"
    assert [len(s.frames) for s in made[1]] == [0, 0], "glyphs before start_delay"


def test_running_after_start_delay_and_frames_reach_every_output():
    ov, made, _logs, _p = make('[world]\nstream_density = 1.0\nstart_delay_ms = 100\n')
    ov.start()
    made[1].clear()
    ov.scheduler.advance(200, ov.clock)
    assert ov.state == RUNNING
    assert ov.world.tick > 0
    assert all(s.frames for s in made[1]), "an output received no cells"


def test_stop_destroys_surfaces_and_reports_the_reason():
    ov, made, logs, _p = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    ov.scheduler.advance(50, ov.clock)
    assert ov.handle_key(0xFF1B) is True
    assert ov.state in (STOPPING, EXITED)
    assert all(s.destroyed >= 1 for s in made[1])
    assert "finished:key" in logs


def test_other_keys_do_not_stop_the_show():
    ov, _made, logs, _p = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    assert ov.handle_key(0x71) is False
    assert ov.state == STARTING


def test_custom_stop_key_is_honoured():
    ov, _made, _logs, _p = make('[input]\nstop_key = "q"\n[world]\nstart_delay_ms = 0\n')
    ov.start()
    assert ov.handle_key(0xFF1B) is False
    assert ov.handle_key(0x71) is True


def test_click_stops_by_default_and_can_be_ignored():
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    ov.handle_click()
    assert ov.state in (STOPPING, EXITED)

    ov2, _m2, _l2, _p2 = make('[input]\npointer_click_action = "ignore"\n'
                             '[world]\nstart_delay_ms = 0\n')
    ov2.start()
    ov2.handle_click()
    assert ov2.state == STARTING


def test_idle_auto_stop_fires():
    ov, _made, logs, _p = make('[world]\nstart_delay_ms = 0\nidle_auto_stop_seconds = 2\n')
    ov.start()
    ov.scheduler.advance(1000, ov.clock)
    assert "finished:idle" not in logs
    ov.scheduler.advance(1500, ov.clock)
    assert "finished:idle" in logs


def test_double_start_is_a_noop():
    ov, made, _logs, _p = make()
    ov.start()
    n = len(made[1])
    ov.start()
    assert len(made[1]) == n


# -------------------------------------------------------------- hot reload ---


def test_l1_change_adjusts_dim_without_new_surfaces():
    ov, made, _logs, path = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    surfaces, atlases = len(made[1]), len(made[0])
    path.write_text('[look]\ndim_alpha = 0.4\n[world]\nstart_delay_ms = 0\n', encoding="utf-8")
    assert ov.reload() == {"L1"}
    assert len(made[1]) == surfaces and len(made[0]) == atlases
    assert made[1][0].dims and made[1][0].dims[-1] == 0.4


def test_l2_change_rebuilds_the_atlas_but_keeps_surfaces_and_ticks():
    ov, made, _logs, path = make('[world]\nstart_delay_ms = 0\n', scaled_monitors=(1, 1))
    ov.start()
    assert len(made[0]) == 1, "one atlas per distinct monitor scale (T2)"
    tick = ov.world.tick
    made[1].clear()
    path.write_text('[world]\ncell_size = 24\nstart_delay_ms = 0\n'
                    '[look]\nfont = "DejaVu Sans Mono"\n', encoding="utf-8")
    assert ov.reload() == {"L2"}
    assert len(made[1]) == 0, "surfaces must not be recreated (no veil flash)"
    assert len(made[0]) == 2, "atlas must be rebuilt"
    assert made[0][1].cell_size == 24
    assert ov.world.tick >= tick


def test_l3_replaces_only_the_affected_surfaces():
    """T6: the untouched monitor must keep its veil - no flash in either direction."""
    ov, made, _logs, path = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    first, second = made[1][0], made[1][1]
    path.write_text('[outputs]\nmode = "listed"\nlisted = ["0"]\n'
                    '[world]\nstart_delay_ms = 0\n', encoding="utf-8")
    assert ov.reload() == {"L3"}
    assert first.destroyed == 0, "surviving surface was destroyed"
    assert second.destroyed == 1
    path.write_text('[outputs]\nmode = "all"\n[world]\nstart_delay_ms = 0\n', encoding="utf-8")
    assert ov.reload() == {"L3"}
    assert made[1][-1] is not first and made[1][-1] is not second
    assert first.destroyed == 0


def test_l4_change_reseeds_the_world():
    ov, _made, _logs, path = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    digest_a = ov.world.digest()
    path.write_text('[world]\nseed = 1234\nstart_delay_ms = 0\n', encoding="utf-8")
    assert ov.reload() == {"L4"}
    assert ov.world.digest() != digest_a


def test_rejected_config_keeps_running():
    ov, _made, logs, path = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    path.write_text("[world]\nfps = \n", encoding="utf-8")
    assert ov.reload() == set()
    assert ov.state == STARTING
    assert any("config_rejected" in l for l in logs)


# ------------------------------------------------------------ perf guard ---


def test_guard_degrades_fps_first_and_never_blurs_glyphs():
    """D7: measured cost is a fixed per-frame round-trip, so the ladder may reduce
    the cadence we promise but must not shrink glyph resolution, and it must stop
    at the floor instead of churning forever. Config stays untouched (C3)."""
    ov, _made, logs, path = make('[world]\nstart_delay_ms = 0\nfps = 30\n'
                                '[perf]\nguard = true\nsustain_seconds = 1.0\n'
                                'frame_budget_ratio = 0.8\nmin_fps = 20\n')
    before = path.read_bytes()
    ov.start()
    slow = 1.0 / 30 * 0.95
    for _ in range(200):
        ov.mark_frame(slow)
        ov.clock.step(0.05)
    assert ov.effective.fps < 30, "guard must reduce the promised cadence"
    assert ov.effective.fps >= ov.config.perf.min_fps
    assert ov.effective.render_scale == 1.0, "render_scale must not be degraded (D7)"
    assert ov.status()["degraded"] is True
    assert path.read_bytes() == before, "config is read-only to the app (C3)"
    assert sum(1 for l in logs if "perf_degraded" in l) <= 4
    assert sum(1 for l in logs if "perf_floor_reached" in l) == 0, \
        "a 31.7ms frame fits the floored 40ms budget: ladder must self-limit"
    # now genuinely over even the floored budget -> floor announced exactly once
    for _ in range(200):
        ov.mark_frame(0.05)
        ov.clock.step(0.05)
    assert sum(1 for l in logs if "perf_floor_reached" in l) == 1
    assert ov.effective.fps == ov.config.perf.min_fps
    assert ov.effective.render_scale == 1.0, "floor must not blur glyphs either"
    for _ in range(400):
        ov.mark_frame(0.001)
        ov.clock.step(0.05)
    assert ov.effective.fps == 30, "guard must recover the configured cadence"
    assert ov.status()["degraded"] is False
    assert any("perf_recovered" in l for l in logs)


def test_guard_off_keeps_settings():
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n'
                               '[perf]\nguard = false\nsustain_seconds = 0.5\n')
    ov.start()
    for _ in range(200):
        ov.mark_frame(1.0)
        ov.clock.step(0.05)
    assert ov.effective.fps == 22
    assert ov.effective.render_scale == 1.0


# ---------------------------------------------------------------- status ---


def test_status_reports_the_contract_fields():
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    st = ov.status()
    assert set(st) >= {"state", "pid", "outputs", "outputs_ready", "fps", "fps_effective",
                       "render_scale_effective", "degraded", "uptime_s", "tick",
                       "config_path", "cells_on_screen", "fps_actual", "sim_ms", "draw_ms"}
    assert st["state"] == STARTING
    assert st["outputs"] == ["0:M0", "1:M1"]
    assert isinstance(st["pid"], int)


def test_tick_stops_after_stop():
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    ov.stop("test")
    assert ov.tick() is False
    assert ov.state == EXITED


# ------------------------------------------------------------------ M6: GL --


def test_schedule_subtracts_in_callback_work_from_the_delay():
    """GL presents inside tick, so that work must come out of the next delay.

    Without it every frame silently costs period + work (measured 14.5 fps against
    a 22 fps target with 23 ms of work per frame).
    """
    ov, _made, _logs, _p = make('[world]\nfps = 20\nstart_delay_ms = 0\n')
    ov.start()
    ov.scheduler.advance(10, ov.clock)
    ov._work_s = 0.020           # 20 ms of work inside the callback
    ov._last_frame_s = 0.002     # 2 ms of scheduling lateness
    ov._schedule_tick()
    task = [t for t in ov.scheduler.tasks if not t["cancelled"]][-1]
    assert task["ms"] == 28, "period 50 ms - 20 ms work - 2 ms lateness"


def test_guard_budget_uses_the_full_frame_cost():
    """A guard that only sees lateness never fires on the GL path, because the GL
    path spends its time in-callback."""
    ov, _made, logs, _p = make('[world]\nfps = 30\nstart_delay_ms = 0\n'
                               '[perf]\nguard = true\nsustain_seconds = 0.5\n'
                               'frame_budget_ratio = 0.8\n')
    ov.start()
    ov.scheduler.advance(10, ov.clock)
    for _ in range(40):
        ov._work_s = 0.048        # 48 ms against a ~27 ms budget
        ov.clock.step(0.5)
        ov.mark_frame(ov._frame_cost_s())
    assert any("perf_degraded" in line for line in logs), logs[-4:]


def test_tick_presents_every_surface_even_without_a_runtime():
    """The cairo path keeps working unchanged: present() is its no-op."""
    ov, made, _logs, _p = make('[world]\nstart_delay_ms = 0\n')
    ov.start()
    ov.scheduler.advance(50, ov.clock)
    ov.tick()
    assert all(s.presents >= 1 for s in made[1]), "tick must drive the painter"


def test_tick_pumps_the_compositor_before_presenting():
    """Escape has to be handled before we spend the frame, not after."""
    trace: list[str] = []
    rt = FakeRuntime(trace)
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\nstream_density = 1.0\n',
                                runtime=rt, trace=trace)
    ov.start()
    ov.scheduler.advance(50, ov.clock)
    assert ov.state == RUNNING, "tick() is inert before RUNNING"
    trace.clear()
    ov.tick()
    assert trace[0] == "pump"
    assert trace.count("present") == 2, "one present per output"


def test_runtime_pump_reaches_the_app_via_the_surface_callbacks():
    """The routing itself is GlRuntime's job (test_glsurface); the app must simply
    pump on every tick so a pressed Escape is seen."""
    rt = FakeRuntime()
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n', runtime=rt)
    ov.start()
    ov.scheduler.advance(200, ov.clock)
    before = rt.pumps
    ov.tick()
    assert rt.pumps == before + 1


def test_stop_shuts_the_runtime_down_once():
    rt = FakeRuntime()
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n', runtime=rt)
    ov.start()
    ov.scheduler.advance(50, ov.clock)
    ov.stop("test")
    ov.stop("again")
    assert rt.shutdowns == 1, "double shutdown would touch a freed EGL display"


def test_status_reports_the_active_backend():
    ov, _made, _logs, _p = make('[world]\nstart_delay_ms = 0\n'
                                '[render]\nbackend = "gl"\n', runtime=FakeRuntime())
    ov.start()
    assert ov.status()["backend"] == "gl"


def test_backend_change_is_reported_as_restart_only():
    """Surfaces of one painter cannot become the other's mid-run; the app must say
    so rather than silently keeping the current backend."""
    ov, _made, logs, path = make('[world]\nstart_delay_ms = 0\n', runtime=FakeRuntime())
    ov.start()
    path.write_text(path.read_text() + '\n[render]\nbackend = "cairo"\n',
                    encoding="utf-8")
    ov.reload()
    assert any("backend_restart_only" in line for line in logs), logs[-4:]
