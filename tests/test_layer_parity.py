"""Exit criterion E6: one vocabulary across core, CLI and MCP."""

from __future__ import annotations

import unittest

from scouter import registry
from scouter.cli.main import NON_REGISTRY_COMMANDS, build_parser
from scouter.mcp import schemas

EXPECTED_VERBS = {
    "darkroom.open", "darkroom.set", "darkroom.nudge", "darkroom.enable",
    "darkroom.reset", "darkroom.undo_last", "darkroom.status",
    "darkroom.describe_history", "history.read", "history.commit",
    "scout.stats", "scout.preview", "scout.compare",
    "style.extract", "style.apply", "export.run", "metrics.summary",
}


class ParityTest(unittest.TestCase):
    maxDiff = None

    def test_the_registry_is_exactly_the_phase_1_verb_table(self) -> None:
        self.assertEqual(set(registry.load_verbs()), EXPECTED_VERBS)

    def test_the_cli_exposes_every_verb(self) -> None:
        parser = build_parser()
        groups = parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
        for verb in registry.load_verbs().values():
            with self.subTest(verb=verb.id):
                self.assertIn(verb.group, groups)
                commands = groups[verb.group]._subparsers._group_actions[0].choices
                self.assertIn(verb.cli_command, commands)

    def test_the_cli_exposes_nothing_extra(self) -> None:
        parser = build_parser()
        groups = set(parser._subparsers._group_actions[0].choices)
        expected = {v.group for v in registry.load_verbs().values()}
        self.assertEqual(groups - expected, set(NON_REGISTRY_COMMANDS))

    def test_the_mcp_tool_table_matches_the_registry(self) -> None:
        tools = {tool["name"]: tool for tool in schemas.TOOL_SPECS}
        self.assertEqual(
            set(tools), {v.mcp_name for v in registry.load_verbs().values()}
        )

    def test_argument_names_agree_between_the_registry_and_the_mcp_schema(self) -> None:
        tools = {tool["name"]: tool for tool in schemas.TOOL_SPECS}
        for verb in registry.load_verbs().values():
            with self.subTest(verb=verb.id):
                schema = tools[verb.mcp_name]["inputSchema"]
                self.assertEqual(
                    set(schema["properties"]), {a.name for a in verb.args},
                    f"{verb.id}: the MCP schema and the registry disagree on argument names",
                )
                required = {a.name for a in verb.args if a.required}
                self.assertEqual(set(schema.get("required", [])), required)
