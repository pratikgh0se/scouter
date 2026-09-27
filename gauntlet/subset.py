"""Deterministic, seeded, multi-axis stratified subset selection (spec-
generic: any adapter's layer may want a stratified subset instead of every
case; row 501 item H's own use is the scenarios_agent night-2 pass).

Pure and product-agnostic: `key_fns` extracts whatever axes the CALLER
cares about from an item (e.g. a Case's own meta fields); this module knows
nothing about scouter, problem classes or genres."""

from __future__ import annotations

import hashlib
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def _rank(seed: int, marker: str) -> str:
    #: A seeded, stable sort key (sha256 of the seed + the item's own
    #: marker) -- deterministic across runs/machines, unlike hash() or
    #: random.Random with an unstable item repr.
    return hashlib.sha256(f"{seed}:{marker}".encode()).hexdigest()


def _marker(item: T) -> str:
    return str(getattr(item, "id", None) or repr(item))


def stratified_subset(items: Sequence[T], size: int, seed: int,
                      key_fns: Sequence[Callable[[T], object]]) -> list[T]:
    """`size` items from `items`, deterministic on `seed`: a GREEDY,
    multi-axis cover -- every distinct value of EVERY `key_fns[i](item)`
    that exists in `items` appears at least once in the result (whenever
    `size` is large enough for that to be possible), preferring items that
    cover more than one still-missing axis value at once, before falling
    back to a seeded fill for the remaining slots.

    Fix round 1 (Opus review finding 5): a single-key (or single composite-
    key) pass-1-fill-then-truncate design only guarantees the ONE axis it
    keys on; with a `(class, genre)` composite key alone, pass 1 fills the
    quota one pair at a time and truncates before every genre (or class) is
    ever seen (measured on the real 160-case scenarios_agent pool: up to 19
    of 14 genres missing over 200 seeds). `key_fns` takes one extractor PER
    AXIS instead, and covers every axis independently.

    Never mutates or reorders `items` itself; the same `(items, size, seed,
    key_fns)` always returns the same selection, in the same order, on any
    machine."""
    if size >= len(items):
        return list(items)
    if not key_fns:
        ranked = sorted(items, key=lambda it: _rank(seed, _marker(it)))
        return ranked[:size]

    #: needed[axis] = every distinct value that axis takes across `items`;
    #: covered[axis] = the subset of those values picked so far.
    needed: list[set] = [set() for _ in key_fns]
    for it in items:
        for i, kf in enumerate(key_fns):
            needed[i].add(kf(it))
    covered: list[set] = [set() for _ in key_fns]

    remaining = sorted(items, key=lambda it: _rank(seed, _marker(it)))
    picked: list[T] = []
    #: Greedy set cover: while some axis value is still missing and slots
    #: remain, pick the (seeded-order-first) candidate that covers the most
    #: currently-uncovered axis values.
    while remaining and len(picked) < size and any(needed[i] - covered[i] for i in
                                                    range(len(key_fns))):
        best_idx, best_score = 0, -1
        for idx, it in enumerate(remaining):
            score = sum(1 for i, kf in enumerate(key_fns) if kf(it) not in covered[i])
            if score > best_score:
                best_idx, best_score = idx, score
        it = remaining.pop(best_idx)
        picked.append(it)
        for i, kf in enumerate(key_fns):
            covered[i].add(kf(it))
    #: Fill the rest, seeded order, from whatever is left.
    for it in remaining:
        if len(picked) >= size:
            break
        picked.append(it)
    return picked[:size]
