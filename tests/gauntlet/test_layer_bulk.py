from scouter import paths as scouter_paths
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import bulk
from tests.gauntlet.exec_bulk import job_slug_for


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


# -- n1-bulk fix (a): job slugs for every real recorded case id -------------

def test_job_slug_for_every_real_case_id_is_valid_and_unique():
    """RED (before the fix): `f"htc-{case.id}"[-40:]` keeps the underscores
    `SLUG_RE` forbids and truncates from the LEFT, so `validate_slug` raises
    `usage: invalid job slug` for 34 of these 40 real ids. GREEN: every one
    of the actual night-1 bulk case ids produces a distinct valid slug."""
    cases = bulk.generate(taxonomy.load())
    ids = [c.id for c in cases]
    assert len(ids) == 40
    slugs = [job_slug_for(cid) for cid in ids]
    for slug in slugs:
        assert scouter_paths.SLUG_RE.match(slug), slug
    assert len(set(slugs)) == len(ids), "job slugs must stay unique across all 40 cases"


def test_job_slug_for_rejects_nothing_and_never_loops_forever():
    # A pathological, all-underscore case id still resolves to something
    # `validate_slug` accepts, by right-truncating down to the 48-char bound
    # -- it never loops forever hunting for a valid prefix.
    slug = job_slug_for("_" * 80)
    assert scouter_paths.SLUG_RE.match(slug)
    assert len(slug) <= 48


# -- n1-bulk fix (c)/(d): the bulk generator's own inputs --------------------

def test_hero_edit_phrase_is_not_a_wb_phrase():
    """RED: "warmer" is a WB phrase and hits the WB guard (commit_timeout on
    olympus, value_unknown on a7 -- no WB calibration table for a7).
    GREEN: the bulk hero edit uses a live, non-WB phrase; WB coverage
    belongs to the coverage layer, not bulk."""
    steps = bulk._hero_edit()
    say = next(s for s in steps if s.tool == "darkroom_say")
    assert say.args["phrase"] != "warmer"
    from scouter.core.phrases import PHRASES
    phrase = next(p for p in PHRASES if say.args["phrase"] in p.synonyms)
    modules_touched = {mv.module for mv in phrase.moves}
    assert "temperature" not in modules_touched


def test_style_extract_after_hero_edit_never_targets_the_wb_module():
    """The hero edit no longer touches `temperature`; a style/preset step
    that follows it must extract/apply a module the hero edit actually
    changed, not the old WB module."""
    for flow in ("copy_paste_history", "styles"):
        steps, _checks, _kind = bulk.flow_steps(flow, 7)
        extract = next(s for s in steps if s.tool == "style_extract")
        assert extract.args["modules"] == ["exposure"]

    cases = bulk.generate(taxonomy.load())
    crash_style = next(c for c in cases if c.meta.get("flow") == "crash_mid_job"
                       and c.meta.get("verb") == "style_apply_set")
    extract = next(s for s in crash_style.steps if s.tool == "style_extract")
    assert extract.args["modules"] == ["exposure"]
