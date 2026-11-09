"""`gauntlet run --workers N`: sharding, worker env, the darktable-pid guard,
park propagation and shard merging. No darktable, no real subprocess: a fake
Popen runs the child's `main()` in-process, and process lists are fakes."""

from __future__ import annotations

import json
import os
import signal
import types
from pathlib import Path
from unittest import mock

import pytest

from gauntlet import workers
from gauntlet.case import Case, Expect, Step
from gauntlet.cli import EXIT_PARKED, main
from gauntlet.progress import Progress
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter
from scouter.core import dtprocess
from tests import host_support
from tests.gauntlet import adapter as adapter_mod
from tests.gauntlet.adapter import ScouterAdapter


def make_parking() -> ToyAdapter:
    return ToyAdapter({"alpha-0": "park"})


def make_ok() -> ToyAdapter:
    return ToyAdapter()


def _case(cid, layer, body=None, **meta):
    return Case(id=cid, layer=layer, tier="P1", mode="tool", steps=[Step("set")],
                expect=Expect("edit"), body=body, meta=meta)


def _cases():
    out = [_case(f"a-oly-{i}", "a", "olympus") for i in range(7)]
    out += [_case(f"a-son-{i}", "a", "sony") for i in range(3)]
    out += [_case(f"w-{i}", "w", "olympus", session="walkthrough", order=i) for i in range(4)]
    out += [_case(f"inst-{i}", "install") for i in range(5)]
    return out


# -- sharding ---------------------------------------------------------------


@pytest.mark.parametrize("n", [2, 3, 4])
def test_partition_deterministic_disjoint_complete(n):
    cases = _cases()
    kw = dict(max_cases=3, atomic_layers=frozenset({"install"}))
    shards = workers.partition(cases, n, **kw)
    assert shards == workers.partition(list(cases), n, **kw)
    ids = [c.id for s in shards for c in s]
    assert sorted(ids) == sorted(c.id for c in cases) and len(ids) == len(set(ids))


def test_partition_never_splits_a_session():
    kw = dict(max_cases=3, atomic_layers=frozenset({"install"}))
    shards = workers.partition(_cases(), 3, **kw)
    where = {c.id: k for k, s in enumerate(shards) for c in s}
    assert len({where[f"w-{i}"] for i in range(4)}) == 1  # walkthrough
    assert len({where[f"inst-{i}"] for i in range(5)}) == 1  # atomic layer
    units = workers.session_units(_cases(), **kw)
    for u in units:  # every unit lands whole in one shard
        assert len({where[c.id] for c in u}) == 1
    # olympus (7) is cut at max_cases=3 into 3+3+1, never longer than a session
    assert max(len(u) for u in units if u[0].layer == "a") == 3


def test_partition_balance_within_one_session():
    kw = dict(max_cases=3, atomic_layers=frozenset({"install"}))
    sizes = [len(s) for s in workers.partition(_cases(), 2, **kw)]
    biggest = max(len(u) for u in workers.session_units(_cases(), **kw))
    assert max(sizes) - min(sizes) <= biggest


def test_partition_keeps_selection_order_and_handles_few_cases():
    cases = _cases()[:2]
    shards = workers.partition(cases, 4, max_cases=50)
    assert [c.id for s in shards for c in s if s] and sum(map(len, shards)) == 2
    for s in shards:
        assert [c.id for c in s] == [c.id for c in cases if c in s]


def test_session_numbers_never_collide_between_shards():
    seen = set()
    for k in range(3):
        n = 5
        for _ in range(4):
            n = workers.next_session_n(n, (k, 3))
            assert n % 3 == k
            seen.add((k, n))
    assert len({n for _, n in seen}) == len(seen)
    assert workers.next_session_n(5, None) == 6  # N = 1: plain +1
    assert workers.owns_session_dir("007-olympus", (1, 3))
    assert not workers.owns_session_dir("008-olympus", (1, 3))


# -- worker env ---------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    dt = home / ".config" / "darktable"
    (dt / "lua").mkdir(parents=True)
    (dt / "lua" / "darktable_mcp.lua").write_text("-- plugin\n")
    (dt / "luarc").write_text('require "darktable_mcp"\n')
    monkeypatch.setenv("HOME", str(home))
    return home


