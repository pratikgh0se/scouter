import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from scouter.cli.main import build_parser, main
from tests.scouter_fakes import FakeBridge, TempCache

REPO = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def _verb_fn_replaced(verb_id: str, fn) -> Iterator[None]:
    """Temporarily swap one registered verb's `fn` (a frozen dataclass field)."""
    from scouter import registry

    verbs = registry.load_verbs()
    original = verbs[verb_id]
    verbs[verb_id] = dataclasses.replace(original, fn=fn)
    try:
        yield
    finally:
        verbs[verb_id] = original


class ParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def test_every_verb_has_a_subcommand(self) -> None:
        from scouter import registry

        for verb in registry.load_verbs().values():
            with self.subTest(verb=verb.id):
                args = self.parser.parse_args([verb.group, verb.cli_command, *_min_args(verb)])
                self.assertEqual(args._verb, verb.id)

    def test_dry_run_and_yes_spellings(self) -> None:
        args = self.parser.parse_args(
            ["darkroom", "set", "exposure", "exposure", "1.5", "--dry-run"]
        )
        self.assertTrue(args.dry_run)
        args = self.parser.parse_args(["darkroom", "reset", "exposure", "--yes"])
        self.assertTrue(args.confirm)
        args = self.parser.parse_args(
            ["darkroom", "set", "exposure", "exposure", "1.5", "--no-commit"]
        )
        self.assertFalse(args.commit)

    def test_bad_usage_exits_2(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["darkroom", "set", "exposure"])
        self.assertEqual(ctx.exception.code, 2)

    def test_required_option_style_list_arg_is_enforced(self) -> None:
        # style.extract's `modules` is a required, non-positional list[str]
        # arg (--modules); omitting it must be a usage error (exit 2), not a
        # silent None/default that only fails once the verb itself runs.
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["style", "extract", "foo.ORF", "--name", "n"])
        self.assertEqual(ctx.exception.code, 2)


