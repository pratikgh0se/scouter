"""Route A for warmth (decision rows 458/461, Pratik 2026-09-27 01:25 Bangkok:
"warmth through colour calibration").

On a white balance in darktable 5.4's camera reference (late) mode
(`temperature` preset 4), a Kelvin move through the WB module's sliders
forces preset 2 and drops the late D65 correction, so the frame turns cooler
whichever way it was asked (gauntlet night 1, cov-phrase-warmer-olympus-touch).
Route A moves color calibration's CAT illuminant temperature instead and
leaves the WB module alone. The route is live only when the
`channelmixerrgb.temperature` row is `live` (its action path is host-pinned);
until then the interim guard's refusal stands.

Oracles (Zenkai Rules Z2): what Scouter sent (the FakeBridge's recorded
`darkroom.action`) and what it reports (the result / journal) are checked
separately here; the host oracles -- the sidecar slot darktable writes and
the measured r/b ratio direction -- live in tests/test_host_colorcal.py.
"""

from __future__ import annotations

import binascii
import struct
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal, moves, vocab, wb, xmp
from scouter.errors import ModuleUnavailable
from tests.test_moves import _MovesFixture
from tests.test_wb_late_guard import _PRESET_2_HEX, _PRESET_4_HEX

COLORCAL_PATH = "iop/channelmixerrgb/temperature"
_FIXTURE_COLORCAL = "gz04eJxjYGiwZ8AAxIqRD5igmAWIGYH4MtcRO5vS/XbrskpdQXYxQuUBodMIaA=="
#: tests/fixtures/P6281348.ORF.xmp num 7, as darktable 5.4 wrote it.
FIXTURE_ILLUMINANT_K = 3926.66748046875


def colorcal_hex(*, illuminant: int = 2, adaptation: int = 1,
                 temperature: float = FIXTURE_ILLUMINANT_K) -> str:
    """The fixture's color calibration params with fields swapped, as plain hex
    (`xmp.decode_params` takes hex as well as darktable's gz form)."""
    blob = bytearray(xmp.decode_params(_FIXTURE_COLORCAL))
    struct.pack_into("<i", blob, 120, illuminant)
    struct.pack_into("<i", blob, 132, adaptation)
    struct.pack_into("<f", blob, 144, temperature)
    return binascii.hexlify(bytes(blob)).decode()


