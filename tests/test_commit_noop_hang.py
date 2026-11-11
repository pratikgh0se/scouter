"""Row 431: `history.commit` with nothing to commit must answer promptly.

Bench 20260926t083159z: every session's first setup call -- `history_commit`
on a freshly opened, unedited frame, `timeout_s=10` -- never answered. The
MCP client gave up at 30s, the server's bridge at its own 60s
(`DEFAULT_TIMEOUTS["history.commit"]`), because `history.commit` never
passed `timeout_s` to the bridge and always asked the plugin for a real
change, so the plugin's fallback bounce ran with no deadline at all.
"""

from __future__ import annotations

import shutil
import threading
import time
import unittest
from pathlib import Path

from darktable_mcp.bridge.client import Bridge
from scouter.core import darkroom, dtbridge, history
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


class _SilentPluginBridge(FakeBridge):
    """Answers everything from `responses` except `history.commit`, which goes
    through the REAL file bridge to a plugin that never answers -- the host's
    wedged/slow Lua worker, reduced to its observable: no response file."""

    def __init__(self, responses: dict, cache_dir: Path, plugin: Path) -> None:
        super().__init__(responses)
        self._real = Bridge(cache_dir=cache_dir, plugin_path=plugin)

    def call(self, method, params, timeout=None):
        if method == "history.commit":
            self.calls.append((method, dict(params)))
            return self._real.call(method, params, timeout=timeout)
        return super().call(method, params, timeout=timeout)


class CommitNoopHangTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.root = root
        self.raw = root / "P5240151.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        self.original_text = self.sidecar.read_text()
        self.base = {
            "darkroom.current": {
                "image_id": 12,
                "path": str(self.raw),
                "view": "darkroom",
                "source": "action_images",
            },
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {
                "nan": False,
                "status": p.get("value"),
                "view": "darkroom",
                **p,
            },
        }

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _plugin_like(self, params: dict) -> dict:
        """The Lua auto strategy on an unedited frame: no_bounce finds nothing
        to write; with expect_change it would fall back to a bounce that also
        writes nothing (slowly, on a cold darkroom)."""
        if params.get("expect_change", True) is False:
            return {
                "strategy_used": "no_bounce",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 3,
                "sidecar_written": False,
                "attempts_used": 1,
                "expect_change": False,
            }
        time.sleep(min(params.get("budget_ms", 60000), 60000) / 1000.0)
        return {
            "strategy_used": "view_bounce_action",
            "ok": False,
            "view_after": "darkroom",
            "cpu_ms": 9,
            "sidecar_written": False,
            "attempts_used": 2,
            "expect_change": True,
        }

    def test_nothing_pending_is_a_prompt_honest_noop(self) -> None:
        bridge = FakeBridge({**self.base, "history.commit": self._plugin_like})
        dtbridge.set_bridge(bridge)
        started = time.monotonic()
        result = history.commit(image=str(self.raw), timeout_s=2.0, job="job-test")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "nothing to commit must not wait out the timeout")
        self.assertTrue(result.noop, "reported as a no-op")
        self.assertFalse(result.committed)
        self.assertFalse(result.changed)
        self.assertFalse(result.confirmed, "a no-op is never proof enough for a watermark")
        self.assertTrue(any("nothing to commit" in w for w in result.warnings), result.warnings)
        params = bridge.calls[-1][1]
        self.assertIs(params["expect_change"], False, "no pending write: no bounce asked for")
        self.assertLessEqual(params["budget_ms"], 2000, "the plugin is told the budget")
        fresh, _ = history.watermark_fresh("job-test", 12, str(self.sidecar))
        self.assertFalse(fresh, "and no watermark was stamped")

    def test_a_pending_scouter_write_still_demands_a_real_commit(self) -> None:
        text = self.original_text
        seen: list[dict] = []

        def commit(params: dict) -> dict:
            seen.append(dict(params))
            time.sleep(0.01)
            self.sidecar.write_text(with_new_history_hash(text))
            return {
                "strategy_used": "no_bounce",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 4,
                "sidecar_written": True,
                "attempts_used": 1,
                "expect_change": params.get("expect_change", True),
            }

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        darkroom.set_param(
            module="exposure", param="exposure", value=4.25, commit=False, job="job-test"
        )
        result = history.commit(image=str(self.raw), timeout_s=2.0, job="job-test")
        self.assertIs(
            seen[-1]["expect_change"], True, "a journalled pending write expects a change"
        )
        self.assertTrue(result.committed)
        self.assertFalse(result.noop)

    def test_the_bridge_call_is_bounded_by_timeout_s(self) -> None:
        plugin = self.root / "darktable_mcp.lua"
        plugin.write_text("-- installed, but the worker never answers")
        bridge = _SilentPluginBridge(self.base, self.root / "bridge", plugin)
        dtbridge.set_bridge(bridge)
        outcome: dict = {}

        def run() -> None:
            started = time.monotonic()
            try:
                history.commit(
                    image=str(self.raw), timeout_s=0.5, expect_change=True, job="job-test"
                )
                outcome["result"] = "returned"
            except CommitTimeout as e:
                outcome["result"] = e
            except Exception as e:  # noqa: BLE001 - the test reports whatever escaped
                outcome["result"] = e
            outcome["elapsed"] = time.monotonic() - started

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(3.0)
        self.assertFalse(
            worker.is_alive(),
            "history.commit(timeout_s=0.5) was still " "waiting on the bridge after 3s",
        )
        self.assertIsInstance(outcome["result"], CommitTimeout, outcome)
        self.assertLess(outcome["elapsed"], 1.0)
        self.assertIn("did not answer", str(outcome["result"]))


if __name__ == "__main__":
    unittest.main()
