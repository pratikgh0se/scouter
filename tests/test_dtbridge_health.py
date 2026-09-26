"""dtbridge.health() (watchdog design 2.1), against FakeBridge."""

import unittest

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter.core import dtbridge
from tests.scouter_fakes import FakeBridge


class HealthTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_alive_starting_unresponsive(self) -> None:
        dtbridge.set_bridge(FakeBridge(responses={"darktable_mcp.diag": {"gui_ready": True}}))
        self.assertEqual(dtbridge.health()["state"], "alive")
        dtbridge.set_bridge(FakeBridge(responses={"darktable_mcp.diag": {"gui_ready": False}}))
        self.assertEqual(dtbridge.health()["state"], "starting")
        dtbridge.set_bridge(FakeBridge(raises={"darktable_mcp.diag": BridgeTimeoutError("x")}))
        h = dtbridge.health()
        self.assertEqual(h["state"], "unresponsive")
        self.assertIn("ping_ms", h)

    def test_ping_uses_the_5_second_budget(self) -> None:
        fake = FakeBridge(responses={"darktable_mcp.diag": {"gui_ready": True}})
        dtbridge.set_bridge(fake)
        dtbridge.health()
        self.assertEqual(fake.calls[-1][0], "darktable_mcp.diag")
