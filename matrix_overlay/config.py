"""Config loading, validation and hot-reload classification.

Contract: plan C3. The config file is read-only to the app - this module never
writes it back, not even when [perf] guard degrades render_scale (D1.7).
On a rejected load the previous good Config is kept and the show goes on.

SPEC is the single source of truth for defaults, ranges and reload classes
(no bundled defaults.toml: --print-default-config serializes SPEC).
"""
from __future__ import annotations

import dataclasses
import enum
import os
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

# ------------------------------------------------------- reload classes ---
LIVE = "L1"       # swap params, keep everything (veil stays up)
ATLAS = "L2"      # rebuild glyph atlas, keep veil + world
TOPOLOGY = "L3"   # create/destroy affected layer surfaces only
RESET = "L4"      # reseed / rebuild world state

_ORDER = (LIVE, ATLAS, TOPOLOGY, RESET)


class Kind(enum.Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STR = "str"
    COLOR = "color"
    CHOICE = "choice"
    STRSEQ = "strseq"
    NODE = "node"


@dataclasses.dataclass(frozen=True)
class Spec:
    key: str
    kind: Kind
    default: Any
    cls: str = LIVE
    bounds: tuple[float, float] | None = None
    choices: tuple[str, ...] | None = None


SPEC: tuple[Spec, ...] = (
    # [world]
    # Measured on COSMIC/GTK3 Wayland @ 2560x1440x2 (D5.2): the presentation path caps at
    # ~19-22 fps regardless of renderer (GLArea 2 fps; trivial Cairo 19 fps), so the default
    # target is the sustainable rate. The guard settles to 20 without churn; users can still
    # raise toward 30 (the guard finds reality) or lower to 10.
    Spec("world.fps", Kind.INT, 22, LIVE, (10, 60)),
    Spec("world.fall_speed", Kind.FLOAT, 480.0, LIVE, (60.0, 2000.0)),
    Spec("world.fall_speed_jitter", Kind.FLOAT, 0.35, LIVE, (0.0, 1.0)),
    Spec("world.stream_density", Kind.FLOAT, 0.55, LIVE, (0.0, 1.0)),
    Spec("world.tail_length", Kind.INT, 14, LIVE, (4, 40)),
    Spec("world.fade_rate", Kind.FLOAT, 1.6, LIVE, (0.5, 4.0)),
    Spec("world.mutate_rate", Kind.FLOAT, 12.0, LIVE, (0.0, 40.0)),
    Spec("world.cell_size", Kind.INT, 18, ATLAS, (10, 32)),
    Spec("world.render_scale", Kind.FLOAT, 1.0, ATLAS, (0.5, 1.0)),
    Spec("world.seed", Kind.INT, 0, RESET),
    Spec("world.start_delay_ms", Kind.INT, 120, LIVE, (0, 3000)),
    Spec("world.idle_auto_stop_seconds", Kind.INT, 0, LIVE, (0, 86400)),
    # [look]
    Spec("look.font", Kind.STR, "Noto Sans Mono CJK JP", ATLAS),
    Spec("look.glyph_set", Kind.CHOICE, "matrix", ATLAS, None,
         ("matrix", "ascii", "katakana", "digits", "custom")),
    Spec("look.custom_glyphs", Kind.STR, "", ATLAS),
    Spec("look.head_color", Kind.COLOR, "#d6ffd6", LIVE),
    Spec("look.body_color", Kind.COLOR, "#00b81f", LIVE),
    Spec("look.dim_alpha", Kind.FLOAT, 0.72, LIVE, (0.0, 0.95)),
    Spec("look.glow.enabled", Kind.BOOL, True, ATLAS),
    Spec("look.glow.radius", Kind.INT, 6, ATLAS, (0, 24)),
    Spec("look.glow.intensity", Kind.FLOAT, 0.45, LIVE, (0.0, 1.0)),
    # [outputs]
    Spec("outputs.mode", Kind.CHOICE, "all", TOPOLOGY, None, ("all", "listed")),
    # D5: GTK3 exposes no connector name, so entries match index or model substring
    Spec("outputs.listed", Kind.STRSEQ, (), TOPOLOGY),
    Spec("outputs.span_mode", Kind.CHOICE, "shared", RESET, None,
         ("shared", "independent")),
    # [input]
    Spec("input.keyboard_mode", Kind.CHOICE, "exclusive", LIVE, None,
         ("exclusive", "on_demand")),
    Spec("input.stop_key", Kind.STR, "Escape", LIVE),
    Spec("input.pointer_click_action", Kind.CHOICE, "stop", LIVE, None,
         ("stop", "ignore")),
    Spec("input.pointer_passthrough", Kind.BOOL, False, LIVE),
    # [perf]
    Spec("perf.guard", Kind.BOOL, True, LIVE),
    Spec("perf.frame_budget_ratio", Kind.FLOAT, 0.8, LIVE, (0.05, 0.95)),
    Spec("perf.sustain_seconds", Kind.FLOAT, 3.0, LIVE, (0.5, 60.0)),
    Spec("perf.min_render_scale", Kind.FLOAT, 0.5, LIVE, (0.5, 1.0)),
    Spec("perf.min_fps", Kind.INT, 20, LIVE, (10, 60)),
    # [render]
    # Two presentation backends coexist. "cairo" paints through GTK/GDK; "gl"
    # bypasses GDK entirely via native/libmwl.so (raw layer-shell + EGL/GLES2).
    # Measured on this stack at 22 fps across two 2560x1440 outputs: cairo costs
    # 99.8% of a core (GDK copies the whole damaged shm buffer every frame), gl costs
    # 26.4% - and unlike cairo it actually reaches a 30 fps target. gl is the default;
    # cairo stays available, and cli falls back to it automatically if libmwl.so
    # cannot be built or the compositor has no layer shell for us.
    Spec("render.backend", Kind.CHOICE, "gl", ATLAS, None, ("cairo", "gl")),
    Spec("render.render_node", Kind.NODE, "", LIVE),  # "" = auto-detect (M2)
    # "" = auto: derive the EGL vendor from render_node's PCI vendor
    Spec("render.egl_vendor", Kind.STR, "", LIVE),
)

SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPEC}
SUPPORTED_SCHEMA_VERSION = 1


