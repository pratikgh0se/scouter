import unittest

from darktable_mcp.bridge.client import BridgeError, BridgeTimeoutError
from scouter.core import dtbridge
from scouter.errors import BridgeTimeout, DbLocked, NotInDarkroom, WrongView
from tests.scouter_fakes import FakeBridge


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
            FakeBridge(raises={
                "darkroom.open": BridgeError("wrong image loaded: expected 12, got 7"),
            })
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            dtbridge.call("darkroom.open", {"image_id": 12})
        self.assertIn("wrong image loaded", str(ctx.exception))
        self.assertIn("expected 12, got 7", str(ctx.exception))

    def test_action_path_table_and_fallback(self) -> None:
        self.assertEqual(dtbridge.action_path("exposure", "exposure"),
                         "iop/exposure/exposure")
        self.assertEqual(dtbridge.action_path("sigmoid", "contrast"),
                         "iop/sigmoid/contrast")
        self.assertEqual(dtbridge.action_path("newmod", "newparam"),
                         "iop/newmod/newparam")

    def test_resolve_image_accepts_a_path_and_an_id(self) -> None:
        bridge = FakeBridge({
            "image.lookup": {"found": True, "image_id": 7, "path": "/a/x.ORF"},
            "darkroom.open": {"image_id": 7, "path": "/a/x.ORF", "view": "darkroom",
                              "opened": False, "already_open": True},
        })
        dtbridge.set_bridge(bridge)
        by_path = dtbridge.resolve_image("/a/x.ORF")
        self.assertEqual((by_path.image_id, by_path.xmp_path), (7, "/a/x.ORF.xmp"))
        by_id = dtbridge.resolve_image(7)
        self.assertEqual(by_id.image_id, 7)


if __name__ == "__main__":
    unittest.main()
