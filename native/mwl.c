/* mwl.c - raw Wayland + EGL/GLES2 presentation layer for matrix-overlay.
 *
 * Why this exists: the GTK3/GDK path copied a ~74 MB shm buffer per frame
 * (5120x2880x4 on the scale=2 output plus 2560x1440x4 on the other), which is a
 * fixed ~58-70 ms of CPU per frame regardless of how little changed - measured:
 * density 0.1 (887 cells) and density 1.0 (1866 cells) both pinned a core, while
 * density 0.0 (no draws at all) used 0.3%. Rendering with GLES2 into an EGL
 * window surface hands the compositor dmabuf buffers it imports directly, so the
 * per-frame copy disappears.
 *
 * Single-threaded by design: the Python driver owns the loop, calls mwl_dispatch()
 * to pump Wayland events (blocking with timeout, so the process sleeps between
 * frames instead of spinning) and mwl_draw_quads()/mwl_surface_swap() to present.
 *
 * One EGLContext is shared by all outputs; each output has its own EGLSurface.
 * Atlases differ per scale factor, so textures are created per scale and passed
 * explicitly to mwl_draw_quads().
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>

#include <wayland-client.h>
#include <wayland-egl.h>
#include <EGL/egl.h>
#include <GLES2/gl2.h>

#include "wlr-layer-shell-unstable-v1-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "mwl.h"

#define MWL_MAX_OUTPUTS  16
#define MWL_MAX_SURFACES 16
#define MWL_MAX_EVENTS   64
#define MWL_MAX_ERR     512
#define MWL_EV_LEN      128
#define MWL_NAME_LEN     64

/* Vertex layout: interleaved (x, y, u, v, r, g, b, a) - 8 floats, 32 bytes.
 * Positions are in device pixels with a top-left origin; the vertex shader maps
 * them to NDC and flips Y. Colours are premultiplied, matching the Cairo ARGB32
 * atlas we upload, so blending is (ONE, ONE_MINUS_SRC_ALPHA). */
#define MWL_FLOATS_PER_VERT 8

/* Blend modes for mwl_draw_quads. */
#define MWL_BLEND_PREMULT 0   /* over: dst + src*(1-a) - glyph bodies */
#define MWL_BLEND_ADD     1   /* dst + src - baked head glow */

struct mwl_output {
    struct wl_output *wl;
    struct zxdg_output_v1 *xdg;
    uint32_t id;                  /* registry name */
    int32_t x, y, w, h;           /* logical geometry, global compositor space */
    int32_t scale;
    int have_wl_mode, have_wl_scale, have_xdg_geom, have_xdg_name, xdg_done;
    char name[MWL_NAME_LEN];
};

struct mwl_surface {
    int used;
    int output_idx;
    struct mwl_output *out;   /* stable pointer: buffer scale lives here */
    struct wl_surface *wl;
    struct zwlr_layer_surface_v1 *layer;
    struct wl_egl_window *eglwin;
    EGLSurface egl;
    struct wl_callback *frame_cb;
    int width, height;            /* device pixels (buffer size) */
    int logical_w, logical_h;     /* from configure, logical pixels */
    int configured, closed, frame_ready;
    /* Grid geometry for the native vertex expander (mwl_set_geom): a cell at
     * (col, row) sits at ((col0+col)*cell_logical - x_px) * scale. */
    int col0, row0, x_px, y_px, cell_logical, uv_stride, px_scale;
    float *verts;                 /* expansion scratch, grows as needed */
    int verts_cap;                /* in vertices */
};

/* ---- global state (one client per process; the app is a singleton) ---- */
static struct wl_display *dpy;
static struct wl_registry *reg;
static struct wl_compositor *compositor;
static struct wl_seat *seat;
static struct wl_keyboard *keyboard;
static struct wl_pointer *pointer;
static int pointer_sid = -1;     /* surface currently under the cursor */
static struct zwlr_layer_shell_v1 *layer_shell;
static struct zxdg_output_manager_v1 *xdg_mgr;
static uint32_t layer_shell_version, compositor_version, seat_version;

static struct mwl_output outputs[MWL_MAX_OUTPUTS];
static int n_outputs;
static struct mwl_surface surfaces[MWL_MAX_SURFACES];
static uint32_t bound_names[MWL_MAX_OUTPUTS * 4];   /* registry names we bound */
static int n_bound;

static EGLDisplay egldpy = EGL_NO_DISPLAY;
static EGLConfig eglcfg;
static EGLContext eglctx = EGL_NO_CONTEXT;
static GLuint program;
static GLuint vbo;
static int gl_ready;
static GLint loc_pos = -1, loc_uv = -1, loc_color = -1, loc_res = -1, loc_tex = -1;

static char errbuf[MWL_MAX_ERR];
static char events[MWL_MAX_EVENTS][MWL_EV_LEN];
static int ev_head, ev_tail;      /* ring; ev_head == ev_tail means empty */

/* ------------------------------------------------------------ utilities -- */

static void set_error(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(errbuf, sizeof errbuf, fmt, ap);
    va_end(ap);
}

static void ev_push(const char *fmt, ...)
{
    char line[MWL_EV_LEN];
    va_list ap;
    int next = (ev_tail + 1) % MWL_MAX_EVENTS;

    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);

    if (next == ev_head) {
        ev_head = (ev_head + 1) % MWL_MAX_EVENTS;   /* overflow: drop oldest */
        next = (ev_tail + 1) % MWL_MAX_EVENTS;
    }
    snprintf(events[ev_tail], MWL_EV_LEN, "%s", line);
    ev_tail = next;
}

static int ev_pop(char *buf, int len)
{
    if (ev_head == ev_tail || buf == NULL || len <= 0)
        return 0;
    snprintf(buf, (size_t)len, "%s", events[ev_head]);
    ev_head = (ev_head + 1) % MWL_MAX_EVENTS;
    return 1;
}

static struct mwl_surface *surf_get(int sid)
{
    if (sid < 0 || sid >= MWL_MAX_SURFACES || !surfaces[sid].used)
        return NULL;
    return &surfaces[sid];
}

