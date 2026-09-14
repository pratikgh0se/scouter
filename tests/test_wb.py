"""`scouter.core.wb` (task 11): Kelvin inversion against the real measured
table, `wb.calibrate`'s refusals/monotonicity check, `xmp.latest_params`'s
full coefficient vector, and the coefficient-slider undo.

`tests/fixtures/wb-em10iii.json` is the REAL table (task 11's own host
calibration of `~/Pictures/ScouterScratch/em10iii.ORF`, a scratch copy of the
EditingTrial ORF) -- not the five made-up points task 9 shipped as a
placeholder. `InvertRealTableTest` reads it back generically (its own first,
last, and midpoint samples) rather than hard-coding numbers, so it stays
correct however many points the real sweep produced.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal, wb, xmp
from scouter.errors import UsageError
from scouter.types import CommitResult
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_TABLE = FIXTURES / "wb-em10iii.json"
REAL_XMP = FIXTURES / "P6281348.ORF.xmp"


def _build_tiff_header(model: str = "E-M10 Mark III", make: str = "OLYMPUS CORPORATION") -> bytes:
    """A synthetic single-IFD TIFF header carrying Make/Model, enough for
    `exif.read_tags`/`camera_slug` to resolve a camera without a real raw
    (same construction `tests/test_exif.py::_build_header` uses)."""
    endian = "<"
    ifd0_offset = 8
    make_bytes = make.encode("ascii") + b"\x00"
    model_bytes = model.encode("ascii") + b"\x00"
    num_entries = 2
    ifd_size = 2 + num_entries * 12 + 4
    values_offset = ifd0_offset + ifd_size
    make_offset = values_offset
    model_offset = make_offset + len(make_bytes)

    header = b"II" + struct.pack(endian + "H", 42) + struct.pack(endian + "I", ifd0_offset)
    header += struct.pack(endian + "H", num_entries)
    header += struct.pack(endian + "HHI", 0x010F, 2, len(make_bytes)) + struct.pack(
        endian + "I", make_offset
    )
    header += struct.pack(endian + "HHI", 0x0110, 2, len(model_bytes)) + struct.pack(
        endian + "I", model_offset
    )
    header += struct.pack(endian + "I", 0)
    header += make_bytes + model_bytes
    return header


def _temperature_params_hex(coeffs: tuple[float, float, float, float], preset: int = 2) -> str:
    r, g, b, g2 = coeffs
    return struct.pack("<4fi", r, g, b, g2, preset).hex()


def _temperature_sidecar(
    coeffs: tuple[float, float, float, float], *, num: int = 0, history_end: int | None = None
) -> str:
    if history_end is None:
        history_end = num + 1
    params_hex = _temperature_params_hex(coeffs)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:darktable="http://darktable.sf.net/"
   darktable:history_end="{history_end}"
   darktable:iop_order_version="4"
   darktable:xmp_version="5">
   <darktable:history>
    <rdf:Seq>
     <rdf:li
      darktable:num="{num}"
      darktable:operation="temperature"
      darktable:enabled="1"
      darktable:modversion="4"
      darktable:params="{params_hex}"
      darktable:multi_name=""
      darktable:multi_name_hand_edited="0"
      darktable:multi_priority="0"
      darktable:blendop_version="14"
      darktable:blendop_params=""/>
    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


class _ConfigDirFixture(unittest.TestCase):
    """Redirects `paths.wb_dir()` (via `SCOUTER_CONFIG`) into a fresh temp
    dir, same discipline `tests/test_outliers.py::KelvinFallbackTest` uses."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-wb-test-")
        self.tmp = Path(self._tmpdir.name)
        self._saved_config = os.environ.get("SCOUTER_CONFIG")
        os.environ["SCOUTER_CONFIG"] = str(self.tmp / "config.json")

    def tearDown(self) -> None:
        if self._saved_config is None:
            os.environ.pop("SCOUTER_CONFIG", None)
        else:
            os.environ["SCOUTER_CONFIG"] = self._saved_config
        self._tmpdir.cleanup()


def _expected_interpolated_kelvin(lo: dict, hi: dict, coeffs: list[float]) -> float:
    """The exact value `wb._invert` computes for a point strictly between
    `lo` and `hi`'s own ratios -- same linear interpolation over r/b,
    reproduced here so `InvertRealTableTest` pins a computed number instead
    of just bounds-checking it (should-fix, fix round 1)."""
    lo_r = lo["coeff_r"] / lo["coeff_b"]
    hi_r = hi["coeff_r"] / hi["coeff_b"]
    target = coeffs[0] / coeffs[2]
    frac = (target - lo_r) / (hi_r - lo_r)
    return lo["k"] + frac * (hi["k"] - lo["k"])


