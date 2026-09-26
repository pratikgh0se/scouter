from tests.gauntlet import taxonomy
from tests.gauntlet.layers import bulk


def test_bulk_count_and_crash_cases():
    cases = bulk.generate(taxonomy.load())
    assert len(cases) == bulk.EXPECTED == 40
    crash = [c for c in cases if c.meta["flow"] == "crash_mid_job"]
    assert len(crash) == 4 and all(c.meta["kill_after_frame"] == 50 for c in crash)
    assert next(c for c in cases if c.meta["flow"] == "time_lapse_deflicker").expect.kind \
        == "refuse"


def test_crash_cases_stage_their_setup_before_the_crash_verb():
    cases = bulk.generate(taxonomy.load())
    style_case = next(c for c in cases if c.meta["flow"] == "crash_mid_job"
                      and c.meta["verb"] == "style_apply_set")
    tools = [s.tool for s in style_case.steps]
    assert "style_extract" in tools
    assert tools.index("style_extract") < tools.index("style_apply_set")

    job_case = next(c for c in cases if c.meta["flow"] == "crash_mid_job"
                    and c.meta["verb"] == "job_run")
    tools = [s.tool for s in job_case.steps]
    assert "preset_save" in tools
    assert tools.index("preset_save") < tools.index("job_run")
