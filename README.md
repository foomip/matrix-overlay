# matrix-overlay — Matrix rain over the COSMIC desktop

`Ctrl+Shift+M` dims every screen and runs The Matrix "digital rain" over it;
`Escape` (or a second keybind press, a click, or `--stop`) restores the desktop.

Target: Pop!_OS 24.04 **COSMIC / Wayland** (`cosmic-comp`). One
`zwlr_layer_shell_v1` OVERLAY surface per output, painted either by **GLES2 over
EGL** (default: its own Wayland client in `native/libmwl.so`, GPU-composited) or by
**Cairo through GTK3** (fallback). Python 3.12 + GTK3/GLib drive the loop, input and
config watching. Classic X11 screensavers (`xmatrix`, `glmatrix`,
`cmatrix -x`) cannot work here: Xwayland's root window is never composited.

## Files

| Path | Role |
|---|---|
| `~/.local/bin/matrix-rain` | wrapper: pins interpreter/env, preflight, exec |
| `~/.local/share/matrix-overlay/matrix_overlay/` | the app (`cli` `app` `world` `atlas` `render` `surface` `config`) |
| `…/matrix_overlay/glpaint.py` | pure GL packing: atlas layout, UV tables, cell batches, geometry (unit-tested, needs no display) |
| `…/matrix_overlay/mwl.py` | ctypes bindings for the native library (builds it on first run) |
| `…/matrix_overlay/glsurface.py` | GL backend: runtime, texture cache, event routing, `GlSurface` |
| `~/.local/share/matrix-overlay/native/` | `mwl.c`, protocol XMLs, `build.sh` → `libmwl.so` |
| `~/.config/matrix-overlay/config.toml` | **all tuning lives here, reloaded live** |
| `~/.local/state/matrix-overlay/matrix-rain.log` | log (also where a stray app stdout goes) |
| `$XDG_RUNTIME_DIR/matrix-overlay.lock` / `.status.json` | single-instance flock + live status |

## Bind the shortcut (COSMIC does not expand `$HOME` in `Spawn`)

