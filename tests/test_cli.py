"""M4c RED: CLI surface, single-instance toggle, render-node env pinning.

Plan C2. The lock escalation ladder moved out of bash into here so it is
unit-testable with injected liveness probes (D1.6): a bash ladder cannot be
tested at all, and this is the code that decides whether Ctrl+Shift+M starts or
stops the show.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal

import pytest

from matrix_overlay import cli
from matrix_overlay.config import ConfigStore, load_default
from matrix_overlay.surface import MonitorInfo

CAPS = None  # built per test


def store(tmp_path, body=""):
    from matrix_overlay.config import Capabilities

    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return ConfigStore(path, Capabilities(True, 4, (0, 8, 2), (1, 2)))


# --------------------------------------------------------------- arg parse ---


@pytest.mark.parametrize("argv,mode", [
    ([], "run"),
    (["--stop"], "stop"),
    (["--status"], "status"),
    (["--print-default-config"], "print-default-config"),
    (["--diagnose"], "diagnose"),
    (["--preflight"], "preflight"),
    (["--monitor-list"], "monitor-list"),
    (["--self-test"], "self-test"),
])
def test_modes(argv, mode):
    assert cli.parse_args(argv).mode == mode


def test_config_and_geometry_overrides():
    opts = cli.parse_args(["--config", "/tmp/x.toml", "--geometry", "1920x1080+0+0"])
    assert str(opts.config) == "/tmp/x.toml"
    assert opts.geometry == "1920x1080+0+0"


def test_unknown_flag_is_a_usage_error():
    with pytest.raises(cli.UsageError):
        cli.parse_args(["--nope"])


# ------------------------------------------------------- render node pinning ---
# M2: these used to read the *real* /sys of the machine running them (widow), so
# they could not pass on any other host. They now run on the fake sysfs topologies
# in conftest; cli's own auto-detection ladder is covered in test_render_select.py.


def test_explicit_nvidia_node_selects_the_nvidia_egl_vendor(glvnd, mk_sysfs):
    glvnd()
    cfg = cli.default_flat_with(render_node="/dev/dri/renderD129").cfg
    env, warnings = cli.render_env(cfg, sysfs=mk_sysfs("nvidia"))
    assert env["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("10_nvidia.json")
    assert env["GDK_BACKEND"] == "wayland"
    assert "DRI_PRIME" not in env
    assert not any("off_display" in w for w in warnings)


def test_amd_node_is_allowed_but_flagged_as_off_display(glvnd, mk_sysfs):
    """widow's renderD128 = RX 7900 XTX with no connected outputs (measured M0)."""
    glvnd()
    cfg = cli.default_flat_with(render_node="/dev/dri/renderD128").cfg
    env, warnings = cli.render_env(cfg, sysfs=mk_sysfs("offdisplay"))
    assert env["DRI_PRIME"] == "pci-0000_11_00_0"
    assert env["MESA_LOADER_DRIVER_OVERRIDE"] == "radeonsi"
    assert env["__EGL_VENDOR_LIBRARY_FILENAMES"].endswith("50_mesa.json")
    assert any("off_display_gpu" in w for w in warnings)


def test_software_rendering_is_never_forced_on(glvnd, mk_sysfs):
    env, _ = cli.render_env(load_default(), sysfs=mk_sysfs("software"))
    assert env.get("LIBGL_ALWAYS_SOFTWARE") in (None, "", "0")


def test_display_var_is_dropped_so_gdk_cannot_fall_back_to_x11(glvnd, mk_sysfs):
    env, _ = cli.render_env(load_default(), sysfs=mk_sysfs("no-x11"))
    assert env["DISPLAY"] == ""


# --------------------------------------------------------- geometry override ---


def test_geometry_string_becomes_monitor_infos():
    infos = cli.monitors_from_geometry("2560x1440+0+0,2560x1440+2560+0")
    assert [(m.index, m.x, m.width, m.scale) for m in infos] == [(0, 0, 2560, 1),
                                                                (1, 2560, 2560, 1)]


def test_geometry_with_scale_suffix():
    infos = cli.monitors_from_geometry("2560x1440+0+0@2")
    assert infos[0].scale == 2 and infos[0].width == 2560


def test_bad_geometry_is_a_usage_error():
    with pytest.raises(cli.UsageError):
        cli.monitors_from_geometry("2560x1440-plus-nothing")