static int surf_alloc(void)
{
    int i;
    for (i = 0; i < MWL_MAX_SURFACES; i++)
        if (!surfaces[i].used)
            return i;
    return -1;
}

static struct mwl_surface *surf_of_wl(struct wl_surface *wl)
{
    int i;
    for (i = 0; i < MWL_MAX_SURFACES; i++)
        if (surfaces[i].used && surfaces[i].wl == wl)
            return &surfaces[i];
    return NULL;
}

static int sid_of(const struct mwl_surface *s)
{
    return (int)(s - surfaces);
}

/* forward declarations: listeners are defined next to their protocol below */
static const struct wl_registry_listener registry_listener;
static const struct wl_output_listener output_listener;
static const struct zxdg_output_v1_listener xdg_output_listener;
static const struct wl_seat_listener seat_listener;
static const struct wl_keyboard_listener keyboard_listener;
static const struct zwlr_layer_surface_v1_listener layer_surface_listener;
static const struct wl_callback_listener frame_listener;
/* wayland-client 1.23 passes the offending surface pointer, not an object id. */
static void display_error(void *data, struct wl_display *d, void *surface,
                          uint32_t code, const char *msg);

/* ------------------------------------------------------- display listener -- */

static void display_error(void *data, struct wl_display *d, void *surface,
                          uint32_t code, const char *msg)
{
    (void)data; (void)d; (void)surface;
    set_error("wl_display error code=%u: %s", code, msg ? msg : "(none)");
    ev_push("display_error %u %s", code, msg ? msg : "none");
}

static void display_delete_id(void *data, struct wl_display *d, uint32_t id)
{
    (void)data; (void)d; (void)id;
}

static const struct wl_display_listener display_listener = {
    display_error, display_delete_id,
};

/* ------------------------------------------------------------ wl_output -- */

static void output_geometry(void *data, struct wl_output *o, int32_t x, int32_t y,
                            int32_t pw, int32_t ph, int32_t subpixel,
                            const char *make, const char *model, int32_t transform)
{
    struct mwl_output *out = data;
    (void)o; (void)pw; (void)ph; (void)subpixel; (void)transform;
    out->x = x;
    out->y = y;
    if (!out->have_xdg_name)
        snprintf(out->name, sizeof out->name, "%s %s", make ? make : "", model ? model : "");
}

static void output_mode(void *data, struct wl_output *o, uint32_t flags,
                        int32_t width, int32_t height, int32_t refresh)
{
    struct mwl_output *out = data;
    (void)o; (void)flags; (void)refresh;
    if (!out->have_xdg_geom) {
        /* Mode size is in physical pixels; normalised against scale in mwl_init
         * when no xdg_output logical size was provided. */
        out->w = width;
        out->h = height;
        out->have_wl_mode = 1;
    }
}

static void output_done(void *data, struct wl_output *o)
{
    (void)data; (void)o;
}

static void output_scale(void *data, struct wl_output *o, int32_t factor)
{
    struct mwl_output *out = data;
    (void)o;
    out->scale = factor > 0 ? factor : 1;
    out->have_wl_scale = 1;
}

/* wl_output.name is v4; we bind at <=2, so this stays unused. It exists because
 * wayland-client 1.23's listener struct has the field and -Wall flags the gap.
 * xdg_output's name is the one we actually report. */
static void output_name(void *data, struct wl_output *o, const char *name)
{
    (void)data; (void)o; (void)name;
}

static void output_description(void *data, struct wl_output *o, const char *desc)
{
    (void)data; (void)o; (void)desc;
}

static const struct wl_output_listener output_listener = {
    output_geometry, output_mode, output_done, output_scale, output_name,
    output_description,
};

/* --------------------------------------------------------- zxdg_output_v1 -- */

static void xdg_logical_position(void *data, struct zxdg_output_v1 *x,
                                 int32_t xpos, int32_t ypos)
{
    struct mwl_output *out = data;
    (void)x;
    out->x = xpos;
    out->y = ypos;
}

static void xdg_logical_size(void *data, struct zxdg_output_v1 *x,
                             int32_t width, int32_t height)
{
    struct mwl_output *out = data;
    (void)x;
    out->w = width;
    out->h = height;
    out->have_xdg_geom = 1;
}

static void xdg_done(void *data, struct zxdg_output_v1 *x)
{
    struct mwl_output *out = data;
    (void)x;
    out->xdg_done = 1;
}

static void xdg_name(void *data, struct zxdg_output_v1 *x, const char *name)
{
    struct mwl_output *out = data;
    (void)x;
    if (name && *name) {
        snprintf(out->name, sizeof out->name, "%s", name);
        out->have_xdg_name = 1;
    }
}

static void xdg_description(void *data, struct zxdg_output_v1 *x, const char *desc)
{
    (void)data; (void)x; (void)desc;
}

static const struct zxdg_output_v1_listener xdg_output_listener = {
    xdg_logical_position, xdg_logical_size, xdg_done, xdg_name, xdg_description,
};

/* ---------------------------------------------------------- wl_keyboard -- */

static void kb_keymap(void *data, struct wl_keyboard *k, uint32_t format,
                      int32_t fd, uint32_t size)
{
    (void)data; (void)k; (void)format; (void)size;
    close(fd);            /* raw evdev codes suffice; no xkb keymap needed */
}

static void kb_enter(void *data, struct wl_keyboard *k, uint32_t serial,
                     struct wl_surface *surface, struct wl_array *keys)
{
    struct mwl_surface *s;
    (void)data; (void)k; (void)serial; (void)keys;
    s = surf_of_wl(surface);
    if (s)
        ev_push("kb_enter %d", sid_of(s));
}

static void kb_leave(void *data, struct wl_keyboard *k, uint32_t serial,
                     struct wl_surface *surface)
{
    struct mwl_surface *s;
    (void)data; (void)k; (void)serial;
    s = surf_of_wl(surface);
    if (s)
        ev_push("kb_leave %d", sid_of(s));
}

