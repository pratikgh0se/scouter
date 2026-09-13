"""Tests for `scouter.core.library`: scan, plan, copy+checksum, verify.

Every card is synthetic: `tests/fixtures/exif-orf-header.bin` (the same real
E-M10 Mark III header `test_exif.py` uses) plus `os.urandom` padding so each
file's bytes -- and therefore its sha256 -- differ, with capture times patched
in place via `exif.datetime_offsets` (never a real file under ~/Pictures).
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter import config
from scouter.core import dtbridge, exif, library
from scouter.errors import (
    BackupNotConfigured,
    BridgeTimeout,
    ChecksumMismatch,
    DestinationCollision,
    UsageError,
)
from tests.scouter_fakes import FakeBridge, TempCache, config_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "exif-orf-header.bin"


def _write_one(directory: Path, name: str, time_str: str, *, tail: bytes | None = None) -> Path:
    """One synthetic raw: the real ORF header + random padding, with its
    DateTime/DateTimeOriginal fields patched to `time_str` ("YYYY:MM:DD HH:MM:SS")."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(FIXTURE.read_bytes() + (tail if tail is not None else os.urandom(2048)))
    stamp = time_str.encode("ascii") + b"\x00"
    offsets = exif.datetime_offsets(path)
    with path.open("r+b") as fh:
        for offset, length in offsets:
            fh.seek(offset)
            fh.write(stamp[:length].ljust(length, b"\x00"))
    return path


def _make_card(tmp: Path, n: int, *, prefix: str = "P628", ext: str = ".ORF") -> list[Path]:
    """`n` synthetic raws, one second apart, in capture order == filename order."""
    base = datetime(2026, 6, 28, 15, 49, 28)
    return [
        _write_one(
            tmp,
            f"{prefix}{1000 + i}{ext}",
            (base + timedelta(seconds=i)).strftime("%Y:%m:%d %H:%M:%S"),
        )
        for i in range(n)
    ]


def _flip_byte(path: Path) -> None:
    """Corrupt one byte of a (possibly read-only) file, restoring its mode."""
    mode = path.stat().st_mode & 0o777
    path.chmod(0o644)
    with path.open("r+b") as fh:
        byte = fh.read(1)
        fh.seek(0)
        fh.write(bytes([byte[0] ^ 0xFF]))
    path.chmod(mode)


class LibraryTestCase(unittest.TestCase):
    """A passing backup gate by default; `library.config.load` is patched so
    every call inside `scouter.core.library` sees `self.cfg`, exactly the way
    production code sees whatever `scouter config init` wrote -- no
    `--config` flag exists for these verbs (design section 6)."""

    def setUp(self) -> None:
        self.cache = TempCache()
        self.tmp = self.cache.start()
        self.cfg = config_fixture(self.tmp)
        self._cfg_patch = mock.patch.object(library.config, "load", return_value=self.cfg)
        self._cfg_patch.start()
        self.bridge = FakeBridge()
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self._cfg_patch.stop()
        self.cache.stop()

    def _job_path(self, date: str = "2026-06-28", slug: str = "harbour-wedding") -> Path:
        return config.job_dir(self.cfg, date, slug)


class GateOrderTest(LibraryTestCase):
    def test_gate_runs_before_the_card_is_read(self) -> None:
        bad_cfg = config_fixture(self.tmp, backup=False)
        with mock.patch.object(library.config, "load", return_value=bad_cfg):
            with self.assertRaises(BackupNotConfigured):
                library.import_card(
                    str(self.tmp / "no-such-card-mount"), "harbour-wedding", confirm=True
                )


