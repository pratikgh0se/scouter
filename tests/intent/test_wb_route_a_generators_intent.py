"""Runs tests/intent/wb_route_a_generators.json (Zenkai Rules Z1) against the
real coverage.generate()/scenarios.generate() -- two oracles per example
(Z2): the Expect's kind AND its codes/checks content."""
from __future__ import annotations

import json
from pathlib import Path

from tests.gauntlet import taxonomy
from tests.gauntlet.layers import coverage, scenarios

EXAMPLES = json.loads((Path(__file__).parent / "wb_route_a_generators.json").read_text())[
    "examples"]


def _coverage_case(case_id: str):
    return next(c for c in coverage.generate() if c.id == case_id)


def _scenarios_case(prefix: str, suffix: str):
    tool, _ = scenarios.generate(taxonomy.load())
    return next(c for c in tool if c.id.startswith(prefix) and c.id.endswith(suffix))


def run_example(ex: dict) -> None:
    inp, want = ex["input"], ex["expect"]
    if inp["generator"] == "coverage":
        c = _coverage_case(inp["case_id"])
        assert c.expect.kind == want["kind"], ex["id"]
        assert c.expect.codes == want["codes"], ex["id"]
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


def _make(ex: dict):
    def test(self=None) -> None:
        run_example(ex)
    return test


import unittest  # noqa: E402


class WbRouteAGeneratorsIntentTest(unittest.TestCase):
    pass


for _ex in EXAMPLES:
    setattr(WbRouteAGeneratorsIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))


if __name__ == "__main__":
    unittest.main()
