#!/bin/sh
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Nelson Pascoal
# Build libmwl.so: the raw Wayland + EGL/GLES2 presentation layer.
#
# Replaces the GTK3/GDK paint path, whose fixed per-frame cost (a ~74 MB shm
# buffer copy across both outputs) pegged a CPU core at any frame rate. GL
# renders into dmabuf buffers the compositor imports directly: no CPU copy.
#
# Usage: sh native/build.sh          -> native/libmwl.so
set -e
cd "$(dirname "$0")"

PKG_WL=$(pkg-config --cflags --libs wayland-client wayland-egl)
PKG_EGL=$(pkg-config --cflags --libs egl glesv2 2>/dev/null || echo "-lEGL -lGLESv2")

# xdg-shell is not used directly, but the generated layer-shell code references
# xdg_popup_interface (zwlr_layer_surface_v1.get_popup), so it must be linked in.
for proto in wlr-layer-shell-unstable-v1 xdg-output-unstable-v1 xdg-shell; do
    wayland-scanner client-header "$proto.xml" "$proto-client-protocol.h"
    wayland-scanner private-code  "$proto.xml" "$proto-client-protocol.c"
done

gcc -shared -fPIC -O2 -std=gnu11 -Wall -Wextra -Wno-unused-parameter \
    -o libmwl.so mwl.c \
    wlr-layer-shell-unstable-v1-client-protocol.c \
    xdg-output-unstable-v1-client-protocol.c \
    xdg-shell-client-protocol.c \
    $PKG_WL $PKG_EGL

echo "built $(pwd)/libmwl.so ($(wc -c <libmwl.so) bytes)"
