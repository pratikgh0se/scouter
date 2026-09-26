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
    cases, counts = generate_all(a)
    got = {c.spec.name: c.got for c in counts}
    assert got == {"offline": 2314, "coverage": 1316, "scenarios": 701, "scenarios_agent": 160,
                   "bulk": 40, "looks": 469, "robustness": 32, "parity": 90, "install": 10,
                   "guide": 35}
    assert sum(got.values()) == 5167


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
