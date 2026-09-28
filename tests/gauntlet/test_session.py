"""Session + tool-mode execution against fakes: no darktable, no MCP server."""


import time

import pytest

from scouter.core.dtprocess import LaunchError
from tests.gauntlet import session as sess
from tests.gauntlet.corpus import Corpus


class FakeDt:
    def __init__(self, foreign=(), owned=None, ready=(True,), health="alive", terminate_ok=True):
        self._foreign = list(foreign)
        self._owned = owned
        self._ready = list(ready)
        #: a list/tuple `health` (fix B, night1-robustness-triage.md item 2):
        #: answers in order, then repeats its last value forever -- lets a
        #: test tell "healthy going in, wedged during the step" (a genuine
        #: hang) apart from "already wedged before the step" (inherited).
        self._health = list(health) if isinstance(health, (list, tuple)) else health
        self.launches = 0
        self.launched = []
        self.terminated = []
        self.killed = []
        self.terminate_ok = terminate_ok

    def foreign(self):
        return self._foreign

    def owned(self):
        return self._owned

    def launch(self, frame, library, log, **kw):
        #: (fix round 1, Opus finding 2): **kw for forward compatibility
        #: with fix/htc-hang-recovery's DtControl.launch(..., env=...) --
        #: a future merge must not break this fake over an unrelated kwarg.
        self.launches += 1
        self.launched.append(frame)

    def ready(self, timeout_s, *, stop_if=None):
        #: (fix round 2, Pratik's ruling 4): stop_if for forward
        #: compatibility with `DtControl.ready`'s new keyword; this plain
        #: fake never calls it (nothing here spans real time to poll
        #: during) -- `SlowLockDt` below does.
        return {"ready": self._ready.pop(0) if self._ready else True}

    def health(self):
        if isinstance(self._health, list):
            return self._health.pop(0) if len(self._health) > 1 else self._health[0]
        return self._health

    def kill_launched(self, rec):
        self.killed.append(rec)
        return True

    def terminate(self, *, responsive=None):
        self.terminated.append(responsive)
        if not self.terminate_ok:
            return {"ok": False, "how": "gone_or_reused", "pid": 777}
        self._owned = None
        return {"ok": True}


class FakeMcp:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def call_tool(self, tool, args, timeout):
        self.calls.append((tool, args))
        a = self.answers.get(tool, {"ok": True, "outcome": "ok", "data": {}})
        if isinstance(a, list):  # answered in order; the last one repeats
            a = a.pop(0) if len(a) > 1 else a[0]
        if isinstance(a, Exception):
            raise a
        return a

    def close(self):
        pass


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def _session(corpus, tmp_path, dt=None, mcp=None):
    s = sess.ScouterSession(corpus, tmp_path / "s1", "olympus", dt=dt or FakeDt(),
                            mcp_factory=lambda env, cwd: mcp or FakeMcp())
    return s


def test_refuses_a_darktable_it_did_not_launch(corpus, tmp_path):
    s = _session(corpus, tmp_path, FakeDt(foreign=[4242]))
    with pytest.raises(sess.ForeignDarktable):
        s.open()
    assert s.dt.terminated == [] and s.dt.launches == 0  # never signals a foreign darktable


def test_quits_its_own_leftover_darktable_from_a_killed_run(corpus, tmp_path):
    dt = FakeDt(owned=777)
    _session(corpus, tmp_path, dt).open()
    assert dt.terminated[0] is False and dt.launches == 1


def test_launch_retries_once_then_blocks(corpus, tmp_path):
    dt = FakeDt(ready=(False, True))
    _session(corpus, tmp_path, dt).open()
    assert dt.launches == 2
    with pytest.raises(sess.SessionBlocked):
        _session(corpus, tmp_path, FakeDt(ready=(False, False))).open()


def test_session_env_is_isolated(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUTER_DARKTABLE_LIBRARY_DB", "/elsewhere/library.db")
    monkeypatch.setenv("SCOUTER_CONFIG", "/elsewhere/config.json")
    s = _session(corpus, tmp_path).open()
    env = s.staged.env
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH",
              "SCOUTER_DARKTABLE_LIBRARY_DB"):
        assert env[k].startswith(str(tmp_path / "s1"))
    assert env["SCOUTER_DARKTABLE_LIBRARY_DB"] == str(s.staged.library)
    assert s.staged.frames["hero"].read_bytes() == b"raw"


