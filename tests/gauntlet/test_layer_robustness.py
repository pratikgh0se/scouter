from tests.gauntlet import taxonomy
from tests.gauntlet.layers import robustness


def test_robustness_count_and_p0():
    cases = robustness.generate(taxonomy.load())
    assert len(cases) == robustness.EXPECTED == 32
    assert {c.source["id"] for c in cases if c.tier == "P0"} == {
        "R1",
        "R3",
        "R12",
        "R17",
        "H1",
        "H2",
        "H3",
        "H4",
    }
    assert next(c for c in cases if c.source["id"] == "R24").body == "sony_a7"
