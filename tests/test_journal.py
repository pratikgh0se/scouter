import unittest

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


if __name__ == "__main__":
    unittest.main()