@dataclasses.dataclass(frozen=True)
class WorldParams:
    fps: int
    fall_speed: float
    fall_speed_jitter: float
    stream_density: float
    tail_length: int
    fade_rate: float
    mutate_rate: float
    cell_size: int
    render_scale: float
    seed: int
    start_delay_ms: int
    idle_auto_stop_seconds: int


@dataclasses.dataclass(frozen=True)
class GlowParams:
    enabled: bool
    radius: int
    intensity: float


@dataclasses.dataclass(frozen=True)
class LookParams:
    font: str
    glyph_set: str
    custom_glyphs: str
    head_color: str
    body_color: str
    dim_alpha: float
    glow: GlowParams


@dataclasses.dataclass(frozen=True)
class OutputsParams:
    mode: str
    listed: tuple[str, ...]
    span_mode: str


@dataclasses.dataclass(frozen=True)
class InputParams:
    keyboard_mode: str
    stop_key: str
    pointer_click_action: str
    pointer_passthrough: bool


@dataclasses.dataclass(frozen=True)
class PerfParams:
    guard: bool
    frame_budget_ratio: float
    sustain_seconds: float
    min_render_scale: float
    min_fps: int


@dataclasses.dataclass(frozen=True)
class RenderParams:
    backend: str
    render_node: str
    egl_vendor: str


@dataclasses.dataclass(frozen=True)
class Config:
    schema_version: int
    world: WorldParams
    look: LookParams
    outputs: OutputsParams
    input: InputParams
    perf: PerfParams
    render: RenderParams

    def as_toml(self) -> str:
        return serialize_toml(self)


# ------------------------------------------------------- flatten / patch ---

_SECTION_OF = {"world": WorldParams, "outputs": OutputsParams, "input": InputParams,
               "perf": PerfParams, "render": RenderParams, "look": LookParams,
               "look.glow": GlowParams}


def _table_of(key: str) -> str:
    return key.rsplit(".", 1)[0] if "." in key else ""


def to_flat(cfg: Config) -> dict[str, Any]:
    flat: dict[str, Any] = {"schema_version": cfg.schema_version}
    for spec in SPEC:
        obj: Any = cfg
        for part in spec.key.split("."):
            obj = getattr(obj, part)
        flat[spec.key] = obj
    return flat


def from_flat(flat: Mapping[str, Any]) -> Config:
    def norm(v: Any) -> Any:
        return tuple(v) if isinstance(v, list) else v

    vals = {k: norm(v) for k, v in flat.items()}
    unknown = [k for k in vals if k != "schema_version" and k not in SPEC_BY_KEY]
    if unknown:
        raise KeyError(f"not config keys: {sorted(unknown)}")
    return Config(
        schema_version=vals["schema_version"],
        look=LookParams(
            glow=GlowParams(**{k[10:]: vals[k] for k in vals if _table_of(k) == "look.glow"}),
            **{k[5:]: vals[k] for k in vals if _table_of(k) == "look"},
        ),
        **{sec: cls(**{k[len(sec) + 1:]: vals[k] for k in vals if _table_of(k) == sec})
           for sec, cls in _SECTION_OF.items() if sec != "look.glow" and sec != "look"},
    )


