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
