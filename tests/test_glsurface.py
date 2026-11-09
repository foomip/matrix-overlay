"""M6-N3 RED: GL surface + runtime, against a recording fake handle.

The GL backend's contract is the *same* one app.py already consumes from
surface.OutputSurface, so these tests pin the behaviours the app relies on:
authoritative cell state, present ordering (dim clear -> additive glow -> glyph
bodies), dim updates, resync, close reporting, and event routing.

A fake Mwl handle is used instead of a compositor, exactly as the Cairo tests use
CrSpy: no display, no GL context, decisions still covered.
"""
from __future__ import annotations

import dataclasses

import pytest

from matrix_overlay.atlas import AtlasKey
from matrix_overlay.glpaint import AtlasLayout
from matrix_overlay.glsurface import GlRuntime, GlSurface, monitor_infos
from matrix_overlay.mwl import ESCAPE_KEYSYM, Event, Output
from matrix_overlay.world import (BUCKETS, Crop, Draw, Geom, OutputFrame, Pos,
                                  WorldCanvas)


@dataclasses.dataclass
class Call:
    name: str
    args: tuple


class FakeMwl:
    """Records every native call; returns canned geometry."""

    def __init__(self, outputs=None):
        self.calls: list[Call] = []
        self.outputs_list = outputs or [
            Output(0, "DP-4", 2560, 0, 2560, 1440, 2),
            Output(1, "DP-5", 0, 0, 2560, 1440, 1),
        ]
        self.next_sid = 0
        self.sizes: dict[int, tuple[int, int]] = {}
        self.sid_output: dict[int, int] = {}
        self.textures: list[tuple[int, int]] = []
        self._events: list[Event] = []
        self.closed: list[int] = []

    # lifecycle
    def init(self): self.calls.append(Call("init", ()))
    def shutdown(self): self.calls.append(Call("shutdown", ()))
    def versions(self): return "layer_shell=4"
    def gl_info(self): return "GL_RENDERER=Fake"
    def error(self): return "ok"

    # outputs / surfaces
    def outputs(self): return list(self.outputs_list)

    def create_surface(self, idx, *, namespace="matrix-overlay",
                       exclusive_keyboard=True, exclusive_zone=-1):
        sid = self.next_sid
        self.next_sid += 1
        out = self.outputs_list[idx]
        self.sid_output[sid] = idx
        self.sizes[sid] = (out.width * out.scale, out.height * out.scale)
        self.calls.append(Call("create_surface", (idx, namespace, exclusive_keyboard,
                                                 exclusive_zone)))
        return sid

    def surface_size(self, sid): return self.sizes[sid]

    def surface_scale(self, sid):
        return self.outputs_list[self.sid_output[sid]].scale

    def surface_geom(self, sid):
        o = self.outputs_list[self.sid_output[sid]]
        return (o.x, o.y)

    def make_current(self, sid): self.calls.append(Call("make_current", (sid,)))
    def clear(self, sid, colour): self.calls.append(Call("clear", (sid, tuple(colour))))

    def set_geom(self, sid, crop, scale, uv_stride):
        self.calls.append(Call("set_geom", (sid, getattr(crop, "col0", -1), scale,
                                            uv_stride)))

    def draw(self, sid, tex, batch, blend=0):
        self.calls.append(Call("draw", (sid, tex, batch.n_quads, blend)))

    def draw_cells(self, sid, tex, uv, batch, blend=0, pad=0.0, heads_only=False):
        self.calls.append(Call("draw_cells", (sid, tex, batch.n_cells, blend,
                                              heads_only, pad)))
        return batch.n_cells * 6

    def swap(self, sid): self.calls.append(Call("swap", (sid,)))
    def frame_ready(self, sid): return True

    def destroy_surface(self, sid):
        self.closed.append(sid)
        self.calls.append(Call("destroy_surface", (sid,)))

    # textures
    def tex_create(self, w, h, rgba):
        self.textures.append((w, h))
        self.calls.append(Call("tex_create", (w, h, len(rgba))))
        return 100 + len(self.textures)

    def tex_destroy(self, tex): self.calls.append(Call("tex_destroy", (tex,)))

    # events
    def dispatch(self, ms=0):
        self.calls.append(Call("dispatch", (ms,)))
        return 0

    def push(self, *events): self._events.extend(events)

    def events(self, limit=64):
        self.calls.append(Call("events", ()))
        out, self._events = self._events[:limit], self._events[limit:]
        return out

    def keycode_to_keysym(self, code): return ESCAPE_KEYSYM if code == 9 else code


