"""Product close, item 6: the version says what this is -- a 1.0 release
candidate -- and lands in every event, backup manifest and preset index."""

from __future__ import annotations

import re
import unittest

import scouter


class VersionTest(unittest.TestCase):
    def test_version_is_the_1_0_release_candidate(self) -> None:
        self.assertEqual(scouter.__version__, "1.0rc1")
        # PEP 440 pre-release form, so pip/uv sort it before 1.0.
        self.assertRegex(scouter.__version__, re.compile(r"^\d+\.\d+rc\d+$"))

    def test_phase_stays_an_integer_for_the_metrics_table(self) -> None:
        # metrics.db declares `phase INTEGER NOT NULL`.
        self.assertIsInstance(scouter.PHASE, int)
