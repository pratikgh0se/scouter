"""Phase 3 task 6: phrases as grouped moves, undone together."""

from __future__ import annotations

from unittest import mock

from scouter.core import darkroom, dtbridge, exif, journal, moves
from scouter.errors import NotPinned, UsageError, ValueUnknown
from tests.test_moves import _MovesFixture

CROP_PATHS = {("crop", side): f"iop/crop/{side}" for side in ("left", "right", "top", "bottom")}


class SayTest(_MovesFixture):
    def test_one_move_phrase_lands_in_a_group(self) -> None:
        r = moves.say.__wrapped__("a touch warmer", commit=False, job="job-test")
        self.assertEqual(r.key, "warmer")
        self.assertEqual(len(r.moves), 1)
        self.assertAlmostEqual(r.moves[0]["delta"], 150.0)
        self.assertTrue(r.group.startswith("say-"))
        self.assertEqual(journal.read_all("job-test")[-1].group, r.group)
        self.assertIsNone(journal.GROUP.get(), "the contextvar must be reset")

    def test_contrast_routes_to_sigmoid_on_the_fixture(self) -> None:
        r = moves.say.__wrapped__("less contrast", commit=False, job="job-test")
        self.assertEqual(r.moves[0]["module"], "sigmoid")
        self.assertAlmostEqual(r.moves[0]["target"], 1.3, places=5)

    def test_contrast_routes_to_filmic_when_filmic_is_the_tone_mapper(self) -> None:
        # Task 10 pinned filmic's contrast path and slot; the fixture has no
        # filmicrgb entry and the row declares no neutral, so the routed move
        # is refused value_unknown (it reached filmic, not sigmoid).
        with mock.patch("scouter.core.moves.route_tone", return_value="filmicrgb"):
            with self.assertRaises(ValueUnknown) as cm:
                moves.say.__wrapped__("less contrast", job="job-test")
        self.assertIn("filmicrgb.contrast", cm.exception.message)
        self.assertEqual(self.sets(), [])

    def test_a_phrase_with_any_unreachable_move_is_refused_whole(self) -> None:
        # Task 10 pinned every teal-and-orange row; unpin them to keep the
        # P3-R4 all-or-nothing refusal covered.
        teal = [("colorbalancergb", p) for p in
                ("shadows_hue", "shadows_chroma", "highlights_hue", "highlights_chroma")]
        with mock.patch.dict(dtbridge.ACTION_PATHS):
            for key in teal:
                del dtbridge.ACTION_PATHS[key]
            with self.assertRaises(NotPinned) as cm:
                moves.say.__wrapped__("teal and orange", job="job-test")
        self.assertEqual(len(cm.exception.detail["refused"]), 4)
        self.assertEqual(self.sets(), [], "nothing may land before the pre-validation")
        # Fix round 1, important: the whole phrase's moves here are all
        # non-@tone, so the controller ruling (refuse before touching the
        # bridge) must mean literally ZERO bridge calls -- not even the
        # `darkroom.current` lookup `_current_image()` would otherwise make.
        self.assertEqual(self.bridge.calls, [])

    def test_one_unpinned_sub_move_refuses_teal_and_orange_whole(self) -> None:
        with mock.patch.dict(dtbridge.ACTION_PATHS):
            del dtbridge.ACTION_PATHS[("colorbalancergb", "highlights_chroma")]
            with self.assertRaises(NotPinned) as cm:
                moves.say.__wrapped__("teal and orange", job="job-test")
        self.assertEqual([r["param"] for r in cm.exception.detail["refused"]],
                         ["highlights_chroma"])
        self.assertEqual(self.sets(), [])

    def test_teal_and_orange_runs_whole_once_every_sub_move_is_pinned(self) -> None:
        """Task 10 ruling: with every sub-move pinned and the hue rows'
        darktable default declared, the phrase lands on an image with no
        colour-balance history as ONE group, and undoes as one."""
        r = moves.say.__wrapped__("teal and orange", commit=False, job="job-test")
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(len(r.moves), 4)
        self.assertEqual({e.group for e in journal.read_all("job-test")}, {r.group})
        undone = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(len(undone.undone_group), 4)

    def test_absolute_only_phrase_is_value_unknown(self) -> None:
        # Task 10: "more saturation" is live now; tint has no readable value.
        with self.assertRaises(ValueUnknown):
            moves.say.__wrapped__("more magenta", job="job-test")

    def test_unknown_phrase_is_usage_with_suggestions(self) -> None:
        with self.assertRaises(UsageError) as cm:
            moves.say.__wrapped__("warmmer", job="job-test")
        self.assertIn("warmer", cm.exception.detail["suggestions"])

    def test_one_pre_commit_for_the_whole_phrase(self) -> None:
        # `say` moves may span several modules, so its pre-commit expects
        # none in particular (unlike `adjust`'s single-module bounce).
        moves.say.__wrapped__("brighter", commit=False, job="job-test")
        self.assertEqual(self.commits, [(1, False, None)])

    def test_dry_run_resolves_without_darktable(self) -> None:
        r = moves.say.__wrapped__("warmer", dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])

    def test_partial_when_a_later_move_fails(self) -> None:
        with mock.patch.dict(dtbridge.ACTION_PATHS, {
            ("colorbalancergb", "shadows_chroma"): "iop/colorbalancergb/shadows lift/chroma",
            ("colorbalancergb", "midtones_chroma"): "iop/colorbalancergb/power/chroma",
            ("colorbalancergb", "highlights_chroma"): "iop/colorbalancergb/highlights gain/chroma",
        }), mock.patch("scouter.core.vocab.xmp_named", return_value=True):
            calls = {"n": 0}

            def flaky(p):
                calls["n"] += 1
                if calls["n"] == 2:
                    return {"nan": True, "view": "darkroom", **p}
                return {"nan": False, "status": p.get("value"), "view": "darkroom", **p}

            self.bridge.responses["darkroom.action"] = flaky
            r = moves.say.__wrapped__("stronger grade", commit=False, job="job-test")
        self.assertEqual(r.outcome, "partial")
        self.assertEqual(len(r.moves), 1)
        self.assertEqual(r.refused[0]["code"], "not_in_darkroom")


