"""Row 437 family 1: grain + vignette rows exist, start as candidates, and
their phrases resolve (and refuse honestly until pinned)."""

import unittest

from scouter.core import phrases, vocab


class FinishingRowsTest(unittest.TestCase):
    def test_rows_exist_with_candidate_paths(self) -> None:
        g = vocab.spec("grain", "strength")
        v = vocab.spec("vignette", "brightness")
        self.assertIsNotNone(g)
        self.assertIsNotNone(v)
        self.assertIn("iop/grain/strength", g.candidate_paths)
        self.assertIn("iop/vignette/brightness", v.candidate_paths)
        self.assertEqual((g.lo, g.hi, g.step), (0.0, 60.0, 5.0))
        self.assertEqual((v.lo, v.hi, v.step), (-0.8, 0.4, 0.05))

    def test_phrases_resolve_to_the_rows(self) -> None:
        self.assertEqual(phrases.resolve("add grain").moves[0].param, "strength")
        self.assertEqual(phrases.resolve("less grain").moves[0].notches, -1)
        self.assertEqual(phrases.resolve("darker corners").moves[0].notches, -1)
        self.assertEqual(phrases.resolve("add a vignette").key, "darker_corners")
        self.assertEqual(phrases.resolve("lighter corners a lot").moves[0].notches, 2)

    def test_status_follows_the_pins(self) -> None:
        for s in (vocab.spec("grain", "strength"), vocab.spec("vignette", "brightness")):
            self.assertIn(vocab.status(s), ("candidate", "absolute_only", "live"))
