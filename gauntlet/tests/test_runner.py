import json
import time
from pathlib import Path

import pytest

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
        s = Runner(a, run, sleep=lambda x: None).run_layers(layers=["alpha"], wait_on_park=True)
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
    p.record(
        "alpha",
        "alpha-0",
        "P1",
        {"score": "fail", "outcome": "blocked", "notes": []},
        0.1,
        blocked="inherited_wedge",
    )
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


# -- t_90e75c20: per-case retention and the run-level disk guard --------


def test_retention_deletes_case_workdir_except_the_layers_keep_globs(tmp_path):
    a, run = _setup(tmp_path)
    orig_run_case = ToySession.run_case
    n_mb = 2

    def rc(self, case):
        obs = orig_run_case(self, case)
        d = run.case_workdir(case.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "sidecar.xmp").write_bytes(b"s" * 128)
        (d / "import.raw").write_bytes(b"i" * (n_mb * 1024 * 1024))
        return obs

    ToySession.run_case = rc
    a.retain_keep = lambda layer: ("*.xmp",)
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        ToySession.run_case = orig_run_case
    d = run.case_workdir("alpha-0")
    assert (d / "sidecar.xmp").exists()
    assert not (d / "import.raw").exists()
    assert run.read_result("alpha-0") is not None
    assert "alpha-0" in run.log_md.read_text()


def test_retention_score_only_layer_deletes_the_whole_workdir(tmp_path):
    """bulk's own policy (card t_90e75c20): an empty keep tuple -- nothing
    survives scoring, since bulk needs no artifact after the verdict."""
    a, run = _setup(tmp_path)
    orig_run_case = ToySession.run_case

    def rc(self, case):
        obs = orig_run_case(self, case)
        d = run.case_workdir(case.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "render.tif").write_bytes(b"r" * (1024 * 1024))
        return obs

    ToySession.run_case = rc
    a.retain_keep = lambda layer: ()
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        ToySession.run_case = orig_run_case
    assert list(run.case_workdir("alpha-0").iterdir()) == []
    assert run.read_result("alpha-0") is not None


def test_retention_is_off_when_the_adapter_declares_no_policy(tmp_path):
    a, run = _setup(tmp_path)
    orig_run_case = ToySession.run_case

    def rc(self, case):
        obs = orig_run_case(self, case)
        d = run.case_workdir(case.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "import.raw").write_bytes(b"i" * 1024)
        return obs

    ToySession.run_case = rc
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        ToySession.run_case = orig_run_case
    assert (run.case_workdir("alpha-0") / "import.raw").exists()


def test_retention_never_touches_anything_outside_the_case_workdir(tmp_path):
    a, run = _setup(tmp_path)
    outside = Path(tmp_path) / "outside.txt"
    outside.write_text("keep me")
    orig_run_case = ToySession.run_case

    def rc(self, case):
        obs = orig_run_case(self, case)
        d = run.case_workdir(case.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "junk.bin").write_bytes(b"j" * 1024)
        return obs

    ToySession.run_case = rc
    a.retain_keep = lambda layer: ()
    try:
        s = Runner(a, run).run_layers(layers=["alpha"])
    finally:
        ToySession.run_case = orig_run_case
    assert s.status == "done"
    assert outside.read_text() == "keep me"
    for cid in a.ran:
        assert run.read_result(cid) is not None  # verdicts untouched


def test_disk_guard_parks_before_starting_a_case_below_the_threshold(tmp_path):
    """A real tiny run of the runner over a fake adapter in a temp dir (no
    darktable): the free-space reading is injected through the `free_disk_gb`
    parameter, never by mocking shutil.disk_usage itself."""
    a, run = _setup(tmp_path)
    s = Runner(a, run, free_disk_gb=lambda root: 24.9).run_layers(layers=["alpha"])
    assert s.status == "parked"
    assert a.ran == [] and a.sessions == 0
    assert run.read_results() == []
    assert set(s.parked) == {"alpha-0", "alpha-1", "alpha-2", "alpha-3"}
    assert "disk" in run.log_md.read_text()
    prog = json.loads(run.progress_json.read_text())
    assert "alpha-0" in prog["parked"] and prog["status"] == "parked"


def test_disk_guard_runs_at_exactly_the_threshold(tmp_path):
    a, run = _setup(tmp_path)
    s = Runner(a, run, free_disk_gb=lambda root: 25.0).run_layers(layers=["alpha"])
    assert s.status == "done"
    assert a.ran != []


def test_disk_guard_receives_the_run_root(tmp_path):
    a, run = _setup(tmp_path)
    seen = []

    def reading(root):
        seen.append(root)
        return 100.0

    Runner(a, run, free_disk_gb=reading).run_layers(layers=["alpha"])
    assert seen and seen[0] == run.root