def test_worker_env_distinct_cache_configdir_and_plugin_seeded(tmp_path, fake_home):
    runs = tmp_path / "runs"
    run = RunDir(runs, "s").ensure()
    adapter = ScouterAdapter(runs_root=runs)
    envs = [workers.worker_env(run, k, 2, adapter, base={"PATH": "/bin"}) for k in (0, 1)]
    assert envs[0]["XDG_CACHE_HOME"] != envs[1]["XDG_CACHE_HOME"]
    assert envs[0]["GAUNTLET_DT_CONFIGDIR"] != envs[1]["GAUNTLET_DT_CONFIGDIR"]
    assert [e[workers.SHARD_ENV] for e in envs] == ["0/2", "1/2"]
    assert envs[0][workers.REGISTRY_ENV] == envs[1][workers.REGISTRY_ENV]
    for e in envs:
        cfg = Path(e["GAUNTLET_DT_CONFIGDIR"])
        assert (cfg / "luarc").read_text() == 'require "darktable_mcp"\n'
        assert (cfg / "lua" / "darktable_mcp.lua").read_text() == "-- plugin\n"
        assert Path(e["XDG_CACHE_HOME"]).is_relative_to(run.root / "workers")
    # the user's own config is only read
    assert sorted(p.name for p in (fake_home / ".config" / "darktable").iterdir()) == \
        ["lua", "luarc"]


def test_worker_env_asserts_isolated_library(tmp_path, fake_home):
    adapter = ScouterAdapter(runs_root=tmp_path / "runs")
    with mock.patch.object(host_support, "assert_isolated_library",
                           side_effect=ValueError("not isolated")) as chk:
        with pytest.raises(ValueError):
            adapter.worker_env(tmp_path / "runs" / "s" / "workers" / "w0")
    assert chk.call_args.args[0].name == "library.db"
    # and the real check refuses Pratik's library and anything outside scratch
    with pytest.raises(ValueError):
        host_support.assert_isolated_library(host_support.REAL_LIBRARY,
                                             scratch=host_support.REAL_LIBRARY.parent)
    with pytest.raises(ValueError):
        host_support.assert_isolated_library(tmp_path / "x" / "library.db",
                                             scratch=tmp_path / "runs")


def test_seed_worker_config_without_a_plugin_is_harmless(tmp_path):
    adapter_mod.seed_worker_config(tmp_path / "none", tmp_path / "cfg")
    assert (tmp_path / "cfg").is_dir() and not list((tmp_path / "cfg").iterdir())


def test_adapter_process_dir_is_per_shard(tmp_path, monkeypatch):
    a = ScouterAdapter(runs_root=tmp_path)
    monkeypatch.delenv(workers.SHARD_ENV, raising=False)
    assert a.process_dir == tmp_path / "_process"
    monkeypatch.setenv(workers.SHARD_ENV, "1/2")
    assert a.process_dir == tmp_path / "_process-w1"


def test_adopt_leftover_owner_ignores_sibling_sessions(tmp_path, monkeypatch):
    run = RunDir(tmp_path, "s").ensure()
    for n in (1, 2):
        cache = run.sessions_dir / "bulk" / f"{n:03d}-olympus" / "cache"
        cache.mkdir(parents=True)
        (cache / dtprocess.OWNER_FILE).write_text("{}")
    got = []

    def owned():  # "live" only once a record was copied in
        got.append(1)
        return 1 if (tmp_path / "pc" / dtprocess.OWNER_FILE).exists() else None

    monkeypatch.setenv("SCOUTER_CACHE", str(tmp_path / "pc"))
    rec = adapter_mod.adopt_leftover_owner(run, tmp_path / "pc", owned_pid=owned, shard=(0, 2))
    assert rec is not None and rec.parent.parent.name == "002-olympus"
    (tmp_path / "pc" / dtprocess.OWNER_FILE).unlink()
    # a worker owning neither number adopts nothing
    assert adapter_mod.adopt_leftover_owner(run, tmp_path / "pc", owned_pid=owned,
                                            shard=(0, 3)) is None


# -- the guard ------------------------------------------------------------------


def test_unregistered_darktable_blocks():
    assert host_support._darktable_is_running(pids=lambda: [100], registered=lambda: set())


def test_registered_harness_pids_are_allowed():
    assert not host_support._darktable_is_running(pids=lambda: [100, 200],
                                                  registered=lambda: {100, 200})
    assert not host_support._darktable_is_running(pids=lambda: [], registered=lambda: set())


