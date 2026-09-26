
from types import SimpleNamespace

from gauntlet.adapter import CallRecord
from gauntlet.case import Case, Expect, Step
from tests.gauntlet import exec_bulk
from tests.gauntlet.corpus import Corpus
from tests.gauntlet.session import SessionBlocked


def test_reported_n_reads_lists_and_counts():
    assert exec_bulk.reported_n([{"data": {"applied": [1, 2, 3]}}, {"data": {"done": 7}}]) == 7
    assert exec_bulk.reported_n([{"data": {}}]) is None


def test_relaunch_session_blocked_is_returned_as_observation(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)

    class FakeSession:
        def __init__(self) -> None:
            self.root = tmp_path / "run"
            self.corpus = corpus
            self.staged = SimpleNamespace(
                vars={"out_dir": str(tmp_path / "run" / "exports")},
                env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")},
                job_frames=[],
            )

        def call(self, tool, args, budget_s, write=False):
            return CallRecord(tool=tool, args=args, envelope={"ok": False}, error="timeout",
                              write=write)

        def relaunch(self) -> None:
            raise SessionBlocked("darktable did not quit")

    session = FakeSession()
    case = Case(id="blk-crash-test", layer="bulk", tier="P1", mode="tool",
                steps=[Step("job_run", {}, 8.0, True)],
                expect=Expect("workflow"), body="olympus", frame_role="card",
                meta={"frames": 2, "kill_after_frame": 50})

    obs = exec_bulk.run_bulk_case(session, case)

    assert obs.blocked == "session: darktable did not quit"
    assert "session: darktable did not quit" in obs.hang_evidence
    assert not (session.root / "bulk" / case.id / "card").exists()