def test_free_disk_gb_default_matches_shutil_disk_usage(tmp_path):
    import shutil

    from gauntlet.runner import free_disk_gb

    assert free_disk_gb(tmp_path) == pytest.approx(shutil.disk_usage(tmp_path).free / 1e9)


# -- fix round 1 (Opus review, t_90e75c20) ----------------------------------


def test_free_disk_gb_resolves_at_call_time_not_bind_time(tmp_path, monkeypatch):
    """I3: the default must not be bound once when Runner.__init__ is
    defined -- monkeypatching gauntlet.runner.free_disk_gb (as the conftest
    fixtures do globally) must reach a Runner that never passed its own
    free_disk_gb, the way 45 existing `Runner(...)` calls in this suite do."""
    import gauntlet.runner as runner_mod

    a, run = _setup(tmp_path)
    monkeypatch.setattr(runner_mod, "free_disk_gb", lambda root: 1.0)  # far below the floor
    s = Runner(a, run).run_layers(layers=["alpha"])
    assert s.status == "parked"
    assert a.ran == []


def test_retain_error_is_logged_not_fatal(tmp_path):
    """I2: a retention failure (e.g. an OSError from the filesystem) must
    reach the log, never the run -- this card exists to keep a night ALIVE,
    so retention itself must never be what kills one."""
    a, run = _setup(tmp_path)
    a.retain_keep = lambda layer: ()

    def boom(case_id, keep=()):
        raise OSError("disk gone")

    run.retain = boom

    s = Runner(a, run).run_layers(layers=["alpha"])

    assert s.status == "done"
    assert a.ran == ["alpha-0", "alpha-1", "alpha-2", "alpha-3"]
    assert run.read_result("alpha-0") is not None
    assert "retain alpha-0 failed" in run.log_md.read_text()


def test_disk_park_stops_the_run_without_opening_later_layers(tmp_path):
    """M3: once the guard parks one layer, every later layer would just
    immediately re-park its own first case on the same disk floor -- the
    run stops there instead of doing that once per remaining layer."""
    a, run = _setup(tmp_path)  # alpha (P0 first) + beta, per ToyAdapter.layers()
    s = Runner(a, run, free_disk_gb=lambda root: 24.9).run_layers()
    assert s.status == "parked"
    assert a.sessions == 0
    prog = json.loads(run.progress_json.read_text())
    assert prog["layers"]["alpha"]["status"] == "parked"
    assert "beta" not in prog["layers"]  # never even registered


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


# -- N15: session-close pruning -------------------------------------------


class _DirSession(ToySession):
    def __init__(self, adapter, root):
        super().__init__(adapter)
        self.root = root

    def session_dir(self):
        return None if self.closed else self.root


def _prune_setup(tmp_path, bad=(), **runner_kw):
    from gauntlet.tests.toy import ToyAdapter

    class A(ToyAdapter):
        def open_session(self, layer, run_root):
            self.sessions += 1
            root = run.sessions_dir / layer / f"{self.sessions:03d}-x"
            for d, f, n in (
                ("frames/r", "a.ORF", 4000),
                ("frames/r", "a.ORF.xmp", 10),
                ("card", "c.ORF", 3000),
                ("render", "r.png", 500),
                ("library", "library.db", 20),
                ("config", "c.json", 5),
            ):
                (root / d).mkdir(parents=True, exist_ok=True)
                (root / d / f).write_bytes(b"x" * n)
            (root / "darktable.log").write_text("log")
            return _DirSession(self, root)

        def session_prune_names(self):
            return ("frames", "card", "render")

        def session_prune_keep(self):
            return ("*.xmp",)

    a = A(behaviours={c: "silent" for c in bad})
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    return a, run, Runner(a, run, session_max_cases=2, **runner_kw)


def _kept(run, layer="alpha"):
    out = {}
    for d in sorted((run.sessions_dir / layer).glob("*")):
        out[d.name] = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
    return out


_KEPT = ["config/c.json", "darktable.log", "frames/r/a.ORF.xmp", "library/library.db"]


def test_closed_passing_session_is_pruned_to_the_kept_set(tmp_path):
    a, run, r = _prune_setup(tmp_path)
    r.run_layers(layers=["alpha"])
    k = _kept(run)
    assert k and all(v == _KEPT for v in k.values())


def test_closed_session_with_a_nonpass_case_keeps_its_full_folder(tmp_path):
    a, run, r = _prune_setup(tmp_path, bad=["alpha-2"])
    r.run_layers(layers=["alpha"])
    k = _kept(run)
    assert k["001-x"] == _KEPT
    assert "card/c.ORF" in k["002-x"] and "frames/r/a.ORF" in k["002-x"]


