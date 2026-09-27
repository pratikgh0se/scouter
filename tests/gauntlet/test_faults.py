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
    """Just enough ScouterSession for faults: records calls and relaunches.

    `touched`/`baselines` + `ensure_frame` (fix C): enough for a real
    `execute.isolate()` call to run against this fake, since `run_fault_case`
    now calls it directly for the freeze faults, before `arm`."""

    def __init__(self, tmp_path, relaunch_error=None):
        base = _session(tmp_path)
        self.staged, self.root, self.body = base.staged, base.root, base.body
        self.relaunches = 0
        self.tools: list[str] = []
        self.relaunch_error = relaunch_error
        self.touched: set[str] = set()
        self.baselines: dict = {}
        self.mcp_reopened = 0

    def relaunch(self, frame=None):
        self.relaunches += 1
        if self.relaunch_error:
            raise self.relaunch_error

    def reopen_mcp(self) -> None:
        self.mcp_reopened += 1

    def ensure_frame(self, role):
        return self.staged.frames[role]

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
    # (fix round 1 review H2): disarm's SIGCONT-then-relaunch is now the one
    # fence_and_recover helper -- no bare SIGCONT any more (a SIGSTOPped
    # process is terminated outright by the quit ladder's eventual SIGKILL,
    # never resumed first); FakeSession has no _quit/_launch, so it falls
    # back to its own relaunch().
    assert kills == [signal.SIGSTOP] and s.relaunches == 1


def test_disarm_step_raising_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case, **kw: Observation())
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


def test_hang_triggers_relaunch_and_mcp_reopen(tmp_path, monkeypatch):
    """Fix A (night1-robustness-triage.md item 1): a case that ends `hang`
    must relaunch darktable and reopen the MCP client before returning, so
    the NEXT case starts clean instead of inheriting the wedge (rob-r11's one
    true hang scored as six, r12-r15/r17 all queued behind it)."""
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case, **kw: Observation(blocked="hang"))
    s = FakeSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R12"])
    assert obs.blocked == "hang"
    assert s.relaunches == 1
    assert s.mcp_reopened == 1


def test_a_typed_hang_reason_also_triggers_recovery(tmp_path, monkeypatch):
    """`obs.blocked` from a real hang is never the bare string "hang" alone
    (`execute.run_tool_case` writes "hang" exactly, but a virtual tool's own
    subprocess timeout writes "hang: <tool> exceeded budget") -- recovery
    must fire on the whole `hang*` family, matching `evaluate.is_hang`."""
    monkeypatch.setattr(execute, "run_tool_case",
                        lambda session, case, **kw: Observation(blocked="hang: scouter cli exceeded "
                                                                "budget"))
    s = FakeSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R12"])
    assert obs.blocked.startswith("hang")
    assert s.relaunches == 1 and s.mcp_reopened == 1


def test_non_hang_case_never_triggers_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case, **kw: Observation())
    s = FakeSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R12"])
    assert obs.blocked is None
    assert s.relaunches == 0 and s.mcp_reopened == 0


def test_hang_recovery_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case, **kw: Observation(blocked="hang"))
    s = FakeSession(tmp_path, relaunch_error=SessionBlocked("relaunch: did not quit"))
    obs = faults.run_fault_case(s, _cases()["R12"])
    assert obs.blocked == "hang"
    # (fix round 1 review H2): fence_and_recover, same helper disarm uses.
    assert obs.extra["disarm_errors"] == ["relaunch: SessionBlocked: relaunch: did not quit"]
    assert s.mcp_reopened == 0  # relaunch failed; reopen was never attempted


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

        def launch(self, *a, **kw):
            launched.append(a)

    s = ScouterSession(None, tmp_path, "olympus", dt=Dt(), mcp_factory=lambda *a: None)
    s.staged = _session(tmp_path).staged
    obs = faults.run_fault_case(s, _cases()["R23"])  # dtprocess.launch raises if called
    assert obs.blocked.startswith("session: broken_luarc: darktable did not quit")
    assert launched == [] and kills == []
    # (fix round 1 review H2): a real ScouterSession's fence_and_recover
    # quits and launches as two separate guarded steps -- never launches a
    # second darktable when quit itself did not confirm ok.
    assert obs.extra["disarm_errors"][0].startswith("quit darktable: SessionBlocked")


