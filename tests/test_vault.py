"""Tests for `scouter.core.vault`: the AI-first job note, append-never-overwrite,
and `library.jobs` / `library.relocate`, which lean on vault-adjacent layout
helpers (`config.job_dir`, `xmp.rating`).

`$OBSIDIAN_VAULT_PATH` is ALWAYS pointed at a temp dir for these tests -- never
the real vault -- via `_vault_env`, which also restores whatever the real
environment had (this machine's session may have the real vault set).
"""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from scouter.core import dtbridge, library, vault
from scouter.errors import UsageError
from tests.scouter_fakes import FakeBridge, TempCache, config_fixture
from tests.test_library import _make_card


@contextmanager
def _vault_env(path: Path | None) -> Iterator[None]:
    """Point `$OBSIDIAN_VAULT_PATH` at `path` (or unset it) for one block,
    restoring whatever the real environment had -- this session may have the
    real vault configured, and it must never be touched by these tests."""
    saved = os.environ.get("OBSIDIAN_VAULT_PATH")
    if path is None:
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
    else:
        os.environ["OBSIDIAN_VAULT_PATH"] = str(path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        else:
            os.environ["OBSIDIAN_VAULT_PATH"] = saved


def _manifest(
    *,
    job_slug: str = "harbour-wedding",
    date: str = "2026-06-28",
    frames: int = 30,
    total_bytes: int = 432520470,
    camera: str = "em10iii",
    camera_model: str | None = "E-M10 Mark III",
) -> dict:
    return {
        "version": 1,
        "job_slug": job_slug,
        "date": date,
        "camera": camera,
        "camera_model": camera_model,
        "camera_make": "OLYMPUS CORPORATION",
        "imported_at": "2026-09-14T10:22:41.000000Z",
        "scouter_version": "0.2.0",
        "source_root": "/Volumes/OLYMPUS/DCIM",
        "files": [{}] * frames,
        "totals": {
            "planned": frames,
            "copied": frames,
            "skipped_identical": 0,
            "bytes": total_bytes,
        },
    }


class VaultTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.tmp = self.cache.start()
        self.cfg = config_fixture(self.tmp)
        self._cfg_patch = mock.patch.object(library.config, "load", return_value=self.cfg)
        self._cfg_patch.start()
        self.bridge = FakeBridge()
        dtbridge.set_bridge(self.bridge)
        self.vault_dir = self.tmp / "vault"
        self.vault_dir.mkdir()

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self._cfg_patch.stop()
        self.cache.stop()


class NoVaultTest(VaultTestCase):
    def test_no_vault_is_a_warning_not_a_failure(self) -> None:
        with _vault_env(None):
            result = vault.write_job_note(self.cfg, _manifest())
        self.assertEqual(result, {"written": False, "reason": vault._VAULT_ABSENT_REASON})

        source = self.tmp / "card"
        _make_card(source, 2)
        with _vault_env(None):
            import_result = library.import_card(str(source), "harbour-wedding", confirm=True)
        self.assertEqual(import_result.outcome, "ok")
        self.assertTrue(any("vault" in w for w in import_result.warnings))

    def test_missing_vault_directory_also_degrades(self) -> None:
        # Set, but pointing at a path that doesn't exist -- same degradation.
        with _vault_env(self.tmp / "no-such-vault"):
            result = vault.write_job_note(self.cfg, _manifest())
        self.assertFalse(result["written"])


class NewNoteTest(VaultTestCase):
    def test_new_note_has_ai_first_frontmatter_and_preamble(self) -> None:
        with _vault_env(self.vault_dir):
            result = vault.write_job_note(self.cfg, _manifest())
        self.assertTrue(result["written"])
        self.assertEqual(result["mode"], "created")

        note_path = Path(result["path"])
        self.assertEqual(
            note_path,
            self.vault_dir / "Projects" / "Photo jobs" / "2026-06-28 harbour-wedding.md",
        )
        text = note_path.read_text(encoding="utf-8")

        self.assertIn("ai-first: true", text)
        self.assertIn("## For future agent", text)
        self.assertIn("type: photo-job", text)
        self.assertIn('project: "[[Capsule Corp]]"', text)
        self.assertIn("job_slug: harbour-wedding", text)
        self.assertIn("status: imported", text)
        self.assertIn("## Stages", text)
        self.assertIn("- [ ] cull proposed", text)

        # library_path is RELATIVE -- the note must survive an SSD move.
        self.assertIn("library_path: 2026/2026-06-28 harbour-wedding", text)
        self.assertNotIn(str(self.cfg.root()), text)


class AppendMergeTest(VaultTestCase):
    def test_second_import_appends_and_merges(self) -> None:
        with _vault_env(self.vault_dir):
            first = vault.write_job_note(self.cfg, _manifest(frames=30, total_bytes=400))
            note_path = Path(first["path"])
            original_text = note_path.read_text(encoding="utf-8")

            second = vault.write_job_note(self.cfg, _manifest(frames=12, total_bytes=100))

        self.assertEqual(second["mode"], "appended")
        self.assertEqual(second["path"], first["path"])

        merged_text = note_path.read_text(encoding="utf-8")

        # frames/bytes are summed.
        self.assertIn("frames: 42", merged_text)
        self.assertIn("bytes: 500", merged_text)

        # tags stay unioned (both imports produce the same static set here,
        # so this also proves the merge doesn't drop any of them).
        for tag in ("photo-job", "scouter", "darktable", "capsule-corp"):
            self.assertIn(tag, merged_text)

        # the original BODY (everything after the frontmatter block) survives
        # byte for byte -- only the frontmatter block itself is rewritten.
        original_body = original_text.split("---\n", 2)[2]
        self.assertIn(original_body, merged_text)

        # exactly one extra "## Import" section landed.
        self.assertEqual(merged_text.count("## Import "), 2)


class AtomicWriteTest(VaultTestCase):
    def test_crash_mid_write_leaves_previous_note_intact(self) -> None:
        """`os.replace` failing mid-write (a simulated crash / full disk) must
        never leave a half-written note in place of the old one -- the write
        goes through a tmp file, and only a successful `os.replace` swaps it
        in."""
        with _vault_env(self.vault_dir):
            first = vault.write_job_note(self.cfg, _manifest(frames=30, total_bytes=400))
            note_path = Path(first["path"])
            original_text = note_path.read_text(encoding="utf-8")

            with mock.patch("os.replace", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    vault.write_job_note(self.cfg, _manifest(frames=12, total_bytes=100))

        # The note is untouched: the crash happened after the tmp file was
        # written but before it replaced the real note.
        self.assertEqual(note_path.read_text(encoding="utf-8"), original_text)

    def test_crash_mid_append_stage_leaves_previous_note_intact(self) -> None:
        with _vault_env(self.vault_dir):
            created = vault.write_job_note(self.cfg, _manifest())
            note_path = Path(created["path"])
            original_text = note_path.read_text(encoding="utf-8")

            with mock.patch("os.replace", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    vault.append_stage(
                        self.cfg, "harbour-wedding", "Cull", rows=[("proposed", "12")]
                    )

        self.assertEqual(note_path.read_text(encoding="utf-8"), original_text)


class SlugValidationTest(VaultTestCase):
    def test_write_job_note_refuses_a_bad_job_slug(self) -> None:
        with _vault_env(self.vault_dir):
            with self.assertRaises(UsageError):
                vault.write_job_note(self.cfg, _manifest(job_slug="Not A Slug!"))

    def test_append_stage_refuses_a_bad_job_slug(self) -> None:
        with _vault_env(self.vault_dir):
            with self.assertRaises(UsageError):
                vault.append_stage(self.cfg, "Not A Slug!", "Cull", rows=[])


class AppendStageTest(VaultTestCase):
    def test_append_stage_ticks_the_checklist(self) -> None:
        with _vault_env(self.vault_dir):
            created = vault.write_job_note(self.cfg, _manifest())
            note_path = Path(created["path"])

            result = vault.append_stage(
                self.cfg,
                "harbour-wedding",
                "Cull",
                rows=[("proposed", "12"), ("applied", "10")],
                checklist_key="cull proposed",
            )
        self.assertTrue(result["written"])
        self.assertTrue(result["ticked"])

        text = note_path.read_text(encoding="utf-8")
        self.assertIn("- [x] cull proposed", text)
        self.assertNotIn("- [ ] cull proposed", text)
        self.assertIn("## Cull ", text)
        self.assertIn("| proposed | 12 |", text)
        self.assertIn("| applied | 10 |", text)

    def test_append_stage_missing_note(self) -> None:
        with _vault_env(self.vault_dir):
            result = vault.append_stage(
                self.cfg, "no-such-job", "Cull", rows=[], checklist_key=None
            )
        self.assertEqual(result, {"written": False, "reason": "no job note"})

    def test_append_stage_missing_checklist_line_still_appends(self) -> None:
        with _vault_env(self.vault_dir):
            created = vault.write_job_note(self.cfg, _manifest())
            note_path = Path(created["path"])
            result = vault.append_stage(
                self.cfg,
                "harbour-wedding",
                "Cull",
                rows=[("proposed", "12")],
                checklist_key="not a real checklist item",
            )
        self.assertTrue(result["written"])
        self.assertFalse(result["ticked"])
        self.assertIn("note", result)
        text = note_path.read_text(encoding="utf-8")
        self.assertIn("## Cull ", text)


class JobsTest(VaultTestCase):
    def test_jobs_lists_ratings_histogram(self) -> None:
        source = self.tmp / "card"
        _make_card(source, 3)
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result.path)
        raw = job_path / "raw"

        names = [Path(f["dest"]).name for f in result.files]
        (raw / f"{names[0]}.xmp").write_text('<x xmp:Rating="2"/>', encoding="utf-8")
        (raw / f"{names[1]}.xmp").write_text('<x xmp:Rating="1"/>', encoding="utf-8")
        # names[2] gets no sidecar at all -- buckets under 0 ("unrated").

        jr = library.jobs()
        self.assertEqual(len(jr.jobs), 1)
        entry = jr.jobs[0]
        self.assertEqual(entry["date"], "2026-06-28")
        self.assertEqual(entry["slug"], "harbour-wedding")
        self.assertEqual(entry["path"], "2026/2026-06-28 harbour-wedding")
        self.assertEqual(entry["frames"], 3)
        self.assertEqual(entry["ratings"], {2: 1, 1: 1, 0: 1})
        self.assertIn("checksums.sha256", entry["manifests"])
        self.assertIn("import.json", entry["manifests"])

    def test_jobs_empty_library(self) -> None:
        jr = library.jobs()
        self.assertEqual(jr.jobs, [])


class RelocateTest(VaultTestCase):
    def _write_config(self, extra: dict | None = None) -> Path:
        config_path = Path(self.cfg.path)
        data = {
            "_comment": "top-level comment",
            "version": 1,
            "library_root": str(self.cfg.root()),
            "cameras": {"E-M10 Mark III": "em10iii"},
            "backup": {
                "_comment": "backup comment",
                "enabled": True,
                "target": str(self.tmp / "backup"),
                "encryption": "openssl",
                "openssl_passfile": str(self.tmp / "backup.pass"),
            },
        }
        if extra:
            data.update(extra)
        config_path.write_text(json.dumps(data, indent=2))
        return config_path

    def test_relocate_dry_run_touches_nothing(self) -> None:
        config_path = self._write_config()
        before = config_path.read_text()
        new_root = self.tmp / "new-library"
        new_root.mkdir()

        result = library.relocate(str(new_root), dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(config_path.read_text(), before)
        self.assertFalse((new_root / "2026").exists())

    def test_relocate_rewrites_only_library_root(self) -> None:
        config_path = self._write_config()
        original = json.loads(config_path.read_text())

        new_root = self.tmp / "new-library"
        new_root.mkdir()

        result = library.relocate(str(new_root), confirm=True)

        self.assertEqual(result.old_root, str(self.cfg.root()))
        self.assertEqual(result.new_root, str(new_root))
        self.assertTrue(result.darktable_rollpaths_stale)
        self.assertIn("library.db", result.next_step)

        updated = json.loads(config_path.read_text())
        diffs = {k for k in set(original) | set(updated) if original.get(k) != updated.get(k)}
        self.assertEqual(diffs, {"library_root"})
        self.assertEqual(updated["library_root"], str(new_root))
        self.assertEqual(updated["_comment"], "top-level comment")
        self.assertEqual(updated["backup"]["_comment"], "backup comment")

    def test_relocate_refuses_nested_root(self) -> None:
        self._write_config()
        nested = self.cfg.root() / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(UsageError):
            library.relocate(str(nested), confirm=True)

    def test_relocate_refuses_missing_new_root(self) -> None:
        self._write_config()
        with self.assertRaises(UsageError):
            library.relocate(str(self.tmp / "does-not-exist"), confirm=True)

    def test_relocate_reports_per_job_verify(self) -> None:
        self._write_config()
        source = self.tmp / "card"
        _make_card(source, 2)
        result = library.import_card(str(source), "harbour-wedding", confirm=True)
        job_path = Path(result.path)

        new_root = self.tmp / "new-library"
        new_root.mkdir()
        # Mirror the job at the new root so `relocate`'s re-verify passes.
        import shutil

        shutil.copytree(job_path, new_root / "2026" / "2026-06-28 harbour-wedding")

        outcome = library.relocate(str(new_root), confirm=True)
        self.assertEqual(len(outcome.jobs), 1)
        self.assertTrue(outcome.jobs[0]["ok"])
        self.assertEqual(outcome.jobs[0]["slug"], "harbour-wedding")


if __name__ == "__main__":
    unittest.main()
