# Packaging and store distribution (M4-M6)

Three install profiles share one tree and one compile path:

    sh install.sh --mode dotfile                 # ~/.local/{share,bin} (development)
    make install                                 # same, via the Makefile
    sh install.sh --mode system --destdir DIR    # FHS: /usr/bin, /usr/lib/matrix-overlay,
                                                 # /etc/matrix-overlay/config.toml (default only)

    make native | make dotfile | make system | make uninstall | make clean

The Debian build calls the same installer (`dh_auto_install → install.sh --mode
system --destdir debian/matrix-overlay`), so a hand install and a .deb cannot
drift. `debian/rules` compiles with `make native` and stamps the result with the
app's own `matrix_overlay.mwl.write_stamp`, so a package built anywhere reuses its
`libmwl.so` instead of recompiling at run time - and on a target without a
toolchain that would mean quietly falling back from GL to cairo.

## Store visibility

cosmic-comp filters layer-shell globals on `client_not_sandboxed`
(pop-os/cosmic-comp#2734; same wall in Hyprland), and Flatpak >= 1.15 always
attaches a Wayland security context. A Flatpak therefore cannot draw this overlay
at all, whatever permissions it asks for. The only route that works today is a
native package in a signed apt repository.

The store's catalogue comes from AppStream metadata that apt downloads - and
libappstream only ingests metadata from *trusted* archives, so signing is not
optional. `packaging/publish_repo.sh` builds that repository. Four rules, each
found by measurement and each silent when broken:

1. **Path.** apt's index targets (`/etc/apt/apt.conf.d/50appstream`) read
   `$(COMPONENT)/dep11/Components-$(NATIVE_ARCHITECTURE).yml`, i.e. inside the
   component directory - at the suite root apt never fetches it.
2. **Three forms.** The MetaKey is the uncompressed name. Publishing only
   `.yml.gz` made apt fetch the icon tars and skip the catalogue with no error
   anywhere. Publish `.yml`, `.yml.gz`, `.yml.xz` like Pop!_OS and Ubuntu.
3. **Header.** `appstreamcli convert` writes `File`/`Version` only; appstream then
   warns `No origin found for file ...` and cannot attribute entries. The script
   rewrites the header (`Origin`, `Archive`, `Component`, `Architectures`, `Time`).
   The origin id keys the client's catalogue (`/var/lib/swcatalog/icons/<origin>-<suite>-main`),
   so it must stay stable.
4. **Package link.** A component with no `Package:` entry is visible but not
   installable. compose cannot know the owning package from an extracted tree, so
   the script injects `Package: <pkgname>` per id - Pop's own spelling.

Everything under `dists/<suite>` is regenerated on each run; `pool/` and the signing
key are kept, so republishing never rotates the key.

About that key, stated exactly: it is a **primary** ed25519 key created with
`--quick-generate-key ... sign never`, so it has no encryption or authentication
capability; like every primary key it also self-certifies its own uid, so
`gpg --list-keys` reports `[SC]`. There are no subkeys. The secret key has **no
passphrase**, because publishing has to sign unattended - which means the
`0700` permissions on `<out>/.gnupg` are the only protection it has. Anyone who can
read `private-keys-v1.d/*.key` can publish packages that every client pinning this
keyring will accept and install. Treat the directory like a password: encrypted
backup, off the publishing machine, never in git.

Back up all of it: `<out>/.gnupg` (secret key, public keyring, trustdb and
`openpgp-revocs.d/`), `<out>/revocation/revoke-<fpr>.rev`, and a copy of
`<out>/keyring/matrix-overlay.gpg` (public, already published). Losing the private
key means we cannot sign new metadata, so clients stop updating; losing the
revocation certificate means we cannot tell clients to distrust a key we can no
longer keep secret - so rotate instead. To move publishing to another machine,
copy the whole `<out>` directory: `GNUPGHOME` travels inside it.

## Client side

    sudo install -d -m 755 /etc/apt/keyrings
    sudo install -m 644 matrix-overlay.gpg /etc/apt/keyrings/matrix-overlay.gpg
    printf 'deb [signed-by=/etc/apt/keyrings/matrix-overlay.gpg arch=amd64] https://… stable main\n' \
        | sudo tee /etc/apt/sources.list.d/matrix-overlay.list
    sudo apt-get update

No pinning: no `NotAutomatic` and no `Valid-Until` are emitted, so a forgotten
repository turns apt red instead of degrading quietly, and clients never need
`-t`. On Pop!_OS the DEP-11 icon index targets are already enabled
(`/etc/apt/apt.conf.d/60icons*`), so icons arrive with the catalogue.

## Proven

On grogu against an HTTP-served repository, with the package **not installed**:
apt fetched `stable/main amd64 Components`, `appstreamcli get
tech.i2r.matrixoverlay` returned the component with `Package: matrix-overlay`,
homepage and icon, and `/var/lib/swcatalog/icons/` gained
`matrix-overlay-stable-main`. Then `apt-get install` resolved the auto-generated
Depends/Recommends, the packaged binary ran (`--diagnose`, AMD: radeonsi +
`50_mesa.json` picked automatically), and `apt-get purge` plus removing the source
and keyring left the machine exactly as it was.
