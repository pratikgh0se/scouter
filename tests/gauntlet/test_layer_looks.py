import collections

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
