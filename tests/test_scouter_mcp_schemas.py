"""Golden snapshot of Scouter's agent-facing MCP contract.

A reworded description or a changed default is a contract change the agent sees,
so it must be a deliberate edit to GOLDEN below, not a silent drift.
"""

from __future__ import annotations

import difflib
import json
import unittest

from scouter import registry
from scouter.mcp import schemas
from scouter.mcp.server import ScouterServer


def _render(specs: list[dict]) -> str:
    return json.dumps(sorted(specs, key=lambda s: s["name"]), indent=2, sort_keys=True)


class GoldenContractTest(unittest.TestCase):
    maxDiff = None

    #: Regenerate deliberately with:
    #:   python3 -c "from scouter.mcp import schemas; \
    #:     import json; print(json.dumps(sorted(schemas.TOOL_SPECS, \
    #:     key=lambda s: s['name']), indent=2, sort_keys=True))" > /tmp/golden.json
    #: then paste it here and explain the change in the commit message.
    GOLDEN_PATH = "tests/fixtures/scouter_mcp_golden.json"

    def test_served_contract_matches_the_golden_file(self) -> None:
        from pathlib import Path

        golden_file = Path(__file__).resolve().parent / "fixtures" / "scouter_mcp_golden.json"
        actual = _render(schemas.TOOL_SPECS)
        if not golden_file.exists():
            golden_file.write_text(actual)
            self.fail(
                f"golden file created at {golden_file}; review it and commit it, then "
                "re-run. It is the agent-facing contract."
            )
        expected = golden_file.read_text()
        if expected != actual:
            diff = "\n".join(
                difflib.unified_diff(
                    expected.splitlines(), actual.splitlines(), "golden", "served", lineterm=""
                )
            )
            self.fail(
                "the MCP tool contract changed. If the change is intended, update "
                f"{golden_file} in the same commit and say why.\n{diff}"
            )

    def test_golden_covers_every_registered_verb(self) -> None:
        self.assertEqual(
            {spec["name"] for spec in schemas.TOOL_SPECS},
            {v.mcp_name for v in registry.load_verbs().values()},
        )

    def test_every_schema_is_closed_and_declares_its_required_args(self) -> None:
        for spec in schemas.TOOL_SPECS:
            with self.subTest(tool=spec["name"]):
                schema = spec["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertIn("required", schema)
                for name, prop in schema["properties"].items():
                    self.assertIn("description", prop, f"{spec['name']}.{name}")

    def test_gated_tools_document_what_confirm_authorises(self) -> None:
        gated = {v.mcp_name for v in registry.load_verbs().values() if v.gated}
        for spec in schemas.TOOL_SPECS:
            if spec["name"] in gated:
                with self.subTest(tool=spec["name"]):
                    confirm = spec["inputSchema"]["properties"]["confirm"]
                    self.assertEqual(confirm["default"], False)
                    self.assertIn("required", confirm["description"])

    def test_tools_build_as_real_mcp_objects(self) -> None:
        tools = schemas.tools()
        self.assertEqual(len(tools), len(schemas.TOOL_SPECS))
        self.assertEqual(
            [tool.name for tool in tools], [spec["name"] for spec in schemas.TOOL_SPECS]
        )
        for tool in tools:
            self.assertTrue(tool.description)

    def test_unknown_tool_is_readable_output_not_a_transport_error(self) -> None:
        payload = json.loads(ScouterServer().call("nope", {}))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "error:unknown_tool")
