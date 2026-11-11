"""Runs tests/intent/walkthrough_state.json (Zenkai Rules Z1). Two
independent observations per example (Z2): the MCP call log (whether
isolate()'s own darkroom_reset reached the fake "job", oracle A) against
`obs.extra["journal_reset_entries"]` (the harness's own journal-isolation
bookkeeping, oracle B) -- neither is derived from the other."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.case import Case, Expect, Step
from tests.gauntlet import execute, observe
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeMcp, _session

EXAMPLES = {
    e["id"]: e
    for e in json.loads((Path(__file__).parent / "walkthrough_state.json").read_text())["examples"]
}


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {
        "frames": [{"rel": "H.ORF", "body": "olympus"}],
        "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}},
    }
    return Corpus(doc, trial)


def test_walkthrough_case_after_an_earlier_step_shares_state(corpus, tmp_path, monkeypatch):
    EXAMPLES["walkthrough-case-after-an-earlier-step-shares-state"]  # documents the case run here
    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(
        observe, "snapshot", lambda p, k=None: {"fingerprint": "a", "params": {}, "units": {}}
    )
    case1 = Case(
        id="gd-01",
        layer="guide",
        tier="P0",
        mode="tool",
        steps=[Step("darkroom_set", {"module": "exposure"}, 3.5, True)],
        expect=Expect("workflow"),
        body="olympus",
        frame_role="hero",
        meta={"session": "walkthrough", "order": 0},
    )
    execute.run_tool_case(s, case1)
    mcp.calls.clear()
    case2 = Case(
        id="gd-02",
        layer="guide",
        tier="P0",
        mode="tool",
        steps=[Step("darkroom_say", {"phrase": "less contrast"}, 8.0, True)],
        expect=Expect("workflow"),
        body="olympus",
        frame_role="hero",
        meta={"session": "walkthrough", "order": 1},
    )
    obs2 = execute.run_tool_case(s, case2)
    # Oracle A: isolate()'s own darkroom_reset never reached the fake job.
    assert "darkroom_reset" not in [t for t, _ in mcp.calls]
    # Oracle B: the harness's own journal-isolation bookkeeping did not run.
    assert "journal_reset_entries" not in obs2.extra


def test_ordinary_case_stays_isolated(corpus, tmp_path, monkeypatch):
    EXAMPLES["ordinary-case-stays-isolated"]
    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.touched = {"exposure"}  # as if a previous case wrote it
    monkeypatch.setattr(
        observe, "snapshot", lambda p, k=None: {"fingerprint": "a", "params": {}, "units": {}}
    )
    case = Case(
        id="cov-1",
        layer="coverage",
        tier="P1",
        mode="tool",
        steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
        expect=Expect("edit"),
        body="olympus",
        frame_role="hero",
    )
    obs = execute.run_tool_case(s, case)
    # Oracle A: isolate()'s own darkroom_reset reached the fake job.
    assert "darkroom_reset" in [t for t, _ in mcp.calls]
    # Oracle B: the harness's own journal-isolation bookkeeping ran.
    assert obs.extra["journal_reset_entries"] == 0
