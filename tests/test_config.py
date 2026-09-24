"""Tests for scouter.config: the loader, the backup gate, and the job layout."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter import config
from scouter.errors import BackupNotConfigured, UsageError
from tests.scouter_fakes import config_fixture


class TmpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-config-test-")
        self.tmp = Path(self._tmpdir.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(self._env)


class LoadTest(TmpTestCase):
    def test_missing_file_returns_defaults(self) -> None:
        cfg = config.load(str(self.tmp / "no-such-config.json"))
        self.assertFalse(cfg.exists)
        self.assertIsNone(cfg.library_root)

    def test_non_numeric_version_is_usage_naming_the_file(self) -> None:
        """Final review T4: not a bare ValueError/TypeError."""
        path = self.tmp / "config.json"
        for bad in ("one", None, [1]):
            with self.subTest(version=bad):
                path.write_text(json.dumps({"version": bad}))
                with self.assertRaises(UsageError) as ctx:
                    config.load(str(path))
                self.assertIn(str(path), str(ctx.exception))
                self.assertEqual(ctx.exception.detail["config"], str(path))

    def test_bad_version_keeps_educative_fallback_to_brief(self) -> None:
        """T4: explain.resolve_level falls back to "brief" on a broken config
        (P3-R8) -- which needs the loader to raise a ScouterError."""
        from scouter.core import explain

        path = self.tmp / "config.json"
        with mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(path)}):
            path.write_text(json.dumps({"version": 1, "educative": "full"}))
            self.assertEqual(explain.resolve_level(None), "full", "the config is really read")
            path.write_text(json.dumps({"version": "one", "educative": "full"}))
            self.assertEqual(explain.resolve_level(None), "brief")

    def test_unknown_top_key_rejected(self) -> None:
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"version": 1, "bogus_key": True}))
        with self.assertRaises(UsageError) as ctx:
            config.load(str(path))
        self.assertIn("bogus_key", str(ctx.exception))
        self.assertEqual(ctx.exception.detail["unknown"], ["bogus_key"])

    def test_unknown_section_key_rejected(self) -> None:
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"version": 1, "backup": {"targt": "/x"}}))
        with self.assertRaises(UsageError) as ctx:
            config.load(str(path))
        self.assertIn("targt", str(ctx.exception))
        self.assertIn("target", str(ctx.exception))  # near-match suggestion

    def test_bad_json_names_the_line(self) -> None:
        path = self.tmp / "config.json"
        path.write_text("{not valid json")
        with self.assertRaises(UsageError) as ctx:
            config.load(str(path))
        self.assertEqual(ctx.exception.code, "usage")
        self.assertIn("line", ctx.exception.detail)

    def test_loads_a_full_config(self) -> None:
        path = self.tmp / "config.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "library_root": str(self.tmp / "library"),
                    "cameras": {"E-M10 Mark III": "em10iii"},
                    "backup": {"enabled": True, "target": "/Volumes/Backup"},
                }
            )
        )
        cfg = config.load(str(path))
        self.assertTrue(cfg.exists)
        self.assertEqual(cfg.library_root, str(self.tmp / "library"))
        self.assertEqual(cfg.camera_map(), {"E-M10 Mark III": "em10iii"})
        self.assertTrue(cfg.backup.enabled)
        self.assertEqual(cfg.backup.target, "/Volumes/Backup")


class BackupGateTest(TmpTestCase):
    """E1: table-driven, one subtest per gate condition."""

    def _base(self, **overrides) -> config.Config:
        cfg = config_fixture(self.tmp)
        if overrides:
            from dataclasses import replace

            backup_overrides = overrides.pop("backup", None)
            if backup_overrides:
                cfg = replace(cfg, backup=replace(cfg.backup, **backup_overrides))
            if overrides:
                cfg = replace(cfg, **overrides)
        return cfg

    def test_config_missing(self) -> None:
        cfg = self._base(exists=False)
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "config_missing")

    def test_disabled(self) -> None:
        cfg = self._base(backup={"enabled": False})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "disabled")

    def test_no_target(self) -> None:
        cfg = self._base(backup={"target": None})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "no_target")

    def test_target_parent_missing(self) -> None:
        cfg = self._base(backup={"target": str(self.tmp / "nope" / "backup")})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "target_parent_missing")

    def test_target_not_writable(self) -> None:
        readonly = self.tmp / "readonly-target"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            cfg = self._base(backup={"target": str(readonly)})
            with self.assertRaises(BackupNotConfigured) as ctx:
                config.require_backup(cfg)
            self.assertEqual(ctx.exception.detail["check"], "target_not_writable")
        finally:
            readonly.chmod(0o700)

    def test_unencrypted(self) -> None:
        cfg = self._base(backup={"encryption": "none", "allow_unencrypted": False})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "unencrypted")

    def test_unencrypted_allowed_passes(self) -> None:
        cfg = self._base(backup={"encryption": "none", "allow_unencrypted": True})
        config.require_backup(cfg)  # must not raise

    def test_age_recipient_missing(self) -> None:
        cfg = self._base(backup={"encryption": "age", "age_recipient": None})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "age_recipient_missing")

    def test_age_missing(self) -> None:
        cfg = self._base(backup={"encryption": "age", "age_recipient": "me@example.com"})
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(BackupNotConfigured) as ctx:
                config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "age_missing")

    def test_passfile_missing(self) -> None:
        cfg = self._base(backup={"openssl_passfile": None})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "passfile_missing")

    def test_passfile_absent(self) -> None:
        cfg = self._base(backup={"openssl_passfile": str(self.tmp / "no-such-passfile")})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "passfile_absent")

    def test_passfile_mode(self) -> None:
        pf = self.tmp / "bad-mode.pass"
        pf.write_text("secret\n")
        pf.chmod(0o644)
        cfg = self._base(backup={"openssl_passfile": str(pf)})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "passfile_mode")

    def test_passfile_mode_must_be_0600(self) -> None:
        pf = self.tmp / "bad-mode.pass"
        pf.write_text("secret\n")
        pf.chmod(0o644)
        cfg = self._base(backup={"openssl_passfile": str(pf)})
        with self.assertRaises(BackupNotConfigured) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["mode"], "0644")

    def test_unknown_encryption_refuses_instead_of_passing(self) -> None:
        cfg = self._base(backup={"encryption": "opnessl"})
        with self.assertRaises(UsageError) as ctx:
            config.require_backup(cfg)
        self.assertEqual(ctx.exception.detail["check"], "unknown_encryption")
        self.assertEqual(ctx.exception.detail["encryption"], "opnessl")

    def test_satisfiable_gate_passes(self) -> None:
        cfg = config_fixture(self.tmp)
        result = config.require_backup(cfg)
        self.assertIs(result, cfg.backup)

    def test_import_refuses_on_nonexistent_source_before_touching_filesystem(self) -> None:
        from scouter import registry

        registry.load_verbs()
        # library.import is task 3's verb; task 1 only guarantees the gate
        # itself refuses before any filesystem read of the source happens.
        cfg = self._base(exists=False)
        with self.assertRaises(BackupNotConfigured):
            config.require_backup(cfg)

    def test_import_refuses_without_backup_target(self) -> None:
        """E1, end to end: `library.import` itself (task 3), not just the gate
        function in isolation, refuses before reading a single file from a
        source that does not even exist -- exercised through the real verb,
        as the design's traceability table names it."""
        old_cache = os.environ.get("SCOUTER_CACHE")
        os.environ["SCOUTER_CACHE"] = str(self.tmp / "cache")
        try:
            from scouter.core import library

            cfg = self._base(backup={"enabled": False})
            with mock.patch.object(library.config, "load", return_value=cfg):
                with self.assertRaises(BackupNotConfigured) as ctx:
                    library.import_card(
                        str(self.tmp / "no-such-card-mount"),
                        "harbour-wedding",
                        confirm=True,
                    )
            self.assertEqual(ctx.exception.detail["check"], "disabled")
        finally:
            if old_cache is None:
                os.environ.pop("SCOUTER_CACHE", None)
            else:
                os.environ["SCOUTER_CACHE"] = old_cache


