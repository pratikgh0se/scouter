"""Runs tests/intent/exposure_black_default_tolerance.json (Zenkai Rules
Z1). `examples` (per-value tolerance): two independent oracles -- (1) raw
arithmetic against the tolerance, computed WITHOUT calling `darkroom._close`
(fix round 1, finding 5: makes this oracle external to the code under test),
and (2) `darkroom._close` itself, the actual helper `_module_at_defaults`
calls per row. `module_examples` (fix round 1, finding 1's regression): the
production `_module_at_defaults` on a real sidecar with BOTH exposure.black
and exposure.exposure rewritten, plus a structural oracle straight from
`vocab.PARAMS` that exposure.exposure declares no neutral/default at all."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scouter.core import darkroom, vocab
from tests.scouter_fakes import with_param_value

EXAMPLES_PATH = Path(__file__).parent / "exposure_black_default_tolerance.json"
_DOC = json.loads(EXAMPLES_PATH.read_text())
EXAMPLES = _DOC["examples"]
MODULE_EXAMPLES = _DOC["module_examples"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: darktable's own declared default for exposure.black on a scene-referred
#: colour raw (src/iop/exposure.c reload_defaults(), master, fetched
#: 2026-09-27) -- hardcoded here, independently of
#: vocab.EXPOSURE_BLACK_SCENE_RAW_DEFAULT, so this test does not just check
#: the constant against itself.
_DARKTABLE_SCENE_RAW_BLACK_DEFAULT = -0.000244140625


def _sidecar_with_params(tmp_path: Path, **params: float) -> str:
    raw = tmp_path / "P6281348.ORF"
    sidecar = Path(str(raw) + ".xmp")
    text = (FIXTURES / "P6281348.ORF.xmp").read_text()
    for param, value in params.items():
        text = with_param_value(text, "exposure", param, value)
    sidecar.write_text(text)
    return str(raw)


def test_the_real_darktable_constant_lies_inside_the_tolerance() -> None:
    """Fix round 1, finding 5: an assertion against the literal darktable
    constant (not the code's own name for it), independent of every
    per-example oracle below."""
    tol = vocab.default_tolerance("exposure", "black")
    assert abs(_DARKTABLE_SCENE_RAW_BLACK_DEFAULT - 0.0) <= tol


@pytest.mark.parametrize("ex", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_exposure_black_default_tolerance(ex: dict) -> None:
    black = ex["black"]
    want = ex["expect"]["at_default"]
    tol = vocab.default_tolerance("exposure", "black")

    # Oracle 1 (external): raw arithmetic, no call into darkroom._close.
    got_1 = abs(black - 0.0) <= tol
    assert got_1 is want, (ex["id"], black, tol)

    # Oracle 2 (production): the actual helper _module_at_defaults uses.
    got_2 = darkroom._close(black, 0.0, tol=tol)
    assert got_2 is want, (ex["id"], black, tol)


@pytest.mark.parametrize("ex", MODULE_EXAMPLES, ids=[e["id"] for e in MODULE_EXAMPLES])
def test_exposure_module_mix_regression(ex: dict, tmp_path: Path) -> None:
    want = ex["expect"]["at_default"]

    # Oracle 1 (production): the real, module-wide confirmation check.
    path = _sidecar_with_params(tmp_path, exposure=ex["exposure"], black=ex["black"])
    got_1 = darkroom._module_at_defaults(path, "exposure", 0)
    assert got_1 is want, (ex["id"], ex, path)

    # Oracle 2 (structural, independent of darkroom entirely): straight
    # from vocab.PARAMS, exposure.exposure declares no neutral/default --
    # which is WHY the module can never be confirmed "at defaults", no
    # matter how close exposure.black reads to ITS default.
    row = vocab.spec("exposure", "exposure")
    assert row is not None
    assert row.neutral is None and row.default is None, row
