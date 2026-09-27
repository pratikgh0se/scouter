"""Product close, item 1: `scouter setup` / `scouter setup --check`.

Everything here runs against a temp plugin path, a temp config and a
FakeBridge: never the real ~/.config/darktable, ~/.config/scouter, the real
bridge spool or Claude Code's own config.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scouter.cli import main as cli_main
from scouter.cli import setup
from scouter.core import dtbridge
from scouter.errors import BridgeTimeout
from tests.scouter_fakes import FakeBridge

REPO = Path(__file__).resolve().parent.parent


class SetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="scouter-setup-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.plugin = self.tmp / "dtconfig" / "lua" / "darktable_mcp.lua"
        self.luarc = self.tmp / "dtconfig" / "luarc"
        env = mock.patch.dict(
            os.environ,
            {
                "DARKTABLE_MCP_PLUGIN_PATH": str(self.plugin),
                "SCOUTER_CONFIG": str(self.tmp / "scouter" / "config.json"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.bridge = FakeBridge({"view.current": {"view": "lighttable"}})
        dtbridge.set_bridge(self.bridge)
        self.addCleanup(dtbridge.set_bridge, None)

    def items(self, report: dict) -> dict:
        return {i["item"]: i for i in report["items"]}

    def test_check_changes_nothing_and_reports_missing(self) -> None:
        report = setup.run(check=True)
        items = self.items(report)
        self.assertEqual(items["plugin"]["status"], "missing")
        self.assertEqual(items["luarc"]["status"], "missing")
        self.assertFalse(self.plugin.exists())
        self.assertFalse(self.luarc.exists())
        self.assertFalse((self.tmp / "scouter").exists(), "--check must not create the wb dir")
        self.assertFalse(report["ok"])

    def test_setup_installs_persistently_and_is_idempotent(self) -> None:
        first = self.items(setup.run(check=False))
        self.assertEqual(first["plugin"]["status"], "installed")
        self.assertEqual(first["luarc"]["status"], "installed")
        self.assertTrue(self.plugin.is_file())
        second = self.items(setup.run(check=False))
        self.assertEqual(second["plugin"]["status"], "ok")
        self.assertEqual(second["luarc"]["status"], "ok")
        self.assertEqual(self.luarc.read_text().count('require "darktable_mcp"'), 1)
        # Persistent: nothing uninstalls on exit.
        self.assertTrue(self.plugin.is_file())

    def test_a_stale_plugin_is_reported_then_updated(self) -> None:
        setup.run(check=False)
        self.plugin.write_text("-- an older plugin\n")
        self.assertEqual(self.items(setup.run(check=True))["plugin"]["status"], "stale")
        self.assertEqual(self.items(setup.run(check=False))["plugin"]["status"], "updated")

    def test_measured_wb_tables_are_installed_but_never_overwritten(self) -> None:
        items = self.items(setup.run(check=False))
        table = self.tmp / "scouter" / "wb" / "em10iii.json"
        self.assertTrue(table.is_file())
        self.assertIn("em10iii", items["wb_tables"]["detail"])
        table.write_text('{"mine": true}')
        setup.run(check=False)
        self.assertEqual(table.read_text(), '{"mine": true}')

    def test_packaged_table_matches_the_measured_fixture(self) -> None:
        packaged = json.loads((REPO / "scouter" / "data" / "wb" / "em10iii.json").read_text())
        fixture = json.loads((REPO / "tests" / "fixtures" / "wb-em10iii.json").read_text())
        self.assertEqual(packaged, fixture)

    def test_doctor_is_reported_and_decides_ok(self) -> None:
        report = setup.run(check=False)
        doctor = self.items(report)["doctor"]
        self.assertEqual(doctor["status"], "ok")
        self.assertTrue(report["ok"])
        self.bridge.raises["view.current"] = BridgeTimeout("no darktable")
        report = setup.run(check=True)
        self.assertEqual(self.items(report)["doctor"]["status"], "bridge_timeout")
        self.assertFalse(report["ok"])
        self.assertTrue(any("restart darktable" in n for n in report["next"]))

    def test_doctor_is_optional_in_install_mode_and_required_under_check(self) -> None:
        """Pratik's ruling, row 496 item 6 (night1-parity-install-guide-triage.md
        rank 5/'needs Pratik' 2): a fresh machine's first `scouter setup`
        (install mode, check=False) must not fail purely because no
        darktable is running yet -- it completes and prints what `doctor`
        will verify once one is. `scouter setup --check` keeps doctor
        required, exactly as before."""
        self.bridge.raises["view.current"] = BridgeTimeout("no darktable")
        report = setup.run(check=False)
        doctor = self.items(report)
        self.assertEqual(doctor["doctor"]["status"], "bridge_timeout")
        self.assertTrue(report["ok"], report)  # doctor down does not fail plain setup
        self.assertTrue(any("doctor" in n and "verif" in n for n in report["next"]), report["next"])

        report = setup.run(check=True)
        self.assertFalse(report["ok"])  # --check still requires doctor

    def test_the_claude_mcp_add_command_is_stdio_with_absolute_paths(self) -> None:
        cmd = setup.mcp_command(repo_root=Path("/r/scouter"), python="/r/scouter/.venv/bin/python")
        self.assertEqual(
            cmd,
            "claude mcp add --transport stdio --scope user --env PYTHONPATH=/r/scouter "
            "scouter -- /r/scouter/.venv/bin/python -m scouter.mcp.server",
        )
        snippet = setup.mcp_json(repo_root=Path("/r/scouter"), python="/r/scouter/.venv/bin/python")
        self.assertEqual(
            snippet,
            {"mcpServers": {"scouter": {
                "type": "stdio",
                "command": "/r/scouter/.venv/bin/python",
                "args": ["-m", "scouter.mcp.server"],
                "env": {"PYTHONPATH": "/r/scouter"},
            }}},
        )

    def test_report_carries_the_command_and_the_snippet(self) -> None:
        report = setup.run(check=True)
        self.assertTrue(report["claude_mcp_add"].startswith("claude mcp add --transport stdio"))
        self.assertIn("mcpServers", report["mcp_json"])
        self.assertTrue(Path(report["mcp_json"]["mcpServers"]["scouter"]["command"]).is_absolute())

    def test_cli_setup_check_exit_code_and_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main.main(["setup", "--check"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["verb"], "setup")
        self.assertEqual(code, 1)
        self.assertFalse(self.plugin.exists())

    def test_cli_setup_installs(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main.main(["setup"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(self.plugin.is_file())


if __name__ == "__main__":
    unittest.main()
