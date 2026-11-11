"""Stale-rewrite retry (probe 2026-11-09): darktable sometimes rewrites the sidecar with an
older history right after the view bounce (~6-11% of writes on a long history); the sidecar is
restored, and ONE re-bounce lands the edit (23 of 23 in the probe). `darkroom._commit` retries
exactly once, only when the sidecar was restored, and stays honest when the retry also fails."""

from __future__ import annotations

import unittest
from unittest import mock

from scouter.core import darkroom
from scouter.errors import CommitTimeout
from scouter.types import CommitResult


def _stale(restored: bool = True) -> CommitTimeout:
    return CommitTimeout(
        "stale_rewrite: darktable rewrote the sidecar with an older history",
        detail={"stale_rewrite": "history_end went back from 76 to 54",
                "stale_rewrite_restored": restored, "strategy_used": "view_bounce_action"},
    )


def _ok() -> CommitResult:
    return CommitResult(strategy_used="view_bounce_action", committed=True)


def _run(side_effect):
    with mock.patch("scouter.core.history.commit_expecting", side_effect=side_effect) as m:
        try:
            return m, darkroom._commit("job", 1, expect_module="exposure"), None
        except CommitTimeout as e:
            return m, None, e


class StaleRewriteRetryTest(unittest.TestCase):
    def test_one_retry_lands_the_edit_and_says_so(self) -> None:
        m, result, err = _run([_stale(), _ok()])
        self.assertIsNone(err)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(result.committed)
        self.assertIn("stale", " ".join(result.warnings).lower())

    def test_retry_is_bounded_and_the_refusal_stays_honest(self) -> None:
        m, result, err = _run([_stale(), _stale(), _ok()])
        self.assertIsNone(result)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(err.code, "commit_timeout")
        self.assertTrue(err.detail["stale_rewrite_retried"])
        self.assertIn("stale_rewrite", err.detail)

    def test_no_retry_when_the_sidecar_was_not_restored(self) -> None:
        m, _, err = _run([_stale(restored=False), _ok()])
        self.assertEqual(m.call_count, 1)
        self.assertFalse(err.detail.get("stale_rewrite_retried", False))

    def test_other_timeouts_are_not_retried(self) -> None:
        m, _, err = _run([CommitTimeout("no write", detail={"strategy_used": "x"}), _ok()])
        self.assertEqual(m.call_count, 1)
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
