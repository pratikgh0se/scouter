from tests.gauntlet import taxonomy
from tests.gauntlet.layers import offline


def test_offline_count_ids_tier():
    cases = offline.generate(taxonomy.load())
    assert len(cases) == offline.EXPECTED == 2314
    assert len({c.id for c in cases}) == 2314
    assert {c.tier for c in cases} == {"P2"} and {c.mode for c in cases} == {"offline"}
