"""Auto-heal (spec 6 item 4) with a fake heal: no darktable."""

import json
import unittest
from unittest import mock

from scouter import paths
from scouter.config import WatchdogConfig
from scouter.core import dtbridge, dtprocess, watchdog
from scouter.errors import DarktableUnresponsive
from tests.scouter_fakes import TempCache


class HealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()
        self.heals = []

    def tearDown(self) -> None:
        self.cache.stop()

    def _heal(self, outcome="healed"):
        def heal(job, budget):
            self.heals.append(job)
            return {"outcome": outcome, "ms": 5, "pid_old": 1, "pid_new": 2}
        return heal

    def _flaky(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise DarktableUnresponsive("wedged", detail={})
            return "ok"
        return fn, calls

    def test_off_by_default_raises_with_counters(self) -> None:
        fn, _ = self._flaky()
        with self.assertRaises(DarktableUnresponsive) as cm:
            watchdog.run_with_heal("darkroom.say", fn, "j1", cfg=WatchdogConfig(),
                                   heal=self._heal())
        self.assertEqual(cm.exception.detail["heals_max"], 1)
        self.assertEqual(self.heals, [])

    def test_one_heal_one_retry_and_telemetry(self) -> None:
        fn, calls = self._flaky()
        res, healed = watchdog.run_with_heal("darkroom.say", fn, "j1",
                                             cfg=WatchdogConfig(auto_heal=True),
                                             heal=self._heal())
        self.assertEqual((res, healed, len(calls)), ("ok", True, 2))
        self.assertEqual(watchdog.heals_used("j1"), 1)
        events = self.cache.events()
        self.assertEqual(events[-1]["verb"], "watchdog.heal")
        self.assertEqual(events[-1]["outcome"], "healed")

    def test_cap_reached_raises_typed(self) -> None:
        cfg = WatchdogConfig(auto_heal=True, max_heals_per_job=1)
        fn, _ = self._flaky()
        watchdog.run_with_heal("darkroom.say", fn, "j1", cfg=cfg, heal=self._heal())
        fn2, _ = self._flaky()
        with self.assertRaises(DarktableUnresponsive) as cm:
            watchdog.run_with_heal("darkroom.say", fn2, "j1", cfg=cfg, heal=self._heal())
        self.assertEqual(cm.exception.detail["heals_used"], 1)
        self.assertEqual(len(self.heals), 1)

    def test_job_run_and_import_are_never_retried(self) -> None:
        for verb in ("job.run", "library.import"):
            fn, calls = self._flaky()
            with self.assertRaises(DarktableUnresponsive) as cm:
                watchdog.run_with_heal(verb, fn, f"j-{verb}", cfg=WatchdogConfig(auto_heal=True),
                                       heal=self._heal())
            self.assertTrue(cm.exception.detail["healed"])
            self.assertEqual(len(calls), 1)

    def test_foreign_darktable_is_refused_not_healed(self) -> None:
        fn, calls = self._flaky()
        with self.assertRaises(DarktableUnresponsive):
            watchdog.run_with_heal("darkroom.say", fn, "j2", cfg=WatchdogConfig(auto_heal=True),
                                   heal=self._heal("refused_not_ours"))
        self.assertEqual(self.cache.events()[-1]["outcome"], "refused_not_ours")
        self.assertEqual(len(calls), 1)


# --- Beyond the brief's five: the real heal, registry and doctor wiring, all
# with dtprocess/dtbridge mocked. Nothing here launches or signals a process.

_REC = dtprocess.Launched(4242, "Sat Sep 26 10:00:00 2026", "/x/darktable", "/tmp/lib.db",
                          "/tmp/cfg", None)


class HealDarktableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()
        self.launch = mock.patch.object(dtprocess, "launch",
                                        return_value=dtprocess.Launched(
                                            4343, "t", "/x/darktable", "/tmp/lib.db",
                                            "/tmp/cfg", None))
        self.patches = [
            mock.patch.object(dtprocess, "read_owner", return_value=_REC),
            mock.patch.object(dtprocess, "owned_pid", return_value=4242),
            mock.patch.object(dtprocess, "terminate", return_value={"ok": True, "how": "SIGTERM"}),
            mock.patch.object(dtprocess, "clear_stale_locks", return_value=[]),
            mock.patch.object(dtprocess, "wait_gui_ready", return_value={"ready": True}),
            mock.patch.object(dtbridge, "call", return_value={}),
        ]
        self.mocks = {p.attribute: p.start() for p in self.patches}
        self.mocks["launch"] = self.launch.start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.cache.stop()

    def test_heals_owned_pid_without_osascript_and_reopens(self) -> None:
        paths.job_state_update("j1", lambda s: s.update(opened_path="/tmp/a.ORF"))
        res = watchdog.heal_darktable("j1", 60.0)
        self.assertEqual((res["outcome"], res["pid_old"], res["pid_new"]), ("healed", 4242, 4343))
        self.mocks["terminate"].assert_called_once_with(4242, responsive=False)
        self.mocks["launch"].assert_called_once_with(None, "/tmp/lib.db", "/tmp/cfg")
        self.mocks["wait_gui_ready"].assert_called_once_with(30.0)
        self.mocks["call"].assert_called_once_with("darkroom.open", {"path": "/tmp/a.ORF"},
                                                   timeout=30.0)

    def test_no_owned_pid_is_refused_and_nothing_is_touched(self) -> None:
        self.mocks["owned_pid"].return_value = None
        res = watchdog.heal_darktable("j1", 60.0)
        self.assertEqual(res["outcome"], "refused_not_ours")
        self.mocks["terminate"].assert_not_called()
        self.mocks["launch"].assert_not_called()

    def test_terminate_that_fails_never_relaunches(self) -> None:
        self.mocks["terminate"].return_value = {"ok": False, "how": "SIGKILL"}
        res = watchdog.heal_darktable("j1", 60.0)
        self.assertEqual(res["outcome"], "heal_failed")
        self.mocks["launch"].assert_not_called()

    def test_launch_error_is_heal_failed_not_a_crash(self) -> None:
        self.mocks["launch"].side_effect = dtprocess.LaunchError("no start time")
        res = watchdog.heal_darktable("j1", 60.0)
        self.assertEqual(res["outcome"], "heal_failed")

    def test_gui_never_ready_is_heal_failed(self) -> None:
        self.mocks["wait_gui_ready"].return_value = {"ready": False}
        self.assertEqual(watchdog.heal_darktable("j1", 10.0)["outcome"], "heal_failed")
        self.mocks["wait_gui_ready"].assert_called_once_with(10.0)

    def test_failed_heal_spends_the_budget(self) -> None:
        cfg = WatchdogConfig(auto_heal=True)
        fn, _ = HealTest._flaky(self)
        with self.assertRaises(DarktableUnresponsive):
            watchdog.run_with_heal("darkroom.say", fn, "j3", cfg=cfg,
                                   heal=lambda j, b: {"outcome": "heal_failed", "ms": 1,
                                                      "pid_old": 1, "pid_new": 2})
        self.assertEqual(watchdog.heals_used("j3"), 1)

    def test_plain_bridge_timeout_never_heals(self) -> None:
        from scouter.errors import BridgeTimeout

        def fn():
            raise BridgeTimeout("closed")
        heal = mock.Mock()
        with self.assertRaises(BridgeTimeout) as cm:
            watchdog.run_with_heal("darkroom.say", fn, "j4", cfg=WatchdogConfig(auto_heal=True),
                                   heal=heal)
        self.assertNotIsInstance(cm.exception, DarktableUnresponsive)
        heal.assert_not_called()


class WiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def test_config_section_parses_and_defaults_off(self) -> None:
        from scouter import config

        self.assertFalse(config.Config().watchdog.auto_heal)
        self.assertEqual(config.SECTIONS["watchdog"], ("watchdog", WatchdogConfig))

    def test_registry_reports_healed_in_data(self) -> None:
        from scouter import registry
        from scouter.types import Envelope

        verb_id = sorted(registry.load_verbs())[0]
        fake = mock.Mock()
        fake.to_dict.return_value = {"outcome": "ok"}
        with mock.patch.object(watchdog, "run_with_heal", return_value=(fake, True)):
            env = registry.call(verb_id, "core")
        self.assertIsInstance(env, Envelope)
        self.assertTrue(env.data["healed"])

    def test_doctor_heal_refuses_without_an_owned_darktable(self) -> None:
        from scouter.cli import main as cli

        with mock.patch.object(dtprocess, "read_owner", return_value=None), \
                mock.patch.object(dtprocess, "owned_pid", return_value=None), \
                mock.patch.object(dtprocess, "terminate") as term, \
                mock.patch("builtins.print") as out:
            self.assertEqual(cli.main(["doctor", "--heal"]), 1)
        term.assert_not_called()
        body = json.loads(out.call_args[0][0])
        self.assertEqual(body["outcome"], "refused_not_ours")
        self.assertEqual(self.cache.events()[-1]["verb"], "watchdog.heal")

    def test_doctor_reports_the_auto_heal_line(self) -> None:
        from scouter.cli import main as cli

        with mock.patch.object(dtbridge, "health", return_value={"state": "alive", "ping_ms": 1}), \
                mock.patch.object(dtprocess, "running_pids", return_value=[]), \
                mock.patch.object(dtprocess, "owned_pid", return_value=None), \
                mock.patch("builtins.print") as out:
            cli.main(["doctor"])
        report = json.loads(out.call_args[0][0])
        self.assertEqual(report["auto_heal_line"], "auto_heal: off, heals this job: 0/1")
