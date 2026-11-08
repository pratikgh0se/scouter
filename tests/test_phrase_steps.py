"""Phrase step ladder: for EVERY phrase family, touch < bare < lot, strictly,
on the real resolver output (2026-11-09 phrase-step ruling)."""

from __future__ import annotations

import unittest

from scouter.core import phrases, vocab


def _mags(phrase: str) -> dict[tuple[str, str], float]:
    return {(m.module, m.param): abs(m.notches * phrases.row_for(m).step)
            for m in phrases.resolve(phrase).moves if m.kind == "adjust"}


class PhraseLadderTest(unittest.TestCase):
    def test_every_adjusting_phrase_family_is_strictly_ordered(self) -> None:
        checked = 0
        for p in phrases.PHRASES:
            if not any(m.kind == "adjust" for m in p.moves):
                continue
            syn = p.synonyms[0]
            touch, bare, lot = (_mags(f"{syn} a touch"), _mags(syn), _mags(f"{syn} a lot"))
            self.assertTrue(touch, p.key)
            for k in touch:
                with self.subTest(family=p.key, param=k):
                    self.assertLess(touch[k], bare[k])
                    self.assertLess(bare[k], lot[k])
            checked += 1
        self.assertGreater(checked, 25)

    def test_lot_stays_inside_the_declared_range_after_the_step_raise(self) -> None:
        for p in phrases.PHRASES:
            for m in p.moves:
                if m.kind != "adjust":
                    continue
                s = phrases.row_for(m)
                with self.subTest(family=p.key, param=f"{s.module}.{s.param}"):
                    self.assertLessEqual(2.0 * abs(m.notches) * s.step, s.hi - s.lo)
                    self.assertLessEqual(s.step, (s.hi - s.lo) / 4)


if __name__ == "__main__":
    unittest.main()
