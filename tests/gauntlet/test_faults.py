import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gauntlet.adapter import CallRecord, Observation
from tests.gauntlet import execute, faults, taxonomy
from tests.gauntlet.layers import robustness
from tests.gauntlet.session import ScouterSession, SessionBlocked


@pytest.fixture(autouse=True)
def kills(monkeypatch):
    """Safety: no unit test signals a real process, runs hdiutil or launches
    or quits darktable. Signals are recorded instead."""
    sent: list = []

    def fake_kill(sig=signal.SIGKILL):
        sent.append(sig)

    def forbidden(*a, **k):
        raise AssertionError(f"real side effect in a unit test: {a!r}")

    from scouter.core import dtprocess

    monkeypatch.setattr(faults, "_owned_kill", fake_kill)
    monkeypatch.setattr(faults, "_hdiutil", forbidden)
    monkeypatch.setattr(dtprocess, "launch", forbidden)
    monkeypatch.setattr(dtprocess, "terminate", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    return sent


def _session(tmp_path):
    hero = tmp_path / "frames" / "hero" / "H.ORF"
    hero.parent.mkdir(parents=True)
    hero.write_bytes(b"raw")
    Path(str(hero) + ".xmp").write_text("<x:xmpmeta>" + "a" * 100 + "</x:xmpmeta>")
    staged = SimpleNamespace(frames={"hero": hero}, library=tmp_path / "library.db",
                             env={"SCOUTER_CONFIG": str(tmp_path / "config" / "config.json")},
                             vars={}, job_frames=[])
    return SimpleNamespace(staged=staged, root=tmp_path, body="olympus")


def test_file_faults_arm_and_disarm(tmp_path):
    s = _session(tmp_path)
    v = faults.arm(s, "chmod_sidecar_readonly")
    side = Path(v["_restore_mode"])
    assert oct(side.stat().st_mode & 0o777) == "0o444"
    faults.disarm(s, v)
    assert oct(side.stat().st_mode & 0o777) == "0o644"
    v = faults.arm(s, "torn_sidecar_copy")
    torn = Path(v["torn_copy"] + ".xmp").read_text()
    assert len(torn) < 70
    v = faults.arm(s, "none")
    assert not Path(v["missing_file"]).exists() and Path(v["unsupported_file"]).exists()
    v = faults.arm(s, "future_mtime")
    assert os.path.getmtime(str(s.staged.frames["hero"]) + ".xmp") > 2e9 - 1e9
    v = faults.arm(s, "modal_dialog")
    assert "no modal-dialog test hook" in v["_skip"]


def _cases():
    return {c.source["id"]: c for c in robustness.generate(taxonomy.load())}


class FakeSession:
    """Just enough ScouterSession for faults: records calls and relaunches."""

    def __init__(self, tmp_path, relaunch_error=None):
        base = _session(tmp_path)
        self.staged, self.root, self.body = base.staged, base.root, base.body
        self.relaunches = 0
        self.tools: list[str] = []
        self.relaunch_error = relaunch_error

    def relaunch(self, frame=None):
        self.relaunches += 1
        if self.relaunch_error:
            raise self.relaunch_error

    def call(self, tool, args, budget_s, *, write=False):
        self.tools.append(tool)
        return CallRecord(tool, args, {"ok": True})


def _hdiutil_fake(mount, log):
    def fake(*args, timeout=60):
        log.append(args)
        out = f"/dev/disk99\tGUID_partition_scheme\t\n/dev/disk99s1\tApple_HFS\t{mount}\n" \
            if args[0] == "attach" else ""
        return SimpleNamespace(stdout=out, returncode=0)
    return fake


def test_r18_filler_sized_from_statvfs_and_detached(tmp_path, monkeypatch):
    mount = tmp_path / "mnt"
    mount.mkdir()
    log: list = []
    monkeypatch.setattr(faults, "_hdiutil", _hdiutil_fake(mount, log))
    free = 3 * 1024 * 1024
    monkeypatch.setattr(faults, "_statvfs",
                        lambda p: SimpleNamespace(f_bavail=free // 4096, f_frsize=4096))
    s = FakeSession(tmp_path)
    v = faults.arm(s, "disk_nearly_full")
    size = (mount / "filler.bin").stat().st_size
    assert size == free - faults.FILL_HEADROOM and size < free
    assert v["_detach"] == str(mount) == v["full_disk"]
    assert faults.disarm(s, v) == []
    assert ("detach", str(mount), "-force") in log


def test_r18_mount_detached_when_a_later_arm_step_fails(tmp_path, monkeypatch):
    mount = tmp_path / "mnt"
    mount.mkdir()
    log: list = []
    monkeypatch.setattr(faults, "_hdiutil", _hdiutil_fake(mount, log))

    def broken_statvfs(p):
        raise OSError(5, "statvfs failed")
    monkeypatch.setattr(faults, "_statvfs", broken_statvfs)
    obs = faults.run_fault_case(FakeSession(tmp_path), _cases()["R18"])
    assert obs.blocked.startswith("harness_bug: OSError")
    assert ("detach", str(mount), "-force") in log


def test_arm_raising_is_typed_blocked_and_partial_disarm_ran(tmp_path, monkeypatch, kills):
    def stuck(sig=signal.SIGKILL):
        kills.append(sig)
        if sig == signal.SIGSTOP:
            raise ProcessLookupError("gone")
    monkeypatch.setattr(faults, "_owned_kill", stuck)
    s = FakeSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R3"])  # wedge_worker
    assert obs.blocked == "harness_bug: ProcessLookupError: gone"
    assert kills == [signal.SIGSTOP, signal.SIGCONT] and s.relaunches == 1


def test_disarm_step_raising_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case: Observation())
    s = FakeSession(tmp_path, relaunch_error=SessionBlocked("relaunch: did not quit"))
    obs = faults.run_fault_case(s, _cases()["R17"])  # slow_bridge: SIGCONT + relaunch
    assert obs.blocked is None
    assert obs.extra["disarm_errors"] == ["relaunch: SessionBlocked: relaunch: did not quit"]
    # the other undo steps still ran
    v = {"_relaunch": True}
    side = Path(str(s.staged.frames["hero"]) + ".xmp")
    before = side.read_bytes()
    faults._snap(v, side)
    side.write_bytes(b"clobbered")
    errors = faults.disarm(s, v)
    assert len(errors) == 1 and errors[0].startswith("relaunch: SessionBlocked")
    assert side.read_bytes() == before


def test_r21_fake_version_returns_without_raising(tmp_path):
    s = FakeSession(tmp_path)
    s.staged.env = {"SCOUTER_CONFIG": str(tmp_path / "config" / "config.json"),
                    "SCOUTER_CACHE": str(tmp_path / "cache"),
                    "SCOUTER_DARKTABLE_LIBRARY_DB": str(tmp_path / "library" / "library.db")}
    real_config = os.environ.get("SCOUTER_CONFIG")
    obs = faults.run_fault_case(s, _cases()["R21"])
    assert obs.blocked is None and len(obs.calls) == 1
    assert os.environ.get("SCOUTER_CONFIG") == real_config  # env put back


def test_broken_luarc_never_launches_when_quit_fails(tmp_path, kills):
    launched: list = []

    class Dt:
        def terminate(self, *, responsive=None):
            return {"ok": False, "how": "still running"}

        def launch(self, *a):
            launched.append(a)

    s = ScouterSession(None, tmp_path, "olympus", dt=Dt(), mcp_factory=lambda *a: None)
    s.staged = _session(tmp_path).staged
    obs = faults.run_fault_case(s, _cases()["R23"])  # dtprocess.launch raises if called
    assert obs.blocked.startswith("session: broken_luarc: darktable did not quit")
    assert launched == [] and kills == []
    assert obs.extra["disarm_errors"][0].startswith("relaunch: SessionBlocked")


def test_kill_timer_starts_at_the_step_and_is_cancelled_in_disarm(tmp_path, monkeypatch,
                                                                   kills):
    monkeypatch.setattr(faults, "KILL_DELAY_S", 0.2)
    s = FakeSession(tmp_path)
    v = faults.arm(s, "kill_during_call")
    assert v["_relaunch"] and not v["_timer"].is_alive()
    s.call("darkroom_open", {}, 30.0)  # ensure_frame's open does not start it
    assert not v["_timer"].is_alive()
    faults.disarm(s, v)
    time.sleep(0.4)
    assert kills == [] and s.relaunches == 1
    assert "call" not in vars(s)  # the wrapper is gone; the class method is back
    v = faults.arm(s, "kill_during_call")
    s.call("darkroom_say", {"phrase": "brighter"}, 12.0)
    assert v["_timer"].is_alive() or kills  # started relative to the call
    faults.disarm(s, v)
    time.sleep(0.4)
    assert kills in ([], [signal.SIGKILL])
    assert v["_timer"].finished.is_set()


def _wd(tmp_path, original):
    cfg = tmp_path / "config" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(original)
    s = FakeSession(tmp_path)
    step = SimpleNamespace(args={"scenario": "auto_heal_once"})
    return s, cfg, step


def test_watchdog_probe_restores_config_exactly_even_when_kill_raises(tmp_path, monkeypatch):
    original = '{ "watchdog": {"auto_heal": false},\n  "x": 1 }\n'
    s, cfg, step = _wd(tmp_path, original)

    def boom(sig=signal.SIGKILL):
        raise ProcessLookupError("no owned darktable")
    monkeypatch.setattr(faults, "_owned_kill", boom)
    with pytest.raises(ProcessLookupError):
        faults.watchdog_probe(s, None, step, Observation())
    assert cfg.read_text() == original and s.relaunches == 1


def test_watchdog_probe_heal_path_restores_config_and_resumes(tmp_path, kills):
    original = '{"watchdog": {"auto_heal": false, "max_heals_per_job": 3}}'
    s, cfg, step = _wd(tmp_path, original)
    seen: list = []
    orig_call = s.call

    def spy(tool, args, budget_s, *, write=False):
        seen.append(json.loads(cfg.read_text())["watchdog"]["auto_heal"])
        return orig_call(tool, args, budget_s, write=write)
    s.call = spy
    faults.watchdog_probe(s, None, step, Observation())
    assert seen == [True] and cfg.read_text() == original
    assert kills == [signal.SIGSTOP, signal.SIGCONT] and s.relaunches == 1


def test_file_mutating_faults_restored_byte_for_byte(tmp_path):
    s = FakeSession(tmp_path)
    wb = Path(s.staged.env["SCOUTER_CONFIG"]).parent / "wb"
    wb.mkdir(parents=True)
    (wb / "em10iii.json").write_bytes(b'{"olympus": 1}')
    a7 = wb / "a7.json"
    a7.write_bytes(b'{"sony": 2}\n')
    a7.chmod(0o640)
    os.utime(a7, ns=(1_600_000_000_000_000_000, 1_600_000_000_123_456_789))
    lock = Path(str(s.staged.library) + ".lock")
    lock.write_bytes(b"live-darktable-lock\n")
    side = Path(str(s.staged.frames["hero"]) + ".xmp")
    side_bytes, side_mtime = side.read_bytes(), side.stat().st_mtime_ns

    def state(p):
        st = p.stat()
        return p.read_bytes(), st.st_mode & 0o7777, st.st_mtime_ns

    for fault, path in (("wrong_wb_table", a7), ("second_library_lock", lock),
                        ("delete_sidecar", side), ("future_mtime", side),
                        ("stale_sidecar", side), ("chmod_sidecar_readonly", side)):
        before = state(path)
        v = faults.arm(s, fault)
        assert not path.exists() or state(path) != before, fault
        assert faults.disarm(s, v) == [], fault
        assert state(path) == before, fault
    assert side.read_bytes() == side_bytes and side.stat().st_mtime_ns == side_mtime
    v = faults.arm(s, "chmod_out_dir")
    faults.disarm(s, v)
    assert not (tmp_path / "denied").exists()
