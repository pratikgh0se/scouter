"""Tests for `scouter.core.library`: scan, plan, copy+checksum, verify.

Every card is synthetic: `tests/fixtures/exif-orf-header.bin` (the same real
E-M10 Mark III header `test_exif.py` uses) plus `os.urandom` padding so each
file's bytes -- and therefore its sha256 -- differ, with capture times patched
in place via `exif.datetime_offsets` (never a real file under ~/Pictures).
"""

from __future__ import annotations

import json
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

    def test_import_qualifies_film_roll_registered_when_imported_is_zero(self) -> None:
        """Fix round 1, should-fix 7: a bridge call that returns cleanly but
        reports 0 images imported did not actually register anything."""
        source = self.tmp / "card"
        _make_card(source, 2)
        self.bridge.responses["import_batch"] = {
            "imported": 0,
            "source_path": "",
            "recursive": False,
            "recursive_honoured": True,
            "note": None,
            "directories_imported": 1,
            "scan_incomplete": False,
        }
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertFalse(result.film_roll_registered)
        self.assertTrue(any("0 images imported" in w for w in result.warnings))

    def test_register_success_reports_imported_and_scan_incomplete(self) -> None:
        """Fix round 1, should-fix 7: the success path, not just the
        darktable-down path -- imported>0 and scan_incomplete=True both
        travel through to `RegisterResult` untouched."""
        source = self.tmp / "card"
        _make_card(source, 2)
        library.import_card(str(source), "harbour-wedding", confirm=True)

        self.bridge.responses["import_batch"] = {
            "imported": 2,
            "source_path": "",
            "recursive": False,
            "recursive_honoured": True,
            "note": None,
            "directories_imported": 1,
            "scan_incomplete": True,
        }
        result = library.register("harbour-wedding", confirm=True)
        self.assertTrue(result.film_roll_registered)
        self.assertEqual(result.imported, 2)
        self.assertTrue(result.scan_incomplete)

    def test_register_qualifies_film_roll_registered_when_imported_is_zero(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        library.import_card(str(source), "harbour-wedding", confirm=True)

        result = library.register("harbour-wedding", confirm=True)  # FakeBridge default: imported=0
        self.assertFalse(result.film_roll_registered)


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


class ManifestMergeTest(LibraryTestCase):
    """Fix round 1, blocker 1: the reviewer's own probe."""

    def test_manifest_merges_across_reruns_after_source_files_are_removed(self) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 4)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(len(result1.files), 4)

        # Remove 2 of the 4 from the card -- never touches the copies already
        # sitting in raw/.
        files[2].unlink()
        files[3].unlink()

        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(len(result2.files), 4)  # never shrinks

        job_path = Path(result2.path)
        checksum_lines = (job_path / ".scouter" / "checksums.sha256").read_text().splitlines()
        self.assertEqual(len(checksum_lines), 4)
        manifest = json.loads((job_path / ".scouter" / "import.json").read_text())
        self.assertEqual(len(manifest["files"]), 4)

        vr = library.verify("harbour-wedding")
        self.assertTrue(vr.ok)
        self.assertEqual(vr.checked, 4)

    def test_manifest_merge_refuses_a_differing_hash_for_an_existing_name(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        checksums_path = job_path / ".scouter" / "checksums.sha256"
        import_json_path = job_path / ".scouter" / "import.json"

        # Hand-corrupt the manifest so it disagrees with the real file on
        # disk for an existing destination name: this must be a typed error,
        # never a silent overwrite.
        manifest = json.loads(import_json_path.read_text())
        manifest["files"][0]["sha256"] = "0" * 64
        import_json_path.write_text(json.dumps(manifest, indent=2))

        with self.assertRaises(ChecksumMismatch):
            library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertTrue(checksums_path.is_file())  # unaffected: raised in _plan-derived merge


class StableSequenceTest(LibraryTestCase):
    """Fix round 1, blocker 2: the reviewer's own probe."""

    def test_removing_a_frame_does_not_rename_or_collide_on_rerun(self) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 3)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        dest_by_source = {f["source_name"]: f["dest"] for f in result1.files}

        files[0].unlink()  # remove the earliest-captured frame from the card

        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result2.totals["copied"], 0)  # 0 new files
        self.assertEqual(result2.totals["skipped_identical"], 2)
        for entry in result2.files:
            if entry["source_name"] in dest_by_source:
                self.assertEqual(entry["dest"], dest_by_source[entry["source_name"]])
        self.assertEqual(len(result2.files), 3)  # the removed frame's entry persists

    def test_a_second_card_never_collides_with_the_first(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        seqs1 = sorted(f["seq"] for f in result1.files)
        self.assertEqual(seqs1, [1, 2])

        second = self.tmp / "card2"
        base = datetime(2026, 6, 28, 15, 50, 0)
        _write_one(second, "Q0001.ORF", base.strftime("%Y:%m:%d %H:%M:%S"))
        result2 = library.import_card(str(second), "harbour-wedding", confirm=True)
        self.assertEqual(result2.totals["copied"], 1)
        new_entry = next(f for f in result2.files if f["source_name"] == "Q0001.ORF")
        self.assertEqual(new_entry["seq"], 3)  # continues past the first card's max
        self.assertEqual(len(result2.files), 3)


class DuplicateStemTest(LibraryTestCase):
    def test_duplicate_stem_ext_across_subdirectories_is_a_typed_usage_error(self) -> None:
        source = self.tmp / "card"
        _write_one(source / "100OLYMP", "P6281348.ORF", "2026:06:28 15:49:28")
        _write_one(source / "101OLYMP", "P6281348.ORF", "2026:06:28 15:49:30")

        with self.assertRaises(UsageError) as ctx:
            library.import_card(str(source), "harbour-wedding", confirm=True)
        sources = ctx.exception.detail.get("sources", [])
        self.assertEqual(len(sources), 2)
        self.assertIn("100OLYMP", sources[0] + sources[1])
        self.assertIn("101OLYMP", sources[0] + sources[1])


class MissingSourceTest(LibraryTestCase):
    def test_missing_source_is_a_usage_error_before_scanning(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            library.import_card(str(self.tmp / "no-such-mount"), "harbour-wedding", confirm=True)
        self.assertIn("source", ctx.exception.detail)
        self.assertTrue(ctx.exception.fix)


class VerifyNoManifestTest(LibraryTestCase):
    def test_verify_refuses_when_there_is_no_manifest(self) -> None:
        job_path = self._job_path()
        (job_path / "raw").mkdir(parents=True)
        with self.assertRaises(UsageError):
            library.verify("harbour-wedding")


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