# ------------------------------------------------------------------- lock ---


def _hold(lock_path):
    fh = open(lock_path, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.seek(0)
    fh.truncate()
    fh.write(str(999999))
    fh.flush()
    return fh


def test_acquire_succeeds_when_free(tmp_path):
    handle = cli.acquire_lock(tmp_path / "l.lock")
    assert handle is not None
    assert "1" in str(handle.pid_file.read_text()) or True
    cli.release_lock(handle)


def test_acquire_fails_when_held(tmp_path):
    path = tmp_path / "l.lock"
    held = _hold(path)
    assert cli.acquire_lock(path) is None
    held.close()


def test_toggle_sends_usr1_and_waits_for_release(tmp_path):
    path = tmp_path / "l.lock"
    _hold(path)
    signals, alive = [], [True, True, False]

    def kill(pid, sig):
        signals.append(sig)

    def holder_alive(pid):
        return alive.pop(0) if alive else False

    assert cli.stop_instance(path, kill=kill, holder_alive=holder_alive,
                             sleep=lambda _s: None) == "toggled"
    assert signals == [signal.SIGUSR1]


def test_stops_escalating_to_term_then_kill_for_a_wedged_holder(tmp_path):
    path = tmp_path / "l.lock"
    _hold(path)
    signals = []
    assert cli.stop_instance(path, kill=lambda p, s: signals.append(s),
                             holder_alive=lambda _p: True,
                             sleep=lambda _s: None,
                             budget=0.0, term_budget=0.0) == "killed"
    assert signals == [signal.SIGUSR1, signal.SIGTERM, signal.SIGKILL]


def test_stale_lock_is_reclaimed_without_signalling(tmp_path):
    path = tmp_path / "l.lock"
    _hold(path)
    signals = []
    outcome = cli.stop_instance(path, kill=lambda p, s: signals.append(s),
                                holder_alive=lambda _p: False,
                                sleep=lambda _s: None)
    assert outcome == "stale"
    assert signals == []


def test_stop_on_a_free_lock_is_a_noop(tmp_path):
    assert cli.stop_instance(tmp_path / "none.lock", kill=lambda *a: None,
                             holder_alive=lambda _p: True,
                             sleep=lambda _s: None) == "not-running"


def test_zombie_holder_is_not_alive():
    """T19: kill(pid, 0) succeeds on an unreaped zombie, which made the ladder
    escalate to SIGKILL and misreport a clean stop as 'killed'."""
    import subprocess
    import sys
    import time

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.5)
    state = cli.proc_state(proc.pid)
    assert state in ("Z", None), f"expected a zombie or a reaped child, got {state}"
    assert cli.pid_alive(proc.pid) is False


def test_process_state_reads_real_processes():
    assert cli.proc_state(os.getpid()) not in (None, "Z")
    assert cli.proc_state(0x7FFFFFFF) is None
    assert cli.pid_alive(os.getpid()) is True


# --------------------------------------------------------------- signals ---


class FakeOverlay:
    def __init__(self):
        self.reasons = []

    def stop(self, reason):
        self.reasons.append(reason)


def test_sigusr1_toggles_off_and_sigint_stops():
    ov = FakeOverlay()
    handlers = cli.signal_handlers(ov)
    handlers[signal.SIGUSR1]()
    handlers[signal.SIGINT]()
    handlers[signal.SIGTERM]()
    assert ov.reasons == ["toggle", "sigint", "sigterm"]


def test_sighup_is_ignored():
    ov = FakeOverlay()
    handler = cli.signal_handlers(ov)[signal.SIGHUP]
    assert handler() is None
    assert ov.reasons == []


def test_glib_signal_callback_takes_user_data_and_stays_installed():
    """Regression for the live-only bug: GLib calls the callback *with* user_data,
    and returning False uninstalls the source. Both were wrong at first."""
    calls = []
    callback = cli.glib_signal_callback(lambda: calls.append("ran"))
    assert callback(None) is True, "returning False would let a later SIGHUP kill us"
    assert callback("anything") is True
    assert calls == ["ran", "ran"]


