"""The Matrix rain simulation. Pure: no Gtk, no Gdk, no cairo (plan 1.5).

`World.step()` returns a new state plus a `FrameDelta` of dirty cells; all
colour/alpha is *derived* from state, never stored (plan C4). Column behaviour
is a function of (seed, global column) only - never of which monitor owns the
column - which is what makes the shared world seam-free (D3).
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import random
from collections.abc import Sequence

BUCKETS = 8  # brightness quantisation; must match GlyphAtlas buckets
_U64 = (1 << 64) - 1


def _col_seed(seed: int, col: int) -> int:
    """splitmix-ish mixing so each column has its own stable RNG stream."""
    x = ((seed + 0x9E3779B97F4A7C15) * 0xFF51AFD7ED558CCD + col * 0xD6E8FEB86659FD93) & _U64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & _U64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & _U64
    return x ^ (x >> 31)


@dataclasses.dataclass(frozen=True)
class Geom:
    """A monitor in the compositor's global (logical) coordinate space."""

    name: str
    x: int
    y: int
    width: int
    height: int


@dataclasses.dataclass(frozen=True)
class Crop:
    """The slice of the shared grid drawn by one output."""

    name: str
    col0: int
    col_span: int
    row0: int
    row_span: int
    x_px: int
    y_px: int

    def holds_col(self, global_col: int) -> bool:
        return self.col0 <= global_col < self.col0 + self.col_span

    def holds_row(self, global_row: int) -> bool:
        return self.row0 <= global_row < self.row0 + self.row_span

    def to_global(self, col: int, row: int) -> tuple[int, int]:
        return (self.col0 + col, self.row0 + row)

    def local_x(self, global_col: int) -> float:
        """Pixel x inside this output; may be slightly negative when a cell is
        physically clipped by the bezel - correct, and the surface clips it."""
        return global_col * self.cell_size - self.x_px

    cell_size: int = 18  # carried for renderer convenience; set by the canvas


@dataclasses.dataclass(frozen=True)
class WorldCanvas:
    x0: int
    y0: int
    width: int
    height: int
    cell_size: int
    cols: int
    rows: int
    crops: tuple[Crop, ...]

    @classmethod
    def from_geoms(cls, geoms: Sequence[Geom], cell_size: int) -> WorldCanvas:
        if not geoms:
            raise ValueError("no enabled outputs")
        x0 = min(g.x for g in geoms)
        y0 = min(g.y for g in geoms)
        width = max(g.x + g.width for g in geoms) - x0
        height = max(g.y + g.height for g in geoms) - y0
        cols, rows = width // cell_size, height // cell_size
        crops = []
        for g in geoms:
            crops.append(cls._crop_for(g, x0, y0, cell_size, cols, rows))
        return cls(x0, y0, width, height, cell_size, cols, rows, tuple(crops))

    @staticmethod
    def _crop_for(g: Geom, x0: int, y0: int, cell: int, cols: int, rows: int) -> Crop:
        def span(origin: int, size: int, limit: int) -> tuple[int, int]:
            a = max(0, min(limit, math.ceil(origin / cell - 0.5)))
            b = max(0, min(limit, math.ceil((origin + size) / cell - 0.5)))
            return (a, max(0, b - a))

        c0, cs = span(g.x - x0, g.width, cols)
        r0, rs = span(g.y - y0, g.height, rows)
        return Crop(name=g.name, col0=c0, col_span=cs, row0=r0, row_span=rs,
                    x_px=g.x - x0, y_px=g.y - y0, cell_size=cell)

    def local_of(self, global_col: int, global_row: int) -> tuple[int, int, int] | None:
        for idx, crop in enumerate(self.crops):
            if crop.holds_col(global_col) and crop.holds_row(global_row):
                return (idx, global_col - crop.col0, global_row - crop.row0)
        return None


@dataclasses.dataclass(frozen=True)
class Params:
    fps: int = 30
    cell_size: int = 18
    fall_speed: float = 480.0
    fall_speed_jitter: float = 0.35
    stream_density: float = 0.55
    tail_length: int = 14
    fade_rate: float = 1.6
    mutate_rate: float = 12.0
    seed: int = 0
    n_glyphs: int = 96
    span_mode: str = "shared"

    @property
    def spawn_prob_per_step(self) -> float:
        return max(0.0, min(1.0, self.stream_density / max(1, self.tail_length)))


