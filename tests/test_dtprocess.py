"""Unit tests for scouter/core/dtprocess.py (no darktable: fake processes)."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from scouter.core import dtprocess

SLEEPER = [sys.executable, "-c", "import signal,time\n"
           "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)"]


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(dtprocess, "owner_path", lambda: tmp_path / "darktable.owner.json")
    return tmp_path


def test_launch_writes_owner_file_and_owned_pid_matches(owner, monkeypatch, tmp_path):
    fake = tmp_path / "darktable"
    # `exec -a "$0"` keeps argv[0] (what macOS `ps -o comm=` reports) as the
    # fake's path, so comm's basename stays `darktable` after the exec.
    fake.write_text('#!/bin/bash\nexec -a "$0" sleep 30\n')
    fake.chmod(0o755)
    rec = dtprocess.launch(None, tmp_path / "lib" / "library.db", exe=str(fake))
    child = dtprocess._children[rec.pid]
    try:
        data = json.loads((owner / "darktable.owner.json").read_text())
        assert data["pid"] == rec.pid and data["start_time"]
        deadline = time.monotonic() + 5  # let bash reach the exec
        while dtprocess.owned_pid() != rec.pid and time.monotonic() < deadline:
            time.sleep(0.1)
        assert dtprocess.owned_pid() == rec.pid
    finally:
        os.kill(rec.pid, signal.SIGKILL)
        child.wait(timeout=5)
    assert dtprocess.owned_pid() is None


def test_launch_without_start_time_stops_child_and_writes_no_owner(owner, monkeypatch,
                                                                    tmp_path):
    fake = tmp_path / "darktable"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    started = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        started.append(p)
        return p

    monkeypatch.setattr(dtprocess.subprocess, "Popen", popen)
    monkeypatch.setattr(dtprocess, "_lstart", lambda pid: None)
    monkeypatch.setattr(dtprocess.time, "sleep", lambda s: None)
    with pytest.raises(dtprocess.LaunchError):
        dtprocess.launch(None, tmp_path / "lib" / "library.db", exe=str(fake))
    assert len(started) == 1 and started[0].poll() is not None
    assert not (owner / "darktable.owner.json").exists()
    assert started[0].pid not in dtprocess._children


def test_launch_forwards_env_to_popen(owner, monkeypatch, tmp_path):
    """C1: launch's env kwarg reaches Popen so the child never inherits the
    real HOME/XDG_*; no real process is spawned here (Popen is monkeypatched)."""
    captured = {}

    class FakeProc:
        pid = 4321

        def kill(self):
            pass

        def wait(self, timeout=None):
            pass

    def fake_popen(argv, *, stdout=None, stderr=None, start_new_session=None, env=None):
        captured["env"] = env
        return FakeProc()

    monkeypatch.setattr(dtprocess.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(dtprocess, "_lstart", lambda pid: None)  # force the fast LaunchError path
    monkeypatch.setattr(dtprocess.time, "sleep", lambda s: None)
    custom_env = {"HOME": "/isolated/home"}
    with pytest.raises(dtprocess.LaunchError):
        dtprocess.launch(None, tmp_path / "lib" / "library.db", env=custom_env)
    assert captured["env"] == custom_env


def test_foreign_pid_is_refused_and_never_signalled(owner):
    # A genuinely owned record (this test process: live pid, real lstart and
    # comm); `kill` is a recorder, so nothing is ever signalled.
    me = os.getpid()
    (owner / "darktable.owner.json").write_text(json.dumps({
        "pid": me, "start_time": dtprocess._lstart(me), "exe": dtprocess._comm(me),
        "library": "/l", "configdir": None, "frame": None}))
    assert dtprocess.owned_pid() == me
    other = 12345 if me != 12345 else 12346
    sent = []
    res = dtprocess.terminate(other, kill=lambda p, s: sent.append((p, s)))
    assert res["how"] == "refused_not_ours" and not res["ok"] and sent == []


def test_pid_reuse_is_not_ours(owner, monkeypatch):
    (owner / "darktable.owner.json").write_text(json.dumps({
        "pid": os.getpid(), "start_time": "Mon Jan  1 00:00:00 1990", "exe": "/x/python",
        "library": "/l", "configdir": None, "frame": None}))
    assert dtprocess.owned_pid() is None


def test_ladder_escalates_to_sigkill_without_osascript(owner, monkeypatch):
    p = subprocess.Popen(SLEEPER)
    time.sleep(0.3)
    monkeypatch.setattr(dtprocess, "owned_pid", lambda: p.pid)
    alive = lambda pid: p.poll() is None  # noqa: E731
    t0 = time.monotonic()
    res = dtprocess.terminate(p.pid, responsive=False, alive=alive)
    assert res["ok"] and res["how"] == "SIGKILL"
    assert "osascript" not in res.get("steps", [])
    assert time.monotonic() - t0 < 20


def test_wait_gui_ready_polls_diag_not_view():
    answers = iter([RuntimeError("no bridge"), {"gui_ready": False}, {"gui_ready": True,
                                                                      "gui_ready_source": "event"}])
    calls = []

    def call(method, params, timeout):
        calls.append(method)
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    t = [0.0]
    res = dtprocess.wait_gui_ready(10, call=call, clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert res["ready"] and set(calls) == {"darktable_mcp.diag"}


def test_wait_gui_ready_times_out():
    t = [0.0]
    res = dtprocess.wait_gui_ready(2, call=lambda *a, **k: {"gui_ready": False},
                                   clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert not res["ready"]


def test_wait_gui_ready_stop_if_ends_the_poll_early():
    """A caller's own fatal-fast condition (checked once per poll, same
    cadence as the diag call) must end the wait well before `timeout_s`,
    not only once it elapses."""
    t = [0.0]
    polls = [0]

    def stop_if():
        polls[0] += 1
        return polls[0] >= 3  # true on the third poll

    res = dtprocess.wait_gui_ready(30, call=lambda *a, **k: {"gui_ready": False},
                                   clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s),
                                   stop_if=stop_if)
    assert res == {"ready": False, "gui_ready": False}
    assert t[0] < 5  # ended after ~2 poll_s (1 s default), nowhere near the 30 s timeout


def test_wait_gui_ready_with_no_stop_if_behaves_exactly_as_before():
    """Control: omitting `stop_if` (every existing caller) never calls it."""
    t = [0.0]
    res = dtprocess.wait_gui_ready(2, call=lambda *a, **k: {"gui_ready": False},
                                   clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert not res["ready"]


def test_stale_lock_removed_only_for_dead_owner(owner, tmp_path):
    lib = tmp_path / "library.db"
    lock = tmp_path / "library.db.lock"
    (owner / "darktable.owner.json").write_text(json.dumps({
        "pid": 999999, "start_time": "x", "exe": "darktable", "library": str(lib),
        "configdir": None, "frame": None}))
    lock.write_text("999999\n")
    assert dtprocess.clear_stale_locks(lib) == [str(lock)]
    lock.write_text("4242\n")
    assert dtprocess.clear_stale_locks(lib) == []


def test_osascript_skipped_when_a_foreign_darktable_runs(owner, monkeypatch):
    """osascript quits by app name, so it must not run beside a foreign darktable."""
    monkeypatch.setattr(dtprocess, "owned_pid", lambda: 4242)
    monkeypatch.setattr(dtprocess, "foreign_pids", lambda: [999])

    def no_osascript(*a, **k):
        raise AssertionError("osascript must not run")

    monkeypatch.setattr(dtprocess.subprocess, "run", no_osascript)
    sent, state = [], {"alive": True}

    def kill(p, s):
        sent.append((p, s))
        state["alive"] = False

    res = dtprocess.terminate(kill=kill, alive=lambda p: state["alive"], sleep=lambda s: None)
    assert res["ok"] and res["how"] == "SIGTERM"
    assert sent == [(4242, signal.SIGTERM)]


def test_no_signal_when_ownership_lost_before_sigkill(owner, monkeypatch):
    """After SIGTERM's wait the pid may be reused: SIGKILL re-checks ownership."""
    owned = iter([4242, 4242, None])  # entry, before SIGTERM, before SIGKILL
    monkeypatch.setattr(dtprocess, "owned_pid", lambda: next(owned))
    sent = []
    res = dtprocess.terminate(responsive=False, kill=lambda p, s: sent.append((p, s)),
                              alive=lambda p: True, sleep=lambda s: None)
    assert sent == [(4242, signal.SIGTERM)]
    assert res["how"] == "gone_or_reused" and not res["ok"]


def test_pgrep_timeout_fails_closed_no_osascript(owner, monkeypatch):
    monkeypatch.setattr(dtprocess, "owned_pid", lambda: 4242)

    def run(argv, *a, **k):
        if argv[0] == "pgrep":
            assert k.get("timeout"), "pgrep must have a timeout"
            raise subprocess.TimeoutExpired(argv, k["timeout"])
        raise AssertionError(f"unexpected subprocess.run {argv}")

    monkeypatch.setattr(dtprocess.subprocess, "run", run)
    sent, state = [], {"alive": True}

    def kill(p, s):
        sent.append((p, s))
        state["alive"] = False

    res = dtprocess.terminate(kill=kill, alive=lambda p: state["alive"], sleep=lambda s: None)
    assert "osascript_skipped_unknown" in res["steps"] and res["how"] == "SIGTERM"
    assert sent == [(4242, signal.SIGTERM)]
