"""Tests for `scouter.core.export`: presets, `export.release`, `export.verify`.

`FakeCLIWrapper` (tests/scouter_fakes.py) is monkeypatched onto
`scouter.core.scout.CLIWrapper`, so every render captures the exact kwargs
that reached `CLIWrapper.export_image` -- including `extra_args` -- without
shelling out to darktable-cli at all.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter import config
from scouter.core import dtbridge, export
from scouter.errors import (
    ChecksumMismatch,
    ExportAllFailed,
    ManifestUnauthenticated,
    UsageError,
)
from tests.scouter_fakes import FakeBridge, FakeCLIWrapper, TempCache, config_fixture


class PresetArgsTest(unittest.TestCase):
    """`preset_args` is pure and preset-only: no job, no bridge, no CLI, and
    (fix round 1, blocker 1) never emits `--core` itself -- `CLIWrapper`
    owns the one `--core --configdir` block and splits this list at its
    first `"--conf"` to decide what precedes/follows it."""

    def test_preset_flags(self) -> None:
        cases = {
            "preview": [],
            "client-proof": [
                "--icc-type",
                "SRGB",
                "--conf",
                "plugins/imageio/format/jpeg/quality=90",
            ],
            "client-final": [
                "--icc-type",
                "SRGB",
                "--conf",
                "plugins/imageio/format/jpeg/quality=95",
            ],
            "print-tiff": [
                "--icc-type",
                "ADOBERGB",
                "--conf",
                "plugins/imageio/format/tiff/bpp=16",
                "--conf",
                "plugins/imageio/format/tiff/compress=5",
            ],
        }
        for preset, expected in cases.items():
            with self.subTest(preset=preset):
                args = export.preset_args(export.PRESETS[preset])
                self.assertEqual(args, expected)
                self.assertNotIn("--core", args)

    def test_style_and_overwrite_precede_conf(self) -> None:
        args = export.preset_args(
            export.PRESETS["client-proof"], style="hero-harbour", overwrite_style=True
        )
        self.assertEqual(
            args,
            [
                "--icc-type",
                "SRGB",
                "--style",
                "hero-harbour",
                "--style-overwrite",
                "--conf",
                "plugins/imageio/format/jpeg/quality=90",
            ],
        )


class ExportReleaseTestBase(unittest.TestCase):
    JOB_SLUG = "harbour-wedding"
    JOB_DATE = "2026-06-28"

    def setUp(self) -> None:
        self.cache = TempCache()
        self.tmp = self.cache.start()
        self.cfg = config_fixture(self.tmp)
        self._cfg_patch = mock.patch.object(export.config, "load", return_value=self.cfg)
        self._cfg_patch.start()

        self.bridge = FakeBridge()
        # Deterministic image_id per path, assigned in first-seen order --
        # so a test overriding "image.color_labels" for specific ids can
        # predict what `dtbridge.resolve_image` will hand it, without every
        # test having to restate its own "image.lookup" fake.
        self._image_ids: dict[str, int] = {}

        def _lookup(p: dict) -> dict:
            path = p["path"]
            if path not in self._image_ids:
                self._image_ids[path] = len(self._image_ids) + 1
            return {"found": True, "image_id": self._image_ids[path], "path": path}

        self.bridge.responses["image.lookup"] = _lookup
        dtbridge.set_bridge(self.bridge)

        self._saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")
        os.environ["SCOUTER_DARKTABLE_CLI"] = "unused-fake-darktable-cli"
        self._cli_patch = mock.patch("scouter.core.scout.CLIWrapper", FakeCLIWrapper)
        self._cli_patch.start()
        FakeCLIWrapper.calls = []
        FakeCLIWrapper.raises = {}

        self.job_path = config.job_dir(self.cfg, self.JOB_DATE, self.JOB_SLUG)
        (self.job_path / "raw").mkdir(parents=True)
        (self.job_path / ".scouter").mkdir(parents=True)

    def tearDown(self) -> None:
        self._cli_patch.stop()
        if self._saved_cli is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved_cli
        dtbridge.set_bridge(None)
        self._cfg_patch.stop()
        self.cache.stop()

    def _write_frame(self, name: str, rating: int) -> tuple[Path, str]:
        """One raw + XMP sidecar under raw/, with a real sha256 for import.json."""
        raw = self.job_path / "raw" / name
        raw.write_bytes(os.urandom(256))
        Path(str(raw) + ".xmp").write_text(f'<x xmp:Rating="{rating}"/>', encoding="utf-8")
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        return raw, sha

    def _write_import_json(self, files: list[dict]) -> None:
        (self.job_path / ".scouter" / "import.json").write_text(
            json.dumps({"job_slug": self.JOB_SLUG, "files": files}), encoding="utf-8"
        )

    def _standard_job(self) -> None:
        """Three frames: rating 3, rating 2, rating 0 -- the middle two clear
        the default `export.min_rating` (2), the last does not."""
        entries = []
        for name, rating in (("f1.orf", 3), ("f2.orf", 2), ("f3.orf", 0)):
            raw, sha = self._write_frame(name, rating)
            entries.append({"dest": f"raw/{name}", "sha256": sha})
        self._write_import_json(entries)


class ExportReleaseTest(ExportReleaseTestBase):
    def test_client_proof_is_2048_px(self) -> None:
        self._standard_job()
        export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertTrue(FakeCLIWrapper.calls)
        for call in FakeCLIWrapper.calls:
            self.assertEqual(call["max_width"], 2048)
            self.assertEqual(call["max_height"], 2048)

    def test_empty_selection_refused_with_histogram(self) -> None:
        self._write_import_json(
            [
                {"dest": "raw/f1.orf", "sha256": "x"},
            ]
        )
        raw, _ = self._write_frame("f1.orf", 0)
        with self.assertRaises(UsageError) as ctx:
            export.release(self.JOB_SLUG, "client-proof", min_rating=2, confirm=True)
        message = str(ctx.exception)
        self.assertIn("rating>=2", message)
        self.assertIn("histogram", message.lower())
        self.assertIn("'0': 1", message)

    def test_manifest_shape_and_checksum(self) -> None:
        self._standard_job()
        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertEqual(result.exported, 2)
        manifest_path = Path(result.manifest_path)
        self.assertTrue(manifest_path.is_file())
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))

        for key in (
            "version",
            "job_slug",
            "preset",
            "released_at",
            "scouter_version",
            "darktable",
            "style",
            "style_overwrite",
            "selection",
            "input_sha256_source",
            "files",
            "totals",
        ):
            self.assertIn(key, doc, f"manifest missing {key!r}")
        self.assertEqual(doc["input_sha256_source"], "import.json")
        self.assertEqual(doc["selection"]["rule"], "rating>=2")
        self.assertEqual(len(doc["files"]), 2)
        for f in doc["files"]:
            for key in (
                "input",
                "input_sha256",
                "output",
                "output_sha256",
                "bytes",
                "px",
                "render_ms",
            ):
                self.assertIn(key, f)
            self.assertTrue(f["input_sha256"])

        sha_path = manifest_path.with_suffix(".sha256")
        self.assertTrue(sha_path.is_file())
        recorded = sha_path.read_text(encoding="utf-8").split()[0]
        actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        self.assertEqual(recorded, actual)

    def test_input_sha_read_not_recomputed(self) -> None:
        """The input raw is never opened for hashing: `input_sha256` comes
        straight from import.json. Proven by making the raw file unreadable
        right after import.json is written -- if `export.release` ever tried
        to re-hash it, this would raise `PermissionError` and the release
        would fail."""
        self._standard_job()
        raw = self.job_path / "raw" / "f1.orf"
        mode = raw.stat().st_mode & 0o777
        raw.chmod(0o000)
        try:
            result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        finally:
            raw.chmod(mode)
        self.assertEqual(result.exported, 2)
        self.assertEqual(result.failed, 0)

    def test_existing_output_is_gated(self) -> None:
        self._standard_job()
        first = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertEqual(first.exported, 2)

        again = export.release(self.JOB_SLUG, "client-proof")
        self.assertEqual(again.outcome, "dry_run")
        self.assertIn("already exist", again.reason or "")

        forced = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertEqual(forced.exported, 2)

    def test_one_render_failure_keeps_the_rest(self) -> None:
        self._standard_job()
        bad_raw = str(self.job_path / "raw" / "f1.orf")
        FakeCLIWrapper.raises = {bad_raw: RuntimeError("boom")}
        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertEqual(result.exported, 1)
        self.assertEqual(result.failed, 1)
        failed_entries = [f for f in result.files if not f.ok]
        self.assertEqual(len(failed_entries), 1)
        self.assertIn("boom", failed_entries[0].error or "")

    def test_style_overwrite_only_reaches_the_cli(self) -> None:
        self._standard_job()
        export.release(
            self.JOB_SLUG,
            "client-proof",
            style="hero-harbour",
            overwrite_style=True,
            confirm=True,
        )
        self.assertTrue(FakeCLIWrapper.calls)
        for call in FakeCLIWrapper.calls:
            self.assertIn("--style-overwrite", call["extra_args"] or [])
            self.assertIn("hero-harbour", call["extra_args"] or [])
        # style-overwrite is render-only: nothing in the library changed.
        self.assertEqual(self.bridge.calls, [])

    def test_finals_select_green_labelled_frames_only(self) -> None:
        """Ruling R12: `client-final`/`print-tiff` select by colour label,
        not rating -- proven here with all three of `_standard_job`'s frames
        rated differently (3, 2, 0) but only ONE (f1) carrying the green
        label, via a single batched `image.color_labels` bridge call."""
        self._standard_job()
        green_path = str(self.job_path / "raw" / "f1.orf")

        def color_labels(p: dict) -> dict:
            green_id = self._image_ids[green_path]
            per_image = [
                {"image_id": image_id, "labels": ["green"] if image_id == green_id else []}
                for image_id in p["image_ids"]
            ]
            return {"missing": [], "per_image": per_image}

        self.bridge.responses["image.color_labels"] = color_labels

        result = export.release(self.JOB_SLUG, "client-final", confirm=True)
        self.assertEqual(result.selection_rule, "color_label=green")
        self.assertEqual(result.selected, 1)
        self.assertEqual(result.of, 3)
        self.assertEqual(result.exported, 1)
        self.assertEqual(result.files[0].input, green_path)
        self.assertIn("image.color_labels", self.bridge.methods_called())
        # One batched call for all three candidates, not one per frame.
        self.assertEqual(self.bridge.count("image.color_labels"), 1)

    def test_finals_warns_on_an_id_the_bridge_did_not_know(self) -> None:
        self._standard_job()

        def color_labels(p: dict) -> dict:
            ids = p["image_ids"]
            return {
                "missing": [ids[0]],
                "per_image": [{"image_id": i, "labels": ["green"]} for i in ids[1:]],
            }

        self.bridge.responses["image.color_labels"] = color_labels
        result = export.release(self.JOB_SLUG, "client-final", confirm=True)
        self.assertEqual(result.exported, 2)
        self.assertTrue(any("missed" in w or "missing" in w.lower() for w in result.warnings))

    def test_raw_and_jpeg_pair_get_disambiguated_output_names(self) -> None:
        """Fix round 1, blocker 2: a raw and a same-stem JPEG (a card holding
        both for one frame) must not silently clobber each other's output."""
        entries = []
        for name in ("f1.orf", "f1.jpg"):
            raw, sha = self._write_frame(name, 3)
            entries.append({"dest": f"raw/{name}", "sha256": sha})
        self._write_import_json(entries)

        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertEqual(result.exported, 2)
        outputs = {Path(f.output).name for f in result.files}
        self.assertEqual(outputs, {"f1-orf.jpg", "f1-jpg.jpg"})

    def test_all_renders_failing_is_a_typed_error_not_a_success(self) -> None:
        """Fix round 1, should-fix 7: never a success with exported=0."""
        self._standard_job()
        for name in ("f1.orf", "f2.orf"):
            FakeCLIWrapper.raises[str(self.job_path / "raw" / name)] = RuntimeError("boom")
        with self.assertRaises(ExportAllFailed) as ctx:
            export.release(self.JOB_SLUG, "client-proof", confirm=True)
        self.assertEqual(len(ctx.exception.detail["files"]), 2)
        # No manifest was written for an all-failed release.
        self.assertEqual(list((self.job_path / ".scouter").glob("manifest-*.json")), [])

    def test_unregistered_frame_becomes_a_warning_not_an_abort(self) -> None:
        """Fix round 1, should-fix 5: ImageNotFound during label resolution
        excludes just that frame, with a warning -- never aborts the release."""
        self._standard_job()
        unregistered_path = str(self.job_path / "raw" / "f1.orf")
        green_path = str(self.job_path / "raw" / "f2.orf")
        base_lookup = self.bridge.responses["image.lookup"]

        def lookup(p: dict) -> dict:
            if p["path"] == unregistered_path:
                return {"found": False, "image_id": None, "path": p["path"]}
            return base_lookup(p)

        self.bridge.responses["image.lookup"] = lookup

        def color_labels(p: dict) -> dict:
            green_id = self._image_ids[green_path]
            per_image = [
                {"image_id": image_id, "labels": ["green"] if image_id == green_id else []}
                for image_id in p["image_ids"]
            ]
            return {"missing": [], "per_image": per_image}

        self.bridge.responses["image.color_labels"] = color_labels

        result = export.release(self.JOB_SLUG, "client-final", confirm=True)
        self.assertEqual(result.exported, 1)
        self.assertEqual(result.files[0].input, green_path)
        self.assertTrue(any("not registered" in w for w in result.warnings))

    def test_unknown_final_label_is_a_typed_refusal(self) -> None:
        """Fix round 1, should-fix 6: a typo in export.final_label is refused
        against the real vocabulary, not silently treated as "matches nothing"."""
        self._standard_job()
        bad_cfg = dataclasses.replace(
            self.cfg, export=dataclasses.replace(self.cfg.export, final_label="gren")
        )
        with mock.patch.object(export.config, "load", return_value=bad_cfg):
            with self.assertRaises(UsageError) as ctx:
                export.release(self.JOB_SLUG, "client-final", confirm=True)
        self.assertIn("gren", str(ctx.exception))

    def test_finals_empty_selection_reports_the_label_histogram(self) -> None:
        self._standard_job()

        def color_labels(p: dict) -> dict:
            return {
                "missing": [],
                "per_image": [{"image_id": i, "labels": ["red"]} for i in p["image_ids"]],
            }

        self.bridge.responses["image.color_labels"] = color_labels
        with self.assertRaises(UsageError) as ctx:
            export.release(self.JOB_SLUG, "client-final", confirm=True)
        message = str(ctx.exception)
        self.assertIn("color_label=green", message)
        self.assertIn("'red': 3", message)


