"""Phase 3 task 5: relative moves read, clamp, set -- with a call-counting FakeBridge."""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal, moves
from scouter.errors import NotPinned, UsageError, ValueUnknown
from scouter.types import CommitResult, SetResult
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _MovesFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURES / "P6281348.ORF.xmp", str(self.raw) + ".xmp")
        # The measured EM10iii table, under a temp config dir (never ~/.config).
        cfg_dir = root / "config"
        (cfg_dir / "wb").mkdir(parents=True)
        shutil.copy(FIXTURES / "wb-em10iii.json", cfg_dir / "wb" / "em10iii.json")
        env = mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(cfg_dir / "config.json")})
        env.start()
        self.addCleanup(env.stop)
        cam = mock.patch("scouter.core.moves._camera_for", return_value="em10iii")
        cam.start()
        self.addCleanup(cam.stop)
        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "status": p.get("value"),
                                          "view": "darkroom", **p},
        })
        dtbridge.set_bridge(self.bridge)
        self.commits: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto", attempts=None, expect_change=True, expect_module=None):
            self.commits.append((attempts, expect_change, expect_module))
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def sets(self) -> list[dict]:
        return [p for m, p in self.bridge.calls if m == "darkroom.action"]


class AdjustTest(_MovesFixture):
    def test_adjust_reads_then_sets_the_sum(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertAlmostEqual(r.previous, 1.5500002, places=5)
        self.assertAlmostEqual(r.target, 1.8500002, places=5)
        self.assertEqual(r.source, "xmp")
        self.assertEqual(len(self.sets()), 1)
        self.assertAlmostEqual(self.sets()[0]["value"], 1.8500002, places=5)
        self.assertEqual(r.summary, "exposure › exposure: +0.3 EV (1.55 EV → 1.85 EV)")

    def test_pre_commit_is_one_expect_no_change_bounce(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.commits[0], (1, False, "exposure"))

    def test_pre_commit_passes_the_module_the_writer_expects(self) -> None:
        # Row 461 follow-up: the applied-verify pre-commit bounce must name
        # its module too, exactly like every other writer's `_commit` call
        # (`_try_commit`, `_commit_with_readback`, ...) -- otherwise a stale
        # rewrite from an unrelated module's history item is accepted as
        # this move's own pre-commit, the same hole row 434/461 closed for
        # the real commit.
        moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, commit=False, job="job-test")
        self.assertEqual(self.commits[0][2], "colorbalancergb")

    def test_trust_sidecar_skips_the_pre_commit(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                 trust_sidecar=True, job="job-test")
        self.assertEqual(self.commits, [])

    def test_clamp_is_reported(self) -> None:
        # Pratik's ruling, row 471 (COV-D-residual-clamp-capped): a real,
        # clamped move is `moved_to_limit`, never a plain "ok" -- and the
        # CLI/MCP text says so.
        r = moves.adjust.__wrapped__("exposure", "exposure", 10.0, commit=False, job="job-test")
        self.assertTrue(r.clamped)
        self.assertEqual(r.target, 4.0)
        self.assertIn("moved to the limit (4 EV)", r.summary)
        self.assertEqual(r.outcome, "moved_to_limit")
        self.assertEqual(r.cap, 4.0)
        self.assertAlmostEqual(r.requested, 11.5500002, places=5)

    def test_clamped_commit_with_undecodable_readback_is_plain_ok_not_moved_to_limit(self) -> None:
        # Review round 1 (I3, gap ii): a `commit=True` write that lands and
        # clamps but whose readback could not decode (`applied is None`) must
        # not claim a VERIFIED `moved_to_limit` -- a real write was sent and
        # we do not know where it landed. Falls back to plain "ok", `cap`
        # None, with a warning naming the gap.
        with mock.patch("scouter.core.moves._set_recording_before",
                        return_value=SetResult(module="exposure", param="exposure",
                                                requested=4.0, applied=None, previous=1.55,
                                                committed=True, clamped=True, noop=False)):
            r = moves.adjust.__wrapped__("exposure", "exposure", 10.0, commit=True, job="job-test")
        self.assertTrue(r.clamped)
        self.assertEqual(r.outcome, "ok")
        self.assertIsNone(r.cap)
        self.assertTrue(any("could not be verified" in w for w in r.warnings), r.warnings)

    def test_clamped_commit_with_a_hard_bound_below_our_own_computed_target_is_moved_to_limit(
        self,
    ) -> None:
        # Review round 1 (I3, gap i): darktable's own hard bound can differ
        # slightly from `vocab.clamp`'s own computed `target` -- `cap` must
        # NAME the value that actually bound it (the verified readback,
        # `applied`), not our own guess (`target`).
        with mock.patch("scouter.core.moves._set_recording_before",
                        return_value=SetResult(module="exposure", param="exposure",
                                                requested=4.0, applied=3.95, previous=1.55,
                                                committed=True, clamped=True, noop=False)):
            r = moves.adjust.__wrapped__("exposure", "exposure", 10.0, commit=True, job="job-test")
        self.assertTrue(r.clamped)
        self.assertEqual(r.outcome, "moved_to_limit")
        self.assertEqual(r.cap, 3.95)
        self.assertNotEqual(r.cap, r.target)

    def test_adjust_at_the_limit_sends_nothing(self) -> None:
        moves.adjust.__wrapped__("exposure", "exposure", 10.0, commit=False, job="job-test")
        before = len(self.sets())
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertTrue(r.noop)
        self.assertEqual(len(self.sets()), before, "a move at the limit must send nothing")
        # Fix round 1, controller ruling: the no-op path still reports
        # plan[0] with ms_precommit/ms_read/ms_set (spec §16).
        self.assertEqual(r.plan[0]["ms_set"], 0)
        self.assertIn("ms_precommit", r.plan[0])
        self.assertIn("ms_read", r.plan[0])

    def test_kelvin_comes_from_the_measured_table(self) -> None:
        r = moves.adjust.__wrapped__("temperature", "temperature", 300.0, commit=False,
                                     job="job-test")
        self.assertEqual(r.source, "wb_table")
        self.assertAlmostEqual(r.previous, 5399.05, places=1)
        self.assertAlmostEqual(self.sets()[0]["value"], 5699.05, places=1)
        self.assertTrue(any("tint 1.0" in w for w in r.warnings))

    def test_kelvin_without_a_table_is_value_unknown(self) -> None:
        with mock.patch("scouter.core.moves._camera_for", return_value="a7"):
            with self.assertRaises(ValueUnknown) as cm:
                moves.adjust.__wrapped__("temperature", "temperature", 300.0, job="job-test")
        self.assertIn("wb calibrate", cm.exception.detail["reason"])
        self.assertEqual(self.sets(), [])

    def test_absent_module_uses_the_declared_neutral(self) -> None:
        r = moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, commit=False,
                                     job="job-test")
        self.assertEqual((r.previous, r.source), (0.0, "neutral"))
        self.assertIn("+5% (0% → 5%)", r.summary)

    def test_a_neutral_sourced_move_is_undoable(self) -> None:
        # Fix round 1, controller ruling, load-bearing: `set_param`'s own
        # `before_state` is built purely from the XMP/journal and has no
        # idea `read_current` fell back to the declared neutral, so
        # WITHOUT `journal.set_before_neutral` this journals
        # `before.value: None` -- the very first move on a fresh module
        # could never be undone. Assert the journal entry carries the
        # neutral, and that `undo_last` sends the neutral back rather than
        # raising "the previous value ... was never readable from the XMP".
        moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, commit=False,
                                 job="job-test")
        entry = journal.read_all("job-test")[-1]
        self.assertEqual(entry.before.get("value"), 0.0)
        self.assertEqual(entry.before.get("source"), "neutral")
        darkroom.undo_last.__wrapped__(job="job-test")
        self.assertAlmostEqual(self.sets()[-1]["value"], 0.0)
        self.assertTrue(journal.read_all("job-test")[-1].undone)

    def test_neutral_patch_survives_a_commit_timeout_after_the_write_landed(self) -> None:
        """Final review #1b: `set_param` lands the write, then its commit
        raises `CommitTimeout`. The neutral must still be journalled as the
        entry's before, so `undo_last` restores it instead of wedging on
        "seq N ... never readable"."""
        from scouter.errors import CommitTimeout

        def timeout_on_change(job, image, *, strategy="auto", attempts=None, expect_change=True, expect_module=None):
            if expect_change:
                raise CommitTimeout("sidecar never advanced")
            return CommitResult(committed=True, changed=False, warnings=[])

        with mock.patch("scouter.core.darkroom._commit", side_effect=timeout_on_change):
            with self.assertRaises(CommitTimeout):
                moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, job="job-test")
        entry = journal.read_all("job-test")[-1]
        self.assertEqual((entry.module, entry.param), ("colorbalancergb", "contrast"))
        self.assertEqual(entry.after, {"value": 0.05}, "the write landed")
        self.assertEqual((entry.before.get("value"), entry.before.get("source")), (0.0, "neutral"))
        darkroom.undo_last.__wrapped__(single=True, job="job-test")
        self.assertAlmostEqual(self.sets()[-1]["value"], 0.0)
        self.assertTrue(journal.read_all("job-test")[-1].undone)

    def test_neutral_patch_survives_any_bridge_error_after_the_write_landed(self) -> None:
        """Task 10 (parked final-review issue): a non-CommitTimeout error
        from the commit (here BridgeTimeout) after the write landed must
        still journal the neutral as before, or `undo_last` wedges."""
        from scouter.errors import BridgeTimeout

        def bridge_dies_on_change(job, image, *, strategy="auto", attempts=None,
                                  expect_change=True, expect_module=None):
            if expect_change:
                raise BridgeTimeout("bridge went away mid-commit")
            return CommitResult(committed=True, changed=False, warnings=[])

        with mock.patch("scouter.core.darkroom._commit", side_effect=bridge_dies_on_change):
            with self.assertRaises(BridgeTimeout):
                moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, job="job-test")
        entry = journal.read_all("job-test")[-1]
        self.assertEqual(entry.after, {"value": 0.05}, "the write landed")
        self.assertEqual((entry.before.get("value"), entry.before.get("source")), (0.0, "neutral"))
        darkroom.undo_last.__wrapped__(single=True, job="job-test")
        self.assertAlmostEqual(self.sets()[-1]["value"], 0.0)
        self.assertTrue(journal.read_all("job-test")[-1].undone)

    def test_a_write_that_never_landed_is_not_patched(self) -> None:
        """#1b: the patch only applies to a landed entry -- a bridge failure
        (journal.fail, `after` None) is left exactly as the engine wrote it."""
        from scouter.errors import NotInDarkroom

        self.bridge.responses["darkroom.action"] = lambda p: {"nan": True, "view": "darkroom", **p}
        with self.assertRaises(NotInDarkroom):
            moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, job="job-test")
        entry = journal.read_all("job-test")[-1]
        self.assertIsNone(entry.after)
        self.assertIsNone(entry.before.get("value"))

    def test_no_sidecar_refuses_value_unknown_not_neutral(self) -> None:
        # Fix round 1, important: `_module_state`'s `enabled: None` is
        # ambiguous between "no sidecar at all" and "a sidecar exists but
        # this module has no active entry" -- only the second case may fall
        # back to the declared neutral (spec §6.1 row 4). With the sidecar
        # removed entirely, colorbalancergb.contrast (a neutral=0.0 row) must
        # refuse value_unknown, not silently report (0.0, "neutral").
        os.remove(str(self.raw) + ".xmp")
        with self.assertRaises(ValueUnknown) as cm:
            moves.adjust.__wrapped__("colorbalancergb", "contrast", 0.05, job="job-test")
        self.assertIn("no sidecar", cm.exception.detail["reason"])
        self.assertEqual(self.sets(), [])

    def test_precommit_only_downgrades_commit_timeout(self) -> None:
        # Controller ruling: only CommitTimeout is downgraded to a warning;
        # every other ScouterError from the pre-commit bounce propagates.
        from scouter.errors import WrongView

        with mock.patch("scouter.core.darkroom._commit", side_effect=WrongView("not darkroom")):
            with self.assertRaises(WrongView):
                moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertEqual(self.sets(), [])

    def test_precommit_timeout_is_downgraded_to_a_warning(self) -> None:
        from scouter.errors import CommitTimeout

        with mock.patch("scouter.core.darkroom._commit",
                        side_effect=CommitTimeout("no bounce")):
            r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False, job="job-test")
        self.assertTrue(any("pre-commit did not confirm" in w for w in r.warnings))
        self.assertEqual(len(self.sets()), 1)

    def test_absolute_only_refuses_value_unknown_before_any_write(self) -> None:
        # Task 10 pinned sigmoid.skew's slot; tint has no XMP value source.
        with self.assertRaises(ValueUnknown) as cm:
            moves.adjust.__wrapped__("temperature", "tint", 0.05, job="job-test")
        self.assertIn("darkroom set temperature tint", cm.exception.fix)
        self.assertEqual(self.bridge.calls, [])

    def test_candidate_refuses_not_pinned_before_any_bridge_call(self) -> None:
        # Task 10 pinned every candidate row; unpin one to keep this covered.
        with mock.patch.dict(dtbridge.ACTION_PATHS), self.assertRaises(NotPinned):
            del dtbridge.ACTION_PATHS[("colorbalancergb", "shadows_luminance")]
            moves.adjust.__wrapped__("colorbalancergb", "shadows_luminance", 0.02, job="job-test")
        self.assertEqual(self.bridge.calls, [])

    def test_hue_wraps_modulo_360(self) -> None:
        hue = ("colorbalancergb", "shadows_hue")
        with mock.patch.dict(dtbridge.ACTION_PATHS, {hue: "iop/colorbalancergb/lift/hue"}), \
             mock.patch("scouter.core.vocab.xmp_named", return_value=True), \
             mock.patch("scouter.core.moves.read_current", return_value=(350.0, "xmp", None)):
            r = moves.adjust.__wrapped__(*hue, 20.0, commit=False, job="job-test")
        self.assertAlmostEqual(r.target, 10.0)
        self.assertFalse(r.clamped)

    def test_zero_or_nonfinite_delta_is_usage(self) -> None:
        for bad in (0.0, float("nan"), float("inf")):
            with self.subTest(delta=bad), self.assertRaises(UsageError):
                moves.adjust.__wrapped__("exposure", "exposure", bad, job="job-test")

    def test_not_in_vocabulary_is_usage_naming_darkroom_set(self) -> None:
        with self.assertRaises(UsageError) as cm:
            moves.adjust.__wrapped__("lens", "tca", 0.1, job="job-test")
        self.assertIn("darkroom set", cm.exception.fix)

    def test_dry_run_makes_no_bridge_calls(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(r.plan[0]["path"], "iop/exposure/exposure")

    def test_brief_explain_is_attached(self) -> None:
        r = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                     educative="brief", job="job-test")
        self.assertEqual(len(r.explain), 1)
        self.assertIn("watch the highlights", r.explain[0])
        off = moves.adjust.__wrapped__("exposure", "exposure", 0.3, commit=False,
                                       educative="off", job="job-test")
        self.assertEqual(off.explain, [])

    def test_ruling_30_guard_applies(self) -> None:
        from scouter import paths
        paths.job_state_update("job-test", lambda s: s.__setitem__("opened_image_id", 99))
        from scouter.errors import NotInDarkroom
        with self.assertRaises(NotInDarkroom):
            moves.adjust.__wrapped__("exposure", "exposure", 0.3, job="job-test")
        self.assertEqual(self.sets(), [])


