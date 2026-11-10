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

The supported path is the configuration package: one .deb that installs the key
and the source entry, nothing else.

    curl -fsSLO https://…/pool/main/m/matrix-overlay-repo/matrix-overlay-repo_0.1.1_all.deb
    sudo apt install ./matrix-overlay-repo_0.1.1_all.deb && sudo apt update

Doing it by hand is the same two files:

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

Over HTTPS against the published Pages site, on both workstations: the keyring
fetched from the public URL matched the signing key, `apt-get update` fetched
`InRelease`, `Packages`, the DEP-11 catalogue and three icon sets with no
warnings, and `appstreamcli get tech.i2r.matrixoverlay` resolved to
`Package: matrix-overlay`. The one-file path was then run **on grogu** exactly as
a user would: remove the hand-written key and source, `curl` the published
`matrix-overlay-repo` .deb, `sudo apt install ./<file>.deb`, `apt update` - after
which the catalogue resolves again and `Candidate: 0.1.1-1`. Both boxes were then
upgraded `0.1.1 -> 0.1.1-1` by apt, with `/etc/matrix-overlay/config.toml` kept.

## Version numbering, and why the .deb says `0.1.1-1`

Three places state the **upstream** version and move together:
`matrix_overlay/__init__.py`, the AppStream `<release version=...>`, and the git
tag. `debian/changelog` carries the **Debian** version with a revision
(`0.1.1-1`), because the source format is `3.0 (quilt)` and the archive expects
that form for a package whose tarball comes from an upstream tag. apt orders
`0.1.1-1` above `0.1.1`, so adopting the scheme did not strand the installs
already published. The consistency test compares the *upstream* part of the
changelog version with `__version__` and the metainfo and insists the revision
starts with a digit, so a stray hyphen in an upstream version still fails.

`matrix-overlay-repo` versions with the upstream release, not the Debian
revision: it is configuration, not a rebuild of the app.

## One field that is not free: `Priority`

Policy 4.7.3 stopped requiring `Priority`, and the upgrading checklist implies
you may drop it. Measured on this box, that is not safe yet: with no Priority
anywhere, `dpkg-genchanges` warns and writes `Priority: -` into the `.changes`
(Debian #1124443 is about that checklist wording). `debian/control` therefore
declares it **once**, in the source stanza, and the binary stanza inherits it -
quiet under old tooling, harmless under new. A test pins that shape so nobody
"modernises" it back into a warning.

## Where the two workstations stand

Both are configured through `matrix-overlay-repo`, so the store entry survives
`apt-get update`. Undoing it is one command per box:

    sudo apt-get purge matrix-overlay-repo    # removes the keyring and the source entry

grogu runs the packaged install (`/usr/bin/matrix-rain`); widow keeps the
dotfile dev install in `~/.local` and has the repository configured but the app
not installed, which is what makes the packaged candidate visible there.
grogu has **no** dotfile install at all, so `Ctrl+Shift+M` can only reach the
packaged shim: there is no second copy of the app there to wonder about. Both
boxes are on the archive-gated `0.1.4-2` (widow: candidate only, app not
installed), and `/etc/matrix-overlay/config.toml` survived the two upgrades as a
documented conffile (md5 unchanged).

## What the archive gate found (measured)

Every line here came from running the gate, not from reading policy.

- **The fallback's dependency was the primary path's dependency.** The glyph
  atlas is PangoCairo into Cairo `ImageSurface`s, then uploaded as a GL texture,
  so `python3-cairo` and `gir1.2-pango-1.0` belong in Depends. The build that
  declared them as Recommends installed fine and was unusable: `matrix-rain
  --help` died on `import cairo`. What caught it was the autopkgtest `smoke`
  entry, which installs the binary package **without** Recommends - the same
  configuration anyone gets from `--no-install-recommends`. "It worked on the
  developer's box" is exactly the failure mode this class has.
- **A diagnostic must not need the thing it diagnoses.** `render.py` (Cairo
  painter) now imports Cairo inside the three methods that draw, and the packaged
  shim runs its preflight only when a show is about to start. Otherwise the one
  command a stuck user reaches for (`--diagnose`) is the one command that cannot
  run. Guarded by a test that runs the shim against an interpreter which always
  answers "no usable backend", asserting both halves: diagnostics reach the
  program, starting a show is still gated.
- **`Autopkgtest: @` runs from the top of the source tree**, and `python -m
  matrix_overlay` puts the working directory on `sys.path[0]`. An entry claiming
  to test the *installed* package was testing the copy autopkgtest had just
  unpacked; the giveaway was a preflight line naming
  `/tmp/autopkgtest-.../build.*/src/native/libmwl.so`. Entries now `cd
  "$AUTOPKGTEST_TMP"` and assert `matrix_overlay.__file__` starts with
  `/usr/lib/matrix-overlay`. The old scratch variable is not spelled out anywhere
  in the file - lintian's `uses-deprecated-adttmp` fires on the token in a
  comment too.
- **`readelf -m` does not exist.** An invalid option prints a usage block, and
  under `set -eu` the grep under it fails as a confusing exit 1 far from the
  cause. The hardening assertions read the dynamic section (`readelf -dW`,
  `DT_BIND_NOW`) and the program headers (`GNU_RELRO`, non-executable `GNU_STACK`).
- **Comments in `debian/control` must start at column 0.** An indented `#` is a
  continuation of the field above it, so the comment text became part of the
  `Depends:` value and broke any parser that splits on commas.
- **Changelog lines have to fit the archive width (80).** Three releases in a row
  failed the build on their own entry. `packaging/wrap_changelog.py` re-wraps the
  bullets (78 columns, trailers and headers untouched), and a test asserts the
  committed changelog is already wrapped, so the failure surfaces in seconds
  instead of four minutes into `dpkg-buildpackage`.
- **A packaging-only fix takes a Debian revision, not a new tag.** Tag `0.1.4`
  names the upstream version; `0.1.4-1` and `0.1.4-2` are packaging. The orig
  tarball exported from the tag never changes, `debian/` is the delta, and apt
  still sorts `0.1.4-2 > 0.1.4-1 > 0.1.1-1`, so installed machines follow along.
- **Reproducibility, measured honestly:** two `dpkg-buildpackage -F` runs of the
  same source gave byte-identical `.deb` files here. That is not the same claim
  as a `diffoscope` run across toolchains, which has not been done.

