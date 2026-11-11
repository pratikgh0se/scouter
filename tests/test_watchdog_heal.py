"""Auto-heal (spec 6 item 4) with a fake heal: no darktable."""

import json
import os
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
            watchdog.run_with_heal(
                "darkroom.say", fn, "j1", cfg=WatchdogConfig(), heal=self._heal()
            )
        self.assertEqual(cm.exception.detail["heals_max"], 1)
        self.assertEqual(self.heals, [])

    def test_one_heal_one_retry_and_telemetry(self) -> None:
        fn, calls = self._flaky()
        res, healed = watchdog.run_with_heal(
            "darkroom.set", fn, "j1", cfg=WatchdogConfig(auto_heal=True), heal=self._heal()
        )
        self.assertEqual((res, healed, len(calls)), ("ok", True, 2))
        self.assertEqual(watchdog.heals_used("j1"), 1)
        events = self.cache.events()
        self.assertEqual(events[-1]["verb"], "watchdog.heal")
        self.assertEqual(events[-1]["outcome"], "healed")

    def test_cap_reached_raises_typed(self) -> None:
        cfg = WatchdogConfig(auto_heal=True, max_heals_per_job=1)
        fn, _ = self._flaky()
        watchdog.run_with_heal("darkroom.set", fn, "j1", cfg=cfg, heal=self._heal())
        fn2, _ = self._flaky()
        with self.assertRaises(DarktableUnresponsive) as cm:
            watchdog.run_with_heal("darkroom.set", fn2, "j1", cfg=cfg, heal=self._heal())
        self.assertEqual(cm.exception.detail["heals_used"], 1)
        self.assertEqual(len(self.heals), 1)

    def test_job_run_and_import_are_never_retried(self) -> None:
        for verb in ("job.run", "library.import"):
            fn, calls = self._flaky()
            with self.assertRaises(DarktableUnresponsive) as cm:
                watchdog.run_with_heal(
                    verb, fn, f"j-{verb}", cfg=WatchdogConfig(auto_heal=True), heal=self._heal()
                )
            self.assertTrue(cm.exception.detail["healed"])
            self.assertEqual(len(calls), 1)

    def test_foreign_darktable_is_refused_not_healed(self) -> None:
        fn, calls = self._flaky()
        with self.assertRaises(DarktableUnresponsive):
            watchdog.run_with_heal(
                "darkroom.say",
                fn,
                "j2",
                cfg=WatchdogConfig(auto_heal=True),
                heal=self._heal("refused_not_ours"),
            )
        self.assertEqual(self.cache.events()[-1]["outcome"], "refused_not_ours")
        self.assertEqual(len(calls), 1)


# --- Beyond the brief's five: the real heal, registry and doctor wiring, all
# with dtprocess/dtbridge mocked. Nothing here launches or signals a process.

_REC = dtprocess.Launched(
    4242, "Sat Sep 26 10:00:00 2026", "/x/darktable", "/tmp/lib.db", "/tmp/cfg", None
)


class HealDarktableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()
        self.launch = mock.patch.object(
            dtprocess,
            "launch",
            return_value=dtprocess.Launched(
                4343, "t", "/x/darktable", "/tmp/lib.db", "/tmp/cfg", None
            ),
        )
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
        res = watchdog.heal_darktable("j1", 60.0, clock=lambda: 0.0)
        self.assertEqual((res["outcome"], res["pid_old"], res["pid_new"]), ("healed", 4242, 4343))
        self.mocks["terminate"].assert_called_once_with(4242, responsive=False)
        self.mocks["launch"].assert_called_once_with(None, "/tmp/lib.db", "/tmp/cfg")
        self.mocks["wait_gui_ready"].assert_called_once_with(30.0)
        self.mocks["call"].assert_called_once_with(
            "darkroom.open", {"path": "/tmp/a.ORF"}, timeout=30.0
        )

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
        self.assertEqual(
            watchdog.heal_darktable("j1", 10.0, clock=lambda: 0.0)["outcome"], "heal_failed"
        )
        self.mocks["wait_gui_ready"].assert_called_once_with(10.0)

    def test_failed_heal_spends_the_budget(self) -> None:
        cfg = WatchdogConfig(auto_heal=True)
        fn, _ = HealTest._flaky(self)
        with self.assertRaises(DarktableUnresponsive):
            watchdog.run_with_heal(
                "darkroom.say",
                fn,
                "j3",
                cfg=cfg,
                heal=lambda j, b: {"outcome": "heal_failed", "ms": 1, "pid_old": 1, "pid_new": 2},
            )
        self.assertEqual(watchdog.heals_used("j3"), 1)

    def test_plain_bridge_timeout_never_heals(self) -> None:
        from scouter.errors import BridgeTimeout

        def fn():
            raise BridgeTimeout("closed")

        heal = mock.Mock()
        with self.assertRaises(BridgeTimeout) as cm:
            watchdog.run_with_heal(
                "darkroom.say", fn, "j4", cfg=WatchdogConfig(auto_heal=True), heal=heal
            )
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

        with (
            mock.patch.object(dtprocess, "read_owner", return_value=None),
            mock.patch.object(dtprocess, "owned_pid", return_value=None),
            mock.patch.object(dtprocess, "terminate") as term,
            mock.patch("builtins.print") as out,
        ):
            self.assertEqual(cli.main(["doctor", "--heal"]), 1)
        term.assert_not_called()
        body = json.loads(out.call_args[0][0])
        self.assertEqual(body["outcome"], "refused_not_ours")
        self.assertEqual(self.cache.events()[-1]["verb"], "watchdog.heal")

    def test_doctor_reports_the_auto_heal_line(self) -> None:
        from scouter.cli import main as cli

        with (
            mock.patch.object(dtbridge, "health", return_value={"state": "alive", "ping_ms": 1}),
            mock.patch.object(dtprocess, "running_pids", return_value=[]),
            mock.patch.object(dtprocess, "owned_pid", return_value=None),
            mock.patch("builtins.print") as out,
        ):
            cli.main(["doctor"])
        report = json.loads(out.call_args[0][0])
        self.assertEqual(report["auto_heal_line"], "auto_heal: off, heals this job: 0/1")


