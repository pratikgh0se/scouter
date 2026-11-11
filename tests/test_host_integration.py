"""Exit criteria E1-E3: one hero ORF edited by a scripted conversation.

Host-only. Launches the real darktable with the file argument, waits for the
bridge, drives the darkroom verbs, reads values back from the XMP, undoes the
last step, and ALWAYS quits darktable (osascript, then SIGTERM, then SIGKILL).
It writes only the sidecar next to the EditingTrial ORF.

Gated on SCOUTER_HOST_TESTS=1 (controller ruling 17), same as
tests/test_commit_trigger_host.py -- this launches a real GUI app and drives
its darkroom, so it must never run by accident in CI or a plain `pytest tests`.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import pytest

DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
HERO = Path.home() / "Pictures/EditingTrial/P6281348.ORF"
PLUGIN_SRC = Path(__file__).resolve().parent.parent / "darktable_mcp" / "lua" / "darktable_mcp.lua"
INSTALLED_PLUGIN = Path.home() / ".config" / "darktable" / "lua" / "darktable_mcp.lua"

_MISSING = []
if os.environ.get("SCOUTER_HOST_TESTS") != "1":
    _MISSING.append(
        "SCOUTER_HOST_TESTS=1 not set (this launches darktable and edits the EditingTrial ORF)"
    )
if not DARKTABLE.is_file():
    _MISSING.append(f"darktable app ({DARKTABLE} not present)")
if not HERO.is_file():
    _MISSING.append(f"sample ORF ({HERO} not present)")
SKIP_REASON = (
    "host-only: missing " + ", ".join(_MISSING) + " -- the live darkroom verbs cannot be exercised"
    if _MISSING
    else ""
)


def _quit(proc: subprocess.Popen[bytes]) -> None:
    # The osascript call itself can hang past its own timeout (observed on
    # this Mac: a stuck AppleEvent leaves osascript blocked). subprocess.run's
    # timeout only stops the *wait*, not the risk of the exception escaping
    # before the SIGTERM/SIGKILL fallback ever runs -- so it must never be
    # allowed to abort this function. darktable must never be left running.
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "darktable" to quit'],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass
    for wait, sig in ((20, None), (10, signal.SIGTERM), (10, signal.SIGKILL)):
        if sig is not None:
            proc.send_signal(sig) if sig == signal.SIGTERM else proc.kill()
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


@unittest.skipUnless(not _MISSING, SKIP_REASON)
@pytest.mark.host
class HeroEditIntegrationTest(unittest.TestCase):
    """One test method: the conversation is a sequence, not independent cases."""

    maxDiff = None
    proc: subprocess.Popen[bytes] | None = None
    #: Seconds between `view.current` first reporting "darkroom" and the bridge
    #: confirming an image is loaded. Measured, not assumed.
    readiness_s: float | None = None

    # Cleanup bookkeeping: this machine had no luarc and no lua/ dir before
    # this test suite ever ran (task brief context). We install into the
    # real ~/.config/darktable (the plugin only loads at darktable start, so
    # there is no throwaway --configdir here) and must leave that directory
    # exactly as we found it once the run is done.
    _plugin_existed_before: bool = False
    _plugin_backup: bytes | None = None
    _luarc_existed_before: bool = False
    _luarc_line_added: bool = False
    _luarc_path: Path | None = None

    @classmethod
    def setUpClass(cls) -> None:
        # Two phases, each with its own unwind. `unittest` does NOT call
        # tearDownClass when setUpClass raises, so every failure path here has
        # to quit darktable and put ~/.config/darktable back itself (final
        # review, should-fix 1) -- otherwise a crash mid-setup leaves the real
        # darktable config carrying our plugin and a running GUI behind.
        cls._install_plugin()
        try:
            cls._launch_and_wait()
        except BaseException:
            if cls.proc is not None:
                _quit(cls.proc)
            cls._cleanup_plugin_install()
            cls._restore_job_env()
            raise

    @classmethod
    def _install_plugin(cls) -> None:
        """Install the working tree's plugin, atomically with its own restore.

        darktable only loads Lua at start, so this has to touch the REAL
        ~/.config/darktable (there is no throwaway --configdir that would
        still load the plugin). Every step records what it is about to change
        BEFORE changing it, and any failure unwinds everything recorded so
        far, so a half-finished install is never left behind.
        """
        try:
            INSTALLED_PLUGIN.parent.mkdir(parents=True, exist_ok=True)
            cls._plugin_existed_before = INSTALLED_PLUGIN.exists()
            if cls._plugin_existed_before:
                cls._plugin_backup = INSTALLED_PLUGIN.read_bytes()
            shutil.copy(PLUGIN_SRC, INSTALLED_PLUGIN)

            luarc = INSTALLED_PLUGIN.parent.parent / "luarc"
            cls._luarc_path = luarc
            line = 'require "darktable_mcp"\n'
            cls._luarc_existed_before = luarc.exists()
            if not luarc.exists() or line not in luarc.read_text():
                # Recorded before the write, so a failed append still unwinds.
                cls._luarc_line_added = True
                with luarc.open("a") as fh:
                    fh.write(line)
        except BaseException:
            cls._cleanup_plugin_install()
            raise

    @classmethod
    def _launch_and_wait(cls) -> None:
        # ONE job for the whole scripted conversation. `darkroom.undo_last`
        # undoes the newest write in the CURRENT job's journal, and
        # `scouter.paths.current_job()` derives a fresh id per process-second
        # unless $SCOUTER_JOB pins one -- so without this every registry.call
        # here would journal into its own job and undo_last would correctly
        # report "nothing to undo: journal is empty". A conversation is one
        # job; step 9 already assumes $SCOUTER_JOB is set.
        cls._job_before = os.environ.get("SCOUTER_JOB")
        os.environ["SCOUTER_JOB"] = f"job-hostint-{os.getpid()}-{int(time.time())}"

        cls.proc = subprocess.Popen(
            [str(DARKTABLE), str(HERO)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for the bridge AND the darkroom.
        from scouter.core import dtbridge

        deadline = time.monotonic() + 90
        last = "never answered"
        reached_darkroom = False
        while time.monotonic() < deadline:
            try:
                view = dtbridge.current_view()
            except Exception as e:  # bridge not up yet
                last = str(e)
            else:
                last = view
                if view == "darkroom":
                    reached_darkroom = True
                    break
            time.sleep(1.0)

        if not reached_darkroom:
            raise unittest.SkipTest(
                f"darktable did not reach the darkroom within 90s (last view: {last})"
            )

        # A REAL readiness check, replacing task 12's blind 25s settle.
        #
        # That settle was a misdiagnosis. Task 12 round 1 re-measured the
        # same window from inside a dt.control.dispatch coroutine
        # (spike/scouter_view_switch_probe.lua) and the darkroom pipe
        # answered a real `dt.gui.action` on the FIRST attempt, 0ms after
        # `view.current` first said "darkroom" -- so there is nothing to
        # sleep off. What actually failed in task 12 was the commit, for
        # an unrelated reason (write_sidecar_files; see the plugin's
        # comment block).
        #
        # So instead of a fixed sleep, ask the bridge the question the
        # test depends on -- is an image loaded in the darkroom? -- and
        # proceed as soon as it answers. `darkroom.current` is read-only:
        # it never selects, switches views, or imports.
        ready_started = time.monotonic()
        deadline = ready_started + 60
        detail = "never answered"
        while time.monotonic() < deadline:
            try:
                current = dtbridge.call("darkroom.current", timeout=10.0)
            except Exception as e:
                detail = str(e)
            else:
                if current.get("image_id") is not None:
                    cls.readiness_s = time.monotonic() - ready_started
                    print(
                        f"\ndarkroom readiness (real check, not a sleep): "
                        f"{cls.readiness_s:.2f}s -- {current}"
                    )
                    return
                detail = f"no image loaded yet: {current}"
            time.sleep(0.5)
        raise unittest.SkipTest(f"the darkroom never reported a loaded image within 60s ({detail})")

    @classmethod
    def _restore_job_env(cls) -> None:
        if getattr(cls, "_job_before", None) is None:
            os.environ.pop("SCOUTER_JOB", None)
        else:
            os.environ["SCOUTER_JOB"] = cls._job_before

    @classmethod
    def _cleanup_plugin_install(cls) -> None:
        """Leave ~/.config/darktable exactly as we found it (never left running,
        never left with an install we made -- this machine had no luarc and no
        lua/darktable_mcp.lua before this test suite)."""
        if cls._luarc_line_added and cls._luarc_path is not None:
            line = 'require "darktable_mcp"\n'
            if cls._luarc_existed_before:
                text = cls._luarc_path.read_text()
                cls._luarc_path.write_text(text.replace(line, "", 1))
            else:
                cls._luarc_path.unlink(missing_ok=True)
        if cls._plugin_existed_before and cls._plugin_backup is not None:
            INSTALLED_PLUGIN.write_bytes(cls._plugin_backup)
        elif not cls._plugin_existed_before:
            INSTALLED_PLUGIN.unlink(missing_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            # Ruling 16: commit history before quitting, so whatever the test
            # left mid-edit is flushed to the XMP rather than lost on quit.
            # Best-effort: darktable may already be unresponsive or gone.
            if cls.proc is not None and cls.proc.poll() is None:
                try:
                    from scouter import registry

                    registry.call("history.commit", "cli", image=str(HERO), timeout_s=10.0)
                except Exception:
                    pass
            if cls.proc is not None:
                _quit(cls.proc)
            assert (
                subprocess.run(["pgrep", "-f", "MacOS/darktable"], capture_output=True).returncode
                != 0
            ), "darktable is still running after _quit's full escalation"
        finally:
            cls._cleanup_plugin_install()
            cls._restore_job_env()

    def test_edits_one_orf_end_to_end(self) -> None:
        from scouter import registry
        from scouter.core import history

        raw = str(HERO)

        # 1. Open the hero in the darkroom.
        opened = registry.call("darkroom.open", "cli", image=raw)
        self.assertTrue(opened.ok, opened.to_dict())
        image_id = opened.data["image_id"]
        self.assertEqual(opened.data["view"], "darkroom")

        # 2. Exposure to an absolute +1.20 EV.
        exposure = registry.call(
            "darkroom.set", "cli", module="exposure", param="exposure", value=1.20
        )
        self.assertTrue(exposure.ok, exposure.to_dict())
        self.assertTrue(exposure.data["committed"])
        self.assertAlmostEqual(exposure.data["applied"], 1.20, places=3)
        print(
            f"\nLIVE set latency: darkroom.set exposure={1.20} -> {exposure.ms} ms "
            f"(applied {exposure.data['applied']}, committed "
            f"{exposure.data['committed']}); warnings={exposure.data.get('warnings')}"
        )
        # 3. Contrast through sigmoid.
        contrast = registry.call(
            "darkroom.set", "cli", module="sigmoid", param="contrast", value=1.35
        )
        self.assertTrue(contrast.ok, contrast.to_dict())
        self.assertAlmostEqual(contrast.data["applied"], 1.35, places=3)

        # 4. White balance.
        wb = registry.call(
            "darkroom.set", "cli", module="temperature", param="temperature", value=6000
        )
        self.assertTrue(wb.ok, wb.to_dict())

        # 5. E2: every step is in the XMP darktable itself wrote.
        read = history.read(image=image_id)
        self.assertTrue(read.confirmed)
        operations = [e.operation for e in read.entries if e.active]
        for module in ("exposure", "sigmoid", "temperature"):
            self.assertIn(module, operations, f"{module} missing from the history")
        exposure_entry = max(
            (e for e in read.entries if e.operation == "exposure" and e.active),
            key=lambda e: e.num,
        )
        assert exposure_entry.decoded is not None
        self.assertAlmostEqual(exposure_entry.decoded["exposure"], 1.20, places=3)
        print(
            f"LIVE readback from the XMP darktable wrote: history_end="
            f"{read.history_end}, active operations={sorted(set(operations))}, "
            f"exposure={exposure_entry.decoded['exposure']:.4f}"
        )

        # 6. The Scouter loop runs and fits the budget.
        stats = registry.call("scout.stats", "cli", image=raw, px=1024)
        self.assertTrue(stats.ok, stats.to_dict())
        self.assertEqual(stats.data["px_used"], 1024)
        # Ruling 29: the design target is 3.0s (docs/measurements.md measured
        # 1.96s in isolation), but this runs with the real darktable GUI alive
        # and contending for the machine. So the assertion is 4.0s and
        # anything over the 3.0s target is printed as a warning -- a slow loop
        # is visible in the output without turning GUI contention into a red
        # suite.
        self.assertLess(stats.ms, 4000, f"Scouter loop took {stats.ms} ms")
        if stats.ms > 3000:
            print(
                f"WARNING: Scouter loop {stats.ms} ms is over the 3000 ms design "
                f"target (under the 4000 ms assertion; GUI contention, ruling 29)"
            )
        self.assertEqual(len(stats.data["histogram"]["r"]), 256)
        print(f"LIVE scout.stats loop: {stats.ms} ms at {stats.data['px_used']} px")

        # 7. E3: undo the last step and verify it from the XMP.
        #
        # The white balance CANNOT be the verified undo, and this live run is
        # what proved it (task 12 fix round 1). `temperature.temperature` is a
        # Kelvin GUI slider with no counterpart in darktable's stored params:
        # the sidecar holds channel coefficients (coeff_r/g/b/g2 + preset, see
        # xmp.LAYOUTS[("temperature", 4)]), so the previous Kelvin value is
        # not recoverable from the XMP and `undo_last` refuses -- correctly,
        # and with exactly that reason. Assert the honest refusal first,
        # because a silent guess there would be the real bug...
        refused = registry.call("darkroom.undo_last", "cli")
        self.assertFalse(refused.ok, refused.to_dict())
        self.assertIn("temperature", refused.to_dict()["error"]["message"])
        self.assertIn("never readable from the XMP", refused.to_dict()["error"]["message"])
        print(f"LIVE honest refusal: {refused.to_dict()['error']['message']}")

        # ...then make one more edit that IS decodable and undo THAT, which is
        # what E3 actually asks for: an undo verified from the sidecar.
        again = registry.call("darkroom.set", "cli", module="sigmoid", param="contrast", value=1.10)
        self.assertTrue(again.ok, again.to_dict())
        undo = registry.call("darkroom.undo_last", "cli")
        self.assertTrue(undo.ok, undo.to_dict())
        self.assertEqual(undo.data["undone"]["module"], "sigmoid")
        self.assertAlmostEqual(undo.data["undone"]["restored_to"], 1.35, places=3)
        after = history.read(image=image_id)
        self.assertGreater(
            after.history_end, read.history_end, "undo must add a history item, not delete one"
        )
        readback = max(
            (e for e in after.entries if e.operation == "sigmoid" and e.active),
            key=lambda e: e.num,
        )
        assert readback.decoded is not None
        self.assertAlmostEqual(readback.decoded["contrast"], 1.35, places=3)
        print(
            f"LIVE undo verified from the XMP: undone={undo.data['undone']['module']}."
            f"{undo.data['undone']['param']} restored to "
            f"{undo.data['undone']['restored_to']:.4f} (XMP reads "
            f"{readback.decoded['contrast']:.4f}), verified="
            f"{undo.data['undone']['verified']}, history_end {read.history_end} -> "
            f"{after.history_end} in {undo.ms} ms"
        )

        # 7b. Commit latency on its own, measured after everything that
        # asserts a value.
        #
        # It has to follow an edit that is NOT committed and that genuinely
        # MOVES: darktable writes a sidecar only when the history actually
        # changes, so timing a commit with nothing to write measures a no-op.
        # (`exposure.black` is the wrong probe for this and the live run said
        # so: the black-level slider quantises, so a 1e-5 nudge lands on the
        # value already stored and produces no history item at all.) A
        # run-unique sigmoid contrast does move, every run.
        probe_value = 1.20 + (int(time.time()) % 11) * 0.01
        uncommitted = registry.call(
            "darkroom.set",
            "cli",
            module="sigmoid",
            param="contrast",
            value=probe_value,
            commit=False,
        )
        self.assertTrue(uncommitted.ok, uncommitted.to_dict())
        commit_only = registry.call("history.commit", "cli", image=raw)
        self.assertTrue(commit_only.ok, commit_only.to_dict())
        print(
            f"LIVE commit latency: history.commit -> {commit_only.ms} ms "
            f"(strategy {commit_only.data['strategy_used']}, view_after "
            f"{commit_only.data['view_after']}, committed "
            f"{commit_only.data['committed']})"
        )

        # 8. A gated verb still refuses without --yes, even mid-conversation.
        gated = registry.call("darkroom.reset", "cli", module="sigmoid")
        self.assertEqual(gated.outcome, "dry_run")

        # 9. The whole conversation is in the metrics and the audit log.
        from scouter import metrics, paths

        summary = metrics.summary(since="1h")
        verbs = {row["verb"] for row in summary.verbs}
        for verb_id in ("darkroom.open", "darkroom.set", "scout.stats", "darkroom.undo_last"):
            self.assertIn(verb_id, verbs)
        self.assertTrue(paths.events_path().exists())
        # A REAL assertion (rulings 14/15; the `or True` that used to end this
        # expression made it unfalsifiable): this conversation pinned
        # $SCOUTER_JOB in setUpClass, so the audit log for that exact job must
        # exist and must carry a line for every verb this test drove.
        job = os.environ["SCOUTER_JOB"]
        audit = paths.audit_path(job)
        self.assertTrue(audit.exists(), f"no audit log at {audit} for job {job}")
        audited = [
            json.loads(line)
            for line in audit.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        audited_verbs = {row["verb"] for row in audited}
        for verb_id in ("darkroom.open", "darkroom.set", "scout.stats", "darkroom.undo_last"):
            self.assertIn(verb_id, audited_verbs, f"{verb_id} is missing from {audit}")
        self.assertTrue(all(row["job"] == job for row in audited))
        print(
            f"LIVE audit log: {len(audited)} lines in {audit} "
            f"({len(audited_verbs)} distinct verbs)"
        )

    def test_colorbalancergb_slot_probe_pins_the_layout(self) -> None:
        """Task 5 step 5: prove the colorbalancergb float slot for `contrast`."""
        from scouter import registry
        from scouter.core import history, xmp

        raw = str(HERO)
        registry.call("darkroom.open", "cli", image=raw)
        registry.call("darkroom.enable", "cli", module="colorbalancergb")
        result = registry.call(
            "darkroom.set", "cli", module="colorbalancergb", param="contrast", value=0.2
        )
        if not result.ok:
            self.skipTest(f"colorbalancergb not reachable on this build: {result.outcome}")
        read = history.read(image=raw)
        entry = max(
            (e for e in read.entries if e.operation == "colorbalancergb"), key=lambda e: e.num
        )
        # This used to `shutil.copy` the live sidecar STRAIGHT OVER
        # tests/fixtures/colorbalancergb.xmp (final review, should-fix 4): a
        # host run silently rewrote the committed fixture that the unit suite
        # asserts against, so the fixture could never disagree with the host
        # and the unit test could not fail. The capture now goes to a
        # throwaway file, and the committed fixture is ASSERTED against
        # instead: same module, same modversion, still decodable.
        captured = Path(tempfile.gettempdir()) / f"scouter-cbrgb-{os.getpid()}.xmp"
        try:
            shutil.copy(xmp.sidecar_path(raw), captured)
            fixture = Path(__file__).resolve().parent / "fixtures" / "colorbalancergb.xmp"
            self.assertTrue(fixture.is_file(), f"the committed fixture {fixture} is missing")
            fixture_entry = max(
                (
                    e
                    for e in xmp.parse(fixture.read_text(encoding="utf-8")).entries
                    if e.operation == "colorbalancergb"
                ),
                key=lambda e: e.num,
            )
            self.assertEqual(
                fixture_entry.modversion,
                entry.modversion,
                f"this host writes colorbalancergb v{entry.modversion} but the committed "
                f"fixture is v{fixture_entry.modversion}: the pinned layout in "
                f"scouter/core/xmp.py covers a different version. The live sidecar was "
                f"captured at {captured} -- review it and update the fixture deliberately.",
            )
            assert fixture_entry.decoded is not None
            self.assertIn("contrast", fixture_entry.decoded)
            print(
                f"colorbalancergb fixture agrees with this host: v{entry.modversion}, "
                f"fixture contrast={fixture_entry.decoded['contrast']:.4f}"
            )
        finally:
            captured.unlink(missing_ok=True)
        assert entry.decoded is not None
        # Task 12 PINNED this layout (xmp.LAYOUTS[("colorbalancergb", 5)] ->
        # named_slots, slot 31 of 33 is `contrast`), so the decoder now hands
        # back the named field and no longer exposes the raw float vector.
        # Assert the named value; fall back to the slot scan only for a
        # modversion whose layout is not pinned yet, which is what that scan
        # exists to discover.
        if "contrast" in entry.decoded:
            print(
                f"colorbalancergb v{entry.modversion}: pinned named layout decoded "
                f"contrast={entry.decoded['contrast']:.4f}"
            )
            self.assertAlmostEqual(entry.decoded["contrast"], 0.2, places=3)
        else:
            floats = entry.decoded.get("floats", [])
            matches = [i for i, v in enumerate(floats) if abs(v - 0.2) < 1e-4]
            print(
                f"colorbalancergb v{entry.modversion}: 0.2 found at float slots {matches} "
                f"of {len(floats)}; pin the named layout in scouter/core/xmp.py LAYOUTS"
            )
            self.assertTrue(matches, "the value set through the bridge is not in the params")


if __name__ == "__main__":
    unittest.main()
