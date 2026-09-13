import unittest

from scouter.core import ppm


def p6_8bit(width: int, height: int, pixels: bytes, comment: bool = False) -> bytes:
    head = b"P6\n"
    if comment:
        head += b"# written by a test\n"
    return head + f"{width} {height}\n255\n".encode() + pixels


def p6_16bit(width: int, height: int, samples: list[int]) -> bytes:
    body = b"".join(v.to_bytes(2, "big") for v in samples)
    return b"P6\n" + f"{width} {height}\n65535\n".encode() + body


class ParseTest(unittest.TestCase):
    def test_8bit_roundtrip(self) -> None:
        raster = ppm.parse_p6(p6_8bit(2, 1, bytes([255, 0, 0, 0, 255, 0])))
        self.assertEqual((raster.width, raster.height, raster.bit_depth), (2, 1, 8))
        self.assertEqual(raster.data, bytes([255, 0, 0, 0, 255, 0]))
        self.assertEqual(raster.channel("r"), bytes([255, 0]))

    def test_16bit_keeps_the_high_byte(self) -> None:
        raster = ppm.parse_p6(p6_16bit(1, 1, [0xFFFF, 0x8000, 0x0100]))
        self.assertEqual(raster.bit_depth, 16)
        self.assertEqual(raster.data, bytes([0xFF, 0x80, 0x01]))

    def test_comments_and_odd_whitespace_in_the_header(self) -> None:
        raster = ppm.parse_p6(p6_8bit(1, 1, bytes([1, 2, 3]), comment=True))
        self.assertEqual(raster.data, bytes([1, 2, 3]))

    def test_rejects_p3_and_odd_maxval(self) -> None:
        with self.assertRaises(ValueError):
            ppm.parse_p6(b"P3\n1 1\n255\n1 2 3\n")
        with self.assertRaises(ValueError):
            ppm.parse_p6(b"P6\n1 1\n1023\n\x00\x00\x00")

    def test_short_payload_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ppm.parse_p6(p6_8bit(4, 4, bytes([1, 2, 3])))
        self.assertIn("payload short", str(ctx.exception))


class StatsTest(unittest.TestCase):
    def test_flat_mid_grey(self) -> None:
        raster = ppm.parse_p6(p6_8bit(4, 4, bytes([128, 128, 128] * 16)))
        s = ppm.stats(raster)
        self.assertEqual(s.channels["r"].mean, 128.0)
        self.assertEqual(s.channels["r"].median, 128)
        self.assertEqual(s.clipped_high_pct, 0.0)
        self.assertEqual(s.clipped_low_pct, 0.0)
        self.assertEqual(s.wb_ratio_rg, 1.0)
        self.assertEqual(s.wb_ratio_bg, 1.0)
        self.assertEqual(s.sharpness, 0.0)

    def test_all_white_is_fully_clipped_high(self) -> None:
        raster = ppm.parse_p6(p6_8bit(2, 2, bytes([255] * 12)))
        s = ppm.stats(raster)
        self.assertEqual(s.clipped_high_pct, 100.0)
        self.assertEqual(s.luminance_p99, 255)

    def test_all_black_is_fully_clipped_low(self) -> None:
        raster = ppm.parse_p6(p6_8bit(2, 2, bytes([0] * 12)))
        s = ppm.stats(raster)
        self.assertEqual(s.clipped_low_pct, 100.0)
        self.assertEqual(s.luminance_p01, 0)

    def test_warm_cast_moves_the_wb_ratios(self) -> None:
        raster = ppm.parse_p6(p6_8bit(2, 2, bytes([200, 100, 50] * 4)))
        s = ppm.stats(raster)
        self.assertEqual(s.wb_ratio_rg, 2.0)
        self.assertEqual(s.wb_ratio_bg, 0.5)

    def test_histogram_bins_sum_to_the_sample_count(self) -> None:
        raster = ppm.parse_p6(p6_8bit(8, 8, bytes(range(192)) * 1))
        hist = ppm.histogram(raster)
        for name in ppm.CHANNELS:
            self.assertEqual(len(hist[name]), 256)
            self.assertEqual(sum(hist[name]), raster.samples)

    def test_sharpness_is_higher_for_an_edge_than_for_a_flat_field(self) -> None:
        flat = ppm.parse_p6(p6_8bit(8, 4, bytes([128, 128, 128] * 32)))
        edge = bytearray()
        for _ in range(4):
            for x in range(8):
                v = 0 if x % 2 == 0 else 255
                edge += bytes([v, v, v])
        striped = ppm.parse_p6(p6_8bit(8, 4, bytes(edge)))
        self.assertGreater(ppm.stats(striped).sharpness, ppm.stats(flat).sharpness)


class DownscaleTest(unittest.TestCase):
    def test_no_op_when_already_small(self) -> None:
        raster = ppm.parse_p6(p6_8bit(4, 4, bytes([9] * 48)))
        self.assertIs(ppm.downscale(raster, 1024), raster)

    def test_halves_an_8px_raster_to_4px(self) -> None:
        raster = ppm.parse_p6(p6_8bit(8, 8, bytes([7] * 192)))
        small = ppm.downscale(raster, 4)
        self.assertEqual((small.width, small.height), (4, 4))
        self.assertEqual(len(small.data), 4 * 4 * 3)
        self.assertEqual(set(small.data), {7})
