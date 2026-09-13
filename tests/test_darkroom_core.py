import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

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
            # No "darkroom.open" stub here (fix round 2, blocker 1): none of
            # the verbs under test send it any more (set/nudge/enable/reset
            # read via darkroom.current; undo_last verifies via darkroom.
            # current too). Its absence makes a regression back to
            # force-switch-via-open fail loudly instead of silently passing.
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
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
        # suite about darkroom.py's behaviour, not history.commit's. The
        # unpatched seam is exercised separately in DarkroomCommitSeamTest
        # (fix round 2, should-fix 8).
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
        self.assertFalse(entry.commit, "commit=False must be recorded on the entry")
        self.assertEqual(self.commit_calls, [])

    def test_set_with_commit_routes_through_the_commit_seam(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertEqual(len(self.commit_calls), 1)
        job, image, strategy = self.commit_calls[0]
        self.assertEqual((job, image, strategy), ("job-test", 12, "auto"))
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertTrue(entry.commit)

    def test_commit_timeout_is_satisfied_by_a_confirming_readback(self) -> None:
        """A set to the value the XMP already carries is committed, not failed.

        Host finding (task 12 fix round 1): darktable writes a sidecar only
        when the history actually CHANGES. Setting a slider to the value it
        already holds adds no history item, so nothing is written and the
        commit times out -- while the sidecar already says exactly what was
        asked for. Re-running the same edit must not report a failure.
        """
        self._commit_patcher.stop()
        with mock.patch(
            "scouter.core.darkroom._commit",
            side_effect=CommitTimeout("darktable did not write the sidecar"),
        ):
            result = darkroom.set_param(module="exposure", param="exposure", value=1.5500002)
        self._commit_patcher.start()
        self.assertTrue(result.committed)
        self.assertTrue(
            any("no history change to write" in w for w in result.warnings), result.warnings
        )

    def test_commit_timeout_still_raises_when_the_readback_disagrees(self) -> None:
        """A write that genuinely never landed is never reported as committed."""
        self._commit_patcher.stop()
        with mock.patch(
            "scouter.core.darkroom._commit",
            side_effect=CommitTimeout("darktable did not write the sidecar"),
        ):
            with self.assertRaises(CommitTimeout):
                darkroom.set_param(module="exposure", param="exposure", value=4.25)
        self._commit_patcher.start()

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
        # latest_open skips a failed entry (should-fix 4); read_all sees it.
        entry = journal.read_all("job-test")[-1]
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
        # write's `after` (2.0) instead -- because the first write's own
        # `commit` flag was False (fix round 2, blocker 2).
        self.assertAlmostEqual(entries[-1].before["value"], 2.0, places=5)

    def test_before_state_ignores_an_undone_entry_set_undo_set(self) -> None:
        """The review's own probe: set 2.0, undo it, set 3.0 -- the third
        write's `before` must be the ORIGINAL 1.55, not the undone write's
        stale `after` of 2.0 (fix round 2, blocker 2)."""
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        darkroom.undo_last()
        darkroom.set_param(module="exposure", param="exposure", value=3.0, commit=False)
        entries = journal.read_all("job-test")
        self.assertAlmostEqual(entries[-1].before["value"], 1.5500002, places=5)

    def test_reset_invalidates_the_commit_false_fallback_for_its_module(self) -> None:
        """Fix round 3, item 2: set(commit=False) -> reset -> set -> undo. The
        reset discarded the module entirely, so the third write's `before`
        must NOT be the first write's stale `after` (2.0) -- `_last_journal_
        write` has to stop at the reset, not walk past it."""
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        darkroom.reset(module="exposure", confirm=True)
        darkroom.set_param(module="exposure", param="exposure", value=5.0, commit=False)
        entries = journal.read_all("job-test")
        last = entries[-1]
        self.assertEqual(last.verb, "darkroom.set")
        self.assertAlmostEqual(last.before["value"], 1.5500002, places=5)
        result = darkroom.undo_last()
        assert result.undone is not None
        self.assertAlmostEqual(result.undone["restored_to"], 1.5500002, places=5)

    def test_before_state_takes_no_fallback_when_the_last_write_committed(self) -> None:
        """Fix round 3, item 3 (discriminator): a write whose OWN `commit`
        flag was True must never feed the fallback, even though its `after`
        exists and a later write is commit=False."""
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=True)
        darkroom.set_param(module="exposure", param="exposure", value=9.0, commit=False)
        entries = journal.read_all("job-test")
        # If the (wrong) fallback fired, before would be 2.0; the honest,
        # committed-write answer is a fresh XMP read (1.5500002 in this
        # static fixture).
        self.assertAlmostEqual(entries[-1].before["value"], 1.5500002, places=5)

    def test_before_state_instance_filter_is_exact(self) -> None:
        """Fix round 3, item 3 (discriminator): an entry for instance 1 must
        not feed instance 0's `before`."""
        darkroom.set_param(module="exposure", param="exposure", value=2.0, instance=1,
                           commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=9.0, instance=0,
                           commit=False)
        entries = journal.read_all("job-test")
        last = entries[-1]
        self.assertEqual(last.instance, 0)
        # If instance 1's entry wrongly fed instance 0, before would be 2.0.
        self.assertAlmostEqual(last.before["value"], 1.5500002, places=5)

    def test_torn_sidecar_retries_then_raises_commit_timeout(self) -> None:
        (Path(str(self.raw) + ".xmp")).write_text("<not well formed", encoding="utf-8")
        with mock.patch("scouter.core.darkroom.time.sleep"):
            with self.assertRaises(CommitTimeout) as ctx:
                darkroom.set_param(module="exposure", param="exposure", value=2.0,
                                   commit=False)
        self.assertIn("torn write", str(ctx.exception))

    # -- nudge -----------------------------------------------------------

    def test_nudge_repeats_the_step_effect_and_records_the_landed_value(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=-3, commit=False)
        effects = [p["effect"] for m, p in self.bridge.calls if m == "darkroom.action"]
        self.assertEqual(effects, ["down", "down", "down"])
        entry = journal.latest_open("job-test")
        assert entry is not None
        # `after.value` comes from the bridge's own `status` on the last
        # action, not a guess (fix round 2, blocker 6) -- required so a
        # subsequent commit=False write can fall back to it.
        self.assertEqual(entry.after, {"steps": -3, "steps_landed": 3, "value": 1.0})

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
        # latest_open skips a failed entry (should-fix 4); read_all sees it.
        entry = journal.read_all("job-test")[-1]
        self.assertIsNone(entry.after)
        self.assertEqual(entry.outcome, "error:not_in_darkroom")

    def test_a_nudge_is_a_valid_fallback_source_for_a_later_commit_false_set(self) -> None:
        """Blocker 6's point: a nudge's `after.value` must be usable by
        `_last_journal_write`, the same as a `set`'s."""
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=5.0, commit=False)
        entries = journal.read_all("job-test")
        # The nudge's FakeBridge `status` echoes the requested step value (1),
        # not a real EV -- what matters here is that the set's `before` came
        # from the nudge's `after`, not a fresh (stale) XMP read of 1.55.
        self.assertEqual(entries[-1].before["value"], 1.0)

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
        self.assertEqual(result.warnings, [])
        last = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(last["effect"], "set")
        self.assertAlmostEqual(last["value"], 1.5500002, places=5)
        self.assertIsNone(journal.latest_open("job-test"))
        self.assertEqual(len(self.commit_calls), 1)

    def test_undo_last_refuses_when_another_image_is_loaded(self) -> None:
        """Blocker 1: undo_last reads `darkroom.current` and REFUSES on a
        mismatch, rather than force-switching via `darkroom.open`."""
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        self.bridge.responses["darkroom.current"] = {
            "image_id": 99, "path": "/other/path.ORF", "view": "darkroom",
            "source": "action_images",
        }
        with self.assertRaises(NotInDarkroom) as ctx:
            darkroom.undo_last()
        self.assertIn("another image is loaded: 99", str(ctx.exception))
        # The write is still journaled and not marked undone: the refused
        # undo made no change.
        entry = journal.latest_open("job-test")
        assert entry is not None
        self.assertFalse(entry.undone)

    def test_undo_last_refuses_when_nothing_is_loaded(self) -> None:
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        self.bridge.responses["darkroom.current"] = {"view": "lighttable"}
        with self.assertRaises(NotInDarkroom) as ctx:
            darkroom.undo_last()
        self.assertIn("no image is loaded", str(ctx.exception))

    def test_undo_last_respects_instance(self) -> None:
        entry = journal.begin(
            "job-test", verb="darkroom.set", image_id=12, path=str(self.raw),
            module="exposure", param="exposure", instance=2,
            before={"source": "xmp", "value": 0.5, "enabled": True, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"value": 1.0})
        result = darkroom.undo_last()
        last = [p for m, p in self.bridge.calls if m == "darkroom.action"][-1]
        self.assertEqual(last["instance"], 2)
        self.assertEqual(last["value"], 0.5)
        # The fixture has no history entry at multi_priority=2, so the
        # post-restore XMP read genuinely cannot confirm 0.5 -- verified must
        # be False, and that must show up as a warning, not fail silently
        # (fix round 2, should-fix 7).
        assert result.undone is not None
        self.assertFalse(result.undone["verified"])
        self.assertTrue(any("could not verify" in w for w in result.warnings))

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

    def test_undo_last_dry_run_on_a_reset_entry_is_pure(self) -> None:
        """Fix round 3, item 1: a dry run must not raise and must not mutate
        the journal -- it just reports that the entry cannot be undone."""
        darkroom.reset(module="exposure", confirm=True)
        result = darkroom.undo_last(dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("cannot undo the reset", result.reason or "")
        entry = journal.read_all("job-test")[-1]
        self.assertEqual(entry.verb, "module.reset")
        self.assertFalse(entry.undone, "a dry run must not mark the entry undone")
        self.assertIsNone(entry.note)
        # A REAL call afterwards still refuses (and does mutate) -- the dry
        # run above changed nothing.
        with self.assertRaises(ScouterError):
            darkroom.undo_last()
        self.assertTrue(journal.read_all("job-test")[-1].undone)

    def test_undo_last_reset_refusal_marks_the_entry_so_older_writes_stay_reachable(
        self,
    ) -> None:
        """Should-fix 5: the reset refusal marks that entry `undone` (with a
        note) so it is not asked about again -- the write underneath it is
        reachable on the next `undo_last` call."""
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        darkroom.reset(module="exposure", confirm=True)
        with self.assertRaises(ScouterError):
            darkroom.undo_last()
        entries = journal.read_all("job-test")
        reset_entry = next(e for e in entries if e.verb == "module.reset")
        self.assertTrue(reset_entry.undone)
        self.assertEqual(reset_entry.note, "skipped: reset is not undoable")

        result = darkroom.undo_last()
        assert result.undone is not None
        self.assertEqual(result.undone["verb"], "darkroom.set")

    def test_undo_last_on_an_empty_journal_is_a_dry_run_not_a_crash(self) -> None:
        result = darkroom.undo_last()
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("nothing to undo", result.reason or "")

    def test_undo_last_names_the_failed_entry_when_that_is_all_there_is(self) -> None:
        """Should-fix 4: a journal that is not empty, but whose only entry
        failed, is a different (more informative) case than a truly empty one.
        Fix round 3, nit: a real (non-dry-run) call reports outcome "error"
        for this, not "dry_run" -- there is no plan to preview."""
        self.bridge.responses["darkroom.action"] = {"nan": True, "view": "darkroom"}
        with self.assertRaises(NotInDarkroom):
            darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        result = darkroom.undo_last()
        self.assertEqual(result.outcome, "error")
        self.assertIn("failed", result.reason or "")
        self.assertNotIn("journal for job-test is empty", result.reason or "")

        dry_result = darkroom.undo_last(dry_run=True)
        self.assertEqual(dry_result.outcome, "dry_run")

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


class DarkroomCommitSeamTest(unittest.TestCase):
    """Exercises `darkroom._commit` UNPATCHED: the real `history.commit`
    (task 8) against a FakeBridge whose response makes the sidecar's mtime
    genuinely advance, so `history.commit`'s own poll loop succeeds for real
    (fix round 2, should-fix 8). No `darkroom._commit` mock anywhere here.
    """

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURE, str(self.raw) + ".xmp")

        def commit_response(p: dict) -> dict:
            # Simulate darktable writing the sidecar on commit: bump its
            # mtime forward so history.commit's poll loop sees it advance.
            sidecar = Path(str(self.raw) + ".xmp")
            future = time.time() + 2
            os.utime(sidecar, (future, future))
            return {"strategy_used": "view_bounce", "ok": True, "view_after": "darkroom",
                    "cpu_ms": 5}

        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "darkroom.open": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                              "opened": False, "already_open": True},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "status": p.get("value"),
                                          "view": "darkroom", **p},
            "history.commit": commit_response,
        })
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_commit_seam_calls_the_real_history_commit_and_verifies_from_disk(self) -> None:
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertIn("history.commit", self.bridge.methods_called())
        self.assertTrue(result.committed)


if __name__ == "__main__":
    unittest.main()
