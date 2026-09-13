"""The `.dtstyle` writer/reader -- a real module filter, pinned by a host probe.

Design section 9.2's schema (from darktable's `src/common/styles.c`) was
**unverified** -- a claim about another project's file format, never checked
against real output. Task 7 pinned it: `style.create_from_current` built a
style, the new bridge verb `style.export_file` (`dt.styles.export`) exported
it, and the real file darktable wrote is committed as
`tests/fixtures/hero.dtstyle` (docs/measurements-generated.md has the full
capture and the diff from section 9.2's draft, corrected in this task's
commit). This module is asserted against THAT fixture's element names and
nesting, never against the design doc's prose.

Two corrections the probe made, both load-bearing:

* There is no per-`<plugin>` `<iop_order>` float. Section 9.2 worried the
  writer might need one and have no source for it (the XMP carries none).
  The real file has no such element, so that worry does not apply and the
  module filter never has to fall back for this reason.
* `<info>` carries `<iop_list>`, not `<iop_order_version>` /
  `<iop_order_list>` as drafted -- a single comma-separated
  `name,priority,name,priority,...` enumeration of darktable's BUILT-IN
  pipeline order (every module the probe's whole-history style pulled in
  had one, and every priority value observed was `0`, i.e. "use the
  built-in order", matching this camera's own sidecar which carries
  `iop_order_version="4"`, docs/measurements-generated.md). The XMP has
  nothing resembling this list, so the writer emits `<iop_list></iop_list>`
  empty -- host-verified (docs/measurements-generated.md) that `dt.styles.
  import` accepts that and the imported style's `<plugin>` set round-trips
  correctly (darktable falls back to the built-in order on import, the same
  order `iop_order_version="4"` already means for this image).

`op_params` and `blendop_params` are copied VERBATIM from the XMP attribute
strings -- the same hex or `gz<level><base64>` text, never decoded and
re-encoded. Phase 1's decoder exists for reading; a round trip through it
would risk changing a single float, and a style is the one artefact that
lands on 300 frames. `modversion` and `blendop_version` likewise come
straight from the entry.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from scouter import paths
from scouter.core.xmp import ParsedXmp, active_entries

#: The exact `<plugin>` child order and names pinned by tests/fixtures/hero.dtstyle
#: (task 7 host probe, 2026-09-14). `test_style_dtstyle.py`'s
#: `test_build_matches_the_fixtures_element_shape` asserts this SET against
#: the fixture's own, so a darktable upgrade that adds/removes a child fails
#: loudly instead of silently.
PLUGIN_CHILDREN = (
    "num",
    "module",
    "operation",
    "op_params",
    "enabled",
    "blendop_params",
    "blendop_version",
    "multi_priority",
    "multi_name",
    "multi_name_hand_edited",
)


def build(parsed: ParsedXmp, name: str, description: str, operations: list[str]) -> str:
    """Render a `.dtstyle` carrying only `operations`, in the XMP's own order.

    `params` and `blendop_params` are copied VERBATIM from the XMP attribute
    strings -- the same hex or `gz<level><base64>` text. Phase 1's decoder
    exists for reading; a round trip through it would risk changing a single
    float, and a style is the one artefact that lands on 300 frames.

    `operations` is filtered through `xmp.active_entries` (task 7:
    newest-wins per (operation, multi_priority), so a module touched twice
    contributes exactly one `<plugin>`), never the raw entry list -- a
    superseded slider value must never ride into the style either.

    Escaping goes through `xml.etree.ElementTree` (never string
    concatenation), so a description containing `&` or `<` cannot produce a
    broken file.
    """
    wanted = set(operations)
    entries = [entry for entry in active_entries(parsed) if entry.operation in wanted]

    root = ET.Element("darktable_style", {"version": "1.0"})
    info = ET.SubElement(root, "info")
    ET.SubElement(info, "name").text = name
    ET.SubElement(info, "description").text = description
    # Host-pinned: the XMP carries nothing resembling darktable's own
    # <iop_list> (a full built-in-pipeline enumeration, not per-image data),
    # so this is always empty -- darktable falls back to the built-in order
    # on import (see the module docstring; docs/measurements-generated.md).
    ET.SubElement(info, "iop_list").text = ""

    style_el = ET.SubElement(root, "style")
    for entry in entries:
        plugin = ET.SubElement(style_el, "plugin")
        ET.SubElement(plugin, "num").text = str(entry.num)
        ET.SubElement(plugin, "module").text = str(entry.modversion)
        ET.SubElement(plugin, "operation").text = entry.operation
        ET.SubElement(plugin, "op_params").text = entry.params_hex
        ET.SubElement(plugin, "enabled").text = "1" if entry.enabled else "0"
        ET.SubElement(plugin, "blendop_params").text = entry.blendop_params_hex
        ET.SubElement(plugin, "blendop_version").text = str(entry.blendop_version)
        ET.SubElement(plugin, "multi_priority").text = str(entry.multi_priority)
        ET.SubElement(plugin, "multi_name").text = entry.multi_name
        ET.SubElement(plugin, "multi_name_hand_edited").text = (
            "1" if entry.multi_name_hand_edited else "0"
        )

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def write(path: Path, text: str) -> Path:
    """Write `text` to `path`, creating parent directories as needed."""
    paths.ensure(path)
    path.write_text(text, encoding="utf-8")
    return path


def read_operations(path: Path) -> list[str]:
    """The `<operation>` list of a `.dtstyle`, in file order.

    Used by `test_fixture_parses_and_lists_operations` (the real, committed
    `.dtstyle`) and by `style.extract`'s post-import verification (the
    written-and-requested set must match, or `StyleWriteFailed`).
    """
    root = ET.fromstring(Path(path).read_text(encoding="utf-8"))
    style_el = root.find("style")
    if style_el is None:
        return []
    return [plugin.findtext("operation") or "" for plugin in style_el.findall("plugin")]