static void kb_key(void *data, struct wl_keyboard *k, uint32_t serial,
                   uint32_t time, uint32_t key, uint32_t state)
{
    (void)data; (void)k; (void)serial; (void)time;
    if (state == WL_KEYBOARD_KEY_STATE_PRESSED)
        ev_push("key %u", key + 8);   /* evdev -> XKB keycode (Escape = 9) */
}

static void kb_modifiers(void *data, struct wl_keyboard *k, uint32_t serial,
                         uint32_t depressed, uint32_t latched, uint32_t locked,
                         uint32_t group)
{
    (void)data; (void)k; (void)serial;
    ev_push("mods %u %u %u %u", depressed, latched, locked, group);
}

static void kb_repeat_info(void *data, struct wl_keyboard *k, int32_t rate,
                           int32_t delay)
{
    (void)data; (void)k; (void)rate; (void)delay;
}

static const struct wl_keyboard_listener keyboard_listener = {
    kb_keymap, kb_enter, kb_leave, kb_key, kb_modifiers, kb_repeat_info,
};

/* ------------------------------------------------------------- pointer -- */
/* Only enter/leave/button matter: pointer_click_action needs a press, and the
 * press must be attributed to the surface under the cursor. Designated
 * initialisers so a newer wl_pointer_listener (frame/axis_*) still compiles. */

static void ptr_enter(void *data, struct wl_pointer *p, uint32_t serial,
                      struct wl_surface *surface, wl_fixed_t sx, wl_fixed_t sy)
{
    struct mwl_surface *s;
    (void)data; (void)p; (void)serial; (void)sx; (void)sy;
    s = surf_of_wl(surface);
    pointer_sid = s ? sid_of(s) : -1;
}

static void ptr_leave(void *data, struct wl_pointer *p, uint32_t serial,
                      struct wl_surface *surface)
{
    (void)data; (void)p; (void)serial; (void)surface;
    pointer_sid = -1;
}

static void ptr_motion(void *data, struct wl_pointer *p, uint32_t time,
                       wl_fixed_t sx, wl_fixed_t sy)
{
    (void)data; (void)p; (void)time; (void)sx; (void)sy;
}

static void ptr_button(void *data, struct wl_pointer *p, uint32_t serial,
                       uint32_t time, uint32_t button, uint32_t state)
{
    (void)data; (void)p; (void)serial; (void)time;
    if (state == WL_POINTER_BUTTON_STATE_PRESSED && pointer_sid >= 0)
        ev_push("button %d %u", pointer_sid, button);
}

static const struct wl_pointer_listener pointer_listener = {
    .enter = ptr_enter,
    .leave = ptr_leave,
    .motion = ptr_motion,
    .button = ptr_button,
};

/* --------------------------------------------------------------- wl_seat -- */

static void seat_capabilities(void *data, struct wl_seat *s, uint32_t caps)
{
    (void)data;
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && keyboard == NULL) {
        keyboard = wl_seat_get_keyboard(s);
        if (keyboard)
            wl_keyboard_add_listener(keyboard, &keyboard_listener, NULL);
    }
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && pointer == NULL) {
        pointer = wl_seat_get_pointer(s);
        if (pointer)
            wl_pointer_add_listener(pointer, &pointer_listener, NULL);
    }
}

static void seat_name(void *data, struct wl_seat *s, const char *name)
{
    (void)data; (void)s; (void)name;
}

static const struct wl_seat_listener seat_listener = {
    seat_capabilities, seat_name,
};

/* ------------------------------------------------------------- registry -- */

static void reg_global(void *data, struct wl_registry *r, uint32_t name,
                       const char *iface, uint32_t version)
{
    int i;
    (void)data;
    /* Guard against binding one registry name twice: libwayland rejects a second
     * listener on the same proxy ("proxy already has listener") and the first
     * binding would leak. Seen once during bring-up; this makes the client
     * idempotent however the compositor advertises its globals. */
    for (i = 0; i < n_bound; i++) {
        if (bound_names[i] == name)
            return;
    }
    if (n_bound < (int)(sizeof bound_names / sizeof bound_names[0]))
        bound_names[n_bound++] = name;

    if (strcmp(iface, wl_compositor_interface.name) == 0) {
        compositor_version = version < 4 ? version : 4;
        compositor = wl_registry_bind(r, name, &wl_compositor_interface,
                                      compositor_version);
    } else if (strcmp(iface, wl_seat_interface.name) == 0) {
        seat_version = version < 5 ? version : 5;
        seat = wl_registry_bind(r, name, &wl_seat_interface, seat_version);
        if (seat)
            wl_seat_add_listener(seat, &seat_listener, NULL);
    } else if (strcmp(iface, wl_output_interface.name) == 0) {
        struct mwl_output *o;
        if (n_outputs >= MWL_MAX_OUTPUTS)
            return;
        o = &outputs[n_outputs++];
        memset(o, 0, sizeof *o);
        o->id = name;
        o->scale = 1;
        snprintf(o->name, sizeof o->name, "output-%u", name);
        o->wl = wl_registry_bind(r, name, &wl_output_interface,
                                 version < 2 ? version : 2);
        if (o->wl)
            wl_output_add_listener(o->wl, &output_listener, o);
    } else if (strcmp(iface, zwlr_layer_shell_v1_interface.name) == 0) {
        layer_shell_version = version < 4 ? version : 4;
        layer_shell = wl_registry_bind(r, name, &zwlr_layer_shell_v1_interface,
                                       layer_shell_version);
    } else if (strcmp(iface, zxdg_output_manager_v1_interface.name) == 0) {
        xdg_mgr = wl_registry_bind(r, name, &zxdg_output_manager_v1_interface,
                                   version < 3 ? version : 3);
    }
}

static void reg_global_remove(void *data, struct wl_registry *r, uint32_t name)
{
    int i;
    (void)data; (void)r;
    for (i = 0; i < n_outputs; i++) {
        if (outputs[i].id != name)
            continue;
        ev_push("output_removed %u", name);
        /* Surfaces on a removed output get 'closed' from the compositor; we keep
         * the slot so ids stay stable for the driver. */
        outputs[i].wl = NULL;
        outputs[i].xdg = NULL;
        return;
    }
}

static const struct wl_registry_listener registry_listener = {
    reg_global, reg_global_remove,
};