# ------------------------------------------------------------ cell output ---


@dataclasses.dataclass(frozen=True)
class Draw:
    col: int
    row: int
    global_col: int
    glyph: int
    brightness: float
    head: bool


@dataclasses.dataclass(frozen=True)
class Pos:
    col: int
    row: int


@dataclasses.dataclass(frozen=True)
class OutputFrame:
    name: str
    draw: tuple[Draw, ...] = ()
    clear: tuple[Pos, ...] = ()


@dataclasses.dataclass(frozen=True)
class FrameDelta:
    tick: int
    per_output: tuple[OutputFrame, ...]


@dataclasses.dataclass(frozen=True)
class ColumnView:
    active: bool
    head_row: float
    speed_rows_per_step: float
    length: int


@dataclasses.dataclass
class _Column:
    rng: random.Random
    active: bool
    head_row: float
    factor: float
    cells: dict[int, int]
    emitted: dict[int, tuple[int, int]]


def level_of(head_row: float, row: int, length: int, fade_rate: float) -> int:
    """Derived, never stored: brightness bucket for a cell (C4)."""
    d = head_row - row
    t = 0.0 if length <= 1 else max(0.0, min(1.0, d / (length - 1)))
    curve = 1.0 - (t ** (1.0 / max(0.05, fade_rate)))
    return max(0, min(BUCKETS - 1, int(round(curve * (BUCKETS - 1)))))


