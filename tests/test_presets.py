"""Phase 3 task 7: save the session's edits as a named darktable style; list presets."""
# mutation-check: targets-predate-range 55018012923d77440ffa3291dbf35b00a3a7fe15 -- presets.save's flush (_commit attempts=1, expect_change=False; 75f68fd) is untouched by this range; only the shared _MovesFixture commit tuple grew an expect_module slot. Proof: dropping attempts=1 in presets.py fails it. :: PresetSaveTest.test_modules_default_to_this_sessions_edits

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, journal, presets
from scouter.errors import CommitTimeout, UsageError, WrongView
from scouter.types import StyleResult
from tests.test_moves import _MovesFixture
from tests.scouter_fakes import write_minimal_style_sidecar


class _PresetFixture(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.library = self.cache.root / "library"
        self.library.mkdir()
        cfg = Path(os.environ["SCOUTER_CONFIG"])
        cfg.write_text(json.dumps({"library_root": str(self.library)}))
        self.bridge.responses["list_styles"] = {"styles": [], "count": 0}
        self.extract_calls: list[dict] = []
        dtstyle = self.cache.root / "styles" / "harbour-warm.dtstyle"
        dtstyle.parent.mkdir(parents=True, exist_ok=True)
        dtstyle.write_text("<darktable_style/>")

        def fake_extract(image, name, modules, **kw):
            self.extract_calls.append({"image": image, "name": name, "modules": list(modules), **kw})
            return StyleResult(name=name, created=True, dtstyle_path=str(dtstyle),
                               operations_included=list(modules), modules_honoured=True)

        p = mock.patch("scouter.core.style.extract.__wrapped__", side_effect=fake_extract)
        p.start()
        self.addCleanup(p.stop)

    def _edit(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        darkroom.set_param.__wrapped__("sigmoid", "contrast", 1.6, commit=False, job="job-test")


class PresetSaveTest(_PresetFixture):
    def test_modules_default_to_this_sessions_edits(self) -> None:
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(self.extract_calls[0]["modules"], ["exposure", "sigmoid"])
        self.assertEqual(r.modules, ["exposure", "sigmoid"])
        self.assertEqual(self.commits[0], (1, False, None), "flush before extracting")

    def test_flush_timeout_is_a_warning(self) -> None:
        """Final review #4: a CommitTimeout from the flush is downgraded to a
        warning (like moves._precommit); the save still happens."""
        self._edit()
        with mock.patch("scouter.core.darkroom._commit",
                        side_effect=CommitTimeout("no bounce")):
            r = presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertTrue(any("flush before saving did not confirm (commit_timeout)" in w
                            for w in r.warnings), r.warnings)
        self.assertEqual(len(self.extract_calls), 1)

    def test_flush_failure_other_than_a_timeout_propagates(self) -> None:
        """Final review #4: a wrong view / dead bridge is a real failure, not
        a warning -- nothing is extracted."""
        self._edit()
        with mock.patch("scouter.core.darkroom._commit", side_effect=WrongView("not darkroom")):
            with self.assertRaises(WrongView):
                presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(self.extract_calls, [])

    def test_save_keeps_the_filename_rule(self) -> None:
        """Final review #3: only apply drops validate_style_name; save still
        writes `<name>.dtstyle`, so a name with spaces is refused there."""
        self._edit()
        with self.assertRaises(UsageError):
            presets.save.__wrapped__("Portra 400", job="job-test")
        self.assertEqual(self.extract_calls, [])

    def test_session_modules_skip_undone_and_failed_writes(self) -> None:
        self._edit()
        darkroom.undo_last.__wrapped__(single=True, job="job-test")
        self.assertEqual(presets.session_modules("job-test", 12), ["exposure"])

    def test_nothing_edited_is_usage(self) -> None:
        with self.assertRaises(UsageError):
            presets.save.__wrapped__("harbour-warm", job="job-test")

    def test_never_include_is_dropped_from_the_default(self) -> None:
        with mock.patch.object(journal, "read_all", return_value=[
            journal.Entry(seq=1, ts="t", verb="darkroom.set", image_id=12, path="p",
                          module="crop", param="cx", instance=0, after={"value": 1.0}),
            journal.Entry(seq=2, ts="t", verb="darkroom.set", image_id=12, path="p",
                          module="exposure", param="exposure", instance=0, after={"value": 1.0}),
        ]):
            self.assertEqual(presets.session_modules("job-test", 12), ["exposure"])

    def test_memory_copy_and_index(self) -> None:
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        mem = self.library / "scouter" / "memory" / "presets"
        self.assertEqual(Path(r.memory_path), mem / "harbour-warm.dtstyle")
        index = json.loads((mem / "presets.json").read_text())
        self.assertEqual(index["schema"], 1)
        self.assertEqual(index["presets"][0]["name"], "harbour-warm")
        self.assertEqual(index["presets"][0]["modules"], ["exposure", "sigmoid"])

    def test_resaving_keeps_the_old_file(self) -> None:
        self._edit()
        presets.save.__wrapped__("harbour-warm", job="job-test")
        presets.save.__wrapped__("harbour-warm", confirm=True, job="job-test")
        mem = self.library / "scouter" / "memory" / "presets"
        self.assertEqual(len(list(mem.glob("harbour-warm.*.dtstyle"))), 1)
        self.assertEqual(len(json.loads((mem / "presets.json").read_text())["presets"]), 1)

    def test_no_library_root_warns_and_still_saves(self) -> None:
        Path(os.environ["SCOUTER_CONFIG"]).write_text("{}")
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertIsNone(r.memory_path)
        self.assertTrue(any("library_root" in w for w in r.warnings))

    def test_bad_name_is_usage_before_anything(self) -> None:
        with self.assertRaises(UsageError):
            presets.save.__wrapped__("../etc/x", job="job-test")
        self.assertEqual(self.bridge.calls, [])

    def test_dry_run_makes_no_bridge_calls(self) -> None:
        r = presets.save.__wrapped__("harbour-warm", dry_run=True, job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])

    def test_explain_line(self) -> None:
        self._edit()
        r = presets.save.__wrapped__("harbour-warm", educative="brief", job="job-test")
        self.assertIn("as style 'harbour-warm'", r.explain[0])

    def test_existing_name_is_gated_before_resolving_the_image(self) -> None:
        # Controller ruling: check `style._existing_styles()` before the
        # darkroom image is resolved or anything is flushed/committed -- an
        # existing name with no confirm returns the dry-run plan without any
        # darktable write or commit.
        self.bridge.responses["list_styles"] = {"styles": [{"name": "harbour-warm"}], "count": 1}
        r = presets.save.__wrapped__("harbour-warm", job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.methods_called(), ["list_styles"])
        self.assertEqual(self.commits, [])


class PresetListTest(_PresetFixture):
    def test_list_merges_darktable_and_the_index(self) -> None:
        self._edit()
        presets.save.__wrapped__("harbour-warm", job="job-test")
        self.bridge.responses["list_styles"] = {"styles": [{"name": "someone-elses"}], "count": 1}
        r = presets.list_presets.__wrapped__(job="job-test")
        self.assertEqual(r.missing_in_darktable, ["harbour-warm"])
        self.assertEqual(r.darktable_only, ["someone-elses"])
        row = next(p for p in r.presets if p["name"] == "harbour-warm")
        self.assertEqual((row["scouter"], row["in_darktable"]), (True, False))


class PresetRealExtractTest(_MovesFixture):
    """Exercises the REAL `style.extract` (not the `_PresetFixture` fake),
    so `list_styles` has to genuinely model "absent before the import,
    present after it" rather than answer a fixed response either way."""

    def setUp(self) -> None:
        super().setUp()
        self.library = self.cache.root / "library"
        self.library.mkdir()
        cfg = Path(os.environ["SCOUTER_CONFIG"])
        cfg.write_text(json.dumps({"library_root": str(self.library)}))
        write_minimal_style_sidecar(self.raw)
        self._imported = False

        def list_styles(params: dict) -> dict:
            if self._imported:
                return {"styles": [{"name": "real-one"}], "count": 1}
            return {"styles": [], "count": 0}

        def import_file(params: dict) -> dict:
            self._imported = True
            return {"imported": True, "name": "real-one", "replaced": False}

        self.bridge.responses["list_styles"] = list_styles
        self.bridge.responses["style.import_file"] = import_file

    def test_real_extract_writes_a_real_dtstyle(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.8, commit=False, job="job-test")
        r = presets.save.__wrapped__("real-one", job="job-test")
        self.assertTrue(r.dtstyle_path)
        self.assertTrue(str(r.dtstyle_path).endswith("real-one.dtstyle"))
        self.assertIn("<operation>exposure</operation>", Path(r.dtstyle_path).read_text(encoding="utf-8"))
