"""The product seam (row 436: engine generic, product adapters separate).

A product plugs in by providing an object that satisfies `Adapter`. The
engine calls only these methods; it never imports the product."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from gauntlet.case import Case


@dataclass(frozen=True)
class LayerSpec:
    name: str
    order: int
    expected_count: int
    live: bool
    p1_gate: float | None = 0.95
    title: str = ""
    #: Extra per-check gates, e.g. looks: (("confidence", ("documented",
    #: "community-consensus"), 0.90), ("confidence", ("inferred",), 0.95)).
    check_gates: tuple = ()


@dataclass
class CallRecord:
    tool: str
    args: dict
    envelope: dict | None = None
    wall_s: float = 0.0
    ms: int | None = None
    bounced: bool = False
    error: str | None = None  # "timeout", "transport", or a typed code
    health: str | None = None  # alive | starting | unresponsive (after a timeout)
    write: bool = False


@dataclass
class Observation:
    calls: list[CallRecord] = field(default_factory=list)
    before: dict = field(default_factory=dict)  # {"params": {...}, "fingerprint": str, "history_end": int}  # noqa: E501
    after: dict = field(default_factory=dict)
    stats_before: dict = field(default_factory=dict)  # {region: {stat: value}}
    stats_after: dict = field(default_factory=dict)
    floors: dict = field(default_factory=dict)  # {stat: floor}
    agent_text: str | None = None
    blocked: str | None = None  # launch | hang | hard_cap | transient | ...
    crash_reports: list[str] = field(default_factory=list)
    hang_evidence: list[str] = field(default_factory=list)
    healed: bool = False
    extra: dict = field(default_factory=dict)
    wall_s: float = 0.0


class Park(Exception):
    """Raised by a session when an external limit stops it (agent usage limit).

    `until` is a POSIX timestamp when the limit resets, or None if unknown.
    The runner records the case as parked (no result file) and moves on."""

    def __init__(self, reason: str, until: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.until = until


class RefuseRun(Exception):
    """(final fix I2): the machine is not in a state the run may go on in
    (e.g. a product process the runner did not start is running). Raised by
    an adapter's `preflight`, `open_session` or a session's `run_case`: the
    runner writes NO case result, stops the run and reports the status
    `blocked: <reason>`. A later `--resume` picks up where it stopped."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Session(Protocol):
    def run_case(self, case: Case) -> Observation: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


class Adapter(Protocol):
    """What the engine calls. `signoff_lines(run)` gets the RunDir being
    reported on (a report may run in a fresh process, so product sign-off
    state must be read from the run dir, not memory).

    Optional (looked up with getattr, default none):
    `signoff_blockers(run) -> list[str]`: product sign-off items that fail the
    release bar; the report lists them under S8 "blockers".
    `preflight(run, layer) -> str | None` (final fix I2): called before each
    layer that has cases to run; a returned reason (or a raised RefuseRun)
    stops the run with status `blocked: <reason>` and writes no case result.
    `harness_allowance_s(case) -> float` (final fix I7): seconds added to the
    case's hard cap for the harness's own work inside a case (default 0)."""

    name: str

    def layers(self) -> list[LayerSpec]: ...
    def generate(self, layer: str) -> list[Case]: ...
    def open_session(self, layer: str, run_root: Any) -> Session: ...
    def crash_count(self) -> int: ...
    def crash_reports_since(self, t: float) -> list[str]: ...
    def signoff_lines(self, run: Any = None) -> list[str]: ...  # run: the RunDir reported on
    def generation_notes(self, cases: list[Case]) -> str: ...
    def write_tools(self) -> frozenset[str]: ...
    def policy(self) -> Any: ...  # gauntlet.evaluate.Policy


def load_adapter(dotted: str) -> Adapter:
    """`package.module:factory` -> factory(). The only place a product is imported."""
    mod_name, _, attr = dotted.partition(":")
    if not attr:
        raise ValueError(f"adapter must be 'module:factory', got {dotted!r}")
    factory = getattr(importlib.import_module(mod_name), attr)
    return factory()
