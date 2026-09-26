"""Tests for `scouter.core.exif`.

`tests/fixtures/exif-orf-header.bin` is a 64 KiB byte slice of the head of
Pratik's own P6281348.ORF (a real Olympus E-M10 Mark III raw), kept so the
reader is tested against a real camera's header and not a synthetic one.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from scouter.config import Config
from scouter.core import exif
from scouter.errors import ExifUnreadable

FIXTURE = Path(__file__).parent / "fixtures" / "exif-orf-header.bin"


def _build_header(endian_bytes: bytes, endian: str, magic: int, make: str, model: str) -> bytes:
    """A synthetic single-IFD TIFF header: byte order, magic, IFD0 offset, then
    IFD0 with Make and Model as ASCII values living past the 12-byte entry."""
    ifd0_offset = 8
    make_bytes = make.encode("ascii") + b"\x00"
    model_bytes = model.encode("ascii") + b"\x00"

    entries = []

    # two entries: MAKE, MODEL, both ASCII (type 2), values stored after the IFD.
    num_entries = 2
    ifd_size = 2 + num_entries * 12 + 4  # count + entries + next-ifd pointer
    values_offset = ifd0_offset + ifd_size

    make_offset = values_offset
    model_offset = make_offset + len(make_bytes)

    entries.append(struct.pack(endian + "HHI", exif.MAKE, 2, len(make_bytes)))
    entries.append(struct.pack(endian + "I", make_offset))
    entries.append(struct.pack(endian + "HHI", exif.MODEL, 2, len(model_bytes)))
    entries.append(struct.pack(endian + "I", model_offset))

    header = endian_bytes
    header += struct.pack(endian + "H", magic)
    header += struct.pack(endian + "I", ifd0_offset)
    header += struct.pack(endian + "H", num_entries)
    header += entries[0] + entries[1]
    header += entries[2] + entries[3]
    header += struct.pack(endian + "I", 0)  # next IFD offset
    header += make_bytes + model_bytes
    return header


def _build_exif_ifd_count_zero_header() -> bytes:
    """A 40-byte little-endian TIFF whose sole IFD0 entry is EXIF_IFD with
    count=0 -- the value blob `_read_ifd` hands back for it is empty, so
    naively unpacking a 4-byte sub-IFD pointer out of it raises
    `struct.error`. Reproduces the reviewer's blocker at exif.py:154/:247."""
    endian = "<"
    ifd0_offset = 8
    header = b"II"
    header += struct.pack(endian + "H", 42)
    header += struct.pack(endian + "I", ifd0_offset)
    header += struct.pack(endian + "H", 1)  # IFD0: one entry
    header += struct.pack(endian + "HHI", exif.EXIF_IFD, 4, 0)  # type=LONG, count=0
    header += struct.pack(endian + "I", 0)  # inline value: empty, but 4 bytes reserved
    header += struct.pack(endian + "I", 0)  # next-IFD offset
    header += b"\x00" * (40 - len(header))
    assert len(header) == 40
    return header


def test_exif_ifd_pointer_count_zero_does_not_crash():
    """read_tags on the reviewer's crafted header must degrade, never raise
    struct.error/IndexError."""
    tmp = FIXTURE.parent / "_tmp_exififd_count_zero.bin"
    tmp.write_bytes(_build_exif_ifd_count_zero_header())
    try:
        tags = exif.read_tags(tmp)
        assert tags.model is None
        assert tags.source == "none"
    finally:
        tmp.unlink()


def test_datetime_offsets_exif_ifd_pointer_count_zero_does_not_crash():
    """Same crafted header through `datetime_offsets`: no struct.error, and no
    Exif-IFD offset is reported since the pointer could not be read."""
    tmp = FIXTURE.parent / "_tmp_exififd_count_zero_offsets.bin"
    tmp.write_bytes(_build_exif_ifd_count_zero_header())
    try:
        assert exif.datetime_offsets(tmp) == []
    finally:
        tmp.unlink()


def test_real_orf_header():
    tags = exif.read_tags(FIXTURE)
    assert tags.byte_order == "II"
    assert tags.magic == 20306
    assert tags.make == "OLYMPUS CORPORATION"
    assert tags.model == "E-M10 Mark III"
    assert tags.captured_at == "2026-06-28 15:49:28"
    assert tags.exposure_time == 0.02
    assert tags.source == "ifd0+exififd"


def test_magic_42_accepted():
    header = _build_header(b"II", "<", 42, "ACME", "X100")
    tmp = FIXTURE.parent / "_tmp_magic42.bin"
    tmp.write_bytes(header)
    try:
        tags = exif.read_tags(tmp)
        assert tags.magic == 42
        assert tags.make == "ACME"
        assert tags.model == "X100"
    finally:
        tmp.unlink()


def test_big_endian():
    header = _build_header(b"MM", ">", 42, "ACME", "X100")
    tmp = FIXTURE.parent / "_tmp_bigendian.bin"
    tmp.write_bytes(header)
    try:
        tags = exif.read_tags(tmp)
        assert tags.byte_order == "MM"
        assert tags.magic == 42
        assert tags.make == "ACME"
        assert tags.model == "X100"
    finally:
        tmp.unlink()


