# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""mwl: ctypes bindings for native/libmwl.so (raw Wayland + EGL/GLES2).

Kept deliberately thin: this module owns symbol wiring, event parsing and the
error string. All decisions about *what* to draw live in glpaint/glsurface,
which are testable without a compositor.

`ensure_library()` builds the .so on first use (and whenever mwl.c is newer), so
`matrix-rain` works straight after a checkout/copy without a manual build step.
"""
from __future__ import annotations

import ctypes
import dataclasses
import os
import pathlib
import subprocess
from typing import Sequence

PKG_DIR = pathlib.Path(__file__).resolve().parent
APP_DIR = PKG_DIR.parent
DEFAULT_LIB = APP_DIR / "native" / "libmwl.so"
ENV_LIB = "MATRIX_OVERLAY_LIBMWL"
ESCAPE_KEYSYM = 0xFF1B
XKB_ESCAPE_KEYCODE = 9          # evdev code 1 + 8


class MwlError(RuntimeError):
    """The native library is missing, failed to build, or refused a request."""


def library_path() -> pathlib.Path:
    override = os.environ.get(ENV_LIB)
    return pathlib.Path(override) if override else DEFAULT_LIB


def ensure_library(path: pathlib.Path | None = None, *, rebuild: bool = True) -> pathlib.Path:
    """Build libmwl.so when absent or stale. Raises MwlError with compiler output."""
    lib = path or library_path()
    native = lib.parent
    source = native / "mwl.c"
    if not lib.exists():
        if not (rebuild and source.exists()):
            raise MwlError(f"{lib} not found (and no {source} to build it from)")
    elif not rebuild or not source.exists() or source.stat().st_mtime <= lib.stat().st_mtime:
        return lib
    script = native / "build.sh"
    if not script.exists():
        raise MwlError(f"{script} missing - cannot build the GL backend")
    proc = subprocess.run(["sh", str(script)], cwd=str(native), capture_output=True,
                          text=True, timeout=180, check=False)
    if proc.returncode != 0 or not lib.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise MwlError("failed to build libmwl.so:\n  " + "\n  ".join(tail))
    return lib


@dataclasses.dataclass(frozen=True)
class Output:
    """A wl_output as seen by our own client, with xdg_output's connector name."""

    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    scale: int

    def signature(self) -> tuple:
        return (self.name, self.x, self.y, self.width, self.height, self.scale)


@dataclasses.dataclass(frozen=True)
class Event:
    """One queued native event: kind plus its integer payload."""

    kind: str
    args: tuple[int, ...] = ()
    text: str = ""

    @property
    def sid(self) -> int:
        """Surface id for the surface-scoped kinds, -1 otherwise.

        Scoped deliberately: on a `key` event args[0] is the keycode, so treating
        the first integer as a surface id would silently misroute input.
        """
        if self.kind in SURFACE_EVENT_KINDS and self.args:
            return self.args[0]
        return -1


SURFACE_EVENT_KINDS = frozenset({"configure", "closed", "kb_enter", "kb_leave",
                                "button"})


def parse_event(line: str) -> Event:
    """'configure 0 2560 1440' -> Event('configure', (0, 2560, 1440))."""
    parts = line.strip().split()
    if not parts:
        return Event("empty")
    kind = parts[0]
    nums: list[int] = []
    for tok in parts[1:]:
        try:
            nums.append(int(tok))
        except ValueError:
            break
    rest = " ".join(parts[1 + len(nums):]) if len(nums) < len(parts) - 1 else ""
    return Event(kind, tuple(nums), rest)


_C = ctypes