/* ------------------------------------------------------------------ EGL -- */

#ifndef EGL_PLATFORM_WAYLAND_KHR
#define EGL_PLATFORM_WAYLAND_KHR 0x31D8
#endif

static int egl_init(void)
{
    static const EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_NONE
    };
    EGLint maj = 0, min = 0, n = 0;

#ifdef EGL_PLATFORM_WAYLAND_KHR
    egldpy = eglGetPlatformDisplay(EGL_PLATFORM_WAYLAND_KHR, (void *)dpy, NULL);
#endif
    if (egldpy == EGL_NO_DISPLAY)
        egldpy = eglGetDisplay((EGLNativeDisplayType)dpy);
    if (egldpy == EGL_NO_DISPLAY) {
        set_error("eglGetDisplay failed: 0x%x", eglGetError());
        return -1;
    }
    if (!eglInitialize(egldpy, &maj, &min)) {
        set_error("eglInitialize failed: 0x%x (EGL vendor pin may not match the "
                  "render node; see [render] render_node)", eglGetError());
        return -1;
    }
    if (!eglChooseConfig(egldpy, cfg_attribs, &eglcfg, 1, &n) || n < 1) {
        set_error("eglChooseConfig found no RGBA8 ES2 window config: 0x%x",
                  eglGetError());
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------ lifecycle -- */

int mwl_init(void)
{
    int i;

    errbuf[0] = '\0';
    dpy = wl_display_connect(NULL);
    if (!dpy) {
        set_error("wl_display_connect failed (WAYLAND_DISPLAY=%s)",
                  getenv("WAYLAND_DISPLAY") ? getenv("WAYLAND_DISPLAY") : "unset");
        return -1;
    }
    wl_display_add_listener(dpy, &display_listener, NULL);
    reg = wl_display_get_registry(dpy);
    if (!reg) {
        set_error("wl_display_get_registry returned NULL");
        return -1;
    }
    wl_registry_add_listener(reg, &registry_listener, NULL);
    if (wl_display_roundtrip(dpy) < 0) {
        set_error("registry roundtrip failed: %s", strerror(errno));
        return -1;
    }
    if (!compositor) { set_error("compositor did not offer wl_compositor"); return -1; }
    if (!layer_shell) {
        set_error("compositor did not offer zwlr_layer_shell_v1 - not a wlroots-style "
                  "layer-shell compositor");
        return -1;
    }
    if (n_outputs == 0) { set_error("compositor advertised no wl_output"); return -1; }

    if (xdg_mgr) {
        for (i = 0; i < n_outputs; i++) {
            outputs[i].xdg = zxdg_output_manager_v1_get_xdg_output(xdg_mgr,
                                                                  outputs[i].wl);
            if (outputs[i].xdg)
                zxdg_output_v1_add_listener(outputs[i].xdg, &xdg_output_listener,
                                            &outputs[i]);
        }
        wl_display_roundtrip(dpy);
        wl_display_roundtrip(dpy);   /* name/description can follow logical_size */
    }

    for (i = 0; i < n_outputs; i++) {
        struct mwl_output *o = &outputs[i];
        if (o->scale <= 0)
            o->scale = 1;
        if (!o->have_xdg_geom && o->have_wl_mode) {
            o->w /= o->scale;        /* mode is physical; logical = physical/scale */
            o->h /= o->scale;
        }
        if (o->w <= 0 || o->h <= 0) {
            set_error("output %u reported unusable geometry %dx%d", o->id, o->w, o->h);
            return -1;
        }
    }
    return egl_init();
}

const char *mwl_error(void)
{
    return errbuf[0] ? errbuf : "ok";
}

int mwl_output_count(void)
{
    int i, live = 0;
    for (i = 0; i < n_outputs; i++)
        if (outputs[i].wl)
            live++;
    return live;
}

int mwl_output_info(int idx, int *x, int *y, int *w, int *h, int *scale,
                    char *name, int namelen)
{
    int i, seen = -1;
    struct mwl_output *o = NULL;

    for (i = 0; i < n_outputs; i++) {
        if (!outputs[i].wl)
            continue;
        if (++seen == idx) { o = &outputs[i]; break; }
    }
    if (!o)
        return -1;
    if (x) *x = o->x;
    if (y) *y = o->y;
    if (w) *w = o->w;
    if (h) *h = o->h;
    if (scale) *scale = o->scale;
    if (name && namelen > 0)
        snprintf(name, (size_t)namelen, "%s", o->name);
    return 0;
}

int mwl_versions(char *buf, int len)
{
    if (!buf || len <= 0)
        return -1;
    snprintf(buf, (size_t)len,
             "layer_shell=%u compositor=%u seat=%u xdg_output=%s outputs=%d",
             layer_shell_version, compositor_version, seat_version,
             xdg_mgr ? "yes" : "no", n_outputs);
    return 0;
}

static struct mwl_output *output_by_index(int idx)
{
    int i, seen = -1;

    for (i = 0; i < n_outputs; i++) {
        if (!outputs[i].wl)
            continue;
        if (++seen == idx)
            return &outputs[i];
    }
    return NULL;
}

/* --------------------------------------------------- layer surface events -- */

static void frame_done(void *data, struct wl_callback *cb, uint32_t time)
{
    struct mwl_surface *s = data;
    (void)time;
    s->frame_ready = 1;
    if (cb) {
        wl_callback_destroy(cb);
        if (s->frame_cb == cb)
            s->frame_cb = NULL;
    }
}

static const struct wl_callback_listener frame_listener = { frame_done };

static void surf_resize_to_logical(struct mwl_surface *s)
{
    int scale = s->out ? s->out->scale : 1;

    if (scale <= 0)
        scale = 1;
    s->width = s->logical_w * scale;
    s->height = s->logical_h * scale;
    if (s->eglwin)
        wl_egl_window_resize(s->eglwin, s->width, s->height, 0, 0);
    if (s->wl)
        wl_surface_set_buffer_scale(s->wl, scale);
}

static void ls_configure(void *data, struct zwlr_layer_surface_v1 *l,
                         uint32_t serial, uint32_t w, uint32_t h)
{
    struct mwl_surface *s = data;

    zwlr_layer_surface_v1_ack_configure(l, serial);
    s->configured = 1;
    if (w > 0 && h > 0) {
        s->logical_w = (int)w;
        s->logical_h = (int)h;
    }
    surf_resize_to_logical(s);
    ev_push("configure %d %u %u", sid_of(s), w, h);
}

static void ls_closed(void *data, struct zwlr_layer_surface_v1 *l)
{
    struct mwl_surface *s = data;
    (void)l;
    s->closed = 1;
    ev_push("closed %d", sid_of(s));
}

static const struct zwlr_layer_surface_v1_listener layer_surface_listener = {
    ls_configure, ls_closed,
};

/* -------------------------------------------------------------- GL setup -- */

static const char *VS_SRC =
    "attribute vec2 a_pos;\n"
    "attribute vec2 a_uv;\n"
    "attribute vec4 a_color;\n"
    "uniform vec2 u_resolution;\n"
    "varying vec2 v_uv;\n"
    "varying vec4 v_color;\n"
    "void main() {\n"
    "  vec2 ndc = (a_pos / u_resolution) * 2.0 - 1.0;\n"
    "  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);\n"   /* device px, y down */
    "  v_uv = a_uv;\n"
    "  v_color = a_color;\n"
    "}\n";

static const char *FS_SRC =
    "precision mediump float;\n"
    "varying vec2 v_uv;\n"
    "varying vec4 v_color;\n"
    "uniform sampler2D u_tex;\n"
    "void main() {\n"
    "  gl_FragColor = texture2D(u_tex, v_uv) * v_color;\n"  /* both premultiplied */
    "}\n";

static GLuint compile_shader(GLenum type, const char *src)
{
    GLuint sh = glCreateShader(type);
    GLint ok = 0;

    if (!sh) {
        set_error("glCreateShader returned 0");
        return 0;
    }
    glShaderSource(sh, 1, &src, NULL);
    glCompileShader(sh);
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024];
        GLsizei n = 0;
        glGetShaderInfoLog(sh, sizeof log, &n, log);
        set_error("shader compile failed (%s): %s", type == GL_VERTEX_SHADER ? "vs" : "fs",
                  log);
        glDeleteShader(sh);
        return 0;
    }
    return sh;
}

