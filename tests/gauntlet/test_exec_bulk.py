
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


# -- final fix M1: the kill timer is stopped at case end -------------------------

def test_kill_after_never_signals_once_stopped(tmp_path, monkeypatch):
    """A stopped killer returns without signalling even when a later case's
    journal reaches its frame count."""
    import os
    import threading

    from scouter.core import dtprocess

    def forbidden(*a, **k):
        raise AssertionError("signalled after the case ended")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(dtprocess, "owned_pid", lambda: 999)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "events.jsonl").write_text('{"frame": 1}\n')  # a later case's journal
    session = SimpleNamespace(staged=SimpleNamespace(env={"SCOUTER_CACHE": str(cache)}))
    stop = threading.Event()
    stop.set()
    exec_bulk._kill_after(session, 1, poll_s=0.01, cap_s=0.5, stop=stop)


def test_run_bulk_case_stops_its_killer_in_finally(tmp_path, monkeypatch):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)
    stops = []

    def fake_killer(session, frame_n, poll_s=1.0, cap_s=1800, stop=None):
        stops.append(stop)

    monkeypatch.setattr(exec_bulk, "_kill_after", fake_killer)
    session = SimpleNamespace(
        root=tmp_path / "run", corpus=corpus,
        staged=SimpleNamespace(vars={"out_dir": str(tmp_path / "run" / "exports")},
                               env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")},
                               job_frames=[]),
        call=lambda tool, args, budget_s, write=False: CallRecord(tool, args, {"ok": True}))
    case = Case(id="blk-k", layer="bulk", tier="P1", mode="tool",
                steps=[Step("job_run", {}, 8.0, True)], expect=Expect("workflow"),
                body="olympus", frame_role="card", meta={"frames": 2, "kill_after_frame": 5})
    exec_bulk.run_bulk_case(session, case)
    assert stops and stops[0] is not None and stops[0].is_set()
