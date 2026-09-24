"""E12 D1: `cull propose` must score the frame as imported, never its current
edited history. `scout.render` never passes an explicit sidecar to
darktable-cli, so a plain render of the real raw path picks up whatever
`.xmp` already sits beside it (darktable-cli's own auto-detection) -- the
CURRENT edit, not the frame as imported. `unedited=True` renders from a
scratch copy with no adjacent sidecar instead.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import dtbridge, scout, xmp
from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, TempCache, fake_cli_script


def _lookup_response(params: dict) -> dict:
    path = params["path"]
    return {"found": True, "image_id": abs(hash(path)) % 100000, "path": path}


class UneditedRenderTest(unittest.TestCase):
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

    def _raw_with_sidecar(self, name: str) -> Path:
        p = self.root / name
        p.write_bytes(b"raw-bytes")
        xmp.sidecar_path(str(p)).write_text("<x/>", encoding="utf-8")
        return p

    def test_unedited_render_uses_a_path_with_no_sidecar(self) -> None:
        raw = self._raw_with_sidecar("hero.ORF")
        real_render = scout.render
        seen: list[str] = []

        def spy_render(raw_path, out_path, px, ext, **kwargs):
            seen.append(raw_path)
            return real_render(raw_path, out_path, px, ext, **kwargs)

        with mock.patch.object(scout, "render", side_effect=spy_render):
            scout.stats_many([str(raw)], px=64, workers=1, unedited=True)

        self.assertEqual(len(seen), 1)
        rendered_path = seen[0]
        self.assertNotEqual(rendered_path, str(raw), "must not render the real raw in place")
        self.assertFalse(
            xmp.sidecar_path(rendered_path).exists(),
            "the scratch copy must have no adjacent sidecar for darktable-cli to auto-apply",
        )

    def test_unedited_scratch_copy_is_cleaned_up(self) -> None:
        raw = self._raw_with_sidecar("clean.ORF")
        before = set(self.root.rglob("unedited-*"))
        scout.stats_many([str(raw)], px=64, workers=1, unedited=True)
        after = set(self.root.rglob("unedited-*"))
        self.assertEqual(before, after, "no scratch copy must be left behind")

    def test_default_render_still_uses_the_real_path(self) -> None:
        """Regression: every OTHER caller (style, outliers) must keep scoring
        the current edited history -- only cull opts into `unedited`."""
        raw = self._raw_with_sidecar("normal.ORF")
        real_render = scout.render
        seen: list[str] = []

        def spy_render(raw_path, out_path, px, ext, **kwargs):
            seen.append(raw_path)
            return real_render(raw_path, out_path, px, ext, **kwargs)

        with mock.patch.object(scout, "render", side_effect=spy_render):
            scout.stats_many([str(raw)], px=64, workers=1)

        self.assertEqual(seen, [str(raw)])

    def test_unedited_and_edited_cache_entries_do_not_collide(self) -> None:
        raw = self._raw_with_sidecar("cached.ORF")
        edited = scout.stats_many([str(raw)], px=64, workers=1, unedited=False)
        unedited = scout.stats_many([str(raw)], px=64, workers=1, unedited=True)
        self.assertFalse(edited[0]["cached"])
        self.assertFalse(unedited[0]["cached"], "must be a separate cache key, not a hit")


if __name__ == "__main__":
    unittest.main()
