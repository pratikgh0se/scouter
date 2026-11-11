"""Runs tests/intent/parity_commit_flag.json (Zenkai Rules Z1). Two oracles
(Z2): the literal argv list `cli_argv` builds (oracle A) against the CLI's
own `build_parser()` actually accepting it (oracle B, no `SystemExit`) --
independent of each other, so a fix that satisfies one but not the other
still shows red."""

from __future__ import annotations

import json
from pathlib import Path

from scouter.cli.main import build_parser
from tests.gauntlet import exec_parity

EXAMPLES = {
    e["id"]: e
    for e in json.loads((Path(__file__).parent / "parity_commit_flag.json").read_text())["examples"]
}


def test_commit_true_emits_no_flag():
    ex = EXAMPLES["commit-true-emits-no-flag"]
    argv = exec_parity.cli_argv(ex["input"]["verb"], ex["input"]["args"])
    # Oracle A: the flag list itself.
    assert ex["expect"]["argv_excludes"] not in argv
    # Oracle B: the real parser accepts the resulting argv (no SystemExit).
    build_parser().parse_args(argv)


def test_commit_false_still_emits_no_commit():
    ex = EXAMPLES["commit-false-still-emits-no-commit"]
    argv = exec_parity.cli_argv(ex["input"]["verb"], ex["input"]["args"])
    assert ex["expect"]["argv_includes"] in argv
    args = build_parser().parse_args(argv)
    assert args.commit is False
