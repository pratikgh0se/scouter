"""Runs tests/intent/nudge_status_not_a_value.json (Zenkai Rules Z1; t_fcf77055).

Two independent oracles per example (Z2): oracle A is the JSON's own recorded
facts about where `status` comes from (the Lua plugin source text and the
phase 1 design spec, quoted -- not re-derived from the fix under test) against
oracle B, a live replay of `darkroom.nudge` through the same `FakeBridge`/
`_commit`-timeout seam `tests/test_darkroom_core.py::CommitReadbackTest`
uses. Oracle A never calls `darkroom.nudge`; oracle B never reads the JSON.
"""

from __future__ import annotations

import json
import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache, with_param_value

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent /
                       "nudge_status_not_a_value.json").read_text())["examples"]}
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "P6281348.ORF.xmp"
LUA_PATH = (Path(__file__).resolve().parents[2] / "darktable_mcp" / "lua" /
            "darktable_mcp.lua")
SPEC_PATH = (Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "specs" /
             "2026-09-13-scouter-phase1-design.md")


class NudgeStatusNotAValueIntentTest(unittest.TestCase):
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

    def _timeout(self) -> CommitTimeout:
        return CommitTimeout("darktable did not write the sidecar in time")

    def _rewrite_exposure_value(self, value: float) -> None:
        self.sidecar.write_text(
            with_param_value(self.sidecar.read_text(), "exposure", "exposure", value))

    def _history_end(self) -> int:
        m = re.search(r'darktable:history_end="(\d+)"', self.sidecar.read_text())
        assert m
        return int(m.group(1))

    def _clone_and_rewrite_exposure(self, value: float) -> None:
        text = self.sidecar.read_text()
        end = self._history_end()
        src = re.search(r'<rdf:li\s(?:(?!/>).)*?darktable:num="11"(?:(?!/>).)*?/>', text, re.S)
        assert src, "fixture exposure entry (num=11) not found"
        item = re.sub(r'darktable:num="\d+"', f'darktable:num="{end}"', src.group(0), count=1)
        close = text.index("</darktable:history>")
        seq_close = text.rindex("</rdf:Seq>", 0, close)
        text = text[:seq_close] + " " + item + "\n    " + text[seq_close:]
        text = re.sub(r'darktable:history_end="\d+"', f'darktable:history_end="{end + 1}"',
                      text, count=1)
        self.sidecar.write_text(with_param_value(text, "exposure", "exposure", value))

    def test_status_echoing_the_request_does_not_confirm_an_unlanded_nudge(self) -> None:
        fx = EXAMPLES["status-echoing-the-request-does-not-confirm-an-unlanded-nudge"]
        # Oracle A: the Lua source's own text -- `darkroom.action` returns
        # `status = value` from `gui_action`, and the design spec says that
        # value is a normalised position, never a real applied value.
        lua_text = LUA_PATH.read_text()
        self.assertIn('status = value', lua_text)
        spec_text = SPEC_PATH.read_text()
        self.assertIn("normalised position", spec_text)
        self.assertIn("never relabelled as a value", spec_text)

        # Oracle B: live replay. `nudge` always sends `value=1` for an
        # up/down step, and the default FakeBridge above echoes it back as
        # `status` -- exactly the coincidence the JSON describes. The real
        # on-disk value is rewritten to 1.0 (matching the echo) but nothing
        # else touches the sidecar, and `_commit` times out.
        self._rewrite_exposure_value(1.0)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module=fx["input"]["module"], param=fx["input"]["param"],
                               steps=fx["input"]["steps"])
        self.assertEqual(fx["expect"]["raises"], "CommitTimeout")

    def test_status_as_a_normalised_position_does_not_confirm_an_unlanded_nudge(self) -> None:
        fx = EXAMPLES["status-as-a-normalised-position-does-not-confirm-an-unlanded-nudge"]
        self._rewrite_exposure_value(0.5)
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False, "status": 0.5, "view": "darkroom", **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module=fx["input"]["module"], param=fx["input"]["param"],
                               steps=fx["input"]["steps"])
        self.assertEqual(fx["expect"]["raises"], "CommitTimeout")

    def test_a_genuine_landed_nudge_after_a_timeout_is_still_reported_applied(self) -> None:
        fx = EXAMPLES["a-genuine-landed-nudge-after-a-timeout-is-still-reported-applied"]

        def _late_flush(*_a, **_kw):
            self._clone_and_rewrite_exposure(9.0)
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = darkroom.nudge(module=fx["input"]["module"], param=fx["input"]["param"],
                                    steps=fx["input"]["steps"])
        self.assertEqual(result.committed, fx["expect"]["committed"])
        self.assertFalse(any("no new sidecar" in w for w in result.warnings), result.warnings)
        self.assertTrue(
            any("AFTER the commit timeout" in w for w in result.warnings), result.warnings)
