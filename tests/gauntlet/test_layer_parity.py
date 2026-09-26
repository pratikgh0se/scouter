from tests.gauntlet.layers import parity


def test_parity_is_46_verbs_times_2():
    cases = parity.generate()
    assert len(cases) == parity.EXPECTED == 92  # 46 verbs today
    assert sum(c.tier == "P0" for c in cases) == 24
