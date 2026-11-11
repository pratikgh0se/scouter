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
    'uv run --extra dev python -c "import json; from tests.test_phrases import INPUTS; '
    "from scouter.core import phrases; "
    "print(json.dumps({t: phrases.resolution_dict(phrases.resolve(t)) for t in INPUTS}, "
    'indent=2, sort_keys=True))" > tests/fixtures/phrases_golden.json'
)

#: 40 fixed inputs: canonical forms, intensity words, explicit amounts, noise.
INPUTS = [
    "brighter",
    "darker",
    "more contrast",
    "less contrast",
    "warmer",
    "cooler",
    "more magenta",
    "more green",
    "lift the shadows",
    "deeper shadows",
    "brighter midtones",
    "darker midtones",
    "brighter highlights",
    "tame the highlights",
    "deeper blacks",
    "faded blacks",
    "more saturation",
    "less saturation",
    "more vibrance",
    "less vibrance",
    "more chroma",
    "less chroma",
    "warm the highlights",
    "cool the shadows",
    "teal and orange",
    "stronger colour grade",
    "weaker color grade",
    "a touch warmer",
    "slightly less contrast",
    "much cooler",
    "way brighter",
    "a little bit darker",
    "a lot more saturation",
    "Make it warmer, please!",
    "cooler by 300k",
    "brighter by 0.5 EV",
    "more saturation by 10%",
    "Lift The Shadows",
    "punchier",
    "desaturate",
]


class PhraseGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_every_input_resolves_as_pinned(self) -> None:
        actual = (
            json.dumps(
                {text: phrases.resolution_dict(phrases.resolve(text)) for text in INPUTS},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if not GOLDEN.exists():
            GOLDEN.write_text(actual)
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text()
        if expected != actual:
            diff = "\n".join(
                difflib.unified_diff(
                    expected.splitlines(), actual.splitlines(), "golden", "actual", lineterm=""
                )
            )
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

    def test_colon_survives_normalise_only_between_digits(self) -> None:
        # Fix round 1, I2: a bare/trailing/mid-sentence colon must still
        # resolve the way main did before the crop presets' "4:5" needed a
        # colon kept at all.
        self.assertEqual(phrases.resolve("warmer:").key, "warmer")
        self.assertEqual(phrases.resolve("Make it warmer: please").key, "warmer")
        self.assertAlmostEqual(phrases.resolve("crop to 4:5").moves[0].value, 0.8)

    def test_outer_filler_words_do_not_break_a_synonym_that_contains_one(self) -> None:
        # Fix round 1, M1: "make it square" is itself a registered synonym
        # containing the filler word "make it" -- an outer "please"/"can
        # you" must still resolve to it, not just the bare synonym.
        for text in ("please make it square", "make it square please", "can you make it square"):
            with self.subTest(text=text):
                self.assertEqual(phrases.resolve(text).key, "crop_square")

    def test_by_n_on_a_toggle_or_crop_names_the_kind_not_the_move_count(self) -> None:
        # Fix round 1, M2: the message must say WHY ("a toggle/crop takes no
        # amount"), not the misleading one-move-phrase count.
        with self.assertRaises(UsageError) as cm:
            phrases.resolve("correct the lens by 1")
        self.assertIn("toggle", cm.exception.message)
        with self.assertRaises(UsageError) as cm:
            phrases.resolve("crop square by 1")
        self.assertIn("crop", cm.exception.message)

    def test_intensity_words_are_dropped_for_toggle_and_crop_phrases(self) -> None:
        # Fix round 1, M3: the move for a toggle/crop phrase never changes
        # with an intensity word, so the reported intensity must not pretend
        # it does.
        self.assertEqual(phrases.resolve("crop square a touch").intensity, 1.0)
        self.assertEqual(phrases.resolve("correct the lens a lot").intensity, 1.0)

    def test_unknown_phrase_suggests_neighbours(self) -> None:
        with self.assertRaises(UsageError) as cm:
            phrases.resolve("warmmer")
        self.assertIn("warmer", cm.exception.detail["suggestions"])

    def test_contrast_phrases_route_through_tone(self) -> None:
        self.assertEqual(phrases.resolve("less contrast").moves[0].module, phrases.TONE)

    def test_every_phrase_targets_a_vocabulary_row(self) -> None:
        # The oracle is the vocabulary registry itself (`vocab.PARAMS`), not
        # `phrases.row_for` (the code under test): every move's own row must
        # be registered. A `@tone` move needs the contrast row on BOTH tone
        # modules; a `crop` move (param `aspect`) needs all four crop sides.
        registry = {(s.module, s.param): s for s in vocab.PARAMS}
        crop_sides = [("crop", side) for side in ("left", "right", "top", "bottom")]
        for phrase in phrases.PHRASES:
            for mv in phrase.moves:
                with self.subTest(key=phrase.key, move=f"{mv.module}.{mv.param}"):
                    if mv.module == phrases.TONE:
                        needed = [("sigmoid", mv.param), ("filmicrgb", mv.param)]
                        own = ("sigmoid", mv.param)
                    elif mv.module == "crop":
                        needed = crop_sides
                        own = ("crop", "left")
                    else:
                        needed = [(mv.module, mv.param)]
                        own = (mv.module, mv.param)
                    for row in needed:
                        self.assertIn(row, registry)
                    # row_for (used by gauntlet's layers) must hand back that
                    # registered row, never a stand-in
                    self.assertIs(phrases.row_for(mv), registry[own])

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
        self.assertEqual(
            (mv["module"], mv["param"], mv["unit"]), ("temperature", "temperature", "K")
        )
        self.assertAlmostEqual(mv["delta"], 300.0)
        self.assertEqual(mv["status"], "live")
        tone = phrases.resolve_verb.__wrapped__("more contrast", job="job-test").moves[0]
        self.assertEqual(tone["routes"], ["sigmoid", "filmicrgb"])
