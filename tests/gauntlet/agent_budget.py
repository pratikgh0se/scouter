"""Row 501 item H (Pratik's ruling; fix round 1, Opus review finding 5): the
agent pass's own budget controls, separate from the darktable/session
budgets in session.py.

1. `stratified_subset`: night 2 runs a STRATIFIED 40-case subset of the
   scenarios_agent layer instead of the full 160 -- every problem class
   (`case.meta["class"]`) AND every genre (`case.meta["genre"]`) represented
   at least once, a deterministic, seeded, two-axis greedy cover (the
   generic engine, `gauntlet.subset.stratified_subset`; re-exported here as
   `scouter_stratified_subset` bound to the two scouter meta keys the
   ruling names). Fix round 1: the first cut's single composite-key pass
   filled its 40 slots one (class, genre) PAIR at a time and truncated
   before every genre was ever seen (measured on the real 160-case pool:
   up to 19 of 14 genres missing over 200 seeds) -- the generic engine's
   version covers each axis independently instead.

2. `usage_fraction` / `check_usage_budget`: the pass must park itself
   (`Park("usage_meter: ...", until=reset_at)`) once the usage meter passes
   a threshold (60% by Pratik's ruling), resuming after the reset, rather
   than running until the CLI's own wall limit hits mid-case (item A's
   `LIMIT_RE` fix, the sanctioned fallback while no real source exists).

   Real source (fix round 1, Opus finding 5): capsule-corp's
   `maintenance/usage_guard.py` reads the account's session-window
   utilization through Hermes every 10 minutes, but (as of 2026-09-27) does
   not persist it -- a capsule-corp card is being filed for a writer that
   drops `{utilization, reset_at, written_at}` to
   `~/.local/share/capsule/usage-window.json` on each tick. This module
   reads exactly that path and schema, read-only, with a staleness bound
   (`written_at` older than `STALE_S` is treated as no source at all --
   stale data must never silently gate a live run). Until the writer
   exists, the file is simply absent, `usage_fraction()` returns `None`
   and says so (this docstring), and `check_usage_budget` never raises:
   the pass falls back to parking on the wall-limit text alone (item A)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from gauntlet.adapter import Park
from gauntlet.subset import stratified_subset as _generic_stratified_subset

#: Env var override, read first (and the only source exercised in most
#: tests): a read-only, injectable stand-in for the real ledger below.
USAGE_ENV = "SCOUTER_GAUNTLET_USAGE_FRACTION"
#: capsule-corp's usage-window state (fix round 1, Opus finding 5): written
#: by maintenance/usage_guard.py's tick (a card is filed for the writer;
#: this module only ever READS it). Schema: {"utilization": 0..1,
#: "reset_at": <epoch seconds>, "written_at": <epoch seconds>}.
USAGE_WINDOW_PATH = Path.home() / ".local" / "share" / "capsule" / "usage-window.json"
DEFAULT_USAGE_THRESHOLD = 0.6
#: A reading older than this is stale -- treated as no source (never gates
#: a live run on a stopped writer's last-known number).
STALE_S = 15 * 60


def usage_fraction(*, now: float | None = None) -> tuple[float, float] | None:
    """`(utilization, reset_at)` from the real source, or `None` when no
    read-only source exists yet, the file is malformed, or the reading is
    older than `STALE_S`. Never raises -- an absent/stale meter is the
    documented fallback case, not a harness error."""
    env = os.environ.get(USAGE_ENV)
    if env:
        try:
            return float(env), time.time() + 3600
        except ValueError:
            return None
    if not USAGE_WINDOW_PATH.exists():
        return None
    try:
        data = json.loads(USAGE_WINDOW_PATH.read_text(encoding="utf-8"))
        util, reset_at, written_at = (data.get("utilization"), data.get("reset_at"),
                                      data.get("written_at"))
        if util is None or reset_at is None or written_at is None:
            return None
        now = time.time() if now is None else now
        if now - float(written_at) > STALE_S:
            return None
        return float(util), float(reset_at)
    except (ValueError, OSError, TypeError):
        return None


def check_usage_budget(threshold: float = DEFAULT_USAGE_THRESHOLD,
                       usage_fn: Callable[[], tuple[float, float] | None] = usage_fraction
                       ) -> None:
    """Raises Park("usage_meter: ...", until=reset_at) when a real,
    non-stale usage reading is known and at or over `threshold`, so the
    runner resumes after the reset (spec: Park.until). No reading (no
    source, or stale) never parks here -- that is the wall-limit
    fallback's job (agent.py's LIMIT_RE, item A)."""
    reading = usage_fn()
    if reading is None:
        return
    frac, reset_at = reading
    if frac >= threshold:
        raise Park(f"usage_meter: {frac:.0%} >= {threshold:.0%}", until=reset_at)


def scouter_stratified_subset(cases, size: int, seed: int) -> list:
    """The ruling's own two axes for scenarios_agent: `meta["class"]`
    (in/partial/out/impossible/workflow) and `meta["genre"]`. Thin binding
    over the generic, product-agnostic `gauntlet.subset.stratified_subset`."""
    return _generic_stratified_subset(
        cases, size, seed,
        key_fns=[lambda c: c.meta.get("class"), lambda c: c.meta.get("genre")])
