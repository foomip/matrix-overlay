"""CLI, single-instance toggle and environment pinning (plan C2).

Kept import-light on purpose: nothing in this module needs Gtk, so the parts
that decide whether Ctrl+Shift+M starts or stops the show - and which GPU owns
the buffers - are unit-testable (D1.6 moved the escalation ladder out of bash
for exactly that reason).
"""
from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import Capabilities, Config, ConfigStore, load_default, replace_config
from .surface import MonitorInfo

EXIT_OK, EXIT_CONFIG, EXIT_NO_LAYER_SHELL, EXIT_LOCK, EXIT_DIAGNOSE = 0, 2, 3, 4, 5

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(os.environ.get("HOME", "")) / ".config/matrix-overlay/config.toml"
RUNTIME_LOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "matrix-overlay.lock"
LOG_DIR = Path(os.environ.get("HOME", "")) / ".local/state/matrix-overlay"
NVIDIA_JSON = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
MESA_JSON = "/usr/share/glvnd/egl_vendor.d/50_mesa.json"
AMD_VENDOR = "0x1002"


class UsageError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Options:
    mode: str = "run"
    config: Path | None = None
    geometry: str | None = None
    config_env: bool = False


def parse_args(argv: list[str]) -> Options:
    parser = argparse.ArgumentParser(prog="matrix-rain", add_help=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--geometry", default=None)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--monitor-list", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--print-default-config", action="store_true")
    parser.exit_on_error = True  # argparse>=3.9: raise instead of sys.exit
    try:
        ns = parser.parse_args(argv)
    except (SystemExit, Exception) as exc:  # argparse raises both
        raise UsageError(str(exc)) from exc
    for mode in ("print-default-config", "self-test", "monitor-list", "diagnose",
                 "status", "stop"):
        flag = mode.replace("-", "_")
        if getattr(ns, flag, False):
            if sum(bool(getattr(ns, f.replace("-", "_"), False)) for f in
                   ("stop", "status", "diagnose", "monitor_list", "self_test",
                    "print_default_config")) > 1:
                raise UsageError("modes are mutually exclusive")
            return Options(mode=mode, config=_cfgpath(ns), geometry=ns.geometry)
    return Options(mode="run", config=_cfgpath(ns), geometry=ns.geometry)


def _cfgpath(ns) -> Path | None:
    return Path(ns.config) if ns.config else None


def config_path(opts: Options) -> Path:
    if opts.config is not None:
        return opts.config
    env = os.environ.get("MATRIX_OVERLAY_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG


# --------------------------------------------------------- render-node env ---


def _resolve_pci(render_node: str) -> str | None:
    """/dev/dri/renderD129 -> '0000:04:00.0' via the sysfs class link."""
    device = Path("/sys/class/drm") / Path(render_node).name / "device"
    try:
        if not device.exists():
            return None
        return Path(device.resolve()).name
    except OSError:
        return None


def _pci_vendor(pci: str) -> str | None:
    try:
        return Path(f"/sys/bus/pci/devices/{pci}/vendor").read_text().strip()
    except OSError:
        return None


def _pci_drives_output(pci: str) -> bool:
    """Does any *connected* DRM connector live on this PCI function?
    (measured: only 0000:04:00.0 - the NVIDIA - has DP-4/DP-5 attached)"""
    drm = Path("/sys/class/drm")
    for status in sorted(drm.glob("card[0-9]*-*/status")):
        try:
            if status.read_text().strip() != "connected":
                continue
            card = status.parent.name.split("-")[0]
            if Path(drm / card / "device").resolve().name == pci:
                return True
        except OSError:
            continue
    return False


def render_env(cfg: Config) -> tuple[dict[str, Any], list[str]]:
    """Environment for the app process. '' means 'remove from the inherited env'.

    With the GL backend (the default since M6) these are load-bearing: they choose
    the EGL vendor and therefore the GPU that renders. Verified live - renderD129
    reports "GL_RENDERER=NVIDIA GeForce RTX 3060", renderD128 reports "Radeon RX
    7900 XTX (radeonsi...)". On the cairo fallback they only steer GDK's buffer
    allocation, which is why they were diagnostic-only before M6 (plan 1.1).
    """
    warnings: list[str] = []
    node = cfg.render.render_node
    env: dict[str, Any] = {
        "GDK_BACKEND": "wayland",
        "DISPLAY": "",                    # never let GDK pick Xwayland (:1 exists)
        "GI_TYPELIB_PATH": "",            # don't inherit another tool's typelib path
        "PYTHONPATH": str(APP_DIR),
        "MATRIX_OVERLAY_CONFIG": str(config_path(Options())),
        "LIBGL_ALWAYS_SOFTWARE": "",
    }
    pci = _resolve_pci(node)
    off_display = bool(pci) and not _pci_drives_output(pci)
    vendor = _pci_vendor(pci) if pci else None
    if off_display:
        warnings.append(f"off_display_gpu:{node} (pci {pci}) drives no connected output; "
                        "expect a cross-device copy path")
    if off_display or (vendor == AMD_VENDOR):
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = cfg.render.egl_vendor or MESA_JSON
        env["DRI_PRIME"] = ("pci-" + pci.replace(":", "_").replace(".", "_")
                            if pci else "1")
        env["MESA_LOADER_DRIVER_OVERRIDE"] = "radeonsi"
    else:
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = cfg.render.egl_vendor or NVIDIA_JSON
        if cfg.render.egl_vendor and cfg.render.egl_vendor.endswith("50_mesa.json"):
            warnings.append(f"egl_vendor_mismatch:{node} is nvidia but mesa was requested")
    if not Path(node).exists():
        warnings.append(f"unknown_render_node:{node}")
    return env, warnings


def apply_env(env: dict[str, Any]) -> None:
    for key, value in env.items():
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


def default_flat_with(**overrides: Any):
    """Test helper: a Config with dotted-path overrides applied."""
    @dataclasses.dataclass
    class Wrapped:
        cfg: Config

    return Wrapped(replace_config(load_default(), **overrides))


# ------------------------------------------------------------ geometry -----


def monitors_from_geometry(spec: str) -> list[MonitorInfo]:
    """'WxH+X+Y[@SCALE],...' - lets --self-test and CI run without a compositor."""
    infos: list[MonitorInfo] = []
    for i, chunk in enumerate(s for s in spec.split(",") if s.strip()):
        try:
            body, _, scale = chunk.partition("@")
            size, _, origin = body.partition("+")
            width, _, height = size.partition("x")
            x, _, y = origin.partition("+")
            infos.append(MonitorInfo(index=i, model=f"geom{i}", x=int(x), y=int(y),
                                     width=int(width), height=int(height),
                                     scale=int(scale or 1)))
        except ValueError as exc:
            raise UsageError(f"bad --geometry {chunk!r}: {exc}") from exc
    if not infos:
        raise UsageError("--geometry listed no outputs")
    return infos


# ------------------------------------------------------------------ lock ---


@dataclasses.dataclass
class LockHandle:
    fh: Any
    pid_file: Path

    @property
    def pid(self) -> int:
        return os.getpid()


def acquire_lock(path: Path = RUNTIME_LOCK) -> LockHandle | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return None
        raise
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return LockHandle(fh=fh, pid_file=path)


def release_lock(handle: LockHandle | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fh.fileno(), fcntl.LOCK_UN)
    finally:
        handle.fh.close()


def lock_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


POLL_STEP = 0.05


def stop_instance(path: Path = RUNTIME_LOCK, *,
                  kill: Callable[[int, int], Any] = os.kill,
                  holder_alive: Callable[[int], bool] = lambda pid: True,
                  sleep: Callable[[float], None] = time.sleep,
                  budget: float = 1.5, term_budget: float = 0.5) -> str:
    """Toggle off the running instance, escalating USR1 -> TERM -> KILL (T4)."""
    pid = lock_pid(path)
    if pid is None:
        return "not-running"
    if not holder_alive(pid):
        return "stale"
    kill(pid, signal.SIGUSR1)
    for _ in range(int(budget / POLL_STEP)):
        sleep(POLL_STEP)
        if not holder_alive(pid):
            return "toggled"
    kill(pid, signal.SIGTERM)
    for _ in range(int(term_budget / POLL_STEP)):
        sleep(POLL_STEP)
        if not holder_alive(pid):
            return "terminated"
    kill(pid, signal.SIGKILL)
    return "killed"


def proc_state(pid: int) -> str | None:
    """/proc/<pid>/stat state char; comm can contain spaces, so split after ')'."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tail = stat.rpartition(")")[2].split()
    return tail[0] if tail else None


def pid_alive(pid: int) -> bool:
    """A zombie is dead for our purposes.

    `kill(pid, 0)` succeeds for an unreaped zombie, so a naive liveness probe made
    the escalation ladder SIGKILL an already-exited holder and report "killed" -
    which is exactly the misleading outcome seen on the first live run (T19).
    """
    state = proc_state(pid)
    if state is not None:
        return state not in ("Z", "X", "x")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# -------------------------------------------------------------- signals ---


def signal_handlers(overlay) -> dict[int, Callable[[], Any]]:
    return {
        signal.SIGUSR1: lambda: overlay.stop("toggle"),
        signal.SIGINT: lambda: overlay.stop("sigint"),
        signal.SIGTERM: lambda: overlay.stop("sigterm"),
        signal.SIGHUP: lambda: None,
    }


def glib_signal_callback(handler: Callable[[], Any]) -> Callable[[Any], bool]:
    """Adapter for GLib.unix_signal_add(priority, signum, callback, user_data).

    GLib invokes the callback *with user_data*, so it must be accepted
    positionally: binding `h=handler` as a default got rebound to the None we
    passed, and every signal died with TypeError: 'NoneType' object is not
    callable - the overlay simply never stopped (found live, not in unit tests).
    """

    def on_signal(_data):
        handler()
        return True  # keep the source installed (False would let SIGHUP kill us)

    return on_signal


# ------------------------------------------------------------- diagnostics --


def diagnose(store: ConfigStore, caps) -> dict[str, Any]:
    blockers: list[str] = []
    cfg = store.load()
    if store.rejected:
        blockers.append("config_unreadable:" + ";".join(store.warnings))
    if not caps.layer_shell_supported:
        blockers.append("layer_shell_unsupported")
    if caps.lib_version < (0, 6, 0):
        blockers.append("keyboard_mode_unavailable:libgtk-layer-shell<0.6")
    if not caps.monitor_scales:
        blockers.append("no_outputs")
    if not blockers:
        exit_code = EXIT_OK
    elif any("layer_shell" in b or "keyboard_mode" in b for b in blockers):
        exit_code = EXIT_NO_LAYER_SHELL
    elif all(b.startswith("config") for b in blockers):
        exit_code = EXIT_CONFIG
    else:
        exit_code = EXIT_DIAGNOSE
    return {
        "exit": exit_code if blockers else EXIT_OK,
        "blockers": blockers,
        "warnings": list(store.warnings),
        "supported": caps.layer_shell_supported,
        "protocol_version": caps.protocol_version,
        "lib_version": list(caps.lib_version),
        "monitor_scales": list(caps.monitor_scales),
        "backend": cfg.render.backend,
        "config_path": str(store.path),
        "render": render_env(cfg)[0],
        "render_warnings": render_env(cfg)[1],
    }


def status_payload(*, state: str, pid: int, outputs: list[str], ready: int,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"state": state, "pid": pid, "outputs": list(outputs),
               "outputs_ready": ready}
    payload.update(extra or {})
    return payload


def as_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------- adapters ---

STATUS_PATH = RUNTIME_LOCK.parent / "matrix-overlay.status.json"


def _neutral_caps() -> Capabilities:
    return Capabilities(True, 4, (0, 8, 2), (1,))


class Task:
    """A schedulable source that remembers whether it already fired.

    GLib has no GLib.Source.find() in this GIR, and removing an already-fired
    one-shot prints "Source ID N was not found when attempting to remove it" -
    which is exactly what aborted Overlay.stop() mid-way in a live run.
    """

    __slots__ = ("source_id", "fired")

    def __init__(self, source_id):
        self.source_id = source_id
        self.fired = False


class GLibScheduler:
    """Same contract as the test fake: every() keeps while fn returns True."""

    def __init__(self):
        from gi.repository import GLib

        self._g = GLib

    def every(self, ms, fn):
        task = Task(None)

        def cb():
            keep = fn()
            if not keep:
                task.fired = True
            return bool(keep)

        task.source_id = self._g.timeout_add(max(1, int(ms)), cb)
        return task

    def once(self, ms, fn):
        task = Task(None)

        def cb():
            task.fired = True
            fn()
            return False

        task.source_id = self._g.timeout_add(max(0, int(ms)), cb)
        return task

    def cancel(self, task):
        if task is None or task.fired:
            return
        self._g.source_remove(task.source_id)
        task.fired = True


def log_line(text: str) -> None:
    import time

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(LOG_DIR / "matrix-rain.log", "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {text}\n")


def detach_stdio() -> None:
    """T10: shortcut-spawned Wayland apps must not inherit a tty or a pipe."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / "matrix-rain.log", "a", encoding="utf-8")
    devnull = open(os.devnull, "r", encoding="utf-8")
    os.dup2(devnull.fileno(), 0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    try:
        os.setsid()
    except OSError:  # already a session leader
        pass


# ------------------------------------------------------------- modes ---


def print_default_config() -> int:
    sys.stdout.write(load_default().as_toml())
    return EXIT_OK


def self_test(opts: Options, frames: int = 40) -> int:
    """Headless: no compositor, no window. Renders real frames and hashes them."""
    import hashlib
    import time

    import cairo

    from .atlas import AtlasKey, GlyphAtlas
    from .render import Renderer
    from .world import World, WorldCanvas

    infos = cli_monitors(opts)
    cfg = ConfigStore(config_path(opts), _neutral_caps()).load()
    canvas = WorldCanvas.from_geoms([m.geom() for m in infos], cell_size=cfg.world.cell_size)
    # atlas key built once; scale is overridden per output below
    atlases = {}
    for info in infos:
        if info.scale not in atlases:                      # one atlas per scale (T2)
            atlases[info.scale] = GlyphAtlas(AtlasKey(**{**dataclasses.asdict(
                _atlas_key(cfg, 1, cfg.world.render_scale)), "scale": info.scale}))
    first = next(iter(atlases.values()))
    world = World(canvas, _params_for(cfg, first))
    digests, blank = [], False
    for index, (info, crop) in enumerate(zip(infos, canvas.crops)):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, info.width, info.height)
        rend = Renderer(crop=crop, canvas=canvas, atlas=atlases[info.scale],
                        scale=info.scale, glow_enabled=cfg.look.glow.enabled)
        ctx = cairo.Context(surf)
        t0 = time.perf_counter()
        for _ in range(frames):
            rend.apply(ctx, world.step().per_output[index])
        cost = (time.perf_counter() - t0) / frames
        surf.flush()
        digest = hashlib.sha256(bytes(memoryview(surf.get_data())[:])).hexdigest()[:16]
        digests.append(digest)
        blank = blank or digest == "0" * 16
        print(f"self-test {info.name} scale={info.scale}: {frames} frames, "
              f"{cost * 1e3:.2f} ms/frame, {len(rend.screen)} cells, digest {digest}")
    if blank:
        eprint("self-test FAILED: blank output (font cannot render the glyph set?)")
        return EXIT_DIAGNOSE
    total = sum(len(a.unsupported) for a in atlases.values())
    print(f"self-test OK unsupported={total} frames_total={frames * len(infos)}")
    return EXIT_OK


def _atlas_key(cfg: Config, scale: int, render_scale: float) -> "AtlasKey":
    from .atlas import AtlasKey

    look, world = cfg.look, cfg.world
    return AtlasKey(font=look.font, cell_size=world.cell_size, scale=scale,
                    render_scale=render_scale, glyph_set=look.glyph_set,
                    custom_glyphs=look.custom_glyphs, head_color=look.head_color,
                    body_color=look.body_color, glow_enabled=look.glow.enabled,
                    glow_radius=look.glow.radius, glow_intensity=look.glow.intensity)


def _params_for(cfg, atlas):
    from .world import Params

    w = cfg.world
    return Params(fps=w.fps, cell_size=w.cell_size, fall_speed=w.fall_speed,
                  fall_speed_jitter=w.fall_speed_jitter, stream_density=w.stream_density,
                  tail_length=w.tail_length, fade_rate=w.fade_rate, mutate_rate=w.mutate_rate,
                  seed=w.seed, n_glyphs=atlas.n_glyphs, span_mode=cfg.outputs.span_mode)


def cli_monitors(opts: Options):
    if opts.geometry:
        return monitors_from_geometry(opts.geometry)
    return display_monitors(opts)


def display_monitors(opts: Options):
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk

    from .surface import select_monitors, unmatched_entries

    display = Gdk.Display.get_default()
    if display is None:
        raise UsageError("no Wayland display (is WAYLAND_DISPLAY set?)")
    cfg = ConfigStore(config_path(opts), _neutral_caps()).load()
    infos = select_monitors(display, mode=cfg.outputs.mode, listed=cfg.outputs.listed)
    for entry in unmatched_entries(display, listed=cfg.outputs.listed):
        log_line(f"outputs_listed_unmatched {entry}")
    return infos


def gl_monitors(opts: Options, runtime) -> list:
    """MonitorInfo list from our own Wayland client instead of Gdk.

    The GL backend sees connector names (DP-4/DP-5), so outputs.listed can match
    on those as well as on the model string; the matching rules stay shared
    (surface.filter_infos) so both backends honour the same config.
    """
    from .glsurface import monitor_infos
    from .surface import filter_infos, unmatched_in

    cfg = ConfigStore(config_path(opts), _neutral_caps()).load()
    infos = monitor_infos(runtime.handle)
    for entry in unmatched_in(infos, listed=cfg.outputs.listed):
        log_line(f"outputs_listed_unmatched {entry}")
    return filter_infos(infos, mode=cfg.outputs.mode, listed=cfg.outputs.listed)


def start_gl_runtime():
    """Build/load libmwl.so and connect. Returns None (with a logged reason) so the
    app falls back to the proven Cairo painter instead of refusing to run."""
    from .glsurface import GlRuntime
    from .mwl import MwlError

    try:
        runtime = GlRuntime(log=log_line).start()
    except MwlError as exc:
        log_line(f"gl_unavailable {exc}")
        eprint(f"matrix-rain: GL backend unavailable ({exc}); falling back to cairo")
        return None
    except Exception as exc:  # pragma: no cover - broken build artefact
        log_line(f"gl_unavailable {type(exc).__name__}: {exc}")
        eprint(f"matrix-rain: GL backend failed to start ({exc}); using cairo")
        return None
    log_line(f"gl_runtime {runtime.describe()}")
    return runtime


def gl_status() -> dict[str, Any]:
    """--diagnose report for the native layer: build state, protocol versions,
    outputs and the GL strings that prove [render] render_node was honoured.

    Non-destructive: no rebuild, no keyboard grab, and the probe surface is
    destroyed without ever attaching a buffer, so nothing flashes on screen.
    """
    from .mwl import Mwl, MwlError, ensure_library, library_path

    path = library_path()
    out: dict[str, Any] = {"library": str(path), "built": path.exists()}
    try:
        handle = Mwl(ensure_library(path, rebuild=False), rebuild=False)
    except MwlError as exc:
        out["error"] = str(exc)
        return out
    try:
        handle.init()
        out["versions"] = handle.versions()
        out["outputs"] = [dataclasses.asdict(o) for o in handle.outputs()]
        sid = handle.create_surface(0, exclusive_keyboard=False)
        handle.make_current(sid)
        out["gl"] = handle.gl_info()
        handle.destroy_surface(sid)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["native_error"] = handle.error()
    finally:
        handle.shutdown()
    return out


def monitor_list(opts: Options) -> int:
    infos = cli_monitors(opts)
    print(as_json([{f.name: getattr(m, f.name) for f in dataclasses.fields(m)
                    if f.name != "monitor"} | {"key": m.name} for m in infos]))
    return EXIT_OK


def init_gtk():
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import Gdk, GLib, Gtk, GtkLayerShell

    Gtk.init(None)
    return Gtk, Gdk, GLib, GtkLayerShell


def run(opts: Options) -> int:
    import time

    from .app import Overlay, OverlayError
    from .atlas import GlyphAtlas
    from .surface import OutputSurface, install_veil_css

    # Acquire the lock *before* detaching stdio: when we are only toggling another
    # instance off, the outcome must still reach the caller's terminal (a second
    # keybind press ran fine but printed into the log file).
    handle = acquire_lock()
    if handle is None:
        outcome = stop_instance(RUNTIME_LOCK, kill=os.kill, holder_alive=pid_alive)
        print(f"matrix-rain: already running ({outcome})")
        return EXIT_OK if outcome in ("toggled", "terminated", "killed", "stale") else EXIT_LOCK

    detach_stdio()
    try:
        os.nice(5)  # T11: shortcut-spawned procs share cosmic-comp's weight
    except OSError:
        pass

    provisional = ConfigStore(config_path(opts), _neutral_caps()).load()
    env, warnings = render_env(provisional)
    for warning in warnings:
        log_line(f"render_env {warning}")
    apply_env(env)

    Gtk, Gdk, GLib, ls = init_gtk()
    probe = diagnose_probe(ls)
    # The backend is chosen from the provisional load (same file, neutral caps):
    # render.backend has no caps-dependent validation, and the runtime must exist
    # before the monitors factory can be picked.
    runtime = start_gl_runtime() if provisional.render.backend == "gl" else None
    backend = "gl" if runtime is not None else "cairo"
    monitors = (lambda: gl_monitors(opts, runtime)) if runtime else \
        (lambda: display_monitors(opts))
    caps = Capabilities(probe["supported"], probe["protocol_version"],
                        tuple(probe["lib_version"]),
                        tuple(m.scale for m in monitors()))
    store = ConfigStore(config_path(opts), caps)
    cfg = store.load()
    for warning in store.warnings:
        log_line(f"config {warning}")
    if not caps.layer_shell_supported:
        eprint("matrix-rain: compositor does not expose zwlr_layer_shell_v1")
        return EXIT_NO_LAYER_SHELL
    if cfg.input.keyboard_mode == "exclusive" and caps.protocol_version < 1:
        log_line("keyboard_mode_downgraded protocol_absent")

    scheduler = GLibScheduler()
    if runtime is None:
        install_veil_css(Gdk.Display.get_default(), cfg.look.dim_alpha)  # cairo dim
    surface_factory = runtime.make_surface if runtime else \
        (lambda info, crop, **kw: OutputSurface(
            info=info, crop=crop, canvas=kw["canvas"], atlas=kw["atlas"],
            dim_alpha=kw["dim_alpha"], keyboard_mode=kw["keyboard_mode"],
            resync=kw["resync"], on_key=kw["on_key"], on_click=kw["on_click"],
            on_closed=kw["on_closed"], glow_enabled=kw["glow_enabled"]))
    try:
        overlay = Overlay(
            store=store, caps=caps,
            monitors=monitors,
            atlas_factory=lambda key: GlyphAtlas(key),
            surface_factory=surface_factory,
            scheduler=scheduler, clock=time.monotonic,
            key_resolver=lambda name: int(Gdk.keyval_from_name(name)),
            runtime=runtime,
            log=log_line, on_finished=lambda reason: Gtk.main_quit())
    except OverlayError as exc:
        eprint(f"matrix-rain: {exc}")
        release_lock(handle)
        return EXIT_CONFIG

    for signum, handler in signal_handlers(overlay).items():
        try:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum,
                                 glib_signal_callback(handler), None)
        except Exception as exc:  # pragma: no cover - very old GLib
            log_line(f"unix_signal_add_failed {signum} {exc}; using signal.signal")
            import signal as _sig

            _sig.signal(signum, lambda *_a: handler())

    def write_status():
        try:
            STATUS_PATH.write_text(as_json(overlay.status()), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            log_line(f"status_write_failed {exc}")
        return True

    scheduler.every(1000, write_status)
    if not watch_config(store, overlay, scheduler):
        log_line("warn live config reload unavailable")
    try:
        overlay.start()
    except OverlayError as exc:
        eprint(f"matrix-rain: {exc}")
        release_lock(handle)
        return EXIT_CONFIG
    Gtk.main()
    release_lock(handle)
    return EXIT_OK


def diagnose_probe(ls) -> dict[str, Any]:
    from .surface import probe_capabilities

    return probe_capabilities(ls)


def watch_config(store: ConfigStore, overlay, scheduler, GLib=None) -> bool:
    """T3: watch the *directory* (editors save by rename) and debounce.

    Returns True when the watcher is attached. A previous version probed the
    non-existent `GLib.File` and returned silently, so live config tuning was
    never wired at all - hence the explicit failure log and the return value.
    """
    from gi.repository import Gio

    directory = store.path.parent
    name = store.path.name
    pending = {"timer": None}

    def interesting(monitor, file, other_file, event) -> bool:
        for candidate in (file, other_file):
            if candidate is None:
                continue
            path = str(candidate.get_path())
            if path.endswith(name):
                return True
        return False

    def on_changed(monitor, file, other_file, event):
        if not interesting(monitor, file, other_file, event):
            return
        if pending["timer"] is not None:
            return

        def fire():
            pending["timer"] = None
            classes = overlay.reload()
            log_line(f"config_changed {sorted(classes) or 'rejected-or-unchanged'}")
            return False

        pending["timer"] = scheduler.once(150, fire)

    try:
        monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
    except Exception as exc:  # pragma: no cover
        log_line(f"config_watch_failed {type(exc).__name__} {exc}")
        return False
    if monitor is None:  # pragma: no cover - defensive
        log_line("config_watch_unsupported")
        return False
    monitor.connect("changed", on_changed)
    store._watcher = monitor  # noqa: SLF001 - GLib needs a strong reference
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = parse_args(argv)
    except UsageError as exc:
        eprint(f"matrix-rain: {exc}")
        return 64
    if opts.mode == "print-default-config":
        return print_default_config()
    if opts.mode == "self-test":
        return self_test(opts)
    if opts.mode == "monitor-list":
        return monitor_list(opts)
    if opts.mode == "diagnose":
        _Gtk, _Gdk, _GLib, ls = init_gtk()
        probe = diagnose_probe(ls)
        try:
            scales = tuple(m.scale for m in display_monitors(opts))
        except Exception as exc:  # pragma: no cover
            eprint(f"monitor enumeration failed: {exc}")
            scales = ()
        caps = _Capabilities(probe, scales)
        report = diagnose(ConfigStore(config_path(opts), caps), caps)
        if report.get("backend") == "gl":
            report["gl"] = gl_status()
        print(as_json(report))
        return int(report["exit"])
    if opts.mode == "status":
        return status_mode()
    if opts.mode == "stop":
        outcome = stop_instance(RUNTIME_LOCK, kill=os.kill, holder_alive=pid_alive)
        print(outcome)
        return EXIT_OK if outcome != "not-running" else 1
    return run(opts)


def _Capabilities(probe: dict[str, Any], scales: tuple[int, ...] = ()) -> Capabilities:
    return Capabilities(bool(probe["supported"]), int(probe["protocol_version"]),
                        tuple(probe["lib_version"]), scales)


def status_mode() -> int:
    pid = lock_pid(RUNTIME_LOCK)
    if pid is None or not pid_alive(pid):
        print(as_json({"state": "IDLE", "running": False, "lock": str(RUNTIME_LOCK)}))
        return 1
    try:
        live = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        live = {}
    live.setdefault("state", "RUNNING")
    live["running"] = True
    live["pid"] = pid
    print(as_json(live))
    return EXIT_OK
