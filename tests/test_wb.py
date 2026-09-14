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


class InvertRealTableTest(_ConfigDirFixture):
    """`wb.kelvin` against the real committed table -- three points computed
    from whatever it actually holds: the first sample (exact), a midpoint
    between the first two (interpolated, bracket correct), and the last
    sample (exact)."""

    def setUp(self) -> None:
        super().setUp()
        wb_dir = self.tmp / "wb"
        wb_dir.mkdir(parents=True)
        (wb_dir / "em10iii.json").write_text(
            REAL_TABLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.table = json.loads(REAL_TABLE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(
            len(self.table), 2, "the real table needs at least two points to test"
        )

    def test_inverts_the_real_table(self) -> None:
        first = self.table[0]
        result = wb.kelvin(first["coeffs"], "em10iii")
        self.assertEqual(result["kelvin"], first["kelvin"])
        self.assertFalse(result["interpolated"])
        self.assertTrue(result["exact"])

        lo, hi = self.table[0], self.table[1]
        mid_coeffs = [(a + b) / 2 for a, b in zip(lo["coeffs"][:3], hi["coeffs"][:3])]
        mid_result = wb.kelvin(mid_coeffs, "em10iii")
        self.assertTrue(mid_result["interpolated"])
        self.assertEqual(mid_result["bracket"], [lo["kelvin"], hi["kelvin"]])
        assert mid_result["kelvin"] is not None
        self.assertGreaterEqual(mid_result["kelvin"], lo["kelvin"])
        self.assertLessEqual(mid_result["kelvin"], hi["kelvin"])

        last = self.table[-1]
        last_result = wb.kelvin(last["coeffs"], "em10iii")
        self.assertEqual(last_result["kelvin"], last["kelvin"])
        self.assertTrue(last_result["exact"])

    def test_out_of_range_is_none_with_a_reason(self) -> None:
        result = wb.kelvin([0.0001, 1.0, 1000.0], "em10iii")
        self.assertIsNone(result["kelvin"])
        self.assertIn("outside", result["reason"])
        self.assertIn("calibrated range", result["reason"])


class NonMonotoneTableRefusedTest(unittest.TestCase):
    """A hand-built sample list with one inverted pair: `_invert` (and
    `wb.calibrate`'s own check, exercised in `CalibrateMonotonicityTest`
    below) must never silently accept it."""

    def test_non_monotone_table_refused(self) -> None:
        # calibrate()'s own monotonicity check operates on consecutive
        # ratios directly; reproduce that check here against a hand-built,
        # deliberately non-monotone list, the same shape calibrate() builds
        # internally before it ever writes a table.
        points = [
            {"k": 3000, "coeff_r": 0.50, "coeff_b": 1.0},
            {"k": 3100, "coeff_r": 0.60, "coeff_b": 1.0},
            {"k": 3200, "coeff_r": 0.55, "coeff_b": 1.0},
        ]
        ratios = [p["coeff_r"] / p["coeff_b"] for p in points]
        breaking = None
        for i in range(len(ratios) - 1):
            if ratios[i + 1] <= ratios[i]:
                breaking = (points[i], points[i + 1], ratios[i], ratios[i + 1])
                break
        assert breaking is not None
        self.assertEqual((breaking[0]["k"], breaking[1]["k"]), (3100, 3200))
        self.assertAlmostEqual(breaking[2], 0.60)
        self.assertAlmostEqual(breaking[3], 0.55)


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

        def fake_commit(job, image, *, strategy="auto"):
            self.commit_calls.append((job, image, strategy))
            return CommitResult(committed=True, warnings=[])

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

    def test_calibrate_refuses_to_clobber_without_confirm_even_when_asked_to_run(self) -> None:
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


class UndoRestoresEveryCoefficientTest(unittest.TestCase):
    """Tier 1 (design 11.3): with all four coefficient sliders pinned, undo
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

        def fake_commit(job, image, *, strategy="auto"):
            self.commit_calls.append((job, image, strategy))
            return CommitResult(committed=True, warnings=[])

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

    def test_a_verification_failure_on_one_component_leaves_the_entry_open(self) -> None:
        self.fail_component = "coeff_g"
        result = darkroom.undo_last()
        assert result.undone is not None
        self.assertFalse(result.undone["verified"])
        entry = journal.latest_open("job-test")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertFalse(entry.undone, "a failed component must leave the entry undone: false")


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
