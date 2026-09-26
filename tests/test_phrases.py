"""Phase 3 task 3: phrase -> moves, deterministic, pinned by a golden."""

from __future__ import annotations

import difflib
import json
import unittest
from pathlib import Path

from scouter.core import phrases, vocab
from scouter.errors import UsageError

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "phrases_golden.json"
REGEN = (
    "uv run --extra dev python -c \"import json; from tests.test_phrases import INPUTS; "
    "from scouter.core import phrases; "
    "print(json.dumps({t: phrases.resolution_dict(phrases.resolve(t)) for t in INPUTS}, "
    "indent=2, sort_keys=True))\" > tests/fixtures/phrases_golden.json"
)

#: 40 fixed inputs: canonical forms, intensity words, explicit amounts, noise.
INPUTS = [
    "brighter", "darker", "more contrast", "less contrast", "warmer", "cooler",
    "more magenta", "more green", "lift the shadows", "deeper shadows",
    "brighter midtones", "darker midtones", "brighter highlights", "tame the highlights",
    "deeper blacks", "faded blacks", "more saturation", "less saturation",
    "more vibrance", "less vibrance", "more chroma", "less chroma",
    "warm the highlights", "cool the shadows", "teal and orange",
    "stronger colour grade", "weaker color grade",
    "a touch warmer", "slightly less contrast", "much cooler", "way brighter",
    "a little bit darker", "a lot more saturation", "Make it warmer, please!",
    "cooler by 300k", "brighter by 0.5 EV", "more saturation by 10%",
    "Lift The Shadows", "punchier", "desaturate",
]


class PhraseGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_every_input_resolves_as_pinned(self) -> None:
        actual = json.dumps(
            {text: phrases.resolution_dict(phrases.resolve(text)) for text in INPUTS},
            indent=2, sort_keys=True,
        ) + "\n"
        if not GOLDEN.exists():
            GOLDEN.write_text(actual)
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text()
        if expected != actual:
            diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                  "golden", "actual", lineterm=""))
            self.fail(f"phrase resolution changed; regenerate deliberately with:\n{REGEN}\n{diff}")


class PhraseRulesTest(unittest.TestCase):
    def test_intensity_scales_notches(self) -> None:
        self.assertEqual(phrases.resolve("warmer").moves[0].notches, 1.0)
        self.assertEqual(phrases.resolve("a touch warmer").moves[0].notches, 0.5)
        self.assertEqual(phrases.resolve("much warmer").moves[0].notches, 2.0)
        self.assertEqual(phrases.resolve("cooler").moves[0].notches, -1.0)

    def test_amount_replaces_intensity_and_is_bounded(self) -> None:
        self.assertEqual(phrases.resolve("warmer", amount=3).moves[0].notches, 3.0)
        for bad in (0, -1, 6, float("nan")):
            with self.subTest(amount=bad), self.assertRaises(UsageError):
                phrases.resolve("warmer", amount=bad)

    def test_explicit_by_amount_converts_display_units(self) -> None:
        mv = phrases.resolve("more saturation by 10%").moves[0]
        self.assertIsNone(mv.notches)
        self.assertAlmostEqual(mv.value, 0.10)
        self.assertAlmostEqual(phrases.resolve("cooler by 300k").moves[0].value, -300.0)

    def test_zero_by_amount_is_refused(self) -> None:
        """Final review #5: 'by 0' is a zero-delta move; refuse it as usage."""
        for text in ("brighter by 0", "warmer by 0k", "brighter by 0.0 ev"):
            with self.subTest(text=text), self.assertRaises(UsageError) as cm:
                phrases.resolve(text)
            self.assertIn("non-zero", cm.exception.message)

    def test_negative_by_amount_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("warmer by -300k")

    def test_by_amount_with_the_wrong_unit_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("warmer by 0.5 ev")

    def test_by_amount_on_a_multi_move_phrase_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("teal and orange by 10%")

    def test_amount_and_by_together_is_refused(self) -> None:
        with self.assertRaises(UsageError):
            phrases.resolve("warmer by 300k", amount=2)

    def test_unknown_phrase_suggests_neighbours(self) -> None:
        with self.assertRaises(UsageError) as cm:
            phrases.resolve("warmmer")
        self.assertIn("warmer", cm.exception.detail["suggestions"])

    def test_contrast_phrases_route_through_tone(self) -> None:
        self.assertEqual(phrases.resolve("less contrast").moves[0].module, phrases.TONE)

    def test_every_phrase_targets_a_vocabulary_row(self) -> None:
        for phrase in phrases.PHRASES:
            for mv in phrase.moves:
                if mv.module == phrases.TONE:
                    module, param = "sigmoid", mv.param
                elif mv.kind == "crop":
                    # A crop move carries the target aspect, not one row; the
                    # left margin row stands in for the whole crop group.
                    module, param = "crop", "left"
                else:
                    module, param = mv.module, mv.param
                with self.subTest(key=phrase.key, move=f"{module}.{param}"):
                    self.assertIsNotNone(vocab.spec(module, param))

    def test_synonyms_are_unique_across_phrases(self) -> None:
        seen: dict[str, str] = {}
        for phrase in phrases.PHRASES:
            for syn in phrase.synonyms:
                self.assertNotIn(syn, seen, f"{syn!r} in {phrase.key} and {seen.get(syn)}")
                seen[syn] = phrase.key


class ResolveVerbTest(unittest.TestCase):
    def test_view_reports_delta_unit_and_status(self) -> None:
        r = phrases.resolve_verb.__wrapped__("warmer", job="job-test")
        mv = r.moves[0]
        self.assertEqual((mv["module"], mv["param"], mv["unit"]), ("temperature", "temperature", "K"))
        self.assertAlmostEqual(mv["delta"], 300.0)
        self.assertEqual(mv["status"], "live")
        tone = phrases.resolve_verb.__wrapped__("more contrast", job="job-test").moves[0]
        self.assertEqual(tone["routes"], ["sigmoid", "filmicrgb"])