def test_kill_timer_starts_at_the_step_and_is_cancelled_in_disarm(tmp_path, monkeypatch,
                                                                   kills):
    monkeypatch.setattr(faults, "KILL_DELAY_S", 0.2)
    s = FakeSession(tmp_path)
    v = faults.arm(s, "kill_during_call", step_tool="darkroom_say")
    assert v["_relaunch"] and not v["_timer"].is_alive()
    s.call("darkroom_open", {}, 30.0)  # ensure_frame's open does not start it
    s.call("darkroom_reset", {"module": "exposure"}, 12.0)  # nor isolate()'s resets
    assert not v["_timer"].is_alive()
    faults.disarm(s, v)
    time.sleep(0.4)
    assert kills == [] and s.relaunches == 1
    assert "call" not in vars(s)  # the wrapper is gone; the class method is back
    v = faults.arm(s, "kill_during_call", step_tool="darkroom_say")
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


def test_kill_during_call_needs_the_step_tool(tmp_path):
    with pytest.raises(ValueError):
        faults.arm(FakeSession(tmp_path), "kill_during_call")


def test_r2_timer_starts_on_the_fault_step_not_on_isolate_resets(tmp_path, monkeypatch):
    """Real run_tool_case: ensure_frame + isolate() (session.touched non-empty,
    so darkroom_reset calls run) before R2's darkroom_say."""
    from tests.gauntlet import observe

    started_after: list = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.alive = False

        def start(self):
            self.alive = True
            started_after.append(list(s.tools))

        def cancel(self):
            self.alive = False

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return self.alive

    class ToolSession(FakeSession):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self.touched = {"*"}
            self.baselines, self.floors = {}, {}
            self.previews_dir = tmp_path / "previews"

        def ensure_frame(self, role):
            return self.staged.frames[role]

    monkeypatch.setattr(faults.threading, "Timer", FakeTimer)
    monkeypatch.setattr(observe, "snapshot", lambda *a, **k: {"params": {}, "units": {}})
    monkeypatch.setattr(execute, "needs_stats", lambda case: False)
    s = ToolSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R2"])
    assert obs.blocked is None
    resets = [t for t in s.tools if t == "darkroom_reset"]
    assert resets and s.tools[-1] == "darkroom_say"
    assert len(started_after) == 1
    # started exactly as darkroom_say began: every reset had already run
    assert started_after[0] == s.tools[:-1] and "darkroom_say" not in started_after[0]
    assert "call" not in vars(s) and s.relaunches == 1


def test_r3_isolate_runs_before_the_freeze_signal(tmp_path, monkeypatch, kills):
    """Fix C (night1-robustness-triage.md item 3): rob-r3's own isolate()
    resets ran against a darktable the fault had already SIGSTOPped -- 6
    resets x ~20s each blew the whole 132s hard cap. isolate must run while
    darktable is still responsive, before arm's SIGSTOP freezes it."""
    from tests.gauntlet import observe

    order: list[str] = []

    class ToolSession(FakeSession):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self.touched = {"exposure"}

        def call(self, tool, args, budget_s, *, write=False):
            order.append(tool)
            return super().call(tool, args, budget_s, write=write)

    def recording_kill(sig=signal.SIGKILL):
        order.append({signal.SIGSTOP: "SIGSTOP", signal.SIGCONT: "SIGCONT"}.get(sig, "kill"))

    monkeypatch.setattr(faults, "_owned_kill", recording_kill)
    monkeypatch.setattr(observe, "snapshot", lambda *a, **k: {"params": {}, "units": {}})
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case, **kw: Observation())
    s = ToolSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R3"])  # wedge_worker
    assert obs.blocked is None
    # the reset ran, and answered, BEFORE the freeze signal -- never after
    assert order == ["darkroom_reset", "SIGSTOP"]  # (H2) no bare SIGCONT any more
    assert s.touched == set()  # isolate cleared it before arm ever ran