class LaunchFailDt(FakeDt):
    """launch() raises dtprocess.LaunchError for the first `fails` attempts."""

    def __init__(self, fails, **kw):
        super().__init__(**kw)
        self.fails = fails

    def launch(self, frame, library, log, **kw):
        self.launches += 1
        if self.launches <= self.fails:
            raise LaunchError(f"could not read the start time of darktable pid {self.launches}")


def test_launch_error_is_a_failed_attempt(corpus, tmp_path):
    dt = LaunchFailDt(1)
    _session(corpus, tmp_path, dt).open()
    assert dt.launches == 2
    dt = LaunchFailDt(2)
    with pytest.raises(sess.SessionBlocked, match="LaunchError: could not read the start time"):
        _session(corpus, tmp_path, dt).open()
    assert dt.launches == 2


def test_stage_frames_under_role_blind_directories(tmp_path):
    """(row 501 item F, fix round 1, Opus finding 1, BLOCKER): the earlier
    fix (a symlink alias inside agent.py) broke darktable's own path
    matching and still leaked the real, role-named path through every MCP
    envelope's own `path` field. Staging every role under a hashed,
    role-blind directory in stage() itself removes the leak at its one
    source: the library, the envelopes and the agent's prompt all ever see
    this one path."""
    from tests.gauntlet.corpus import ROLES

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    roles = {r: "H.ORF" for r in ROLES}
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {"olympus": roles}}
    corpus = Corpus(doc, trial)
    staged = sess.stage(corpus, "olympus", tmp_path / "s1")
    for role, path in staged.frames.items():
        rel = str(path.relative_to(tmp_path / "s1"))
        for token in ROLES:
            assert token not in rel, (role, token, rel)
    # the library's own staged path is real and readable -- not a symlink,
    # not a copy under a second, hidden directory tree.
    assert staged.frames["warm_cast"].is_file() and not staged.frames["warm_cast"].is_symlink()
    assert staged.frames["warm_cast"].read_bytes() == b"raw"


class LockedDt(FakeDt):
    """launch() APPENDS a line to darktable.log on every call, as
    `dtprocess.launch` genuinely does (`open(log_path, "ab")`) -- across the
    whole session, not one log per launch. (Fix round 1, Opus finding 2:
    the first cut of this fake used `write_text`, which OVERWRITES on every
    call and so could never reproduce the sticky-log bug it was meant to
    catch.) `lines` is one string per call, in order; the last one repeats
    once exhausted. `**kw` for forward compatibility with
    fix/htc-hang-recovery's `DtControl.launch(..., env=...)`."""

    def __init__(self, *lines, **kw):
        super().__init__(**kw)
        self.lines = list(lines)

    def launch(self, frame, library, log, **kw):
        super().launch(frame, library, log, **kw)
        line = self.lines.pop(0) if len(self.lines) > 1 else (self.lines[0] if self.lines else "")
        if line:
            with log.open("a", encoding="utf-8") as f:
                f.write(line)


#: darktable's own real wording (fix round 1, Opus finding 2): "[init] the
#: database lock file contains a pid that seems to be alive in your
#: system: 69648". The first cut's test used invented wording ("contains a
#: pid 69648 that seems...") that happened to satisfy the old, too-narrow
#: regex -- never the real one.
REAL_LOCK_LINE = ("2026-09-26 13:45:06 [init] the database lock file contains a pid "
                  "that seems to be alive in your system: 69648\n")


def test_library_lock_in_the_log_stops_the_session_fast(corpus, tmp_path):
    """The session fails fast on the first attempt (never burns a second
    doomed launch) with a typed reason naming the offending pid, parsed
    from darktable's REAL wording."""
    dt = LockedDt(REAL_LOCK_LINE)
    with pytest.raises(sess.LibraryLocked, match="library_lock 69648"):
        _session(corpus, tmp_path, dt).open()
    assert dt.launches == 1
    assert dt._ready == []  # ready() ran once; the post-ready check caught the lock


def test_locked_launch_quits_before_raising_no_orphan(corpus, tmp_path):
    """A locked launch must not leave an owned darktable running."""
    dt = LockedDt(REAL_LOCK_LINE)
    with pytest.raises(sess.LibraryLocked):
        _session(corpus, tmp_path, dt).open()
    assert dt.terminated == [False]  # terminate(responsive=False), same as abort()'s own path


