"""Phase 3 task 1: the educative key, the two new error classes, journal groups."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scouter import config, errors
from scouter.core import journal
from scouter.errors import UsageError
from scouter.types import AdjustResult, UndoResult
from tests.scouter_fakes import TempCache


class EducativeConfigTest(unittest.TestCase):
    def _load(self, body: dict) -> config.Config:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(json.dumps(body))
            return config.load(str(p))

    def test_default_is_brief_with_no_file(self) -> None:
        self.assertEqual(config.load("/nonexistent/scouter/config.json").educative, "brief")

    def test_each_level_loads(self) -> None:
        for level in ("off", "brief", "full"):
            with self.subTest(level=level):
                self.assertEqual(self._load({"educative": level}).educative, level)

    def test_an_unknown_level_is_a_usage_error_naming_the_three(self) -> None:
        with self.assertRaises(UsageError) as cm:
            self._load({"educative": "verbose"})
        self.assertIn("off, brief, full", str(cm.exception))

    def test_starter_file_carries_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            config.init(str(target), confirm=True)
            self.assertEqual(json.loads(target.read_text())["educative"], "brief")


class NewErrorClassesTest(unittest.TestCase):
    def test_codes_and_exit_codes(self) -> None:
        self.assertEqual(errors.ValueUnknown("x").code, "value_unknown")
        self.assertEqual(errors.NotPinned("x").code, "not_pinned")
        self.assertEqual(errors.EXIT_CODES["value_unknown"], 20)
        self.assertEqual(errors.EXIT_CODES["not_pinned"], 21)
        self.assertIn(errors.ValueUnknown, errors.ERROR_CLASSES)
        self.assertIn(errors.NotPinned, errors.ERROR_CLASSES)
        self.assertEqual(errors.exit_code_for("error:not_pinned"), 21)

    def test_both_carry_a_fix(self) -> None:
        self.assertIn("darkroom set", errors.ValueUnknown.fix)
        self.assertIn("host", errors.NotPinned.fix)


class JournalGroupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def _begin(self):
        return journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=1,
            path="/x.orf",
            module="exposure",
            param="exposure",
            instance=0,
            before={"value": 1.0},
        )

    def test_group_defaults_to_none(self) -> None:
        self.assertIsNone(self._begin().group)

    def test_group_is_stamped_from_the_contextvar_and_reset(self) -> None:
        token = journal.GROUP.set("say-7")
        try:
            entry = self._begin()
        finally:
            journal.GROUP.reset(token)
        self.assertEqual(entry.group, "say-7")
        self.assertEqual(journal.read_all("job-test")[-1].group, "say-7")
        self.assertIsNone(self._begin().group)

    def test_an_old_journal_line_without_group_still_loads(self) -> None:
        path = journal.paths.journal_path("job-test")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "ts": "2026-09-14T00:00:00+00:00",
                    "verb": "darkroom.set",
                    "image_id": 1,
                    "path": "/x.orf",
                    "module": "exposure",
                    "param": "exposure",
                    "instance": 0,
                    "before": {},
                    "after": {"value": 1.0},
                    "undone": False,
                    "outcome": None,
                    "commit": True,
                    "note": None,
                }
            )
            + "\n"
        )
        self.assertIsNone(journal.read_all("job-test")[0].group)


class ResultTypesTest(unittest.TestCase):
    def test_adjust_result_defaults_serialise(self) -> None:
        d = AdjustResult(module="exposure", param="exposure").to_dict()
        for key in (
            "delta",
            "previous",
            "target",
            "applied",
            "clamped",
            "source",
            "committed",
            "noop",
            "summary",
            "explain",
            "unit",
        ):
            self.assertIn(key, d)

    def test_undo_result_gains_undone_group(self) -> None:
        self.assertEqual(UndoResult().to_dict()["undone_group"], [])
