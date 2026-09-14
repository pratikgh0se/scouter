"""Exit criterion E6: one vocabulary across core, CLI and MCP."""

from __future__ import annotations

import inspect
import unittest

from scouter import registry
from scouter.cli.main import NON_REGISTRY_COMMANDS, build_parser
from scouter.mcp import schemas

EXPECTED_VERBS = {
    "config.init",
    "config.show",
    "darkroom.open",
    "darkroom.set",
    "darkroom.nudge",
    "darkroom.enable",
    "darkroom.reset",
    "darkroom.undo_last",
    "darkroom.status",
    "darkroom.describe_history",
    "history.read",
    "history.commit",
    "history.changed",
    "scout.stats",
    "scout.preview",
    "scout.compare",
    "style.extract",
    "style.apply",
    "style.apply_set",
    "export.run",
    "export.release",
    "export.verify",
    "metrics.summary",
    "library.import",
    "library.verify",
    "library.register",
    "library.jobs",
    "library.relocate",
    "cull.propose",
    "cull.apply",
    "backup.run",
    "backup.verify",
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
        self.assertEqual(set(tools), {v.mcp_name for v in registry.load_verbs().values()})

    def test_argument_names_agree_between_the_registry_and_the_mcp_schema(self) -> None:
        tools = {tool["name"]: tool for tool in schemas.TOOL_SPECS}
        for verb in registry.load_verbs().values():
            with self.subTest(verb=verb.id):
                schema = tools[verb.mcp_name]["inputSchema"]
                self.assertEqual(
                    set(schema["properties"]),
                    {a.name for a in verb.args},
                    f"{verb.id}: the MCP schema and the registry disagree on argument names",
                )
                required = {a.name for a in verb.args if a.required}
                self.assertEqual(set(schema.get("required", [])), required)

    def test_argument_names_agree_between_the_registry_and_the_core_functions(self) -> None:
        """The third leg (final review nit): the registry is also pinned against
        the functions it actually calls.

        Without this, an ArgSpec could agree with the MCP schema and the CLI
        while naming a parameter the core function does not have -- a
        `TypeError` at call time that no parity test would catch. `job` is the
        one parameter every core function takes and no layer exposes as a verb
        argument (it comes from `--job` / $SCOUTER_JOB), so it is excluded.
        """
        for verb in registry.load_verbs().values():
            with self.subTest(verb=verb.id):
                fn = getattr(verb.fn, "__wrapped__", verb.fn)
                signature = inspect.signature(fn)
                accepted = {
                    name
                    for name, param in signature.parameters.items()
                    if name != "job" and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
                }
                self.assertEqual(
                    {a.name for a in verb.args},
                    accepted,
                    f"{verb.id}: the registry and {fn.__module__}.{fn.__name__} "
                    "disagree on argument names",
                )
                required = {
                    name
                    for name, param in signature.parameters.items()
                    if name != "job"
                    and param.default is param.empty
                    and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
                }
                self.assertEqual(
                    {a.name for a in verb.args if a.required},
                    required,
                    f"{verb.id}: the registry and {fn.__module__}.{fn.__name__} "
                    "disagree on which arguments are required",
                )