def test_one_unregistered_pid_among_registered_still_blocks():
    """Pratik's own darktable running beside two workers."""
    assert host_support._darktable_is_running(pids=lambda: [100, 200, 999],
                                              registered=lambda: {100, 200})


def test_assert_no_concurrent_blocks_with_clear_message(tmp_path):
    with pytest.raises(RuntimeError, match="another darktable is running"):
        host_support.assert_no_concurrent_darktable(
            cache_dir=tmp_path, darktable_running=lambda: host_support._darktable_is_running(
                pids=lambda: [999], registered=lambda: {100}))


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = tmp_path / "pids"
    reg.mkdir()
    monkeypatch.setenv(dtprocess.REGISTRY_ENV, str(reg))
    monkeypatch.setattr(dtprocess, "_alive", lambda pid: True)
    monkeypatch.setattr(dtprocess, "_lstart", lambda pid: f"start-{pid}")
    return reg


def test_registry_only_trusts_matching_start_time(registry, monkeypatch):
    (registry / "100.json").write_text(json.dumps({"pid": 100, "start_time": "start-100"}))
    (registry / "200.json").write_text(json.dumps({"pid": 200, "start_time": "stale"}))
    (registry / "junk.json").write_text("nope")
    assert dtprocess.registered_pids() == {100}


def test_guard_allows_registered_blocks_unregistered(registry, monkeypatch):
    (registry / "100.json").write_text(json.dumps({"pid": 100, "start_time": "start-100"}))
    monkeypatch.setattr(dtprocess, "running_pids", lambda: [100, 999])
    monkeypatch.setattr(dtprocess, "owned_pid", lambda cache_root=None: None)
    assert dtprocess.unregistered_foreign_pids() == [999]
    monkeypatch.setattr(dtprocess, "running_pids", lambda: [100])
    assert dtprocess.unregistered_foreign_pids() == []
    monkeypatch.delenv(dtprocess.REGISTRY_ENV)  # no registry: everything foreign blocks
    assert dtprocess.unregistered_foreign_pids() == [100]


def test_launch_registers_its_pid(registry, tmp_path, monkeypatch):
    proc = mock.Mock(pid=4242)
    monkeypatch.setattr(dtprocess.subprocess, "Popen", lambda *a, **k: proc)
    env = {"SCOUTER_CACHE": str(tmp_path / "cache"), dtprocess.REGISTRY_ENV: str(registry)}
    rec = dtprocess.launch(None, tmp_path / "lib" / "library.db", tmp_path / "cfg", env=env)
    assert json.loads((registry / "4242.json").read_text())["pid"] == rec.pid
    assert dtprocess.registered_pids() == {4242}
    argv = None  # the configdir reached darktable's argv

    def popen(a, **k):
        nonlocal argv
        argv = a
        return proc

    monkeypatch.setattr(dtprocess.subprocess, "Popen", popen)
    dtprocess.launch(None, tmp_path / "lib" / "library.db", tmp_path / "cfg", env=env)
    assert argv[argv.index("--configdir") + 1] == str(tmp_path / "cfg")


def test_quit_ladder_still_skips_osascript_beside_a_registered_sibling(registry, monkeypatch):
    """osascript quits by app name, so a sibling worker's darktable (registered)
    must still stop the ladder from using it; signals reach only our own pid."""
    (registry / "999.json").write_text(json.dumps({"pid": 999, "start_time": "start-999"}))
    monkeypatch.setattr(dtprocess, "owned_pid", lambda cache_root=None: 4242)
    monkeypatch.setattr(dtprocess, "running_pids", lambda: [4242, 999])

    def no_osascript(*a, **k):
        raise AssertionError("osascript must not run")

    monkeypatch.setattr(dtprocess.subprocess, "run", no_osascript)
    sent, state = [], {"alive": True}

    def kill(p, s):
        sent.append((p, s))
        state["alive"] = False

    res = dtprocess.terminate(kill=kill, alive=lambda p: state["alive"], sleep=lambda s: None)
    assert res["ok"] and sent == [(4242, signal.SIGTERM)]
    assert dtprocess.terminate(pid=999)["how"] == "refused_not_ours"


# -- park propagation and merging -------------------------------------------------


