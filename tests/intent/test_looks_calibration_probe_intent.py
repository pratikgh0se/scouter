"""Runs tests/intent/looks_calibration_probe.json (Zenkai Rules Z1). Two
observations per example (Z2): which tool the probe sent (`darkroom_set`,
`darkroom_adjust`, or none) and what the calibration dict/plan_moves records
for that key -- a measured slope, an explicit failure, or neither."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gauntlet.adapter import Observation
from tests.gauntlet import exec_looks
from tests.gauntlet.test_exec_looks import _FakeChartSession, _stats

EXAMPLES = json.loads((Path(__file__).parent / "looks_calibration_probe.json").read_text())[
    "examples"]

#: the look-target field that exercises `plan_moves`'s `add()` for each key
#: (spec §4.2's linear solve), so a calibration hole is proven to block
#: planning, not merely to leave the key untouched.
_TARGETS_FOR_KEY = {
    "exposure.black": {"black_lift": 1.0},
    "colorbalancergb.global_chroma": {"saturation_delta": 1},
    "temperature.temperature": {"grey_cast": {"b": 1.0}},
}


class LooksCalibrationProbeIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        key = f"{inp['module']}.{inp['param']}"
        with tempfile.TemporaryDirectory() as tmp:
            session = _FakeChartSession(Path(tmp))

            def fake_render_stats(src, out_ppm, _key=key):
                if out_ppm.name.startswith(f"cal-{_key}"):
                    # A real, measured move on whichever region/stat this
                    # key's CALIBRATE entry reads -- not just exposure.black.
                    region, stat = exec_looks.chart_render.CALIBRATE[_key]
                    bumped = _stats()
                    bumped[region] = {**bumped[region], stat: bumped[region][stat] + 1.0}
                    return bumped
                return _stats()

            with mock.patch.object(exec_looks.chart_render, "render_stats", fake_render_stats), \
                mock.patch.object(exec_looks.chart_render, "compare_golden",
                                  lambda stats, golden=None: {"patch_de": 0.0, "verdict": "pass"}), \
                mock.patch.object(exec_looks.chart_render, "write_golden_candidate",
                                  lambda run_root, stats: None):
                obs = Observation()
                rec = exec_looks.chart_golden(session, case=None, step=None, obs=obs)
            self.assertIsNone(rec.error, (ex["id"], rec.error))

            # Oracle 1: which tool the probe sent for this key (or none).
            sent = [(t, a) for t, a in session.calls if a.get("module") == inp["module"] and
                   a.get("param") == inp["param"]]
            if want["tool"] is None:
                self.assertEqual(sent, [], (ex["id"], sent))
            else:
                tools = {t for t, _ in sent}
                self.assertEqual(tools, {want["tool"]}, (ex["id"], sent))
                set_call = next(a for t, a in sent if t == "darkroom_set")
                self.assertEqual(set_call["value"], want["value"], (ex["id"], set_call))

            # Oracle 2: the calibration record -- measured, explicitly failed,
            # measured-but-unverified (row 474), or neither -- never a silent
            # falsy zero standing in for a hole.
            cal = obs.extra["calibration"]
            errors = obs.extra.get("calibration_errors", {})
            if want["calibration"] == "measured":
                self.assertNotIn(cal[key], (None, 0.0), (ex["id"], cal[key]))
                self.assertNotIn(key, errors, (ex["id"], errors))
            elif want["calibration"] == "measured_unverified":
                # A real slope WAS measured (probed from an un-pinned
                # `default_candidate`), but flagged, not folded in clean.
                self.assertNotIn(cal[key], (None, 0.0), (ex["id"], cal[key]))
                self.assertEqual(errors.get(key), "default_unverified", (ex["id"], errors))
            else:
                self.assertIsNone(cal[key], (ex["id"], cal[key]))
                self.assertIn(key, errors, (ex["id"], errors))

            look = {"targets": {"tone": {}, **_TARGETS_FOR_KEY.get(key, {})}}
            moves, _notes = exec_looks.plan_moves(look, cal, {})
            planned = {(m, p) for m, p, _ in moves}
            pair = (inp["module"], inp["param"])
            if want["planned"]:
                self.assertIn(pair, planned, (ex["id"], moves))
            else:
                self.assertNotIn(pair, planned, (ex["id"], moves))


def _make(ex: dict):
    def test(self: LooksCalibrationProbeIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(LooksCalibrationProbeIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
