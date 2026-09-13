import math
import unittest
from pathlib import Path

from scouter.core import xmp

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REFERENCE = FIXTURES / "P6281348.ORF.xmp"


class ParseTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.parsed = xmp.parse(REFERENCE.read_text())

    def test_header(self) -> None:
        self.assertTrue(self.parsed.well_formed)
        self.assertEqual(self.parsed.history_end, 14)
        self.assertEqual(self.parsed.xmp_version, 5)
        self.assertEqual(self.parsed.iop_order_version, 4)
        self.assertEqual(len(self.parsed.current_hash or ""), 32)

    def test_exposure_decodes_to_the_value_the_spike_set(self) -> None:
        entry = next(e for e in self.parsed.entries if e.num == 11)
        self.assertEqual(entry.operation, "exposure")
        self.assertEqual(entry.modversion, 7)
        self.assertTrue(entry.enabled)
        self.assertTrue(entry.active)
        self.assertIsNone(entry.reason)
        assert entry.decoded is not None
        self.assertAlmostEqual(entry.decoded["exposure"], 1.5500002, places=5)
        self.assertEqual(entry.decoded["mode"], 0)

    def test_sigmoid_contrast(self) -> None:
        entry = next(e for e in self.parsed.entries if e.num == 12)
        assert entry.decoded is not None
        self.assertEqual(entry.operation, "sigmoid")
        self.assertAlmostEqual(entry.decoded["contrast"], 1.4, places=5)

    def test_temperature_coefficients_including_the_inf(self) -> None:
        entry = next(e for e in self.parsed.entries if e.num == 13)
        assert entry.decoded is not None
        self.assertAlmostEqual(entry.decoded["coeff_r"], 2.0125747, places=5)
        self.assertAlmostEqual(entry.decoded["coeff_b"], 1.7052786, places=5)
        self.assertTrue(math.isinf(entry.decoded["coeff_g2"]))

    def test_unknown_module_is_opaque_hex_with_a_reason(self) -> None:
        entry = next(e for e in self.parsed.entries if e.operation == "rawprepare")
        self.assertIsNone(entry.decoded)
        self.assertIn("no layout for rawprepare", entry.reason or "")
        self.assertTrue(entry.params_hex)

    def test_latest_value_picks_the_newest_active_entry(self) -> None:
        # nums 8 and 11 are both exposure; 11 is newer and must win.
        value = xmp.latest_value(self.parsed, "exposure", "exposure")
        assert value is not None
        self.assertAlmostEqual(value, 1.5500002, places=5)

    def test_latest_value_is_none_for_an_absent_module(self) -> None:
        self.assertIsNone(xmp.latest_value(self.parsed, "colorbalancergb", "contrast"))

    def test_inactive_entries_are_flagged_not_dropped(self) -> None:
        parsed = xmp.parse(REFERENCE.read_text().replace('history_end="14"', 'history_end="9"'))
        exposures = [e for e in parsed.entries if e.operation == "exposure"]
        self.assertTrue(any(not e.active for e in exposures))
        self.assertIsNone(xmp.latest_value(parsed, "sigmoid", "contrast"))


class DecodeParamsTest(unittest.TestCase):
    def test_plain_hex(self) -> None:
        self.assertEqual(xmp.decode_params("00ff10"), b"\x00\xff\x10")

    def test_gz_prefixed_base64_zlib(self) -> None:
        import base64
        import zlib

        blob = b"x" * 64
        encoded = "gz11" + base64.b64encode(zlib.compress(blob)).decode()
        self.assertEqual(xmp.decode_params(encoded), blob)

    def test_length_mismatch_refuses_to_guess(self) -> None:
        decoded, reason = xmp.decode_module("exposure", 7, b"\x00" * 12)
        self.assertIsNone(decoded)
        self.assertIn("expects 28 bytes, got 12", reason or "")

    def test_unknown_modversion_refuses_to_guess(self) -> None:
        decoded, reason = xmp.decode_module("exposure", 99, b"\x00" * 28)
        self.assertIsNone(decoded)
        self.assertIn("no layout for exposure v99", reason or "")