def test_sqlite_database_is_locked_line_is_also_caught(corpus, tmp_path):
    dt = LockedDt("sqlite3_step: database is locked\n")
    with pytest.raises(sess.LibraryLocked, match="library_lock unknown") as exc_info:
        _session(corpus, tmp_path, dt).open()
    assert "library_lock unknown" in str(exc_info.value)


def test_lock_is_not_sticky_a_clean_relaunch_after_a_locked_one_succeeds(corpus, tmp_path):
    """(fix round 1, Opus finding 2, the sticky-log bug, reproduced then
    fixed): darktable.log is append-mode across the whole session. A first
    launch that logs the real lock line must not poison a LATER, clean
    launch that reached gui_ready and appended nothing more -- only the
    bytes THIS launch appended are ever in scope. Probe: a first session
    logs the real lock line and fails; a second session against the SAME
    log file (append mode -- the old line is still sitting there) launches
    cleanly and must succeed, not inherit the first session's own lock."""
    dt = LockedDt(REAL_LOCK_LINE, "")  # session 1: locked; session 2: a clean launch
    s1 = _session(corpus, tmp_path, dt)
    with pytest.raises(sess.LibraryLocked):
        s1.open()
    s2 = _session(corpus, tmp_path, dt)  # same tmp_path -> same root -> same darktable.log
    s2.open()
    assert dt.launches == 2


def test_no_lock_line_launches_normally(corpus, tmp_path):
    """Control: a plain log (or no log at all, FakeDt's default) never
    trips the breaker."""
    dt = LockedDt("darktable started fine\n")
    _session(corpus, tmp_path, dt).open()
    assert dt.launches == 1


class SlowLockDt(FakeDt):
    """(row 501 item D, fix round 2, Pratik's ruling 4, Opus re-review
    finding 4): `launch()` starts the process WITHOUT writing the lock line
    -- exactly like real darktable, whose `dt_init`/database check (and so
    the lock-file error) lands well after `dtprocess.launch` itself
    returns, during what would otherwise be the 30 s `gui_ready` wait.
    `ready()` stands in for `dtprocess.wait_gui_ready`'s own poll loop: it
    writes the lock line partway through its own polling and calls
    `stop_if()` every poll, the same cadence the real fix wires through
    `session._launch` -> `DtControl.ready(stop_if=...)` ->
    `dtprocess.wait_gui_ready`. `poll_s` is real wall-clock (`time.sleep`)
    so a caller measuring elapsed time with `time.monotonic()` sees the
    genuine saving."""

    def __init__(self, lock_line: str, *, write_after_polls: int = 2, poll_s: float = 0.05,
                 **kw):
        super().__init__(**kw)
        self.lock_line = lock_line
        self.write_after_polls = write_after_polls
        self.poll_s = poll_s
        self._log_path = None
        self.ready_calls = 0

    def launch(self, frame, library, log, **kw):
        super().launch(frame, library, log, **kw)
        self._log_path = log

    def ready(self, timeout_s, *, stop_if=None):
        self.ready_calls += 1
        for i in range(int(timeout_s / self.poll_s) + 1):
            if i == self.write_after_polls and self._log_path is not None:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(self.lock_line)
            if stop_if is not None and stop_if():
                return {"ready": False, "stopped": True}
            time.sleep(self.poll_s)
        return {"ready": False}


def test_lock_line_written_during_the_ready_wait_is_caught_within_about_a_second(
        corpus, tmp_path):
    """RED (before the fix): the lock check only ran once before the ready
    wait and once after `dt.ready(LAUNCH_READY_S)` returns -- a lock line
    darktable writes only DURING that wait (the real, common timing: the
    process is running well before `dt_init` gets to the database check)
    was invisible until the full 30 s elapsed. GREEN: `stop_if` is checked
    once per ready poll (~poll_s apart), so a line written on the SECOND
    poll is caught almost immediately -- proven here by a real elapsed-time
    bound, not a mocked clock, against a fake whose own `poll_s` (0.05 s)
    stands in for `wait_gui_ready`'s real ~0.5 s cadence."""
    dt = SlowLockDt(REAL_LOCK_LINE, write_after_polls=2, poll_s=0.05)
    t0 = time.monotonic()
    with pytest.raises(sess.LibraryLocked, match="library_lock 69648"):
        _session(corpus, tmp_path, dt).open()
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, elapsed
    assert dt.ready_calls == 1  # one ready() call; stop_if caught it mid-poll, no retry
    assert dt.launches == 1  # no second, doomed launch attempt either


