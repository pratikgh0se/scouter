"""Row 418/422 follow-up: library.db history reads, the new commit strategies,
and `scouter doctor --probe-main-thread`. Fakes only -- no darktable."""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scouter.cli import main as cli_main
from scouter.core import dtbridge, history
from scouter.errors import CommitTimeout, UsageError
from tests.scouter_fakes import FakeBridge, TempCache

#: exposure v7's 28-byte params (the same bytes MINIMAL_STYLE_SIDECAR carries):
#: mode 0, black -0.000244, exposure 0.7, ...
EXPOSURE_V7 = bytes.fromhex("00000000000080b93333333f00004842000080c00100000001000000")


def make_library(path: Path, image_id: int = 12, history_end: int = 2) -> None:
    """A minimal library.db with darktable 5.4.1's history/images columns."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE images (id INTEGER PRIMARY KEY, history_end INTEGER);
        CREATE TABLE history (imgid INTEGER, num INTEGER, module INTEGER,
          operation VARCHAR(256), op_params BLOB, enabled INTEGER,
          blendop_params BLOB, blendop_version INTEGER, multi_priority INTEGER,
          multi_name VARCHAR(256), multi_name_hand_edited INTEGER);
        CREATE TABLE module_order (imgid INTEGER PRIMARY KEY, version INTEGER, iop_list VARCHAR);
        CREATE TABLE history_hash (imgid INTEGER PRIMARY KEY, basic_hash BLOB,
          auto_hash BLOB, current_hash BLOB);
        """)
    conn.execute("INSERT INTO images VALUES (?, ?)", (image_id, history_end))
    rows = [
        (image_id, 0, 7, "exposure", EXPOSURE_V7, 1, b"\x01\x02", 14, 0, "", 0),
        (image_id, 1, 3, "mystery", b"\xde\xad", 0, b"", 14, 0, "second", 1),
        (image_id, 2, 7, "exposure", EXPOSURE_V7, 1, b"", 14, 0, "", 0),
    ]
    conn.executemany("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO module_order VALUES (?, 4, '')", (image_id,))
    conn.execute("INSERT INTO history_hash VALUES (?, NULL, NULL, ?)", (image_id, b"\xab\xcd"))
    conn.commit()
    conn.close()


class ReadDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.addCleanup(self.cache.stop)
        self.raw = root / "P1.ORF"
        self.raw.write_bytes(b"raw")
        self.lib = root / "library.db"
        make_library(self.lib)
        dtbridge.set_bridge(
            FakeBridge({"image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)}})
        )
        self.addCleanup(dtbridge.set_bridge, None)
        # Never let a real running darktable on the test host leak in.
        patcher = mock.patch.object(history, "_running_darktable_library", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._env = os.environ.pop("SCOUTER_DARKTABLE_LIBRARY_DB", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._env is not None:
            os.environ["SCOUTER_DARKTABLE_LIBRARY_DB"] = self._env

    def test_reads_every_row_with_decoding_and_the_history_end(self) -> None:
        result = history.read_db(str(self.raw), library=str(self.lib))
        self.assertEqual(result.source, "db")
        self.assertTrue(result.confirmed)
        self.assertEqual(result.history_end, 2)
        self.assertEqual(result.iop_order_version, 4)
        self.assertEqual(result.current_hash, "abcd")
        self.assertEqual([e.num for e in result.entries], [0, 1, 2])
        first = result.entries[0]
        self.assertEqual(first.operation, "exposure")
        self.assertEqual(first.modversion, 7)
        self.assertTrue(first.enabled)
        self.assertTrue(first.active)
        self.assertEqual(first.params_hex, EXPOSURE_V7.hex())
        self.assertEqual(first.blendop_params_hex, "0102")
        self.assertEqual(first.blendop_version, 14)
        assert first.decoded is not None
        self.assertAlmostEqual(first.decoded["exposure"], 0.7, places=5)
        mystery = result.entries[1]
        self.assertFalse(mystery.enabled)
        self.assertIsNone(mystery.decoded)
        self.assertEqual(mystery.multi_name, "second")
        self.assertTrue(mystery.multi_name_hand_edited)
        self.assertFalse(result.entries[2].active, "num >= history_end is the redo tail")
        self.assertTrue(any("lags the darkroom" in w for w in result.warnings))
        self.assertTrue(any("mystery" in w for w in result.warnings))

    def test_db_params_decode_the_same_as_the_xmp_hex(self) -> None:
        from scouter.core import xmp

        result = history.read_db(str(self.raw), library=str(self.lib))
        self.assertEqual(xmp.decode_params(result.entries[0].params_hex), EXPOSURE_V7)

    def test_the_file_is_opened_read_only(self) -> None:
        before = self.lib.read_bytes()
        history.read_db(str(self.raw), library=str(self.lib))
        self.assertEqual(self.lib.read_bytes(), before)

    def test_an_image_missing_from_the_library_names_the_wrong_library(self) -> None:
        other = self.lib.parent / "other.db"
        make_library(other, image_id=99)
        with self.assertRaises(UsageError) as ctx:
            history.read_db(str(self.raw), library=str(other))
        self.assertIn("not the library", ctx.exception.message)

    def test_read_source_db_delegates(self) -> None:
        result = history.read(str(self.raw), source="db", library=str(self.lib))
        self.assertEqual(result.source, "db")

    def test_read_source_auto_falls_back_to_the_db_without_a_sidecar(self) -> None:
        result = history.read(str(self.raw), source="auto", library=str(self.lib))
        self.assertEqual(result.source, "db")
        self.assertIn("fell back to library.db", result.warnings[0])

    def test_read_default_is_still_the_xmp(self) -> None:
        result = history.read(str(self.raw))
        self.assertEqual(result.source, "xmp")
        self.assertFalse(result.confirmed, "no sidecar -> unconfirmed, exactly as before")

    def test_an_unknown_source_is_a_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            history.read(str(self.raw), source="memory")

    def test_library_resolution_order(self) -> None:
        path, how = history.darktable_library_db(str(self.lib))
        self.assertEqual((path, how), (self.lib, "library argument"))
        os.environ["SCOUTER_DARKTABLE_LIBRARY_DB"] = str(self.lib)
        try:
            path, how = history.darktable_library_db()
            self.assertEqual(how, "$SCOUTER_DARKTABLE_LIBRARY_DB")
        finally:
            del os.environ["SCOUTER_DARKTABLE_LIBRARY_DB"]
        with mock.patch.object(history, "_running_darktable_library", return_value=self.lib):
            path, how = history.darktable_library_db()
            self.assertEqual(how, "running darktable --library")

    def test_no_library_anywhere_is_a_usage_error_naming_every_candidate(self) -> None:
        with mock.patch(
            "scouter.core.backup._darktable_library_db_candidates",
            return_value=[("default", self.lib.parent / "nope.db")],
        ):
            with self.assertRaises(UsageError) as ctx:
                history.darktable_library_db()
        self.assertIn("nope.db", ctx.exception.message)


class RunningLibraryTest(unittest.TestCase):
    def _ps(self, text: str):
        return mock.patch.object(
            history.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=text, stderr=""),
        )

    def test_parses_the_gui_process_library_flag(self) -> None:
        ps = (
            "/usr/bin/login -pf pratik\n"
            "/Applications/darktable.app/Contents/MacOS/darktable-cli --library /x.db\n"
            "/Applications/darktable.app/Contents/MacOS/darktable --library "
            "/Users/p/bench/library.db /Users/p/frame.ORF\n"
        )
        with self._ps(ps):
            self.assertEqual(
                history._running_darktable_library(), Path("/Users/p/bench/library.db")
            )

    def test_equals_form_and_absent(self) -> None:
        with self._ps("/opt/darktable --library=/a/lib.db\n"):
            self.assertEqual(history._running_darktable_library(), Path("/a/lib.db"))
        with self._ps("/opt/darktable\n/bin/zsh\n"):
            self.assertIsNone(history._running_darktable_library())


class CommitStrategyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.addCleanup(self.cache.stop)
        self.raw = root / "P1.ORF"
        self.raw.write_bytes(b"raw")
        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "history.commit": {
                    "strategy_used": "no_bounce",
                    "ok": False,
                    "view_after": "darkroom",
                },
            }
        )
        dtbridge.set_bridge(self.bridge)
        self.addCleanup(dtbridge.set_bridge, None)

    def _params(self) -> dict:
        return [p for m, p in self.bridge.calls if m == "history.commit"][-1]

    def test_no_bounce_names_the_image(self) -> None:
        with self.assertRaises(CommitTimeout):
            history.commit(image=str(self.raw), timeout_s=0.1, strategy="no_bounce")
        self.assertEqual(self._params()["strategy"], "no_bounce")
        self.assertEqual(self._params()["image_id"], 12)

    def test_view_bounce_action_passes_through_without_an_image_id(self) -> None:
        with self.assertRaises(CommitTimeout):
            history.commit(image=str(self.raw), timeout_s=0.1, strategy="view_bounce_action")
        self.assertEqual(self._params()["strategy"], "view_bounce_action")
        self.assertNotIn("image_id", self._params())

    def test_an_unknown_strategy_never_reaches_the_bridge(self) -> None:
        with self.assertRaises(UsageError):
            history.commit(image=str(self.raw), timeout_s=0.1, strategy="teleport")
        self.assertEqual(self.bridge.count("history.commit"), 0)

    def test_the_registry_offers_every_strategy(self) -> None:
        from scouter import registry

        verb = registry.load_verbs()["history.commit"]
        spec = next(a for a in verb.args if a.name == "strategy")
        self.assertEqual(tuple(spec.choices), history.COMMIT_STRATEGIES)


PROBE = {
    "api_version": "9.6.0",
    "view": "darkroom",
    "view_switch_transport": "proxy",
    "recommended_view_transport": "action",
    "rows": [
        {
            "route": "a_lua_shortcut",
            "candidate": "lua/darktable_mcp_main_thread_proxy",
            "ran": False,
            "action_return": "nan",
            "detail": "dt.gui.action returned nan: darktable refused it",
            "thread": "lua pool",
            "expect": "never runs",
        },
        {
            "route": "d2_switch_views_action",
            "candidate": "global/switch views/lighttable + global/switch views/darkroom",
            "ran": True,
            "detail": "lighttable=landed 150ms, darkroom=landed 900ms",
            "thread": "GTK main thread",
            "expect": "lands",
        },
        {
            "route": "d3_gui_mimic",
            "candidate": "dt.gui.mimic",
            "ran": False,
            "skipped": True,
            "detail": "excluded",
            "thread": "GTK main thread",
            "expect": "deadlock",
        },
    ],
}


class DoctorProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.bridge = FakeBridge({"darktable_mcp.probe_main_thread": PROBE})
        dtbridge.set_bridge(self.bridge)
        self.addCleanup(dtbridge.set_bridge, None)

    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main.main(["doctor", *argv])
        return code, out.getvalue()

    def test_prints_one_line_per_route_with_thread_and_expectation(self) -> None:
        code, text = self._run("--probe-main-thread")
        self.assertEqual(code, 0)
        self.assertIn("recommended action", text)
        self.assertIn("lua/darktable_mcp_main_thread_proxy", text)
        self.assertIn("returned nan", text)
        self.assertIn("skip", text)
        self.assertIn("thread: GTK main thread", text)
        self.assertIn("expect: deadlock", text)
        params = [p for m, p in self.bridge.calls if m == "darktable_mcp.probe_main_thread"][-1]
        self.assertIs(params["switch_views"], False)

    def test_switch_views_is_passed_through(self) -> None:
        self._run("--probe-main-thread", "--switch-views")
        params = [p for m, p in self.bridge.calls if m == "darktable_mcp.probe_main_thread"][-1]
        self.assertIs(params["switch_views"], True)

    def test_a_bridge_failure_is_a_json_error_not_a_traceback(self) -> None:
        from scouter.errors import BridgeTimeout

        dtbridge.set_bridge(
            FakeBridge(raises={"darktable_mcp.probe_main_thread": BridgeTimeout("no darktable")})
        )
        code, text = self._run("--probe-main-thread")
        self.assertEqual(code, 1)
        self.assertIn("bridge_timeout", text)

    def test_view_transport_helper(self) -> None:
        bridge = FakeBridge(
            {"darktable_mcp.view_transport": lambda p: {"transport": p.get("set", "proxy")}}
        )
        dtbridge.set_bridge(bridge)
        self.assertEqual(dtbridge.view_transport(), "proxy")
        self.assertEqual(dtbridge.view_transport("action"), "action")
        self.assertEqual(bridge.calls[-1], ("darktable_mcp.view_transport", {"set": "action"}))


if __name__ == "__main__":
    unittest.main()
