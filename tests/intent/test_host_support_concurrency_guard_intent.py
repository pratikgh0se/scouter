"""Runs tests/intent/host_support_concurrency_guard.json (Zenkai Rules Z1).
Two oracles per example (Z2): whether `assert_no_concurrent_darktable` raised
at all, and (when it does) that the message names the actual cause."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import host_support as hs

EXAMPLES = json.loads(
    (Path(__file__).parent / "host_support_concurrency_guard.json").read_text()
)["examples"]


class HostSupportConcurrencyGuardIntentTest(unittest.TestCase):
    def run_example(self, ex: dict) -> None:
        with tempfile.TemporaryDirectory() as d:
            cache_dir = Path(d)
            for rel in ex["cache_files"]:
                path = cache_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")

            want = ex["expect"]
            try:
                hs.assert_no_concurrent_darktable(
                    cache_dir=cache_dir,
                    darktable_running=lambda: ex["darktable_running"],
                )
            except RuntimeError as e:
                # Oracle 1: it raised.
                self.assertTrue(want["raises"], f"{ex['id']}: unexpectedly raised: {e}")
                # Oracle 2: the message names the real cause, not a generic refusal.
                if want["message_contains"] is not None:
                    self.assertIn(want["message_contains"], str(e), ex["id"])
            else:
                self.assertFalse(want["raises"], f"{ex['id']}: did not raise but should have")


def _make(ex: dict):
    def test(self: HostSupportConcurrencyGuardIntentTest) -> None:
        self.run_example(ex)
    return test


for _ex in EXAMPLES:
    setattr(HostSupportConcurrencyGuardIntentTest, "test_" + _ex["id"].replace("-", "_"), _make(_ex))
