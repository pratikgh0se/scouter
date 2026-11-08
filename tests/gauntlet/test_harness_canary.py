"""Gauntlet harness fixes from canary-colorcal-20261108 (harness only; no product code).

Group B2 (evaluator silent-failure mislabel, Z1 examples in
tests/intent/canary_setup_write.json, Z2: envelope outcome vs sidecar snapshots),
group A (derived keys are not collateral), group B1 (self-contained adjust
cases), group D (walkthrough prefix auto-included). Never launches darktable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import evaluate
from gauntlet.runner import with_walkthrough_prefix
from tests.gauntlet.layers import coverage, guide
from tests.gauntlet.policy import load_policy

POLICY = load_policy()
CASES = {c.id: c for c in coverage.generate()}
INTENT = json.loads((Path(__file__).parents[1] / "intent" / "canary_setup_write.json").read_text())

BASE = {"channelmixerrgb.temperature": 3926.7, "channelmixerrgb.x": 0.3, "channelmixerrgb.y": 0.32,
        "channelmixerrgb.gamut": 1.0, "temperature.preset": 4}
UNITS = {"channelmixerrgb.temperature": "K"}


def _obs(call_envs: list[dict], snaps: list[dict]) -> Observation:
    calls = [CallRecord("darkroom_set" if i == 0 else "darkroom_nudge", {}, env, 1.0, write=True)
             for i, env in enumerate(call_envs)]
    return Observation(calls=calls, before={"params": snaps[0], "fingerprint": "a"},
                       after={"params": snaps[-1],
                              "fingerprint": "a" if snaps[-1] == snaps[0] else "b",
                              "units": UNITS},
                       extra={"snapshots": snaps})


OK = {"ok": True, "outcome": "ok", "data": {"committed": True, "module": "channelmixerrgb",
                                           "param": "temperature"}}
TIMEOUT = {"ok": False, "outcome": "error", "error": {"code": "commit_timeout", "message": "x"}}


@pytest.mark.parametrize("ex", INTENT["examples"], ids=lambda e: e["id"])
def test_setup_write_does_not_make_action_error_silent(ex):
    case = CASES[ex["case"]]
    assert case.steps[0].setup and not case.steps[1].setup  # the prep is marked, the action is not
    after_set = {**BASE, "channelmixerrgb.temperature": 7000.0}
    action = TIMEOUT if ex["input"]["action"].startswith("error") else OK
    final = after_set if not ex["input"]["after_moved"] else \
        {**after_set, "channelmixerrgb.temperature": 7001.0}
    v = evaluate(case, _obs([OK, action], [BASE, after_set, final]), POLICY)
    assert v.outcome == ex["expect"]["outcome"], v
    assert bool(v.silent_failure) is ex["expect"]["silent_failure"]


def _collateral_case():
    return CASES["cov-channelmixerrgb.temperature-olympus-adjust_dn1"]


def _adjust_obs(after: dict) -> Observation:
    start = {**BASE, "channelmixerrgb.temperature": 7000.0}
    case = _collateral_case()
    envs = [OK, {**OK, "data": {**OK["data"], "previous": 7000.0, "applied": 6700.0}}]
    return _obs(envs[:len(case.steps)], [BASE, start, after][:len(case.steps) + 1])


def test_derived_xy_moving_with_temperature_is_not_collateral():
    after = {**BASE, "channelmixerrgb.temperature": 6700.0, "channelmixerrgb.x": 0.31,
             "channelmixerrgb.y": 0.326}
    assert _collateral_case().expect.sidecar["allowed"] == ["channelmixerrgb.x",
                                                             "channelmixerrgb.y"]
    v = evaluate(_collateral_case(), _adjust_obs(after), POLICY)
    assert v.outcome != "collateral", v


def test_other_channelmixerrgb_param_moving_is_still_collateral():
    after = {**BASE, "channelmixerrgb.temperature": 6700.0, "channelmixerrgb.x": 0.31,
             "channelmixerrgb.gamut": 1.2}
    v = evaluate(_collateral_case(), _adjust_obs(after), POLICY)
    assert v.outcome == "collateral" and "channelmixerrgb.gamut" in " ".join(v.notes), v


def test_adjust_cases_never_inherit_start_state():
    """A relative move needs a start the case set itself (the earlier set_hi_plus
    case left the frame at the 12000 K limit)."""
    up1 = CASES["cov-temperature.temperature-olympus-adjust_up1"]
    assert up1.steps[0].tool == "darkroom_set" and up1.steps[0].setup
    assert 3000 < up1.steps[0].args["value"] < 12000
    assert up1.steps[1].tool == "darkroom_adjust"
    bad = []
    for cid, c in CASES.items():
        names = [s.tool for s in c.steps]
        if "darkroom_adjust" not in names and "darkroom_nudge" not in names:
            continue
        # Every other module is reset by the runner's isolate() between cases; the
        # leak is route A: a temperature.temperature write lands on
        # channelmixerrgb, which isolate (keyed on the touched module) does not reset.
        if not cid.startswith("cov-temperature.temperature-"):
            continue
        if c.expect.kind == "typed_error" or c.meta.get("accept_refusal") or not c.expect.checks:
            continue  # a refusal case writes nothing, so nothing to inherit
        first = next(i for i, n in enumerate(names) if n in ("darkroom_adjust", "darkroom_nudge"))
        if first == 0 or not c.steps[first - 1].setup:
            bad.append(cid)
    assert not bad, bad[:10]


def test_walkthrough_subset_gets_its_prefix():
    cases = guide.generate()
    walk = [c for c in cases if c.meta.get("session") == "walkthrough"]
    gd12 = next(c for c in walk if c.meta["order"] == 12)
    out = with_walkthrough_prefix([gd12], cases)
    assert [c.meta["order"] for c in out if c.meta.get("session") == "walkthrough"] == \
        list(range(13))
    edu = [c for c in cases if c.meta.get("session", "").startswith("edu-")][:2]
    assert with_walkthrough_prefix(edu, cases) == edu
    assert with_walkthrough_prefix(walk, cases) == walk
    first = [c for c in walk if c.meta["order"] == 0]
    assert with_walkthrough_prefix(first, cases) == first
