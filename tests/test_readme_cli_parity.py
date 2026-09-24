"""E12 D2: every `scouter ...` command line shown in README.md must parse with
the real argparse tree -- not just execute against a synthetic minimal-args
harness (test_layer_parity.py already covers that). The README showed
`cull propose ... --pretty` (global flag placed after the group), `style
apply-set --name ...` and `export release --job-slug ...` (options where the
registry only had positionals); all three exited 2 in the E12 live run.

This test extracts every `scouter ...` line from README's ```sh fences,
expands the doc shorthand `a | b | c` (one shared prefix, several verb/flag
alternatives) into separate commands, substitutes `<placeholder>` tokens with
a dummy string, and parses each with `build_parser()`. It never calls `main`
or a verb `fn`, so it needs no darktable, cache dir or network -- pure
argparse.
"""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

from scouter import registry
from scouter.cli.main import build_parser

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text()

_PLACEHOLDER = re.compile(r"<[^>]+>")


def _expand(line: str) -> tuple[str, list[str]]:
    """`scouter library verify X | jobs | register` -> the one full command
    the README actually spells out with real args (first alt) plus the bare
    verb names of the shorthand alternatives (`jobs`, `register`, ...),
    which are documentation shorthand for "or", not standalone runnable
    lines -- so only the first alt is asserted to fully parse; the rest are
    only asserted to be real, known subcommands of the same group.
    """
    if "|" not in line:
        return line, []
    parts = [p.strip() for p in line.split("|")]
    tokens = parts[0].split()
    prefix = " ".join(tokens[:2])  # "scouter <group>"
    first = f"{prefix} {' '.join(tokens[2:])}".strip()
    bare_verbs = [(tokens[1], p.split()[0]) for p in parts[1:]]
    return first, bare_verbs


def _readme_commands() -> tuple[list[str], list[tuple[str, str]]]:
    full: list[str] = []
    bare: list[tuple[str, str]] = []
    for fence in re.findall(r"```sh\n(.*?)```", README, re.DOTALL):
        for raw in fence.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line.startswith("scouter "):
                continue
            line = _PLACEHOLDER.sub("X", line)
            first, more_bare = _expand(line)
            full.append(first)
            bare.extend(more_bare)
    return full, bare


class ReadmeCliParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.commands, self.bare_verbs = _readme_commands()

    def test_at_least_the_documented_commands_were_found(self) -> None:
        # A canary against the extraction itself silently finding nothing.
        self.assertGreater(len(self.commands), 15, self.commands)

    def test_every_readme_command_parses(self) -> None:
        for command in self.commands:
            argv = shlex.split(command)[1:]  # drop "scouter"
            with self.subTest(command=command):
                try:
                    self.parser.parse_args(argv)
                except SystemExit as e:
                    self.fail(f"{command!r} failed to parse (exit {e.code})")

    def test_shorthand_alternatives_are_real_subcommands(self) -> None:
        cli_commands_by_group: dict[str, set[str]] = {}
        for verb in registry.load_verbs().values():
            cli_commands_by_group.setdefault(verb.group, set()).add(verb.cli_command)
        for group, verb_word in self.bare_verbs:
            with self.subTest(group=group, verb=verb_word):
                self.assertIn(verb_word, cli_commands_by_group.get(group, set()))

    def test_e12_reported_forms_specifically(self) -> None:
        for command in (
            "cull propose --job-slug harbour-wedding --pretty",
            "style apply-set --name hero-harbour --job-slug harbour-wedding --yes",
            "export release --job-slug harbour-wedding --preset client-proof --yes",
        ):
            with self.subTest(command=command):
                args = self.parser.parse_args(command.split())
                self.assertTrue(args.pretty if "pretty" in command else True)


if __name__ == "__main__":
    unittest.main()
