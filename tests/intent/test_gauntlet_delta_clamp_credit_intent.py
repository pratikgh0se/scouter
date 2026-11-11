"""Runs tests/intent/gauntlet_delta_clamp_credit.json (Zenkai Z1/Z2). Oracle A:
the raw facts in the example (committed value against the declared lo, the
envelope's clamp flag). Oracle B: evaluate() scoring the real generated case."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import evaluate
from tests.gauntlet.layers import coverage
from tests.gauntlet.policy import load_policy

DOC = json.loads((Path(__file__).parent / "gauntlet_delta_clamp_credit.json").read_text())
CASE = {c.id: c for c in coverage.generate()}[DOC["case"]]
POLICY = load_policy()


def _adjust_envelope(clamped: bool) -> dict:
    """The product's real clamp envelope (scouter/core/moves.py: outcome
    moved_to_limit -> ok false, data carries clamped/cap/applied), or a
    plain ok when it did not report a clamp."""
    if not clamped:
        return {"ok": True, "outcome": "ok", "data": {"clamped": False}}
    return {
        "ok": False,
        "outcome": "moved_to_limit",
        "data": {"clamped": True, "cap": 0.0, "applied": 0.0, "requested": -0.075},
    }


def _observation(ex: dict) -> Observation:
    key = DOC["key"]
    module, param = key.split(".")
    args = {"module": module, "param": param}
    snaps = [
        {"fingerprint": fp, "history_end": n, "params": {key: v}}
        for fp, n, v in (("a", 0, None), ("b", 1, ex["start"]), ("c", 2, ex["committed"]))
    ]
    calls = [
        CallRecord("darkroom_set", args, {"ok": True, "outcome": "ok", "data": {}}, write=True),
        CallRecord("darkroom_adjust", args, _adjust_envelope(ex["clamped"]), write=True),
    ]
    return Observation(
        calls=calls,
        before=snaps[0],
        after=snaps[2],
        extra={"snapshots": [x["params"] for x in snaps]},
    )


@pytest.mark.parametrize("ex", DOC["examples"], ids=lambda e: e["id"])
def test_example(ex: dict) -> None:
    # oracle A: facts about the subject, not the example restating itself:
    # the real generated case declares this lo and the ask overshoots it
    target = CASE.expect.checks[0].target
    assert target["lo"] == DOC["lo"]
    assert ex["start"] + target["delta"] < target["lo"]
    should_pass = abs(ex["committed"] - target["lo"]) < 1e-9 and ex["clamped"]
    assert (ex["expect"]["score"] == "pass") == should_pass, ex["id"]
    # oracle B: the evaluator
    v = evaluate(CASE, _observation(ex), POLICY)
    assert (v.score == "pass") == should_pass, (ex["id"], v.score, v.outcome)
    assert v.outcome == ex["expect"]["outcome"], ex["id"]
    if "reason" in ex["expect"]:
        assert ex["expect"]["reason"] in (v.silent_failure or ""), ex["id"]
