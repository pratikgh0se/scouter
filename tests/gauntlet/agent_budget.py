"""Row 501 item H (Pratik's ruling): the agent pass's own budget controls,
separate from the darktable/session budgets in session.py.

1. `stratified_subset`: night 2 runs a STRATIFIED 40-case subset of the
   scenarios_agent layer instead of the full 160 -- every problem class
   (`case.meta["class"]`: in/partial/out/impossible/workflow) and every
   genre (`case.meta["genre"]`) represented at least once, a deterministic
   selection keyed on the run seed.

2. `usage_fraction` / `check_usage_budget`: the pass must park itself
   (`Park("usage_meter", ...)`) once the usage meter passes a threshold
   (60% by Pratik's ruling), resuming after the reset, rather than running
   until the CLI's own wall limit hits mid-case (item A's `LIMIT_RE` fix).

   Read-only source investigated (2026-09-27): capsule-corp's own ledger
   under `~/.local/share/capsule/` (`config.json` and its siblings) has no
   usage/meter/budget field for the Claude session/usage meter itself (only
   agent turn_budget caps, an unrelated concept) -- and per the harness's
   own forbidden-path/no-network rules this module never probes further
   than that one documented, read-only location plus an env var override
   for testing. Until a real source exists, `usage_fraction()` returns
   `None` and `check_usage_budget` never raises: the pass falls back to
   parking on the wall-limit text alone (item A), which is the explicitly
   sanctioned fallback ("if no read-only source exists, park on the
   wall-limit text only and say so")."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from gauntlet.adapter import Park

T = TypeVar("T")

#: Env var override, read first (and the only source exercised in tests):
#: a read-only, injectable stand-in for whatever ledger eventually exposes
#: the Claude usage meter as a 0..1 fraction.
USAGE_ENV = "SCOUTER_GAUNTLET_USAGE_FRACTION"
#: The one documented, read-only, on-disk candidate investigated for a
#: usage-meter field (none found there as of 2026-09-27; see module
#: docstring). Read-only: this module never writes here.
USAGE_LEDGER_PATH = Path.home() / ".local" / "share" / "capsule" / "usage.json"
DEFAULT_USAGE_THRESHOLD = 0.6


def usage_fraction() -> float | None:
    """The usage meter as a 0..1 fraction, or None when no read-only source
    has one. Never raises on a missing/malformed source -- an absent meter
    is not a harness error, it is the documented fallback case."""
    env = os.environ.get(USAGE_ENV)
    if env:
        try:
            return float(env)
        except ValueError:
            return None
    if USAGE_LEDGER_PATH.exists():
        try:
            data = json.loads(USAGE_LEDGER_PATH.read_text(encoding="utf-8"))
            frac = data.get("fraction")
            return float(frac) if frac is not None else None
        except (ValueError, OSError):
            return None
    return None


def check_usage_budget(threshold: float = DEFAULT_USAGE_THRESHOLD,
                       usage_fn: Callable[[], float | None] = usage_fraction) -> None:
    """Raises Park("usage_meter") when a real usage fraction is known and at
    or over `threshold`. A None reading (no source) never parks here --
    that is the wall-limit fallback's job (agent.py's LIMIT_RE, item A)."""
    frac = usage_fn()
    if frac is not None and frac >= threshold:
        raise Park(f"usage_meter: {frac:.0%} >= {threshold:.0%}")


def stratified_subset(items: Sequence[T], size: int, seed: int,
                      key_fn: Callable[[T], object]) -> list[T]:
    """`size` items from `items`, deterministic on `seed`: every distinct
    `key_fn(item)` value present in `items` appears at least once in the
    result (when `size` is at least the number of distinct keys), the rest
    filled by a seeded, stable pseudo-random order. Never mutates or
    reorders `items` itself; the same `(items, size, seed)` always returns
    the same selection, in the same order, on any machine."""
    if size >= len(items):
        return list(items)
    groups: dict[object, list[T]] = {}
    for it in items:
        groups.setdefault(key_fn(it), []).append(it)

    def rank(it: T) -> str:
        #: A seeded, stable sort key (sha256 of the seed + the item's own
        #: position marker) -- deterministic across runs/machines, unlike
        #: hash() or random.Random with an unstable item repr.
        marker = getattr(it, "id", None) or repr(it)
        return hashlib.sha256(f"{seed}:{marker}".encode()).hexdigest()

    for g in groups.values():
        g.sort(key=rank)
    picked: list[T] = []
    seen_keys: set[object] = set()
    #: Pass 1: one representative per stratum, in a seeded, deterministic
    #: key order (never dict insertion order, which would depend on the
    #: caller's own `items` ordering).
    for k in sorted(groups, key=lambda k: hashlib.sha256(f"{seed}:{k!r}".encode()).hexdigest()):
        picked.append(groups[k][0])
        seen_keys.add(k)
        if len(picked) >= size:
            return picked[:size]
    #: Pass 2: fill the rest from every remaining item, seeded order.
    rest = [it for k, g in groups.items() for it in g[1:]]
    rest.sort(key=rank)
    for it in rest:
        if len(picked) >= size:
            break
        picked.append(it)
    return picked[:size]
