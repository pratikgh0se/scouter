"""Runs tests/intent/setup_mcp_add_order.json (Zenkai Rules Z1) against the real
`scouter.cli.setup`. Two oracles (Z2): the tokenised printed command against
the independently built `mcpServers` JSON block."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from unittest import mock

from scouter.cli import setup
from scouter.core import dtbridge
from tests.scouter_fakes import FakeBridge

EXAMPLES = {e["id"]: e for e in
            json.loads((Path(__file__).parent / "setup_mcp_add_order.json").read_text())[
                "examples"]}


def _build(ex: dict) -> tuple[str, dict]:
    kw = {"repo_root": Path(ex["input"]["repo_root"]), "python": ex["input"]["python"]}
    return setup.mcp_command(**kw), setup.mcp_json(**kw)


def test_name_follows_add_before_env():
    ex = EXAMPLES["name-follows-add-before-env"]
    cmd, _ = _build(ex)
    toks = shlex.split(cmd)
    assert toks[toks.index("add") + 1] == ex["expect"]["token_after_add"]
    assert toks.index("scouter") < toks.index("--env")
    assert toks.index("--") > toks.index("--env")
    assert toks[toks.index("--") + 1] == ex["input"]["python"]


def test_json_block_matches_the_command():
    ex = EXAMPLES["json_block_matches_the_command"]
    cmd, block = _build(ex)
    entry = block["mcpServers"]["scouter"]
    assert entry["env"]["PYTHONPATH"] == ex["expect"]["env_value_in_json"]
    assert entry["command"] == ex["expect"]["command_in_json"]
    assert entry["args"] == ex["expect"]["args_in_json"]
    # Oracle B: the same values appear in the command's tokens.
    toks = shlex.split(cmd)
    assert f"PYTHONPATH={entry['env']['PYTHONPATH']}" in toks
    assert toks[toks.index("--") + 1:] == [entry["command"], *entry["args"]]


def test_setup_report_prints_the_working_order():
    ex = EXAMPLES["setup_report_prints_the_working_order"]
    with tempfile.TemporaryDirectory(prefix="scouter-mcp-order-") as tmp:
        env = {"DARKTABLE_MCP_PLUGIN_PATH": f"{tmp}/dt/lua/darktable_mcp.lua",
               "SCOUTER_CONFIG": f"{tmp}/scouter/config.json"}
        dtbridge.set_bridge(FakeBridge({"view.current": {"view": "lighttable"}}))
        try:
            with mock.patch.dict(os.environ, env):
                report = setup.run(check=True)
        finally:
            dtbridge.set_bridge(None)
    assert report["claude_mcp_add"].startswith(ex["expect"]["startswith"])
    toks = shlex.split(report["claude_mcp_add"])
    assert toks[toks.index("add") + 1] in report["mcp_json"]["mcpServers"]