class FakeAtlas:
    """Duck-typed GlyphAtlas: only what the texture packer touches."""

    def __init__(self, scale=1, cell_size=18, n_glyphs=6, glow_pad=0):
        self.n_glyphs = n_glyphs
        self.cell_px = max(4, round(cell_size * scale))
        self.glow_pad = glow_pad
        self.key = AtlasKey(font="monospace 12", cell_size=cell_size, scale=scale,
                            glow_enabled=bool(glow_pad))
        self.surface_count = n_glyphs * BUCKETS

    def surface_for(self, glyph, level):
        return _Blank(self.cell_px)

    def glow_surface_for(self, glyph, level):
        if not self.glow_pad:
            raise RuntimeError("glow is disabled for this atlas")
        return _Blank(self.cell_px + 2 * self.glow_pad)


class _Blank:
    def __init__(self, px):
        self._px = px

    def get_data(self): return memoryview(bytes(self._px * self._px * 4))
    def get_stride(self): return self._px * 4
    def get_width(self): return self._px
    def get_height(self): return self._px


def make_runtime(handle=None, **kw):
    rt = GlRuntime(handle=handle or FakeMwl(), log=lambda _m: None, **kw)
    rt.start()
    return rt


def _canvas(handle, cell=18):
    return WorldCanvas.from_geoms([Geom(o.name, o.x, o.y, o.width, o.height)
                                   for o in handle.outputs_list], cell_size=cell)


def make_surface(rt=None, *, scale=2, info_index=0, glow=False, keyboard="exclusive",
                 atlas=None):
    handle = FakeMwl() if rt is None else rt.handle
    rt = rt or make_runtime(handle)
    info = monitor_infos(handle)[info_index]
    crop = _canvas(handle).crops[info_index]
    seen: list[str] = []
    surf = rt.make_surface(
        info, crop, canvas=_canvas(handle),
        atlas=atlas or FakeAtlas(scale=scale, glow_pad=4 if glow else 0),
        dim_alpha=0.72, keyboard_mode=keyboard, glow_enabled=glow,
        resync=lambda: OutputFrame(crop.name, ()),
        on_key=lambda k: seen.append(f"key{k}") or True,
        on_click=lambda: seen.append("click"),
        on_closed=lambda n: seen.append(f"closed:{n}"))
    return surf, rt, handle, seen


def draw(col, row, glyph=1, brightness=1.0, head=False) -> Draw:
    return Draw(col=col, row=row, global_col=col, glyph=glyph,
                brightness=brightness, head=head)


# --------------------------------------------------------------- monitor --

def test_monitor_infos_carry_the_connector_name_and_index():
    infos = monitor_infos(FakeMwl())
    assert [(i.index, i.name, i.scale) for i in infos] == [
        (0, "0:DP-4", 2), (1, "1:DP-5", 1)]
    assert infos[0].width == 2560 and infos[0].x == 2560


def test_monitor_infos_geom_matches_the_shared_canvas():
    infos = monitor_infos(FakeMwl())
    assert infos[1].geom() == Geom("1:DP-5", 0, 0, 2560, 1440)


# ---------------------------------------------------------------- textures --

def test_textures_are_uploaded_once_per_atlas_and_shared_between_outputs():
    rt = make_runtime()
    hot, cold = FakeAtlas(scale=2), FakeAtlas(scale=1)
    s0, _rt, _h, _k = make_surface(rt, atlas=hot, info_index=0)
    s1, _rt, _h, _k = make_surface(rt, atlas=cold, info_index=1)
    assert s0.textures.cell_px != s1.textures.cell_px, "atlases are per output scale"
    assert len(rt.handle.textures) == 2, "one upload per atlas, not per output"

    s2, _rt, _h, _k = make_surface(rt, atlas=hot, info_index=0)
    assert len(rt.handle.textures) == 2, "an identical AtlasKey must not re-upload"
    assert s2.textures is s0.textures


