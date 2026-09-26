import json

from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _setup(tmp_path, **kw):
    a = ToyAdapter(**kw)
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    return a, run


def test_runs_every_case_writes_results_and_progress(tmp_path):
    a, run = _setup(tmp_path)
    s = Runner(a, run).run_layers()
    assert s.ran == 6 and s.status == "done"
    prog = json.loads(run.progress_json.read_text())
    assert prog["layers"]["alpha"]["pass"] == 4 and prog["layers"]["alpha"]["status"] == "done"
    assert prog["layers"]["alpha"]["p0_pass"] == 1
    assert "alpha-0" in run.log_md.read_text()
    doc = run.read_result("alpha-0")
    assert doc["verdict"]["outcome"] == "verified" and doc["repro"].endswith("--stamp s")


def test_resume_skips_done_and_only_failed_reruns(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-2": "silent"})
    Runner(a, run).run_layers()
    a.ran.clear()
    Runner(a, run).run_layers(resume=True)
    assert a.ran == []
    Runner(a, run).run_layers(only_failed=True)
    assert a.ran == ["alpha-2"]
    assert run.read_result("alpha-2")["verdict"]["outcome"] == "silent_failure"


def test_case_filter(tmp_path):
    a, run = _setup(tmp_path)
    Runner(a, run).run_layers(case_ids={"beta-1"}, resume=False)
    assert a.ran == ["beta-1"]


def test_crash_stops_the_layer_but_not_the_run(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-1": "crash"})
    s = Runner(a, run).run_layers()
    assert s.stopped_layers == ["alpha"]
    assert "alpha-2" not in a.ran and "beta-0" in a.ran
    assert run.read_result("alpha-1")["verdict"]["outcome"] == "crash"


def test_hard_cap_blocks_and_aborts_the_session(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-0": "sleep"})
    # the real cap (budget + 30 s per step) is too slow for a unit test: shrink it.
    import gauntlet.case as case_mod
    orig = case_mod.Case.budget_cap_s
    case_mod.Case.budget_cap_s = lambda self: 0.2
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        case_mod.Case.budget_cap_s = orig
    assert run.read_result("alpha-0")["verdict"]["outcome"] == "blocked"
    assert a.aborts == 1 and a.sessions >= 2


def test_park_records_and_resume_picks_up(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-1": "park"})
    s = Runner(a, run).run_layers()
    assert s.status == "parked" and "alpha-1" in s.parked
    assert run.read_result("alpha-1") is None
    prog = json.loads(run.progress_json.read_text())
    assert prog["status"] == "parked" and "alpha-1" in prog["parked"]
    assert "beta-0" in a.ran  # other layers go on
    b = ToyAdapter()
    Runner(b, run).run_layers(resume=True)
    assert b.ran == ["alpha-1", "alpha-2", "alpha-3"]
    assert run.read_result("alpha-1") is not None


def test_session_rotation(tmp_path):
    a, run = _setup(tmp_path, n=5)
    Runner(a, run, session_max_cases=2).run_layers(layers=["alpha"])
    assert a.sessions == 3


def test_tier_filter_runs_only_the_p0_slice(tmp_path):
    a, run = _setup(tmp_path)
    Runner(a, run).run_layers(tiers={"P0"})
    assert a.ran == ["alpha-0", "beta-0"]
