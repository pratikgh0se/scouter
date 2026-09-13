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
library.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
HERO = Path("/Users/pratik/Pictures/EditingTrial/P6281348.ORF")
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

    _plugin_existed_before: bool = False
    _plugin_backup: bytes | None = None
    _luarc_existed_before: bool = False
    _luarc_line_added: bool = False
    _luarc_path: Path | None = None
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
                cls._luarc_line_added = True
                with luarc.open("a") as fh:
                    fh.write(line)
        except BaseException:
            cls._cleanup_plugin_install()
            raise

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

    def test_pin_dtstyle_format(self) -> None:
        from scouter import registry
        from scouter.core import dtbridge

        raw = str(HERO)

        opened = registry.call("darkroom.open", "cli", image=raw)
        self.assertTrue(opened.ok, opened.to_dict())

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


if __name__ == "__main__":
    unittest.main()
