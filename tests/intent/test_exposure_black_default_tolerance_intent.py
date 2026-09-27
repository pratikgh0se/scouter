"""Runs tests/intent/exposure_black_default_tolerance.json (Zenkai Rules
Z1). Two oracles per example (Z2): the tolerance function directly
(`vocab.default_tolerance`, feeding `darkroom._close`), and the production
"is this module at its defaults" check (`darkroom._module_at_defaults`) on a
real sidecar whose exposure.black param was rewritten to the reported
value -- so the same ruling is proven at the unit level and at the one
production call site that compares a live value to a declared default."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scouter.core import darkroom, vocab
from tests.scouter_fakes import with_param_value

EXAMPLES = json.loads(
    (Path(__file__).parent / "exposure_black_default_tolerance.json").read_text()
)["examples"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _sidecar_with_black(tmp_path: Path, value: float) -> str:
    raw = tmp_path / "P6281348.ORF"
    sidecar = Path(str(raw) + ".xmp")
    text = (FIXTURES / "P6281348.ORF.xmp").read_text()
    sidecar.write_text(with_param_value(text, "exposure", "black", value))
    return str(raw)


@pytest.mark.parametrize("ex", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_exposure_black_default_tolerance(ex: dict, tmp_path: Path) -> None:
    reported = ex["reported"]
    want_at_default = ex["expect"]["at_default"]

    # Oracle 1: the tolerance function directly, as `_module_at_defaults`
    # uses it.
    tol = vocab.default_tolerance("exposure", "black")
    got_1 = darkroom._close(reported, 0.0, tol=tol)
    assert got_1 is want_at_default, (ex["id"], reported, tol)

    # Oracle 2: the production check, on a real sidecar (row 495/496: this
    # is the one place a live XMP value is compared to a declared
    # neutral/default).
    path = _sidecar_with_black(tmp_path, reported)
    got_2 = darkroom._module_at_defaults(path, "exposure", 0)
    assert got_2 is want_at_default, (ex["id"], reported, path)
