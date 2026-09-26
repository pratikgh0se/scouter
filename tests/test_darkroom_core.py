import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal
from scouter.errors import CommitTimeout, NotInDarkroom, ScouterError, UsageError, WrongView
from scouter.types import CommitResult
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


class _DarkroomFixture(unittest.TestCase):
    """The shared fixture: temp cache, a FakeBridge, and a recording `_commit`.

    Carries no tests of its own -- the suites below inherit it so each one
    gets the fixture without re-running the others' cases.
    """

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURE, str(self.raw) + ".xmp")
        self.bridge = FakeBridge(
            {
                # No "darkroom.open" stub here (fix round 2, blocker 1): none of
                # the verbs under test send it any more (set/nudge/enable/reset
                # read via darkroom.current; undo_last verifies via darkroom.
                # current too). Its absence makes a regression back to
                # force-switch-via-open fail loudly instead of silently passing.
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
                "darkroom.status": {"position": 0.6429, "nan": False, "view": "darkroom"},
                "module.enable": {
                    "module": "exposure",
                    "action": "enable",
                    "nan": False,
                    "view": "darkroom",
                },
                "module.disable": {
                    "module": "exposure",
                    "action": "disable",
                    "nan": False,
                    "view": "darkroom",
                },
                "module.reset": {
                    "module": "exposure",
                    "action": "reset",
                    "nan": False,
                    "view": "darkroom",
                },
            }
        )
        dtbridge.set_bridge(self.bridge)

        # Isolate darkroom.py's own logic from `scouter.core.history` (task 8)
        # by patching the private `_commit` seam it routes every commit
        # through (ruling 21): a recorder, not a real commit, keeps this
        # suite about darkroom.py's behaviour, not history.commit's. The
        # unpatched seam is exercised separately in DarkroomCommitSeamTest
        # (fix round 2, should-fix 8).
        self.commit_calls: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto", attempts=None, expect_change=True):
            self.commit_calls.append((job, image, strategy, attempts, expect_change))
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        self._commit_patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        self._commit_patcher.start()
        self.addCleanup(self._commit_patcher.stop)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()


