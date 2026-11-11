"""Runs tests/intent/host_probe_library_isolation.json (Zenkai Z1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import host_support as hs

EXAMPLES = json.loads(
    (Path(__file__).parent / "host_probe_library_isolation.json").read_text()
)["examples"]


class HostProbeLibraryIsolationIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        with tempfile.TemporaryDirectory() as d:
            scratch = Path(d) / "ScouterScratch"
            real = Path(d) / "dot-config" / "library.db"
            scratch.mkdir()
            library = Path(ex["library"].format(scratch=scratch, real=real))
            want = ex["expect"]
            try:
                got = hs.assert_isolated_library(library, scratch=scratch, real=real)
            except ValueError as e:
                self.assertTrue(want["raises"], f"{ex['id']}: unexpectedly raised: {e}")
                self.assertIn(want["message_contains"], str(e), ex["id"])
                return
            self.assertFalse(want["raises"], f"{ex['id']}: did not raise but should have")
            self.assertEqual(got, library.resolve())


def _make(ex: dict):
    def test(self: HostProbeLibraryIsolationIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(HostProbeLibraryIsolationIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
