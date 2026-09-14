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
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"

#: The fixture's own committed value for exposure.exposure (entry num=11,
#: the latest active one) -- matches test_darkroom_core.py's fixture reads.
FIXTURE_EXPOSURE_VALUE = 1.5500002


def _commit_handler(sidecar: Path, original_text: str):
    """A `history.commit` bridge double that behaves like the real plugin
    (task 12's Lua contract): `expect_change=False` never rewrites the
    sidecar and still reports `ok=True, sidecar_written=False` (nothing to
    write, honestly reported, not a failure); any other call performs a real
    rewrite + mtime bump, so `history.commit`'s own poll loop has something
    genuine to observe -- the same trick `test_metrics.py`'s
    `WriteVerbSingleEventTest` uses.
    """

    def handler(params: dict) -> dict:
        expect_change = params.get("expect_change", True)
        attempts = params.get("attempts", 2)
        written = False
        if expect_change:
            time.sleep(0.01)  # guarantee the mtime tick
            sidecar.write_text(original_text)
            future = time.time() + 2
            os.utime(sidecar, (future, future))
            written = True
        return {
            "strategy_used": "view_bounce",
            "ok": True,
            "view_after": "darkroom",
            "cpu_ms": 5,
            "sidecar_written": written,
            "attempts_used": attempts,
            "expect_change": expect_change,
        }

    return handler


class HistoryChangedTest(unittest.TestCase):
    def setUp(self) -> None:
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
                "darkroom.action": lambda p: {
                    "nan": False,
                    "status": p.get("value"),
                    "view": "darkroom",
                    **p,
                },
                "module.enable": {"nan": False, "view": "darkroom"},
                "history.commit": _commit_handler(self.sidecar, self.original_text),
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
        self.assertFalse(warm.noop, "the warm-up call has no watermark yet, so it is not a noop")

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
        self.assertFalse(result.noop)
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
