"""Task 12: the repeat-set cost -- the watermark, `history.changed`, and the
zero-bounce/one-bounce/two-bounce ladder `darkroom.set_param` now climbs.

Unlike `test_darkroom_core.py`, `_commit` is left REAL here (never mocked):
the whole point of this suite is the interaction between `darkroom.set_param`,
`history.commit`'s `attempts`/`expect_change`, and the watermark it writes --
a mocked `_commit` would hide exactly the bug this suite exists to catch.
"""

from __future__ import annotations

import os
import shutil
import time
import unittest
from pathlib import Path

from scouter.core import darkroom, dtbridge, history
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash, with_param_value

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"

#: The fixture's own committed value for exposure.exposure (entry num=11,
#: the latest active one) -- matches test_darkroom_core.py's fixture reads.
FIXTURE_EXPOSURE_VALUE = 1.5500002


def _write_sidecar(
    sidecar: Path,
    original_text: str,
    sent: dict | None = None,
    module: str | None = None,
) -> None:
    time.sleep(0.01)  # guarantee the mtime tick
    text = original_text
    if sent and sent.get("value") is not None:
        # Row 433: a real commit carries the value darktable was sent; a
        # rewrite that kept the old value is a write that did not land.
        text = with_param_value(text, "exposure", "exposure", sent["value"])
    sidecar.write_text(with_new_history_hash(text, module))
    future = time.time() + 2
    os.utime(sidecar, (future, future))


def _commit_handler(sidecar: Path, original_text: str, *, outcome: str = "auto",
                    sent: dict | None = None):
    """A `history.commit` bridge double that behaves like the real plugin
    (task 12's Lua contract; fix round 1, should-fix 6 asked for more than
    one `outcome` so both blockers can be driven through this one double).

    - `"auto"` (default): `expect_change=True` performs a real rewrite +
      mtime bump, so `history.commit`'s own poll loop has something genuine
      to observe (the same trick `test_metrics.py`'s
      `WriteVerbSingleEventTest` uses); `expect_change=False` never
      rewrites, and reports `ok=True, sidecar_written=False` (nothing to
      write, honestly reported, not a failure).
    - `"never_write"`: NEVER rewrites the sidecar, regardless of
      `expect_change` -- `ok` follows the real Lua formula
      (`written != False or not expect_change`), so a genuine change
      (`expect_change=True`) that this one bounce failed to flush reports
      `ok=False`, exactly like a real missed bounce.
    - `"mismatch_then_recover"`: the FIRST call (the mid path's
      `expect_change=False` attempt) deletes the sidecar outright --
      simulating the on-disk state no longer being what the caller
      predicted, so a post-bounce readback cannot possibly confirm it. Every
      call after the first performs the real rewrite (as `"auto"` does for
      `expect_change=True`), so the ESCALATED, `expect_change=True` retry a
      correct fix round 1, blocker 2 succeeds.
    """
    calls = {"n": 0}

    def handler(params: dict) -> dict:
        calls["n"] += 1
        expect_change = params.get("expect_change", True)
        attempts = params.get("attempts", 2)
        written = False
        ok = True
        if outcome == "auto":
            if expect_change:
                _write_sidecar(sidecar, original_text, sent, params.get("expect_module"))
                written = True
        elif outcome == "never_write":
            ok = not expect_change
        elif outcome == "mismatch_then_recover":
            if calls["n"] == 1:
                sidecar.unlink(missing_ok=True)
                ok = not expect_change
            else:
                _write_sidecar(sidecar, original_text, sent, params.get("expect_module"))
                written = True
        else:
            raise AssertionError(f"unknown _commit_handler outcome: {outcome!r}")
        return {
            "strategy_used": "view_bounce",
            "ok": ok,
            "view_after": "darkroom",
            "cpu_ms": 5,
            "sidecar_written": written,
            "attempts_used": attempts,
            "expect_change": expect_change,
        }

    return handler


