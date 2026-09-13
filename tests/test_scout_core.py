import os
import time
import unittest
from pathlib import Path
from unittest import mock

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
        dtbridge.set_bridge(
            FakeBridge(
                {
                    "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                    "darkroom.open": {
                        "image_id": 12,
                        "path": str(self.raw),
                        "view": "darkroom",
                        "opened": False,
                        "already_open": True,
                    },
                }
            )
        )
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

    def test_compare_carries_px_used_with_no_warning_when_it_matches(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        result = scout.compare(a=str(self.raw), b=str(self.raw), px=256)
        self.assertEqual(result.a["px_used"], 256)
        self.assertEqual(result.b["px_used"], 256)
        self.assertEqual(len(result.warnings), 1)  # no px-mismatch warning

    def test_compare_warns_when_px_used_differs_between_a_and_b(self) -> None:
        # Stub `stats.__wrapped__` directly rather than forcing a real budget
        # fallback through the fake CLI: the only thing `compare` cares about
        # is that the two `StatsResult.px_used` values differ, and doing it
        # this way keeps the test independent of the fallback's own timing.
        from scouter.types import StatsResult

        flat_stats = {
            "luminance_mean": 0.0,
            "luminance_p50": 0,
            "clipped_high_pct": 0.0,
            "clipped_low_pct": 0.0,
            "wb_ratio_rg": 1.0,
            "wb_ratio_bg": 1.0,
            "sharpness": 0.0,
        }
        calls = {"n": 0}

        def fake_stats(image, *, px=1024, budget_s=3.0, job=None):
            calls["n"] += 1
            used = px if calls["n"] == 1 else scout.FALLBACK_PX
            return StatsResult(
                image_id=12, path=str(image), px_requested=px, px_used=used, stats=dict(flat_stats)
            )

        with mock.patch.object(scout.stats, "__wrapped__", fake_stats):
            result = scout.compare(a=str(self.raw), b=str(self.raw), px=1024)
        self.assertNotEqual(result.a["px_used"], result.b["px_used"])
        self.assertTrue(any("resolution-dependent" in w for w in result.warnings))

    def test_stats_rejects_px_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            scout.stats(image=str(self.raw), px=32)
        with self.assertRaises(ValueError):
            scout.stats(image=str(self.raw), px=8192)

    def test_px_reaches_darktable_cli(self) -> None:
        log = self.root / "args.log"
        body = f'echo "$@" >> "{log}"\n' + FAKE_CLI_WRITES_PPM
        self._use_cli(body)
        scout.stats(image=str(self.raw), px=128)
        args_line = log.read_text()
        self.assertIn("--width 128", args_line)
        self.assertIn("--height 128", args_line)

    def test_two_renders_use_different_configdirs(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        seen: list[Path] = []
        original = scout._render_configdir

        def spy() -> Path:
            configdir = original()
            seen.append(configdir)
            return configdir

        with mock.patch.object(scout, "_render_configdir", side_effect=spy):
            scout.stats(image=str(self.raw), px=64)
            scout.stats(image=str(self.raw), px=64)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    def test_render_configdir_is_removed_after_the_call(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        seen: list[Path] = []
        original = scout._render_configdir

        def spy() -> Path:
            configdir = original()
            seen.append(configdir)
            return configdir

        with mock.patch.object(scout, "_render_configdir", side_effect=spy):
            scout.stats(image=str(self.raw), px=64)
        self.assertFalse(seen[0].exists())

    def test_missing_cli_binary_is_the_typed_error(self) -> None:
        os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        with (
            mock.patch.object(scout, "_MAC_CLI_PATH", self.root / "no-such-binary"),
            mock.patch("shutil.which", return_value=None),
        ):
            with self.assertRaises(scout.CliMissing) as ctx:
                scout.cli_binary()
        self.assertEqual(ctx.exception.code, "cli_missing")
        self.assertIn("SCOUTER_DARKTABLE_CLI", ctx.exception.fix)

    def test_preview_cache_name_is_stable_across_sidecar_edits(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        sidecar = Path(str(self.raw) + ".xmp")
        sidecar.write_text("<a/>")

        first = scout.preview(image=str(self.raw), px=64, fmt="ppm")
        self.assertFalse(first.cached)

        time.sleep(0.01)
        sidecar.write_text("<b/>")  # a real edit+commit advances mtime

        second = scout.preview(image=str(self.raw), px=64, fmt="ppm")
        self.assertEqual(first.preview_path, second.preview_path)
        self.assertFalse(second.cached)  # re-rendered: the sidecar moved on

        third = scout.preview(image=str(self.raw), px=64, fmt="ppm")
        self.assertEqual(second.preview_path, third.preview_path)
        self.assertTrue(third.cached)  # nothing changed since `second`

    def test_preview_prunes_files_older_than_7_days(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        from scouter import paths

        stale = paths.preview_dir()
        stale.mkdir(parents=True, exist_ok=True)
        stale_file = stale / "ancient-preview.png"
        stale_file.write_bytes(b"old")
        old = time.time() - scout.PREVIEW_MAX_AGE_S - 3600
        os.utime(stale_file, (old, old))

        scout.preview(image=str(self.raw), px=64, fmt="ppm")
        self.assertFalse(stale_file.exists())

    def test_preview_prunes_over_the_size_budget(self) -> None:
        self._use_cli(FAKE_CLI_WRITES_PPM)
        from scouter import paths

        directory = paths.preview_dir()
        directory.mkdir(parents=True, exist_ok=True)
        older = directory / "big-old.png"
        newer = directory / "big-new.png"
        older.write_bytes(b"x" * 40)
        newer.write_bytes(b"x" * 40)
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(newer, (now - 10, now - 10))

        with mock.patch.object(scout, "PREVIEW_MAX_BYTES", 50):
            scout.preview(image=str(self.raw), px=64, fmt="ppm")
        # over budget: the older file is pruned first, the newer one survives
        self.assertFalse(older.exists())
        self.assertTrue(newer.exists())
