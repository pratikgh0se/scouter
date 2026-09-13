"""Tests for the backup launchd plist template. Renders and lints only --
no test installs the job (that is Pratik's own action)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "ops"
    / "launchd"
    / "com.pratik.scouter-backup.plist.template"
)

PLACEHOLDERS = {
    "@PYTHON@": "/usr/bin/python3",
    "@REPO@": "/Users/fake/CodeBase/scouter",
    "@VAULT@": "/Users/fake/second-brain",
    "@LOG@": "/Users/fake/Library/Logs/scouter-backup.log",
}


class LaunchdTemplateTest(unittest.TestCase):
    def test_template_exists(self) -> None:
        self.assertTrue(TEMPLATE_PATH.is_file(), TEMPLATE_PATH)

    def _rendered(self) -> str:
        text = TEMPLATE_PATH.read_text(encoding="utf-8")
        for placeholder, value in PLACEHOLDERS.items():
            text = text.replace(placeholder, value)
        return text

    def test_template_lints(self) -> None:
        rendered = self._rendered()

        self.assertIn("<key>Label</key><string>com.pratik.scouter-backup</string>", rendered)
        self.assertIn("<key>Hour</key><integer>3</integer>", rendered)
        self.assertIn("<key>Minute</key><integer>30</integer>", rendered)
        self.assertIn("<key>RunAtLoad</key><false/>", rendered)
        for value in PLACEHOLDERS.values():
            self.assertIn(value, rendered)
        self.assertNotRegex(rendered, r"@[A-Z]+@", "a placeholder survived rendering")

        plutil = shutil.which("plutil")
        if not plutil:
            self.skipTest("plutil not found on this machine")

        with tempfile.NamedTemporaryFile("w", suffix=".plist", delete=False) as fh:
            fh.write(rendered)
            tmp_path = fh.name
        try:
            result = subprocess.run([plutil, "-lint", tmp_path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_no_placeholder_survives_when_all_four_are_substituted(self) -> None:
        rendered = self._rendered()
        self.assertNotIn("@PYTHON@", rendered)
        self.assertNotIn("@REPO@", rendered)
        self.assertNotIn("@VAULT@", rendered)
        self.assertNotIn("@LOG@", rendered)


class InstallScriptTest(unittest.TestCase):
    """Sanity on the installer script's shape -- never executed (no launchctl)."""

    SCRIPT_PATH = Path(__file__).resolve().parent.parent / "ops" / "install-backup-job.sh"

    def test_install_script_exists_and_is_executable(self) -> None:
        self.assertTrue(self.SCRIPT_PATH.is_file())
        self.assertTrue(self.SCRIPT_PATH.stat().st_mode & 0o111, "script is not executable")

    def test_install_script_has_safety_flags_and_uninstall_path(self) -> None:
        text = self.SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("--uninstall", text)
        self.assertIn("bootout", text)
        self.assertIn("bootstrap", text)
        self.assertIn("plutil -lint", text)
        self.assertIn("backup_gate", text)

    def test_install_script_syntax_is_valid(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not found")
        result = subprocess.run([bash, "-n", str(self.SCRIPT_PATH)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
