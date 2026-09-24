"""Phase 3 task 4: educative lines (row 346), pinned by a golden."""

from __future__ import annotations

import difflib
import json
import unittest
from pathlib import Path

from scouter import paths
from scouter.core import explain, vocab
from scouter.errors import UsageError
from tests.scouter_fakes import TempCache

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "explain_golden.json"
REGEN = (
    "uv run --extra dev python -c \"from scouter.core import explain, vocab; import json; "
    "data = {'brief': {f'{s.module}.{s.param}': explain.brief_line("
    "explain.MoveNote(s, s.lo + s.step, s.lo + 2 * s.step)) for s in vocab.PARAMS "
    "if vocab.status(s) != 'unreachable'}, "
    "'concepts': {cid: explain.concept_line(cid) for cid in vocab.CONCEPT_IDS}}; "
    "print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))\" "
    "> tests/fixtures/explain_golden.json"
)


def _note(module: str, param: str, prev: float, target: float, kind: str = "adjust"):
    return explain.MoveNote(vocab.spec(module, param), prev, target, kind)


class ExplainGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_brief_lines_and_concepts_match_the_golden(self) -> None:
        data = {
            "brief": {
                f"{s.module}.{s.param}": explain.brief_line(
                    explain.MoveNote(s, s.lo + s.step, s.lo + 2 * s.step))
                for s in vocab.PARAMS if vocab.status(s) != "unreachable"
            },
            "concepts": {cid: explain.concept_line(cid) for cid in vocab.CONCEPT_IDS},
        }
        actual = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if not GOLDEN.exists():
            GOLDEN.write_text(actual, encoding="utf-8")
            self.fail(f"golden created at {GOLDEN}; review and commit it")
        expected = GOLDEN.read_text(encoding="utf-8")
        if expected != actual:
            diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                  "golden", "actual", lineterm=""))
            self.fail(f"explanation text changed; regenerate deliberately with:\n{REGEN}\n{diff}")


class ExplainLevelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def test_brief_sentence_shape(self) -> None:
        line = explain.brief_line(_note("exposure", "exposure", 1.55, 1.85))
        self.assertEqual(
            line,
            "exposure › exposure up +0.3 EV (1.55 EV → 1.85 EV): brightens or darkens the whole "
            "frame evenly, before any tone mapping; watch the highlights for clipping and the "
            "shadows for noise.",
        )

    def test_percent_rows_show_display_units(self) -> None:
        line = explain.brief_line(_note("colorbalancergb", "contrast", 0.0, 0.05))
        self.assertIn("up +5% (0% → 5%)", line)

    def test_wrapped_hue_uses_signed_shortest_angle(self) -> None:
        line = explain.brief_line(_note("colorbalancergb", "shadows_hue", 350.0, 10.0))
        self.assertIn("up +20°", line)
        self.assertIn("(350° → 10°)", line)

    def test_off_is_empty(self) -> None:
        self.assertEqual(explain.lines([_note("exposure", "exposure", 1, 1.3)],
                                       level="off", job="job-test"), [])

    def test_full_explains_a_concept_once_per_session(self) -> None:
        notes = [_note("exposure", "exposure", 1, 1.3)]
        first = explain.lines(notes, level="full", job="job-test")
        self.assertEqual(len(first), 2)
        self.assertTrue(first[0].startswith("[Scene-referred exposure]"))
        second = explain.lines(notes, level="full", job="job-test")
        self.assertEqual(len(second), 1, "the concept must not repeat in the same session")
        self.assertEqual(paths.job_state_read("job-test")["explained_concepts"], ["exposure"])
        fresh = explain.lines(notes, level="full", job="job-other")
        self.assertEqual(len(fresh), 2, "a new session explains it again")

    def test_brief_does_not_mark_concepts(self) -> None:
        explain.lines([_note("exposure", "exposure", 1, 1.3)], level="brief", job="job-test")
        self.assertNotIn("explained_concepts", paths.job_state_read("job-test"))

    def test_two_moves_sharing_a_concept_explain_it_once(self) -> None:
        notes = [_note("colorbalancergb", "shadows_hue", 0, 220, "set"),
                 _note("colorbalancergb", "shadows_chroma", 0, 0.02)]
        out = explain.lines(notes, level="full", job="job-test")
        self.assertEqual(sum(1 for line in out if line.startswith("[")), 1)
        self.assertEqual(len(out), 3)

    def test_resolve_level(self) -> None:
        self.assertEqual(explain.resolve_level("full"), "full")
        self.assertEqual(explain.resolve_level(None), "brief")
        with self.assertRaises(UsageError):
            explain.resolve_level("loud")

    def test_preset_lines(self) -> None:
        out = explain.preset_lines("save", "harbour-warm", ["temperature", "sigmoid"], 2,
                                   level="brief", job="job-test")
        self.assertEqual(out, ["saved 2 module(s) (temperature, sigmoid) as style 'harbour-warm': "
                               "apply it from darktable's styles panel or with preset apply."])

    def test_every_row_concept_has_text(self) -> None:
        self.assertEqual(set(explain.CONCEPTS), set(vocab.CONCEPT_IDS))


if __name__ == "__main__":
    unittest.main()
