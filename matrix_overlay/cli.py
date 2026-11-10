# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
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
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import Capabilities, Config, ConfigStore, load_default, replace_config
from .surface import MonitorInfo

EXIT_OK, EXIT_CONFIG, EXIT_NO_LAYER_SHELL, EXIT_LOCK, EXIT_DIAGNOSE = 0, 2, 3, 4, 5

APP_DIR = Path(__file__).resolve().parent.parent
# The distro layer: a packaged install ships this, and it applies when the user
# has never edited a config of their own (one file wins, no merge: see the
# config_path docstring).
SYSTEM_CONFIG = Path("/etc/matrix-overlay/config.toml")
RUNTIME_LOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "matrix-overlay.lock"
def state_dir() -> Path:
    """$XDG_STATE_HOME/matrix-overlay, with $HOME/.local/state as the fallback
    (the dotfile profile). A system install must not invent a second location."""
    base = os.environ.get("XDG_STATE_HOME") or Path(os.environ.get("HOME", "")) / ".local/state"
    return Path(base) / "matrix-overlay"


LOG_DIR = state_dir()
NVIDIA_JSON = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
MESA_JSON = "/usr/share/glvnd/egl_vendor.d/50_mesa.json"
AMD_VENDOR = "0x1002"
NVIDIA_VENDOR = "0x10de"


class UsageError(ValueError):
    pass


class NoWayland(UsageError):
    """A mode that has to ask the compositor something found no session.

    Distinct from a usage mistake: the command line was fine, the environment is
    not a graphical session. It gets its own status (3) and a one-line message
    instead of a traceback, because `--monitor-list` and `--diagnose` are exactly
    what someone runs when something is already wrong.
    """


@dataclasses.dataclass(frozen=True)
class Options:
    mode: str = "run"
    config: Path | None = None
    geometry: str | None = None
    config_env: bool = False


MODE_FLAGS = ("preflight", "print-default-config", "self-test", "monitor-list",
              "diagnose", "status", "stop")


def parse_args(argv: list[str]) -> Options:
    # exit_on_error is a constructor keyword, not an attribute set after the
    # fact: argparse reads it while parsing. False means its own errors come to
    # us as ArgumentError instead of calling sys.exit(). `--help` and `--version`
    # still exit through SystemExit(0) whatever we do, and that is a *success*
    # path: it used to be swept into UsageError, so `matrix-rain --help` printed
    # the help text and returned 64, which breaks any script or autopkgtest that
    # chains on it.
    parser = argparse.ArgumentParser(prog="matrix-rain", add_help=True,
                                     exit_on_error=False)
    parser.add_argument("--config", default=None)
    parser.add_argument("--geometry", default=None)
    parser.add_argument("--version", "-V", action="version",
                        version=f"matrix-rain {__version__}")
    for mode in MODE_FLAGS:
        parser.add_argument("--" + mode, action="store_true")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise  # --help / --version: let main() honour the success status
        # argparse has already written a precise complaint to stderr; say only
        # enough to be useful instead of echoing its exit code back.
        raise UsageError("invalid usage (run matrix-rain --help)") from exc
    except Exception as exc:  # ArgumentError and friends, with exit_on_error=False
        raise UsageError(str(exc)) from exc
    chosen = [m for m in MODE_FLAGS if getattr(ns, m.replace("-", "_"), False)]
    if len(chosen) > 1:
        raise UsageError("modes are mutually exclusive")
    if not chosen:
        return Options(mode="run", config=_cfgpath(ns), geometry=ns.geometry)
    return Options(mode=chosen[0], config=_cfgpath(ns), geometry=ns.geometry)


def _cfgpath(ns) -> Path | None:
    return Path(ns.config) if ns.config else None


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path(os.environ.get("HOME", "")) / ".config"
    return Path(base) / "matrix-overlay" / "config.toml"


