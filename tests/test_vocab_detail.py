"""Row 437 family 3: denoise strength and sharpen amount."""

import unittest

from scouter.core import dtbridge, phrases, vocab


class DetailRowsTest(unittest.TestCase):
    def test_rows(self) -> None:
        d = vocab.spec("denoiseprofile", "strength")
        s = vocab.spec("sharpen", "amount")
        self.assertEqual((d.lo, d.hi, d.step, d.neutral), (0.0, 4.0, 0.1, 1.0))
        self.assertEqual((s.lo, s.hi, s.step, s.neutral), (0.0, 2.0, 0.1, 0.5))
        self.assertIn("iop/denoiseprofile/strength", d.candidate_paths)
        self.assertIn("iop/sharpen/amount", s.candidate_paths)

    def test_phrases(self) -> None:
        # (phrase, key, module, param, notches): the sign is the direction on
        # the row -- denoiseprofile.strength up is more denoising, sharpen.amount
        # down is less sharpening.
        cases = (
            ("reduce noise", "denoise_more", "denoiseprofile", "strength", 1),
            ("denoise", "denoise_more", "denoiseprofile", "strength", 1),
            ("less denoising", "denoise_less", "denoiseprofile", "strength", -1),
            ("sharpen", "sharpen_more", "sharpen", "amount", 1),
            ("sharpen a touch", "sharpen_more", "sharpen", "amount", 0.5),
            ("less sharpening", "sharpen_less", "sharpen", "amount", -1),
        )
        for text, key, module, param, notches in cases:
            with self.subTest(phrase=text):
                r = phrases.resolve(text)
                self.assertEqual(r.key, key)
                self.assertEqual(len(r.moves), 1)
                mv = r.moves[0]
                self.assertEqual((mv.module, mv.param, mv.notches), (module, param, notches))
        self.assertEqual(phrases.resolve("softer contrast").key, "less_contrast")  # unchanged

    def test_status_follows_the_pins(self) -> None:
        # Neither row has a host-pinned action path yet (Task 34 Step 6 is the
        # host probe), so both are exactly `candidate` -- never `live`.
        for s in (vocab.spec("denoiseprofile", "strength"), vocab.spec("sharpen", "amount")):
            with self.subTest(row=f"{s.module}.{s.param}"):
                self.assertNotIn((s.module, s.param), dtbridge.ACTION_PATHS)
                self.assertEqual(vocab.status(s), "candidate")


if __name__ == "__main__":
    unittest.main()
