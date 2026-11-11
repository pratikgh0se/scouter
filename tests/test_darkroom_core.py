import json
import os
import re
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal, vocab, xmp
from scouter.errors import (
    CommitTimeout,
    NotInDarkroom,
    ScouterError,
    UsageError,
    WriteUnverified,
    WrongView,
)
from scouter.types import CommitResult
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash, with_param_value

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"
HOST_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "phase3-host.xmp"


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
        self.sent: dict = {}
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
                "darkroom.action": self._action,
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
        # Row 461 (review Important 2): what each commit was told to expect.
        self.commit_modules: list[str | None] = []

        def fake_commit(
            job, image, *, strategy="auto", attempts=None, expect_change=True, expect_module=None
        ):
            self.commit_calls.append((job, image, strategy, attempts, expect_change))
            self._land_sent()
            self.commit_modules.append(expect_module)
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        self._commit_patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        self._commit_patcher.start()
        self.addCleanup(self._commit_patcher.stop)

    def _action(self, p: dict) -> dict:
        # Remember what darktable was sent, per slider, so a commit can land
        # it in the sidecar the way darktable would (row 433).
        self.sent.clear()
        key = next((k for k, v in dtbridge.ACTION_PATHS.items() if v == p.get("path")), None)
        if p.get("effect") == "set" and key is not None:
            self.sent[key] = p.get("value")
        return {"nan": False, "status": p.get("value"), "view": "darkroom", **p}

    def _land_sent(self) -> None:
        """A committed set lands in the sidecar (row 433): without this the
        fixture sidecar keeps its old value and the write is, correctly,
        `write_unverified`."""
        last = self.bridge.calls[-1][0] if self.bridge.calls else None
        if last != "darkroom.action":
            self.sent.clear()
            return
        sidecar = Path(str(self.raw) + ".xmp")
        text = sidecar.read_text()
        for (module, param), value in self.sent.items():
            if module in ("exposure", "sigmoid") and value is not None:
                text = with_param_value(text, module, param, value)
        sidecar.write_text(text)
        self.sent.clear()

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
        self.assertEqual(self.commit_modules, ["exposure"], "row 434: the commit expects exposure")
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

    def test_commit_timeout_for_a_real_change_off_the_declared_default_still_raises(self) -> None:
        """Batch 3 item 9 (row 531), fix round 1 (reviewer finding 1);
        updated for card t_29a9ef47 (rows 548/549) after the host probe
        promoted `colorzones.strength`'s `default` from `default_candidate`
        to `0.0` (`tests/test_host_phase3.py::
        ProbeColorzonesStrengthDefaultHostTest`, host run 2026-09-29).
        `colorzones.strength` now HAS a declared default, but 37.0 is not
        it -- `_effective_current` reads `current=0.0` (the declared
        default, since the fixture has no active colorzones history entry),
        so `matched_before = _close(37.0, 0.0)` is still False, and neither
        `matched_before` nor the post-timeout XMP readback can confirm a
        no-op for a request that far off the default.

        This is a NEGATIVE proof, not the positive one fix round 0 wrongly
        shipped: `FakeBridge._action` (this file, `_action`) unconditionally
        echoes `status = value` for every `set`, exactly like darktable's
        own `dt.gui.action` echoes back a GUI report AFTER the write (spec
        `docs/superpowers/specs/2026-09-13-scouter-phase1-design.md` §7.3:
        "never relabelled as a value"; `darktable_mcp/lua/darktable_mcp.lua`
        calls it a normalised slider position). An echoing status therefore
        proves nothing about what the sidecar holds -- a `set` to 37 (well
        past `colorzones.strength`'s own `hi=2.0`, and NOT its default)
        that never lands (sidecar genuinely unchanged, exactly the proof3
        shape) must still raise `commit_timeout`, the same as
        `test_commit_timeout_still_raises_when_the_readback_disagrees`
        does for `exposure`. Fix round 0 (commit ed903f3) trusted that echo
        as an oracle and returned `committed=True` for this exact call --
        the row 430/461 failure class. This must pass on main and on this
        branch, and fail (raise nothing / return committed) against
        ed903f3."""
        self.assertEqual(darkroom._declared_default("colorzones", "strength"), 0.0)
        self._commit_patcher.stop()
        with mock.patch(
            "scouter.core.darkroom._commit",
            side_effect=CommitTimeout("darktable did not write the sidecar"),
        ):
            with self.assertRaises(CommitTimeout):
                darkroom.set_param(module="colorzones", param="strength", value=37.0)
        self._commit_patcher.start()

    def test_commit_timeout_on_a_row_with_no_declared_default_still_raises(self) -> None:
        """The status-oracle guard (review finding 2 on 9ae3978). The test
        above lost it when colorzones.strength got a declared default:
        `_effective_current` now answers 0.0 there, so ed903f3's
        `current is None` branch that trusted the echoed status is never
        reached. This row is picked for having NO declared default and no
        history entry in the fixture, so the echo is all there is to go on,
        and it must not confirm anything. If a later promotion gives this
        row a default, move the test to another row that still has none."""
        self.assertIsNone(darkroom._declared_default("filmicrgb", "contrast"))
        self._commit_patcher.stop()
        with mock.patch(
            "scouter.core.darkroom._commit",
            side_effect=CommitTimeout("darktable did not write the sidecar"),
        ):
            with self.assertRaises(CommitTimeout):
                darkroom.set_param(module="filmicrgb", param="contrast", value=1.3)
        self._commit_patcher.start()

    def test_commit_timeout_for_the_declared_default_resolves_as_a_noop(self) -> None:
        """Card t_29a9ef47 (decision rows 548/549): the positive case the
        promotion above was FOR. The two P0 gauntlet cases this closes
        (`cov-colorzones.strength-{olympus,sony_a7}-set_lo`) are exactly
        this shape: `darkroom.set colorzones.strength 0.0` on a frame with
        no colorzones history entry (this suite's real fixture,
        `P6281348.ORF.xmp`, matches the olympus case's frame) -- darktable
        already holds 0.0 (its own default), so the write lands no new
        history item and the commit times out for real, exactly like
        `test_commit_timeout_is_satisfied_by_a_confirming_readback` above
        for `exposure`.

        Before the promotion (`default_candidate` only, `default=None`),
        `_effective_current` returned `(None, "xmp")` for this exact call --
        RED on 52c1d14 (pre-promotion): this test raises `CommitTimeout`
        there, the same failure `cov-colorzones.strength-*-set_lo` hit on
        the host. After promotion, `_effective_current` reads
        `current=0.0, source="default"` (no code in `darkroom.py` changed
        to get there -- only `vocab.py`'s declared `default`), so
        `matched_before = _close(0.0, 0.0)` is True and the post-timeout
        readback (which ALSO reads the declared default, since the
        fixture's sidecar still has no colorzones entry) confirms it: GREEN.
        No `dt.gui.action` status oracle is involved anywhere in this path
        (`FakeBridge._action`'s echo is asserted meaningless in the sibling
        test above; this test does not even need it to matter, since the
        no-op resolves before any echo is consulted)."""
        self._commit_patcher.stop()
        with mock.patch(
            "scouter.core.darkroom._commit",
            side_effect=CommitTimeout("darktable did not write the sidecar"),
        ):
            result = darkroom.set_param(module="colorzones", param="strength", value=0.0)
        self._commit_patcher.start()
        self.assertTrue(result.committed)
        self.assertTrue(result.noop)
        self.assertTrue(
            any("no history change to write" in w for w in result.warnings), result.warnings
        )

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
        # The committed 2.0 landed in the sidecar (row 433 double); put the
        # fixture's 1.5500002 back so the honest XMP read and the (wrong)
        # journal fallback give different answers.
        shutil.copy(FIXTURE, str(self.raw) + ".xmp")
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
        # `status` is a slider position, never a value (t_176d9d85): the
        # key stays (pending commit=False detection) with the value unknown.
        self.assertEqual(
            entry.after, {"steps": -3, "steps_landed": 3, "value": None, "value_unknown": True}
        )

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
        """Blocker 6's point, restated (t_176d9d85): a nudge is still seen by
        `_last_journal_write` (so the set's `before` is not the stale XMP
        read of 1.55), but its value is unknown, never the bridge `status`."""
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=5.0, commit=False)
        entries = journal.read_all("job-test")
        self.assertIsNone(entries[-1].before["value"])
        self.assertTrue(entries[-1].before["value_unknown"])

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
                self.commit_modules.clear()
                result = darkroom.enable(module=module)
                self.assertNotEqual(result.outcome, "error:usage")
                self.assertTrue(
                    set(self.commit_modules) <= {module},
                    f"enable commits expect {module}: {self.commit_modules}",
                )

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
        self.assertEqual(self.commit_modules, ["exposure"], "the undo commit expects exposure")

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
        self.assertEqual(self.commit_modules, ["exposure"], "the disable commit expects exposure")

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
        # Row 461 (review I3): the commit must expect THIS module, not go
        # in blind -- a behavioural oracle alongside CommitWiringTest's
        # static AST check that the call site passes expect_module at all.
        self.assertEqual(self.commit_modules[-1], "exposure")
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

        payload = json.loads(ScouterServer().call("darkroom_undo_last", {"job": "job-test"}))
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