COSMIC Settings → Keyboard → *View and customize shortcuts* → Custom shortcuts → **+**
→ name `Matrix rain`, command **`matrix-rain`** (bare word: `~/.local/bin` is on the
settings daemon's `PATH`), then press `Ctrl+Shift+M`.

`Spawn()` is **not** run by a shell (the daemon uses `pidfd_spawnp`), so never write
`"$HOME/..."`, `sh -c '...'`, `&&`, `|` or `>` there. Known COSMIC quirk: the Command
field cannot be edited after saving — delete the entry and re-add it.

## Tuning (live, no restart)

```bash
$HOME/.local/bin/matrix-rain --print-default-config   # show every key
tail -f $HOME/.local/state/matrix-overlay/matrix-rain.log
```

| Want | Key |
|---|---|
| darker / barely dimmed | `[look] dim_alpha` 0..0.95 |
| faster / slower rain | `[world] fall_speed`, `fps` |
| more / fewer streams | `stream_density` |
| longer comet tails | `tail_length`, `fade_rate` |
| bigger glyphs | `cell_size` (also the main CPU lever: fewer, larger cells) |
| the white-hot head | `head_color` (with `[look.glow]`) |
| katakana vs ASCII vs your own glyphs | `glyph_set`, `custom_glyphs` |
| auto-stop after a while | `[world] idle_auto_stop_seconds` |
| painter: GPU or GTK/Cairo | `[render] backend = "gl"` (default) / `"cairo"` |
| which GPU renders | `[render] render_node` (authoritative on the gl backend) |

Bad values are clamped, unknown keys ignored, and an unparseable file is rejected
while the running show keeps its last-good config — a typo can never drop the veil.

## Render backends (`[render] backend`)

**`gl` (default).** Its own Wayland client: `native/libmwl.so` binds
`zwlr_layer_shell_v1` + `zxdg_output_v1` + `wl_seat`, then renders textured quads
with GLES2 into an EGL window surface. The compositor imports those buffers as
(dmabuf) plane-backed EGL surfaces, so nothing is copied on the CPU per frame.

**`cairo` (fallback).** GTK3 + gtk-layer-shell + a `DrawingArea`, painting dirty
cells with Cairo and damaging only what changed. Chosen automatically if
`libmwl.so` cannot be built (the reason it stays fully maintained and tested).

Why the switch: on this stack the GTK paint path costs a **fixed ~58–70 ms per
frame regardless of content** — measured 99.8 % of one core at 22 fps, and it was
99 % even with only 522 cells, 0 % with no rain drawn at all. The cost is GDK
committing the whole damaged shm buffer (5120×2880×4 + 2560×1440×4 ≈ 74 MB) to the
compositor each frame, not the drawing. `Gtk.GLArea` is worse here (2 fps: it
starves on frame-callback serialisation), which is why the client is hand-rolled.

| | cairo | gl |
|---|---|---|
| CPU at 22 fps, 2 outputs | **99.8 %** | **25.7 %** |
| CPU at 22 fps, 1 output | ~60 % | 13.2 % |
| 30 fps target | not reachable (~22 max) | **30.0 fps** at 34.6 % |
| headroom | — | 60 fps with 20 000 quads/output at 9 % |
| coverage of the panel strip | no (clamped to 2560×1408) | **yes** (`exclusive_zone = -1`) |

The GL backend also fixes coverage: anchoring all four edges with
`exclusive_zone = -1` ignores the COSMIC panel's reserved area, so the veil now
covers the full 2560×1440 where the GTK surface was shortened to 1408 px.

Remaining cost is the **Python simulation** (~10 ms/frame at ~2000 cells); lowering
`fps`, `cell_size`, `stream_density` or `tail_length` reduces it proportionally.

## Diagnostics

```bash
/usr/bin/python3 -m matrix_overlay --diagnose       # caps, protocol, env, blockers
/usr/bin/python3 -m matrix_overlay --monitor-list   # what "listed" can match
/usr/bin/python3 -m matrix_overlay --self-test      # render frames headlessly, hash them
/usr/bin/python3 -m matrix_overlay --status         # live JSON (tick, fps_actual, cells)
```

`outputs.listed` matches **index** (`"0"`, `"1"`) or a **case-insensitive substring**.
On the `gl` backend the substring is matched against the *connector name* reported by
`zxdg_output_v1` (`"DP-4"`, `"DP-5"`) — an improvement over GTK3, which exposes no
connector name at all. On the `cairo` backend it matches the monitor **model**
(`"S2817Q"`) and `"DP-5"` cannot match. A digits-only entry always means index *only*.

## Tests

```bash
cd ~/.local/share/matrix-overlay
/usr/bin/python3 -m pytest -q                     # 280 unit tests, no compositor needed
MATRIX_OVERLAY_LIVE=1 /usr/bin/python3 -m pytest -q -s tests/live_smoke.py   # 3 live
```

`native/build.sh` regenerates the protocol code with `wayland-scanner` and compiles
`libmwl.so`; `mwl.py` runs it automatically on first use (and whenever `mwl.c` is
newer), and `test_glpaint.py` cross-checks the native vertex expander against the
pure Python packer so the two cannot drift apart.

The live suite covers both backends and asserts a **CPU budget** for the GL one
(< 40 % of a core at the shipped default) — the regression that motivated M6 was
precisely invisible to every other test.

Always invoke pytest as `/usr/bin/python3 -m pytest`: `PATH` `pytest`/`python3` are
asdf shims on Python 3.14 that have no `gi` (conftest aborts loudly if you don't).

## Measured on this machine

2 × 2560×1440, one at `scale=2`; NVIDIA RTX 3060 (`/dev/dri/renderD129`) drives both.

| Item | Value |
|---|---|
| cadence, shipped default | **22.0 fps achieved** (target 22), 2 outputs, ~2000 cells |
| CPU, gl backend | **25.7 %** of one core (single output: 13.2 %) |
| per frame | sim+queue ~9.7 ms, present ~2.9 ms per output |
| CPU, cairo backend | 99.8 % of one core at ~17 fps (kept only as fallback) |
| headroom | 60.0 fps with 20 000 quads per output at 9.0 % CPU |
| `[perf] guard` | keeps the promise: degrades `fps` toward `min_fps` when frames overrun, and never blurs glyphs (`render_scale` untouched — the cost is not pixel-bound) |

`[render] render_node` is authoritative on the gl backend, verified live — the
config key selects the physical GPU:

| `render_node` | `GL_RENDERER` reported |
|---|---|
| `/dev/dri/renderD129` (default) | `NVIDIA GeForce RTX 3060/PCIe/SSE2` (OpenGL ES 3.2, NVIDIA 580.173.02) |
| `/dev/dri/renderD128` | `Radeon RX 7900 XTX (radeonsi, navi31, ACO)` (Mesa 26.1.6) + an `off_display_gpu` warning |

The AMD node renders but presents slower (17.3 fps: it drives no connected output,
so buffers must cross devices), which is why NVIDIA stays the default here.

Two scheduling invariants the measurements established, both regression-tested:
the frame timer must subtract work done *inside* the tick (GL presents in-tick while
Cairo painted in GDK's draw callback, so only lateness used to be visible and GL
silently cost `period + work` per frame); and the compositor socket must be pumped
before the event queue is read, or buffer releases never arrive and
`eglSwapBuffers` stalls (measured: 14 fps from 17 ms of real work).

## Uninstall

Delete the shortcut, then:
`rm -rf ~/.local/share/matrix-overlay ~/.local/bin/matrix-rain ~/.config/matrix-overlay ~/.local/state/matrix-overlay`
Optionally `sudo apt remove gir1.2-gtklayershell-0.1 python3-gi-cairo python3-pytest` —
but **keep `libgtk-layer-shell0`**: `handy` depends on it.

The GL backend additionally needs `libwayland-dev`, `wayland-protocols`, `libegl-dev`,
`libgles-dev` and `gcc` to build `libmwl.so` (plus `wayland-scanner`, from
`libwayland-bin`). Removing them is harmless: the build fails, the app logs
`gl_unavailable` and paints with cairo instead.