class FixRound1Test(unittest.TestCase):
    """Task 30 fix round 1: I1-I5. Everything mocked; no process is touched."""

    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()
        self.on = WatchdogConfig(auto_heal=True)

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.cache.stop()

    @staticmethod
    def _healed(job, budget):
        return {"outcome": "healed", "ms": 5, "pid_old": 1, "pid_new": 2}

    # I1 --------------------------------------------------------------
    def test_nudge_interrupted_during_commit_is_healed_but_not_resent(self) -> None:
        sent = []

        def nudge():
            sent.append("steps")  # the steps land, then the commit wedges
            raise DarktableUnresponsive("history.commit wedged", detail={})

        with self.assertRaises(DarktableUnresponsive) as cm:
            watchdog.run_with_heal("darkroom.nudge", nudge, "n1", cfg=self.on, heal=self._healed)
        self.assertEqual(sent, ["steps"])
        self.assertTrue(cm.exception.detail["healed"])
        self.assertEqual(cm.exception.detail["heals_used"], 1)

    def test_darkroom_set_is_retried(self) -> None:
        fn, calls = HealTest._flaky(self)
        res, healed = watchdog.run_with_heal(
            "darkroom.set", fn, "s1", cfg=self.on, heal=self._healed
        )
        self.assertEqual((res, healed, len(calls)), ("ok", True, 2))

    def test_retry_safe_allow_list(self) -> None:
        for verb in (
            "darkroom.set",
            "darkroom.enable",
            "darkroom.reset",
            "darkroom.open",
            "darkroom.get",
            "wb.kelvin",
            "history.read",
        ):
            self.assertTrue(watchdog.retry_safe(verb), verb)
        for verb in (
            "darkroom.nudge",
            "darkroom.say",
            "darkroom.adjust",
            "style.apply",
            "style.apply_set",
            "preset.apply",
            "preset.apply_job",
            "cull.apply",
            "export.run",
            "library.import",
            "job.run",
            "no.such_verb",
        ):
            self.assertFalse(watchdog.retry_safe(verb), verb)

    # I2 --------------------------------------------------------------
    def _heal_mocks(self, clock_steps):
        t = iter(clock_steps)
        rec = dtprocess.Launched(4242, "t", "/x/darktable", "/tmp/lib.db", None, None)
        mock.patch.object(dtprocess, "read_owner", return_value=rec).start()
        mock.patch.object(dtprocess, "owned_pid", return_value=4242).start()
        mock.patch.object(dtprocess, "terminate", return_value={"ok": True}).start()
        mock.patch.object(dtprocess, "clear_stale_locks", return_value=[]).start()
        mock.patch.object(
            dtprocess,
            "launch",
            return_value=dtprocess.Launched(4343, "t", "/x/darktable", "/tmp/lib.db", None, None),
        ).start()
        wait = mock.patch.object(dtprocess, "wait_gui_ready", return_value={"ready": True}).start()
        call = mock.patch.object(dtbridge, "call", return_value={}).start()
        return (lambda: next(t)), wait, call

    def test_heal_is_bounded_by_the_budget(self) -> None:
        # t0=0; terminate took 55 s of a 60 s budget: gui wait gets only 5 s.
        clock, wait, call = self._heal_mocks([0.0, 55.0, 55.0, 55.0, 57.0, 57.0, 57.0, 58.0, 58.0])
        paths.job_state_update("b1", lambda s: s.update(opened_path="/tmp/a.ORF"))
        res = watchdog.heal_darktable("b1", 60.0, clock=clock)
        self.assertEqual(res["outcome"], "healed")
        wait.assert_called_once_with(5.0)
        self.assertEqual(call.call_args.kwargs["timeout"], 3.0)

    def test_budget_exceeded_is_heal_failed_and_in_telemetry(self) -> None:
        clock, wait, _ = self._heal_mocks([0.0, 61.0, 61.0])
        res = watchdog.heal_darktable("b2", 60.0, clock=clock)
        self.assertEqual(res["outcome"], "heal_failed")
        self.assertTrue(res["budget_exceeded"])
        dtprocess.launch.assert_not_called()
        wait.assert_not_called()
        with self.assertRaises(DarktableUnresponsive):
            watchdog.run_with_heal(
                "darkroom.set", HealTest._flaky(self)[0], "b2", cfg=self.on, heal=lambda j, b: res
            )
        self.assertTrue(self.cache.events()[-1]["outcome"] == "heal_failed")
        self.assertTrue(self.cache.audit("b2")[-1]["detail"]["budget_exceeded"])

    # I3 --------------------------------------------------------------
    def test_watchdog_config_is_type_checked(self) -> None:
        from scouter.errors import UsageError

        for bad in (
            {"auto_heal": "false"},
            {"auto_heal": 1},
            {"max_heals_per_job": "1"},
            {"max_heals_per_job": True},
            {"max_heals_per_job": -1},
            {"heal_budget_s": "60"},
            {"heal_budget_s": 0},
            {"heal_budget_s": float("inf")},
        ):
            with self.assertRaises(UsageError, msg=bad):
                WatchdogConfig(**bad)
        WatchdogConfig(auto_heal=True, max_heals_per_job=0, heal_budget_s=30)

    def test_bad_config_file_is_a_usage_error_and_never_heals(self) -> None:
        from scouter import config
        from scouter.errors import UsageError

        cfg_file = self.cache.root / "config.json"
        cfg_file.write_text(json.dumps({"watchdog": {"auto_heal": "false"}}), encoding="utf-8")
        with self.assertRaises(UsageError):
            config.load(str(cfg_file))
        heal = mock.Mock()
        with (
            mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(cfg_file)}),
            self.assertRaises(DarktableUnresponsive),
        ):
            watchdog.run_with_heal("darkroom.set", HealTest._flaky(self)[0], "c1", heal=heal)
        heal.assert_not_called()

    # I4 --------------------------------------------------------------
    def test_failed_retry_still_reports_healed(self) -> None:
        from scouter.errors import BridgeTimeout

        def fn():
            raise (DarktableUnresponsive if not fn.n else BridgeTimeout)("x", detail={})

        fn.n = 0

        def heal(job, budget):
            fn.n = 1
            return self._healed(job, budget)

        with self.assertRaises(BridgeTimeout) as cm:
            watchdog.run_with_heal("darkroom.set", fn, "r1", cfg=self.on, heal=heal)
        self.assertTrue(cm.exception.detail["healed"])
        self.assertEqual(cm.exception.detail["heals_used"], 1)

    # I5 --------------------------------------------------------------
    def test_lock_unlink_error_is_heal_failed(self) -> None:
        self._heal_mocks([0.0] * 20)
        dtprocess.clear_stale_locks.side_effect = PermissionError("locked")
        res = watchdog.heal_darktable("l1", 60.0, clock=lambda: 0.0)
        self.assertEqual(res["outcome"], "heal_failed")
        self.assertIn("clear_stale_locks", res["error"])
        dtprocess.launch.assert_not_called()
