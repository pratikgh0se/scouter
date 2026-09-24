"""Phase 3 task 9: the cheap closures from phase 2 (design §11)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter import PHASE, __version__, paths
from scouter.core import wb
from scouter.mcp import server


class SessionJobTest(unittest.TestCase):
    def test_session_job_is_pinned_once_and_respects_an_explicit_one(self) -> None:
        env: dict[str, str] = {}
        job = server._pin_session_job(env)
        self.assertRegex(job, r"^mcp-\d{8}-\d{6}-\d+$")
        self.assertEqual(paths.validate_job(job), job)
        self.assertEqual(env["SCOUTER_JOB"], job)
        self.assertEqual(server._pin_session_job(env), job, "a second call keeps the same job")
        named = {"SCOUTER_JOB": "pratik-teaching"}
        self.assertEqual(server._pin_session_job(named), "pratik-teaching")

    def test_an_invalid_explicit_job_is_refused(self) -> None:
        from scouter.errors import UsageError
        with self.assertRaises(UsageError):
            server._pin_session_job({"SCOUTER_JOB": "../escape"})


class WbTableVersionTest(unittest.TestCase):
    def test_version_2_is_refused_with_the_file_named(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "wb-em10iii.json").read_text())
        fixture["version"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "wb").mkdir()
            (Path(tmp) / "wb" / "em10iii.json").write_text(json.dumps(fixture))
            with mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(Path(tmp) / "config.json")}):
                with self.assertRaises(ValueError) as cm:
                    wb.load_table("em10iii")
                self.assertIn("version 2", str(cm.exception))
                self.assertIsNone(wb.kelvin([2.0, 1.0, 1.7, 1.0], "em10iii")["kelvin"])


class HostSupportScopeTest(unittest.TestCase):
    def test_plugin_install_touches_only_the_plugin_and_luarc(self) -> None:
        from tests import host_support
        touched = {p.name for p in host_support.PluginInstall().touched_paths()}
        self.assertEqual(touched, {"darktable_mcp.lua", "luarc"})

    def test_no_host_file_snapshots_the_whole_darktable_config(self) -> None:
        for path in Path(__file__).parent.glob("test_host*.py"):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"copytree\([^)]*darktable", text),
                                  "ruling R22 / P3-R18: never snapshot ~/.config/darktable wholesale")


class VersionTest(unittest.TestCase):
    def test_phase_and_version(self) -> None:
        self.assertEqual((PHASE, __version__), (3, "0.3.0"))