class InvertRealTableTest(_ConfigDirFixture):
    """`wb.kelvin` against the real committed table (design 11.4's object
    shape) -- three points computed from whatever it actually holds: the
    first sample (exact), a midpoint between the first two (interpolated,
    bracket AND value pinned to a computed expectation), and the last
    sample (exact)."""

    def setUp(self) -> None:
        super().setUp()
        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        (wb_dir / "em10iii.json").write_text(
            REAL_TABLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        doc = json.loads(REAL_TABLE.read_text(encoding="utf-8"))
        self.assertIsInstance(doc, dict, "the committed table is the object shape (design 11.4)")
        self.assertFalse(doc["synthetic"])
        self.samples = sorted(doc["samples"], key=lambda p: p["k"])
        self.assertGreaterEqual(len(self.samples), 2, "need at least two points to test")

    def _coeffs(self, sample: dict) -> list[float]:
        return [sample["coeff_r"], sample["coeff_g"], sample["coeff_b"]]

    def test_inverts_the_real_table(self) -> None:
        first = self.samples[0]
        result = wb.kelvin(self._coeffs(first), "em10iii")
        self.assertEqual(result["kelvin"], first["k"])
        self.assertFalse(result["interpolated"])
        self.assertTrue(result["exact"])

        lo, hi = self.samples[0], self.samples[1]
        mid_coeffs = [(a + b) / 2 for a, b in zip(self._coeffs(lo), self._coeffs(hi))]
        expected_k = _expected_interpolated_kelvin(lo, hi, mid_coeffs)
        mid_result = wb.kelvin(mid_coeffs, "em10iii")
        self.assertTrue(mid_result["interpolated"])
        self.assertEqual(mid_result["bracket"], [lo["k"], hi["k"]])
        self.assertAlmostEqual(mid_result["kelvin"], expected_k, places=6)

        last = self.samples[-1]
        last_result = wb.kelvin(self._coeffs(last), "em10iii")
        self.assertEqual(last_result["kelvin"], last["k"])
        self.assertTrue(last_result["exact"])

    def test_out_of_range_is_none_with_a_reason(self) -> None:
        result = wb.kelvin([0.0001, 1.0, 1000.0], "em10iii")
        self.assertIsNone(result["kelvin"])
        self.assertIn("outside", result["reason"])
        self.assertIn("calibrated range", result["reason"])


class LoadTableLegacyShapeTest(_ConfigDirFixture):
    """`load_table` (fix round 1, blocker 1) still reads task 9's original
    bare-array shape -- `synthetic: true` assumed, and a warning surfaced
    all the way out through `wb.kelvin`'s own `warnings`."""

    def setUp(self) -> None:
        super().setUp()
        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        (wb_dir / "em10iii.json").write_text(
            json.dumps(
                [
                    {"kelvin": 3000, "coeffs": [1.2, 1.0, 2.4]},
                    {"kelvin": 6000, "coeffs": [1.8, 1.0, 1.6]},
                ]
            ),
            encoding="utf-8",
        )

    def test_load_table_reads_the_legacy_array_as_synthetic_with_a_warning(self) -> None:
        table = wb.load_table("em10iii")
        assert table is not None
        self.assertTrue(table["synthetic"])
        self.assertEqual([s["kelvin"] for s in table["samples"]], [3000, 6000])
        self.assertTrue(table["warnings"])
        self.assertIn("legacy", table["warnings"][0])

    def test_kelvin_surfaces_the_legacy_warning(self) -> None:
        result = wb.kelvin([1.2, 1.0, 2.4], "em10iii")
        self.assertEqual(result["kelvin"], 3000)
        self.assertTrue(result["warnings"])
        self.assertIn("legacy", result["warnings"][0])


class LatestParamsRealVectorTest(unittest.TestCase):
    def test_latest_params_returns_the_whole_vector(self) -> None:
        parsed = xmp.parse(REAL_XMP.read_text())
        params = xmp.latest_params(parsed, "temperature")
        assert params is not None
        self.assertAlmostEqual(params["coeff_r"], 2.0125747, places=5)
        self.assertAlmostEqual(params["coeff_g"], 1.0, places=5)
        self.assertAlmostEqual(params["coeff_b"], 1.7052786, places=5)
        self.assertIsNone(params["coeff_g2"], "inf -> null, phase 1's rule")
        self.assertIn("params_hex", params)
        self.assertTrue(params["params_hex"])


class _CalibrateFixture(_ConfigDirFixture):
    """A darkroom + bridge fixture for `wb.calibrate`, mirroring
    `tests/test_darkroom_core.py::_DarkroomFixture`'s FakeBridge/patched-
    `_commit` discipline. `darkroom.action` for the Kelvin slider rewrites
    the sidecar with whichever coefficients `self.coeffs_by_k` maps the
    requested Kelvin to, so `calibrate`'s per-point XMP readback sees real
    (if synthetic) values with no darktable involved.
    """

    def setUp(self) -> None:
        super().setUp()
        self._cache_tmp = tempfile.TemporaryDirectory(prefix="scouter-wb-cache-")
        self._saved_cache = os.environ.get("SCOUTER_CACHE")
        os.environ["SCOUTER_CACHE"] = self._cache_tmp.name
        self._saved_job = os.environ.get("SCOUTER_JOB")
        os.environ["SCOUTER_JOB"] = "job-test"

        self.raw = Path(self._cache_tmp.name) / "scratch" / "em10iii.ORF"
        self.raw.parent.mkdir(parents=True, exist_ok=True)
        self.raw.write_bytes(_build_tiff_header())
        Path(str(self.raw) + ".xmp").write_text(
            _temperature_sidecar((1.6, 1.0, 1.8, float("inf")), num=0), encoding="utf-8"
        )

        self.coeffs_by_k: dict[float, tuple[float, float, float]] = {}
        self._num = 1

        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 55, "path": str(self.raw)},
                "darkroom.open": {
                    "image_id": 55,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "opened": True,
                    "already_open": False,
                },
                "darkroom.current": {
                    "image_id": 55,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "darkroom.action": self._action,
            }
        )
        dtbridge.set_bridge(self.bridge)

        self.commit_calls: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto", attempts=None, expect_change=True):
            self.commit_calls.append((job, image, strategy, attempts, expect_change))
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        self._commit_patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        self._commit_patcher.start()
        self.addCleanup(self._commit_patcher.stop)

    def _action(self, params: dict) -> dict:
        value = params.get("value")
        if params.get("path") == "iop/temperature/temperature":
            coeffs = self.coeffs_by_k.get(float(value))
            if coeffs is not None:
                Path(str(self.raw) + ".xmp").write_text(
                    _temperature_sidecar((*coeffs, float("inf")), num=self._num), encoding="utf-8"
                )
                self._num += 1
        return {"nan": False, "status": value, "view": "darkroom", **params}

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved_cache is None:
            os.environ.pop("SCOUTER_CACHE", None)
        else:
            os.environ["SCOUTER_CACHE"] = self._saved_cache
        if self._saved_job is None:
            os.environ.pop("SCOUTER_JOB", None)
        else:
            os.environ["SCOUTER_JOB"] = self._saved_job
        self._cache_tmp.cleanup()
        super().tearDown()


