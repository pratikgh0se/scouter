"""`run --workers N`: a case the adapter wants serial-first (the chart golden,
which writes the run-wide calibration) runs in the parent before the shards
spawn and never lands in a shard file."""

from gauntlet import workers
from gauntlet.cli import main
from gauntlet.tests.toy import ToyAdapter

FIRST = "alpha-0"


class FirstAdapter(ToyAdapter):
    def serial_first_ids(self, todo):
        return [FIRST] if any(c.id == FIRST for c in todo) and len(todo) > 1 else []


def make():
    return FirstAdapter()


def _args(tmp_path):
    return ["--adapter", "gauntlet.tests.test_serial_first:make", "--runs-root", str(tmp_path)]


def _patch(monkeypatch):
    seen = {}

    def fake_parallel(run, shards, argv_for, env_for, progress, **kw):
        seen["shards"] = [[c.id for c in s] for s in shards]
        seen["done_before"] = sorted(p.stem for p in (run.root / "cases").glob("*.json"))
        return 0

    monkeypatch.setattr(workers, "run_parallel", fake_parallel)
    return seen


def test_first_case_runs_before_shards_and_is_excluded(tmp_path, monkeypatch):
    seen = _patch(monkeypatch)
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--workers", "2"]) == 0
    assert FIRST in seen["done_before"]
    assert all(FIRST not in s for s in seen["shards"])
    assert sum(len(s) for s in seen["shards"]) > 0


def test_resume_with_first_case_done_does_not_rerun_it(tmp_path, monkeypatch):
    seen = _patch(monkeypatch)
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--case", FIRST]) == 0
    path = tmp_path / "s" / "cases" / f"{FIRST}.json"
    before = path.read_text()
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--resume"]) == 0
    assert path.read_text() == before
    assert all(FIRST not in s for s in seen["shards"])


def test_set_without_first_case_does_no_prerun(tmp_path, monkeypatch):
    seen = _patch(monkeypatch)
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert (
        main(_args(tmp_path) + ["run", "--stamp", "s", "--workers", "2", "--prefix", "beta-"]) == 0
    )
    assert seen["done_before"] == []
    assert sorted(i for s in seen["shards"] for i in s) == ["beta-0", "beta-1"]
