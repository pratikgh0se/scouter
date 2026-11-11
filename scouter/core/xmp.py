"""The read path: darktable's XMP sidecar.

Lua API 9.6.0 has no image.history, so this is the only way to learn what an
image's history actually contains. darktable writes the sidecar on commit (see
core/history.commit), so a read is always preceded by a commit or an explicit
`confirmed=False`.

Layouts below are decoded from the real sidecar the phase 0 spike produced
(tests/fixtures/P6281348.ORF.xmp) -- not from darktable's source, and not from
memory. An unknown module, an unknown modversion, or a length mismatch returns
the raw hex with a reason. Never a guessed value.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from scouter.types import HistoryEntry

DT_NS = "http://darktable.sf.net/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

#: One decoder per (operation, modversion). `fields` is (name, struct_format,
#: byte_offset); `size` is the exact params length the layout was verified at.
#: Extend only with a layout proven against a committed fixture.
#:
#: Honesty rule (controller ruling 6): a field is only named when its meaning
#: is actually known. `sigmoid` names slot 0 (`contrast`, the only slot the
#: phase 0 spike pinned); the remaining floats are exposed as unnamed
#: `f1..fN` via `named_slots` rather than invented names. `colorbalancergb`
#: is the same story one level further back -- no slot is pinned yet, so the
#: whole module decodes as an unnamed float vector until task 12's host
#: probe fixes the slot map.
LAYOUTS: dict[tuple[str, int], dict] = {
    ("exposure", 7): {
        "size": 28,
        "fields": (
            ("mode", "<i", 0),
            ("black", "<f", 4),
            ("exposure", "<f", 8),
            ("deflicker_percentile", "<f", 12),
            ("deflicker_target_level", "<f", 16),
            ("compensate_exposure_bias", "<i", 20),
            # Ruling 5: the seventh field (bytes 24-27) is decoded, not
            # dropped. Its meaning is unknown, so it is named `_tail` rather
            # than guessed -- the honesty rule applies to naming too.
            ("_tail", "<i", 24),
        ),
        "verified": "tests/fixtures/P6281348.ORF.xmp num 11 -> exposure 1.5500002",
    },
    ("sigmoid", 3): {
        "size": 56,
        # Ruling 6: only slot 0 (`contrast`) is a proven name. The other 13
        # floats are exposed as unnamed f1..f13 (decode_module numbers them
        # by their 0-based slot index) -- no invented field names.
        # Phase 3 task 10 host run, 2026-09-24: skew sentinel 0.16 appeared
        # at exactly one slot (1), as the editor benchmark's 0.2 did (f1).
        "named_slots": {0: "contrast", 1: "skew"},
        "float_count": 14,
        "verified": "tests/fixtures/P6281348.ORF.xmp num 12 -> contrast 1.4; "
        "tests/fixtures/phase3-host.xmp -> skew 0.16",
    },
    ("temperature", 4): {
        "size": 20,
        "fields": (
            ("coeff_r", "<f", 0),
            ("coeff_g", "<f", 4),
            ("coeff_b", "<f", 8),
            ("coeff_g2", "<f", 12),
            ("preset", "<i", 16),
        ),
        "verified": "tests/fixtures/P6281348.ORF.xmp num 13 -> 2.0125747/1.0/1.7052786",
    },
    # colorbalancergb's slot map, pinned by tests/fixtures/colorbalancergb.xmp
    # (task 12's host probe, test_host_integration.py
    # test_colorbalancergb_slot_probe_pins_the_layout): the probe enables the
    # module, sets `contrast` to 0.2 through the bridge, commits, and reads
    # the value back from the sidecar darktable itself wrote. 0.2 appeared at
    # exactly one float slot (31 of 33), so only that slot is named -- the
    # remaining 32 floats are exposed as unnamed f0..f32 (honesty rule,
    # controller ruling 6: a field is only named when its meaning is proven).
    ("colorbalancergb", 5): {
        "size": 132,
        # Phase 3 task 10 host run, 2026-09-24 (sentinel method, one launch):
        # each sentinel appeared at exactly one float slot. 19 matches the
        # editor benchmark's finding (global saturation = f19).
        "named_slots": {
            0: "shadows_luminance",
            1: "shadows_chroma",
            2: "shadows_hue",
            3: "midtones_luminance",
            4: "midtones_chroma",
            5: "midtones_hue",
            6: "highlights_luminance",
            7: "highlights_chroma",
            8: "highlights_hue",
            17: "global_chroma",
            19: "global_saturation",
            29: "global_vibrance",
            31: "contrast",
        },
        "float_count": 33,
        "verified": "tests/fixtures/colorbalancergb.xmp (task 12 host probe); "
        "tests/fixtures/phase3-host.xmp (phase 3 task 10)",
    },
    # Phase 3 task 10 host run, 2026-09-24: filmic rgb v6 (116 bytes, 29
    # four-byte slots). Sentinels 4.96 (white relative exposure) and 1.451
    # (contrast) each appeared at exactly one slot.
    ("filmicrgb", 6): {
        "size": 116,
        # black: sentinel -9.94 at exactly one slot (1), second launch.
        "named_slots": {1: "black_relative_exposure", 2: "white_relative_exposure", 14: "contrast"},
        "float_count": 29,
        "verified": "tests/fixtures/phase3-host.xmp (phase 3 task 10)",
    },
    # Route A for warmth (decision rows 458/461): color calibration v3, 160
    # bytes. Field order and types from darktable 5.4's
    # `dt_iop_channelmixer_rgb_params_t` (src/iop/channelmixerrgb.c): six
    # float[4] channel rows (bytes 0-95, left unnamed), six gboolean
    # normalize flags (96-119), then the fields below. Checked against the
    # darktable-written entry in tests/fixtures/P6281348.ORF.xmp (num 7):
    # illuminant 2 (DT_ILLUMINANT_D), adaptation 1 (CAT16), x 0.3829,
    # y 0.3739 and temperature 3926.67 K -- McCamy's CCT of that (x, y) is
    # ~3923 K, so the slot is the illuminant temperature, not a coincidence.
    # The sentinel pin of `temperature` is still owed by the host probe
    # (tests/test_host_colorcal.py); until then the vocabulary row stays
    # `candidate` because its action path is not pinned.
    ("channelmixerrgb", 3): {
        "size": 160,
        "fields": (
            ("illuminant", "<i", 120),
            ("illum_fluo", "<i", 124),
            ("illum_led", "<i", 128),
            ("adaptation", "<i", 132),
            ("x", "<f", 136),
            ("y", "<f", 140),
            ("temperature", "<f", 144),
            ("gamut", "<f", 148),
            ("clip", "<i", 152),
            ("version", "<i", 156),
        ),
        "verified": "tests/fixtures/P6281348.ORF.xmp num 7 -> illuminant 2, temperature 3926.67",
    },
    # Phase 3 task 10 host run, 2026-09-24: color zones v5 (520 bytes = 130
    # four-byte slots; some are ints, which only the unnamed slots expose).
    # The mix ("strength") sentinel 0.16 appeared at exactly one slot (127).
    ("colorzones", 5): {
        "size": 520,
        "named_slots": {127: "strength"},
        "float_count": 130,
        "verified": "tests/fixtures/phase3-host.xmp (phase 3 task 10)",
    },
}


@dataclass
class ParsedXmp:
    history_end: int = 0
    xmp_version: int = 0
    iop_order_version: int = 0
    current_hash: str | None = None
    entries: list[HistoryEntry] = field(default_factory=list)
    well_formed: bool = True


def sidecar_path(raw_path: str | Path) -> Path:
    """darktable's sidecar for `raw_path`: the full filename plus `.xmp`."""
    return Path(str(raw_path) + ".xmp")


