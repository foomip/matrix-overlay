#!/bin/sh
# Build and publish everything the repository needs, in one go.
#
#   sh packaging/publish.sh            # build both packages, publish, tell you how to push
#   ARTDIR=/some/dir APTDIR=/other sh packaging/publish.sh
#
# Two packages go into one repository:
#   * matrix-overlay        - the application (+ its AppStream metainfo, which is
#                             what makes the store listing exist)
#   * matrix-overlay-repo   - our signing public key + the source entry, so adding
#                             the repository is one file instead of four commands
#
# The published tree under $APTDIR is what belongs on the Pages branch; .gnupg and
# revocation belong to the operator and must never be pushed.
set -eu
CDPATH=
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
ARTDIR=${ARTDIR:-$HOME/artifacts}
APTDIR=${APTDIR:-$ARTDIR/apt}
VERSION=$(/usr/bin/python3 -c "import sys; sys.path.insert(0, '$ROOT');
import matrix_overlay; print(matrix_overlay.__version__)")

say() { printf 'publish: %s\n' "$*"; }
mkdir -p "$ARTDIR"

# ---------------------------------------------------------------- build app --
# A clean copy is built, exactly like the laboratory gate: the tree in front of us
# may hold generated protocol sources, caches or half-saved files.
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT INT TERM
say "building matrix-overlay $VERSION from a clean copy"
( CDPATH= cd -- "$ROOT" && tar -cf - --exclude=./.git --exclude='*/__pycache__' \
      --exclude=./.pytest_cache --exclude='*-client-protocol.c' \
      --exclude='*-client-protocol.h' . ) | ( CDPATH= cd -- "$BUILD" && tar -xf - )
( CDPATH= cd -- "$BUILD" && dpkg-buildpackage -us -uc -b >/dev/null )
# The .deb is named with the changelog version, which carries a Debian revision
# (0.1.1-1) now that the source is quilt format, so glob it instead of
# reconstructing the name from the upstream version.
built=$(ls -1t "$BUILD/.."/matrix-overlay_*_amd64.deb 2>/dev/null | head -1)
[ -n "$built" ] || { say "the package build produced nothing"; exit 1; }
mv -f "$built" "$ARTDIR/"
APP=$ARTDIR/$(basename "$built")
say "changelog version: $(basename "$APP" | sed -E 's/^matrix-overlay_(.*)_amd64.deb$/\1/')"
say "app package: $APP"

# ------------------------------------------------------------ build repo cfg --
OUTDIR=$ARTDIR sh packaging/repo-config/build.sh
CFG=$ARTDIR/matrix-overlay-repo_${VERSION}_all.deb
[ -f "$CFG" ] || { say "missing $CFG"; exit 1; }
say "repository package: $CFG"

# ------------------------------------------------------------------ publish --
sh packaging/publish_repo.sh --deb "$APP" --deb "$CFG" --out "$APTDIR"

cat <<NOTE

publish: next step - push the published tree to the Pages branch (gh-pages):
  git -C <checkout> worktree add /tmp/mo-pages gh-pages
  rsync -a --delete $APTDIR/pool $APTDIR/dists $APTDIR/keyring /tmp/mo-pages/
  git -C /tmp/mo-pages add -A && git -C /tmp/mo-pages commit -m "release: $VERSION"
  git -C /tmp/mo-pages push && git -C <checkout> worktree remove /tmp/mo-pages
Never copy .gnupg/ or revocation/ anywhere public; they are the signing identity.

Client side (what the matrix-overlay-repo package does for them):
  curl -fsSLO https://foomip.github.io/matrix-overlay/pool/main/m/matrix-overlay-repo/matrix-overlay-repo_${VERSION}_all.deb
  sudo apt install ./matrix-overlay-repo_${VERSION}_all.deb && sudo apt update
NOTE