def test_dtcontrol_ready_passes_stop_if_through_to_wait_gui_ready(monkeypatch):
    """`_launch`'s early lock catch only works if `DtControl.ready` actually
    forwards its `stop_if` to `dtprocess.wait_gui_ready` -- nothing else
    exercises that one link (`SlowLockDt` fakes `ready()` itself)."""
    from scouter.core import dtprocess

    seen = {}
    monkeypatch.setattr(dtprocess, "wait_gui_ready",
                        lambda timeout_s, *, stop_if=None: seen.update(
                            timeout_s=timeout_s, stop_if=stop_if) or {"ready": True})
    marker = lambda: False  # noqa: E731 - identity is the whole point here
    sess.DtControl().ready(12.0, stop_if=marker)
    assert seen == {"timeout_s": 12.0, "stop_if": marker}


def test_stage_job_imports_the_staged_card_into_the_session_slug(corpus, tmp_path):
    """(row 501 item I): a real library_import of the staged card into the
    session's own job slug, so library_jobs/export_release find a real job
    (night-1 rank 11: r0066's agent found library_jobs empty)."""
    mcp = FakeMcp({"library_import": {"ok": True, "outcome": "ok", "data": {}}})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.stage_job()
    assert s.job_stage_error is None
    tool, args = mcp.calls[-1]
    assert tool == "library_import"
    assert args["job_slug"] == s.staged.vars["job_slug"]
    assert args["source"] == str(s.staged.card)


def test_stage_job_records_a_failed_import_without_raising(corpus, tmp_path):
    mcp = FakeMcp({"library_import": {"ok": False, "outcome": "error",
                                      "error": {"code": "no_frames"}}})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.stage_job()  # never raises
    assert s.job_stage_error == "no_frames"


def test_refreshing_a_role_twice_never_reuses_a_contaminated_dir(corpus, tmp_path):
    s = _session(corpus, tmp_path).open()
    first = s.refresh_frame("dark")
    (first.parent / (first.name + ".xmp")).write_text("contaminated history")
    second = s.refresh_frame("dark")
    assert first.parent != second.parent
    assert list(second.parent.glob("*.xmp")) == []
    assert second.read_bytes() == b"raw" and s.staged.frames["dark"] == second


def test_refresh_frame_is_not_current_until_the_next_ensure_frame_reopens_it(corpus, tmp_path):
    """Night-1 fix (class LK-refresh-no-open): before the fix, `refresh_frame`'s
    `relaunch` -> `_launch` marked the fresh copy `current_frame` the moment
    darktable was gui_ready with it, so the very next `ensure_frame(role)`
    saw `current_frame == staged.frames[role]` and skipped `darkroom_open`
    entirely (RED: zero open calls after a refresh). The job then never
    recorded the fresh image as opened, and every later write refused
    "darkroom.current reports N+1, job opened N". Fixed: refresh leaves the
    frame merely launched, and the next ensure_frame call opens it for real
    (GREEN: exactly one open call, and current_frame only updates on its
    success)."""
    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.ensure_frame("dark")
    mcp.calls.clear()

    fresh = s.refresh_frame("dark")
    assert s.current_frame != fresh  # launched, not yet current

    assert s.ensure_frame("dark") == fresh
    assert [c[0] for c in mcp.calls] == ["darkroom_open"]  # exactly one open, not zero
    assert s.current_frame == fresh  # current only now, after the open succeeded


def test_open_failure_after_launch_quits_our_darktable(corpus, tmp_path):
    dt = FakeDt()

    def boom(env, cwd):
        raise RuntimeError("mcp server did not start")

    s = sess.ScouterSession(corpus, tmp_path / "s1", "olympus", dt=dt, mcp_factory=boom)
    with pytest.raises(RuntimeError, match="mcp server did not start"):
        s.open()
    assert dt.launches == 1 and dt.terminated == [False]
    s2 = sess.ScouterSession(corpus, tmp_path / "s2", "olympus", dt=FakeDt(),
                             mcp_factory=lambda env, cwd: FakeMcp(),
                             floors_fn=lambda frame: ({}, False))
    with pytest.raises(sess.SessionBlocked, match="harness_bug"):
        s2.open()
    assert s2.dt.terminated == [False] and s2.mcp is None


