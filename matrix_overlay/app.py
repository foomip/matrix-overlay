# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""Lifecycle: the 1.3 state machine, the fixed-cadence tick, reload dispatch
and the [perf] guard ladder.

No GLib, Gdk or cairo import happens at module scope - the scheduler, clock,
key resolver, atlas and surface factories are all injected, so the whole state
machine is unit-testable without a compositor (plan 1.5).
"""
from __future__ import annotations

import dataclasses
import os
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from .atlas import AtlasKey
from .config import ATLAS, RESET, TOPOLOGY, change_classes, dominant_class
from .surface import filter_infos
from .world import Params, World, WorldCanvas

IDLE, STARTING, RUNNING, STOPPING, EXITED = "IDLE", "STARTING", "RUNNING", "STOPPING", "EXITED"


@dataclasses.dataclass(frozen=True)
class Effective:
    """Config values the [perf] guard is allowed to move. Never written back."""

    fps: int
    render_scale: float


class OverlayError(RuntimeError):
    pass


@dataclasses.dataclass
class Overlay:
    store: Any
    caps: Any
    monitors: Callable[[], Sequence]
    atlas_factory: Callable[[AtlasKey], Any]
    surface_factory: Callable[..., Any]
    scheduler: Any
    clock: Callable[[], float]
    key_resolver: Callable[[str], int]
    log: Callable[[str], None] = print
    on_finished: Callable[[str], None] = lambda _r: None
    # GL backend only: owns the native handle, textures and event routing (None
    # for cairo, where GDK keeps the compositor connection).
    runtime: Any = None

    def __post_init__(self) -> None:
        self.state = IDLE
        self.config = self.store.load()
        self.effective = Effective(self.config.world.fps, self.config.world.render_scale)
        self._config_untouched = True
        self._canvas: WorldCanvas | None = None
        self._atlases: dict[int, Any] = {}
        self._surfaces: list[Any] = []
        self._tick_task = None
        self._delay_task = None
        self._idle_task = None
        self._frames: list[float] = []
        self._last_frame_s = 0.0
        self._work_s = 0.0
        self._sim_s = 0.0
        self._draw_s = 0.0
        self._at_floor = False
        self._tick_times: deque[float] = deque(maxlen=90)
        self._eval_at = 0.0
        self._started_at = 0.0
        self.world: World | None = None

    # ------------------------------------------------------------- start ---

    def start(self) -> None:
        if self.state not in (IDLE, EXITED):
            return
        infos = self._enabled_infos()
        if not infos:
            raise OverlayError("no enabled outputs matched the config")
        self.state = STARTING
        self._started_at = self.clock()
        self._monitor_infos = list(infos)
        self._build_atlases(infos)
        self._build_canvas(infos)
        self._build_surfaces()
        cfg = self.config
        delay = max(0, cfg.world.start_delay_ms)
        self._delay_task = self.scheduler.once(delay, self._begin_show)
        if cfg.world.idle_auto_stop_seconds > 0:
            self._idle_task = self.scheduler.once(cfg.world.idle_auto_stop_seconds * 1000,
                                                  lambda: (self.stop("idle"), False)[1])
        self.log(f"start outputs={[m.name for m in infos]} delay_ms={delay}")

    def _begin_show(self) -> bool:
        if self.state != STARTING:
            return False
        self.state = RUNNING
        self._schedule_tick()
        return False

    def _frame_cost_s(self) -> float:
        """Real cost of the last frame: in-callback work + scheduling lateness.

        The two backends spend their time in different places, and both terms are
        needed. Cairo paints from GDK's draw callback - outside tick, so it only
        shows up as lateness. GL presents inside tick, so it shows up as work and
        the lateness term goes near zero. Measured with only the lateness term:
        a 22 fps target against 23 ms of work delivered 14.5 fps, because every
        frame silently cost period + work.
        """
        return max(0.0, getattr(self, "_last_frame_s", 0.0)) \
            + max(0.0, getattr(self, "_work_s", 0.0))

    def _schedule_tick(self) -> None:
        """Self-re-arming one-shot, budgeting for the work already done.

        GLib's timeout_add fires `interval` after the callback *returns*, so a
        persistent every(33 ms) silently stretched the real period to
        33 ms + cost (measured 12.4 ticks/s instead of 30). Subtracting the
        elapsed time keeps the cadence honest without drifting.
        """
        if self._tick_task is not None:
            self.scheduler.cancel(self._tick_task)
        period = 1000.0 / max(1, self.effective.fps)
        spent = self._frame_cost_s() * 1000.0
        # floor of 1 ms: a zero delay would spin the loop and starve input/signals
        self._armed_delay = max(1, round(period - spent)) / 1000.0
        # Arm *here*, at the moment of registration: the next tick's elapsed time
        # minus this delay is the cost the loop actually paid, including GDK's
        # buffer flush/upload which happens outside any callback we can time.
        self._armed_at = self.clock()
        self._tick_task = self.scheduler.once(round(self._armed_delay * 1000.0),
                                              self._tick_once)

    def _tick_once(self) -> bool:
        keep = self.tick()
        if keep:
            self._schedule_tick()
        return False

    # ------------------------------------------------------------- build ---

    def _enabled_infos(self):
        """Gdk enumeration minus what outputs.mode/listed excludes (D5)."""
        o = self.config.outputs
        return filter_infos(list(self.monitors()), mode=o.mode, listed=o.listed)

    def _atlas_key(self, scale: int) -> AtlasKey:
        look, world = self.config.look, self.config.world
        return AtlasKey(font=look.font, cell_size=world.cell_size, scale=scale,
                        render_scale=self.effective.render_scale, glyph_set=look.glyph_set,
                        custom_glyphs=look.custom_glyphs, head_color=look.head_color,
                        body_color=look.body_color, glow_enabled=look.glow.enabled,
                        glow_radius=look.glow.radius, glow_intensity=look.glow.intensity)

    def _build_atlases(self, infos: Sequence) -> None:
        self._atlases = {}
        for info in infos:
            if info.scale not in self._atlases:  # T2: one atlas per distinct scale
                self._atlases[info.scale] = self.atlas_factory(self._atlas_key(info.scale))

    def _world_params(self) -> Params:
        w = self.config.world
        n_glyphs = max(atlas.n_glyphs for atlas in self._atlases.values()) or 96
        return Params(fps=self.effective.fps, cell_size=w.cell_size, fall_speed=w.fall_speed,
                      fall_speed_jitter=w.fall_speed_jitter, stream_density=w.stream_density,
                      tail_length=w.tail_length, fade_rate=w.fade_rate,
                      mutate_rate=w.mutate_rate, seed=w.seed, n_glyphs=n_glyphs,
                      span_mode=self.config.outputs.span_mode)

    def _build_canvas(self, infos: Sequence) -> None:
        self._monitor_infos = list(infos)
        self._canvas = WorldCanvas.from_geoms([m.geom() for m in infos],
                                              cell_size=self.config.world.cell_size)
        self.world = World(self._canvas, self._world_params())

    def _build_surfaces(self) -> None:
        cfg = self.config
        keep = {s.info.signature(): s for s in self._surfaces}
        self._surfaces = []
        for info, crop in zip(self._monitor_infos, self._canvas.crops):
            existing = keep.pop(info.signature(), None)
            if existing is not None:
                self._rebind(existing, crop)
                self._surfaces.append(existing)
                continue
            surf = self.surface_factory(
                info, crop, canvas=self._canvas, atlas=self._atlases[info.scale],
                dim_alpha=cfg.look.dim_alpha, keyboard_mode=cfg.input.keyboard_mode,
                glow_enabled=cfg.look.glow.enabled, resync=self._resync_for(info),
                on_key=self.handle_key, on_click=self.handle_click,
                on_closed=self._on_surface_closed)
            surf.show()
            self._surfaces.append(surf)
        for stale in keep.values():  # T6: only the removed output loses its veil
            stale.destroy()

    def _rebind(self, surf, crop) -> None:
        surf.crop = crop
        renderer = getattr(surf, "renderer", None)
        if renderer is not None:
            renderer.crop = crop
            renderer.canvas = self._canvas
            renderer.atlas = self._atlases[surf.info.scale]
            renderer.glow = bool(renderer.glow_enabled and renderer.atlas.glow_pad)
        surf.request_resync()

    def _resync_for(self, info):
        index = [m.name for m in self._monitor_infos].index(info.name)

        def provider():
            return self.world.render_state()[index]

        return provider

    # -------------------------------------------------------------- input --

    def handle_key(self, keyval: int) -> bool:
        if keyval == self._stop_keysym():
            self.stop("key")
            return True
        return False

    def handle_click(self) -> None:
        if self.config.input.pointer_click_action == "stop":
            self.stop("click")

    def _stop_keysym(self) -> int:
        return self.key_resolver(self.config.input.stop_key)

    def _on_surface_closed(self, name: str) -> None:
        self.log(f"surface_closed {name}")

    # ---------------------------------------------------------------- tick --

    def tick(self) -> bool:
        if self.state != RUNNING or self.world is None:
            return False
        t0 = self.clock()
        # Real cost of the *previous* frame = what actually elapsed minus the
        # delay we asked for. Measuring only the in-callback duration hid the
        # dominant term (GDK flushing a damaged 5120x2880 buffer), so the guard
        # never fired while fps_actual sat at 12 of 30.
        if getattr(self, "_armed_at", None) is not None:
            self._last_frame_s = max(0.0, (t0 - self._armed_at) - self._armed_delay)
        delta = self.world.step()
        for surf, frame in zip(self._surfaces, delta.per_output):
            surf.queue(frame)
        # GL backend: drain the compositor (Escape, configure, closed) *before*
        # painting, then submit the frame. On cairo both are inert: present() is a
        # no-op because GDK paints from its own draw signal, and there is no
        # runtime to pump.
        if self.runtime is not None:
            self.runtime.pump()
        for surf in list(self._surfaces):
            surf.present()
        # The draw happens later, inside the draw callback; it is usually the real
        # cost, so the guard must see max(sim, last draw) or it never fires (T8).
        self._tick_times.append(t0)
        sim_s = self.clock() - t0
        self._sim_s = sim_s
        self._work_s = sim_s          # counted by _frame_cost_s: this callback's own cost
        self._draw_s = max((getattr(s, "last_draw_s", 0.0) for s in self._surfaces),
                           default=0.0)
        self.mark_frame(self._frame_cost_s())
        return True

    def mark_frame(self, duration_s: float) -> None:
        cfg = self.config.perf
        if not cfg.guard:
            return
        self._frames.append(max(0.0, duration_s))
        now = self.clock()
        if now < self._eval_at:
            return
        self._eval_at = now + cfg.sustain_seconds
        if not self._frames:
            return
        ordered = sorted(self._frames)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        budget = cfg.frame_budget_ratio * (1.0 / max(1, self.effective.fps))
        self._frames.clear()
        if p95 > budget:
            self._degrade(p95, budget)
        elif self._degraded and p95 < 0.4 * budget:
            self._recover()

    @property
    def _degraded(self) -> bool:
        return self.effective.fps < self.config.world.fps

    def _degrade(self, p95: float, budget: float) -> None:
        """Ladder: fps first, then stop.

        A/B-measured reason: render_scale changes glyph resolution, but the frame
        cost here is a fixed ~45 ms GDK/compositor round-trip - cutting damage by
        40% changed the frame time by nothing. Shrinking the atlas blurs glyphs for
        no gain; the honest lever is the cadence we promise.
        """
        cfg = self.config.perf
        if self.effective.fps > cfg.min_fps:
            fps = max(cfg.min_fps, self.effective.fps - 3)
            self._set_effective(fps=fps, render_scale=self.config.world.render_scale)
            self.log(f"perf_degraded p95={p95 * 1e3:.1f}ms budget={budget * 1e3:.1f}ms fps={fps}")
        elif not self._at_floor:
            self._at_floor = True
            self.log(f"perf_floor_reached p95={p95 * 1e3:.1f}ms fps={self.effective.fps} "
                     "(render_scale kept at config value: cost is not pixel-bound)")

    def _recover(self) -> None:
        self._at_floor = False
        self._tick_times: deque[float] = deque(maxlen=90)
        self._set_effective(fps=self.config.world.fps,
                            render_scale=self.config.world.render_scale)
        self.log("perf_recovered")

    def _set_effective(self, *, fps: int, render_scale: float) -> None:
        self.effective = Effective(fps=fps, render_scale=render_scale)
        self._build_atlases(self._monitor_infos)  # render_scale is part of the atlas key
        for surf in self._surfaces:
            self._rebind(surf, surf.crop)
        if self.world is not None:
            self.world.apply_params(self._world_params())
        self._schedule_tick()

    # -------------------------------------------------------------- reload --

    def reload(self) -> set[str]:
        old = self.config
        self.store.load()
        if self.store.rejected:
            for warning in self.store.warnings:
                self.log(warning)
            self.config = old
            return set()
        new = self.store.config
        classes = set(change_classes(old, new))
        if new.render.backend != old.render.backend:
            # The painter is chosen once at startup (surfaces of one backend cannot
            # be swapped for the other), so say so instead of silently ignoring it:
            # the value still governs the next start.
            self.log(f"backend_restart_only {old.render.backend}->{new.render.backend}")
        self.config = new
        for warning in self.store.warnings:
            self.log(warning)
        if not classes:
            return classes
        dominant = dominant_class(sorted(classes))
        infos = self._enabled_infos()
        if dominant in (ATLAS, TOPOLOGY):
            self._build_atlases(infos)          # new scales and/or new L2 keys
        if dominant in (ATLAS, TOPOLOGY, RESET):
            self._build_canvas(infos)
        if dominant in (ATLAS, TOPOLOGY):
            self._build_surfaces()         # reconcile: rebind survivors, replace the rest
        self._apply_live()
        self.log(f"config_reload classes={sorted(classes)} dominant={dominant}")
        return classes

    def _apply_live(self) -> None:
        for surf in self._surfaces:
            surf.set_dim(self.config.look.dim_alpha)
        if self.world is not None:
            self.world.apply_params(self._world_params())
        self._schedule_tick()
        if self._idle_task is not None and self.config.world.idle_auto_stop_seconds == 0:
            self.scheduler.cancel(self._idle_task)
            self._idle_task = None
        elif self.state in (STARTING, RUNNING) and self._idle_task is None:
            if self.config.world.idle_auto_stop_seconds > 0:
                self._idle_task = self.scheduler.once(
                    self.config.world.idle_auto_stop_seconds * 1000,
                    lambda: (self.stop("idle"), False)[1])

    # ---------------------------------------------------------------- stop --

    def stop(self, reason: str) -> None:
        if self.state in (STOPPING, EXITED):
            return
        self.state = STOPPING
        self.log(f"stop_requested reason={reason}")
        for task in (self._tick_task, self._delay_task, self._idle_task):
            if task is not None:
                self.scheduler.cancel(task)
        self._tick_task = self._delay_task = self._idle_task = None
        for surf in self._surfaces:
            surf.destroy()
        self._surfaces = []
        if self.runtime is not None:
            self.runtime.shutdown()      # releases EGL + the Wayland connection
        self.state = EXITED
        self.log(f"stop reason={reason} uptime_s={self.uptime_s():.1f}")
        self.on_finished(reason)

    # -------------------------------------------------------------- status --

    def uptime_s(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    def fps_actual(self) -> float:
        """Rolling cadence over the last ~90 frames.

        tick/uptime would have been a lie: an L2 reload rebuilds the World (a
        cell-size change renumbers the grid), so the counter legitimately resets
        while the show - and the veil - keep running.
        """
        times = list(self._tick_times)
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return round((len(times) - 1) / span, 1) if span > 0 else 0.0

    def status(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "state": self.state,
            "pid": os.getpid(),
            "outputs": [m.name for m in getattr(self, "_monitor_infos", [])],
            "outputs_ready": sum(1 for s in self._surfaces if not getattr(s, "destroyed", 0)),
            "fps": cfg.world.fps,
            "fps_effective": self.effective.fps,
            "render_scale_effective": self.effective.render_scale,
            "backend": cfg.render.backend,
            "degraded": self._degraded,
            "uptime_s": round(self.uptime_s(), 1),
            "tick": self.world.tick if self.world else 0,
            "config_path": str(self.store.path),
            "cells_on_screen": sum(s.screen_cells for s in self._surfaces),
            "draw_ms": round(1000.0 * getattr(self, "_draw_s", 0.0), 2),
            "sim_ms": round(1000.0 * getattr(self, "_sim_s", 0.0), 2),
            # Full period of the last frame (work + whatever the loop waited). If this
            # is much larger than sim_ms, the cost is outside our callbacks - i.e. the
            # compositor or the scheduler, not the renderer.
            "frame_ms": round(1000.0 * getattr(self, "_last_frame_s", 0.0), 2),
            "fps_actual": self.fps_actual(),
        }
