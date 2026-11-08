import json
from pathlib import Path

import pytest

from gauntlet.adapter import Park, load_adapter
from gauntlet.case import Case, Expect
from gauntlet.generate import generate_all
from gauntlet.report import layer_table
from gauntlet.store import RunDir
from scouter.core import dtprocess
from tests.gauntlet import adapter as adapter_mod
from tests.gauntlet import session as sess
from tests.gauntlet.adapter import ScouterAdapter
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeDt, FakeMcp


def test_generation_passes_the_engine_gates(tmp_path):
    a = ScouterAdapter(tmp_path)
    cases, counts, _ = generate_all(a)
    got = {c.spec.name: c.got for c in counts}
    assert got == {"offline": 3214, "coverage": 1636, "scenarios": 701, "scenarios_agent": 160,
                   "bulk": 40, "looks": 469, "robustness": 32, "parity": 92, "install": 10,
                   "guide": 35}
    assert sum(got.values()) == 6389


def test_notes_and_signoff_without_live_layers(tmp_path):
    a = ScouterAdapter(tmp_path)
    assert "P0 dry run: 100 requests" in a.generation_notes([])
    assert "not checked" in a.signoff_lines()[0]


def test_corpus_missing_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="corpus survey"):
        ScouterAdapter(tmp_path).corpus()


# -- carry-over requirements (t28-carry.md) ---------------------------------

ENV_KEYS = ("SCOUTER_CONFIG", "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH",
            "SCOUTER_DARKTABLE_LIBRARY_DB")


@pytest.fixture
def pinned_env(tmp_path, monkeypatch):
    """The adapter writes os.environ (isolate_process / point_cache): pin the keys
    to tmp so monkeypatch restores them, and never resolve Pratik's real cache."""
    for k in ENV_KEYS:
        monkeypatch.setenv(k, str(tmp_path / "pinned" / k.lower()))
    return tmp_path


@pytest.fixture
def fake_corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}, {"rel": "H.ORF", "body": "sony_a7"}],
           "roles": {"olympus": {"hero": "H.ORF"}, "sony_a7": {"hero": "H.ORF"}}}
    return Corpus(doc, trial)


def _routed(tmp_path, corpus, layer="coverage", dt=None):
    a = ScouterAdapter(tmp_path / "runs")
    a._corpus = corpus
    run = RunDir(tmp_path / "runs", "stamp").ensure()
    dts = []

    def factory(corpus, root, body, run_root):
        d = dt or FakeDt()
        dts.append(d)
        return sess.ScouterSession(corpus, root, body, dt=d, run_root=run_root,
                                   mcp_factory=lambda env, cwd: FakeMcp())
    return a, run, adapter_mod.RoutedSession(a, layer, run, session_factory=factory), dts


def _case(layer="coverage", body="olympus"):
    return Case(id=f"{layer}-x-{body}", layer=layer, tier="P1", mode="tool", steps=[],
                expect=Expect("edit", []), body=body, frame_role="hero")


def test_runner_and_mcp_child_resolve_one_owner_record(pinned_env, fake_corpus, monkeypatch):
    """Carry 1: a heal inside the MCP server must write the record the runner reads."""
    _, _, routed, _ = _routed(pinned_env, fake_corpus)
    s = routed._session("olympus")
    runner_side = dtprocess.owner_path()
    with monkeypatch.context() as m:  # the MCP server child's view: its own env
        m.setenv("SCOUTER_CACHE", s.staged.env["SCOUTER_CACHE"])
        child_side = dtprocess.owner_path()
    assert runner_side == child_side == s.root / "cache" / dtprocess.OWNER_FILE


def test_owner_record_follows_the_process_but_never_from_the_real_cache(
        pinned_env, fake_corpus, monkeypatch):
    real = pinned_env / "real-cache"  # outside the runs root: stands for ~/.cache/scouter
    real.mkdir()
    (real / dtprocess.OWNER_FILE).write_text('{"pid": 1}')
    monkeypatch.setenv("SCOUTER_CACHE", str(real))
    _, _, routed, _ = _routed(pinned_env, fake_corpus)
    first = routed._session("olympus")
    assert not (first.root / "cache" / dtprocess.OWNER_FILE).exists()
    (first.root / "cache" / dtprocess.OWNER_FILE).write_text('{"pid": 2}')  # "launched"
    second = routed._session("sony_a7")  # body switch: a record carried, not lost
    assert (second.root / "cache" / dtprocess.OWNER_FILE).read_text() == '{"pid": 2}'
    routed.close()  # back to the process cache, still carrying it
    assert dtprocess.owner_path() == \
        pinned_env / "runs" / adapter_mod.PROCESS_DIR / "cache" / dtprocess.OWNER_FILE
    assert dtprocess.owner_path().read_text() == '{"pid": 2}'


def test_every_declared_looks_gate_has_tagged_checks(tmp_path):
    """Carry 2: sourced per component >= 0.90, inferred >= 0.95; no n=0 gate."""
    a = ScouterAdapter(tmp_path)
    cases = a.generate("looks")
    spec = next(s for s in a.layers() if s.name == "looks")
    comps = {t["component"] for c in cases for t in c.meta.get("check_tags") or []
             if t["confidence"] in ("documented", "community-consensus")}
    assert {g[1][0] for g in spec.check_gates if g[0] == "sourced_component"} == comps
    assert all(g[2] == 0.90 for g in spec.check_gates if g[0] == "sourced_component")
    assert ("confidence", ("inferred",), 0.95) in spec.check_gates
    results = [{"id": c.id, "layer": c.layer, "tier": c.tier, "meta": c.meta,
                "verdict": {"score": "pass",
                            "checks": [{"ok": True} for _ in c.expect.checks]}}
               for c in cases]
    row = next(r for r in layer_table(results, a) if r["layer"] == "looks")
    assert row["check_gates"] and all(g["n"] > 0 and g["ok"] for g in row["check_gates"])


