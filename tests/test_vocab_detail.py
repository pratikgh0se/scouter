"""Row 437 family 3: denoise strength and sharpen amount."""

import unittest

from scouter.core import phrases, vocab


class DetailRowsTest(unittest.TestCase):
    def test_rows(self) -> None:
        d = vocab.spec("denoiseprofile", "strength")
        s = vocab.spec("sharpen", "amount")
        self.assertEqual((d.lo, d.hi, d.step, d.neutral), (0.0, 4.0, 0.1, 1.0))
        self.assertEqual((s.lo, s.hi, s.step, s.neutral), (0.0, 2.0, 0.1, 0.5))
        self.assertIn("iop/denoiseprofile/strength", d.candidate_paths)
        self.assertIn("iop/sharpen/amount", s.candidate_paths)

    def test_phrases(self) -> None:
        self.assertEqual(phrases.resolve("reduce noise").key, "denoise_more")
        self.assertEqual(phrases.resolve("less denoising").moves[0].notches, -1)
        self.assertEqual(phrases.resolve("sharpen a touch").moves[0].notches, 0.5)
        self.assertEqual(phrases.resolve("less sharpening").key, "sharpen_less")
        self.assertEqual(phrases.resolve("softer contrast").key, "less_contrast")  # unchanged


if __name__ == "__main__":
    unittest.main()
