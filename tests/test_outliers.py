"""`outliers.list`: the four measures against the hero, and their tolerances
(task 9). Boundary/Kelvin-fallback tests exercise `style._measure_frame`
directly; the set-assembly tests (exclusion, sorting, failed frames, the
empty-set refusal) exercise `style.outliers` end to end against a fake
darktable-cli whose output pixel value is chosen by the raw path's own name.

`tests/fixtures/wb-em10iii.json` here is the REAL table (task 11's own host
calibration of `~/Pictures/ScouterScratch/em10iii.ORF`, a scratch copy of the
EditingTrial ORF) -- ten measured points, 3000-12000 K, replacing task 9's
five made-up ones.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter.config import Config
from scouter.core import dtbridge, scout, style
from scouter.errors import UsageError
from tests.scouter_fakes import FakeBridge, TempCache, fake_cli_script

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _lookup_response(params: dict) -> dict:
    """A per-path `image.lookup` stand-in: echoes the path back as its own image."""
    path = params["path"]
    return {"found": True, "image_id": abs(hash(path)) % 100000, "path": path}


#: A fake darktable-cli whose output pixel is chosen by a `__vNNN__` marker in
#: the RAW input path ($1), gray (r=g=b) so wb_ratio_rg/bg stay 1.0 and only
#: luminance (hence ev_proxy) varies -- exactly what the sorting/exclusion
#: tests need in isolation, with no darktable and no real pixels.
_GRAY_FAKE_CLI = """
in="$1"
out=""
for a in "$@"; do case "$a" in *.ppm|*.png) out="$a";; esac; done
val=128
case "$in" in
  *__v060__*) val=60 ;;
  *__v128__*) val=128 ;;
  *__v140__*) val=140 ;;
  *__v200__*) val=200 ;;
  *__v230__*) val=230 ;;
esac
printf "P6\\n2 1\\n255\\n" > "$out"
python3 -c "import sys; sys.stdout.buffer.write(bytes([$val] * 6))" >> "$out"
"""


def _frame(seq=1, path=None, lum=100.0, rg=0.0, bg=0.0, clip=0.0, coeffs=None, camera=None):
    return {
        "seq": seq,
        "path": path or f"/tmp/scouter-outliers-test/frame-{seq}.orf",
        "image_id": seq,
        "stats": {
            "luminance_mean": lum,
            "wb_ratio_rg": rg,
            "wb_ratio_bg": bg,
            "clipped_high_pct": clip,
        },
        "coeffs": coeffs,
        "camera": camera,
    }


class MeasureFrameBoundaryTest(unittest.TestCase):
    """`_measure_frame`'s four measures, each right at its tolerance."""

    def setUp(self) -> None:
        self.tol = Config().tolerances

    def test_each_tolerance_boundary(self) -> None:
        tol = self.tol

        with self.subTest("ev_proxy"):
            ref = _frame(lum=100.0)
            # log2((lum+0.5)/(ref+0.5)) round-trips with ~1e-16 float noise,
            # so a tiny epsilon keeps the "in tolerance" case from tipping
            # over the boundary due to that noise alone.
            in_lum = (100.5) * (2**tol.exposure_ev) - 0.5 - 1e-9
            out_lum = (100.5) * (2 ** (tol.exposure_ev + 0.01)) - 0.5
            row_in = style._measure_frame(_frame(lum=in_lum), ref, tol)
            row_out = style._measure_frame(_frame(lum=out_lum), ref, tol)
            self.assertFalse(row_in["is_outlier"], row_in["reasons"])
            self.assertTrue(row_out["is_outlier"])
            self.assertTrue(any("ev_proxy" in r for r in row_out["reasons"]), row_out["reasons"])

        with self.subTest("temperature_k"):
            ref = _frame(lum=100.0)
            frame_in = _frame(lum=100.0)
            frame_out = _frame(lum=100.0)
            with mock.patch.object(
                style, "_resolve_kelvin", side_effect=[(5000.0, ""), (4700.0, "")]
            ):
                row_in = style._measure_frame(frame_in, ref, tol)
            with mock.patch.object(
                style, "_resolve_kelvin", side_effect=[(5000.0, ""), (4699.0, "")]
            ):
                row_out = style._measure_frame(frame_out, ref, tol)
            self.assertEqual(row_in["wb_test"], "kelvin")
            self.assertFalse(row_in["is_outlier"], row_in["reasons"])
            self.assertTrue(row_out["is_outlier"])
            self.assertTrue(
                any("temperature_k" in r for r in row_out["reasons"]), row_out["reasons"]
            )

        with self.subTest("wb_ratio"):
            ref = _frame(lum=100.0, rg=0.0, bg=0.0)
            row_in = style._measure_frame(_frame(lum=100.0, rg=tol.wb_ratio, bg=0.0), ref, tol)
            row_out = style._measure_frame(
                _frame(lum=100.0, rg=tol.wb_ratio + 0.001, bg=0.0), ref, tol
            )
            self.assertEqual(row_in["wb_test"], "ratio")
            self.assertFalse(row_in["is_outlier"], row_in["reasons"])
            self.assertTrue(row_out["is_outlier"])
            self.assertTrue(any("wb_ratio_rg" in r for r in row_out["reasons"]), row_out["reasons"])

        with self.subTest("clipped_high_pct"):
            ref = _frame(lum=100.0, clip=0.0)
            row_in = style._measure_frame(_frame(lum=100.0, clip=tol.clipped_high_pct), ref, tol)
            row_out = style._measure_frame(
                _frame(lum=100.0, clip=tol.clipped_high_pct + 0.01), ref, tol
            )
            self.assertFalse(row_in["is_outlier"], row_in["reasons"])
            self.assertTrue(row_out["is_outlier"])
            self.assertTrue(
                any("clipped_high_pct" in r for r in row_out["reasons"]), row_out["reasons"]
            )


