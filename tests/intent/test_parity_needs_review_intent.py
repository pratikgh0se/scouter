"""Runs tests/intent/parity_needs_review.json (Zenkai Rules Z1) by driving
the REAL tests/gauntlet/exec_parity.py parity_pair (review fix round 2,
finding 11 / D-3: the old version of this test re-implemented the
parity_both_failed formula inline instead of exercising production code, so
a regression in parity_pair itself would never have failed it).

Two independent oracles (Z2):
  - oracle A: the REAL `gauntlet.evaluate.evaluate()` verdict (score,
    outcome), computed from whatever `parity_pair` actually wrote to
    `obs.extra`;
  - oracle B: a fixture verdict list this test maintains on its own
    (`_EXPECT_REASON`, keyed by example id) against whether `parity_pair`'s
    raw `obs.extra["parity_needs_review"]` fired at all -- read from a
    DIFFERENT field than oracle A, and never derived from oracle A's own
    evaluate() call."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gauntlet.adapter import Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import Policy
from gauntlet.evaluate import evaluate as real_evaluate
from tests.gauntlet import exec_parity
from tests.gauntlet.policy import load_policy
from tests.gauntlet.test_exec_parity import corpus  # noqa: F401 (fixture)
from tests.gauntlet.test_session import FakeMcp, _session

EXAMPLES = {
    e["id"]: e
    for e in json.loads((Path(__file__).parent / "parity_needs_review.json").read_text())[
        "examples"
    ]
}

#: Oracle B (independent of evaluate()): does parity_pair's own reason
#: field fire at all for this example. Never the exact wording -- that is
#: production's to phrase -- only whether a reason exists.
_EXPECT_REASON = {
    "happy-both-sides-fail-same-way-is-needs-review": True,
    "error-both-sides-refuse-same-typed-way-is-pass": False,
    "error-a-side-accepted-the-bad-input-is-needs-review": True,
    "flag-flip-restores-old-scoring-for-happy": True,  # the reason still fires; the FLAG hides it
}


def _drive_parity_pair(ex_id, corpus, tmp_path, monkeypatch):
    ex = EXAMPLES[ex_id]
    inp = ex["input"]
    mcp = FakeMcp(answers={"vocab_list": inp["mcp_envelope"]})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    cli_code = 0 if inp["cli_envelope"]["ok"] else 2
    monkeypatch.setattr(
        exec_parity.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], cli_code, stdout=json.dumps(inp["cli_envelope"]), stderr=""
        ),
    )
    case = Case(
        id=f"par-vocab-list-{ex_id}",
        layer="parity",
        tier="P1",
        mode="tool",
        steps=[],
        expect=Expect(
            "workflow", [Check("extra", target={"path": "envelopes_match", "op": "truthy"})]
        ),
        body="olympus",
        frame_role="hero",
        meta={"mcp": "vocab_list", "variant": inp["variant"]},
    )
    step = Step("parity_pair", {"verb": "vocab.list", "args": {}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    return case, obs


def _policy_for(ex) -> Policy:
    base = load_policy()
    if not ex["input"]["flag_set"]:
        return base
    return Policy(**{**vars(base), "flags": frozenset({"parity.same_failure_is_pass"})})


@pytest.mark.parametrize("ex_id", sorted(EXAMPLES))
def test_parity_needs_review_examples(ex_id, corpus, tmp_path, monkeypatch):
    ex = EXAMPLES[ex_id]
    case, obs = _drive_parity_pair(ex_id, corpus, tmp_path, monkeypatch)
    policy = _policy_for(ex)
    v = real_evaluate(case, obs, policy)
    # Oracle A: the real evaluator's verdict.
    assert (v.score, v.outcome) == (ex["expect"]["score"], ex["expect"]["outcome"]), v
    # Oracle B: an independently-maintained expectation of whether the raw
    # reason fired, regardless of the flag (which only gates evaluate(),
    # never parity_pair's own computation).
    assert bool(obs.extra.get("parity_needs_review")) == _EXPECT_REASON[ex_id]


def test_the_flag_is_real_config_not_just_a_constructor_default():
    # Oracle B (this test's own): the REAL check_map.json, through the REAL
    # loader, must not have the flag on by default (the brief's "defaulting
    # to false").
    policy = load_policy()
    assert "parity.same_failure_is_pass" not in policy.flags