def test_run_config_keeps_auto_heal_off(tmp_path):
    """Carry 3: only the robustness watchdog probe turns auto_heal on (try/finally)."""
    from scouter import config

    cfg_path = sess.write_config(tmp_path / "run")
    assert "auto_heal" not in (json.loads(cfg_path.read_text()).get("watchdog") or {})
    assert config.load(str(cfg_path)).watchdog.auto_heal is False


def test_install_runs_in_order_on_one_rehearsal(pinned_env, monkeypatch):
    """Carry 4: leak_check reads teardown's facts off the same Rehearsal."""
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {"x.ORF": "h"})
    a = ScouterAdapter(pinned_env / "runs")
    orders = [c.meta["order"] for c in a.generate("install")]
    assert orders == sorted(orders) == list(range(10))
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    first = a.open_session("install", run)
    assert a.open_session("install", run) is first  # the runner reopened (cap/rotation)
    assert first.rehearsal.root == run.root / "install"


def test_robustness_keeps_generator_order(tmp_path):
    """Carry 5: wd_auto_heal before wd_heal_cap (per-job heal counter)."""
    from tests.gauntlet.layers import robustness

    a = ScouterAdapter(tmp_path)
    ids = [c.id for c in a.generate("robustness")]
    assert ids == [c.id for c in robustness.generate(a.tax())]
    assert ids.index("rob-wd_auto_heal") < ids.index("rob-wd_heal_cap")


def test_a_foreign_darktable_refuses_the_run(pinned_env, fake_corpus):
    """(final fix I2, supersedes carry 7 for this one failure): a foreign
    darktable is not a per-case result (thousands would be written and
    --resume would treat them done); it refuses the run."""
    from gauntlet.adapter import RefuseRun

    _, _, routed, _ = _routed(pinned_env, fake_corpus, dt=FakeDt(foreign=[4242]))
    with pytest.raises(RefuseRun, match="4242"):
        routed.run_case(_case())
    assert routed.current is None


def test_preflight_refuses_a_live_layer_while_a_foreign_darktable_runs(pinned_env):
    a = ScouterAdapter(pinned_env / "runs")
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    a.foreign_pids = lambda: [4242]
    assert "4242" in a.preflight(run, "coverage")
    assert a.preflight(run, "offline") is None  # no darktable needed
    a.foreign_pids = lambda: []
    assert a.preflight(run, "coverage") is None


def test_preflight_refuses_when_it_cannot_tell(pinned_env):
    a = ScouterAdapter(pinned_env / "runs")
    run = RunDir(pinned_env / "runs", "stamp").ensure()

    def unknown():
        raise OSError("pgrep failed")
    a.foreign_pids = unknown
    assert "cannot tell" in a.preflight(run, "coverage")


def test_runner_stops_on_a_foreign_darktable_without_case_results(pinned_env, monkeypatch):
    """End to end through the engine: preflight refuses, no result is written."""
    from gauntlet.runner import Runner

    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {})
    a = ScouterAdapter(pinned_env / "runs")
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    run.write_cases([_case("coverage"), _case("coverage", "sony_a7")])
    a.foreign_pids = lambda: [4242]
    s = Runner(a, run).run_layers(layers=["coverage"])
    assert s.status.startswith("blocked: foreign darktable") and run.read_results() == []


def test_a_failed_launch_is_a_typed_session_block(pinned_env, fake_corpus):
    _, _, routed, _ = _routed(pinned_env, fake_corpus, dt=FakeDt(ready=(False, False)))
    obs = routed.run_case(_case())
    assert obs.blocked.startswith("session: launch:")


def test_park_reaches_the_runner(pinned_env, fake_corpus, monkeypatch):
    """Carry 7: an agent usage limit parks; RoutedSession must not swallow it."""
    from tests.gauntlet import agent

    def limit(self, case):
        raise Park("agent usage limit", until=None)

    monkeypatch.setattr(agent.AgentSession, "run_case", limit)
    _, _, routed, _ = _routed(pinned_env, fake_corpus, layer="scenarios_agent")
    with pytest.raises(Park):
        routed.run_case(_case("scenarios_agent"))


def test_usage_budget_parks_before_the_session_opens(pinned_env, fake_corpus, monkeypatch):
    """Row 501 item H / card t_e82c2951: the 60% usage parker fires once per
    scenarios_agent case, before the (expensive) darktable session even
    opens -- covering both "before each agent-layer case" and, for the
    layer's first case, "once before the agent layer starts" in the same
    check. A non-agent layer must never see it (the tool layers run to
    the CLI's own wall limit, item A's fallback, not this meter)."""
    from tests.gauntlet import agent_budget

    def over_budget():
        raise Park("usage_meter: 75% >= 60%", until=12345.0)

    monkeypatch.setattr(agent_budget, "check_usage_budget", over_budget)
    a, run, routed, dts = _routed(pinned_env, fake_corpus, layer="scenarios_agent")
    with pytest.raises(Park, match="usage_meter"):
        routed.run_case(_case("scenarios_agent"))
    assert dts == []  # no darktable session was opened before the park