class HistoryChangedTest(unittest.TestCase):
    def _action(self, p: dict) -> dict:
        self.sent["value"] = p.get("value")
        return {"nan": False, "status": p.get("value"), "view": "darkroom", **p}

    def setUp(self) -> None:
        self.sent: dict = {}
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        self.original_text = self.sidecar.read_text()
        self.bridge = FakeBridge(
            {
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.action": self._action,
                "module.enable": {"nan": False, "view": "darkroom"},
                "history.commit": _commit_handler(
                    self.sidecar, self.original_text, sent=self.sent
                ),
            }
        )
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    # -- E8: a fresh watermark makes a repeat set cost nothing at all -------

    def test_fresh_watermark_noop_makes_zero_write_calls(self) -> None:
        # Warm the watermark: a first set to the value the sidecar already
        # carries. The watermark is absent here, so this call is NOT a
        # shortcut -- it sends the action and commits with expect_change=False
        # (the pre-check matched, but there was nothing to trust it with yet).
        warm = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertTrue(warm.committed)
        # Row 433: darktable wrote nothing and the readback confirmed the
        # value, so the warm-up IS reported as a no-op now -- but it still
        # sent the action (no watermark to shortcut on yet).
        self.assertTrue(warm.noop)
        self.assertEqual(self.bridge.count("darkroom.action"), 1)

        self.bridge.calls.clear()
        result = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertTrue(result.noop)
        self.assertTrue(result.committed)
        self.assertEqual(self.bridge.count("darkroom.action"), 0)
        self.assertEqual(self.bridge.count("history.commit"), 0)

    # -- The stale-but-matching path: one bounce, not two -------------------

    def test_stale_watermark_uses_one_attempt(self) -> None:
        # No watermark at all yet (a fresh session): the pre-check matches
        # the value but cannot trust it, so the write is sent and committed
        # with expect_change=False, attempts=1 -- one bounce, not two.
        result = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        # Row 433: readback-confirmed "nothing to write" is reported noop.
        self.assertTrue(result.noop)
        self.assertEqual(self.bridge.count("darkroom.action"), 1)
        self.assertTrue(result.committed)
        self.assertEqual(self.bridge.count("history.commit"), 1)
        params = [p for m, p in self.bridge.calls if m == "history.commit"][-1]
        self.assertEqual(params["attempts"], 1)
        self.assertIs(params["expect_change"], False)

    # -- A real change is unaffected -----------------------------------------

    def test_real_change_is_unaffected(self) -> None:
        result = darkroom.set_param(module="exposure", param="exposure", value=4.25, job="job-test")
        self.assertFalse(result.noop)
        self.assertTrue(result.committed)
        self.assertEqual(self.bridge.count("history.commit"), 1)
        params = [p for m, p in self.bridge.calls if m == "history.commit"][-1]
        self.assertIs(params["expect_change"], True)
        self.assertEqual(params["attempts"], history.DEFAULT_COMMIT_ATTEMPTS)

    # -- history.changed itself ----------------------------------------------

    def test_no_watermark_is_not_fresh(self) -> None:
        result = history.changed(image=str(self.raw), job="job-test")
        self.assertFalse(result.fresh)
        self.assertIsNotNone(result.reason)
        self.assertIn("no watermark", result.reason or "")

    def test_changed_is_a_pure_read(self) -> None:
        history.changed(image=str(self.raw), job="job-test")
        self.assertEqual(self.bridge.methods_called(), ["image.lookup"])

    # -- A disabled module is never a noop, even with a matching value ------

    def test_disabled_module_is_never_a_noop(self) -> None:
        disabled_text = self.original_text.replace(
            'darktable:num="11"\n'
            '      darktable:operation="exposure"\n'
            '      darktable:enabled="1"',
            'darktable:num="11"\n'
            '      darktable:operation="exposure"\n'
            '      darktable:enabled="0"',
        )
        self.assertNotEqual(disabled_text, self.original_text, "the replace target must exist")
        self.sidecar.write_text(disabled_text)
        # A watermark that matches this (disabled) sidecar exactly -- so
        # freshness alone cannot explain what stops the shortcut below.
        history.watermark_write("job-test", 12, str(self.sidecar))

        result = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertFalse(result.noop, "a disabled module must never take the noop shortcut")
        self.assertEqual(self.bridge.count("darkroom.action"), 1)

    # -- watermark_write also runs after a readback-resolved commit ---------

    def test_watermark_written_after_readback_resolution(self) -> None:
        from unittest import mock

        from scouter.errors import CommitTimeout

        # `exposure` is already enabled in the fixture, so `enable()`'s own
        # readback confirmation succeeds immediately -- `_commit` never has
        # to run for real here, only its readback-resolution wrapper does.
        with mock.patch(
            "scouter.core.darkroom._commit",
            side_effect=CommitTimeout("darktable did not write the sidecar"),
        ):
            result = darkroom.enable(module="exposure", job="job-test")
        self.assertTrue(result.committed)

        fresh, reason = history.watermark_fresh("job-test", 12, str(self.sidecar))
        self.assertTrue(fresh, reason)

    # -- blocker 1: a pending uncommitted write disqualifies the shortcut,
    #    even when the watermark is genuinely fresh -------------------------

    def test_pending_uncommitted_write_disqualifies_the_noop_shortcut_even_with_a_fresh_watermark(
        self,
    ) -> None:
        """Fix round 1, blocker 1, the deeper danger behind the reviewer's
        repro: a watermark that is genuinely fresh (the sidecar has not
        changed since Scouter last committed it) is STILL not enough, if a
        `commit=False` write sits in between -- that write means darktable's
        live history has moved on from what the sidecar (and the watermark)
        show, so trusting the watermark here would silently drop the pending
        edit and re-confirm stale disk state as if nothing had happened.
        """
        # Establish a real, fresh watermark for the fixture's own value.
        warm = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertTrue(warm.committed)
        fresh, _ = history.watermark_fresh("job-test", 12, str(self.sidecar))
        self.assertTrue(fresh, "the warm-up commit must leave a fresh watermark")

        # An uncommitted write: darktable's live history is now 4.25, but the
        # sidecar (and the watermark) still say FIXTURE_EXPOSURE_VALUE.
        darkroom.set_param(module="exposure", param="exposure", value=4.25, commit=False)

        self.bridge.calls.clear()
        result = darkroom.set_param(module="exposure", param="exposure", value=4.25, job="job-test")
        self.assertFalse(result.noop, "a pending uncommitted write must never be a no-op")
        self.assertEqual(self.bridge.count("darkroom.action"), 1, "the write must still bounce")
        self.assertGreaterEqual(self.bridge.count("history.commit"), 1, "and still commit")

    def test_changed_reports_pending_uncommitted_and_is_never_fresh_with_one(self) -> None:
        """Carried finding 7 (task 12): `history.changed` folds
        `pending_uncommitted` in.

        The watermark is a pure FILE signature, so a Scouter write
        journalled with `commit=False` leaves the sidecar byte-identical and
        this verb used to answer `fresh=True` -- exactly when darktable's
        live history had already diverged from disk. A script calls
        `history.changed` to decide whether it can skip a bounce; answering
        "fresh" here sends it down the skip branch precisely when the bounce
        is the correct move.
        """
        warm = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertTrue(warm.committed)
        self.assertTrue(history.changed(str(self.raw), job="job-test").fresh)

        darkroom.set_param(
            module="exposure", param="exposure", value=4.25, commit=False, job="job-test"
        )

        result = history.changed(str(self.raw), job="job-test")
        self.assertEqual(result.pending_uncommitted, ["exposure.exposure"])
        self.assertFalse(result.fresh, "an uncommitted write can never read as fresh")
        self.assertIn("uncommitted", result.reason)
        self.assertIn("history commit", result.reason)
        # Still a pure read: the fold costs a journal read, not a bridge
        # call -- `image.lookup` remains the only method this verb calls.
        self.bridge.calls.clear()
        history.changed(str(self.raw), job="job-test")
        self.assertEqual(self.bridge.methods_called(), ["image.lookup"])

    def test_changed_stays_fresh_once_the_pending_write_is_committed(self) -> None:
        darkroom.set_param(
            module="exposure", param="exposure", value=4.25, commit=False, job="job-test"
        )
        self.assertFalse(history.changed(str(self.raw), job="job-test").fresh)

        committed = darkroom.set_param(
            module="exposure", param="exposure", value=4.25, commit=True, job="job-test"
        )
        self.assertTrue(committed.committed)
        result = history.changed(str(self.raw), job="job-test")
        self.assertEqual(result.pending_uncommitted, [])
        self.assertTrue(result.fresh)

    def test_changed_is_unaffected_by_another_images_pending_write(self) -> None:
        """The fold is per-image: a pending write on image 99 says nothing
        about image 12."""
        darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        from scouter.core import journal

        journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=99,
            path="/elsewhere/other.ORF",
            module="exposure",
            param="exposure",
            instance=0,
            before={},
            commit=False,
        )
        result = history.changed(str(self.raw), job="job-test")
        self.assertEqual(result.pending_uncommitted, [])
        self.assertTrue(result.fresh)

    # -- blocker 2: expect_change=False never certifies freshness on its own -

    def test_expect_change_false_unconfirmed_stamps_no_watermark(self) -> None:
        """Fix round 1, blocker 2, isolated: a bounce that found nothing to
        flush is NOT proof nothing changed -- `history.commit` itself must
        never stamp a watermark from that alone, and must report the
        uncertainty honestly (`changed=False, confirmed=False`)."""
        self.bridge.responses["history.commit"] = _commit_handler(
            self.sidecar, self.original_text, outcome="never_write", sent=self.sent
        )
        result = history.commit(
            image=str(self.raw), job="job-test", attempts=1, expect_change=False
        )
        self.assertFalse(result.changed)
        self.assertFalse(result.confirmed)
        fresh, reason = history.watermark_fresh("job-test", 12, str(self.sidecar))
        self.assertFalse(fresh, "no watermark may be stamped without a confirming readback")
        self.assertIn("no watermark", reason or "")

    def test_expect_change_true_unwritten_still_raises_commit_timeout(self) -> None:
        """The companion case (should-fix 6): a GENUINE change
        (`expect_change=True`) that a bounce fails to flush is still a real
        failure -- `history.commit` must keep raising `commit_timeout`, not
        silently swallow it the way `expect_change=False` now honestly can."""
        from scouter.errors import CommitTimeout

        self.bridge.responses["history.commit"] = _commit_handler(
            self.sidecar, self.original_text, outcome="never_write", sent=self.sent
        )
        with self.assertRaises(CommitTimeout):
            history.commit(
                image=str(self.raw), job="job-test", timeout_s=0.2, expect_change=True
            )

    def test_wrong_prediction_escalates_to_a_real_commit_not_a_false_noop(self) -> None:
        """Fix round 1, blocker 2, end-to-end: the mid path's `expect_change=
        False` bounce reports nothing written, and the post-bounce readback
        does NOT confirm the requested value (the sidecar vanished from under
        it -- standing in for "the prediction was wrong"). `set_param` must
        escalate to a real `expect_change=True` commit rather than ever
        certifying a stale/missing sidecar as fresh, and the escalated commit
        landing for real must still report `committed=True, noop=False`.
        """
        self.bridge.responses["history.commit"] = _commit_handler(
            self.sidecar, self.original_text, outcome="mismatch_then_recover", sent=self.sent
        )
        result = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertTrue(result.committed)
        self.assertFalse(result.noop)
        calls = [p for m, p in self.bridge.calls if m == "history.commit"]
        self.assertEqual(len(calls), 2, "the wrong prediction must escalate to a second commit")
        self.assertIs(calls[0]["expect_change"], False)
        self.assertIs(calls[1]["expect_change"], True)

    # -- nudge's steps=0 shortcut ---------------------------------------------

    def test_nudge_shortcut(self) -> None:
        warm = darkroom.set_param(
            module="exposure", param="exposure", value=FIXTURE_EXPOSURE_VALUE, job="job-test"
        )
        self.assertTrue(warm.committed)

        self.bridge.calls.clear()
        result = darkroom.nudge(module="exposure", param="exposure", steps=0, job="job-test")
        self.assertTrue(result.noop)
        self.assertTrue(result.committed)
        self.assertEqual(self.bridge.count("darkroom.action"), 0)
        self.assertEqual(self.bridge.count("history.commit"), 0)


if __name__ == "__main__":
    unittest.main()
