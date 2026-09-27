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
    not change which or how many cases exist, only their order."""
    cases = looks.generate(taxonomy.load())
    assert _switches(cases) <= 1
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