def read_sidecar(raw_path: str | Path) -> tuple[str, float]:
    """Return (text, mtime). Raises FileNotFoundError when there is no sidecar."""
    path = sidecar_path(raw_path)
    stat = path.stat()
    return path.read_text(encoding="utf-8", errors="replace"), stat.st_mtime


def decode_params(raw: str) -> bytes:
    """Decode a `darktable:params` value.

    Two encodings occur in real sidecars: plain lowercase hex, and
    `gz<level><base64>` whose payload is zlib-compressed (darktable uses it for
    anything long -- every `blendop_params` in the reference fixture is `gz11`).
    """
    if raw.startswith("gz"):
        payload = raw[4:] if raw[2:4].isdigit() else raw[2:]
        return zlib.decompress(base64.b64decode(payload))
    return binascii.unhexlify(raw)


def decode_module(operation: str, modversion: int, blob: bytes) -> tuple[dict | None, str | None]:
    """Decode one module's params. Returns (decoded, reason_it_could_not_be)."""
    layout = LAYOUTS.get((operation, modversion))
    if layout is None:
        return None, f"no layout for {operation} v{modversion}"
    if layout.get("vector"):
        count = len(blob) // 4
        values = struct.unpack(f"<{count}f", blob[: count * 4])
        return {"floats": list(values), "named": False}, None
    if "named_slots" in layout:
        expected = layout["size"]
        if expected is not None and len(blob) != expected:
            return None, (
                f"{operation} v{modversion} layout expects {expected} bytes, got {len(blob)}"
            )
        count = layout["float_count"]
        values = struct.unpack(f"<{count}f", blob[: count * 4])
        named_slots: dict[int, str] = layout["named_slots"]
        decoded: dict = {"named": True}
        for idx, value in enumerate(values):
            name = named_slots.get(idx, f"f{idx}")
            decoded[name] = value
        return decoded, None
    expected = layout["size"]
    if expected is not None and len(blob) != expected:
        return None, (f"{operation} v{modversion} layout expects {expected} bytes, got {len(blob)}")
    decoded = {}
    for name, fmt, offset in layout["fields"]:
        size = struct.calcsize(fmt)
        (value,) = struct.unpack(fmt, blob[offset : offset + size])
        decoded[name] = value
    return decoded, None


