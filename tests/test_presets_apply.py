"""Phase 3 task 8: apply a preset to the open image (bounce) or another image; to a job."""
# mutation-check: targets-predate-range 55018012923d77440ffa3291dbf35b00a3a7fe15 -- presets.apply's one follow-up commit (f5f1e52) is untouched by this range; only the shared _MovesFixture commit tuple grew an expect_module slot. Proof: removing or flipping that _commit in presets.py fails it.

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, journal, presets
from scouter.errors import CommitTimeout, NoSidecar, UsageError, WrongView
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
        entry = journal.read_all("job-test")[-1]
        self.assertEqual((entry.verb, entry.after["history_end"]), ("preset.apply", 16))

    def test_history_already_advanced_means_no_second_commit(self) -> None:
        """Final review #2: the bounce inside darkroom.apply_style already
        made darktable write the sidecar; when history_end has advanced, no
        second commit (a second bounce) is spent, and the entry is complete."""
        r = presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(self.commits, [], "one bounce only")
        self.assertEqual(r.warnings, [])
        entry = journal.read_all("job-test")[-1]
        self.assertIsNone(entry.outcome)
        self.assertEqual(entry.after, {"history_end": 16, "style": "harbour-warm"})

    def test_history_not_yet_advanced_commits_once_then_completes(self) -> None:
        self.history_end = iter([14, 14, 16])
        r = presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(self.commits, [(None, True, None)])
        self.assertEqual(r.history_end_after, 16)
        self.assertIsNone(journal.read_all("job-test")[-1].outcome)

    def test_commit_timeout_after_an_applied_style_is_a_warning_and_undo_refuses(self) -> None:
        """Final review #2: an APPLIED style is never journal.fail'ed. A
        commit that times out is a warning, the entry is complete (advance
        unverified), and undo_last gives the §8.4 refusal instead of
        silently restoring a value underneath the style."""
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        self.history_end = iter([14, 14, 14])
        with mock.patch("scouter.core.darkroom._commit",
                        side_effect=CommitTimeout("sidecar never advanced")):
            r = presets.apply.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(r.outcome, "ok")
        self.assertIsNone(r.history_end_after)
        self.assertTrue(any("commit after applying did not confirm (commit_timeout)" in w
                            for w in r.warnings), r.warnings)
        self.assertTrue(any("could not be verified" in w for w in r.warnings), r.warnings)
        entry = journal.read_all("job-test")[-1]
        self.assertEqual((entry.verb, entry.outcome), ("preset.apply", None))
        self.assertIsNotNone(entry.after)
        sets_before = len([m for m, _ in self.bridge.calls if m == "darkroom.action"])
        with self.assertRaises(darkroom._UnsupportedUndo) as cm:
            darkroom.undo_last.__wrapped__(job="job-test")
        self.assertIn("appended some history item(s)", str(cm.exception))
        self.assertEqual(len([m for m, _ in self.bridge.calls if m == "darkroom.action"]),
                         sets_before, "nothing restored underneath the style")

    def test_a_bridge_error_leaves_a_failed_entry(self) -> None:
        def boom(p):
            raise WrongView("could not leave the darkroom")

        self.bridge.responses["darkroom.apply_style"] = boom
        with self.assertRaises(WrongView):
            presets.apply.__wrapped__("harbour-warm", job="job-test")
        entry = journal.read_all("job-test")[-1]
        self.assertEqual((entry.verb, entry.outcome), ("preset.apply", "error:wrong_view"))
        self.assertIsNone(journal.latest_open("job-test"))

    def test_a_gui_style_name_with_spaces_is_accepted(self) -> None:
        """Final review #3: apply never turns the name into a filename, so a
        GUI style like "Portra 400" (listed by preset.list as
        darktable_only) must apply; only save keeps the filename rule."""
        self.bridge.responses["list_styles"] = {"styles": [{"name": "Portra 400"}], "count": 1}
        r = presets.apply.__wrapped__("Portra 400", job="job-test")
        self.assertEqual(r.applied_to, "darkroom")
        sent = [p for m, p in self.bridge.calls if m == "darkroom.apply_style"]
        self.assertEqual(sent[0]["name"], "Portra 400")

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
