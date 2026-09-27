import collections
import json
import shutil

import pytest

from gauntlet.case import Case, Step
from tests.gauntlet import checkmap, p0, taxonomy


@pytest.fixture(scope="module")
def tax():
    return taxonomy.load()


def test_counts_match_spec_section_0(tax):
    assert len(tax.modules) == 69 and len(tax.unique_phrases()) == 378
    assert len(tax.looks) == 234 and len(tax.requests) == 601


def test_a_silent_data_edit_fails_loudly(tmp_path):
    for f in taxonomy.DATA.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    doc = json.loads((tmp_path / "requests.json").read_text())
    doc["requests"].pop()
    (tmp_path / "requests.json").write_text(json.dumps(doc))
    with pytest.raises(taxonomy.TaxonomyError, match="requests: expected 601, got 600"):
        taxonomy.load(tmp_path)


def test_every_request_maps_no_unmapped(tax):
    """(row 501 item I, Pratik's ruling): r0066 ("client proof gallery")
    moves out -> workflow/export via check_map.json's workflow_verb_overrides
    -- export_release has a client-proof preset, so it is in scope."""
    plans = [checkmap.plan_request(r, tax.check_map) for r in tax.requests]
    classes = collections.Counter(p.klass for p in plans)
    assert classes["unmapped"] == 0
    assert classes == {"out": 322, "in": 168, "impossible": 60, "workflow": 38, "partial": 13}
    r0066 = next(p for p in plans if p.request_id == "r0066")
    #: (fix round 1, Opus finding 4): a dedicated export_release verb, not
    #: the generic export_run one -- the ruling names "export_release with
    #: the client-proof preset on a staged job", a job-level release path
    #: export_run never exercises.
    assert r0066.klass == "workflow" and r0066.workflow_verb == "export_release"


def test_split_and_direction_examples(tax):
    cm = tax.check_map
    assert checkmap.split_parts("dynamic range compression (shadow up, highlight down)", cm) == \
        ["shadow up", "highlight down"]
    assert checkmap.direction_of("increase, limited", cm)["kind"] == "capped"
    assert checkmap.direction_of("decrease toward 0", cm)["sign"] == -1
    assert checkmap.direction_of("step back by 2", cm)["steps"] == 2
    plan = checkmap.plan_request({"id": "x", "intent": "tonal", "expected": {
        "kind": "edit", "check": {"stat_or_param": "color temp up, mean luminance up",
                                  "direction": "both"}}}, cm)
    assert plan.klass == "in" and plan.capabilities == ("warmer", "brighter")


def test_every_capability_is_reachable_from_a_concept(tax):
    caps = {c for concept in tax.check_map["concepts"].values() for c in concept["caps"].values()}
    assert caps <= set(tax.check_map["capabilities"])


def test_p0_selection_is_100_with_the_genre_floor(tax):
    sel = p0.select_p0(tax.requests)
    assert len(sel) == 100 and len({r["id"] for r in sel}) == 100
    genres = collections.Counter(r["genre"] for r in sel)
    assert len(genres) == 14 and min(genres.values()) >= 3
    assert not any(r["intent"] == "impossible" for r in sel)
    assert sel == p0.select_p0(tax.requests)  # deterministic


def test_quotas_sum_to_100_with_floor_one():
    q = p0.quotas({"a": 71, "b": 1, "c": 469})
    assert sum(q.values()) == 100 and q["b"] == 1