class SetMoveBeforeTest(_MovesFixture):
    """Final review #1a ruling: a `set` move (the hue rows of warm_highlights,
    cool_shadows, teal_orange) whose before value is unreadable -- the
    fixture's XMP has no colorbalancergb entry at all -- journals the row's
    darktable default when it declares one, and is refused value_unknown
    up front (before any write) when it declares neither neutral nor default."""

    HUE = ("colorbalancergb", "highlights_hue")
    CHROMA = ("colorbalancergb", "highlights_chroma")

    def _pinned(self):
        return mock.patch.dict(dtbridge.ACTION_PATHS, {
            self.HUE: "iop/colorbalancergb/highlights gain/hue",
            self.CHROMA: "iop/colorbalancergb/highlights gain/chroma",
        })

    def _with_default(self, default):
        import dataclasses

        from scouter.core import vocab

        row = dataclasses.replace(vocab.spec(*self.HUE), default=default)
        return mock.patch.dict(vocab._BY_KEY, {self.HUE: row})

    def test_the_hue_rows_declare_darktables_default(self) -> None:
        """Task 10 ruling: every colour balance rgb hue row declares 0.0
        (darktable's module default), so hue phrases run without history."""
        from scouter.core import vocab

        hues = [s for s in vocab.PARAMS if s.wraps]
        self.assertEqual(len(hues), 3)
        self.assertEqual({s.default for s in hues}, {0.0})

    def test_no_neutral_and_no_default_refuses_before_any_write(self) -> None:
        # Task 10: the real hue rows now declare a default; the refusal path
        # still exists for any row that declares neither, so force None.
        with self._pinned(), self._with_default(None), \
             mock.patch("scouter.core.vocab.xmp_named", return_value=True):
            with self.assertRaises(ValueUnknown) as cm:
                moves.say.__wrapped__("warm the highlights", commit=False, job="job-test")
        refused = cm.exception.detail["refused"]
        self.assertEqual([(r["module"], r["param"]) for r in refused], [self.HUE])
        self.assertIn("no neutral or darktable default", refused[0]["reason"])
        self.assertEqual(self.sets(), [], "all-or-nothing: no move may land")
        self.assertEqual(journal.read_all("job-test"), [], "no un-undoable entry is created")

    def test_a_declared_default_is_journalled_and_undo_restores_it(self) -> None:
        with self._pinned(), self._with_default(0.0), \
             mock.patch("scouter.core.vocab.xmp_named", return_value=True):
            r = moves.say.__wrapped__("warm the highlights", commit=False, job="job-test")
            self.assertEqual(r.outcome, "ok")
            hue = [e for e in journal.read_all("job-test") if e.param == "highlights_hue"][0]
            self.assertEqual((hue.before.get("value"), hue.before.get("source")), (0.0, "default"))
            self.assertEqual(r.moves[0]["previous"], 0.0, "the result names the default too")
            result = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(result.undone_group), 2)
        self.assertAlmostEqual(self.sets()[-1]["value"], 0.0, msg="hue restored to the default")
        self.assertTrue(all(e.undone for e in journal.read_all("job-test")))

    def test_default_patch_survives_a_commit_timeout_after_the_write_landed(self) -> None:
        from scouter.errors import CommitTimeout
        from scouter.types import CommitResult

        def timeout_on_change(job, image, *, strategy="auto", attempts=None, expect_change=True, expect_module=None):
            if expect_change:
                raise CommitTimeout("sidecar never advanced")
            return CommitResult(committed=True, changed=False, warnings=[])

        with self._pinned(), self._with_default(0.0), \
             mock.patch("scouter.core.vocab.xmp_named", return_value=True), \
             mock.patch("scouter.core.darkroom._commit", side_effect=timeout_on_change):
            with self.assertRaises(CommitTimeout):
                moves.say.__wrapped__("warm the highlights", job="job-test")
        [entry] = journal.read_all("job-test")
        self.assertEqual((entry.before.get("value"), entry.before.get("source")), (0.0, "default"))

    def test_a_readable_before_is_left_to_set_param(self) -> None:
        with self._pinned(), mock.patch("scouter.core.vocab.xmp_named", return_value=True), \
             mock.patch("scouter.core.darkroom._raw_and_before_state",
                        return_value=({"value": 120.0, "enabled": True, "history_end": 9},
                                      {"value": 120.0, "enabled": True, "history_end": 9},
                                      False)):
            r = moves.say.__wrapped__("warm the highlights", commit=False, job="job-test")
        self.assertEqual(r.outcome, "ok")
        hue = [e for e in journal.read_all("job-test") if e.param == "highlights_hue"][0]
        self.assertEqual(hue.before.get("value"), 120.0)
        self.assertNotIn(hue.before.get("source"), ("neutral", "default"))


