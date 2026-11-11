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
from scouter.types import CommitResult
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
        self.bridge = FakeBridge(
            {
                "darkroom.current": {
                    "image_id": 12,
                    "path": str(self.raw),
                    "view": "darkroom",
                    "source": "action_images",
                },
                "image.lookup": {"found": True, "image_id": 12, "path": str(self.raw)},
                "darkroom.action": lambda p: {
                    "nan": False,
                    "view": "darkroom",
                    **p,
                    "status": SLIDER_POSITION,
                },
            }
        )
        dtbridge.set_bridge(self.bridge)

    def tearDown(self) -> None:
        dtbridge.set_bridge(None)
        self.cache.stop()

    def _timeout(self) -> CommitTimeout:
        return CommitTimeout("darktable did not write the sidecar in time")

    def _set_actions(self) -> list[dict]:
        return [
            p for m, p in self.bridge.calls if m == "darkroom.action" and p.get("effect") == "set"
        ]


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
            JOB, 12, str(self.raw), "exposure", "exposure", 0
        )
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
            with_param_value(self.sidecar.read_text(), "sigmoid", "skew", value)
        )

    def test_a_nudge_at_a_declared_bound_is_an_honest_noop(self) -> None:
        self._put_skew_at(1.0)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            result = darkroom.nudge(module="sigmoid", param="skew", steps=1)
        # oracle 1: the result says nothing moved
        self.assertTrue(result.noop)
        self.assertTrue(result.committed)
        self.assertAlmostEqual(result.applied, 1.0, places=5)
        # oracle 2: the sidecar still reads the bound
        self.assertAlmostEqual(
            darkroom._read_value(str(self.raw), "sigmoid", "skew", 0), 1.0, places=5
        )

    def test_a_nudge_away_from_the_bound_is_not_a_noop(self) -> None:
        self._put_skew_at(1.0)
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout):
                darkroom.nudge(module="sigmoid", param="skew", steps=-1)
        self.assertAlmostEqual(
            darkroom._read_value(str(self.raw), "sigmoid", "skew", 0), 1.0, places=5
        )
        self.assertEqual(self._set_actions(), [])

    def _with_cloned_sigmoid_item(self, text: str) -> str:
        """A new top-of-history sigmoid item with the SAME params (value stays)."""
        end = int(re.search(r'darktable:history_end="(\d+)"', text).group(1))
        items = [
            m
            for m in re.finditer(r"<rdf:li\s(?:(?!/>).)*?/>", text, re.S)
            if 'darktable:operation="sigmoid"' in m.group(0)
        ]
        item = re.sub(r'darktable:num="\d+"', f'darktable:num="{end}"', items[-1].group(0), count=1)
        seq_close = text.rindex("</rdf:Seq>", 0, text.index("</darktable:history>"))
        text = text[:seq_close] + " " + item + "\n    " + text[seq_close:]
        return re.sub(
            r'darktable:history_end="\d+"', f'darktable:history_end="{end + 1}"', text, count=1
        )

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
        self.assertAlmostEqual(
            darkroom._read_value(str(self.raw), "sigmoid", "skew", 0), 1.0, places=5
        )

    def test_a_timeout_off_a_bound_says_read_the_value_and_not_to_retry(self) -> None:
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            with self.assertRaises(CommitTimeout) as caught:
                darkroom.nudge(module="exposure", param="exposure", steps=1)
        fix = caught.exception.fix.lower()
        # oracle 1: the fix text; oracle 2: the sidecar is unmoved, as it says
        self.assertIn("read the value", fix)
        self.assertIn("do not retry", fix)
        self.assertNotIn("first", fix)
        self.assertAlmostEqual(
            darkroom._read_value(str(self.raw), "exposure", "exposure", 0), 1.5500002, places=5
        )
        self.assertEqual(caught.exception.detail.get("param"), "exposure")


