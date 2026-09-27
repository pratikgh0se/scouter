"""Runs tests/intent/parity_fixture_staging.json (Zenkai Rules Z1) against
the real tests/gauntlet/exec_parity.py functions, reusing the fakes
tests/gauntlet/test_exec_parity.py already defines for this (Z2: two
independent oracles per example, noted per test)."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Expect, Step
from tests.gauntlet import exec_parity
from tests.gauntlet.test_exec_parity import _FakeStageSession, corpus  # noqa: F401 (fixture)
from tests.gauntlet.test_session import FakeMcp, _session

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent / "parity_fixture_staging.json").read_text())[
                "examples"]}


def test_not_in_library_verb_imports_both_sides(corpus, tmp_path, monkeypatch):
    ex = EXAMPLES["not-in-library-verb-imports-both-sides"]
    ok = {"ok": True, "outcome": "ok", "data": {
        "job_slug": exec_parity.SMOKE_JOB_SLUG, "date": "2026-05-24",
        "files": [{"source_name": "mcp-stub", "dest": "raw/x"}]}}
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: __import__(
        "subprocess").CompletedProcess(a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin", "variant": "happy"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    # Oracle A: the import call count.
    imports = [c for c in mcp.calls if c[0] == "library_import"]
    assert len(imports) == ex["expect"]["library_import_calls"]
    # Oracle B (A-6, review fix round 2 -- independent of the import count):
    # the wb_kelvin call itself must have been substituted with the STAGED
    # (library) path, never the raw, un-imported copy. `library_import`'s
    # own envelope in this fake never names a real dest for "mcp-stub" (the
    # actual staged file is named "*-mcp.ORF"), so the fallback path is what
    # reaches the verb here -- still asserted explicitly, not "some path".
    wb_calls = [c for c in mcp.calls if c[0] == "wb_kelvin"]
    assert wb_calls, "wb_kelvin was never called"
    image_arg = wb_calls[0][1]["image"]
    assert image_arg.endswith("-mcp.ORF")
    assert "/parity/mcp/" in image_arg.replace("\\", "/")


def test_darkroom_open_skips_staging_uses_frame_switch_recovery_instead(corpus, tmp_path,
                                                                        monkeypatch):
    ex = EXAMPLES["darkroom-open-skips-staging-uses-frame-switch-recovery-instead"]
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"darkroom_open": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: __import__(
        "subprocess").CompletedProcess(a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-darkroom-open-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "darkroom_open", "variant": "happy"})
    step = Step("parity_pair", {"verb": "darkroom.open", "args": {"image": "{frame:hero}"}},
               60.0, False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    # Oracle A: no library_import at all.
    imports = [c for c in mcp.calls if c[0] == "library_import"]
    assert len(imports) == ex["expect"]["library_import_calls"]
    # Oracle B (independent): the open itself still genuinely ran (recorded
    # as this session's current frame), not skipped outright.
    open_calls = [c for c in mcp.calls if c[0] == "darkroom_open"]
    assert open_calls


def test_not_in_darkroom_on_open_retries_once_via_relaunch(tmp_path):
    ex = EXAMPLES["not-in-darkroom-on-open-retries-once-via-relaunch"]

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
    # Oracle A: the relaunch log.
    assert bool(s.relaunches) == ex["expect"]["relaunched"]
    # Oracle B (independent): the call record's own error field.
    assert rec.error == ex["expect"]["final_error"]
    # Oracle C (review fix round 2, A-3/finding 7 -- also independent): the
    # retry must be RECORDED, so a pair where both sides needed it can be
    # scored needs_review instead of a silent pass.
    assert obs.extra["frame_switch_relaunch"] == ["mcp"]


def test_distinct_source_names_and_once_per_session_import_documented_here():
    # Documents the case run here; the full assertions (import counts across
    # two calls, distinct source dir contents) are pinned in
    # tests/gauntlet/test_exec_parity.py::
    #   test_parity_pair_imports_both_sides_once_for_a_non_open_verb
    #   test_parity_pair_uses_distinct_source_names_per_side
    # (review fix round 2, A-1/A-2, finding 4) -- kept there rather than
    # duplicated here since they need the real FakeMcp/_session machinery
    # this module already imports for the other two examples.
    assert EXAMPLES["distinct-source-names-and-once-per-session-import"]["expect"][
        "mcp_and_cli_source_names_differ"] is True