class Mwl:
    """Typed handle on libmwl.so. Single-threaded: call from the main loop only."""

    def __init__(self, path: pathlib.Path | None = None, *, rebuild: bool = True) -> None:
        self.path = path or library_path()
        if not self.path.exists():
            ensure_library(self.path, rebuild=rebuild)
        try:
            self._lib = _C.CDLL(str(self.path))
        except OSError as exc:  # pragma: no cover - broken build artefact
            raise MwlError(f"cannot load {self.path}: {exc}") from exc
        self._wire()
        self._initialised = False
        self._keysym_cache: dict[int, int] = {}

    # -------------------------------------------------------------- wiring --

    def _wire(self) -> None:
        lib, PI = self._lib, _C.POINTER(_C.c_int)
        lib.mwl_init.restype = _C.c_int
        lib.mwl_error.restype = _C.c_char_p
        lib.mwl_output_count.restype = _C.c_int
        lib.mwl_output_info.argtypes = [_C.c_int, PI, PI, PI, PI, PI,
                                        _C.c_char_p, _C.c_int]
        lib.mwl_output_info.restype = _C.c_int
        for fn, args in (("mwl_versions", (_C.c_char_p, _C.c_int)),
                         ("mwl_gl_info", (_C.c_char_p, _C.c_int)),
                         ("mwl_surface_create", (_C.c_int, _C.c_char_p, _C.c_int,
                                                 _C.c_int)),
                         ("mwl_surface_make_current", (_C.c_int,)),
                         ("mwl_surface_swap", (_C.c_int,)),
                         ("mwl_surface_scale", (_C.c_int,)),
                         ("mwl_surface_frame_ready", (_C.c_int,)),
                         ("mwl_surface_size", (_C.c_int, PI, PI)),
                         ("mwl_surface_geom", (_C.c_int, PI, PI)),
                         ("mwl_surface_destroy", (_C.c_int,)),
                         ("mwl_tex_create", (_C.c_int, _C.c_int,
                                             _C.POINTER(_C.c_ubyte))),
                         ("mwl_tex_destroy", (_C.c_uint,)),
                         ("mwl_clear", (_C.c_int, _C.c_float, _C.c_float, _C.c_float,
                                        _C.c_float)),
                         ("mwl_draw_quads", (_C.c_int, _C.c_uint,
                                             _C.POINTER(_C.c_float), _C.c_int,
                                             _C.c_int)),
                         ("mwl_set_geom", (_C.c_int,) * 8),
                         ("mwl_draw_cells", (_C.c_int, _C.c_uint,
                                             _C.POINTER(_C.c_float), _C.c_int,
                                             _C.POINTER(_C.c_int), _C.c_int,
                                             _C.c_int, _C.c_float, _C.c_int)),
                         ("mwl_dispatch", (_C.c_int,)),
                         ("mwl_next_event", (_C.c_char_p, _C.c_int))):
            getattr(lib, fn).argtypes = list(args)
        lib.mwl_tex_create.restype = _C.c_uint
        lib.mwl_next_event.restype = _C.c_int
        lib.mwl_fd.restype = _C.c_int
        lib.mwl_display_error_code.restype = _C.c_int
        lib.mwl_shutdown.restype = None

    # ----------------------------------------------------------- lifecycle --

    def init(self) -> None:
        if self._initialised:
            return
        if self._lib.mwl_init() != 0:
            raise MwlError(f"mwl_init failed: {self.error()}")
        self._initialised = True

    def shutdown(self) -> None:
        if not self._initialised:
            return
        self._lib.mwl_shutdown()
        self._initialised = False

    def error(self) -> str:
        raw = self._lib.mwl_error()
        return raw.decode("utf-8", "replace") if raw else "ok"

    def versions(self) -> str:
        return self._str(self._lib.mwl_versions)

    def gl_info(self) -> str:
        """GL_RENDERER/EGL_VENDOR: the proof that [render] render_node is honoured."""
        return self._str(self._lib.mwl_gl_info)

    def _str(self, fn, *args) -> str:
        buf = _C.create_string_buffer(512)
        rc = fn(*(list(args) + [buf, 512]))
        return buf.value.decode("utf-8", "replace") if rc == 0 else ""

    # -------------------------------------------------------------- outputs --

    def outputs(self) -> list[Output]:
        if not self._initialised:
            raise MwlError("outputs() before init()")
        out: list[Output] = []
        for i in range(self._lib.mwl_output_count()):
            # a tuple, not a generator: the byref() comprehension below consumes it
            vals = tuple(_C.c_int() for _ in range(5))
            xv, yv, wv, hv, sv = vals
            name = _C.create_string_buffer(64)
            rc = self._lib.mwl_output_info(i, *(_C.byref(v) for v in vals), name, 64)
            if rc != 0:
                raise MwlError(f"mwl_output_info({i}) failed: {self.error()}")
            out.append(Output(i, name.value.decode("utf-8", "replace"), xv.value,
                              yv.value, wv.value, hv.value, sv.value))
        return out

    # ------------------------------------------------------------- surfaces --

    def create_surface(self, output_index: int, *, namespace: str = "matrix-overlay",
                       exclusive_keyboard: bool = True,
                       exclusive_zone: int = -1) -> int:
        sid = self._lib.mwl_surface_create(output_index, namespace.encode(),
                                           1 if exclusive_keyboard else 0,
                                           int(exclusive_zone))
        if sid < 0:
            raise MwlError(f"create_surface(output {output_index}) failed: "
                           f"{self.error()}")
        return sid

    def surface_size(self, sid: int) -> tuple[int, int]:
        w, h = _C.c_int(), _C.c_int()
        if self._lib.mwl_surface_size(sid, _C.byref(w), _C.byref(h)) != 0:
            raise MwlError(f"surface_size({sid}): {self.error()}")
        return w.value, h.value

    def surface_scale(self, sid: int) -> int:
        return self._lib.mwl_surface_scale(sid)

    def surface_geom(self, sid: int) -> tuple[int, int]:
        x, y = _C.c_int(), _C.c_int()
        if self._lib.mwl_surface_geom(sid, _C.byref(x), _C.byref(y)) != 0:
            return (-1, -1)
        return x.value, y.value

    def make_current(self, sid: int) -> None:
        if self._lib.mwl_surface_make_current(sid) != 0:
            raise MwlError(f"make_current({sid}): {self.error()}")

    def swap(self, sid: int) -> None:
        if self._lib.mwl_surface_swap(sid) != 0:
            raise MwlError(f"swap({sid}): {self.error()}")

    def frame_ready(self, sid: int) -> bool:
        return bool(self._lib.mwl_surface_frame_ready(sid))

    def destroy_surface(self, sid: int) -> None:
        self._lib.mwl_surface_destroy(sid)

    # ----------------------------------------------------------- GL objects --

    def tex_create(self, width: int, height: int, rgba: bytes) -> int:
        buf = (ctypes.c_ubyte * len(rgba)).from_buffer_copy(rgba)
        tex = self._lib.mwl_tex_create(width, height, buf)
        if not tex:
            raise MwlError(f"tex_create({width}x{height}): {self.error()}")
        return int(tex)

    def tex_destroy(self, tex: int) -> None:
        self._lib.mwl_tex_destroy(_C.c_uint(tex))

    def clear(self, sid: int, colour: Sequence[float]) -> None:
        r, g, b, a = colour
        self._lib.mwl_clear(sid, r, g, b, a)

    def draw_quads(self, sid: int, tex: int, verts_ptr, n_verts: int,
                   blend: int = 0) -> None:
        if self._lib.mwl_draw_quads(sid, _C.c_uint(tex), verts_ptr, n_verts,
                                    blend) != 0:
            raise MwlError(f"draw_quads({sid}, {n_verts} verts): {self.error()}")

    def draw(self, sid: int, tex: int, batch, blend: int = 0) -> None:
        """Submit a QuadBatch with no copy; empty batches are skipped."""
        if not getattr(batch, "n_quads", 0):
            return
        ptr, n_verts = batch.as_ctypes()
        self.draw_quads(sid, tex, ptr, n_verts, blend)

    # ------------------------------------------------- native cell expansion --

    def set_geom(self, sid: int, crop, scale: int, uv_stride: int) -> None:
        """Teach one surface how to turn grid cells into device-pixel quads."""
        rc = self._lib.mwl_set_geom(sid, crop.col0, crop.row0, crop.x_px, crop.y_px,
                                    crop.cell_size, int(scale), int(uv_stride))
        if rc != 0:
            raise MwlError(f"set_geom({sid}): {self.error()}")

    def draw_cells(self, sid: int, tex: int, uv_table, batch, blend: int = 0,
                   pad: float = 0.0, heads_only: bool = False) -> int:
        """Submit indices for the native expander. Returns vertices drawn.

        `uv_table` is an array('f') owned by the caller (GlTextures): the buffer is
        read by reference, never copied, so it must outlive this call.
        """
        if not batch.n_cells:
            return 0
        from .glpaint import as_float_ptr

        uv_ptr = as_float_ptr(uv_table)
        cells_ptr, n_cells = batch.as_ctypes()
        rc = self._lib.mwl_draw_cells(sid, _C.c_uint(tex), uv_ptr, len(uv_table),
                                      cells_ptr, n_cells, blend, _C.c_float(pad),
                                      1 if heads_only else 0)
        if rc < 0:
            raise MwlError(f"draw_cells({sid}, {n_cells} cells): {self.error()}")
        return int(rc)

    # --------------------------------------------------------------- events --

    @property
    def fd(self) -> int:
        return self._lib.mwl_fd()

    def dispatch(self, timeout_ms: int = 0) -> int:
        return self._lib.mwl_dispatch(int(timeout_ms))

    def events(self, limit: int = 64) -> list[Event]:
        """Drain queued events. Never blocks: the pacer is the frame timer."""
        out: list[Event] = []
        buf = _C.create_string_buffer(256)
        while len(out) < limit and self._lib.mwl_next_event(buf, 256):
            out.append(parse_event(buf.value.decode("utf-8", "replace")))
        return out

    def keycode_to_keysym(self, keycode: int) -> int:
        """XKB keycode -> keysym, via Gdk's keymap when one is reachable.

        The GL backend keeps using Gdk for *this only* (no drawing): it is the
        authoritative mapping for a configurable stop_key. Falls back to the
        Escape mapping, which is the documented default.
        """
        cached = self._keysym_cache.get(keycode)
        if cached is not None:
            return cached
        sym = 0
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
            km = Gdk.Keymap.get_default()
            if km is not None:
                result = km.lookup_key(keycode, 0, 0)
                sym = int(result[0]) if isinstance(result, (tuple, list)) else int(result)
        except Exception:  # pragma: no cover - no display, or older signature
            sym = 0
        if not sym and keycode == XKB_ESCAPE_KEYCODE:
            sym = ESCAPE_KEYSYM
        self._keysym_cache[keycode] = sym
        return sym

    def display_error_code(self) -> int:
        return self._lib.mwl_display_error_code()
