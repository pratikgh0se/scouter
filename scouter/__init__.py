"""Scouter: a live darktable editing core, CLI and MCP adapter."""

import time

__version__ = "0.3.0"
PHASE = 3

#: Review round 1, should-fix (D3, row 407): captured as close to process
#: start as Scouter itself can measure -- this module is the first thing a
#: `scouter ...` CLI invocation or `scouter-mcp` server imports. `metrics.
#: instrumented` reports each call's `startup_ms` (this call's own "verb
#: start" minus this), so a future stall that happens BEFORE a verb's body
#: ever runs (a stalled bridge handshake, a contended lock, an import
#: that hung) is visible in the event line even though it happened
#: entirely outside the verb's own `ms` timing -- exactly the gap the E12
#: live run's two 15-18 minute stalls fell into (D3's root cause is still
#: open; this only makes the NEXT one attributable).
_PROCESS_STARTED_MONOTONIC = time.monotonic()


def main() -> int:
    """Console-script entry point for `scouter`."""
    from scouter.cli.main import main as cli_main

    return cli_main()
