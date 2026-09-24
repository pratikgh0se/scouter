"""Phase 3 task 8: apply a preset to the open image (bounce) or another image; to a job."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, journal, presets
from scouter.errors import CommitTimeout, NoSidecar, UsageError
from scouter.types import StyleResult, StyleSetResult
from tests.test_moves import _MovesFixture


class PresetApplyTest(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.bridge.responses["list_styles"] = {"styles": [{"name": "harbour-warm"}], "count": 1}
        self.history_end = iter([14, 16])
        he = mock.patch("scouter.core.presets._history_end",
                        side_effect=lambda path: next(self.history_end))
        he.start()
        self.addCleanup(he.stop)
        self.bridge.responses["darkroom.apply_style"] = lambda p: {
            "applied": True, "name": p["name"], "image_id": p["image_id"], "view": "darkroom",
            "left_ms": 150, "returned_ms": 50}

    def test_open_image_goes_through_the_bounce_method(self) -> None:
        r = presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertIn("darkroom.apply_style", [m for m, _ in self.bridge.calls])
        self.assertNotIn("style.apply", [m for m, _ in self.bridge.calls])
        self.assertEqual((r.applied_to, r.history_end_before, r.history_end_after),
                         ("darkroom", 14, 16))
        self.assertEqual(self.commits[-1], (None, True))
        entry = journal.read_all("job-test")[-1]
        self.assertEqual((entry.verb, entry.after["history_end"]), ("preset.apply", 16))

    def test_history_that_did_not_advance_is_commit_timeout(self) -> None:
        self.history_end = iter([14, 14])
        with self.assertRaises(CommitTimeout) as cm:
            presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual((cm.exception.detail["before"], cm.exception.detail["after"]), (14, 14))

    def test_another_image_uses_the_library_path(self) -> None:
        with mock.patch("scouter.core.style.apply.__wrapped__",
                        return_value=StyleResult(name="harbour-warm", applied=1, image_id=7)) as ap:
            r = presets.apply.__wrapped__("harbour-warm", "7", job="job-test")
        ap.assert_called_once()
        self.assertEqual(r.applied_to, "library")
        self.assertNotIn("darkroom.apply_style", [m for m, _ in self.bridge.calls])

    def test_library_path_works_with_no_darkroom_image_loaded(self) -> None:
        """Fix round 1, item 1: spec §8.3 says the library path never touches
        the view. `darkroom.current` answering "nothing loaded" must not
        block applying to ANOTHER, explicitly-named image."""
        self.bridge.responses["darkroom.current"] = {"view": "lighttable"}
        with mock.patch("scouter.core.style.apply.__wrapped__",
                        return_value=StyleResult(name="harbour-warm", applied=1, image_id=7)) as ap:
            r = presets.apply.__wrapped__("harbour-warm", "7", job="job-test")
        ap.assert_called_once()
        self.assertEqual(r.applied_to, "library")

    def test_open_image_named_by_an_alternate_path_still_bounces(self) -> None:
        """Fix round 1, item 4 (controller ruling): a relative/`.`-laden
        spelling of the OPEN image's own path must still be recognised as
        the open image (and go through the bounce), not silently diverted to
        the in-memory-overwrite-risky library path."""
        alt_path = os.path.join(str(self.raw.parent), ".", self.raw.name)
        self.assertNotEqual(alt_path, str(self.raw), "the fixture actually differs textually")
        r = presets.apply.__wrapped__("harbour-warm", alt_path, job="job-test")
        self.assertEqual(r.applied_to, "darkroom")
        self.assertIn("darkroom.apply_style", [m for m, _ in self.bridge.calls])

    def test_no_history_end_refuses_before_any_bridge_call(self) -> None:
        """Fix round 1, item 5 (controller ruling): a sidecar whose
        history_end cannot be read at all is a typed refusal, never "send it
        anyway and call the result commit_timeout" (a phantom outcome)."""
        self.history_end = iter([None])
        with self.assertRaises(NoSidecar) as cm:
            presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(cm.exception.detail["path"], str(self.raw))
        self.assertNotIn("darkroom.apply_style", [m for m, _ in self.bridge.calls])

    def test_unknown_style_is_usage_with_suggestions(self) -> None:
        with self.assertRaises(UsageError) as cm:
            presets.apply.__wrapped__("harbour-wram", job="job-test")
        self.assertIn("harbour-warm", cm.exception.detail["suggestions"])

    def test_undo_of_a_preset_application_is_refused_and_marked(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        presets.apply.__wrapped__("harbour-warm", job="job-test")
        with self.assertRaises(darkroom._UnsupportedUndo) as cm:
            darkroom.undo_last.__wrapped__(job="job-test")
        self.assertIn("2 history item(s)", str(cm.exception))
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertEqual(r.undone["module"], "exposure", "the older write stays reachable")

    def test_dry_run_sends_nothing(self) -> None:
        r = presets.apply.__wrapped__("harbour-warm", dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])


class PresetApplyJobTest(_MovesFixture):
    def test_delegates_to_apply_set_unchanged(self) -> None:
        sentinel = StyleSetResult(outcome="dry_run")
        with mock.patch("scouter.core.style.apply_set.__wrapped__", return_value=sentinel) as a:
            r = presets.apply_job.__wrapped__("harbour-warm", job_slug="harbour-wedding",
                                              exclude=["1"], job="job-test")
        self.assertIs(r, sentinel)
        _, kwargs = a.call_args
        self.assertEqual((kwargs["job_slug"], kwargs["exclude"], kwargs["confirm"]),
                         ("harbour-wedding", ["1"], False))