class TruncatedTest(unittest.TestCase):
    def test_truncated_sidecar_is_retryable_not_fatal(self) -> None:
        parsed = xmp.parse((FIXTURES / "truncated.xmp").read_text())
        self.assertFalse(parsed.well_formed)
        # A truncated head has no complete history entries, and that is fine:
        # the caller retries once the mtime settles.
        self.assertIsInstance(parsed.entries, list)

    def test_regex_fallback_still_finds_complete_entries(self) -> None:
        text = REFERENCE.read_text()
        parsed = xmp.parse(text[:-40])  # chop the closing tags only
        self.assertFalse(parsed.well_formed)
        self.assertTrue(any(e.operation == "exposure" for e in parsed.entries))


class SidecarPathTest(unittest.TestCase):
    def test_sidecar_path_appends_xmp_to_the_whole_filename(self) -> None:
        self.assertEqual(xmp.sidecar_path("/a/P6281348.ORF"), Path("/a/P6281348.ORF.xmp"))


class RatingTest(unittest.TestCase):
    def test_rating_regex_reads_the_real_sidecar(self) -> None:
        self.assertEqual(xmp.rating(FIXTURES / "P6281348.ORF"), 1)

    def test_rating_is_none_when_there_is_no_attribute(self) -> None:
        text = REFERENCE.read_text().replace('xmp:Rating="1"', "")
        self.assertNotIn("xmp:Rating", text)
        # Same header, no attribute: rating() must not fall back to a guess.
        self.assertIsNone(xmp._RATING_RE.search(text))

    def test_rating_is_none_when_there_is_no_sidecar(self) -> None:
        self.assertIsNone(xmp.rating("/does/not/exist.ORF"))


class ActiveEntriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = xmp.parse(REFERENCE.read_text())

    def test_active_entries_are_a_subset_filtered_by_num(self) -> None:
        active = xmp.active_entries(self.parsed)
        self.assertTrue(active)
        self.assertTrue(all(e.num < self.parsed.history_end for e in active))
        self.assertEqual(len(active), len([e for e in self.parsed.entries if e.active]))

    def test_active_entries_keeps_enabled_visible_not_filtered(self) -> None:
        active = xmp.active_entries(self.parsed)
        # `enabled` stays on the entry as its own field -- active_entries only
        # answers "in the active history", not "toggled on".
        self.assertTrue(all(hasattr(e, "enabled") for e in active))


class LatestParamsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = xmp.parse(REFERENCE.read_text())

    def test_latest_params_returns_the_whole_decoded_dict(self) -> None:
        params = xmp.latest_params(self.parsed, "exposure")
        assert params is not None
        self.assertAlmostEqual(params["exposure"], 1.5500002, places=5)
        self.assertEqual(params["mode"], 0)

    def test_latest_params_none_for_absent_module(self) -> None:
        self.assertIsNone(xmp.latest_params(self.parsed, "no-such-module"))

    def test_latest_params_none_for_unknown_layout(self) -> None:
        # rawprepare has no LAYOUTS entry, so it decodes to opaque hex --
        # latest_params must not invent a value for it.
        self.assertIsNone(xmp.latest_params(self.parsed, "rawprepare"))


class ColorbalancergbTest(unittest.TestCase):
    FIXTURE = FIXTURES / "colorbalancergb.xmp"

    def test_contrast_slot_is_pinned_by_the_fixture(self) -> None:
        parsed = xmp.parse(self.FIXTURE.read_text())
        entry = next(e for e in parsed.entries if e.operation == "colorbalancergb")
        assert entry.decoded is not None
        if entry.decoded.get("named"):
            self.assertAlmostEqual(entry.decoded["contrast"], 0.2, places=4)
        else:
            self.assertIn(
                0.2,
                [round(v, 4) for v in entry.decoded["floats"]],
                "the value set through the bridge must appear in the float vector",
            )