def test_usage_budget_does_not_gate_tool_layers(pinned_env, fake_corpus, monkeypatch):
    """Only scenarios_agent reads the usage meter; a tool-mode layer's own
    session must still open normally even when the meter is over budget."""
    from tests.gauntlet import agent_budget

    def over_budget():
        raise Park("usage_meter: 75% >= 60%", until=12345.0)

    monkeypatch.setattr(agent_budget, "check_usage_budget", over_budget)
    _, _, routed, dts = _routed(pinned_env, fake_corpus, layer="coverage")
    routed.run_case(_case("coverage"))
    assert len(dts) == 1  # the coverage layer's session opened as usual


def test_crash_reports_are_darktable_or_python_only(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "CRASH_DIR", tmp_path)
    for name in ("darktable-2026.ips", "Python-1.crash", "Safari.ips", "darktable.txt"):
        (tmp_path / name).write_text("x")
    a = ScouterAdapter(tmp_path)
    assert a.crash_count() == 2
    assert a.crash_reports_since(0.0) == [str(tmp_path / "Python-1.crash"),
                                          str(tmp_path / "darktable-2026.ips")]
    assert a.crash_reports_since(4102444800.0) == []  # year 2100


def test_gauntlet_json_loads_the_scouter_adapter():
    cfg = json.loads((Path(__file__).resolve().parents[2] / "gauntlet.json").read_text())
    assert cfg == {"adapter": "tests.gauntlet.adapter:make",
                   "runs_root": "~/Pictures/ScouterScratch/gauntlet", "title": "Scouter Gauntlet"}
    assert isinstance(load_adapter(cfg["adapter"]), ScouterAdapter)


# -- fix round 1 (t28-r1-findings.md) ----------------------------------------


@pytest.mark.parametrize("changed", [False, True])
def test_a_fresh_report_process_checks_the_originals(tmp_path, monkeypatch, changed):
    """I1: `gauntlet report` builds a fresh adapter; it reads originals.before.json."""
    now = {"a.ORF": "h1", "b.ORF": "h2" if not changed else "EDITED"}
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: dict(now))
    run = RunDir(tmp_path, "stamp").ensure()
    (run.root / "originals.before.json").write_text(json.dumps({"a.ORF": "h1", "b.ORF": "h2"}))
    a = ScouterAdapter(tmp_path)  # fresh: nothing in memory
    line = a.signoff_lines(run)[0]
    assert line.endswith("NO" if changed else "yes") and "sha256 of 2" in line
    assert a.signoff_blockers(run) == (["EditingTrial originals changed"] if changed else [])


def test_no_live_layer_means_not_checked_and_no_blocker(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals",
                        lambda: pytest.fail("must not hash without a before snapshot"))
    run = RunDir(tmp_path, "stamp").ensure()
    a = ScouterAdapter(tmp_path)
    assert "not checked" in a.signoff_lines(run)[0] and a.signoff_blockers(run) == []


def test_looks_cases_are_grouped_by_routed_body_golden_first(tmp_path):
    """I2: 233 body switches -> at most 3; the calibration case stays first."""
    cases = ScouterAdapter(tmp_path).generate("looks")
    bodies = [adapter_mod.routed_body(c) for c in cases]
    assert sum(1 for x, y in zip(bodies, bodies[1:]) if x != y) <= 3
    assert cases[0].steps[0].tool == "chart_golden"
    assert len(cases) == 469


def test_bulk_keeps_generator_order(tmp_path):
    """I2: bulk shares out_dir within a session, so it is not body-sorted."""
    from tests.gauntlet.layers import bulk

    a = ScouterAdapter(tmp_path)
    assert [c.id for c in a.generate("bulk")] == [c.id for c in bulk.generate(a.tax())]


def test_run_case_repoints_a_cache_a_late_close_reset(pinned_env, fake_corpus, monkeypatch):
    """N1: re-point to this session's cache, and never carry a stale record over it."""
    from tests.gauntlet import execute

    monkeypatch.setattr(execute, "run_tool_case", lambda s, c: "ran")
    _, _, routed, _ = _routed(pinned_env, fake_corpus)
    s = routed._session("olympus")
    live = s.root / "cache" / dtprocess.OWNER_FILE
    live.write_text('{"pid": "live"}')
    stale = pinned_env / "runs" / adapter_mod.PROCESS_DIR / "cache"
    stale.mkdir(parents=True)
    (stale / dtprocess.OWNER_FILE).write_text('{"pid": "stale"}')
    monkeypatch.setenv("SCOUTER_CACHE", str(stale))  # what a late close does
    assert routed.run_case(_case()) == "ran"
    assert dtprocess.owner_path() == live and live.read_text() == '{"pid": "live"}'


def test_a_vanished_crash_file_does_not_hide_the_others(tmp_path, monkeypatch):
    """N2: stat per file."""
    monkeypatch.setattr(adapter_mod, "CRASH_DIR", tmp_path)
    (tmp_path / "darktable-1.ips").write_text("x")
    (tmp_path / "darktable-gone.ips").symlink_to(tmp_path / "vanished")  # stat -> OSError
    a = ScouterAdapter(tmp_path)
    assert a.crash_reports_since(0.0) == [str(tmp_path / "darktable-1.ips")]


