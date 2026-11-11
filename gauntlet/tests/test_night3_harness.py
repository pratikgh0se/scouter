"""Night-3 harness fixes (fix/night3-harness): evaluator artefacts found in the
night3-20261108 coverage run. Fixtures are distilled from the REAL recorded
cases (gauntlet/tests/fixtures/night3_cases.json: the case spec from cases.jsonl
and the observation from cases/<id>.json, trimmed to the colour-balance keys).
Every fix has a guard proving a REAL silent failure of the same shape still
scores silent_failure (Zenkai Z2: sidecar and envelope are separate oracles)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case
from gauntlet.evaluate import Policy, evaluate

FX = json.loads((Path(__file__).parent / "fixtures" / "night3_cases.json").read_text())
POLICY = Policy(  # the real check_map policy's transient and refusal codes
    refusal_codes=frozenset(
        {
            "usage",
            "value_unknown",
            "not_pinned",
            "module_unavailable",
            "not_found",
            "not_in_darkroom",
            "unsupported_format",
        }
    ),
    transient_codes=frozenset({"gui_not_ready", "bridge_timeout", "commit_timeout"}),
    refusal_markers=("can't", "cannot"),
    claim_markers=(r"\bdone\b",),
)

S1A = "cov-phrase-more_chroma-sony_a7-explicit"
S2A = "cov-phrase-weaker_grade-olympus-lot"
S2B = "cov-synonym-weaker_grade-weaker_colour_grade-olympus"
WARM = "cov-phrase-warm_highlights-olympus-bare"
TEAL = "cov-phrase-teal_orange-sony_a7-lot"
P4 = "cov-p4-brighter_highlights-olympus"
GC = "colorbalancergb.global_chroma"


def load(cid: str, mutate=None) -> tuple[Case, Observation]:
    fx = copy.deepcopy(FX[cid])
    if mutate:
        mutate(fx)
    o = fx["observation"]
    obs = Observation(
        calls=[CallRecord(**c) for c in o["calls"]],
        before=o["before"],
        after=o["after"],
        extra=o["extra"],
    )
    return Case.from_dict(fx["case"]), obs


def score(cid: str, mutate=None):
    case, obs = load(cid, mutate)
    return evaluate(case, obs, POLICY)


def set_after(fx, key, value):
    fx["observation"]["after"]["params"][key] = value
    fx["observation"]["extra"]["snapshots"][-1][key] = value


# ------------------------------------------------ G1a absent-module "before"


def test_g1a_recorded_shape_was_a_false_silent():
    assert FX[S1A]["recorded_verdict"]["outcome"] == "silent_failure"
    v = score(S1A)
    assert (v.outcome, v.score) == ("verified", "pass"), (v.outcome, v.silent_failure)


def test_g1a_real_silent_landed_nothing_still_silent():
    # ok envelope claims 0.0 -> 0.05, the sidecar still reads the default
    v = score(S1A, lambda fx: set_after(fx, GC, 0.0))
    assert v.outcome == "silent_failure"


def test_g1a_real_silent_landed_wrong_value_still_silent():
    v = score(S1A, lambda fx: set_after(fx, GC, 0.2))
    assert v.outcome == "silent_failure"


def _drop_before_params(fx):
    fx["observation"]["before"]["params"] = {}
    fx["observation"]["extra"]["snapshots"][0] = {}


def test_g1a_unreadable_before_still_fails_closed():
    # an EMPTY before snapshot is unreadable, not "module absent"
    assert score(S1A, _drop_before_params).outcome == "silent_failure"


def test_g1a_no_declared_default_still_fails_closed():
    def m(fx):
        for s in ("before", "after"):
            fx["observation"][s]["verified_defaults"].pop(GC, None)

    assert score(S1A, m).outcome == "silent_failure"


def test_g1a_module_present_but_key_missing_still_fails_closed():
    def m(fx):
        fx["observation"]["before"]["params"]["colorbalancergb.__enabled"] = 1
        fx["observation"]["extra"]["snapshots"][0]["colorbalancergb.__enabled"] = 1

    assert score(S1A, m).outcome == "silent_failure"


def test_g1a_envelope_disagreeing_with_default_fails_closed():
    # the product's own "previous" contradicts the declared default: no fallback
    def m(fx):
        fx["observation"]["calls"][0]["envelope"]["data"]["moves"][0]["previous"] = 0.3

    assert score(S1A, m).outcome == "silent_failure"


def test_g1a_p4_monotonic_absent_before_passes_and_guard():
    case, obs = load(P4)
    assert FX[P4]["recorded_verdict"]["outcome"] == "failed"
    v = evaluate(case, obs, POLICY)
    assert (v.outcome, v.score) == ("verified", "pass"), (v.outcome, [r.measured for r in v.checks])

    def flat(fx):  # touch and bare land the same: not strictly increasing
        snaps = fx["observation"]["extra"]["snapshots"]
        k = "colorbalancergb.highlights_luminance"
        snaps[3][k] = snaps[1][k]
        snaps[4][k] = snaps[1][k]
        snaps[5][k] = snaps[1][k]

    assert score(P4, flat).score != "pass"


# ------------------------------------------------ G1b reported = the failing step


def test_g1b_honest_partial_after_ok_antecedent_is_not_silent():
    for cid in (S2A, S2B):
        assert FX[cid]["recorded_verdict"]["outcome"] == "silent_failure"
        v = score(cid)
        assert v.outcome != "silent_failure", (cid, v.silent_failure)
        assert v.score != "pass"  # the refused move did not happen


def test_g1b_guard_final_step_ok_but_unchanged_is_silent():
    # same two steps, but the second answers a plain ok (all three moves) while
    # the sidecar did not move: a real silent failure
    def m(fx):
        c = fx["observation"]["calls"][1]
        moves = fx["observation"]["calls"][0]["envelope"]["data"]["moves"]
        c["envelope"] = {
            "ok": True,
            "outcome": "ok",
            "data": {"moves": [{**mv, "previous": 0.15, "applied": 0.0} for mv in moves]},
        }
        c["error"] = None

    assert score(S2A, m).outcome == "silent_failure"


def test_g1b_guard_earlier_ok_step_not_landed_is_still_silent():
    # step 1 said ok (0.15) but the sidecar after step 1 never got it
    def m(fx):
        s1 = fx["observation"]["extra"]["snapshots"][1]
        for k in list(s1):
            if k.endswith("_chroma"):
                s1[k] = 0.0

    assert score(S2A, m).outcome == "silent_failure"


def test_g1b_guard_partial_claims_a_move_the_sidecar_does_not_show():
    # the partial says shadows landed at 0.0; the sidecar still holds 0.15
    def m(fx):
        set_after(fx, "colorbalancergb.shadows_chroma", 0.15)

    assert score(S2A, m).outcome == "silent_failure"


# ------------------------------------------------ G1c nested transient codes


def test_g1c_nested_commit_timeout_is_blocked_not_failed():
    for cid, was in ((WARM, "failed"), (TEAL, "partial")):
        assert FX[cid]["recorded_verdict"]["outcome"] == was
        v = score(cid)
        assert (v.outcome, v.score) == ("blocked", "fail"), (cid, v.outcome)
        assert v.notes == ["commit_timeout"]


def test_g1c_nested_non_transient_code_is_not_blocked():
    def m(fx):
        for c in fx["observation"]["calls"]:
            for r in c["envelope"]["data"].get("refused") or []:
                r["code"] = "value_unknown"

    v = score(WARM, m)
    assert v.outcome != "blocked"


def test_g1c_guard_nested_transient_does_not_hide_a_real_silent():
    # a nested transient refusal AND an ok write that claims what the sidecar
    # does not show: the silent failure wins over blocked
    def m(fx):
        c = fx["observation"]["calls"][0]
        c["envelope"]["ok"] = True
        c["envelope"]["outcome"] = "ok"
        c["error"] = None
        set_after(fx, "colorbalancergb.highlights_hue", 0.0)
        fx["observation"]["after"]["fingerprint"] = fx["observation"]["before"]["fingerprint"]

    assert score(WARM, m).outcome == "silent_failure"
