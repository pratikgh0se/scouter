"""Phase 3 task 2: the vocabulary table, its derived status, and its golden."""

from __future__ import annotations

import difflib
import json
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import dtbridge, vocab, xmp
from scouter.errors import UsageError

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "vocab_golden.json"
REGEN = (
    "uv run --extra dev python -c \"from scouter.core import vocab; import json; "
    "print(json.dumps(vocab.table(), indent=2, sort_keys=True))\" > tests/fixtures/vocab_golden.json"
)


class VocabGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_the_table_matches_the_golden(self) -> None:
        actual = json.dumps(vocab.table(), indent=2, sort_keys=True) + "\n"
        if not GOLDEN.exists():
            GOLDEN.write_text(actual)
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text()
        if expected != actual:
            diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                  "golden", "actual", lineterm=""))
            self.fail(f"vocabulary changed; if intended, regenerate with:\n{REGEN}\n{diff}")


class VocabShapeTest(unittest.TestCase):
    def test_every_row_is_complete_and_sane(self) -> None:
        seen = set()
        for s in vocab.PARAMS:
            with self.subTest(row=f"{s.module}.{s.param}"):
                self.assertNotIn((s.module, s.param), seen)
                seen.add((s.module, s.param))
                self.assertIn(s.unit, ("EV", "K", "ratio", "%", "°", "—"))
                self.assertIn(s.value_source, ("xmp", "wb_table", "none"))
                self.assertIn(s.concept, vocab.CONCEPT_IDS)
                if s.reachable:
                    self.assertLess(s.lo, s.hi)
                    self.assertGreater(s.step, 0)
                    self.assertLessEqual(s.step, (s.hi - s.lo) / 4)
                    self.assertTrue(s.why and s.watch)
                if (s.module, s.param) not in dtbridge.ACTION_PATHS and s.reachable:
                    self.assertTrue(s.candidate_paths, "an unpinned reachable row needs candidates")
                self.assertEqual(s.display_scale, 100.0 if s.unit == "%" else 1.0)

    def test_initial_statuses_match_the_spec_table(self) -> None:
        live = {f"{s.module}.{s.param}" for s in vocab.PARAMS if vocab.status(s) == "live"}
        self.assertEqual(live, {
            "exposure.exposure", "exposure.black", "sigmoid.contrast",
            "temperature.temperature", "colorbalancergb.contrast",
        })
        absolute = {f"{s.module}.{s.param}" for s in vocab.PARAMS if vocab.status(s) == "absolute_only"}
        self.assertEqual(absolute, {
            "sigmoid.skew", "temperature.tint", "colorbalancergb.global_saturation",
        })
        self.assertEqual(vocab.status(vocab.spec("colorzones", "curves")), "unreachable")

    def test_status_follows_a_new_path_pin_then_a_slot_pin(self) -> None:
        s = vocab.spec("colorbalancergb", "global_vibrance")
        self.assertEqual(vocab.status(s), "candidate")
        with mock.patch.dict(dtbridge.ACTION_PATHS,
                             {("colorbalancergb", "global_vibrance"): "iop/colorbalancergb/global vibrance"}):
            self.assertEqual(vocab.status(s), "absolute_only")
            layout = dict(xmp.LAYOUTS[("colorbalancergb", 5)])
            layout["named_slots"] = {**layout["named_slots"], 7: "global_vibrance"}
            with mock.patch.dict(xmp.LAYOUTS, {("colorbalancergb", 5): layout}):
                self.assertEqual(vocab.status(s), "live")

    def test_clamp_and_wrap(self) -> None:
        exp = vocab.spec("exposure", "exposure")
        self.assertEqual(vocab.clamp(exp, 9.0), (4.0, True))
        self.assertEqual(vocab.clamp(exp, 1.0), (1.0, False))
        hue = vocab.spec("colorbalancergb", "shadows_hue")
        self.assertEqual(vocab.clamp(hue, 370.0), (10.0, False))
        self.assertEqual(vocab.clamp(hue, -20.0), (340.0, False))

    def test_display_units(self) -> None:
        sat = vocab.spec("colorbalancergb", "global_saturation")
        self.assertAlmostEqual(vocab.to_display(sat, 0.05), 5.0)

    def test_closest_live_prefers_the_same_concept(self) -> None:
        alt = vocab.closest_live(vocab.spec("filmicrgb", "contrast"))
        self.assertEqual((alt.module, alt.param), ("sigmoid", "contrast"))


class VocabListVerbTest(unittest.TestCase):
    def test_list_counts_and_filter(self) -> None:
        result = vocab.list_vocab.__wrapped__(job="job-test")
        self.assertEqual(sum(result.counts.values()), len(vocab.PARAMS))
        only = vocab.list_vocab.__wrapped__("sigmoid", job="job-test")
        self.assertEqual({r["module"] for r in only.rows}, {"sigmoid"})

    def test_unknown_module_is_usage(self) -> None:
        with self.assertRaises(UsageError):
            vocab.list_vocab.__wrapped__("nosuchmodule", job="job-test")