class PlanTest(LibraryTestCase):
    def test_plan_names_and_sequences(self) -> None:
        source = self.tmp / "card"
        # "ZZZ..." sorts AFTER "AAA..." by filename but was captured EARLIER --
        # the plan must order by capture time, not filename.
        _write_one(source, "ZZZ0001.ORF", "2026:06:28 15:49:28")
        _write_one(source, "AAA0002.ORF", "2026:06:28 15:49:30")
        _write_one(source, "ZZZ0001.JPG", "2026:06:28 15:49:28")

        result = library.import_card(str(source), "harbour-wedding", confirm=True)

        dest_names = {Path(f["dest"]).name for f in result.files}
        self.assertIn("2026-06-28_harbour-wedding_em10iii_0001.orf", dest_names)

        by_source = {f["source_name"]: f for f in result.files}
        self.assertEqual(by_source["ZZZ0001.ORF"]["seq"], 1)
        self.assertEqual(by_source["ZZZ0001.JPG"]["seq"], 1)  # shared stem
        self.assertEqual(by_source["AAA0002.ORF"]["seq"], 2)  # later capture time

    def test_mixed_camera_card_refused_without_camera_flag(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        with mock.patch.object(exif, "camera_slug", side_effect=["em10iii", "a7"]):
            with self.assertRaises(UsageError) as ctx:
                library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertIn("em10iii", str(ctx.exception))
        self.assertIn("a7", str(ctx.exception))

    def test_camera_flag_overrides(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        with mock.patch.object(exif, "camera_slug", side_effect=["em10iii", "a7"]):
            result = library.import_card(
                str(source), "harbour-wedding", camera="em10iii", confirm=True
            )
        self.assertEqual(result.camera, "em10iii")


class DryRunTest(LibraryTestCase):
    def test_dry_run_creates_nothing(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)

        result = library.import_card(str(source), "harbour-wedding", dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertFalse(self._job_path().exists())
        self.assertEqual(self.bridge.calls, [])

        # No --confirm at all is ALSO a dry run (an import always touches
        # more than one file, so it is always gated).
        result2 = library.import_card(str(source), "harbour-wedding")
        self.assertEqual(result2.outcome, "dry_run")
        self.assertFalse(self._job_path().exists())
        self.assertEqual(self.bridge.calls, [])


class CopyTest(LibraryTestCase):
    def test_copy_verifies_and_chmods(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result.outcome, "ok")

        job_path = Path(result.path)
        checksum_lines = (job_path / ".scouter" / "checksums.sha256").read_text().splitlines()
        self.assertEqual(len(checksum_lines), 3)
        for entry in result.files:
            dest = job_path / entry["dest"]
            self.assertEqual(dest.stat().st_mode & 0o777, 0o444)
            self.assertEqual(library._hash_file(dest), entry["sha256"])

    def test_checksum_mismatch_deletes_and_stops(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        real_replace = os.replace
        flipped = {"done": False}

        def flipping_replace(src, dst) -> None:
            real_replace(src, dst)
            if not flipped["done"]:
                flipped["done"] = True
                _flip_byte(Path(dst))

        with mock.patch.object(library.os, "replace", side_effect=flipping_replace):
            with self.assertRaises(ChecksumMismatch):
                library.import_card(str(source), "harbour-wedding", confirm=True)

        raw = self._job_path() / "raw"
        # The corrupted file's destination was deleted by `_copy_verified`
        # itself, and being first in capture order, nothing after it in the
        # plan was ever written.
        self.assertEqual(list(raw.iterdir()) if raw.exists() else [], [])

    def test_resume_skips_identical_and_refuses_collision(self) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 2)

        with mock.patch.object(library.os, "chmod", wraps=os.chmod) as chmod_spy:
            result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result1.totals["copied"], 2)
        self.assertEqual(chmod_spy.call_count, 2)

        with mock.patch.object(library.os, "chmod", wraps=os.chmod) as chmod_spy2:
            result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result2.totals["skipped_identical"], 2)
        self.assertEqual(result2.totals["copied"], 0)
        self.assertEqual(chmod_spy2.call_count, 0)  # never re-chmod-ed

        # Now the source content changes under the same name: resume must
        # refuse rather than silently overwrite.
        files[0].write_bytes(files[0].read_bytes() + b"more-bytes-now")
        with self.assertRaises(DestinationCollision):
            library.import_card(str(source), "harbour-wedding", confirm=True)


class FilmRollTest(LibraryTestCase):
    def test_import_succeeds_with_darktable_down(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        self.bridge.raises["import_batch"] = BridgeTimeoutError("darktable not reachable")

        result = library.import_card(str(source), "harbour-wedding", confirm=True)

        self.assertEqual(result.outcome, "ok")  # the copy is the irreversible part
        self.assertFalse(result.film_roll_registered)
        self.assertIn(BridgeTimeout.fix, result.warnings)


class ChecksumsFileTest(LibraryTestCase):
    def test_checksums_file_is_coreutils_format(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        text = (Path(result.path) / ".scouter" / "checksums.sha256").read_text()
        lines = text.splitlines()

        rels = []
        for line in lines:
            sha, sep, rel = line.partition("  ")  # exactly two spaces
            self.assertEqual(sep, "  ")
            self.assertEqual(len(sha), 64)
            self.assertTrue(all(c in "0123456789abcdef" for c in sha))
            self.assertTrue(rel.startswith("raw/"))
            self.assertTrue(rel.endswith(".orf"))
            rels.append(rel)
        self.assertEqual(rels, sorted(rels))  # sorted by name


class VerifyTest(LibraryTestCase):
    def test_verify_detects_mismatch_missing_extra(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result.path)
        raw = job_path / "raw"

        name0 = result.files[0]["dest"].split("raw/", 1)[1]
        name1 = result.files[1]["dest"].split("raw/", 1)[1]
        _flip_byte(raw / name0)
        (raw / name1).chmod(0o644)
        (raw / name1).unlink()
        (raw / "unlisted.orf").write_bytes(b"not in the manifest")

        vr = library.verify("harbour-wedding")
        self.assertFalse(vr.ok)
        self.assertEqual([m["name"] for m in vr.mismatched], [name0])
        self.assertEqual(vr.missing, [name1])
        self.assertIn("unlisted.orf", vr.extra)

    def test_verify_ignores_xmp_sidecars(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 1)
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result.path)
        name0 = result.files[0]["dest"].split("raw/", 1)[1]
        (job_path / "raw" / f"{name0}.xmp").write_text("<xmp/>", encoding="utf-8")

        vr = library.verify("harbour-wedding")
        self.assertTrue(vr.ok)
        self.assertEqual(vr.extra, [])
        self.assertEqual(vr.checked, 1)


class SingleEventTest(LibraryTestCase):
    def test_import_emits_exactly_one_event(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        before = len(self.cache.events())
        library.import_card(str(source), "harbour-wedding", confirm=True)
        after = self.cache.events()
        self.assertEqual(len(after) - before, 1)
        self.assertEqual(after[-1]["verb"], "library.import")


if __name__ == "__main__":
    unittest.main()