def config_path(opts: Options) -> Path:
    """Chain: --config, then $MATRIX_OVERLAY_CONFIG, then the user's file, then
    the system one. Exactly one file is read, so a user file *replaces* the
    system defaults instead of merging with them - which is why the packaged
    system file holds only deliberate distro choices, never a full dump."""
    if opts.config is not None:
        return Path(opts.config)
    env = os.environ.get("MATRIX_OVERLAY_CONFIG")
    if env:
        return Path(env)
    user = user_config_path()
    if user.exists():
        return user
    if SYSTEM_CONFIG.exists():
        return SYSTEM_CONFIG
    return user


# --------------------------------------------------------- render-node env ---

SYSFS_DRM = Path("/sys/class/drm")
SYSFS_PCI = Path("/sys/bus/pci/devices")


@dataclasses.dataclass(frozen=True)
class Sysfs:
    """sysfs roots, injectable so the GPU selection is testable anywhere."""
    drm: Path = SYSFS_DRM
    pci: Path = SYSFS_PCI


SYSFS = Sysfs()


@dataclasses.dataclass(frozen=True)
class RenderPlan:
    """What the GPU decision was, not just the env it produced: a wrong pick on a
    headless build box used to be invisible."""
    env: dict[str, Any]
    warnings: list[str]
    source: dict[str, Any]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _pci_of(node: str, sysfs: Sysfs = SYSFS) -> str | None:
    """/dev/dri/renderD129 -> '0000:04:00.0' via the sysfs class link."""
    device = sysfs.drm / Path(node).name / "device"
    try:
        if not device.exists():
            return None
        return Path(device.resolve()).name
    except OSError:
        return None


def _pci_vendor(pci: str, sysfs: Sysfs = SYSFS) -> str | None:
    vendor = _read(sysfs.pci / pci / "vendor")
    return vendor.lower() if vendor else None


def _card_pci(card_dir: Path, sysfs: Sysfs) -> str | None:
    try:
        if not (card_dir / "device").exists():
            return None
        return Path((card_dir / "device").resolve()).name
    except OSError:
        return None


def _connected_counts(sysfs: Sysfs) -> dict[str, int]:
    """PCI function -> count of *connected* DRM connectors on it.

    Measured on widow: 0000:04:00.0 (RTX 3060) owns DP-5 + DP-6, 0000:11:00.0
    (RX 7900 XTX) owns none. On grogu 0000:66:00.0 (Phoenix1) owns HDMI-A-1.
    """
    counts: dict[str, int] = {}
    for status in sorted(sysfs.drm.glob("card[0-9]*-*/status")):
        if _read(status) != "connected":
            continue
        card = sysfs.drm / status.parent.name.split("-")[0]
        pci = _card_pci(card, sysfs)
        if pci:
            counts[pci] = counts.get(pci, 0) + 1
    return counts


def _vram_total(pci: str | None, sysfs: Sysfs) -> int:
    """amdgpu and nvidia both expose mem_info_vram_total on the PCI function."""
    raw = _read(sysfs.pci / pci / "mem_info_vram_total") if pci else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _render_nodes(sysfs: Sysfs) -> list[str]:
    names = [p.name for p in sysfs.drm.glob("renderD*") if p.is_dir()]
    return sorted(names, key=lambda n: (len(n), n))


def _node_exists(node: str, sysfs: Sysfs = SYSFS) -> bool:
    """Checked through the sysfs root, so an injected topology is authoritative
    (in production sysfs.drm *is* /sys/class/drm, and a /dev/dri node exists
    exactly when its /sys/class/drm counterpart does)."""
    if not node:
        return False
    try:
        return (sysfs.drm / Path(node).name).exists()
    except OSError:
        return False


def _candidates(sysfs: Sysfs) -> list[tuple[str, str]]:
    """[(renderDnnn, pci)] for every render node mappable to a PCI function."""
    out: list[tuple[str, str]] = []
    for name in _render_nodes(sysfs):
        pci = _pci_of(f"/dev/dri/{name}", sysfs)
        if pci:
            out.append((name, pci))
    return out


