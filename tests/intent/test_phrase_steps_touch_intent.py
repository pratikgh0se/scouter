"""Runs tests/intent/phrase_steps_touch.json (Zenkai Z1/Z2). Oracle A: scaled
recorded night-3 evidence. Oracle B: the resolver's own planned touch delta."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scouter.core import phrases, vocab

DOC = json.loads((Path(__file__).parent / "phrase_steps_touch.json").read_text())


def _planned_delta(phrase: str, key: str) -> float:
    module, param = key.split(".")
    res = phrases.resolve(phrase)
    (mv,) = [
        m
        for m in res.moves
        if (phrases.row_for(m).module, phrases.row_for(m).param) == (module, param)
    ]
    return abs(mv.notches * vocab.spec(module, param).step)


@pytest.mark.parametrize("ex", DOC["examples"], ids=lambda e: e["id"])
def test_touch_lands_in_the_visible_band(ex: dict) -> None:
    delta = _planned_delta(ex["phrase"], ex["key"])  # oracle B
    predicted = abs(ex["recorded_dstat"]) / ex["recorded_dparam"] * delta  # oracle A
    if ex["expect"] == "clears":
        assert ex["floor"] <= predicted <= 1.5 * ex["floor"], (predicted, ex["floor"])
    else:
        assert predicted < ex["floor"], "residual now clears its floor: promote it to 'clears'"
