"""Tool-mode execution against the session fakes: no darktable, no MCP server."""

import pytest

from gauntlet.case import Case, Check, Expect, Step
from tests.gauntlet import execute, observe
from tests.gauntlet.corpus import Corpus, OriginalPathRefused
from tests.gauntlet.test_session import FakeDt, FakeMcp, _session


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def test_timeout_with_unresponsive_ping_blocks_as_hang(corpus, tmp_path, monkeypatch):
    mcp = FakeMcp({"darkroom_say": TimeoutError("no answer")})
    s = _session(corpus, tmp_path, FakeDt(health="unresponsive"), mcp).open()
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                 "params": {}, "units": {}})
    case = Case(id="c", layer="scenarios", tier="P0", mode="tool",
                steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
                expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert obs.blocked == "hang" and obs.calls[0].health == "unresponsive"


def test_placeholders_and_originals_guard(corpus, tmp_path, monkeypatch):
    s = _session(corpus, tmp_path).open()
    out = execute.substitute({"image": "{frame:hero}", "slug": "{job_slug}"}, s, [])
    assert out["image"].endswith("H.ORF") and out["image"].startswith(str(tmp_path / "s1"))
    monkeypatch.setattr("tests.gauntlet.corpus.TRIAL", corpus.root)
    with pytest.raises(OriginalPathRefused):
        execute.substitute(str(corpus.root / "H.ORF"), s, [])


def test_step_snapshots_are_recorded_per_write(corpus, tmp_path, monkeypatch):
    s = _session(corpus, tmp_path).open()
    seq = iter([0.7, 0.7, 0.7, 1.0, 1.3])  # isolate, before, step 1, step 2, after
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x", "params": {"exposure.exposure": next(seq)}, "units": {}})
    case = Case(id="c2", layer="coverage", tier="P1", mode="tool",
                steps=[Step("darkroom_set", {"module": "exposure", "param": "exposure",
                                             "value": 1.0}, 3.5, True),
                       Step("darkroom_adjust", {"module": "exposure", "param": "exposure",
                                                "delta": 0.3}, 3.5, True)],
                expect=Expect("edit", [Check("M1")]), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert [x["exposure.exposure"] for x in obs.extra["snapshots"]] == [0.7, 0.7, 1.0]
    assert s.touched == {"exposure"}


def test_flatten_drops_unnamed_slots_and_adds_enabled():
    snap = {"fingerprint": "f", "history_end": 3, "modules": {
        "sigmoid": {"enabled": True, "decoded": {"contrast": 1.5, "f2": 100.0}},
        "temperature": {"enabled": True, "decoded": {"coeff_r": 2.0, "coeff_g": 1.0,
                                                      "coeff_b": 1.7, "preset": 2}}}}
    flat = observe.flatten(snap, kelvin=lambda c: 5500.0)
    p = flat["params"]
    assert p["sigmoid.contrast"] == 1.5 and "sigmoid.f2" not in p
    assert p["sigmoid.__enabled"] == 1 and p["temperature.temperature"] == 5500.0
    assert flat["units"]["sigmoid.__enabled"] == "bool"


def test_flatten_carries_the_param_meta_the_evaluator_reads():
    """Night-1 evaluator fixes: module defaults (bug 3), WB derived keys and the
    coeff_g2 null equivalence (bug 4) ride on every snapshot, like units."""
    flat = observe.flatten({"fingerprint": "f", "modules": {}})
    assert flat["defaults"]["colorbalancergb.highlights_chroma"] == 0.0
    assert flat["defaults"]["colorbalancergb.shadows_hue"] == 0.0
    assert "exposure.exposure" not in flat["defaults"]  # no known default: never assumed
    assert "temperature.coeff_r" in flat["derived"]["temperature.tint"]
    assert "temperature.coeff_g2" in flat["derived"]["temperature.temperature"]
    assert flat["null_equiv"] == {"temperature.coeff_g2": 0.0}


def test_ensure_frame_blocked_is_returned_as_observation(corpus, tmp_path):
    bad = FakeMcp({"darkroom_open": {"ok": False, "outcome": "error",
                                     "error": {"code": "not_found"}, "data": {}}})
    s = _session(corpus, tmp_path, mcp=bad).open()
    case = Case(id="c3", layer="scenarios", tier="P0", mode="tool", steps=[],
                expect=Expect("edit"), body="olympus", frame_role="dark")
    obs = execute.run_tool_case(s, case)
    assert obs.blocked.startswith("session:") and obs.calls == []


def test_isolate_refresh_blocked_is_returned_as_observation(corpus, tmp_path, monkeypatch):
    dt = FakeDt(ready=(True, False, False))
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"exposure.exposure": 0.7}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x", "params": {"exposure.exposure": 1.3}, "units": {}})
    case = Case(id="c4", layer="scenarios", tier="P0", mode="tool", steps=[],
                expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert obs.blocked.startswith("session:") and dt.launches == 3


def test_virtual_tool_exception_blocks_as_harness_error(corpus, tmp_path, monkeypatch):
    def boom(session, case, step, obs):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(execute.VIRTUAL, "boom_tool", boom)
    s = _session(corpus, tmp_path).open()
    case = Case(id="c5", layer="scenarios", tier="P0", mode="tool",
                steps=[Step("boom_tool", {}, 8.0, False)],
                expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert obs.blocked == "harness_error: boom_tool: RuntimeError: kaboom"
    assert obs.calls == []


def test_virtual_tool_park_propagates(corpus, tmp_path, monkeypatch):
    from gauntlet.adapter import Park

    def park(session, case, step, obs):
        raise Park("usage limit")

    monkeypatch.setitem(execute.VIRTUAL, "park_tool", park)
    s = _session(corpus, tmp_path).open()
    case = Case(id="c6", layer="scenarios", tier="P0", mode="tool",
                steps=[Step("park_tool", {}, 8.0, False)],
                expect=Expect("edit"), body="olympus", frame_role="hero")
    with pytest.raises(Park):
        execute.run_tool_case(s, case)


def test_contaminated_frame_is_restaged(corpus, tmp_path, monkeypatch):
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"exposure.exposure": 0.7}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x", "params": {"exposure.exposure": 1.3}, "units": {}})
    assert execute.isolate(s, frame, "hero") is True
    assert s.staged.frames["hero"] != frame and dt.launches == 2


def test_isolate_does_not_restage_a_reset_absent_module_at_default(corpus, tmp_path, monkeypatch):
    """Night-1 fix (class LK-refresh-no-open, the isolate half): a chart<->hero
    switch resets whatever the previous case touched, on the new frame. When
    that module was never in THIS frame's history (the PFM chart has no
    temperature module), the reset writes it in at darktable's default:
    absent -> present-at-default, not a real change. Before the fix, a plain
    param_equal read that as "moved" and re-staged a perfectly clean frame,
    which then hit the refresh-no-open bug on every such switch."""
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"sigmoid.contrast": None}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x",
        "params": {"sigmoid.contrast": 1.0, "sigmoid.__enabled": 1},
        "units": {"sigmoid.contrast": "ratio", "sigmoid.__enabled": "bool"},
        "defaults": {"sigmoid.contrast": 1.0}})
    assert execute.isolate(s, frame, "hero") is False
    assert dt.launches == 1  # no relaunch: this was never a real move


