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
    ok = {"ok": True, "outcome": "ok", "data": {}}
    mcp = FakeMcp(answers={"wb_kelvin": ok})
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(exec_parity.subprocess, "run", lambda *a, **k: __import__(
        "subprocess").CompletedProcess(a[0], 0, stdout=json.dumps(ok), stderr=""))
    case = Case(id="par-wb-kelvin-happy", layer="parity", tier="P0", mode="tool", steps=[],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"mcp": "wb_kelvin"})
    step = Step("parity_pair", {"verb": "wb.kelvin", "args": {"image": "{frame:hero}"}}, 60.0,
               False)
    obs = Observation()
    exec_parity.parity_pair(s, case, step, obs)
    # Oracle A: the import call count.
    imports = [c for c in mcp.calls if c[0] == "library_import"]
    assert len(imports) == ex["expect"]["library_import_calls"]
    # Oracle B (independent): the pair still completed and compared cleanly.
    assert "envelopes_match" in obs.extra


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
                meta={"mcp": "darkroom_open"})
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
    path = tmp_path / "parity" / "mcp" / "H.ORF"
    rec = exec_parity._call_with_frame_switch(s, "darkroom_open", {"image": str(path)}, path,
                                              30.0, False)
    # Oracle A: the relaunch log.
    assert bool(s.relaunches) == ex["expect"]["relaunched"]
    # Oracle B (independent): the call record's own error field.
    assert rec.error == ex["expect"]["final_error"]
