/* mwl.h - C API of the raw Wayland + EGL/GLES2 presentation layer.
 *
 * Bound from Python by matrix_overlay/mwl.py via ctypes. All calls are
 * single-threaded and must happen on the thread that called mwl_init().
 *
 * Surface ids are small ints (0..15) indexing an internal table; -1 means
 * failure and mwl_error() explains why. Events (key presses, configure, closed,
 * frame pacing, GL errors) are queued as short strings and drained with
 * mwl_next_event(); mwl_dispatch() blocks in poll() so the process sleeps
 * between frames instead of spinning.
 */
#ifndef MWL_H
#define MWL_H

#ifdef __cplusplus
extern "C" {
#endif

/* ---- lifecycle ---- */
int mwl_init(void);
void mwl_shutdown(void);
const char *mwl_error(void);
int mwl_versions(char *buf, int len);
int mwl_gl_info(char *buf, int len);

/* ---- outputs ---- */
int mwl_output_count(void);
int mwl_output_info(int idx, int *x, int *y, int *w, int *h, int *scale,
                    char *name, int namelen);

/* ---- layer-shell overlay surfaces (one per output we cover) ---- */
int mwl_surface_create(int output_idx, const char *ns, int kb_exclusive,
                       int exclusive_zone);
int mwl_surface_make_current(int sid);
int mwl_surface_size(int sid, int *w, int *h);
int mwl_surface_scale(int sid);
int mwl_surface_geom(int sid, int *x, int *y);
int mwl_surface_frame_ready(int sid);
int mwl_surface_swap(int sid);
void mwl_surface_destroy(int sid);

/* ---- GL resources ---- */
unsigned int mwl_tex_create(int w, int h, const unsigned char *rgba);
int mwl_tex_update(unsigned int tex, int x, int y, int w, int h,
                   const unsigned char *rgba);
void mwl_tex_destroy(unsigned int tex);
int mwl_clear(int sid, float r, float g, float b, float a);
int mwl_draw_quads(int sid, unsigned int tex, const float *verts, int nverts,
                   int blend);

/* ---- native cell expander (keeps Python out of the vertex hot path) ----
 * Cells are five int32 each: col, row, glyph, level, head. The uv table holds
 * four floats per (glyph, level) slot, indexed glyph * uv_stride + level. */
int mwl_set_geom(int sid, int col0, int row0, int x_px, int y_px,
                 int cell_logical, int scale, int uv_stride);
int mwl_draw_cells(int sid, unsigned int tex, const float *uv, int uv_count,
                   const int *cells, int n_cells, int blend, float pad,
                   int heads_only);
/* Pure variant (no GL, no surface): exposed so tests can compare the native
 * expansion vertex-for-vertex against the Python packer. */
int mwl_expand_cells(float *out, int out_cap_verts,
                     int col0, int row0, int x_px, int y_px,
                     int cell_logical, int scale, int uv_stride,
                     const float *uv, int uv_count,
                     const int *cells, int n_cells, float pad, int heads_only);

/* ---- event pump ---- */
int mwl_fd(void);
int mwl_dispatch(int timeout_ms);
int mwl_next_event(char *buf, int len);
int mwl_display_error_code(void);

#ifdef __cplusplus
}
#endif

#endif /* MWL_H */
