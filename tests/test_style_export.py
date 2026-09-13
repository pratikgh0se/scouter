import os
import unittest
from pathlib import Path

from scouter.core import dtbridge, export, style
from scouter.core.style import UnsupportedMode
from scouter.errors import NotInDarkroom, StylePartial
from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, TempCache, fake_cli_script


class StyleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        self.raw = self.root / "hero.ORF"
        self.raw.write_bytes(b"raw")
        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.open": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "opened": False,
                    "already_open": True,
                },
                "list_styles": {"styles": [{"name": "old-look"}], "count": 1},
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "style.create_from_current": {
                    "name": "hero",
                    "created": True,
                    "existing_replaced": False,
                    "modules_requested": 2,
                    "modules_honoured": False,
                    "note": "dt.styles.create has no module filter in Lua API 9.6.0",
                    "source": "action_images",
                },
                "style.apply": {
                    "name": "hero",
                    "mode": "append",
                    "applied": 1,
                    "missed": [],
                    "errors": [],
                },
            }
        )
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_extract_is_honest_about_the_missing_module_filter(self) -> None:
        result = style.extract(
            image=str(self.raw), name="hero", modules=["exposure", "sigmoid"], no_filter=True
        )
        self.assertTrue(result.created)
        self.assertFalse(result.modules_honoured)
        self.assertEqual(result.modules_requested, ["exposure", "sigmoid"])
        self.assertTrue(any("module filter" in w for w in result.warnings))

    def test_extract_over_an_existing_name_needs_confirm(self) -> None:
        result = style.extract(image=str(self.raw), name="old-look", modules=[], no_filter=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("already exists", result.reason or "")
        self.assertNotIn("style.create_from_current", self.bridge.methods_called())
        confirmed = style.extract(
            image=str(self.raw), name="old-look", modules=[], confirm=True, no_filter=True
        )
        self.assertEqual(confirmed.outcome, "ok")

    def test_extract_keeps_the_lua_reported_source(self) -> None:
        result = style.extract(image=str(self.raw), name="hero", modules=[], no_filter=True)
        self.assertEqual(result.source, "action_images")

    def test_extract_refuses_when_the_darkroom_shows_another_image(self) -> None:
        """Final re-review: `style.create_from_current` builds the style from

        whatever the darkroom currently displays, not from the `image`
        argument `extract` resolved by lookup. If the darkroom shows a
        different image, refuse BEFORE `style.create_from_current` is ever
        called (the RefusingBridge pattern of `tests/test_confirm_gates.py`,
        applied inline here) rather than silently building the style from the
        wrong photo and reporting the requested one.
        """
        self.bridge.responses["darkroom.current"] = {
            "image_id": 999,
            "path": "/somewhere/else.ORF",
            "view": "darkroom",
            "source": "action_images",
        }
        before = len(self.bridge.calls)
        with self.assertRaises(NotInDarkroom) as ctx:
            style.extract(image=str(self.raw), name="hero", modules=[], no_filter=True)
        self.assertIn("darkroom shows 999, requested 12", str(ctx.exception))
        self.assertNotIn("style.create_from_current", self.bridge.methods_called()[before:])

    def test_extract_proceeds_when_the_darkroom_matches(self) -> None:
        result = style.extract(image=str(self.raw), name="hero", modules=[], no_filter=True)
        self.assertEqual(result.outcome, "ok")
        self.assertIn("darkroom.current", self.bridge.methods_called())
        self.assertIn("style.create_from_current", self.bridge.methods_called())

    def test_extract_dry_run_does_not_call_darkroom_current(self) -> None:
        result = style.extract(image=str(self.raw), name="old-look", modules=[])
        self.assertEqual(result.outcome, "dry_run")
        self.assertNotIn("darkroom.current", self.bridge.methods_called())

    def test_apply_append_is_ungated(self) -> None:
        result = style.apply(image=str(self.raw), name="hero")
        self.assertEqual(result.applied, 1)
        self.assertEqual(result.outcome, "ok")

    def test_apply_overwrite_is_refused_outright_before_any_bridge_call(self) -> None:
        """Final review, should-fix 7: `overwrite` is not a confirmation gate.

        No `confirm=True` makes it work -- darktable's Lua API only appends --
        so it is a typed refusal (`unsupported_mode`) raised before ANYTHING
        reaches the bridge, not a dry run that implies a confirmable action.
        """
        before = len(self.bridge.calls)
        with self.assertRaises(UnsupportedMode) as ctx:
            style.apply(image=str(self.raw), name="hero", mode="overwrite")
        self.assertEqual(ctx.exception.code, "unsupported_mode")
        self.assertEqual(ctx.exception.detail["modes_available"], ["append"])
        self.assertEqual(self.bridge.calls[before:], [], "the refusal called the bridge")
        self.assertNotIn("style.apply", self.bridge.methods_called())

    def test_partial_apply_is_the_typed_error(self) -> None:
        self.bridge.responses["style.apply"] = {
            "name": "hero",
            "mode": "append",
            "applied": 0,
            "missed": [12],
            "errors": [{"image_id": 12, "error": "boom"}],
        }
        with self.assertRaises(StylePartial) as ctx:
            style.apply(image=str(self.raw), name="hero")
        self.assertEqual(ctx.exception.detail["missed"], [12])

    def test_unknown_mode_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            style.apply(image=str(self.raw), name="hero", mode="merge")


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        self.raw = self.root / "hero.ORF"
        self.raw.write_bytes(b"raw")
        self.second = self.root / "other.ORF"
        self.second.write_bytes(b"raw")
        dtbridge.set_bridge(
            FakeBridge(
                {
                    "image.lookup": lambda p: {"found": True, "image_id": 12, "path": p["path"]},
                    "darkroom.open": {
                        "image_id": 12,
                        "path": str(self.raw),
                        "view": "darkroom",
                        "opened": False,
                        "already_open": True,
                    },
                }
            )
        )
        self._saved = os.environ.get("SCOUTER_DARKTABLE_CLI")
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(fake_cli_script(self.root, FAKE_CLI_WRITES_PPM))
        self.out = self.root / "exports"

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved
        self.cache.stop()

    def test_single_new_file_exports_without_confirm(self) -> None:
        result = export.run(images=[str(self.raw)], preset="preview", out_dir=str(self.out))
        self.assertEqual(result.exported, 1)
        self.assertTrue(Path(result.files[0].output or "").is_file())

    def test_more_than_one_image_needs_confirm(self) -> None:
        result = export.run(
            images=[str(self.raw), str(self.second)], preset="preview", out_dir=str(self.out)
        )
        self.assertEqual(result.outcome, "dry_run")
        self.assertIn("2 images", result.reason or "")
        self.assertFalse(self.out.exists())

    def test_overwriting_an_existing_file_needs_confirm(self) -> None:
        export.run(images=[str(self.raw)], preset="preview", out_dir=str(self.out))
        again = export.run(images=[str(self.raw)], preset="preview", out_dir=str(self.out))
        self.assertEqual(again.outcome, "dry_run")
        self.assertIn("already exist", again.reason or "")
        forced = export.run(
            images=[str(self.raw)], preset="preview", out_dir=str(self.out), confirm=True
        )
        self.assertEqual(forced.exported, 1)

    def test_unknown_preset_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            export.run(images=[str(self.raw)], preset="print-tiff", out_dir=str(self.out))