static int gl_init(void)
{
    GLuint vs, fs;
    GLint ok = 0;

    vs = compile_shader(GL_VERTEX_SHADER, VS_SRC);
    fs = vs ? compile_shader(GL_FRAGMENT_SHADER, FS_SRC) : 0;
    if (!vs || !fs) {
        if (vs) glDeleteShader(vs);
        if (fs) glDeleteShader(fs);
        return -1;
    }
    program = glCreateProgram();
    glAttachShader(program, vs);
    glAttachShader(program, fs);
    glBindAttribLocation(program, 0, "a_pos");
    glBindAttribLocation(program, 1, "a_uv");
    glBindAttribLocation(program, 2, "a_color");
    glLinkProgram(program);
    glGetProgramiv(program, GL_LINK_STATUS, &ok);
    glDeleteShader(vs);
    glDeleteShader(fs);
    if (!ok) {
        char log[1024];
        GLsizei n = 0;
        glGetProgramInfoLog(program, sizeof log, &n, log);
        set_error("program link failed: %s", log);
        glDeleteProgram(program);
        program = 0;
        return -1;
    }
    loc_pos = 0;
    loc_uv = 1;
    loc_color = 2;
    loc_res = glGetUniformLocation(program, "u_resolution");
    loc_tex = glGetUniformLocation(program, "u_tex");
    glGenBuffers(1, &vbo);

    /* Non-blocking swaps: we pace with wl_surface frame callbacks plus the
     * driver's own timer. Leaving the interval at 1 would block inside
     * eglSwapBuffers until vblank, stalling the Python loop and adding latency
     * to the Escape keypress that tears the overlay down. */
    eglSwapInterval(egldpy, 0);

    glUseProgram(program);
    glUniform1i(loc_tex, 0);
    glEnable(GL_BLEND);
    glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);   /* premultiplied alpha */
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_SCISSOR_TEST);
    gl_ready = 1;
    return 0;
}

/* ------------------------------------------------------------- surfaces -- */

