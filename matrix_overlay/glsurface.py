"""glsurface: the GL presentation backend - counterpart of surface.OutputSurface.

It implements the same contract app.py already consumes (show/queue/present/
destroy/set_dim/request_resync/screen_cells/renderer/info/crop/last_draw_s), but
the pixels reach the compositor as EGL/dmabuf buffers instead of the ~74 MB per
frame shm copy that pinned a core.

Unlike the Cairo renderer there is no damage, clip or pending-backlog machinery:
each frame redraws the authoritative cell set, which the GPU does in ~1 ms
(measured: 60 fps at 20 000 quads per output on 9% of one core). That removes the
whole class of livelocks the Cairo path needed FrameBuffer to survive.
"""
from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Sequence
from typing import Any

from .glpaint import (AtlasLayout, CellBatch, CellGeom, QuadBatch, atlas_cells,
                      clear_colour, emit_cells, emit_glow, emit_indexed, pack_atlas,
                      uv_table)
from .mwl import Event, Mwl
from .surface import MonitorInfo
from .world import BUCKETS, Crop, Draw, OutputFrame, WorldCanvas

NAMESPACE = "matrix-overlay"
KEYBOARD_MODES = ("exclusive", "on_demand", "none")
EXCLUSIVE_MODES = ("exclusive", "on_demand")
OVER = 0                                  # premultiplied glyph bodies
ADDITIVE = 1                              # baked head bloom
MAX_EVENTS_PER_PUMP = 64


def _monotonic() -> float:
    return time.monotonic()


def monitor_infos(handle: Any) -> list[MonitorInfo]:
    """MonitorInfo records straight from our own output enumeration.

    Better than the Gdk view it replaces: xdg_output reports real connector names
    (DP-4/DP-5), so outputs.listed can select by connector, and the shared column
    grid has one source of geometry truth instead of two.
    """
    infos: list[MonitorInfo] = []
    for out in handle.outputs():
        infos.append(MonitorInfo(index=out.index, model=out.name, x=out.x, y=out.y,
                                 width=out.width, height=out.height, scale=out.scale,
                                 monitor=None))
    return infos


@dataclasses.dataclass(frozen=True)
class GlTextures:
    """Uploaded atlases for one AtlasKey, shared by every output with that key.

    Also owns the uv tables: the native expander reads them by pointer, so they
    must live as long as any frame submission that references them.
    """

    key: Any
    cell_px: int
    layout: AtlasLayout
    cell_tex: int
    uv: Any = None                                  # array('f'), 4 per slot
    glow_layout: AtlasLayout | None = None
    glow_tex: int = 0
    glow_uv: Any = None
    pad_px: int = 0

    def release(self, handle: Any) -> None:
        handle.tex_destroy(self.cell_tex)
        if self.glow_tex:
            handle.tex_destroy(self.glow_tex)


def build_textures(handle: Any, atlas: Any, glow_enabled: bool) -> GlTextures:
    """Pack + upload the atlas for one GlyphAtlas. One upload per atlas, never per
    frame (plan C4 carries over: no per-frame text or conversion work)."""
    layout = AtlasLayout(n_glyphs=atlas.n_glyphs, buckets=BUCKETS,
                         cell_px=atlas.cell_px)
    blob, _cells = pack_atlas(layout, atlas_cells(atlas))
    cell_tex = handle.tex_create(layout.width, layout.height, blob)

    glow_layout: AtlasLayout | None = None
    glow_tex = 0
    pad = int(getattr(atlas, "glow_pad", 0) or 0)
    if glow_enabled and pad:
        glow_layout = AtlasLayout(n_glyphs=atlas.n_glyphs, buckets=BUCKETS,
                                  cell_px=atlas.cell_px + 2 * pad)
        gblob, _gn = pack_atlas(glow_layout, atlas_cells(atlas, glow=True))
        glow_tex = handle.tex_create(glow_layout.width, glow_layout.height, gblob)

    return GlTextures(key=atlas.key, cell_px=atlas.cell_px, layout=layout,
                      cell_tex=cell_tex, uv=uv_table(layout),
                      glow_layout=glow_layout, glow_tex=glow_tex,
                      glow_uv=uv_table(glow_layout) if glow_layout else None,
                      pad_px=pad if glow_layout else 0)


