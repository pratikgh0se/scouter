"""Session + tool-mode execution against fakes: no darktable, no MCP server."""


import pytest

from scouter.core.dtprocess import LaunchError
from tests.gauntlet import session as sess
from tests.gauntlet.corpus import Corpus


class FakeDt:
    def __init__(self, foreign=(), owned=None, ready=(True,), health="alive"):
        self._foreign = list(foreign)
        self._owned = owned
        self._ready = list(ready)
        self._health = health
        self.launches = 0
        self.terminated = []

    def foreign(self):
        return self._foreign

    def owned(self):
        return self._owned

    def launch(self, frame, library, log):
        self.launches += 1

    def ready(self, timeout_s):
        return {"ready": self._ready.pop(0) if self._ready else True}

    def health(self):
        return self._health

    def terminate(self, *, responsive=None):
        self.terminated.append(responsive)
        self._owned = None
        return {"ok": True}


class FakeMcp:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def call_tool(self, tool, args, timeout):
        self.calls.append((tool, args))
        a = self.answers.get(tool, {"ok": True, "outcome": "ok", "data": {}})
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


def test_session_env_is_isolated(corpus, tmp_path):
    s = _session(corpus, tmp_path).open()
    env = s.staged.env
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH"):
        assert env[k].startswith(str(tmp_path / "s1"))
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
