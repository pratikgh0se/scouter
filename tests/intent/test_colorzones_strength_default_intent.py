"""Runs tests/intent/colorzones_strength_default.json (Zenkai Rules Z1).
Card t_29a9ef47 (decision rows 548/549), promotion companion to
tests/test_host_phase3.py::ProbeColorzonesStrengthDefaultHostTest. Two
independent oracles per example (Z2), mirroring
tests/intent/test_exposure_black_default_tolerance_intent.py: (1) raw
arithmetic against vocab.default_tolerance, computed WITHOUT calling into
darkroom (external to the code under test), and (2) darkroom's own
production helpers -- `_effective_current` for the no-history-entry path,
`_close` for the plain tolerance check. Neither oracle is derived from the
other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scouter.core import darkroom, vocab

EXAMPLES_PATH = Path(__file__).parent / "colorzones_strength_default.json"
_DOC = json.loads(EXAMPLES_PATH.read_text())
EXAMPLES = _DOC["examples"]


def test_no_history_entry_resolves_through_the_declared_default() -> None:
    """The exact gap fix round 0/1 diagnosed: no active colorzones history
    entry means darktable runs the module at its default."""
    ex = next(e for e in EXAMPLES if e["id"] == "no-history-entry-resolves-through-the-declared-default")

    # Oracle 1 (structural, external to darkroom): the declared default
    # itself, straight from vocab.PARAMS.
    row = vocab.spec("colorzones", "strength")
    assert row is not None
    assert row.default == ex["expect"]["value"]

    # Oracle 2 (production): darkroom._effective_current on the exact
    # raw_state shape _module_state/_read_value produce for a sidecar with
    # NO active history entry (value=None, history_end set, enabled=None --
    # see _effective_current's own docstring, row 433).
    raw_state = {"value": None, "history_end": 12, "enabled": None}
    value, source = darkroom._effective_current(raw_state, "colorzones", "strength")
    assert value == ex["expect"]["value"]
    assert source == ex["expect"]["source"]


@pytest.mark.parametrize(
    "ex", [e for e in EXAMPLES if "at_default" in e["expect"]],
    ids=[e["id"] for e in EXAMPLES if "at_default" in e["expect"]],
)
def test_colorzones_strength_default_tolerance(ex: dict) -> None:
    strength = ex["strength"]
    want = ex["expect"]["at_default"]
    tol = vocab.default_tolerance("colorzones", "strength")

    # Oracle 1 (external): raw arithmetic, no call into darkroom._close.
    got_1 = abs(strength - 0.0) <= tol
    assert got_1 is want, (ex["id"], strength, tol)

    # Oracle 2 (production): the actual helper _module_at_defaults uses.
    got_2 = darkroom._close(strength, 0.0, tol=tol)
    assert got_2 is want, (ex["id"], strength, tol)
