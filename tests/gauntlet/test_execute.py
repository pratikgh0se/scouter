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