def test_wb_route_a_block_loads_and_resolves(tax):
    """Route A (decision rows 458/461): the check map carries the preset-4
    warmth expectations as data, and they resolve both before and after the
    pin."""
    cmap = tax.check_map
    assert checkmap.route_a_pinned(cmap) is False  # candidate until the host probe
    before = checkmap.wb_route_a_expect(cmap, "warmer", 4, pinned=False)
    assert before == {"expect": "typed_error", "codes": ["module_unavailable"]}
    after = checkmap.wb_route_a_expect(cmap, "temperature.temperature", 4, pinned=True)
    assert after["m1_key"] == "channelmixerrgb.temperature"
    assert after["replaces"] == "temperature.temperature" and after["unit"] == "K"
    assert after["allowed"] == ["channelmixerrgb.x", "channelmixerrgb.y"]
    assert after["unchanged"] == ["temperature.preset"]
    assert after["direction_verified"] is False
    assert checkmap.wb_route_a_expect(cmap, "warmer", 2, pinned=True) is None
    assert checkmap.wb_route_a_expect(cmap, "brighter", 4, pinned=True) is None
    from scouter.core import vocab
    assert vocab.spec("channelmixerrgb", "temperature") is not None


def test_wb_route_a_pinned_edit_builds_the_pinned_expect(tax):
    """Review round 2, finding 10 (GC-1): shared through checkmap.py so
    every generator (coverage.py, and now layers/guide.py) scores a pinned
    Route A move the same way -- the channelmixerrgb.temperature key, its
    derived x/y keys, the untouched WB preset, and (review round 3, GR-1) a
    `sign_unverified` tag -- never `needs_review`, which collides with
    install's own evaluator-reactive tag key -- on the move check while
    direction_verified is still False."""
    cmap = tax.check_map
    route = checkmap.wb_route_a_expect(cmap, "temperature.temperature", 4, pinned=True)
    exp = checkmap.wb_route_a_pinned_edit(route, sign=1)
    assert exp.kind == "edit"
    move, unchanged = exp.checks
    assert move.target["key"] == "channelmixerrgb.temperature"
    assert move.kind == "changed" and move.sign == 0  # direction_verified False: no guessed sign
    assert move.tags.get("sign_unverified")
    assert "needs_review" not in move.tags
    assert unchanged.target["keys"] == ["temperature.preset"]
    assert exp.sidecar == {"changed": True, "key": "channelmixerrgb.temperature",
                           "units": {"channelmixerrgb.temperature": "K"},
                           "allowed": ["channelmixerrgb.x", "channelmixerrgb.y"]}
    # an explicit value skips the sign branch entirely -- and (GR-1) is
    # NEVER tagged: a wrong landed value there is a real product failure.
    exp_val = checkmap.wb_route_a_pinned_edit(route, value=6500.0)
    assert exp_val.checks[0].target == {"key": "channelmixerrgb.temperature", "value": 6500.0,
                                        "unit": "K"}
    assert exp_val.checks[0].tags == {}


def test_wb_route_a_pinned_equals_miss_never_scores_needs_review(tax):
    """GR-1's own probe, made a test: Route A pinned, wanted 3300 K, the
    product lands 5800 K. Before this fix, `equals` checks still carried
    the (then-shared) tag, so this scored `partial needs_review` with the
    note "sign unverified" -- a wrong value, a genuine product failure,
    relabelled as a pending decision with a false reason. It must score a
    real failure/partial, and never `needs_review`."""
    from gauntlet.adapter import CallRecord, Observation
    from gauntlet.evaluate import Policy, evaluate

    cmap = tax.check_map
    route = checkmap.wb_route_a_expect(cmap, "temperature.temperature", 4, pinned=True)
    exp = checkmap.wb_route_a_pinned_edit(route, value=3300.0)
    case = Case(id="c", layer="coverage", tier="P0", mode="tool",
               steps=[Step("darkroom_set", {}, budget_s=3.5, write=True)],
               expect=exp, body="olympus", frame_role="hero")
    policy = Policy(refusal_codes=frozenset(), transient_codes=frozenset(),
                    refusal_markers=(), claim_markers=())
    before = {"fingerprint": "a", "params": {"channelmixerrgb.temperature": 6500.0,
                                             "temperature.preset": 4}}
    after = {"fingerprint": "b", "params": {"channelmixerrgb.temperature": 5800.0,  # wanted 3300
                                            "temperature.preset": 4}}
    obs = Observation(calls=[CallRecord("darkroom_set", {}, {"ok": True, "outcome": "ok",
                                                              "data": {}}, write=True)],
                      before=before, after=after)
    v = evaluate(case, obs, policy)
    assert v.outcome != "needs_review", v
    assert v.score in ("fail", "partial"), v