def test_isolate_process_pins_the_library_db(pinned_env):
    """N3: the runner process never falls back to the real darktable library."""
    import os

    root = pinned_env / "runs" / adapter_mod.PROCESS_DIR
    adapter_mod.isolate_process(root)
    assert os.environ["SCOUTER_DARKTABLE_LIBRARY_DB"] == str(root / "library" / "library.db")
    assert os.environ["SCOUTER_CACHE"] == str(root / "cache")


# -- final fix I1: resume after a killed run ---------------------------------

def _fake_owned(pid_ok):
    """owned_pid stand-in: accepts the record at the CURRENT owner_path() only
    when its pid is `pid_ok` (never runs ps, never signals)."""
    def owned():
        try:
            pid = json.loads(dtprocess.owner_path().read_text())["pid"]
        except (OSError, ValueError, KeyError):
            return None
        return pid if pid == pid_ok else None
    return owned


def test_resume_adopts_a_killed_sessions_owner_record(pinned_env):
    """A killed run left its darktable's owner record in a session cache; on
    resume that darktable must read as ours (quit), not foreign (block all)."""
    runs = pinned_env / "runs"
    run = RunDir(runs, "stamp").ensure()
    stale = run.sessions_dir / "coverage" / "001-olympus" / "cache"
    live = run.sessions_dir / "coverage" / "002-sony_a7" / "cache"
    for d, pid in ((stale, 111), (live, 777)):
        d.mkdir(parents=True)
        (d / dtprocess.OWNER_FILE).write_text(json.dumps({"pid": pid}))
    process_cache = runs / adapter_mod.PROCESS_DIR / "cache"
    process_cache.mkdir(parents=True)
    adopted = adapter_mod.adopt_leftover_owner(run, process_cache, owned_pid=_fake_owned(777))
    assert adopted == live / dtprocess.OWNER_FILE
    assert json.loads((process_cache / dtprocess.OWNER_FILE).read_text())["pid"] == 777


def test_resume_without_a_live_leftover_changes_nothing(pinned_env):
    runs = pinned_env / "runs"
    run = RunDir(runs, "stamp").ensure()
    stale = run.sessions_dir / "coverage" / "001-olympus" / "cache"
    stale.mkdir(parents=True)
    (stale / dtprocess.OWNER_FILE).write_text(json.dumps({"pid": 111}))
    process_cache = runs / adapter_mod.PROCESS_DIR / "cache"
    process_cache.mkdir(parents=True)
    assert adapter_mod.adopt_leftover_owner(run, process_cache,
                                            owned_pid=_fake_owned(777)) is None
    assert not (process_cache / dtprocess.OWNER_FILE).exists()


def test_open_session_adopts_before_the_first_body(pinned_env, monkeypatch):
    seen = []
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {})
    monkeypatch.setattr(adapter_mod, "adopt_leftover_owner",
                        lambda run, cache, **kw: seen.append((run.root, cache)))
    a = ScouterAdapter(pinned_env / "runs")
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    a.open_session("parity", run)
    assert seen == [(run.root, pinned_env / "runs" / adapter_mod.PROCESS_DIR / "cache")]


def test_resumed_session_dirs_number_after_the_highest(pinned_env, fake_corpus):
    """A resumed run must not reuse an old NNN-body dir (edited sidecars/library)."""
    _, run, _, _ = _routed(pinned_env, fake_corpus)
    (run.sessions_dir / "coverage" / "007-olympus").mkdir(parents=True)
    (run.sessions_dir / "coverage" / "003-sony_a7").mkdir(parents=True)
    _, _, routed, _ = _routed(pinned_env, fake_corpus)
    s = routed._session("olympus")
    assert s.root.name == "008-olympus"


def test_mcp_death_reaches_the_observation_and_the_session_reopens(pinned_env, fake_corpus,
                                                                    monkeypatch):
    """(final fix I3): release bar 1 counts MCP-server deaths as crashes."""
    from gauntlet.adapter import Observation
    from gauntlet.evaluate import evaluate
    from tests.gauntlet import execute
    from tests.gauntlet.policy import POLICY

    def dies(s, c):
        s.mcp_deaths.append("mcp_server_death: exit 1")
        s.mcp_dead = True
        return Observation()

    monkeypatch.setattr(execute, "run_tool_case", dies)
    _, _, routed, _ = _routed(pinned_env, fake_corpus)
    case = _case()
    obs = routed.run_case(case)
    assert obs.crash_reports == ["mcp_server_death: exit 1"]
    assert evaluate(case, obs, POLICY).outcome == "crash"
    first = routed.current
    monkeypatch.setattr(execute, "run_tool_case", lambda s, c: Observation())
    routed.run_case(case)
    assert routed.current is not first  # the dead session was replaced


def test_a_hang_outside_robustness_still_recovers(pinned_env, fake_corpus, monkeypatch):
    """Fix round 1 review M1 (medium): hang recovery lived only in
    faults.run_fault_case (the robustness layer). A hang in scenarios/looks/
    coverage/bulk left darktable wedged with nothing to recover it -- with
    Fix B, every LATER case in that session then reads inherited_wedge
    forever, and nothing ever clears it."""
    from gauntlet.adapter import Observation
    from tests.gauntlet import execute, faults

    calls: list = []
    monkeypatch.setattr(faults, "fence_and_recover", lambda s: calls.append(s) or [])
    monkeypatch.setattr(execute, "run_tool_case", lambda s, c: Observation(blocked="hang"))
    _, _, routed, _ = _routed(pinned_env, fake_corpus, layer="coverage")
    obs = routed.run_case(_case("coverage"))
    assert obs.blocked == "hang"
    assert calls == [routed.current]