def test_isolate_still_restages_an_absent_module_leaked_at_a_non_default_value(
        corpus, tmp_path, monkeypatch):
    """Reviewer follow-up on the absent-vs-default fix above (n1-session-review):
    the escape hatch is narrow on purpose. A module absent before that comes
    back at some OTHER value after the reset (not darktable's own default --
    a real leak from the previous case, or a reset that silently failed) is
    still a genuine move and must still restage. This guards the exact
    condition `evaluate._absent_equiv` requires -- `_module_at_default` -- by
    only ever satisfying `not evaluate._absent_equiv`; it does not touch or
    duplicate that logic."""
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"sigmoid.contrast": None}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x",
        "params": {"sigmoid.contrast": 2.5, "sigmoid.__enabled": 1},  # leaked, not default
        "units": {"sigmoid.contrast": "ratio", "sigmoid.__enabled": "bool"},
        "defaults": {"sigmoid.contrast": 1.0}})
    assert execute.isolate(s, frame, "hero") is True
    assert dt.launches == 2  # a real move: restaged


def test_walkthrough_continuation_skips_isolate_and_journal_reset(corpus, tmp_path, monkeypatch):
    """Fix A (night-1 triage rank 6, GD-walkthrough-state): a walkthrough case
    (meta["session"] == "walkthrough") after the walkthrough's own first case
    must not be isolate()'d or journal-reset -- that would erase the state
    the next scripted step depends on (gd-04's undo would find nothing)."""
    from tests.gauntlet.session import reset_journal

    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "a", "params": {}, "units": {}})
    job = s.staged.env["SCOUTER_JOB"]
    journal_file = s.staged.cache / "journal" / f"{job}.jsonl"
    journal_file.parent.mkdir(parents=True, exist_ok=True)
    journal_file.write_text('{"seq": 1}\n', encoding="utf-8")

    case1 = Case(id="gd-01", layer="guide", tier="P0", mode="tool",
                steps=[Step("darkroom_set", {"module": "exposure", "exposure": 1.2}, 3.5,
                            True)],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"session": "walkthrough", "order": 0})
    obs1 = execute.run_tool_case(s, case1)
    assert s.walkthrough_started is True
    assert s.touched == {"exposure"}  # a real write: isolate() would reset it next case
    assert obs1.extra.get("journal_reset_entries") == 1  # the first case IS reset (control)

    # gd-01's own write left a journal entry (simulated: the real MCP fake
    # here does not touch the journal file itself). gd-02 must see it, not
    # a wiped one -- gd-04/gd-08's own undo later depends on it.
    journal_file.write_text('{"seq": 2, "verb": "param.set"}\n', encoding="utf-8")
    mcp.calls.clear()
    case2 = Case(id="gd-02", layer="guide", tier="P0", mode="tool",
                steps=[Step("darkroom_say", {"phrase": "less contrast"}, 8.0, True)],
                expect=Expect("workflow"), body="olympus", frame_role="hero",
                meta={"session": "walkthrough", "order": 1})
    obs2 = execute.run_tool_case(s, case2)

    assert "darkroom_reset" not in [t for t, _ in mcp.calls]  # isolate() skipped
    assert "journal_reset_entries" not in obs2.extra  # reset_journal() skipped
    assert journal_file.read_text(encoding="utf-8") == '{"seq": 2, "verb": "param.set"}\n'


