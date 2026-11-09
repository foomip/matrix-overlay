"""Dirty-cell Cairo compositor (plan 1.4 / C4).

Geometry is in *logical* pixels - the coordinate space GTK3 hands the `draw`
handler - and high-resolution atlas surfaces are mapped into it with a pattern
matrix. That keeps HiDPI glyphs crisp (DP-4 measured scale=2) without the
renderer ever needing to know about device pixels.

Three invariants this module exists to protect:
  1. every draw clears its own cell first (Cairo OVER accumulates alpha, so a
     churning glyph would otherwise grow brighter every frame it is redrawn);
  2. the veil is CSS-only - no full-surface op is ever emitted;
  3. nothing is lost to a clipped `draw` callback: clipped work is deferred and
     drained on a later call, with a backlog cap that asks for a full re-sync.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable

import cairo

from .atlas import level_from_brightness
from .world import Crop, Draw, OutputFrame, Pos, WorldCanvas

Cell = tuple[int, int]  # internal (col, row) key; world.Pos is the wire type
Rect = tuple[float, float, float, float]
_MAX_PENDING = 4096


def _intersects(a: Rect, b: Rect) -> bool:
    if b is None:
        return True
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _ordered(*groups: Iterable[Cell]) -> list[Cell]:
    seen: dict[Cell, None] = {}
    for group in groups:
        for pos in group:
            seen.setdefault(pos, None)
    return list(seen)


def merge_rows(rects: Iterable[Rect]) -> list[Rect]:
    """Coalesce per-cell rects into one rect per damaged row band.

    Measured reason: ~1600 queue_draw_area calls/frame collapsed the live rate to
    ~10 fps although the same frames cost ~16 ms headless - the cost is GTK region
    bookkeeping, not the blits. Rain touches most columns of a row, so a per-row
    union is near-optimal at ~20x fewer rects.
    """
    bands: dict[tuple[float, float], list[float]] = {}
    for x, y, w, h in rects:
        span = bands.setdefault((y, h), [x, x + w])
        span[0] = min(span[0], x)
        span[1] = max(span[1], x + w)
    return [(x, y, right - x, h) for (y, h), (x, right) in bands.items()]


@dataclasses.dataclass
class FrameBuffer:
    """Compose successive deltas for one output: the last write to a cell wins.

    A draw can cost more than the tick interval, so buffering frames as a list made
    every draw heavier until the GLib loop never yielded (livelock: ~10 fps and
    signals ignored). Composing deltas keeps one frame's worth of work no matter how
    far rendering falls behind the simulation.
    """

    name: str
    draws: dict[Cell, Draw] = dataclasses.field(default_factory=dict)
    clears: set[Cell] = dataclasses.field(default_factory=set)

    def add(self, frame: OutputFrame) -> None:
        for c in frame.clear:
            pos = (c.col, c.row)
            self.draws.pop(pos, None)
            self.clears.add(pos)
        for d in frame.draw:
            self.clears.discard((d.col, d.row))
            self.draws[(d.col, d.row)] = d

    def take(self) -> OutputFrame | None:
        if not self.draws and not self.clears:
            return None
        frame = OutputFrame(self.name, tuple(self.draws.values()),
                            tuple(Pos(col, row) for col, row in sorted(self.clears)))
        self.draws.clear()
        self.clears.clear()
        return frame

    def clear(self) -> None:
        self.draws.clear()
        self.clears.clear()

    def __bool__(self) -> bool:
        return bool(self.draws or self.clears)

    def rects(self, renderer: Renderer) -> list[Rect]:
        """Damage for the composed frame, requested before the draw."""
        pending = OutputFrame(self.name, tuple(self.draws.values()),
                              tuple(Pos(col, row) for col, row in sorted(self.clears)))
        return renderer.plan(pending)



@dataclasses.dataclass
class Renderer:
    crop: Crop
    canvas: WorldCanvas
    atlas: object
    scale: int = 1
    glow_enabled: bool = False
    max_pending: int = _MAX_PENDING

    def __post_init__(self) -> None:
        self.screen: set[Cell] = set()
        self.on_resync: Callable[[], None] | None = None
        self._pending_draw: dict[Cell, Draw] = {}
        self._pending_clear: dict[Cell, None] = {}
        self.glow = bool(self.glow_enabled and getattr(self.atlas, "glow_pad", 0))
        self.cell = self.canvas.cell_size  # logical px per cell

    # ------------------------------------------------------------ geometry --

    def rect_of(self, col: int, row: int) -> Rect:
        """Device-independent rect; x may be negative when a cell is physically
        clipped by the bezel at the seam - the surface clips it, correctly."""
        gx = (self.crop.col0 + col) * self.cell - self.crop.x_px
        gy = (self.crop.row0 + row) * self.cell - self.crop.y_px
        return (gx, gy, self.cell, self.cell)

    @property
    def blit_scale(self) -> float:
        """Logical units per atlas pixel: 0.5 on the measured scale=2 output, so a
        crisp 36px glyph lands in an 18px logical cell."""
        return self.cell / self.atlas.cell_px

    def _clip(self, cr) -> Rect | None:
        try:
            x0, y0, x1, y1 = cr.clip_extents()
        except Exception:  # pragma: no cover - spy/older cairo
            return None
        if x0 <= -1e30 or x1 >= 1e30 or y0 <= -1e30 or y1 >= 1e30:
            return None  # unbounded clip: treat as "everything"
        return (x0, y0, x1 - x0, y1 - y0)

    # ---------------------------------------------------------- paint ---

    def _clear_rect(self, cr, rect: Rect) -> None:
        cr.save()
        cr.set_operator(cairo.Operator.DEST_OUT)
        cr.set_source_rgb(0.0, 0.0, 0.0)
        cr.rectangle(*rect)
        cr.fill()
        cr.restore()

    def _blit(self, cr, surface, x: float, y: float, operator=None, alpha: float = 1.0) -> None:
        """Scale via the CTM, not the pattern matrix: cairo's pattern matrix maps
        user space *into* pattern space, so a 0.5 there magnifies (measured)."""
        k = self.blit_scale
        cr.save()
        if operator is not None:
            cr.set_operator(operator)
        cr.translate(x, y)
        cr.scale(k, k)
        pat = cairo.SurfacePattern(surface)
        pat.set_filter(cairo.FILTER_GOOD)
        cr.set_source(pat)
        cr.rectangle(0.0, 0.0, surface.get_width(), surface.get_height())
        if alpha >= 1.0:
            cr.fill()
        else:  # pragma: no cover - reserved for faded halos
            cr.fill_preserve()
        cr.restore()

    def _paint_cell(self, cr, d: Draw, rect: Rect) -> None:
        level = level_from_brightness(d.brightness)
        if level <= 0:
            return
        if self.glow and d.head:
            pad = self.atlas.glow_pad
            k = self.blit_scale
            self._blit(cr, self.atlas.glow_surface_for(d.glyph, level),
                       rect[0] - pad * k, rect[1] - pad * k, cairo.Operator.ADD)
        self._blit(cr, self.atlas.surface_for(d.glyph, level), rect[0], rect[1],
                   cairo.Operator.OVER)

    # -------------------------------------------------------------- public --

    def plan(self, frame: OutputFrame) -> list[Rect]:
        """Rects this frame will touch, reported *before* the draw.

        Damage must be requested from outside the `draw` handler: GTK only invokes
        `draw` for regions already marked dirty, so reporting damage from inside
        `apply` would deadlock the pipeline (measured: tick advanced, cells_on_screen
        stayed 0, and the rain was invisible behind the veil).
        """
        rects: list[Rect] = []
        seen: set[Cell] = set()
        drawn = {(d.col, d.row) for d in frame.draw}
        for c in frame.clear:
            pos = (c.col, c.row)
            if pos in drawn or pos in seen:
                continue
            seen.add(pos)
            rects.append(self.rect_of(*pos))
        pad = self.atlas.glow_pad * self.blit_scale if self.glow else 0
        for d in frame.draw:
            rect = self.rect_of(d.col, d.row)
            if d.head and pad:
                rect = (rect[0] - pad, rect[1] - pad, rect[2] + 2 * pad, rect[3] + 2 * pad)
            rects.append(rect)
        return rects

    def apply(self, cr, frame: OutputFrame) -> None:
        """Consume one FrameDelta for this output. Clip-safe."""
        clip = self._clip(cr)
        draws: dict[Cell, Draw] = dict(self._pending_draw)
        for d in frame.draw:
            draws[(d.col, d.row)] = d
        clear_only = [p for p in _ordered(self._pending_clear,
                                          ((c.col, c.row) for c in frame.clear))
                      if p not in draws]

        for pos in clear_only:
            rect = self.rect_of(*pos)
            if not _intersects(rect, clip):
                self._defer_clear(pos)
                continue
            self._clear_rect(cr, rect)
            self._pending_clear.pop(pos, None)
            self.screen.discard(pos)

        for pos in _ordered(self._pending_draw, ((d.col, d.row) for d in frame.draw)):
            d = draws.get(pos)
            if d is None:
                continue
            rect = self.rect_of(*pos)
            if not _intersects(rect, clip):
                self._defer_draw(pos, d)
                continue
            self._clear_rect(cr, rect)  # invariant 1: replace, never accumulate
            self._paint_cell(cr, d, rect)
            self._pending_draw.pop(pos, None)
            self.screen.add(pos)

    def sync(self, cr, frame: OutputFrame) -> None:
        """Full repaint from World.render_state(): authoritative state, no ghosts."""
        clip = self._clip(cr)
        want = {(d.col, d.row): d for d in frame.draw}
        for pos in _ordered(self.screen - set(want)):
            rect = self.rect_of(*pos)
            if _intersects(rect, clip):
                self._clear_rect(cr, rect)
        self._pending_clear.clear()
        self._pending_draw.clear()
        self.screen = set()
        for pos, d in want.items():
            rect = self.rect_of(*pos)
            if not _intersects(rect, clip):
                self._defer_draw(pos, d)
                continue
            self._clear_rect(cr, rect)
            self._paint_cell(cr, d, rect)
            self.screen.add(pos)

    def reset(self) -> None:
        self.screen.clear()
        self._pending_clear.clear()
        self._pending_draw.clear()

    # ------------------------------------------------------ pending queue --

    @property
    def pending_draws(self) -> set[Cell]:
        return set(self._pending_draw)

    @property
    def pending_clears(self) -> set[Cell]:
        return set(self._pending_clear)

    def _defer_draw(self, pos: Cell, d: Draw) -> None:
        self._pending_draw[pos] = d
        self._check_backlog()

    def _defer_clear(self, pos: Cell) -> None:
        self._pending_clear.setdefault(pos, None)
        self._check_backlog()

    def _check_backlog(self) -> None:
        if len(self._pending_draw) + len(self._pending_clear) <= self.max_pending:
            return
        self.reset()
        if self.on_resync is not None:
            self.on_resync()

