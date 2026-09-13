import os
import unittest

from darktable_mcp.bridge.client import BridgeError, BridgeTimeoutError
from scouter.core import dtbridge
from scouter.errors import BridgeTimeout, DbLocked, NotInDarkroom, WrongView
from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, fake_cli_script


class ClassificationTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_timeout_becomes_bridge_timeout_with_a_fix(self) -> None:
        dtbridge.set_bridge(FakeBridge(raises={"view.current": BridgeTimeoutError("no resp")}))
        with self.assertRaises(BridgeTimeout) as ctx:
            dtbridge.current_view()
        self.assertIn("darktable", ctx.exception.fix)

    def test_locked_database_message_becomes_db_locked(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(raises={"view.current": BridgeError("ERROR: database is locked")})
        )
        with self.assertRaises(DbLocked):
            dtbridge.current_view()

    def test_wrong_view_names_the_view_and_the_fix(self) -> None:
        dtbridge.set_bridge(FakeBridge({"view.current": {"view": "lighttable"}}))
        with self.assertRaises(WrongView) as ctx:
            dtbridge.require_darkroom()
        self.assertIn("lighttable", str(ctx.exception))
        self.assertIn("darkroom open", ctx.exception.fix)

    def test_nan_becomes_not_in_darkroom_never_a_value(self) -> None:
        dtbridge.set_bridge(
            FakeBridge({"darkroom.action": {"nan": True, "status": None, "view": "darkroom"}})
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.action("iop/exposure/exposure", effect="set", value=1.0)
        self.assertIn("nan", str(ctx.exception))

    def test_wrong_image_loaded_becomes_not_in_darkroom_with_message_preserved(self) -> None:
        """Lua fix round 2: a loaded-image mismatch is now a raised JSON-RPC
        error ("wrong image loaded: ...") instead of {opened: False, error:
        ...}. There is no WrongImage class (spec 9's table), so it classifies
        as not_in_darkroom, with the plugin's own message kept verbatim."""
        dtbridge.set_bridge(
            FakeBridge(
                raises={
                    "darkroom.open": BridgeError("wrong image loaded: expected 12, got 7"),
                }
            )
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.call("darkroom.open", {"image_id": 12})
        self.assertIn("wrong image loaded", str(ctx.exception))
        self.assertIn("expected 12, got 7", str(ctx.exception))

    def test_action_path_table_and_fallback(self) -> None:
        self.assertEqual(dtbridge.action_path("exposure", "exposure"), "iop/exposure/exposure")
        self.assertEqual(dtbridge.action_path("sigmoid", "contrast"), "iop/sigmoid/contrast")
        self.assertEqual(dtbridge.action_path("newmod", "newparam"), "iop/newmod/newparam")

    def test_resolve_image_accepts_a_path_and_an_id(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 7, "path": "/a/x.ORF"},
                "darkroom.open": {
                    "image_id": 7,
                    "path": "/a/x.ORF",
                    "view": "darkroom",
                    "opened": False,
                    "already_open": True,
                },
            }
        )
        dtbridge.set_bridge(bridge)
        by_path = dtbridge.resolve_image("/a/x.ORF")
        self.assertEqual((by_path.image_id, by_path.xmp_path), (7, "/a/x.ORF.xmp"))
        by_id = dtbridge.resolve_image(7)
        self.assertEqual(by_id.image_id, 7)
        # Final review, blocker 2: BOTH forms resolve through `image.lookup`,
        # which reads dt.database and touches no GUI state. `darkroom.open`
        # loads an image into the darkroom and must never be a resolution
        # mechanism -- not for an id, not for a path.
        self.assertEqual(bridge.methods_called(), ["image.lookup", "image.lookup"])
        self.assertEqual(bridge.calls[1][1], {"image_id": 7})

    def test_resolve_image_accepts_a_numeric_string_id(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 7, "path": "/a/x.ORF"},
            }
        )
        dtbridge.set_bridge(bridge)
        self.assertEqual(dtbridge.resolve_image("7").image_id, 7)
        self.assertEqual(bridge.calls[0], ("image.lookup", {"image_id": 7}))

    def test_an_unknown_path_is_a_usage_error_unless_the_verb_may_import(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": False, "image_id": None, "path": "/a/x.ORF"},
                "darkroom.open": {
                    "image_id": 7,
                    "path": "/a/x.ORF",
                    "view": "darkroom",
                    "opened": True,
                    "already_open": False,
                    "imported": True,
                },
            }
        )
        dtbridge.set_bridge(bridge)
        with self.assertRaises(dtbridge.ImageNotFound) as ctx:
            dtbridge.resolve_image("/a/x.ORF")
        self.assertEqual(ctx.exception.code, "usage")
        self.assertNotIn("darkroom.open", bridge.methods_called())
        # A write verb may import it -- that IS darkroom.open's job (ruling 4).
        self.assertEqual(dtbridge.resolve_image("/a/x.ORF", may_import=True).image_id, 7)
        self.assertIn("darkroom.open", bridge.methods_called())

    def test_an_unknown_id_is_never_imported_even_for_a_writer(self) -> None:
        bridge = FakeBridge(
            {
                "image.lookup": {"found": False, "image_id": 99, "path": None},
            }
        )
        dtbridge.set_bridge(bridge)
        with self.assertRaises(dtbridge.ImageNotFound):
            dtbridge.resolve_image(99, may_import=True)
        self.assertNotIn("darkroom.open", bridge.methods_called())


