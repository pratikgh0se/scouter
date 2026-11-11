"""t_90e75c20 round 2: the real bulk layer's disk footprint end to end --
ScouterSession staging its per-case library imports under the run's own
case workdir (session.py:point_library_root, exec_bulk.py:run_bulk_case),
ScouterAdapter.retain_keep's score-only bulk policy, and the generic engine
retention (gauntlet.runner.Runner._retain / gauntlet.store.RunDir.retain)
that already prunes it. No darktable, no real gauntlet run: the darktable
seam (FakeDt) and the MCP transport seam (a fake mcp client) that
tests/gauntlet/test_session.py and test_adapter.py already use for every
session/adapter test stand in for the product boundary; the disk-heavy
`library_import` write itself goes through the REAL `scouter.config.load`/
`scouter.config.job_dir` (pure stdlib path math, no darktable) so the bytes
retention prunes are real, not simulated."""

from __future__ import annotations

from pathlib import Path

import pytest

import scouter.config as scouter_config
from gauntlet.case import Case, Expect, Step
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from tests.gauntlet import session as sess
from tests.gauntlet.adapter import ScouterAdapter
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeDt


@pytest.fixture(autouse=True)
def _no_real_wb_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("HTC_WB_TABLES", str(tmp_path / "no-wb-tables"))


@pytest.fixture
def pinned_env(tmp_path, monkeypatch):
    for k in (
        "SCOUTER_CONFIG",
        "SCOUTER_CACHE",
        "OBSIDIAN_VAULT_PATH",
        "SCOUTER_DARKTABLE_LIBRARY_DB",
    ):
        monkeypatch.setenv(k, str(tmp_path / "pinned" / k.lower()))
    return tmp_path


@pytest.fixture
def fake_corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {"olympus": {"hero": "H.ORF"}}}
    return Corpus(doc, trial)


class DiskFakeMcp:
    """The MCP transport seam (stands in for the darktable-mcp bridge,
    exactly like FakeDt stands in for darktable itself): `library_import`
    does the SAME real file write a real import would -- via the REAL
    `scouter.config.load`/`job_dir` (pure path math, no darktable) -- so
    retention has real bytes on disk to prune. Every other tool answers a
    plain ok envelope, matching test_session.py's FakeMcp default."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.calls: list[tuple[str, dict]] = []
        self.job_paths: list[Path] = []
        self.bytes_written = 0

    def call_tool(self, tool: str, args: dict, timeout: float) -> dict:
        self.calls.append((tool, dict(args)))
        if tool == "library_import":
            cfg = scouter_config.load(str(self.config_path))
            job_path = scouter_config.job_dir(cfg, "2026-09-29", args["job_slug"])
            job_path.mkdir(parents=True, exist_ok=True)
            self.job_paths.append(job_path)
            files = sorted(p for p in Path(args["source"]).rglob("*") if p.is_file())
            written = []
            for f in files:
                payload = f.read_bytes() * 4096  # a few KB per source file, real bytes
                (job_path / f.name).write_bytes(payload)
                self.bytes_written += len(payload)
                written.append({"source_name": f.name, "dest": f.name})
            return {
                "ok": True,
                "outcome": "ok",
                "data": {
                    "job_slug": args["job_slug"],
                    "date": "2026-09-29",
                    "files": written,
                    "imported": len(written),
                },
            }
        return {"ok": True, "outcome": "ok", "data": {}}

    def close(self) -> None:
        pass


def _bulk_case(case_id: str) -> Case:
    return Case(
        id=case_id,
        layer="bulk",
        tier="P1",
        mode="tool",
        steps=[
            Step(
                "library_import",
                {"source": "{card}", "job_slug": "{job_slug}", "confirm": True},
                6.0,
                True,
            )
        ],
        expect=Expect("workflow", []),
        body="olympus",
        frame_role="card",
        meta={"frames": 2},
    )


def _routed_adapter(pinned_env, fake_corpus, run):
    a = ScouterAdapter(pinned_env / "runs")
    a._corpus = fake_corpus
    mcps: list[DiskFakeMcp] = []

    def factory(corpus, root, body, run_root):
        holder: dict = {}

        def mcp_factory(env, cwd):
            mcp = DiskFakeMcp(Path(env["SCOUTER_CONFIG"]))
            holder["mcp"] = mcp
            mcps.append(mcp)
            return mcp

        s = sess.ScouterSession(
            corpus, root, body, dt=FakeDt(), run_root=run_root, mcp_factory=mcp_factory
        )
        return s

    a.session_factory = factory
    return a, mcps


def test_a_scored_bulk_case_holds_only_kept_files_after_retention(pinned_env, fake_corpus):
    """bulk = score-only (ScouterAdapter.retain_keep): nothing survives a
    bulk case's own workdir once it is scored."""
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    a, mcps = _routed_adapter(pinned_env, fake_corpus, run)
    case1 = _bulk_case("blk-import-and-cull-olympus-card")
    run.write_cases([case1])

    Runner(a, run).run_layers(layers=["bulk"])

    assert run.read_result(case1.id) is not None  # the verdict itself is untouched
    assert mcps and mcps[0].calls  # the real import call actually ran
    assert mcps[0].bytes_written > 0  # real bytes were written somewhere
    job_path = mcps[0].job_paths[0]
    assert not job_path.exists()  # ... and are gone once the case is scored
    work = run.case_workdir(case1.id)
    assert not work.exists() or list(work.rglob("*")) == []


