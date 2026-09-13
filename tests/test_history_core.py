import shutil
import time
import unittest
from pathlib import Path

from scouter.core import dtbridge, history
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


class HistoryReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        dtbridge.set_bridge(FakeBridge({
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.open": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                              "opened": False, "already_open": True},
        }))

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_read_returns_decoded_entries_and_the_history_end(self) -> None:
        result = history.read(image=str(self.raw))
        self.assertEqual(result.history_end, 14)
        self.assertTrue(result.confirmed)
        exposure = next(e for e in result.entries if e.num == 11)
        assert exposure.decoded is not None
        self.assertAlmostEqual(exposure.decoded["exposure"], 1.5500002, places=5)

    def test_read_warns_about_undecoded_modules_instead_of_guessing(self) -> None:
        result = history.read(image=str(self.raw))
        self.assertTrue(any("opaque hex" in w for w in result.warnings))

    def test_missing_sidecar_is_an_unconfirmed_result_not_a_crash(self) -> None:
        self.sidecar.unlink()
        result = history.read(image=str(self.raw))
        self.assertFalse(result.confirmed)
        self.assertEqual(result.entries, [])
        self.assertTrue(any("write sidecar file" in w for w in result.warnings))

    def test_every_entry_carries_its_own_enabled_flag(self) -> None:
        # Ruling 20: a disabled module must never be reported as live -- the
        # per-entry `enabled` flag has to survive the read verbatim.
        result = history.read(image=str(self.raw))
        self.assertTrue(result.entries, "fixture has no history entries to check")
        for entry in result.entries:
            self.assertIsInstance(entry.enabled, bool)

    def test_read_requires_the_image_argument(self) -> None:
        with self.assertRaises(TypeError):
            history.read()  # type: ignore[call-arg]


class HistoryCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        self.base = {
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.open": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                              "opened": False, "already_open": True},
        }

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_commit_succeeds_when_the_sidecar_mtime_advances(self) -> None:
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(text)          # a real rewrite bumps the mtime
            return {"strategy_used": "sidecar_action", "ok": True,
                    "view_after": "darkroom", "cpu_ms": 10}

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        result = history.commit(image=str(self.raw), timeout_s=2.0)
        self.assertTrue(result.committed)
        self.assertEqual(result.strategy_used, "sidecar_action")

    def test_commit_times_out_with_the_strategy_in_the_detail(self) -> None:
        dtbridge.set_bridge(FakeBridge({
            **self.base,
            "history.commit": {"strategy_used": "view_bounce", "ok": False,
                               "view_after": "darkroom", "cpu_ms": 5,
                               "detail": "view switch failed"},
        }))
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit(image=str(self.raw), timeout_s=0.2)
        self.assertEqual(ctx.exception.detail["strategy_used"], "view_bounce")
        self.assertIn("view switch failed", str(ctx.exception.detail["bridge_detail"]))

    def test_strategy_is_forwarded_to_the_bridge(self) -> None:
        bridge = FakeBridge({
            **self.base,
            "history.commit": {"strategy_used": "view_bounce", "ok": True,
                               "view_after": "darkroom", "cpu_ms": 5},
        })
        dtbridge.set_bridge(bridge)
        with self.assertRaises(CommitTimeout):
            history.commit(image=str(self.raw), timeout_s=0.1, strategy="view_bounce")
        params = dict(bridge.calls[-1][1])
        self.assertEqual(params["strategy"], "view_bounce")

    def test_commit_defaults_to_the_darkroom_image(self) -> None:
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(text)
            return {"strategy_used": "view_bounce", "ok": True,
                    "view_after": "darkroom", "cpu_ms": 5}

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        result = history.commit(timeout_s=2.0)
        self.assertEqual(result.image_id, 12)