class FakeProc:
    def __init__(self, codes, stop, on_stop=0):
        self.codes, self.stop, self.on_stop = list(codes), stop, on_stop

    def poll(self):
        if self.codes:
            return self.codes.pop(0)
        return self.on_stop if self.stop.exists() else None


def _shards(n=2):
    cs = [_case(f"c-{i}", "a", "olympus") for i in range(4)]
    return [cs[: len(cs) // 2], cs[len(cs) // 2:]][:n]


class RecordingProgress:
    def __init__(self):
        self.merged = []

    def merge_shard(self, doc):
        self.merged.append(doc)


def test_park_in_one_worker_stops_the_other_politely_and_exits_75(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    stop = workers.stop_path(run)
    procs = {}

    def popen(argv, env):
        k = argv[0]
        d = workers.shard_dir(run, k)
        (d / "progress.json").write_text(json.dumps({"layers": {}, "parked": [f"p{k}"]}))
        procs[k] = FakeProc([75] if k == 0 else [], stop)
        return procs[k]

    prog = RecordingProgress()
    code = workers.run_parallel(run, _shards(), lambda k, n: [k], lambda k: {}, prog,
                                popen=popen, sleep=lambda s: None)
    assert code == EXIT_PARKED
    assert stop.exists()  # the sibling was asked to stop, never signalled
    assert [d["parked"] for d in prog.merged] == [["p0"], ["p1"]]


def test_all_workers_clean_exits_zero_and_refusal_wins(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    ok = workers.run_parallel(run, _shards(), lambda k, n: [k], lambda k: {},
                              RecordingProgress(), sleep=lambda s: None,
                              popen=lambda a, env: FakeProc([0], workers.stop_path(run)))
    assert ok == 0
    refused = workers.run_parallel(
        run, _shards(), lambda k, n: [k], lambda k: {}, RecordingProgress(),
        sleep=lambda s: None,
        popen=lambda a, env: FakeProc([3 if a[0] == 1 else 75], workers.stop_path(run)))
    assert refused == 3


def test_empty_shards_are_not_spawned(tmp_path):
    run = RunDir(tmp_path, "s").ensure()
    spawned = []
    workers.run_parallel(run, [_shards()[0], []], lambda k, n: [k], lambda k: {},
                         RecordingProgress(), sleep=lambda s: None,
                         popen=lambda a, env: spawned.append(a[0]) or
                         FakeProc([0], workers.stop_path(run)))
    assert spawned == [0]


def test_runner_stops_between_cases_when_asked(tmp_path):
    adapter = ToyAdapter(n=4)
    run = RunDir(tmp_path, "s")
    from gauntlet.generate import write_generation

    write_generation(run, adapter)
    stop_after = {"n": 0}
    runner = Runner(adapter, run, should_stop=lambda: stop_after["n"] >= 2)
    orig = adapter.open_session

    def counting(layer, run_root):
        sess = orig(layer, run_root)
        real = sess.run_case

        def run_case(c):
            stop_after["n"] += 1
            return real(c)

        sess.run_case = run_case
        return sess

    adapter.open_session = counting
    summary = runner.run_layers()
    assert summary.ran == 2 and summary.stopped_early and summary.status == "parked"
    assert not summary.parked  # nothing was parked by a case; resume picks the rest up


def test_merge_of_shard_progress_and_logs_equals_a_single_run(tmp_path):
    from gauntlet.generate import write_generation

    base = RunDir(tmp_path / "single", "s")
    write_generation(base, ToyAdapter())
    Runner(ToyAdapter(), base).run_layers()
    single = json.loads(base.progress_json.read_text())

    run = RunDir(tmp_path / "par", "s")
    write_generation(run, ToyAdapter())
    cases = run.read_cases()
    shards = workers.partition(cases, 2, max_cases=50)
    workers.prepare_shard_files(run, shards)
    for k, shard in enumerate(shards):
        child = RunDir(tmp_path / "par", "s")
        d = workers.shard_dir(run, k)
        child.progress_json, child.log_md = d / "progress.json", d / "log.md"
        Runner(ToyAdapter(), child).run_layers(None, resume=False,
                                               case_ids={c.id for c in shard})
    parent = Runner(ToyAdapter(), run)
    for spec in ToyAdapter().layers():
        mine = [c for c in cases if c.layer == spec.name]
        parent.progress.layer(spec.name, len(mine), sum(c.tier == "P0" for c in mine), spec.live)
    workers.merge_shard_outputs(run, parent.progress, [0, 1])
    parent.progress.finish("done")
    merged = json.loads(run.progress_json.read_text())
    for name, lay in single["layers"].items():
        for k in ("done", "pass", "partial", "fail", "points", "p0_total", "p0_pass", "total"):
            assert merged["layers"][name][k] == lay[k], (name, k)
        assert merged["layers"][name]["status"] == "done"
    log = run.log_md.read_text()
    assert "## Worker 0" in log and "## Worker 1" in log
    assert len(run.done_ids()) == len(cases)
    assert not (workers.shard_dir(run, 0) / "progress.json").exists()


# -- the CLI, end to end through an in-process fake Popen ----------------------------


class InProcessPopen:
    """Stands in for subprocess.Popen: runs the child's main() right away."""

    spawned: list = []

    def __init__(self, argv, env):
        self.argv = argv
        type(self).spawned.append((argv, env))
        i = argv.index("-m") + 2  # python -m gauntlet <args...>
        with mock.patch.dict(os.environ, env):
            self.rc = main(argv[i:])

    def poll(self):
        return self.rc


def _cli(tmp_path, factory="tests.gauntlet.test_parallel_workers:make_ok"):
    return ["--adapter", factory, "--runs-root", str(tmp_path)]


@pytest.fixture
def inproc(monkeypatch):
    InProcessPopen.spawned = []
    monkeypatch.setattr(workers, "subprocess", types.SimpleNamespace(Popen=InProcessPopen))
    return InProcessPopen


def test_cli_workers_runs_everything_and_report_works_unchanged(tmp_path, inproc):
    main(_cli(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "2"]) == 0
    run = RunDir(tmp_path, "s")
    assert len(run.done_ids()) == 6
    envs = [e for _, e in inproc.spawned]
    assert len({e["XDG_CACHE_HOME"] for e in envs}) == 2
    prog = json.loads(run.progress_json.read_text())
    assert prog["layers"]["alpha"]["done"] == 4 and prog["layers"]["beta"]["done"] == 2
    assert prog["status"] == "done"
    assert main(_cli(tmp_path) + ["report", "--stamp", "s"]) == 0


def test_cli_park_exits_75_and_resume_finishes(tmp_path, inproc):
    park = _cli(tmp_path, "tests.gauntlet.test_parallel_workers:make_parking")
    main(park + ["generate", "--stamp", "s"])
    assert main(park + ["run", "--stamp", "s", "--workers", "2"]) == EXIT_PARKED
    run = RunDir(tmp_path, "s")
    assert 0 < len(run.done_ids()) < 6
    assert json.loads(run.progress_json.read_text())["status"] == "parked"
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--resume"]) == 0
    assert len(run.done_ids()) == 6
    prog = json.loads(run.progress_json.read_text())
    assert prog["layers"]["alpha"]["done"] == 4 and prog["layers"]["beta"]["done"] == 2


def test_cli_filters_compose_with_workers(tmp_path, inproc):
    main(_cli(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--tier", "P0"]) == 0
    assert sorted(RunDir(tmp_path, "s").done_ids()) == ["alpha-0", "beta-0"]
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--prefix",
                                  "beta-", "--rerun"]) == 0
    assert sorted(RunDir(tmp_path, "s").done_ids()) == ["alpha-0", "beta-0", "beta-1"]
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--case",
                                  "alpha-3"]) == 0
    assert "alpha-3" in RunDir(tmp_path, "s").done_ids()


def test_cli_nothing_to_run_spawns_nothing(tmp_path, inproc):
    main(_cli(tmp_path) + ["generate", "--stamp", "s"])
    main(_cli(tmp_path) + ["run", "--stamp", "s"])
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--resume"]) == 0
    assert inproc.spawned == []


def test_workers_one_is_the_unchanged_single_process_path(tmp_path, inproc):
    main(_cli(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "1"]) == 0
    assert inproc.spawned == []
    run = RunDir(tmp_path, "s")
    assert len(run.done_ids()) == 6 and not (run.root / "workers").exists()
    with pytest.raises(SystemExit):
        main(_cli(tmp_path) + ["run", "--stamp", "s", "--workers", "0"])
