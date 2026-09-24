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

    def test_statuses_after_the_task_10_host_run(self) -> None:
        # Task 10 (2026-09-24 host run) pinned all 15 candidate paths and
        # every XMP slot the sentinel method found, so only tint (no value
        # source) stays absolute_only and colorzones.curves unreachable.
        by = {}
        for s in vocab.PARAMS:
            by.setdefault(vocab.status(s), set()).add(f"{s.module}.{s.param}")
        self.assertEqual(by.get("absolute_only"), {"temperature.tint"})
        self.assertEqual(by.get("unreachable"), {"colorzones.curves"})
        self.assertNotIn("candidate", by)
        self.assertEqual(len(by["live"]), 22)
        for row in ("colorbalancergb.shadows_hue", "colorbalancergb.highlights_chroma",
                    "sigmoid.skew", "filmicrgb.contrast", "colorzones.strength"):
            self.assertIn(row, by["live"])

    def test_status_follows_a_new_path_pin_then_a_slot_pin(self) -> None:
        # Unpin global_vibrance's path and slot, then put them back one by one.
        s = vocab.spec("colorbalancergb", "global_vibrance")
        layout = dict(xmp.LAYOUTS[("colorbalancergb", 5)])
        layout["named_slots"] = {i: n for i, n in layout["named_slots"].items()
                                 if n != "global_vibrance"}
        with mock.patch.dict(dtbridge.ACTION_PATHS), \
             mock.patch.dict(xmp.LAYOUTS, {("colorbalancergb", 5): layout}):
            path = dtbridge.ACTION_PATHS.pop(("colorbalancergb", "global_vibrance"))
            self.assertEqual(vocab.status(s), "candidate")
            dtbridge.ACTION_PATHS[("colorbalancergb", "global_vibrance")] = path
            self.assertEqual(vocab.status(s), "absolute_only")
            layout["named_slots"][29] = "global_vibrance"
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


class VocabPrettyTableTest(unittest.TestCase):
    """Spec §14.1: `--pretty` renders `vocab.list` as a table
    (module.param, status, unit, clamp, step)."""

    def test_vocab_list_has_a_table_renderer(self) -> None:
        from scouter.cli import tables

        payload = vocab.list_vocab.__wrapped__(job="job-test").to_dict()
        rendered = tables.render("vocab.list", payload)
        self.assertIsNotNone(rendered)
        lines = rendered.splitlines()
        self.assertEqual(lines[0].split(), ["module.param", "status", "unit", "clamp", "step"])
        body = lines[2:2 + len(vocab.PARAMS)]
        self.assertEqual(len(body), len(vocab.PARAMS))
        exposure = next(line for line in body if line.startswith("exposure.exposure "))
        self.assertEqual(exposure.split(), ["exposure.exposure", "live", "EV", "-3", "..", "4",
                                            "0.3"])
        hue = next(line for line in body if line.startswith("colorbalancergb.shadows_hue "))
        self.assertIn("0 .. 360 (wraps)", hue)
        curves = next(line for line in body if line.startswith("colorzones.curves "))
        self.assertEqual(curves.split()[1], "unreachable")
        self.assertEqual(curves.split()[-2:], ["-", "-"], "no clamp/step when unreachable")
        self.assertTrue(lines[-1].startswith(f"{len(vocab.PARAMS)} word(s): live "))
