"""Row 437 family 1: grain + vignette rows exist, start as candidates, and
their phrases resolve (and refuse honestly until pinned)."""

import unittest

from scouter.core import dtbridge, phrases, vocab


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
        # (phrase, key, module, param, notches): the sign is the direction on
        # the row -- grain.strength up is more grain, vignette.brightness
        # down is darker corners.
        cases = (
            ("add grain", "more_grain", "grain", "strength", 1),
            ("more grain", "more_grain", "grain", "strength", 1),
            ("less grain", "less_grain", "grain", "strength", -1),
            ("darker corners", "darker_corners", "vignette", "brightness", -1),
            ("add a vignette", "darker_corners", "vignette", "brightness", -1),
            ("lighter corners", "lighter_corners", "vignette", "brightness", 1),
            ("lighter corners a lot", "lighter_corners", "vignette", "brightness", 2),
        )
        for text, key, module, param, notches in cases:
            with self.subTest(phrase=text):
                r = phrases.resolve(text)
                self.assertEqual(r.key, key)
                self.assertEqual(len(r.moves), 1)
                mv = r.moves[0]
                self.assertEqual((mv.module, mv.param, mv.notches), (module, param, notches))

    def test_status_follows_the_pins(self) -> None:
        # Neither row has a host-pinned action path or XMP slot yet (Task 32
        # adds them as candidates pending their own host probe), so both are
        # exactly `candidate` -- never `live` without a pin.
        for s in (vocab.spec("grain", "strength"), vocab.spec("vignette", "brightness")):
            with self.subTest(row=f"{s.module}.{s.param}"):
                self.assertNotIn((s.module, s.param), dtbridge.ACTION_PATHS)
                self.assertEqual(vocab.status(s), "candidate")
