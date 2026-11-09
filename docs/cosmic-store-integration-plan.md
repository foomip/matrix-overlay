# Cosmic Store integration plan (matrix-overlay)

Status: M1-M5 done and measured; M6 built and proven over HTTP. The remaining
step is publishing the repository on a public host, which needs the repository
owner (enable Pages or an OBS project, then push the published tree).

## Decision: the store route is native packaging, not Flatpak

cosmic-comp registers zwlr_layer_shell_v1 (and the toplevel-info globals) through
WlrLayerShellState::new_with_filter(dh, client_not_sandboxed)
(pop-os/cosmic-comp#2734, 2026-08-13; Drakulix 2026-08-14: "Currently there is no
path for this" - a future reworked cosmic-session "components" mechanism may allow
it, no timeline). Flatpak >= 1.15 always attaches wp_security_context_v1, so a
sandboxed client is filtered out whatever it asks for: a Flatpak cannot draw this
overlay at all. Upstream's interim recommendation is to package shell components
with the distribution's packaging system, which is what this does.

So: native .deb + AppStream metainfo + a signed apt repository publishing DEP-11.

## Identity and license

- Component id: tech.i2r.matrixoverlay (all lowercase; the AppStream validator
  flags uppercase ids). The .desktop file, the icon basenames and the metainfo
  filename must all equal it, because appstream-compose merges resources by
  basename.
- License: MIT, repository public at github.com/foomip/matrix-overlay. The vendored
  Wayland protocol XMLs keep their own copyrights (MIT / MIT-style / CC0), recorded
  in debian/copyright rather than overwritten.
- appstreamcli validate on the installed tree passes; the only note left is
  pedantic (metadata-provided-translation).

## Milestones

| milestone | state |
|---|---|
| M1 render-node / EGL vendor auto-detection | done, measured on AMD and NVIDIA |
| M2 port to grogu (AMD, single output) | done: GL renderer radeonsi, protocols fine |
| M3 grogu tuning and CPU/frame budget | done (--tune, live smoke passes on AMD) |
| M4 install profiles (dotfile / FHS) | done: install.sh --mode, Makefile, config chain |
| M5 .deb plus store assets (metainfo, icon, screenshot, desktop entry) | done: built, installed, verified, purged cleanly |
| M6 apt repository (signed, DEP-11) plus publication | built and proven end to end on a client; publication host pending |
| COSMIC custom shortcut Ctrl+Shift+M on grogu | manual step for the owner (no supported non-interactive registration) |

## What is left, and who owes it

- Repository owner: enable Pages (Settings, Pages, branch) or an OBS project, push
  the published tree (pool/, dists/, keyring/) to the Pages branch, and keep
  .gnupg/ plus revocation/ off that machine and backed up.
- Repository owner: bind Ctrl+Shift+M to matrix-rain in COSMIC Settings, Keyboard,
  Custom shortcuts on each box. COSMIC's Spawn() runs through pidfd_spawnp with no
  shell and no HOME expansion, so the command must be a PATH-resolvable bare word.
- Decide whether the default stream_density clamp should stay at the configured
  value or follow free VRAM on every start.

See docs/packaging-and-store.md for the packaging rules that were measured rather
than assumed (catalogue path, three compressed forms, the Origin header, the
Package link, key handling) and how to verify store visibility before shipping.