class CalibrateMonotonicityTest(_CalibrateFixture):
    def test_non_monotone_sweep_refused_and_nothing_written(self) -> None:
        self.coeffs_by_k = {
            3000.0: (0.50, 1.0, 1.0),
            3100.0: (0.60, 1.0, 1.0),
            3200.0: (0.55, 1.0, 1.0),
        }
        with self.assertRaises(UsageError) as ctx:
            wb.calibrate(str(self.raw), 3000, 3200, 100, confirm=True, job="job-test")
        self.assertIn("3100", str(ctx.exception))
        self.assertIn("3200", str(ctx.exception))
        detail = ctx.exception.detail
        self.assertEqual(detail["breaking_pair"], [3100, 3200])
        self.assertAlmostEqual(detail["ratios"][0], 0.60)
        self.assertAlmostEqual(detail["ratios"][1], 0.55)
        wb_dir = self.tmp / "wb"
        written = list(wb_dir.glob("*.json")) if wb_dir.exists() else []
        self.assertEqual(written, [], "a non-monotone table must never reach disk")


class CalibrateRefusesInsideJobTest(_CalibrateFixture):
    def test_calibrate_refuses_inside_a_job(self) -> None:
        job_raw_dir = self.raw.parent.parent / "job" / "raw"
        job_raw_dir.mkdir(parents=True, exist_ok=True)
        job_raw = job_raw_dir / "em10iii.ORF"
        shutil.copy(self.raw, job_raw)
        shutil.copy(str(self.raw) + ".xmp", str(job_raw) + ".xmp")
        self.bridge.responses["image.lookup"] = {
            "found": True,
            "image_id": 77,
            "path": str(job_raw),
        }

        with self.assertRaises(UsageError) as ctx:
            wb.calibrate(str(job_raw), 3000, 3100, 100, job="job-test")
        self.assertIn("raw/", str(ctx.exception))
        self.assertEqual(self.bridge.count("darkroom.open"), 0)


