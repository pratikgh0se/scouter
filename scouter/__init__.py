"""Scouter: a live darktable editing core, CLI and MCP adapter."""

__version__ = "0.2.0"
PHASE = 2


def main() -> int:
    """Console-script entry point for `scouter`."""
    from scouter.cli.main import main as cli_main

    return cli_main()