def pick_render_node(cfg_node: str, sysfs: Sysfs = SYSFS
                     ) -> tuple[str | None, str, list[str]]:
    """The M2 selection ladder. Returns (node, reason, warnings).

    1. an explicit node that exists always wins;
    2. an explicit node that does not exist is reported and auto resumes (a
       widow-only default must never hard-fail a different box);
    3. auto: the card owning the most *connected* DRM outputs;
    4. tie-break: most VRAM, then lowest PCI address - never the lowest render
       node number, which on widow would pick the off-display XTX (measured
       17.3 fps against 22.0 on the card that actually drives the screens).
       amdgpu exposes mem_info_vram_total; the NVIDIA driver exposes no mem_info_*
       on widow, so an unreadable size means 0 (unknown) and the connected-output
       step is the real decider on a mixed box;
    5. nothing connected at all: biggest GPU, flagged off_display;
    6. no render nodes (headless, container): no node, no pin.
    """
    warnings: list[str] = []
    if cfg_node:
        if _node_exists(cfg_node, sysfs):
            return cfg_node, "explicit", warnings
        warnings.append(f"render_node_missing:{cfg_node} (auto-detecting)")
    cands = _candidates(sysfs)
    if not cands:
        warnings.append("no_render_node: no /dev/dri/renderD* maps to a PCI function")
        return None, "no_render_node", warnings
    counts = _connected_counts(sysfs)
    best = max(counts.get(pci, 0) for _, pci in cands)
    if best > 0:
        pool = [(n, p) for n, p in cands if counts.get(p, 0) == best]
        reason = "auto_connected"
    else:
        pool, reason = cands, "auto_off_display"
    if len({p for _, p in pool}) > 1:
        warnings.append("ambiguous_gpu:" + ",".join(sorted({p for _, p in pool})))
    node, _pci = sorted(pool, key=lambda item: (-_vram_total(item[1], sysfs),
                                                item[1]))[0]
    return f"/dev/dri/{node}", reason, warnings


def render_plan(cfg: Config, sysfs: Sysfs | None = None) -> RenderPlan:
    """Environment for the app process, plus the GPU decision that made it.

    '' in the env dict means 'remove from the inherited env'. On the gl backend
    (default since M6) these variables are load-bearing: they choose the EGL
    vendor and therefore the GPU that renders. Verified live on widow -
    renderD129 answers "GL_RENDERER=NVIDIA GeForce RTX 3060", renderD128 answers
    "Radeon RX 7900 XTX (radeonsi, navi31, ACO)" and presents slower because it
    drives no connected output. A glvnd file that does not exist must never be
    pinned: glvnd then loads no vendor at all and EGL dies with NOT_INITIALIZED
    (0x3001) at native/mwl.c:532.
    """
    sysfs = sysfs or SYSFS
    node, reason, warnings = pick_render_node(cfg.render.render_node, sysfs)
    cands = _candidates(sysfs)
    counts = _connected_counts(sysfs)
    pci = _pci_of(node, sysfs) if node else None
    vendor_id = _pci_vendor(pci, sysfs) if pci else None
    vendor = {AMD_VENDOR: "amd", NVIDIA_VENDOR: "nvidia"}.get(vendor_id or "",
                                                               "unknown")
    connected = counts.get(pci, 0) if pci else 0
    multi_gpu = len({p for _, p in cands}) > 1

    env: dict[str, Any] = {
        "GDK_BACKEND": "wayland",
        "DISPLAY": "",                    # never let GDK pick Xwayland (:1 exists)
        "GI_TYPELIB_PATH": "",            # don't inherit another tool's typelib path
        "PYTHONPATH": str(APP_DIR),
        "MATRIX_OVERLAY_CONFIG": str(config_path(Options())),
        "LIBGL_ALWAYS_SOFTWARE": "",
    }
    json_path = ""
    if node:
        if cfg.render.egl_vendor:
            json_path = cfg.render.egl_vendor
            if vendor == "nvidia" and json_path.endswith("50_mesa.json"):
                warnings.append(f"egl_vendor_mismatch:{node} is nvidia but mesa was requested")
        else:
            default = MESA_JSON if vendor == "amd" else NVIDIA_JSON
            if Path(default).exists():
                json_path = default
            else:
                warnings.append(f"glvnd_vendor_json_missing:{default} "
                                "(leaving glvnd to auto-discover)")
        if json_path:
            env["__EGL_VENDOR_LIBRARY_FILENAMES"] = json_path
    if pci and connected == 0:
        warnings.append(f"off_display_gpu:{node} (pci {pci}) drives no connected "
                        "output; expect a cross-device copy path")
    if vendor == "amd":
        env["MESA_LOADER_DRIVER_OVERRIDE"] = "radeonsi"
    if pci and connected == 0 and multi_gpu:
        env["DRI_PRIME"] = "pci-" + pci.replace(":", "_").replace(".", "_")
    source = {
        "node": node, "requested": cfg.render.render_node, "pci": pci or "",
        "vendor": vendor, "vendor_id": vendor_id or "",
        "json": json_path, "reason": reason, "connected": connected,
        "gpus": len({p for _, p in cands}), "vram_total": _vram_total(pci, sysfs),
    }
    return RenderPlan(env=env, warnings=warnings, source=source)


