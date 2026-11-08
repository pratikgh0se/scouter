"""Card t_176d9d85: a nudge's `dt.gui.action` status (a 0..1 slider position,
design 7.3) must never become a journalled value, and a nudge at a real limit
must not tell the agent to retry. Each check pairs two oracles (Z2): the
journal entry / raised error against the undo outcome / bridge traffic.
"""

from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scouter.core import darkroom, dtbridge, journal, moves, vocab
from scouter.errors import CommitTimeout, ScouterError, ValueUnknown
from tests.scouter_fakes import FakeBridge, TempCache, with_param_value

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "P6281348.ORF.xmp"
JOB = "job-test"
SLIDER_POSITION = 0.5


class _NudgeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        root = self.cache.start()
        self.raw = root / "P6281348.ORF"
        self.raw.write_bytes(b"not really a raw")
        self.sidecar = Path(str(self.raw) + ".xmp")
        shutil.copy(FIXTURE, self.sidecar)
        self.bridge = FakeBridge({
            "darkroom.current": {"image_id": 12, "path": str(self.raw), "view": "darkroom",
                                 "source": "action_images"},
            "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
            "darkroom.action": lambda p: {"nan": False, "view": "darkroom", **p,
                                          "status": SLIDER_POSITION},
        })
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _timeout(self) -> CommitTimeout:
        return CommitTimeout("darktable did not write the sidecar in time")

    def _set_actions(self) -> list[dict]:
        return [p for m, p in self.bridge.calls
                if m == "darkroom.action" and p.get("effect") == "set"]


class UncommittedNudgeJournalTest(_NudgeFixture):
    def test_status_never_reaches_the_journal_or_a_later_before_state(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        nudge_entry, set_entry = journal.read_all(JOB)[-2:]
        # oracle 1: the journal never carries the slider position as a value
        self.assertNotEqual(nudge_entry.after.get("value"), SLIDER_POSITION)
        self.assertIsNone(nudge_entry.after.get("value"))
        self.assertNotEqual(set_entry.before.get("value"), SLIDER_POSITION)
        # oracle 2: undoing the later write refuses and sends no restore at all
        with self.assertRaises(ScouterError):
            darkroom.undo_last(single=True)
        self.assertEqual([p["value"] for p in self._set_actions()], [2.0])
        self.assertFalse(journal.read_all(JOB)[-1].undone)

    def test_the_pending_uncommitted_flag_survives_an_unknown_value(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        raw, before, pending = darkroom._raw_and_before_state(
            JOB, 12, str(self.raw), "exposure", "exposure", 0)
        self.assertTrue(pending)
        self.assertIsNone(before.get("value"))
        self.assertAlmostEqual(raw["value"], 1.5500002, places=5)

    def test_undoing_the_nudge_itself_restores_the_real_on_disk_value(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)
        entry = journal.read_all(JOB)[-1]
        # oracle 1: the nudge's before is the sidecar's real value
        self.assertAlmostEqual(entry.before["value"], 1.5500002, places=5)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            darkroom.undo_last(single=True)
        # oracle 2: the restore sent is that real value, never the position
        self.assertAlmostEqual(self._set_actions()[-1]["value"], 1.5500002, places=5)


class NudgeAtALimitTest(_NudgeFixture):
    def _put_skew_at(self, value: float) -> None:
        self.sidecar.write_text(
            with_param_value(self.sidecar.read_text(), "sigmoid", "skew", value))

    def test_a_nudge_at_a_declared_bound_is_an_honest_noop(self) -> None:
        self._put_skew_at(1.0)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            result = darkroom.nudge(module="sigmoid", param="skew", steps=1)
        # oracle 1: the result says nothing moved
        self.assertTrue(result.noop)
        self.assertTrue(result.committed)
        self.assertAlmostEqual(result.applied, 1.0, places=5)
        # oracle 2: the sidecar still reads the bound
        self.assertAlmostEqual(darkroom._read_value(str(self.raw), "sigmoid", "skew", 0),
                               1.0, places=5)

    def test_a_nudge_away_from_the_bound_is_not_a_noop(self) -> None:
        self._put_skew_at(1.0)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="sigmoid", param="skew", steps=-1)
        self.assertAlmostEqual(darkroom._read_value(str(self.raw), "sigmoid", "skew", 0),
                               1.0, places=5)
        self.assertEqual(self._set_actions(), [])

    def _with_cloned_sigmoid_item(self, text: str) -> str:
        """A new top-of-history sigmoid item with the SAME params (value stays)."""
        end = int(re.search(r'darktable:history_end="(\d+)"', text).group(1))
        items = [m for m in re.finditer(r'<rdf:li\s(?:(?!/>).)*?/>', text, re.S)
                 if 'darktable:operation="sigmoid"' in m.group(0)]
        item = re.sub(r'darktable:num="\d+"', f'darktable:num="{end}"', items[-1].group(0), count=1)
        seq_close = text.rindex("</rdf:Seq>", 0, text.index("</darktable:history>"))
        text = text[:seq_close] + " " + item + "\n    " + text[seq_close:]
        return re.sub(r'darktable:history_end="\d+"', f'darktable:history_end="{end + 1}"',
                      text, count=1)

    def test_a_bound_value_with_an_advanced_history_is_not_a_noop(self) -> None:
        self._put_skew_at(1.0)
        before = self.sidecar.read_text()

        def _history_moved(*_a, **_kw):
            self.sidecar.write_text(self._with_cloned_sigmoid_item(before))
            raise self._timeout()

        with mock.patch("scouter.core.darkroom._commit", side_effect=_history_moved):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="sigmoid", param="skew", steps=1)
        # the value really is still at the bound: only the history oracle refuses
        self.assertAlmostEqual(darkroom._read_value(str(self.raw), "sigmoid", "skew", 0),
                               1.0, places=5)

    def test_a_timeout_off_a_bound_says_read_the_value_and_not_to_retry(self) -> None:
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout) as caught:
                darkroom.nudge(module="exposure", param="exposure", steps=1)
        fix = caught.exception.fix.lower()
        # oracle 1: the fix text; oracle 2: the sidecar is unmoved, as it says
        self.assertIn("read the value", fix)
        self.assertIn("do not retry", fix)
        self.assertNotIn("first", fix)
        self.assertAlmostEqual(darkroom._read_value(str(self.raw), "exposure", "exposure", 0),
                               1.5500002, places=5)
        self.assertEqual(caught.exception.detail.get("param"), "exposure")