def test_an_inherited_wedge_outside_robustness_also_recovers(pinned_env, fake_corpus,
                                                              monkeypatch):
    """(M1) inherited_wedge triggers the same recovery as a hang -- Fix B's
    whole point is that the case that reads it never caused it, so nothing
    else in that layer will ever clear it either."""
    from gauntlet.adapter import Observation
    from tests.gauntlet import execute, faults

    calls: list = []
    monkeypatch.setattr(faults, "fence_and_recover", lambda s: calls.append(s) or [])
    monkeypatch.setattr(execute, "run_tool_case",
                        lambda s, c: Observation(blocked="inherited_wedge"))
    _, _, routed, _ = _routed(pinned_env, fake_corpus, layer="coverage")
    obs = routed.run_case(_case("coverage"))
    assert obs.blocked == "inherited_wedge"
    assert calls == [routed.current]


def test_owner_drift_survives_a_late_close_race(pinned_env, fake_corpus, monkeypatch):
    """Fix round 2 review N2 (blocker): reviewer's six-step owner-drift
    scenario, production shape (row 493, pid 69648). A late close of an
    earlier session can re-point ambient SCOUTER_CACHE back to the process
    cache AFTER a newer session has already pointed it at its own cache --
    that must never happen (RoutedSession.close/abort's own guard), and
    even where it does, a session's own DtControl must still find and quit
    its own darktable, bound to ITS OWN cache, never the ambient one.
    No darktable, no real signals: Popen/_lstart/_comm/_alive/running_pids/
    os.kill are all faked; the real dtprocess.launch/terminate/
    foreign_pids/owned_pid and the real ScouterSession/RoutedSession run
    for real over those fakes."""
    import os
    import signal

    from tests.gauntlet.session import DtControl

    PID = 4242424
    START = "Sun Sep 27 13:42:41 2026"
    alive_state = {"value": False}
    signalled: list = []

    class FakeProc:
        pid = PID

        def kill(self):
            pass

        def wait(self, timeout=None):
            pass

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        alive_state["value"] = True
        return FakeProc()

    def fake_kill(pid, sig):
        signalled.append((pid, sig))
        alive_state["value"] = False

    fake_alive = lambda pid: pid == PID and alive_state["value"]  # noqa: E731

    monkeypatch.setattr(dtprocess.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(dtprocess, "_lstart", lambda pid: START if pid == PID else None)
    monkeypatch.setattr(dtprocess, "_comm", lambda pid: dtprocess.DARKTABLE if pid == PID
                        else None)
    monkeypatch.setattr(dtprocess, "running_pids", lambda: [PID] if alive_state["value"] else [])
    monkeypatch.setattr(dtprocess, "_alive", fake_alive)
    monkeypatch.setattr(dtprocess.os, "kill", fake_kill)
    monkeypatch.setattr(dtprocess, "wait_gui_ready", lambda *a, **k: {"ready": True})
    #: `terminate`'s own `kill`/`alive`/`sleep` parameters default to
    #: `os.kill`/`_alive`/`time.sleep` bound ONCE at module-import time
    #: (ordinary Python default-argument semantics) -- monkeypatching the
    #: module attributes above does not reach a call that never overrides
    #: them, which is exactly how `DtControl.terminate` calls it. Wrap the
    #: real function so every call through this test gets the fakes, the
    #: same way the existing dtprocess unit tests pass them explicitly.
    real_terminate = dtprocess.terminate

    def terminate_with_fakes(pid=None, *, responsive=None, cache_root=None, **kw):
        return real_terminate(pid, responsive=responsive, cache_root=cache_root,
                              kill=fake_kill, alive=fake_alive, sleep=lambda s: None)
    monkeypatch.setattr(dtprocess, "terminate", terminate_with_fakes)

    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = RunDir(pinned_env / "runs", "stamp").ensure()

    def factory(corpus, root, body, run_root):
        return sess.ScouterSession(corpus, root, body, dt=DtControl(), run_root=run_root,
                                   mcp_factory=lambda env, cwd: FakeMcp())

    routed = adapter_mod.RoutedSession(a, "coverage", run, session_factory=factory)

    # 1. Open session A -- launches pid P.
    session_a = routed._session("olympus")
    a_cache = session_a.root / "cache"
    assert alive_state["value"] is True

    # 2/3. Open session B: point_cache (ambient -> B's own cache) runs as
    # part of _session()'s own bookkeeping; simulate A's late close
    # finishing AFTER that -- re-point ambient back to the process cache
    # via the same guarded helper close()/abort() use. The guard must
    # refuse: ambient no longer names A's cache (it is B's now).
    process_cache = a.runs_root / adapter_mod.PROCESS_DIR / "cache"
    routed.n += 1
    b_root = run.sessions_dir / "coverage" / f"{routed.n:03d}-sony_a7"
    from tests.gauntlet.adapter import point_cache

    point_cache(b_root / "cache", *routed._roots())
    assert os.environ.get("SCOUTER_CACHE") == str(b_root / "cache")
    routed._back_to_process_cache_if_mine(a_cache)  # the late close, simulated
    assert os.environ.get("SCOUTER_CACHE") == str(b_root / "cache"), \
        "the guard must refuse to stomp on B's own cache pointer"

    session_b = factory(a.corpus(), b_root, "sony_a7", run.root)
    routed.current = session_b
    session_b.open()
    assert os.environ.get("SCOUTER_CACHE") == str(b_root / "cache")

    # 4. A relaunch must still quit P: session B's own DtControl is bound to
    # B's own cache (`ScouterSession.open` binds it), never the ambient one.
    session_b.relaunch()
    assert (PID, signal.SIGTERM) in signalled or (PID, signal.SIGKILL) in signalled, \
        f"relaunch's quit never signalled our own darktable: {signalled}"
    alive_state["value"] = True  # the relaunch's own new darktable is up

    # 5. Rotate to session C -- opened directly (not through the normal
    # open_body path, whose own old.close() would itself quit P via the
    # SAME fixed terminate() and make the foreign check moot: nothing
    # "foreign" would even be running by the time C opens). P must still be
    # ALIVE here, so foreign() genuinely has to tell "P is ours" (carried
    # over into C's cache by point_cache) from "P is foreign" -- open()
    # must not raise ForeignDarktable.
    #
    # (fix round 3 review R1): `_session()`'s own `point_cache(C's cache,
    # ...)` re-points ambient correctly BEFORE `s.open()` runs -- setting
    # the drift any earlier is overwritten by that call and never reaches
    # `foreign()` at all (the review's own mutation finding: unbinding
    # `DtControl.foreign` left the round-2 version of this test all green).
    # Pin the drift to fire AT the exact call `foreign()` makes, by
    # patching `DtControl.foreign` for this one call: ambient only drifts
    # to the process cache the INSTANT the check runs -- the production
    # race window (another session's late close finishing in between).
    routed.n += 1
    c_root = run.sessions_dir / "coverage" / f"{routed.n:03d}-olympus"
    point_cache(c_root / "cache", *routed._roots())

    real_foreign = DtControl.foreign

    def foreign_under_drift(self_dt):
        monkeypatch.setenv("SCOUTER_CACHE", str(process_cache))
        return real_foreign(self_dt)
    monkeypatch.setattr(DtControl, "foreign", foreign_under_drift)

    session_c = factory(a.corpus(), c_root, "olympus", run.root)
    routed.current = session_c
    session_c.open()  # raises ForeignDarktable on failure

    # 6. Repeat with the drift STILL in place (no re-point this time) -- the
    # _launch retry / fence_and_recover path is covered by the SAME
    # property: DtControl reads/writes through the session's own bound
    # cache, so persisted ambient drift changes nothing for it.
    monkeypatch.setenv("SCOUTER_CACHE", str(process_cache))
    signalled.clear()
    session_c = routed.current
    session_c.relaunch()
    assert (PID, signal.SIGTERM) in signalled or (PID, signal.SIGKILL) in signalled, \
        "relaunch under persisted drift never signalled our own darktable"


def test_recovery_is_never_doubled_up_in_the_robustness_layer(pinned_env, fake_corpus,
                                                                monkeypatch):
    """(M1) faults.run_fault_case already fence_and_recover's its own hangs
    (Fix A/H2) -- run_case must never call it a second time for that layer."""
    from gauntlet.adapter import Observation
    from tests.gauntlet import faults

    calls: list = []
    monkeypatch.setattr(faults, "fence_and_recover", lambda s: calls.append(s) or [])
    monkeypatch.setattr(faults, "run_fault_case",
                        lambda s, c: Observation(blocked="hang"))
    _, _, routed, _ = _routed(pinned_env, fake_corpus, layer="robustness")
    obs = routed.run_case(_case("robustness"))
    assert obs.blocked == "hang"
    assert calls == []  # run_case itself never calls it for this layer


class _KillableMcp(FakeMcp):
    """(fix round 3 review N7): `FakeMcp` has no `.proc` at all, so
    `fence_and_recover`'s own MCP-kill step (N4) never fires against it and
    `mcp_dead` never gets set in the first place -- a test built on plain
    `FakeMcp` would pass whether or not `reopen_mcp` clears the flag. A
    tiny `.proc.kill()` seam makes the kill step (and so N4's `mcp_dead =
    True`) actually happen, so the reopen path genuinely has something to
    clear."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.proc = type("P", (), {"kill": lambda self: None})()


def test_a_successful_fence_keeps_the_same_session(pinned_env, fake_corpus, monkeypatch):
    """Fix round 3 review N7 (medium): fence_and_recover's own deliberate
    SIGKILL of the old MCP client sets mcp_dead=True (N4), and reopen_mcp
    installs a fresh client but (before this fix) never cleared it --
    RoutedSession._session then discarded the healthy, just-fenced session
    on the NEXT case and opened a brand new one: one more relaunch plus a
    full open() (staging, launch, noise-floor renders) for every fenced
    case, and one more chance for a ForeignDarktable refusal to stop the
    whole layer. Uses the REAL fence_and_recover/reopen_mcp (not mocked),
    unlike the M1/N3 tests above -- that mock is exactly how N7 got
    through review round 2."""
    from gauntlet.adapter import Observation
    from tests.gauntlet import execute

    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = RunDir(pinned_env / "runs", "stamp").ensure()

    def factory(corpus, root, body, run_root):
        return sess.ScouterSession(corpus, root, body, dt=FakeDt(), run_root=run_root,
                                   mcp_factory=lambda env, cwd: _KillableMcp())

    routed = adapter_mod.RoutedSession(a, "coverage", run, session_factory=factory)
    routed.run_case(_case("coverage"))
    s = routed.current
    monkeypatch.setattr(execute, "run_tool_case", lambda sess, c: Observation(blocked="hang"))
    routed.run_case(_case("coverage"))  # this case's own hang fences and recovers
    assert s.mcp_dead is False, "reopen_mcp succeeded but left mcp_dead set"
    monkeypatch.setattr(execute, "run_tool_case", lambda sess, c: Observation())
    routed.run_case(_case("coverage"))  # the NEXT case must reuse the SAME session
    assert routed.current is s


def test_a_fence_whose_quit_failed_still_gets_replaced(pinned_env, fake_corpus, monkeypatch):
    """(N7, control) a fence whose quit never confirmed ok never reaches
    reopen_mcp -- mcp_dead must stay set, so the next case still reopens
    (there is no healthy session left to keep)."""
    from gauntlet.adapter import Observation
    from tests.gauntlet import execute

    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = RunDir(pinned_env / "runs", "stamp").ensure()

    def factory(corpus, root, body, run_root):
        return sess.ScouterSession(corpus, root, body, dt=FakeDt(terminate_ok=False),
                                   run_root=run_root, mcp_factory=lambda env, cwd: _KillableMcp())

    routed = adapter_mod.RoutedSession(a, "coverage", run, session_factory=factory)
    routed.run_case(_case("coverage"))
    s = routed.current
    monkeypatch.setattr(execute, "run_tool_case", lambda sess, c: Observation(blocked="hang"))
    routed.run_case(_case("coverage"))
    assert s.mcp_dead is True
    routed.run_case(_case("coverage"))
    assert routed.current is not s


# -- final fix I7: eager open + harness allowance ------------------------------

def test_harness_allowance_is_a_base_plus_a_share_per_render(tmp_path):
    from gauntlet.case import Check

    a = ScouterAdapter(tmp_path)
    plain = _case()
    assert a.harness_allowance_s(plain) == adapter_mod.HARNESS_BASE_S
    stats = Case(id="c-s", layer="coverage", tier="P1", mode="tool", steps=[],
                 expect=Expect("edit", [Check("M2", stat="L_mean", sign=1)]), body="olympus")
    assert a.harness_allowance_s(stats) == \
        adapter_mod.HARNESS_BASE_S + adapter_mod.RENDERS_PER_STATS_CASE * \
        adapter_mod.HARNESS_PER_RENDER_S
    assert a.harness_allowance_s(Case(**{**vars(plain), "layer": "offline"})) == 0.0


def _write_run(pinned_env, cases):
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    run.write_cases(cases)
    return run


def test_open_session_opens_the_first_body_eagerly(pinned_env, fake_corpus, monkeypatch):
    """The product session opens inside open_session (the runner's 180 s open
    cap), not lazily inside the first case's cap."""
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {})
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = _write_run(pinned_env, [_case("coverage", "sony_a7"), _case("coverage", "olympus")])
    run.write_result("coverage-x-sony_a7", {"id": "coverage-x-sony_a7"})  # done: resume
    dts = []

    def factory(corpus, root, body, run_root):
        d = FakeDt()
        dts.append(d)
        return sess.ScouterSession(corpus, root, body, dt=d, run_root=run_root,
                                   mcp_factory=lambda env, cwd: FakeMcp())
    a.session_factory = factory
    routed = a.open_session("coverage", run)
    assert routed.current is not None and routed.current.body == "olympus"
    assert dts[0].launches == 1


