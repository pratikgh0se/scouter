"""Runs tests/intent/cli_argparse_json_envelope.json (Zenkai Rules Z1) against
the real `scouter.cli.main.main`, in-process (no subprocess: `main()` prints
straight to stdout, redirected here). Two independent oracles (Z2): the
JSON payload on stdout (what the CLI told the caller) against the
`SystemExit` code the process actually exits with (what a shell script
sees) -- neither is derived from the other."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from scouter.cli.main import main

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent / "cli_argparse_json_envelope.json").read_text())[
                "examples"]}


def _run(argv: list[str]) -> tuple[dict, int]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as ctx:
        main(argv)
    return json.loads(buf.getvalue()), ctx.value.code


def test_unknown_flag_on_a_real_verb_is_json_usage():
    ex = EXAMPLES["unknown-flag-on-a-real-verb-is-json-usage"]
    payload, code = _run(ex["input"]["argv"])
    # Oracle B: the process exit code.
    assert code == ex["expect"]["exit_code"]
    # Oracle A: the JSON envelope on stdout.
    assert payload["ok"] == ex["expect"]["ok"]
    assert payload["outcome"] == ex["expect"]["outcome"]
    assert payload["error"]["code"] == ex["expect"]["error.code"]


def test_unknown_flag_on_a_required_positional_verb_is_json_usage():
    ex = EXAMPLES["unknown-flag-on-a-required-positional-verb-is-json-usage"]
    payload, code = _run(ex["input"]["argv"])
    assert code == ex["expect"]["exit_code"]
    assert payload["ok"] == ex["expect"]["ok"]
    assert payload["outcome"] == ex["expect"]["outcome"]
    assert payload["error"]["code"] == ex["expect"]["error.code"]


def test_unknown_flag_resolves_the_real_verb_not_null():
    ex = EXAMPLES["unknown-flag-resolves-the-real-verb-not-null"]
    payload, code = _run(ex["input"]["argv"])
    # Oracle A: the resolved verb field.
    assert payload["verb"] == ex["expect"]["verb"]
    # Oracle B (independent): the exit code still matches the errors
    # module's own contract, by name, not a bare literal.
    from scouter.errors import EXIT_CODES

    assert code == EXIT_CODES["usage"]


def test_a_plain_value_error_is_also_error_usage_not_bare_usage():
    import contextlib as _contextlib

    from scouter.errors import EXIT_CODES
    from tests.test_cli import _verb_fn_replaced

    ex = EXAMPLES["a-plain-value-error-is-also-error-usage-not-bare-usage"]

    def bad_value(**kwargs):
        raise ValueError("bad since")

    buf = io.StringIO()
    with _verb_fn_replaced(ex["input"]["verb"], bad_value), _contextlib.redirect_stdout(buf):
        code = main(["metrics", "summary"])
    payload = json.loads(buf.getvalue())
    # Oracle A: the envelope shape.
    assert payload["ok"] == ex["expect"]["ok"]
    assert payload["outcome"] == ex["expect"]["outcome"]
    assert payload["error"]["code"] == ex["expect"]["error.code"]
    # Oracle B (independent): the exit code, by the errors module's own name
    # -- this branch returns normally from main(), it never raises
    # SystemExit (unlike argparse's own error() path above).
    assert code == EXIT_CODES[ex["expect"]["exit_code_name"]]
