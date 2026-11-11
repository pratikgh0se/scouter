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
    assert sorted(c.id for c in cases) == sorted({c.id for c in looks.generate(taxonomy.load())})


def _case(cid: str, role: str) -> Case:
    return Case(
        id=cid,
        layer="looks",
        tier="P1",
        mode="tool",
        steps=[],
        expect=Expect("edit"),
        frame_role=role,
    )


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
        ["lk-golden-chart"]
        + [f"lk-look{i}-chart" for i in range(4)]
        + [f"lk-look{i}-hero" for i in range(4)]
    )
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
    assert produced <= set(
        looks.FRAME_ROLE_ORDER
    ), f"FRAME_ROLE_ORDER is stale: missing {sorted(produced - set(looks.FRAME_ROLE_ORDER))}"


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
    (its sign check unreliable) when another zone moves the opposite way by at
    least half its size (so -0.2 against +0.1 shares both zones)."""
    zones = {k: tone.get(k) or 0 for k in ("shadows_ev", "mids_ev", "highlights_ev")}
    return {
        k
        for k, v in zones.items()
        if v
        and any(
            w and (w > 0) != (v > 0) and abs(w) * 2 >= abs(v) for j, w in zones.items() if j != k
        )
    }


def _zone_checks(c):
    return [
        ch
        for ch in c.expect.checks
        if ch.metric == "M2"
        and (ch.region.startswith("patch:ramp_") or ch.region in ("shadows", "highlights", "frame"))
        and ch.stat in ("ev", "L")
        and ch.sign
    ]


def test_zone_checks_are_owned_by_one_zone_when_masks_would_cross_talk():
    """Z1 intent (night 3 cluster C): 'crush shadows slightly, lift highlights
    strongly' -- the shadows patch is allowed to move with the highlights mask,
    so a zone check whose zone clashes with an opposing zone is a declared gap
    (tagged, recorded in meta gaps). Looks whose zones agree keep all."""
    cases = looks.generate(taxonomy.load())
    tax = {lk["id"]: lk for lk in taxonomy.load().looks}
    seen_gap = 0
    for c in cases[1:]:
        tone = tax[c.meta["look"]]["targets"].get("tone") or {}
        if not any(tone.get(k) for k in ("shadows_ev", "mids_ev", "highlights_ev")):
            continue
        shared = _expected_shared_zones(tone)
        gapped = [ch for ch in _zone_checks(c) if ch.tags.get("declared_gap")]
        # every shared zone is gapped; other gaps are only confounder-tagged shadows
        assert len(gapped) >= len(shared), c.id
        assert {g for g in c.meta["gaps"] if g.startswith("zone_crosstalk:")} == {
            f"zone_crosstalk:{k}" for k in shared
        }, c.id
        seen_gap += len(shared)
    assert seen_gap  # the taxonomy does contain cross-talk looks


def test_zone_agreeing_looks_keep_every_zone_check_asserted():
    cases = looks.generate(taxonomy.load())
    tax = {lk["id"]: lk for lk in taxonomy.load().looks}
    for c in cases[1:]:
        t = tax[c.meta["look"]]["targets"]
        tone = t.get("tone") or {}
        if tone and not _expected_shared_zones(tone) and not _is_confounded(t):
            assert not any(ch.tags.get("declared_gap") for ch in c.expect.checks), c.id


def _is_confounded(t: dict) -> bool:
    st = t.get("split_toning") or {}
    split = any(
        st.get(f"{e}_hue") is not None and st.get(f"{e}_sat") for e in ("shadow", "highlight")
    )
    tone = t.get("tone") or {}
    zones = any(tone.get(k) for k in ("shadows_ev", "mids_ev", "highlights_ev"))
    return bool(
        split
        or t.get("saturation_delta")
        or t.get("black_lift")
        or (tone.get("contrast") and zones)
    )


def _synthetic(**targets):
    """The taxonomy with one extra look whose targets are `targets` (everything
    else neutral); returns that look's generated cases (chart, hero)."""
    import copy
    import dataclasses

    tax = taxonomy.load()
    look = copy.deepcopy(tax.looks[0])
    look["id"] = "synthetic-confound"
    t = look["targets"]
    t["tone"] = dict(t["tone"], **targets.pop("tone", {}))
    t.update(targets)
    cases = looks.generate(dataclasses.replace(tax, looks=[look]))
    return [c for c in cases if c.meta.get("look") == "synthetic-confound"]


def _gap(case, metric, stat=None):
    out = [
        ch for ch in case.expect.checks if ch.metric == metric and (stat is None or ch.stat == stat)
    ]
    assert out, (case.id, metric)
    return out[0].tags.get("declared_gap")


def test_equal_magnitude_zone_clash_declares_both_zones_gaps():
    """Z1: -0.2 shadows against +0.1 highlights: each zone moves the other's
    patch by at least half its size, so neither sign can be read."""
    for c in _synthetic(tone={"shadows_ev": -0.2, "highlights_ev": 0.1}):
        gaps = [ch.tags.get("declared_gap") for ch in _zone_checks(c)]
        assert len(gaps) == 2 and all(gaps), c.id
        assert {g for g in c.meta["gaps"] if g.startswith("zone_crosstalk:")} == {
            "zone_crosstalk:shadows_ev",
            "zone_crosstalk:highlights_ev",
        }


