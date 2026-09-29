"""Runs tests/intent/batch3_late_flush_noop.json (Zenkai Rules Z1; format
pending t_450535e2). Two independent observations per example (Z2): the
recorded case's own envelope facts (oracle A -- the raw `previous`/
`applied`/`requested` numbers and the `noop: true` verdict night-2 actually
recorded, not a re-statement of the fix) against a live replay of the SAME
phrase through the real `darkroom.say` path (oracle B -- `moves.py`'s
`_adjust_core`/`_set_recording_before`, which is `set_param.__wrapped__`,
the path fix round 1 (F1) found every night-2 late-flush case actually took,
not `_commit_with_readback`). Neither oracle is derived from the other:
oracle A never calls `darkroom.say`; oracle B never reads the recorded
json's own verdict."""

from __future__ import annotations

import json
import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, moves, xmp
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache, with_param_value

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent /
                       "batch3_late_flush_noop.json").read_text())["examples"]}
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "P6281348.ORF.xmp"
#: Recorded envelope facts for cov-synonym-more_contrast-punchier-olympus
#: (night2-20260927/cases/, data.moves[0]): previous=1.5, applied/target=1.6,
#: noop=True (wrong), warnings=["darktable wrote no new sidecar ..."].
PUNCHIER_ENVELOPE = {"module": "sigmoid", "param": "contrast", "previous": 1.5,
                     "applied": 1.6000000238418579, "requested": 1.6, "delta": 0.1,
                     "recorded_noop": True,
                     "recorded_warning": "darktable wrote no new sidecar for sigmoid.contrast"}


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
            "module.enable": {"nan": False, "view": "darkroom"},
        })
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _history_end(self, text: str) -> int:
        m = re.search(r'darktable:history_end="(\d+)"', text)
        assert m
        return int(m.group(1))

    def _clone_sigmoid_entry(self, text: str) -> str:
        """A real new top-of-history `sigmoid` entry, cloned from the
        fixture's own newest active one, so `with_param_value` can rewrite
        its contrast with a valid layout/modversion."""
        end = self._history_end(text)
        parsed = xmp.parse(text)
        template = max((e for e in parsed.entries if e.operation == "sigmoid" and e.active),
                       key=lambda e: e.num)
        src = re.search(
            rf'<rdf:li\s(?:(?!/>).)*?darktable:num="{template.num}"(?:(?!/>).)*?/>', text, re.S)
        assert src
        item = re.sub(r'darktable:num="\d+"', f'darktable:num="{end}"', src.group(0), count=1)
        close = text.index("</darktable:history>")
        seq_close = text.rindex("</rdf:Seq>", 0, close)
        text = text[:seq_close] + " " + item + "\n    " + text[seq_close:]
        return re.sub(r'darktable:history_end="\d+"', f'darktable:history_end="{end + 1}"',
                      text, count=1)

    def test_sigmoid_contrast_late_flush_reported_as_noop(self):
        fx = EXAMPLES["sigmoid-contrast-late-flush-reported-as-noop"]
        env = PUNCHIER_ENVELOPE
        # Oracle A: the recorded envelope's own numbers -- the value really
        # moved (applied 1.6 != previous 1.5) while the OLD verdict called
        # it a noop, with the "no new sidecar" text this fix must not
        # produce for a landed edit.
        self.assertNotAlmostEqual(env["applied"], env["previous"], places=3)
        self.assertAlmostEqual(env["applied"], env["requested"], places=3)
        self.assertTrue(env["recorded_noop"])
        self.assertEqual(fx["input"]["case"], "cov-synonym-more_contrast-punchier-olympus")

        # Oracle B: the phrase "punchier" (the SAME synonym the recorded
        # case used) replayed live through `darkroom.say`, with `_commit`
        # mocked to write a real new sigmoid.contrast history entry landing
        # at the target AFTER raising the timeout -- a genuine late flush
        # on the real `set_param` path (F1).
        before_text = self.sidecar.read_text()
        before_params = xmp.latest_params(xmp.parse(before_text), "sigmoid", 0)
        target = round(before_params["contrast"] + 0.1, 6)

        def _late_flush(*_a, **kw):
            # `say`'s own pre-commit bounce also calls `_commit` (with
            # `expect_change=False`) before `_adjust_core` ever reads
            # "previous" -- only the real adjust's commit (`expect_change=
            # True`) should simulate the late flush, or the bounce's own
            # mutation would corrupt the value the adjust reads as its
            # starting point.
            if kw.get("expect_change"):
                landed = self._clone_sigmoid_entry(self.sidecar.read_text())
                landed = with_param_value(landed, "sigmoid", "contrast", target)
                self.sidecar.write_text(landed)
            raise CommitTimeout("darktable did not write the sidecar in time")

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = moves.say("punchier")
        move = result.moves[0]
        self.assertTrue(move["committed"])
        self.assertFalse(move["noop"], "a landed late flush must not be reported as a noop")
        self.assertFalse(any("no new sidecar" in w for w in move["warnings"]), move["warnings"])
        self.assertTrue(any("AFTER the commit timeout" in w for w in move["warnings"]),
                        move["warnings"])

    def test_genuine_early_noop_stays_a_noop(self):
        EXAMPLES["genuine_early_noop_stays_a_noop"]  # documents the case
        # Oracle A: no history movement at all -- the scope guard's own
        # contract, not a recorded case (see the JSON's "source").
        # Oracle B: replay with NO sidecar mutation in the mocked `_commit`.
        # `enable`, not `nudge`: `enable` knows its real target (the module
        # is already enabled in the fixture), so its early-noop confirmation
        # is a genuine XMP readback, unaffected by t_fcf77055 (`nudge` has
        # no known target and no longer resolves an unconfirmed timeout at
        # all -- see `test_a_nudge_whose_status_echoes_the_already_on_disk_
        # value_still_raises` in test_darkroom_core.py).
        with mock.patch("scouter.core.darkroom._commit",
                        side_effect=CommitTimeout("darktable did not write the sidecar in time")):
            result = darkroom.enable(module="exposure")
        self.assertTrue(result.committed)
        self.assertTrue(any("no new sidecar" in w for w in result.warnings), result.warnings)

    def test_scenario_and_guide_late_flush_cases_are_the_same_root_cause(self):
        # Documentary examples (row 523): both are the same `set_param`
        # inline path as the replayed "punchier" case above, on a
        # different verb/module -- cited for the case id trail, not
        # separately replayed here (a full "say"/tool-mode session is
        # exercised by the gauntlet itself, not this unit suite).
        for eid in ("scenario_tool_late_flush", "guide_less_contrast_late_flush"):
            fx = EXAMPLES[eid]
            self.assertEqual(fx["expect"]["noop"], False, eid)
            self.assertEqual(fx["expect"]["committed"], True, eid)
            self.assertIn("late-flush", fx["source"], eid)