def test_eager_open_failure_raises_to_the_runner_and_leaves_no_session(pinned_env, fake_corpus,
                                                                       monkeypatch):
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {})
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = _write_run(pinned_env, [_case("coverage")])
    a.session_factory = lambda corpus, root, body, run_root: sess.ScouterSession(
        corpus, root, body, dt=FakeDt(ready=(False, False)), run_root=run_root,
        mcp_factory=lambda env, cwd: FakeMcp())
    with pytest.raises(sess.SessionBlocked):
        a.open_session("coverage", run)
    assert dtprocess.owner_path().parent == pinned_env / "runs" / adapter_mod.PROCESS_DIR / "cache"


# -- pre-night-1 N1/N2/N3 ------------------------------------------------------

class _Proc:
    def __init__(self, pid):
        self.pid, self.killed = pid, False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


def test_scouter_open_cap_covers_a_cold_launch():
    assert ScouterAdapter.session_open_cap_s == 420.0


def test_abort_open_with_nothing_opening_is_a_typed_no_op(tmp_path):
    res = ScouterAdapter(tmp_path / "runs").abort_open()
    assert res["ok"] is True and res["killed"] == []


def test_abort_open_kills_only_its_own_in_flight_session(pinned_env, fake_corpus, monkeypatch):
    """N1: an open past the runner's cap is aborted from the runner thread: the
    MCP child still initialising and the darktable this session launched are
    killed by their recorded pids; the late open thread then neither re-reads
    the (by now foreign) owner record nor resets SCOUTER_CACHE."""
    import threading

    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {})
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = _write_run(pinned_env, [_case("coverage")])
    entered, release = threading.Event(), threading.Event()
    dt = FakeDt()
    dt.launch = lambda frame, library, log, env=None: "rec-777"
    proc = _Proc(9001)
    box = {}

    def mcp_factory(env, cwd):
        box["s"]._mcp_pending = type("C", (), {"proc": proc})()
        entered.set()
        release.wait(5)
        return FakeMcp()

    def factory(corpus, root, body, run_root):
        box["s"] = sess.ScouterSession(corpus, root, body, dt=dt, run_root=run_root,
                                       mcp_factory=mcp_factory)
        return box["s"]
    a.session_factory = factory
    def opener():
        try:
            box["routed"] = a.open_session("coverage", run)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
    t = threading.Thread(target=opener)
    t.start()
    assert entered.wait(5)
    res = a.abort_open()
    assert res["ok"] and 9001 in res["killed"] and proc.killed
    assert dt.killed == ["rec-777"] and len(dt.terminated) == 1
    monkeypatch.setenv("SCOUTER_CACHE", "/next/session/cache")  # the next open's
    release.set()
    t.join(5)
    assert not t.is_alive() and isinstance(box.get("err"), sess.SessionBlocked)
    assert len(dt.terminated) == 1  # the late thread never re-read the owner record
    assert dt.killed == ["rec-777", "rec-777"]  # ...but re-killed its own pid
    assert __import__("os").environ["SCOUTER_CACHE"] == "/next/session/cache"


