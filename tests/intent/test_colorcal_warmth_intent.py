"""Runs tests/intent/colorcal_warmth.json (Zenkai Rules Z1; format pending
t_450535e2). Two observations per example (Z2): what reached darktable (the
FakeBridge's recorded `darkroom.action`) and the sidecar's WB preset after
the move, which no Scouter write path produces (only a `temperature` action
could change it, and none may be sent)."""

from __future__ import annotations

import json
from pathlib import Path

from scouter.core import darkroom, moves
from scouter.errors import ScouterError
from tests.test_wb_colorcal_route import FIXTURE_ILLUMINANT_K, _RouteFixture

EXAMPLES = json.loads((Path(__file__).parent / "colorcal_warmth.json").read_text())["examples"]


class ColorCalWarmthIntentTest(_RouteFixture):
    def run_example(self, ex: dict) -> None:
        inp, want = ex["input"], ex["expect"]
        if inp["temperature_preset"] == 4:
            self.preset_4()
        if inp["illuminant"] != 2 or "temperature_k" in inp:
            self.colorcal(illuminant=inp["illuminant"],
                          temperature=inp.get("temperature_k", FIXTURE_ILLUMINANT_K))
        if not inp["colorcal_pinned"]:
            self.unpin()
        error = None
        result = None
        try:
            if inp["verb"] == "adjust":
                result = moves.adjust.__wrapped__(inp["module"], inp["param"], inp["delta"],
                                                  commit=False, job="job-test")
            elif inp["verb"] == "say":
                moves.say.__wrapped__(inp["phrase"], commit=False, job="job-test")
            else:
                darkroom.set_param.__wrapped__(inp["module"], inp["param"], inp["value"],
                                               commit=False, job="job-test")
        except ScouterError as e:
            error = e
        sent = self.sets()
        if want["outcome"] == "refused":
            self.assertIsNotNone(error, ex["id"])
            self.assertEqual(error.code, want["code"])
            self.assertEqual(sent, [])
            if "fix_mentions" in want:
                self.assertIn(want["fix_mentions"], error.fix)
            return
        self.assertIsNone(error, ex["id"])
        self.assertEqual([s["path"] for s in sent], [want["sent_path"]])
        if "sent_value" in want:
            self.assertEqual(sent[0]["value"], want["sent_value"])
        if "direction" in want and want["sent_path"].startswith("iop/channelmixerrgb"):
            delta = sent[0]["value"] - FIXTURE_ILLUMINANT_K
            self.assertEqual((delta > 0) - (delta < 0), want["direction"])
        if "clamped" in want:
            self.assertEqual(result.clamped, want["clamped"], ex["id"])
        if "wb_preset_after" in want:
            self.assertEqual(darkroom.wb_preset(str(self.raw)), want["wb_preset_after"])


def _make(ex: dict):
    def test(self: ColorCalWarmthIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(ColorCalWarmthIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