def test_glow_atlas_gets_its_own_texture():
    surf, rt, _h, _k = make_surface(glow=True)
    assert surf.textures.glow_tex, "glow texture built when glow is on"
    assert surf.textures.glow_layout.cell_px == surf.textures.layout.cell_px + \
        2 * surf.atlas.glow_pad


def test_layout_matches_the_atlas_dimensions():
    surf, _rt, _h, _k = make_surface()
    lay = surf.textures.layout
    assert lay.n_glyphs == surf.atlas.n_glyphs
    assert lay.buckets == BUCKETS
    assert lay.cell_px == surf.atlas.cell_px


# ------------------------------------------------------------------ state --

def test_queue_keeps_authoritative_cell_state():
    surf, _rt, _h, _k = make_surface()
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0), draw(1, 1)), ()))
    assert surf.screen_cells == 2
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0, brightness=0.5),), ()))
    assert surf.screen_cells == 2, "same cell updated, not duplicated"
    assert surf.cells[(0, 0)].brightness == 0.5


def test_queue_clear_removes_cells():
    surf, _rt, _h, _k = make_surface()
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0), draw(1, 0)), ()))
    surf.queue(OutputFrame(surf.crop.name, (), (Pos(1, 0),)))
    assert sorted(surf.cells) == [(0, 0)]


def test_resync_replaces_state_from_the_authoritative_frame():
    surf, _rt, _h, _k = make_surface()
    surf.show()
    surf.request_resync()
    surf._resync = lambda: OutputFrame(surf.crop.name, (draw(3, 4),), ())
    surf.present()
    assert sorted(surf.cells) == [(3, 4)]


def test_show_paints_authoritative_state_and_shows_the_veil_immediately():
    surf, _rt, handle, _k = make_surface()
    handle.calls.clear()
    surf._resync = lambda: OutputFrame(surf.crop.name, (draw(2, 5),), ())
    surf.show()
    assert sorted(surf.cells) == [(2, 5)]
    names = [c.name for c in handle.calls]
    assert "clear" in names and "swap" in names, "veil must appear at show()"


# ---------------------------------------------------------------- present --

def test_present_order_is_clear_then_glyphs_then_swap():
    surf, _rt, handle, _k = make_surface()
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0),), ()))
    handle.calls.clear()
    surf.present()
    names = [c.name for c in handle.calls if c.name != "make_current"]
    assert names == ["clear", "set_geom", "draw_cells", "swap"]


def test_present_clears_with_the_premultiplied_dim_colour():
    surf, _rt, handle, _k = make_surface()
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0),), ()))
    handle.calls.clear()
    surf.present()
    clear = next(c for c in handle.calls if c.name == "clear")
    assert clear.args[1] == (0.0, 0.0, 0.0, 0.72), "the dim is now the clear value"


def test_set_dim_changes_the_next_clear():
    surf, _rt, handle, _k = make_surface()
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0),), ()))
    surf.set_dim(0.5)
    handle.calls.clear()
    surf.present()
    assert next(c for c in handle.calls if c.name == "clear").args[1][3] == 0.5


def test_present_draws_glow_additively_before_glyph_bodies():
    surf, _rt, handle, _k = make_surface(glow=True)
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, (draw(0, 0, head=True),
                                            draw(1, 0, head=False)), ()))
    handle.calls.clear()
    surf.present()
    draws = [c for c in handle.calls if c.name == "draw_cells"]
    assert [d.args[3] for d in draws] == [1, 0], "additive glow first, then over"
    assert [d.args[4] for d in draws] == [True, False], \
        "the glow pass asks the native side to filter to heads"
    assert draws[0].args[5] == 4, "glow quads grow by the baked pad"
    assert draws[1].args[2] == 2, "both cells draw"
    assert draws[0].args[1] != draws[1].args[1], "glow uses its own atlas texture"


def test_present_uses_one_cell_texture_batch_for_every_cell():
    surf, _rt, handle, _k = make_surface()
    surf.show()
    surf.queue(OutputFrame(surf.crop.name, tuple(draw(c, 0) for c in range(5)), ()))
    handle.calls.clear()
    surf.present()
    assert [c for c in handle.calls if c.name == "draw_cells"][0].args[2] == 5