class ExportVerifyTest(ExportReleaseTestBase):
    def test_export_verify_detects_a_flipped_byte(self) -> None:
        self._standard_job()
        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        manifest_path = result.manifest_path

        good = export.verify(manifest_path)
        self.assertTrue(good.ok)
        self.assertEqual(good.checked, 2)
        self.assertEqual(good.mismatched, [])
        self.assertEqual(good.missing, [])

        output_path = Path(result.files[0].output)
        data = bytearray(output_path.read_bytes())
        data[0] ^= 0xFF
        output_path.write_bytes(bytes(data))

        flipped = export.verify(manifest_path)
        self.assertFalse(flipped.ok)
        self.assertEqual(len(flipped.mismatched), 1)

    def test_verify_refuses_when_the_manifest_itself_was_tampered(self) -> None:
        self._standard_job()
        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        manifest_path = Path(result.manifest_path)
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        doc["totals"]["bytes"] = 999999
        manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        with self.assertRaises(ChecksumMismatch):
            export.verify(str(manifest_path))

    def test_verify_refuses_a_manifest_with_no_sha256_sidecar(self) -> None:
        """Fix round 1, blocker 3: a missing sidecar is a typed refusal, not
        an optional check that gets silently skipped."""
        self._standard_job()
        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        sha_path = Path(result.manifest_path).with_suffix(".sha256")
        sha_path.unlink()

        with self.assertRaises(ManifestUnauthenticated):
            export.verify(result.manifest_path)

    def test_verify_root_resolves_a_manifest_shipped_beside_its_exports_folder(self) -> None:
        """Fix round 1, nit 8: `--root` for a manifest+exports pair copied
        somewhere else entirely (e.g. handed to a client on a USB stick),
        where the manifest no longer sits at `<job>/.scouter/`."""
        self._standard_job()
        result = export.release(self.JOB_SLUG, "client-proof", confirm=True)
        manifest_path = Path(result.manifest_path)
        sha_path = manifest_path.with_suffix(".sha256")

        shipped = self.tmp / "shipped"
        shipped.mkdir()
        shutil.copy(manifest_path, shipped / manifest_path.name)
        shutil.copy(sha_path, shipped / sha_path.name)
        shutil.copytree(Path(result.out_dir), shipped / "exports" / "client-proof")

        verified = export.verify(str(shipped / manifest_path.name), root=str(shipped))
        self.assertTrue(verified.ok)
        self.assertEqual(verified.checked, 2)
        self.assertEqual(verified.missing, [])


if __name__ == "__main__":
    unittest.main()
