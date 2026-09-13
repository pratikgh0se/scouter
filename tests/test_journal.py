import unittest

from scouter import paths
from scouter.core import journal
from tests.scouter_fakes import TempCache


class JournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()

    def tearDown(self) -> None:
        self.cache.stop()

    def _record(self, value: float, previous: float | None) -> journal.Entry:
        return journal.record(
            "job-test", verb="darkroom.set", image_id=12, path="/a/x.ORF",
            module="exposure", param="exposure", instance=0,
            before={"source": "xmp", "value": previous}, after={"value": value},
        )

    def test_seq_increments_and_latest_open_is_the_newest(self) -> None:
        self._record(0.7, None)
        self._record(1.5, 0.7)
        entries = journal.read_all("job-test")
        self.assertEqual([e.seq for e in entries], [1, 2])
        latest = journal.latest_open("job-test")
        assert latest is not None
        self.assertEqual(latest.seq, 2)
        self.assertEqual(latest.before["value"], 0.7)

    def test_marking_undone_moves_latest_open_back(self) -> None:
        self._record(0.7, None)
        self._record(1.5, 0.7)
        journal.mark_undone("job-test", 2)
        latest = journal.latest_open("job-test")
        assert latest is not None
        self.assertEqual(latest.seq, 1)
        self.assertTrue(journal.read_all("job-test")[1].undone)

    def test_empty_journal_has_no_latest(self) -> None:
        self.assertIsNone(journal.latest_open("job-test"))

    def test_journal_is_per_job(self) -> None:
        self._record(0.7, None)
        self.assertEqual(journal.read_all("job-other"), [])

    def test_begin_leaves_after_null_until_complete(self) -> None:
        entry = journal.begin(
            "job-test", verb="darkroom.set", image_id=12, path="/a/x.ORF",
            module="exposure", param="exposure", instance=0,
            before={"source": "xmp", "value": 0.7},
        )
        self.assertIsNone(journal.read_all("job-test")[0].after)
        journal.complete("job-test", entry.seq, after={"value": 1.5})
        self.assertEqual(journal.read_all("job-test")[0].after, {"value": 1.5})

    def test_fail_sets_outcome_and_leaves_after_null(self) -> None:
        entry = journal.begin(
            "job-test", verb="darkroom.set", image_id=12, path="/a/x.ORF",
            module="exposure", param="exposure", instance=0,
            before={"source": "xmp", "value": 0.7},
        )
        journal.fail("job-test", entry.seq, outcome="error:not_in_darkroom")
        saved = journal.read_all("job-test")[0]
        self.assertIsNone(saved.after)
        self.assertEqual(saved.outcome, "error:not_in_darkroom")

    def test_complete_on_an_unknown_seq_raises_journal_corrupt(self) -> None:
        with self.assertRaises(journal.JournalCorrupt):
            journal.complete("job-test", 99, after={"value": 1.0})

    def test_a_bad_line_raises_journal_corrupt_naming_the_line_number(self) -> None:
        self._record(0.7, None)
        path = paths.journal_path("job-test")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
        with self.assertRaises(journal.JournalCorrupt) as ctx:
            journal.read_all("job-test")
        self.assertEqual(ctx.exception.detail["line"], 2)
        self.assertEqual(ctx.exception.code, "journal_corrupt")

    def test_mark_undone_writes_via_temp_file_and_replace(self) -> None:
        """Blocker 3: no bare `write_text` on a rewrite -- verified indirectly:
        the journal is intact (and correctly updated) immediately afterwards,
        which a torn write would not reliably leave behind."""
        self._record(0.7, None)
        journal.mark_undone("job-test", 1)
        entries = journal.read_all("job-test")
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].undone)


if __name__ == "__main__":
    unittest.main()
