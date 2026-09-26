"""Row 433, night-1 gauntlet: the write verbs verify what darktable stored.

A. `ok` while the sidecar value is not the target (silent failure):
   `cov-phrase-faded_blacks-olympus-touch` (target -0.002744, sidecar 0.0098,
   history_end 112 -> 92), `cov-phrase-warmer-olympus-bare` (coefficients
   untouched), `cov-phrase-more_contrast-sony_a7-lot` (1.7 asked, 1.5 read).
B. A darktable hard clamp reported as a plain ok: `cov-sigmoid.skew-olympus-
   set_hi_plus` (1.1 asked, 1.0 stored, no `clamped`, no warning).
C. `commit_timeout` after 5.4 s when darktable writes nothing because the
   effective state already is the sidecar's: `cov-colorbalancergb.
   highlights_chroma-olympus-set_lo_minus` (-0.02 clamps to the 0 it holds),
   `cov-colorbalancergb.contrast-olympus-repeat_set` (0 on a module with no
   history entry), `cov-colorbalancergb-olympus-reset` (already at defaults),
   `cov-colorbalancergb.contrast-olympus-repeat_adjust` (undo of a set that
   never reached the sidecar).

The fake darktable below lands (or does not land) values in a real sidecar
file, so every readback goes through the real XMP decoder.
"""

from __future__ import annotations

import os
import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter import registry
from scouter.core import darkroom, dtbridge, journal, moves, vocab, wb
from scouter.errors import CommitTimeout, WriteUnverified
from scouter.types import CommitResult
from tests.scouter_fakes import FakeBridge, TempCache, with_new_history_hash, with_param_value

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_BY_PATH = {v: k for k, v in dtbridge.ACTION_PATHS.items()}