def test_open_session_opens_the_body_of_the_case_the_runner_names(pinned_env, fake_corpus,
                                                                 monkeypatch):
    """N2: under --case / --only-failed the first case without a result is not
    necessarily the one the runner runs next."""
    monkeypatch.setattr(adapter_mod.corpus_mod, "sha256_originals", lambda: {})
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = _write_run(pinned_env, [_case("coverage", "olympus"), _case("coverage", "sony_a7")])
    a.session_factory = lambda corpus, root, body, run_root: sess.ScouterSession(
        corpus, root, body, dt=FakeDt(), run_root=run_root, mcp_factory=lambda e, c: FakeMcp())
    routed = a.open_session("coverage", run, next_case=_case("coverage", "sony_a7"))
    assert routed.current.body == "sony_a7"


def test_an_abort_before_the_agent_exists_still_reaches_it(pinned_env, fake_corpus):
    """N3: abort() landing after the product session opened but before the
    agent handle was assigned must still stop that agent's claude turns."""
    from gauntlet.adapter import Observation

    a, run, routed, _ = _routed(pinned_env, fake_corpus, layer="scenarios_agent")

    class Agent:
        def __init__(self):
            self.aborted = False
            self.stopped = False

        def stop_claude(self):
            self.stopped = True

        def run_case(self, case):
            box["aborted_at_run"] = self.aborted
            return Observation()

    box = {}

    def agent_factory(inner):
        routed.abort()  # the runner's hard cap lands here
        box["agent"] = Agent()
        return box["agent"]
    routed.agent_factory = agent_factory
    routed.run_case(_case("scenarios_agent"))
    assert box["aborted_at_run"] is True


