import json
import subprocess

import pytest

from gauntlet.adapter import Observation
from gauntlet.case import Case, Expect, Step
from tests.gauntlet import exec_parity
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeMcp, _session


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def test_normalise_drops_volatile_fields():
    a = {"ok": True, "outcome": "ok", "ms": 12, "job": "j1", "data": {"k": 1, "path": "/a"}}
    b = {"ok": True, "outcome": "ok", "ms": 99, "job": "j2", "data": {"k": 1, "path": "/b"}}
    assert exec_parity.compare(a, b) == []
    c = dict(b, data={"k": 2})
    assert exec_parity.compare(a, c) == ["values"]


def test_cli_argv_follows_the_registry():
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": True})
    assert argv[:2] == ["darkroom", "set"] and "--no-commit" not in argv
    argv = exec_parity.cli_argv("darkroom.say", {"phrase": "brighter", "dry_run": True})
    assert "--dry-run" in argv and "brighter" in argv


def test_cli_argv_never_emits_the_nonexistent_commit_flag():
    # PAR-set-commit-flag (triage rank 10): `commit=True` (SMOKE_ARGS's own
    # darkroom.set happy/error args) used to map to `--commit`, a flag the CLI
    # never defines (only `--no-commit` exists, for a default-True commit) --
    # argparse rejected it, failing both P0 darkroom.set pairs. `commit=True`
    # is already the default, so no flag at all should be emitted for it.
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": True})
    assert "--commit" not in argv
    # commit=False must still emit --no-commit (unchanged behaviour).
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": False})
    assert "--no-commit" in argv


def test_cli_timeout_blocks_as_hang_without_raising(corpus, tmp_path, monkeypatch):
    s = _session(corpus, tmp_path).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: (_ for _ in ())
                        .throw(subprocess.TimeoutExpired(cmd="scouter", timeout=90.0)))
    case = Case(id="par-darkroom-say-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_say"})
    step = Step("parity_pair", {"verb": "darkroom.say", "args": {"phrase": "brighter"}}, 60.0,
               True)
    obs = Observation()
    rec = exec_parity.parity_pair(s, case, step, obs)
    assert rec.error == "timeout"
    assert obs.blocked == "hang: scouter cli exceeded budget"
    assert obs.hang_evidence and obs.extra["envelopes_match"] is False


def test_parity_both_failed_when_both_sides_answer_the_same_error(corpus, tmp_path, monkeypatch):
    # PAR-hollow-pass (triage rank 2): envelopes_match alone can't tell a
    # genuine pass from both sides refusing identically -- the verb never
    # ran either way. parity_pair must flag that fact for the evaluator.
    err = {"ok": False, "outcome": "error:usage",
          "error": {"code": "usage", "message": "bad"}}
    mcp = FakeMcp(answers={"vocab_list": err})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 2, stdout=json.dumps(err), stderr=""))
    case = Case(id="par-vocab-list-error", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {"__htc_unknown__": 1}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_both_failed"] is True


def test_parity_both_failed_is_false_when_both_sides_answer_ok(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"vocab_list": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-vocab-list-happy", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_both_failed"] is False