class _FakeDarktable(unittest.TestCase):
    """A sidecar-backed darktable double.

    `darkroom.action` records what was sent; the patched `darkroom._commit`
    then applies `store(module, param, value)` (default: darktable's own
    effective value -- clamps and hue wrap) to the sidecar. When that changes
    nothing, darktable writes nothing: `expect_change=False` answers
    `committed=False`, `expect_change=True` times out -- exactly the host
    behaviour behind night-1 C.
    """

    fixture = "P6281348.ORF.xmp"

    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURES / self.fixture, self.sidecar)
        cfg_dir = root / "config"
        (cfg_dir / "wb").mkdir(parents=True)
        shutil.copy(FIXTURES / "wb-em10iii.json", cfg_dir / "wb" / "em10iii.json")
        env = mock.patch.dict(os.environ, {"SCOUTER_CONFIG": str(cfg_dir / "config.json")})
        env.start()
        self.addCleanup(env.stop)
        cam = mock.patch("scouter.core.moves._camera_for", return_value="em10iii")
        cam.start()
        self.addCleanup(cam.stop)
        self.pending: list[tuple[str, str, float]] = []
        self.store = lambda module, param, value: vocab.dt_effective(module, param, value)[0]
        #: Force a history rewrite even when no value changed (a stale
        #: library.db re-sync: the commit "succeeds", the value does not land).
        self.force_rewrite = False
        self.history_end: int | None = None
        self.commits: list[tuple] = []
        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": self._action,
            "module.reset": self._reset,
            "module.enable": {"nan": False, "view": "darkroom"},
        })
        dtbridge.set_bridge(self.bridge)
        patcher = mock.patch("scouter.core.darkroom._commit", side_effect=self._commit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    # -- the double -------------------------------------------------------

    def _action(self, p: dict) -> dict:
        key = _BY_PATH.get(p.get("path"))
        if key is not None and p.get("effect") == "set":
            self.pending.append((key[0], key[1], float(p["value"])))
        return {"nan": False, "status": p.get("value"), "view": "darkroom", **p}

    def _reset(self, p: dict) -> dict:
        self.pending.append((p["module"], "__reset__", 0.0))
        return {"nan": False, "view": "darkroom"}

    def _commit(self, job, image, *, strategy="auto", attempts=None, expect_change=True):
        self.commits.append((attempts, expect_change))
        before = self.sidecar.read_text()
        text = before
        for module, param, value in self.pending:
            if param == "__reset__":
                continue  # the only reset under test is of a module already at defaults
            stored = self.store(module, param, value)
            if stored is not None and self.value(module, param) == stored:
                continue  # already what darktable holds: no history item
            if stored is not None and module in ("exposure", "sigmoid", "colorbalancergb"):
                try:
                    text = with_param_value(text, module, param, stored)
                except AssertionError:
                    # No history entry for the module: darktable runs it at
                    # its defaults, and a set to the default adds nothing.
                    if stored != darkroom._declared_default(module, param):
                        raise
        self.pending.clear()
        if text == before and not self.force_rewrite:
            if not expect_change:
                return CommitResult(committed=False, changed=False, confirmed=False)
            raise CommitTimeout("darktable did not write the sidecar (fake)")
        if self.history_end is not None:
            text = re.sub(r'darktable:history_end="\d+"',
                          f'darktable:history_end="{self.history_end}"', text)
        self.sidecar.write_text(with_new_history_hash(text))
        return CommitResult(committed=True, changed=True, warnings=[])

    def value(self, module: str, param: str) -> float | None:
        return darkroom._read_value(str(self.raw), module, param, 0)


class SilentFailureTest(_FakeDarktable):
    """A: a committed write whose sidecar says something else is an error."""

    def test_set_whose_sidecar_shows_another_value_is_write_unverified(self) -> None:
        # faded blacks, touch: the commit "succeeded" by rewriting the sidecar
        # from an older state -- black 0.0098, history 14 -> 12.
        self.store = lambda m, p, v: 0.0098 if (m, p) == ("exposure", "black") else v
        self.history_end = 12
        with self.assertRaises(WriteUnverified) as cm:
            darkroom.set_param("exposure", "black", -0.002744140625, job="job-test")
        d = cm.exception.detail
        self.assertEqual(cm.exception.code, "write_unverified")
        self.assertAlmostEqual(d["applied"], 0.0098, places=6)
        self.assertAlmostEqual(d["expected"], -0.002744140625)
        self.assertEqual((d["history_end_before"], d["history_end_after"]), (14, 12))
        self.assertIn("BACKWARDS", cm.exception.message)
        self.assertIn("bounce", cm.exception.fix)
        # The action was sent, so the journal entry stays undoable.
        self.assertIsNotNone(journal.latest_open("job-test"))

    def test_the_envelope_is_not_ok(self) -> None:
        self.store = lambda m, p, v: 0.0098
        env = registry.call("darkroom.say", "mcp", phrase="faded blacks a touch", job="job-test")
        self.assertFalse(env.ok)
        self.assertEqual(env.outcome, "error:write_unverified")
        self.assertEqual(env.error["code"], "write_unverified")

    def test_adjust_that_did_not_land_is_write_unverified(self) -> None:
        # more contrast, lot: sigmoid.contrast 1.4 -> 1.7 asked, 1.4 kept,
        # while the history was rewritten (so the commit reports success).
        self.store = lambda m, p, v: None
        self.force_rewrite = True
        with self.assertRaises(WriteUnverified) as cm:
            moves.adjust.__wrapped__("sigmoid", "contrast", 0.3, job="job-test")
        self.assertAlmostEqual(cm.exception.detail["applied"], 1.4, places=5)

    def test_warmer_whose_coefficients_did_not_move_is_write_unverified(self) -> None:
        # warmer, bare: 5399 K -> 5699 K asked; the temperature coefficients
        # in the sidecar never changed, the commit rewrote the history.
        self.force_rewrite = True
        with self.assertRaises(WriteUnverified) as cm:
            moves.adjust.__wrapped__("temperature", "temperature", 300.0, job="job-test")
        self.assertIn("did not change", cm.exception.message)
        self.assertEqual(cm.exception.code, "write_unverified")

    def test_warmer_that_landed_within_the_model_offset_is_ok(self) -> None:
        # darktable's Kelvin model and the measured table disagree by ~2.3%
        # in r/b (night 1: 5225 K always reads back 5124 K). That is a landed
        # move, not a failure.
        before = darkroom._temperature_extra(str(self.raw), 0)
        target = 5399.05 + 300.0
        want = wb.expected_ratio("em10iii", target) * (1 - 0.023)
        landed = dict(before["params"])
        landed["coeff_r"] = want * landed["coeff_b"]
        sent = {"n": 0}
        real = self._action

        def action(p: dict) -> dict:
            sent["n"] += 1
            return real(p)

        self.bridge.responses["darkroom.action"] = action
        self.force_rewrite = True
        with mock.patch("scouter.core.darkroom._temperature_extra",
                        side_effect=lambda path, inst: {"params": landed, "params_hex": None}
                        if sent["n"] else before):
            r = moves.adjust.__wrapped__("temperature", "temperature", 300.0, job="job-test")
        self.assertEqual(r.outcome, "ok")

    def test_slider_rounding_is_not_a_mismatch(self) -> None:
        # darktable stores black -0.002744 as -0.0027 (its 4 displayed digits).
        self.store = lambda m, p, v: round(v, 4)
        r = darkroom.set_param("exposure", "black", -0.002744140625, job="job-test")
        self.assertEqual(r.outcome, "ok")
        self.assertFalse(r.clamped)

    def test_a_small_move_that_did_not_land_is_caught_despite_the_tolerance(self) -> None:
        # A 0.01 EV set is far below exposure's step/10 (0.03): the tolerance
        # is capped at half the requested change, so "unchanged" still fails.
        self.store = lambda m, p, v: None
        self.force_rewrite = True
        with self.assertRaises(WriteUnverified):
            darkroom.set_param("exposure", "exposure", 1.5600002, job="job-test")

    def test_a_nudge_whose_sidecar_disagrees_with_darktables_report_is_unverified(self) -> None:
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False, "status": 1.6, "view": "darkroom", **p}
        self.force_rewrite = True
        with self.assertRaises(WriteUnverified):
            darkroom.nudge("exposure", "exposure", steps=1, job="job-test")

    def test_say_reports_every_move_it_sent_when_a_later_one_fails(self) -> None:
        # Spec 5.4 point 4: the moves that landed are reported, the one that
        # did not is in `refused` with what the sidecar really says, and the
        # envelope is not ok (outcome partial).
        calls = {"n": 0}

        def store(m, p, v):
            calls["n"] += 1
            return v if calls["n"] == 1 else 0.0098

        from scouter.core import phrases

        two = phrases.Phrase("two_moves", ("two moves",), (
            phrases._adj("exposure", "exposure", 1), phrases._adj("exposure", "black", -1)))
        self.store = store
        with mock.patch.dict(phrases._INDEX, {"two moves": two}):
            env = registry.call("darkroom.say", "mcp", phrase="two moves", job="job-test")
        self.assertFalse(env.ok)
        self.assertEqual(env.outcome, "partial")
        self.assertEqual(len(env.data["moves"]), 1)
        refused = env.data["refused"][0]
        self.assertEqual(refused["code"], "write_unverified")
        self.assertAlmostEqual(refused["detail"]["applied"], 0.0098, places=6)

    def test_hue_wraps_and_is_neither_clamped_nor_unverified(self) -> None:
        shutil.copy(FIXTURES / "colorbalancergb.xmp", self.sidecar)
        r = darkroom.set_param("colorbalancergb", "highlights_hue", -10.0, job="job-test")
        self.assertAlmostEqual(r.applied, 350.0, places=3)
        self.assertFalse(r.clamped)