class NudgeAtASoftLimitTest(_NudgeFixture):
    """N12: a GUI `+` step stops at the slider's SOFT max (colour calibration's
    illuminant temperature: 7000 K, measured on darktable 5.4.1) although `set`
    goes past it. The nudge must say so promptly, not wait out the commit
    timeout. Oracles (Z2): the sidecar value against the declared soft bound,
    and darktable's own live slider position (status 1.0 / 0.0)."""

    MODULE, PARAM = "channelmixerrgb", "temperature"

    def _state(self, value: float) -> dict:
        return {"value": value, "source": "xmp", "enabled": True}

    def _nudge(self, value: float, steps: int, position: float):
        self.bridge.responses["darkroom.action"] = lambda p: {
            "nan": False,
            "view": "darkroom",
            **p,
            "status": position,
        }
        with (
            mock.patch("scouter.core.darkroom._read_state", return_value=self._state(value)),
            mock.patch("scouter.core.darkroom._read_value", return_value=value),
            mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()) as commit,
        ):
            result = darkroom.nudge(module=self.MODULE, param=self.PARAM, steps=steps)
        return result, commit

    def test_the_soft_bound_is_declared_in_data_inside_the_safe_range(self) -> None:
        lo, hi = vocab.DT_SOFT_BOUNDS[(self.MODULE, self.PARAM)]
        row = vocab.spec(self.MODULE, self.PARAM)
        self.assertEqual((lo, hi), (2000.0, 7000.0))
        self.assertTrue(row.lo <= lo < hi <= row.hi)

    def test_nudge_up_at_the_soft_max_is_a_prompt_noop_that_names_the_way_on(self) -> None:
        result, commit = self._nudge(7000.0, 1, position=1.0)
        self.assertTrue(result.noop)
        self.assertTrue(result.committed)
        self.assertEqual(result.applied, 7000.0)
        text = " ".join(result.warnings)
        self.assertIn("soft limit", text)
        self.assertIn("darkroom_set", text)
        # oracle 2: nothing was sent to move the slider and nothing waited for a commit
        commit.assert_not_called()
        self.assertEqual(
            [
                p
                for m, p in self.bridge.calls
                if m == "darkroom.action" and p.get("effect") in ("up", "down")
            ],
            [],
        )
        self.assertEqual(journal.read_all(JOB), [])

    def test_nudge_up_from_beyond_the_soft_max_is_the_same_noop(self) -> None:
        result, commit = self._nudge(9000.0, 1, position=1.0)
        self.assertTrue(result.noop)
        self.assertEqual(result.applied, 9000.0)
        commit.assert_not_called()

    def test_nudge_down_from_the_soft_max_still_goes_to_darktable(self) -> None:
        with self.assertRaises(CommitTimeout):  # the double never writes; it must be SENT
            self._nudge(7000.0, -1, position=1.0)
        self.assertTrue(
            [
                p
                for m, p in self.bridge.calls
                if m == "darkroom.action" and p.get("effect") == "down"
            ]
        )

    def test_nudge_down_at_the_soft_min_is_a_prompt_noop(self) -> None:
        result, commit = self._nudge(2000.0, -1, position=0.0)
        self.assertTrue(result.noop)
        self.assertIn("soft limit", " ".join(result.warnings))
        commit.assert_not_called()

    def test_darktable_disagreeing_about_the_position_does_not_short_circuit(self) -> None:
        # the sidecar reads the soft max but the slider is mid-range: do not guess
        with self.assertRaises(CommitTimeout):
            self._nudge(7000.0, 1, position=0.5)
        self.assertTrue(
            [p for m, p in self.bridge.calls if m == "darkroom.action" and p.get("effect") == "up"]
        )


