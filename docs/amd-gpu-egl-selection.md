# AMD / hybrid render-node selection (measured)

The app picks the GPU that renders, and proves it, rather than trusting a static
setting. Everything below was measured on the two workstations, not inferred.

## Topology

| box | GPU driving the outputs | render node | EGL vendor file |
|---|---|---|---|
| widow (Pop!_OS 24.04, cosmic-comp) | NVIDIA RTX 3060 (PCI 0000:04:00.0, DP-4/DP-5) | renderD129 | 10_nvidia.json (only file present) |
| widow, iGPU | Radeon RX 7900 XTX (renderD128) - no connected output | renderD128 | - |
| grogu (GMKtec M6 Ultra) | AMD Phoenix1 1002:15BF, Radeon 760M, HDMI-A-1 | renderD128 (the only node) | 50_mesa.json (the only file present) |

## The ladder (cli.render_env -> render_source.reason in --diagnose)

1. [render] render_node honoured verbatim, and its PCI function checked against
   the connected DRM connectors (_resolve_pci / _pci_drives_output).
2. Empty, or a node that does not exist or does not drive any connected output:
   auto-select the function that owns a connected connector (reason=auto_connected),
   else the vendor implied by egl_vendor, else the only render node present
   (reason=auto_off_display, plus the off_display_gpu warning and
   LIBGL_ALWAYS_SOFTWARE=1).
3. render_scale is clamped to the smaller of the two requested scales, and
   stream_density to the amount of VRAM actually free (vram_free_bytes read from
   the sysfs drm render node), so a hybrid box cannot over-allocate against a
   dma_buf_pool_size of 69 MB. vram_total is also reported by --diagnose.

## Vendor steering

Vendor is read from PCI (0x1002 = AMD, else NVIDIA when 10_nvidia.json exists),
then:

    __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json
    MESA_LOADER_DRIVER_OVERRIDE=radeonsi

On an AMD-only box that is exactly what the auto ladder produces - grogu's
--diagnose reports vendor=amd, vendor_id=0x1002, json=.../50_mesa.json,
MESA_LOADER_DRIVER_OVERRIDE=radeonsi, node=/dev/dri/renderD128,
reason=auto_connected, and the GL layer answers
GL_RENDERER=AMD Radeon 760M Graphics (radeonsi). Forcing the vendor explicitly is
still allowed (setting render.egl_vendor to the mesa json plus
render.render_node=/dev/dri/renderD128) and produced the same result on the first
grogu run, before auto-detection existed.

A pinned node that does not exist is not fatal: config warns (unknown_render_node)
and keeps the value so the wrapper still runs elsewhere, and the ladder falls back
to the GPU that actually drives the displays.

## Caveats measured the hard way

- A user config always *replaces* the packaged one (no merge), so a stale
  render_node in ~/.config/matrix-overlay/config.toml is still a live problem on a
  different machine - the fallback is what saves it, not a merge.
- When the display is asleep (connected: 0), --diagnose reports
  blockers: ["layer_shell_unsupported", "no_outputs"] and exits 3 even though the
  GPU, library and protocols are fine. Check connector state before believing it.
- python3 on PATH may be an asdf shim with no PyGObject; run the suite and the app
  through /usr/bin/python3.