class OpenPathGuardTest(_DarkroomFixture):
    """(night1-robustness-triage.md item 4, rob-r11): `darktable.log` showed a
    `darkroom.open` on a path that does not exist reach `dt.database.import`
    and never answer -- the Lua worker wedged for ~5.8 minutes with no typed
    reply at all, and 4 later cases (r12/r13/r14/r15) queued behind it and
    inherited the same wedge. A path `open_image` cannot possibly load is
    refused before it ever reaches the bridge."""

    def test_a_missing_path_is_refused_before_the_bridge(self) -> None:
        missing = str(self.raw.parent / "does-not-exist.ORF")
        before = len(self.bridge.calls)
        with self.assertRaises(UsageError) as ctx:
            darkroom.open_image(image=missing)
        self.assertEqual(ctx.exception.detail["reason"], "not_found")
        self.assertEqual(self.bridge.calls[before:], [])

    def test_an_unsupported_extension_is_refused_before_the_bridge(self) -> None:
        notes = self.raw.parent / "notes.txt"
        notes.write_text("not an image")
        before = len(self.bridge.calls)
        with self.assertRaises(UsageError) as ctx:
            darkroom.open_image(image=str(notes))
        self.assertEqual(ctx.exception.detail["reason"], "unsupported_format")
        self.assertEqual(self.bridge.calls[before:], [])

    def test_a_real_raw_path_still_opens_normally(self) -> None:
        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        result = darkroom.open_image(image=str(self.raw))
        self.assertEqual(result.image_id, 12)

    def test_an_image_id_is_never_filesystem_checked(self) -> None:
        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        result = darkroom.open_image(image=12)
        self.assertEqual(result.image_id, 12)

    def test_a_directory_is_refused_before_the_bridge(self) -> None:
        """(fix round 1 review H1/L3): a directory passing the old guard
        still reached `dt.database.import` -- an untested wedge candidate."""
        d = self.raw.parent / "a-directory"
        d.mkdir()
        before = len(self.bridge.calls)
        with self.assertRaises(UsageError) as ctx:
            darkroom.open_image(image=str(d))
        self.assertEqual(ctx.exception.detail["reason"], "is_directory")
        self.assertEqual(self.bridge.calls[before:], [])

    def test_a_pfm_chart_frame_reaches_the_bridge(self) -> None:
        """(fix round 1 review H1, blocker): the gauntlet's own looks-layer
        chart frame is `chart-v1.pfm` (`tests/gauntlet/session.py`) -- about
        235 night-1 cases open it. A whitelist that does not name `.pfm` (or
        any other format darktable reads that the whitelist never listed)
        blocks the whole layer; the denylist never refuses a format it does
        not explicitly name."""
        pfm = self.raw.parent / "chart-v1.pfm"
        pfm.write_bytes(b"PF\n1 1\n-1.0\n\x00\x00\x80?\x00\x00\x80?\x00\x00\x80?")
        self.bridge.responses["darkroom.open"] = {
            "image_id": 99,
            "path": str(pfm),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        before = len(self.bridge.calls)
        result = darkroom.open_image(image=str(pfm))
        self.assertEqual(result.image_id, 99)
        self.assertGreater(len(self.bridge.calls), before)

    def test_a_symlink_with_no_extension_to_a_raw_reaches_the_bridge(self) -> None:
        """(fix round 1 review H1): the suffix is read from the RESOLVED
        path, so a symlink with no extension of its own is judged on the
        raw it points at, not refused for lacking a suffix itself."""
        link = self.raw.parent / "no-extension-link"
        link.symlink_to(self.raw)
        self.bridge.responses["darkroom.open"] = {
            "image_id": 12,
            "path": str(self.raw),
            "view": "darkroom",
            "opened": True,
            "already_open": False,
        }
        before = len(self.bridge.calls)
        result = darkroom.open_image(image=str(link))
        self.assertEqual(result.image_id, 12)
        self.assertGreater(len(self.bridge.calls), before)

    def test_an_over_long_path_component_is_a_typed_usage_error(self) -> None:
        """(fix round 1 review M4): `Path.exists()` re-raises `ENAMETOOLONG`
        for a component over `NAME_MAX` (the r11 shape, exactly 255 'x', is
        fine -- this is one character further). A raw `OSError` escaping
        here would crash the case with an untyped harness error instead of
        the typed refusal every other unopenable path gets."""
        too_long = str(self.raw.parent / ("x" * 256 + ".ORF"))
        before = len(self.bridge.calls)
        with self.assertRaises(UsageError) as ctx:
            darkroom.open_image(image=too_long)
        self.assertEqual(ctx.exception.detail["reason"], "not_found")
        self.assertEqual(self.bridge.calls[before:], [])


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

    def test_a_nudge_whose_status_echoes_the_already_on_disk_value_still_raises(self) -> None:
        """t_fcf77055: `status` is a post-write GUI echo, never a value
        (design 7.3) -- so a status that happens to equal the fixture's own
        `exposure.exposure` (1.5500002) is not evidence the step landed, it
        is a coincidence. `nudge` no longer trusts `status` at all: with no
        real movement on disk, a commit timeout must raise, not be dressed
        up as an honest noop. Formerly `..._is_committed_honestly` -- that
        was the false-success bug this card diagnosed and fixed."""
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 1.5500002,
            "view": "darkroom",
            **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

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

    def _rewrite_exposure_value(self, value: float) -> None:
        sidecar = self._sidecar_path()
        sidecar.write_text(with_param_value(sidecar.read_text(), "exposure", "exposure", value))

    def test_status_echoing_the_request_must_not_falsely_confirm_an_unlanded_nudge(self) -> None:
        """Diagnosis (t_fcf77055): the Lua plugin's `darkroom.action` `status`
        is a post-write GUI echo, not the param's real absolute value (design
        7.3; `darktable_mcp/lua/darktable_mcp.lua` 1657-1668). `nudge` always
        sends `value=1` for an up/down step, so a bridge that (honestly, per
        the default `FakeBridge` in this module) echoes the request back as
        `status` reports 1.0 -- with no relationship at all to
        `exposure.exposure`'s real units. The fixture is rewritten so the
        REAL on-disk value is 1.0 too, purely by coincidence, and `_commit`
        is mocked to time out with no other side effect: the sidecar never
        actually changes. Honest behaviour is a `CommitTimeout` (nothing on
        disk confirms a landed write); deciding success from the status
        echo instead reports `committed=True` for a nudge that never
        touched the sidecar."""
        self._rewrite_exposure_value(1.0)
        # Bridge default already echoes the request (`status: p.get("value")`,
        # set in `setUp`), and nudge's own bridge call always sends `value=1`.
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_status_as_a_normalised_position_must_not_falsely_confirm_an_unlanded_nudge(
        self,
    ) -> None:
        """Same failure, the other documented shape of `status` (design 7.3:
        "a normalised position in the slider's visible range"). The fixture's
        real on-disk value is rewritten to 0.5 (a value plausible as BOTH a
        real `exposure.exposure` EV and a normalised slider position), the
        bridge reports `status=0.5`, and `_commit` times out with no other
        side effect -- the sidecar never changes. Deciding success from that
        coincidence is the same bug as the request-echo case above."""
        self._rewrite_exposure_value(0.5)
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 0.5,
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
        """`reset` cannot be confirmed from the XMP, so its timeout
        propagates. `exposure` has a MIX of declared and undeclared rows
        (`black` has a cited `default=0.0`, `exposure` has none) -- fix
        round 1, finding 1: `_module_at_defaults` must refuse to confirm a
        module unless EVERY one of its xmp-sourced rows declares a
        neutral/default, not just the ones that happen to. Checking only
        the declared rows would silently ignore whatever
        `exposure.exposure` (undeclared, and NOT at this fixture's own
        1.5500002) really holds -- exactly what a reset changes."""
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.reset(module="exposure", confirm=True)

    def test_a_reset_timeout_on_a_module_with_no_declared_rows_at_all_also_propagates(self) -> None:
        """`sigmoid`: neither of its rows declares a neutral or a darktable
        default at all (the `rows == []` path, distinct from `exposure`'s
        mixed-declaration path above) -- `_module_at_defaults` always
        returns `False` for it too, for a different reason."""
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.reset(module="sigmoid", confirm=True)

    def _sidecar_path(self) -> Path:
        return Path(str(self.raw) + ".xmp")

    def _history_end(self) -> int:
        m = re.search(r'darktable:history_end="(\d+)"', self._sidecar_path().read_text())
        assert m
        return int(m.group(1))

    def _flip_enabled(self, text: str, num: int, enabled: str) -> str:
        pattern = re.compile(rf'(darktable:num="{num}"[^>]*?darktable:enabled=")[^"]*(")', re.S)
        new, count = pattern.subn(lambda m: m.group(1) + enabled + m.group(2), text, count=1)
        assert count == 1, f"no item num={num} to flip"
        return new

    def _clone_exposure_entry(self, text: str) -> str:
        """A real new top-of-history `exposure` entry, cloned from the
        fixture's own (num=11, so its modversion/params are genuinely
        `exposure`'s, unlike `with_new_history_hash`'s placeholder params)
        -- lets `with_param_value` rewrite ITS value afterward."""
        end = self._history_end()
        src = re.search(r'<rdf:li\s(?:(?!/>).)*?darktable:num="11"(?:(?!/>).)*?/>', text, re.S)
        assert src, "fixture exposure entry (num=11) not found"
        item = re.sub(r'darktable:num="\d+"', f'darktable:num="{end}"', src.group(0), count=1)
        close = text.index("</darktable:history>")
        seq_close = text.rindex("</rdf:Seq>", 0, close)
        text = text[:seq_close] + " " + item + "\n    " + text[seq_close:]
        return re.sub(
            r'darktable:history_end="\d+"', f'darktable:history_end="{end + 1}"', text, count=1
        )

    def test_a_nudge_that_lands_after_the_timeout_is_reported_applied_not_noop(self) -> None:
        """A late `history.commit` answer lands the edit after `CommitTimeout`;
        the mock writes both a real new history entry and the landed value,
        so Z2's two oracles agree, unlike a bare history bump."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _late_flush(*_a, **_kw):
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 9.0)
            sidecar.write_text(landed)
            raise self._timeout()

        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 9.0,
            "view": "darkroom",
            **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = darkroom.nudge(module="exposure", param="exposure", steps=1)
        self.assertTrue(result.committed, "the late-landed edit must be reported committed")
        self.assertTrue(
            any("late flush" in w or "AFTER the commit timeout" in w for w in result.warnings),
            result.warnings,
        )
        self.assertFalse(any("no new sidecar" in w for w in result.warnings), result.warnings)

    def test_a_disable_that_lands_after_the_timeout_is_reported_applied_not_noop(self) -> None:
        """Same shape for `_do_module_action`'s `_commit_with_readback` call
        site -- `exposure` starts enabled, so `disable` has
        `matched_before=False` (unlike `enable`, below)."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()
        end = self._history_end()

        def _late_flush(*_a, **_kw):
            landed = with_new_history_hash(before_text, module="exposure")
            landed = self._flip_enabled(landed, end, "0")
            sidecar.write_text(landed)
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = darkroom.disable(module="exposure")
        self.assertTrue(result.committed)
        self.assertTrue(
            any("late flush" in w or "AFTER the commit timeout" in w for w in result.warnings),
            result.warnings,
        )

    def test_a_predicted_noop_module_action_stays_a_noop_even_with_a_late_history_bump(
        self,
    ) -> None:
        """A write whose pre-write state already matched the target must
        never be relabelled "landed" by `matched_before` even when history
        and the readback happen to agree -- `enable` on an already-enabled
        `exposure` stays a noop despite the mocked timeout bumping history."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _late_flush(*_a, **_kw):
            sidecar.write_text(with_new_history_hash(before_text, module="exposure"))
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = darkroom.enable(module="exposure")
        self.assertTrue(result.committed)
        self.assertTrue(any("no new sidecar" in w for w in result.warnings), result.warnings)
        self.assertFalse(any("late flush" in w for w in result.warnings), result.warnings)

    def test_a_timeout_with_no_history_movement_still_raises(self) -> None:
        """Control, updated for t_fcf77055: a timeout where the sidecar
        genuinely never changes (no late flush at all) must raise
        `CommitTimeout`, not be resolved from `status` -- `status` is a
        post-write GUI echo (design 7.3), not a value, so a status that
        happens to equal the fixture's own unchanged value proves nothing
        landed. Formerly asserted the opposite (`committed=True`, "honest
        noop"); that was this card's false-success bug."""
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 1.5500002,
            "view": "darkroom",
            **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_a_concurrent_change_to_the_same_module_is_not_credited_late_flush(self) -> None:
        """Z2: history moving for `expect_module` is not by itself a second
        oracle -- a concurrent write to the same module (a hand drag, another
        job) also bumps history but disagrees with the readback, so the
        timeout must propagate, not be credited as landed."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _concurrent_edit(*_a, **_kw):
            # History moves (a real new exposure entry), but its value is
            # nowhere near the nudge's requested 9.0 -- an unrelated write.
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 0.2)
            sidecar.write_text(landed)
            raise self._timeout()

        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 9.0,
            "view": "darkroom",
            **p,
        }
        with mock.patch("scouter.core.darkroom._commit", side_effect=_concurrent_edit):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_a_pending_uncommitted_nudges_status_must_not_leak_into_the_next_nudges_confirm(
        self,
    ) -> None:
        """Fix round 1, C1 (review Critical): `_confirm_nudge` and the
        post-check must measure movement from what is actually ON DISK
        (`raw_state`), never from `before_state` -- after a `commit=False`
        nudge, `before_state`'s value is THAT nudge's own bridge `status`
        (`_raw_and_before_state`'s `pending_uncommitted` fallback), which
        design 7.3 says is a normalised position, not a value. Reviewer
        probe `test_one`: nudge #1 (`commit=False`) reports a status of
        0.6, well below the fixture's real on-disk 1.5500002; nudge #2
        (`commit=True`, up) times out with NO sidecar change at all. Using
        `before_state` (0.6) as the baseline makes `1.5500002 > 0.6` read
        as "moved up," a write that never reached disk reported as landed
        -- the exact false-success bug this branch was meant to remove.
        Using `raw_state` (1.5500002, genuinely unchanged) is not confirmed,
        so the timeout must propagate."""
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": 0.6,
            "view": "darkroom",
            **p,
        }
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def _pending_uncommitted_nudge_with_status(self, status: float) -> None:
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "status": status,
            "view": "darkroom",
            **p,
        }
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)

    def test_a_pending_uncommitted_nudge_then_a_real_late_flush_below_status_still_honest(
        self,
    ) -> None:
        """Fix round 2, R2-I1 (review Important): the C1 test above passes
        even with `before_state` put back as the baseline, because it never
        exercises a second oracle -- with no history movement, I2's
        `allow_noop=False` alone forces `CommitTimeout` regardless of which
        baseline decided `confirm()`. C1 is only observable when the SECOND
        oracle (history advanced, or the commit itself succeeds) agrees.
        Reviewer probe `test_six`: a pending `commit=False` nudge reports
        status 0.6; the next nudge's `_commit` then writes a REAL new
        exposure history entry at 1.0 (below the true on-disk 1.5500002,
        above the pending status 0.6) and raises `CommitTimeout` (a late
        flush). With the `before_state` (0.6) baseline, `1.0 > 0.6` reads
        as "moved up" and history genuinely advanced, so `_late_flush_
        landed` would credit it -- a write whose real destination (1.0) is
        BELOW the true starting value (1.5500002) reported as a landed
        up-nudge. With the `raw_state` (1.5500002) baseline, `1.0` is NOT
        above it, so this must stay unconfirmed -- `CommitTimeout`
        propagates (no oracle agrees it moved up)."""
        self._pending_uncommitted_nudge_with_status(0.6)
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _late_flush_below_raw(*_a, **_kw):
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 1.0)
            sidecar.write_text(landed)
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush_below_raw):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_a_pending_uncommitted_nudge_then_a_real_successful_commit_below_status_raises(
        self,
    ) -> None:
        """Fix round 2, R2-I1 (review Important), the successful-commit
        twin of the test above. Reviewer probe `test_seven`: same pending
        `commit=False` nudge (status 0.6), but this time `_commit` itself
        returns `committed=True` (no timeout at all) after writing the same
        real new exposure entry at 1.0. With the `before_state` (0.6)
        baseline, the post-commit check's `_moved_as_asked(0.6, 1.0, "up")`
        is true (1.0 > 0.6), crediting a write whose real destination is
        BELOW the true on-disk value before this nudge. With the
        `raw_state` (1.5500002) baseline, `_moved_as_asked(1.5500002, 1.0,
        "up")` is false (1.0 is not above 1.5500002), so the post-check
        must raise `WriteUnverified`."""
        self._pending_uncommitted_nudge_with_status(0.6)
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _ok_below_raw(*_a, **_kw):
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 1.0)
            sidecar.write_text(landed)
            return mock.Mock(committed=True, warnings=[])

        with mock.patch("scouter.core.darkroom._commit", side_effect=_ok_below_raw):
            with self.assertRaises(WriteUnverified):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_a_successful_commit_that_moved_the_wrong_way_is_not_credited(self) -> None:
        """Fix round 1, I1 (review Important): the post-commit check must
        test DIRECTION, not just "unchanged" -- a successful (non-timeout)
        commit whose readback moved the WRONG way (an up-nudge whose XMP
        reads 1.5500002 -> 0.1, a concurrent drag landing in the other
        direction) must not be credited as this nudge's own landed step.
        Reviewer probe `test_four`."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _opposite_direction(*_a, **_kw):
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 0.1)
            sidecar.write_text(landed)
            return mock.Mock(committed=True, warnings=[])

        with mock.patch("scouter.core.darkroom._commit", side_effect=_opposite_direction):
            with self.assertRaises(WriteUnverified):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_a_value_that_moved_without_history_advancing_is_not_an_honest_noop(self) -> None:
        """Fix round 1, I2 (review Important): a `commit=True` nudge whose
        timeout resolution sees the value move (`confirm()` true) but the
        module's OWN history not advance past `before_fp` is unconfirmed,
        not an honest "the state requested is the one the XMP already
        carried" noop -- that text is only true for a caller with a real,
        known target (`set`, `enable`), never for `nudge`. Reviewer probe
        `test_two`: the bridge action itself writes a new exposure entry
        (2.0) DURING the call, before `_commit` captures its own
        `before_fp` snapshot and raises the timeout -- so by the time
        `_resolve_timeout` runs, the readback shows 2.0 (confirmed=True,
        moved up) but `_history_advanced_for` sees no advance past that
        already-later fingerprint. `allow_noop=False` must turn this into a
        propagated `CommitTimeout`, not a false noop."""
        sidecar = self._sidecar_path()

        def _early_flush(p):
            landed = self._clone_exposure_entry(sidecar.read_text())
            landed = with_param_value(landed, "exposure", "exposure", 2.0)
            sidecar.write_text(landed)
            return {"nan": False, "status": 0.7, "view": "darkroom", **p}

        self.bridge.responses["darkroom.action"] = _early_flush
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="exposure", param="exposure", steps=1)

    def test_set_param_that_lands_after_the_timeout_is_reported_applied_not_noop(self) -> None:
        """`set_param` (what `moves.py`'s adjust/say verbs write through, and
        what every night-2 late-flush case actually hit) has its own inline
        late-flush resolution, separate from `_commit_with_readback`."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _late_flush(*_a, **_kw):
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 9.0)
            sidecar.write_text(landed)
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_late_flush):
            result = darkroom.set_param(module="exposure", param="exposure", value=9.0)
        self.assertTrue(result.committed)
        self.assertTrue(
            any("late flush" in w or "AFTER the commit timeout" in w for w in result.warnings),
            result.warnings,
        )
        self.assertFalse(any("already carried" in w for w in result.warnings), result.warnings)

    def test_set_param_concurrent_change_to_the_same_module_is_not_credited(self) -> None:
        """Z2 for the `set_param` path: same shape as the nudge version
        above -- history moves, but the readback disagrees with the
        request, so the timeout must still propagate."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _concurrent_edit(*_a, **_kw):
            landed = self._clone_exposure_entry(before_text)
            landed = with_param_value(landed, "exposure", "exposure", 0.2)
            sidecar.write_text(landed)
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_concurrent_edit):
            with self.assertRaises(CommitTimeout):
                darkroom.set_param(module="exposure", param="exposure", value=9.0)

    def test_a_value_that_moved_without_the_module_history_advancing_still_raises(self) -> None:
        """Fix round 2: the readback confirming the target is not enough on
        its own either -- rewriting the value in an EXISTING history item
        in place (no new item, `history_end` unmoved) is a value that moved
        without this module's history advancing, which is neither a landed
        edit nor an honest noop, so the timeout must still propagate."""
        sidecar = self._sidecar_path()
        before_text = sidecar.read_text()

        def _value_only_change(*_a, **_kw):
            sidecar.write_text(with_param_value(before_text, "exposure", "exposure", 9.0))
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_value_only_change):
            with self.assertRaises(CommitTimeout):
                darkroom.set_param(module="exposure", param="exposure", value=9.0)

    def test_a_reset_of_exposure_is_not_confirmed_by_black_alone(self) -> None:
        """Regression guard for fix round 1, finding 1: the fixture's own
        `exposure.black` (-0.000244140625) reads AS darktable's
        scene-referred-raw default within `default_tolerance` -- but
        `exposure.exposure` (this fixture: 1.5500002, nowhere near either
        of darktable's real exposure defaults, 0.0 or 0.7) declares NO
        neutral/default at all, so the module as a whole must still refuse
        to be confirmed at defaults. Before the fix, `black` alone made
        `_module_at_defaults("exposure")` return `True` regardless of what
        `exposure.exposure` held -- a real honesty regression: a timed-out
        reset that would actually change `exposure` from 1.5500002 to
        darktable's real default was reported as a successful commit."""
        self.assertFalse(darkroom._module_at_defaults(str(self.raw), "exposure", 0))
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.reset(module="exposure", confirm=True)

    def test_a_colorzones_reset_that_never_landed_is_not_a_noop(self) -> None:
        """Review finding 1 on 9ae3978 (row 430/461 shape). colorzones.strength
        has a declared default, but its zone curves (value_source "none")
        cannot be decoded, so strength reading its default says nothing about
        whether the module is at defaults. The sidecar is the real host
        fixture with strength at the default and one curve slot moved; a
        reset whose commit wrote nothing must report committed=False, as on
        main, not a confirmed noop."""
        text = HOST_FIXTURE.read_text()
        strength_default = vocab.spec("colorzones", "strength").default
        text = with_param_value(text, "colorzones", "strength", strength_default)
        curve = xmp.latest_params(xmp.parse(text), "colorzones", 0)["f10"]
        text = with_param_value(text, "colorzones", "f10", curve + 0.37)
        self._sidecar_path().write_text(text)
        self.assertFalse(darkroom._module_at_defaults(str(self.raw), "colorzones", 0))
        not_landed = CommitResult(committed=False, changed=False, warnings=[])
        with mock.patch("scouter.core.darkroom._commit", return_value=not_landed):
            result = darkroom.reset(module="colorzones", confirm=True)
        self.assertFalse(result.committed)
        self.assertFalse(result.noop)


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
        self.sent: dict = {}

        def commit_response(p: dict) -> dict:
            # Simulate darktable writing the sidecar on commit: bump its
            # mtime forward so history.commit's poll loop sees it advance.
            sidecar = Path(str(self.raw) + ".xmp")
            text = sidecar.read_text()
            if self.sent.get("value") is not None:
                # Row 433: darktable's rewrite carries the value it was sent.
                text = with_param_value(text, "exposure", "exposure", self.sent["value"])
            sidecar.write_text(with_new_history_hash(text, p.get("expect_module")))
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
                    "status": self.sent.update(value=p.get("value")) or p.get("value"),
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


