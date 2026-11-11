"""Runs tests/intent/nudge_undo_journal.json (Zenkai Rules Z1; t_176d9d85).

Two oracles per example (Z2): a direct read of the journal file / the sidecar
(`xmp.parse`) against the live replay's outcome through `FakeBridge` and a
`_commit`-timeout seam. The replay reads the JSON's `input` only.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal, xmp
from scouter.errors import CommitTimeout, ScouterError
from tests.scouter_fakes import FakeBridge, TempCache, with_param_value

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent / "nudge_undo_journal.json").read_text())["examples"]}
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "P6281348.ORF.xmp"


class NudgeUndoJournalIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "view": "darkroom", **p, "status": 0.5},
        })
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _timeout(self) -> CommitTimeout:
        return CommitTimeout("darktable did not write the sidecar in time")

    def _sidecar_value(self, module: str, param: str) -> float | None:
        return xmp.latest_value(xmp.parse(self.sidecar.read_text()), module, param, 0)

    def test_uncommitted_nudge_then_set_is_not_undoable_to_a_slider_position(self) -> None:
        fx = EXAMPLES["uncommitted-nudge-then-set-is-not-undoable-to-a-slider-position"]
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        # Oracle A: the journal file and the untouched sidecar.
        entries = journal.read_all("job-test")
        self.assertEqual(entries[-2].after.get("value"), fx["expect"]["journal_nudge_after_value"])
        self.assertEqual(entries[-1].before.get("value"), fx["expect"]["journal_set_before_value"])
        self.assertAlmostEqual(self._sidecar_value("exposure", "exposure"),
                               fx["input"]["sidecar_exposure"], places=5)
        # Oracle B: the undo itself refuses and sends no restore of 0.5.
        with self.assertRaises(ScouterError):
            darkroom.undo_last(single=True)
        restores = [p for m, p in self.bridge.calls
                    if m == "darkroom.action" and p.get("effect") == "set"]
        self.assertEqual([p["value"] for p in restores], [2.0])

    def test_nudge_at_a_declared_hard_bound_is_an_honest_noop(self) -> None:
        fx = EXAMPLES["nudge-at-a-declared-hard-bound-is-an-honest-noop"]["input"]
        self.sidecar.write_text(with_param_value(
            self.sidecar.read_text(), fx["module"], fx["param"], fx["sidecar_skew"]))
        before_text = self.sidecar.read_text()
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            result = darkroom.nudge(module=fx["module"], param=fx["param"], steps=fx["steps"])
        # Oracle A: the sidecar bytes are exactly what they were, still at the bound.
        self.assertEqual(self.sidecar.read_text(), before_text)
        self.assertAlmostEqual(self._sidecar_value("sigmoid", "skew"), 1.0, places=5)
        # Oracle B: the result reports a committed noop at that value.
        self.assertTrue(result.noop and result.committed)
        self.assertAlmostEqual(result.applied, 1.0, places=5)

    def test_nudge_timeout_off_a_bound_says_read_and_do_not_retry(self) -> None:
        fx = EXAMPLES["nudge-timeout-off-a-bound-says-read-and-do-not-retry"]
        before_text = self.sidecar.read_text()
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout) as caught:
                darkroom.nudge(module="exposure", param="exposure", steps=1)
        # Oracle A: nothing in the sidecar changed, so there is nothing to retry into.
        self.assertEqual(self.sidecar.read_text(), before_text)
        # Oracle B: the raised fix text carries both instructions.
        for phrase in fx["expect"]["fix_mentions"]:
            self.assertIn(phrase, caught.exception.fix.lower())


if __name__ == "__main__":
    unittest.main()