_LI_RE = re.compile(r"<rdf:li\b(?P<attrs>.*?)/>", re.S)
_ATTR_RE = re.compile(r"darktable:(?P<key>\w+)=\"(?P<value>.*?)\"", re.S)
_DESC_RE = re.compile(r"darktable:(?P<key>\w+)=\"(?P<value>[^\"]*)\"")


def _entry_from_attrs(attrs: dict[str, str], history_end: int) -> HistoryEntry:
    raw_params = attrs.get("params", "")
    decoded: dict | None = None
    reason: str | None = None
    modversion = int(attrs.get("modversion", "0") or 0)
    operation = attrs.get("operation", "")
    try:
        blob = decode_params(raw_params)
    except (binascii.Error, zlib.error, ValueError) as e:
        reason = f"params not decodable: {e}"
    else:
        decoded, reason = decode_module(operation, modversion, blob)
    num = int(attrs.get("num", "-1") or -1)
    return HistoryEntry(
        num=num,
        operation=operation,
        enabled=attrs.get("enabled", "0") == "1",
        modversion=modversion,
        multi_name=attrs.get("multi_name", ""),
        multi_priority=int(attrs.get("multi_priority", "0") or 0),
        active=num < history_end,
        params_hex=raw_params,
        decoded=decoded,
        reason=reason,
        # Task 7: verbatim, never decoded -- the `.dtstyle` writer copies
        # these two attributes character for character, the same way it
        # copies `params_hex` (pinned against every real entry in
        # tests/fixtures/P6281348.ORF.xmp, e.g. num 11's
        # blendop_version="14").
        blendop_params_hex=attrs.get("blendop_params", ""),
        blendop_version=int(attrs.get("blendop_version", "0") or 0),
        multi_name_hand_edited=attrs.get("multi_name_hand_edited", "0") == "1",
    )


def parse(text: str) -> ParsedXmp:
    """Parse a sidecar. Falls back to a regex scan when the XML is not yet valid.

    darktable rewrites the sidecar in place, so a read that races the write sees
    a truncated document. That is a retryable condition, not a crash: the
    fallback returns whatever complete `rdf:li` elements it can see and sets
    `well_formed=False` so the caller can retry after the mtime settles.
    """
    header: dict[str, str] = {}
    entries: list[HistoryEntry] = []
    well_formed = True
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        well_formed = False
        root = None

    if root is not None:
        desc = root.find(f".//{{{RDF_NS}}}Description")
        if desc is not None:
            for key, value in desc.attrib.items():
                if key.startswith(f"{{{DT_NS}}}"):
                    header[key.split("}", 1)[1]] = value
        history_end = int(header.get("history_end", "0") or 0)
        seq = root.find(f".//{{{DT_NS}}}history/{{{RDF_NS}}}Seq")
        for li in list(seq) if seq is not None else []:
            attrs = {
                k.split("}", 1)[1]: v for k, v in li.attrib.items() if k.startswith(f"{{{DT_NS}}}")
            }
            entries.append(_entry_from_attrs(attrs, history_end))
    else:
        header = {m.group("key"): m.group("value") for m in _DESC_RE.finditer(text)}
        history_end = int(header.get("history_end", "0") or 0)
        for match in _LI_RE.finditer(text):
            attrs = {
                m.group("key"): m.group("value") for m in _ATTR_RE.finditer(match.group("attrs"))
            }
            if "operation" in attrs:
                entries.append(_entry_from_attrs(attrs, history_end))

    entries.sort(key=lambda e: e.num)
    return ParsedXmp(
        history_end=int(header.get("history_end", "0") or 0),
        xmp_version=int(header.get("xmp_version", "0") or 0),
        iop_order_version=int(header.get("iop_order_version", "0") or 0),
        current_hash=header.get("history_current_hash"),
        entries=entries,
        well_formed=well_formed,
    )