def render_env(cfg: Config, sysfs: Sysfs | None = None) -> tuple[dict[str, Any], list[str]]:
    """Back-compatible view of render_plan: (env, warnings)."""
    plan = render_plan(cfg, sysfs)
    return plan.env, plan.warnings


def render_source(cfg: Config, sysfs: Sysfs | None = None) -> dict[str, Any]:
    """Which GPU was chosen and why - the part --diagnose used not to show."""
    return render_plan(cfg, sysfs).source


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
    plan = render_plan(cfg)
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
        "render": plan.env,
        "render_warnings": plan.warnings,
        "render_source": plan.source,
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

    try:
        import cairo  # gi.repository.cairo: a CPU render, so this needs no GPU
    except ImportError as exc:  # the headless proof must fail loudly, not traceback
        eprint(f"self-test cannot render: {exc}. Install python3-gi-cairo "
               "(the GL backend does not need it; --preflight proves that path).")
        return EXIT_DIAGNOSE

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
    # Naming Gdk explicitly: without it PyGObject loads whichever Gdk version is
    # already resident and prints a warning onto stderr - which, for a JSON mode
    # like --monitor-list, is noise where an answer should be.
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk

    from .surface import select_monitors, unmatched_entries

    display = Gdk.Display.get_default()
    if display is None:
        raise NoWayland("no Wayland display (is WAYLAND_DISPLAY set?)")
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

    Nothing the user can perceive happens here: no buffer is ever attached to the
    probe surface, no keyboard is grabbed, and the surface is destroyed before the
    call returns. Compiling libmwl.so *is* allowed - a packaged profile that
    ships no prebuilt .so in a read-only tree could not report its real GL strings
    otherwise, and the compile is exactly what the first launch does anyway
    (measured on grogu during M4: the old "no build permitted" probe reported a
    healthy AMD install as broken).

    The handle is created with rebuild=False so a stale artefact is compiled once,
    by us, with its output in the log, rather than inside ctypes loading.
    """
    from .mwl import Mwl, MwlError, ensure_library, library_path

    path = library_path()
    out: dict[str, Any] = {"library": str(path), "built": path.exists()}
    try:
        path = ensure_library(path)     # may compile; the answer is what we report
        out["library"] = str(path)
        out["built"] = path.exists()
        handle = Mwl(path, rebuild=False)
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
    try:
        infos = cli_monitors(opts)
    except NoWayland as exc:
        eprint(f"matrix-rain: {exc}")
        return EXIT_NO_LAYER_SHELL
    print(as_json([{f.name: getattr(m, f.name) for f in dataclasses.fields(m)
                    if f.name != "monitor"} | {"key": m.name} for m in infos]))
    return EXIT_OK


# ------------------------------------------------------- backend gating ---

# The two backends need different things, and conflating them is what made the
# wrapper (and run()) demand GtkLayerShell from a process that never uses it:
# the GL client speaks layer-shell itself through native/libmwl.so. On a box
# without gir1.2-gtklayershell-0.1 that turned a working GPU path into exit 3.
NATIVE_LIB = (255, 255, 255)   # sentinel: libgtk-layer-shell is not in play
BUILD_TOOLS = ("gcc", "wayland-scanner", "pkg-config")
HEADER_PATHS = ("/usr/include/EGL/egl.h", "/usr/include/GLES2/gl2.h",
                "/usr/include/wayland-client.h")
PROTOCOL_XMLS = tuple(str(APP_DIR / "native" / name) for name in
                      ("wlr-layer-shell-unstable-v1.xml",
                       "xdg-output-unstable-v1.xml", "xdg-shell.xml"))
TYPELIB_VERSIONS = {"Gdk": "3.0", "Gtk": "3.0", "GtkLayerShell": "0.1",
                    "GLib": "2.0", "Gio": "2.0"}


def which(name: str) -> str | None:
    """Indirection so the build-blocker report is testable."""
    return shutil.which(name)


def required_typelibs(backend: str, gl_available: bool) -> tuple[str, ...]:
    """GObject-Introspection namespaces this run actually needs.

    GtkLayerShell belongs to the cairo backend only - but the fallback ladder
    makes it mandatory again the moment GL is unusable, which is why the input
    is (backend, gl_available) and not the backend name alone.
    """
    libs = ["GLib", "Gio", "Gdk", "Gtk"]
    if backend != "gl" or not gl_available:
        libs.append("GtkLayerShell")
    return tuple(libs)


def parse_versions(text: str) -> dict[str, str]:
    """'layer_shell=4 compositor=4 seat=5 xdg_output=yes' -> dict."""
    out: dict[str, str] = {}
    for chunk in (text or "").split():
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def caps_from_native(versions_text: str, scales: tuple[int, ...] = ()
                     ) -> Capabilities:
    """Capabilities measured by our own Wayland client, not by libgtk-layer-shell.

    lib_version is a sentinel: the '<0.6 has no keyboard_mode' rule is about
    libgtk-layer-shell, and asserting it against the native path would invent a
    blocker that does not exist (exclusive keyboard is ours, gated on the
    layer-shell protocol version).
    """
    parsed = parse_versions(versions_text)
    try:
        protocol = int(parsed.get("layer_shell", "0"))
    except ValueError:
        protocol = 0
    return Capabilities(protocol >= 1, protocol, NATIVE_LIB, tuple(scales))


def _typelib_ok(ns: str, version: str) -> bool:
    try:
        import gi

        gi.require_version(ns, version)
        __import__("gi.repository", fromlist=[ns])
        return True
    except (ImportError, ValueError):
        return False


def gl_usable() -> tuple[bool, str]:
    """Can the GL backend run here, before we try it?

    A prebuilt libmwl.so is enough. Otherwise the build has to be possible, and
    saying *which* prerequisite is missing is the whole point: the grogu box had
    gcc and wayland-scanner but no EGL/GLES dev headers, and the old failure
    message blamed the compositor.
    """
    from . import mwl as mwl_mod

    path = mwl_mod.library_path()
    if path.exists():
        return True, f"libmwl.so built at {path}"
    missing_tools = [tool for tool in BUILD_TOOLS if not which(tool)]
    missing_headers = [h for h in HEADER_PATHS if not Path(h).exists()]
    missing_xmls = [x for x in PROTOCOL_XMLS if not Path(x).exists()]
    parts: list[str] = []
    if missing_tools:
        parts.append("missing tools: " + " ".join(missing_tools))
    if missing_headers:
        parts.append("missing headers: " + " ".join(missing_headers))
    if missing_xmls:
        parts.append("missing protocol xml: " + " ".join(missing_xmls))
    return False, ("; ".join(parts) or f"libmwl.so not built at {path}")


def preflight(opts: Options) -> int:
    """--preflight: is *any* backend usable here? The wrapper runs this instead
    of importing cairo's dependencies unconditionally."""
    cfg = ConfigStore(config_path(opts), _neutral_caps()).load()
    backend = cfg.render.backend
    gl_ok, gl_why = gl_usable()
    if backend == "gl" and gl_ok:
        print(f"matrix-rain: gl backend ready ({gl_why})")
        return EXIT_OK
    reasons = [] if gl_ok else [f"gl:{gl_why}"]
    need = [(ns, TYPELIB_VERSIONS[ns]) for ns in required_typelibs(backend, gl_ok)]
    missing = [ns for ns, version in need if not _typelib_ok(ns, version)]
    if not missing:
        if backend == "gl":
            print(f"matrix-rain: GL unusable ({gl_why}); the cairo fallback is ready")
        return EXIT_OK
    reasons.append(f"cairo: missing typelibs {' '.join(missing)} "
                   "(install gir1.2-gtklayershell-0.1 python3-gi python3-gi-cairo)")
    eprint("matrix-rain: no usable backend - " + "; ".join(reasons))
    return EXIT_NO_LAYER_SHELL


