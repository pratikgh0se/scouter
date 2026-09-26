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
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    rec = dtprocess.launch(None, tmp_path / "lib" / "library.db", exe=str(fake))
    try:
        data = json.loads((owner / "darktable.owner.json").read_text())
        assert data["pid"] == rec.pid and data["start_time"]
        assert dtprocess.owned_pid() in (rec.pid, None)  # comm may be `sleep` after exec
    finally:
        os.kill(rec.pid, signal.SIGKILL)


def test_foreign_pid_is_refused_and_never_signalled(owner):
    sent = []
    res = dtprocess.terminate(12345, kill=lambda p, s: sent.append((p, s)))
    assert res["how"] in ("refused_not_ours", "not running") and sent == []


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
