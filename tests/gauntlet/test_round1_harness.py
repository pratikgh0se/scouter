"""Round 1 non-looks harness fixes (fix/harness-r1): C6 taxonomy, C9 scenario
silents, C11 lens_on before-snapshot, C17 parity fixtures, C18 guide cases.

Fixtures are the REAL round-1 observations (tests/gauntlet/data/
round1_harness_observations.json, copied read-only from round1-20261109).
Every fix has a guard proving a REAL failure of the same shape still fails
(Zenkai Z2: the sidecar and the stat are separate oracles). These tests only
use the layers' public entry points (generate), never new helper names."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gauntlet.adapter import CallRecord, Observation
from gauntlet.evaluate import evaluate
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import coverage, guide, scenarios
from tests.gauntlet.policy import POLICY

FX = json.loads((Path(__file__).parent / "data" / "round1_harness_observations.json").read_text())


@pytest.fixture(scope="module")
def scn():
    tool, agent = scenarios.generate(taxonomy.load())
    return {c.id: c for c in tool}


def obs_of(cid: str, mutate=None) -> Observation:
    o = copy.deepcopy(FX[cid]["observation"])
    if mutate:
        mutate(o)
    o["calls"] = [CallRecord(**c) for c in o["calls"]]
    return Observation(**o)


# ------------------------------------------------------------ C6 taxonomy


def test_c6_phrases_the_vocabulary_serves_are_not_scored_as_refusals(scn):
    """`warm it up`, `brighten`, `open the shadows` are live vocabulary rows:
    their "out" requests are normal served edits, with a stat check."""
    served = [c for c in scn.values() if c.meta["class"] == "out"
              and c.meta["roadmap_key"] in ("temperature", "exposure", "toneequal")]
    assert len(served) >= 40
    for c in served:
        assert c.expect.kind in ("edit", "typed_error"), c.id
        assert c.meta.get("served_out_class") is True, c.id
        if c.expect.kind == "edit":
            assert any(ch.metric in ("M2", "M4") for ch in c.expect.checks), c.id


def test_c6_unserved_phrases_still_expect_an_honest_refusal(scn):
    """Derived from the real resolver, not a hand list: every remaining
    out/impossible refuse case's phrase either does not resolve or makes a
    move whose vocabulary row is not live."""
    from scouter.core import phrases, vocab
    from scouter.errors import ScouterError

    refuse = [c for c in scn.values() if c.meta["class"] in ("out", "impossible")
              and not c.meta.get("served_out_class")]
    assert len(refuse) > 300
    for c in refuse:
        assert c.expect.kind == "refuse", c.id
        phrase = c.steps[0].args["phrase"]
        try:
            res = phrases.resolve(phrase)
        except ScouterError:
            continue
        assert any(vocab.status(phrases.row_for(m)) != "live" for m in res.moves), \
            f"{c.id}: {phrase!r} is served but still expects a refusal"


# ------------------------------------------------------------ C9 silents


def test_c9_m6_spread_after_a_darker_step_is_a_declared_gap_not_silent(scn):
    c = scn["scn-r0214-sony_a7-tool"]
    v = evaluate(c, obs_of(c.id), POLICY)
    assert v.silent_failure is None
    assert v.outcome == "confounded_check" and v.score == "partial"
    m6 = [r for r in v.checks if r.metric == "M6"][0]
    assert not m6.ok and "declared gap" in m6.note  # stays in the denominator


def test_c9_hue_only_highlights_step_is_not_blamed_for_the_contrast_shift(scn):
    for cid in ("scn-r0326-sony_a7-tool", "scn-r0330-sony_a7-tool"):
        c = scn[cid]
        v = evaluate(c, obs_of(cid), POLICY)
        assert v.silent_failure is None, cid
        assert v.score != "fail", cid
        assert any("declared gap" in r.note for r in v.checks), cid


def test_c9_confounded_check_that_passes_still_passes(scn):
    c = scn["scn-r0214-sony_a7-tool"]

    def flip(o):  # the spread really rose after the contrast step
        o["stats_after"]["frame"]["spread"] = o["stats_before"]["frame"]["spread"] * 3

    v = evaluate(c, obs_of(c.id, flip), POLICY)
    assert v.score == "pass" and v.outcome == "verified"


def test_c9_a_contrast_step_that_changed_nothing_is_still_silent(scn):
    """Z2 guard: the declared gap does not hide a write that never landed. The
    sidecar oracle (history/fingerprint) still catches it."""
    c = scn["scn-r0214-sony_a7-tool"]

    def nothing(o):
        o["after"] = copy.deepcopy(o["before"])
        o["extra"]["snapshots"] = [o["extra"]["snapshots"][0]] * len(o["extra"]["snapshots"])

    v = evaluate(c, obs_of(c.id, nothing), POLICY)
    assert v.score == "fail", (v.outcome, v.silent_failure)


def test_c9_first_step_refusal_does_not_blame_the_next_steps_floor(scn):
    c = scn["scn-r0374-sony_a7-tool"]
    v = evaluate(c, obs_of(c.id), POLICY)
    assert v.silent_failure is None
    assert v.score == "partial"
    assert any("honestly refused" in r.note for r in v.checks)  # a declared gap, in the denominator


def test_c9_every_check_gapped_is_never_a_free_pass(scn):
    """When the refused step owns the only stat check there is nothing left
    to judge: score it normally (a plain failure), never needs_review."""
    c = scn["scn-r0374-sony_a7-tool"]
    c2 = copy.deepcopy(c)
    c2.expect.checks = c2.expect.checks[:1]  # only the refused step's own check
    v = evaluate(c2, obs_of(c.id), POLICY)
    assert v.outcome != "needs_review" and v.score != "pass"


# ------------------------------------------------------------ C11 lens_on


def test_c11_lens_on_setup_disable_is_snapshotted():
    case = next(c for c in coverage.generate() if c.id == "cov-p4-lens_on-olympus")
    assert case.steps[0].tool == "darkroom_disable"
    assert case.steps[0].setup and case.steps[0].write, \
        "the disable must re-snapshot, or the before snapshot predates it"


def test_c11_stale_before_snapshot_was_the_silent_failure_and_fixed_one_passes():
    case = next(c for c in coverage.generate() if c.id == "cov-p4-lens_on-olympus")
    stale = evaluate(case, obs_of(case.id), POLICY)
    assert stale.silent_failure and "lens.__enabled" in stale.silent_failure  # recorded bug

    def after_disable(o):  # the snapshot the fixed executor takes after the disable
        o["extra"]["snapshots"][1] = o["extra"]["snapshots"][3]

    fixed = evaluate(case, obs_of(case.id, after_disable), POLICY)
    assert fixed.silent_failure is None and fixed.score == "pass"


# ------------------------------------------------------------ C18 guide


def _guide_cases():
    return {c.id: c for c in guide.generate()}


def test_c18_dry_run_job_expects_the_documented_exit_5():
    from scouter.errors import EXIT_CODES

    c = _guide_cases()["gd-18-guide_job_run_dry"]
    exit_check = next(ch for ch in c.expect.checks if ch.metric == "extra")
    assert exit_check.target["value"] == EXIT_CODES["dry_run"] == 5


def test_c18_edu_full_cases_set_up_their_own_precondition():
    cases = _guide_cases()
    for cid in ("gd-edu-full-0-brighter", "gd-edu-full-1-darker", "gd-edu-full-4-more_contrast"):
        c = cases[cid]
        assert c.steps[0].tool == "reset_explained", cid
        at = c.expect.checks[0].step
        assert c.steps[at].args["phrase"] == cid.split("-", 4)[-1].replace("_", " ")
    darker = cases["gd-edu-full-1-darker"]
    # the second move on a concept plays the first itself, then expects ONE line
    assert [s.tool for s in darker.steps] == ["reset_explained", "darkroom_say", "darkroom_say"]
    assert darker.steps[1].args["phrase"] == "brighter"
    assert darker.expect.checks[0].target == {"path": "data.explain", "op": "len_eq", "value": 1}
    first = cases["gd-edu-full-0-brighter"]
    assert first.expect.checks[0].target["op"] == "len_gte"  # paragraph + line on first sight


def test_c18_reset_explained_clears_only_the_explained_concepts(tmp_path):
    from types import SimpleNamespace

    from tests.gauntlet import exec_guide  # noqa: F401  (registers the virtual tool)
    from tests.gauntlet.execute import VIRTUAL

    state = tmp_path / "state"
    state.mkdir()
    (state / "job1.json").write_text(json.dumps(
        {"explained_concepts": ["exposure"], "last_image": "x"}))
    session = SimpleNamespace(staged=SimpleNamespace(env={"SCOUTER_JOB": "job1"}, cache=tmp_path))
    obs = Observation()
    rec = VIRTUAL["reset_explained"](session, None, None, obs)
    assert rec.envelope["data"]["cleared"] == ["exposure"]
    assert json.loads((state / "job1.json").read_text()) == {"last_image": "x"}


def test_a_priori_confounded_gap_with_passing_checks_is_never_a_pass():
    """Z2: a check declared `confounded by ...` leaves one oracle for its
    component, so the case caps at partial needs_review even when the rest pass."""
    from gauntlet.case import Case, Check, Expect, Step

    ok = Check("envelope", step=0, target={"path": "ok", "op": "eq", "value": True})
    gap = Check("M6", stat="spread", sign=1, tags={"declared_gap": "confounded by x"})
    case = Case(id="syn-confounded", layer="looks", tier="P1", mode="tool",
                steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
                expect=Expect("workflow", [ok, gap]), body="olympus", frame_role="hero",
                meta={"look": True})
    obs = Observation(calls=[CallRecord("darkroom_say", {"phrase": "brighter"},
                                        {"ok": True, "outcome": "ok", "data": {}}, write=True)])
    v = evaluate(case, obs, POLICY)
    assert (v.score, v.outcome) == ("partial", "needs_review")
    assert any("one oracle (Z2)" in n for n in v.notes)
