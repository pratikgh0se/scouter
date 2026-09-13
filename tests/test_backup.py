"""Tests for scouter.core.backup: tarfile -> age|openssl -> rsync, and verify.

Real `openssl` (present on this Mac, OpenSSL 3.6.3 at /opt/homebrew/bin/openssl)
is used for the round-trip and verify tests; `age` is not installed here, so
its coverage is the missing-binary degrade to a typed error, never a
traceback (E9's other half).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from scouter import config, metrics
from scouter.core import backup
from scouter.errors import BackupNotConfigured, EncryptionUnavailable

OPENSSL_PRESENT = Path("/opt/homebrew/bin/openssl").exists() or bool(shutil.which("openssl"))


def _build_library(root: Path) -> dict:
    """A tiny job with a raw + xmp, .scouter/, exports/, and scouter/memory/."""
    job_dir = root / "2026" / "2026-06-28 harbour-wedding"
    raw_dir = job_dir / "raw"
    raw_dir.mkdir(parents=True)
    scouter_dir = job_dir / ".scouter"
    scouter_dir.mkdir()

    raw_name = "2026-06-28_harbour-wedding_em10iii_0001.orf"
    raw_file = raw_dir / raw_name
    raw_file.write_bytes(b"FAKE-RAW-BYTES-0001")
    raw_file.chmod(0o444)
    xmp_file = raw_dir / f"{raw_name}.xmp"
    xmp_file.write_text("<x:xmpmeta/>\n", encoding="utf-8")

    raw_sha = hashlib.sha256(raw_file.read_bytes()).hexdigest()
    import_json = {
        "version": 1,
        "job_slug": "harbour-wedding",
        "date": "2026-06-28",
        "files": [
            {
                "seq": 1,
                "dest": f"raw/{raw_name}",
                "source_name": "P6281348.ORF",
                "bytes": raw_file.stat().st_size,
                "sha256": raw_sha,
                "status": "copied",
            }
        ],
        "totals": {
            "planned": 1,
            "copied": 1,
            "skipped_identical": 0,
            "bytes": raw_file.stat().st_size,
        },
    }
    (scouter_dir / "import.json").write_text(json.dumps(import_json), encoding="utf-8")
    (scouter_dir / "checksums.sha256").write_text(f"{raw_sha}  raw/{raw_name}\n", encoding="utf-8")

    exports_dir = job_dir / "exports" / "client-final"
    exports_dir.mkdir(parents=True)
    (exports_dir / "out.jpg").write_bytes(b"JPEGDATA-MUST-NOT-BE-BACKED-UP")

    memory_dir = root / "scouter" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / ".keep").write_text("", encoding="utf-8")

    return {
        "job_dir": job_dir,
        "raw_file": raw_file,
        "xmp_file": xmp_file,
        "raw_sha": raw_sha,
        "raw_name": raw_name,
    }


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-backup-test-")
        self.tmp = Path(self._tmpdir.name)
        self._env = dict(os.environ)
        self._orig_path = os.environ.get("PATH", "")

        self.library_root = self.tmp / "library"
        self.library_root.mkdir()
        self.fixture = _build_library(self.library_root)

        self.cfg_dir = self.tmp / "cfgdir"
        self.cfg_dir.mkdir()
        (self.cfg_dir / "wb").mkdir()
        (self.cfg_dir / "wb" / "table.json").write_text("{}", encoding="utf-8")
        self.passfile = self.cfg_dir / "backup.pass"
        self.passfile.write_text("test-passphrase\n", encoding="utf-8")
        self.passfile.chmod(0o600)

        self.target = self.tmp / "target"
        self.target.mkdir()

        self.cache_dir = self.tmp / "cache"
        self.cache_dir.mkdir()

        self.config_path = self.cfg_dir / "config.json"

        os.environ["SCOUTER_CONFIG"] = str(self.config_path)
        os.environ["SCOUTER_CACHE"] = str(self.cache_dir)
        os.environ["SCOUTER_JOB"] = "job-test"
        os.environ.pop("SCOUTER_DARKTABLE_LIBRARY_DB", None)

        # A real (schema-valid) metrics.db and a real events.jsonl line, so
        # the member scan finds files that are actually openable.
        metrics._connect().close()
        with metrics.paths.events_path().open("a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-06-28T00:00:00Z"}\n')

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

    def _write_config(
        self,
        *,
        enabled: bool = True,
        encryption: str = "openssl",
        rsync_raws: bool = True,
        keep_archives: int = 14,
    ) -> config.Config:
        payload = {
            "version": 1,
            "library_root": str(self.library_root),
            "backup": {
                "enabled": enabled,
                "target": str(self.target),
                "rsync_raws": rsync_raws,
                "encryption": encryption,
                "openssl_passfile": str(self.passfile),
                "keep_archives": keep_archives,
            },
        }
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        return config.load(str(self.config_path))

    def _prepend_fake_bin(self, name: str, body: str) -> Path:
        bindir = self.tmp / "fakebin"
        bindir.mkdir(exist_ok=True)
        script = bindir / name
        script.write_text(body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{self._orig_path}"
        return script


class MemberListTest(BackupTestCase):
    def test_member_list_is_sorted_and_covers_the_set(self) -> None:
        cfg = self._write_config()
        members = backup._members(cfg)
        names = [name for _, name in members]

        self.assertEqual(names, sorted(names))
        self.assertTrue(any(n.endswith(".orf.xmp") for n in names), names)
        self.assertTrue(any(n.endswith(".scouter/import.json") for n in names), names)
        self.assertTrue(any(n.endswith(".scouter/checksums.sha256") for n in names), names)
        self.assertTrue(
            any(n.startswith("config/") and n.endswith("config.json") for n in names), names
        )
        self.assertTrue(any(n.startswith("config/wb/") for n in names), names)
        self.assertIn("cache/metrics.db", names)
        self.assertIn("cache/events.jsonl", names)
        self.assertTrue(any("scouter/memory/" in n for n in names), names)

    def test_exports_and_raws_not_in_the_archive(self) -> None:
        cfg = self._write_config()
        members = backup._members(cfg)
        names = [name for _, name in members]

        self.assertFalse(any(n.endswith(".orf") for n in names), names)
        self.assertFalse(any("/exports/" in n for n in names), names)


class TarAndEncryptTest(BackupTestCase):
    @unittest.skipUnless(OPENSSL_PRESENT, "openssl not found")
    def test_plaintext_hash_round_trips(self) -> None:
        cfg = self._write_config(encryption="openssl")
        members = backup._members(cfg)
        plain = self.tmp / "scratch.tar.gz"
        plaintext_sha256, plaintext_bytes, count = backup._write_archive(members, plain)
        self.assertGreater(count, 0)
        self.assertEqual(plaintext_bytes, plain.stat().st_size)

        encrypted, args = backup._encrypt(plain, cfg)
        self.assertTrue(encrypted.is_file())
        self.assertNotEqual(encrypted, plain)

        decrypted = self.tmp / "roundtrip.tar.gz"
        backup._decrypt(encrypted, cfg, decrypted)
        self.assertEqual(backup._sha256_file(decrypted), plaintext_sha256)

    @unittest.skipUnless(OPENSSL_PRESENT, "openssl not found")
    def test_passfile_not_in_argv(self) -> None:
        cfg = self._write_config(encryption="openssl")
        plain = self.tmp / "scratch2.tar.gz"
        backup._write_archive(backup._members(cfg), plain)
        _out, args = backup._encrypt(plain, cfg)

        self.assertNotIn("test-passphrase", args)
        self.assertTrue(any(a == f"file:{self.passfile}" for a in args), args)

    def test_age_missing_binary_degrades_to_typed_error(self) -> None:
        """age is NOT installed on this Mac: this is the missing-binary path
        for the age encryption case, and it must be typed, never a traceback."""
        from dataclasses import replace

        cfg = self._write_config(encryption="openssl")
        cfg = replace(
            cfg,
            backup=replace(cfg.backup, encryption="age", age_recipient="age1exampleexampleexample"),
        )
        plain = self.tmp / "scratch3.tar.gz"
        backup._write_archive(backup._members(cfg), plain)
        with self.assertRaises(EncryptionUnavailable) as ctx:
            backup._encrypt(plain, cfg)
        self.assertEqual(ctx.exception.detail["check"], "age_missing")


class PruningTest(BackupTestCase):
    def test_pruning_never_removes_the_newest_two(self) -> None:
        archives = self.target / "archives"
        archives.mkdir()
        names = [
            "2026-09-10T030000Z",
            "2026-09-11T030000Z",
            "2026-09-12T030000Z",
            "2026-09-13T030000Z",
            "2026-09-14T030000Z",
        ]
        for name in names:
            (archives / name).mkdir()

        removed = backup._prune_archives(self.target, keep_archives=1)

        self.assertEqual(len(removed), 3)
        remaining = sorted(p.name for p in archives.iterdir())
        self.assertEqual(remaining, names[-2:])


class RunGateTest(BackupTestCase):
    def test_gate_refusal_before_any_work(self) -> None:
        self._write_config(enabled=False)
        with self.assertRaises(BackupNotConfigured):
            backup.run(confirm=True)
        self.assertFalse((self.target / "archives").exists())
        self.assertFalse((self.target / "library").exists())

    def test_dry_run_writes_nothing(self) -> None:
        self._write_config()
        result = backup.run(dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertFalse((self.target / "archives").exists())
        self.assertFalse((self.target / "library").exists())

    def test_dry_run_without_confirm_also_writes_nothing(self) -> None:
        self._write_config()
        result = backup.run(confirm=False)
        self.assertEqual(result.outcome, "dry_run")
        self.assertFalse((self.target / "archives").exists())


@unittest.skipUnless(OPENSSL_PRESENT, "openssl not found")
class RunAndVerifyTest(BackupTestCase):
    RSYNC_SUCCESS_SCRIPT = (
        "#!/usr/bin/env python3\n"
        "import shutil, sys, pathlib\n"
        "src, dest = sys.argv[-2], sys.argv[-1]\n"
        "pathlib.Path(dest).mkdir(parents=True, exist_ok=True)\n"
        "shutil.copytree(src, dest, dirs_exist_ok=True)\n"
        'print("Number of files transferred: 1")\n'
        'print("Total transferred file size: 20 bytes")\n'
    )

    def test_run_produces_archive_and_verify_passes_named_checks(self) -> None:
        self._write_config(rsync_raws=True)
        self._prepend_fake_bin("rsync", self.RSYNC_SUCCESS_SCRIPT)

        result = backup.run(confirm=True)
        self.assertEqual(result.outcome, "ok")
        self.assertTrue(Path(result.manifest_path).is_file())
        self.assertGreater(result.members, 0)

        vresult = backup.verify()
        self.assertEqual([c["name"] for c in vresult.checks], list(backup.CHECK_NAMES))

    def test_verify_fails_on_one_flipped_byte(self) -> None:
        self._write_config(rsync_raws=False)
        result = backup.run(confirm=True)
        manifest = json.loads(Path(result.manifest_path).read_text())
        archive_dir = Path(result.manifest_path).parent
        ciphertext_path = archive_dir / manifest["archive"]["name"]

        data = bytearray(ciphertext_path.read_bytes())
        data[0] ^= 0xFF
        ciphertext_path.write_bytes(bytes(data))

        vresult = backup.verify()
        self.assertFalse(vresult.verified)
        failing = [c["name"] for c in vresult.checks if not c["ok"]]
        self.assertIn("ciphertext_sha256", failing)

    def test_verify_names_every_check(self) -> None:
        self._write_config(rsync_raws=False)
        result = backup.run(confirm=True)
        vresult = backup.verify(archive=Path(result.archive_dir).name)
        self.assertEqual([c["name"] for c in vresult.checks], list(backup.CHECK_NAMES))
        for c in vresult.checks:
            self.assertIn("ok", c)
            self.assertIn("detail", c)

    def test_rsync_failure_is_a_warning_not_a_failure(self) -> None:
        self._write_config(rsync_raws=True)
        self._prepend_fake_bin(
            "rsync",
            "#!/bin/sh\necho 'rsync error: some files could not be transferred' >&2\nexit 23\n",
        )
        result = backup.run(confirm=True)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.rsync_exit, 23)
        self.assertTrue(any("23" in w for w in result.warnings), result.warnings)


if __name__ == "__main__":
    unittest.main()