def test_ensure_frame_sets_current_only_on_success(corpus, tmp_path):
    s = _session(corpus, tmp_path).open()
    assert s.ensure_frame("dark") == s.staged.frames["dark"]
    assert s.current_frame == s.staged.frames["dark"]

    bad = FakeMcp({"darkroom_open": {"ok": False, "outcome": "error",
                                     "error": {"code": "not_found"}, "data": {}}})
    s = _session(corpus, tmp_path, mcp=bad).open()
    hero = s.current_frame
    with pytest.raises(sess.SessionBlocked, match="frame dark"):
        s.ensure_frame("dark")
    assert s.current_frame == hero

    slow = FakeMcp({"darkroom_open": TimeoutError("slow")})
    s = _session(corpus, tmp_path, dt=FakeDt(health="alive"), mcp=slow).open()
    with pytest.raises(sess.SessionBlocked, match="timeout"):
        s.ensure_frame("dark")
    assert s.current_frame == hero

    # (night-1 fix, sibling gap): a relaunch recovers darktable, but the job
    # only records the frame as opened through a REAL follow-up darkroom_open
    # -- the fresh process being gui_ready is not that call. slow_then_ok's
    # first darkroom_open times out (unresponsive); the one after relaunch()
    # succeeds, same shape as the not_in_darkroom recovery below.
    ok = {"ok": True, "outcome": "ok", "data": {}}
    slow_then_ok = FakeMcp({"darkroom_open": [TimeoutError("slow"), ok]})
    dt = FakeDt(health="unresponsive")
    s = _session(corpus, tmp_path, dt=dt, mcp=slow_then_ok).open()
    assert s.ensure_frame("dark") == s.staged.frames["dark"]
    assert dt.launches == 2 and s.current_frame == s.staged.frames["dark"]
    assert [c[0] for c in slow_then_ok.calls] == ["darkroom_open", "darkroom_open"]


WRONG_IMAGE = {"ok": False, "outcome": "error", "data": {},
               "error": {"code": "not_in_darkroom", "detail": {"method": "darkroom.open"},
                         "message": "handler raised: darktable_mcp.lua:1586: wrong image "
                                    "loaded: asked for 2, darkroom shows 1 after 15000ms"}}


