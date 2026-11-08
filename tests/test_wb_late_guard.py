"""Interim guard: white balance in darktable 5.4's "camera reference (late)" mode.

darktable 5.4 (src/iop/temperature.c) stores `preset = DT_IOP_TEMP_D65_LATE`
(4) in the temperature params by default on the scene-referred workflow: the
as-shot coefficients stay in the module and color calibration applies the D65
correction late. Any temperature/tint/coefficient slider change runs
`_temp_tint_callback`/`gui_changed`, which forces `preset = DT_IOP_TEMP_USER`
(2) and drops the late correction, so the frame renders cooler whichever way
Kelvin moved (gauntlet night 1, cov-phrase-warmer-olympus-touch: preset 4->2,
centre r/b 0.746->0.516). Scouter cannot keep preset 4 through GUI actions,
so every white-balance write on such an image is refused BEFORE any write.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from scouter import errors
from scouter.core import darkroom, dtbridge, journal, moves, phrases, vocab
from scouter.errors import ModuleUnavailable
from scouter.types import CommitResult
from tests import test_wb as _test_wb
from tests.test_moves import _MovesFixture

#: The fixture's newest temperature entry (preset 2, after the phase 0 spike's
#: Kelvin write) and the same coefficients with darktable 5.4's default
#: preset 4 (DT_IOP_TEMP_D65_LATE) -- the state night 1's raws were in.
_PRESET_2_HEX = "06ce00400000803f9246da3f0000807f02000000"
_PRESET_4_HEX = "06ce00400000803f9246da3f0000807f04000000"


def unpin_route_a(case) -> None:
    """The pre-promotion state: route A's pair absent from ACTION_PATHS (pinned
    since the 2026-11-08 host probe), so the candidate/refusal paths of
    `wb_route` stay covered. Restored on cleanup."""
    p = mock.patch.dict(dtbridge.ACTION_PATHS)
    p.start()
    case.addCleanup(p.stop)
    del dtbridge.ACTION_PATHS[darkroom.COLORCAL_WB]
    case.assertEqual(vocab.status(vocab.spec(*darkroom.COLORCAL_WB)), "candidate")


class _WbLateFixture(_MovesFixture):
    def setUp(self) -> None:
        super().setUp()
        unpin_route_a(self)

    def set_preset(self, preset_hex: str) -> None:
        sidecar = Path(str(self.raw) + ".xmp")
        text = sidecar.read_text(encoding="utf-8")
        self.assertIn(_PRESET_2_HEX, text)
        sidecar.write_text(text.replace(_PRESET_2_HEX, preset_hex), encoding="utf-8")

    def assert_nothing_written(self) -> None:
        self.assertEqual(self.sets(), [], "no darkroom.action may be sent")
        self.assertEqual(self.commits, [], "no commit bounce may be sent")
        self.assertEqual(journal.read_all("job-test"), [], "nothing may be journalled")
        self.assertEqual([m for m, _ in self.bridge.calls], ["darkroom.current"],
                         "only the read-only darkroom.current lookup is allowed")

    def assert_honest(self, err: ModuleUnavailable) -> None:
        self.assertEqual(err.code, "module_unavailable")
        self.assertIn("camera reference (late)", err.fix)
        self.assertIn("color calibration", err.fix)
        self.assertIn("does not drive", err.fix)
        self.assertEqual(err.detail["temperature_preset"], 4)


class SayUnderD65LateTest(_WbLateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.set_preset(_PRESET_4_HEX)

    def test_warmer_is_a_typed_refusal_with_no_write(self) -> None:
        with self.assertRaises(ModuleUnavailable) as cm:
            moves.say.__wrapped__("warmer", job="job-test")
        self.assert_honest(cm.exception)
        self.assertIn("warmer", cm.exception.message)
        self.assertIn("warm the highlights", cm.exception.fix)
        self.assert_nothing_written()

    def test_every_warmth_synonym_is_refused(self) -> None:
        for phrase in ("a touch warmer", "warm up", "more warmth", "cooler", "colder",
                       "cool down", "less warm", "a lot cooler", "warmer by 300k"):
            with self.subTest(phrase=phrase):
                self.bridge.calls.clear()
                with self.assertRaises(ModuleUnavailable):
                    moves.say.__wrapped__(phrase, job="job-test")
                self.assert_nothing_written()

    def test_a_mixed_phrase_with_a_wb_part_does_not_half_apply(self) -> None:
        # No shipped phrase mixes white balance with another module yet, so
        # the resolution is built here: exposure FIRST, so a guard that only
        # fired inside the per-move loop would land the exposure write.
        res = phrases.Resolution(
            phrase="brighter and warmer", normalised="brighter and warmer", key="mixed",
            intensity=1.0, explicit=False,
            moves=(phrases.Move("exposure", "exposure", "adjust", notches=1),
                   phrases.Move("temperature", "temperature", "adjust", notches=1)))
        with mock.patch("scouter.core.moves.phrases.resolve", return_value=res):
            with self.assertRaises(ModuleUnavailable) as cm:
                moves.say.__wrapped__("brighter and warmer", job="job-test")
        self.assertEqual([(r["module"], r["param"]) for r in cm.exception.detail["refused"]],
                         [("temperature", "temperature")])
        self.assert_nothing_written()

    def test_non_wb_phrases_still_run(self) -> None:
        r = moves.say.__wrapped__("brighter", commit=False, job="job-test")
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(len(self.sets()), 1)


class SayUnderD65LateRevealedByTheBounceTest(_WbLateFixture):
    """No sidecar before the pre-read bounce; the bounce is what writes the
    preset-4 sidecar. The guard must re-check after the bounce, before any
    move of the phrase lands."""

    def test_mixed_phrase_refused_after_the_bounce_reveals_preset_4(self) -> None:
        sidecar = Path(str(self.raw) + ".xmp")
        text = sidecar.read_text(encoding="utf-8").replace(_PRESET_2_HEX, _PRESET_4_HEX)
        sidecar.unlink()

        def bounce(job, image, *, strategy="auto", attempts=None, expect_change=True, expect_module=None):
            self.commits.append((attempts, expect_change))
            sidecar.write_text(text, encoding="utf-8")
            return CommitResult(committed=True, changed=False, warnings=[])

        res = phrases.Resolution(
            phrase="brighter and warmer", normalised="brighter and warmer", key="mixed",
            intensity=1.0, explicit=False,
            moves=(phrases.Move("exposure", "exposure", "adjust", notches=1),
                   phrases.Move("temperature", "temperature", "adjust", notches=1)))
        with mock.patch("scouter.core.darkroom._commit", side_effect=bounce), \
                mock.patch("scouter.core.moves.phrases.resolve", return_value=res):
            with self.assertRaises(ModuleUnavailable):
                moves.say.__wrapped__("brighter and warmer", job="job-test")
        self.assertEqual(len(self.commits), 1, "the one expect-no-change bounce only")
        self.assertEqual(self.sets(), [])
        self.assertEqual(journal.read_all("job-test"), [])


class SayUnderUserPresetTest(_WbLateFixture):
    """Preset 2 (user modified, the fixture's own state): behaviour unchanged."""

    def test_warmer_proceeds_as_before(self) -> None:
        r = moves.say.__wrapped__("warmer", commit=False, job="job-test")
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(len(self.sets()), 1)
        self.assertEqual(self.sets()[0]["path"], "iop/temperature/temperature")

    def test_other_presets_are_not_guarded(self) -> None:
        for preset in (0, 1, 3):
            with self.subTest(preset=preset):
                hex_ = _PRESET_2_HEX[:-8] + f"{preset:02x}000000"
                sidecar = Path(str(self.raw) + ".xmp")
                text = sidecar.read_text(encoding="utf-8")
                sidecar.write_text(text.replace(_PRESET_2_HEX, hex_), encoding="utf-8")
                try:
                    darkroom.set_param.__wrapped__("temperature", "temperature", 5000.0,
                                                   commit=False, job="job-test")
                finally:
                    sidecar.write_text(text, encoding="utf-8")
        self.assertEqual(len(self.sets()), 3)


class DirectVerbsUnderD65LateTest(_WbLateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.set_preset(_PRESET_4_HEX)

    def test_set_temperature_is_refused(self) -> None:
        with self.assertRaises(ModuleUnavailable) as cm:
            darkroom.set_param.__wrapped__("temperature", "temperature", 5000.0, job="job-test")
        self.assert_honest(cm.exception)
        self.assert_nothing_written()

    def test_set_tint_is_refused(self) -> None:
        with self.assertRaises(ModuleUnavailable) as cm:
            darkroom.set_param.__wrapped__("temperature", "tint", 1.1, job="job-test")
        self.assert_honest(cm.exception)
        self.assertNotIn("warm the highlights", cm.exception.fix)
        self.assert_nothing_written()

    def test_nudge_temperature_is_refused(self) -> None:
        with self.assertRaises(ModuleUnavailable):
            darkroom.nudge.__wrapped__("temperature", "temperature", 2, job="job-test")
        self.assert_nothing_written()

    def test_adjust_temperature_is_refused_before_the_bounce(self) -> None:
        with self.assertRaises(ModuleUnavailable):
            moves.adjust.__wrapped__("temperature", "temperature", 300.0, job="job-test")
        self.assert_nothing_written()

    def test_other_modules_are_not_guarded(self) -> None:
        darkroom.set_param.__wrapped__("exposure", "exposure", 1.0, commit=False,
                                       job="job-test")
        self.assertEqual(len(self.sets()), 1)

    def test_dry_run_is_unchanged(self) -> None:
        r = darkroom.set_param.__wrapped__("temperature", "temperature", 5000.0, dry_run=True,
                                           job="job-test")
        self.assertEqual(r.outcome, "dry_run")
        self.assertEqual(self.bridge.calls, [])

    def test_wb_calibrate_sweep_is_exempt(self) -> None:
        # wb.calibrate measures the Kelvin->coefficient mapping on a scratch
        # copy; the late correction does not enter that measurement.
        token = darkroom.WB_LATE_EXEMPT.set(True)
        try:
            darkroom.set_param.__wrapped__("temperature", "temperature", 5000.0, commit=False,
                                           job="job-test")
        finally:
            darkroom.WB_LATE_EXEMPT.reset(token)
        self.assertEqual(len(self.sets()), 1)
        self.assertFalse(darkroom.WB_LATE_EXEMPT.get(), "the exemption must not leak")


class ModuleUnavailableErrorTest(_WbLateFixture):
    def test_code_is_in_the_refusal_set_with_an_exit_code(self) -> None:
        self.assertEqual(ModuleUnavailable.code, "module_unavailable")
        self.assertIn(ModuleUnavailable, errors.ERROR_CLASSES)
        # 26, not 25: fix/htc-applied-verify's write_unverified takes 25.
        self.assertEqual(errors.EXIT_CODES["module_unavailable"], 26)


class UndoOfALateModeMoveWarnsTest(_test_wb.UndoViaKelvinInversionTest):
    """Undo is not blocked, but a move made while the preset was 4 cannot be
    fully restored: the Kelvin slider forces preset 2, so the coefficients
    come back without the late correction. Undo must say so."""

    def setUp(self) -> None:
        super().setUp()
        entry = journal.begin(
            "job-test", verb="darkroom.set", image_id=12, path=str(self.raw),
            module="temperature", param="temperature", instance=0,
            before={"source": "xmp", "enabled": True, "history_end": 14, "value": None,
                    "params": {**self.before_params, "preset": 4}, "params_hex": "deadbeef"},
        )
        journal.complete("job-test", entry.seq, after={"value": 6500.0})

    def test_undo_warns_the_late_correction_was_not_restored(self) -> None:
        self._landed_ratio = self._target_ratio
        result = darkroom.undo_last(single=True)
        assert result.undone is not None
        self.assertTrue(any("camera reference (late)" in w for w in result.warnings),
                        result.warnings)
        self.assertEqual(result.undone.get("late_correction_restored"), False)


# The inherited base-class tests run in test_wb.py already.
for _name in ("test_undo_via_kelvin_inversion_when_verified",
              "test_undo_via_kelvin_inversion_unverified_raises_and_leaves_entry_open"):
    setattr(UndoOfALateModeMoveWarnsTest, _name, None)


class CalibrateOnALateModeImageTest(_test_wb._CalibrateFixture):
    """`wb.calibrate`'s sweep is exempt: it measures Kelvin->coefficients,
    which the late correction does not enter. The starting sidecar is preset 4."""

    def setUp(self) -> None:
        super().setUp()
        sidecar = Path(str(self.raw) + ".xmp")
        text = sidecar.read_text(encoding="utf-8")
        p2 = _test_wb._temperature_params_hex((1.6, 1.0, 1.8, float("inf")))
        p4 = _test_wb._temperature_params_hex((1.6, 1.0, 1.8, float("inf")), preset=4)
        sidecar.write_text(text.replace(p2, p4), encoding="utf-8")
        self.assertEqual(darkroom.wb_preset(str(self.raw)), 4)

    def test_sweep_runs_and_the_exemption_does_not_leak(self) -> None:
        from scouter.core import wb
        self.coeffs_by_k = {3000.0: (0.50, 1.0, 1.0), 3100.0: (0.60, 1.0, 1.0)}
        wb.calibrate(str(self.raw), 3000, 3100, 100, confirm=True, job="job-test")
        sent = [p for m, p in self.bridge.calls if m == "darkroom.action"]
        self.assertEqual(len(sent), 2)
        self.assertFalse(darkroom.WB_LATE_EXEMPT.get())
