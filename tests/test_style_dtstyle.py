"""Task 7: the `.dtstyle` writer, asserted against a real darktable file.

`tests/fixtures/hero.dtstyle` is not invented -- it is the exact file the
real darktable wrote during the task 7 host probe (`tests/test_host_phase2.py
::test_pin_dtstyle_format`, SCOUTER_HOST_TESTS=1, docs/measurements-generated.md
has the full capture). `scouter.core.dtstyle.build`'s output is checked
against ITS element shape, never against the design doc's unverified draft.
"""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scouter.core import dtbridge, dtstyle, style, xmp
from scouter.errors import StyleWriteFailed, UsageError
from tests.scouter_fakes import FakeBridge, TempCache

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HERO_DTSTYLE = FIXTURES / "hero.dtstyle"
REFERENCE_XMP = FIXTURES / "P6281348.ORF.xmp"


class ReadOperationsTest(unittest.TestCase):
    def test_fixture_parses_and_lists_operations(self) -> None:
        self.assertTrue(HERO_DTSTYLE.is_file(), f"missing committed fixture {HERO_DTSTYLE}")
        operations = dtstyle.read_operations(HERO_DTSTYLE)
        self.assertTrue(operations, "the real .dtstyle carries at least one <plugin>")
        # The probe's whole-history style (task 7 host run) covers the full
        # built-in pipeline, so both edited modules from the reference XMP
        # show up in the real file.
        self.assertIn("exposure", operations)
        self.assertIn("sigmoid", operations)


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = xmp.parse(REFERENCE_XMP.read_text())

    def test_build_matches_the_fixtures_element_shape(self) -> None:
        """Set comparison (not exact order): a darktable upgrade that adds or
        removes an `<info>` or `<plugin>` child must fail this test loudly,
        rather than the writer silently diverging from real output."""
        fixture_root = ET.fromstring(HERO_DTSTYLE.read_text(encoding="utf-8"))
        built_text = dtstyle.build(self.parsed, "hero-harbour", "phase 2 hero", ["exposure"])
        built_root = ET.fromstring(built_text)

        self.assertEqual(built_root.tag, fixture_root.tag)
        self.assertEqual(built_root.get("version"), fixture_root.get("version"))

        fixture_info_children = {c.tag for c in fixture_root.find("info")}
        built_info_children = {c.tag for c in built_root.find("info")}
        self.assertEqual(built_info_children, fixture_info_children)

        fixture_plugins = fixture_root.find("style").findall("plugin")
        built_plugins = built_root.find("style").findall("plugin")
        self.assertTrue(built_plugins)
        fixture_plugin_children = {c.tag for c in fixture_plugins[0]}
        built_plugin_children = {c.tag for c in built_plugins[0]}
        self.assertEqual(built_plugin_children, fixture_plugin_children)

    def test_only_requested_operations_included(self) -> None:
        text = dtstyle.build(self.parsed, "hero", "", ["exposure", "sigmoid"])
        root = ET.fromstring(text)
        operations = [p.findtext("operation") for p in root.find("style").findall("plugin")]
        self.assertEqual(sorted(operations), ["exposure", "sigmoid"])

    def test_params_are_byte_identical_to_the_xmp(self) -> None:
        # num 11 is the newest (active-entries-winning) exposure entry in the
        # reference fixture; its blendop is a gz-prefixed value.
        entry = next(e for e in xmp.active_entries(self.parsed) if e.operation == "exposure")
        text = dtstyle.build(self.parsed, "hero", "", ["exposure"])
        root = ET.fromstring(text)
        plugin = root.find("style").find("plugin")
        self.assertEqual(plugin.findtext("op_params"), entry.params_hex)
        self.assertEqual(plugin.findtext("blendop_params"), entry.blendop_params_hex)
        self.assertTrue(entry.blendop_params_hex.startswith("gz"), "fixture blendop is gz-prefixed")

    def test_inactive_tail_excluded(self) -> None:
        # history_end=9: exposure's newest entry (num 11) becomes a redo-able
        # tail and must never ride into the style. num 8 (also exposure) is
        # still < 9? no -- with history_end=9, num 8 is active and num 11 is
        # not, so the surviving exposure entry is num 8, not 11.
        parsed = xmp.parse(REFERENCE_XMP.read_text().replace('history_end="14"', 'history_end="9"'))
        text = dtstyle.build(parsed, "hero", "", ["exposure"])
        root = ET.fromstring(text)
        plugins = root.find("style").findall("plugin")
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0].findtext("num"), "8")

        # history_end=8 excludes exposure entirely (num 8 is now the tail too).
        parsed_none = xmp.parse(
            REFERENCE_XMP.read_text().replace('history_end="14"', 'history_end="8"')
        )
        text_none = dtstyle.build(parsed_none, "hero", "", ["exposure"])
        root_none = ET.fromstring(text_none)
        self.assertEqual(root_none.find("style").findall("plugin"), [])

    def test_description_with_ampersand_is_escaped(self) -> None:
        text = dtstyle.build(self.parsed, "hero", "moody & warm", ["exposure"])
        # Must parse (ElementTree escaping did its job) and round-trip the
        # original text exactly.
        root = ET.fromstring(text)
        self.assertEqual(root.find("info").findtext("description"), "moody & warm")
        self.assertIn("moody &amp; warm", text)