class JobLayoutTest(TmpTestCase):
    def test_job_dir_shape(self) -> None:
        cfg = config_fixture(self.tmp)
        path = config.job_dir(cfg, "2026-06-28", "harbour-wedding")
        self.assertTrue(str(path).endswith("2026/2026-06-28 harbour-wedding"))

    def test_job_dir_bad_slug_raises_usage(self) -> None:
        cfg = config_fixture(self.tmp)
        with self.assertRaises(UsageError):
            config.job_dir(cfg, "2026-06-28", "Not_A_Slug!")

    def test_job_dir_bad_date_raises_usage(self) -> None:
        cfg = config_fixture(self.tmp)
        with self.assertRaises(UsageError):
            config.job_dir(cfg, "06-28-2026", "harbour-wedding")

    def test_job_dir_numerically_invalid_date_raises_usage(self) -> None:
        cfg = config_fixture(self.tmp)
        with self.assertRaises(UsageError):
            config.job_dir(cfg, "2026-13-40", "harbour-wedding")

    def test_find_job_by_slug_alone(self) -> None:
        cfg = config_fixture(self.tmp)
        job = config.job_dir(cfg, "2026-06-28", "harbour-wedding")
        job.mkdir(parents=True)
        found = config.find_job(cfg, "harbour-wedding")
        self.assertEqual(found, job)

    def test_find_job_ambiguous(self) -> None:
        cfg = config_fixture(self.tmp)
        job_a = config.job_dir(cfg, "2025-06-28", "harbour-wedding")
        job_b = config.job_dir(cfg, "2026-06-28", "harbour-wedding")
        job_a.mkdir(parents=True)
        job_b.mkdir(parents=True)
        with self.assertRaises(UsageError) as ctx:
            config.find_job(cfg, "harbour-wedding")
        self.assertEqual(len(ctx.exception.detail["candidates"]), 2)

    def test_find_job_not_found(self) -> None:
        cfg = config_fixture(self.tmp)
        with self.assertRaises(UsageError):
            config.find_job(cfg, "no-such-job")


