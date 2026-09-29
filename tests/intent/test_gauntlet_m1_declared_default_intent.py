"""Runs every example in tests/intent/gauntlet_m1_declared_default.json
(Zenkai Rules Z1; review round 2, Minor M2 -- the file is actually loaded and
executed here, not just prose next to unit tests):

`kind: "case"` examples replay the real proof4-20260929 evidence (loaded
straight from gauntlet/tests/fixtures/proof4-20260929/<case>.json --
byte-identical to the run-dir file, review round 2 Minor M3's smaller option,
no separate trimmed copy) through the ACTUAL generated case
(tests.gauntlet.layers.coverage.generate()). The recorded evidence predates
`verified_defaults` (review round 1, I1), so the runner merges TODAY's
adapter knowledge (tests.gauntlet.observe.param_meta()["verified_defaults"])
into the rebuilt before/after snapshots before scoring -- this is today's
adapter being used to rescore old evidence, not a claim the recording itself
carried that field.

`kind: "synthetic"` examples build a minimal Case/Observation directly from
the example's own fields (mirroring gauntlet/tests/test_evaluate.py's unit
tests) and call `evaluate()` on it.

Two independent oracles per example (Z2): oracle A reads the raw recorded or
constructed facts a correct evaluator should honour (the envelope's own
ok/noop/applied fields, the module's absence from after.params, membership
in verified_defaults) -- never the old recorded verdict, which is the thing
being corrected for the `case` examples; oracle B is the fixed `evaluate()`
scoring the SAME observation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import evaluate
from tests.gauntlet import observe
from tests.gauntlet.layers import coverage
from tests.gauntlet.policy import load_policy

EXAMPLES = json.loads((Path(__file__).parent /
                       "gauntlet_m1_declared_default.json").read_text())["examples"]
FIXTURES_DIR = Path(__file__).parent.parent.parent / "gauntlet" / "tests" / "fixtures" / \
    "proof4-20260929"
POLICY = load_policy()
CASES = {c.id: c for c in coverage.generate()}
#: today's adapter knowledge (review round 1, I1 item 4): the recorded
#: proof4 evidence has no `verified_defaults` key at all (it predates this
#: fix), so it is merged in here, explicitly, rather than assumed present.
VERIFIED_DEFAULTS = observe.verified_defaults()


def _module_absent(after_params: dict, module: str) -> bool:
    return not any(v is not None for k, v in after_params.items()
                  if k.split(".", 1)[0] == module)


def _run_case_example(ex: dict) -> None:
    case_id = ex["case"]
    fixture = json.loads((FIXTURES_DIR / f"{case_id}.json").read_text())
    o = fixture["observation"]

    # oracle A: the raw recorded facts a correct evaluator should honour.
    after_params = o["after"]["params"]
    assert _module_absent(after_params, "colorzones"), case_id
    assert VERIFIED_DEFAULTS.get("colorzones.strength") == 0.0, case_id
    data = o["calls"][-1]["envelope"]["data"]
    assert data["noop"] is True and data["applied"] == 0.0, case_id
    # the case really did score fail/null before this fix -- proof4's own
    # recorded ground truth, never used as the thing under test below.
    assert fixture["verdict"]["outcome"] == "failed", case_id
    assert fixture["verdict"]["checks"][0]["measured"] is None, case_id

    # oracle B: the fixed evaluator, on the same observation PLUS today's
    # adapter's verified_defaults (I1 item 4 -- explicit, not silent).
    calls = [CallRecord(c["tool"], c.get("args", {}), c.get("envelope"), c.get("wall_s", 0.0),
                        c.get("ms"), c.get("bounced", False), c.get("error"), c.get("health"),
                        c.get("write", False)) for c in o["calls"]]
    before = {**o["before"], "verified_defaults": VERIFIED_DEFAULTS}
    after = {**o["after"], "verified_defaults": VERIFIED_DEFAULTS}
    obs = Observation(calls=calls, before=before, after=after, extra=o.get("extra", {}))
    case = CASES[case_id]
    v = evaluate(case, obs, POLICY)
    exp = ex["expect"]
    assert (v.score, v.outcome) == (exp["score"], exp["outcome"]), (case_id, v.outcome, v.notes)
    assert v.checks[0].measured == exp["measured"], case_id


def _run_synthetic_example(ex: dict) -> None:
    key, target, unit = ex["key"], ex["target"], ex["unit"]
    module, param = key.split(".", 1)
    chk = Check("M1", kind="equals", target={"key": key, "value": target, "unit": unit})
    steps = [Step("darkroom_set", {}, budget_s=3.5, write=True) for _ in ex["calls"]]
    case = Case(id="synthetic", layer="coverage", tier="P0", mode="tool", steps=steps,
               expect=Expect("edit", [chk], {"changed": False, "key": key, "units": {key: unit}}),
               body="olympus", frame_role="hero", source={}, meta={})
    calls = []
    for c in ex["calls"]:
        cmod, cparam = c.get("module", module), c.get("param", param)
        env = {"ok": c.get("ok", True),
              "data": {"module": cmod, "param": cparam, "noop": c["noop"],
                        "applied": c["applied"], "committed": True}}
        calls.append(CallRecord("darkroom_set", {"module": cmod, "param": cparam}, env,
                                write=True))
    after_params = dict(ex.get("after_extra_params") or {})
    snap = lambda params: {"fingerprint": "a", "history_end": 1, "params": params,  # noqa: E731
                           "verified_defaults": ex["verified_defaults"]}
    obs = Observation(calls=calls, before=snap({}), after=snap(after_params))
    v = evaluate(case, obs, POLICY)

    # oracle A: the raw constructed facts a correct evaluator should honour
    # (whichever condition this example is meant to be missing).
    if key in ex["verified_defaults"]:
        assert _module_absent(after_params, module) == (not ex.get("after_extra_params")), ex["id"]

    # oracle B: the fixed evaluator, on the constructed observation.
    exp = ex["expect"]
    assert v.score == exp["score"], (ex["id"], v.outcome, v.notes)
    assert v.checks[0].measured == exp["measured"], ex["id"]


@pytest.mark.parametrize("ex", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_intent_example(ex):
    if ex["kind"] == "case":
        _run_case_example(ex)
    elif ex["kind"] == "synthetic":
        _run_synthetic_example(ex)
    else:
        raise AssertionError(f"{ex['id']}: unknown example kind {ex['kind']!r}")
