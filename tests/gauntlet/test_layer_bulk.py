from tests.gauntlet import taxonomy
from tests.gauntlet.layers import bulk


def test_bulk_count_and_crash_cases():
    cases = bulk.generate(taxonomy.load())
    assert len(cases) == bulk.EXPECTED == 40
    crash = [c for c in cases if c.meta["flow"] == "crash_mid_job"]
    assert len(crash) == 4 and all(c.meta["kill_after_frame"] == 50 for c in crash)
    assert next(c for c in cases if c.meta["flow"] == "time_lapse_deflicker").expect.kind \
        == "refuse"
