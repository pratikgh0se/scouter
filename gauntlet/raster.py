"""Minimal image containers and codecs for the evaluator. Pure stdlib.

`Raster8` holds interleaved 8-bit sRGB-encoded RGB (what a PPM export gives).
`RasterF` holds interleaved linear float RGB (what the synthetic chart is).
"""

from __future__ import annotations

import struct
import sys
from array import array
from dataclasses import dataclass


@dataclass(frozen=True)
class Raster8:
    width: int
    height: int
    data: bytes  # len == width * height * 3

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]


@dataclass(frozen=True)
class RasterF:
    width: int
    height: int
    data: array  # array('f'), len == width * height * 3, row 0 = top

    def pixel(self, x: int, y: int) -> tuple[float, float, float]:
        i = (y * self.width + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]


def _header_tokens(data: bytes, count: int) -> tuple[list[bytes], int]:
    out: list[bytes] = []
    i = 0
    while len(out) < count:
        while i < len(data) and data[i : i + 1].isspace():
            i += 1
        if i < len(data) and data[i : i + 1] == b"#":
            while i < len(data) and data[i : i + 1] not in (b"\n", b"\r"):
                i += 1
            continue
        start = i
        while i < len(data) and not data[i : i + 1].isspace():
            i += 1
        if start == i:
            raise ValueError("truncated header")
        out.append(data[start:i])
    return out, i + 1


def parse_ppm(data: bytes) -> Raster8:
    """Binary PPM (P6), maxval 255 or 65535 (reduced to 8 bits, high byte)."""
    (magic, w, h, maxval), off = _header_tokens(data, 4)
    if magic != b"P6":
        raise ValueError(f"not a binary PPM: {magic!r}")
    width, height, mv = int(w), int(h), int(maxval)
    stride = 1 if mv == 255 else 2 if mv == 65535 else 0
    if not stride:
        raise ValueError(f"unsupported maxval {mv}")
    need = width * height * 3 * stride
    payload = data[off : off + need]
    if len(payload) < need:
        raise ValueError(f"PPM payload short: {len(payload)} < {need}")
    return Raster8(width, height, payload[0::2] if stride == 2 else payload)


def encode_ppm(r: Raster8) -> bytes:
    return f"P6\n{r.width} {r.height}\n255\n".encode() + r.data


def encode_pfm(r: RasterF) -> bytes:
    """PFM colour ('PF'), little-endian (scale -1.0), rows stored bottom-to-top."""
    rows = []
    row_len = r.width * 3
    for y in range(r.height - 1, -1, -1):
        row = r.data[y * row_len : (y + 1) * row_len]
        if sys.byteorder != "little":
            row = array("f", row)
            row.byteswap()
        rows.append(row.tobytes())
    return f"PF\n{r.width} {r.height}\n-1.0\n".encode() + b"".join(rows)


def parse_pfm(data: bytes) -> RasterF:
    (magic, w, h, scale), off = _header_tokens(data, 4)
    if magic != b"PF":
        raise ValueError(f"not a colour PFM: {magic!r}")
    width, height = int(w), int(h)
    little = float(scale) < 0
    row_len = width * 3
    need = width * height * 3 * 4
    payload = data[off : off + need]
    if len(payload) < need:
        raise ValueError("PFM payload short")
    out = array("f")
    for y in range(height - 1, -1, -1):
        row = array("f")
        row.frombytes(payload[y * row_len * 4 : (y + 1) * row_len * 4])
        if little != (sys.byteorder == "little"):
            row.byteswap()
        out.extend(row)
    return RasterF(width, height, out)


def encode_tiff16(r: RasterF, *, clip: float = 1.0) -> bytes:
    """Uncompressed baseline RGB TIFF, 16 bits/sample, LINEAR values scaled by
    65535/clip. The chart's fallback when darktable rejects PFM (spec 4.3)."""
    w, h = r.width, r.height
    pix = array("H", (min(65535, max(0, int(round(v / clip * 65535)))) for v in r.data))
    if sys.byteorder != "little":
        pix.byteswap()
    body = pix.tobytes()
    entries = [
        (256, 3, 1, w), (257, 3, 1, h), (258, 3, 3, None), (259, 3, 1, 1),
        (262, 3, 1, 2), (273, 4, 1, None), (277, 3, 1, 3), (278, 3, 1, h),
        (279, 4, 1, len(body)), (284, 3, 1, 1),
    ]
    ifd_off = 8
    ifd_size = 2 + len(entries) * 12 + 4
    bps_off = ifd_off + ifd_size
    data_off = bps_off + 6
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd_off))
    out += struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        if tag == 258:
            out += struct.pack("<HHII", tag, typ, count, bps_off)
        elif tag == 273:
            out += struct.pack("<HHII", tag, typ, count, data_off)
        elif typ == 3:
            out += struct.pack("<HHIHH", tag, typ, count, value, 0)
        else:
            out += struct.pack("<HHII", tag, typ, count, value)
    out += struct.pack("<I", 0)
    out += struct.pack("<HHH", 16, 16, 16)
    out += body
    return bytes(out)