class ExtractFilteredTest(unittest.TestCase):
    """`style.extract`'s default (filtered) path, exercised against the real
    reference XMP -- no darktable needed, since this path never touches the
    darkroom (it reads a file)."""

    def setUp(self) -> None:
        self.cache = TempCache()
        self.root = self.cache.start()
        self.raw = self.root / "P6281348.ORF"
        self.raw.write_bytes(b"raw")
        sidecar = self.raw.with_suffix(self.raw.suffix + ".xmp")
        sidecar.write_text(REFERENCE_XMP.read_text(), encoding="utf-8")

        self.bridge = FakeBridge(
            {
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "list_styles": {"styles": [], "count": 0},
                "style.import_file": lambda p: {
                    "name": p["name"],
                    "imported": True,
                    "replaced": bool(p.get("overwrite")),
                    "styles_before": 0,
                    "styles_after": 1,
                },
            }
        )
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def test_filtered_extract_writes_and_imports_the_requested_modules(self) -> None:
        result = style.extract(
            image=str(self.raw), name="hero", modules=["exposure", "sigmoid"], description=""
        )
        self.assertEqual(result.outcome, "ok")
        self.assertTrue(result.modules_honoured)
        self.assertEqual(sorted(result.operations_included), ["exposure", "sigmoid"])
        self.assertEqual(result.modules_missing, [])
        self.assertTrue(result.dtstyle_path and Path(result.dtstyle_path).is_file())
        self.assertNotIn("darkroom.current", self.bridge.methods_called())
        self.assertNotIn("style.create_from_current", self.bridge.methods_called())
        written = dtstyle.read_operations(result.dtstyle_path)
        self.assertEqual(sorted(written), ["exposure", "sigmoid"])

    def test_missing_module_is_reported_not_silently_dropped(self) -> None:
        result = style.extract(
            image=str(self.raw), name="hero", modules=["exposure", "nope-not-a-module"]
        )
        self.assertEqual(result.modules_missing, ["nope-not-a-module"])
        self.assertEqual(result.operations_included, ["exposure"])
        self.assertTrue(any("nope-not-a-module" in w for w in result.warnings))

    def test_never_include_module_refused(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            style.extract(image=str(self.raw), name="hero", modules=["exposure", "crop"])
        self.assertIn("crop", str(ctx.exception))
        self.assertNotIn("style.import_file", self.bridge.methods_called())

    def test_all_modules_missing_refused(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            style.extract(image=str(self.raw), name="hero", modules=["not-a-real-module"])
        self.assertIn("empty style is not a style", str(ctx.exception))
        self.assertNotIn("style.import_file", self.bridge.methods_called())

    def test_import_failure_keeps_the_file(self) -> None:
        from darktable_mcp.bridge.client import BridgeError

        self.bridge.raises["style.import_file"] = BridgeError("malformed .dtstyle")
        with self.assertRaises(StyleWriteFailed) as ctx:
            style.extract(image=str(self.raw), name="hero", modules=["exposure"])
        path = Path(ctx.exception.detail["path"])
        self.assertTrue(path.is_file(), "the .dtstyle must stay on disk for inspection")


if __name__ == "__main__":
    unittest.main()