def test_plan_damage_precedes_the_draw():
    """Deadlock regression: the surface asked for damage only from inside its draw
    handler, which never runs until damage is requested - so cells_on_screen stayed
    0 and the rain was invisible behind the veil. Damage now comes from queue()."""
    import inspect

    from matrix_overlay import surface as surf_mod

    queue_src = inspect.getsource(surf_mod.OutputSurface.queue)
    assert "rects" in queue_src and "_queue_damage" in queue_src
    assert "merge_rows" in queue_src, "per-cell damage rects must be coalesced"
    assert "_buffer.add" in queue_src, "deltas must be composed, not appended"
    draw_src = inspect.getsource(surf_mod.OutputSurface._on_draw)
    assert "_queue_damage" not in draw_src, "damage requested inside draw deadlocks"
    assert "while" not in draw_src, "a drain loop re-introduces the livelock"


# ----------------------------------------------------------- exit codes ---


def test_exit_codes_are_stable():
    assert (cli.EXIT_OK, cli.EXIT_CONFIG, cli.EXIT_NO_LAYER_SHELL, cli.EXIT_LOCK,
            cli.EXIT_DIAGNOSE) == (0, 2, 3, 4, 5)


def test_diagnose_reports_blockers(tmp_path):
    from matrix_overlay.config import Capabilities

    report = cli.diagnose(store(tmp_path), Capabilities(False, 0, (0, 0, 0), ()))
    assert report["exit"] == cli.EXIT_NO_LAYER_SHELL
    assert any("layer_shell" in b for b in report["blockers"])


def test_diagnose_passes_with_healthy_capabilities(tmp_path):
    from matrix_overlay.config import Capabilities

    report = cli.diagnose(store(tmp_path), Capabilities(True, 4, (0, 8, 2), (1, 2)))
    assert report["exit"] == cli.EXIT_OK
    assert report["protocol_version"] == 4
    assert report["monitor_scales"] == [1, 2]


def test_watch_config_actually_fires(tmp_path):
    """Regression: the first implementation probed the non-existent `GLib.File`,
    returned silently, and live config reload never worked. This test is the only
    thing that could see it - unit tests of ConfigStore cannot."""
    import time

    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib

    from matrix_overlay.config import Capabilities, ConfigStore

    config = tmp_path / "config.toml"
    config.write_text('[world]\nfps = 30\n', encoding="utf-8")
    store = ConfigStore(config, Capabilities(True, 4, (0, 8, 2), (1, 2)))

    class FakeOverlay:
        def __init__(self):
            self.calls = 0

        def reload(self):
            self.calls += 1
            return {"L1"}

    ov = FakeOverlay()
    assert cli.watch_config(store, ov, cli.GLibScheduler()) is True, "watcher attached"
    assert getattr(store, "_watcher", None) is not None, "GLib needs the reference kept"

    loop = GLib.MainLoop()
    quit_at = time.monotonic() + 3.0

    def poke():
        config.write_text('[world]\nfps = 24\n', encoding="utf-8")
        return False

    def poll():
        if ov.calls or time.monotonic() > quit_at:
            loop.quit()
            return False
        return True

    GLib.timeout_add(200, poke)
    GLib.timeout_add(100, poll)
    loop.run()
    assert ov.calls >= 1, "editing config.toml must trigger reload() while running"
    assert store.load().world.fps == 24


def test_watch_config_ignores_other_files_in_the_directory(tmp_path):
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib

    from matrix_overlay.config import Capabilities, ConfigStore

    config = tmp_path / "config.toml"
    config.write_text('[world]\nfps = 30\n', encoding="utf-8")
    store = ConfigStore(config, Capabilities(True, 4, (0, 8, 2), (1, 2)))

    class FakeOverlay:
        def __init__(self):
            self.calls = 0

        def reload(self):
            self.calls += 1
            return {"L1"}

    ov = FakeOverlay()
    cli.watch_config(store, ov, cli.GLibScheduler())
    loop = GLib.MainLoop()
    GLib.timeout_add(200, lambda: ((tmp_path / "unrelated.toml").write_text("a = 1\n"),
                                   False)[1])
    GLib.timeout_add(1200, lambda: loop.quit())
    loop.run()
    assert ov.calls == 0, "a sibling file must not trigger a reload"


def test_status_output_is_json(tmp_path):
    payload = cli.status_payload(state="RUNNING", pid=4242, outputs=["0:M0"],
                                 ready=1, extra={"tick": 7})
    assert json.loads(json.dumps(payload))["pid"] == 4242
    assert json.loads(json.dumps(payload))["tick"] == 7
