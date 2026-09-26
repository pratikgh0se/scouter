import json

from gauntlet import report
from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _run(tmp_path, **kw):
    a = ToyAdapter(**kw)
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run).run_layers()
    return a, run


def test_clean_run_meets_the_bar(tmp_path):
    a, run = _run(tmp_path)
    rep = report.write(run, a)
    s8 = rep["S8_signoff"]
    assert s8["release_bar_met"] and s8["p0"] == "2/2" and s8["crashes"] == 0
    md = run.report_md.read_text()
    assert md.index("## S8 Sign-off") < md.index("## S1 Headline")
    assert "toy originals unchanged: yes" in md
    assert json.loads(run.report_json.read_text())["S1_headline"]["ran"] == 6


def test_silent_failure_blocks_release_and_leads_the_fix_list(tmp_path):
    a, run = _run(tmp_path, behaviours={"alpha-3": "silent", "alpha-0": "silent"})
    rep = report.write(run, a)
    assert not rep["S8_signoff"]["release_bar_met"]
    fixes = json.loads(run.fixlist_json.read_text())
    assert [f["id"] for f in fixes] == ["alpha-0", "alpha-3"]
    assert fixes[0]["tier"] == "P0" and fixes[0]["repro"].endswith("--stamp s")
    assert "| `alpha-0` | P0 | silent_failure |" in run.fixlist_md.read_text()


def test_missing_cases_block_release(tmp_path):
    a = ToyAdapter()
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run).run_layers(layers=["alpha"])
    assert report.write(run, a)["S8_signoff"]["cases_missing"] == 2


def test_roadmap_scoring():
    rs = [
        {"id": "1", "layer": "scn", "tier": "P0", "mode": "tool", "source": {"id": "r1"},
         "meta": {"roadmap_key": "lens"}, "verdict": {"outcome": "correct_refusal"}},
        {"id": "2", "layer": "scn", "tier": "P1", "mode": "tool", "source": {"id": "r2"},
         "meta": {"roadmap_key": "lens"}, "verdict": {"outcome": "correct_refusal"}},
        {"id": "3", "layer": "scn", "tier": "P0", "mode": "agent", "source": {"id": "r1"},
         "meta": {"roadmap_key": "lens"}, "verdict": {"outcome": "correct_refusal"}},
        {"id": "4", "layer": "looks", "tier": "P1", "mode": "tool", "source": {},
         "meta": {"gaps": ["grain", "grain"]}, "verdict": {"outcome": "verified"}},
    ]
    rows = {r["key"]: r for r in report.roadmap(rs)}
    assert rows["lens"]["score"] == 2 + 3 * 1 + 1
    assert rows["grain"]["score"] == 0.2


def test_timing_percentiles_and_contact_sheet(tmp_path):
    rs = [{"id": "x", "layer": "l", "verdict": {"outcome": "verified", "score": "pass"},
           "observation": {"calls": [{"tool": "t", "wall_s": w} for w in (1, 2, 3, 4)],
                           "extra": {"previews": {"before": "p/b.jpg", "after": "p/a.jpg"}}}}]
    t = report.timing(rs)["percentiles"]["l:t"]
    assert (t["p50"], t["p90"], t["p99"]) == (2.0, 4.0, 4.0)
    sheet = report.contact_sheet(rs)
    assert 'src="p/b.jpg"' in sheet and "x - verified" in sheet