class DarkroomCoreTest(_DarkroomFixture):
    # -- set_param -----------------------------------------------------

    def test_set_sends_an_absolute_set_and_journals_the_previous_value(self) -> None:
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
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
        job, image, strategy, _attempts, expect_change = self.commit_calls[0]
        self.assertEqual((job, image, strategy), ("job-test", 12, "auto"))
        self.assertTrue(expect_change, "a real change (2.0 != fixture's 1.55) expects a change")
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
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0, dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(result.plan[0]["value"], 2.0)
        self.assertIn("no darktable calls", result.reason or "")

    def test_unpinned_pair_refuses_before_any_bridge_call(self) -> None:
        """Row 408, B8: `action_path(module, param)` used to be evaluated
        only inside the `darkroom.action` call, AFTER `_current_image`,
        `require_darkroom` and the XMP reads -- so an agent's guessed
        (module, param) cost a `darkroom.current` round trip, a
        `view.current` round trip and an XMP read before it was refused. It
        is now computed once, up front, before either the dry-run or the
        real path does anything else."""
        with self.assertRaises(UsageError) as ctx:
            darkroom.set_param(module="colorzones", param="green_hue", value=0.1)
        self.assertEqual(ctx.exception.code, "usage")
        self.assertEqual(self.bridge.calls, [], "no bridge call for an unpinned pair")

    def test_unpinned_pair_refuses_on_a_dry_run_too(self) -> None:
        with self.assertRaises(UsageError):
            darkroom.set_param(module="colorzones", param="green_hue", value=0.1, dry_run=True)
        self.assertEqual(self.bridge.calls, [])

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

    def test_repeat_set_after_an_uncommitted_write_still_bounces_and_commits(self) -> None:
        """Task 12 fix round 1, blocker 1 -- the reviewer's repro: set 4.25
        with `commit=False` (a real, unflushed edit darktable now holds, that
        the sidecar does not reflect yet), then set 4.25 again. The second
        call's raw XMP read still shows the OLD (1.5500002) value -- not 4.25
        -- so even the raw-XMP-only comparison alone would catch this. But
        the deeper point (`pending_uncommitted`) is what stops a value that
        happens to coincidentally match the sidecar from being treated as a
        no-op while a real uncommitted edit is pending: this must always
        bounce and commit for real, never take the zero-bounce shortcut.
        """
        darkroom.set_param(module="exposure", param="exposure", value=4.25, commit=False)
        before_actions = self.bridge.count("darkroom.action")
        result = darkroom.set_param(module="exposure", param="exposure", value=4.25)
        self.assertFalse(result.noop, "a pending uncommitted write must never be a no-op")
        self.assertEqual(
            self.bridge.count("darkroom.action"),
            before_actions + 1,
            "the second set must still bounce (send the action)",
        )
        self.assertEqual(len(self.commit_calls), 1, "the second set must still commit")

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
        darkroom.set_param(module="exposure", param="exposure", value=2.0, instance=1, commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=9.0, instance=0, commit=False)
        entries = journal.read_all("job-test")
        last = entries[-1]
        self.assertEqual(last.instance, 0)
        # If instance 1's entry wrongly fed instance 0, before would be 2.0.
        self.assertAlmostEqual(last.before["value"], 1.5500002, places=5)

    def test_torn_sidecar_retries_then_raises_commit_timeout(self) -> None:
        """A mid-write sidecar is retried a bounded number of times, then
        reported -- never read as a missing value.

        The retry count itself is asserted (final review nit: the old version
        only checked the message, so dropping the retry loop entirely would
        still have passed): one sleep per retry except the last, and the
        refusal names the file so it is actionable.
        """
        sidecar = Path(str(self.raw) + ".xmp")
        sidecar.write_text("<not well formed", encoding="utf-8")
        with mock.patch("scouter.core.darkroom.time.sleep") as sleep:
            with self.assertRaises(CommitTimeout) as ctx:
                darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        self.assertIn("torn write", str(ctx.exception))
        self.assertIn(str(sidecar), str(ctx.exception))
        self.assertEqual(ctx.exception.detail["attempts"], darkroom._TORN_RETRIES)
        self.assertEqual(sleep.call_count, darkroom._TORN_RETRIES - 1)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [darkroom._TORN_SLEEP_S] * (darkroom._TORN_RETRIES - 1),
        )
        # The torn read happens while computing `before`, i.e. BEFORE anything
        # is sent: no action reached darktable and nothing was journalled, so
        # there is no half-recorded write to reconcile later.
        self.assertNotIn("darkroom.action", self.bridge.methods_called())
        self.assertEqual(journal.read_all("job-test"), [])

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

    def test_enable_refuses_an_unpinned_module_before_any_bridge_call(self) -> None:
        """Row 408, B6: `darkroom.enable`/`darkroom.reset` used to send
        `iop/<module>` for ANY string -- an agent that mistypes a module
        name (e.g. "denoise" instead of "denoiseprofile") risks wedging
        darktable's Lua worker for the whole session. The refusal must land
        before `darkroom.current`/`journal.begin`, not just before
        `module.enable` itself."""
        with self.assertRaises(UsageError) as ctx:
            darkroom.enable(module="denoise")
        self.assertEqual(ctx.exception.code, "usage")
        self.assertIn("denoiseprofile", ctx.exception.fix)
        self.assertEqual(self.bridge.calls, [], "no bridge call for an unpinned module")

    def test_reset_refuses_an_unpinned_module_before_any_bridge_call(self) -> None:
        with self.assertRaises(UsageError):
            darkroom.reset(module="bogus-module", confirm=True)
        self.assertEqual(self.bridge.calls, [])

    def test_enable_accepts_every_whitelisted_module(self) -> None:
        for module in dtbridge.MODULE_WHITELIST:
            with self.subTest(module=module):
                result = darkroom.enable(module=module)
                self.assertNotEqual(result.outcome, "error:usage")

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
            "image_id": 99,
            "path": "/other/path.ORF",
            "view": "darkroom",
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
            "job-test",
            verb="darkroom.set",
            image_id=12,
            path=str(self.raw),
            module="exposure",
            param="exposure",
            instance=2,
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
            "job-test",
            verb="module.enable",
            image_id=12,
            path=str(self.raw),
            module="exposure",
            param="",
            instance=0,
            before={"source": "xmp", "enabled": False, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"action": "enable", "enabled": True})
        result = darkroom.undo_last()
        self.assertIn("module.disable", self.bridge.methods_called())
        assert result.undone is not None
        self.assertEqual(len(self.commit_calls), 1)

    def test_undo_last_of_enable_is_a_noop_when_already_enabled_before(self) -> None:
        entry = journal.begin(
            "job-test",
            verb="module.enable",
            image_id=12,
            path=str(self.raw),
            module="exposure",
            param="",
            instance=0,
            before={"source": "xmp", "enabled": True, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"action": "enable", "enabled": True})
        result = darkroom.undo_last()
        self.assertNotIn("module.disable", self.bridge.methods_called())
        assert result.undone is not None
        self.assertTrue(result.undone["verified"])

    def test_undo_last_of_disable_re_enables_when_it_was_enabled_before(self) -> None:
        # Row 437 fix round 1, C1: a `module.disable` entry has no
        # `before.value`, so before this fix it fell through to the
        # generic set-undo branch and raised "was never readable" --
        # leaving the entry as the newest open one and wedging every
        # later `undo_last` in the job. Mirrors the enable test above.
        entry = journal.begin(
            "job-test",
            verb="module.disable",
            image_id=12,
            path=str(self.raw),
            module="exposure",
            param="",
            instance=0,
            before={"source": "xmp", "enabled": True, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"action": "disable", "enabled": False})
        result = darkroom.undo_last()
        self.assertIn("module.enable", self.bridge.methods_called())
        assert result.undone is not None
        self.assertTrue(result.undone["verified"])
        self.assertEqual(len(self.commit_calls), 1)
        # The journal must not be stuck: a second call is a clean "nothing
        # to undo" noop, not a repeat of the same failure.
        second = darkroom.undo_last()
        self.assertEqual(second.outcome, "noop")

    def test_undo_last_of_disable_is_a_noop_when_already_disabled_before(self) -> None:
        entry = journal.begin(
            "job-test",
            verb="module.disable",
            image_id=12,
            path=str(self.raw),
            module="exposure",
            param="",
            instance=0,
            before={"source": "xmp", "enabled": False, "history_end": 14},
        )
        journal.complete("job-test", entry.seq, after={"action": "disable", "enabled": False})
        result = darkroom.undo_last()
        self.assertNotIn("module.enable", self.bridge.methods_called())
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

    def test_undo_last_on_an_empty_journal_is_a_noop_not_a_crash(self) -> None:
        """Row 408, B2: a REAL (non-dry) call with nothing to undo used to
        report `outcome: dry_run` unconditionally -- indistinguishable from
        a genuine preview, and `ok = outcome in ("ok", "dry_run")` counted
        it as success. It is now "noop" (never "ok", never "dry_run") for a
        real call, and still "dry_run" for an actual dry run."""
        result = darkroom.undo_last()
        self.assertEqual(result.outcome, "noop")
        self.assertIn("nothing to undo", result.reason or "")
        # Review round 1, should-fix (row 408 B2): the reason names the
        # REAL cause (a fresh job id per call), not just the symptom.
        self.assertIn("SCOUTER_JOB", result.reason or "")
        self.assertIn("phase 3", result.reason or "")

        dry_result = darkroom.undo_last(dry_run=True)
        self.assertEqual(dry_result.outcome, "dry_run")

    def test_a_real_noop_undo_is_never_reported_ok_by_the_registry(self) -> None:
        from scouter import registry

        envelope = registry.call("darkroom.undo_last", "cli", job="job-test")
        self.assertEqual(envelope.outcome, "noop")
        self.assertFalse(envelope.ok, "a noop undo must never be reported ok")
        from scouter.errors import exit_code_for

        self.assertNotIn(exit_code_for(envelope.outcome), (0, 5))
        # Review round 1, should-fix: a "noop" RESULT (not a raised
        # ScouterError) still gets a structured error.fix over every
        # layer -- MCP has nowhere else to surface an actionable hint.
        self.assertIsNotNone(envelope.error)
        self.assertEqual(envelope.error["code"], "noop")
        self.assertTrue(envelope.error["fix"])
        self.assertIn("SCOUTER_JOB", envelope.error["fix"])

    def test_mcp_layer_also_gets_the_noop_error_fix(self) -> None:
        import json

        from scouter.mcp.server import ScouterServer

        payload = json.loads(
            ScouterServer().call("darkroom_undo_last", {"job": "job-test"})
        )
        self.assertEqual(payload["outcome"], "noop")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "noop")
        self.assertTrue(payload["error"]["fix"])

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