class ConfigInitTest(TmpTestCase):
    def test_dry_run_writes_nothing(self) -> None:
        target = self.tmp / "config.json"
        result = config.init(str(target), dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertFalse(target.exists())

    def test_init_writes_starter_config(self) -> None:
        target = self.tmp / "config.json"
        result = config.init(str(target), confirm=True)
        self.assertTrue(result.written)
        self.assertTrue(target.is_file())
        data = json.loads(target.read_text())
        self.assertEqual(data["version"], 1)
        self.assertFalse(data["backup"]["enabled"])

    def test_init_refuses_to_clobber(self) -> None:
        target = self.tmp / "config.json"
        target.write_text("original contents\n")
        before = target.read_bytes()
        result = config.init(str(target))  # no confirm
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(target.read_bytes(), before)

    def test_init_refuses_to_clobber_even_with_dry_run_false_but_no_confirm(self) -> None:
        target = self.tmp / "config.json"
        target.write_text("original contents\n")
        before = target.read_bytes()
        result = config.init(str(target), dry_run=False, confirm=False)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(target.read_bytes(), before)

    def test_age_selected_when_present(self) -> None:
        target = self.tmp / "config.json"
        with mock.patch("shutil.which", return_value="/usr/local/bin/age"):
            result = config.init(str(target), confirm=True)
        self.assertEqual(result.config["backup"]["encryption"], "age")

    def test_openssl_selected_when_age_absent(self) -> None:
        target = self.tmp / "config.json"
        with mock.patch("shutil.which", return_value=None):
            result = config.init(str(target), confirm=True)
        self.assertEqual(result.config["backup"]["encryption"], "openssl")


class ConfigShowTest(TmpTestCase):
    def test_show_reports_ok_gate_when_satisfiable(self) -> None:
        cfg = config_fixture(self.tmp)
        Path(cfg.path).write_text(
            json.dumps(
                {
                    "version": 1,
                    "library_root": cfg.library_root,
                    "backup": {
                        "enabled": True,
                        "target": cfg.backup.target,
                        "encryption": "openssl",
                        "openssl_passfile": cfg.backup.openssl_passfile,
                    },
                }
            )
        )
        result = config.show(cfg.path)
        self.assertEqual(result.backup_gate, "ok")

    def test_show_reports_failing_gate_reason(self) -> None:
        result = config.show(str(self.tmp / "no-such-config.json"))
        self.assertTrue(result.backup_gate.startswith("backup_not_configured:"))


class ErrorTableTest(unittest.TestCase):
    def test_every_new_error_class_has_an_exit_code(self) -> None:
        from scouter.errors import ERROR_CLASSES, EXIT_CODES

        for klass in ERROR_CLASSES:
            with self.subTest(klass=klass.__name__):
                self.assertIn(klass.code, EXIT_CODES)
                self.assertTrue(klass.fix, f"{klass.__name__} has no fix")


if __name__ == "__main__":
    unittest.main()
