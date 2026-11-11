"""Runs tests/intent/batch3_bridge_relaunch.json (Zenkai Rules Z1; format
pending t_450535e2). Two independent observations per example (Z2): the fake
transport's own call log (oracle A, what actually reached darktable/the
"job") against the harness's own belief -- `session.current_frame` or
`Observation.blocked` (oracle B) -- for the runner examples; the same split
for the bulk example via `_bulk_session`'s call log and relaunch count."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.case import Case, Expect, Step
from tests.gauntlet import exec_bulk
from tests.gauntlet import session as sess
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_exec_bulk import _bulk_case, _bulk_session
from tests.gauntlet.test_session import FakeDt, FakeMcp, _session

EXAMPLES = {
    e["id"]: e
    for e in json.loads((Path(__file__).parent / "batch3_bridge_relaunch.json").read_text())[
        "examples"
    ]
}


@pytest.fixture
def corpus(tmp_path):
    from tests.gauntlet.corpus import Corpus

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {
        "frames": [{"rel": "H.ORF", "body": "olympus"}],
        "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}},
    }
    return Corpus(doc, trial)


def _err(code):
    return {"ok": False, "outcome": f"error:{code}", "data": {}, "error": {"code": code}}


def test_runner_bridge_timeout_relaunches_and_recovers(corpus, tmp_path):
    EXAMPLES["runner-bridge-timeout-relaunches-and-recovers"]  # documents the case
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp({"darkroom_open": [_err("bridge_timeout"), ok]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt, mcp=mcp).open()
    dark = s.staged.frames["dark"]

    result = s.ensure_frame("dark")

    # Oracle A: the fake transport's own call log.
    assert [c[0] for c in mcp.calls] == ["darkroom_open", "darkroom_open"]
    # Oracle B: darktable's own launch count, and the harness's belief.
    assert dt.launches == 2 and result == dark and s.current_frame == dark


def test_runner_commit_timeout_relaunches_and_recovers(corpus, tmp_path):
    EXAMPLES["runner-commit-timeout-relaunches-and-recovers"]  # documents the case
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp({"darkroom_open": [_err("commit_timeout"), ok]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt, mcp=mcp).open()
    dark = s.staged.frames["dark"]

    result = s.ensure_frame("dark")

    assert [c[0] for c in mcp.calls] == ["darkroom_open", "darkroom_open"]
    assert dt.launches == 2 and result == dark and s.current_frame == dark


def test_runner_second_bridge_timeout_marks_bridge_relaunch_failed(corpus, tmp_path):
    EXAMPLES["runner-second-bridge-timeout-marks-bridge-relaunch-failed-not-a-retry-loop"]
    mcp = FakeMcp({"darkroom_open": [_err("bridge_timeout"), _err("bridge_timeout")]})
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt, mcp=mcp).open()

    with pytest.raises(sess.SessionBlocked, match="bridge_relaunch_failed"):
        s.ensure_frame("dark")

    # Oracle A: the transport saw exactly two attempts, never a third.
    assert len(mcp.calls) == 2
    # Oracle B: darktable was launched (owned) once, then relaunched once --
    # never a third launch, i.e. never a darktable the runner did not track.
    assert dt.launches == 2


def test_bulk_step_bridge_timeout_relaunches_and_recovers(tmp_path):
    EXAMPLES["bulk-step-bridge-timeout-relaunches-and-recovers"]  # documents the case
    from tests.gauntlet.corpus import Corpus

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    bulk_corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)
    session, calls, relaunches = _bulk_session(
        tmp_path, bulk_corpus, [{"error": "bridge_timeout"}, {"envelope": {"ok": True, "data": {}}}]
    )

    obs = exec_bulk.run_bulk_case(session, _bulk_case())

    # Oracle A: the fake transport's own call log.
    assert calls == ["job_run", "job_run"]
    # Oracle B: the relaunch count and the case's own observation.
    assert len(relaunches) == 1 and obs.blocked is None


def test_bulk_c12_open_then_write_wedge_write_not_retried(tmp_path):
    EXAMPLES["bulk-c12-open-then-write-wedge-write-not-retried"]  # documents the case
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    bulk_corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)
    session, calls, relaunches = _bulk_session(
        tmp_path,
        bulk_corpus,
        [
            {"error": "not_in_darkroom"},
            {"envelope": {"ok": True, "data": {}}},
            {"error": "commit_timeout"},
        ],
    )
    session.current_frame = "not-none-until-the-relaunch-resets-it"  # R1-2
    case = Case(
        id="blk-copy_paste_history-olympus-card",
        layer="bulk",
        tier="P1",
        mode="tool",
        steps=[
            Step("darkroom_open", {"image": "{job_frame:1}"}, 30.0, False),
            Step("darkroom_say", {"phrase": "brighter"}, 8.0, True),
        ],
        expect=Expect("workflow"),
        body="olympus",
        frame_role="card",
        meta={"frames": 7},
    )

    obs = exec_bulk.run_bulk_case(session, case)

    # Oracle A: the fake transport's own call log -- the write is never resent.
    assert calls == ["darkroom_open", "darkroom_open", "darkroom_say"]
    # Oracle B: the relaunch count and the case's own observation.
    assert len(relaunches) == 1
    assert obs.blocked is not None and obs.blocked.startswith("bridge_relaunch_failed")
    # R1-1: relaunched on the card frame darkroom_open asked for, not the hero.
    assert relaunches == [session.staged.job_frames[0]]
    # R1-2: relaunch is launched, not current, until a real reopen confirms it.
    assert session.current_frame is None
