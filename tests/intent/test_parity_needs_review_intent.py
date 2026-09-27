"""Runs tests/intent/parity_needs_review.json (Zenkai Rules Z1). Two
independent oracles (Z2): `gauntlet.evaluate.evaluate`'s own Verdict (oracle
A, built straight from the example's envelopes) against `tests.gauntlet.
policy.load_policy()` reading the REAL check_map.json's `flags` list (oracle
B) -- proving the "one-line config flip" is real config, not just a
constructor argument no loader ever reads."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import Policy, evaluate
from tests.gauntlet.policy import load_policy

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent / "parity_needs_review.json").read_text())[
                "examples"]}

BASE_POLICY = Policy(refusal_codes=frozenset(), transient_codes=frozenset(),
                     refusal_markers=(), claim_markers=())


def _case() -> Case:
    return Case(id="par-example", layer="parity", tier="P1", mode="tool",
               steps=[Step("parity_pair")],
               expect=Expect("workflow", [Check("extra", target={"path": "envelopes_match",
                                                                  "op": "truthy"})]),
               body="olympus", frame_role="hero")


def _observation(ex: dict) -> Observation:
    inp = ex["input"]
    diffs = inp["parity_diffs"]
    both_failed = not diffs and not inp["mcp_envelope"]["ok"] and not inp["cli_envelope"]["ok"]
    return Observation(
        calls=[CallRecord("parity_pair", {}, {"ok": not diffs, "outcome": "ok",
                                              "data": {"diffs": diffs}})],
        extra={"envelopes_match": not diffs, "parity_diffs": diffs,
               "cli_envelope": inp["cli_envelope"], "parity_both_failed": both_failed})


def _run(ex_id: str) -> tuple:
    ex = EXAMPLES[ex_id]
    flags = frozenset({"parity.same_failure_is_pass"}) if ex["input"]["flag_set"] else frozenset()
    policy = Policy(**{**vars(BASE_POLICY), "flags": flags})
    v = evaluate(_case(), _observation(ex), policy)
    return v.score, v.outcome


def test_both_sides_error_same_way_is_needs_review():
    ex = EXAMPLES["both-sides-error-same-way-is-needs-review"]
    assert _run("both-sides-error-same-way-is-needs-review") == \
        (ex["expect"]["score"], ex["expect"]["outcome"])


def test_both_sides_ok_same_envelope_is_pass():
    ex = EXAMPLES["both-sides-ok-same-envelope-is-pass"]
    assert _run("both-sides-ok-same-envelope-is-pass") == \
        (ex["expect"]["score"], ex["expect"]["outcome"])


def test_flag_flip_restores_old_scoring():
    ex = EXAMPLES["flag-flip-restores-old-scoring"]
    assert _run("flag-flip-restores-old-scoring") == \
        (ex["expect"]["score"], ex["expect"]["outcome"])


def test_the_flag_is_real_config_not_just_a_constructor_default():
    # Oracle B: the REAL check_map.json, through the REAL loader, must not
    # have the flag on by default (the brief's "defaulting to false").
    policy = load_policy()
    assert "parity.same_failure_is_pass" not in policy.flags