def qualify_key(name: str) -> str:
    """Accept a dotted spec path, or a bare leaf name when it is unambiguous.
    Unknown names raise instead of being silently dropped (that footgun cost me
    a green-looking test that asserted on an unapplied override)."""
    if name in SPEC_BY_KEY or name == "schema_version":
        return name
    matches = [k for k in SPEC_BY_KEY if k.rsplit(".", 1)[-1] == name]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"unknown config key {name!r} (matches: {matches or 'none'})")


def replace_config(cfg: Config, **overrides: Any) -> Config:
    """Pure copy with dotted-path overrides, e.g. replace_config(c, **{"world.fps": 24})."""
    flat = to_flat(cfg)
    flat.update({qualify_key(k): v for k, v in overrides.items()})
    return from_flat(flat)


def change_classes(old: Config, new: Config) -> Iterable[str]:
    a, b = to_flat(old), to_flat(new)
    seen: list[str] = []
    for key, value in b.items():
        if key in a and a[key] == value:
            continue
        spec = SPEC_BY_KEY.get(key)
        cls = spec.cls if spec else RESET if key == "schema_version" else LIVE
        if cls not in seen:
            seen.append(cls)
    return sorted(seen, key=_ORDER.index)


def dominant_class(classes: Sequence[str] | Iterable[str]) -> str | None:
    order = [c for c in _ORDER if c in set(classes)]
    return order[-1] if order else None


# --------------------------------------------------------------- defaults ---


def default_flat() -> dict[str, Any]:
    flat = {"schema_version": SUPPORTED_SCHEMA_VERSION}
    flat.update({s.key: s.default for s in SPEC})
    return flat


def load_default() -> Config:
    return from_flat(default_flat())


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """Runtime facts measured from the compositor (see M0 probe)."""

    layer_shell_supported: bool
    protocol_version: int
    lib_version: tuple[int, int, int]
    monitor_scales: tuple[int, ...] = ()


# ------------------------------------------------------------- validation ---

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _warn(out: list[str], code: str, key: str, detail: str = "") -> None:
    out.append(f"{code}:{key}{':' + detail if detail else ''}")


def _coerce(spec: Spec, value: Any, out: list[str]) -> tuple[bool, Any]:
    """Return (ok, value). ok=False means the key was unusable -> use default."""
    k = spec.key
    if spec.kind is Kind.BOOL:
        if isinstance(value, bool):
            return True, value
        _warn(out, "bad_type", k, type(value).__name__)
        return False, None
    if spec.kind in (Kind.INT, Kind.FLOAT):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _warn(out, "bad_type", k, type(value).__name__)
            return False, None
        val = int(value) if spec.kind is Kind.INT else float(value)
        if spec.bounds and not spec.bounds[0] <= val <= spec.bounds[1]:
            clamped = max(spec.bounds[0], min(spec.bounds[1], val))
            val = int(clamped) if spec.kind is Kind.INT else float(clamped)
            _warn(out, "clamped", k, f"{value}->{val}")
        return True, val
    if spec.kind is Kind.COLOR:
        if isinstance(value, str) and _COLOR_RE.match(value):
            return True, value
        _warn(out, "bad_color", k, str(value))
        return False, None
    if spec.kind is Kind.CHOICE:
        if isinstance(value, str) and value in (spec.choices or ()):
            return True, value
        _warn(out, "bad_choice", k, str(value))
        return False, None
    if spec.kind is Kind.STRSEQ:
        if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            return True, tuple(value)
        _warn(out, "bad_type", k, type(value).__name__)
        return False, None
    if spec.kind is Kind.NODE:
        if not isinstance(value, str):
            _warn(out, "bad_type", k, type(value).__name__)
            return False, None
        if value and not Path(value).exists():
            # kept: the wrapper may run elsewhere, and cli's auto-detect ladder
            # recovers from a node that is not on this box
            _warn(out, "unknown_render_node", k, value)
        return True, value
    if isinstance(value, str):
        return True, value
    _warn(out, "bad_type", k, type(value).__name__)
    return False, None