class NonFiniteValueTest(_DarkroomFixture):
    """Should-fix 5: nan/inf is refused as `usage` before any bridge call.

    nan is also the bridge's own "not in the darkroom" signal, so sending one
    makes the answer unreadable; and JSON cannot carry it either.
    """

    def test_a_non_finite_set_value_is_refused_before_the_bridge(self) -> None:
        from scouter.errors import UsageError

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                before = len(self.bridge.calls)
                with self.assertRaises(UsageError) as ctx:
                    darkroom.set_param(module="exposure", param="exposure", value=value)
                self.assertEqual(ctx.exception.code, "usage")
                self.assertEqual(self.bridge.calls[before:], [])
                self.assertEqual(journal.read_all("job-test"), [])

    def test_a_non_finite_nudge_step_count_is_refused_before_the_bridge(self) -> None:
        from scouter.errors import UsageError

        for steps in (float("nan"), float("inf"), 1.5):
            with self.subTest(steps=steps):
                before = len(self.bridge.calls)
                with self.assertRaises(UsageError):
                    darkroom.nudge(module="exposure", param="exposure", steps=steps)
                self.assertEqual(self.bridge.calls[before:], [])

    def test_a_dry_run_refuses_a_non_finite_value_too(self) -> None:
        from scouter.errors import UsageError

        with self.assertRaises(UsageError):
            darkroom.set_param(
                module="exposure", param="exposure", value=float("nan"), dry_run=True
            )


