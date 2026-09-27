"""Runs tests/intent/wb_route_a_generators.json (Zenkai Rules Z1) against the
real coverage.generate()/scenarios.generate()/guide.generate() -- two
oracles per example (Z2): the Expect's kind/codes AND (review round 2,
finding 14 / GC-2, for the guide examples) the real evaluator's own verdict
against a synthetic refusal envelope (verified) and a synthetic landed one
(must NOT verify) -- neither derived from the other."""
from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import evaluate
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import coverage, guide, scenarios
from tests.gauntlet.policy import POLICY

EXAMPLES = json.loads((Path(__file__).parent / "wb_route_a_generators.json").read_text())[
    "examples"]


def _coverage_case(case_id: str):
    return next(c for c in coverage.generate() if c.id == case_id)


def _scenarios_case(prefix: str, suffix: str):
    tool, _ = scenarios.generate(taxonomy.load())
    return next(c for c in tool if c.id.startswith(prefix) and c.id.endswith(suffix))


def _guide_case(case_id: str):
    return next(c for c in guide.generate() if c.id == case_id)


def _refusal_obs(code: str) -> Observation:
    env = {"ok": False, "outcome": f"error:{code}", "error": {"code": code}}
    return Observation(calls=[CallRecord("call", {}, env, write=True)],
                       before={"fingerprint": "a", "params": {}},
                       after={"fingerprint": "a", "params": {}})


def _landed_obs() -> Observation:
    env = {"ok": True, "outcome": "ok", "data": {}}
    return Observation(calls=[CallRecord("call", {}, env, write=True)],
                       before={"fingerprint": "a", "params": {}},
                       after={"fingerprint": "b", "params": {}})


def run_example(ex: dict) -> None:
    inp, want = ex["input"], ex["expect"]
    if inp["generator"] == "coverage":
        c = _coverage_case(inp["case_id"])
        assert c.expect.kind == want["kind"], ex["id"]
        assert c.expect.codes == want["codes"], ex["id"]
    elif inp["generator"] == "guide":
        c = _guide_case(inp["case_id"])
        assert c.expect.kind == want["kind"], ex["id"]
        assert c.expect.codes == want["codes"], ex["id"]
        # Oracle B (finding 14 / GC-2): the real evaluator, run against a
        # synthetic envelope -- independent of the generator's own Expect.
        code = want["codes"][0]
        v = evaluate(c, _refusal_obs(code), POLICY)
        assert v.outcome == "verified", (ex["id"], v)
        v_landed = evaluate(c, _landed_obs(), POLICY)
        assert v_landed.outcome != "verified", (ex["id"], v_landed)
    else:
        c = _scenarios_case(inp["case_id_prefix"], inp["case_id_suffix"])
        assert c.expect.kind == want["kind"], ex["id"]
        stats = {(chk.metric, chk.stat) for chk in c.expect.checks}
        assert tuple(want["wb_stat_absent"]) not in stats, ex["id"]
        assert any(chk.metric == want["other_stat_present"] for chk in c.expect.checks), ex["id"]
        if want.get("envelope_refusal_present"):
            env = [chk for chk in c.expect.checks if chk.metric == "envelope"]
            assert any(chk.target == {"path": "ok", "op": "eq", "value": False} for chk in env), \
                ex["id"]
            assert any(chk.target.get("path") == "error.code" for chk in env), ex["id"]


def test_guide_touch_warmer_pinned(monkeypatch):
    """EXAMPLES["guide-touch-warmer-pinned"] (finding 10 / GC-1): once Route
    A is pinned, gd-12 must get the shared checkmap.wb_route_a_pinned_edit
    shape, not its original (now unreachable) +150 K temperature.temperature
    delta check."""
    from tests.gauntlet import checkmap as checkmap_mod

    next(e for e in EXAMPLES if e["id"] == "guide-touch-warmer-pinned")  # documents this case
    monkeypatch.setattr(checkmap_mod, "route_a_pinned", lambda cmap: True)
    c = _guide_case("gd-12-guide_touch_warmer")
    assert c.expect.kind == "edit"
    move = c.expect.checks[0]
    assert move.target["key"] == "channelmixerrgb.temperature"
    assert move.metric == "M1"


def _make(ex: dict):
    def test(self=None) -> None:
        run_example(ex)
    return test


import unittest  # noqa: E402


class WbRouteAGeneratorsIntentTest(unittest.TestCase):
    pass


#: "guide-touch-warmer-pinned" needs monkeypatch scaffolding run_example
#: does not have (finding 10 / GC-1); it has its own dedicated pytest
#: function (test_guide_touch_warmer_pinned) above instead.
for _ex in EXAMPLES:
    if _ex["id"] == "guide-touch-warmer-pinned":
        continue
    setattr(WbRouteAGeneratorsIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))


if __name__ == "__main__":
    unittest.main()
