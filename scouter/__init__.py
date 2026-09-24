"""Scouter: a live darktable editing core, CLI and MCP adapter."""

__version__ = "0.3.0"
PHASE = 3


def main() -> int:
    """Console-script entry point for `scouter`."""
    from scouter.cli.main import main as cli_main

    return cli_main()
