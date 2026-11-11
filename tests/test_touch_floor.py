"""Touch-floor ruling (row: htc-touch-floor, provisional/reversible).

Every vocabulary phrase's `touch` magnitude (INTENSITY factor 0.5, the
smallest ladder rung -- see phrases.py) must land at or above the visible
floor for its parameter: the gauntlet evaluator's M5 stat floor
(`gauntlet.metrics.FLOOR_MIN`) for the stat that phrase's `bare` case is
checked against (`tests/gauntlet/layers/coverage.py:PHRASE_STAT`), scaled to
param-space by the measured sensitivity in
`tests/fixtures/touch_floor_sensitivity.json` (generated from the recorded
night-1 gauntlet evidence by `tools/gen_touch_floor_sensitivity.py` -- not
hand-copied).

`test_vocab.py::VocabShapeTest` enforces `step <= (hi - lo) / 4` for every
reachable row (an existing Scouter safety invariant, untouched here). Four
(param, body) pairs cannot clear their floor within that invariant given the
current clamp and the dark/muted olympus hero frame -- the same physical
limit `htc-triage-coverage.md`'s D-bare-step-below-perceptual-floor cluster
flagged as needing a brighter/more chromatic hero or a rescaled M5 floor
(Pratik's roadmap call). Those are named explicitly in KNOWN_RESIDUAL below
so a regression (a residual case that starts passing, or a newly-short one)
is still caught.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scouter.core import phrases, vocab

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "touch_floor_sensitivity.json"
SENSITIVITY = json.loads(FIXTURE.read_text())

#: The 25 P0 case ids from htc-triage-coverage.md's
#: D-bare-step-below-perceptual-floor cluster (all "-bare", both hero
#: bodies), and the (phrase key, body) pairs still short of their floor
#: after this fix, with the reason -- documented, not silently skipped.
#:
#: Review round 1 (I6): each entry is now `{"residual": ..., "why": ...}`,
#: not a bare string, so the taxonomy classification lives somewhere a
#: reader (or the report) can find it without re-deriving it. The marker
#: is `"step-below-floor"`, not `"clamp"`: I2's own rescore of the real
#: night-1 recordings (scratchpad/n1rev_d.py / n1rev_d2.py, all 125
#: D-bare-step-below-perceptual-floor rows) found every one of these moves
#: `clamped: false` -- the "cap" named below is the vocabulary's own
#: `step <= (hi-lo)/4` invariant (or a soft range), never a clamp any move
#: actually reaches at runtime (the hero starts at neutral). Calling it
#: `"clamp"` here would misname the very thing I2 exists to correct; the
#: evaluator's matching outcome is `imperceptible_step`
#: (gauntlet/evaluate.py `_imperceptible_step_residual`, row 475).
KNOWN_RESIDUAL = {
    ("deepen_shadows", "olympus"): {
        "residual": "step-below-floor",
        "why": "shadows_luminance touch capped at the +-0.25 clamp (0.0625 < 0.50 needed)",
    },
    ("deepen_shadows", "sony_a7"): {
        "residual": "step-below-floor",
        "why": "shadows_luminance touch capped at the +-0.25 clamp (0.0625 < 0.08 needed)",
    },
    ("lift_shadows", "olympus"): {
        "residual": "step-below-floor",
        "why": "shadows_luminance touch capped at the +-0.25 clamp (0.0625 < 0.50 needed)",
    },
    ("lift_shadows", "sony_a7"): {
        "residual": "step-below-floor",
        "why": "shadows_luminance touch capped at the +-0.25 clamp (0.0625 < 0.08 needed)",
    },
    ("more_vibrance", "olympus"): {
        "residual": "step-below-floor",
        "why": "global_vibrance touch capped by the step<=(hi-lo)/4 invariant (0.125 < 0.58 needed)",
    },
    ("more_vibrance", "sony_a7"): {
        "residual": "step-below-floor",
        "why": "global_vibrance touch capped by the step<=(hi-lo)/4 invariant (0.125 < 0.45 needed)",
    },
    ("less_vibrance", "olympus"): {
        "residual": "step-below-floor",
        "why": "global_vibrance touch capped by the step<=(hi-lo)/4 invariant (0.125 < 0.58 needed)",
    },
    ("less_vibrance", "sony_a7"): {
        "residual": "step-below-floor",
        "why": "global_vibrance touch capped by the step<=(hi-lo)/4 invariant (0.125 < 0.45 needed)",
    },
    ("cool_shadows", "olympus"): {
        "residual": "step-below-floor",
        "why": "shadows_chroma touch capped at the 0-0.3 clamp (0.0375 < 0.15 needed)",
    },
    ("teal_orange", "olympus"): {
        "residual": "step-below-floor",
        "why": "shadows_chroma touch capped at the 0-0.3 clamp (0.0375 < 0.31 needed)",
    },
}

#: The 25 P0 case ids (see htc-triage-coverage.md), spelled out so the test
#: fails loudly if the cluster's membership ever changes.
P0_CASE_IDS = (
    "cov-phrase-brighter_highlights-olympus-bare",
    "cov-phrase-brighter_highlights-sony_a7-bare",
    "cov-phrase-brighter_midtones-olympus-bare",
    "cov-phrase-cool_shadows-olympus-bare",
    "cov-phrase-cool_shadows-sony_a7-bare",
    "cov-phrase-deepen_shadows-olympus-bare",
    "cov-phrase-deepen_shadows-sony_a7-bare",
    "cov-phrase-less_chroma-olympus-bare",
    "cov-phrase-less_chroma-sony_a7-bare",
    "cov-phrase-less_saturation-olympus-bare",
    "cov-phrase-less_saturation-sony_a7-bare",
    "cov-phrase-less_vibrance-olympus-bare",
    "cov-phrase-less_vibrance-sony_a7-bare",
    "cov-phrase-lift_shadows-olympus-bare",
    "cov-phrase-lift_shadows-sony_a7-bare",
    "cov-phrase-more_chroma-olympus-bare",
    "cov-phrase-more_chroma-sony_a7-bare",
    "cov-phrase-more_saturation-olympus-bare",
    "cov-phrase-more_saturation-sony_a7-bare",
    "cov-phrase-more_vibrance-olympus-bare",
    "cov-phrase-more_vibrance-sony_a7-bare",
    "cov-phrase-tame_highlights-olympus-bare",
    "cov-phrase-tame_highlights-sony_a7-bare",
    "cov-phrase-teal_orange-olympus-bare",
    "cov-phrase-teal_orange-sony_a7-bare",
)


def _case_key(case_id: str) -> tuple[str, str]:
    rest = case_id[len("cov-phrase-") : -len("-bare")]
    for body in ("olympus", "sony_a7"):
        if rest.endswith(f"-{body}"):
            return rest[: -len(f"-{body}")], body
    raise AssertionError(case_id)


def _touch_delta(module: str, param: str) -> float:
    """The raw (pre-clamp) magnitude of a single 'a touch' adjust move on
    `module.param`: INTENSITY's 0.5 factor times the row's notch step."""
    spec = vocab.spec(module, param)
    touch_factor = min(f for _, f in phrases.INTENSITY)
    return abs(1.0 * touch_factor * spec.step)


class TouchFloorTest(unittest.TestCase):
    def test_p0_case_ids_match_the_triage_cluster(self) -> None:
        self.assertEqual(len(P0_CASE_IDS), 25)
        self.assertEqual(len(set(P0_CASE_IDS)), 25)

    def test_touch_clears_the_floor_or_is_a_named_residual(self) -> None:
        for case_id in P0_CASE_IDS:
            key, body = _case_key(case_id)
            with self.subTest(case=case_id):
                entry = SENSITIVITY[key]
                body_ev = entry["bodies"][body]
                floor = body_ev["floor"]
                sens = abs(body_ev["sensitivity"])
                touch = _touch_delta(entry["module"], entry["param"])
                clamped_touch, _ = vocab.clamp(vocab.spec(entry["module"], entry["param"]), touch)
                achieved_stat_delta = sens * clamped_touch
                if (key, body) in KNOWN_RESIDUAL:
                    self.assertLess(
                        achieved_stat_delta,
                        floor,
                        f"{case_id} was a documented residual "
                        f"({KNOWN_RESIDUAL[(key, body)]['why']}) "
                        "but now clears its floor -- update KNOWN_RESIDUAL",
                    )
                else:
                    self.assertGreaterEqual(
                        achieved_stat_delta,
                        floor,
                        f"{case_id}: touch moves {entry['module']}.{entry['param']} by "
                        f"{clamped_touch}, predicted stat delta {achieved_stat_delta:.4f} "
                        f"< floor {floor} for stat {entry['stat']!r}",
                    )

    def test_every_reachable_param_touch_step_is_positive_and_within_the_safety_cap(self) -> None:
        for spec in vocab.PARAMS:
            if not spec.reachable:
                continue
            with self.subTest(row=f"{spec.module}.{spec.param}"):
                self.assertGreater(spec.step, 0)
                # A toggle's one "notch" is the whole 0/1 flip, not a
                # quarter-range slider move (Task 33, row 437 family 2) --
                # still checked, just against the whole range, not a quarter.
                if spec.param != "__enabled":
                    self.assertLessEqual(spec.step, (spec.hi - spec.lo) / 4)
                else:
                    self.assertEqual(spec.step, spec.hi - spec.lo)


class LadderMonotonicTest(unittest.TestCase):
    """touch < bit (bare/no intensity word) < lot, for every phrase with an
    adjustable move (spec: 'touch < bit < lot, full stop')."""

    def test_intensity_factors_are_ordered(self) -> None:
        touch = min(f for _, f in phrases.INTENSITY)
        lot = max(f for _, f in phrases.INTENSITY)
        bare = 1.0  # no intensity word: phrases.resolve's default factor
        self.assertLess(touch, bare)
        self.assertLess(bare, lot)

    def test_ladder_is_monotonic_for_every_adjustable_move(self) -> None:
        touch_factor = min(f for _, f in phrases.INTENSITY)
        lot_factor = max(f for _, f in phrases.INTENSITY)
        for phrase in phrases.PHRASES:
            for mv in phrase.moves:
                if mv.kind != "adjust" or not mv.notches:
                    continue
                with self.subTest(phrase=phrase.key, module=mv.module, param=mv.param):
                    spec = phrases.row_for(mv)
                    step = spec.step
                    n = abs(mv.notches)
                    touch_mag = n * touch_factor * step
                    bare_mag = n * 1.0 * step
                    lot_mag = n * lot_factor * step
                    self.assertLess(touch_mag, bare_mag)
                    self.assertLess(bare_mag, lot_mag)


if __name__ == "__main__":
    unittest.main()
