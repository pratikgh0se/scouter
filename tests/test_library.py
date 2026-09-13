"""Tests for `scouter.core.library`: scan, plan, copy+checksum, verify.

Every card is synthetic: `tests/fixtures/exif-orf-header.bin` (the same real
E-M10 Mark III header `test_exif.py` uses) plus `os.urandom` padding so each
file's bytes -- and therefore its sha256 -- differ, with capture times patched
in place via `exif.datetime_offsets` (never a real file under ~/Pictures).
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter import config
from scouter.core import dtbridge, exif, library
from scouter.core.library import ManifestCorrupt, SourceStemCollision
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
        # refuse rather than silently overwrite. This is a reused STEM (fix
        # round 2, item 4), so the typed error is `SourceStemCollision`, not
        # the generic `DestinationCollision`.
        files[0].write_bytes(files[0].read_bytes() + b"more-bytes-now")
        with self.assertRaises(SourceStemCollision):
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


class ManifestRecoveryTest(LibraryTestCase):
    """Fix round 2, item 1: `import.json` missing/unparseable/malformed."""

    def test_missing_import_json_recovers_seq_from_checksums(self) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 3)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        (job_path / ".scouter" / "import.json").unlink()

        # A NEW card for the re-run (the old frames are not rescanned -- once
        # `import.json` is gone, the original camera filename that would
        # stem-match them is gone with it; see `_synthesize_from_checksums`'s
        # documented limitation). This is also the realistic case: a lost
        # manifest discovered when the NEXT card is imported.
        for old in files:
            old.unlink()
        _write_one(source, "P6281003.ORF", "2026:06:28 15:49:31")
        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)

        self.assertEqual(result2.totals["copied"], 1)  # only the new frame
        new_entry = next(f for f in result2.files if f["source_name"] == "P6281003.ORF")
        self.assertEqual(new_entry["seq"], 4)  # never restarts at 1
        self.assertEqual(len(result2.files), 4)  # the recovered 3 + the new one
        vr = library.verify("harbour-wedding")
        self.assertTrue(vr.ok)

    def test_missing_both_manifest_files_recovers_seq_from_raw_dir(self) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        (job_path / ".scouter" / "import.json").unlink()
        (job_path / ".scouter" / "checksums.sha256").unlink()

        for old in files:
            old.unlink()
        _write_one(source, "P6281002.ORF", "2026:06:28 15:49:30")
        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)

        self.assertEqual(result2.totals["copied"], 1)
        new_entry = next(f for f in result2.files if f["source_name"] == "P6281002.ORF")
        self.assertEqual(new_entry["seq"], 3)
        self.assertEqual(len(result2.files), 3)  # the 2 recovered from raw/ + the new one

    def test_unparseable_import_json_is_manifest_corrupt(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        import_json = Path(result1.path) / ".scouter" / "import.json"
        import_json.write_text("{not valid json", encoding="utf-8")

        with self.assertRaises(ManifestCorrupt) as ctx:
            library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(ctx.exception.detail["path"], str(import_json))
        self.assertTrue(ctx.exception.fix)

    def test_malformed_import_json_is_manifest_corrupt(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        import_json = Path(result1.path) / ".scouter" / "import.json"
        # Valid JSON, wrong shape: a `files[]` entry missing required keys.
        import_json.write_text(json.dumps({"files": [{"dest": "raw/x.orf"}]}), encoding="utf-8")

        with self.assertRaises(ManifestCorrupt):
            library.import_card(str(source), "harbour-wedding", confirm=True)


class AtomicManifestPairTest(LibraryTestCase):
    """Fix round 2, item 2: checksums.sha256 newer than import.json (a torn
    write) is the authority for `seq` recovery."""

    def test_checksums_newer_than_import_json_is_authoritative_for_seq(self) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        import_json = job_path / ".scouter" / "import.json"

        # Simulate a crash between the two atomic replaces: roll import.json
        # back to look like an EARLIER state (as if the checksums.sha256
        # replace for the 2nd frame landed but import.json's never did), then
        # make sure its mtime is older than checksums.sha256's.
        manifest = json.loads(import_json.read_text())
        stale_files = [f for f in manifest["files"] if f["seq"] != 2]
        manifest["files"] = stale_files
        import_json.write_text(json.dumps(manifest, indent=2))
        os.utime(import_json, (time.time() - 10, time.time() - 10))

        # A NEW card for the re-run: the frame recovered from checksums.sha256
        # (seq 2) has a degraded `source_name` (see `_synthesize_from_checksums`),
        # so it would not stem-match if rescanned -- keep the old files off
        # the card, exactly like `test_missing_import_json_recovers_seq_from_checksums`.
        for old in files:
            old.unlink()
        _write_one(source, "P6281002.ORF", "2026:06:28 15:49:31")
        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        new_entry = next(f for f in result2.files if f["source_name"] == "P6281002.ORF")
        # Had seq recovery trusted the rolled-back import.json (max seq 1), the
        # new frame would have collided with seq 2; checksums.sha256 (never
        # rolled back) keeps it at 3.
        self.assertEqual(new_entry["seq"], 3)


class PartialManifestOnErrorTest(LibraryTestCase):
    """Fix round 1, should-fix 5 + fix round 2, items 2-3."""

    def test_keyboard_interrupt_mid_loop_still_persists_what_landed(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        real_copy_verified = library._copy_verified
        calls = {"n": 0}

        def flaky_copy(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real_copy_verified(src, dst)

        with mock.patch.object(library, "_copy_verified", side_effect=flaky_copy):
            with self.assertRaises(KeyboardInterrupt):
                library.import_card(str(source), "harbour-wedding", confirm=True)

        job_path = self._job_path()
        manifest = json.loads((job_path / ".scouter" / "import.json").read_text())
        self.assertEqual(len(manifest["files"]), 1)  # the one file that landed
        checksum_lines = (job_path / ".scouter" / "checksums.sha256").read_text().splitlines()
        self.assertEqual(len(checksum_lines), 1)

    def test_a_persist_failure_in_the_error_path_does_not_mask_the_original_error(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        with mock.patch.object(library, "_persist_manifest", side_effect=RuntimeError("disk full")):
            real_copy_verified = library._copy_verified
            calls = {"n": 0}

            def flaky_copy(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise ChecksumMismatch("boom", detail={})
                return real_copy_verified(src, dst)

            with mock.patch.object(library, "_copy_verified", side_effect=flaky_copy):
                with self.assertRaises(ChecksumMismatch) as ctx:
                    library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertFalse(ctx.exception.detail["manifest_written"])
        self.assertIn("disk full", ctx.exception.detail["manifest_persist_error"])

    def test_a_keyboard_interrupt_during_the_persist_itself_does_not_mask_the_original(
        self,
    ) -> None:
        """Fix round 3, should-fix 3: the inner guard catches BaseException,
        not just Exception -- a SECOND Ctrl-C landing during the recovery
        write is recorded, not left to propagate in place of the original."""
        source = self.tmp / "card"
        _make_card(source, 3)
        with mock.patch.object(library, "_persist_manifest", side_effect=KeyboardInterrupt):
            real_copy_verified = library._copy_verified
            calls = {"n": 0}

            def flaky_copy(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise ChecksumMismatch("boom", detail={})
                return real_copy_verified(src, dst)

            with mock.patch.object(library, "_copy_verified", side_effect=flaky_copy):
                with self.assertRaises(ChecksumMismatch) as ctx:
                    library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertFalse(ctx.exception.detail["manifest_written"])
        self.assertIn("KeyboardInterrupt", ctx.exception.detail["manifest_persist_error"])


class OverlappingStemTest(LibraryTestCase):
    """Fix round 2, item 4: a second card reusing a source stem."""

    def test_overlapping_stem_different_content_is_a_typed_error(self) -> None:
        first = self.tmp / "card"
        _write_one(first, "P0001.ORF", "2026:06:28 15:49:28")
        library.import_card(str(first), "harbour-wedding", confirm=True)

        second = self.tmp / "card2"
        _write_one(second, "P0001.ORF", "2026:06:28 15:50:00")  # same stem, different bytes
        with self.assertRaises(SourceStemCollision) as ctx:
            library.import_card(str(second), "harbour-wedding", confirm=True)
        self.assertEqual(ctx.exception.detail["stem"], "P0001")
        self.assertTrue(ctx.exception.fix)

    def test_overlapping_stem_identical_content_is_skipped(self) -> None:
        first = self.tmp / "card"
        tail = os.urandom(2048)
        _write_one(first, "P0001.ORF", "2026:06:28 15:49:28", tail=tail)
        result1 = library.import_card(str(first), "harbour-wedding", confirm=True)

        second = self.tmp / "card2"
        _write_one(second, "P0001.ORF", "2026:06:28 15:49:28", tail=tail)  # identical bytes
        result2 = library.import_card(str(second), "harbour-wedding", confirm=True)
        self.assertEqual(result2.totals["copied"], 0)
        self.assertEqual(result2.totals["skipped_identical"], 1)
        self.assertEqual(len(result2.files), 1)
        self.assertEqual(result1.files[0]["dest"], result2.files[0]["dest"])

    def test_a_foreign_file_occupying_a_brand_new_name_is_still_destination_collision(
        self,
    ) -> None:
        """A genuinely NEW stem (never imported before) computing a name that
        happens to be occupied by unrelated content is still the generic
        `DestinationCollision` -- `SourceStemCollision` is specifically for a
        REUSED stem, not any occupied name."""
        source = self.tmp / "card"
        _write_one(source, "P0001.ORF", "2026:06:28 15:49:28")
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        foreign_name = result1.files[0]["dest"].replace("_0001.", "_0002.")
        (job_path / foreign_name).write_bytes(b"not one of scouter's own copies")

        _write_one(source, "P0002.ORF", "2026:06:28 15:49:30")  # a genuinely new stem
        with self.assertRaises(DestinationCollision):
            library.import_card(str(source), "harbour-wedding", confirm=True)


class MidnightCardTest(LibraryTestCase):
    """Fix round 2, item 5: the job date is fixed at first import."""

    def test_job_date_is_fixed_and_reused_across_reruns(self) -> None:
        source = self.tmp / "card"
        early = _write_one(source, "P0001.ORF", "2026:06:28 23:58:00")
        _write_one(source, "P0002.ORF", "2026:06:29 00:03:00")
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result1.date, "2026-06-28")
        manifest = json.loads((Path(result1.path) / ".scouter" / "import.json").read_text())
        self.assertEqual(manifest["job_date"], "2026-06-28")

        # Remove the earliest frame: if the date were re-derived instead of
        # reused, this re-run would compute "2026-06-29" and create a SECOND
        # job directory.
        early.unlink()
        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result2.date, "2026-06-28")
        self.assertEqual(result2.path, result1.path)
        drifted_path = self._job_path("2026-06-29").parent / "2026-06-29 harbour-wedding"
        self.assertFalse(drifted_path.exists())

    def test_explicit_date_differing_from_existing_job_is_refused(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)

        with self.assertRaises(UsageError) as ctx:
            library.import_card(str(source), "harbour-wedding", date="2026-07-01", confirm=True)
        self.assertEqual(ctx.exception.detail["existing_date"], "2026-06-28")
        self.assertEqual(ctx.exception.detail["requested_date"], "2026-07-01")
        self.assertIn(result1.path, ctx.exception.detail["existing_path"])
        self.assertTrue(ctx.exception.fix)

    def test_explicit_date_matching_existing_job_is_fine(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        result2 = library.import_card(
            str(source), "harbour-wedding", date="2026-06-28", confirm=True
        )
        self.assertEqual(result2.path, result1.path)


class ManifestWinsMergeTest(LibraryTestCase):
    """Fix round 3 blocker: the reviewer's own falsifiable probe -- a re-run
    after a torn write, with the card still inserted, must not duplicate it."""

    def test_torn_write_with_card_present_does_not_duplicate_the_card(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        import_json = job_path / ".scouter" / "import.json"

        # Roll import.json back one entry (as if checksums.sha256's replace
        # for the 2nd frame landed but import.json's own replace never did),
        # with an OLDER mtime than checksums.sha256's -- the exact "torn
        # write" shape `_recover_existing_files` is built to detect.
        manifest = json.loads(import_json.read_text())
        manifest["files"] = [f for f in manifest["files"] if f["seq"] != 2]
        import_json.write_text(json.dumps(manifest, indent=2))
        os.utime(import_json, (time.time() - 10, time.time() - 10))

        # The card is UNCHANGED -- both original frames are still there.
        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result2.totals["copied"], 0)
        self.assertEqual(result2.totals["skipped_identical"], 2)
        self.assertEqual(len(result2.files), 2)  # never duplicated
        vr = library.verify("harbour-wedding")
        self.assertTrue(vr.ok)
        self.assertEqual(vr.checked, 2)

    def test_recovered_entries_never_overwrite_good_parsed_entries(self) -> None:
        """Narrower unit-level check of the same blocker: `_fill_manifest_gaps`
        itself, independent of the full import path above."""
        good = [{"dest": "raw/a.orf", "sha256": "aaaa", "seq": 1, "source_name": "P1.ORF"}]
        degraded = [
            {
                "dest": "raw/a.orf",
                "sha256": "aaaa",
                "seq": 1,
                "source_name": "raw/a.orf",  # what a recovered entry looks like
            },
            {"dest": "raw/b.orf", "sha256": "bbbb", "seq": 2, "source_name": "raw/b.orf"},
        ]
        merged = library._fill_manifest_gaps(good, degraded)
        by_dest = {f["dest"]: f for f in merged}
        self.assertEqual(by_dest["raw/a.orf"]["source_name"], "P1.ORF")  # good entry kept
        self.assertEqual(by_dest["raw/b.orf"]["source_name"], "raw/b.orf")  # gap filled


class MissingRawFileTest(LibraryTestCase):
    """Fix round 3, should-fix 2."""

    def test_missing_raw_file_with_matching_source_hash_is_recopied(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        dest0 = job_path / result1.files[0]["dest"]
        dest0.chmod(0o644)
        dest0.unlink()

        result2 = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(result2.totals["copied"], 1)  # the re-copied one
        self.assertEqual(result2.totals["skipped_identical"], 1)
        self.assertTrue(dest0.is_file())
        vr = library.verify("harbour-wedding")
        self.assertTrue(vr.ok)

    def test_missing_raw_file_with_differing_source_hash_is_a_typed_error_before_any_write(
        self,
    ) -> None:
        source = self.tmp / "card"
        files = _make_card(source, 2)
        result1 = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result1.path)
        dest0 = job_path / result1.files[0]["dest"]
        dest0.chmod(0o644)
        dest0.unlink()

        # The source file at that same name/position now has DIFFERENT bytes.
        files[0].write_bytes(files[0].read_bytes() + b"changed-after-the-fact")
        with self.assertRaises(ChecksumMismatch) as ctx:
            library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertFalse(dest0.exists())  # refused before any write landed
        self.assertIn("expected", ctx.exception.detail)
        self.assertIn("actual", ctx.exception.detail)


class SeqFromDestNameTest(LibraryTestCase):
    """Fix round 3, should-fix 4: parses from the right; the invariant it
    depends on (no `_` in a slug) is pinned with an assertion."""

    def test_parses_seq_from_the_right(self) -> None:
        seq = library._seq_from_dest_name(
            "2026-06-28_harbour-wedding_em10iii_0007.orf", "2026-06-28", "harbour-wedding"
        )
        self.assertEqual(seq, 7)

    def test_non_matching_name_returns_none(self) -> None:
        self.assertIsNone(
            library._seq_from_dest_name("unrelated.orf", "2026-06-28", "harbour-wedding")
        )

    def test_camera_slug_with_underscore_trips_the_invariant_assertion(self) -> None:
        with self.assertRaises(AssertionError):
            library._seq_from_dest_name(
                "2026-06-28_harbour-wedding_em10_iii_0001.orf",
                "2026-06-28",
                "harbour-wedding",
            )


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
