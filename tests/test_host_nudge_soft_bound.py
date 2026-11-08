# tests/test_host_nudge_soft_bound.py
"""N12 host proof: a nudge at a GUI soft limit answers promptly and honestly.

Gated: SCOUTER_HOST_TESTS=1 + darktable installed. Run with:

    SCOUTER_HOST_TESTS=1 /Users/pratik/CodeBase/scouter/.venv/bin/python -m pytest -q -s \
        tests/test_host_nudge_soft_bound.py

Isolation as in tests/test_host_colorcal.py: its own `--library` on a scratch copy
of the Olympus hero frame under ~/Pictures/ScouterScratch/colorcal-probe/<run>/.

Two oracles per claim (Z2): the sidecar's illuminant temperature (and its
colour calibration history entry count) against the envelope the product returned.
"""

from __future__ import annotations

import os
import time
import unittest

from tests import host_support as hs
from tests.colorcal_probe_logic import unique_run_dir
from tests.test_host_colorcal import PROBE_ROOT, _Session
from tests.test_host_phase3 import REASON, RUN

#: A prompt answer, far under the 5 s commit timeout the bug cost.
PROMPT_S = 3.0


@unittest.skipUnless(RUN, REASON)
class NudgeSoftBoundHostTest(unittest.TestCase):
    def test_nudge_up_at_and_past_the_soft_max(self) -> None:
        hs.assert_no_concurrent_darktable()
        PROBE_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = unique_run_dir(PROBE_ROOT, os.getpid(), "nudgesoft" + time.strftime("%H%M%S"))
        run_dir.mkdir()
        os.environ.setdefault("SCOUTER_JOB", f"nudge-soft-{os.getpid()}")
        install = hs.PluginInstall()
        install.install()
        try:
            session = _Session(run_dir, "olympus", "A", time.monotonic() + 900)
            session.run(self._work)
        finally:
            install.restore()

    def _work(self, s: _Session) -> None:
        d = s.darkroom

        def temp() -> float:
            return (s.colorcal()[0] or {})["temperature"]

        def put(k: float) -> None:
            d.set_param("channelmixerrgb", "temperature", float(k))
            self.assertAlmostEqual(temp(), k, places=1)

        def nudge(steps: int):
            before_count = s.cc_count()
            t = time.monotonic()
            result = d.nudge("channelmixerrgb", "temperature", steps)
            return result, time.monotonic() - t, before_count

        # at the soft max: prompt noop; sidecar value AND history both untouched
        put(7000.0)
        result, secs, count = nudge(1)
        self.assertTrue(result.noop, result)
        self.assertIn("soft limit", " ".join(result.warnings))
        self.assertLess(secs, PROMPT_S)
        self.assertEqual(temp(), 7000.0)
        self.assertEqual(s.cc_count(), count)
        # past it (set crossed the soft max fine): still the same honest noop
        put(7300.0)
        result, secs, count = nudge(1)
        self.assertTrue(result.noop, result)
        self.assertLess(secs, PROMPT_S)
        self.assertEqual(temp(), 7300.0)
        self.assertEqual(s.cc_count(), count)
        # a real nudge is not swallowed: below the soft max it still lands
        put(6500.0)
        result, _secs, count = nudge(1)
        self.assertFalse(result.noop, result)
        self.assertGreater(temp(), 6500.0)
        # and nudging down from the soft max still lands
        put(7000.0)
        result, _secs, count = nudge(-1)
        self.assertFalse(result.noop, result)
        self.assertLess(temp(), 7000.0)
