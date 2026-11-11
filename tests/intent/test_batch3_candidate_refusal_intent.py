"""Runs tests/intent/batch3_candidate_refusal.json (Zenkai Rules Z1; format
pending t_450535e2). Two independent observations per example (Z2): the
generator's own `Case.meta`/`Case.expect.kind` (oracle A, what the gauntlet
runner ships) against the evaluator's `Verdict` on a synthetic observation
built straight from the recorded case json's own refusal shape (oracle B,
what actually scores it) -- neither oracle is derived from the other."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import Policy, evaluate
from tests.gauntlet.layers import coverage

EXAMPLES = {
    e["id"]: e
    for e in json.loads((Path(__file__).parent / "batch3_candidate_refusal.json").read_text())[
        "examples"
    ]
}

POLICY = Policy(
    refusal_codes=frozenset({"usage", "value_unknown", "not_pinned", "unreachable"}),
    transient_codes=frozenset({"gui_not_ready", "bridge_timeout", "commit_timeout"}),
    refusal_markers=("can't", "cannot", "not able"),
    claim_markers=(r"\bdone\b", r"\bi've\b"),
)


def _snap(fp: str, **params) -> dict:
    return {"fingerprint": fp, "history_end": 1, "params": params}


def _refusal_envelope(module: str, param: str, code: str = "not_pinned") -> dict:
    return {
        "ok": False,
        "outcome": f"error:{code}",
        "error": {
            "code": code,
            "detail": {
                "refused": [{"module": module, "param": param, "code": code, "status": "candidate"}]
            },
            "fix": "this parameter has no pinned darktable action path",
            "message": f"'{param}' cannot run: not reachable",
        },
    }


_not_pinned_envelope = _refusal_envelope


def _bare_refusal_case(cid: str, module: str, param: str) -> None:
    EXAMPLES[cid]  # documents the case
    cases = {c.id: c for c in coverage.phrase_cases("olympus")}
    real_id = EXAMPLES[cid]["input"]["case"]
    case = cases[real_id]
    # Oracle A: the generator's own meta flag.
    assert case.meta.get("accept_refusal") is True
    assert case.expect.kind == "edit"
    # Oracle B: the evaluator's verdict on a synthetic observation shaped
    # exactly like the recorded case json's own refusal envelope.
    obs = Observation(
        calls=[
            CallRecord(
                "darkroom_say", {}, _not_pinned_envelope(module, param), wall_s=0.01, write=True
            )
        ],
        before=_snap("a"),
        after=_snap("a"),
    )
    v = evaluate(case, obs, POLICY)
    assert (v.score, v.outcome) == ("pass", "correct_refusal")


def test_crop_family_bare_phrase_refuses_not_pinned():
    _bare_refusal_case("crop-family-bare-phrase-refuses-not-pinned", "crop", "left")


def test_sharpen_family_bare_phrase_refuses_not_pinned():
    _bare_refusal_case("sharpen-family-bare-phrase-refuses-not-pinned", "sharpen", "amount")


def test_rotate_family_p4_property_sequence_all_refuse():
    EXAMPLES["rotate-family-p4-property-sequence-all-refuse"]  # documents the case
    cases = {c.id: c for c in coverage.property_cases("olympus")}
    case = cases["cov-p4-rotate_left-olympus"]
    # Oracle A: the generator's own meta flag and expect kind.
    assert case.meta.get("accept_refusal") is True
    assert case.expect.kind == "property"
    # Oracle B: the evaluator never gets a real M1 pair (every "say" in the
    # sequence refuses honestly first) but still passes as a refusal.
    obs = Observation(
        calls=[
            CallRecord(
                "darkroom_say",
                {},
                _not_pinned_envelope("ashift", "rotation"),
                wall_s=0.01,
                write=True,
            )
        ],
        before=_snap("a"),
        after=_snap("a"),
        extra={"snapshots": [{"ashift.rotation": 0.0}] * 6},
    )
    v = evaluate(case, obs, POLICY)
    assert (v.score, v.outcome) == ("pass", "correct_refusal")


def test_pinned_live_phrase_still_expects_a_plain_edit():
    EXAMPLES["pinned-live-phrase-still-expects-a-plain-edit"]  # documents the case
    cases = {c.id: c for c in coverage.phrase_cases("olympus")}
    case = cases["cov-phrase-brighter-olympus-bare"]
    # Oracle A: the generator never sets accept_refusal for a pinned param.
    assert case.meta.get("accept_refusal") is not True
    assert case.expect.kind == "edit"
    assert case.expect.checks
    # Oracle B: the evaluator has no honest-refusal escape hatch for it --
    # a refusal on this case scores wrong_refusal, not a pass.
    obs = Observation(
        calls=[
            CallRecord(
                "darkroom_say",
                {},
                _not_pinned_envelope("exposure", "exposure"),
                wall_s=0.01,
                write=True,
            )
        ],
        before=_snap("a"),
        after=_snap("a"),
    )
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "wrong_refusal"


def test_crop_family_wrong_refusal_code_is_not_accepted():
    """F3 fix round 1: `accept_refusal` names `not_pinned` specifically
    (row 523 passes the HONEST not_pinned refusal, not any refusal at
    all) -- a refusal on the SAME case, same module/param, but with a
    different policy code (`usage`, a wrong-argument mistake; `disk_full`,
    an unrelated environment fault) must still fail, not pass."""
    cases = {c.id: c for c in coverage.phrase_cases("olympus")}
    case = cases["cov-phrase-crop_16x9-olympus-bare"]
    assert case.meta.get("refusal_codes") == ["not_pinned"]
    # A code the POLICY itself still recognises as a refusal (so the outer
    # `_code(...) in policy.refusal_codes` gate would have let it through
    # before this fix): must score `wrong_refusal`, never a pass.
    for code in ("usage", "unreachable"):
        obs = Observation(
            calls=[
                CallRecord(
                    "darkroom_say",
                    {},
                    _refusal_envelope("crop", "left", code=code),
                    wall_s=0.01,
                    write=True,
                )
            ],
            before=_snap("a"),
            after=_snap("a"),
        )
        v = evaluate(case, obs, POLICY)
        assert v.outcome == "wrong_refusal", (code, v.outcome)
    # A code the policy does not even recognise as a refusal at all
    # (`disk_full`, `not_in_darkroom`) -- never a pass either way, but
    # scored generically rather than as a typed wrong refusal.
    for code in ("disk_full", "not_in_darkroom"):
        obs = Observation(
            calls=[
                CallRecord(
                    "darkroom_say",
                    {},
                    _refusal_envelope("crop", "left", code=code),
                    wall_s=0.01,
                    write=True,
                )
            ],
            before=_snap("a"),
            after=_snap("a"),
        )
        v = evaluate(case, obs, POLICY)
        assert v.score != "pass", (code, v.outcome)


def test_crop_family_refusal_naming_an_unrelated_param_is_not_accepted():
    """F3 fix round 1, remedy 2: `error.detail.refused[*]` must name one of
    THIS family's own candidate keys (`candidate_keys`) -- a `not_pinned`
    refusal on some other, unrelated candidate param must not be credited
    to this case just because the policy code matches."""
    cases = {c.id: c for c in coverage.phrase_cases("olympus")}
    case = cases["cov-phrase-crop_16x9-olympus-bare"]
    assert "crop.left" in case.meta.get("candidate_keys", [])
    obs = Observation(
        calls=[
            CallRecord(
                "darkroom_say", {}, _refusal_envelope("sharpen", "amount"), wall_s=0.01, write=True
            )
        ],
        before=_snap("a"),
        after=_snap("a"),
    )
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "wrong_refusal"