class HardClampTest(_FakeDarktable):
    """B: darktable's own hard limit is reported, never passed off as ok."""

    def test_skew_beyond_darktables_limit_is_reported_clamped(self) -> None:
        r = darkroom.set_param("sigmoid", "skew", 1.1, job="job-test")
        self.assertEqual(r.outcome, "ok")
        self.assertTrue(r.clamped)
        self.assertAlmostEqual(r.applied, 1.0, places=6)
        self.assertEqual(r.requested, 1.1)
        self.assertTrue(any("hard limit" in w and "1" in w for w in r.warnings), r.warnings)
        env = registry.call("darkroom.set", "mcp", module="sigmoid", param="skew", value=-1.1,
                            job="job-test")
        self.assertTrue(env.data["clamped"])

    def test_a_mismatch_no_bound_explains_is_still_unverified(self) -> None:
        self.store = lambda m, p, v: 0.3
        with self.assertRaises(WriteUnverified):
            darkroom.set_param("sigmoid", "skew", 0.5, job="job-test")


class NothingToWriteTest(_FakeDarktable):
    """C: darktable wrote nothing because the effective state was already
    the sidecar's -- answer promptly with noop, never commit_timeout."""

    fixture = "colorbalancergb.xmp"

    def test_set_below_darktables_min_at_that_min_is_a_prompt_clamped_noop(self) -> None:
        self.assertEqual(self.value("colorbalancergb", "highlights_chroma"), 0.0)
        r = darkroom.set_param("colorbalancergb", "highlights_chroma", -0.02, job="job-test")
        self.assertTrue(r.committed)
        self.assertTrue(r.noop)
        self.assertTrue(r.clamped)
        self.assertEqual(r.applied, 0.0)
        # One expect-no-change bounce, not the 5.4 s expect-change wait.
        self.assertEqual(self.commits, [(1, False)])

    def test_set_to_the_default_of_a_module_with_no_history_is_a_prompt_noop(self) -> None:
        shutil.copy(FIXTURES / "P6281348.ORF.xmp", self.sidecar)  # no colorbalancergb entry
        r = darkroom.set_param("colorbalancergb", "contrast", 0.0, job="job-test")
        self.assertTrue(r.noop)
        self.assertEqual(r.applied, 0.0)
        self.assertEqual(self.commits, [(1, False)])
        self.assertTrue(any("default" in w for w in r.warnings), r.warnings)

    def test_a_write_that_truly_never_landed_still_times_out(self) -> None:
        self.store = lambda m, p, v: None  # darktable ignores the set entirely
        with self.assertRaises(CommitTimeout):
            darkroom.set_param("colorbalancergb", "highlights_chroma", 0.1, job="job-test")

    def test_reset_of_a_module_already_at_defaults_is_a_prompt_noop(self) -> None:
        text = with_param_value(self.sidecar.read_text(), "colorbalancergb", "contrast", 0.0)
        self.sidecar.write_text(text)
        r = darkroom.reset("colorbalancergb", confirm=True, job="job-test")
        self.assertTrue(r.committed)
        self.assertTrue(r.noop)
        self.assertEqual(self.commits, [(1, False)])

    def test_reset_of_a_module_not_at_defaults_still_times_out(self) -> None:
        # contrast is 0.2 in the fixture; the fake never writes a reset.
        with self.assertRaises(CommitTimeout):
            darkroom.reset("colorbalancergb", confirm=True, job="job-test")

    def test_undo_of_a_set_that_never_reached_the_sidecar_is_a_prompt_noop(self) -> None:
        shutil.copy(FIXTURES / "P6281348.ORF.xmp", self.sidecar)
        self.store = lambda m, p, v: None  # the set is never written
        with self.assertRaises(CommitTimeout):
            darkroom.set_param("exposure", "exposure", 2.0, job="job-test")
        self.commits.clear()
        r = darkroom.undo_last.__wrapped__(job="job-test")
        self.assertTrue(r.committed)
        self.assertTrue(r.undone["verified"])
        self.assertEqual(self.commits, [(1, False)])


if __name__ == "__main__":
    unittest.main()