class KelvinFallbackTest(unittest.TestCase):
    """`wb.kelvin` (task 11, imported lazily) degrades to the ratio test when
    there is no table -- its own designed fallback -- and is used for real
    once a committed table exists."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="scouter-outliers-wb-")
        self.tmp = Path(self._tmp.name)
        self._saved_config = os.environ.get("SCOUTER_CONFIG")
        os.environ["SCOUTER_CONFIG"] = str(self.tmp / "config.json")
        self.tol = Config().tolerances

    def tearDown(self) -> None:
        if self._saved_config is None:
            os.environ.pop("SCOUTER_CONFIG", None)
        else:
            os.environ["SCOUTER_CONFIG"] = self._saved_config
        self._tmp.cleanup()

    def test_kelvin_null_falls_back_to_ratio(self) -> None:
        ref = _frame(lum=100.0, coeffs=[1.60, 1.00, 1.80], camera="em10iii")
        frame = _frame(lum=100.0, coeffs=[1.80, 1.00, 1.60], camera="em10iii")
        row = style._measure_frame(frame, ref, self.tol)
        self.assertEqual(row["wb_test"], "ratio")
        self.assertIsNone(row["temperature_k_delta"])
        self.assertIn("wb calibrate", row["temperature_k_reason"])

    def test_kelvin_used_when_the_table_exists(self) -> None:
        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        (wb_dir / "em10iii.json").write_text(
            (FIXTURES / "wb-em10iii.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        # Task 11: `wb-em10iii.json` is now the REAL measured table (a host
        # calibration of the EditingTrial ORF), not five made-up points --
        # two of its own sample rows stand in for the old hard-coded
        # 5000/6000 K coefficients, so this stays an "exact" match (delta ==
        # the Kelvin gap between the two rows) however the real numbers move
        # on a future re-calibration.
        table = json.loads((FIXTURES / "wb-em10iii.json").read_text(encoding="utf-8"))
        lo, hi = sorted(table, key=lambda p: p["kelvin"])[:2]
        ref = _frame(lum=100.0, coeffs=lo["coeffs"], camera="em10iii")
        frame = _frame(lum=100.0, coeffs=hi["coeffs"], camera="em10iii")
        row = style._measure_frame(frame, ref, self.tol)
        self.assertEqual(row["wb_test"], "kelvin")
        self.assertIsNotNone(row["temperature_k_delta"])
        self.assertAlmostEqual(row["temperature_k_delta"], hi["kelvin"] - lo["kelvin"], places=1)
        self.assertNotIn("temperature_k_reason", row)


class OutliersListTest(unittest.TestCase):
    """`style.outliers` end to end: set assembly, exclusion, sorting, and the
    failed/empty edge cases -- through a fake darktable-cli, never real
    darktable."""

    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        dtbridge.set_bridge(FakeBridge({"image.lookup": _lookup_response}))
        self._saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")
        script = fake_cli_script(self.root, _GRAY_FAKE_CLI)
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(script)
        self._config_patch = mock.patch.object(style.config, "load", return_value=Config())
        self._config_patch.start()

    def tearDown(self) -> None:
        self._config_patch.stop()
        dtbridge.set_bridge(None)
        if self._saved_cli is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved_cli
        self.cache.stop()

    def _raw(self, name: str) -> Path:
        p = self.root / name
        p.write_bytes(b"raw")
        return p

    def test_reference_excluded_from_its_own_comparison(self) -> None:
        ref_raw = self._raw("hero__v128__.orf")
        a = self._raw("a__v128__.orf")
        b = self._raw("b__v128__.orf")
        result = style.outliers(str(ref_raw), images=[str(ref_raw), str(a), str(b)])
        self.assertEqual(result.checked, 2, "the reference itself must not be in its own set")
        self.assertEqual(result.within_tolerance, 2)
        self.assertEqual(result.outliers, [])

    def test_empty_set_refused(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            style.outliers("whatever-reference")
        self.assertEqual(ctx.exception.code, "usage")

    def test_failed_frames_are_not_within_tolerance(self) -> None:
        ref_raw = self._raw("hero__v128__.orf")
        good = self._raw("good__v128__.orf")
        bad = self._raw("bad__v128__.orf")
        real_render = scout.render

        def flaky_render(raw_path, out_path, px, ext):
            if raw_path == str(bad):
                raise RuntimeError("simulated render failure")
            return real_render(raw_path, out_path, px, ext)

        with mock.patch.object(scout, "render", side_effect=flaky_render):
            result = style.outliers(str(ref_raw), images=[str(good), str(bad)])

        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0]["path"], str(bad))
        self.assertEqual(result.checked, 1, "the failed frame is not counted as checked")
        self.assertEqual(
            result.checked,
            result.within_tolerance + len(result.outliers),
            "checked == within + outliers, with failed in neither bucket",
        )

    def test_sorted_worst_first(self) -> None:
        ref_raw = self._raw("hero__v128__.orf")
        dark = self._raw("dark__v060__.orf")  # severity ~3.6, the worst
        bright1 = self._raw("bright1__v230__.orf")  # severity ~2.8
        bright2 = self._raw("bright2__v200__.orf")  # severity ~2.1
        fine = self._raw("fine__v140__.orf")  # within tolerance

        result = style.outliers(
            str(ref_raw), images=[str(fine), str(bright2), str(bright1), str(dark)]
        )

        self.assertEqual(result.within_tolerance, 1)
        self.assertEqual(len(result.outliers), 3)
        severities = [row["severity"] for row in result.outliers]
        self.assertEqual(severities, sorted(severities, reverse=True), severities)
        self.assertEqual(result.outliers[0]["path"], str(dark), "the worst breach sorts first")

    def test_ev_is_labelled_proxy(self) -> None:
        ref_raw = self._raw("hero__v128__.orf")
        dark = self._raw("dark__v060__.orf")
        result = style.outliers(str(ref_raw), images=[str(dark)])
        self.assertEqual(len(result.outliers), 1)
        row = result.outliers[0]
        self.assertIn("ev_proxy_delta", row)
        self.assertNotIn("ev", row)
        self.assertTrue(any("metered exposure" in w for w in result.warnings), result.warnings)


if __name__ == "__main__":
    unittest.main()
