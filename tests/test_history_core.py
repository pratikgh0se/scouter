import shutil
import time
import unittest
from pathlib import Path

from scouter.core import dtbridge, history
from scouter.errors import CommitTimeout
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"


class HistoryReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        dtbridge.set_bridge(
            FakeBridge(
                {
                    "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                    "darkroom.open": {
                        "image_id": 12,
                        "path": str(self.raw),
                        "view": "darkroom",
                        "opened": False,
                        "already_open": True,
                    },
                }
            )
        )

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

    def test_explicit_xmp_with_missing_sidecar_is_no_sidecar(self) -> None:
        """rob-r5: asking for the sidecar by name when it is absent is an error."""
        from scouter.errors import NoSidecar

        self.sidecar.unlink()
        with self.assertRaises(NoSidecar) as ctx:
            history.read(image=str(self.raw), source="xmp")
        self.assertEqual(ctx.exception.code, "no_sidecar")

    def test_auto_with_missing_sidecar_stays_ok(self) -> None:
        # Unchanged: auto never raises no_sidecar; it falls back to library.db
        # (entries depend on the host library) and says so.
        # Hermetic: an explicit fixture library holding image 12, never whatever
        # library the running darktable (e.g. a gauntlet session) is using.
        import sqlite3

        lib = self.raw.parent / "library.db"
        conn = sqlite3.connect(lib)
        conn.executescript(
            "CREATE TABLE images (id INTEGER PRIMARY KEY, history_end INTEGER);"
            "CREATE TABLE history (imgid INTEGER, num INTEGER, module INTEGER, operation TEXT,"
            " op_params BLOB, enabled INTEGER, blendop_params BLOB, blendop_version INTEGER,"
            " multi_priority INTEGER, multi_name TEXT, multi_name_hand_edited INTEGER);"
            "INSERT INTO images VALUES (12, 0);")
        conn.commit()
        conn.close()
        self.sidecar.unlink()
        result = history.read(image=str(self.raw), source="auto", library=str(lib))
        self.assertTrue(any("no sidecar" in w for w in result.warnings))

    def test_explicit_xmp_with_existing_sidecar_is_unchanged(self) -> None:
        result = history.read(image=str(self.raw), source="xmp")
        self.assertTrue(result.confirmed)
        self.assertEqual(result.history_end, 14)

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
            "darkroom.current": {
                "image_id": 12,
                "path": str(self.raw),
                "view": "darkroom",
                "source": "action_images",
            },
            "darkroom.open": {
                "image_id": 12,
                "path": str(self.raw),
                "view": "darkroom",
                "opened": False,
                "already_open": True,
            },
        }

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_commit_succeeds_when_the_sidecar_mtime_advances(self) -> None:
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(with_new_history_hash(text))  # a real rewrite bumps the mtime
            return {
                "strategy_used": "sidecar_action",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 10,
            }

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        result = history.commit(image=str(self.raw), timeout_s=2.0)
        self.assertTrue(result.committed)
        self.assertEqual(result.strategy_used, "sidecar_action")

    def test_a_metadata_only_rewrite_is_not_a_commit(self) -> None:
        # Row 430 (bench 20260926t073459z E1/W1/S2): the no_bounce rating
        # touch re-synched the XMP from a library.db the darkroom never
        # flushed to -- new change_timestamp, new mtime, SAME history -- and
        # every relative move reported committed=true with the edit missing.
        # A rewrite whose history is byte-identical must not count.
        text = self.sidecar.read_text()
        self.assertIn('darktable:change_timestamp="', text)
        touched = text.replace('darktable:change_timestamp="', 'darktable:change_timestamp="9', 1)

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(touched)
            return {
                "strategy_used": "no_bounce",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 10,
                "sidecar_written": True,
            }

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit(image=str(self.raw), timeout_s=0.3)
        why = ctx.exception.detail.get("sidecar_rewritten_history_unchanged")
        self.assertIn("history", str(why))

    def test_commit_times_out_with_the_strategy_in_the_detail(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(
                {
                    **self.base,
                    "history.commit": {
                        "strategy_used": "view_bounce",
                        "ok": False,
                        "view_after": "darkroom",
                        "cpu_ms": 5,
                        "detail": "view switch failed",
                    },
                }
            )
        )
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit(image=str(self.raw), timeout_s=0.2)
        self.assertEqual(ctx.exception.detail["strategy_used"], "view_bounce")
        self.assertIn("view switch failed", str(ctx.exception.detail["bridge_detail"]))

    def test_strategy_is_forwarded_to_the_bridge(self) -> None:
        bridge = FakeBridge(
            {
                **self.base,
                "history.commit": {
                    "strategy_used": "view_bounce",
                    "ok": True,
                    "view_after": "darkroom",
                    "cpu_ms": 5,
                },
            }
        )
        dtbridge.set_bridge(bridge)
        with self.assertRaises(CommitTimeout):
            history.commit(image=str(self.raw), timeout_s=0.1, strategy="view_bounce")
        params = dict(bridge.calls[-1][1])
        self.assertEqual(params["strategy"], "view_bounce")

    def test_commit_defaults_to_the_darkroom_image(self) -> None:
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(with_new_history_hash(text))
            return {
                "strategy_used": "view_bounce",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 5,
            }

        bridge = FakeBridge({**self.base, "history.commit": commit})
        dtbridge.set_bridge(bridge)
        result = history.commit(timeout_s=2.0)
        self.assertEqual(result.image_id, 12)
        # Final review, blocker 1: the darkroom image is READ through
        # `darkroom.current`, never through `darkroom.open {}` -- which the
        # real plugin refuses outright (FakeBridge now refuses it too, so this
        # assertion can actually fail).
        self.assertIn("darkroom.current", bridge.methods_called())
        self.assertNotIn("darkroom.open", bridge.methods_called())

    def test_commit_without_an_image_refuses_when_the_darkroom_is_empty(self) -> None:
        """No image loaded is an honest `not_in_darkroom`, not a crash."""
        from scouter.errors import NotInDarkroom

        dtbridge.set_bridge(
            FakeBridge(
                {
                    **self.base,
                    "darkroom.current": {"view": "lighttable", "source": "action_images"},
                }
            )
        )
        with self.assertRaises(NotInDarkroom) as ctx:
            history.commit(timeout_s=0.2)
        self.assertIn("nothing to commit", str(ctx.exception))

    def test_commit_surfaces_view_after_and_warns_off_darkroom(self) -> None:
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(with_new_history_hash(text))
            return {
                "strategy_used": "sidecar_action",
                "ok": True,
                "view_after": "lighttable",
                "cpu_ms": 5,
            }

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        result = history.commit(image=str(self.raw), timeout_s=2.0)
        self.assertEqual(result.view_after, "lighttable")
        self.assertTrue(any("lighttable" in w for w in result.warnings))

    def test_commit_does_not_warn_when_the_view_ends_in_darkroom(self) -> None:
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)
            self.sidecar.write_text(with_new_history_hash(text))
            return {
                "strategy_used": "sidecar_action",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 5,
            }

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        result = history.commit(image=str(self.raw), timeout_s=2.0)
        self.assertFalse(any("view" in w for w in result.warnings))

    def test_timeout_detail_carries_ok_and_view_after(self) -> None:
        dtbridge.set_bridge(
            FakeBridge(
                {
                    **self.base,
                    "history.commit": {
                        "strategy_used": "view_bounce",
                        "ok": False,
                        "view_after": "darkroom",
                        "cpu_ms": 5,
                        "detail": "view switch failed",
                    },
                }
            )
        )
        with self.assertRaises(CommitTimeout) as ctx:
            history.commit(image=str(self.raw), timeout_s=0.1)
        self.assertEqual(ctx.exception.detail["ok"], False)
        self.assertEqual(ctx.exception.detail["view_after"], "darkroom")

    def test_final_check_after_the_loop_still_catches_a_late_write(self) -> None:
        # `timeout_s=0` puts the deadline in the past by the time the `while`
        # loop is reached (the synchronous bridge call above it already took
        # nonzero time), so the loop itself runs zero iterations. Only the
        # post-loop "one last look" can still see the write the bridge call
        # made -- this is what fix round 1, point 3 added.
        text = self.sidecar.read_text()

        def commit(_params: dict) -> dict:
            time.sleep(0.01)  # guarantee the mtime ticks
            self.sidecar.write_text(with_new_history_hash(text))
            return {
                "strategy_used": "sidecar_action",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 1,
            }

        dtbridge.set_bridge(FakeBridge({**self.base, "history.commit": commit}))
        result = history.commit(image=str(self.raw), timeout_s=0.0)
        self.assertTrue(result.committed)