def test_ordinary_case_after_a_walkthrough_is_still_isolated_and_journal_reset(
        corpus, tmp_path, monkeypatch):
    """Z1 contrast case: a non-walkthrough case (no meta["session"], or any
    other value) never inherits the walkthrough's shared-state exemption,
    even in the same session right after one."""
    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    s.walkthrough_started = True  # as if a walkthrough case already ran
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "a", "params": {}, "units": {}})
    s.touched = {"exposure"}
    job = s.staged.env["SCOUTER_JOB"]
    journal_file = s.staged.cache / "journal" / f"{job}.jsonl"
    journal_file.parent.mkdir(parents=True, exist_ok=True)
    journal_file.write_text('{"seq": 1}\n', encoding="utf-8")

    case = Case(id="cov-1", layer="coverage", tier="P1", mode="tool",
               steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
               expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)

    assert "darkroom_reset" in [t for t, _ in mcp.calls]  # isolate() ran
    assert obs.extra["journal_reset_entries"] == 1  # reset_journal() ran


def test_a_virtual_tool_that_blocks_stops_the_later_steps(corpus, tmp_path, monkeypatch):
    """(final fix M3): a virtual tool setting obs.blocked itself ends the case."""
    from gauntlet.adapter import CallRecord

    def blocks(session, case, step, obs):
        obs.blocked = "session: frame switch failed"
        return CallRecord("blocker", {}, {"ok": False})

    monkeypatch.setitem(execute.VIRTUAL, "blocker", blocks)
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                 "params": {}, "units": {}})
    mcp = FakeMcp()
    s = _session(corpus, tmp_path, mcp=mcp).open()
    case = Case(id="c9", layer="looks", tier="P1", mode="tool",
                steps=[Step("blocker", {}, 8.0, False),
                       Step("darkroom_set", {"module": "exposure"}, 3.5, True)],
                expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert obs.blocked == "session: frame switch failed"
    assert [c.tool for c in obs.calls] == ["blocker"]
    assert "darkroom_set" not in [t for t, _ in mcp.calls]
