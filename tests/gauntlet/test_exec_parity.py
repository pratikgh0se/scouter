import json
import subprocess

import pytest

from gauntlet.adapter import Observation
from gauntlet.case import Case, Expect, Step
from tests.gauntlet import exec_parity
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.test_session import FakeMcp, _session


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def test_normalise_drops_volatile_fields():
    a = {"ok": True, "outcome": "ok", "ms": 12, "job": "j1", "data": {"k": 1, "path": "/a"}}
    b = {"ok": True, "outcome": "ok", "ms": 99, "job": "j2", "data": {"k": 1, "path": "/b"}}
    assert exec_parity.compare(a, b) == []
    c = dict(b, data={"k": 2})
    assert exec_parity.compare(a, c) == ["values"]


def test_cli_argv_follows_the_registry():
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": True})
    assert argv[:2] == ["darkroom", "set"] and "--no-commit" not in argv
    argv = exec_parity.cli_argv("darkroom.say", {"phrase": "brighter", "dry_run": True})
    assert "--dry-run" in argv and "brighter" in argv


def test_cli_argv_never_emits_the_nonexistent_commit_flag():
    # PAR-set-commit-flag (triage rank 10): `commit=True` (SMOKE_ARGS's own
    # darkroom.set happy/error args) used to map to `--commit`, a flag the CLI
    # never defines (only `--no-commit` exists, for a default-True commit) --
    # argparse rejected it, failing both P0 darkroom.set pairs. `commit=True`
    # is already the default, so no flag at all should be emitted for it.
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": True})
    assert "--commit" not in argv
    # commit=False must still emit --no-commit (unchanged behaviour).
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": False})
    assert "--no-commit" in argv


def test_cli_timeout_blocks_as_hang_without_raising(corpus, tmp_path, monkeypatch):
    s = _session(corpus, tmp_path).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: (_ for _ in ())
                        .throw(subprocess.TimeoutExpired(cmd="scouter", timeout=90.0)))
    case = Case(id="par-darkroom-say-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_say"})
    step = Step("parity_pair", {"verb": "darkroom.say", "args": {"phrase": "brighter"}}, 60.0,
               True)
    obs = Observation()
    rec = exec_parity.parity_pair(s, case, step, obs)
    assert rec.error == "timeout"
    assert obs.blocked == "hang: scouter cli exceeded budget"
    assert obs.hang_evidence and obs.extra["envelopes_match"] is False


def test_parity_both_failed_when_both_sides_answer_the_same_error(corpus, tmp_path, monkeypatch):
    # PAR-hollow-pass (triage rank 2): envelopes_match alone can't tell a
    # genuine pass from both sides refusing identically -- the verb never
    # ran either way. parity_pair must flag that fact for the evaluator.
    err = {"ok": False, "outcome": "error:usage",
          "error": {"code": "usage", "message": "bad"}}
    mcp = FakeMcp(answers={"vocab_list": err})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 2, stdout=json.dumps(err), stderr=""))
    case = Case(id="par-vocab-list-error", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {"__htc_unknown__": 1}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_both_failed"] is True


def test_parity_both_failed_is_false_when_both_sides_answer_ok(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"vocab_list": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-vocab-list-happy", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_both_failed"] is False


# ---- PAR-hollow-pass fixture staging (triage rank 4): the per-side copies
# must be imported, smoke-job/style smoke staged, and the frame switch must
# work on both sides, so the 18 fixture-missing pairs actually run the verb.


def _write_scouter_config(path, library_root):
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"version": 1, "library_root": str(library_root)}))


class _FakeStageSession:
    """A minimal fake for `_stage_side`/`_ensure_smoke_style`/
    `_call_with_frame_switch` in isolation -- no darktable, no MCP server,
    same pattern as tests/gauntlet/test_exec_bulk.py's FakeSession."""

    def __init__(self, tmp_path, *, library_import_envelope=None):
        from types import SimpleNamespace

        self.root = tmp_path / "run"
        self.calls = []
        self.relaunches = []
        self._library_import_envelope = library_import_envelope
        config_path = self.root / "config" / "config.json"
        _write_scouter_config(config_path, self.root / "jobs")
        self.staged = SimpleNamespace(config=config_path, env={})

    def call(self, tool, args, budget_s, write=False):
        from gauntlet.adapter import CallRecord

        self.calls.append((tool, args))
        if tool == "library_import" and self._library_import_envelope is not None:
            env = self._library_import_envelope
        else:
            env = {"ok": True, "outcome": "ok", "data": {}}
        return CallRecord(tool=tool, args=args, envelope=env, write=write)

    def relaunch(self, path=None):
        self.relaunches.append(path)