def test_present_arms_geometry_from_the_surface_crop_and_scale():
    surf, _rt, handle, _k = make_surface()
    surf.show()
    handle.calls.clear()
    surf.present()
    geom = [c for c in handle.calls if c.name == "set_geom"][0]
    assert geom.args[1] == surf.crop.col0
    assert geom.args[2] == surf.scale
    assert geom.args[3] == surf.textures.layout.buckets, "uv stride is the bucket count"


def test_present_is_inert_after_destroy():
    surf, _rt, handle, _k = make_surface()
    surf.show()
    surf.destroy()
    handle.calls.clear()
    surf.present()
    assert handle.calls == []


# ----------------------------------------------------------------- events --

def test_pump_reads_the_socket_before_draining_the_queue():
    """libwayland only fills its internal queue when the fd is pumped. Reading
    events without dispatching strands every buffer release the compositor sends,
    so eglSwapBuffers stalls on an exhausted pool (measured live: 14 fps instead of
    22 with only 17 ms of real work per frame)."""
    _surf, rt, handle, _k = make_surface()
    handle.calls.clear()
    rt.pump()
    assert [c.name for c in handle.calls] == ["dispatch", "events"]
    assert handle.calls[0].args[0] == 0, "must not block the frame timer"


def test_key_event_routes_the_mapped_keysym_to_the_app():
    surf, rt, handle, seen = make_surface()
    surf.show()
    rt.pump_events([Event("key", (9,))])
    assert seen == [f"key{ESCAPE_KEYSYM}"]


def test_button_event_triggers_the_click_action():
    surf, rt, _h, seen = make_surface()
    surf.show()
    rt.pump_events([Event("button", (surf.sid, 272))])
    assert seen == ["click"]


def test_configure_event_marks_the_surface_for_a_full_redraw():
    surf, rt, _h, _k = make_surface()
    surf.show()
    surf._need_sync = False
    rt.pump_events([Event("configure", (surf.sid, 100, 200))])
    assert surf._need_sync is True


def test_closed_event_reports_once_and_drops_the_surface():
    surf, rt, _h, seen = make_surface()
    surf.show()
    rt.pump_events([Event("closed", (surf.sid,))])
    rt.pump_events([Event("closed", (surf.sid,))])
    assert seen == [f"closed:{surf.info.name}"], "must not double-report"
    assert surf not in rt.surfaces


def test_destroy_notifies_the_app_and_releases_the_native_surface():
    surf, rt, handle, seen = make_surface()
    surf.show()
    surf.destroy()
    assert handle.closed == [surf.sid]
    assert seen == [f"closed:{surf.info.name}"]
    assert surf.destroyed is True


def test_destroy_is_idempotent():
    surf, _rt, handle, seen = make_surface()
    surf.show()
    surf.destroy()
    surf.destroy()
    assert len(handle.closed) == 1 and len(seen) == 1


def test_runtime_shutdown_destroys_surfaces_then_the_display():
    surf, rt, handle, _k = make_surface()
    surf.show()
    handle.closed.clear()
    rt.shutdown()
    assert handle.closed == [surf.sid]
    assert [c.name for c in handle.calls if c.name == "shutdown"] == ["shutdown"]


def test_runtime_reports_the_gl_renderer_for_diagnostics():
    rt = make_runtime()
    assert rt.describe() .startswith("layer_shell=4")
    assert "GL_RENDERER=Fake" in rt.describe()


# ------------------------------------------------------------------ input --

@pytest.mark.parametrize("mode, want", [("exclusive", True), ("on_demand", True),
                                        ("none", False)])
def test_keyboard_mode_maps_to_layer_shell_interactivity(mode, want):
    handle = FakeMwl()
    rt = make_runtime(handle)
    rt.exclusive_keyboard_for(mode)     # helper exists and is pure
    make_surface(rt, keyboard=mode)
    created = [c for c in handle.calls if c.name == "create_surface"][-1]
    assert created.args[2] == want, f"{mode} must request interactivity={want}"


def test_unknown_keyboard_mode_is_rejected_before_touching_the_compositor():
    rt = make_runtime()
    with pytest.raises(ValueError):
        rt.exclusive_keyboard_for("sideways")


def test_overlay_zone_is_negative_by_default_to_cover_the_panel():
    handle = FakeMwl()
    rt = make_runtime(handle)
    make_surface(rt)
    created = [c for c in handle.calls if c.name == "create_surface"][0]
    assert created.args[3] == -1