class GetTest(_MovesFixture):
    def test_get_reads_the_value_and_never_commits(self) -> None:
        r = moves.get.__wrapped__("sigmoid", "contrast", job="job-test")
        self.assertAlmostEqual(r.value, 1.4, places=5)
        self.assertEqual((r.source, r.status), ("xmp", "live"))
        self.assertEqual(self.commits, [])
        self.assertFalse(r.fresh)
        self.assertTrue(any("not known-fresh" in w for w in r.warnings))

    def test_get_reports_null_with_a_reason_instead_of_refusing(self) -> None:
        r = moves.get.__wrapped__("temperature", "tint", job="job-test")
        self.assertIsNone(r.value)
        self.assertEqual(r.status, "absolute_only")

    def test_get_with_no_sidecar_reports_null_not_neutral(self) -> None:
        # Fix round 1, important: same ambiguity as the adjust-side test --
        # `get` on a neutral=0.0 row with no sidecar at all must not report
        # `0.0`; `get` never raises (it reports value: null with a reason).
        os.remove(str(self.raw) + ".xmp")
        r = moves.get.__wrapped__("colorbalancergb", "contrast", job="job-test")
        self.assertIsNone(r.value)
        self.assertIn("no sidecar", " ".join(r.warnings))


class CliNegativeDeltaTest(unittest.TestCase):
    def test_negative_positional_delta_parses(self) -> None:
        from scouter.cli.main import build_parser
        ns = build_parser().parse_args(["darkroom", "adjust", "sigmoid", "contrast", "-0.1", "--dry-run"])
        self.assertEqual(ns.delta, -0.1)
