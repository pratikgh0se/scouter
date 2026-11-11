"""Pure decision logic for the route A host probe (tests/test_host_colorcal.py).
No darktable, no I/O beyond a path name."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Smallest relative r/b change that counts as a real colour move.
NOISE_FLOOR = 0.005


@dataclass(frozen=True)
class Evidence:
    pinned: bool
    slots_ok: bool
    sentinel_live: bool
    noop_ok: bool
    illuminants: tuple
    preset: int | None
    contaminated: bool
    base_rb: float
    warm_rb: float
    cool_rb: float
    b_landed: bool
    clamped: str  # "" or "<move>: requested X K, landed Y K"
    floor: float  # absolute r/b noise floor, see noise_floor


def noise_floor(base: float, first: float, second: float) -> float:
    """max(3 x |repeat render difference|, 0.5% of base), as gauntlet/metrics.py::noise_floor."""
    return max(3.0 * abs(first - second), NOISE_FLOOR * base)


def route_verdict(ev: Evidence) -> tuple[str, str]:
    """PROMOTE | DO_NOT_PROMOTE | FLIP_SIGN plus a reason. Sidecar and side
    effect failures always outrank the rendered sign."""
    blockers = [
        (not ev.pinned, "path not pinned (absent or wedges the worker)"),
        (ev.contaminated, "oracle B started from a contaminated frame"),
        (bool(ev.clamped), f"CLAMPED, darktable moved a different temperature: {ev.clamped}"),
        (not ev.slots_ok, "slot 36 oracle failed (sentinel not at exactly slot 36)"),
        (not ev.b_landed, "oracle B write did not land at slot 36 (r/b is not evidence)"),
        (not ev.sentinel_live, "live sentinel failed (fresh value did not change slot 36)"),
        (not ev.noop_ok, "noop check failed (repeat set of the same value changed the slot)"),
        (any(i not in (2, 6) for i in ev.illuminants), f"illuminant left (2, 6): {ev.illuminants}"),
        (ev.preset != 4, f"WB preset changed to {ev.preset}"),
    ]
    for hit, reason in blockers:
        if hit:
            return "DO_NOT_PROMOTE", reason
    up = ev.base_rb + ev.floor
    down = ev.base_rb - ev.floor
    detail = (
        f"r/b base {ev.base_rb:.4f} warm {ev.warm_rb:.4f} cool {ev.cool_rb:.4f} "
        f"floor {ev.floor:.4f}"
    )
    if ev.warm_rb > up and ev.cool_rb < down:
        return "PROMOTE", detail
    if ev.warm_rb < down and ev.cool_rb > up:
        return "FLIP_SIGN", f"higher K gave lower r/b: {detail}"
    return "DO_NOT_PROMOTE", f"rendered r/b inside noise floor or mixed: {detail}"


def unique_run_dir(root: Path, pid: int, stamp: str) -> Path:
    """`<root>/<stamp>-<pid>`, which must not exist yet."""
    path = root / f"{stamp}-{pid}"
    if path.exists():
        raise FileExistsError(f"run dir already exists: {path}")
    return path
