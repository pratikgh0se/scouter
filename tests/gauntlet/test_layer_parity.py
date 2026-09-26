from tests.gauntlet.layers import parity


def test_parity_is_45_verbs_times_2():
    cases = parity.generate()
    assert len(cases) == parity.EXPECTED == 90  # 45 verbs today
    assert sum(c.tier == "P0" for c in cases) == 24
