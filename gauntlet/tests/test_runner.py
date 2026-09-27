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


# -- final fix I2: a run-level refusal stops the run, writes no case result ----

def test_preflight_refusal_stops_the_run_and_writes_nothing(tmp_path):
    from gauntlet.cli import EXIT_REFUSED  # noqa: F401 - the CLI names the status

    a, run = _setup(tmp_path)
    a.preflight = lambda run, layer: "foreign darktable: pid 4242"
    s = Runner(a, run).run_layers()
    assert s.status == "blocked: foreign darktable: pid 4242"
    assert a.ran == [] and a.sessions == 0 and run.read_results() == []
    prog = json.loads(run.progress_json.read_text())
    assert prog["status"] == "blocked: foreign darktable: pid 4242"


def test_preflight_raising_refuse_run_stops_the_run(tmp_path):
    from gauntlet.adapter import RefuseRun

    a, run = _setup(tmp_path)

    def pre(run, layer):
        if layer == "beta":
            raise RefuseRun("foreign darktable")
    a.preflight = pre
    s = Runner(a, run).run_layers()
    assert s.status == "blocked: foreign darktable"
    assert {r["layer"] for r in run.read_results()} == {"alpha"}


def test_refuse_run_mid_layer_writes_no_result_and_stops(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-1": "refuse"})
    s = Runner(a, run).run_layers()
    assert s.status.startswith("blocked: foreign darktable")
    assert run.read_result("alpha-1") is None and run.read_result("alpha-2") is None
    assert run.read_result("alpha-0") is not None and "beta-0" not in a.ran
    # resume re-runs it once the machine is clean
    b = ToyAdapter()
    Runner(b, run).run_layers(resume=True)
    assert b.ran[:3] == ["alpha-1", "alpha-2", "alpha-3"]


def test_refuse_run_from_open_session_writes_no_result(tmp_path):
    from gauntlet.adapter import RefuseRun

    a, run = _setup(tmp_path)

    def refuse(layer, run_root):
        raise RefuseRun("foreign darktable")
    a.open_session = refuse
    s = Runner(a, run).run_layers()
    assert s.status == "blocked: foreign darktable" and run.read_results() == []


def test_session_crash_reports_survive_a_crash_file(tmp_path):
    """(final fix I3): a session-recorded crash (an MCP-server death) is kept
    when crash files also appeared; the runner merges, never overwrites."""
    from gauntlet.adapter import Observation

    a, run = _setup(tmp_path)

    class S(ToySession):
        def run_case(self, case):
            a.crashes += 1
            return Observation(crash_reports=["mcp_server_death: exit 1"])

    a.open_session = lambda layer, run_root: S(a)
    Runner(a, run).run_layers(layers=["alpha"])
    obs = run.read_result("alpha-0")["observation"]
    assert obs["crash_reports"][0] == "mcp_server_death: exit 1"
    assert "toy-0.ips" in obs["crash_reports"]


# -- final fix I6: the progress page counts hangs the way the report does ------

def test_is_hang_is_one_helper():
    from gauntlet.evaluate import is_hang

    assert is_hang("failed_hang", None)
    assert is_hang("blocked", "hard_cap") and is_hang("blocked", "hang: claude turn")
    assert not is_hang("blocked", "session: launch failed") and not is_hang("verified", None)


def test_progress_counts_hard_cap_as_a_hang_and_undoes_it_on_rerun(tmp_path):
    import gauntlet.case as case_mod

    a, run = _setup(tmp_path, behaviours={"alpha-0": "sleep"})
    orig = case_mod.Case.budget_cap_s
    case_mod.Case.budget_cap_s = lambda self: 0.2
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        case_mod.Case.budget_cap_s = orig
    lay = json.loads(run.progress_json.read_text())["layers"]["alpha"]
    assert lay["hang"] == 1 and lay["blocked"] == 1
    a.behaviours.clear()
    Runner(a, run).run_layers(layers=["alpha"], only_failed=True)
    lay = json.loads(run.progress_json.read_text())["layers"]["alpha"]
    assert lay["hang"] == 0 and lay["blocked"] == 0 and lay["done"] == 4


def test_progress_counts_inherited_wedge_in_its_own_bucket(tmp_path):
    """Fix round 1 review L1 (low): `inherited_wedge` is deliberately not a
    hang (`evaluate.is_hang`) and must not just vanish into the generic
    `blocked` counter either -- it gets its own bucket, so a layer's blocked
    cases can be told apart: an inherited wedge (nothing this case did) from
    every other kind of block."""
    from gauntlet.progress import Progress

    run = RunDir(tmp_path, "stamp").ensure()
    p = Progress(run, "adapter")
    p.layer("alpha", 4, 1, True)
    p.start_layer("alpha")
    p.record("alpha", "alpha-0", "P1", {"score": "fail", "outcome": "blocked", "notes": []},
             0.1, blocked="inherited_wedge")
    lay = json.loads(run.progress_json.read_text())["layers"]["alpha"]
    assert lay["inherited_wedge"] == 1
    assert lay["blocked"] == 1
    assert lay["hang"] == 0  # never counted as a hang


def test_progress_page_shows_hangs_and_blocked(tmp_path):
    from gauntlet import html as html_page

    prog = {"layers": {"alpha": {"done": 2, "total": 4, "hang": 1, "blocked": 2}}}
    page = html_page.render(prog)
    assert "Hangs<b>1</b>" in page and "Blocked<b>2</b>" in page


# -- final fix I7: harness work gets its own allowance on top of the case cap --

def test_adapter_harness_allowance_extends_the_case_cap(tmp_path):
    import gauntlet.case as case_mod

    a, run = _setup(tmp_path, behaviours={"alpha-0": "sleep"})
    run_cases = run.read_cases()
    for c in run_cases:
        c.meta["sleep_s"] = 0.4
    run.write_cases(run_cases)
    seen = []

    def allowance(case):
        seen.append(case.id)
        return 2.0
    a.harness_allowance_s = allowance
    orig = case_mod.Case.budget_cap_s
    case_mod.Case.budget_cap_s = lambda self: 0.2
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        case_mod.Case.budget_cap_s = orig
    assert run.read_result("alpha-0")["verdict"]["outcome"] == "verified"
    assert seen[0] == "alpha-0" and a.aborts == 0


# -- pre-night-1 N1/N2: adapter-tunable open cap, abort_open, next-case hint ---

def test_open_cap_is_adapter_tunable(tmp_path):
    a, run = _setup(tmp_path)
    assert Runner(a, run).session_open_cap_s == 180.0  # engine default
    a.session_open_cap_s = 420.0
    assert Runner(a, run).session_open_cap_s == 420.0
    assert Runner(a, run, session_open_cap_s=5.0).session_open_cap_s == 5.0  # explicit wins


def test_an_over_cap_open_is_aborted_through_the_adapter_hook(tmp_path):
    import threading

    a, run = _setup(tmp_path)
    a.session_open_cap_s = 0.2
    released = threading.Event()
    calls = []

    def hung_open(layer, run_root):
        released.wait(5)
        return ToySession(a)

    def abort_open():
        calls.append("abort_open")
        released.set()
        return {"ok": True, "killed": [4321]}

    a.open_session, a.abort_open = hung_open, abort_open
    Runner(a, run, max_consecutive_open_failures=1).run_layers(layers=["alpha"])
    assert calls == ["abort_open"]
    ev = run.read_result("alpha-0")["observation"]["hang_evidence"]
    assert any("abort_open" in e and "4321" in e for e in ev)


def test_a_failing_abort_open_is_folded_into_the_evidence(tmp_path):
    a, run = _setup(tmp_path)
    a.session_open_cap_s = 0.1
    a.open_session = lambda layer, run_root: time.sleep(1)

    def bad():
        raise OSError("kill failed")
    a.abort_open = bad
    Runner(a, run, max_consecutive_open_failures=1).run_layers(layers=["alpha"])
    ev = run.read_result("alpha-0")["observation"]["hang_evidence"]
    assert any("abort_open failed" in e for e in ev)


def test_open_session_is_told_the_case_it_opens_for(tmp_path):
    """N2: under --case / --only-failed the adapter cannot guess the next case."""
    a, run = _setup(tmp_path)
    seen = []

    def open_session(layer, run_root, next_case=None):
        seen.append(next_case.id if next_case else None)
        return ToySession(a)
    a.open_session = open_session
    Runner(a, run).run_layers(layers=["alpha"], case_ids={"alpha-2"})
    assert seen == ["alpha-2"]
