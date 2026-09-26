"""W4 (row 437): 'match daylight' read back 5934 K for a 5500 K set on
olympus (bench run 3, 20260926t100133z). Numbers below are that run's table
(bench-config/wb/em10markiii.json, 5000-6500 K) and its after-set coeffs."""

import unittest

from scouter.core import wb

TABLE = [
    {"kelvin": 5000, "coeffs": [1.9646459817886353, 1.0, 1.8529057502746582]},
    {"kelvin": 5500, "coeffs": [2.068570375442505, 1.0, 1.729513168334961]},
    {"kelvin": 6000, "coeffs": [2.1569316387176514, 1.0, 1.6341935396194458]},
    {"kelvin": 6500, "coeffs": [2.23234486579895, 1.0, 1.5590273141860962]},
]
W4_AFTER = [2.4488253593444824, 1.0, 1.8785064220428467]


class W4OffCurveTest(unittest.TestCase):
    def test_w4_coefficients_are_not_read_as_5934_k(self) -> None:
        k, info = wb._invert(TABLE, W4_AFTER[0] / W4_AFTER[2], W4_AFTER, strict=True)
        self.assertIsNone(k)
        self.assertIn("tint", info["reason"])
        self.assertIn("calibrate", info["reason"])
        self.assertGreater(info["off_curve"], 0.03)

    def test_internal_callers_get_an_approximate_answer(self) -> None:
        k, info = wb._invert(TABLE, W4_AFTER[0] / W4_AFTER[2], W4_AFTER)
        self.assertAlmostEqual(k, 5934, delta=5)
        self.assertTrue(info["approximate"])

    def test_the_verb_is_strict(self) -> None:
        import inspect

        self.assertIn("strict=True", inspect.getsource(wb.kelvin_verb))

    def test_on_curve_coefficients_still_invert(self) -> None:
        c = TABLE[1]["coeffs"]
        k, info = wb._invert(TABLE, c[0] / c[2], c, strict=True)
        self.assertEqual(k, 5500)
        mid = [(a + b) / 2 for a, b in zip(TABLE[1]["coeffs"], TABLE[2]["coeffs"])]
        k, info = wb._invert(TABLE, mid[0] / mid[2], mid, strict=True)
        self.assertAlmostEqual(k, 5750, delta=40)
        self.assertLess(info["off_curve"], 0.03)

    def test_scaled_green_is_the_same_point(self) -> None:
        c = [x * 1.7 for x in TABLE[2]["coeffs"]]
        k, _ = wb._invert(TABLE, c[0] / c[2], c, strict=True)
        self.assertEqual(k, 6000)

    def test_ratio_only_callers_keep_the_old_behaviour(self) -> None:
        c = TABLE[1]["coeffs"]
        k, info = wb._invert(TABLE, c[0] / c[2])
        self.assertEqual(k, 5500)
        self.assertIsNone(info["off_curve"])


if __name__ == "__main__":
    unittest.main()
