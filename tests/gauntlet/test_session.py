"""Session + tool-mode execution against fakes: no darktable, no MCP server."""


import pytest

from scouter.core.dtprocess import LaunchError
from tests.gauntlet import session as sess
from tests.gauntlet.corpus import Corpus


class FakeDt:
    def __init__(self, foreign=(), owned=None, ready=(True,), health="alive", terminate_ok=True):
        self._foreign = list(foreign)
        self._owned = owned
        self._ready = list(ready)
        self._health = health
        self.launches = 0
        self.launched = []
        self.terminated = []
        self.killed = []
        self.terminate_ok = terminate_ok

    def foreign(self):
        return self._foreign

    def owned(self):
        return self._owned

    def launch(self, frame, library, log):
        self.launches += 1
        self.launched.append(frame)

    def ready(self, timeout_s):
        return {"ready": self._ready.pop(0) if self._ready else True}

    def health(self):
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

    def launch(self, frame, library, log):
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


class LockedDt(FakeDt):
    """launch() writes a darktable.log with the lock line, as darktable
    itself would when the library db is wedged by an orphaned process."""

    def __init__(self, line, **kw):
        super().__init__(**kw)
        self.line = line

    def launch(self, frame, library, log):
        super().launch(frame, library, log)
        log.write_text(self.line)


def test_library_lock_in_the_log_stops_the_session_fast(corpus, tmp_path):
    """(row 501 item D, night-1 rank 9, SCA-no-lock-breaker): the session
    fails fast on the first attempt (never burns a second doomed launch)
    with a typed reason naming the offending pid."""
    dt = LockedDt("2026-09-26 13:45:06 [dtprocess] the database lock file "
                  "contains a pid 69648 that seems to be alive\n")
    with pytest.raises(sess.LibraryLocked, match="library_lock 69648"):
        _session(corpus, tmp_path, dt).open()
    assert dt.launches == 1


def test_sqlite_database_is_locked_line_is_also_caught(corpus, tmp_path):
    dt = LockedDt("sqlite3_step: database is locked\n")
    with pytest.raises(sess.LibraryLocked, match="library_lock unknown"):
        _session(corpus, tmp_path, dt).open()


def test_no_lock_line_launches_normally(corpus, tmp_path):
    """Control: a plain log (or no log at all, FakeDt's default) never
    trips the breaker."""
    dt = LockedDt("darktable started fine\n")
    _session(corpus, tmp_path, dt).open()
    assert dt.launches == 1


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