class _RouteFixture(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.sidecar = Path(str(self.raw) + ".xmp")

    def edit_sidecar(self, old: str, new: str) -> None:
        text = self.sidecar.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.sidecar.write_text(text.replace(old, new), encoding="utf-8")

    def preset_4(self) -> None:
        self.edit_sidecar(_PRESET_2_HEX, _PRESET_4_HEX)

    def colorcal(self, **fields) -> None:
        self.edit_sidecar(_FIXTURE_COLORCAL, colorcal_hex(**fields))

    def pin(self):
        """What the host probe will add once `iop/channelmixerrgb/temperature`
        answers non-nan: the pin that turns the row `live`."""
        p = mock.patch.dict(dtbridge.ACTION_PATHS,
                            {darkroom.COLORCAL_WB: COLORCAL_PATH})
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(vocab.status(vocab.spec(*darkroom.COLORCAL_WB)), "live")

    def assert_wb_untouched(self) -> None:
        self.assertNotIn("iop/temperature/temperature", [s["path"] for s in self.sets()])
        self.assertEqual(darkroom.wb_preset(str(self.raw)), 4)


class LayoutAndRowTest(_RouteFixture):
    def test_the_fixture_decodes_the_illuminant_temperature(self) -> None:
        parsed = xmp.parse(self.sidecar.read_text(encoding="utf-8"))
        params = xmp.latest_params(parsed, "channelmixerrgb")
        self.assertEqual(params["illuminant"], 2)
        self.assertEqual(params["adaptation"], 1)
        self.assertAlmostEqual(params["temperature"], FIXTURE_ILLUMINANT_K, places=3)
        # McCamy's CCT from the stored (x, y): the slot is the illuminant's.
        n = (params["x"] - 0.3320) / (0.1858 - params["y"])
        cct = 449 * n ** 3 + 3525 * n ** 2 + 6823.3 * n + 5520.33
        self.assertAlmostEqual(cct, params["temperature"], delta=15)

    def test_the_row_is_a_candidate_until_the_host_probe(self) -> None:
        s = vocab.spec("channelmixerrgb", "temperature")
        self.assertEqual((s.unit, s.lo, s.hi, s.step), ("K", 2000.0, 12000.0, 300.0))
        self.assertEqual(s.candidate_paths, (COLORCAL_PATH,))
        self.assertNotIn(darkroom.COLORCAL_WB, dtbridge.ACTION_PATHS)
        self.assertEqual(vocab.status(s), "candidate")
        self.assertTrue(vocab.xmp_named("channelmixerrgb", "temperature"))


class RouteSelectionTest(_RouteFixture):
    def test_preset_2_keeps_the_white_balance_route(self) -> None:
        self.pin()
        self.assertEqual(darkroom.wb_route(str(self.raw))["route"], "white_balance")

    def test_preset_4_candidate_row_is_unavailable(self) -> None:
        self.preset_4()
        r = darkroom.wb_route(str(self.raw))
        self.assertEqual(r["route"], "unavailable")
        self.assertEqual(r["colorcal_status"], "candidate")
        self.assertIn("host probe", r["reason"])

    def test_preset_4_pinned_row_routes_to_color_calibration(self) -> None:
        self.preset_4()
        self.pin()
        r = darkroom.wb_route(str(self.raw))
        self.assertEqual(r["route"], "color_calibration")
        self.assertEqual(r["illuminant"], "daylight")
        self.assertAlmostEqual(r["illuminant_kelvin"], FIXTURE_ILLUMINANT_K, places=3)

    def test_planckian_illuminant_routes_too(self) -> None:
        self.preset_4()
        self.colorcal(illuminant=6)
        self.pin()
        self.assertEqual(darkroom.wb_route(str(self.raw))["illuminant"], "planckian")

    def test_an_illuminant_without_a_temperature_slider_is_unavailable(self) -> None:
        self.preset_4()
        self.colorcal(illuminant=10)  # as shot in camera
        self.pin()
        r = darkroom.wb_route(str(self.raw))
        self.assertEqual(r["route"], "unavailable")
        self.assertIn("as shot in camera", r["reason"])

    def test_a_bypassed_cat_is_unavailable(self) -> None:
        self.preset_4()
        self.colorcal(adaptation=darkroom.ADAPTATION_BYPASS)
        self.pin()
        self.assertEqual(darkroom.wb_route(str(self.raw))["route"], "unavailable")

    def test_color_calibration_switched_off_is_unavailable(self) -> None:
        self.preset_4()
        self.edit_sidecar('darktable:operation="channelmixerrgb"\n      darktable:enabled="1"',
                          'darktable:operation="channelmixerrgb"\n      darktable:enabled="0"')
        self.pin()
        r = darkroom.wb_route(str(self.raw))
        self.assertEqual(r["route"], "unavailable")
        self.assertIn("not enabled", r["reason"])


class SayRoutedTest(_RouteFixture):
    def setUp(self) -> None:
        super().setUp()
        self.preset_4()
        self.pin()

    def test_warmer_raises_the_illuminant_temperature(self) -> None:
        r = moves.say.__wrapped__("warmer", commit=False, job="job-test")
        self.assertEqual(r.outcome, "ok")
        sent = self.sets()
        self.assertEqual([s["path"] for s in sent], [COLORCAL_PATH])
        self.assertAlmostEqual(sent[0]["value"], FIXTURE_ILLUMINANT_K + 300.0, places=2)
        self.assertEqual(r.moves[0]["module"], "channelmixerrgb")
        self.assertGreater(r.moves[0]["target"], r.moves[0]["previous"])
        self.assertTrue(any("color calibration" in w for w in r.warnings), r.warnings)
        self.assert_wb_untouched()

    def test_cooler_lowers_it(self) -> None:
        moves.say.__wrapped__("a touch cooler", commit=False, job="job-test")
        self.assertAlmostEqual(self.sets()[0]["value"], FIXTURE_ILLUMINANT_K - 150.0, places=2)
        self.assert_wb_untouched()

    def test_explicit_kelvin_amount_is_kept(self) -> None:
        moves.say.__wrapped__("warmer by 500k", commit=False, job="job-test")
        self.assertAlmostEqual(self.sets()[0]["value"], FIXTURE_ILLUMINANT_K + 500.0, places=2)

    def test_the_move_is_journalled_on_color_calibration_and_undoable(self) -> None:
        moves.say.__wrapped__("warmer", commit=False, job="job-test")
        (entry,) = journal.read_all("job-test")
        self.assertEqual((entry.module, entry.param), darkroom.COLORCAL_WB)
        self.assertAlmostEqual(entry.before["value"], FIXTURE_ILLUMINANT_K, places=3)

    def test_tint_is_still_refused(self) -> None:
        # ("more magenta" refuses earlier, value_unknown: tint has no value source.)
        with self.assertRaises(ModuleUnavailable) as cm:
            darkroom.set_param.__wrapped__("temperature", "tint", 1.1, job="job-test")
        self.assertIn("tint", cm.exception.fix)
        self.assertEqual(self.sets(), [])


class DirectVerbsRoutedTest(_RouteFixture):
    def setUp(self) -> None:
        super().setUp()
        self.preset_4()
        self.pin()

    def test_kelvin_set_goes_to_the_illuminant(self) -> None:
        # "Match daylight" (editor benchmark W4: an absolute 5500 K target).
        r = darkroom.set_param.__wrapped__("temperature", "temperature", 5500.0,
                                           commit=False, job="job-test")
        self.assertEqual((r.module, r.param), darkroom.COLORCAL_WB)
        self.assertEqual([(s["path"], s["value"]) for s in self.sets()],
                         [(COLORCAL_PATH, 5500.0)])
        self.assertTrue(any("color calibration" in w for w in r.warnings))
        self.assert_wb_untouched()

    def test_adjust_goes_to_the_illuminant(self) -> None:
        r = moves.adjust.__wrapped__("temperature", "temperature", -300.0, commit=False,
                                     job="job-test")
        self.assertEqual(r.module, "channelmixerrgb")
        self.assertAlmostEqual(self.sets()[0]["value"], FIXTURE_ILLUMINANT_K - 300.0, places=2)
        self.assert_wb_untouched()

    def test_nudge_is_still_refused_and_points_at_the_route(self) -> None:
        with self.assertRaises(ModuleUnavailable) as cm:
            darkroom.nudge.__wrapped__("temperature", "temperature", 2, job="job-test")
        self.assertIn("say 'warmer'", cm.exception.fix)
        self.assertEqual(cm.exception.detail["colorcal_route"]["route"], "color_calibration")

    def test_wb_calibrate_exemption_still_writes_the_wb_module(self) -> None:
        token = darkroom.WB_LATE_EXEMPT.set(True)
        try:
            darkroom.set_param.__wrapped__("temperature", "temperature", 5000.0, commit=False,
                                           job="job-test")
        finally:
            darkroom.WB_LATE_EXEMPT.reset(token)
        self.assertEqual([s["path"] for s in self.sets()], ["iop/temperature/temperature"])


class NeitherRouteTest(_RouteFixture):
    """Preset 4 and no usable color calibration route: the refusal stands,
    names the reason, and nothing is sent."""

    def test_candidate_row_refuses_with_the_host_probe_reason(self) -> None:
        self.preset_4()
        with self.assertRaises(ModuleUnavailable) as cm:
            moves.say.__wrapped__("warmer", job="job-test")
        self.assertIn("host probe", cm.exception.fix)
        self.assertEqual(cm.exception.detail["colorcal_route"]["route"], "unavailable")
        self.assertEqual(self.sets(), [])

    def test_wrong_illuminant_refuses_naming_it(self) -> None:
        self.preset_4()
        self.colorcal(illuminant=7)
        self.pin()
        with self.assertRaises(ModuleUnavailable) as cm:
            darkroom.set_param.__wrapped__("temperature", "temperature", 5500.0,
                                           job="job-test")
        self.assertIn("'custom'", cm.exception.fix)
        self.assertEqual(self.sets(), [])
        self.assertEqual(journal.read_all("job-test"), [])


class KelvinReadbackTest(_RouteFixture):
    def setUp(self) -> None:
        super().setUp()
        for target, value in (("scouter.core.exif.read_tags", {}),
                              ("scouter.core.exif.camera_slug", "em10iii")):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_preset_2_reports_the_white_balance_route(self) -> None:
        r = wb.kelvin_verb.__wrapped__(str(self.raw), job="job-test")
        self.assertEqual(r.route["route"], "white_balance")

    def test_preset_4_live_reports_color_calibration_and_its_kelvin(self) -> None:
        self.preset_4()
        self.pin()
        r = wb.kelvin_verb.__wrapped__(str(self.raw), job="job-test")
        self.assertEqual(r.route["route"], "color_calibration")
        self.assertAlmostEqual(r.route["illuminant_kelvin"], FIXTURE_ILLUMINANT_K, places=3)
        self.assertTrue(any("as-shot" in w for w in r.warnings), r.warnings)

    def test_preset_4_candidate_reports_unavailable(self) -> None:
        self.preset_4()
        r = wb.kelvin_verb.__wrapped__(str(self.raw), job="job-test")
        self.assertEqual(r.route["route"], "unavailable")
        self.assertTrue(any("cannot move warmth" in w for w in r.warnings), r.warnings)
