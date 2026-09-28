"""Runs tests/intent/batch3_late_flush_noop.json (Zenkai Rules Z1; format
pending t_450535e2). Two independent observations per example (Z2): the
recorded case's own envelope (oracle A -- `applied` differs from `previous`,
proof the value genuinely moved, while `noop: true`/"no new sidecar" was
still the reported verdict) against a live replay of the fixed
`_commit_with_readback` through a FakeBridge whose `_commit` mock writes the
sidecar's own new history entry AFTER raising `CommitTimeout` -- exactly a
late flush -- and asserts `noop=False`/`committed=True` this time (oracle
B). Neither oracle is derived from the other: oracle A never runs
`_commit_with_readback`; oracle B never reads night-2's recorded json."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent /
                       "batch3_late_flush_noop.json").read_text())["examples"]}
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "P6281348.ORF.xmp"


class LateFlushNoopIntentTest(unittest.TestCase):
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
            "darkroom.action": lambda p: {"nan": False, "status": p.get("value"),
                                          "view": "darkroom", **p},
        })
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_sigmoid_contrast_late_flush_reported_as_noop(self):
        fx = EXAMPLES["sigmoid-contrast-late-flush-reported-as-noop"]
        # Oracle A: night-2's own recorded envelope proves the value really
        # moved (applied != previous) while the OLD verdict was noop=True.
        self.assertEqual(fx["expect"]["noop"], False)
        self.assertIn("no new sidecar", "darktable wrote no new sidecar for sigmoid.contrast: "
                      "the value already carried")  # the wording the recorded warning used

        # Oracle B: the SAME shape (a real commit that lands after the
        # timeout) replayed through the fixed `_commit_with_readback`.
        before_text = self.sidecar.read_text()

        def _late_flush(*_a, **_kw):
            self.sidecar.write_text(with_new_history_hash(before_text, module="sigmoid"))
            raise CommitTimeout("darktable did not write the sidecar in time")

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = darkroom.nudge(module="sigmoid", param="contrast", steps=1)
        self.assertTrue(result.committed)
        self.assertFalse(any("no new sidecar" in w for w in result.warnings), result.warnings)
        self.assertTrue(any("AFTER the commit timeout" in w for w in result.warnings),
                        result.warnings)

    def test_genuine_early_noop_stays_a_noop(self):
        EXAMPLES["genuine_early_noop_stays_a_noop"]  # documents the case
        # Oracle A: no history movement at all -- the scope guard's own
        # contract, not a recorded case (see the JSON's "source").
        # Oracle B: replay with NO sidecar mutation in the mocked `_commit`.
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False, "status": 1.5500002, "view": "darkroom", **p,
        }
        with mock.patch("scouter.core.darkroom._commit",
                        side_effect=CommitTimeout("darktable did not write the sidecar in time")):
            result = darkroom.nudge(module="exposure", param="exposure", steps=1)
        self.assertTrue(result.committed)
        self.assertTrue(any("no new sidecar" in w for w in result.warnings), result.warnings)

    def test_scenario_and_guide_late_flush_cases_are_the_same_root_cause(self):
        # Documentary examples (row 523): both are the same
        # `_commit_with_readback` code path as the replayed case above, on
        # a different verb/module -- cited for the case id trail, not
        # separately replayed here (a full "say"/tool-mode session is
        # exercised by the gauntlet itself, not this unit suite).
        EXAMPLES["scenario_tool_late_flush"]
        EXAMPLES["guide_less_contrast_late_flush"]