def test_stage_side_resolves_to_the_imported_destination(tmp_path):
    src = tmp_path / "parity" / "mcp" / "H.ORF"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"raw")
    envelope = {"ok": True, "data": {
        "job_slug": exec_parity.SMOKE_JOB_SLUG, "date": "2026-05-24",
        "files": [{"source_name": "H.ORF", "dest": "raw/2026-05-24_smoke-job_H.ORF"}]}}
    s = _FakeStageSession(tmp_path, library_import_envelope=envelope)
    resolved = exec_parity._stage_side(s, src)
    assert resolved != src
    assert resolved.name == "2026-05-24_smoke-job_H.ORF"
    assert ("library_import", {"source": str(src.parent),
                               "job_slug": exec_parity.SMOKE_JOB_SLUG}) in s.calls


def test_stage_side_falls_back_to_the_raw_copy_when_import_names_nothing(tmp_path):
    src = tmp_path / "parity" / "cli" / "H.ORF"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"raw")
    s = _FakeStageSession(tmp_path, library_import_envelope={"ok": False, "data": {}})
    resolved = exec_parity._stage_side(s, src)
    assert resolved == src


def test_ensure_smoke_style_runs_once_per_session(tmp_path):
    s = _FakeStageSession(tmp_path)
    path = tmp_path / "parity" / "mcp" / "H.ORF"
    exec_parity._ensure_smoke_style(s, path)
    exec_parity._ensure_smoke_style(s, path)
    style_calls = [c for c in s.calls if c[0] == "style_extract"]
    assert len(style_calls) == 1
    assert style_calls[0][1]["name"] == exec_parity.SMOKE_STYLE_NAME


def test_call_with_frame_switch_retries_once_after_relaunch(tmp_path):
    from gauntlet.adapter import CallRecord

    class _Session(_FakeStageSession):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self._n = 0

        def call(self, tool, args, budget_s, write=False):
            self._n += 1
            if self._n == 1:
                return CallRecord(tool=tool, args=args, error="not_in_darkroom", write=write)
            return CallRecord(tool=tool, args=args, envelope={"ok": True, "data": {}},
                             write=write)

    s = _Session(tmp_path)
    path = tmp_path / "parity" / "mcp" / "H.ORF"
    rec = exec_parity._call_with_frame_switch(s, "darkroom_open", {"image": str(path)}, path,
                                              30.0, False)
    assert s.relaunches == [path]
    assert rec.error is None and s.current_frame == path


def test_call_with_frame_switch_does_not_retry_other_errors(tmp_path):
    from gauntlet.adapter import CallRecord

    class _Session(_FakeStageSession):
        def call(self, tool, args, budget_s, write=False):
            self.calls.append((tool, args))
            return CallRecord(tool=tool, args=args, error="usage", write=write)

    s = _Session(tmp_path)
    path = tmp_path / "parity" / "mcp" / "H.ORF"
    rec = exec_parity._call_with_frame_switch(s, "darkroom_open", {"image": str(path)}, path,
                                              30.0, False)
    assert s.relaunches == []
    assert rec.error == "usage"


def test_parity_pair_imports_both_sides_for_a_non_open_verb(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    import_calls = [c for c in mcp.calls if c[0] == "library_import"]
    # One `library_import` per side (mcp copy + cli copy) -- the CLI side's
    # own import happens through the SAME MCP-backed session.call, since
    # `_stage_side` always goes through `session.call`.
    assert len(import_calls) == 2


def test_parity_pair_skips_staging_for_darkroom_open(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"darkroom_open": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-darkroom-open-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_open"})
    step = Step("parity_pair", {"verb": "darkroom.open", "args": {"image": "{frame:hero}"}},
               60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert not [c for c in mcp.calls if c[0] == "library_import"]
