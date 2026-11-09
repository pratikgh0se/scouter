import collections

from gauntlet.case import Case, Expect
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import looks


def test_looks_count_gaps_and_tags():
    cases = looks.generate(taxonomy.load())
    assert len(cases) == looks.EXPECTED == 469
    chart = [c for c in cases if c.body == "chart"]
    assert len(chart) == 234
    assert all(len(c.meta["check_tags"]) == len(c.expect.checks) for c in chart)
    gaps = collections.Counter(g for c in chart for g in c.meta["gaps"])
    assert gaps["grain"] == 78 and gaps["lut3d"] == 64
    assert cases[0].id == "lk-golden-chart"


def _switches(cases) -> int:
    roles = [c.frame_role for c in cases]
    return sum(1 for a, b in zip(roles, roles[1:]) if a != b)


def test_generate_orders_cases_to_minimise_chart_hero_switches():
    """Night-1 fix (class LK-frame-switch-relaunch): darktable cannot switch
    onto a frame imported earlier in the same session without a relaunch
    (~45-60 s), so the raw generation order (chart, hero, chart, hero, ...)
    cost one switch per case. Grouping by frame_role is a pure sort: it must
    not change which or how many cases exist, only their order.

    Row LK-hero-below-floor added a third distinct real frame (`hero_bright`,
    olympus's stand-in for its too-dark `hero`); it maps 1:1 with body, so
    grouping by role still groups by body. With 3 distinct frames the floor
    on total switches is 2 (chart -> real, then hero <-> hero_bright), not 1
    -- verified below against `looks.FRAME_ROLE_ORDER` itself so this bound
    tracks the real number of roles rather than a hand-picked constant."""
    cases = looks.generate(taxonomy.load())
    assert _switches(cases) <= len(looks.FRAME_ROLE_ORDER) - 1
    assert sorted(c.id for c in cases) == sorted(
        {c.id for c in looks.generate(taxonomy.load())})


def _case(cid: str, role: str) -> Case:
    return Case(id=cid, layer="looks", tier="P1", mode="tool", steps=[],
                expect=Expect("edit"), frame_role=role)


def test_order_by_frame_role_is_a_stable_sort_on_a_fixture_set():
    """A small fixture standing in for the raw interleaved order: 1 chart-only
    golden case, then 4 (chart, hero) pairs -- 7 switches raw, at most 1 after
    the sort, with every id preserved and grouped in its original order."""
    raw = [_case("lk-golden-chart", "chart")]
    for i in range(4):
        raw.append(_case(f"lk-look{i}-chart", "chart"))
        raw.append(_case(f"lk-look{i}-hero", "hero"))
    assert _switches(raw) == 7

    ordered = looks.order_by_frame_role(raw)
    assert [c.id for c in ordered] == (
        ["lk-golden-chart"] + [f"lk-look{i}-chart" for i in range(4)] +
        [f"lk-look{i}-hero" for i in range(4)])
    assert _switches(ordered) <= 1
    assert {c.id for c in ordered} == {c.id for c in raw}


def test_frame_role_order_covers_every_role_generate_produces():
    """Merge-exposed bug (this integration): `hero_bright` was added to
    `generate()`'s per-case `frame_role` (row LK-hero-below-floor) without
    being added to `FRAME_ROLE_ORDER`, so `order_by_frame_role`'s
    `FRAME_ROLE_ORDER.index(c.frame_role)` raised a bare `ValueError` for
    every case on olympus -- a crash, not a typed failure. `generate()`
    itself calls `order_by_frame_role` before this test ever gets its own
    cases back, so a stale `FRAME_ROLE_ORDER` still surfaces as that same
    `ValueError`, not as this assertion -- this test does not intercept it.
    What it does is pin `FRAME_ROLE_ORDER` as a named, checked invariant
    (every role `generate()` can produce must be listed here) rather than
    an implicit assumption baked into the sort, so the next new role's
    author has something explicit to update and a git-blameable line to
    find, even though the failure they will actually see first is still
    the sort's own crash."""
    cases = looks.generate(taxonomy.load())
    produced = {c.frame_role for c in cases}
    assert produced <= set(looks.FRAME_ROLE_ORDER), (
        f"FRAME_ROLE_ORDER is stale: missing {sorted(produced - set(looks.FRAME_ROLE_ORDER))}")


