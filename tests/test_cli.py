import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Iterator

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
                args = self.parser.parse_args(
                    [verb.group, verb.cli_command, *_min_args(verb)]
                )
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
        dtbridge.set_bridge(FakeBridge({
            "view.current": {"view": "lighttable"},
            "image.lookup": {"found": True, "image_id": 1, "path": raw},
            "darkroom.open": {"image_id": 1, "path": raw, "view": "lighttable",
                              "opened": False, "already_open": False},
        }))
        code = main(["darkroom", "set", "exposure", "exposure", "1.5"])
        self.assertEqual(code, 4)

    def test_dry_run_exits_5(self) -> None:
        from scouter.core import dtbridge

        raw = str(self.root / "hero.ORF")
        dtbridge.set_bridge(FakeBridge({
            "image.lookup": {"found": True, "image_id": 1, "path": raw},
            "darkroom.open": {"image_id": 1, "path": raw, "view": "darkroom",
                              "opened": False, "already_open": True},
        }))
        code = main(["darkroom", "reset", "exposure"])
        self.assertEqual(code, 5)


class UnhandledErrorTest(unittest.TestCase):
    """main() must never let an exception escape: always JSON on stdout, one
    exit code, even for a bug in a verb's own fn."""

    def setUp(self) -> None:
        os.environ.setdefault("SCOUTER_CACHE", "/tmp/scouter-cli-unhandled-test")

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
    def test_stdout_is_exactly_one_json_object(self) -> None:
        env = dict(os.environ, SCOUTER_CACHE="/tmp/scouter-cli-test")
        proc = subprocess.run(
            [sys.executable, "-m", "scouter", "doctor"],
            cwd=REPO, capture_output=True, text=True, env=env, timeout=60,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verb"], "doctor")
        self.assertIn("cache_root", payload)

    def test_metrics_summary_runs_with_no_darktable(self) -> None:
        env = dict(os.environ, SCOUTER_CACHE="/tmp/scouter-cli-test2")
        proc = subprocess.run(
            [sys.executable, "-m", "scouter", "metrics", "summary", "--since", "1d"],
            cwd=REPO, capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verb"], "metrics.summary")
        self.assertEqual(payload["layer"], "cli")