def active_entries(parsed: ParsedXmp) -> list[HistoryEntry]:
    """Entries with num < history_end, newest-wins per (operation, multi_priority).

    history_end is darktable's cut: everything at or past it is a redo-able
    tail and is not part of the image's current look, so it must never ride
    into a style.

    Task 7: this used to be a plain `num < history_end` filter with no
    dedup, which was correct for reporting (every step darktable wrote is
    worth showing) but wrong for a style -- a slider touched twice (e.g.
    exposure at num 8 then again at num 11) must contribute ONE `<plugin>`
    element, the newest, not two competing ones. `enabled` stays on each
    entry as its own field (ruling: enabled surfaced), never folded into the
    filter, because "in the active history" and "the module's own on/off
    toggle" are different questions.
    """
    active = [entry for entry in parsed.entries if entry.active]
    newest_num: dict[tuple[str, int], int] = {}
    for entry in active:
        key = (entry.operation, entry.multi_priority)
        if key not in newest_num or entry.num > newest_num[key]:
            newest_num[key] = entry.num
    return [
        entry
        for entry in active
        if entry.num == newest_num[(entry.operation, entry.multi_priority)]
    ]


def _json_safe(value):
    """`nan`/`inf` -> `None` (phase 1's rule, `types._clean`'s sibling): JSON has
    no literal for either, and a caller comparing two `None`s (both "not a
    finite number") is exactly the "both non-finite" equality task 11's
    undo needs for `temperature`'s `coeff_g2 = inf` -- comparing raw floats
    would make `inf != inf` look like a broken restore."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def latest_params(parsed: ParsedXmp, operation: str, instance: int = 0) -> dict | None:
    """The active, decoded param dict for one module, PLUS `params_hex`
    (design 11.2 / ruling R9), newest entry wins.

    Same newest-active-entry search as `latest_value`, but returns the whole
    decoded dict instead of a single field -- for callers (task 9's WB delta,
    task 11's journalled coefficient vector) that need more than one slot out
    of the same module. Every value that is `nan`/`inf` in the decoded dict
    comes back as `None` (`_json_safe`), same as every other result Scouter
    hands back. Returns None when the module is absent, inactive, or its
    params could not be decoded (an unknown module stays opaque hex, per the
    honesty rule); never a guessed or partial dict.
    """
    for entry in sorted(parsed.entries, key=lambda e: e.num, reverse=True):
        if entry.operation != operation or not entry.active:
            continue
        if entry.multi_priority != instance:
            continue
        if entry.decoded is None:
            return None
        return {
            **{k: _json_safe(v) for k, v in entry.decoded.items()},
            "params_hex": entry.params_hex,
        }
    return None


_RATING_RE = re.compile(r'xmp:Rating="(-?\d+)"')


def rating(raw_path: str | Path) -> int | None:
    """The sidecar's xmp:Rating, or None when there is no sidecar / no attribute.

    A regex, not a parse: this is called for 300 sidecars in a cull and it reads
    one attribute out of the rdf:Description header. Verified present in
    Pratik's real sidecar (xmp:Rating="1", 2026-09-14).
    """
    sidecar = sidecar_path(raw_path)
    if not sidecar.exists():
        return None
    text = sidecar.read_text(encoding="utf-8", errors="replace")
    match = _RATING_RE.search(text)
    return int(match.group(1)) if match else None


def latest_value(parsed: ParsedXmp, operation: str, param: str, instance: int = 0) -> float | None:
    """The active value of one module parameter, newest entry wins.

    `instance` maps onto darktable's `multi_priority`. Returns None when the
    module is absent, inactive, or its params could not be decoded -- the caller
    must not substitute a default.
    """
    for entry in sorted(parsed.entries, key=lambda e: e.num, reverse=True):
        if entry.operation != operation or not entry.active:
            continue
        if entry.multi_priority != instance:
            continue
        if entry.decoded is None:
            return None
        value = entry.decoded.get(param)
        return float(value) if isinstance(value, (int, float)) else None
    return None