class JobImageGuardTest(_DarkroomFixture):
    """Ruling 30: a write refuses when the darkroom shows a different image.

    `darkroom.current` reports the SELECTION, so "something is loaded" is not
    proof it is the image this job opened. Every write compares the two and
    names both ids when they disagree.
    """

    def _open_and_then_switch_to(self, other_id: int) -> None:
        darkroom.open_image(image=str(self.raw))
        self.bridge.responses["darkroom.current"] = {
            "image_id": other_id,
            "path": "/elsewhere/other.ORF",
            "view": "darkroom",
            "source": "action_images",
        }

    def test_open_records_the_job_image(self) -> None:
        from scouter import paths

        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        darkroom.open_image(image=str(self.raw))
        state = json.loads(paths.job_state_path("job-test").read_text())
        self.assertEqual(state["opened_image_id"], 12)

    def test_set_refuses_when_the_darkroom_shows_another_image(self) -> None:
        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        self._open_and_then_switch_to(77)
        before = len(self.bridge.calls)
        with self.assertRaises(NotInDarkroom) as ctx:
            darkroom.set_param(module="exposure", param="exposure", value=2.0)
        self.assertIn("darkroom.current reports 77, job opened 12", str(ctx.exception))
        # Final review #9: the fix works from both layers (an MCP session's
        # job is pinned per process, so "--job / $SCOUTER_JOB" was not
        # actionable there).
        self.assertIn("MCP darkroom_open", ctx.exception.fix)
        self.assertNotIn("$SCOUTER_JOB", ctx.exception.fix)
        # Refused before any write reached darktable, and before journalling.
        sent = [m for m, _ in self.bridge.calls[before:]]
        self.assertNotIn("darkroom.action", sent)
        self.assertEqual(journal.read_all("job-test"), [])

    def test_nudge_enable_reset_and_undo_refuse_the_same_way(self) -> None:
        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        self._open_and_then_switch_to(77)
        calls = (
            lambda: darkroom.nudge(module="exposure", param="exposure", steps=1),
            lambda: darkroom.enable(module="exposure"),
            lambda: darkroom.reset(module="exposure", confirm=True),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(NotInDarkroom) as ctx:
                    call()
                self.assertIn("job opened 12", str(ctx.exception))

    def test_undo_last_refuses_when_the_darkroom_shows_another_image(self) -> None:
        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        darkroom.open_image(image=str(self.raw))
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        self.bridge.responses["darkroom.current"] = {
            "image_id": 77,
            "path": "/elsewhere/other.ORF",
            "view": "darkroom",
            "source": "action_images",
        }
        with self.assertRaises(NotInDarkroom) as ctx:
            darkroom.undo_last()
        self.assertIn("77", str(ctx.exception))

    def test_a_job_that_never_opened_anything_is_not_second_guessed(self) -> None:
        """No recorded image means nothing to contradict -- the write proceeds."""
        result = darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        self.assertEqual(result.image_id, 12)


class CommitReadbackTest(unittest.TestCase):
    """Should-fix 6: a commit that wrote nothing is resolved by reading back.

    darktable writes a sidecar only when the history actually CHANGES, so a
    write asking for the state the image already has produces no write and the
    poll loop times out -- while the XMP already says what was asked for.
    `set` resolved that; `nudge`, `enable`/`disable` and `undo` did not.
    """

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURE, str(self.raw) + ".xmp")
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
                "module.disable": {"nan": False, "view": "darkroom"},
                "module.reset": {"nan": False, "view": "darkroom"},
                # A commit that never writes the sidecar: the poll loop times out.
                "history.commit": {
                    "strategy_used": "view_bounce",
                    "ok": False,
                    "view_after": "darkroom",
                    "cpu_ms": 5,
                    "detail": "nothing to write",
                },
            }
        )
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _timeout(self) -> CommitTimeout:
        return CommitTimeout("darktable did not write the sidecar in time")

    def test_a_nudge_whose_value_is_already_on_disk_is_committed_honestly(self) -> None:
        # The fixture's exposure is 1.5500002; the bridge reports that as the
        # value the last step landed on, so the XMP already agrees.
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 1.5500002,
            "view": "darkroom",
            **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            result = darkroom.nudge(module="exposure", param="exposure", steps=1)
        self.assertTrue(result.committed)
        self.assertTrue(any("no new sidecar" in w for w in result.warnings))

    def test_a_nudge_that_did_not_land_still_raises_commit_timeout(self) -> None:
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 9.0,
            "view": "darkroom",
            **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_an_enable_of_an_already_enabled_module_is_committed_honestly(self) -> None:
        # The fixture has exposure enabled, which is what `enable` asks for.
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            result = darkroom.enable(module="exposure")
        self.assertTrue(result.committed)
        self.assertTrue(any("no new sidecar" in w for w in result.warnings))

    def test_a_reset_timeout_is_never_dressed_up_as_a_commit(self) -> None:
        """`reset` cannot be confirmed from the XMP, so its timeout propagates."""
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.reset(module="exposure", confirm=True)


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
            sidecar.write_text(with_new_history_hash(sidecar.read_text()))
            future = time.time() + 2
            os.utime(sidecar, (future, future))
            return {
                "strategy_used": "view_bounce",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 5,
            }

        self.bridge = FakeBridge(
            {
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "darkroom.open": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "opened": False,
                    "already_open": True,
                },
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.action": lambda p: {
                    "nan": False,
                    "status": p.get("value"),
                    "view": "darkroom",
                    **p,
                },
                "history.commit": commit_response,
            }
        )
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


