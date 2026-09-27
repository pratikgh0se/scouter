from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from tests.gauntlet import chart_render, exec_looks, taxonomy


def _stats(*, black_l: float = 10.0, base_slope: float = 1.0, base_c: float = 1.0) -> dict:
    """Enough of `chart_render.render_stats`'s shape for `slope()` to read
    every `CALIBRATE` region/stat once, for `chart_render.labs()` (every
    `patch:*` region needs L/a/b) and for the two `_base_*` fields
    `chart_golden` reads directly."""
    return {
        "chart_tone": {"slope": base_slope},
        "chart_chromatic": {"C": base_c},
        "patch:ramp_-4": {"L": 30.0, "a": 0.0, "b": 0.0, "ev": 1.0},
        "patch:ramp_+0": {"L": 50.0, "a": 0.0, "b": 0.0, "ev": 1.0},
        "patch:ramp_+2": {"L": 80.0, "a": 0.0, "b": 0.0, "ev": 1.0},
        "patch:ramp_-5": {"L": black_l, "a": 0.0, "b": 0.0},
    }


class _FakeChartSession:
    """A fake gauntlet session: no darktable, no bridge -- `call` answers by
    tool name the way the real MCP server does for a module the PFM chart
    never activates (row LK-calibration-holes): `darkroom_adjust` refuses
    `value_unknown` (nothing to read a current value from), `darkroom_set`
    answers ok (an absolute write needs no current value)."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "run"
        self.run_root = self.root
        self.calls: list[tuple[str, dict]] = []

    def ensure_frame(self, role: str) -> Path:
        return Path("/dev/null")

    def call(self, tool: str, args: dict, budget_s: float, *, write: bool = False) -> CallRecord:
        self.calls.append((tool, dict(args)))
        if tool == "darkroom_adjust":
            return CallRecord(tool, args, envelope={"ok": False,
                                                     "error": {"code": "value_unknown"}},
                              error="value_unknown", write=write)
        if tool == "darkroom_set":
            return CallRecord(tool, args, envelope={"ok": True}, write=write)
        return CallRecord(tool, args, envelope={"ok": True}, write=write)


def _run_chart_golden(monkeypatch, tmp_path) -> tuple[_FakeChartSession, Observation]:
    session = _FakeChartSession(tmp_path)

    def fake_render_stats(src, out_ppm):
        if out_ppm.name.startswith("cal-exposure.black"):
            return _stats(black_l=10.5)  # a real, measured black-point move
        return _stats()

    monkeypatch.setattr(chart_render, "render_stats", fake_render_stats)
    monkeypatch.setattr(chart_render, "compare_golden",
                       lambda stats, golden=None: {"patch_de": 0.0, "verdict": "pass"})
    monkeypatch.setattr(chart_render, "write_golden_candidate", lambda run_root, stats: None)

    obs = Observation()
    rec = exec_looks.chart_golden(session, case=None, step=None, obs=obs)
    assert rec.error is None, rec.error
    return session, obs


def test_chart_golden_probes_exposure_black_with_an_absolute_set_from_its_default(
        monkeypatch, tmp_path):
    """RED (pre-fix, e2fc2dd): `chart_golden` only ever called
    `darkroom_adjust`. The fake bridge above refuses that `value_unknown`
    for exposure.black (the PFM chart never activates it, so there is no
    current value for a relative delta to add to -- night-1 golden-case
    audit), the frame is never touched, `slope()` compares identical
    before/after stats and returns a falsy 0.0, and `plan_moves` reads that
    as "no calibration" -- calibration_ok is false and no black-point move
    is ever planned, with no record of why.

    GREEN (row 474, final-review issue I1): the probe reads exposure.black's
    `default_candidate` (0.0, `scouter/core/vocab.py` -- NOT a cited spec
    §4.3 default: that column is blank for this row by design, and no host
    probe had pinned the value yet) and sends an ABSOLUTE `darkroom_set`
    instead, which the fake bridge answers ok without needing a current
    value at all -- `darkroom_adjust` is never called for this key, and a
    real (non-zero) slope IS measured. At that point the base came from an
    un-pinned candidate, not a verified `neutral`/`default`, so the result
    was recorded as
    `calibration_errors["exposure.black"] = "default_unverified"` and
    `plan_moves` treated it exactly like a skipped calibration.

    RE-GREEN (row 495/496): `tests/test_host_phase3.py::
    ProbeExposureBlackDefaultHostTest` host-verified the candidate (within
    the tolerance that covers darktable's own scene-referred-raw default,
    `vocab.EXPOSURE_BLACK_SCENE_RAW_DEFAULT` -- fix round 1, finding 2: NOT
    a quantisation artifact, a real declared darktable default, per
    `src/iop/exposure.c reload_defaults()`) and `scouter/core/vocab.py`
    promoted it to a cited `default=0.0`. `_probe_base` now returns
    `("default", 0.0)` for this row ahead of `default_candidate`, so
    `chart_golden` no longer flags `exposure.black` as `default_unverified`,
    and `plan_moves` DOES plan a black-point move from the measured
    slope."""
    session, obs = _run_chart_golden(monkeypatch, tmp_path)

    calls_by_tool = {tool for tool, _ in session.calls}
    assert "darkroom_adjust" not in calls_by_tool
    set_calls = {(a["module"], a["param"]): a["value"] for t, a in session.calls
                if t == "darkroom_set"}
    assert set_calls[("exposure", "black")] == 0.0 + chart_render.PROBE["exposure.black"]

    cal = obs.extra["calibration"]
    # Really measured (the fake renders a distinct black-point stat for this
    # key, see `fake_render_stats` above) -- not a falsy zero slope.
    assert cal["exposure.black"] not in (None, 0.0)
    # No longer flagged unverified: the row's default is cited now, not a
    # candidate.
    assert "exposure.black" not in obs.extra.get("calibration_errors", {})
    assert "exposure.black" not in cal.get("_errors", {})

    look = {"targets": {"tone": {}, "black_lift": 1.0}}
    moves, notes = exec_looks.plan_moves(look, cal, {})
    move_by_pair = {(m, p): v for m, p, v in moves}
    assert ("exposure", "black") in move_by_pair
    assert not any("exposure.black calibration unverified" in n for n in notes)


def test_chart_golden_reports_a_probe_with_no_declared_default_as_a_failure_not_a_zero(
        monkeypatch, tmp_path):
    """temperature.temperature has neither a declared `neutral` nor a
    darktable `default` (the PFM chart has no EXIF and no white-balance
    module, so there is no honest Kelvin to probe from). `chart_golden`
    must skip it -- never attempt a doomed call, never fold it into a
    falsy 0.0 slope -- and record it as an explicit calibration failure so
    it is reported, not silently indistinguishable from "measured no
    effect"."""
    session, obs = _run_chart_golden(monkeypatch, tmp_path)

    assert not any(a.get("module") == "temperature" for _, a in session.calls)
    assert obs.extra["calibration"]["temperature.temperature"] is None
    assert "temperature.temperature" in obs.extra["calibration_errors"]


def test_plan_moves_is_linear_and_clamped():
    look = {"targets": {"tone": {"shadows_ev": 0.2, "mids_ev": 0, "highlights_ev": 0,
                                 "contrast": 0}, "saturation_delta": -1,
                        "grey_cast": {"a": 0, "b": 2.0},
                        "split_toning": {"shadow_hue": 200, "shadow_sat": 0.1,
                                         "highlight_hue": None, "highlight_sat": 0},
                        "black_lift": 0}}
    cal = {"colorbalancergb.shadows_luminance": 2.0, "temperature.temperature": 0.004}
    moves, notes = exec_looks.plan_moves(look, cal, {"temperature.temperature": 5000.0})
    d = {(m, p): v for m, p, v in moves}
    assert d[("colorbalancergb", "shadows_luminance")] == 0.1
    assert d[("temperature", "temperature")] == 5500.0
    assert d[("colorbalancergb", "global_saturation")] == -0.5
    assert d[("colorbalancergb", "shadows_hue")] == 200.0
    assert any("monochrome" in n for n in notes)


def test_every_look_plans_without_error():
    cal = {k: 1.0 for k in chart_render.CALIBRATE}
    for look in taxonomy.load().looks:
        exec_looks.plan_moves(look, cal, {})