def validate(raw: Mapping[str, Any], caps: Capabilities) -> tuple[dict[str, Any], list[str], bool]:
    """Turn a parsed TOML mapping into a flat param dict.

    Returns (flat, warnings, rejected). rejected -> caller keeps the last good
    Config; the show must never stop because of a typo (plan C3).
    """
    version = raw.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    out: list[str] = []

    flat = default_flat()
    rest = dict(raw)
    version = rest.pop("schema_version", SUPPORTED_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        _warn(out, "bad_type", "schema_version", type(version).__name__)
        version = SUPPORTED_SCHEMA_VERSION
    if version > SUPPORTED_SCHEMA_VERSION:
        out.insert(0, f"schema_version_unsupported:file says {version}, "
                      f"app knows {SUPPORTED_SCHEMA_VERSION}")
        return default_flat(), out, True

    _walk(rest, "", flat, out)
    _apply_cross_rules(flat, out, caps)
    flat["schema_version"] = version
    return flat, out, False


def _walk(node: Mapping[str, Any], prefix: str, flat: dict[str, Any], out: list[str]) -> None:
    """Recurse TOML tables so [look.glow] resolves to look.glow.enabled."""
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else key
        spec = SPEC_BY_KEY.get(dotted)
        if isinstance(value, dict):
            if not any(k.startswith(dotted + ".") for k in SPEC_BY_KEY):
                _warn(out, "unknown_key", dotted)  # whole table is foreign: skip subtree
                continue
            _walk(value, dotted, flat, out)
            continue
        if spec is None:
            _warn(out, "unknown_key", dotted, "not a table")
            continue
        ok, coerced = _coerce(spec, value, out)
        if ok:
            flat[dotted] = coerced


def _apply_cross_rules(flat: dict[str, Any], out: list[str], caps: Capabilities) -> None:
    # T7: wlr-layer-shell has no click-through; never leave the user trapped.
    if flat["input.pointer_passthrough"]:
        _warn(out, "unsupported_key", "input.pointer_passthrough", "forced false")
        flat["input.pointer_passthrough"] = False
        flat["input.pointer_click_action"] = "stop"
    # D1.4: ON_DEMAND requires protocol version >= 4.
    if flat["input.keyboard_mode"] == "on_demand" and caps.protocol_version < 4:
        _warn(out, "needs_protocol_4", "input.keyboard_mode", "forced exclusive")
        flat["input.keyboard_mode"] = "exclusive"
    # 2.3: a custom set with no glyphs is not a renderable choice.
    if flat["look.glyph_set"] == "custom" and not flat["look.custom_glyphs"]:
        _warn(out, "bad_choice", "look.glyph_set", "empty custom_glyphs")
        flat["look.glyph_set"] = "matrix"
    if flat["outputs.mode"] == "listed" and not flat["outputs.listed"]:
        _warn(out, "bad_value", "outputs.mode", "listed is empty")
        flat["outputs.mode"] = "all"
    if flat["perf.min_render_scale"] > flat["world.render_scale"]:
        flat["perf.min_render_scale"] = flat["world.render_scale"]
    if flat["perf.min_fps"] > flat["world.fps"]:
        flat["perf.min_fps"] = flat["world.fps"]


# ----------------------------------------------------------------- store ---


class ConfigStore:
    """Read-only view of config.toml with last-good retention. Never writes."""

    def __init__(self, path: str | os.PathLike[str], caps: Capabilities) -> None:
        self.path = Path(path)
        self.caps = caps
        self.warnings: tuple[str, ...] = ()
        self.rejected = False
        self._good: Config | None = None

    @property
    def config(self) -> Config:
        return self._good or load_default()

    def load(self) -> Config:
        try:
            raw = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            cfg, self.warnings, self.rejected = load_default(), (), False
            self._good = cfg
            return cfg
        except (tomllib.TOMLDecodeError, OSError) as exc:
            self.warnings = (f"config_rejected:{type(exc).__name__}",)
            self.rejected = True
            return self.config

        flat, warns, rejected = validate(raw, self.caps)
        if rejected:
            self.warnings = (f"config_rejected:{warns[0] if warns else 'rejected'}", *warns)
            self.rejected = True
            return self.config

        cfg = from_flat(flat)
        self._good, self.warnings, self.rejected = cfg, tuple(warns), False
        return cfg


# -------------------------------------------------------------- emit ---


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return repr(value)


def serialize_toml(cfg: Config | None = None) -> str:
    """Serialize a Config back to TOML (used by --print-default-config and tests)."""
    flat = to_flat(cfg or load_default())
    lines = [f"schema_version = {flat['schema_version']}", ""]
    tables: dict[str, list[str]] = {}
    for spec in SPEC:
        tables.setdefault(_table_of(spec.key), []).append(spec.key)
    for table, keys in tables.items():
        lines.append(f"[{table}]" if table else "")
        lines += [f"{k.rsplit('.', 1)[1]} = {_toml_value(flat[k])}" for k in keys]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "ATLAS", "LIVE", "RESET", "TOPOLOGY", "Capabilities", "Config", "ConfigStore",
    "GlowParams", "InputParams", "LookParams", "OutputsParams", "PerfParams",
    "RenderParams", "SPEC", "SPEC_BY_KEY", "Spec", "SUPPORTED_SCHEMA_VERSION",
    "WorldParams", "change_classes", "default_flat", "dominant_class", "from_flat",
    "load_default", "replace_config", "serialize_toml", "to_flat", "validate",
]