def test_wb_route_a_tint_always_refuses_on_preset_4(tax):
    """Unlike warmer/cooler/temperature.temperature, tint has no
    color-calibration reroute target -- pinning channelmixerrgb.temperature
    makes no difference to it (row 461's tint extension)."""
    cmap = tax.check_map
    always = checkmap.wb_route_a_tint_expect(cmap, 4)
    assert always == {"expect": "typed_error", "codes": ["module_unavailable"]}
    # pin does not matter for tint -- there is no pinned parameter to check,
    # so the function does not even take one.
    assert checkmap.wb_route_a_tint_expect(cmap, 2) is None
    assert checkmap.wb_route_a_tint_expect(cmap, None) is None


def test_wb_route_a_undo_mirrors_a_refused_antecedent(tax):
    """An undo right after a WB route-A move that was itself refused has
    nothing to restore -- the journal is empty for this job, because
    refuse_wb_late runs before journal.begin -- so it must answer its own
    fixed code, darkroom_undo_last's "noop" outcome (fix round 1, review
    I2), never the antecedent's own module_unavailable (the undo call never
    reaches refuse_wb_late itself)."""
    cmap = tax.check_map
    refused = checkmap.wb_route_a_expect(cmap, "warmer", 4, pinned=False)
    undo = checkmap.wb_route_a_undo_expect(cmap, refused)
    assert undo == {"expect": "typed_error", "codes": ["noop"]}

    tint_refused = checkmap.wb_route_a_tint_expect(cmap, 4)
    undo_tint = checkmap.wb_route_a_undo_expect(cmap, tint_refused)
    assert undo_tint == {"expect": "typed_error", "codes": ["noop"]}

    # a live (pinned) route did write, so the ordinary undo check applies --
    # wb_route_a_undo_expect defers by returning None.
    live = checkmap.wb_route_a_expect(cmap, "temperature.temperature", 4, pinned=True)
    assert checkmap.wb_route_a_undo_expect(cmap, live) is None
    assert checkmap.wb_route_a_undo_expect(cmap, None) is None


def test_wb_route_a_nudge_always_refuses_pinned_or_not(tax):
    """Fix round 1 (review I4): darkroom.nudge never re-routes onto color
    calibration at all -- unlike set/adjust, pinning
    channelmixerrgb.temperature makes no difference to it, the same shape
    as tint."""
    cmap = tax.check_map
    always = checkmap.wb_route_a_nudge_expect(cmap, 4)
    assert always == {"expect": "typed_error", "codes": ["module_unavailable"]}
    assert checkmap.wb_route_a_nudge_expect(cmap, 2) is None
    assert checkmap.wb_route_a_nudge_expect_for_hero(cmap) == always


def test_wb_route_a_xmp_offsets_and_tolerance_match_the_product(tax):
    """N2 (review): the check-map's copied XMP byte offsets and verify
    tolerance must not rot silently against the product they describe
    (Zenkai: an always-on detector, not a one-time transcription)."""
    from scouter.core import moves, xmp

    r = tax.check_map["wb_route_a"]
    layout = xmp.LAYOUTS[("channelmixerrgb", 3)]
    fields = {name: off for name, _fmt, off in layout["fields"]}
    o = r["xmp_offsets"]
    assert o["modversion"] == 3
    assert (o["illuminant"], o["adaptation"], o["x"], o["y"], o["temperature"]) == (
        fields["illuminant"], fields["adaptation"], fields["x"], fields["y"],
        fields["temperature"])
    assert r["wb_verify_tolerance"]["value"] == pytest.approx(moves._WB_VERIFY_TOLERANCE)
