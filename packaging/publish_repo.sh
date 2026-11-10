#!/bin/sh
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
# publish_repo.sh - assemble an apt repository that COSMIC Store can read.
#
# A .deb alone is not enough for the store: libappstream only ingests metadata
# from *trusted* archives, so the repo has to be signed and has to publish
# AppStream metadata (YAML catalog + icons) referenced from the Release file.
#
#   sh packaging/publish_repo.sh --deb ~/artifacts/matrix-overlay_0.1.0_amd64.deb \
#                                --out ~/artifacts/apt
#
# Everything is derived from the package itself: the metadata is composed from
# the metainfo/.desktop/icons *inside* the .deb, so the catalogue can never
# describe something other than what the package installs.
set -eu

usage() {
    cat <<'EOF'
usage: sh packaging/publish_repo.sh [--deb FILE]... [--out DIR] [--suite NAME]
                                    [--origin NAME] [--components LIST] [--verbose]

  --deb FILE        the .deb to publish; repeat it to publish several packages
                    into one repository (default: newest matrix-overlay_*_amd64.deb
                    in --deb-dir, which defaults to ~/artifacts)
  --out DIR         repository root (default: ~/artifacts/apt)
  --suite NAME      apt suite/branch (default: stable)
  --origin NAME     AppStream origin string, i.e. the catalogue's source name
  --components L    comma-separated component ids to include (default: all found)
  --verbose         pass -v to the metadata tools and echo each step
EOF
}

NDEB=0
OUT="${HOME}/artifacts/apt"
DEB_DIR="${HOME}/artifacts"
SUITE=stable
ORIGIN=matrix-overlay
COMPONENTS=""
ARCH=$(dpkg --print-architecture)
SPLICE=""
VERBOSE=""

while [ $# -gt 0 ]; do
    case "$1" in
        # Indexed variables, not a joined string: package paths can contain spaces,
        # and command substitution would eat the separator such a list would need.
        --deb)         NDEB=$((NDEB+1)); eval "DEB$NDEB=\$2"; shift 2 ;;
        --deb-dir)     DEB_DIR=$2; shift 2 ;;
        --out)         OUT=$2; shift 2 ;;
        --suite)       SUITE=$2; shift 2 ;;
        --origin)      ORIGIN=$2; shift 2 ;;
        --components)  COMPONENTS=$2; shift 2 ;;
        -v|--verbose)  VERBOSE=-v; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             usage >&2; exit 2 ;;
    esac
done

say() { printf 'publish_repo: %s\n' "$*"; }
run() { [ -n "$VERBOSE" ] && printf '  $ %s\n' "$*" >&2; "$@"; }

if [ "$NDEB" -eq 0 ]; then
    DEB1=$(ls -1t "$DEB_DIR"/matrix-overlay_*_amd64.deb 2>/dev/null | head -1)
    [ -n "$DEB1" ] || { say "no .deb found under $DEB_DIR (build one with dpkg-buildpackage)"; exit 2; }
    NDEB=1
fi
i=1
while [ "$i" -le "$NDEB" ]; do
    eval "d=\$DEB$i"
    [ -f "$d" ] || { say "no such package: $d"; exit 2; }
    eval "PKG$i=\$(dpkg-deb -f \"$d\" Package)"
    eval "VER$i=\$(dpkg-deb -f \"$d\" Version)"
    eval "say \"package: \$PKG$i \$VER$i (\$d)\""
    i=$((i+1))
done

# ------------------------------------------------------------- signing key ---
# Unattended signing needs an unprotected key, so it is confined to the repo dir
# with a revocation certificate generated up front. Losing .gnupg means every
# client has to re-trust a new key: back it up like a secret.
export GNUPGHOME="$OUT/.gnupg"
KEYID=""
if [ -f "$OUT/keyid" ]; then
    KEYID=$(cat "$OUT/keyid")