@dataclasses.dataclass
class GlRuntime:
    """Owns the native handle, the texture cache and the Wayland event routing.

    One runtime per process: the surfaces are per output, the EGL context and the
    atlas textures are shared, and the event queue must be drained once per tick
    for all of them.
    """

    handle: Any = None
    log: Callable[[str], None] = print
    exclusive_zone: int = -1
    namespace: str = NAMESPACE
    started: bool = False
    _context_logged: bool = False
    _textures: dict[Any, GlTextures] = dataclasses.field(default_factory=dict)
    surfaces: list[Any] = dataclasses.field(default_factory=list)
    _focused_sid: int = -1

    def __post_init__(self) -> None:
        if self.handle is None:
            self.handle = Mwl()

    # ------------------------------------------------------------ lifecycle --

    def start(self) -> GlRuntime:
        self.handle.init()
        self.started = True
        return self

    def describe(self) -> str:
        """Diagnostics line: protocol versions plus the GL strings, which prove
        that [render] render_node selected the GPU it claims to."""
        parts = [self.handle.versions(), self.handle.gl_info()]
        return " | ".join(str(p) for p in parts if p)

    def shutdown(self) -> None:
        for surf in list(self.surfaces):
            surf.destroy()
        self.surfaces = []
        for tex in self._textures.values():
            tex.release(self.handle)
        self._textures.clear()
        if self.started:
            self.handle.shutdown()
            self.started = False

    # --------------------------------------------------------------- inputs --

    def exclusive_keyboard_for(self, mode: str) -> bool:
        """layer-shell interactivity for a config keyboard_mode."""
        if mode not in KEYBOARD_MODES:
            raise ValueError(f"unknown keyboard mode {mode!r}, expected {KEYBOARD_MODES}")
        return mode in EXCLUSIVE_MODES

    # -------------------------------------------------------------- textures --

    def textures_for(self, atlas: Any, glow_enabled: bool) -> GlTextures:
        """Cached by (AtlasKey, glow): two outputs at the same scale share one
        upload, and an L2 look change produces exactly one new pair."""
        key = (getattr(atlas, "key", None), bool(glow_enabled))
        tex = self._textures.get(key)
        if tex is None:
            tex = build_textures(self.handle, atlas, glow_enabled)
            self._textures[key] = tex
        return tex

    # -------------------------------------------------------------- surfaces --

    def make_surface(self, info: MonitorInfo, crop: Crop, **kw: Any) -> Any:
        mode = kw.get("keyboard_mode", "exclusive")
        sid = self.handle.create_surface(
            info.index, namespace=self.namespace,
            exclusive_keyboard=self.exclusive_keyboard_for(mode),
            exclusive_zone=self.exclusive_zone)
        surf = GlSurface(runtime=self, sid=sid, info=info, crop=crop, **kw)
        self.surfaces.append(surf)
        if not self._context_logged:
            # Only now does a current context exist, so only now can we report which
            # GPU actually answered - the proof that [render] render_node is honoured.
            self._context_logged = True
            self.log(f"gl_context {self.handle.gl_info()}")
        return surf

    def forget(self, surf: Any) -> None:
        if surf in self.surfaces:
            self.surfaces.remove(surf)

    # ------------------------------------------------------------------ draw --

    def make_current(self, sid: int) -> None:
        self.handle.make_current(sid)

    def clear(self, sid: int, colour: Sequence[float]) -> None:
        self.handle.clear(sid, colour)

    def draw(self, sid: int, tex: int, batch: QuadBatch, blend: int = OVER) -> None:
        self.handle.draw(sid, tex, batch, blend)

    def set_geom(self, sid: int, crop: Crop, scale: int, uv_stride: int) -> None:
        self.handle.set_geom(sid, crop, scale, uv_stride)

    def draw_cells(self, sid: int, tex: int, uv, batch: CellBatch, blend: int = OVER,
                   pad: float = 0.0, heads_only: bool = False) -> None:
        self.handle.draw_cells(sid, tex, uv, batch, blend, pad, heads_only)

    def swap(self, sid: int) -> None:
        self.handle.swap(sid)

    # ----------------------------------------------------------------- pump --

    def pump(self) -> list[Event]:
        """Read the socket, then drain the queue - in that order.

        mwl_next_event only sees what libwayland has already dispatched, so without
        the dispatch the compositor's buffer releases and frame callbacks pile up in
        the kernel and eglSwapBuffers stalls. timeout 0 keeps the frame timer honest:
        pacing is the timer's job, and poll() would return immediately anyway when
        the fd happens to be readable.
        """
        self.handle.dispatch(0)
        events = self.handle.events(MAX_EVENTS_PER_PUMP)
        self.pump_events(events)
        return events

    def pump_events(self, events: Sequence[Event]) -> None:
        for ev in events:
            self._route(ev)

    def _route(self, ev: Event) -> None:
        kind = ev.kind
        if kind == "key":
            self._on_key(ev)
        elif kind == "kb_enter":
            self._focused_sid = ev.sid
        elif kind == "kb_leave":
            if self._focused_sid == ev.sid:
                self._focused_sid = -1
        elif kind == "button":
            surf = self._by_sid(ev.sid)
            if surf:
                surf.on_click()
        elif kind == "configure":
            surf = self._by_sid(ev.sid)
            if surf:
                surf.on_configure(ev)
        elif kind == "closed":
            surf = self._by_sid(ev.sid)
            if surf:
                surf.report_closed()
        elif kind in ("gl_error", "display_error"):
            self.log(f"native_{kind} {list(ev.args)} {ev.text}".rstrip())

    def _by_sid(self, sid: int) -> Any:
        for surf in self.surfaces:
            if surf.sid == sid:
                return surf
        return None

    def _on_key(self, ev: Event) -> None:
        if not ev.args or not self.surfaces:
            return
        keysym = self.handle.keycode_to_keysym(ev.args[0])
        target = self._by_sid(self._focused_sid) or self.surfaces[0]
        target.on_key(keysym)


