import json

from gauntlet import report
from gauntlet.adapter import LayerSpec
from gauntlet.case import Case, Expect, Step
from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _run(tmp_path, **kw):
    a = ToyAdapter(**kw)
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run, commit="abc1234").run_layers()
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


# ---------------------------------------------------------------- fix round 1


class _StubRun:
    """A minimal stand-in for RunDir: `build()` only reads `.stamp` off it."""

    stamp = "s"


class _StubAdapter:
    name = "stub"

    def __init__(self, layers, crash_reports=None):
        self._layers = layers
        self._crash_reports = crash_reports or []

    def layers(self):
        return self._layers

    def signoff_lines(self, run=None):
        return []

    def crash_reports_since(self, t):
        return self._crash_reports


def _case(cid, tier="P1"):
    return Case(id=cid, layer="l", tier=tier, mode="tool",
                steps=[Step("set", {}, budget_s=1.0, write=True)], expect=Expect("edit"))


def _result(cid, tier="P1", *, score="fail", outcome="failed", blocked=None,
            hang_evidence=None, crash_reports=None, started_at=0.0, checks=None):
    obs = {"calls": [], "blocked": blocked, "hang_evidence": hang_evidence or [],
           "crash_reports": crash_reports or []}
    return {"id": cid, "layer": "l", "tier": tier, "mode": "tool", "source": {}, "meta": {},
            "repro": f"repro {cid}", "started_at": started_at,
            "verdict": {"score": score, "outcome": outcome, "checks": checks or []},
            "observation": obs}


def test_hard_cap_result_counts_as_a_hang_and_blocks_the_bar():
    case = _case("a-0")
    result = _result("a-0", outcome="blocked", blocked="hard_cap",
                     hang_evidence=["case exceeded its hard cap of 30s"])
    adapter = _StubAdapter([LayerSpec("l", 1, 1, True)])
    rep = report.build(_StubRun(), adapter, [case], [result])
    assert rep["S8_signoff"]["hangs"] == 1
    assert not rep["S8_signoff"]["release_bar_met"]
    assert rep["S6_crashes_hangs"]["hangs"] == [
        {"id": "a-0", "blocked": "hard_cap",
         "evidence": ["case exceeded its hard cap of 30s"]}]
    fixes = {f["id"]: f for f in rep["S3_non_pass"]}
    assert fixes["a-0"]["hang"] is True


def test_run_level_crash_not_attributed_to_any_case_blocks_the_bar():
    case = _case("a-0")
    result = _result("a-0", score="pass", outcome="verified")
    adapter = _StubAdapter([LayerSpec("l", 1, 1, True)], crash_reports=["session-close.ips"])
    rep = report.build(_StubRun(), adapter, [case], [result])
    assert rep["S8_signoff"]["crashes"] == 1
    assert "session-close.ips" in rep["S6_crashes_hangs"]["crash_reports"]
    assert not rep["S8_signoff"]["release_bar_met"]
    # meta["crash_reports_run"] overrides asking the adapter.
    rep2 = report.build(_StubRun(), adapter, [case], [result],
                        meta={"crash_reports_run": ["preset.ips"]})
    assert rep2["S6_crashes_hangs"]["crash_reports"] == ["preset.ips"]


def test_orphan_result_does_not_mask_a_missing_case():
    cases = [_case("a-0"), _case("a-1")]
    orphan = _result("a-2", score="pass", outcome="verified")  # stale, no longer generated
    adapter = _StubAdapter([LayerSpec("l", 1, 2, True)])
    rep = report.build(_StubRun(), adapter, cases, [orphan])
    assert rep["S8_signoff"]["cases_missing"] == 2
    assert rep["S8_signoff"]["missing_ids"] == ["a-0", "a-1"]
    assert rep["S8_signoff"]["orphan_result_ids"] == ["a-2"]
    assert not rep["S8_signoff"]["release_bar_met"]