def test_prune_all_mode_prunes_nonpass_sessions_too(tmp_path):
    a, run, r = _prune_setup(tmp_path, bad=["alpha-2"], session_prune="all")
    r.run_layers(layers=["alpha"])
    assert all(v == _KEPT for v in _kept(run).values())


def test_prune_off_leaves_everything(tmp_path):
    a, run, r = _prune_setup(tmp_path, session_prune="off")
    r.run_layers(layers=["alpha"])
    assert all("card/c.ORF" in v for v in _kept(run).values())


def test_nonpass_sessions_beyond_the_cap_are_pruned_oldest_first(tmp_path):
    a, run, r = _prune_setup(tmp_path, bad=["alpha-0", "alpha-2"], session_keep_nonpass_max=1)
    r.run_layers(layers=["alpha"])
    k = _kept(run)
    assert k["001-x"] == _KEPT and "card/c.ORF" in k["002-x"]


def test_low_disk_prunes_nonpass_oldest_first_before_parking(tmp_path):
    a, run, r = _prune_setup(tmp_path, bad=["alpha-0", "alpha-2"], session_keep_nonpass_max=9)
    order = []
    orig = r._prune_one

    def prune_one(root):
        order.append(root.name)
        orig(root)

    r._prune_one = prune_one
    # 27 GB free: above the 25 floor but inside the 5 GB margin; pruning one
    # session frees enough (the reading rises once a folder was pruned).
    r.free_disk_gb = lambda root: 27.0 if len(order) < 1 else 40.0
    s = r.run_layers(layers=["alpha"])
    assert s.status == "done"
    assert order[0] == "001-x"
    assert _kept(run)["001-x"] == _KEPT


def test_low_disk_still_parks_when_pruning_cannot_free_enough(tmp_path):
    a, run, r = _prune_setup(tmp_path, bad=["alpha-0", "alpha-2"], session_keep_nonpass_max=9)
    r.free_disk_gb = lambda root: 20.0
    s = r.run_layers(layers=["alpha"])
    assert s.status == "parked"


def test_open_session_and_outside_files_are_never_touched(tmp_path):
    a, run, r = _prune_setup(tmp_path, session_prune="all")
    outside = run.root / "previews" / "keep.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"p")
    r.run_layers(layers=["alpha"])
    assert outside.exists()
    # the open session: sweep with its root as open_root prunes nothing
    root = run.sessions_dir / "alpha" / "009-x"
    (root / "frames").mkdir(parents=True)
    (root / "frames" / "f.ORF").write_bytes(b"x")
    r._ledger[root] = ["pass"]
    r._sweep(root)
    assert (root / "frames" / "f.ORF").exists()
    with pytest.raises(AssertionError):
        run.prune_session(run.root / "previews", ("keep.png",))


def test_resume_works_when_closed_session_frames_are_gone(tmp_path):
    a, run, r = _prune_setup(tmp_path, bad=["alpha-2"])
    r.run_layers(layers=["alpha"])
    r2 = Runner(a, run, session_max_cases=2, session_prune="all")
    a.ran.clear()
    r2.run_layers(layers=["alpha"], resume=True)
    r3 = Runner(a, run, session_max_cases=2, session_prune="all")
    r3.run_layers(layers=["alpha"], only_failed=True)
    assert a.ran == ["alpha-2"]


def test_two_runners_over_one_run_dir_never_prune_each_others_folders(tmp_path):
    a, run, ra = _prune_setup(
        tmp_path, bad=["alpha-0", "alpha-2"], session_prune="all", session_keep_nonpass_max=0
    )
    rb = Runner(a, run, session_max_cases=2, session_prune="all", session_keep_nonpass_max=0)
    mine = run.sessions_dir / "alpha" / "090-b-open"
    kept_fail = run.sessions_dir / "alpha" / "091-b-closed-nonpass"
    stranger = run.sessions_dir / "alpha" / "092-nobody"
    for d in (mine, kept_fail, stranger):
        (d / "frames").mkdir(parents=True)
        (d / "frames" / "f.ORF").write_bytes(b"x" * 100)
    rb._ledger[mine] = ["pass"]
    rb._ledger[kept_fail] = ["fail"]
    ra.free_disk_gb = lambda root: 27.0  # inside the margin: low-disk path runs too
    ra.run_layers(layers=["alpha"])
    for d in (mine, kept_fail, stranger):
        assert (d / "frames" / "f.ORF").exists(), d
    assert not set(ra._ledger) & {mine, kept_fail, stranger}
    # B's own low-disk path spares its open session and never reaches strangers
    rb._prune_for_disk(_DirSession(a, mine), lambda root: 20.0)
    assert (mine / "frames" / "f.ORF").exists() and (stranger / "frames" / "f.ORF").exists()
    assert not (kept_fail / "frames" / "f.ORF").exists()  # B's own closed one: prunable
