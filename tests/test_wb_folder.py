"""Product close, item 3 (row 427 item 3): `wb calibrate` on a folder of raws,
one body at a time, so the Sony A7 and NEX-5N tables can be measured from
~/Pictures/EditingTrial/sony without hand-picking a frame.

Folder mode reads each raw's EXIF body, requires `camera` when the folder is
mixed, picks the first frame of that body by name, and sweeps a SCRATCH COPY
of it (never the original). `dry_run` lists the frames per body, the chosen
frame, the scratch copy and the table path, with no bridge call at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from scouter.core import dtbridge, wb
from scouter.errors import UsageError
from tests.scouter_fakes import FakeBridge
from tests.test_wb import _build_tiff_header, _CalibrateFixture, _temperature_sidecar


class _FolderFixture(_CalibrateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / "sony"
        self.folder.mkdir()
        for name, model in (
            ("DSC01426.ARW", "ILCE-7"),
            ("DSC01427.ARW", "ILCE-7"),
            ("DSC07448.ARW", "NEX-5N"),
            ("DSC07449.ARW", "NEX-5N"),
        ):
            (self.folder / name).write_bytes(_build_tiff_header(model=model, make="SONY"))
        (self.folder / "notes.txt").write_text("not a raw")
        self.scratch = self.tmp / "wb-scratch"

    def point_bridge_at(self, copy: Path) -> None:
        """The sweep's fake darktable acts on the scratch copy."""
        copy.parent.mkdir(parents=True, exist_ok=True)
        Path(str(copy) + ".xmp").write_text(
            _temperature_sidecar((1.6, 1.0, 1.8, float("inf")), num=0), encoding="utf-8"
        )
        self.raw = copy
        answer = {"image_id": 77, "path": str(copy), "view": "darkroom"}
        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 77, "path": str(copy)},
                "darkroom.open": {**answer, "opened": True, "already_open": False},
                "darkroom.current": {**answer, "source": "action_images"},
                "darkroom.action": self._action,
            }
        )
        dtbridge.set_bridge(self.bridge)


class FolderDryRunTest(_FolderFixture):
    def test_a_mixed_folder_needs_camera(self) -> None:
        with self.assertRaises(UsageError) as cm:
            wb.calibrate(str(self.folder), dry_run=True, job="job-test")
        self.assertIn("ilce7", cm.exception.message)
        self.assertIn("nex5n", cm.exception.message)
        self.assertEqual(self.bridge.calls, [])

    def test_an_unknown_camera_names_the_bodies_present(self) -> None:
        with self.assertRaises(UsageError) as cm:
            wb.calibrate(str(self.folder), camera="a9", dry_run=True, job="job-test")
        self.assertIn("nex5n", cm.exception.message)

    def test_dry_run_lists_the_frames_it_would_use_with_no_bridge_call(self) -> None:
        r = wb.calibrate(
            str(self.folder),
            3000,
            3200,
            100,
            camera="nex5n",
            scratch=str(self.scratch),
            dry_run=True,
            job="job-test",
        )
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(r.camera, "nex5n")
        self.assertEqual([Path(f["path"]).name for f in r.frames], ["DSC07448.ARW", "DSC07449.ARW"])
        self.assertEqual(Path(r.source_image).name, "DSC07448.ARW")
        self.assertEqual(r.scratch_copy, str(self.scratch / "nex5n" / "DSC07448.ARW"))
        self.assertEqual(r.table_path, str(self.tmp / "wb" / "nex5n.json"))
        self.assertEqual(r.ks, [3000, 3100, 3200])
        self.assertIn("DSC07448.ARW", r.reason)
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.scratch.exists(), "a dry run copies nothing")

    def test_no_confirm_is_a_dry_run_too(self) -> None:
        r = wb.calibrate(
            str(self.folder), camera="ilce7", scratch=str(self.scratch), job="job-test"
        )
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(Path(r.source_image).name, "DSC01426.ARW")

    def test_an_empty_folder_is_refused(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(UsageError):
            wb.calibrate(str(empty), dry_run=True, job="job-test")


class FolderRunTest(_FolderFixture):
    def test_confirmed_run_sweeps_a_scratch_copy_and_lands_where_wb_reads(self) -> None:
        copy = self.scratch / "ilce7" / "DSC01426.ARW"
        self.point_bridge_at(copy)
        self.coeffs_by_k = {
            3000.0: (1.3, 1.0, 2.9),
            3100.0: (1.4, 1.0, 2.8),
            3200.0: (1.5, 1.0, 2.7),
        }
        original = (self.folder / "DSC01426.ARW").read_bytes()
        r = wb.calibrate(
            str(self.folder),
            3000,
            3200,
            100,
            camera="ilce7",
            scratch=str(self.scratch),
            confirm=True,
            job="job-test",
        )
        self.assertTrue(r.written)
        self.assertEqual(r.camera, "ilce7")
        self.assertEqual(r.scratch_copy, str(copy))
        self.assertEqual(copy.read_bytes(), original)
        self.assertFalse(
            (self.folder / "DSC01426.ARW.xmp").exists(), "the original is never touched"
        )
        # It lands where `wb kelvin` reads: <config_dir>/wb/<camera>.json.
        self.assertEqual(Path(r.table_path), wb._table_path("ilce7"))
        table = wb.load_table("ilce7")
        self.assertIsNotNone(table)
        self.assertEqual([s["kelvin"] for s in table["samples"]], [3000, 3100, 3200])
        self.assertEqual(json.loads(Path(r.table_path).read_text())["source_image"], "DSC01426.ARW")
        self.assertEqual(wb.kelvin([1.4, 1.0, 2.8], "ilce7")["kelvin"], 3100)
