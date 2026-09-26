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


def test_refreshing_a_role_twice_never_reuses_a_contaminated_dir(corpus, tmp_path):
    s = _session(corpus, tmp_path).open()
    first = s.refresh_frame("dark")
    (first.parent / (first.name + ".xmp")).write_text("contaminated history")
    second = s.refresh_frame("dark")
    assert first.parent != second.parent
    assert list(second.parent.glob("*.xmp")) == []
    assert second.read_bytes() == b"raw" and s.staged.frames["dark"] == second


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

    dt = FakeDt(health="unresponsive")
    s = _session(corpus, tmp_path, dt=dt, mcp=slow).open()
    assert s.ensure_frame("dark") == s.staged.frames["dark"]
    assert dt.launches == 2 and s.current_frame == s.staged.frames["dark"]


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