class World:
    def __init__(self, canvas: WorldCanvas, params: Params) -> None:
        if params.span_mode != "shared":
            raise NotImplementedError(
                f"span_mode={params.span_mode!r} is deferred (plan 2.3); only 'shared' "
                "is implemented - independent per-monitor worlds would reintroduce a seam"
            )
        self.canvas = canvas
        self.params = params
        # seed 0 == entropy per launch (plan 2.3); any other value is reproducible.
        self._seed = params.seed if params.seed else random.SystemRandom().getrandbits(63)
        self.tick = 0
        self._cols = [self._spawn_column(i) for i in range(canvas.cols)]
        self._owner: list[int | None] = [None] * canvas.cols
        for idx, crop in enumerate(canvas.crops):
            for c in range(crop.col0, crop.col0 + crop.col_span):
                self._owner[c] = idx

    def _spawn_column(self, idx: int) -> _Column:
        p, rng = self.params, random.Random(_col_seed(self._seed, idx))
        factor = 1.0 + p.fall_speed_jitter * rng.uniform(-1.0, 1.0)
        active = p.stream_density > 0 and rng.random() < p.stream_density
        head = -rng.uniform(1.0, max(2.0, float(p.tail_length))) if active else -1.0
        return _Column(rng=rng, active=active, head_row=head, factor=factor,
                       cells={}, emitted={})

    # ------------------------------------------------------------- readback --

    def column(self, global_col: int) -> ColumnView:
        col = self._cols[global_col]
        return ColumnView(col.active, col.head_row, self._speed(col), self.params.tail_length)

    def apply_params(self, params: Params) -> None:
        """L1 hot path: speeds/lengths are derived from params at use time, so
        state and tick survive a live config edit (plan C3)."""
        self.params = params

    def render_state(self) -> tuple[OutputFrame, ...]:
        """Authoritative full repaint: every cell currently on screen, as draws
        with no clears. Needed whenever the renderer's buffer is not in sync -
        expose after hotplug, a fresh buffer, or a re-created surface (T6)."""
        canvas = self.canvas
        buckets = BUCKETS - 1
        draws: list[list[Draw]] = [[] for _ in canvas.crops]
        for idx, col in enumerate(self._cols):
            owner = self._owner[idx]
            if owner is None:
                continue
            head_int = math.floor(col.head_row)
            for row, (glyph, level) in col.emitted.items():
                loc = canvas.local_of(idx, row)
                if loc is None:
                    continue
                _, lc, lr = loc
                draws[owner].append(Draw(lc, lr, idx, glyph, level / buckets,
                                         row == head_int))
        return tuple(OutputFrame(name=c.name, draw=tuple(draws[i]), clear=())
                     for i, c in enumerate(canvas.crops))

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(f"t{self.tick}".encode())
        for i, col in enumerate(self._cols):
            h.update(f"|{i}{int(col.active)}{col.head_row:.6f}{col.factor:.6f}"
                     f"{sorted(col.cells.items())}{sorted(col.emitted.items())}".encode())
        return h.hexdigest()

    # --------------------------------------------------------------- update --

    def _speed(self, col: _Column) -> float:
        p = self.params
        return p.fall_speed * col.factor / (p.cell_size * max(1, p.fps))

    def _pick_glyph(self, col: _Column, old: int | None) -> int:
        n = max(2, self.params.n_glyphs)
        if old is None or self.params.n_glyphs < 2:
            return col.rng.randrange(n)
        return (old + 1 + col.rng.randrange(n - 1)) % n

    def step(self) -> FrameDelta:
        self.tick += 1
        p = self.params
        rows, canvas = self.canvas.rows, self.canvas
        buckets = BUCKETS - 1
        draws_by_crop: list[list[Draw]] = [[] for _ in canvas.crops]
        clears_by_crop: list[list[Pos]] = [[] for _ in canvas.crops]
        for idx, col in enumerate(self._cols):
            owner = self._owner[idx]
            if not col.active:
                self._retire_emitted(idx, owner, col, canvas, clears_by_crop)
                col.cells.clear()
                if p.stream_density > 0 and col.rng.random() < p.spawn_prob_per_step:
                    col.active = True
                    col.head_row = -col.rng.uniform(1.0, max(2.0, float(p.tail_length)))
                continue
            col.head_row += self._speed(col)
            head_int = math.floor(col.head_row)
            visible: dict[int, tuple[int, int]] = {}
            for k in range(p.tail_length):
                row = head_int - k
                if not 0 <= row < rows:
                    continue
                if row not in col.cells:
                    col.cells[row] = self._pick_glyph(col, None)
                level = buckets if k == 0 else level_of(col.head_row, row,
                                                        p.tail_length, p.fade_rate)
                if level > 0:
                    visible[row] = (col.cells[row], level)
            if p.mutate_rate > 0:
                prob = min(1.0, p.mutate_rate / max(1, p.fps))
                for row in visible:
                    if col.rng.random() < prob:
                        glyph = self._pick_glyph(col, col.cells[row])
                        col.cells[row] = glyph
                        visible[row] = (glyph, visible[row][1])
            for row in [r for r in col.cells if r > head_int or r < head_int - p.tail_length]:
                del col.cells[row]
            if head_int - p.tail_length + 1 > rows - 1:
                col.active = False
            if owner is not None:
                for row, (glyph, level) in visible.items():
                    if col.emitted.get(row) == (glyph, level):
                        continue  # unchanged cell: no damage, no upload (plan 1.4)
                    loc = canvas.local_of(idx, row)
                    if loc is None:
                        continue
                    _, lc, lr = loc
                    draws_by_crop[owner].append(
                        Draw(col=lc, row=lr, global_col=idx, glyph=glyph,
                             brightness=level / buckets, head=(row == head_int)))
                gone = [r for r in col.emitted if r not in visible]
                for row in gone:
                    loc = canvas.local_of(idx, row)
                    if loc is None:
                        continue
                    _, lc, lr = loc
                    clears_by_crop[owner].append(Pos(col=lc, row=lr))
            col.emitted = visible
        return FrameDelta(
            self.tick,
            tuple(OutputFrame(name=canvas.crops[i].name,
                              draw=tuple(draws_by_crop[i]),
                              clear=tuple(clears_by_crop[i]))
                  for i in range(len(canvas.crops))),
        )

    @staticmethod
    def _retire_emitted(idx: int, owner: int | None, col: _Column,
                        canvas: WorldCanvas, clears_by_crop: list[list[Pos]]) -> None:
        """A column that went inactive must blank every cell it last drew."""
        if owner is None or not col.emitted:
            col.emitted.clear()
            return
        for row in col.emitted:
            loc = canvas.local_of(idx, row)
            if loc is not None:
                _, lc, lr = loc
                clears_by_crop[owner].append(Pos(col=lc, row=lr))
        col.emitted.clear()