def test_smoke_s2_frame_imported_mid_session_relaunches_on_it(corpus, tmp_path):
    """Smoke S2: darktable cannot load a frame imported into the SAME running
    session (product honest limit R20: 'wrong image loaded' -> not_in_darkroom).
    ensure_frame relaunches darktable on that frame, then re-opens it once."""
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp({"darkroom_open": [WRONG_IMAGE, ok]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt=dt, mcp=mcp).open()
    dark = s.staged.frames["dark"]
    assert s.ensure_frame("dark") == dark
    assert dt.launched == [s.staged.frames["hero"], dark] and s.current_frame == dark
    assert dt.terminated == [False]  # the quit ladder ran before the relaunch
    assert [c[0] for c in mcp.calls] == ["darkroom_open", "darkroom_open"]


def test_smoke_s2_relaunches_at_most_once_then_blocks(corpus, tmp_path):
    mcp = FakeMcp({"darkroom_open": [WRONG_IMAGE]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt=dt, mcp=mcp).open()
    with pytest.raises(sess.SessionBlocked, match="frame dark .*after a relaunch"):
        s.ensure_frame("dark")
    assert dt.launches == 2 and len(mcp.calls) == 2


def test_timeout_recovery_relaunch_reopens_before_marking_current(corpus, tmp_path):
    """Night-1 fix sibling gap (reviewer, n1-session-review.md): the
    timeout/unresponsive recovery branch relaunched and, before the fix,
    trusted `_launch`'s direct `current_frame` assignment as the sign the job
    had opened the frame -- exactly refresh_frame's bug, one call site over.
    A relaunch here must also stay `launched`, not `current`, until a real
    follow-up `darkroom_open` succeeds (same `_relaunch_then_reopen` helper
    the not_in_darkroom recovery above already used)."""
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp({"darkroom_open": [TimeoutError("slow"), ok]})
    dt = FakeDt(health="unresponsive")
    s = _session(corpus, tmp_path, dt=dt, mcp=mcp).open()
    dark = s.staged.frames["dark"]
    assert s.ensure_frame("dark") == dark
    assert dt.launched == [s.staged.frames["hero"], dark] and s.current_frame == dark
    assert dt.terminated == [False]  # the quit ladder ran before the relaunch
    assert [c[0] for c in mcp.calls] == ["darkroom_open", "darkroom_open"]  # not just one


def test_timeout_recovery_relaunches_at_most_once_then_blocks(corpus, tmp_path):
    """The follow-up-open failure path mirrors test_smoke_s2_relaunches_at_
    most_once_then_blocks: a relaunch that still cannot get a confirmed open
    blocks the session rather than silently trusting the launch."""
    mcp = FakeMcp({"darkroom_open": [TimeoutError("slow"), TimeoutError("slow again")]})
    dt = FakeDt(health="unresponsive")
    s = _session(corpus, tmp_path, dt=dt, mcp=mcp).open()
    with pytest.raises(sess.SessionBlocked, match="frame dark .*after a relaunch"):
        s.ensure_frame("dark")
    assert dt.launches == 2 and len(mcp.calls) == 2


BRIDGE_TIMEOUT = {"ok": False, "outcome": "error:bridge_timeout", "data": {},
                  "error": {"code": "bridge_timeout",
                            "message": "never left lighttable after 20s"}}
COMMIT_TIMEOUT = {"ok": False, "outcome": "error:commit_timeout", "data": {},
                  "error": {"code": "commit_timeout", "message": "no answer"}}


@pytest.mark.parametrize("first_error", [BRIDGE_TIMEOUT, COMMIT_TIMEOUT])
def test_bridge_and_commit_timeout_relaunch_once_then_recover(corpus, tmp_path, first_error):
    """Batch 3 / night-2 triage C2 (row 462): `darkroom_open` answering
    `bridge_timeout` or `commit_timeout` must relaunch darktable and reopen
    the frame, the same recovery `not_in_darkroom` already gets -- one
    relaunch, then a real follow-up open, before the harness trusts the
    frame as current (sca-r0401-olympus-agent, scn-r0318-sony_a7-tool)."""
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp({"darkroom_open": [first_error, ok]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt, mcp=mcp).open()
    dark = s.staged.frames["dark"]
    # Oracle A: the harness's own belief about what is open.
    assert s.ensure_frame("dark") == dark and s.current_frame == dark
    # Oracle B: independently, exactly one relaunch and two open calls
    # reached the fake transport/process -- the recovery, not a retry loop.
    assert dt.launches == 2
    assert [c[0] for c in mcp.calls] == ["darkroom_open", "darkroom_open"]


@pytest.mark.parametrize("code", ["bridge_timeout", "commit_timeout"])
def test_bridge_and_commit_timeout_relaunches_at_most_once_then_marks_failed(corpus, tmp_path,
                                                                             code):
    """A second bridge_timeout/commit_timeout after the one bounded relaunch
    must not retry again -- it blocks the case, tagged
    `bridge_relaunch_failed` so triage can tell this apart from a plain
    SessionBlocked, and the runner never launches a third darktable for it."""
    err = {"ok": False, "outcome": f"error:{code}", "data": {}, "error": {"code": code}}
    mcp = FakeMcp({"darkroom_open": [err, err]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt, mcp=mcp).open()
    with pytest.raises(sess.SessionBlocked, match="bridge_relaunch_failed.*frame dark"):
        s.ensure_frame("dark")
    # Oracle B: exactly one relaunch was attempted (bounded), not a loop.
    assert dt.launches == 2 and len(mcp.calls) == 2


def test_never_launches_a_second_darktable_when_quit_fails(corpus, tmp_path):
    dt = FakeDt(owned=777, terminate_ok=False)
    with pytest.raises(sess.SessionBlocked, match="did not quit"):
        _session(corpus, tmp_path, dt).open()
    assert dt.launches == 0

    dt = FakeDt(ready=(False, True), terminate_ok=False)
    with pytest.raises(sess.SessionBlocked, match="did not quit"):
        _session(corpus, tmp_path, dt).open()
    assert dt.launches == 1

    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    dt.terminate_ok = False
    with pytest.raises(sess.SessionBlocked, match="relaunch: darktable did not quit"):
        s.relaunch()
    assert dt.launches == 1


# -- final fix I3: MCP-server deaths are crashes -----------------------------

class _ExitedProc:
    def __init__(self, rc):
        self.rc = rc

    def poll(self):
        return self.rc


class DeadMcp(FakeMcp):
    """An MCP server whose process has exited: every call is a transport error."""

    def __init__(self, rc=1):
        super().__init__()
        self.proc = _ExitedProc(rc)

    def call_tool(self, tool, args, timeout):
        raise BrokenPipeError("[Errno 32] Broken pipe")


def test_a_dead_mcp_server_is_recorded_and_marks_the_session_for_reopen(corpus, tmp_path):
    s = _session(corpus, tmp_path, mcp=DeadMcp(rc=-11)).open()
    rec = s.call("darkroom_set", {"module": "exposure"}, 3.5, write=True)
    assert rec.error.startswith("transport:")
    assert s.mcp_deaths == ["mcp_server_death: exit -11"] and s.mcp_dead
    assert s.take_mcp_deaths() == ["mcp_server_death: exit -11"] and s.mcp_deaths == []


def test_a_live_mcp_transport_error_is_not_a_death(corpus, tmp_path):
    mcp = DeadMcp()
    mcp.proc = _ExitedProc(None)  # still running
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.call("darkroom_set", {"module": "exposure"}, 3.5, write=True)
    assert s.mcp_deaths == [] and not s.mcp_dead


def test_close_quits_darktable_even_when_the_mcp_close_raises(corpus, tmp_path):
    """(final fix M2)."""
    class BadCloseMcp(FakeMcp):
        def close(self):
            raise BrokenPipeError("mcp stdin gone")

    dt = FakeDt()
    s = _session(corpus, tmp_path, dt, mcp=BadCloseMcp()).open()
    with pytest.raises(BrokenPipeError):
        s.close()
    assert dt.terminated == [None] and s.mcp is None


# -- n1-bulk fix: stage()'s default job_slug must be a valid slug too -------

def test_stage_job_slug_is_valid_for_a_body_with_an_underscore(tmp_path):
    """RED: the old `f"htc-{body}-{stamp}"[-40:]` kept `sony_a7`'s underscore
    (`SLUG_RE` forbids it) and sliced from the LEFT; `library.import` would
    refuse it with `usage: invalid job slug`, the same class-6 bug
    `tests/gauntlet/exec_bulk.py` had. GREEN: `stage()` now builds it with
    the shared `job_slug_for`, same as the bulk executor."""
    from scouter import paths as scouter_paths

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "sony_a7"}],
           "roles": {"sony_a7": {"hero": "H.ORF", "dark": "H.ORF"}}}
    corpus = Corpus(doc, trial)

    staged = sess.stage(corpus, "sony_a7", tmp_path / "run")

    assert scouter_paths.validate_slug(staged.vars["job_slug"]) == staged.vars["job_slug"]


# ---------------------------------------------------------------- fix round
# 2 (review R1): per-case undo journal isolation.


def test_reset_journal_truncates_the_real_journal_file(corpus, tmp_path, monkeypatch):
    """Real `scouter.core.journal`, not a mock of the subject: an entry a
    PRIOR case would have left open (a `darkroom_reset`, which `undo_last`
    can never revert) must be gone before the NEXT case's own steps run, so
    that case's `darkroom_undo_last` sees a genuinely empty journal and
    honestly answers `noop`, never `unsupported_undo` for something it
    never touched."""
    from scouter import paths
    from scouter.core import journal

    s = _session(corpus, tmp_path).open()
    job = s.staged.env["SCOUTER_JOB"]
    monkeypatch.setenv("SCOUTER_CACHE", s.staged.env["SCOUTER_CACHE"])
    journal.begin(job, verb="module.reset", image_id=1, path="x", module="temperature",
                  param="", instance=0, before={})
    assert journal.latest_open(job) is not None
    assert paths.journal_path(job) == s.staged.cache / "journal" / f"{job}.jsonl"

    sess.reset_journal(s.staged)

    assert journal.read_all(job) == []
    assert journal.latest_open(job) is None
    assert journal.latest_failed(job) is None
    # A no-op when there is no journal file yet at all (the very first case
    # of a session) -- never raises.
    (s.staged.cache / "journal" / f"{job}.jsonl").unlink()
    sess.reset_journal(s.staged)


def test_run_tool_case_resets_the_journal_before_its_own_steps(corpus, tmp_path):
    """execute.run_tool_case must isolate the journal for every case, not
    only ones with an explicit undo step -- an earlier case's leaked entry
    must never survive into ANY later case."""
    from gauntlet.case import Case, Expect, Step
    from tests.gauntlet import execute, observe

    s = _session(corpus, tmp_path).open()
    job = s.staged.env["SCOUTER_JOB"]
    journal_file = s.staged.cache / "journal" / f"{job}.jsonl"
    journal_file.parent.mkdir(parents=True, exist_ok=True)
    journal_file.write_text('{"seq": 1, "ts": "x", "verb": "module.reset", "image_id": 1, '
                            '"path": "x", "module": "temperature", "param": "", '
                            '"instance": 0, "before": {}}\n', encoding="utf-8")
    case = Case(id="c", layer="coverage", tier="P0", mode="tool",
               steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
               expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert journal_file.read_text(encoding="utf-8") == ""
    assert obs.extra["journal_reset_entries"] == 1


class JournalAwareFakeMcp(FakeMcp):
    """Fix round 3 (re-review R1): a fake MCP whose `darkroom_reset` calls
    ALSO write a real `module.reset` journal entry -- close enough to the
    real registry's behavior to test the ORDER `execute.run_tool_case` runs
    `isolate()` and `session.reset_journal` in, without any darktable.
    `darkroom_undo_last` answers from the REAL journal file's own state at
    call time (`noop` when empty, `unsupported_undo` when an open
    `module.reset` entry is still there) -- exactly the two outcomes the
    ordering bug/fix actually distinguish."""

    def __init__(self, job: str, cache, answers=None):
        super().__init__(answers)
        self.job = job
        self.cache = cache

    def _journal_path(self):
        return self.cache / "journal" / f"{self.job}.jsonl"

    def call_tool(self, tool, args, timeout):
        if tool == "darkroom_reset":
            import json as _json

            path = self._journal_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"seq": 1, "ts": "x", "verb": "module.reset", "image_id": 1, "path": "x",
                    "module": args.get("module", "temperature"), "param": "", "instance": 0,
                    "before": {}, "after": None, "undone": False, "outcome": None,
                    "commit": True, "note": None, "group": None}
            with open(path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry) + "\n")
            return {"ok": True, "outcome": "ok", "data": {}}
        if tool == "darkroom_undo_last":
            path = self._journal_path()
            open_entries = [ln for ln in (path.read_text(encoding="utf-8").splitlines()
                                          if path.exists() else []) if ln.strip()]
            if open_entries:
                return {"ok": False, "outcome": "unsupported_undo", "error": {
                    "code": "unsupported_undo",
                    "message": "cannot undo the reset of temperature: its parameters were "
                              "discarded", "fix": "darktable's own Ctrl-Z is the only way back"}}
            return {"ok": False, "outcome": "noop", "error": {
                "code": "noop", "message": "nothing to undo: journal is empty", "fix": ""}}
        return super().call_tool(tool, args, timeout)


