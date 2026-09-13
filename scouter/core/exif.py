"""EXIF from the raw itself, with `struct` and nothing else.

Why not darktable-cli: it has no metadata dump mode, so the only way to read a
camera model through it is a ~2 s render per frame -- 10 minutes for a 300-frame
card to learn something that takes 2 ms here. Why not exiftool: it is not stdlib
and it is not installed on this Mac (checked 2026-09-14).

ORF, ARW and DNG are all TIFF containers, so one IFD0 walk covers every format
phase 2 accepts. PINNED FACT: ORF's TIFF magic is 0x4F52 (20306), NOT 42 -- a
strict `magic == 42` check rejects every Olympus raw. Verified on
P6281348.ORF: byte order II, magic 20306, IFD0 at offset 8, 23 entries,
Make "OLYMPUS CORPORATION", Model "E-M10 Mark III".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from scouter import paths
from scouter.errors import ExifUnreadable

MAKE, MODEL, DATETIME = 0x010F, 0x0110, 0x0132
EXIF_IFD, DATETIME_ORIGINAL, SUBSEC_ORIGINAL = 0x8769, 0x9003, 0x9291
EXPOSURE_TIME, F_NUMBER, ISO = 0x829A, 0x829D, 0x8827

#: 42 is the TIFF standard (ARW, DNG); 20306 (0x4F52) is Olympus ORF; 21330
#: (0x5352) is the other ORF spelling in the wild; 85 (0x55) is Panasonic RW2.
#: Accepting the extra two costs one set member and stops the magic check from
#: silently becoming a format whitelist.
ACCEPTED_MAGIC = frozenset({42, 20306, 21330, 85})

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


@dataclass(frozen=True)
class ExifTags:
    """What `read_tags` could pull from a TIFF-family header. Missing fields are
    `None`, never guessed -- a caller that needs a value handles its absence."""

    byte_order: str
    magic: int
    make: str | None = None
    model: str | None = None
    captured_at: str | None = None
    subsec: int = 0
    exposure_time: float | None = None
    f_number: float | None = None
    iso: int | None = None
    source: str = "none"


def _read_ifd(data: bytes, endian: str, offset: int) -> dict[int, tuple[int, int, bytes, int]]:
    """tag -> (type, count, value_bytes, value_offset). value_offset is where the
    bytes live in `data` (the entry itself for <=4 bytes), so a caller can patch
    a fixed-length ASCII value in place without re-encoding anything."""
    if offset <= 0 or offset + 2 > len(data):
        return {}
    (count,) = struct.unpack_from(endian + "H", data, offset)
    out: dict[int, tuple[int, int, bytes, int]] = {}
    for i in range(count):
        entry = offset + 2 + i * 12
        if entry + 12 > len(data):
            break
        tag, typ, n = struct.unpack_from(endian + "HHI", data, entry)
        size = _TYPE_SIZE.get(typ, 1) * n
        if size <= 4:
            value_at = entry + 8
        else:
            (value_at,) = struct.unpack_from(endian + "I", data, entry + 8)
        if value_at < 0 or value_at + size > len(data):
            continue  # value lives past our header slice: not a guess
        out[tag] = (typ, n, data[value_at : value_at + size], value_at)
    return out


def _ascii(blob: bytes | None) -> str | None:
    if not blob:
        return None
    text = blob.rstrip(b"\x00 ").decode("ascii", "replace")
    return text or None


def _rational(endian: str, blob: bytes) -> float | None:
    if len(blob) < 8:
        return None
    num, den = struct.unpack_from(endian + "II", blob, 0)
    if den == 0:
        return None
    return num / den


def _normalise_datetime(raw: str | None) -> str | None:
    """`"2026:06:28 15:49:28"` -> `"2026-06-28 15:49:28"`. Malformed input is
    None, never a guess at what the photographer meant."""
    if not raw or len(raw) != 19 or raw[4] != ":" or raw[7] != ":" or raw[10] != " ":
        return None
    date_part, time_part = raw[:10], raw[11:]
    normalised = f"{date_part[0:4]}-{date_part[5:7]}-{date_part[8:10]} {time_part}"
    try:
        mo, d = int(date_part[5:7]), int(date_part[8:10])
        h, mi, s = int(time_part[0:2]), int(time_part[3:5]), int(time_part[6:8])
    except ValueError:
        return None
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 61):
        return None
    return normalised


def _pointer(endian: str, blob: bytes) -> int | None:
    """Read a 4-byte sub-IFD offset out of a tag's value blob. `None` (not a
    crash) when the blob is too short to hold one -- a zero-count EXIF_IFD
    entry stores an empty value blob, and `struct.unpack_from` on that raises
    `struct.error` rather than degrading gracefully."""
    if len(blob) < 4:
        return None
    (offset,) = struct.unpack_from(endian + "I", blob, 0)
    return offset


def _subsec(blob: bytes | None) -> int:
    text = _ascii(blob)
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def read_tags(path: str | Path, *, limit: int = 1 << 16) -> ExifTags:
    """Read up to `limit` bytes of `path` and walk its TIFF/EXIF header.

    Never raises on a short or degraded read -- only on a header that isn't a
    TIFF container at all, or whose magic isn't one we recognise.
    """
    p = Path(path)
    filesize = p.stat().st_size
    with p.open("rb") as f:
        data = f.read(min(limit, filesize))

    if len(data) < 4 or data[0:2] not in (b"II", b"MM"):
        raise ExifUnreadable(
            f"{p} does not start with a TIFF byte-order marker",
            detail={"header": data[:4].hex()},
        )
    byte_order = data[0:2].decode("ascii")
    endian = "<" if byte_order == "II" else ">"
    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic not in ACCEPTED_MAGIC:
        raise ExifUnreadable(
            f"{p} has an unrecognised TIFF magic {magic}",
            detail={"magic": magic, "byte_order": byte_order},
        )
    ifd0: dict[int, tuple[int, int, bytes, int]] = {}
    if len(data) >= 8:
        (ifd0_offset,) = struct.unpack_from(endian + "I", data, 4)
        ifd0 = _read_ifd(data, endian, ifd0_offset)
    make = _ascii(ifd0[MAKE][2]) if MAKE in ifd0 else None
    model = _ascii(ifd0[MODEL][2]) if MODEL in ifd0 else None
    ifd0_datetime = _ascii(ifd0[DATETIME][2]) if DATETIME in ifd0 else None

    exif_ifd: dict[int, tuple[int, int, bytes, int]] = {}
    if EXIF_IFD in ifd0:
        exif_offset = _pointer(endian, ifd0[EXIF_IFD][2])
        if exif_offset is not None:
            exif_ifd = _read_ifd(data, endian, exif_offset)

    exif_datetime = (
        _ascii(exif_ifd[DATETIME_ORIGINAL][2]) if DATETIME_ORIGINAL in exif_ifd else None
    )
    captured_at = _normalise_datetime(exif_datetime) or _normalise_datetime(ifd0_datetime)
    subsec = _subsec(exif_ifd[SUBSEC_ORIGINAL][2]) if SUBSEC_ORIGINAL in exif_ifd else 0
    exposure_time = (
        _rational(endian, exif_ifd[EXPOSURE_TIME][2]) if EXPOSURE_TIME in exif_ifd else None
    )
    f_number = _rational(endian, exif_ifd[F_NUMBER][2]) if F_NUMBER in exif_ifd else None
    iso = None
    if ISO in exif_ifd:
        typ, n, blob, _ = exif_ifd[ISO]
        if len(blob) >= 2:
            (iso,) = struct.unpack_from(endian + "H", blob, 0)

    has_ifd0_data = make is not None or model is not None or ifd0_datetime is not None
    has_exif_data = (
        exif_datetime is not None
        or exposure_time is not None
        or f_number is not None
        or iso is not None
    )
    if has_ifd0_data and has_exif_data:
        source = "ifd0+exififd"
    elif has_ifd0_data:
        source = "ifd0"
    else:
        source = "none"

    return ExifTags(
        byte_order=byte_order,
        magic=magic,
        make=make,
        model=model,
        captured_at=captured_at,
        subsec=subsec,
        exposure_time=exposure_time,
        f_number=f_number,
        iso=iso,
        source=source,
    )


def camera_slug(tags: ExifTags, cfg) -> str:
    """Stripping non-alphanumerics for the fallback slug means two distinct
    models that differ only in punctuation ("E-M10 Mark III" vs "E M10 Mark
    III") collide on the same slug; add an explicit `cameras` entry in the
    config, or pass `--camera`, to pin the one you mean."""
    mapped = cfg.camera_map().get(tags.model or "")
    if mapped:
        return mapped
    if tags.model:
        slug = "".join(c for c in tags.model.lower() if c.isalnum())
        return paths.validate_slug(slug[:48])
    raise ExifUnreadable(
        "no camera model in this file's EXIF, and no --camera was given",
        detail={"magic": tags.magic, "byte_order": tags.byte_order, "source": tags.source},
    )


def capture_key(tags: ExifTags, path: str | Path) -> tuple[str, int, str]:
    """Rename and burst-grouping sort key. No capture time sorts LAST, by filename,
    and the import warns -- it never invents a time."""
    return (tags.captured_at or "9999-99-99 99:99:99", tags.subsec, Path(path).name)


def datetime_offsets(path: str | Path, *, limit: int = 1 << 20) -> list[tuple[int, int]]:
    """Byte ranges of every DateTime/DateTimeOriginal ASCII value in the header.

    The strings are fixed-length ("2026:06:28 15:49:28\\0", 20 bytes), so the
    synthetic-job builder can overwrite them with an equal-length string in
    place: no re-encoding, no EXIF library, and the reader that finds the
    offsets is the reader under test.
    """
    p = Path(path)
    filesize = p.stat().st_size
    with p.open("rb") as f:
        data = f.read(min(limit, filesize))

    if len(data) < 4 or data[0:2] not in (b"II", b"MM"):
        return []
    endian = "<" if data[0:2] == b"II" else ">"
    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic not in ACCEPTED_MAGIC:
        return []
    if len(data) < 8:
        return []
    (ifd0_offset,) = struct.unpack_from(endian + "I", data, 4)

    ifd0 = _read_ifd(data, endian, ifd0_offset)
    offsets: list[tuple[int, int]] = []
    if DATETIME in ifd0:
        _, _, blob, value_at = ifd0[DATETIME]
        offsets.append((value_at, len(blob)))

    if EXIF_IFD in ifd0:
        exif_offset = _pointer(endian, ifd0[EXIF_IFD][2])
        if exif_offset is not None:
            exif_ifd = _read_ifd(data, endian, exif_offset)
            if DATETIME_ORIGINAL in exif_ifd:
                _, _, blob, value_at = exif_ifd[DATETIME_ORIGINAL]
                offsets.append((value_at, len(blob)))

    return offsets
