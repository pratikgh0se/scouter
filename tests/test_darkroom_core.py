import shutil
import unittest
from pathlib import Path
from unittest import mock

from darktable_mcp.bridge.client import BridgeError
from scouter.core import darkroom, dtbridge, journal
from scouter.errors import CommitTimeout, NotInDarkroom, ScouterError, WrongView
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

        # Isolate darkroom.py's own logic from `scouter.core.history` (task 8)
        # by patching the private `_commit` seam it routes every commit
        # through (ruling 21): a recorder, not a real commit, keeps this
        # suite about darkroom.py's behaviour, not history.commit's.
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

    # -- set_param -----------------------------------------------------

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
        self.assertIn("enabled", entry.before)
        self.assertIn("history_end", entry.before)
        self.assertEqual(entry.after, {"value": 2.0})
        self.assertEqual(self.commit_calls, [])

    def test_set_with_commit_routes_through_the_commit_seam(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertEqual(len(self.commit_calls), 1)
        job, image, strategy = self.commit_calls[0]
        self.assertEqual((job, image, strategy), ("job-test", 12, "auto"))

    def test_dry_run_sends_no_action_and_resolves_nothing(self) -> None:
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0,
                                    dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(result.plan[0]["value"], 2.0)
        self.assertIn("no darktable calls", result.reason or "")

    def test_wrong_view_is_raised_before_any_action(self) -> None:
        self.bridge.responses["view.current"] = {"view": "lighttable"}
        with self.assertRaises(WrongView):
            darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertNotIn("darkroom.action", self.bridge.methods_called())

    def test_nan_from_the_bridge_raises_and_leaves_after_null_in_the_journal(self) -> None:
        self.bridge.responses["darkroom.action"] = {"nan": True, "view": "darkroom"}
        with self.assertRaises(NotInDarkroom):
            darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertIsNone(entry.after)
        self.assertEqual(entry.outcome, "error:not_in_darkroom")

    def test_double_set_with_commit_false_journals_from_the_last_write_not_stale_xmp(
        self,
    ) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=3.0, commit=False)
        entries = journal.read_all("job-test")
        self.assertEqual(len(entries), 2)
        # The XMP fixture never actually changes (no real darktable in this
        # test), so a naive re-read of "current value" would show the stale
        # 1.5500002 again; the second write's `before` must be the first
        # write's `after` (2.0) instead (blocker 11).
        self.assertAlmostEqual(entries[-1].before["value"], 2.0, places=5)

    def test_torn_sidecar_retries_then_raises_commit_timeout(self) -> None:
        (Path(str(self.raw) + ".xmp")).write_text("<not well formed", encoding="utf-8")
        with mock.patch("scouter.core.darkroom.time.sleep"):
            with self.assertRaises(CommitTimeout) as ctx:
                darkroom.set_param(module="exposure", param="exposure", value=2.0,
                                   commit=False)
        self.assertIn("torn write", str(ctx.exception))

    # -- nudge -----------------------------------------------------------

    def test_nudge_repeats_the_step_effect(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=-3, commit=False)
        effects = [p["effect"] for m, p in self.bridge.calls if m == "darkroom.action"]
        self.assertEqual(effects, ["down", "down", "down"])
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertEqual(entry.after, {"steps": -3, "steps_landed": 3})

    def test_nudge_mid_loop_failure_leaves_after_null_and_records_the_error(self) -> None:
        calls = {"n": 0}

        def action_response(p: dict) -> dict:
            calls["n"] += 1
            if calls["n"] == 2:
                return {"nan": True, "view": "darkroom", **p}
            return {"nan": False, "status": p.get("value"), "view": "darkroom", **p}

        self.bridge.responses["darkroom.action"] = action_response
        with self.assertRaises(NotInDarkroom):
            darkroom.nudge(module="exposure", param="exposure", steps=3, commit=False)
        self.assertEqual(calls["n"], 2)
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertIsNone(entry.after)
        self.assertEqual(entry.outcome, "error:not_in_darkroom")

    # -- enable / reset ----------------------------------------------------

    def test_reset_without_confirm_is_a_dry_run(self) -> None:
        result = darkroom.reset(module="exposure")
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("discards", result.reason or "")
        self.assertEqual(self.bridge.calls, [])
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

    # -- undo_last -----------------------------------------------------

    def test_undo_last_re_sets_the_previous_value_and_verifies_it(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        result = darkroom.undo_last()
        assert result.undone is not None
        self.assertAlmostEqual(result.undone["restored_to"], 1.5500002, places=5)
        self.assertTrue(result.undone["verified"])
        last = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(last["effect"], "set")
        self.assertAlmostEqual(last["value"], 1.5500002, places=5)
        self.assertIsNone(journal.latest_open("job-test"))
        self.assertEqual(len(self.commit_calls), 1)

    def test_undo_last_verifies_the_journaled_image_is_still_loaded(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)

        def open_response(p: dict) -> dict:
            if p.get("image_id") == 12:
                raise BridgeError("wrong image loaded: expected 12, got 99")
            return {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                    "opened": False, "already_open": True}

        self.bridge.responses["darkroom.open"] = open_response
        with self.assertRaises(NotInDarkroom):
            darkroom.undo_last()
        # The write is still journaled and not marked undone: the aborted
        # undo made no change.
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertFalse(entry.undone)

    def test_undo_last_respects_instance(self) -> None:
        entry = journal.begin(
            "job-test", verb="darkroom.set", image_id=12, path=str(self.raw),
            module="exposure", param="exposure", instance=2,
            before={"source": "xmp", "value": 0.5, "enabled": True, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"value": 1.0})
        darkroom.undo_last()
        last = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(last["instance"], 2)
        self.assertEqual(last["value"], 0.5)

    def test_undo_last_of_enable_disables_when_it_was_disabled_before(self) -> None:
        # The fixture's `exposure` is already enabled, so a real `enable()`
        # call here would be the no-op case (next test); seed the journal
        # directly to exercise the "it really was off before" branch.
        entry = journal.begin(
            "job-test", verb="module.enable", image_id=12, path=str(self.raw),
            module="exposure", param="", instance=0,
            before={"source": "xmp", "enabled": False, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"action": "enable", "enabled": True})
        result = darkroom.undo_last()
        self.assertIn("module.disable", self.bridge.methods_called())
        assert result.undone is not None
        self.assertEqual(len(self.commit_calls), 1)

    def test_undo_last_of_enable_is_a_noop_when_already_enabled_before(self) -> None:
        entry = journal.begin(
            "job-test", verb="module.enable", image_id=12, path=str(self.raw),
            module="exposure", param="", instance=0,
            before={"source": "xmp", "enabled": True, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"action": "enable", "enabled": True})
        result = darkroom.undo_last()
        self.assertNotIn("module.disable", self.bridge.methods_called())
        assert result.undone is not None
        self.assertTrue(result.undone["verified"])

    def test_undo_last_refuses_to_undo_a_reset(self) -> None:
        darkroom.reset(module="exposure", confirm=True)
        with self.assertRaises(ScouterError) as ctx:
            darkroom.undo_last()
        self.assertIn("Ctrl-Z", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "unsupported_undo")

    def test_undo_last_dry_run_still_refuses_a_reset(self) -> None:
        darkroom.reset(module="exposure", confirm=True)
        with self.assertRaises(ScouterError):
            darkroom.undo_last(dry_run=True)

    def test_undo_last_on_an_empty_journal_is_a_dry_run_not_a_crash(self) -> None:
        result = darkroom.undo_last()
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("nothing to undo", result.reason or "")

    # -- status / describe_history --------------------------------------

    def test_status_reports_positions_and_warns_they_are_not_values(self) -> None:
        result = darkroom.status(module="exposure", param="exposure")
        self.assertEqual(result.positions["exposure.exposure"], 0.6429)
        self.assertTrue(any("not parameter values" in w for w in result.warnings))

    def test_describe_history_surfaces_enabled_per_entry_through_real_history_read(
        self,
    ) -> None:
        """Task 8's `history.py` has landed; `describe_history` now runs the
        real `history.read` (ruling 20: every entry keeps its own `enabled`)."""
        result = darkroom.describe_history(image=str(self.raw))
        self.assertEqual(result.image_id, 12)
        self.assertTrue(result.entries, "expected history entries from the fixture")
        exposure = next(e for e in result.entries if e.operation == "exposure")
        self.assertIs(exposure.enabled, True)
        self.assertTrue(all(e.enabled is not None for e in result.entries))


if __name__ == "__main__":
    unittest.main()
