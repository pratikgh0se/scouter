"""Watchdog design 4: typed unresponsive only when the call timed out AND the ping failed
AND a darktable process exists (fix round 1: a closed darktable stays bridge_timeout)."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter.core import dtbridge, dtprocess, history
from scouter.errors import EXIT_CODES, BridgeTimeout, CommitTimeout, DarktableUnresponsive
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


def _procs(running=None, owned=None, raises=None):
    """Patch dtprocess so no test ever runs pgrep/ps against the host."""
    rp = (
        mock.patch.object(dtprocess, "running_pids", side_effect=raises)
        if raises
        else mock.patch.object(dtprocess, "running_pids", return_value=running or [])
    )
    return rp, mock.patch.object(dtprocess, "owned_pid", return_value=owned)


def _dead_bridge(**extra) -> FakeBridge:
    return FakeBridge(
        raises={
            "view.current": BridgeTimeoutError("t"),
            "darktable_mcp.diag": BridgeTimeoutError("t"),
            **extra,
        }
    )


class UnresponsiveTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_timeout_plus_dead_ping_is_typed_unresponsive(self) -> None:
        dtbridge.set_bridge(_dead_bridge())
        rp, op = _procs(running=[4242])
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        self.assertEqual(cm.exception.detail["last_method"], "view.current")
        self.assertEqual(EXIT_CODES["darktable_unresponsive"], 24)

    def test_unresponsive_detail_fields(self) -> None:
        dtbridge.set_bridge(_dead_bridge())
        rp, op = _procs(running=[4242])
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        d = cm.exception.detail
        self.assertEqual(d["pid"], 4242)
        self.assertIs(d["launched_by_scouter"], False)
        self.assertIsInstance(d["ping_ms"], int)
        rp, op = _procs(running=[4242, 77], owned=77)
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        self.assertEqual(cm.exception.detail["pid"], 77)
        self.assertIs(cm.exception.detail["launched_by_scouter"], True)

    def test_unresponsive_is_a_bridge_timeout_with_its_own_code(self) -> None:
        self.assertTrue(issubclass(DarktableUnresponsive, BridgeTimeout))
        self.assertEqual(DarktableUnresponsive.code, "darktable_unresponsive")

    def test_closed_darktable_stays_bridge_timeout(self) -> None:
        dtbridge.set_bridge(_dead_bridge())
        rp, op = _procs(running=[])
        with rp, op, self.assertRaises(BridgeTimeout) as cm:
            dtbridge.call("view.current")
        self.assertNotIsInstance(cm.exception, DarktableUnresponsive)
        self.assertEqual(cm.exception.code, "bridge_timeout")

    def test_pid_lookup_failure_counts_only_scouters_own_darktable(self) -> None:
        dtbridge.set_bridge(_dead_bridge())
        # (fix round 1 review H3d, ruling): `running_pids()` itself raising is
        # UNKNOWN, never proof darktable is not running -- conservative:
        # never collapses to the plain bridge_timeout `running == []` does.
        rp, op = _procs(raises=subprocess.TimeoutExpired("pgrep", 5))
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        self.assertIsNone(cm.exception.detail["pid"])
        self.assertIn("running_pids_error", cm.exception.detail)
        rp, op = _procs(raises=OSError("no pgrep"), owned=91)
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        self.assertEqual(cm.exception.detail["pid"], 91)

    def test_bridge_timeout_fallthrough_carries_the_pid_facts(self) -> None:
        """(fix round 1 review H3a): the ONE remaining fall-through --
        `owned is None` and a CONFIRMED empty `running == []` -- still names
        what the lookup saw, not just `{"method": ...}` (night 1's r11
        `bridge_timeout` gave no way to tell why)."""
        dtbridge.set_bridge(_dead_bridge())
        rp, op = _procs(running=[])
        with rp, op, self.assertRaises(BridgeTimeout) as cm:
            dtbridge.call("view.current")
        self.assertNotIsInstance(cm.exception, DarktableUnresponsive)
        self.assertEqual(cm.exception.detail["owned"], None)
        self.assertEqual(cm.exception.detail["running"], [])

    def test_timeout_with_live_ping_stays_bridge_timeout(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(
                raises={"view.current": BridgeTimeoutError("t")},
                responses={"darktable_mcp.diag": {"gui_ready": True}},
            )
        )
        with self.assertRaises(BridgeTimeout) as cm:
            dtbridge.call("view.current")
        self.assertNotIsInstance(cm.exception, DarktableUnresponsive)

    def test_a_wedged_darkroom_open_surfaces_unresponsive_not_bridge_timeout(self) -> None:
        """(night1-robustness-triage.md item 4, rob-r11): `dtbridge.call` is
        the one seam every verb's bridge call goes through (`darkroom.open`
        included) -- the same timeout-then-ping mapping `view.current` gets
        above must cover it too, so a genuinely wedged darktable is never
        left as the generic `bridge_timeout` ("start darktable...") a
        watchdog heal does not act on."""
        dtbridge.set_bridge(_dead_bridge(**{"darkroom.open": BridgeTimeoutError("t")}))
        rp, op = _procs(running=[20850], owned=20850)
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("darkroom.open", {"path": "/trial/P6281348.ORF"})
        self.assertEqual(cm.exception.code, "darktable_unresponsive")
        self.assertEqual(cm.exception.detail["last_method"], "darkroom.open")

    def test_diag_timeout_never_pings_itself(self) -> None:
        fake = FakeBridge(raises={"darktable_mcp.diag": BridgeTimeoutError("t")})
        dtbridge.set_bridge(fake)
        with self.assertRaises(BridgeTimeout):
            dtbridge.call("darktable_mcp.diag")
        self.assertEqual([m for m, _ in fake.calls], ["darktable_mcp.diag"])

    def test_doctor_ping_exit_codes(self) -> None:
        from scouter.cli import main as cli

        for state, code in (("alive", 0), ("starting", 0), ("unresponsive", 1)):
            with (
                mock.patch.object(dtbridge, "health", return_value={"state": state, "ping_ms": 3}),
                mock.patch.object(dtprocess, "running_pids", return_value=[4242]),
                mock.patch("builtins.print") as out,
            ):
                self.assertEqual(cli.main(["doctor", "--ping"]), code)
            self.assertEqual(json.loads(out.call_args[0][0])["watchdog"], state)

    def test_doctor_ping_with_no_darktable_reads_not_running(self) -> None:
        """Task 29 review carry-over: no process is not "unresponsive"."""
        from scouter.cli import main as cli

        with (
            mock.patch.object(
                dtbridge, "health", return_value={"state": "unresponsive", "ping_ms": 5000}
            ),
            mock.patch.object(dtprocess, "running_pids", return_value=[]),
            mock.patch("builtins.print") as out,
        ):
            self.assertEqual(cli.main(["doctor", "--ping"]), 1)
        self.assertEqual(json.loads(out.call_args[0][0])["watchdog"], "not_running")
        with (
            mock.patch.object(
                dtbridge, "health", return_value={"state": "unresponsive", "ping_ms": 5000}
            ),
            mock.patch.object(
                dtprocess, "running_pids", side_effect=subprocess.TimeoutExpired("pgrep", 5)
            ),
            mock.patch("builtins.print") as out,
        ):
            self.assertEqual(cli.main(["doctor", "--ping"]), 1)
        self.assertEqual(json.loads(out.call_args[0][0])["watchdog"], "unresponsive")


class DoctorWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _doctor_json(self, *patches) -> tuple[int, dict]:
        from scouter.cli import main as cli

        with mock.patch("builtins.print") as out:
            for p in patches:
                p.start()
            try:
                code = cli.main(["doctor"])
            finally:
                for p in patches:
                    p.stop()
        return code, json.loads(out.call_args[0][0])

    def test_watchdog_report_keys_when_alive(self) -> None:
        dtbridge.set_bridge(FakeBridge())
        code, report = self._doctor_json(*_procs(running=[4242]))
        for key in (
            "watchdog",
            "watchdog_line",
            "gui_ready",
            "gui_ready_line",
            "darktable_pid_line",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["watchdog"], "alive")
        self.assertEqual(report["darktable_pid_line"], "darktable pid: 4242 (user)")
        self.assertTrue(report["ok"])
        self.assertEqual(code, 0)

    def test_ok_is_false_when_the_ping_is_unresponsive(self) -> None:
        dtbridge.set_bridge(FakeBridge())
        unresponsive = mock.patch.object(
            dtbridge, "health", return_value={"state": "unresponsive", "ping_ms": 5000}
        )
        code, report = self._doctor_json(unresponsive, *_procs(running=[4242]))
        self.assertEqual(report["bridge"], "ok")
        self.assertEqual(report["watchdog"], "unresponsive")
        self.assertFalse(report["ok"])
        self.assertEqual(code, 1)

    def test_doctor_with_darktable_closed_exits_3_with_bridge_timeout(self) -> None:
        dtbridge.set_bridge(_dead_bridge())
        code, report = self._doctor_json(*_procs(running=[]))
        self.assertEqual(report["bridge"], "bridge_timeout")
        self.assertEqual(code, 3)
        self.assertEqual(report["darktable_pid_line"], "darktable pid: - (-)")

    def test_pgrep_failure_is_unknown_not_a_traceback(self) -> None:
        dtbridge.set_bridge(FakeBridge())
        code, report = self._doctor_json(*_procs(raises=subprocess.TimeoutExpired("pgrep", 5)))
        self.assertEqual(report["darktable_pid_line"], "darktable pid: unknown (pid lookup failed)")
        self.assertIn("config_path", report)  # _doctor_config still ran
        self.assertIn("backup_gate", report)
        self.assertEqual(code, 0)


class HistoryCommitWedgedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P5240151.ORF"
        self.raw.write_bytes(b"not really a raw")
        shutil.copy(FIXTURE, Path(str(self.raw) + ".xmp"))
        self.responses = {
            "darkroom.current": {
                "image_id": 12,
                "path": str(self.raw),
                "view": "darkroom",
                "source": "action_images",
            },
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
        }

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _commit(self):
        return history.commit(
            image=str(self.raw), timeout_s=0.5, expect_change=True, job="job-test"
        )

    def test_wedged_darktable_surfaces_unresponsive_not_commit_timeout(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(
                self.responses,
                raises={
                    "history.commit": BridgeTimeoutError("t"),
                    "darktable_mcp.diag": BridgeTimeoutError("t"),
                },
            )
        )
        rp, op = _procs(running=[4242])
        with rp, op, self.assertRaises(DarktableUnresponsive) as cm:
            self._commit()
        self.assertNotIsInstance(cm.exception, CommitTimeout)
        self.assertEqual(cm.exception.detail["last_method"], "history.commit")

    def test_slow_but_alive_darktable_is_still_commit_timeout(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(self.responses, raises={"history.commit": BridgeTimeoutError("t")})
        )
        rp, op = _procs(running=[4242])
        with rp, op, self.assertRaises(CommitTimeout):
            self._commit()