# ------------------------------------------------------------- the surface --


@dataclasses.dataclass
class GlRenderer:
    """Shim holding the attributes app._rebind() assigns on a surface's renderer.

    The Cairo Renderer owns damage state; this one only carries geometry and the
    atlas identity, and GlSurface notices an atlas swap at present() time.
    """

    crop: Crop
    canvas: WorldCanvas
    atlas: Any
    scale: int = 1
    glow_enabled: bool = False
    glow: bool = False
    screen: set = dataclasses.field(default_factory=set)


class GlSurface:
    """One full-output OVERLAY surface drawn with GLES2.

    State is authoritative rather than incremental: `cells` is the exact set of
    glyphs on screen, updated by queue() and replaced wholesale by a resync.
    """

    def __init__(self, *, runtime: GlRuntime, sid: int, info: MonitorInfo, crop: Crop,
                 canvas: WorldCanvas, atlas: Any, dim_alpha: float, keyboard_mode: str,
                 glow_enabled: bool, resync: Callable[[], OutputFrame],
                 on_key: Callable[[int], bool], on_click: Callable[[], None],
                 on_closed: Callable[[str], None]) -> None:
        self.runtime = runtime
        self.sid = sid
        self.info = info
        self.crop = crop
        self.canvas = canvas
        self.atlas = atlas
        self.keyboard_mode = keyboard_mode
        self.glow_enabled = glow_enabled
        self.dim_alpha = float(dim_alpha)
        self._resync = resync
        self.on_key = on_key
        self.on_click = on_click
        self._on_closed = on_closed
        self.cells: dict[tuple[int, int], Draw] = {}
        self.textures = runtime.textures_for(atlas, glow_enabled)

        handle = runtime.handle
        self.width, self.height = handle.surface_size(sid)
        self.scale = max(1, handle.surface_scale(sid))
        self._clear = clear_colour(self.dim_alpha)
        self._batch = QuadBatch(1024)
        self._glow_batch = QuadBatch(256)
        self._ibatch = CellBatch(1024)      # indices; the native side expands them
        self._need_sync = False
        self._reported_closed = False
        self.destroyed = False
        self.last_draw_s = 0.0
        self.renderer = GlRenderer(crop=crop, canvas=canvas, atlas=atlas,
                                   scale=self.scale, glow_enabled=glow_enabled,
                                   glow=bool(self.textures.glow_tex))

    # ------------------------------------------------------------------ ops --

    def show(self) -> None:
        """Paint immediately: the veil should appear before the first rain tick."""
        self._need_sync = True
        self.present()

    def queue(self, frame: OutputFrame) -> None:
        """Fold one world delta into the authoritative cell set.

        No damage bookkeeping: the whole field is re-submitted each frame, so
        there is nothing to coalesce and no way to lose a cell (the failure mode
        the Cairo FrameBuffer existed to prevent).
        """
        for c in frame.clear:
            self.cells.pop((c.col, c.row), None)
        for d in frame.draw:
            self.cells[(d.col, d.row)] = d

    def present(self) -> None:
        if self.destroyed:
            return
        t0 = _monotonic()
        if self._need_sync or self.renderer.atlas is not self.atlas:
            if self.renderer.atlas is not self.atlas:
                self.adopt_atlas(self.renderer.atlas)
            self._need_sync = False
            self.cells = {(d.col, d.row): d for d in self._resync().draw}
        rt = self.runtime
        rt.make_current(self.sid)
        rt.clear(self.sid, self._clear)          # the dim, formerly the CSS veil
        # Geometry is re-armed every frame: it is one cheap call and removes a
        # whole class of stale-state bugs after a crop, scale or configure change.
        rt.set_geom(self.sid, self.crop, self.scale, self.textures.layout.buckets)
        batch = self._ibatch
        batch.reset()
        emit_indexed(batch, self.cells, self.textures.layout)
        if self.textures.glow_tex and self.textures.glow_uv is not None:
            rt.draw_cells(self.sid, self.textures.glow_tex, self.textures.glow_uv,
                          batch, ADDITIVE, pad=self.textures.pad_px, heads_only=True)
        rt.draw_cells(self.sid, self.textures.cell_tex, self.textures.uv, batch, OVER)
        rt.swap(self.sid)
        self.last_draw_s = _monotonic() - t0

    def adopt_atlas(self, atlas: Any) -> None:
        """Re-point at a rebuilt atlas (L2 look change or a scale change)."""
        self.atlas = atlas
        self.renderer.atlas = atlas
        self.textures = self.runtime.textures_for(atlas, self.glow_enabled)
        self.renderer.glow = bool(self.textures.glow_tex)

    def request_resync(self) -> None:
        self._need_sync = True

    def set_dim(self, dim_alpha: float) -> None:
        self._clear = clear_colour(dim_alpha)

    def set_allocation_size(self, width: int, height: int) -> None:
        self.width, self.height = int(width), int(height)

    def on_configure(self, ev: Event) -> None:
        """The compositor resized us (mode or scale change, panel toggled).

        The native side already resized the EGL window and buffer scale; we pick
        up the new scale and repaint from authoritative state.
        """
        if len(ev.args) >= 3:
            self.width, self.height = ev.args[1], ev.args[2]
        scale = self.runtime.handle.surface_scale(self.sid)
        if scale > 0:
            self.scale = scale
            self.renderer.scale = scale
        self._need_sync = True

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self.runtime.forget(self)
        if not self._reported_closed:
            self._reported_closed = True
            self.runtime.handle.destroy_surface(self.sid)
            self._on_closed(self.info.name)
    def report_closed(self) -> None:
        """The compositor dropped us (output unplugged or layer surface gone).

        Reported once, and never destroys the native surface: it is already gone.
        """
        self.destroyed = True
        if self._reported_closed:
            return
        self._reported_closed = True
        self.runtime.forget(self)
        self._on_closed(self.info.name)

    @property
    def screen_cells(self) -> int:
        return len(self.cells)
