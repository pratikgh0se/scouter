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
    every case on olympus -- a crash, not a typed failure. This test names
    the invariant directly so the next new role fails loudly and specifically
    here instead of as an opaque index error deep in the sort."""
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