int mwl_surface_create(int output_idx, const char *ns, int kb_exclusive,
                       int exclusive_zone)
{
    struct mwl_output *o = output_by_index(output_idx);
    struct mwl_surface *s;
    int sid, guard = 0;

    if (!o) {
        set_error("mwl_surface_create: output index %d out of range (%d live)",
                  output_idx, mwl_output_count());
        return -1;
    }
    sid = surf_alloc();
    if (sid < 0) {
        set_error("mwl_surface_create: no free surface slot (max %d)", MWL_MAX_SURFACES);
        return -1;
    }
    s = &surfaces[sid];
    memset(s, 0, sizeof *s);
    s->used = 1;
    s->output_idx = output_idx;
    s->out = o;
    s->egl = EGL_NO_SURFACE;
    s->logical_w = o->w;
    s->logical_h = o->h;

    s->wl = wl_compositor_create_surface(compositor);
    if (!s->wl) {
        set_error("wl_compositor_create_surface failed");
        s->used = 0;
        return -1;
    }
    s->layer = zwlr_layer_shell_v1_get_layer_surface(
        layer_shell, s->wl, o->wl, ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY,
        (ns && *ns) ? ns : "matrix-overlay");
    if (!s->layer) {
        set_error("zwlr_layer_shell_v1_get_layer_surface failed");
        wl_surface_destroy(s->wl);
        s->used = 0;
        return -1;
    }
    zwlr_layer_surface_v1_add_listener(s->layer, &layer_surface_listener, s);
    /* All four edges anchored with size 0,0 asks the compositor for the full
     * output; exclusive_zone 0 means we do not reserve space (we cover it). */
    zwlr_layer_surface_v1_set_anchor(s->layer,
        ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM |
        ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    zwlr_layer_surface_v1_set_size(s->layer, 0, 0);
    /* 0 = reserve no space; -1 = ignore other surfaces' exclusive zones, which is
     * what lets the overlay cover the COSMIC panel strip (measured: with 0 the
     * compositor configured us at 2560x1408 instead of the full 2560x1440). */
    zwlr_layer_surface_v1_set_exclusive_zone(s->layer, exclusive_zone);
    zwlr_layer_surface_v1_set_margin(s->layer, 0, 0, 0, 0);
    zwlr_layer_surface_v1_set_keyboard_interactivity(s->layer, kb_exclusive
        ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE
        : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
    wl_surface_commit(s->wl);

    while (!s->configured && !s->closed && guard++ < 64) {
        if (wl_display_dispatch(dpy) < 0) {
            set_error("dispatch failed while awaiting configure: %s", strerror(errno));
            break;
        }
    }
    if (s->closed || !s->configured) {
        set_error("layer surface %d never configured (closed=%d guard=%d) - compositor "
                  "rejected the overlay surface", sid, s->closed, guard);
        mwl_surface_destroy(sid);
        return -1;
    }
    surf_resize_to_logical(s);
    s->eglwin = wl_egl_window_create(s->wl, s->width, s->height);
    if (!s->eglwin) {
        set_error("wl_egl_window_create failed for %dx%d", s->width, s->height);
        mwl_surface_destroy(sid);
        return -1;
    }
    s->egl = eglCreateWindowSurface(egldpy, eglcfg, (EGLNativeWindowType)s->eglwin, NULL);
    if (s->egl == EGL_NO_SURFACE) {
        set_error("eglCreateWindowSurface failed: 0x%x", eglGetError());
        mwl_surface_destroy(sid);
        return -1;
    }
    if (mwl_surface_make_current(sid) < 0) {
        mwl_surface_destroy(sid);
        return -1;
    }
    return sid;
}

int mwl_surface_make_current(int sid)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s) {
        set_error("make_current: bad surface id %d", sid);
        return -1;
    }
    if (eglctx == EGL_NO_CONTEXT) {
        static const EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
        eglctx = eglCreateContext(egldpy, eglcfg, EGL_NO_CONTEXT, ctx_attribs);
        if (eglctx == EGL_NO_CONTEXT) {
            set_error("eglCreateContext(ES2) failed: 0x%x", eglGetError());
            return -1;
        }
    }
    if (!eglMakeCurrent(egldpy, s->egl, s->egl, eglctx)) {
        set_error("eglMakeCurrent failed: 0x%x", eglGetError());
        return -1;
    }
    if (!gl_ready)
        return gl_init();
    return 0;
}

int mwl_surface_size(int sid, int *w, int *h)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s)
        return -1;
    if (w) *w = s->width;
    if (h) *h = s->height;
    return 0;
}

int mwl_surface_scale(int sid)
{
    struct mwl_surface *s = surf_get(sid);

    return (s && s->out) ? s->out->scale : -1;
}

int mwl_surface_geom(int sid, int *x, int *y)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s || !s->out)
        return -1;
    if (x) *x = s->out->x;
    if (y) *y = s->out->y;
    return 0;
}

int mwl_surface_frame_ready(int sid)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s)
        return 0;
    if (s->frame_ready) {
        s->frame_ready = 0;
        return 1;
    }
    return 0;
}

int mwl_surface_swap(int sid)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s)
        return -1;
    /* Attach the frame callback before the commit that eglSwapBuffers performs,
     * so the compositor's pacing signal is delivered for this frame. */
    if (!s->frame_cb && s->wl) {
        s->frame_cb = wl_surface_frame(s->wl);
        if (s->frame_cb)
            wl_callback_add_listener(s->frame_cb, &frame_listener, s);
    }
    if (!eglSwapBuffers(egldpy, s->egl)) {
        set_error("eglSwapBuffers failed: 0x%x", eglGetError());
        return -1;
    }
    wl_display_flush(dpy);
    return 0;
}

void mwl_surface_destroy(int sid)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s)
        return;
    if (s->frame_cb) {
        wl_callback_destroy(s->frame_cb);
        s->frame_cb = NULL;
    }
    if (s->egl != EGL_NO_SURFACE) {
        eglMakeCurrent(egldpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        eglDestroySurface(egldpy, s->egl);
        s->egl = EGL_NO_SURFACE;
    }
    if (s->eglwin) {
        wl_egl_window_destroy(s->eglwin);
        s->eglwin = NULL;
    }
    if (s->layer) {
        zwlr_layer_surface_v1_destroy(s->layer);
        s->layer = NULL;
    }
    free(s->verts);
    s->verts = NULL;
    s->verts_cap = 0;
    if (s->wl) {
        wl_surface_destroy(s->wl);
        s->wl = NULL;
    }
    s->used = 0;
    s->out = NULL;
}

int mwl_gl_info(char *buf, int len)
{
    const char *v, *r, *g, *ev;

    if (!buf || len <= 0 || !gl_ready)
        return -1;
    v = (const char *)glGetString(GL_VERSION);
    r = (const char *)glGetString(GL_RENDERER);
    g = (const char *)glGetString(GL_VENDOR);
    ev = eglQueryString(egldpy, EGL_VENDOR);
    snprintf(buf, (size_t)len, "GL_VERSION=%s | GL_RENDERER=%s | GL_VENDOR=%s | "
             "EGL_VENDOR=%s", v ? v : "?", r ? r : "?", g ? g : "?", ev ? ev : "?");
    return 0;
}

/* ------------------------------------------------------------- textures -- */

unsigned int mwl_tex_create(int w, int h, const unsigned char *rgba)
{
    GLuint tex = 0;

    if (!gl_ready || w <= 0 || h <= 0 || !rgba) {
        set_error("tex_create: gl_ready=%d w=%d h=%d rgba=%p", gl_ready, w, h,
                  (const void *)rgba);
        return 0;
    }
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, (GLsizei)w, (GLsizei)h, 0, GL_RGBA,
                 GL_UNSIGNED_BYTE, rgba);
    glBindTexture(GL_TEXTURE_2D, 0);
    return (unsigned int)tex;
}

