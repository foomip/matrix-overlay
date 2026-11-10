#!/bin/sh
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
# install.sh - one source tree, two install profiles, no code branches.
#
#   dotfile  $HOME/.local/share/matrix-overlay + ~/.local/bin/matrix-rain
#            (what widow and grogu run today; the dev path, builds on first run)
#   system   /usr/lib/matrix-overlay + /usr/bin/matrix-rain + /etc/matrix-overlay
#            (the packaged profile; --destdir makes it relocatable for dpkg/dh)
#
# The app resolves its own root from __file__ (cli.APP_DIR), so copying the tree
# anywhere is enough: there is no path to configure and nothing to patch.
set -eu

usage() {
    cat <<'EOF'
usage: sh install.sh [--mode dotfile|system] [--home DIR] [--destdir DIR] [--uninstall]

  --mode dotfile   install under $HOME (default)
  --home DIR       treat DIR as $HOME (the tests use this)
  --mode system    FHS install: /usr/{lib,bin} + /etc/matrix-overlay
  --destdir DIR    install rooted at DIR (what dh/dpkg passes as DESTDIR)
  --uninstall      remove what this script installed (system profile keeps /etc)
EOF
}

MODE=dotfile
DESTDIR=""
HOME_DIR="${HOME:-}"
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)      MODE=$2; shift 2 ;;
        --home)      HOME_DIR=$2; shift 2 ;;
        --destdir)   DESTDIR=$2; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           usage >&2; exit 2 ;;
    esac
done

# An exported CDPATH from an interactive shell would redirect a bare `cd`
# (git#4977 class of bug): always clear it and use `cd --`.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

# copy_sub SRC DEST [tar excludes...]  - SRC may be a file or a directory.
copy_sub() {
    src=$1
    dest=$2
    shift 2
    [ -e "$src" ] || return 0
    mkdir -p "$dest"
    if [ -d "$src" ]; then
        ( CDPATH= cd -- "$src" && tar -cf - "$@" . ) | ( CDPATH= cd -- "$dest" && tar -xf - )
    else
        install -m 0644 "$src" "$dest/$(basename "$src")"
    fi
}

case "$MODE" in
    dotfile)
        [ -n "$HOME_DIR" ] || { echo "install.sh: no \$HOME set and no --home" >&2; exit 2; }
        APP="$HOME_DIR/.local/share/matrix-overlay"
        BIN="$HOME_DIR/.local/bin"
        if [ "$UNINSTALL" = 1 ]; then
            rm -rf "$APP"
            rm -f "$BIN/matrix-rain"
            echo "removed $APP and $BIN/matrix-rain"
            exit 0
        fi
        rm -rf "$APP"
        mkdir -p "$APP" "$BIN"
        # Generated protocol code never travels (build.sh regenerates it). A
        # prebuilt libmwl.so and its content stamp do travel: the stamp is what
        # proves the artifact matches these sources, and it is a content digest,
        # so editing mwl.c still forces exactly one rebuild on the next launch.
        copy_sub "$ROOT" "$APP" \
            --exclude=./.git --exclude=./.pytest_cache --exclude='*/__pycache__' \
            --exclude='*-client-protocol.c' --exclude='*-client-protocol.h' \
            --exclude=./install.sh
        install -m 0755 "$ROOT/bin/matrix-rain" "$BIN/matrix-rain"
        echo "installed dotfile profile: $APP"
        echo "shim: $BIN/matrix-rain (bind the COSMIC shortcut to the bare word 'matrix-rain')"
        ;;
    system)
        D=${DESTDIR:-/}
        LIB="$D/usr/lib/matrix-overlay"
        BIN="$D/usr/bin"
        ETC="$D/etc/matrix-overlay"
        if [ "$UNINSTALL" = 1 ]; then
            # /etc is distro config: dpkg owns its removal policy, we never delete it.
            rm -rf "$LIB"
            rm -f "$BIN/matrix-rain"
            echo "removed $LIB and $BIN/matrix-rain (left $ETC)"
            exit 0
        fi
        rm -rf "$LIB"
        mkdir -p "$LIB" "$BIN" "$ETC" "$D/usr/share/metainfo" "$D/usr/share/applications" \
            "$D/usr/share/icons/hicolor/scalable/apps"
        # Named pieces, never "$ROOT" itself: a deb build dir is called
        # matrix-overlay-<version>, so the tree name cannot be the install name.
        copy_sub "$ROOT/matrix_overlay" "$LIB/matrix_overlay" --exclude='*/__pycache__'
        # The prebuilt .so *and* its content stamp travel together: in a read-only
        # /usr install the stamp is what proves the artifact matches the shipped
        # sources, so the first launch does not go looking for a compiler.
        copy_sub "$ROOT/native" "$LIB/native" \
            --exclude='*-client-protocol.c' --exclude='*-client-protocol.h'
        # The native sources and build.sh ship on purpose: when the prebuilt .so
        # is unusable (wrong driver, unusual compositor), the app can rebuild the
        # client into a writable state dir. That only works if the script it runs
        # is executable, and lintian is right that an installed script with a
        # shebang should be.
        chmod 0755 "$LIB/native/build.sh"
        # No $LIB/bin: /usr/bin/matrix-rain is the one entry point and the app
        # never reads bin/. A second copy would be a second file to keep in step.
        copy_sub "$ROOT/LICENSE" "$LIB"
        copy_sub "$ROOT/README.md" "$LIB"
        install -m 0755 "$ROOT/bin/matrix-rain" "$BIN/matrix-rain"
        # Desktop integration: this trio is what puts the app in COSMIC Store
        # (metainfo + .desktop + hicolor icons). The component id, the .desktop
        # basename and the icon basenames must all be the same string, because
        # appstream-compose merges by basename.
        ID=tech.i2r.matrixoverlay
        SHARE="$D/usr/share"
        install -m 0644 "$ROOT/assets/$ID.metainfo.xml" "$SHARE/metainfo/$ID.metainfo.xml"
        install -m 0644 "$ROOT/assets/$ID.desktop" "$SHARE/applications/$ID.desktop"
        install -m 0644 "$ROOT/assets/$ID.svg" "$SHARE/icons/hicolor/scalable/apps/$ID.svg"
        for png in "$ROOT"/assets/hicolor/*/apps/$ID.png; do
            [ -e "$png" ] || continue
            size=$(basename "$(dirname "$(dirname "$png")")")
            mkdir -p "$SHARE/icons/hicolor/$size/apps"
            install -m 0644 "$png" "$SHARE/icons/hicolor/$size/apps/$ID.png"
        done
        if [ ! -f "$ETC/config.toml" ]; then
            cat > "$ETC/config.toml" <<'EOF'
# System-wide defaults for matrix-overlay.
# Exactly ONE config file is read (--config, then $MATRIX_OVERLAY_CONFIG, then
# ~/.config/matrix-overlay/config.toml, then this one): a user file replaces
# these values rather than merging with them, so this file only ever holds
# deliberate distro choices. Keys omitted here fall back to the built-in
# defaults in matrix_overlay/config.py.
schema_version = 1

[render]
backend = "gl"
render_node = ""        # "" = auto-detect the GPU that drives the connected outputs
EOF
        fi
        echo "installed system profile: $LIB, $BIN/matrix-rain, $ETC/config.toml"
        ;;
    *)
        echo "install.sh: unknown --mode '$MODE' (dotfile|system)" >&2
        exit 2
        ;;
esac