class GroupUndoTest(_MovesFixture):
    def _two_moves(self) -> str:
        """Land two exposure writes in one group by hand (no multi-move live phrase exists yet)."""
        token = journal.GROUP.set("say-test")
        try:
            darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
            darkroom.set_param.__wrapped__("sigmoid", "contrast", 1.6, commit=False, job="job-test")
        finally:
            journal.GROUP.reset(token)
        return "say-test"

    def test_undo_reverts_the_whole_group_newest_first(self) -> None:
        self._two_moves()
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual([u["undone"]["module"] for u in r.undone_group], ["sigmoid", "exposure"])
        self.assertTrue(all(e.undone for e in journal.read_all("job-test")))

    def test_single_undoes_only_the_newest(self) -> None:
        self._two_moves()
        r = darkroom.undo_last.__wrapped__(single=True, job="job-test")
        self.assertEqual(r.undone["module"], "sigmoid")
        self.assertEqual(r.undone_group, [])
        self.assertEqual([e.undone for e in journal.read_all("job-test")], [False, True])

    def test_ungrouped_writes_behave_as_before(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        darkroom.set_param.__wrapped__("sigmoid", "contrast", 1.6, commit=False, job="job-test")
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(r.undone["module"], "sigmoid")
        self.assertEqual([e.undone for e in journal.read_all("job-test")], [False, True])

    def test_group_dry_run_lists_every_member_and_changes_nothing(self) -> None:
        self._two_moves()
        r = darkroom.undo_last.__wrapped__(dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(len(r.plan), 2)
        self.assertFalse(any(e.undone for e in journal.read_all("job-test")))

    def test_a_stuck_member_stops_the_loop_and_reports_partial(self) -> None:
        """Controller ruling: the group-undo loop must terminate. A member
        whose own undo leaves it open (temperature tier 2 with an unverified
        restore is the real-world case) must not spin forever -- the loop
        iterates over the group's members computed ONCE, and stops with
        outcome=partial the first time a member is still open after its
        own _undo_one call."""
        self._two_moves()

        def stuck(*, dry_run, job):
            # Never marks the newest (sigmoid) entry undone, simulating an
            # undo whose verification never landed -- without a bounded
            # loop this would spin on the same open entry forever.
            entry = journal.latest_open(job)
            return darkroom.UndoResult(
                image_id=entry.image_id, path=entry.path, seq=entry.seq,
                undone={"verb": entry.verb, "module": entry.module, "param": entry.param,
                        "restored_to": None, "verified": False},
                committed=False,
            )

        with mock.patch("scouter.core.darkroom._undo_one", side_effect=stuck):
            r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(r.outcome, "partial")
        self.assertEqual([e.undone for e in journal.read_all("job-test")], [False, False])

    def test_stronger_grade_group_undo_restores_every_neutral_sourced_member(self) -> None:
        # Fix round 1, controller ruling: every move of "stronger grade"
        # reads its previous value from colorbalancergb's declared neutral
        # (the fixture's XMP has no colorbalancergb history at all) -- the
        # group undo must be able to restore all three, not raise "the
        # previous value ... was never readable from the XMP" the moment
        # `undo_last` reaches one.
        with mock.patch.dict(dtbridge.ACTION_PATHS, {
            ("colorbalancergb", "shadows_chroma"): "iop/colorbalancergb/shadows lift/chroma",
            ("colorbalancergb", "midtones_chroma"): "iop/colorbalancergb/power/chroma",
            ("colorbalancergb", "highlights_chroma"): "iop/colorbalancergb/highlights gain/chroma",
        }), mock.patch("scouter.core.vocab.xmp_named", return_value=True):
            r = moves.say.__wrapped__("stronger grade", commit=False, job="job-test")
            self.assertEqual(r.outcome, "ok")
            self.assertEqual(len(r.moves), 3)
            for entry in journal.read_all("job-test"):
                self.assertEqual(entry.before.get("source"), "neutral")
                self.assertEqual(entry.before.get("value"), 0.0)
            result = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(result.undone_group), 3)
        self.assertTrue(all(e.undone for e in journal.read_all("job-test")))

class LensToggleUndoTest(_MovesFixture):
    """Row 437 fix round 1, C1: `say("turn off lens correction")` dispatches
    to `darkroom.disable`, whose journal entry has no `before.value` -- the
    generic set-undo branch used to raise "was never readable" on it and
    leave the entry stuck as the newest open one forever."""

    def setUp(self) -> None:
        super().setUp()
        self.bridge.responses["module.enable"] = {
            "module": "lens", "action": "enable", "nan": False, "view": "darkroom",
        }
        self.bridge.responses["module.disable"] = {
            "module": "lens", "action": "disable", "nan": False, "view": "darkroom",
        }

    def test_lens_off_then_undo_last_does_not_wedge_the_journal(self) -> None:
        r = moves.say.__wrapped__("turn off lens correction", commit=False, job="job-test")
        self.assertEqual(r.key, "lens_off")
        self.assertIn("module.disable", self.bridge.methods_called())
        result = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(len(result.undone_group), 1)
        self.assertTrue(all(e.undone for e in journal.read_all("job-test")))
        # The journal must not be stuck: a second call is a clean "nothing
        # to undo" noop, not a repeat of the same "was never readable" error.
        second = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(second.outcome, "noop")


class CropPresetDispatchTest(_MovesFixture):
    """Row 437 fix round 1: crop presets gate on all four crop rows and
    account for EXIF orientation before computing margins (I3)."""

    def setUp(self) -> None:
        super().setUp()
        self._pins = mock.patch.dict(dtbridge.ACTION_PATHS, CROP_PATHS)
        self._pins.start()
        self.addCleanup(self._pins.stop)
        self._named = mock.patch("scouter.core.vocab.xmp_named", return_value=True)
        self._named.start()
        self.addCleanup(self._named.stop)

    def _current(self, width: int, height: int) -> None:
        self.bridge.responses["darkroom.current"] = {
            "image_id": 12, "path": str(self.raw), "view": "darkroom",
            "source": "action_images", "width": width, "height": height,
        }

    def _orientation(self, value: int | None):
        return mock.patch(
            "scouter.core.moves.exif.read_tags",
            return_value=exif.ExifTags(byte_order="II", magic=42, orientation=value),
        )

    def test_crop_square_normal_orientation_uses_raw_dimensions_directly(self) -> None:
        self._current(6000, 4000)
        with self._orientation(1):
            r = moves.say.__wrapped__("crop square", commit=False, job="job-test")
        self.assertEqual(r.key, "crop_square")
        self.assertEqual(len(r.moves), 4)
        by_path = {p["path"]: p["value"] for p in self.sets()}
        self.assertAlmostEqual(by_path["iop/crop/left"], 1 / 6, places=6)
        self.assertAlmostEqual(by_path["iop/crop/right"], 5 / 6, places=6)
        self.assertEqual((by_path["iop/crop/top"], by_path["iop/crop/bottom"]), (0.0, 1.0))

    def test_crop_square_90_degree_orientation_swaps_width_and_height(self) -> None:
        # The Lua-reported dimensions are the RAW, unoriented sensor size
        # (6000x4000, "landscape" numbers); a 90-degree EXIF orientation
        # means the frame actually displays as a 4000x6000 portrait, so the
        # crop margins must be computed against the SWAPPED dimensions.
        self._current(6000, 4000)
        with self._orientation(6):
            r = moves.say.__wrapped__("crop square", commit=False, job="job-test")
        self.assertEqual(len(r.moves), 4)
        by_path = {p["path"]: p["value"] for p in self.sets()}
        self.assertAlmostEqual(by_path["iop/crop/top"], 1 / 6, places=6)
        self.assertAlmostEqual(by_path["iop/crop/bottom"], 5 / 6, places=6)
        self.assertEqual((by_path["iop/crop/left"], by_path["iop/crop/right"]), (0.0, 1.0))

    def test_crop_mirrored_orientation_refuses_with_a_typed_reason(self) -> None:
        self._current(6000, 4000)
        with self._orientation(2):
            with self.assertRaises(ValueUnknown) as cm:
                moves.say.__wrapped__("crop square", commit=False, job="job-test")
        self.assertEqual(cm.exception.detail.get("reason"), "mirrored_orientation")
        self.assertEqual(self.sets(), [])

    def test_crop_preset_refuses_not_pinned_unless_all_four_sides_are_pinned(self) -> None:
        with mock.patch.dict(dtbridge.ACTION_PATHS):
            del dtbridge.ACTION_PATHS[("crop", "bottom")]
            with self.assertRaises(NotPinned) as cm:
                moves.say.__wrapped__("crop square", commit=False, job="job-test")
        self.assertEqual(cm.exception.detail["refused"][0]["param"], "bottom")
        # Nothing may land before the pre-validation, including no
        # darkroom.current lookup (mirrors the teal-and-orange test above).
        self.assertEqual(self.bridge.calls, [])
