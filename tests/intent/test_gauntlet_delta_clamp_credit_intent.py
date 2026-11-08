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


def _observation(ex: dict) -> Observation:
    key = DOC["key"]
    module, param = key.split(".")
    args = {"module": module, "param": param}
    snaps = [{"fingerprint": fp, "history_end": n, "params": {key: v}}
             for fp, n, v in (("a", 0, None), ("b", 1, ex["start"]), ("c", 2, ex["committed"]))]
    calls = [
        CallRecord("darkroom_set", args, {"ok": True, "outcome": "ok", "data": {}}, write=True),
        CallRecord("darkroom_adjust", args,
                   {"ok": True, "outcome": "ok", "data": {"clamped": ex["clamped"]}}, write=True),
    ]
    return Observation(calls=calls, before=snaps[0], after=snaps[2],
                       extra={"snapshots": [x["params"] for x in snaps]})


@pytest.mark.parametrize("ex", DOC["examples"], ids=lambda e: e["id"])
def test_example(ex: dict) -> None:
    # oracle A: raw facts, no evaluator involved
    at_bound = abs(ex["committed"] - DOC["lo"]) < 1e-9
    should_pass = at_bound and ex["clamped"]
    assert (ex["expect"]["score"] == "pass") == should_pass, ex["id"]
    # oracle B: the evaluator
    v = evaluate(CASE, _observation(ex), POLICY)
    assert (v.score == "pass") == should_pass, (ex["id"], v.score, v.outcome)
    if should_pass:
        assert v.outcome == ex["expect"]["outcome"], ex["id"]
