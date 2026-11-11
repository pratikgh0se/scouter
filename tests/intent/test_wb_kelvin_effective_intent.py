"""Runs tests/intent/wb_kelvin_effective.json (Zenkai Rules Z1). Two observations
per example (Z2): the result's fields, and the committed sidecar slot read back
through the repo's own xmp decoder (not through wb.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, wb, xmp
from tests.test_wb_colorcal_route import _RouteFixture, synthetic_table

EXAMPLES = json.loads((Path(__file__).parent / "wb_kelvin_effective.json").read_text())["examples"]


class WbKelvinEffectiveIntentTest(_RouteFixture):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        for target, value in (("scouter.core.exif.read_tags", {}),
                              ("scouter.core.exif.camera_slug", "em10iii")):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        synthetic_table(self)
        if inp["temperature_preset"] == 4:
            self.preset_4()
        if inp["route_a_live"]:
            self.pin()
            self.colorcal(temperature=inp["illuminant_k"])
        elif inp["temperature_preset"] == 4:
            self.unpin()
        r = wb.kelvin_verb.__wrapped__(str(self.raw), job="job-test")
        if "effective_kelvin" in want:
            sidecar = xmp.latest_params(darkroom._read_sidecar_parsed(str(self.raw)),
                                        "channelmixerrgb")["temperature"]
            self.assertAlmostEqual(sidecar, want["effective_kelvin"], places=1, msg=ex["id"])
            self.assertAlmostEqual(r.effective_kelvin, want["effective_kelvin"], places=1)
        if want["kelvin_is_effective"]:
            self.assertIsNotNone(r.kelvin, ex["id"])
            self.assertEqual(r.effective_kelvin, r.kelvin, ex["id"])
        elif "effective_kelvin" not in want:  # the unavailable route: no claim at all
            self.assertIsNone(r.effective_kelvin, ex["id"])
            self.assertIn(want["note_mentions"], r.note, ex["id"])
        else:
            self.assertNotAlmostEqual(r.effective_kelvin, r.kelvin, delta=50, msg=ex["id"])
        if want["note_mentions"] is None:
            self.assertIsNone(r.note, ex["id"])
        else:
            self.assertIn(want["note_mentions"], r.note, ex["id"])


def _make(ex: dict):
    def test(self: WbKelvinEffectiveIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(WbKelvinEffectiveIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
