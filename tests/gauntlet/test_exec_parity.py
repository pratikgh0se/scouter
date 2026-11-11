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
                meta={"mcp": "darkroom_say", "variant": "happy"})
    step = Step("parity_pair", {"verb": "darkroom.say", "args": {"phrase": "brighter"}}, 60.0,
               True)
    obs = Observation()
    rec = exec_parity.parity_pair(s, case, step, obs)
    assert rec.error == "timeout"
    assert obs.blocked == "hang: scouter cli exceeded budget"
    assert obs.hang_evidence and obs.extra["envelopes_match"] is False


# ---- D-1 (review fix round 2, Pratik's ruling row 496 item 5): a HAPPY
# variant where both sides fail the same way is needs_review; an ERROR
# variant is a genuine pass when both sides refuse the SAME typed way, and
# needs_review only when a side accepted the bad input (or the shared code
# is transient).


def test_happy_variant_both_sides_error_same_way_needs_review(corpus, tmp_path, monkeypatch):
    err = {"ok": False, "outcome": "error:not_in_darkroom",
          "error": {"code": "not_in_darkroom", "message": "bad"}}
    mcp = FakeMcp(answers={"vocab_list": err})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 6, stdout=json.dumps(err), stderr=""))
    case = Case(id="par-vocab-list-happy", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list", "variant": "happy"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_needs_review"] == \
        "happy variant: both sides failed the same way -- the verb never ran"


def test_error_variant_both_sides_refuse_the_same_typed_way_is_verified(corpus, tmp_path,
                                                                        monkeypatch):
    # This is the error variant's whole point: the harness deliberately
    # breaks the call so both sides refuse identically. That is a genuine
    # pass, never needs_review.
    err = {"ok": False, "outcome": "error:usage", "error": {"code": "usage", "message": "bad"}}
    mcp = FakeMcp(answers={"vocab_list": err})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 2, stdout=json.dumps(err), stderr=""))
    case = Case(id="par-vocab-list-error", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list", "variant": "error"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {"__htc_unknown__": 1}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_needs_review"] is None


