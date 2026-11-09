# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
"""Layer-shell surfaces: veil CSS, the C5 call sequence, monitor selection.

Split so the parts that decide compositor behaviour are testable with injected
doubles (plan 1.5): `configure_layer_surface`, `veil_css`, `select_monitors`
need no compositor; `OutputSurface` is the thin GTK integration layer on top.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import Any

from .render import FrameBuffer, Renderer, merge_rows
from .world import Crop, Geom, OutputFrame, WorldCanvas

NAMESPACE = "matrix-overlay"
VEIL_WIDGET = "matrix-veil"
CANVAS_WIDGET = "matrix-canvas"
MODES = ("exclusive", "on_demand", "none")


class LayerShellConfigError(RuntimeError):
    """The requested layer configuration cannot honour the input contract."""


def _fmt(x: float) -> str:
    s = ("%.4f" % x).rstrip("0").rstrip(".")
    return s if "." in s else s + ".0"


def window_title(monitor_name: str) -> str:
    return f"{NAMESPACE}:{monitor_name}"


def veil_css(dim_alpha: float) -> str:
    """The dim lives here and nowhere else: the renderer must never paint a
    background (C4). The canvas is transparent so clearing a cell to alpha 0
    reveals the veil, i.e. the correct dim, with no full-surface pass."""
    alpha = max(0.0, min(0.95, float(dim_alpha)))
    return (f"#{VEIL_WIDGET} {{ background-color: rgba(0, 0, 0, {_fmt(alpha)}); "
            "border-width: 0; padding: 0; margin: 0; }\n"
            f"#{CANVAS_WIDGET} {{ background-color: transparent; }}\n")


@dataclasses.dataclass(frozen=True)
class MonitorInfo:
    """Logical geometry + scale, from Gdk (D5: GTK3 exposes no connector name)."""

    index: int
    model: str
    x: int
    y: int
    width: int
    height: int
    scale: int = 1
    monitor: Any = None

    @property
    def name(self) -> str:
        return f"{self.index}:{self.model}"

    def geom(self) -> Geom:
        return Geom(self.name, self.x, self.y, self.width, self.height)

    def signature(self) -> tuple:
        """Identity used to decide whether a surface survives a topology reload
        (T6: an unchanged output must keep its mapped veil)."""
        return (self.index, self.model, self.x, self.y, self.width, self.height, self.scale)


def _info(display, i: int) -> MonitorInfo:
    mon = display.get_monitor(i)
    geo = mon.get_geometry()
    try:
        model = mon.get_model() or ""
    except Exception:  # pragma: no cover - ancient Gdk
        model = ""
    return MonitorInfo(index=i, model=model, x=geo.x, y=geo.y,
                       width=geo.width, height=geo.height,
                       scale=int(mon.get_scale_factor() or 1), monitor=mon)


def _matches(info: MonitorInfo, entry: str) -> bool:
    """Digits mean *index only*; anything else is a case-insensitive model match.

    A substring test on digits is wrong: the entry "1" also occurs inside model
    names like "DELL S2817Q", which silently selected the wrong output (caught by
    the live smoke test, not by the unit tests - the fixture models had no '1').
    """
    entry = entry.strip()
    if not entry:
        return False
    if entry.isdigit():
        return int(entry) == info.index
    return entry.lower() in info.model.lower()


def filter_infos(infos: Sequence[MonitorInfo], *, mode: str,
                 listed: Sequence[str] = ()) -> list[MonitorInfo]:
    """Apply outputs.mode / outputs.listed (D5 matching rules)."""
    if mode != "listed":
        return list(infos)
    keys = [str(x) for x in listed]
    return [m for m in infos if any(_matches(m, k) for k in keys)]


def select_monitors(display, *, mode: str, listed: Sequence[str] = ()) -> list[MonitorInfo]:
    return filter_infos([_info(display, i) for i in range(display.get_n_monitors())],
                        mode=mode, listed=listed)


def unmatched_in(infos: Sequence[MonitorInfo], *, listed: Sequence[str] = ()) -> tuple[str, ...]:
    """Listed entries that matched nothing. Split out from the Gdk-based variant so
    the GL backend can apply the same rules to its own output list."""
    return tuple(k for k in (str(x) for x in listed)
                 if not any(_matches(m, k) for m in infos))


def unmatched_entries(display, *, listed: Sequence[str]) -> tuple[str, ...]:
    infos = [_info(display, i) for i in range(display.get_n_monitors())]
    return unmatched_in(infos, listed=listed)


def configure_layer_surface(ls, win, monitor, *, mode: str = "exclusive",
                            namespace: str = NAMESPACE) -> None:
    """C5 order. EXCLUSIVE is only honoured on TOP/OVERLAY (D1.2a) and must be
    committed before the first commit, or Escape never arrives (T1)."""
    if mode not in MODES:
        raise LayerShellConfigError(f"unknown keyboard mode {mode!r}, expected {MODES}")
    if mode == "on_demand":
        version = ls.get_protocol_version() if hasattr(ls, "get_protocol_version") else 0
        if version < 4:
            raise LayerShellConfigError(
                f"on_demand needs layer-shell protocol >= 4, compositor offers {version}")

    ls.init_for_window(win)
    ls.set_namespace(win, namespace)
    ls.set_layer(win, ls.Layer.OVERLAY)
    if monitor is not None:
        ls.set_monitor(win, monitor)
    for edge in ("TOP", "LEFT", "RIGHT", "BOTTOM"):
        ls.set_anchor(win, getattr(ls.Edge, edge), True)
    ls.set_exclusive_zone(win, 0)  # never resize the COSMIC panel

    if mode == "exclusive":
        value = ls.KeyboardMode.EXCLUSIVE
    elif mode == "on_demand":
        value = ls.KeyboardMode.ON_DEMAND
    else:
        value = ls.KeyboardMode.NONE
    if hasattr(ls, "set_keyboard_mode"):
        ls.set_keyboard_mode(win, value)
    else:  # pragma: no cover - library < 0.6 (D1.4 shim)
        ls.set_keyboard_interactivity(win, mode != "none")


def probe_capabilities(ls) -> dict[str, Any]:
    """For --diagnose / M0 gate. is_supported() may block on a roundtrip."""
    out: dict[str, Any] = {"lib_version": None, "supported": False, "protocol_version": 0,
                           "has_keyboard_mode": hasattr(ls, "set_keyboard_mode")}
    try:
        out["lib_version"] = (ls.get_major_version(), ls.get_minor_version(),
                              ls.get_micro_version())
    except Exception:  # pragma: no cover
        pass
    try:
        out["supported"] = bool(ls.is_supported())
        out["protocol_version"] = int(ls.get_protocol_version())
    except Exception:  # pragma: no cover
        pass
    return out


# ------------------------------------------------------- integration layer ---


def _monotonic() -> float:
    import time

    return time.monotonic()


class OutputSurface:
    """One full-output OVERLAY surface: veil + canvas + renderer + input."""

    def __init__(self, *, info: MonitorInfo, crop: Crop, canvas: WorldCanvas,
                 atlas, dim_alpha: float, keyboard_mode: str,
                 resync: Callable[[], OutputFrame], on_key: Callable[[int], bool],
                 on_click: Callable[[], None], on_closed: Callable[[str], None],
                 glow_enabled: bool) -> None:
        from gi.repository import Gdk, Gtk  # imported late: pure tests stay display-free

        self.info = info
        self.crop = crop
        self._resync = resync
        self._buffer = FrameBuffer(crop.name)
        self._need_sync = False
        self._destroyed = False
        self.last_draw_s = 0.0

        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_name(VEIL_WIDGET)
        win.set_title(window_title(info.name))
        win.set_decorated(False)
        win.set_resizable(False)
        configure_layer_surface(_layer_shell(), win, info.monitor, mode=keyboard_mode)
        win.set_size_request(info.width, info.height)  # inert under 4 anchors (D1.2b)

        area = Gtk.DrawingArea()
        area.set_name(CANVAS_WIDGET)
        area.set_hexpand(True)
        area.set_vexpand(True)
        win.add(area)

        # Damage is the surface's business (widget invalidation); the renderer only
        # paints. Per-cell rects are coalesced per row band (render.merge_rows).
        self.renderer = Renderer(crop=crop, canvas=canvas, atlas=atlas, scale=info.scale,
                                 glow_enabled=glow_enabled)
        self.renderer.on_resync = self.request_resync
        self._area = area
        self._win = win
        self._escape_keycode = Gdk.KEY_Escape
        self._on_key = on_key
        self._on_click = on_click
        self._on_closed = on_closed
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        area.connect("draw", self._on_draw)
        area.connect("button-press-event", self._on_button)
        win.connect("key-press-event", self._on_key_press)
        win.connect("delete-event", lambda *_a: True)  # T16: never let 0.8.2 self-close
        win.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------ ops --

    def show(self) -> None:
        self._win.show_all()
        self.request_resync()

    def destroy(self) -> None:
        if not self._destroyed:
            self._win.destroy()

    def queue(self, frame: OutputFrame) -> None:
        """Compose the delta into the pending buffer, then damage those bands.

        Merge, never accumulate: a draw can cost more than the 33 ms tick interval,
        and appending frames made each draw heavier until the GLib loop never yielded
        (measured ~10 fps with signals ignored - a livelock, not a slow renderer).
        """
        self._buffer.add(frame)
        for rect in merge_rows(self._buffer.rects(self.renderer)):
            self._queue_damage(rect)

    def _take_merged(self) -> OutputFrame | None:
        return self._buffer.take()

    def request_resync(self) -> None:
        self._need_sync = True
        self._area.queue_draw()

    def present(self) -> None:
        """No-op for this backend: GDK paints from the `draw` signal.

        Exists so Overlay.tick() can call present() uniformly on either backend
        (the GL one submits its vertex batches and swaps here).
        """

    def set_dim(self, dim_alpha: float) -> None:
        _apply_veil_css(self._win, dim_alpha)

    def set_allocation_size(self, width: int, height: int) -> None:
        """T15: the configure event is authoritative, not Gdk geometry."""
        self._alloc = (width, height)

    @property
    def screen_cells(self) -> int:
        return len(self.renderer.screen)

    # ------------------------------------------------------------- handlers --

    def _queue_damage(self, rect) -> None:
        x, y, w, h = rect
        self._area.queue_draw_area(int(x), int(y), max(1, int(w)), max(1, int(h)))

    def _on_draw(self, _widget, cr):
        t0 = _monotonic()
        if self._need_sync:
            self._need_sync = False
            self._buffer.clear()
            self.renderer.sync(cr, self._resync())
        else:
            frame = self._take_merged()
            if frame is not None:
                self.renderer.apply(cr, frame)
        self.last_draw_s = _monotonic() - t0
        return False

    def _on_key_press(self, _win, event):
        return self._on_key(event.keyval)

    def _on_button(self, _widget, _event):
        self._on_click()
        return True

    def _on_destroy(self, *_a) -> None:
        self._destroyed = True
        self._on_closed(self.info.name)


_LS = None
_PROVIDER = None


def _layer_shell():
    global _LS
    if _LS is None:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import GtkLayerShell

        _LS = GtkLayerShell
    return _LS


def _apply_veil_css(widget, dim_alpha: float) -> None:
    """Reload the provider instead of stacking one per hot-reload (T3)."""
    from gi.repository import Gtk

    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            widget.get_display().get_default_screen(), _PROVIDER,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _PROVIDER.load_from_data(veil_css(dim_alpha).encode("utf-8"))


def install_veil_css(display, dim_alpha: float) -> None:
    from gi.repository import Gtk

    provider = Gtk.CssProvider()
    provider.load_from_data(veil_css(dim_alpha).encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(
        display.get_default_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    globals()["_PROVIDER"] = provider