def test_undo_after_the_previous_case_left_a_module_reset_answers_noop_not_unsupported_undo(
        corpus, tmp_path, monkeypatch):
    """Fix round 3 (re-review R1): reset_journal must run AFTER isolate(),
    not before it. `isolate()` resets every module a PREVIOUS case touched
    (`session.touched`), and that reset itself writes a `module.reset`
    journal entry -- if the journal were cleared BEFORE isolate() runs (the
    round-2 ordering), that fresh entry would still be sitting there when
    THIS case's own `darkroom_undo_last` runs, wrongly answering
    `unsupported_undo` for a reset this case never asked for. RED on
    cd0cb5b (round-2's ordering): this test fails there because
    reset_journal ran before isolate()."""
    from gauntlet.case import Case, Expect, Step
    from tests.gauntlet import execute, observe

    mcp = JournalAwareFakeMcp(job="", cache=tmp_path)  # patched below, post-open
    s = _session(corpus, tmp_path, mcp=mcp).open()
    mcp.job = s.staged.env["SCOUTER_JOB"]
    mcp.cache = s.staged.cache
    # Simulate: the PREVIOUS case touched (wrote to) the temperature module,
    # so this case's isolate() call resets it first.
    s.touched = {"temperature"}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "a", "params": {}, "units": {}})

    case = Case(id="c", layer="coverage", tier="P0", mode="tool",
               steps=[Step("darkroom_undo_last", {}, 3.5, True)],
               expect=Expect("typed_error", codes=["noop"]), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)

    assert obs.calls[-1].envelope["outcome"] == "noop", obs.calls[-1].envelope
    assert obs.extra["journal_reset_entries"] == 1  # isolate's own module.reset entry, cleared
