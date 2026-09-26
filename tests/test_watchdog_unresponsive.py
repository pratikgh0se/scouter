"""Watchdog design 4: typed unresponsive only when the call timed out AND the ping failed."""

import json
import unittest
from unittest import mock

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter.core import dtbridge
from scouter.errors import EXIT_CODES, BridgeTimeout, DarktableUnresponsive
from tests.scouter_fakes import FakeBridge


class UnresponsiveTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_timeout_plus_dead_ping_is_typed_unresponsive(self) -> None:
        dtbridge.set_bridge(FakeBridge(raises={"view.current": BridgeTimeoutError("t"),
                                               "darktable_mcp.diag": BridgeTimeoutError("t")}))
        with self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        self.assertEqual(cm.exception.detail["last_method"], "view.current")
        self.assertEqual(EXIT_CODES["darktable_unresponsive"], 24)

    def test_timeout_with_live_ping_stays_bridge_timeout(self) -> None:
        dtbridge.set_bridge(FakeBridge(raises={"view.current": BridgeTimeoutError("t")},
                                       responses={"darktable_mcp.diag": {"gui_ready": True}}))
        with self.assertRaises(BridgeTimeout):
            dtbridge.call("view.current")

    def test_diag_timeout_never_pings_itself(self) -> None:
        fake = FakeBridge(raises={"darktable_mcp.diag": BridgeTimeoutError("t")})
        dtbridge.set_bridge(fake)
        with self.assertRaises(BridgeTimeout):
            dtbridge.call("darktable_mcp.diag")
        self.assertEqual([m for m, _ in fake.calls], ["darktable_mcp.diag"])

    def test_doctor_ping_exit_codes(self) -> None:
        from scouter.cli import main as cli

        for state, code in (("alive", 0), ("starting", 0), ("unresponsive", 1)):
            with mock.patch.object(dtbridge, "health", return_value={"state": state,
                                                                     "ping_ms": 3}), \
                    mock.patch("builtins.print") as out:
                self.assertEqual(cli.main(["doctor", "--ping"]), code)
            self.assertEqual(json.loads(out.call_args[0][0])["watchdog"], state)
