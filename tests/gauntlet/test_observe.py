"""tests/gauntlet/observe.py -- the adapter-side param-knowledge functions
(`defaults`, `verified_defaults`, `param_meta`). Unlike `gauntlet/tests/
test_evaluate.py` (the pure engine, which may import only stdlib +
`gauntlet.*` -- `gauntlet/tests/test_case_store.py
::test_engine_is_stdlib_only_and_imports_no_product`), this file sits on the
adapter side of that boundary and can import the product directly."""

from __future__ import annotations

from gauntlet.adapter import Observation
from gauntlet.evaluate import POINTS, evaluate
from gauntlet.tests.test_evaluate import POLICY, _cz_call, _cz_case, _cz_chk, _cz_snap
from scouter.core import vocab
from tests.gauntlet import observe


def test_observe_verified_defaults_matches_vocab_default_rows_exactly():
    """Review round 2, Important R2-I1's stronger option: pin
    `observe.verified_defaults()` itself, independent of any M1 fallback
    test -- without this, mutating it to `return defaults()` (exactly the
    round-1 false-pass bug, `neutral` rows counting as defaults) left every
    M1 fallback test green, because none of them consulted the real
    function. Two oracles: vocab's own `default is not None` predicate
    (oracle A, never `verified_defaults()`'s own logic) against what the
    function actually returns (oracle B)."""
    want = {f"{s.module}.{s.param}": s.default for s in vocab.PARAMS if s.default is not None}
    got = observe.verified_defaults()
    assert got == want
    assert want  # sanity: the vocab rows with a real default are not empty
    # every neutral-only row (has neutral, no default) must be absent.
    neutral_only = {f"{s.module}.{s.param}" for s in vocab.PARAMS
                    if s.default is None and s.neutral is not None}
    assert neutral_only and not (neutral_only & got.keys())


def test_m1_declared_default_fallback_rejects_neutral_only_key_via_real_adapter():
    """The same negative case as `gauntlet/tests/test_evaluate.py
    ::test_m1_declared_default_fallback_neutral_only_key_still_fails`, but
    driven through the REAL `observe.verified_defaults()` (review round 2,
    Important R2-I1a) instead of a hard-coded `{}` that stays green even if
    that function regresses to include `neutral` rows -- the exact round-1
    bug the reviewer's mutation (`verified_defaults = defaults`) reproduced
    against every round-1 test."""
    vd = observe.verified_defaults()
    # oracle A: the raw vocab fact this whole test depends on.
    assert "grain.strength" not in vd and "grain.strength" in observe.defaults()
    chk = _cz_chk(target=0.0, key="grain.strength")
    case = _cz_case(chk=chk, key="grain.strength")
    obs = Observation(calls=[_cz_call(0.0, True, 0.0, module="grain", param="strength")],
                      before=_cz_snap("a", verified_defaults=vd),
                      after=_cz_snap("a", verified_defaults=vd))
    # oracle B: the fixed evaluator, on the real adapter's own table.
    v = evaluate(case, obs, POLICY)
    assert v.checks[0].measured is None
    assert POINTS[v.score] == 0.0


def test_m1_declared_default_fallback_accepts_a_real_verified_default_via_real_adapter():
    """Mirror of the test above, positive side: `colorzones.strength` IS a
    real, host-probed vocab `default` (0.0, commit 9ae3978), driven through
    `observe.verified_defaults()` -- the fallback must accept it and score a
    pass. This is the test in this file that discriminates the mutation
    engine's own revert-mutation check against `gauntlet/evaluate.py`
    itself: with `_m1_default_fallback` reverted to base (no fallback at
    all), the SAME observation would score `fail`/`measured: None` instead,
    so this test -- unlike the negative one above, which fails identically
    with or without the fallback -- actually goes red when the production
    fix it names is undone."""
    vd = observe.verified_defaults()
    # oracle A: the raw vocab fact this whole test depends on.
    assert vd.get("colorzones.strength") == 0.0
    obs = Observation(calls=[_cz_call(0.0, True, 0.0)],
                      before=_cz_snap("a", verified_defaults=vd),
                      after=_cz_snap("a", verified_defaults=vd))
    # oracle B: the fixed evaluator, on the real adapter's own table.
    v = evaluate(_cz_case(), obs, POLICY)
    assert (v.score, v.outcome) == ("pass", "verified")
    assert v.checks[0].measured == 0.0


#: The 10 colorbalancergb rows a host probe promoted from `neutral`-only to
#: a verified `default` (card t_cc03dcb4, decision row 560); kept in sync by
#: hand with `tests/test_host_phase3.py::_CB_NEUTRAL_ROWS`, which names the
#: same 10 rows for the probe that measures them. Listing all 10 here (not a
#: sample) matters (fix round 1, I3): reverting any ONE row's promotion in
#: `scouter/core/vocab.py` must fail this test, not just the golden fixture.
_CB_PROMOTED_ROWS = (
    "contrast", "global_chroma", "global_saturation", "global_vibrance",
    "shadows_luminance", "shadows_chroma",
    "midtones_luminance", "midtones_chroma",
    "highlights_luminance", "highlights_chroma",
)


def test_m1_declared_default_fallback_accepts_the_colorbalancergb_neutral_promotions():
    """Card t_cc03dcb4 (decision row 560, follows t_29a9ef47/rows 548/549/554):
    mirror of the colorzones test above for all 10 `_CB_PROMOTED_ROWS`
    (were `neutral`-only, excluded from `verified_defaults()` by design, I1
    of the M1 fix, until a host probe verified each at 0.0 and promoted
    `default` in `scouter/core/vocab.py`; see docs/measurements.md
    "colorbalancergb neutral-only defaults" for the run this pins to),
    driven through the REAL `observe.verified_defaults()`, not a hard-coded
    dict, so a regression that drops any ONE row's promotion (or reverts
    the function to `defaults()`, the round-1 false-pass bug) fails this
    test at that row."""
    vd = observe.verified_defaults()
    for param in _CB_PROMOTED_ROWS:
        module = "colorbalancergb"
        key = f"{module}.{param}"
        # oracle A: the raw vocab fact this whole example depends on.
        assert vd.get(key) == 0.0, key
        obs = Observation(
            calls=[_cz_call(0.0, True, 0.0, module=module, param=param)],
            before=_cz_snap("a", verified_defaults=vd),
            after=_cz_snap("a", verified_defaults=vd),
        )
        # oracle B: the fixed evaluator, on the real adapter's own table.
        v = evaluate(_cz_case(chk=_cz_chk(target=0.0, key=key), key=key), obs, POLICY)
        assert (v.score, v.outcome) == ("pass", "verified"), key
        assert v.checks[0].measured == 0.0, key