class CalibrateDryRunTest(_CalibrateFixture):
    def test_calibrate_dry_run_lists_the_k_values_and_estimate(self) -> None:
        result = wb.calibrate(str(self.raw), 3000, 3200, 100, dry_run=True, job="job-test")
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(result.ks, [3000, 3100, 3200])
        self.assertAlmostEqual(result.estimated_s, 3 * 1.8, places=3)
        self.assertEqual(self.bridge.count("darkroom.action"), 0)
        self.assertEqual(self.bridge.count("darkroom.open"), 0)


class CalibrateOverwriteTest(_CalibrateFixture):
    def test_calibrate_does_not_overwrite_without_confirm(self) -> None:
        from scouter.config import load as load_config
        from scouter.core import exif

        tags = exif.read_tags(self.raw)
        camera = exif.camera_slug(tags, load_config())
        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        table_path = wb_dir / f"{camera}.json"
        original = json.dumps([{"kelvin": 3000, "coeffs": [1.0, 1.0, 1.0]}])
        table_path.write_text(original)

        # Gated (design: "wb.calibrate: gated yes"): without confirm, this is
        # a dry run -- never a raised error, and never a touch on the table.
        result = wb.calibrate(str(self.raw), 3000, 3100, 100, job="job-test")
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("confirm", result.reason)
        self.assertEqual(
            table_path.read_text(), original, "an unconfirmed run must not touch the file"
        )
        self.assertEqual(self.bridge.count("darkroom.open"), 0)

    def test_calibrate_overwrites_an_existing_table_when_confirmed(self) -> None:
        """`confirm` is the one gate: passing it lets a second calibrate call
        overwrite a table that already exists, and the result says so."""
        from scouter.config import load as load_config
        from scouter.core import exif

        tags = exif.read_tags(self.raw)
        camera = exif.camera_slug(tags, load_config())
        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        table_path = wb_dir / f"{camera}.json"
        table_path.write_text(json.dumps([{"kelvin": 3000, "coeffs": [1.0, 1.0, 1.0]}]))

        self.coeffs_by_k = {3000.0: (1.2, 1.0, 2.4), 3100.0: (1.3, 1.0, 2.2)}
        result = wb.calibrate(str(self.raw), 3000, 3100, 100, confirm=True, job="job-test")
        self.assertEqual(result.outcome, "ok")
        self.assertTrue(result.written)
        self.assertTrue(result.overwritten)


class CalibrateDarktableVersionProvenanceTest(_CalibrateFixture):
    """Fix round 2, IMPORTANT 1/2: `calibrate`'s `darktable` provenance
    field never shells out to a real `darktable-cli` when a `FakeBridge` is
    installed (`subprocess.run` must never be called from a non-host unit
    test), and when a version resolver IS used, only the parsed `X.Y.Z`
    token is stored -- never the raw multi-line `--version` blob."""

    def test_calibrate_never_calls_subprocess_run_with_a_fake_bridge(self) -> None:
        self.coeffs_by_k = {3000.0: (1.2, 1.0, 2.4), 3100.0: (1.3, 1.0, 2.2)}
        with mock.patch("subprocess.run") as run:
            result = wb.calibrate(str(self.raw), 3000, 3100, 100, confirm=True, job="job-test")
        run.assert_not_called()
        self.assertEqual(result.outcome, "ok")

    def test_calibrate_records_unknown_with_no_resolver_and_a_fake_bridge(self) -> None:
        self.coeffs_by_k = {3000.0: (1.2, 1.0, 2.4), 3100.0: (1.3, 1.0, 2.2)}
        wb.calibrate(str(self.raw), 3000, 3100, 100, confirm=True, job="job-test")
        table_path = next((self.tmp / "wb").glob("*.json"))
        doc = json.loads(table_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["darktable"], "unknown")

    def test_calibrate_stores_only_the_parsed_version_token(self) -> None:
        # Fix round 2, IMPORTANT 2: a realistic multi-line, URL-bearing
        # `--version` blob -- only "5.4.1" (the first line's token) may
        # ever reach the table.
        multiline_stdout = (
            "this is darktable 5.4.1\n"
            "Copyright (C) 2026 darktable developers.\n"
            "\n"
            "This program is free software.\n"
            "See https://www.darktable.org/ for more detail.\n"
        )
        self.coeffs_by_k = {3000.0: (1.2, 1.0, 2.4), 3100.0: (1.3, 1.0, 2.2)}
        # `_resolve_darktable_version` is the test seam (not a `calibrate(...,
        # darktable_version_resolver=...)` parameter -- that would give the
        # registered verb a Python parameter with no CLI/MCP-representable
        # type, breaking the registry/core-function signature pin).
        with mock.patch(
            "scouter.core.wb._resolve_darktable_version",
            return_value=wb._parse_darktable_version(multiline_stdout),
        ):
            wb.calibrate(str(self.raw), 3000, 3100, 100, confirm=True, job="job-test")
        table_path = next((self.tmp / "wb").glob("*.json"))
        doc = json.loads(table_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["darktable"], "5.4.1")
        self.assertNotIn("Copyright", doc["darktable"])
        self.assertNotIn("\n", doc["darktable"])