def test_shadows_check_confounded_by_split_tone_desat_or_black_lift():
    """Z1 (round 1 C2): the shadows sign is not judged when split-tone chroma,
    global chroma/desat or a black lift in the same plan disturbs the same patch."""
    split = {
        "split_toning": {
            "shadow_hue": 200,
            "shadow_sat": 0.2,
            "highlight_hue": None,
            "highlight_sat": 0,
        }
    }
    for extra, name in (
        (split, "split_toning"),
        ({"saturation_delta": -0.5}, "global_chroma"),
        ({"black_lift": 0.02}, "black_lift"),
    ):
        for c in _synthetic(tone={"shadows_ev": -0.2}, **extra):
            g = _gap(c, "M2")
            assert g and g.startswith("confounded by") and name in g, (c.id, name, g)
            assert any(x == "confounded:shadows_ev" for x in c.meta["gaps"]) or c.body != "chart"


def test_single_owner_plans_keep_their_shadows_chroma_and_black_lift_checks():
    for c in _synthetic(tone={"shadows_ev": -0.2}):
        assert _gap(c, "M2") is None, c.id
    for c in _synthetic(saturation_delta=-0.3):
        assert _gap(c, "M5") is None, c.id
    for c in _synthetic(black_lift=0.02):
        assert _gap(c, "M3") is None, c.id


def test_chroma_and_black_lift_checks_confounded_by_their_disturbers():
    split = {
        "split_toning": {
            "shadow_hue": 30,
            "shadow_sat": 0.2,
            "highlight_hue": 200,
            "highlight_sat": 0.2,
        }
    }
    for c in _synthetic(saturation_delta=-0.5, **split):
        assert "split_toning" in _gap(c, "M5"), c.id
    for c in _synthetic(black_lift=0.02, tone={"shadows_ev": -0.24}):
        assert "tone:shadows_ev" in _gap(c, "M3"), c.id


def test_confounded_check_stays_in_the_denominator():
    """Z2 / gates: a confounded check is kept (tagged), never removed, so the
    per-component gate denominator is unchanged and cannot get easier."""
    for c in _synthetic(tone={"shadows_ev": -0.2}, saturation_delta=-0.5):
        assert len(c.meta["check_tags"]) == len(c.expect.checks)
        assert any(ch.metric == "M2" and ch.tags.get("declared_gap") for ch in c.expect.checks)


def test_mids_and_highlights_checks_confounded_by_chroma_and_split_tone():
    """Z1 (round 1 C2): a highlights or mids luminance check is not judged when the
    same plan has global chroma/desat or split-tone moves that shift the patch."""
    split = {
        "split_toning": {
            "shadow_hue": None,
            "shadow_sat": 0,
            "highlight_hue": 50,
            "highlight_sat": 0.2,
        }
    }
    for tone, region in (({"highlights_ev": -0.1}, "highlights"), ({"mids_ev": 0.1}, "frame")):
        for extra, name in (({"saturation_delta": -0.5}, "global_chroma"), (split, "split_toning")):
            for c in _synthetic(tone=tone, **extra):
                zc = _zone_checks(c)
                assert zc and all(
                    (ch.tags.get("declared_gap") or "").startswith("confounded by")
                    and name in ch.tags["declared_gap"]
                    for ch in zc
                ), (c.id, region, name)
    for c in _synthetic(tone={"highlights_ev": -0.1}):  # single owner keeps its check
        assert not any(ch.tags.get("declared_gap") for ch in _zone_checks(c)), c.id


def test_contrast_slope_check_confounded_by_zone_luminance_or_split_tone():
    """Z2 (round 2b): sigmoid slope is not judged when zone luminance or split-tone
    moves in the same plan reshape the ramp; the check stays in the denominator."""
    split = {
        "split_toning": {
            "shadow_hue": 200,
            "shadow_sat": 0.2,
            "highlight_hue": None,
            "highlight_sat": 0,
        }
    }
    for tone, extra, name in (
        ({"contrast": 0.15, "shadows_ev": -0.2}, {}, "tone:shadows_ev"),
        ({"contrast": 0.2, "mids_ev": -0.5}, {}, "tone:mids_ev"),
        ({"contrast": 0.15}, split, "split_toning"),
    ):
        for c in _synthetic(tone=tone, **extra):
            g = _gap(c, "M6")
            assert g and g.startswith("confounded by") and name in g, (c.id, name, g)
            assert len(c.meta["check_tags"]) == len(c.expect.checks)


def test_single_owner_contrast_plan_keeps_its_slope_check():
    for c in _synthetic(tone={"contrast": 0.15}):
        assert _gap(c, "M6") is None, c.id


def test_chroma_check_confounded_by_contrast_and_zone_moves():
    for tone, name in (
        ({"contrast": -0.2}, "tone:contrast"),
        ({"shadows_ev": 0.3}, "tone:shadows_ev"),
    ):
        for c in _synthetic(tone=tone, saturation_delta=0.05):
            g = _gap(c, "M5")
            assert g and g.startswith("confounded by") and name in g, (c.id, name, g)
