import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal
from scouter.errors import ScouterError, WrongView
from scouter.types import CommitResult
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


class DarkroomCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURE, str(self.raw) + ".xmp")
        self.bridge = FakeBridge({
            "darkroom.open": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                              "opened": False, "already_open": True},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "status": p.get("value"),
                                          "view": "darkroom", **p},
            "darkroom.status": {"position": 0.6429, "nan": False, "view": "darkroom"},
            "module.enable": {"module": "exposure", "action": "enable", "nan": False,
                              "view": "darkroom"},
            "module.disable": {"module": "exposure", "action": "disable", "nan": False,
                               "view": "darkroom"},
            "module.reset": {"module": "exposure", "action": "reset", "nan": False,
                             "view": "darkroom"},
        })
        dtbridge.set_bridge(self.bridge)

        # `scouter.core.history` (task 8) doesn't exist yet. darkroom.py routes
        # every commit through the private `_commit` seam (ruling 21); patch
        # that seam directly rather than a bridge "history.commit" response, so
        # this test suite does not depend on task 8 landing.
        self.commit_calls: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto"):
            self.commit_calls.append((job, image, strategy))
            return CommitResult(committed=True, warnings=[])

        self._commit_patcher = mock.patch(
            "scouter.core.darkroom._commit", side_effect=fake_commit
        )
        self._commit_patcher.start()
        self.addCleanup(self._commit_patcher.stop)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_set_sends_an_absolute_set_and_journals_the_previous_value(self) -> None:
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0,
                                    commit=False)
        self.assertEqual(result.requested, 2.0)
        self.assertAlmostEqual(result.previous or 0, 1.5500002, places=5)
        sent = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(sent["effect"], "set")
        self.assertEqual(sent["path"], "iop/exposure/exposure")
        self.assertEqual(sent["value"], 2.0)
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertAlmostEqual(entry.before["value"], 1.5500002, places=5)
        self.assertEqual(self.commit_calls, [])

    def test_set_with_commit_routes_through_the_commit_seam(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertEqual(len(self.commit_calls), 1)
        job, image, strategy = self.commit_calls[0]
        self.assertEqual((job, image, strategy), ("job-test", 12, "auto"))

    def test_dry_run_sends_no_action(self) -> None:
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0,
                                    dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertNotIn("darkroom.action", self.bridge.methods_called())
        self.assertEqual(result.plan[0]["value"], 2.0)

    def test_wrong_view_is_raised_before_any_action(self) -> None:
        self.bridge.responses["view.current"] = {"view": "lighttable"}
        with self.assertRaises(WrongView):
            darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertNotIn("darkroom.action", self.bridge.methods_called())

    def test_nudge_repeats_the_step_effect(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=-3, commit=False)
        effects = [p["effect"] for m, p in self.bridge.calls if m == "darkroom.action"]
        self.assertEqual(effects, ["down", "down", "down"])

    def test_reset_without_confirm_is_a_dry_run(self) -> None:
        result = darkroom.reset(module="exposure")
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("discards", result.reason or "")
        self.assertNotIn("module.reset", self.bridge.methods_called())
        self.assertEqual(self.commit_calls, [])

    def test_reset_with_confirm_calls_the_bridge(self) -> None:
        darkroom.reset(module="exposure", confirm=True)
        self.assertIn("module.reset", self.bridge.methods_called())
        self.assertEqual(len(self.commit_calls), 1)
        self.assertEqual(self.commit_calls[0][2], "auto")

    def test_enable_has_no_confirm_kwarg(self) -> None:
        """Ruling 9: `darkroom.enable` is not gated and takes no `confirm`."""
        with self.assertRaises(TypeError):
            darkroom.enable(module="exposure", confirm=True)  # type: ignore[call-arg]

    def test_undo_last_re_sets_the_previous_value(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        result = darkroom.undo_last()
        assert result.undone is not None
        self.assertAlmostEqual(result.undone["restored_to"], 1.5500002, places=5)
        last = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(last["effect"], "set")
        self.assertAlmostEqual(last["value"], 1.5500002, places=5)
        self.assertIsNone(journal.latest_open("job-test"))
        self.assertEqual(len(self.commit_calls), 1)

    def test_undo_last_of_enable_disables(self) -> None:
        darkroom.enable(module="exposure")
        darkroom.undo_last()
        self.assertIn("module.disable", self.bridge.methods_called())

    def test_undo_last_refuses_to_undo_a_reset(self) -> None:
        darkroom.reset(module="exposure", confirm=True)
        with self.assertRaises(ScouterError) as ctx:
            darkroom.undo_last()
        self.assertIn("Ctrl-Z", str(ctx.exception))

    def test_undo_last_on_an_empty_journal_is_a_dry_run_not_a_crash(self) -> None:
        result = darkroom.undo_last()
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("nothing to undo", result.reason or "")

    def test_status_reports_positions_and_warns_they_are_not_values(self) -> None:
        result = darkroom.status(module="exposure", param="exposure")
        self.assertEqual(result.positions["exposure.exposure"], 0.6429)
        self.assertTrue(any("not parameter values" in w for w in result.warnings))

    def test_describe_history_raises_cleanly_before_task_8_lands(self) -> None:
        """`history.py` (task 8) doesn't exist yet; `describe_history` still
        imports and registers, and fails loudly rather than silently, per the
        lazy-import seam (ruling 21's pattern applied to the read side too)."""
        with self.assertRaises(ImportError):
            darkroom.describe_history(image=str(self.raw))


if __name__ == "__main__":
    unittest.main()
