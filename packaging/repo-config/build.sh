#!/bin/sh
# Build matrix-overlay-repo: the "add our apt repository" package.
#
# Why this exists: COSMIC Store (and GNOME Software, and apt itself) only show an
# app whose metadata comes from a repository the machine trusts. Our repository is
# signed and publishes the AppStream catalogue, but adding it used to be four
# commands and a downloaded key - fine for us, invisible to everyone else. This
# package is that step reduced to one file: install it and the catalogue is
# reachable, so the store gains an Install button.
#
# It carries exactly two files and nothing else: the signing *public* key and a
# deb822 source entry. It never runs apt itself, and it must never contain a
# secret key (see the fingerprint test in tests/test_repo_config_package.py).
#
#   sh packaging/repo-config/build.sh            -> ../matrix-overlay-repo_<ver>_all.deb
#
# Rebuild it whenever the signing key rotates: the shipped key is the one clients
# will pin.
set -eu
CDPATH=
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
HERE="$ROOT/packaging/repo-config"
# Artifacts land with the other published ones, never inside the source tree.
OUTDIR=${OUTDIR:-${ARTDIR:-$HOME/artifacts}}
mkdir -p "$OUTDIR"

VERSION=$(/usr/bin/python3 - <<PY
import sys; sys.path.insert(0, "$ROOT")
from matrix_overlay import __version__
print(__version__)
PY
)
KEY=$HERE/matrix-overlay.gpg
[ -f "$KEY" ] || { echo "repo-config: missing $KEY (export it: gpg --homedir <repo>/.gnupg --export <fpr> > $KEY)" >&2; exit 1; }
gpg --show-keys --with-colons "$KEY" | grep -q '^pub:' || { echo "repo-config: $KEY is not a public key" >&2; exit 1; }
if gpg --show-keys --with-colons "$KEY" | grep -q '^sec:'; then
    echo "repo-config: refusing to package a SECRET key" >&2; exit 1
fi

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT INT TERM
# The staging root comes from mktemp (0700); a package that records those modes
# installs a root directory no one but root can read. Force 755 on every directory
# we create, and strip group write from the config files.
chmod 755 "$STAGE"
install -d -m 755 "$STAGE/DEBIAN" "$STAGE/etc/apt/keyrings" \
                  "$STAGE/etc/apt/sources.list.d" "$STAGE/usr/share/doc/matrix-overlay-repo"

install -m 644 "$KEY" "$STAGE/etc/apt/keyrings/matrix-overlay.gpg"

# deb822, the form Pop!_OS documents and cosmic-store-era apt prefers. Keeping the
# architecture list explicit avoids apt probing binary-<arch> we do not publish and
# printing 404s at every update.
cat > "$STAGE/etc/apt/sources.list.d/matrix-overlay.sources" <<'SOURCES'
Types: deb
URIs: https://foomip.github.io/matrix-overlay
Suites: stable
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/matrix-overlay.gpg
Description: matrix-overlay - Matrix rain overlay for COSMIC/Wayland (signed repository publishing AppStream metadata)
SOURCES

# Best effort: make the new catalogue visible to software centres without waiting
# for the next `apt update`. Never fail an install over it.
cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
# Best effort only: refresh the software-centre cache so the new catalogue shows up
# without waiting for the next apt update. Never fail an install over it.
if command -v appstreamcli >/dev/null 2>&1; then
    appstreamcli refresh --source=os >/dev/null 2>&1 || true
fi
exit 0
POSTINST

# dpkg refuses maintainer scripts that are not executable (and a non-executable
# one would be silently skipped, which is worse).
chmod 755 "$STAGE/DEBIAN/postinst"

# Both config files are conffiles: a local edit (a mirror URL, a proxy) survives an
# upgrade, and a purge removes them instead of leaving a dead source behind.
printf '/etc/apt/keyrings/matrix-overlay.gpg\n/etc/apt/sources.list.d/matrix-overlay.sources\n' \
    > "$STAGE/DEBIAN/conffiles"

FPR=$(gpg --show-keys --with-colons "$KEY" | awk -F: '/^fpr/{print $10; exit}')
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: matrix-overlay-repo
Version: $VERSION
Section: misc
Priority: optional
Architecture: all
Depends: apt (>= 1.6), ca-certificates
Recommends: appstream
Maintainer: Nelson Pascoal <foomip@users.noreply.github.com>
Homepage: https://github.com/foomip/matrix-overlay
Description: apt repository signing key and source for matrix-overlay
 matrix-overlay draws The Matrix "digital rain" over the COSMIC/Wayland desktop
 and stops with Escape or a second keybinding press.
 .
 This package only installs the signing public key and the repository entry, so
 that the application and its store listing become installable. It contains no
 program code and runs nothing at install time beyond refreshing the software
 centre cache. The key is stored in the usual place (signed-by) rather than in
 the trusted-keyring directory, so its trust stays scoped to this one source.
 .
 Repository: https://foomip.github.io/matrix-overlay stable main
 Signing key fingerprint: $FPR
CONTROL

printf 'Source: https://github.com/foomip/matrix-overlay\n' \
    > "$STAGE/usr/share/doc/matrix-overlay-repo/README"

# Text files must not be group-writable in the final package, and the control
# archive is read-only by convention.
chmod 644 "$STAGE/etc/apt/sources.list.d/matrix-overlay.sources" \
          "$STAGE/usr/share/doc/matrix-overlay-repo/README" \
          "$STAGE/DEBIAN/conffiles" "$STAGE/DEBIAN/control"

deb=$OUTDIR/matrix-overlay-repo_${VERSION}_all.deb
dpkg-deb --build --root-owner-group "$STAGE" "$deb" >/dev/null
echo "built $deb ($(wc -c <"$deb") bytes, key $FPR)"
