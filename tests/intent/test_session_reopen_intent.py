"""Runs tests/intent/session_reopen.json (Zenkai Rules Z1; format pending
t_450535e2). Two independent observations per example (Z2): the MCP call log
(what actually reached the "job", oracle A) against `session.current_frame`
(the harness's own belief about what is open, oracle B) for the reopen
examples, and `isolate()`'s verdict against the darktable relaunch count for
the contamination examples -- neither oracle is derived from the other."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.gauntlet import execute, observe
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeDt, FakeMcp, _session

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent / "session_reopen.json").read_text())["examples"]}


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def test_refresh_then_write_not_refused(corpus, tmp_path):
    ex = EXAMPLES["refresh-then-write-not-refused"]
    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.ensure_frame("dark")
    mcp.calls.clear()

    fresh = s.refresh_frame("dark")
    # Oracle B: the harness's own state says "launched, not yet current".
    assert s.current_frame != fresh

    result = s.ensure_frame("dark")
    # Oracle A: exactly one darkroom_open reached the fake "job" transport.
    assert [c[0] for c in mcp.calls] == ["darkroom_open"] == \
        ["darkroom_open"] * ex["expect"]["open_calls_after_refresh"]
    assert result == fresh and s.current_frame == fresh


def test_isolate_reset_of_absent_module_is_not_contamination(corpus, tmp_path, monkeypatch):
    EXAMPLES["isolate-reset-of-absent-module-is-not-contamination"]  # documents the case run here
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"sigmoid.contrast": None}  # oracle A input: module absent before
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x",
        "params": {"sigmoid.contrast": 1.0, "sigmoid.__enabled": 1},  # at default after reset
        "units": {"sigmoid.contrast": "ratio", "sigmoid.__enabled": "bool"},
        "defaults": {"sigmoid.contrast": 1.0}})
    # Oracle A: isolate()'s own verdict.
    assert execute.isolate(s, frame, "hero") is False
    # Oracle B: darktable was never relaunched (a second, independent signal).
    assert dt.launches == 1


def test_isolate_a_real_move_still_restages(corpus, tmp_path, monkeypatch):
    EXAMPLES["isolate-a-real-move-still-restages"]
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"exposure.exposure": 0.7}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x", "params": {"exposure.exposure": 1.3}, "units": {}})
    assert execute.isolate(s, frame, "hero") is True
    assert dt.launches == 2
