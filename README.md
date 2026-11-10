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
| `~/.local/bin/matrix-rain` (installed copy of `bin/matrix-rain`; `/usr/bin/matrix-rain` when packaged) | wrapper: pins interpreter/env, runs `--preflight`, execs the app |
| `~/.local/share/matrix-overlay/matrix_overlay/` | the app (`cli` `app` `world` `atlas` `render` `surface` `config`) |
| `…/matrix_overlay/glpaint.py` | pure GL packing: atlas layout, UV tables, cell batches, geometry (unit-tested, needs no display) |
| `…/matrix_overlay/mwl.py` | ctypes bindings for the native library (builds it on first run) |
| `…/matrix_overlay/glsurface.py` | GL backend: runtime, texture cache, event routing, `GlSurface` |
| `~/.local/share/matrix-overlay/native/` | `mwl.c`, protocol XMLs, `build.sh` → `libmwl.so` |
| `~/.config/matrix-overlay/config.toml` | **all tuning lives here, reloaded live** |
| `~/.local/state/matrix-overlay/matrix-rain.log` | log (also where a stray app stdout goes) |
| `$XDG_RUNTIME_DIR/matrix-overlay.lock` / `.status.json` | single-instance flock + live status |

## Install profiles (dotfile and system)

One tree, two profiles, no code branches: the app resolves its own root from
`__file__` (`cli.APP_DIR`), so copying the tree somewhere is the whole install.

```sh
make native        # build libmwl.so - the only compile path; packaging calls this
make test          # unit suite (conftest aborts under a non-system interpreter)
make preflight diagnose selftest
sh install.sh --mode dotfile                    # $HOME/.local/share/matrix-overlay + ~/.local/bin/matrix-rain
sh install.sh --mode system --destdir DIR       # /usr/{lib,bin} + /etc/matrix-overlay (dpkg's DESTDIR)
sh install.sh --mode dotfile --uninstall        # remove; the system profile keeps /etc
```

`--mode system` ships `/etc/matrix-overlay/config.toml` (distro defaults) and never
writes into `/usr` at run time. If the install tree is read-only and holds no
prebuilt `libmwl.so`, the build stages its sources into
`$XDG_STATE_HOME/matrix-overlay/native` and stamps the artifact with a content
digest, so staged copies never cause a rebuild on every launch.

**Config chain** - exactly one file is read, so a user file *replaces* the system
defaults instead of merging with them: `--config PATH` > `$MATRIX_OVERLAY_CONFIG`
> `$XDG_CONFIG_HOME/matrix-overlay/config.toml` (`$HOME/.config` fallback) >
`/etc/matrix-overlay/config.toml`. Logs, the first-run build dir and crash traces
live under `$XDG_STATE_HOME/matrix-overlay` (`$HOME/.local/state` fallback).

## Install from the apt repository (what software centres see)

The published repository is signed and publishes the AppStream catalogue, so the
app shows up in COSMIC Store search and installs from there.

**Add the repository with one file** (this is the whole setup - it installs the
signing public key and the source entry, and nothing else):

```sh
curl -fsSLO https://foomip.github.io/matrix-overlay/pool/main/m/matrix-overlay-repo/matrix-overlay-repo_0.1.1_all.deb
sudo apt install ./matrix-overlay-repo_0.1.1_all.deb && sudo apt update
apt-cache policy matrix-overlay          # Candidate: 0.1.1
```

Or do it by hand, which is the same two files:

```sh
sudo install -d -m 755 /etc/apt/keyrings
sudo install -m 644 matrix-overlay.gpg /etc/apt/keyrings/matrix-overlay.gpg
printf 'deb [signed-by=/etc/apt/keyrings/matrix-overlay.gpg arch=amd64] https://foomip.github.io/matrix-overlay stable main\n' \
    | sudo tee /etc/apt/sources.list.d/matrix-overlay.list
sudo apt-get update
```

