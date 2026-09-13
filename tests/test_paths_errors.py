import os
import unittest
from pathlib import Path

from scouter import errors, paths


class PathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_scouter_cache_env_wins(self) -> None:
        os.environ["SCOUTER_CACHE"] = "/tmp/x"
        self.assertEqual(paths.cache_root(), Path("/tmp/x"))
        self.assertEqual(paths.events_path(), Path("/tmp/x/events.jsonl"))
        self.assertEqual(paths.audit_path("j1"), Path("/tmp/x/audit/j1.jsonl"))
        self.assertEqual(paths.journal_path("j1"), Path("/tmp/x/journal/j1.jsonl"))

    def test_xdg_cache_home_is_second(self) -> None:
        os.environ.pop("SCOUTER_CACHE", None)
        os.environ["XDG_CACHE_HOME"] = "/tmp/xdg"
        self.assertEqual(paths.cache_root(), Path("/tmp/xdg/scouter"))

    def test_job_id_from_env_or_clock(self) -> None:
        os.environ["SCOUTER_JOB"] = "job-fixed"
        self.assertEqual(paths.current_job(), "job-fixed")
        os.environ.pop("SCOUTER_JOB")
        self.assertRegex(paths.current_job(), r"^job-\d{8}-\d{6}-\d+$")


class ErrorsTest(unittest.TestCase):
    def test_every_spec_error_class_exists_with_a_unique_code(self) -> None:
        expected = {
            "bridge_timeout", "wrong_view", "not_in_darkroom", "cli_wrote_nothing",
            "db_locked", "style_partial", "commit_timeout",
        }
        codes = {cls.code for cls in errors.ERROR_CLASSES}
        self.assertEqual(codes, expected)
        self.assertEqual(len(codes), len(errors.ERROR_CLASSES))

    def test_every_code_has_an_exit_code_and_a_fix(self) -> None:
        for cls in errors.ERROR_CLASSES:
            self.assertIn(cls.code, errors.EXIT_CODES, cls.__name__)
            self.assertTrue(cls.fix, f"{cls.__name__} has no fix sentence")

    def test_exit_code_for(self) -> None:
        self.assertEqual(errors.exit_code_for("ok"), 0)
        self.assertEqual(errors.exit_code_for("dry_run"), 5)
        self.assertEqual(errors.exit_code_for("error:wrong_view"), 4)
        self.assertEqual(errors.exit_code_for("error:nonsense"), 1)

    def test_to_dict_carries_code_message_fix(self) -> None:
        d = errors.WrongView("not in darkroom", detail={"view": "lighttable"}).to_dict()
        self.assertEqual(d["code"], "wrong_view")
        self.assertEqual(d["outcome"], "error")
        self.assertEqual(d["detail"], {"view": "lighttable"})
        self.assertIn("darkroom open", d["fix"])