def test_looks_real_frame_cases_use_the_bright_hero_on_olympus_only():
    """Row LK-hero-below-floor: the olympus `hero` is near the darkest frame
    in the corpus, so direction-only moves sized from the chart's
    calibration land below the noise floor on it. Real-frame look cases
    must run on `hero_bright` for olympus (sony_a7 has no bright hero yet
    and keeps the plain `hero`) -- both the case's own `frame_role` (which
    frame the runner opens) and the `look_plan` step's `image` argument
    (which frame the step is told to edit) have to name the same role, or
    the plan would be measured against one frame and applied to another."""
    cases = looks.generate(taxonomy.load())
    real = [c for c in cases if c.body in ("olympus", "sony_a7")]
    assert real and all(c.body == "olympus" or c.body == "sony_a7" for c in real)
    for c in real:
        want = "hero_bright" if c.body == "olympus" else "hero"
        assert c.frame_role == want, (c.id, c.frame_role)
        assert c.steps[0].args["image"] == "{frame:" + want + "}", (c.id, c.steps[0].args)


def test_looks_checks_name_the_planner_param_they_depend_on():
    """Z1 intent: a look's contrast check is owned by sigmoid.contrast and its warm/cool
    check by temperature.*; the evaluator uses the tag to declare a gap when the
    planner could not plan (or Scouter refused) that component."""
    cases = looks.generate(taxonomy.load())
    keys = collections.Counter(ch.tags.get("look_key") for c in cases for ch in c.expect.checks)
    assert keys["sigmoid.contrast"] and keys["temperature.temperature"]
    for c in cases:
        for ch in c.expect.checks:
            if ch.metric == "M6":
                assert ch.tags["look_key"] == "sigmoid.contrast"


def _expected_shared_zones(tone: dict) -> set[str]:
    """The intent, stated independently of the implementation: a zone is shared
    (its sign check unreliable) when another zone moves the opposite way by more
    than half its size, unless it is the largest-moving zone."""
    zones = {k: tone.get(k) or 0 for k in ("shadows_ev", "mids_ev", "highlights_ev")}
    top = max(zones, key=lambda k: abs(zones[k]))
    return {k for k, v in zones.items()
            if v and k != top and any(w and (w > 0) != (v > 0) and abs(w) * 2 > abs(v)
                                      for j, w in zones.items() if j != k)}


def test_zone_checks_are_owned_by_one_zone_when_masks_would_cross_talk():
    """Z1 intent (night 3 cluster C): 'crush shadows slightly, lift highlights
    strongly' -- the shadows patch is allowed to move with the highlights mask,
    so the shadows check is a declared gap (tagged, recorded in meta gaps) while
    the dominant zone keeps a real sign check. Looks whose zones agree keep all."""
    cases = looks.generate(taxonomy.load())
    tax = {lk["id"]: lk for lk in taxonomy.load().looks}
    seen_gap = 0
    for c in cases[1:]:
        tone = tax[c.meta["look"]]["targets"].get("tone") or {}
        if not any(tone.get(k) for k in ("shadows_ev", "mids_ev", "highlights_ev")):
            continue
        shared = _expected_shared_zones(tone)
        zone_checks = [ch for ch in c.expect.checks
                       if ch.metric == "M2" and (ch.region.startswith("patch:ramp_")
                                                 or ch.region in ("shadows", "highlights",
                                                                  "frame"))
                       and ch.stat in ("ev", "L") and ch.sign]
        gapped = [ch for ch in zone_checks if ch.tags.get("declared_gap")]
        assert len(gapped) == len(shared), c.id
        assert {g for g in c.meta["gaps"] if g.startswith("zone_crosstalk:")} == \
            {f"zone_crosstalk:{k}" for k in shared}, c.id
        assert len(zone_checks) - len(gapped) >= 1, c.id  # one owned tone check survives
        seen_gap += len(shared)
    assert seen_gap  # the taxonomy does contain cross-talk looks


def test_zone_agreeing_looks_keep_every_zone_check_asserted():
    cases = looks.generate(taxonomy.load())
    tax = {lk["id"]: lk for lk in taxonomy.load().looks}
    for c in cases[1:]:
        tone = tax[c.meta["look"]]["targets"].get("tone") or {}
        if tone and not _expected_shared_zones(tone):
            assert not any(ch.tags.get("declared_gap") for ch in c.expect.checks), c.id
