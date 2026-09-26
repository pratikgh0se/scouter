
from tests.gauntlet import exec_bulk


def test_reported_n_reads_lists_and_counts():
    assert exec_bulk.reported_n([{"data": {"applied": [1, 2, 3]}}, {"data": {"done": 7}}]) == 7
    assert exec_bulk.reported_n([{"data": {}}]) is None