# -- Fix B (night-1 triage rank 9, GD-job-not-staged) -----------------------


class _ImportingFakeMcp(FakeMcp):
    """A fake MCP whose `library_import` answers from the REAL staged card on
    disk (like the real registry would), close enough to test that
    `_stage_guide_job` (adapter.py) resolves `job_frames` from the import's
    own envelope, never the card copies themselves."""

    def call_tool(self, tool, args, timeout):
        if tool == "library_import":
            self.calls.append((tool, args))
            files = sorted(Path(args["source"]).rglob("*.ORF"))
            data = {"job_slug": args["job_slug"], "date": "2026-09-27",
                    "files": [{"source_name": f.name, "dest": f.name} for f in files]}
            return {"ok": True, "outcome": "ok", "data": data}
        return super().call_tool(tool, args, timeout)


def test_guide_session_open_stages_the_job_with_a_real_import(pinned_env, fake_corpus):
    """Fix B (night-1 triage rank 9, GD-job-not-staged): opening a guide
    session must import the staged card into the job slug the guide layer's
    {job_slug}/{job_frame:N} placeholders point at -- stage() only ever
    predicted the slug and copied card frames, it never ran library_import
    (unlike bulk's own run_bulk_case)."""
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    holder: dict = {}

    def factory(corpus, root, body, run_root):
        mcp = _ImportingFakeMcp()
        holder["mcp"] = mcp
        return sess.ScouterSession(corpus, root, body, dt=FakeDt(), run_root=run_root,
                                   mcp_factory=lambda env, cwd: mcp)

    routed = adapter_mod.RoutedSession(a, "guide", run, session_factory=factory)
    s = routed._session("olympus")

    assert "library_import" in [t for t, _ in holder["mcp"].calls]
    # The resolved job_frames are the import's own destinations (job_dir),
    # never the harness's own card copies under session.root / "card".
    assert s.staged.job_frames
    assert all(s.staged.card not in p.parents for p in s.staged.job_frames)
    assert all(p.name.endswith(".ORF") for p in s.staged.job_frames)


def test_guide_stage_is_a_noop_when_the_import_errors(pinned_env, fake_corpus):
    """A failed import must never crash session open -- a later guide step
    fails honestly on its own terms, exactly as it did before this fix."""
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    run = RunDir(pinned_env / "runs", "stamp").ensure()

    def factory(corpus, root, body, run_root):
        mcp = FakeMcp({"library_import": {"ok": False, "outcome": "error",
                                          "error": {"code": "usage"}, "data": {}}})
        return sess.ScouterSession(corpus, root, body, dt=FakeDt(), run_root=run_root,
                                   mcp_factory=lambda env, cwd: mcp)

    routed = adapter_mod.RoutedSession(a, "guide", run, session_factory=factory)
    s = routed._session("olympus")
    before = list(s.staged.job_frames)
    assert before  # the card copies stage() made are still there, untouched
    # Review round 2, finding 6 (GB-1): the failure is recorded, not silent.
    assert s.guide_job_stage_error == "usage"