class UnknownValueReadersTest(_NudgeFixture):
    """Every reader of the journal's value must honour "unknown"."""

    def _nudge_uncommitted(self) -> None:
        darkroom.nudge(module="exposure", param="exposure", steps=1, commit=False)

    def test_adjust_after_an_uncommitted_nudge_refuses_and_sends_nothing(self) -> None:
        self._nudge_uncommitted()
        calls_before = len(self.bridge.calls)
        with self.assertRaises(ValueUnknown) as caught:
            moves.adjust(
                module="exposure", param="exposure", delta=0.5, commit=False, trust_sidecar=True
            )
        # oracle 1: nothing was sent after the refusal; oracle 2: the reason is the truth
        self.assertNotIn("darkroom.action", [m for m, _ in self.bridge.calls[calls_before:]])
        self.assertEqual(self._set_actions(), [])
        self.assertIn("bounce", caught.exception.detail["reason"])

    def test_get_after_an_uncommitted_nudge_does_not_report_the_stale_value(self) -> None:
        self._nudge_uncommitted()
        result = moves.get(module="exposure", param="exposure")
        # oracle 1: no value; oracle 2: the sidecar does still hold the stale one
        self.assertIsNone(result.value)
        self.assertTrue(any("uncommitted nudge" in w for w in result.warnings))
        self.assertAlmostEqual(
            darkroom._read_value(str(self.raw), "exposure", "exposure", 0), 1.5500002, places=5
        )

    def test_get_with_bounce_reads_the_flushed_sidecar(self) -> None:
        self._nudge_uncommitted()
        with mock.patch("scouter.core.moves._precommit", return_value=(None, True)):
            result = moves.get(module="exposure", param="exposure", bounce=True)
        self.assertAlmostEqual(result.value, 1.5500002, places=5)
        self.assertEqual(result.source, "xmp")

    def test_adjust_with_bounce_moves_from_the_flushed_sidecar_value(self) -> None:
        self._nudge_uncommitted()
        with mock.patch("scouter.core.moves._precommit", return_value=(None, True)):
            result = moves.adjust(
                module="exposure", param="exposure", delta=0.5, commit=False, bounce=True
            )
        self.assertAlmostEqual(result.previous, 1.5500002, places=5)
        self.assertAlmostEqual(self._set_actions()[-1]["value"], 2.0500002, places=5)

    def _unconfirmed(self) -> CommitResult:
        """What `expect_change=False` returns when one bounce found nothing to flush."""
        return CommitResult(committed=False, changed=False, confirmed=False)

    def _assert_unflushed_refusal(self, act) -> None:
        self._nudge_uncommitted()
        sidecar_before = self.sidecar.read_text()
        sent = len(self.bridge.calls)
        with self.assertRaises(ValueUnknown):
            act()
        # oracle 1: no darkroom action followed; oracle 2: the sidecar is untouched
        self.assertNotIn("darkroom.action", [m for m, _ in self.bridge.calls[sent:]])
        self.assertEqual(self.sidecar.read_text(), sidecar_before)

    def test_adjust_with_an_unconfirmed_bounce_still_refuses(self) -> None:
        with mock.patch("scouter.core.darkroom._commit", return_value=self._unconfirmed()):
            self._assert_unflushed_refusal(
                lambda: moves.adjust(
                    module="exposure", param="exposure", delta=0.5, commit=False, bounce=True
                )
            )

    def test_adjust_by_default_with_an_unconfirmed_bounce_still_refuses(self) -> None:
        with mock.patch("scouter.core.darkroom._commit", return_value=self._unconfirmed()):
            self._assert_unflushed_refusal(
                lambda: moves.adjust(module="exposure", param="exposure", delta=0.5, commit=False)
            )

    def test_adjust_with_a_timed_out_bounce_still_refuses(self) -> None:
        with mock.patch("scouter.core.darkroom._commit", side_effect=self._timeout()):
            self._assert_unflushed_refusal(
                lambda: moves.adjust(
                    module="exposure", param="exposure", delta=0.5, commit=False, bounce=True
                )
            )

    def test_get_with_an_unconfirmed_bounce_does_not_report_the_stale_value(self) -> None:
        self._nudge_uncommitted()
        with mock.patch("scouter.core.darkroom._commit", return_value=self._unconfirmed()):
            result = moves.get(module="exposure", param="exposure", bounce=True)
        self.assertIsNone(result.value)
        self.assertAlmostEqual(
            darkroom._read_value(str(self.raw), "exposure", "exposure", 0), 1.5500002, places=5
        )

    def test_say_with_an_unconfirmed_bounce_refuses_and_a_confirmed_one_moves(self) -> None:
        with mock.patch("scouter.core.darkroom._commit", return_value=self._unconfirmed()):
            self._assert_unflushed_refusal(lambda: moves.say("brighter", commit=False, bounce=True))
        confirmed = CommitResult(committed=True, changed=True, confirmed=True)
        with mock.patch("scouter.core.darkroom._commit", return_value=confirmed):
            result = moves.say("brighter", commit=False, bounce=True)
        self.assertAlmostEqual(result.moves[0]["previous"], 1.5500002, places=5)
        self.assertEqual(len(self._set_actions()), 1)

    def test_before_fallback_names_the_unknown_value_as_the_reason(self) -> None:
        self._nudge_uncommitted()
        resolved = darkroom._current_image()
        with self.assertRaises(ValueUnknown) as caught:
            moves.before_fallback(resolved, vocab.spec("exposure", "exposure"), 0, JOB)
        self.assertIn("uncommitted nudge", caught.exception.detail["reason"])
        self.assertNotIn("did not decode", caught.exception.detail["reason"])

    def test_a_journal_entry_written_before_the_fix_is_still_unknown(self) -> None:
        resolved = darkroom._current_image()
        old = journal.begin(
            JOB,
            verb="darkroom.nudge",
            image_id=12,
            path=resolved.path,
            module="exposure",
            param="exposure",
            instance=0,
            before={"value": 1.5500002},
            commit=False,
        )
        journal.complete(
            JOB, old.seq, after={"steps": 1, "steps_landed": 1, "value": SLIDER_POSITION}
        )
        darkroom.set_param(module="exposure", param="exposure", value=2.0, commit=False)
        set_entry = journal.read_all(JOB)[-1]
        self.assertIsNone(set_entry.before.get("value"))
        with self.assertRaises(ScouterError):
            darkroom.undo_last(single=True)
        self.assertEqual([p["value"] for p in self._set_actions()], [2.0])


if __name__ == "__main__":
    unittest.main()