def test_zero_p0_run_does_not_meet_the_bar():
    case = _case("a-0", tier="P1")
    result = _result("a-0", tier="P1", score="pass", outcome="verified")
    adapter = _StubAdapter([LayerSpec("l", 1, 1, True)])
    rep = report.build(_StubRun(), adapter, [case], [result])
    assert rep["S8_signoff"]["p0"] == "0/0"
    assert not rep["S8_signoff"]["release_bar_met"]


def test_declared_check_gate_with_no_tagged_checks_fails():
    spec = LayerSpec("l", 1, 1, True, check_gates=(("confidence", ("documented",), 0.9),))
    case = _case("a-0", tier="P0")
    result = _result("a-0", tier="P0", score="pass", outcome="verified", checks=[])
    adapter = _StubAdapter([spec])
    rep = report.build(_StubRun(), adapter, [case], [result])
    gate = rep["S2_layers"][0]["check_gates"][0]
    assert gate["n"] == 0 and gate["ok"] is False
    assert not rep["S8_signoff"]["release_bar_met"]


def test_p0_needs_review_blocks_the_bar():
    case = _case("a-0", tier="P0")
    result = _result("a-0", tier="P0", score="partial", outcome="needs_review")
    adapter = _StubAdapter([LayerSpec("l", 1, 1, True)])
    rep = report.build(_StubRun(), adapter, [case], [result])
    assert rep["S8_signoff"]["p0"] == "0/1"
    assert not rep["S8_signoff"]["release_bar_met"]


class _BlockingToy(ToyAdapter):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen_run = None

    def signoff_lines(self, run=None):
        self.seen_run = run
        return ["toy originals unchanged: NO"]

    def signoff_blockers(self, run=None):
        return ["toy originals changed"]


def test_an_adapter_blocker_fails_the_bar_and_is_listed(tmp_path):
    """Task 28 fix round 1, I1: optional signoff_blockers(run); signoff_lines(run)."""
    a = _BlockingToy()
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run, commit="abc1234").run_layers()
    rep = report.write(run, a)
    s8 = rep["S8_signoff"]
    assert not s8["release_bar_met"] and s8["blockers"] == ["toy originals changed"]
    assert a.seen_run is run
    assert "  - toy originals changed" in run.report_md.read_text()


def test_no_blockers_method_means_no_blockers(tmp_path):
    a, run = _run(tmp_path)
    s8 = report.write(run, a)["S8_signoff"]
    assert s8["blockers"] == [] and s8["release_bar_met"]


# -- final fix I5: one full run of one commit ---------------------------------

def test_results_carry_the_commit_and_a_clean_one_commit_run_meets_the_bar(tmp_path):
    a, run = _run(tmp_path)
    assert run.read_result("alpha-0")["commit"] == "abc1234"
    s8 = report.write(run, a)["S8_signoff"]
    assert s8["commits"] == ["abc1234"] and s8["release_bar_met"]
    assert "- Commits: abc1234" in run.report_md.read_text()


def test_results_from_two_commits_do_not_meet_the_bar(tmp_path):
    a, run = _run(tmp_path)
    doc = run.read_result("alpha-1")
    doc["commit"] = "def5678"  # a --resume after a new commit
    run.write_result("alpha-1", doc)
    s8 = report.write(run, a)["S8_signoff"]
    assert s8["commits"] == ["abc1234", "def5678"] and not s8["release_bar_met"]


def test_a_result_without_a_commit_does_not_meet_the_bar(tmp_path):
    a = ToyAdapter()
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run).run_layers()  # commit unknown ("")
    s8 = report.write(run, a)["S8_signoff"]
    assert s8["commits"] == [""] and not s8["release_bar_met"]


def test_a_partial_generation_does_not_meet_the_bar(tmp_path):
    """Every adapter layer must have cases in cases.jsonl (generate --layer X
    alone must not meet the bar)."""
    a = ToyAdapter()
    run = RunDir(tmp_path, "s")
    write_generation(run, a, ["alpha"])
    Runner(a, run, commit="abc1234").run_layers()
    s8 = report.write(run, a)["S8_signoff"]
    assert s8["layers_without_cases"] == ["beta"] and not s8["release_bar_met"]
    assert s8["p0"] == "1/1" and s8["cases_missing"] == 0
