"""Phase 3 task 9: the cheap closures from phase 2 (design §11)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scouter import PHASE, __version__, paths
from scouter.core import wb
from scouter.mcp import server


class SessionJobTest(unittest.TestCase):
    def test_session_job_is_pinned_once_and_respects_an_explicit_one(self) -> None:
        env: dict[str, str] = {}
        job = server._pin_session_job(env)
        self.assertRegex(job, r"^mcp-\d{8}-\d{6}-\d+$")
        self.assertEqual(paths.validate_job(job), job)
        self.assertEqual(env["SCOUTER_JOB"], job)
        self.assertEqual(server._pin_session_job(env), job, "a second call keeps the same job")
        named = {"SCOUTER_JOB": "pratik-teaching"}
        self.assertEqual(server._pin_session_job(named), "pratik-teaching")

    def test_an_invalid_explicit_job_is_refused(self) -> None:
        from scouter.errors import UsageError
        with self.assertRaises(UsageError):
            server._pin_session_job({"SCOUTER_JOB": "../escape"})


class WbTableVersionTest(unittest.TestCase):
    def test_version_2_is_refused_with_the_file_named(self) -> None:
        fixture = json.loads((Path(__file__).parent / "fixtures" / "wb-em10iii.json").read_text())
        fixture["version"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "wb").mkdir()
            (Path(tmp) / "wb" / "em10iii.json").write_text(json.dumps(fixture))
            with mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(Path(tmp) / "config.json")}):
                with self.assertRaises(ValueError) as cm:
                    wb.load_table("em10iii")
                self.assertIn("version 2", str(cm.exception))
                self.assertIsNone(wb.kelvin([2.0, 1.0, 1.7, 1.0], "em10iii")["kelvin"])


class HostSupportScopeTest(unittest.TestCase):
    def test_plugin_install_touches_only_the_plugin_and_luarc(self) -> None:
        """Final review #8 (spec §10.4 / P3-R18): a REAL install() then
        restore() against a temp home tree (never the real ~/.config/darktable),
        snapshotting the whole tree before, after install and after restore.
        Every path that ever differs must be one of touched_paths(), inside
        the scratch backup area, or a directory install() had to create to
        hold one of those (an ancestor, e.g. `lua/` on a fresh config). The
        user's own files come back byte-identical, and every sibling config
        file is never touched."""
        from tests import host_support
        from tests.conftest import allowed_tree

        def snapshot(root: Path) -> dict[str, object]:
            out: dict[str, object] = {}
            for dirpath, dirnames, filenames in os.walk(root):
                for d in dirnames:
                    out[os.path.relpath(os.path.join(dirpath, d), root)] = "dir"
                for f in filenames:
                    full = os.path.join(dirpath, f)
                    out[os.path.relpath(full, root)] = Path(full).read_bytes()
            return out

        def changed(a: dict, b: dict) -> set[str]:
            return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}

        for pre_existing in (True, False):
            with self.subTest(pre_existing=pre_existing), \
                 tempfile.TemporaryDirectory(prefix="scouter-p3r18-") as tmp, \
                 allowed_tree(tmp):
                home = Path(tmp) / "home"
                dtconf = home / ".config" / "darktable"
                plugin = dtconf / "lua" / "darktable_mcp.lua"
                scratch = home / "Pictures" / "ScouterScratch"
                # Siblings that must never be touched (the R22 scope).
                dtconf.mkdir(parents=True)
                (dtconf / "darktablerc").write_text("plugins/lighttable/x=1\n")
                (dtconf / "library.db").write_bytes(b"not really sqlite")
                (dtconf / "styles").mkdir()
                (dtconf / "styles" / "mine.dtstyle").write_text("<darktable_style/>")
                if pre_existing:
                    plugin.parent.mkdir(parents=True)
                    plugin.write_text("-- the user's own plugin\n")
                    (dtconf / "luarc").write_text('require "tools/script_manager"\n')
                with mock.patch.object(host_support, "INSTALLED_PLUGIN", plugin), \
                     mock.patch.object(host_support, "SCRATCH_DIR", scratch), \
                     mock.patch.dict(os.environ, {"HOME": str(home)}):
                    # Guard: the patched targets are inside the temp tree.
                    for target in (host_support.INSTALLED_PLUGIN, host_support.SCRATCH_DIR):
                        self.assertTrue(str(target.resolve()).startswith(
                            str(Path(tmp).resolve()) + os.sep), target)
                    inst = host_support.PluginInstall()
                    touched = {os.path.relpath(p, tmp) for p in inst.touched_paths()}
                    before = snapshot(Path(tmp))
                    inst.install()
                    installed = snapshot(Path(tmp))
                    inst.restore()
                    restored = snapshot(Path(tmp))

                scratch_rel = os.path.relpath(scratch, tmp)
                ancestors = {str(Path(t).parents[i]) for t in (*touched, scratch_rel)
                             for i in range(len(Path(t).parents) - 1)}
                ever = changed(before, installed) | changed(installed, restored)
                stray = {p for p in ever if p not in touched and p not in ancestors
                         and p != scratch_rel and not p.startswith(scratch_rel + os.sep)}
                self.assertEqual(stray, set(), "install/restore touched a path outside P3-R18")
                self.assertTrue(touched & changed(before, installed), "install really ran")
                self.assertEqual(touched, {os.path.relpath(plugin, tmp),
                                           os.path.relpath(dtconf / "luarc", tmp)})
                # After restore, everything outside the scratch area and the
                # created ancestor directories is exactly as it was.
                left = {p for p in changed(before, restored)
                        if p != scratch_rel and not p.startswith(scratch_rel + os.sep)}
                self.assertEqual(left - ancestors, set(), "restore left a change behind")

    def test_no_host_file_snapshots_the_whole_darktable_config(self) -> None:
        for path in Path(__file__).parent.glob("test_host*.py"):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"copytree\([^)]*darktable", text),
                                  "ruling R22 / P3-R18: never snapshot ~/.config/darktable wholesale")


class VersionTest(unittest.TestCase):
    def test_phase_and_version(self) -> None:
        # Product close (row 427): phase 3's code as the 1.0 release candidate.
        self.assertEqual((PHASE, __version__), (3, "1.0rc1"))
