"""Product close, item 5: the README user guide's example sentences are real.

"Editing with Scouter from Claude Code" lists example sentences in a table
(`| "sentence" | ... |`). Each must resolve through the real phrase resolver,
and every move it makes must be a `live` vocabulary row (the @tone contrast
move routes to sigmoid, the default tone mapper) -- a guide that promises a
sentence Scouter refuses is worse than no guide.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from scouter.core import phrases, vocab

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def _guide() -> str:
    start = README.index("## Editing with Scouter from Claude Code")
    end = README.index("\n## ", start + 5)
    return README[start:end]


def _examples() -> list[str]:
    return re.findall(r'^\| "([^"]+)" \|', _guide(), re.MULTILINE)


class UserGuideTest(unittest.TestCase):
    def test_the_guide_has_its_sections(self) -> None:
        guide = _guide()
        for heading in (
            "Register the server",
            "Single frame by phrases",
            "Undo",
            "Save as a preset",
            "Apply to a job",
            "Batch: `job run`",
            "Export",
            "Educative mode",
            "What refuses, and why",
        ):
            self.assertIn(heading, guide)

    def test_six_example_sentences_map_to_live_rows(self) -> None:
        examples = _examples()
        self.assertGreaterEqual(len(examples), 6, examples)
        for sentence in examples:
            with self.subTest(sentence=sentence):
                res = phrases.resolve(sentence, None)
                for mv in res.moves:
                    module = "sigmoid" if mv.module == phrases.TONE else mv.module
                    spec = vocab.spec(module, mv.param)
                    self.assertIsNotNone(spec, f"{module}.{mv.param}")
                    self.assertEqual(vocab.status(spec), "live", f"{module}.{mv.param}")

    def test_the_refusals_named_are_really_refused(self) -> None:
        guide = _guide()
        self.assertIn("tint", guide)
        self.assertIn("mask", guide)
        self.assertIn("curves", guide)
        self.assertEqual(vocab.status(vocab.spec("temperature", "tint")), "absolute_only")
        self.assertEqual(vocab.status(vocab.spec("colorzones", "curves")), "unreachable")
