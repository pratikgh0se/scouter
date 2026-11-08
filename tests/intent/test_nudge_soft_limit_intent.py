"""Runs tests/intent/nudge_soft_limit.json (Zenkai Rules Z1; backlog N12).

Two oracles per example (Z2). Oracle A never calls `darkroom.nudge`: the host
measured slider positions recorded in the JSON, with the declared
`vocab.DT_SOFT_BOUNDS`, say whether a GUI step can move from the sidecar
value. Oracle B is a live replay of `darkroom.nudge` through the `FakeBridge`
seam, whose slider position answers come from that same measured table, and
which counts what reached the bridge and whether a commit was awaited.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, vocab
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache

DOC = json.loads((Path(__file__).parent / "nudge_soft_limit.json").read_text())
EXAMPLES = {e["id"]: e for e in DOC["examples"]}
POSITIONS = {float(k): v for k, v in DOC["measured_slider_positions"].items()}


def _measured_position(k: float) -> float:
    """Slider position at `k`: the measured table, linear between its points."""
    pts = sorted(POSITIONS.items())
    if k >= pts[-1][0]:
        return pts[-1][1]
    for (k0, p0), (k1, p1) in zip(pts, pts[1:]):
        if k0 <= k <= k1:
            return p0 + (p1 - p0) * (k - k0) / (k1 - k0)
    return pts[0][1]


class NudgeSoftLimitIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        self.sidecar.write_text((Path(__file__).resolve().parents[1] / "fixtures" /
                                 "P6281348.ORF.xmp").read_text())
        self.value = 0.0
        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "view": "darkroom", **p,
                                          "status": _measured_position(self.value)},
        })
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _replay(self, fx: dict):
        i = fx["input"]
        self.value = i["sidecar_value"]
        state = {"value": self.value, "source": "xmp", "enabled": True}
        with mock.patch("scouter.core.darkroom._read_state", return_value=state), \
                mock.patch("scouter.core.darkroom._read_value", return_value=self.value), \
                mock.patch("scouter.core.darkroom._commit",
                           side_effect=CommitTimeout("no sidecar write")) as commit:
            try:
                result = darkroom.nudge(module=i["module"], param=i["param"], steps=i["steps"])
            except CommitTimeout:
                result = None
        sent = [p.get("effect") for m, p in self.bridge.calls if m == "darkroom.action"
                and p.get("effect") in ("up", "down")]
        return result, sent, commit

    def _oracle_a_cannot_step_up(self, value: float) -> bool:
        _lo, hi = vocab.DT_SOFT_BOUNDS[("channelmixerrgb", "temperature")]
        return value >= hi and _measured_position(value) >= 1.0

    def _check_noop(self, example_id: str) -> None:
        fx = EXAMPLES[example_id]
        self.assertTrue(self._oracle_a_cannot_step_up(fx["input"]["sidecar_value"]))
        result, sent, commit = self._replay(fx)
        self.assertIsNotNone(result, "the nudge timed out instead of answering")
        self.assertEqual(result.noop, fx["expect"]["noop"])
        self.assertEqual(result.committed, fx["expect"]["committed"])
        self.assertEqual(result.applied, fx["expect"]["applied"])
        text = " ".join(result.warnings)
        for needle in fx["expect"]["warning_mentions"]:
            self.assertIn(needle, text)
        self.assertEqual(bool(sent), fx["expect"]["bridge_up_or_down_sent"])
        self.assertEqual(commit.called, fx["expect"]["commit_awaited"])

    def test_nudge_up_at_7000k_is_a_prompt_noop_naming_the_soft_limit(self) -> None:
        self._check_noop("nudge-up-at-7000k-is-a-prompt-noop-naming-the-soft-limit")

    def test_nudge_up_from_9000k_is_the_same_noop(self) -> None:
        self._check_noop("nudge-up-from-9000k-is-the-same-noop")

    def test_nudge_up_below_the_soft_max_is_still_sent(self) -> None:
        fx = EXAMPLES["nudge-up-below-the-soft-max-is-still-sent"]
        self.assertFalse(self._oracle_a_cannot_step_up(fx["input"]["sidecar_value"]))
        result, sent, _commit = self._replay(fx)
        self.assertIsNone(result)  # the double never writes, so the real path times out
        self.assertEqual(sent, ["up"] if fx["expect"]["bridge_up_sent"] else [])


if __name__ == "__main__":
    unittest.main()
