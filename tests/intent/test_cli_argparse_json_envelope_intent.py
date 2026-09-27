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
