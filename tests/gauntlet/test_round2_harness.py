"""fix/harness-r2: HB3 export_files counter (two oracles) and HB4 stale scenario
taxonomy. Only existing public entry points (run_tool_case, scenarios.generate)."""

from __future__ import annotations

import pytest

from gauntlet.case import Case, Expect, Step
from tests.gauntlet import execute, observe, taxonomy
from tests.gauntlet.layers import scenarios
from tests.gauntlet.test_execute import corpus  # noqa: F401  (fixture)
from tests.gauntlet.test_session import FakeMcp, _session, FakeDt


def _export_case(out_dir):
    return Case(id="c", layer="scenarios", tier="P0", mode="tool",
                steps=[Step("export_run", {"images": ["x"], "out_dir": str(out_dir),
                                           "confirm": True}, 60.0, True)],
                expect=Expect("workflow"), body="olympus", frame_role="hero")


def _run(corpus, tmp_path, monkeypatch, envelope, out_dir):  # noqa: F811
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                 "params": {}, "units": {}})
    s = _session(corpus, tmp_path, FakeDt(), FakeMcp({"export_run": envelope})).open()
    return execute.run_tool_case(s, _export_case(out_dir))


def _env(out_dir, name, ok=True):
    return {"ok": True, "outcome": "ok", "data": {"out_dir": str(out_dir), "files": [
        {"ok": ok, "output": str(out_dir / name), "bytes": 5, "error": None}]}}


def test_hb3_export_files_counts_the_ok_files_that_are_on_disk(corpus, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "exports"
    out.mkdir()
    (out / "a.jpg").write_bytes(b"jpeg!")
    obs = _run(corpus, tmp_path, monkeypatch, _env(out, "a.jpg"), out)
    assert obs.extra["export_files"] == 1


def test_hb3_envelope_and_disk_disagreeing_fails_the_check(corpus, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "exports"
    out.mkdir()  # the envelope claims a.jpg, the disk has nothing
    obs = _run(corpus, tmp_path, monkeypatch, _env(out, "a.jpg"), out)
    assert obs.extra["export_files"] == 0
    assert obs.extra["export_files_oracles"] == {"reported": 1, "on_disk": 0}


def test_hb3_a_failed_file_is_not_counted(corpus, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "exports"
    out.mkdir()
    (out / "a.jpg").write_bytes(b"jpeg!")
    obs = _run(corpus, tmp_path, monkeypatch, _env(out, "a.jpg", ok=False), out)
    assert obs.extra["export_files"] == 0


@pytest.fixture(scope="module")
def scn():
    tool, _ = scenarios.generate(taxonomy.load())
    return {c.id: c for c in tool}


@pytest.mark.parametrize("rid", ["r0212", "r0574"])
def test_hb4_open_the_shadows_is_served_so_its_step_expects_ok(scn, rid):
    cases = [c for c in scn.values() if c.id.startswith(f"scn-{rid}-")]
    assert cases
    for c in cases:
        last = len(c.steps) - 1
        assert c.steps[last].args["phrase"] == "open the shadows"
        env = [ch for ch in c.expect.checks if ch.metric == "envelope" and ch.step == last]
        assert env and env[0].target["value"] is True, c.id


def test_hb4_a_still_unserved_module_phrase_keeps_expecting_a_refusal(scn):
    refusing = [c for c in scn.values() if c.meta.get("class") == "partial"
                and any(ch.metric == "envelope" and ch.target.get("value") is False
                        for ch in c.expect.checks)]
    assert refusing