int mwl_tex_update(unsigned int tex, int x, int y, int w, int h,
                   const unsigned char *rgba)
{
    if (!gl_ready || !tex || !rgba || w <= 0 || h <= 0)
        return -1;
    glBindTexture(GL_TEXTURE_2D, (GLuint)tex);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexSubImage2D(GL_TEXTURE_2D, 0, (GLint)x, (GLint)y, (GLsizei)w, (GLsizei)h,
                    GL_RGBA, GL_UNSIGNED_BYTE, rgba);
    glBindTexture(GL_TEXTURE_2D, 0);
    return 0;
}

void mwl_tex_destroy(unsigned int tex)
{
    GLuint t = (GLuint)tex;

    if (gl_ready && t)
        glDeleteTextures(1, &t);
}

/* ----------------------------------------------------------------- draw -- */

int mwl_clear(int sid, float r, float g, float b, float a)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s || !gl_ready)
        return -1;
    glViewport(0, 0, s->width, s->height);
    /* Premultiplied clear: the compositor blends our buffer as premultiplied. */
    glClearColor(r * a, g * a, b * a, a);
    glClear(GL_COLOR_BUFFER_BIT);
    return 0;
}

/* One submission: viewport, uniforms, orphan + refill of the streaming VBO, draw.
 * Shared by the explicit-vertex path and the native cell expander. */
static int gl_submit(struct mwl_surface *s, unsigned int tex, const float *verts,
                     int nverts, int blend)
{
    const GLsizei stride = (GLsizei)(MWL_FLOATS_PER_VERT * sizeof(float));
    GLenum err;

    if (nverts <= 0)
        return 0;
    glViewport(0, 0, s->width, s->height);
    glUseProgram(program);
    glUniform2f(loc_res, (GLfloat)s->width, (GLfloat)s->height);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, (GLuint)tex);
    glUniform1i(loc_tex, 0);
    /* Set per call: the head glow is additive, glyph bodies are premultiplied
     * over, and the two batches interleave within one frame. */
    if (blend == MWL_BLEND_ADD)
        glBlendFunc(GL_ONE, GL_ONE);
    else
        glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);

    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    /* Orphan + refill: one streaming buffer, no client-side arrays in ES2. */
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)nverts * stride, verts, GL_STREAM_DRAW);
    glEnableVertexAttribArray(loc_pos);
    glVertexAttribPointer(loc_pos, 2, GL_FLOAT, GL_FALSE, stride, (const void *)0);
    glEnableVertexAttribArray(loc_uv);
    glVertexAttribPointer(loc_uv, 2, GL_FLOAT, GL_FALSE, stride,
                          (const void *)(2 * sizeof(float)));
    glEnableVertexAttribArray(loc_color);
    glVertexAttribPointer(loc_color, 4, GL_FLOAT, GL_FALSE, stride,
                          (const void *)(4 * sizeof(float)));
    glDrawArrays(GL_TRIANGLES, 0, (GLsizei)nverts);
    glBindBuffer(GL_ARRAY_BUFFER, 0);

    err = glGetError();
    if (err != GL_NO_ERROR) {
        ev_push("gl_error %u", (unsigned)err);
        set_error("GL error 0x%x after draw of %d verts", (unsigned)err, nverts);
        return -1;
    }
    return 0;
}

int mwl_draw_quads(int sid, unsigned int tex, const float *verts, int nverts,
                   int blend)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s || !gl_ready || !verts || nverts <= 0)
        return -1;
    return gl_submit(s, tex, verts, nverts, blend);
}

/* -------------------------------------------------- native vertex builder -- */

static void put_vert(float *dst, float x, float y, float u, float v)
{
    dst[0] = x;
    dst[1] = y;
    dst[2] = u;
    dst[3] = v;
    /* Colour stays opaque white: the tint and brightness are already baked into
     * the atlas cell, so a per-vertex multiplier would cost bandwidth for nothing. */
    dst[4] = 1.0f;
    dst[5] = 1.0f;
    dst[6] = 1.0f;
    dst[7] = 1.0f;
}

static int verts_reserve(struct mwl_surface *s, int n_verts)
{
    int cap;
    float *p;

    if (n_verts <= s->verts_cap)
        return 0;
    cap = s->verts_cap > 0 ? s->verts_cap : 4096;
    while (cap < n_verts)
        cap *= 2;
    p = realloc(s->verts, (size_t)cap * MWL_FLOATS_PER_VERT * sizeof(float));
    if (!p) {
        set_error("out of memory expanding %d vertices", n_verts);
        return -1;
    }
    s->verts = p;
    s->verts_cap = cap;
    return 0;
}

int mwl_set_geom(int sid, int col0, int row0, int x_px, int y_px,
                 int cell_logical, int scale, int uv_stride)
{
    struct mwl_surface *s = surf_get(sid);

    if (!s)
        return -1;
    s->col0 = col0;
    s->row0 = row0;
    s->x_px = x_px;
    s->y_px = y_px;
    s->cell_logical = cell_logical > 0 ? cell_logical : 1;
    s->px_scale = scale > 0 ? scale : 1;
    s->uv_stride = uv_stride > 0 ? uv_stride : 1;
    return 0;
}

/* Cells arrive as five int32 each (col, row, glyph, level, head) and are expanded
 * here. Doing this in Python measured ~7 ms per surface per frame at ~1000 cells,
 * which dominated the whole GL frame once the pixels were on the GPU.
 *
 * mwl_expand_cells is deliberately pure - no GL, no surface state - so tests can
 * call it over the same library and compare it vertex-for-vertex against the
 * Python packer, which is what proves the two paths still agree. */