def test_error_variant_transient_shared_code_needs_review(corpus, tmp_path, monkeypatch):
    err = {"ok": False, "outcome": "error:bridge_timeout",
          "error": {"code": "bridge_timeout", "message": "no answer"}}
    mcp = FakeMcp(answers={"vocab_list": err})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 3, stdout=json.dumps(err), stderr=""))
    case = Case(id="par-vocab-list-error", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list", "variant": "error"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {"__htc_unknown__": 1}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["parity_needs_review"] == \
        "error variant: the shared code 'bridge_timeout' is transient, not a typed refusal"


def test_error_variant_accepted_by_a_side_needs_review(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    err = {"ok": False, "outcome": "error:usage", "error": {"code": "usage", "message": "bad"}}
    mcp = FakeMcp(answers={"vocab_list": ok})  # MCP wrongly accepted the bad input
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 2, stdout=json.dumps(err), stderr=""))
    case = Case(id="par-vocab-list-error", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list", "variant": "error"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {"__htc_unknown__": 1}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    # envelopes differ (ok vs error), so this is already scored a fail by
    # envelopes_match -- parity_needs_review is not reached (same=False).
    assert obs.extra["envelopes_match"] is False
    assert obs.extra["parity_needs_review"] is None


def test_error_variant_both_sides_wrongly_accept_needs_review(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"vocab_list": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-vocab-list-error", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list", "variant": "error"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {"__htc_unknown__": 1}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_needs_review"] == \
        "error variant: a side accepted the bad input instead of refusing it"


def test_happy_variant_both_sides_ok_is_still_pass(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"vocab_list": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-vocab-list-happy", layer="parity", tier="P1", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "vocab_list", "variant": "happy"})
    step = Step("parity_pair", {"verb": "vocab.list", "args": {}}, 60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["envelopes_match"] is True
    assert obs.extra["parity_needs_review"] is None


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
        self.staged = SimpleNamespace(config=config_path, env={},
                                      frames={"hero": tmp_path / "trial" / "H.ORF"})

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
    src = tmp_path / "parity" / "mcp" / "H-mcp.ORF"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"raw")
    envelope = {"ok": True, "data": {
        "job_slug": exec_parity.SMOKE_JOB_SLUG, "date": "2026-05-24",
        "files": [{"source_name": "H-mcp.ORF", "dest": "raw/2026-05-24_smoke-job_H-mcp.ORF"}]}}
    s = _FakeStageSession(tmp_path, library_import_envelope=envelope)
    resolved = exec_parity._stage_side(s, src, "mcp")
    assert resolved != src
    assert resolved.name == "2026-05-24_smoke-job_H-mcp.ORF"
    # C17: an unconfirmed import is a dry-run plan that copies nothing
    assert ("library_import", {"source": str(src.parent),
                               "job_slug": exec_parity.SMOKE_JOB_SLUG,
                               "confirm": True}) in s.calls
    assert exec_parity._fixture_errors(s) == []


def test_stage_side_names_a_dry_run_import_as_a_fixture_error(tmp_path):
    src = tmp_path / "parity" / "cli" / "H-cli.ORF"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"raw")
    envelope = {"ok": True, "outcome": "dry_run", "data": {"plan": [{"source_name": "H-cli.ORF"}]}}
    s = _FakeStageSession(tmp_path, library_import_envelope=envelope)
    assert exec_parity._stage_side(s, src, "cli") == src
    assert exec_parity._fixture_errors(s) == ["cli_import_failed: import_was_dry_run"]


def test_stage_side_falls_back_and_records_the_failure(tmp_path):
    # A-4/A-5 (finding 13): a failed import falls back to the raw copy AND
    # is recorded, never silent.
    src = tmp_path / "parity" / "cli" / "H-cli.ORF"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"raw")
    s = _FakeStageSession(tmp_path, library_import_envelope={"ok": False, "data": {}})
    resolved = exec_parity._stage_side(s, src, "cli")
    assert resolved == src
    assert exec_parity._fixture_errors(s) == ["cli_import_failed: no_files_named"]


def test_ensure_smoke_style_runs_once_per_session_on_success(tmp_path):
    s = _FakeStageSession(tmp_path)
    path = tmp_path / "parity" / "mcp" / "H-mcp.ORF"
    exec_parity._ensure_smoke_style(s, path)
    exec_parity._ensure_smoke_style(s, path)
    style_calls = [c for c in s.calls if c[0] == "style_extract"]
    assert len(style_calls) == 1
    assert style_calls[0][1]["name"] == exec_parity.SMOKE_STYLE_NAME
    assert style_calls[0][1]["confirm"] is True
    assert exec_parity._fixture_errors(s) == []


def test_ensure_smoke_style_failure_is_retried_and_recorded(tmp_path):
    # A-4 (finding 13): a FAILED style_extract must not set the done flag
    # (so it is retried next case) and must be recorded, never silently
    # swallowed.
    class _Session(_FakeStageSession):
        def call(self, tool, args, budget_s, write=False):
            from gauntlet.adapter import CallRecord

            self.calls.append((tool, args))
            return CallRecord(tool=tool, args=args,
                             envelope={"ok": False, "error": {"code": "no_sidecar"}}, write=write)

    s = _Session(tmp_path)
    path = tmp_path / "parity" / "mcp" / "H-mcp.ORF"
    exec_parity._ensure_smoke_style(s, path)
    exec_parity._ensure_smoke_style(s, path)
    style_calls = [c for c in s.calls if c[0] == "style_extract"]
    assert len(style_calls) == 2  # retried, since it never succeeded
    assert exec_parity._fixture_errors(s) == \
        ["smoke_style_failed: no_sidecar", "smoke_style_failed: no_sidecar"]


def test_call_with_frame_switch_retries_once_and_records_it(tmp_path):
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
    path = tmp_path / "parity" / "mcp" / "H-mcp.ORF"
    obs = Observation()
    rec = exec_parity._call_with_frame_switch(s, obs, "darkroom_open", {"image": str(path)}, path,
                                              30.0, False, "mcp")
    assert s.relaunches == [path]
    assert rec.error is None and s.current_frame == path
    assert obs.extra["frame_switch_relaunch"] == ["mcp"]


def test_call_with_frame_switch_does_not_retry_other_errors(tmp_path):
    from gauntlet.adapter import CallRecord

    class _Session(_FakeStageSession):
        def call(self, tool, args, budget_s, write=False):
            self.calls.append((tool, args))
            return CallRecord(tool=tool, args=args, error="usage", write=write)

    s = _Session(tmp_path)
    path = tmp_path / "parity" / "mcp" / "H-mcp.ORF"
    obs = Observation()
    rec = exec_parity._call_with_frame_switch(s, obs, "darkroom_open", {"image": str(path)}, path,
                                              30.0, False, "mcp")
    assert s.relaunches == []
    assert rec.error == "usage"
    assert "frame_switch_relaunch" not in obs.extra


def test_parity_pair_imports_both_sides_once_for_a_non_open_verb(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": "happy"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs1 = Observation()
    exec_parity.parity_pair(s, case, step, obs1)
    import_calls = [c for c in mcp.calls if c[0] == "library_import"]
    # One `library_import` per side (mcp copy + cli copy) on the FIRST case.
    assert len(import_calls) == 2

    # A-1/A-2 (finding 4): a SECOND parity case in the same session must NOT
    # re-import -- the destinations are cached on the session.
    obs2 = Observation()
    exec_parity.parity_pair(s, case, step, obs2)
    import_calls_after = [c for c in mcp.calls if c[0] == "library_import"]
    assert len(import_calls_after) == 2


def test_parity_pair_uses_distinct_source_names_per_side(corpus, tmp_path, monkeypatch):
    # A-1 (finding 4): `library_import`'s resume identity is (source_name,
    # sha256) -- byte-identical copies sharing ONE name would resume-skip
    # into the same dest. Distinct per-side names must reach the CLI.
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": "happy"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    import_calls = [c for c in mcp.calls if c[0] == "library_import"]
    sources = [args["source"] for _, args in import_calls]
    assert len(set(sources)) == 2
    assert sources[0].endswith("-mcp") is False  # the SOURCE is a directory, not a filename
    # The two staged directories are the per-side "mcp"/"cli" ones, each
    # holding one distinctly-named file.
    import os
    names = [os.listdir(src)[0] for src in sources]
    assert names[0] != names[1]
    assert names[0].endswith("-mcp.ORF") or names[0].endswith("-cli.ORF")


def test_parity_pair_skips_staging_for_darkroom_open(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"darkroom_open": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-darkroom-open-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_open", "variant": "happy"})
    step = Step("parity_pair", {"verb": "darkroom.open", "args": {"image": "{frame:hero}"}},
               60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert not [c for c in mcp.calls if c[0] == "library_import"]


def test_parity_pair_records_fixture_errors_in_obs_extra(corpus, tmp_path, monkeypatch):
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": ok, "library_import": {"ok": False, "data": {}}})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": "happy"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["fixture_errors"] == \
        ["mcp_import_failed: no_files_named", "cli_import_failed: no_files_named"]


def test_frame_switch_relaunch_does_not_hide_a_real_mismatch(corpus, tmp_path, monkeypatch):
    # R2-1 (review fix round 3): both sides needing the relaunch must NEVER
    # lift a genuine envelope mismatch from fail to needs_review -- only
    # when the pair still AGREES after both relaunches. Real
    # par-darkroom.open-happy shape: not_in_darkroom on the first try, a
    # genuine (differing) open answer on the retry.
    err = {"ok": False, "outcome": "error:not_in_darkroom",
          "error": {"code": "not_in_darkroom"}}
    ok_mcp = {"ok": True, "outcome": "ok", "data": {"opened": True, "view": "darkroom"}}
    ok_cli = {"ok": True, "outcome": "ok", "data": {"opened": True, "view": "lighttable"}}
    mcp = FakeMcp(answers={"darkroom_open": [err, ok_mcp]})
    s = _session(corpus, tmp_path, mcp=mcp).open()

    cli_answers = [err, ok_cli]

    def fake_run(*a, **k):
        env = cli_answers.pop(0) if len(cli_answers) > 1 else cli_answers[0]
        code = 0 if env.get("ok") else 6
        return subprocess.CompletedProcess(a[0], code, stdout=json.dumps(env), stderr="")

    monkeypatch.setattr(exec_parity.subprocess, "run", fake_run)
    case = Case(id="par-darkroom-open-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_open", "variant": "happy"})
    step = Step("parity_pair", {"verb": "darkroom.open", "args": {"image": "{frame:hero}"}},
               60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["frame_switch_relaunch"] == ["mcp", "cli"]
    assert obs.extra["envelopes_match"] is False
    assert obs.extra["parity_needs_review"] is None


# ---- R2-2 (review fix round 3): the per-case sidecar-restore isolation in
# _staged_dests/_restore_sidecars was untested -- a hand mutant that
# replaced _restore_sidecars's body with `pass` still left the whole suite
# green. These two tests kill that exact mutant.


def test_staged_dests_restores_a_real_baseline_between_cases(tmp_path):
    from scouter.core import xmp

    hero = tmp_path / "trial" / "H.ORF"
    hero.parent.mkdir(parents=True, exist_ok=True)
    hero.write_bytes(b"raw")
    s = _FakeStageSession(tmp_path)
    s.staged.frames["hero"] = hero
    # Pre-create the sidecar at the destination _stage_side's fallback will
    # use (library_import fails in this fake, same as
    # test_stage_side_falls_back_and_records_the_failure), so the FIRST
    # _staged_dests call captures a REAL, non-empty baseline.
    dest_dir = s.root / "parity" / "mcp"
    dest_dir.mkdir(parents=True, exist_ok=True)
    expected_dest = dest_dir / "H-mcp.ORF"
    baseline_bytes = b"<xmp>baseline history_end=0</xmp>"
    xmp.sidecar_path(expected_dest).write_bytes(baseline_bytes)

    mcp_dest, _cli_dest = exec_parity._staged_dests(s)
    assert mcp_dest == expected_dest
    assert xmp.sidecar_path(mcp_dest).read_bytes() == baseline_bytes

    # Case 1 (a write verb) dirties the sidecar.
    xmp.sidecar_path(mcp_dest).write_bytes(b"<xmp>case-1 wrote something else</xmp>")

    # Case 2: _staged_dests must restore the ORIGINAL baseline, not case
    # 1's leftovers.
    mcp_dest2, _cli_dest2 = exec_parity._staged_dests(s)
    assert mcp_dest2 == mcp_dest
    assert xmp.sidecar_path(mcp_dest2).read_bytes() == baseline_bytes


def test_staged_dests_missing_baseline_deletes_a_dirtied_sidecar(tmp_path):
    from scouter.core import xmp

    hero = tmp_path / "trial" / "H.ORF"
    hero.parent.mkdir(parents=True, exist_ok=True)
    hero.write_bytes(b"raw")
    s = _FakeStageSession(tmp_path)
    s.staged.frames["hero"] = hero
    # No pre-existing sidecar this time -> the baseline snapshot is None.
    mcp_dest, _cli_dest = exec_parity._staged_dests(s)
    assert exec_parity._read_sidecar_bytes(mcp_dest) is None

    # A case writes a sidecar into existence (e.g. a real darkroom write).
    xmp.sidecar_path(mcp_dest).write_bytes(b"<xmp>written by a case, no baseline existed</xmp>")
    assert xmp.sidecar_path(mcp_dest).exists()

    # The NEXT case: with no baseline to restore to, _staged_dests must
    # DELETE it -- not leave the previous case's write in place.
    exec_parity._staged_dests(s)
    assert not xmp.sidecar_path(mcp_dest).exists()


def test_parity_pair_records_the_isolation_mechanism_honestly(corpus, tmp_path, monkeypatch):
    # Review fix round 3, residual ruling: the per-case restore is XMP-file
    # only, never darktable's library.db -- recorded, not claimed as full
    # isolation.
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": "happy"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["isolation"] == "xmp_file_restore (library.db not reset)"


def test_parity_pair_names_a_missing_mcp_envelope(corpus, tmp_path, monkeypatch):
    # C17: an MCP leg that answered nothing is "mcp_no_envelope", never a
    # vague `values` diff -- and it can never be a pass.
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": None})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": "happy"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    rec = exec_parity.parity_pair(s, case, step, obs)
    assert "mcp_no_envelope" in obs.extra["parity_diffs"]
    assert obs.extra["envelopes_match"] is False
    assert rec.envelope["ok"] is False


# ---- final-20261110 HB6 / HB7 ------------------------------------------------


def _env(data, **kw):
    return {"ok": True, "outcome": "ok", "data": data, **kw}


def test_compare_ignores_per_leg_volatile_keys():
    mcp = _env({"applied": 1.5, "analyse_ms": 3, "render_ms": 2060, "cached": False,
                "archive_dir": "/a/2026-11-09T10", "xmp_mtime": 1.0, "preview_path": "/p/1.ppm",
                "from_ts": "2026-11-09T12:46:48Z", "totals": {"calls": 3}})
    cli = _env({"applied": 1.5, "analyse_ms": 0, "render_ms": 11, "cached": True,
                "archive_dir": "/a/2026-11-09T11", "xmp_mtime": 2.0, "preview_path": "/p/2.ppm",
                "from_ts": "2026-11-09T12:50:00Z", "totals": {"calls": 9}})
    assert exec_parity.compare(mcp, cli) == []


def test_compare_still_differs_on_meaningful_keys():
    a = _env({"applied": 1.5, "analyse_ms": 3})
    b = _env({"applied": 1.0, "analyse_ms": 3})
    assert exec_parity.compare(a, b) == ["values"]
    assert exec_parity.compare(_env({"noop": False}), _env({"noop": True})) == ["values"]


def test_compare_histograms_by_shape_not_by_raw_counts():
    h1 = {"r": [1, 2, 3], "g": [4, 5, 6]}
    h2 = {"r": [9, 9, 9], "g": [0, 0, 1]}
    assert exec_parity.compare(_env({"histogram": h1}), _env({"histogram": h2})) == []
    odd = {"r": [9, 9], "g": [0, 0, 1]}
    assert exec_parity.compare(_env({"histogram": h1}), _env({"histogram": odd})) == ["shapes"]


def _pair_session(corpus, tmp_path, mcp, monkeypatch, cli_env):
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(cli_env), stderr=""))
    return s


def _kelvin_case(variant="happy"):
    case = Case(id=f"par-wb-kelvin-{variant}", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": variant})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    return case, step


def test_parity_pair_stores_the_mcp_envelope_next_to_the_cli_one(corpus, tmp_path, monkeypatch):
    mcp_env = _env({"kelvin": 4000})
    cli_env = _env({"kelvin": 4000})
    s = _pair_session(corpus, tmp_path, FakeMcp(answers={"wb_kelvin": mcp_env}), monkeypatch,
                      cli_env)
    case, step = _kelvin_case()
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert obs.extra["mcp_envelope"] == mcp_env
    assert obs.extra["cli_envelope"] == cli_env


def test_parity_pair_resets_the_frame_between_the_two_legs(corpus, tmp_path, monkeypatch):
    # HB6: the CLI leg ran on the frame the MCP leg had just edited (noop).
    from tests.gauntlet import execute

    order: list[str] = []
    ok = _env({})
    monkeypatch.setattr(execute, "isolate", lambda *a, **k: order.append("reset") or False)
    s = _session(corpus, tmp_path, mcp=FakeMcp(answers={"darkroom_set": ok})).open()

    def run(*a, **k):
        order.append("cli")
        return subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(ok), stderr="")

    monkeypatch.setattr(exec_parity.subprocess, "run", run)
    case = Case(id="par-darkroom-set-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_set", "variant": "happy"})
    step = Step("parity_pair", {"verb": "darkroom.set",
                                "args": {"module": "exposure", "param": "exposure",
                                         "value": 1.0}}, 60.0, True)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert order == ["reset", "cli"]
    assert obs.extra["leg_restore"] == "reset"


def test_smoke_style_is_staged_with_a_non_empty_module_list(tmp_path):
    s = _FakeStageSession(tmp_path)
    exec_parity._ensure_smoke_style(s, tmp_path / "parity" / "mcp" / "H-mcp.ORF")
    call = [c for c in s.calls if c[0] == "style_extract"][0]
    assert call[1]["modules"] and "exposure" in call[1]["modules"]


def test_fixture_runs_a_confirmed_backup_once(corpus, tmp_path, monkeypatch):
    ok = _env({})
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _pair_session(corpus, tmp_path, mcp, monkeypatch, ok)
    case, step = _kelvin_case()
    exec_parity.parity_pair(s, case, step, Observation())
    exec_parity.parity_pair(s, case, step, Observation())
    runs = [c for c in mcp.calls if c[0] == "backup_run"]
    assert len(runs) == 1 and runs[0][1].get("confirm") is True


def test_a_failed_fixture_scores_blocked_not_needs_review(corpus, tmp_path, monkeypatch):
    err = {"ok": False, "outcome": "error:usage", "error": {"code": "usage", "message": "x"}}
    mcp = FakeMcp(answers={"wb_kelvin": err, "library_import": {"ok": False, "data": {}}})
    s = _pair_session(corpus, tmp_path, mcp, monkeypatch, err)
    case, step = _kelvin_case("happy")
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    assert (obs.blocked or "").startswith("harness_bug: parity fixture failed")


def test_error_variants_of_gated_verbs_send_confirm_and_no_dry_run():
    from scouter import registry
    from tests.gauntlet.layers import parity

    verbs = registry.load_verbs()
    for vid in ("job.run", "config.init"):
        args = parity.error_args(verbs[vid])
        assert args.get("confirm") is True and "dry_run" not in args
    assert "dry_run" not in parity.error_args(verbs["preset.apply"])


def test_parity_happy_args_for_fixture_starved_verbs():
    from tests.gauntlet.layers import parity

    assert parity.happy_args("history.commit")["timeout_s"] >= 1
    assert parity.happy_args("export.release")["min_rating"] == 0
    assert "images" not in parity.happy_args("outliers.list")