else
    mkdir -p "$OUT"
    install -d -m 700 "$GNUPGHOME"
    say "no signing key yet: generating an ed25519 key (usage: sign) in $GNUPGHOME"
    gpg --batch --yes --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "matrix-overlay apt signing key" ed25519 sign never
    KEYID=$(gpg --batch --with-colons --list-secret-keys \
            | awk -F: '/^fpr/{print $10; exit}')
    [ -n "$KEYID" ] || { say "key generation failed"; exit 3; }
    echo "$KEYID" > "$OUT/keyid"
    mkdir -p "$OUT/keyring" "$OUT/revocation"
    gpg --batch --armor --export "$KEYID" > "$OUT/keyring/matrix-overlay.asc"
    gpg --batch --export "$KEYID" > "$OUT/keyring/matrix-overlay.gpg"
    # gpg 2.4 refuses --gen-revoke in batch mode but already wrote an automatic
    # self-revocation next to the key: keep a copy outside GNUPGHOME, because a
    # leaked key you cannot revoke is worse than one you can.
    AUTO=$(ls "$GNUPGHOME"/openpgp-revocs.d/*.rev 2>/dev/null | head -1)
    if [ -n "$AUTO" ]; then
        cp "$AUTO" "$OUT/revocation/revoke-$KEYID.rev"
        chmod 600 "$OUT/revocation/revoke-$KEYID.rev"
    else
        say "WARNING: no revocation certificate found; generate one before publishing"
    fi
    say "signing key $KEYID"
    say "public key: $OUT/keyring/matrix-overlay.asc"
    say "REVOCATION CERTIFICATE: $OUT/revocation/ - back it up with .gnupg"
fi

# Everything under dists/<suite> is generated: regenerate it wholesale, or a file
# from an older layout keeps being served and indexed (a stale metadata path is
# invisible to the publisher and confusing on the client). The pool and the key
# are kept, so republishing never rotates the signing key or drops a release.
rm -rf "$OUT/dists/$SUITE"

# ------------------------------------------------------------------- pool ----
mkdir -p "$OUT/dists/$SUITE/main/binary-$ARCH"
i=1
while [ "$i" -le "$NDEB" ]; do
    eval "d=\$DEB$i; p=\$PKG$i"
    eval "POOL$i=\"pool/main/\$(printf '%.1s' \"\$p\")/\$p\""
    eval "mkdir -p \"\$OUT/\$POOL$i\""
    eval "cp -f \"\$d\" \"\$OUT/\$POOL$i/\$(basename \"\$d\")\""
    eval "say \"pool: \$POOL$i/\$(basename \"\$d\")\""
    i=$((i+1))
done

# ---------------------------------------------------------------- metadata ---
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM
# Which package owns which component id: the catalogue must carry a Package: link
# per component, and icon tarballs are named <pkgname>_<id>.png, so both need the
# mapping rather than a single package name now that a repository can ship several.
MAP=""
i=1
while [ "$i" -le "$NDEB" ]; do
    eval "d=\$DEB$i; p=\$PKG$i"
    for meta in $(dpkg-deb -c "$d" | awk '{print $NF}' \
                    | grep '^\./usr/share/metainfo/.*\.metainfo\.xml$'); do
        MAP="$MAP$(printf '%s' "${MAP:+,}")$(basename "$meta" .metainfo.xml)=$p"
    done
    i=$((i+1))
done
component_owner() { printf '%s\n' "$MAP" | tr ',' '\n' | sed -n "s|^$1=||p" | head -n 1; }

# Every payload is unpacked into the same root, so the catalogue is the union of
# what the repository actually ships. A package with no metainfo (the repository
# configuration package, for instance) simply contributes nothing here.
i=1
while [ "$i" -le "$NDEB" ]; do
    eval "dpkg-deb -x \"\$DEB$i\" \"$WORK/root\""
    i=$((i+1))
done
[ -d "$WORK/root/usr/share/metainfo" ] || { say "package ships no metainfo"; exit 4; }

args="--prefix=/usr --result-root=$WORK/meta --data-dir=$WORK/meta --origin=$ORIGIN $VERBOSE"
[ -n "$COMPONENTS" ] && args="$args --components=$COMPONENTS"
# shellcheck disable=SC2086
run appstreamcli compose $args "$WORK/root"
[ -f "$WORK/meta/$ORIGIN.xml.gz" ] || { say "compose produced no catalog"; exit 4; }

# libappstream reads YAML from archives, and `appstreamcli convert` emits exactly
# the DEP-11 catalogue layout that Pop!_OS and Ubuntu publish today. The path is
# not free choice: apt's own index targets (/etc/apt/apt.conf.d/50appstream) look
# the catalogue up as "$(COMPONENT)/dep11/Components-<arch>.yml", so it lives
# under the component directory - at the suite root apt simply never fetches it
# and the store stays blind.
gunzip -c "$WORK/meta/$ORIGIN.xml.gz" > "$WORK/meta/catalog.xml"
run appstreamcli convert "$WORK/meta/catalog.xml" "$WORK/meta/components.yml"
mkdir -p "$OUT/dists/$SUITE/main/dep11"

# Compose cannot know which package owns a component when it reads a bare
# extracted tree, and a component with no package link is something the store can
# show but not install. Add the link the way Pop's own DEP-11 files spell it
# (`Package: <pkgname>`), per component id.
if [ -z "$COMPONENTS" ]; then
    COMPONENTS=""
    for meta in "$WORK/root/usr/share/metainfo"/*.metainfo.xml; do
        [ -e "$meta" ] || continue
        COMPONENTS="$COMPONENTS$(printf '%s' "${COMPONENTS:+,}")$(basename "$meta" .metainfo.xml)"
    done
fi
python3 - "$WORK/meta/components.yml" "$COMPONENTS" "$MAP" "$ORIGIN" "$SUITE" "$ARCH" <<'PYIN'
import sys
import time
path, want, pkgmap, origin, suite, arch = sys.argv[1:7]
owner = dict(pair.split("=", 1) for pair in pkgmap.split(",") if "=" in pair)
body = open(path, encoding="utf-8").read()

# Replace the catalogue header with a canonical DEP-11 one. `convert` writes
# File/Version only, and appstream then warns "No origin found for file ..." and
# cannot attribute the entries; Ubuntu's own files carry Origin/Archive/
# Component/Architectures/Time. The origin id must be unique per repository - it
# is how the catalogue is keyed on the client (/var/lib/swcatalog/<origin>).
lines = body.splitlines(keepends=True)
i = 0
if lines and lines[0].strip() == "---":          # skip the header block
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        i += 1
    if i < len(lines):
        i += 1
header = ("File: DEP-11\nVersion: '1.0'\n"
          f"Origin: {origin}-{suite}-main\nArchive: {suite}\nComponent: main\n"
          f"Architectures:\n - {arch}\nTime: {time.strftime('%Y%m%dT%H%M%S', time.gmtime())}\n")
body = "---\n" + header + "---\n" + "".join(lines[i:])

out, hit = [], 0
for line in body.splitlines(keepends=True):
    out.append(line)
    if line.startswith("ID: ") and line[4:].strip() in want:
        cid = line[4:].strip()
        pkg = owner.get(cid) or (next(iter(owner.values()), "") if owner else "")
        if not pkg:
            raise SystemExit(f"publish_repo: component {cid} has no owning package "
                             f"in the published set - check --components")
        out.append(f"Package: {pkg}\n")
        hit += 1
if not hit:
    raise SystemExit(f"publish_repo: no component matched {sorted(want)} - fix the ids")
open(path, "w", encoding="utf-8").write("".join(out))
PYIN
# Publish the catalogue uncompressed *and* .gz *and* .xz, exactly like Pop!_OS and
# Ubuntu do. apt's MetaKey is the uncompressed name
# (main/dep11/Components-<arch>.yml); a Release file advertising only the .gz form
# made apt skip the index - measured: it fetched the icon tars and silently never
# fetched the catalogue, so the store never saw the app at all.
DEP11="$OUT/dists/$SUITE/main/dep11/Components-$ARCH.yml"
cp "$WORK/meta/components.yml" "$DEP11"
gzip -9 -c "$DEP11" > "$DEP11.gz"
xz -9 -c "$DEP11" > "$DEP11.xz"

# DEP-11 icon pools are flat tarballs whose entries are named
# <pkgname>_<component-id>.png (read straight out of Pop's icons-64x64.tar.gz).
for size in 48x48 64x64 128x128; do
    src="$WORK/root/usr/share/icons/hicolor/$size/apps"
    [ -d "$src" ] || continue
    stage="$WORK/icons-$size"
    mkdir -p "$stage"
    for meta in "$WORK/root/usr/share/metainfo"/*.metainfo.xml; do
        cid=$(basename "$meta" .metainfo.xml)
        own=$(component_owner "$cid"); [ -n "$own" ] || own=$PKG1
        [ -f "$src/$cid.png" ] && cp "$src/$cid.png" "$stage/${own}_${cid}.png"
    done
    ( CDPATH= cd -- "$stage" && tar -cf "$OUT/dists/$SUITE/main/dep11/icons-$size.tar" . )
done
say "metadata: main/dep11/Components-$ARCH.yml{,.gz,.xz} (+ Package: link), icons tarballs"

# ------------------------------------------------------------- index, sign ---
# Index the whole pool, not one package's directory: a repository that publishes
# two packages has to offer both to apt.
( CDPATH= cd -- "$OUT" && apt-ftparchive packages pool \
    > "dists/$SUITE/main/binary-$ARCH/Packages" )
for ext in gz xz; do
    ( CDPATH= cd -- "$OUT/dists/$SUITE/main/binary-$ARCH" && case $ext in
        gz) gzip -9 -c Packages > Packages.gz ;;
        xz) xz -9 -c Packages > Packages.xz ;;
      esac )
done
# No NotAutomatic (it would force clients to name the suite on every install) and
# no Valid-Until (a stamp we forget to refresh turns apt red later).
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin="$ORIGIN" \
    -o APT::FTPArchive::Release::Label="$ORIGIN" \
    -o APT::FTPArchive::Release::Suite="$SUITE" \
    -o APT::FTPArchive::Release::Codename="$SUITE" \
    -o APT::FTPArchive::Release::Version=1 \
    release "$OUT/dists/$SUITE" > "$OUT/dists/$SUITE/Release"
gpg --batch --yes --local-user "$KEYID" --pinentry-mode loopback --passphrase '' \
    --armor --output "$OUT/dists/$SUITE/InRelease" --clearsign "$OUT/dists/$SUITE/Release"
say "signed: dists/$SUITE/InRelease (key $KEYID)"

# ------------------------------------------------------------- client lines --
BASEURL=${BASEURL:-https://foomip.github.io/matrix-overlay}
cat <<EOF

client setup (target machine):
  sudo install -d -m 755 /etc/apt/keyrings
  sudo install -m 644 keyring/matrix-overlay.gpg /etc/apt/keyrings/matrix-overlay.gpg
  printf 'deb [signed-by=/etc/apt/keyrings/matrix-overlay.gpg] $BASEURL $SUITE main\n' \\
      | sudo tee /etc/apt/sources.list.d/matrix-overlay.list
  sudo apt-get update && apt-cache policy matrix-overlay
EOF
say "repository ready under $OUT"