class ParseDarktableVersionTest(unittest.TestCase):
    """`wb._parse_darktable_version` directly: the first line's `X.Y.Z`
    token, or `"unknown"` -- never `None`, never the raw blob."""

    def test_parses_the_first_lines_version_token(self) -> None:
        stdout = (
            "this is darktable 5.4.1\n"
            "Copyright (C) 2026 darktable developers.\n"
            "See https://www.darktable.org/ for more detail.\n"
        )
        self.assertEqual(wb._parse_darktable_version(stdout), "5.4.1")

    def test_unparseable_output_is_unknown(self) -> None:
        self.assertEqual(wb._parse_darktable_version("not a version string at all"), "unknown")

    def test_none_is_unknown(self) -> None:
        self.assertEqual(wb._parse_darktable_version(None), "unknown")

    def test_empty_string_is_unknown(self) -> None:
        self.assertEqual(wb._parse_darktable_version(""), "unknown")


class UndoRestoresEveryCoefficientTest(unittest.TestCase):
    """Tier 2 (design 11.3): with all four coefficient sliders pinned, undo
    sends one `set` per component, ONE commit, and verifies the whole vector
    -- a failure on any single component leaves the journal entry open."""

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "test.ORF"
        self.raw.write_bytes(b"not really a raw")

        self.before_params = {"coeff_r": 2.0, "coeff_g": 1.0, "coeff_b": 1.8, "coeff_g2": 1.5}
        self._pending = {"coeff_r": 1.2, "coeff_g": 0.9, "coeff_b": 2.4, "coeff_g2": 0.5}
        self._write_sidecar(self._pending, num=14)
        self.fail_component: str | None = None

        self._original_action_paths = dict(dtbridge.ACTION_PATHS)
        dtbridge.ACTION_PATHS.update(
            {
                ("temperature", "coeff_r"): "iop/temperature/red",
                ("temperature", "coeff_g"): "iop/temperature/green",
                ("temperature", "coeff_b"): "iop/temperature/blue",
                ("temperature", "coeff_g2"): "iop/temperature/emerald",
            }
        )

        self.bridge = FakeBridge(
            {
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.action": self._action,
            }
        )
        dtbridge.set_bridge(self.bridge)

        self.commit_calls: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto", attempts=None, expect_change=True):
            self.commit_calls.append((job, image, strategy, attempts, expect_change))
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        self._commit_patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        self._commit_patcher.start()
        self.addCleanup(self._commit_patcher.stop)

        entry = journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=12,
            path=str(self.raw),
            module="temperature",
            param="temperature",
            instance=0,
            before={
                "source": "xmp",
                "enabled": True,
                "history_end": 14,
                "value": None,
                "params": dict(self.before_params),
                "params_hex": "deadbeef",
            },
        )
        journal.complete("job-test", entry.seq, after={"value": 6000.0})

    def _write_sidecar(self, coeffs: dict, *, num: int) -> None:
        text = _temperature_sidecar(
            (coeffs["coeff_r"], coeffs["coeff_g"], coeffs["coeff_b"], coeffs["coeff_g2"]),
            num=num,
            history_end=num + 1,
        )
        Path(str(self.raw) + ".xmp").write_text(text, encoding="utf-8")

    def _action(self, params: dict) -> dict:
        path_ = params.get("path")
        value = params.get("value")
        name_by_path = {v: k[1] for k, v in dtbridge.ACTION_PATHS.items() if k[0] == "temperature"}
        name = name_by_path.get(path_)
        if name is not None and name != self.fail_component:
            self._pending[name] = value
            self._write_sidecar(self._pending, num=15)
        return {"nan": False, "status": value, "view": "darkroom", **params}

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        dtbridge.ACTION_PATHS.clear()
        dtbridge.ACTION_PATHS.update(self._original_action_paths)
        self.cache.stop()

    def test_undo_restores_every_coefficient(self) -> None:
        result = darkroom.undo_last()
        sent = [p for m, p in self.bridge.calls if m == "darkroom.action"]
        self.assertEqual(len(sent), 4)
        self.assertEqual(
            {p["value"] for p in sent},
            set(self.before_params.values()),
        )
        self.assertEqual(len(self.commit_calls), 1)
        assert result.undone is not None
        self.assertTrue(result.undone["verified"])
        self.assertIsNone(journal.latest_open("job-test"))

    def test_dry_run_reports_the_coefficient_vector_and_the_tier(self) -> None:
        # Should-fix (fix round 1): a dry run on a temperature entry never
        # sends anything, but names the journalled coefficients and
        # predicts the tier a real call would take -- here "coefficient_
        # sliders" (tier 2), since all four are pinned in this fixture.
        result = darkroom.undo_last(dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(self.bridge.count("darkroom.action"), 0)
        self.assertEqual(len(self.commit_calls), 0)
        plan_entry = result.plan[0]
        self.assertEqual(plan_entry["coefficients"], self.before_params)
        self.assertEqual(plan_entry["tier"], "coefficient_sliders")

    def test_a_verification_failure_on_one_component_leaves_the_entry_open(self) -> None:
        self.fail_component = "coeff_g"
        result = darkroom.undo_last()
        assert result.undone is not None
        self.assertFalse(result.undone["verified"])
        entry = journal.latest_open("job-test")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertFalse(entry.undone, "a failed component must leave the entry undone: false")


class UndoViaKelvinInversionTest(unittest.TestCase):
    """Tier 3 (design 11.4): no pinned coefficient sliders, but a measured
    table exists -- undo inverts the journalled coefficients to a target
    Kelvin and restores through the ordinary `temperature` slider, verified
    within `wb._EXACT_TOLERANCE` (fix round 1, blocker 2/3). A verification
    failure raises `_UndoUnverified` (`undo_unverified`) rather than
    reporting an unconfirmed restore as done, and leaves the entry open.
    """

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "test.ORF"
        self.raw.write_bytes(_build_tiff_header())

        self._saved_config = os.environ.get("SCOUTER_CONFIG")
        os.environ["SCOUTER_CONFIG"] = str(root / "config.json")

        from scouter.config import load as load_config
        from scouter.core import exif

        tags = exif.read_tags(self.raw)
        self.camera = exif.camera_slug(tags, load_config())
        wb_dir = root / "wb"
        wb_dir.mkdir(parents=True)
        (wb_dir / f"{self.camera}.json").write_text(
            REAL_TABLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        table = json.loads(REAL_TABLE.read_text(encoding="utf-8"))
        samples = sorted(table["samples"], key=lambda p: p["k"])
        exact = samples[3]
        self.target_kelvin = exact["k"]
        self.before_params = {
            "coeff_r": exact["coeff_r"],
            "coeff_g": exact["coeff_g"],
            "coeff_b": exact["coeff_b"],
            "coeff_g2": None,
        }
        self._target_ratio = exact["coeff_r"] / exact["coeff_b"]

        # No pinned coefficient sliders -- tier 3 is what must run.
        self._original_action_paths = dict(dtbridge.ACTION_PATHS)
        for key in list(dtbridge.ACTION_PATHS):
            if key[0] == "temperature" and key[1].startswith("coeff_"):
                del dtbridge.ACTION_PATHS[key]

        self._landed_ratio: float | None = None
        self.bridge = FakeBridge(
            {
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "darkroom.action": self._action,
            }
        )
        dtbridge.set_bridge(self.bridge)

        self.commit_calls: list[tuple] = []

        def fake_commit(job, image, *, strategy="auto", attempts=None, expect_change=True):
            self.commit_calls.append((job, image, strategy, attempts, expect_change))
            return CommitResult(committed=True, changed=expect_change, warnings=[])

        self._commit_patcher = mock.patch("scouter.core.darkroom._commit", side_effect=fake_commit)
        self._commit_patcher.start()
        self.addCleanup(self._commit_patcher.stop)

        entry = journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=12,
            path=str(self.raw),
            module="temperature",
            param="temperature",
            instance=0,
            before={
                "source": "xmp",
                "enabled": True,
                "history_end": 14,
                "value": None,
                "params": dict(self.before_params),
                "params_hex": "deadbeef",
            },
        )
        journal.complete("job-test", entry.seq, after={"value": 6500.0})

    def _action(self, params: dict) -> dict:
        value = params.get("value")
        if params.get("path") == "iop/temperature/temperature" and self._landed_ratio is not None:
            Path(str(self.raw) + ".xmp").write_text(
                _temperature_sidecar((self._landed_ratio, 1.0, 1.0, float("inf")), num=15),
                encoding="utf-8",
            )
        return {"nan": False, "status": value, "view": "darkroom", **params}

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        dtbridge.ACTION_PATHS.clear()
        dtbridge.ACTION_PATHS.update(self._original_action_paths)
        if self._saved_config is None:
            os.environ.pop("SCOUTER_CONFIG", None)
        else:
            os.environ["SCOUTER_CONFIG"] = self._saved_config
        self.cache.stop()

    def test_undo_via_kelvin_inversion_when_verified(self) -> None:
        self._landed_ratio = self._target_ratio  # exact match: within tolerance
        result = darkroom.undo_last()
        sent = [
            p
            for m, p in self.bridge.calls
            if m == "darkroom.action" and p["path"] == "iop/temperature/temperature"
        ]
        self.assertEqual(len(sent), 1)
        self.assertAlmostEqual(sent[0]["value"], self.target_kelvin, places=0)
        self.assertEqual(len(self.commit_calls), 1)
        assert result.undone is not None
        self.assertTrue(result.undone["verified"])
        self.assertEqual(result.undone["tier"], "kelvin_inversion")
        self.assertEqual(result.undone["restored_kelvin"], self.target_kelvin)
        self.assertIsNone(journal.latest_open("job-test"))

    def test_undo_via_kelvin_inversion_unverified_raises_and_leaves_entry_open(self) -> None:
        self._landed_ratio = self._target_ratio * 2  # well outside wb._EXACT_TOLERANCE
        with self.assertRaises(darkroom._UndoUnverified) as ctx:
            darkroom.undo_last()
        self.assertEqual(ctx.exception.code, "undo_unverified")
        detail = ctx.exception.detail
        self.assertAlmostEqual(detail["measured_ratio"], self._landed_ratio, places=4)
        self.assertAlmostEqual(detail["target_ratio"], self._target_ratio, places=4)
        self.assertEqual(detail["tolerance"], wb._EXACT_TOLERANCE)
        # Fix round 2, minor (a): `_commit_with_readback`'s own warnings must
        # not be silently dropped on the unverified path.
        self.assertIn("warnings", detail)
        self.assertIsInstance(detail["warnings"], list)
        entry = journal.latest_open("job-test")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertFalse(entry.undone, "an unverified tier-3 undo must leave the entry open")


class WbKelvinVerbTest(_ConfigDirFixture):
    """`wb.kelvin` as a REGISTERED verb, through `registry.call` end to end
    (fix round 1, blocker 3) -- not just the undecorated `kelvin()` function
    `outliers.list`/`style.py` call directly."""

    def setUp(self) -> None:
        super().setUp()
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "em10iii.ORF"
        self.raw.write_bytes(_build_tiff_header())

        from scouter.config import load as load_config
        from scouter.core import exif

        tags = exif.read_tags(self.raw)
        self.camera = exif.camera_slug(tags, load_config())

        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        (wb_dir / f"{self.camera}.json").write_text(
            REAL_TABLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        table = json.loads(REAL_TABLE.read_text(encoding="utf-8"))
        first = sorted(table["samples"], key=lambda p: p["k"])[0]
        Path(str(self.raw) + ".xmp").write_text(
            _temperature_sidecar(
                (first["coeff_r"], first["coeff_g"], first["coeff_b"], float("inf")), num=0
            ),
            encoding="utf-8",
        )
        self.expected_kelvin = first["k"]

        self.bridge = FakeBridge(
            {"image.lookup": {"found": True, "image_id": 9, "path": str(self.raw)}}
        )
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()
        super().tearDown()

    def test_wb_kelvin_verb_through_registry(self) -> None:
        from scouter import registry

        envelope = registry.call("wb.kelvin", "cli", image=str(self.raw))
        self.assertTrue(envelope.ok, envelope.to_dict())
        self.assertEqual(envelope.data["kelvin"], self.expected_kelvin)
        self.assertTrue(envelope.data["exact"])
        self.assertEqual(envelope.data["camera"], self.camera)


class RefusalCarriesCoefficientsTest(unittest.TestCase):
    """Tier 4: no pinned sliders, no calibration table -- the refusal hands
    over all four journalled coefficients, a (best-effort, here unavailable)
    target Kelvin, and the `scouter wb calibrate` hint."""

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "test.ORF"
        self.raw.write_bytes(b"not really a raw")  # deliberately not readable EXIF

        self.before_params = {"coeff_r": 2.0, "coeff_g": 1.0, "coeff_b": 1.8, "coeff_g2": None}
        Path(str(self.raw) + ".xmp").write_text(
            _temperature_sidecar((1.2, 0.9, 2.4, float("inf")), num=14, history_end=15),
            encoding="utf-8",
        )

        self._original_action_paths = dict(dtbridge.ACTION_PATHS)
        for key in list(dtbridge.ACTION_PATHS):
            if key[0] == "temperature" and key[1].startswith("coeff_"):
                del dtbridge.ACTION_PATHS[key]

        self.bridge = FakeBridge(
            {
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
            }
        )
        dtbridge.set_bridge(self.bridge)

        self._saved_config = os.environ.get("SCOUTER_CONFIG")
        os.environ["SCOUTER_CONFIG"] = str(root / "no-such-config.json")

        entry = journal.begin(
            "job-test",
            verb="darkroom.set",
            image_id=12,
            path=str(self.raw),
            module="temperature",
            param="temperature",
            instance=0,
            before={
                "source": "xmp",
                "enabled": True,
                "history_end": 14,
                "value": None,
                "params": dict(self.before_params),
                "params_hex": "deadbeef",
            },
        )
        journal.complete("job-test", entry.seq, after={"value": 6000.0})

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        dtbridge.ACTION_PATHS.clear()
        dtbridge.ACTION_PATHS.update(self._original_action_paths)
        if self._saved_config is None:
            os.environ.pop("SCOUTER_CONFIG", None)
        else:
            os.environ["SCOUTER_CONFIG"] = self._saved_config
        self.cache.stop()

    def test_refusal_carries_the_coefficients(self) -> None:
        with self.assertRaises(darkroom._UnsupportedUndo) as ctx:
            darkroom.undo_last()
        self.assertEqual(ctx.exception.code, "unsupported_undo")
        detail = ctx.exception.detail
        self.assertEqual(detail["coefficients"], self.before_params)
        self.assertIn("scouter wb calibrate", detail["hint"])
        self.assertIn("target_kelvin", detail)
        entry = journal.latest_open("job-test")
        self.assertIsNotNone(entry, "a refused undo leaves the write journaled and reachable")


class RatioHelpersTest(unittest.TestCase):
    """Carried finding 10 (task 11): the tier-3 Kelvin-inversion undo spelled
    the r/b ratio and its tolerance test out three times (`_after_ratio`,
    `_confirm`, and again inlined for `verified`). Three copies of the
    comparison that decides whether an undo is reported verified is three
    chances to drift; these are the one helper each, tested directly."""

    def test_rb_ratio(self) -> None:
        from scouter.core import darkroom

        self.assertAlmostEqual(darkroom._rb_ratio({"coeff_r": 2.0, "coeff_b": 1.0}), 2.0)
        self.assertIsNone(darkroom._rb_ratio(None))
        self.assertIsNone(darkroom._rb_ratio({}))
        self.assertIsNone(darkroom._rb_ratio({"coeff_r": 2.0}), "no blue, no ratio")
        self.assertIsNone(darkroom._rb_ratio({"coeff_b": 1.0}), "no red, no ratio")
        self.assertIsNone(
            darkroom._rb_ratio({"coeff_r": 2.0, "coeff_b": 0.0}), "zero blue is not a ratio of 0"
        )
        # Fix round 1, minor b: the docstring promised None for a non-finite
        # blue and the code returned `2.0 / inf == 0.0` -- a finite-looking
        # ratio that the tolerance test would then compare. `coeff_g2 = inf`
        # is a real value on this camera, so a stray inf is not hypothetical.
        self.assertIsNone(
            darkroom._rb_ratio({"coeff_r": 2.0, "coeff_b": float("inf")}),
            "r / inf is 0.0, which is not a ratio -- it must be None",
        )
        self.assertIsNone(
            darkroom._rb_ratio({"coeff_r": float("inf"), "coeff_b": 2.0}),
            "a non-finite red has no ratio either",
        )
        self.assertIsNone(darkroom._rb_ratio({"coeff_r": 2.0, "coeff_b": float("nan")}))
        self.assertIsNone(darkroom._rb_ratio({"coeff_r": float("nan"), "coeff_b": 2.0}))

    def test_a_non_finite_coefficient_never_reads_as_verified(self) -> None:
        """The consequence, not just the helper: an inf that used to become
        0.0 went on to be COMPARED, and `0.0` against a target of `0.0`
        would have reported a verified undo."""
        from scouter.core import darkroom

        self.assertFalse(
            darkroom._ratio_within_tolerance(
                darkroom._rb_ratio({"coeff_r": 2.0, "coeff_b": float("inf")}), 0.0
            )
        )

    def test_ratio_within_tolerance(self) -> None:
        from scouter.core import darkroom, wb

        tol = wb._EXACT_TOLERANCE
        self.assertTrue(darkroom._ratio_within_tolerance(2.0, 2.0))
        self.assertTrue(darkroom._ratio_within_tolerance(2.0 * (1 + tol / 2), 2.0))
        self.assertFalse(darkroom._ratio_within_tolerance(2.0 * (1 + tol * 4), 2.0))
        # "Cannot compare" is never "verified".
        self.assertFalse(darkroom._ratio_within_tolerance(None, 2.0))
        self.assertFalse(darkroom._ratio_within_tolerance(2.0, None))
        self.assertFalse(darkroom._ratio_within_tolerance(None, None))
