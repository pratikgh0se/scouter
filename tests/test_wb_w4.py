"""W4 (row 437): 'match daylight' read back 5934 K for a 5500 K set on
olympus (bench run 3, 20260926t100133z). Numbers below are that run's table
(bench-config/wb/em10markiii.json, 5000-6500 K) and its after-set coeffs."""

import json
import unittest
from pathlib import Path

from scouter.core import wb

#: The shipped em10iii table (tests/fixtures/wb-em10iii.json is the same
#: content, staged for the other tests that redirect wb_dir() at it) --
#: read directly here, bypassing `wb.load_table`/`paths.wb_dir()` so this
#: test never touches the real `~/.config/scouter` (implementer rule).
_SHIPPED_TABLE_PATH = (
    Path(__file__).resolve().parent.parent / "scouter" / "data" / "wb" / "em10iii.json"
)


def _shipped_em10iii_samples() -> list[dict]:
    data = json.loads(_SHIPPED_TABLE_PATH.read_text(encoding="utf-8"))
    samples = [
        {"kelvin": p["k"], "coeffs": [p["coeff_r"], p["coeff_g"], p["coeff_b"]]}
        for p in data["samples"]
    ]
    return sorted(samples, key=lambda p: p["kelvin"])


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

    def test_one_sample_table_does_not_crash(self) -> None:
        """Fix round 1, I1: `calibrate` with `k_from == k_to` writes a single
        point (wb.py:727); `load_table` accepts it (wb.py:238, 273). A
        single-point table has no curve to be off of -- `_off_curve` must
        return `None`, not index past the end of a 1-element list."""
        one = [{"kelvin": 5500, "coeffs": [2.0, 1.0, 1.7]}]
        for strict in (False, True):
            with self.subTest(strict=strict):
                k, info = wb._invert(one, 2.0 / 1.7, [2.0, 1.0, 1.7], strict=strict)
                self.assertEqual(k, 5500)
                self.assertTrue(info["exact"])
                self.assertIsNone(info["off_curve"])

    def test_shipped_table_quadratic_fit_is_not_falsely_refused(self) -> None:
        """Fix round 1, I2: a straight chord between bracketing samples on
        the real shipped em10iii.json (1000 K steps, 3000-12000 K) bends
        away from the table's own curve enough to read >2% off at the low
        end even for coefficients that sit exactly on a smooth fit through
        the table. `_off_curve` now fits a quadratic in mireds through the
        3 samples nearest the inverted Kelvin; points on that same fit
        must read close to 0% and must not be strict-refused."""
        samples = _shipped_em10iii_samples()

        def norm(p: dict) -> tuple[float, float]:
            c = p["coeffs"]
            return c[0] / c[1], c[2] / c[1]

        for k in (3250, 3500, 3750):
            with self.subTest(k=k):
                m = 1e6 / k
                nearest = sorted(samples, key=lambda p: abs(1e6 / p["kelvin"] - m))[:3]
                nearest = sorted(nearest, key=lambda p: p["kelvin"])
                ms = [1e6 / p["kelvin"] for p in nearest]
                rs = [norm(p)[0] for p in nearest]
                bs = [norm(p)[1] for p in nearest]
                r_t = wb._quad_interp(ms, rs, m)
                b_t = wb._quad_interp(ms, bs, m)
                coeffs = [r_t, 1.0, b_t]
                got_k, info = wb._invert(samples, r_t / b_t, coeffs, strict=True)
                self.assertIsNotNone(got_k)
                self.assertLess(info["off_curve"], 0.01)

    def test_halved_table_still_accepts_the_dropped_real_sample(self) -> None:
        """Fix round 1, I2 (measured case): halving the shipped table to a
        2000 K step (dropping the real 4000 K sample) used to make that
        real sample's own coefficients read 6.1% off under the old linear
        chord -- a false refusal on a correct edit. The quadratic-in-mireds
        fit must accept it."""
        samples = _shipped_em10iii_samples()
        halved = samples[0::2]
        real_4000 = next(s for s in samples if s["kelvin"] == 4000)
        c = real_4000["coeffs"]
        k, info = wb._invert(halved, c[0] / c[2], c, strict=True)
        self.assertIsNotNone(k)
        self.assertLess(info["off_curve"], 0.03)


if __name__ == "__main__":
    unittest.main()