def test_a_second_bulk_case_in_the_same_body_never_sees_the_firsts_import(pinned_env, fake_corpus):
    """Two bulk cases of the same body share ONE session (RoutedSession
    reuses it -- no relaunch between them); each still gets its own
    library_root, so the second case's import can never land in, or read
    back, the first case's job directory."""
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    a, mcps = _routed_adapter(pinned_env, fake_corpus, run)
    case1 = _bulk_case("blk-import-and-cull-olympus-card")
    case2 = _bulk_case("blk-import-and-cull-olympus-job")
    run.write_cases([case1, case2])

    Runner(a, run).run_layers(layers=["bulk"])

    assert run.read_result(case1.id) is not None and run.read_result(case2.id) is not None
    mcp = mcps[0]  # one shared session -> one mcp for both cases
    assert len(mcp.job_paths) == 2
    job_path_1, job_path_2 = mcp.job_paths
    assert job_path_1 != job_path_2
    work1, work2 = run.case_workdir(case1.id), run.case_workdir(case2.id)
    assert job_path_1.is_relative_to(work1) and not job_path_1.is_relative_to(work2)
    assert job_path_2.is_relative_to(work2) and not job_path_2.is_relative_to(work1)
    # by the time case2's import ran, case1's own job dir was already pruned
    assert not job_path_1.exists()


def test_z2_a_tiny_runner_run_sums_bytes_before_and_after_retention(pinned_env, fake_corpus):
    """Z2 second oracle: a real tiny run of the runner over the Scouter
    adapter with the fake seam, in a temp dir -- summing real bytes written
    against `RunDir.du()`'s own post-retention count for the bulk layer."""
    run = RunDir(pinned_env / "runs", "stamp").ensure()
    a, mcps = _routed_adapter(pinned_env, fake_corpus, run)
    cases = [
        _bulk_case("blk-import-and-cull-olympus-card"),
        _bulk_case("blk-import-and-cull-olympus-job"),
    ]
    run.write_cases(cases)

    summary = Runner(a, run).run_layers(layers=["bulk"])

    assert summary.ran == 2
    written = sum(m.bytes_written for m in mcps)
    verdicts = sum(run.case_path(c.id).stat().st_size for c in cases)
    after = run.du().get("bulk", 0)
    assert written > 0
    # score-only: every byte retention could reach (the real library import
    # writes) is gone; du()'s own leftover is exactly the two verdict jsons
    # retention structurally can never touch.
    assert after == verdicts
    assert after < written