def test_r3_real_run_tool_case_runs_its_step_after_the_freeze(tmp_path, monkeypatch, kills):
    """Fix round 1 review B1 (blocker): the reviewer's repro. Fix B's pre-case
    ping and Fix C's pre-arm isolate combined to block r3/r17 as
    `inherited_wedge` with zero calls -- `run_tool_case`'s OWN ping (unlike
    the pre-arm isolate) ran AFTER `arm`'s SIGSTOP, against the darktable the
    fault had just frozen ON PURPOSE. Uses the real `run_fault_case` +
    `run_tool_case` (not monkeypatched away, unlike every Fix A/C test above --
    that mock is exactly how this got through review round 1)."""
    from tests.gauntlet import observe

    state = {"stopped": False}

    class Dt:
        def health(self):
            return "unresponsive" if state["stopped"] else "alive"

    def recording_kill(sig=signal.SIGKILL):
        if sig == signal.SIGSTOP:
            state["stopped"] = True
        if sig == signal.SIGCONT:
            state["stopped"] = False

    class S(FakeSession):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self.dt = Dt()

        def call(self, tool, args, budget_s, *, write=False):
            self.tools.append(tool)
            if state["stopped"]:
                rec = CallRecord(tool, args, {"ok": False,
                                              "error": {"code": "darktable_unresponsive"}})
                rec.error = "darktable_unresponsive"
                return rec
            return CallRecord(tool, args, {"ok": True})

    monkeypatch.setattr(faults, "_owned_kill", recording_kill)
    monkeypatch.setattr(observe, "snapshot", lambda *a, **kw: {"params": {}, "units": {}})
    s = S(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R3"])  # wedge_worker
    assert "darkroom_say" in s.tools, f"step never ran: blocked={obs.blocked!r} tools={s.tools!r}"


def test_r3_restage_is_reopened_before_the_freeze(tmp_path, monkeypatch, kills):
    """Fix round 1 review M3 (medium): when the pre-arm isolate restages
    (returns True), the fresh frame must be re-opened BEFORE arm's SIGSTOP
    freezes it -- `refresh_frame` leaves `current_frame = None` until a real
    follow-up open succeeds (`session.py`'s own docstring). Skip that
    re-open and arm's freeze lands on a session that is not actually
    current; `run_tool_case`'s own `ensure_frame` then sends the real open
    to the now-frozen process, times out, and relaunches -- silently
    dropping BOTH the fault and `contaminated_restaged`."""
    order: list[str] = []

    class ToolSession(FakeSession):
        def ensure_frame(self, role):
            order.append("ensure_frame")
            return self.staged.frames[role]

    def recording_kill(sig=signal.SIGKILL):
        order.append({signal.SIGSTOP: "SIGSTOP", signal.SIGCONT: "SIGCONT"}.get(sig, "kill"))

    monkeypatch.setattr(faults, "isolate", lambda session, frame, role: True)
    monkeypatch.setattr(faults, "_owned_kill", recording_kill)
    monkeypatch.setattr(execute, "run_tool_case", lambda session, case, **kw: Observation())
    s = ToolSession(tmp_path)
    obs = faults.run_fault_case(s, _cases()["R3"])  # wedge_worker
    assert order == ["ensure_frame", "ensure_frame", "SIGSTOP"]  # (H2) no bare SIGCONT
    assert obs.extra.get("contaminated_restaged") is True


def test_fence_and_recover_runs_in_the_fenced_order(tmp_path):
    """Fix round 1 review H2 (high): one fenced helper, always in this exact
    order: SIGKILL the old MCP server, quit darktable, purge the spool,
    launch fresh, open a fresh client. A swapped order (e.g. purge before
    quit, or launch before purge) fails this test -- the previous branch's
    Fix A tests only asserted `relaunches == 1`/`mcp_reopened == 1`, which a
    swapped order still satisfies (review finding: "swapping the order
    leaves 3 of the 4 Fix A tests green")."""
    order: list[str] = []

    class Proc:
        def kill(self):
            order.append("mcp_kill")

    class Mcp:
        proc = Proc()

    class S:
        mcp = Mcp()
        current_frame = None
        staged = SimpleNamespace(frames={"hero": Path("/hero.ORF")})

        def _quit(self, why):
            order.append("dt_quit")

        def purge_spool(self):
            order.append("spool_purge")

        def _launch(self, frame):
            order.append("dt_launch")

        def reopen_mcp(self):
            order.append("mcp_open")

    errors = faults.fence_and_recover(S())
    assert errors == []
    assert order == ["mcp_kill", "dt_quit", "spool_purge", "dt_launch", "mcp_open"]


def test_fence_and_recover_purges_a_stale_request_before_launch(tmp_path):
    """Fix round 1 review H2 (high): a `request-*.json` the killed MCP server
    left behind must be gone before the fresh darktable launches, not merely
    eventually swept by the plugin's own 600s stale-request sweep -- the
    triage's r17 ghost edit is exactly this file surviving a relaunch."""
    from tests.gauntlet.session import ScouterSession

    cache = tmp_path / "cache"
    spool = cache / "darktable-mcp"
    spool.mkdir(parents=True)
    stale_request = spool / "request-42.json"
    stale_request.write_text("{}")
    stale_tmp = spool / "response-42.json.tmp"
    stale_tmp.write_text("{}")
    order: list[str] = []

    class S:
        mcp = None
        current_frame = None
        staged = SimpleNamespace(env={"XDG_CACHE_HOME": str(cache)},
                                 frames={"hero": Path("/hero.ORF")})

        def _quit(self, why):
            order.append("dt_quit")

        def purge_spool(self):
            order.append("spool_purge")
            ScouterSession.purge_spool(self)

        def _launch(self, frame):
            order.append("dt_launch")
            # the purge must already have run by the time darktable launches
            assert not stale_request.exists() and not stale_tmp.exists()

    errors = faults.fence_and_recover(S())
    assert errors == []
    assert order == ["dt_quit", "spool_purge", "dt_launch"]
    assert not stale_request.exists() and not stale_tmp.exists()


def test_r18_attach_timeout_detaches_the_image(tmp_path, monkeypatch):
    import plistlib
    import subprocess

    img = tmp_path / "full.dmg"
    log: list = []
    info = plistlib.dumps({"images": [
        {"image-path": "/elsewhere/other.dmg",
         "system-entities": [{"dev-entry": "/dev/disk5"}]},
        {"image-path": str(img), "system-entities": [
            {"dev-entry": "/dev/disk99s1", "mount-point": "/Volumes/HTCFULL"},
            {"dev-entry": "/dev/disk99"}]}]}).decode()

    def fake(*args, timeout=60):
        log.append(args)
        if args[0] == "attach":
            raise subprocess.TimeoutExpired(["hdiutil", *args], timeout)
        return SimpleNamespace(stdout=info if args[0] == "info" else "", returncode=0)
    monkeypatch.setattr(faults, "_hdiutil", fake)
    obs = faults.run_fault_case(FakeSession(tmp_path), _cases()["R18"])
    assert obs.blocked.startswith("harness_bug: TimeoutExpired")
    assert ("detach", "/dev/disk99", "-force") in log
    assert not any(a[:2] == ("detach", "/dev/disk5") for a in log)


@pytest.mark.parametrize("rid", ["R4", "H1"])  # relaunch_stale_job, cold_launch_commit
def test_relaunch_faults_retry_relaunch_in_disarm(tmp_path, rid):
    s = FakeSession(tmp_path, relaunch_error=SessionBlocked("relaunch: did not quit"))
    obs = faults.run_fault_case(s, _cases()[rid])
    assert obs.blocked == "session: relaunch: did not quit"
    assert s.relaunches == 2  # arm's attempt + disarm's retry
    assert obs.extra["disarm_errors"] == ["relaunch: SessionBlocked: relaunch: did not quit"]


@pytest.mark.parametrize("env,ok", [
    ({"ok": True, "outcome": "ok", "data": {"healed": True}}, True),   # registry shape
    ({"ok": True, "outcome": "ok", "data": {}}, False),                # no heal
    ({"ok": False, "error": {"code": "darktable_unresponsive",
                             "detail": {"healed": True}}}, False),     # healed, no retry
])
def test_watchdog_auto_heal_reads_healed_where_the_registry_puts_it(tmp_path, env, ok):
    """(final fix I4): the registry reports a heal in data.healed (Envelope is
    frozen; there is no top-level `healed`)."""
    s, _, step = _wd(tmp_path, '{"watchdog": {}}')
    s.call = lambda tool, args, budget_s, *, write=False: CallRecord(tool, args, env)
    obs = Observation()
    faults.watchdog_probe(s, None, step, obs)
    assert obs.extra["scenario_ok"] is ok


def test_healed_helper_reads_data_and_error_detail():
    assert faults.healed({"data": {"healed": True}})
    assert faults.healed({"ok": False, "error": {"detail": {"healed": True}}})
    assert not faults.healed({"ok": True, "data": {}}) and not faults.healed(None)