class CommitWiringTest(unittest.TestCase):
    """Row 461 (review Important 2): every darkroom writer tells its commit
    which module it touched. An AST scan of darkroom.py, so dropping
    `expect_module=` from any call site fails here even though the fakes
    accept the call either way. moves.py/presets.py flushes are multi-module
    and rely on rule 1 (history_end must not go back), tested in
    tests/test_history_expected.py."""

    def test_every_commit_call_in_darkroom_passes_expect_module(self) -> None:
        import ast

        src = Path(darkroom.__file__).read_text()
        missing = []
        seen = 0
        for node in ast.walk(ast.parse(src)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("_commit", "_commit_with_readback")
            ):
                seen += 1
                if not any(k.arg == "expect_module" for k in node.keywords):
                    missing.append(f"{node.func.id} at darkroom.py:{node.lineno}")
        self.assertGreaterEqual(seen, 8, "the scan found the commit call sites")
        self.assertEqual(missing, [], "commit calls without expect_module")


class DefaultConsumersOutsideModuleAtDefaultsTest(unittest.TestCase):
    """Fix round 1, finding 3 (reviewer): `vocab.ParamSpec.default` is read
    by more than `_module_at_defaults` (`scouter/core/darkroom.py`). These
    pin the other two production consumers the review named, both on
    `exposure.black` (the row whose `default` this branch promoted):
    `_effective_current` (the no-op check's "what does darktable really
    hold" answer) and `moves.before_fallback` (a move's undo-before value).
    Each is off by at most `EXPOSURE_BLACK_SCENE_RAW_DEFAULT`'s magnitude
    (2.4e-4) from what darktable may really hold on a scene-referred raw
    with no exposure history -- small, and the report notes it -- but both
    consumers exist and behave as documented."""

    def test_effective_current_answers_exposure_black_default_when_module_has_no_history(
        self,
    ) -> None:
        # Exactly the "sidecar exists, module has no active history entry"
        # shape _effective_current's own docstring describes: history_end
        # is set (the sidecar exists), enabled is None (no active entry for
        # THIS module), value is None (nothing decoded).
        raw_state = {"value": None, "history_end": 12, "enabled": None}
        value, source = darkroom._effective_current(raw_state, "exposure", "black")
        self.assertEqual(value, 0.0)
        self.assertEqual(source, "default")

    def test_effective_current_is_none_for_exposure_exposure_in_the_same_shape(self) -> None:
        # The contrast case: exposure.exposure declares no neutral/default
        # at all, so the SAME "no active history entry" shape answers None,
        # never a guessed value -- _module_at_defaults's finding-1 fix
        # relies on this asymmetry existing.
        raw_state = {"value": None, "history_end": 12, "enabled": None}
        value, source = darkroom._effective_current(raw_state, "exposure", "exposure")
        self.assertIsNone(value)
        self.assertEqual(source, "xmp")
