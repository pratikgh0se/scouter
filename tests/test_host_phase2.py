"""Task 7 step 2: pin the `.dtstyle` file format against a real darktable.

Host-only, same gating and lifecycle discipline as
`tests/test_host_integration.py` (controller ruling 17): SCOUTER_HOST_TESTS=1
or the whole class is skipped with the reason printed, launches the real
darktable exactly once, waits for the bridge with a real readiness check
(never a blind sleep), and ALWAYS quits darktable in `tearDownClass` --
osascript, then SIGTERM, then SIGKILL -- restoring `~/.config/darktable`
to what it was before.

This is design section 9.2's whole point: the `.dtstyle` schema documented
there is an UNVERIFIED claim about another project's file format. This test
creates a style with the existing `style.create_from_current`, exports it
with the new `style.export_file` bridge verb (a thin wrapper over
`dt.styles.export`), and captures the real file darktable itself wrote.
That capture is committed as `tests/fixtures/hero.dtstyle` -- deliberately,
by hand, once this run is reviewed -- and `scouter/core/dtstyle.py`'s writer
is asserted against ITS element names and nesting (`tests/test_style_dtstyle.py`),
never against section 9.2's prose.

What this run writes, honestly (fix round 1, should-fix 10 -- an earlier
draft of this docstring claimed `tests/fixtures/` was untouched, which was
not true): the EditingTrial ORF's `.xmp` sidecar, throwaway temp/cache
directories, AND `tests/fixtures/hero.dtstyle` itself -- this test stages
the real capture straight to that committed path (the same discipline
`test_host_integration.py`'s colorbalancergb probe used before final
review's should-fix 4 moved IT to a throwaway file; this one still writes
the fixture path directly because, unlike that probe, there is no separate
unit assertion this file could silently stop catching -- the stage is
reviewed via this test's own printed capture and `git diff` before it is
ever committed). Every style this test creates in darktable's own library
(`scouter-probe-*`, `scouter-probe-filtered-*`) is deleted again via the new
`style.delete` bridge verb before the run ends, in a `finally`, so a host
run never leaves throwaway styles behind in the user's real darktable
library. `Phase2EndToEndTest`'s own `scouter-e2e-hero-<ts>` style is deleted
the same way (fix round 1, blocker 2 -- it was not, and two green runs left
two styles behind in `data.db`; its name also had to gain the `scouter-`
prefix that `style.delete`'s own safety check requires).

**What a host run in this file MUTATES and does NOT restore: darktable's
`library.db`.** `Phase2EndToEndTest` imports a 30-frame film roll into it
and `cull.apply` writes ratings; the WB and dtstyle probes add the scratch
ORF. Scouter may not write `library.db` (the phase-1 invariant), so there is
nothing here that could put it back, and pretending otherwise would be worse
than saying so. Two consequences worth knowing: darktable remembers a path's
rating across a delete and re-import (which is why the E2E job slug carries
the pid), and the film rolls these runs create stay in the user's library
until they remove them in the GUI.

What IS restored: the Lua plugin and `luarc` (via `_PluginInstall`, from an
on-disk backup under `~/Pictures/ScouterScratch/dtconfig-backup-<ts>/` taken
before the first write, so a crash between install and teardown is
recoverable by hand), every environment variable each class sets, and every
style each class creates.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest import mock

DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
EDITING_TRIAL = Path("/Users/pratik/Pictures/EditingTrial")
HERO = EDITING_TRIAL / "P6281348.ORF"
PLUGIN_SRC = Path(__file__).resolve().parent.parent / "darktable_mcp" / "lua" / "darktable_mcp.lua"
INSTALLED_PLUGIN = Path.home() / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
CAPTURED_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

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
    "host-only: missing " + ", ".join(_MISSING) + " -- the .dtstyle format cannot be pinned"
    if _MISSING
    else ""
)


class _PluginInstall:
    """Install this repo's Lua plugin over the user's, restorably.

    Fix round 1, minor g: every host class here had its own copy of this,
    each keeping the user's plugin bytes in a CLASS ATTRIBUTE -- so a hard
    kill, a power loss or a pytest crash between install and teardown left
    Scouter's development plugin installed in the user's real
    `~/.config/darktable` with no way to get theirs back. That is exactly
    what the backup existed to prevent.

    The copy now goes to `~/Pictures/ScouterScratch/dtconfig-backup-<ts>/`
    BEFORE anything is overwritten, `restore()` reads from that file rather
    than from memory, and the directory survives a crash (it is deleted only
    after a successful restore) so a human can undo it by hand from a path
    its own MANIFEST.txt names.

    **What is NOT backed up, and NOT restored: darktable's `library.db`.**
    A host run imports a film roll into it and writes ratings, and Scouter
    may not write `library.db` itself, so there is nothing here that could
    put it back. Stated rather than hidden.
    """

    def __init__(self) -> None:
        self.backup_dir: Path | None = None
        self.plugin_existed_before = False
        self.luarc_existed_before = False
        self.luarc_line_added = False
        self.luarc_path: Path | None = None

    def install(self) -> None:
        # FIRST, before any mkdir (fix round 2, minor m1): `install`'s own
        # `except BaseException` calls `restore()`, and `restore()` UNLINKS
        # the plugin when `plugin_existed_before` is False. Reading these
        # flags after a mkdir meant a failing mkdir left them at their
        # `False` defaults and the restore then deleted the user's real
        # plugin -- the cleanup destroying exactly what the backup exists to
        # protect. Two `exists()` calls, no side effects, nothing before them.
        luarc = INSTALLED_PLUGIN.parent.parent / "luarc"
        self.luarc_path = luarc
        self.plugin_existed_before = INSTALLED_PLUGIN.exists()
        self.luarc_existed_before = luarc.exists()

        try:
            INSTALLED_PLUGIN.parent.mkdir(parents=True, exist_ok=True)
            SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
            # The pid is in the name (minor m2) so two host classes running
            # inside the same second -- or two pytest processes -- cannot
            # share a backup directory and overwrite each other's copy of the
            # user's plugin.
            self.backup_dir = SCRATCH_DIR / (
                f"dtconfig-backup-{time.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
            )
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            # Everything needed to undo this, on disk, before the first write.
            if self.plugin_existed_before:
                (self.backup_dir / "darktable_mcp.lua").write_bytes(INSTALLED_PLUGIN.read_bytes())
            if self.luarc_existed_before:
                (self.backup_dir / "luarc").write_bytes(luarc.read_bytes())
            (self.backup_dir / "MANIFEST.txt").write_text(
                "Scouter host test backup of the user's darktable Lua config.\n"
                f"taken_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                f"plugin_path: {INSTALLED_PLUGIN}\n"
                f"plugin_existed_before: {self.plugin_existed_before}\n"
                f"luarc_path: {luarc}\n"
                f"luarc_existed_before: {self.luarc_existed_before}\n"
                "\n"
                "If this directory still exists, a test did not finish its\n"
                "restore. To undo by hand: copy darktable_mcp.lua and luarc\n"
                "back to the paths above, deleting them instead if the\n"
                "matching *_existed_before line says false.\n"
                "\n"
                "NOT backed up and NOT restored: darktable's library.db. A\n"
                "host run imports a film roll into it and writes ratings.\n",
                encoding="utf-8",
            )

            shutil.copy(PLUGIN_SRC, INSTALLED_PLUGIN)

            line = 'require "darktable_mcp"\n'
            if not luarc.exists() or line not in luarc.read_text():
                self.luarc_line_added = True
                with luarc.open("a") as fh:
                    fh.write(line)
        except BaseException:
            self.restore()
            raise

    def restore(self) -> None:
        backup = self.backup_dir
        try:
            if self.luarc_path is not None:
                saved_luarc = (backup / "luarc") if backup is not None else None
                if self.luarc_existed_before:
                    if saved_luarc is not None and saved_luarc.is_file():
                        self.luarc_path.write_bytes(saved_luarc.read_bytes())
                    elif self.luarc_line_added:
                        # No backup file to fall back on: strip just the line
                        # this install appended, which is all it changed.
                        line = 'require "darktable_mcp"\n'
                        text = self.luarc_path.read_text()
                        self.luarc_path.write_text(text.replace(line, "", 1))
                elif self.luarc_line_added:
                    self.luarc_path.unlink(missing_ok=True)

            saved_plugin = (backup / "darktable_mcp.lua") if backup is not None else None
            if self.plugin_existed_before:
                if saved_plugin is not None and saved_plugin.is_file():
                    INSTALLED_PLUGIN.write_bytes(saved_plugin.read_bytes())
            else:
                INSTALLED_PLUGIN.unlink(missing_ok=True)
        except OSError as e:
            print(
                "WARNING: could not restore the darktable Lua config; the backup is at "
                f"{backup} and its MANIFEST.txt says how to undo this by hand: {e}"
            )
            return
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        self.backup_dir = None


def _quit(proc: subprocess.Popen[bytes]) -> None:
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
class PinDtstyleFormatTest(unittest.TestCase):
    """One test method: install -> launch -> probe -> capture -> quit -> restore."""

    maxDiff = None
    proc: subprocess.Popen[bytes] | None = None

    #: The on-disk-backed plugin install (see `_PluginInstall`).
    _install: _PluginInstall | None = None
    _job_before: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
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
        cls._install = _PluginInstall()
        cls._install.install()

    @classmethod
    def _launch_and_wait(cls) -> None:
        cls._job_before = os.environ.get("SCOUTER_JOB")
        os.environ["SCOUTER_JOB"] = f"job-hostp2-{os.getpid()}-{int(time.time())}"

        cls.proc = subprocess.Popen(
            [str(DARKTABLE), str(HERO)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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

        # A real readiness check (task 12's precedent): ask the bridge
        # whether an image is actually loaded, rather than sleeping a fixed
        # window.
        deadline = time.monotonic() + 60
        detail = "never answered"
        while time.monotonic() < deadline:
            try:
                current = dtbridge.call("darkroom.current", timeout=10.0)
            except Exception as e:
                detail = str(e)
            else:
                if current.get("image_id") is not None:
                    return
                detail = f"no image loaded yet: {current}"
            time.sleep(0.5)
        raise unittest.SkipTest(f"the darkroom never reported a loaded image within 60s ({detail})")

    @classmethod
    def _restore_job_env(cls) -> None:
        if cls._job_before is None:
            os.environ.pop("SCOUTER_JOB", None)
        else:
            os.environ["SCOUTER_JOB"] = cls._job_before

    @classmethod
    def _cleanup_plugin_install(cls) -> None:
        if cls._install is not None:
            cls._install.restore()
            cls._install = None

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls.proc is not None and cls.proc.poll() is None:
                try:
                    from scouter import registry

                    registry.call("history.commit", "cli", image=str(HERO), timeout_s=10.0)
                except Exception:
                    pass
            if cls.proc is not None:
                _quit(cls.proc)
                # By PID, not `pgrep -f MacOS/darktable` (fix round 1, minor
                # c): that pattern also matches a darktable the USER has open
                # in another window, so it failed runs that had cleaned up
                # perfectly -- and it would have passed while OUR instance
                # survived under a different match. Ruling 14 wants a real
                # assertion about the instance this class launched, and
                # `poll()` is exactly that question.
                assert cls.proc.poll() is not None, (
                    f"the darktable this test launched (pid {cls.proc.pid}) is still "
                    "running after _quit's full escalation"
                )
        finally:
            cls._cleanup_plugin_install()
            cls._restore_job_env()

    def test_pin_dtstyle_format(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge

        raw = str(HERO)

        opened = registry.call("darkroom.open", "cli", image=raw)
        self.assertTrue(opened.ok, opened.to_dict())

        # `call(verb_id, layer, **kwargs)` -- ONE verb id. This read
        # `registry.call("darkroom.open", "darkroom.set", "cli", ...)`, three
        # positionals, which is a TypeError: the class could never run.
        exposure = registry.call(
            "darkroom.set", "cli", module="exposure", param="exposure", value=0.5
        )
        self.assertTrue(exposure.ok, exposure.to_dict())
        self.assertTrue(exposure.data["committed"])

        # Both names are fixed BEFORE either style is created (not returned
        # from the helpers that create them), so an assertion failure
        # partway through either helper still leaves the `finally` below
        # able to find and delete whatever was actually created.
        style_name = f"scouter-probe-{os.getpid()}-{int(time.time())}"
        filtered_name = f"scouter-probe-filtered-{os.getpid()}-{int(time.time())}"
        try:
            self._probe_body(raw, style_name)
            self._probe_filtered_body(raw, filtered_name)
        finally:
            # Fix round 1, should-fix 10: never leave throwaway styles behind
            # in the user's real darktable library, success or failure.
            for name in (style_name, filtered_name):
                try:
                    dtbridge.call("style.delete", {"name": name, "confirm": True})
                except Exception as e:
                    print(f"WARNING: cleanup could not delete style {name!r}: {e}")

    def _probe_body(self, raw: str, style_name: str) -> None:
        from scouter.core import dtbridge

        created = dtbridge.call(
            "style.create_from_current",
            {"name": style_name, "modules": [], "description": "phase 2 host probe"},
        )
        self.assertTrue(created.get("created"))

        with tempfile.TemporaryDirectory(prefix="scouter-dtstyle-probe-") as tmp:
            exported = dtbridge.call("style.export_file", {"name": style_name, "dir": tmp})
            self.assertTrue(exported.get("exported"))
            exported_path = Path(exported["path"])
            self.assertTrue(exported_path.is_file(), f"{exported_path} was not written")
            self.assertGreater(exported.get("bytes", 0), 0)

            text = exported_path.read_text(encoding="utf-8")
            print("\n----- real darktable .dtstyle (captured, not yet committed) -----")
            print(text)
            print("----- end capture -----")

            root = ET.fromstring(text)
            info = root.find("info")
            style_el = root.find("style")
            plugins = style_el.findall("plugin") if style_el is not None else []
            first_plugin_children = [c.tag for c in plugins[0]] if plugins else []
            has_iop_order = any(p.find("iop_order") is not None for p in plugins)

            print(
                "\nSCHEMA PROBE RESULT:\n"
                f"  root tag: {root.tag!r}, version attr: {root.get('version')!r}\n"
                f"  <info> children (in order): {[c.tag for c in (info or [])]}\n"
                f"  <plugin> count: {len(plugins)}\n"
                f"  first <plugin> children (in order): {first_plugin_children}\n"
                f"  <iop_order> present: {has_iop_order}\n"
            )

            # This is the pin: copy the captured file to the committed
            # fixture path so the next step of this task can commit it
            # deliberately (a host run must never silently overwrite a
            # reviewed fixture -- final review should-fix 4 -- so this test
            # only stages it under the real fixtures/ path for `git add`,
            # exactly once, under explicit review of this run's output).
            CAPTURED_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
            staged = CAPTURED_FIXTURE_DIR / "hero.dtstyle"
            staged.write_text(text, encoding="utf-8")
            print(f"staged fixture at {staged} -- review the printed capture above, then commit it")

    def _probe_filtered_body(self, raw: str, filtered_name: str) -> None:
        """Phase 2: the writer this probe pins (scouter/core/dtstyle.py) built
        AFTER seeing the capture above -- exercised live, against the real
        `dt.styles.import`, in the SAME darktable session (one launch for
        the whole task, per the host-test discipline). This is the thing
        design section 9.2 actually asked for: "if darktable imports the
        style ... the omission [of <iop_order>/a real <iop_list>] is fine".

        `filtered_name` is chosen by the caller BEFORE this runs (not
        generated and returned here), so its `finally` can delete it even if
        an assertion below fails partway through.
        """
        from scouter import registry
        from scouter.core import dtbridge
        from scouter.core import dtstyle as dtstyle_module

        # Fix round 2, item 3: a MULTI-plugin request, not just one module --
        # this is what actually exercises the dense 0..N-1 renumbering
        # (scouter/core/dtstyle.py's build()) across more than a single
        # <plugin>, in XMP history order, round-tripped through the real
        # dt.styles.import.
        requested_modules = ["exposure", "temperature", "colorin"]
        filtered = registry.call(
            "style.extract",
            "cli",
            image=raw,
            name=filtered_name,
            modules=requested_modules,
            description="phase 2 filtered probe (multi-plugin renumbering)",
        )
        self.assertTrue(filtered.ok, filtered.to_dict())
        self.assertTrue(filtered.data["modules_honoured"])
        self.assertEqual(sorted(filtered.data["operations_included"]), sorted(requested_modules))
        dtstyle_path = filtered.data["dtstyle_path"]
        self.assertTrue(dtstyle_path and Path(dtstyle_path).is_file())
        written_ops = dtstyle_module.read_operations(dtstyle_path)
        self.assertEqual(sorted(written_ops), sorted(requested_modules))
        self.assertGreater(len(written_ops), 1, "this is the multi-plugin case, not a single one")

        # Dense 0..N-1 renumbering, in XMP history order (fix round 2, item
        # 3's wording correction): <num> must be exactly 0..N-1 with no gaps,
        # regardless of what `num` those entries carried in the source XMP.
        written_root = ET.fromstring(Path(dtstyle_path).read_text(encoding="utf-8"))
        nums = [p.findtext("num") for p in written_root.find("style").findall("plugin")]
        self.assertEqual(
            nums,
            [str(i) for i in range(len(nums))],
            f"<num> must be dense 0..{len(nums) - 1}, got {nums}",
        )
        print(f"\nRENUMBERING CHECK: {len(nums)} plugins, <num> sequence = {nums} (dense 0..N-1)")

        # The real proof: darktable's own dt.styles now lists a style under
        # this name -- style.import_file (dt.styles.import) accepted our
        # writer's empty <iop_list>, not just our own read-back of the file
        # we wrote.
        after_import = dtbridge.call("list_styles", {})
        names = [s.get("name") for s in after_import.get("styles", [])]
        self.assertIn(
            filtered_name,
            names,
            "the filtered .dtstyle (writer output, empty <iop_list>) did not "
            "actually import into darktable",
        )
        print(
            f"\nLIVE filtered style.extract: modules_honoured="
            f"{filtered.data['modules_honoured']}, operations_included="
            f"{filtered.data['operations_included']}, dtstyle_path={dtstyle_path}\n"
            f"CONFIRMED: dt.styles.import accepted the writer's empty <iop_list> "
            f"(style {filtered_name!r} is now in dt.styles)"
        )


#: task 11 step 3: candidate `dt.gui.action` paths for the temperature
#: module's coefficient sliders -- exactly the brief's own list. Tried in
#: this order; `darkroom.status` on each records `nan`/non-`nan` (never a
#: value -- design 7.3), which is what tells the pin apart from a guess.
WB_SLIDER_CANDIDATES: tuple[str, ...] = (
    "iop/temperature/red",
    "iop/temperature/green",
    "iop/temperature/blue",
    "iop/temperature/emerald",
    "iop/temperature/channel coefficients/red",
    "iop/temperature/channel coefficients/green",
    "iop/temperature/channel coefficients/blue",
    "iop/temperature/channel coefficients/emerald",
    "iop/temperature/scene illuminant temp",
    "iop/temperature/settings",
)
#: task 11, host-confirmed (docs/measurements-generated.md, two runs): the
#: FIRST candidate this list ever reaches wedges darktable's Lua worker for
#: the rest of that session, regardless of which candidate is first (both
#: "iop/temperature/red" and "iop/temperature/channel coefficients/red"
#: reproduced it independently) -- `dt.gui.action` on an unrecognised path
#: under this module does not fail gracefully on darktable 5.4.1. Tier 2
#: (design 11.3, the coefficient-slider undo) is not available on this
#: host; see docs/measurements-generated.md and the task 11 report for the
#: full record.

#: The two candidate NAMING SCHEMES that would pin all three always-finite
#: coefficients (red/green/blue), tried in this order.
WB_SLIDER_SCHEMES: tuple[tuple[str, str, str], ...] = (
    ("iop/temperature/red", "iop/temperature/green", "iop/temperature/blue"),
    (
        "iop/temperature/channel coefficients/red",
        "iop/temperature/channel coefficients/green",
        "iop/temperature/channel coefficients/blue",
    ),
)

SCRATCH_DIR = Path.home() / "Pictures" / "ScouterScratch"
SCRATCH_ORF = SCRATCH_DIR / "em10iii.ORF"
MEASUREMENTS_GENERATED = (
    Path(__file__).resolve().parent.parent / "docs" / "measurements-generated.md"
)
COMMITTED_TABLE = Path(__file__).resolve().parent / "fixtures" / "wb-em10iii.json"


@unittest.skipUnless(not _MISSING, SKIP_REASON)
class WhiteBalanceCalibrationHostTest(unittest.TestCase):
    """Task 11 step 7: measure the real per-camera Kelvin table, same
    install/launch/quit/restore discipline as `PinDtstyleFormatTest`, run
    against a SCRATCH COPY of the EditingTrial ORF, never the original.

    Kept in its OWN darktable launch, separate from the slider probe below
    (`PinWbCoefficientSlidersHostTest`): a host run proved that sending
    `dt.gui.action` an unrecognised path (exactly what the slider probe does,
    on purpose, to discover which candidates are real) can leave darktable's
    Lua worker permanently unresponsive to every later bridge call in that
    SAME session -- so calibration, which only ever uses the already-proven
    `iop/temperature/temperature` path, must not share a session with the
    probe's guesses.
    """

    maxDiff = None
    proc: subprocess.Popen[bytes] | None = None

    #: The on-disk-backed plugin install (see `_PluginInstall`).
    _install: _PluginInstall | None = None
    _job_before: str | None = None
    _config_before: str | None = None
    _config_tmp: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERO, SCRATCH_ORF)
        # Never touch the original's sidecar: a scratch copy starts with none.
        Path(str(SCRATCH_ORF) + ".xmp").unlink(missing_ok=True)

        # A pinned camera slug for the duration of this run (task 11's own
        # committed-fixture naming convention, "em10iii" -- matching
        # tests/fixtures/wb-em10iii.json, tests/test_outliers.py, etc.),
        # independent of whatever (if anything) `~/.config/scouter/config.json`
        # says on this machine.
        cls._config_before = os.environ.get("SCOUTER_CONFIG")
        cls._config_tmp = tempfile.TemporaryDirectory(prefix="scouter-wb-host-config-")
        config_path = Path(cls._config_tmp.name) / "config.json"
        config_path.write_text(
            '{"version": 1, "cameras": {"E-M10 Mark III": "em10iii"}}', encoding="utf-8"
        )
        os.environ["SCOUTER_CONFIG"] = str(config_path)

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
        cls._install = _PluginInstall()
        cls._install.install()

    @classmethod
    def _launch_and_wait(cls) -> None:
        cls._job_before = os.environ.get("SCOUTER_JOB")
        os.environ["SCOUTER_JOB"] = f"job-wbhost-{os.getpid()}-{int(time.time())}"

        cls.proc = subprocess.Popen(
            [str(DARKTABLE), str(SCRATCH_ORF)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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

        deadline = time.monotonic() + 60
        detail = "never answered"
        while time.monotonic() < deadline:
            try:
                current = dtbridge.call("darkroom.current", timeout=10.0)
            except Exception as e:
                detail = str(e)
            else:
                if current.get("image_id") is not None:
                    return
                detail = f"no image loaded yet: {current}"
            time.sleep(0.5)
        raise unittest.SkipTest(f"the darkroom never reported a loaded image within 60s ({detail})")

    @classmethod
    def _restore_job_env(cls) -> None:
        if cls._job_before is None:
            os.environ.pop("SCOUTER_JOB", None)
        else:
            os.environ["SCOUTER_JOB"] = cls._job_before
        if cls._config_before is None:
            os.environ.pop("SCOUTER_CONFIG", None)
        else:
            os.environ["SCOUTER_CONFIG"] = cls._config_before
        if cls._config_tmp is not None:
            cls._config_tmp.cleanup()

    @classmethod
    def _cleanup_plugin_install(cls) -> None:
        if cls._install is not None:
            cls._install.restore()
            cls._install = None

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls.proc is not None and cls.proc.poll() is None:
                try:
                    from scouter import registry

                    registry.call("history.commit", "cli", image=str(SCRATCH_ORF), timeout_s=10.0)
                except Exception:
                    pass
            if cls.proc is not None:
                _quit(cls.proc)
                # By PID, not `pgrep -f MacOS/darktable` (fix round 1, minor
                # c): that pattern also matches a darktable the USER has open
                # in another window, so it failed runs that had cleaned up
                # perfectly -- and it would have passed while OUR instance
                # survived under a different match. Ruling 14 wants a real
                # assertion about the instance this class launched, and
                # `poll()` is exactly that question.
                assert cls.proc.poll() is not None, (
                    f"the darktable this test launched (pid {cls.proc.pid}) is still "
                    "running after _quit's full escalation"
                )
        finally:
            cls._cleanup_plugin_install()
            cls._restore_job_env()

    def test_calibrate_measures_the_real_table(self) -> None:
        """Step 7: `wb.calibrate` on the scratch copy, real darktable, real
        commits. Writes `~/.config/scouter/wb/em10iii.json`; this test then
        copies that file to `tests/fixtures/wb-em10iii.json` (staged for
        review, same discipline `PinDtstyleFormatTest` uses for
        `hero.dtstyle`) -- committed by hand once the sweep is reviewed."""
        from scouter import registry

        opened = registry.call("darkroom.open", "cli", image=str(SCRATCH_ORF))
        self.assertTrue(opened.ok, opened.to_dict())

        result = registry.call(
            "wb.calibrate",
            "cli",
            image=str(SCRATCH_ORF),
            k_from=3000,
            k_to=12000,
            step=1000,
            confirm=True,
        )
        print(f"\nWB CALIBRATE: outcome={result.outcome}, data={result.data}")
        self.assertTrue(result.ok, result.to_dict())
        self.assertTrue(result.data["written"])
        table_path = Path(result.data["table_path"])
        self.assertTrue(table_path.is_file())

        table_text = table_path.read_text(encoding="utf-8")
        COMMITTED_TABLE.write_text(table_text, encoding="utf-8")
        print(f"\nSTAGED {COMMITTED_TABLE} from {table_path} ({len(result.data['points'])} points)")

        with MEASUREMENTS_GENERATED.open("a", encoding="utf-8") as fh:
            fh.write(
                "\n## White balance calibration (task 11 step 7, "
                f"{time.strftime('%Y-%m-%d')}, real host, scratch copy of EditingTrial ORF)\n\n"
                f"- camera: {result.data['camera']}\n"
                f"- table: {table_path}\n"
                f"- points: {result.data['points']}\n"
            )


@unittest.skipUnless(not _MISSING, SKIP_REASON)
class PinWbCoefficientSlidersHostTest(unittest.TestCase):
    """Task 11 step 3, in its own darktable launch (see
    `WhiteBalanceCalibrationHostTest`'s docstring for why): probes each
    candidate ONE AT A TIME, with a `view.current` sanity call after every
    one, and STOPS at the first candidate that leaves the bridge
    unresponsive -- a host run showed `dt.gui.action` on an unrecognised
    path can wedge darktable's Lua worker for the rest of the session, so
    probing all ten candidates in one go turns one bad guess into zero
    evidence about the other nine. Stopping early after the first wedge
    trades "all ten pinned or refused" for "an honest partial result,"
    which is still real evidence for tier 4's refusal (brief step 3: "If
    none answer: ... Do not guess a path").
    """

    maxDiff = None
    proc: subprocess.Popen[bytes] | None = None

    #: The on-disk-backed plugin install (see `_PluginInstall`).
    _install: _PluginInstall | None = None
    _job_before: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERO, SCRATCH_ORF)
        Path(str(SCRATCH_ORF) + ".xmp").unlink(missing_ok=True)

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
        cls._install = _PluginInstall()
        cls._install.install()

    @classmethod
    def _launch_and_wait(cls) -> None:
        cls._job_before = os.environ.get("SCOUTER_JOB")
        os.environ["SCOUTER_JOB"] = f"job-wbprobe-{os.getpid()}-{int(time.time())}"

        cls.proc = subprocess.Popen(
            [str(DARKTABLE), str(SCRATCH_ORF)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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

        deadline = time.monotonic() + 60
        detail = "never answered"
        while time.monotonic() < deadline:
            try:
                current = dtbridge.call("darkroom.current", timeout=10.0)
            except Exception as e:
                detail = str(e)
            else:
                if current.get("image_id") is not None:
                    return
                detail = f"no image loaded yet: {current}"
            time.sleep(0.5)
        raise unittest.SkipTest(f"the darkroom never reported a loaded image within 60s ({detail})")

    @classmethod
    def _restore_job_env(cls) -> None:
        if cls._job_before is None:
            os.environ.pop("SCOUTER_JOB", None)
        else:
            os.environ["SCOUTER_JOB"] = cls._job_before

    @classmethod
    def _cleanup_plugin_install(cls) -> None:
        if cls._install is not None:
            cls._install.restore()
            cls._install = None

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls.proc is not None:
                _quit(cls.proc)
                # By PID, not `pgrep -f MacOS/darktable` (fix round 1, minor
                # c): that pattern also matches a darktable the USER has open
                # in another window, so it failed runs that had cleaned up
                # perfectly -- and it would have passed while OUR instance
                # survived under a different match. Ruling 14 wants a real
                # assertion about the instance this class launched, and
                # `poll()` is exactly that question.
                assert cls.proc.poll() is not None, (
                    f"the darktable this test launched (pid {cls.proc.pid}) is still "
                    "running after _quit's full escalation"
                )
        finally:
            cls._cleanup_plugin_install()
            cls._restore_job_env()

    def test_pin_wb_coefficient_sliders(self) -> None:
        from scouter.core import dtbridge

        results: dict[str, dict] = {}
        wedged_at: str | None = None
        for path in WB_SLIDER_CANDIDATES:
            try:
                reply = dtbridge.call("darkroom.status", {"path": path, "instance": 0}, timeout=8.0)
            except Exception as e:
                results[path] = {"nan": None, "position": None, "error": str(e)}
            else:
                results[path] = {"nan": bool(reply.get("nan")), "position": reply.get("position")}

            # Sanity check: a host run showed `dt.gui.action` on an
            # unrecognised path can wedge the worker for every LATER bridge
            # call too -- catching it here (instead of only at the very
            # end) stops the loop after the candidate that actually broke
            # it, rather than burning a timeout on nine more doomed calls.
            try:
                dtbridge.call("view.current", timeout=8.0)
            except Exception as e:
                wedged_at = path
                results[path].setdefault("wedged_the_bridge", True)
                results[path]["sanity_check_error"] = str(e)
                break

        pinned_scheme: tuple[str, str, str] | None = None
        for scheme in WB_SLIDER_SCHEMES:
            if all(results.get(p, {}).get("nan") is False for p in scheme):
                pinned_scheme = scheme
                break

        lines = [
            "",
            "## White balance coefficient-slider probe (task 11 step 3, "
            f"{time.strftime('%Y-%m-%d')}, darktable 5.4.1, real host, "
            "scratch copy of EditingTrial ORF)",
            "",
        ]
        for path in WB_SLIDER_CANDIDATES:
            r = results.get(path)
            if r is None:
                lines.append(
                    f"- `{path}`: not tried (probe stopped after an earlier "
                    "candidate wedged the bridge)"
                )
            else:
                lines.append(f"- `{path}`: {r}")
        lines.append("")
        if wedged_at:
            lines.append(
                f"**Bridge wedged after `{wedged_at}`**: darktable's Lua worker stopped answering "
                "ANY bridge call after this candidate, including the unrelated `view.current` "
                "sanity check -- a real host limitation of `dt.gui.action` on an unrecognised "
                "path, not a probe bug. The probe stopped here rather than burn timeouts on the "
                "remaining candidates."
            )
        if pinned_scheme:
            names = ("coeff_r", "coeff_g", "coeff_b")
            pins = dict(zip(names, pinned_scheme))
            lines.append(f"**Pinned:** {pins}")
            # Nit (fix round 1): a host TEST must never mutate
            # `dtbridge.ACTION_PATHS` in-process -- that pin would vanish the
            # moment the process exits, and a "pinned" entry that isn't
            # actually in the committed source is worse than an honest
            # refusal. Print the exact source edit for a human to review and
            # commit into `scouter/core/dtbridge.py`'s own `ACTION_PATHS`
            # table instead.
            lines.append("")
            lines.append("Add to `scouter/core/dtbridge.py`'s `ACTION_PATHS`:")
            lines.append("```python")
            for name, path in pins.items():
                lines.append(f'    ("temperature", {name!r}): {path!r},')
            lines.append("```")
        else:
            lines.append(
                "**Pinned: none.** Tier 2 (the coefficient-slider undo, design 11.3) is not "
                "available from this run -- the negative result IS the evidence for tier 4's "
                "refusal (design 11.5)."
            )
        lines.append("")

        MEASUREMENTS_GENERATED.parent.mkdir(parents=True, exist_ok=True)
        with MEASUREMENTS_GENERATED.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        print("\n".join(lines))
        # The assertion here is that the probe ran and recorded SOMETHING --
        # a wedge after the first candidate, or a clean pass/nan through
        # all ten, are both valid, honest outcomes; neither is a test
        # failure. Only a probe that recorded nothing at all is a bug.
        self.assertTrue(results, "the probe recorded no results at all")


# ---------------------------------------------------------------------------
# Task 14 step 5: the 30-frame job, end to end, on REAL ORFs.
# ---------------------------------------------------------------------------

#: The job id and slug carry the process id, so two runs of this class never
#: share a library path, a metrics job or an audit trail. That is not tidiness:
#: darktable's `library.db` REMEMBERS a path's rating across a delete and
#: re-import (measured, scouter phase 2 task 14's live run 2026-09-14 -- run 4
#: found every frame already carrying run 3's cull ratings in a library
#: directory that had been deleted and rebuilt), so a fixed path makes the
#: second run of "green twice in a row" a different test from the first.
E2E_JOB = f"phase2-e2e-{os.getpid()}"
E2E_SLUG = E2E_JOB

#: The end-to-end run uses **30 REAL ORFs** from one real shoot, not a
#: synthetic card: copies of four ORFs have identical pixels, so every
#: sharpness inside a burst would tie and the cull stage could only ever
#: assert the tie-break. 30 consecutive frames of Pratik's 2026-05-24 shoot
#: have real capture gaps AND real pixel differences, so `cull.propose` is
#: judged against bursts this test derives from the files' own EXIF rather
#: than against a number baked into a fixture.
#:
#: `tests/make_synthetic_job.py` is the unit-level fixture and is exercised
#: (including brief step 1's build-twice-identical-bytes determinism
#: assertion) by `tests/test_make_synthetic_job.py`, which needs no
#: darktable -- NOT by anything in this file.
E2E_SOURCE_FIRST = "P5240141.ORF"
E2E_SOURCE_LAST = "P5240170.ORF"
E2E_FRAMES = 30
E2E_CARD = SCRATCH_DIR / "e2e-card" / "DCIM" / "100OLYMP"

#: The library root lives under `~/Pictures/ScouterScratch`, a REAL path.
#: Darktable fact measured by the first live run of this class (2026-09-14):
#: a library root under macOS's `/var/folders/...` temp tree does not work at
#: all -- `/var` is a symlink to `/private/var`, darktable records the
#: RESOLVED film path, and every `image.lookup` by the unresolved path then
#: misses, so `import_batch` reports 0 images and every later verb refuses
#: with "not in darktable's library". Nothing in Scouter was wrong; the test
#: was asking darktable about a path it does not use.
E2E_LIBRARY_ROOT = SCRATCH_DIR / "e2e-library"


def _jpeg_longest_edge(path: Path) -> int:
    """The longest edge of a JPEG, read from its SOF marker with `struct`.

    No Pillow (stdlib-only, constraint 5). JPEG structure: 0xFFD8, then a
    chain of markers, each 0xFF <code> <2-byte length including itself>.
    Any SOF marker (0xC0-0xCF except the non-frame C4/C8/CC) carries
    `precision, height, width` as `>BHH` right after the length.
    """
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"{path} is not a JPEG")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (seg_len,) = struct.unpack_from(">H", data, i + 2)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            _precision, height, width = struct.unpack_from(">BHH", data, i + 4)
            return max(height, width)
        i += 2 + seg_len
    raise ValueError(f"{path}: no SOF marker found")


@unittest.skipUnless(not _MISSING, SKIP_REASON)
class Phase2EndToEndTest(unittest.TestCase):
    """E1-E9 plus the metrics check, in ONE darktable session, in order.

    The exit criterion for phase 2 that CI can actually assert (the real one,
    E12, is Pratik's own 300-frame job). The card is staged by
    `_stage_real_card` from **30 real ORFs** of one 2026-05-24 shoot (see
    `E2E_SOURCE_FIRST`/`E2E_SOURCE_LAST` for why real frames rather than
    `tests/make_synthetic_job.py`'s copies); everything Scouter writes goes
    into a throwaway tree (`SCOUTER_CACHE`,
    `SCOUTER_CONFIG`, the library root, the backup target and
    `OBSIDIAN_VAULT_PATH` are all pointed at it) -- **the real vault is never
    written by a test**, which is `tests/conftest.py`'s whole reason for
    existing and is re-asserted here because a host run sets these itself.

    Method names are numbered because unittest runs them alphabetically and
    these stages genuinely depend on each other: there is one import, and
    everything after it reads what it produced. A failure therefore cascades,
    which is the honest shape for an end-to-end test -- `cull apply` has
    nothing to say about a job that never imported.
    """

    maxDiff = None
    proc: subprocess.Popen[bytes] | None = None

    #: The on-disk-backed plugin install (see `_PluginInstall`).
    _install: _PluginInstall | None = None
    _env_before: dict[str, str | None] = {}
    #: Holds this class's `allowed_tree` registrations for its lifetime.
    _trees: contextlib.ExitStack | None = None
    _tmp: str | None = None
    #: Per-stage wall times, written to `docs/measurements-generated.md` in
    #: `tearDownClass` and copied into `docs/measurements.md` BY HAND
    #: (phase 1's convention: the test no longer rewrites the reviewed record).
    timings: dict[str, float] = {}
    facts: list[str] = []
    #: The job frame the style is extracted from (test_04-06).
    hero: str | None = None
    #: The image darktable actually has loaded, and therefore the only one
    #: Scouter can edit in this session (test_06a's docstring says why).
    edit_target: str | None = None
    style_name: str = ""

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def setUpClass(cls) -> None:
        cls.timings = {}
        cls.facts = []
        # `tests/conftest.py`'s autouse fixture re-pins SCOUTER_CONFIG,
        # SCOUTER_CACHE and OBSIDIAN_VAULT_PATH before EVERY test method, so
        # env vars set here in `setUpClass` were silently reverted -- the
        # first live run of this class failed all ten stages against the
        # session's own pinned config. `allow_tree` is the explicit opt-in
        # that tells the fixture this tree is a legitimate throwaway, and it
        # refuses the real vault/config/cache/home outright.
        # `allowed_tree` (the context manager), not `allow_tree`: the entry
        # is released in `tearDownClass`, so this class stops widening the
        # isolation guard for every test that runs after it (fix round 1,
        # minor d). `contextlib.ExitStack` holds both for the class's life.
        from tests.conftest import allowed_tree

        cls._trees = contextlib.ExitStack()
        cls._tmp = cls._trees.enter_context(
            allowed_tree(os.path.realpath(tempfile.mkdtemp(prefix="scouter-e2e-")))
        )
        tmp = Path(cls._tmp)
        # NOT under `tmp`: see E2E_LIBRARY_ROOT for the darktable fact.
        cls._trees.enter_context(allowed_tree(str(SCRATCH_DIR)))
        cls.library_root = E2E_LIBRARY_ROOT

        # Everything after the ExitStack exists is inside this try, and the
        # handler closes it (fix round 2, item 3). `_stage_real_card` can
        # raise `SkipTest` and `_launch_and_wait` can raise or skip, and
        # before this an `allowed_tree` registration for `~/Pictures/
        # ScouterScratch` outlived a failed or SKIPPED setup -- silently
        # widening the isolation guard for every test that ran afterwards in
        # the same session, which is exactly what the context-manager form
        # was introduced to prevent.
        try:
            cls.backup_target = tmp / "backup"
            cls.vault = tmp / "vault"
            cls.cache = tmp / "cache"
            cls.config_path = tmp / "config.json"
            cls.passfile = tmp / "backup.pass"
            for d in (cls.library_root, cls.backup_target, cls.vault, cls.cache):
                d.mkdir(parents=True, exist_ok=True)
            cls.passfile.write_text("e2e-not-a-real-secret\n")
            cls.passfile.chmod(0o600)

            cls._env_before = {
                k: os.environ.get(k)
                for k in (
                    "SCOUTER_JOB",
                    "SCOUTER_CACHE",
                    "SCOUTER_CONFIG",
                    "OBSIDIAN_VAULT_PATH",
                )
            }
            os.environ["SCOUTER_JOB"] = E2E_JOB
            os.environ["SCOUTER_CACHE"] = str(cls.cache)
            os.environ["SCOUTER_CONFIG"] = str(cls.config_path)
            os.environ["OBSIDIAN_VAULT_PATH"] = str(cls.vault)

            # E1 needs the gate DISARMED, so the first config written has
            # `backup.enabled: false`; `test_00` flips it once it has proved
            # the refusal.
            cls._write_config(backup_enabled=False)

            started = time.monotonic()
            cls.card = cls._stage_real_card()
            cls.timings["stage_real_card_s"] = time.monotonic() - started

            cls._install_plugin()
            cls._launch_and_wait()
        except BaseException:
            if cls.proc is not None:
                _quit(cls.proc)
            cls._cleanup_plugin_install()
            cls._restore_env()
            cls._close_trees()
            raise

    @classmethod
    def _close_trees(cls) -> None:
        """Release this class's `allowed_tree` registrations.

        Called from BOTH `setUpClass`'s failure handler and
        `tearDownClass`, so a registration cannot outlive the class whichever
        way the class ends -- including a `SkipTest`, which is not an error
        and so is easy to forget about.
        """
        if cls._trees is not None:
            cls._trees.close()
            cls._trees = None

    @classmethod
    def _stage_real_card(cls) -> Path:
        """A card of 30 REAL ORFs, staged under `~/Pictures/ScouterScratch`.

        Copies, never symlinks, and never `~/Pictures/EditingTrial` itself as
        the import source: that directory holds 39 ORFs, 2 JPGs and a
        `sony/` subfolder of ARWs, all of which are in `import.extensions`
        by default, so pointing the import at it would pull in 55 files and
        touch a camera this run has no business touching. The originals are
        only ever READ.
        """
        names = sorted(
            p.name
            for p in EDITING_TRIAL.glob("*.ORF")
            if E2E_SOURCE_FIRST <= p.name <= E2E_SOURCE_LAST
        )
        if len(names) != E2E_FRAMES:
            raise unittest.SkipTest(
                f"expected {E2E_FRAMES} real ORFs between {E2E_SOURCE_FIRST} and "
                f"{E2E_SOURCE_LAST}, found {len(names)}"
            )
        if E2E_CARD.exists():
            shutil.rmtree(E2E_CARD.parent)
        E2E_CARD.mkdir(parents=True)
        for name in names:
            shutil.copyfile(EDITING_TRIAL / name, E2E_CARD / name)
        cls.source_names = names
        return E2E_CARD

    @classmethod
    def _expected_bursts(cls) -> list[list[str]]:
        """The burst structure the staged frames' OWN capture times imply, at
        `cull.burst_gap_s` (2.0 s).

        Derived here, from EXIF, rather than hard-coded: with real frames the
        answer is a property of the shoot, and a number baked into this test
        would only prove that two copies of the same constant agree.
        """
        from scouter.core import exif

        stamps = []
        for name in cls.source_names:
            tags = exif.read_tags(E2E_CARD / name)
            stamps.append((tags.captured_at, tags.subsec, name))
        stamps.sort()
        bursts: list[list[str]] = []
        previous = None
        for captured_at, subsec, name in stamps:
            moment = datetime.strptime(captured_at, "%Y-%m-%d %H:%M:%S").timestamp()
            if subsec:
                moment += subsec / (10 ** len(str(subsec)))
            if previous is not None and moment - previous <= 2.0:
                bursts[-1].append(name)
            else:
                bursts.append([name])
            previous = moment
        return bursts

    @classmethod
    def _write_config(cls, *, backup_enabled: bool) -> None:
        cls.config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "library_root": str(cls.library_root),
                    "backup": {
                        "enabled": backup_enabled,
                        "target": str(cls.backup_target),
                        "encryption": "openssl",
                        "openssl_passfile": str(cls.passfile),
                    },
                    "vault": {"jobs_dir": "Projects/Photo jobs"},
                },
                indent=2,
            )
        )

    @classmethod
    def _install_plugin(cls) -> None:
        cls._install = _PluginInstall()
        cls._install.install()

    @classmethod
    def _cleanup_plugin_install(cls) -> None:
        if cls._install is not None:
            cls._install.restore()
            cls._install = None

    @classmethod
    def _restore_env(cls) -> None:
        for key, value in cls._env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @classmethod
    def _launch_and_wait(cls) -> None:
        from scouter.core import dtbridge

        started = time.monotonic()
        # Launched WITH an image, so darktable comes up in the DARKROOM.
        #
        # Darktable fact, measured by this class's live runs (2026-09-14,
        # darktable 5.4.1): from a cold start with no image argument,
        # darktable sits in the lighttable and `dt.gui.current_view(darkroom)`
        # from the worker coroutine NEVER lands -- `darkroom.open` failed with
        # "view is still lighttable" at a 5s budget, again at 20s, three runs
        # in a row. Task 12 measured lighttable -> darkroom at 50 ms, but that
        # was always on an instance that had already been in the darkroom
        # once (phase 1's host tests launch `darktable <ORF>`). Entering the
        # darkroom for the FIRST time appears to need a current image that
        # only the GUI can establish, and `display_image` cannot supply it
        # from the lighttable (task 12: that call is a silent no-op there).
        # So this launches the way Pratik's own sessions run, and the way
        # `tests/test_host_integration.py` has always launched.
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        if not SCRATCH_ORF.exists():
            shutil.copy(HERO, SCRATCH_ORF)
        cls.proc = subprocess.Popen(
            [str(DARKTABLE), str(SCRATCH_ORF)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 120
        last = "never answered"
        while time.monotonic() < deadline:
            try:
                last = dtbridge.current_view()
            except Exception as e:  # bridge not up yet
                last = str(e)
            else:
                if last == "darkroom":
                    cls.timings["darktable_launch_to_darkroom_s"] = time.monotonic() - started
                    return
            time.sleep(1.0)
        raise unittest.SkipTest(
            f"darktable did not reach the darkroom within 120s (last view: {last})"
        )

    @classmethod
    def _delete_created_style(cls) -> None:
        """Blocker 2 (fix round 1): the `scouter-e2e-hero-<ts>` style this class
        creates lives in the user's REAL `data.db` and was never removed --
        two green runs left two styles behind. Deleted here, in
        `tearDownClass`'s own `finally` chain, exactly the way
        `PinDtstyleFormatTest` has always cleaned up its probe styles.

        Best effort and never fatal: a teardown that raises would mask
        whatever the run was actually reporting, and a leftover style is a
        warning, not a reason to lose the result.
        """
        if not cls.style_name:
            return
        try:
            from scouter.core import dtbridge

            dtbridge.call("style.delete", {"name": cls.style_name, "confirm": True})
        except Exception as e:  # noqa: BLE001 - cleanup must never mask the run
            print(f"WARNING: cleanup could not delete style {cls.style_name!r}: {e}")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            # Before the quit: `style.delete` needs a live bridge.
            if cls.proc is not None and cls.proc.poll() is None:
                cls._delete_created_style()
            if cls.proc is not None and cls.proc.poll() is None and cls.edit_target:
                # Ruling 16: commit before quitting, so darktable is never
                # killed holding history it has not flushed.
                try:
                    from scouter import registry

                    registry.call("history.commit", "cli", image=cls.edit_target, timeout_s=10.0)
                except Exception:
                    pass
            if cls.proc is not None:
                _quit(cls.proc)
                # Ruling 14 wants a REAL assertion that the instance is gone,
                # and this is the instance we launched -- by pid. The old
                # `pgrep -f MacOS/darktable` also matched a darktable the USER
                # had opened in another window, so it failed a run that had
                # cleaned up perfectly and, worse, would have passed while
                # OUR instance survived under a different match. `poll()` is
                # the honest question: has the child we started exited?
                assert cls.proc.poll() is not None, (
                    f"the darktable this test launched (pid {cls.proc.pid}) is still "
                    "running after _quit's full escalation"
                )
        finally:
            cls._write_measurements()
            cls._cleanup_plugin_install()
            cls._restore_env()
            cls._close_trees()
            # `SCOUTER_KEEP_SYNTHETIC=1` keeps this run's tree for
            # inspection. The name is the one the brief documents and is kept
            # for that reason, but what it actually preserves is the STAGED
            # REAL CARD, the library root built from it and the temp tree --
            # there is no synthetic card in this class any more (see
            # `_stage_real_card`).
            if os.environ.get("SCOUTER_KEEP_SYNTHETIC") != "1":
                # The card ROOT (`<scratch>/e2e-card`), not just the DCIM
                # directory inside it -- otherwise an empty shell is left
                # behind in ~/Pictures/ScouterScratch after every run.
                shutil.rmtree(SCRATCH_DIR / "e2e-card", ignore_errors=True)
                # The library's raws are 0444, so the tree needs its files
                # made writable before it can be removed.
                for path in sorted(E2E_LIBRARY_ROOT.rglob("*"), reverse=True):
                    if path.is_file():
                        try:
                            path.chmod(0o644)
                        except OSError:
                            pass
                shutil.rmtree(E2E_LIBRARY_ROOT, ignore_errors=True)
                if cls._tmp:
                    shutil.rmtree(cls._tmp, ignore_errors=True)

    @classmethod
    def _write_measurements(cls) -> None:
        if not cls.timings:
            return
        lines = [
            "",
            f"## Phase 2 end-to-end, 30 REAL frames (task 14, {time.strftime('%Y-%m-%d')}, "
            "darktable 5.4.1, real host, MacBook)",
            "",
            # The unit column is per-row: `cls.timings` carries wall seconds
            # AND byte/frame counts (`*_bytes`, `*_frames`, `*_ms`), and a
            # single "wall seconds" header made 526387934.00 read as 6 days.
            "| measurement | value | unit |",
            "|---|---|---|",
        ]
        for key, value in cls.timings.items():
            if key.endswith("_bytes"):
                unit, shown = "bytes", f"{value:.0f}"
            elif key.endswith("_frames"):
                unit, shown = "frames", f"{value:.0f}"
            elif key.endswith("_ms"):
                unit, shown = "milliseconds", f"{value:.0f}"
            else:
                unit, shown = "wall seconds", f"{value:.2f}"
            lines.append(f"| `{key}` | {shown} | {unit} |")
        if cls.facts:
            lines.extend(["", "New darktable facts observed in this run:", ""])
            lines.extend(f"- {fact}" for fact in cls.facts)
        lines.append("")
        MEASUREMENTS_GENERATED.parent.mkdir(parents=True, exist_ok=True)
        with MEASUREMENTS_GENERATED.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print("\n".join(lines))

    # -- helpers ------------------------------------------------------------

    def _call(self, verb: str, **kwargs):
        from scouter import registry

        return registry.call(verb, "cli", **kwargs)

    def _ok(self, verb: str, **kwargs):
        envelope = self._call(verb, **kwargs)
        self.assertTrue(
            envelope.ok, f"{verb} failed: {envelope.outcome} {envelope.error or envelope.data}"
        )
        return envelope.data

    def _job_dir(self) -> Path:
        from scouter import config as config_module

        return config_module.find_job(config_module.load(), E2E_SLUG)

    def _raws(self) -> list[Path]:
        return sorted(
            p
            for p in (self._job_dir() / "raw").iterdir()
            if p.is_file() and p.suffix.lower() != ".xmp"
        )

    # -- E1 -----------------------------------------------------------------

    def test_00_gate_refuses_first(self) -> None:
        """E1: with `backup.enabled: false`, `library import` exits 11 and
        NOTHING was created -- the gate runs before the card is read."""
        from scouter.errors import exit_code_for

        envelope = self._call(
            "library.import", source=str(self.card), job_slug=E2E_SLUG, confirm=True
        )
        self.assertFalse(envelope.ok)
        self.assertEqual(envelope.outcome, "error:backup_not_configured")
        self.assertEqual(exit_code_for(envelope.outcome), 11)
        self.assertIn("enabled", envelope.error["message"])
        self.assertEqual(
            sorted(p.name for p in self.library_root.iterdir()),
            [],
            "the refused import created something under the library root",
        )
        # Arm the gate for every stage after this one.
        type(self)._write_config(backup_enabled=True)
        self.assertEqual(self._ok("config.show")["config"]["backup"]["enabled"], True)

    def test_00a_repeat_set_cost(self) -> None:
        """E8: the repeat-`darkroom.set` cost, on the image darktable has
        loaded, BEFORE the import.

        Ordering is deliberate (`00a` sorts after `test_00_gate...` and before
        `test_01_import...`). Measured on the real host, 2026-09-14: with the
        job's 30 frames already imported, `history.commit`'s view bounce did
        not get the sidecar written inside its 5 s budget -- the bounce leaves
        the darkroom, and on re-entry darktable does not reliably come back to
        the same image once the library has grown (the same root cause as
        `test_06a`'s pinned refusal). Run before the import, against the
        one-image library phase 1 measured (commit 765-1305 ms), the repeat
        cost is what E8 is actually about, and it is measured rather than
        entangled with a darktable navigation limitation that has its own
        named test.

        The edit target is whatever darktable is showing -- which is exactly
        how Pratik works: he picks the frame, Scouter edits what is loaded.
        """
        from scouter.core import dtbridge

        current = dtbridge.call("darkroom.current")
        self.assertIsNotNone(current.get("image_id"), current)
        target = str(current["path"])
        type(self).edit_target = target
        type(self).facts.append(
            f"darkroom.current before the import: image {current['image_id']} "
            f"({Path(target).name}), source {current.get('source')}"
        )

        opened = self._ok("darkroom.open", image=target)
        self.assertTrue(opened["already_open"], opened)

        self._ok("darkroom.set", module="exposure", param="exposure", value=0.6)
        self._ok("darkroom.set", module="sigmoid", param="contrast", value=1.3)

        repeats = []
        for _ in range(2):
            started = time.monotonic()
            result = self._ok("darkroom.set", module="exposure", param="exposure", value=0.6)
            repeats.append((time.monotonic() - started, bool(result["noop"])))

        type(self).timings["repeat_set_first_s"] = repeats[0][0]
        type(self).timings["repeat_set_second_s"] = repeats[1][0]
        for index, (seconds, _noop) in enumerate(repeats, start=1):
            self.assertLess(
                seconds, 1.5, f"repeat set #{index} cost {seconds:.2f}s (phase 1 was 5.5s)"
            )
        self.assertTrue(repeats[-1][1], f"the second repeat must be a no-op, got {repeats}")
        type(self).facts.append(
            f"repeat darkroom.set: {repeats[0][0]:.2f}s then {repeats[1][0]:.2f}s "
            f"(noop={repeats[0][1]}/{repeats[1][1]}); phase 1 measured 5.5-5.7s"
        )

    # -- E2 -----------------------------------------------------------------

    def test_01_import_30_frames(self) -> None:
        """E2: 30 files in `raw/`, all 0444, `library verify` clean,
        `import.json` + `checksums.sha256` present, the film roll registered,
        and the TEMP vault note written with `ai-first: true`."""
        started = time.monotonic()
        data = self._ok("library.import", source=str(self.card), job_slug=E2E_SLUG, confirm=True)
        elapsed = time.monotonic() - started
        type(self).timings["library_import_30_frames_s"] = elapsed
        type(self).timings["library_import_bytes"] = float(data["totals"]["bytes"])

        self.assertEqual(data["totals"]["copied"], 30)
        raws = self._raws()
        self.assertEqual(len(raws), 30)
        for raw in raws:
            self.assertEqual(
                oct(raw.stat().st_mode & 0o777), "0o444", f"{raw.name} is not read-only"
            )
        scouter_d = self._job_dir() / ".scouter"
        self.assertTrue((scouter_d / "import.json").is_file())
        self.assertTrue((scouter_d / "checksums.sha256").is_file())

        verify = self._ok("library.verify", job_slug=E2E_SLUG)
        self.assertTrue(verify["ok"], verify)
        self.assertEqual(verify["checked"], 30)

        self.assertTrue(
            data.get("film_roll_registered"),
            f"the film roll was not registered: {data.get('warnings')}",
        )
        type(self).facts.append(
            f"library.import registered a film roll of {data.get('imported')} images for "
            f"a 30-frame folder (import_batch, recursive=false)"
        )

        notes = sorted((self.vault / "Projects" / "Photo jobs").glob("*.md"))
        self.assertEqual(len(notes), 1, f"expected one vault job note, got {notes}")
        note = notes[0].read_text()
        self.assertIn("ai-first: true", note)
        self.assertIn("## For future agent", note)
        # The REAL vault must be untouched: this note is in the temp tree.
        self.assertTrue(str(notes[0]).startswith(str(self.vault)))

    # -- E3 -----------------------------------------------------------------

    def test_02_cull(self) -> None:
        """E3: `cull propose` groups the job into the bursts its frames' own
        capture times imply and proposes exactly one pick per burst, every
        proposal carrying its numbers; `cull apply --yes` writes the ratings
        and every changed frame is accounted for as verified or unverified.

        NOT "6 bursts of 5": that was the synthetic card's shape. These are
        30 consecutive frames of one real evening shoot, so the answer is a
        property of the shoot -- 20 bursts on the runs so far (one of 7, one
        of 3, two of 2, sixteen singletons) -- and the expected grouping is
        derived here from the files' EXIF rather than written down, because a
        number baked into this test would only prove that two copies of the
        same constant agree.
        """
        started = time.monotonic()
        proposal = self._ok("cull.propose", job_slug=E2E_SLUG)
        type(self).timings["cull_propose_30_frames_s"] = time.monotonic() - started
        type(self).timings["cull_propose_stats_ms"] = float(proposal["stats"]["ms"])

        real_bursts = [g for g in proposal["groups"] if g["burst_id"] != "no-time"]
        expected = self._expected_bursts()
        self.assertEqual(
            sorted(len(g["frames"]) for g in real_bursts),
            sorted(len(b) for b in expected),
            "cull.propose's grouping must match what the frames' own capture "
            f"times imply at a 2.0 s gap: {[len(b) for b in expected]}",
        )
        self.assertEqual(
            sum(len(g["frames"]) for g in proposal["groups"]),
            E2E_FRAMES,
            "every frame must land in exactly one group",
        )
        type(self).facts.append(
            f"cull.propose over 30 real frames of one shoot: "
            f"{len(real_bursts)} bursts at the 2.0 s gap, sizes "
            f"{sorted((len(g['frames']) for g in real_bursts), reverse=True)}"
        )

        # One pick per burst -- no more, and the picks are the design's own
        # `cull.pick_rating` (2).
        picks_by_burst: dict[str, int] = {}
        by_seq_burst = {p["seq"]: p["burst_id"] for p in proposal["proposals"]}
        for p in proposal["proposals"]:
            if p["rating_proposed"] == 2:
                picks_by_burst[p["burst_id"]] = picks_by_burst.get(p["burst_id"], 0) + 1
        self.assertTrue(by_seq_burst)
        for burst_id, count in picks_by_burst.items():
            self.assertEqual(count, 1, f"burst {burst_id} proposed {count} picks")
        multi = [g for g in real_bursts if len(g["frames"]) > 1]
        for g in multi:
            self.assertEqual(
                picks_by_burst.get(g["burst_id"]),
                1,
                f"multi-frame burst {g['burst_id']} must have exactly one pick",
            )

        # `tie_broken_by` is asserted against what the DATA implies, not
        # against "one of the two legal values" -- that was a tautology
        # (`assertIn(x, (None, "seq"))` passes for every value the field can
        # hold). A burst ties exactly when its two sharpest frames report the
        # same sharpness, which this computes from the proposals themselves.
        sharpness_by_seq = {p["seq"]: p["sharpness"] for p in proposal["proposals"]}
        for g in real_bursts:
            ranked = sorted((sharpness_by_seq[s] for s in g["frames"]), reverse=True)
            tied = len(ranked) >= 2 and ranked[0] == ranked[1]
            self.assertEqual(
                g["tie_broken_by"],
                "seq" if tied else None,
                f"burst {g['burst_id']} sharpnesses {ranked} say tied={tied}, "
                f"but tie_broken_by is {g['tie_broken_by']!r}",
            )
        ties = sum(1 for g in real_bursts if g["tie_broken_by"] == "seq")
        type(self).facts.append(
            f"real-frame sharpness ties: {ties} of {len(real_bursts)} bursts "
            "needed the seq tie-break"
        )

        for p in proposal["proposals"]:
            self.assertTrue(p["reasons"], f"frame {p['seq']} has no reason")
            for key in ("sharpness", "clipped_high_pct", "luminance_mean"):
                self.assertIsNotNone(p[key], f"frame {p['seq']} has no {key}")

        started = time.monotonic()
        applied = self._ok("cull.apply", job_slug=E2E_SLUG, confirm=True)
        type(self).timings["cull_apply_30_frames_s"] = time.monotonic() - started

        changed = applied["would_change"]
        self.assertEqual(
            applied["verified"] + applied["unverified"],
            changed,
            "every changed frame must be accounted for as verified or unverified",
        )
        self.assertEqual(applied["missed"], [], applied)

        # The real claim behind E3 is not "some ratings changed" (a job whose
        # ratings already match the proposal legitimately changes nothing);
        # it is that after the apply, every frame's rating ON DISK is the one
        # the proposal named -- or is honestly reported as not yet visible.
        # Asserting the count would make this test depend on what darktable's
        # library happened to remember about these paths.
        from scouter.core import xmp as xmp_module

        unverified_paths = {row["image_id"] for row in applied["per_image"] if not row["verified"]}
        mismatched = []
        for p in proposal["proposals"]:
            on_disk = xmp_module.rating(p["path"]) or 0
            if on_disk != p["rating_proposed"] and p["image_id"] not in unverified_paths:
                mismatched.append((p["seq"], p["rating_proposed"], on_disk))
        self.assertEqual(
            mismatched,
            [],
            "after cull.apply every frame's on-disk rating must be the proposed one "
            "unless the result reported it unverified: (seq, proposed, on_disk)",
        )
        type(self).facts.append(
            f"cull.apply: {changed} of 30 frames needed a rating change; "
            f"{applied['verified']} were visible in the XMP within the 2.0s poll "
            f"window, {applied['unverified']} not yet"
        )

    # -- E4 -----------------------------------------------------------------

    def test_04_style_extract_filtered(self) -> None:
        """E4: `--modules exposure,sigmoid` writes a `.dtstyle` holding
        exactly two operations, `modules_honoured is True`, and darktable's
        own `list_styles` shows it."""
        from scouter.core import dtbridge

        # `scouter-` prefixed on purpose: the Lua `style.delete` verb refuses
        # any name that does not start with it, which is the safety check
        # that stops a test from deleting one of Pratik's own styles. Fix
        # round 1 (blocker 2) found the teardown hitting exactly that
        # refusal -- the guard was right and this NAME was wrong, so the name
        # changed rather than the guard being bypassed with a `prefix`
        # override.
        name = f"scouter-e2e-hero-{int(time.time())}"
        type(self).style_name = name
        # The style comes from the frame that was actually EDITED
        # (`test_00a`'s target, the image darktable had loaded), because that
        # is the only frame in this session with a hero recipe on it --
        # exposure 0.6 + sigmoid contrast 1.3, set through Scouter. A job
        # frame cannot be it: darktable will not load one into the darkroom
        # this session (test_06a), so none of them has a history beyond
        # darktable's own defaults, and `style.extract` correctly refuses to
        # build a style out of modules that are not in the active history.
        #
        # This is also the real workflow the limitation leaves: Pratik edits
        # the frame that is open, the style is extracted from it, and it
        # lands on the set. `style.extract` reads the XMP, not the darkroom.
        self.assertIsNotNone(self.edit_target, "test_00a must have run first")
        type(self).hero = self.edit_target
        started = time.monotonic()
        data = self._ok(
            "style.extract",
            image=self.hero,
            name=name,
            modules=["exposure", "sigmoid"],
            confirm=True,
        )
        type(self).timings["style_extract_s"] = time.monotonic() - started

        self.assertTrue(data["modules_honoured"], data)
        self.assertEqual(sorted(data["operations_included"]), ["exposure", "sigmoid"])
        dtstyle = Path(data["dtstyle_path"])
        self.assertTrue(dtstyle.is_file())
        root = ET.fromstring(dtstyle.read_text())
        operations = sorted(el.text for el in root.iter("operation"))
        self.assertEqual(operations, ["exposure", "sigmoid"], operations)

        styles = dtbridge.call("list_styles", {})
        self.assertIn(name, [s["name"] for s in styles["styles"]])

    # -- E5 -----------------------------------------------------------------

    def test_05_style_apply_set(self) -> None:
        """E5: the whole job gets the hero's style, `failed == 0`, one
        `per_image` row per frame.

        All 30, not 29: the hero is the edited frame OUTSIDE the job
        (test_04's docstring says why darktable leaves no other choice), so
        there is nothing in the job to exclude. The claim E5 makes -- every
        frame gets the style, per-image results, no failures -- is unchanged;
        only the arithmetic is.
        """
        started = time.monotonic()
        data = self._ok(
            "style.apply_set",
            name=self.style_name,
            job_slug=E2E_SLUG,
            confirm=True,
        )
        elapsed = time.monotonic() - started
        type(self).timings["style_apply_set_30_frames_s"] = elapsed

        self.assertEqual(data["failed"], 0, data)
        self.assertEqual(len(data["per_image"]), E2E_FRAMES, len(data["per_image"]))
        self.assertEqual(data["applied"], E2E_FRAMES, data)
        type(self).facts.append(
            f"style.apply_set: {E2E_FRAMES} frames in {elapsed:.2f}s "
            f"({elapsed / E2E_FRAMES * 1000:.0f} ms/frame, batched at 25 ids per "
            "bridge call)"
        )

    # -- E6 -----------------------------------------------------------------

    def test_06_outliers(self) -> None:
        """E6: against the hero; every row carries `ev_proxy_delta` and the
        tolerances used; `checked == 29`."""
        # The reference is a JOB frame, not test_04's out-of-job hero: an
        # outlier report is only meaningful against a frame from the same
        # shoot, and comparing 30 evening frames to an unrelated daylight
        # scratch copy would produce 30 "outliers" that say nothing. The
        # reference is excluded from its own set, so `checked` is 29.
        reference = str(self._raws()[0])
        started = time.monotonic()
        data = self._ok("outliers.list", reference=reference, job_slug=E2E_SLUG)
        type(self).timings["outliers_list_29_frames_s"] = time.monotonic() - started

        self.assertEqual(data["checked"], 29, data)
        self.assertIn(data["wb_test"], ("kelvin", "ratio"))
        self.assertTrue(data["tolerances"])
        self.assertEqual(
            data["within_tolerance"] + len(data["outliers"]) + len(data["failed"]),
            29,
            "every checked frame must land in exactly one bucket",
        )
        for row in data["outliers"]:
            self.assertIn("ev_proxy_delta", row, row)
            self.assertIn("reasons", row, row)
        for key in ("exposure_ev", "clipped_high_pct"):
            self.assertIn(key, data["tolerances"], data["tolerances"])
        type(self).facts.append(
            f"outliers.list over 29 post-style frames: wb_test={data['wb_test']}, "
            f"{len(data['outliers'])} outside tolerance, {data['within_tolerance']} within"
        )

    def test_06a_darkroom_open_refuses_a_frame_imported_this_session(self) -> None:
        """The measured darktable limitation, pinned as a refusal.

        **Darktable fact (2026-09-14, darktable 5.4.1, five runs):** Scouter
        cannot move darktable's darkroom onto a frame from a film roll
        imported during the same session. `display_image(image)` returns
        without error and the darkroom keeps showing the image it already had
        (15 s of polling, `darkroom.current` unchanged); bouncing out to the
        lighttable with that image selected and switching back returns to the
        SAME previously-edited image, not the selection. `darkroom.open` is
        therefore honest about it -- it raises rather than reporting
        `opened: true` for an image that never loaded -- and this asserts the
        refusal rather than pretending the capability exists.

        Consequence for the product, and why this is a test rather than a bug:
        the frame is chosen in darktable's GUI and Scouter edits what is
        loaded. Every set-wide verb (`cull.*`, `style.apply_set`,
        `outliers.list`, `export.*`) works off the XMP and `darktable-cli`, so
        none of them needs the darkroom -- which is why the other eight stages
        pass.
        """
        job_frame = str(self._raws()[0])
        envelope = self._call("darkroom.open", image=job_frame)
        self.assertFalse(
            envelope.ok,
            "darktable 5.4.1 could not be moved onto a frame imported this "
            "session; if this now SUCCEEDS the limitation has gone and this "
            "test, docs/measurements.md and README.md must be updated",
        )
        self.assertIn(
            "wrong image loaded",
            envelope.error["message"],
            "the refusal must name what actually happened, not a generic timeout",
        )
        type(self).facts.append(
            "darkroom.open on a frame imported this session: refused with "
            f"{envelope.error['code']} -- {envelope.error['message'][-140:]}"
        )

    # -- E7 -----------------------------------------------------------------

    def test_07_release(self) -> None:
        """E7: `--preset client-proof --min-rating 1 --yes`; every JPEG's
        longest edge is 2048, read from the SOF header; `export verify`
        clean."""
        started = time.monotonic()
        data = self._ok(
            "export.release",
            job_slug=E2E_SLUG,
            preset="client-proof",
            min_rating=1,
            confirm=True,
        )
        elapsed = time.monotonic() - started
        type(self).timings["export_release_client_proof_s"] = elapsed
        type(self).timings["export_release_frames"] = float(data["exported"])

        self.assertEqual(data["failed"], 0, data)
        self.assertGreater(data["exported"], 0, data)
        outputs = sorted(Path(data["out_dir"]).glob("*.jpg"))
        self.assertEqual(len(outputs), data["exported"])
        for out in outputs:
            self.assertEqual(
                _jpeg_longest_edge(out), 2048, f"{out.name} is not 2048 px on its longest edge"
            )

        verified = self._ok("export.verify", manifest=data["manifest_path"])
        self.assertTrue(verified["ok"], verified)
        self.assertEqual(verified["checked"], data["exported"])
        type(self).facts.append(
            f"export.release client-proof: {data['exported']} frames in {elapsed:.2f}s "
            f"({elapsed / max(data['exported'], 1):.2f} s/frame at 2048 px, q90 sRGB)"
        )

    # -- E9 -----------------------------------------------------------------

    def test_08_backup(self) -> None:
        """E9: `backup run --yes` then `backup verify` -> `verified is True`,
        every check named."""
        started = time.monotonic()
        run = self._ok("backup.run", confirm=True)
        type(self).timings["backup_run_s"] = time.monotonic() - started

        started = time.monotonic()
        # `archive` is the archive's timestamp DIRECTORY name, not a file
        # path; omitted, `backup.verify` picks the newest, which is the one
        # this run just wrote.
        verified = self._ok("backup.verify")
        type(self).timings["backup_verify_s"] = time.monotonic() - started

        self.assertTrue(verified["verified"], verified)
        self.assertTrue(verified["checks"], verified)
        for check in verified["checks"]:
            self.assertIn("name", check)
            self.assertTrue(check["ok"], f"check {check['name']} failed: {check}")
        type(self).timings["backup_plaintext_bytes"] = float(run["plaintext_bytes"])
        type(self).timings["backup_ciphertext_bytes"] = float(run["ciphertext_bytes"])
        type(self).timings["backup_ms_tar"] = float(run["ms_tar"])
        type(self).timings["backup_ms_encrypt"] = float(run["ms_encrypt"])
        type(self).timings["backup_ms_rsync"] = float(run["ms_rsync"] or 0)
        type(self).facts.append(
            f"backup.run wrote {run['archive_dir']} "
            f"({run['plaintext_bytes']} plaintext -> {run['ciphertext_bytes']} ciphertext "
            f"bytes, encryption={run['encryption']}, rsync_ran={run['rsync_ran']}); "
            f"backup.verify ran {len(verified['checks'])} named checks, "
            f"archive={verified['archive']}"
        )

    # -- metrics ------------------------------------------------------------

    def test_09_metrics(self) -> None:
        """One event per verb invoked, `phase == 2`, and no `error:` outcome
        other than `test_00`'s deliberate gate refusal."""
        data = self._ok("metrics.summary", since="1d")
        by_verb = {row["verb"]: row for row in data["verbs"]}

        for verb in (
            "library.import",
            "library.verify",
            "cull.propose",
            "cull.apply",
            "darkroom.set",
            "style.extract",
            "style.apply_set",
            "outliers.list",
            "export.release",
            "export.verify",
            "backup.run",
            "backup.verify",
        ):
            self.assertIn(verb, by_verb, f"{verb} emitted no metrics event")

        # Exactly two verbs may record an error, and each exactly one:
        #  - library.import: test_00's deliberate backup-gate refusal (E1);
        #  - darkroom.open: test_06a's pinned refusal for a frame darktable
        #    cannot load this session (that test's docstring has the measured
        #    detail).
        # Everything else must be clean. Naming the two, rather than allowing
        # "some errors", is what keeps this from becoming a rubber stamp.
        self.assertEqual(by_verb["library.import"]["calls"], 2, by_verb["library.import"])
        self.assertEqual(by_verb["library.import"]["errors"], 1, "only test_00 may have errored")
        self.assertEqual(by_verb["darkroom.open"]["calls"], 2, by_verb["darkroom.open"])
        self.assertEqual(by_verb["darkroom.open"]["errors"], 1, by_verb["darkroom.open"])
        self.assertEqual(
            by_verb["darkroom.open"]["by_class"],
            {"bridge_timeout": 1},
            "test_06a's refusal is the only darkroom.open error, and it is the "
            "typed one that test asserts",
        )
        expected_errors = {"library.import", "darkroom.open"}
        for verb, row in by_verb.items():
            if verb in expected_errors:
                continue
            self.assertEqual(row["errors"], 0, f"{verb} recorded an error: {row}")

        from scouter import paths

        audit = paths.audit_path(E2E_JOB)
        self.assertTrue(audit.is_file(), f"no audit trail at {audit}")
        events = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
        self.assertTrue(events)
        self.assertTrue(all(e.get("phase") == 2 for e in events), "every event must say phase 2")


if __name__ == "__main__":
    unittest.main()


class PluginInstallSafetyTest(unittest.TestCase):
    """`_PluginInstall`'s non-host guarantees (fix round 2, minors m1/m2).

    No darktable and no touching of the real `~/.config/darktable`:
    `INSTALLED_PLUGIN` and `SCRATCH_DIR` are patched to a temp tree, so this
    runs in the ordinary suite. What it pins is the property that makes the
    class safe to use at all -- its own cleanup must never delete the user's
    plugin.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="scouter-plugininstall-")
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.plugin = self.tmp / "config" / "darktable" / "lua" / "darktable_mcp.lua"
        self.luarc = self.tmp / "config" / "darktable" / "luarc"
        self.scratch = self.tmp / "scratch"
        self._patches = [
            mock.patch.object(sys.modules[__name__], "INSTALLED_PLUGIN", self.plugin),
            mock.patch.object(sys.modules[__name__], "SCRATCH_DIR", self.scratch),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_failing_mkdir_never_unlinks_the_users_plugin(self) -> None:
        """Minor m1, the reason it matters: `install`'s handler calls
        `restore()`, and `restore()` unlinks the plugin when
        `plugin_existed_before` is False. Reading that flag after the mkdir
        meant a failing mkdir left it False and the cleanup deleted the real
        plugin."""
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text("-- the user's own plugin")
        self.luarc.write_text('require "something_else"\n')

        install = _PluginInstall()
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            with self.assertRaises(OSError):
                install.install()

        self.assertTrue(self.plugin.is_file(), "the user's plugin was deleted by the cleanup")
        self.assertEqual(self.plugin.read_text(), "-- the user's own plugin")
        self.assertEqual(self.luarc.read_text(), 'require "something_else"\n')
        self.assertTrue(install.plugin_existed_before, "the flag must be read before any mkdir")

    def test_install_then_restore_round_trips_the_users_files(self) -> None:
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text("-- the user's own plugin")
        self.luarc.write_text('require "something_else"\n')

        install = _PluginInstall()
        install.install()
        self.assertNotEqual(self.plugin.read_text(), "-- the user's own plugin")
        self.assertIn('require "darktable_mcp"', self.luarc.read_text())
        backup = install.backup_dir
        self.assertIsNotNone(backup)
        self.assertTrue((backup / "MANIFEST.txt").is_file())
        self.assertIn("library.db", (backup / "MANIFEST.txt").read_text())

        install.restore()
        self.assertEqual(self.plugin.read_text(), "-- the user's own plugin")
        self.assertEqual(self.luarc.read_text(), 'require "something_else"\n')
        self.assertIsNone(install.backup_dir, "a successful restore cleans its backup up")
        self.assertFalse(backup.exists())

    def test_restore_removes_what_it_added_when_nothing_was_there_before(self) -> None:
        install = _PluginInstall()
        install.install()
        self.assertTrue(self.plugin.is_file())

        install.restore()
        self.assertFalse(self.plugin.exists(), "a plugin we installed must be removed")
        self.assertFalse(self.luarc.exists(), "a luarc we created must be removed")

    def test_the_backup_directory_name_carries_the_pid(self) -> None:
        """Minor m2: two classes inside the same second, or two pytest
        processes, must not share a backup directory and overwrite each
        other's copy of the user's plugin."""
        install = _PluginInstall()
        install.install()
        self.addCleanup(install.restore)
        self.assertIsNotNone(install.backup_dir)
        self.assertTrue(
            install.backup_dir.name.endswith(f"-{os.getpid()}"),
            install.backup_dir.name,
        )