def init_gtk(need_layer_shell: bool = True):
    """Import the GTK3 stack. GtkLayerShell is required by the cairo backend only:
    the GL client speaks layer-shell itself, so demanding it there meant a native
    app refused to start over a dependency it never touched (the grogu case)."""
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    if need_layer_shell:
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import Gdk, GLib, Gtk, GtkLayerShell
    else:
        from gi.repository import Gdk, GLib, Gtk

        GtkLayerShell = None

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
    plan = render_plan(provisional)
    for warning in plan.warnings:
        log_line(f"render_env {warning}")
    log_line(f"render_source {as_json(plan.source)}")
    apply_env(plan.env)

    # The backend is chosen from the provisional load (same file, neutral caps):
    # render.backend has no caps-dependent validation, and the runtime must exist
    # before the monitors factory can be picked. Starting it first is also what
    # tells us whether the cairo-only typelibs are needed at all.
    runtime = start_gl_runtime() if provisional.render.backend == "gl" else None
    backend = "gl" if runtime is not None else "cairo"
    log_line(f"backend_selected {backend}")
    try:
        Gtk, Gdk, GLib, ls = init_gtk(need_layer_shell=(runtime is None))
    except (ImportError, ValueError) as exc:
        log_line(f"cairo_deps_unavailable {type(exc).__name__}: {exc}")
        eprint("matrix-rain: the GL backend is unusable and the cairo fallback "
               f"needs the GTK3/GtkLayerShell typelibs ({type(exc).__name__}: {exc}); "
               "install gir1.2-gtklayershell-0.1")
        release_lock(handle)
        return EXIT_NO_LAYER_SHELL
    probe = diagnose_probe(ls) if ls is not None else None
    monitors = (lambda: gl_monitors(opts, runtime)) if runtime else \
        (lambda: display_monitors(opts))
    scales = tuple(m.scale for m in monitors())
    if runtime is not None:
        caps = caps_from_native(runtime.handle.versions(), scales)
    else:
        caps = Capabilities(probe["supported"], probe["protocol_version"],
                            tuple(probe["lib_version"]), scales)
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
    except SystemExit as exc:
        # argparse already wrote the help/version text (or its own error);
        # honour its status instead of inventing one.
        return int(exc.code or 0)
    except UsageError as exc:
        eprint(f"matrix-rain: {exc}")
        return 64
    if opts.mode == "print-default-config":
        return print_default_config()
    if opts.mode == "self-test":
        return self_test(opts)
    if opts.mode == "monitor-list":
        return monitor_list(opts)
    if opts.mode == "preflight":
        return preflight(opts)
    if opts.mode == "diagnose":
        provisional = ConfigStore(config_path(opts), _neutral_caps()).load()
        native = None
        if provisional.render.backend == "gl" and gl_usable()[0]:
            # The native client can answer for itself: no GtkLayerShell needed,
            # so --diagnose still works on a box with no GTK layer-shell typelib.
            native = gl_status()
            caps = caps_from_native(native.get("versions", ""),
                                    tuple(int(o.get("scale", 1))
                                          for o in native.get("outputs", ())))
        else:
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
            report["gl"] = native if native is not None else gl_status()
        print(as_json(report))
        return int(report["exit"])
    if opts.mode == "status":
        return status_mode()
    if opts.mode == "stop":
        outcome = stop_instance(RUNTIME_LOCK, kill=os.kill, holder_alive=pid_alive)
        print(outcome)
        return EXIT_OK if outcome != "not-running" else 1
    try:
        return run(opts)
    except NoWayland as exc:
        # Same shape as the diagnostic modes: say what is missing, once, and use
        # the status the man page promises.
        eprint(f"matrix-rain: {exc}")
        return EXIT_NO_LAYER_SHELL


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