def test_unknown_magic_refused():
    header = _build_header(b"II", "<", 1234, "ACME", "X100")
    tmp = FIXTURE.parent / "_tmp_badmagic.bin"
    tmp.write_bytes(header)
    try:
        with pytest.raises(ExifUnreadable) as exc:
            exif.read_tags(tmp)
        assert exc.value.detail["magic"] == 1234
    finally:
        tmp.unlink()


def test_truncated_header_degrades():
    tmp = FIXTURE.parent / "_tmp_truncated.bin"
    tmp.write_bytes(FIXTURE.read_bytes()[:64])
    try:
        tags = exif.read_tags(tmp)
        assert tags.source == "none"
        assert tags.model is None
    finally:
        tmp.unlink()


def test_value_past_the_limit_is_none_not_a_guess():
    tags = exif.read_tags(FIXTURE, limit=512)
    assert tags.model is None


def test_camera_slug_mapping_and_fallback():
    cfg = Config(cameras=(("E-M10 Mark III", "em10iii"),))
    mapped_tags = exif.ExifTags(byte_order="II", magic=20306, model="E-M10 Mark III")
    assert exif.camera_slug(mapped_tags, cfg) == "em10iii"

    empty_cfg = Config()
    unmapped_tags = exif.ExifTags(byte_order="II", magic=20306, model="E-M10 Mark III")
    assert exif.camera_slug(unmapped_tags, empty_cfg) == "em10markiii"

    no_model_tags = exif.ExifTags(byte_order="II", magic=20306, model=None)
    with pytest.raises(ExifUnreadable):
        exif.camera_slug(no_model_tags, empty_cfg)


def test_capture_key_orders_missing_time_last():
    with_time = exif.ExifTags(
        byte_order="II", magic=20306, captured_at="2026-06-28 15:49:28", subsec=0
    )
    without_time = exif.ExifTags(byte_order="II", magic=20306, captured_at=None)
    key_with = exif.capture_key(with_time, "a.orf")
    key_without = exif.capture_key(without_time, "b.orf")
    assert sorted([key_without, key_with]) == [key_with, key_without]


def test_datetime_offsets_round_trip():
    data = bytearray(FIXTURE.read_bytes())
    offsets = exif.datetime_offsets(FIXTURE)
    assert len(offsets) >= 1

    new_time = b"2030:01:02 03:04:05\x00"
    for offset, length in offsets:
        assert length == len(new_time)
        data[offset : offset + length] = new_time

    tmp = FIXTURE.parent / "_tmp_patched.bin"
    tmp.write_bytes(bytes(data))
    try:
        tags = exif.read_tags(tmp)
        assert tags.captured_at == "2030-01-02 03:04:05"
    finally:
        tmp.unlink()


def _build_orientation_header(orientation: int) -> bytes:
    """A synthetic single-IFD little-endian TIFF header with one IFD0 entry:
    Orientation (SHORT, count 1), inline in the entry's own value field."""
    ifd0_offset = 8
    endian = "<"
    header = b"II"
    header += struct.pack(endian + "H", 42)
    header += struct.pack(endian + "I", ifd0_offset)
    header += struct.pack(endian + "H", 1)  # IFD0: one entry
    header += struct.pack(endian + "HHI", exif.ORIENTATION, 3, 1)  # SHORT, count 1
    header += struct.pack(endian + "HH", orientation, 0)  # value, then padding
    header += struct.pack(endian + "I", 0)  # next-IFD offset
    return header


def test_real_orf_header_orientation_is_normal():
    tags = exif.read_tags(FIXTURE)
    assert tags.orientation == 1
    assert exif.rotation_degrees(tags.orientation) == 0


def test_orientation_tag_round_trips():
    for value in (1, 2, 3, 4, 5, 6, 7, 8):
        tmp = FIXTURE.parent / f"_tmp_orientation_{value}.bin"
        tmp.write_bytes(_build_orientation_header(value))
        try:
            tags = exif.read_tags(tmp)
            assert tags.orientation == value
        finally:
            tmp.unlink()


def test_missing_orientation_tag_is_none_not_a_guess():
    header = _build_header(b"II", "<", 42, "ACME", "X100")  # no Orientation entry
    tmp = FIXTURE.parent / "_tmp_no_orientation.bin"
    tmp.write_bytes(header)
    try:
        tags = exif.read_tags(tmp)
        assert tags.orientation is None
    finally:
        tmp.unlink()


def test_rotation_degrees_pure_rotations():
    assert exif.rotation_degrees(None) == 0  # no tag: the camera's own default
    assert exif.rotation_degrees(1) == 0
    assert exif.rotation_degrees(3) == 180
    assert exif.rotation_degrees(6) == 90
    assert exif.rotation_degrees(8) == 270


def test_rotation_degrees_mirrored_or_unknown_is_none():
    for value in (2, 4, 5, 7, 0, 9, 255):
        assert exif.rotation_degrees(value) is None
