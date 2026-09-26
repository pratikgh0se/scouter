"""Row 437 family 2: lens toggle, rotation and crop presets."""

import unittest

from scouter.core import moves, phrases, vocab


class GeometryTest(unittest.TestCase):
    def test_lens_toggle_is_live_through_the_module_whitelist(self) -> None:
        s = vocab.spec("lens", "__enabled")
        self.assertEqual(vocab.status(s), "live")
        res = phrases.resolve("correct the lens")
        self.assertEqual((res.moves[0].kind, res.moves[0].value), ("toggle", 1.0))
        self.assertEqual(phrases.resolve("turn off lens correction").moves[0].value, 0.0)

    def test_rotation_row_and_phrases(self) -> None:
        s = vocab.spec("ashift", "rotation")
        self.assertEqual((s.lo, s.hi, s.step, s.unit), (-10.0, 10.0, 0.5, "°"))
        self.assertEqual(phrases.resolve("rotate right").moves[0].notches, 1)
        self.assertEqual(phrases.resolve("rotate left a touch").moves[0].notches, -0.5)
        self.assertTrue(phrases.resolve("rotate right by 2°").explicit)

    def test_crop_margins_center_the_target_aspect(self) -> None:
        m = moves.crop_margins(1.0, 6000, 4000)   # 3:2 landscape -> square
        self.assertAlmostEqual(m["left"], 1 / 6, places=6)
        self.assertAlmostEqual(m["right"], 5 / 6, places=6)
        self.assertEqual((m["top"], m["bottom"]), (0.0, 1.0))
        m = moves.crop_margins(16 / 9, 4000, 3000)  # 4:3 -> 16:9 trims top and bottom
        self.assertEqual((m["left"], m["right"]), (0.0, 1.0))
        self.assertAlmostEqual(m["bottom"] - m["top"], (4000 / (16 / 9)) / 3000, places=6)
        with self.assertRaises(ValueError):
            moves.crop_margins(1.0, 0, 4000)

    def test_crop_phrases_carry_the_aspect(self) -> None:
        res = phrases.resolve("crop square")
        self.assertEqual((res.moves[0].kind, res.moves[0].value), ("crop", 1.0))
        self.assertAlmostEqual(phrases.resolve("crop to 4:5").moves[0].value, 0.8)


if __name__ == "__main__":
    unittest.main()
