import os
import unittest
from pathlib import Path

from scouter.core import dtbridge, scout
from scouter.errors import CliWroteNothing, DbLocked
from tests.scouter_fakes import (
    FAKE_CLI_DB_LOCKED,
    FAKE_CLI_WRITES_NOTHING,
    FAKE_CLI_WRITES_PPM,
    FakeBridge,
    TempCache,
    fake_cli_script,
)


class ScoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        self.raw = self.root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        dtbridge.set_bridge(FakeBridge({
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.open": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                              "opened": False, "already_open": True},
        }))
        self._saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved_cli is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved_cli
        self.cache.stop()

    def _use_cli(self, body: str) -> Path:
        script = fake_cli_script(self.root, body)
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(script)
        return script

    def test_stats_on_a_two_pixel_render(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        result = scout.stats(image=str(self.raw), px=1024)
        self.assertEqual(result.px_used, 1024)
        self.assertEqual(result.stats["width"], 2)
        self.assertEqual(len(result.histogram["r"]), 256)
        self.assertEqual(result.stats["channels"]["r"]["clipped_high_pct"], 50.0)
        self.assertTrue(any("8-bit" in w for w in result.warnings))

    def test_stats_accepts_the_image_positionally(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        result = scout.stats(str(self.raw), px=64)
        self.assertEqual(result.image_id, 12)

    def test_cli_writing_nothing_is_the_typed_error(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_NOTHING)
        with self.assertRaises(CliWroteNothing) as ctx:
            scout.stats(image=str(self.raw))
        self.assertIn("write sidecar file", ctx.exception.fix)

    def test_locked_database_is_the_typed_error_with_the_configdir(self) -> None:
        self._use_cli(FAKE_CLI_DB_LOCKED)
        with self.assertRaises(DbLocked) as ctx:
            scout.stats(image=str(self.raw))
        self.assertIn("cli-config", ctx.exception.detail["configdir"])

    def test_over_budget_falls_back_to_512(self) -> None:
        self._use_cli("sleep 0.4\n" + FAKE_CLI_WRITES_PPM)
        result = scout.stats(image=str(self.raw), px=1024, budget_s=0.2)
        self.assertEqual(result.px_used, 512)
        self.assertTrue(any("512px" in w for w in result.warnings))

    def test_preview_returns_a_path_and_never_pixels(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        result = scout.preview(image=str(self.raw), px=512, fmt="ppm")
        self.assertTrue(Path(result.preview_path).is_file())
        self.assertEqual((result.width, result.height), (2, 1))
        self.assertNotIn("data", result.to_dict())
        cached = scout.preview(image=str(self.raw), px=512, fmt="ppm")
        self.assertTrue(cached.cached)

    def test_preview_rejects_an_unknown_format(self) -> None:
        with self.assertRaises(ValueError):
            scout.preview(image=str(self.raw), fmt="jpeg")

    def test_compare_reports_b_minus_a(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        result = scout.compare(a=str(self.raw), b=str(self.raw), px=256)
        self.assertEqual(result.delta["luminance_mean"], 0.0)
        self.assertIn("b minus a", result.warnings[0])

    def test_compare_accepts_positional_images(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        result = scout.compare(str(self.raw), str(self.raw), px=64)
        self.assertEqual(result.delta["luminance_mean"], 0.0)