class ReadsNeverOpenTheDarkroomTest(unittest.TestCase):
    """Final review, blocker 2: a read or a dry run must not move the darkroom.

    The bridge here REFUSES `darkroom.open` outright, so any verb that still
    resolves through it fails loudly instead of silently switching the view
    out from under whoever is using darktable.
    """

    def setUp(self) -> None:
        from tests.scouter_fakes import TempCache

        self.cache = TempCache()
        self.root = self.cache.start()
        self.raw = self.root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "list_styles": {"styles": [], "count": 0},
            },
            raises={
                "darkroom.open": AssertionError(
                    "darkroom.open must not be called by a read verb or a dry run"
                )
            },
        )
        dtbridge.set_bridge(self.bridge)
        self._saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved_cli is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved_cli
        self.cache.stop()

    def _use_fake_cli(self) -> None:
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(fake_cli_script(self.root, FAKE_CLI_WRITES_PPM))

    def test_history_read_does_not_open(self) -> None:
        from scouter.core import history

        history.read(image=str(self.raw))
        self.assertNotIn("darkroom.open", self.bridge.methods_called())

    def test_scout_stats_and_preview_do_not_open(self) -> None:
        from scouter.core import scout

        self._use_fake_cli()
        scout.stats(image=str(self.raw), px=64)
        scout.preview(image=str(self.raw), px=64, fmt="ppm")
        self.assertNotIn("darkroom.open", self.bridge.methods_called())

    def test_style_and_export_dry_runs_do_not_open(self) -> None:
        from scouter.core import export, style

        style.extract(image=str(self.raw), name="hero", modules=[], dry_run=True)
        style.apply(image=str(self.raw), name="hero", dry_run=True)
        export.run(
            images=[str(self.raw)], preset="preview", out_dir=str(self.root / "out"), dry_run=True
        )
        self.assertNotIn("darkroom.open", self.bridge.methods_called())

    def test_a_darkroom_write_dry_run_calls_nothing_at_all(self) -> None:
        from scouter.core import darkroom

        darkroom.set_param(module="exposure", param="exposure", value=1.0, dry_run=True)
        darkroom.nudge(module="exposure", param="exposure", steps=1, dry_run=True)
        self.assertEqual(self.bridge.calls, [])


if __name__ == "__main__":
    unittest.main()