Verified on a fresh machine: `apt-get update` fetches
`stable/main amd64 Components` plus the icon sets, `appstreamcli get
tech.i2r.matrixoverlay` resolves to `Package: matrix-overlay`, and
`/var/lib/swcatalog/icons/` gains `matrix-overlay-stable-main` - all before the
package is installed, which is what makes the store entry installable rather than
informational. To build and publish both packages at once, run `make publish`
(it builds the application and `matrix-overlay-repo` from a clean copy, publishes
them into one signed repository, and prints the push and client instructions);
`sh packaging/publish_repo.sh --deb <deb> [--deb <deb>] --out <dir>` is the step
that signs and indexes a repository.

The notes worth reading before changing any of this:
`docs/packaging-and-store.md` (the four DEP-11 rules that decide whether the
store sees anything, the key ceremony, version numbering),
`docs/cosmic-store-integration-plan.md` (every distribution route and its honest
state), `docs/debian-rfs-draft.md` (the Debian sponsorship request and what is
still unverified), and `docs/amd-gpu-egl-selection.md` (how the rendering GPU is
chosen on multi-vendor machines).

**Not a Flatpak.** cosmic-comp registers `zwlr_layer_shell_v1` through a
`client_not_sandboxed` filter ([pop-os/cosmic-comp#2734](https://github.com/pop-os/cosmic-comp/issues/2734))
and Flatpak >= 1.15 always attaches a Wayland security context, so a sandboxed
build cannot draw the overlay at all, whatever permissions it requests. Upstream's
advice for shell components is the distribution's own packaging system, which is
what this is.

## Building a package (`.deb`)

`debian/` lives in the tree, so debhelper is the only extra build dependency:

```sh
dpkg-buildpackage -us -uc -b     # make native -> make test -> install.sh --mode system
sudo dpkg -i ../matrix-overlay_0.1.1_amd64.deb
```

The package installs exactly what `install.sh --mode system` lays out, plus the
desktop-integration trio: `/usr/share/metainfo/tech.i2r.matrixoverlay.metainfo.xml`,
`/usr/share/applications/tech.i2r.matrixoverlay.desktop` and the hicolor icons.
That trio is what makes the app appear in COSMIC Store - `appstreamcli refresh
--force && appstreamcli search matrix` on an installed box shows the component
with its name, summary and icon, which is the catalogue the store reads.

The component id, the `.desktop` basename and the icon basenames are deliberately
the same lowercase string (`tech.i2r.matrixoverlay`): `appstream-compose` merges
metainfo with the desktop file **by basename**, and AppStream wants ids
lowercase. A metainfo may not name local icon paths either (only `stock` or
`remote`): the cached icons come from the installed hicolor set.

`native/build.sh` honours `CFLAGS`/`LDFLAGS`, so distribution hardening applies,
and `debian/rules` stamps the built `libmwl.so` using the app's own
`mwl.write_stamp` - the same code that reads the stamp, so the packaged artifact
is current without any digest logic duplicated in the build.

## Bind the shortcut (one manual step after installing)

Nothing in COSMIC lets a package register a shortcut for you, so do it once after
installing - from the repository, the `.deb`, or `install.sh`:

**COSMIC Settings → Keyboard → *View and customize shortcuts* → Custom shortcuts → +**
→ name `Matrix rain`, command **`matrix-rain`**, then press `Ctrl+Shift+M` to capture
the binding. Stop the overlay with `Escape`, a click, a second press of the
shortcut, or `matrix-rain --stop`.

The command must be the **bare word** `matrix-rain`: a packaged install puts it in
`/usr/bin`, a dotfile install in `~/.local/bin`, and both are on the settings
daemon's `PATH`.

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
| more / fewer streams | `stream_density` (a per-column spawn probability, so it is capped at 1.0: a larger number is clamped, not denser) |
| longer comet tails | `tail_length`, `fade_rate` |
| bigger glyphs | `cell_size` (also the main CPU lever: fewer, larger cells) |
| the white-hot head | `head_color` (with `[look.glow]`) |
| katakana vs ASCII vs your own glyphs | `glyph_set`, `custom_glyphs` |
| auto-stop after a while | `[world] idle_auto_stop_seconds` |
| painter: GPU or GTK/Cairo | `[render] backend = "gl"` (default) / `"cairo"` |
| which GPU renders | `[render] render_node` (`""` = auto-detect, the default; an explicit node still wins) |

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
/usr/bin/python3 -m matrix_overlay --diagnose       # caps, protocol, env, blockers, render_source
/usr/bin/python3 -m matrix_overlay --preflight        # can any backend run here? (no surfaces)
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
/usr/bin/python3 -m pytest -q                     # 311 unit tests, no compositor needed
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

### Which GPU renders (`[render] render_node`)

`render_node = ""` (the default since M2) auto-detects. The ladder, in order:

1. an explicit node that exists on this host always wins;
2. an explicit node that does not exist logs `render_node_missing` and auto
  resumes, so a config copied from another box self-heals instead of failing;
3. otherwise the card owning the most **connected** DRM connectors wins;
4. tie-break: most VRAM (`mem_info_vram_total`), then PCI address. The render
  node *number* is never the decider: on widow the lower number is the
  off-display XTX, which measured 17.3 fps against 22.0;
5. no connected output anywhere: biggest GPU, with an `off_display_gpu` warning;
6. no render nodes at all: no node and no vendor pin.

The EGL vendor is then pinned **only to a glvnd file that exists**; if the
expected one is absent the variable is left unset (glvnd auto-discovers) and
`glvnd_vendor_json_missing` is logged. Pinning a path that does not exist makes
glvnd load no vendor at all, which is how an AMD-only box dies with
`eglGetDisplay failed: 0x3001`. `DRI_PRIME` is set only for a multi-GPU box whose
chosen node is off-display; `MESA_LOADER_DRIVER_OVERRIDE=radeonsi` only for a
resolved AMD vendor.

`--diagnose` reports the decision, not just the env:
`render_source` carries `node`, `requested`, `pci`, `vendor`, `json`, `reason`,
`connected`, `gpus`, `vram_total`. The same object is logged at start as
`render_source ...`.

The config key still selects the physical GPU explicitly, verified live:

| `render_node` | `GL_RENDERER` reported |
|---|---|
| `/dev/dri/renderD129` (what auto-detect picks here) | `NVIDIA GeForce RTX 3060/PCIe/SSE2` (OpenGL ES 3.2, NVIDIA 595.104.02 as of 2026-11-09) |
| `/dev/dri/renderD128` | `Radeon RX 7900 XTX (radeonsi, navi31, ACO)` (Mesa 26.1.6) + an `off_display_gpu` warning |

The AMD node renders but presents slower (17.3 fps: it drives no connected output,
so buffers must cross devices), which is why auto-detect does not pick it here
(and why `render_node` must stay overridable for a box wired the other way).

Two scheduling invariants the measurements established, both regression-tested:
the frame timer must subtract work done *inside* the tick (GL presents in-tick while
Cairo painted in GDK's draw callback, so only lateness used to be visible and GL
silently cost `period + work` per frame); and the compositor socket must be pumped
before the event queue is read, or buffer releases never arrive and
`eglSwapBuffers` stalls (measured: 14 fps from 17 ms of real work).

## Uninstall

Delete the shortcut, then `sh install.sh --mode dotfile --uninstall` (or
`rm -rf ~/.local/share/matrix-overlay ~/.local/bin/matrix-rain`), plus
`~/.config/matrix-overlay ~/.local/state/matrix-overlay` and, for a system
install, `/etc/matrix-overlay` (dpkg removes its conffile on purge).
Optionally `sudo apt remove gir1.2-gtklayershell-0.1 python3-gi-cairo python3-pytest` —
but **keep `libgtk-layer-shell0`**: `handy` depends on it.

The GL backend additionally needs `libwayland-dev`, `wayland-protocols`, `libegl-dev`,
`libgles-dev` and `gcc` to build `libmwl.so` (plus `wayland-scanner`, from
`libwayland-bin`). Removing them is harmless: the build fails, the app logs
`gl_unavailable` and paints with cairo instead.
