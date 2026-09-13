"""Pure-Python PPM (P6) reading, histograms and statistics.

Why PPM: darktable-cli supports `--out-ext ppm` and writes binary P6 at 16 bits
per sample, so Scouter needs no image library at all -- which is the whole
reason the dependency list is stdlib + mcp.

Why 8-bit statistics: for a 16-bit raster the HIGH BYTE of each sample is taken.
That is 256-bin precision, the same as darktable's own on-screen histogram, and
it costs one slice instead of a per-sample Python loop. Measured at 1024 px:
Counter over a byte slice is 13 ms per channel; a bytes.count loop is 143 ms and
a per-pixel loop 78 ms. Counter is therefore the implementation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from scouter.types import ChannelStats, ImageStats

CHANNELS = ("r", "g", "b")
#: Luminance weights (Rec. 709), used on the already-8-bit reduction.
LUMA = (0.2126, 0.7152, 0.0722)
#: A sample at or above this high byte counts as clipped (~99.2% of full scale).
CLIP_HIGH = 253
#: ...and at or below this as crushed.
CLIP_LOW = 2


@dataclass
class Raster:
    """An 8-bit-per-sample RGB raster reduced from whatever the file held."""

    width: int
    height: int
    bit_depth: int  # the file's depth: 8 or 16
    data: bytes  # width * height * 3 bytes, one byte per sample

    @property
    def samples(self) -> int:
        return self.width * self.height

    def channel(self, name: str) -> bytes:
        return self.data[CHANNELS.index(name) :: 3]


def _tokens(data: bytes, count: int) -> tuple[list[bytes], int]:
    """Read `count` whitespace-separated header tokens, honouring `#` comments."""
    out: list[bytes] = []
    i = 0
    while len(out) < count:
        while i < len(data) and data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            while i < len(data) and data[i : i + 1] != b"\n":
                i += 1
            continue
        start = i
        while i < len(data) and not data[i : i + 1].isspace():
            i += 1
        if start == i:
            raise ValueError("truncated PPM header")
        out.append(data[start:i])
    return out, i + 1  # skip exactly one whitespace byte after maxval


def parse_p6(data: bytes) -> Raster:
    """Parse a binary PPM. Supports maxval 255 and 65535 (big-endian)."""
    tokens, offset = _tokens(data, 4)
    magic, width_s, height_s, maxval_s = tokens
    if magic != b"P6":
        raise ValueError(f"not a binary PPM: magic {magic!r} (expected P6)")
    width, height, maxval = int(width_s), int(height_s), int(maxval_s)
    if maxval == 255:
        depth, stride = 8, 1
    elif maxval == 65535:
        depth, stride = 16, 2
    else:
        raise ValueError(f"unsupported PPM maxval {maxval} (expected 255 or 65535)")
    expected = width * height * 3 * stride
    payload = data[offset : offset + expected]
    if len(payload) < expected:
        raise ValueError(
            f"PPM payload short: {len(payload)} bytes, expected {expected} "
            f"for {width}x{height} at {depth} bits"
        )
    reduced = payload[0::2] if stride == 2 else payload
    return Raster(width=width, height=height, bit_depth=depth, data=reduced)


def histogram(raster: Raster) -> dict[str, list[int]]:
    """256 bins per channel."""
    out: dict[str, list[int]] = {}
    for name in CHANNELS:
        counts = Counter(raster.channel(name))
        out[name] = [counts.get(v, 0) for v in range(256)]
    return out


def luminance_histogram(hist: dict[str, list[int]]) -> list[int]:
    """Approximate luminance histogram from the three channel histograms.

    This weights the per-channel COUNTS, not per-pixel luminance, so it is a
    distribution estimate rather than a true luminance histogram. It is enough
    for percentiles and mean; a per-pixel luminance pass costs 80 ms and buys
    nothing Scouter uses. Labelled as an estimate in the result.
    """
    out = [0] * 256
    for name, weight in zip(CHANNELS, LUMA):
        for value, count in enumerate(hist[name]):
            if count:
                out[value] += int(count * weight)
    return out


def _percentile(hist: list[int], q: float) -> int:
    total = sum(hist)
    if total == 0:
        return 0
    target = q * total
    running = 0
    for value, count in enumerate(hist):
        running += count
        if running >= target:
            return value
    return 255


def _mean(hist: list[int]) -> float:
    total = sum(hist)
    if total == 0:
        return 0.0
    return sum(value * count for value, count in enumerate(hist)) / total


def sharpness(raster: Raster) -> float:
    """Mean absolute horizontal green-channel difference, 0..1.

    Green stands in for luminance (it carries most of it and costs one slice).
    Every 4th row is sampled. Monotone in focus for the SAME frame; not
    comparable across scenes, and the result says so.
    """
    green = raster.channel("g")
    width, height = raster.width, raster.height
    if width < 2 or height < 1:
        return 0.0
    total, count = 0, 0
    for y in range(0, height, 4):
        row = green[y * width : (y + 1) * width]
        for x in range(1, len(row)):
            total += abs(row[x] - row[x - 1])
            count += 1
    return round(total / count / 255.0, 6) if count else 0.0


def stats(raster: Raster) -> ImageStats:
    """Everything Scouter reports about one render."""
    hist = histogram(raster)
    samples = raster.samples or 1
    channels: dict[str, ChannelStats] = {}
    for name in CHANNELS:
        h = hist[name]
        channels[name] = ChannelStats(
            mean=round(_mean(h), 3),
            median=_percentile(h, 0.5),
            clipped_high_pct=round(100.0 * sum(h[CLIP_HIGH:]) / samples, 4),
            clipped_low_pct=round(100.0 * sum(h[: CLIP_LOW + 1]) / samples, 4),
        )
    luma = luminance_histogram(hist)
    mean_g = channels["g"].mean or 1e-9
    return ImageStats(
        width=raster.width,
        height=raster.height,
        bit_depth=raster.bit_depth,
        channels=channels,
        luminance_mean=round(_mean(luma), 3),
        luminance_p01=_percentile(luma, 0.01),
        luminance_p50=_percentile(luma, 0.50),
        luminance_p99=_percentile(luma, 0.99),
        clipped_high_pct=round(max(c.clipped_high_pct for c in channels.values()), 4),
        clipped_low_pct=round(max(c.clipped_low_pct for c in channels.values()), 4),
        wb_ratio_rg=round(channels["r"].mean / mean_g, 4),
        wb_ratio_bg=round(channels["b"].mean / mean_g, 4),
        sharpness=sharpness(raster),
    )


def downscale(raster: Raster, px: int) -> Raster:
    """Nearest-neighbour stride decimation to a longest edge of `px`."""
    longest = max(raster.width, raster.height)
    if longest <= px:
        return raster
    step = -(-longest // px)  # ceil
    new_w = max(1, -(-raster.width // step))
    new_h = max(1, -(-raster.height // step))
    out = bytearray(new_w * new_h * 3)
    for ny in range(new_h):
        src_row = min(raster.height - 1, ny * step) * raster.width * 3
        base = ny * new_w * 3
        for nx in range(new_w):
            src = src_row + min(raster.width - 1, nx * step) * 3
            out[base + nx * 3 : base + nx * 3 + 3] = raster.data[src : src + 3]
    return Raster(width=new_w, height=new_h, bit_depth=raster.bit_depth, data=bytes(out))