class StatusUnpinnedModuleTest(unittest.TestCase):
    """Fix round 1, minor f: `darkroom.status` on an unpinned module refuses
    the same way an unpinned `(module, param)` pair does.

    Before this it returned empty `positions`/`nan` dicts and the generic
    "positions are normalised" warning -- indistinguishable from a module that
    exists and happens to have nothing readable, and the exact opposite of
    what the SAME module gets when a `param` is supplied. One unpinned module,
    one answer.
    """

    def setUp(self) -> None:
        self.bridge = FakeBridge({"view.current": {"view": "darkroom"}})
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_an_unpinned_module_with_no_param_is_a_usage_refusal(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            darkroom.status(module="nosuchmodule")
        self.assertEqual(ctx.exception.code, "usage")
        self.assertIn("nosuchmodule", ctx.exception.message)
        self.assertIn("no parameter of `nosuchmodule` is pinned", ctx.exception.fix)
        self.assertIn("exposure", ctx.exception.fix, "it names what IS pinned")
        # And it costs no `darkroom.status` bridge call: the refusal is local.
        self.assertNotIn("darkroom.status", self.bridge.methods_called())

    def test_the_refusal_matches_the_one_a_pair_gets(self) -> None:
        """Same module, with and without a param -> the same code and the
        same fix. That equality is the point of the fix."""
        with self.assertRaises(UsageError) as without_param:
            darkroom.status(module="nosuchmodule")
        with self.assertRaises(UsageError) as with_param:
            darkroom.status(module="nosuchmodule", param="whatever")
        self.assertEqual(without_param.exception.code, with_param.exception.code)
        self.assertEqual(without_param.exception.fix, with_param.exception.fix)

    def test_a_pinned_module_still_reports_every_pinned_param(self) -> None:
        self.bridge.responses["darkroom.status"] = {"position": 0.5, "nan": False}
        result = darkroom.status(module="exposure")
        self.assertEqual(sorted(result.positions), ["exposure.black", "exposure.exposure"])
        self.assertEqual(result.nan, {"exposure.exposure": False, "exposure.black": False})

    def test_no_module_at_all_still_just_reports_the_view(self) -> None:
        """The `module is None` path is unchanged: a caller asking for
        nothing in particular gets the view and a warning, not a refusal."""
        result = darkroom.status()
        self.assertEqual(result.view, "darkroom")
        self.assertEqual(result.positions, {})
        self.assertTrue(any("only the view" in w for w in result.warnings))
