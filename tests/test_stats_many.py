"""`scout.stats_many`: the disk-cached, thread-pooled stats engine (task 5)."""

from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import dtbridge, scout, xmp
from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, TempCache, fake_cli_script

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _lookup_response(params: dict) -> dict:
    """A per-path `image.lookup` stand-in: echoes the path back as its own image."""
    path = params["path"]
    return {"found": True, "image_id": abs(hash(path)) % 100000, "path": path}


class StatsManyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        dtbridge.set_bridge(FakeBridge({"image.lookup": _lookup_response}))
        self._saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")
        script = fake_cli_script(self.root, FAKE_CLI_WRITES_PPM)
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(script)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        if self._saved_cli is None:
            os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
        else:
            os.environ["SCOUTER_DARKTABLE_CLI"] = self._saved_cli
        self.cache.stop()

    def _raw(self, name: str) -> Path:
        p = self.root / name
        p.write_bytes(b"raw")
        return p

    def test_cache_hit_on_second_call(self) -> None:
        raw = self._raw("a.ORF")
        real_render = scout.render
        calls: list[int] = []

        def counting_render(*args, **kwargs):
            calls.append(1)
            return real_render(*args, **kwargs)

        with mock.patch.object(scout, "render", side_effect=counting_render):
            first = scout.stats_many([str(raw)], px=64, workers=2)
            second = scout.stats_many([str(raw)], px=64, workers=2)

        self.assertEqual(len(calls), 1, "second call must be served from the disk cache")
        self.assertFalse(first[0]["cached"])
        self.assertTrue(second[0]["cached"])
        self.assertEqual(first[0]["path"], second[0]["path"])

    def test_cache_miss_when_xmp_mtime_advances(self) -> None:
        raw = self._raw("b.ORF")
        sidecar = xmp.sidecar_path(str(raw))
        sidecar.write_text("<x/>", encoding="utf-8")
        real_render = scout.render
        calls: list[int] = []

        def counting_render(*args, **kwargs):
            calls.append(1)
            return real_render(*args, **kwargs)

        with mock.patch.object(scout, "render", side_effect=counting_render):
            scout.stats_many([str(raw)], px=64, workers=1)
            newer = sidecar.stat().st_mtime + 5
            os.utime(sidecar, (newer, newer))
            scout.stats_many([str(raw)], px=64, workers=1)

        self.assertEqual(len(calls), 2, "a newer sidecar mtime must invalidate the cache entry")

    def test_one_failing_frame_does_not_fail_the_set(self) -> None:
        raws = [self._raw(f"f{i}.ORF") for i in range(5)]
        bad = str(raws[2])
        real_render = scout.render

        def flaky_render(raw_path, out_path, px, ext):
            if raw_path == bad:
                raise RuntimeError("simulated render failure")
            return real_render(raw_path, out_path, px, ext)

        with mock.patch.object(scout, "render", side_effect=flaky_render):
            results = scout.stats_many([str(p) for p in raws], px=64, workers=3)

        self.assertEqual(len(results), 5)
        errors = [r for r in results if "error" in r]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["path"], bad)
        self.assertIn("code", errors[0]["error"])
        self.assertIn("message", errors[0]["error"])
        ok = [r for r in results if "error" not in r]
        self.assertEqual(len(ok), 4)

    def test_render_configdir_is_unique_per_call_across_worker_threads(self) -> None:
        """Carried finding 8 (task 5's nit): the old name,
        `test_workers_do_not_share_a_configdir`, promised more than this
        test delivers. It patches `scout.render` OUT, so what it actually
        proves is narrower and worth stating: `_render_configdir()` hands
        every caller a distinct directory even when four worker threads call
        it at once. That the REAL `render` uses that directory and deletes
        it afterwards is pinned separately, by
        `tests/test_scout_core.py::test_two_renders_use_different_configdirs`
        and `::test_render_configdir_is_removed_after_the_call`.
        """
        raws = [self._raw(f"w{i}.ORF") for i in range(4)]
        recorded: list[str] = []
        lock = threading.Lock()

        def fake_render(raw_path, out_path, px, ext):
            configdir = scout._render_configdir()
            with lock:
                recorded.append(str(configdir))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"P6\n2 1\n255\n" + bytes([255, 0, 0, 0, 255, 0]))
            return 1

        with mock.patch.object(scout, "render", side_effect=fake_render):
            scout.stats_many([str(p) for p in raws], px=64, workers=4)

        self.assertEqual(
            len(set(recorded)), 4, "each render call must get its own isolated configdir"
        )

    def test_order_is_preserved(self) -> None:
        raws = [self._raw(f"o{i}.ORF") for i in range(6)]
        paths_in = [str(p) for p in raws]
        results = scout.stats_many(paths_in, px=64, workers=4)
        self.assertEqual([r["path"] for r in results], paths_in)

    def test_job_dir_mirror_written(self) -> None:
        raw = self._raw("j.ORF")
        job_dir = self.root / "job"
        results = scout.stats_many([str(raw)], px=64, workers=1, job_dir=job_dir)
        mirror = job_dir / "stats-64.json"
        self.assertTrue(mirror.exists())
        self.assertEqual(json.loads(mirror.read_text()), results)

    def test_rating_regex(self) -> None:
        # The committed real sidecar (tests/fixtures/P6281348.ORF.xmp) carries
        # xmp:Rating="1"; a sidecar with no attribute at all returns None.
        self.assertEqual(xmp.rating(FIXTURES / "P6281348.ORF"), 1)
        no_rating = self.root / "no-rating.ORF"
        no_rating.write_bytes(b"raw")
        sidecar = xmp.sidecar_path(str(no_rating))
        sidecar.write_text('<x xmp:foo="1"/>', encoding="utf-8")
        self.assertIsNone(xmp.rating(str(no_rating)))


if __name__ == "__main__":
    unittest.main()