int mwl_expand_cells(float *out, int out_cap_verts,
                     int col0, int row0, int x_px, int y_px,
                     int cell_logical, int scale, int uv_stride,
                     const float *uv, int uv_count,
                     const int *cells, int n_cells, float pad, int heads_only)
{
    float cell_dev, size, u0, v0, u1, v1, x, y, x1, y1;
    int i, w = 0;

    if (!out || !uv || !cells || n_cells < 0 || out_cap_verts < 0)
        return -1;
    if (cell_logical <= 0)
        cell_logical = 1;
    if (scale <= 0)
        scale = 1;
    if (uv_stride <= 0)
        uv_stride = 1;
    cell_dev = (float)(cell_logical * scale);
    size = cell_dev + 2.0f * pad;
    for (i = 0; i < n_cells; i++) {
        const int *c = cells + i * 5;
        int slot = c[2] * uv_stride + c[3];
        const float *u;

        if (c[3] <= 0 || (heads_only && !c[4]))
            continue;                       /* invisible, or not a head */
        if (slot < 0 || (slot + 1) * 4 > uv_count)
            continue;                       /* stale glyph/level */
        if (w + 6 > out_cap_verts)
            break;
        u = uv + slot * 4;
        u0 = u[0]; v0 = u[1]; u1 = u[2]; v1 = u[3];
        x = (float)((col0 + c[0]) * cell_logical - x_px) * (float)scale - pad;
        y = (float)((row0 + c[1]) * cell_logical - y_px) * (float)scale - pad;
        x1 = x + size;
        y1 = y + size;
        put_vert(out + ((w + 0) * MWL_FLOATS_PER_VERT), x, y, u0, v0);
        put_vert(out + ((w + 1) * MWL_FLOATS_PER_VERT), x1, y, u1, v0);
        put_vert(out + ((w + 2) * MWL_FLOATS_PER_VERT), x1, y1, u1, v1);
        put_vert(out + ((w + 3) * MWL_FLOATS_PER_VERT), x, y, u0, v0);
        put_vert(out + ((w + 4) * MWL_FLOATS_PER_VERT), x1, y1, u1, v1);
        put_vert(out + ((w + 5) * MWL_FLOATS_PER_VERT), x, y1, u0, v1);
        w += 6;
    }
    return w;
}

int mwl_draw_cells(int sid, unsigned int tex, const float *uv, int uv_count,
                   const int *cells, int n_cells, int blend, float pad,
                   int heads_only)
{
    struct mwl_surface *s = surf_get(sid);
    int w;

    if (!s || !gl_ready || !uv || !cells || n_cells < 0)
        return -1;
    if (verts_reserve(s, (n_cells + 1) * 6) < 0)
        return -1;
    w = mwl_expand_cells(s->verts, s->verts_cap, s->col0, s->row0, s->x_px, s->y_px,
                         s->cell_logical, s->px_scale, s->uv_stride, uv, uv_count,
                         cells, n_cells, pad, heads_only);
    if (w <= 0)
        return w;                           /* 0 = nothing visible, not an error */
    return gl_submit(s, tex, s->verts, w, blend);
}

/* --------------------------------------------------------------- events -- */

int mwl_fd(void)
{
    return dpy ? wl_display_get_fd(dpy) : -1;
}

int mwl_next_event(char *buf, int len)
{
    return ev_pop(buf, len);
}

int mwl_display_error_code(void)
{
    return dpy ? (int)wl_display_get_error(dpy) : -1;
}

int mwl_dispatch(int timeout_ms)
{
    struct pollfd pfd;
    int ret;

    if (!dpy)
        return -1;
    wl_display_dispatch_pending(dpy);
    if (wl_display_get_error(dpy))
        return -1;
    if (timeout_ms <= 0) {
        wl_display_flush(dpy);
        return 0;
    }
    if (wl_display_flush(dpy) < 0 && errno != EAGAIN)
        return -1;
    /* prepare_read/read_events is the single-threaded blocking pattern: poll
     * sleeps here, which is why the process idles between frames instead of
     * burning a core the way the GDK paint path did. */
    if (wl_display_prepare_read(dpy) != 0) {
        wl_display_dispatch_pending(dpy);
        return 0;
    }
    pfd.fd = wl_display_get_fd(dpy);
    pfd.events = POLLIN;
    pfd.revents = 0;
    do {
        ret = poll(&pfd, 1, timeout_ms);
    } while (ret < 0 && errno == EINTR);
    if (ret > 0)
        wl_display_read_events(dpy);
    else
        wl_display_cancel_read(dpy);
    wl_display_dispatch_pending(dpy);
    return ret;
}

/* ------------------------------------------------------------ shutdown -- */

void mwl_shutdown(void)
{
    int i;

    for (i = 0; i < MWL_MAX_SURFACES; i++)
        if (surfaces[i].used)
            mwl_surface_destroy(i);
    if (gl_ready) {
        if (vbo) { glDeleteBuffers(1, &vbo); vbo = 0; }
        if (program) { glDeleteProgram(program); program = 0; }
        gl_ready = 0;
    }
    if (egldpy != EGL_NO_DISPLAY) {
        eglMakeCurrent(egldpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        if (eglctx != EGL_NO_CONTEXT) {
            eglDestroyContext(egldpy, eglctx);
            eglctx = EGL_NO_CONTEXT;
        }
        eglTerminate(egldpy);
        egldpy = EGL_NO_DISPLAY;
    }
    for (i = 0; i < n_outputs; i++) {
        if (outputs[i].xdg) {
            zxdg_output_v1_destroy(outputs[i].xdg);
            outputs[i].xdg = NULL;
        }
        if (outputs[i].wl) {
            wl_output_destroy(outputs[i].wl);
            outputs[i].wl = NULL;
        }
    }
    if (keyboard) { wl_keyboard_destroy(keyboard); keyboard = NULL; }
    if (pointer) { wl_pointer_destroy(pointer); pointer = NULL; }
    pointer_sid = -1;
    if (seat) { wl_seat_destroy(seat); seat = NULL; }
    if (layer_shell) {
        if (layer_shell_version >= 3)
            zwlr_layer_shell_v1_destroy(layer_shell);
        else
            wl_registry_destroy(reg);
        layer_shell = NULL;
    }
    if (xdg_mgr) { zxdg_output_manager_v1_destroy(xdg_mgr); xdg_mgr = NULL; }
    if (compositor) { wl_compositor_destroy(compositor); compositor = NULL; }
    if (reg) { wl_registry_destroy(reg); reg = NULL; }
    if (dpy) {
        wl_display_flush(dpy);
        wl_display_disconnect(dpy);
        dpy = NULL;
    }
    n_outputs = 0;
}
