import json
import time

from gauntlet.adapter import Park
from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter, ToySession


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


# ---- fix round 1 regressions (Opus review, t7-r1-findings.md) ----


def test_only_failed_rerun_does_not_double_count(tmp_path):
    """I3: record() must undo the previous verdict's counts before applying
    the new one, or a rerun inflates done/fail/silent past the layer total
    and drives eta_s negative."""
    a, run = _setup(tmp_path, behaviours={"alpha-2": "silent"})
    Runner(a, run).run_layers(layers=["alpha"])
    Runner(a, run).run_layers(layers=["alpha"], only_failed=True)
    lay = json.loads(run.progress_json.read_text())["layers"]["alpha"]
    assert lay["total"] == 4
    assert lay["done"] == 4
    assert lay["pass"] == 3 and lay["fail"] == 1 and lay["silent"] == 1
    assert lay["eta_s"] >= 0


def test_wait_on_park_restores_layer_status(tmp_path):
    """I4: an in-process park-and-resume that succeeds must leave the layer
    (and the run) reporting "done", not stuck at "parked" with an empty
    parked list."""
    a, run = _setup(tmp_path)
    orig_run_case = ToySession.run_case
    state = {"n": 0}

    def rc(self, case):
        if case.id == "alpha-1" and state["n"] == 0:
            state["n"] += 1
            raise Park("limit", until=time.time() + 1)
        return orig_run_case(self, case)

    ToySession.run_case = rc
    try:
        s = Runner(a, run, sleep=lambda x: None).run_layers(layers=["alpha"],
                                                             wait_on_park=True)
    finally:
        ToySession.run_case = orig_run_case
    assert s.status == "done" and s.parked == []
    prog = json.loads(run.progress_json.read_text())
    assert prog["status"] == "done"
    assert prog["layers"]["alpha"]["status"] == "done"
    assert prog["layers"]["alpha"]["parked"] == 0
    assert prog["parked"] == []


def test_open_session_failure_is_recorded_not_fatal(tmp_path):
    """I1: a session that fails to open (or hangs) must never escape
    run_layers untyped -- each affected case gets a typed "blocked" result,
    and after enough consecutive failures the layer stops cleanly."""
    a, run = _setup(tmp_path)

    def boom(layer, run_root):
        raise RuntimeError("darktable failed to launch")

    a.open_session = boom
    s = Runner(a, run).run_layers(layers=["alpha"])
    assert s.status in ("done", "parked")  # run_layers itself never raises
    prog = json.loads(run.progress_json.read_text())
    assert prog["layers"]["alpha"]["status"] == "blocked"
    for cid in ("alpha-0", "alpha-1", "alpha-2"):
        doc = run.read_result(cid)
        assert doc is not None
        assert doc["verdict"]["outcome"] == "blocked"
        assert "session_open" in doc["observation"]["blocked"]
    assert run.read_result("alpha-3") is None


def test_session_abort_and_close_failures_are_swallowed(tmp_path):
    """I2: a broken cleanup path (abort()/close() raising) must not kill the
    run; the failure is folded into the case's hang_evidence (abort) or the
    log (close), and later cases in the layer still run."""
    a, run = _setup(tmp_path, behaviours={"alpha-0": "sleep"})
    import gauntlet.case as case_mod
    orig_cap = case_mod.Case.budget_cap_s
    case_mod.Case.budget_cap_s = lambda self: 0.1
    orig_abort, orig_close = ToySession.abort, ToySession.close

    def bad_abort(self):
        raise OSError("kill failed")

    def bad_close(self):
        raise OSError("close failed")

    ToySession.abort = bad_abort
    ToySession.close = bad_close
    try:
        s = Runner(a, run).run_layers(layers=["alpha"])
    finally:
        case_mod.Case.budget_cap_s = orig_cap
        ToySession.abort = orig_abort
        ToySession.close = orig_close
    assert s.status == "done"
    doc = run.read_result("alpha-0")
    assert doc is not None and doc["verdict"]["outcome"] == "blocked"
    assert any("abort" in e.lower() for e in doc["observation"]["hang_evidence"])
    assert run.read_result("alpha-3") is not None