class UnknownValueReadersTest(_NudgeFixture):
    """Every reader of the journal's value must honour "unknown"."""

    def _nudge_uncommitted(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)

    def test_adjust_after_an_uncommitted_nudge_refuses_and_sends_nothing(self) -> None:
        self._nudge_uncommitted()
        calls_before = len(self.bridge.calls)
        with self.assertRaises(ValueUnknown) as caught:
            moves.adjust(module="exposure", param="exposure", delta=0.5, commit=False,
                         trust_sidecar=True)
        # oracle 1: nothing was sent after the refusal; oracle 2: the reason is the truth
        self.assertNotIn("darkroom.action",
                         [m for m, _ in self.bridge.calls[calls_before:]])
        self.assertEqual(self._set_actions(), [])
        self.assertIn("bounce", caught.exception.detail["reason"])

    def test_get_after_an_uncommitted_nudge_does_not_report_the_stale_value(self) -> None:
        self._nudge_uncommitted()
        result = moves.get(module="exposure", param="exposure")
        # oracle 1: no value; oracle 2: the sidecar does still hold the stale one
        self.assertIsNone(result.value)
        self.assertTrue(any("uncommitted nudge" in w for w in result.warnings))
        self.assertAlmostEqual(darkroom._read_value(str(self.raw), "exposure", "exposure", 0),
                               1.5500002, places=5)

    def test_get_with_bounce_reads_the_flushed_sidecar(self) -> None:
        self._nudge_uncommitted()
        with mock.patch("scouter.core.moves._precommit", return_value=None):
            result = moves.get(module="exposure", param="exposure", bounce=True)
        self.assertAlmostEqual(result.value, 1.5500002, places=5)
        self.assertEqual(result.source, "xmp")

    def test_adjust_with_bounce_moves_from_the_flushed_sidecar_value(self) -> None:
        self._nudge_uncommitted()
        with mock.patch("scouter.core.moves._precommit", return_value=None):
            result = moves.adjust(module="exposure", param="exposure", delta=0.5,
                                  commit=False, bounce=True)
        self.assertAlmostEqual(result.previous, 1.5500002, places=5)
        self.assertAlmostEqual(self._set_actions()[-1]["value"], 2.0500002, places=5)

    def test_before_fallback_names_the_unknown_value_as_the_reason(self) -> None:
        self._nudge_uncommitted()
        resolved = darkroom._current_image()
        with self.assertRaises(ValueUnknown) as caught:
            moves.before_fallback(resolved, vocab.spec("exposure", "exposure"), 0, JOB)
        self.assertIn("uncommitted nudge", caught.exception.detail["reason"])
        self.assertNotIn("did not decode", caught.exception.detail["reason"])

    def test_a_journal_entry_written_before_the_fix_is_still_unknown(self) -> None:
        resolved = darkroom._current_image()
        old = journal.begin(JOB, verb="darkroom.nudge", image_id=12, path=resolved.path,
                            module="exposure", param="exposure", instance=0,
                            before={"value": 1.5500002}, commit=False)
        journal.complete(JOB, old.seq, after={"steps": 1, "steps_landed": 1,
                                              "value": SLIDER_POSITION})
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        set_entry = journal.read_all(JOB)[-1]
        self.assertIsNone(set_entry.before.get("value"))
        with self.assertRaises(ScouterError):
            darkroom.undo_last(single=True)
        self.assertEqual([p["value"] for p in self._set_actions()], [2.0])


if __name__ == "__main__":
    unittest.main()