class ArgparseJsonErrorTest(unittest.TestCase):
    """PAR-cli-argparse-not-json: argparse's own `error()` (an unknown flag,
    or the top parser's leftover-arguments check) must answer the same JSON
    envelope shape as every other CLI failure -- not plain text on stderr
    with a bare exit 2 (which broke the MCP/CLI parity check on
    par-vocab.list-error and par-darkroom.undo_last-error, spec 3.6)."""

    def _run(self, argv: list[str]) -> tuple[dict, int]:
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            main(argv)
        return json.loads(buf.getvalue()), ctx.exception.code

    def test_vocab_list_with_a_bad_argument_is_a_json_usage_envelope(self) -> None:
        payload, code = self._run(["vocab", "list", "--bogus"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "error:usage")
        self.assertEqual(payload["error"]["code"], "usage")
        self.assertIn("bogus", payload["error"]["message"])
        # Review fix round 2, C-1 (finding 12): an UNRECOGNIZED-argument
        # error is raised by the TOP-level parser's own `parse_args`, whose
        # `_defaults` never had `_verb` set -- must still resolve the real
        # verb from the namespace `parse_known_args` already filled in.
        self.assertEqual(payload["verb"], "vocab.list")

    def test_darkroom_undo_last_with_a_bad_argument_is_a_json_usage_envelope(self) -> None:
        payload, code = self._run(["darkroom", "undo-last", "--bogus"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "error:usage")
        self.assertEqual(payload["error"]["code"], "usage")
        self.assertEqual(payload["verb"], "darkroom.undo_last")

    def test_missing_dual_positional_argument_resolves_the_verb_too(self) -> None:
        # main()'s own `parser.error(...)` call site for a missing
        # style.apply_set `name` (neither positional nor `--name` given).
        payload, code = self._run(["style", "apply-set"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["outcome"], "error:usage")
        self.assertEqual(payload["verb"], "style.apply_set")


def _min_args(verb) -> list[str]:
    out: list[str] = []
    for spec in verb.args:
        if not spec.required:
            continue
        value = {"int": "1", "float": "1.0"}.get(spec.type, "x")
        if spec.choices:
            value = spec.choices[0]
        if spec.positional:
            out.append(value)
        else:
            out += [f"--{spec.name.replace('_', '-')}", value]
    return out


class ExitCodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        (self.root / "hero.ORF").write_bytes(b"raw")

    def tearDown(self) -> None:
        from scouter.core import dtbridge

        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_wrong_view_exits_4_and_prints_the_fix(self) -> None:
        from scouter.core import dtbridge

        raw = str(self.root / "hero.ORF")
        dtbridge.set_bridge(
            FakeBridge(
                {
                    "view.current": {"view": "lighttable"},
                    "image.lookup": {"found": True, "image_id": 1, "path": raw},
                    "darkroom.open": {
                        "image_id": 1,
                        "path": raw,
                        "view": "lighttable",
                        "opened": False,
                        "already_open": False,
                    },
                    "darkroom.current": {
                        "image_id": 1,
                        "path": raw,
                        "view": "lighttable",
                        "source": "action_images",
                    },
                }
            )
        )
        code = main(["darkroom", "set", "exposure", "exposure", "1.5"])
        self.assertEqual(code, 4)

    def test_dry_run_exits_5(self) -> None:
        from scouter.core import dtbridge

        raw = str(self.root / "hero.ORF")
        dtbridge.set_bridge(
            FakeBridge(
                {
                    "image.lookup": {"found": True, "image_id": 1, "path": raw},
                    "darkroom.open": {
                        "image_id": 1,
                        "path": raw,
                        "view": "darkroom",
                        "opened": False,
                        "already_open": True,
                    },
                }
            )
        )
        code = main(["darkroom", "reset", "exposure"])
        self.assertEqual(code, 5)


class UnhandledErrorTest(unittest.TestCase):
    """main() must never let an exception escape: always JSON on stdout, one
    exit code, even for a bug in a verb's own fn."""

    def setUp(self) -> None:
        # A throwaway cache root, restored afterwards: a unit test must not
        # write to /tmp (or to the developer's real ~/.cache/scouter), which
        # `setdefault("/tmp/...")` used to do (final review nit).
        self.cache = TempCache()
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_an_unexpected_exception_becomes_error_json_and_exit_1(self) -> None:
        import io

        from scouter.errors import EXIT_CODES

        def boom(**kwargs):
            raise TypeError("boom")

        buf = io.StringIO()
        with _verb_fn_replaced("metrics.summary", boom), contextlib.redirect_stdout(buf):
            code = main(["metrics", "summary"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, EXIT_CODES["error"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "error")
        self.assertEqual(payload["error"]["code"], "error")
        self.assertIn("boom", payload["error"]["message"])

    def test_a_plain_value_error_becomes_error_usage_json_and_exit_2(self) -> None:
        # Triage item 14 / review fix round 2 finding 12: a verb's own
        # ValueError (e.g. metrics.summary's `--since` parse) answered
        # outcome "usage" instead of the same "error:usage" every other
        # typed usage failure gets (PAR-cli-usage-outcome) -- the MCP side
        # of the identical case (par-metrics.summary-error) answers
        # "error:usage", so this was its own parity diff.
        import io

        from scouter.errors import EXIT_CODES

        def bad_value(**kwargs):
            raise ValueError("bad since")

        buf = io.StringIO()
        with _verb_fn_replaced("metrics.summary", bad_value), contextlib.redirect_stdout(buf):
            code = main(["metrics", "summary"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, EXIT_CODES["usage"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "error:usage")
        self.assertEqual(payload["error"]["code"], "usage")
        self.assertIn("bad since", payload["error"]["message"])

    def test_keyboard_interrupt_becomes_interrupted_json_and_exit_130(self) -> None:
        import io

        def interrupt(**kwargs):
            raise KeyboardInterrupt

        buf = io.StringIO()
        with _verb_fn_replaced("metrics.summary", interrupt), contextlib.redirect_stdout(buf):
            code = main(["metrics", "summary"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 130)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "interrupted")


class SubprocessTest(unittest.TestCase):
    """The real CLI in a real subprocess. Its $SCOUTER_CACHE is a temp dir, so
    these runs never touch /tmp or the developer's own cache."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-cli-test-")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_stdout_is_exactly_one_json_object(self) -> None:
        # A SUBPROCESS, so a FakeBridge cannot be installed into it (fix
        # round 1, blocker 4). The isolation is by environment instead: the
        # bridge spool (`$XDG_CACHE_HOME/darktable-mcp`) and the installed
        # plugin are pinned under this test's own temp dir, so `doctor`'s
        # `view.current` call cannot reach the real spool or a live
        # darktable -- it fails fast with `bridge_timeout`, which is exactly
        # what `doctor` is supposed to report when darktable is absent.
        env = dict(
            os.environ,
            SCOUTER_CACHE=str(self.tmp / "doctor-cache"),
            XDG_CACHE_HOME=str(self.tmp / "xdg"),
            DARKTABLE_MCP_PLUGIN_PATH=str(self.tmp / "no-plugin" / "darktable_mcp.lua"),
        )
        proc = subprocess.run(
            [sys.executable, "-m", "scouter", "doctor"],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verb"], "doctor")
        self.assertIn("cache_root", payload)

    def test_metrics_summary_runs_with_no_darktable(self) -> None:
        env = dict(os.environ, SCOUTER_CACHE=str(self.tmp / "metrics-cache"))
        proc = subprocess.run(
            [sys.executable, "-m", "scouter", "metrics", "summary", "--since", "1d"],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verb"], "metrics.summary")
        self.assertEqual(payload["layer"], "cli")


class DoctorPhase2LinesTest(unittest.TestCase):
    """Task 14 step 4: `doctor`'s four phase-2 lines -- `config_path` and
    whether it parses, `backup_gate`, `encryption_tool`, and `library_root`
    + its free bytes.

    All four are REPORT lines: none changes `ok`, which stays a statement
    about the bridge. A config that has never been written is the correct
    state for a fresh install, and the first command a new user runs must
    not exit non-zero because of it.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-doctor-test-")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _report(self, config) -> dict:
        from scouter.cli import main as cli_main

        # A FakeBridge, always (fix round 1, blocker 4): `_doctor` calls
        # `dtbridge.current_view()`, and without one that reaches the REAL
        # `Bridge` -- writing a request file into the user's own spool and, if
        # a darktable with the plugin happened to be running, getting a real
        # answer out of their live session. `tests/conftest.py` now pins the
        # spool and the plugin path too, so the blast radius is contained
        # either way, but a unit test should not be firing bridge calls at
        # all: the fake makes the intent explicit and the test fast.
        from scouter.core import dtbridge
        from tests.scouter_fakes import FakeBridge

        dtbridge.set_bridge(FakeBridge({"view.current": {"view": "lighttable"}}))
        self.addCleanup(dtbridge.set_bridge, None)

        if config is None:
            os.environ["SCOUTER_CONFIG"] = str(self.tmp / "absent.json")
        else:
            path = self.tmp / "config.json"
            path.write_text(config if isinstance(config, str) else json.dumps(config))
            os.environ["SCOUTER_CONFIG"] = str(path)
        return cli_main._doctor()

    def test_a_missing_config_is_reported_not_an_error(self) -> None:
        report = self._report(None)
        self.assertTrue(report["config_path"].endswith("absent.json"))
        self.assertFalse(report["config_exists"])
        self.assertTrue(report["config_parses"], "defaults are not a parse failure")
        self.assertIn("backup_not_configured", report["backup_gate"])
        self.assertIsNone(report["library_root"])
        self.assertIsNone(report["library_free_bytes"])

    def test_an_unparseable_config_says_so_without_raising(self) -> None:
        report = self._report("{not valid json")
        self.assertFalse(report["config_parses"])
        self.assertIn("usage", report["config_error"])
        self.assertIn("config_unreadable", report["backup_gate"])
        self.assertEqual(report["verb"], "doctor")

    def test_a_disarmed_backup_gate_names_its_own_code_and_message(self) -> None:
        report = self._report(
            {
                "version": 1,
                "library_root": str(self.tmp / "library"),
                "backup": {"enabled": False, "target": str(self.tmp / "target")},
            }
        )
        self.assertTrue(report["backup_gate"].startswith("backup_not_configured: "))
        self.assertIn("enabled", report["backup_gate"])

    def test_library_root_and_free_bytes(self) -> None:
        library = self.tmp / "library"
        library.mkdir()
        report = self._report({"version": 1, "library_root": str(library)})
        self.assertEqual(report["library_root"], str(library))
        self.assertTrue(report["library_root_exists"])
        self.assertIsInstance(report["library_free_bytes"], int)
        self.assertGreater(report["library_free_bytes"], 0)

    def test_a_library_root_that_does_not_exist_yet_still_reports_free_bytes(self) -> None:
        """The free space that matters is the filesystem's, and a root about
        to be created shares its parent's -- reporting `null` would hide the
        number right when an import is about to need it."""
        report = self._report({"version": 1, "library_root": str(self.tmp / "not-yet")})
        self.assertFalse(report["library_root_exists"])
        self.assertIsInstance(report["library_free_bytes"], int)

    def test_the_encryption_tool_is_a_resolved_path_or_none(self) -> None:
        report = self._report(
            {"version": 1, "library_root": str(self.tmp / "l"), "backup": {"encryption": "none"}}
        )
        self.assertEqual(report["encryption"], "none")
        self.assertIsNone(report["encryption_tool"], "'none' resolves no tool")

        report = self._report(
            {
                "version": 1,
                "library_root": str(self.tmp / "l"),
                "backup": {"encryption": "openssl"},
            }
        )
        self.assertEqual(report["encryption"], "openssl")
        tool = report["encryption_tool"]
        # A resolved PATH, not the word "openssl": which binary will run is
        # the fact a doctor line exists to give.
        self.assertTrue(tool is None or tool.endswith("openssl"), tool)
        if tool is not None:
            self.assertTrue(tool.startswith("/"), tool)

    def test_the_phase_2_lines_never_change_ok(self) -> None:
        missing = self._report(None)
        good = self._report({"version": 1, "library_root": str(self.tmp / "l")})
        self.assertEqual(missing["ok"], missing["bridge"] == "ok")
        self.assertEqual(good["ok"], good["bridge"] == "ok")


class DoctorUsesBackupsOwnResolverTest(unittest.TestCase):
    """Fix round 1, minor a: `doctor`'s `encryption_tool` line must report the
    binary `backup.run` would actually run, not a second copy of the same
    preference order that can drift away from it."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-doctor-tool-")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self._env = dict(os.environ)
        config = self.tmp / "config.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "library_root": str(self.tmp / "library"),
                    "backup": {"encryption": "openssl"},
                }
            )
        )
        os.environ["SCOUTER_CONFIG"] = str(config)

        from scouter.core import dtbridge
        from tests.scouter_fakes import FakeBridge

        dtbridge.set_bridge(FakeBridge({"view.current": {"view": "lighttable"}}))
        self.addCleanup(dtbridge.set_bridge, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_it_calls_backup_openssl(self) -> None:
        from unittest import mock

        from scouter.cli import main as cli_main

        with mock.patch(
            "scouter.core.backup._openssl", return_value="/sentinel/openssl"
        ) as resolver:
            report = cli_main._doctor()
        self.assertEqual(report["encryption_tool"], "/sentinel/openssl")
        resolver.assert_called_once()

    def test_a_missing_binary_is_a_report_value_not_a_raise(self) -> None:
        from unittest import mock

        from scouter.cli import main as cli_main
        from scouter.errors import EncryptionUnavailable

        with mock.patch(
            "scouter.core.backup._openssl",
            side_effect=EncryptionUnavailable("no openssl anywhere"),
        ):
            report = cli_main._doctor()
        self.assertIsNone(report["encryption_tool"])
        self.assertEqual(report["encryption"], "openssl")
        self.assertEqual(report["verb"], "doctor", "doctor still produced a report")


class DoctorPluginPathHonoursTheOverrideTest(unittest.TestCase):
    """Fix round 2, item 2: `doctor`'s plugin line must come from the SAME
    resolver the bridge uses.

    It was recomputed from `Path.home()`, so with `DARKTABLE_MCP_PLUGIN_PATH`
    set -- which `tests/conftest.py` now always does for the non-host suite --
    `doctor` reported a path the bridge was not looking at. A doctor line that
    contradicts the thing it reports on is worse than no line: it sends the
    reader to the wrong file.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-doctor-plugin-")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self._env = dict(os.environ)

        from scouter.core import dtbridge
        from tests.scouter_fakes import FakeBridge

        dtbridge.set_bridge(FakeBridge({"view.current": {"view": "lighttable"}}))
        self.addCleanup(dtbridge.set_bridge, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _report(self) -> dict:
        from scouter.cli import main as cli_main

        return cli_main._doctor()

    def test_doctor_reports_the_overridden_path(self) -> None:
        plugin = self.tmp / "elsewhere" / "lua" / "darktable_mcp.lua"
        os.environ["DARKTABLE_MCP_PLUGIN_PATH"] = str(plugin)

        report = self._report()
        self.assertEqual(report["plugin_path"], str(plugin))
        self.assertFalse(report["plugin_installed"])

        plugin.parent.mkdir(parents=True)
        plugin.write_text("-- fake")
        self.assertTrue(self._report()["plugin_installed"])

    def test_doctor_and_the_bridge_never_disagree(self) -> None:
        """The property, asserted directly rather than via the string."""
        from darktable_mcp.bridge.client import plugin_path as bridge_plugin_path

        os.environ["DARKTABLE_MCP_PLUGIN_PATH"] = str(self.tmp / "a" / "b.lua")
        self.assertEqual(self._report()["plugin_path"], str(bridge_plugin_path()))

        os.environ.pop("DARKTABLE_MCP_PLUGIN_PATH", None)
        self.assertEqual(self._report()["plugin_path"], str(bridge_plugin_path()))
