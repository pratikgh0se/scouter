"""Runs tests/intent/gauntlet_night3_harness.json (Zenkai Z1/Z2). Oracle A: the
raw facts in each example are checked against the recorded fixture (the
envelope's claim, the sidecar value, the before snapshot). Oracle B: evaluate()."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case
from gauntlet.evaluate import evaluate
from tests.gauntlet.policy import load_policy

ROOT = Path(__file__).resolve().parents[2]
DOC = json.loads((Path(__file__).parent / "gauntlet_night3_harness.json").read_text())
FX = json.loads((ROOT / "gauntlet/tests/fixtures/night3_cases.json").read_text())
POLICY = load_policy()


def _mutate(fx: dict, mutation: str | None) -> None:
    o = fx["observation"]
    if mutation is None:
        return
    kind, _, arg = mutation.partition(":")
    if kind == "after":
        key, _, val = arg.partition("=")
        o["after"]["params"][key] = float(val)
        o["extra"]["snapshots"][-1][key] = float(val)
    elif kind == "empty_before":
        o["before"]["params"] = {}
        o["extra"]["snapshots"][0] = {}
    elif kind == "step1_not_landed":
        for k in list(o["extra"]["snapshots"][1]):
            if k.endswith("_chroma"):
                o["extra"]["snapshots"][1][k] = 0.0
    elif kind == "nested_code":
        for c in o["calls"]:
            for r in (c["envelope"]["data"].get("refused") or []):
                r["code"] = arg
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize("ex", DOC["examples"], ids=lambda e: e["id"])
def test_example(ex: dict) -> None:
    fx = copy.deepcopy(FX[ex["case"]])
    facts = ex["facts"]
    # oracle A: the recorded raw facts
    calls = fx["observation"]["calls"]
    if "module_in_before" in facts and facts["module_in_before"] is False:
        before = fx["observation"]["before"]["params"]
        assert not any(k.startswith("colorbalancergb.") for k in before)
    if "nested_refused" in facts:
        got = [r["code"] for c in calls for r in (c["envelope"]["data"].get("refused") or [])]
        assert got == facts["nested_refused"] or ex["mutation"]
        assert all((c["envelope"].get("error") or {}).get("code") is None for c in calls)
    _mutate(fx, ex["mutation"])
    if "sidecar_after" in facts:
        assert fx["observation"]["after"]["params"]["colorbalancergb.global_chroma"] \
            == pytest.approx(facts["sidecar_after"], abs=1e-6)
    o = fx["observation"]
    obs = Observation(calls=[CallRecord(**c) for c in o["calls"]], before=o["before"],
                      after=o["after"], extra=o["extra"])
    # oracle B: the evaluator
    v = evaluate(Case.from_dict(fx["case"]), obs, POLICY)
    assert (v.outcome, v.score) == (ex["expect"]["outcome"], ex["expect"]["score"]), \
        (ex["id"], v.outcome, v.silent_failure)
    if "reason" in ex["expect"]:
        assert (v.silent_failure or "").startswith(ex["expect"]["reason"]), ex["id"]
