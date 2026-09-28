
from pathlib import Path
from types import SimpleNamespace

import pytest

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
                vars={"out_dir": str(tmp_path / "run" / "exports"),
                      "relocate_root": str(tmp_path / "run" / "relocated")},
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


# -- Batch 3 / night-2 triage C2 + C12 (bulk hero-open half, row 462) -------

def _bulk_session(tmp_path, corpus, answers):
    """A FakeSession whose `call` answers in order (repeating the last
    answer once exhausted) and counts relaunches -- the two independent
    oracles the tests below check: the call log (oracle A) and the relaunch
    count (oracle B)."""
    calls = []
    relaunches = []

    class FakeSession:
        def __init__(self) -> None:
            self.root = tmp_path / "run"
            self.corpus = corpus
            self.staged = SimpleNamespace(
                vars={"out_dir": str(tmp_path / "run" / "exports"),
                      "relocate_root": str(tmp_path / "run" / "relocated")},
                env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")},
                job_frames=[],
            )

        def call(self, tool, args, budget_s, write=False):
            calls.append(tool)
            ans = answers[len(calls) - 1] if len(calls) <= len(answers) else answers[-1]
            return CallRecord(tool=tool, args=args, envelope=ans.get("envelope", {"ok": True}),
                              error=ans.get("error"), write=write)

        def relaunch(self) -> None:
            relaunches.append(True)

    return FakeSession(), calls, relaunches


def _bulk_case():
    return Case(id="blk-wedge-test", layer="bulk", tier="P1", mode="tool",
               steps=[Step("job_run", {}, 8.0, True)], expect=Expect("workflow"),
               body="olympus", frame_role="card", meta={"frames": 2})


@pytest.mark.parametrize("code", ["bridge_timeout", "commit_timeout"])
def test_bulk_bridge_timeout_relaunches_once_then_recovers(tmp_path, code):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)
    answers = [{"error": code}, {"envelope": {"ok": True, "data": {}}}]
    session, calls, relaunches = _bulk_session(tmp_path, corpus, answers)

    obs = exec_bulk.run_bulk_case(session, _bulk_case())

    assert obs.blocked is None  # oracle A: the case observation carries no block
    assert calls == ["job_run", "job_run"] and len(relaunches) == 1  # oracle B: one relaunch


@pytest.mark.parametrize("code", ["bridge_timeout", "commit_timeout"])
def test_bulk_bridge_timeout_relaunches_at_most_once_then_marks_failed(tmp_path, code):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)
    answers = [{"error": code}, {"error": code}]
    session, calls, relaunches = _bulk_session(tmp_path, corpus, answers)

    obs = exec_bulk.run_bulk_case(session, _bulk_case())

    assert obs.blocked is not None and obs.blocked.startswith("bridge_relaunch_failed")
    assert calls == ["job_run", "job_run"] and len(relaunches) == 1  # bounded: never a second


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
        staged=SimpleNamespace(vars={"out_dir": str(tmp_path / "run" / "exports"),
                                     "relocate_root": str(tmp_path / "run" / "relocated")},
                               env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")},
                               job_frames=[]),
        call=lambda tool, args, budget_s, write=False: CallRecord(tool, args, {"ok": True}))
    case = Case(id="blk-k", layer="bulk", tier="P1", mode="tool",
                steps=[Step("job_run", {}, 8.0, True)], expect=Expect("workflow"),
                body="olympus", frame_role="card", meta={"frames": 2, "kill_after_frame": 5})
    exec_bulk.run_bulk_case(session, case)
    assert stops and stops[0] is not None and stops[0].is_set()


# -- n1-bulk fix (a): valid job slugs ----------------------------------------

def test_job_slug_for_real_recorded_case_ids():
    """RED: the old `f"htc-{case.id}"[-40:]` fails `validate_slug` for a real
    recorded id (it keeps the "_" and slices from the wrong end). GREEN: the
    new builder returns something `scouter.paths.validate_slug` accepts."""
    from scouter import paths as scouter_paths

    real_ids = [
        "blk-batch_export_profiles-olympus-card",
        "blk-copy_paste_history-sony_a7-job",
        "blk-crash_mid_job-style_apply_set",
        "blk-time_lapse_deflicker-olympus-card",
    ]
    old_broken = [f"htc-{cid}"[-40:] for cid in real_ids]
    assert not all(scouter_paths.SLUG_RE.match(s) for s in old_broken), \
        "the old formula was expected to still be broken for these ids"

    for cid in real_ids:
        slug = exec_bulk.job_slug_for(cid)
        assert scouter_paths.validate_slug(slug) == slug


# -- n1-bulk fix (b): frame refs resolve to the imported destination --------

def _write_config(path, library_root):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "library_root": str(library_root)}))


def test_run_bulk_case_points_later_steps_at_the_imported_files(tmp_path):
    """RED (before the fix): `session.staged.job_frames` stays the card
    copies for the whole case, so `library_verify`/`style_apply`/etc address
    `card/DCIM/100GAUNT/DUP0000_...` -- the product answers "not in
    darktable's library" / `not_in_darkroom`. GREEN: once `library_import`
    reports its destinations, later steps' `{job_frame:N}` resolves to the
    library path it actually wrote."""
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)

    jobs_root = tmp_path / "run" / "jobs"
    config_path = tmp_path / "run" / "config" / "config.json"
    _write_config(config_path, jobs_root)
    job_slug = "htc-blk-styles-olympus-card"
    date = "2026-05-24"
    dest_names = [f"{date}_{job_slug}_em10iii_000{i + 1}.orf" for i in range(2)]

    seen_args = []

    class FakeSession:
        def __init__(self) -> None:
            self.root = tmp_path / "run"
            self.corpus = corpus
            self.staged = SimpleNamespace(
                vars={"out_dir": str(tmp_path / "run" / "exports"),
                      "relocate_root": str(tmp_path / "run" / "relocated")},
                env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")},
                job_frames=[], config=config_path,
            )

        def call(self, tool, args, budget_s, write=False):
            seen_args.append((tool, args))
            if tool == "library_import":
                files = [{"source_name": f"DUP000{i}_H.ORF", "dest": f"raw/{name}",
                          "sha256": "x", "seq": i + 1}
                         for i, name in enumerate(dest_names)]
                return CallRecord(tool=tool, args=args,
                                  envelope={"ok": True, "data": {
                                      "job_slug": job_slug, "date": date,
                                      "files": files, "imported": len(files)}},
                                  write=True)
            return CallRecord(tool=tool, args=args, envelope={"ok": True, "data": {}},
                              write=write)

    session = FakeSession()
    case = Case(id="blk-styles-olympus-card", layer="bulk", tier="P1", mode="tool",
                steps=[Step("library_import", {"source": "{card}", "job_slug": "{job_slug}",
                                               "confirm": True}, 30.0, True),
                       Step("darkroom_open", {"image": "{job_frame:1}"}, 30.0, False)],
                expect=Expect("workflow"), body="olympus", frame_role="card",
                meta={"frames": 2})

    exec_bulk.run_bulk_case(session, case)

    darkroom_call = next(a for t, a in seen_args if t == "darkroom_open")
    expected = jobs_root / "2026" / f"{date} {job_slug}" / "raw" / dest_names[0]
    assert darkroom_call["image"] == str(expected)
    assert "card" not in Path(darkroom_call["image"]).parts


# -- partial import: blocked, never a silent card-path fallback -------------

def test_run_bulk_case_blocks_on_a_partial_import(tmp_path):
    """RED (before this follow-up): `_imported_frames` filled in a missing
    frame's destination with its card path, so a partial import silently
    downgraded into the SAME false `usage`/`not_in_darkroom` shape a
    genuine harness bug produces -- indistinguishable from a real product
    failure. GREEN: a partial import (here, `library_import`'s own
    `files[]` names only one of the two staged frames) blocks the case
    with a typed `import_incomplete` harness error naming the missing
    source, and no further step runs."""
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)

    jobs_root = tmp_path / "run" / "jobs"
    config_path = tmp_path / "run" / "config" / "config.json"
    _write_config(config_path, jobs_root)
    job_slug = "htc-blk-styles-olympus-card"
    date = "2026-05-24"

    seen_tools = []

    class FakeSession:
        def __init__(self) -> None:
            self.root = tmp_path / "run"
            self.corpus = corpus
            self.staged = SimpleNamespace(
                vars={"out_dir": str(tmp_path / "run" / "exports"),
                      "relocate_root": str(tmp_path / "run" / "relocated")},
                env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")},
                job_frames=[], config=config_path,
            )

        def call(self, tool, args, budget_s, write=False):
            seen_tools.append(tool)
            if tool == "library_import":
                # Only DUP0000 made it into the import's own manifest --
                # DUP0001 (the card's second staged frame) is unaccounted
                # for, a partial import.
                files = [{"source_name": "DUP0000_H.ORF",
                          "dest": f"raw/{date}_{job_slug}_em10iii_0001.orf",
                          "sha256": "x", "seq": 1}]
                return CallRecord(tool=tool, args=args,
                                  envelope={"ok": True, "data": {
                                      "job_slug": job_slug, "date": date,
                                      "files": files, "imported": len(files)}},
                                  write=True)
            return CallRecord(tool=tool, args=args, envelope={"ok": True, "data": {}},
                              write=write)

    session = FakeSession()
    case = Case(id="blk-styles-olympus-card", layer="bulk", tier="P1", mode="tool",
                steps=[Step("library_import", {"source": "{card}", "job_slug": "{job_slug}",
                                               "confirm": True}, 30.0, True),
                       Step("darkroom_open", {"image": "{job_frame:1}"}, 30.0, False)],
                expect=Expect("workflow"), body="olympus", frame_role="card",
                meta={"frames": 2})

    obs = exec_bulk.run_bulk_case(session, case)

    assert obs.blocked is not None
    assert "import_incomplete" in obs.blocked
    assert "DUP0001_H.ORF" in obs.blocked
    assert obs.hang_evidence and "DUP0001_H.ORF" in obs.hang_evidence[0]
    # The case stopped at the import step: darkroom_open never ran.
    assert "darkroom_open" not in seen_tools


def test_imported_frames_raises_import_incomplete_naming_the_missing_source(tmp_path):
    """`_imported_frames`/`ImportIncomplete` live in `tests.gauntlet.execute`
    (shared with `run_tool_case`), not re-exported here."""
    from tests.gauntlet import execute

    envelope = {"ok": True, "data": {
        "job_slug": "htc-x", "date": "2026-05-24",
        "files": [{"source_name": "DUP0000_H.ORF", "dest": "raw/a.orf"}]}}
    session = SimpleNamespace(staged=SimpleNamespace(config=tmp_path / "config.json"))
    with pytest.raises(execute.ImportIncomplete) as exc_info:
        execute._imported_frames(session, envelope,
                                 [Path("card/DUP0000_H.ORF"), Path("card/DUP0001_H.ORF")])
    assert "import_incomplete" in str(exc_info.value)
    assert "DUP0001_H.ORF" in str(exc_info.value)


# -- n1-bulk fix (c): the relocate root exists before the verb runs ---------

def test_run_bulk_case_creates_the_relocate_root(tmp_path):
    """RED: `library_relocate` refuses `new_root` unless it already exists as
    a directory; nothing under the harness ever created it. GREEN: the
    directory is there before any step runs."""
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)
    relocate_root = tmp_path / "run" / "relocated"

    session = SimpleNamespace(
        root=tmp_path / "run", corpus=corpus,
        staged=SimpleNamespace(
            vars={"out_dir": str(tmp_path / "run" / "exports"),
                  "relocate_root": str(relocate_root)},
            env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")}, job_frames=[]),
        call=lambda tool, args, budget_s, write=False: CallRecord(tool, args, {"ok": True}))
    case = Case(id="blk-sidecar-handling-olympus-card", layer="bulk", tier="P1", mode="tool",
                steps=[Step("library_relocate", {"new_root": "{relocate_root}"}, 30.0, True)],
                expect=Expect("workflow"), body="olympus", frame_role="card",
                meta={"frames": 1})

    assert not relocate_root.exists()
    exec_bulk.run_bulk_case(session, case)
    assert relocate_root.is_dir()


# -- n1-bulk fix (e): the false "silent" verdict ------------------------------

def test_reported_n_excludes_librarys_own_import_count(tmp_path):
    """RED: `reported_n` took the max over EVERY write envelope, so an
    otherwise-failed case where only `library_import` succeeded (7 frames
    imported) still reported 7 -- scored `silent_failure` instead of the
    genuine failure it is. GREEN: `library_import`'s own count is excluded
    from `obs.extra["reported_n"]` (it has `imported_n` for that)."""
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    corpus = Corpus({"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {}}, trial)

    calls = iter([
        CallRecord(tool="library_import", args={}, write=True,
                  envelope={"ok": True, "data": {"imported": 7, "files": []}}),
        CallRecord(tool="darkroom_open", args={}, write=False,
                  envelope={"ok": False}, error="not_in_darkroom"),
        CallRecord(tool="style_apply", args={}, write=True,
                  envelope=None, error="usage"),
    ])
    session = SimpleNamespace(
        root=tmp_path / "run", corpus=corpus,
        staged=SimpleNamespace(
            vars={"out_dir": str(tmp_path / "run" / "exports"),
                  "relocate_root": str(tmp_path / "run" / "relocated")},
            env={"SCOUTER_CACHE": str(tmp_path / "run" / "cache")}, job_frames=[]),
        call=lambda tool, args, budget_s, write=False: next(calls))
    case = Case(id="blk-styles-olympus-card", layer="bulk", tier="P1", mode="tool",
                steps=[Step("library_import", {}, 30.0, True),
                       Step("darkroom_open", {}, 30.0, False),
                       Step("style_apply", {}, 30.0, True)],
                expect=Expect("workflow"), body="olympus", frame_role="card",
                meta={"frames": 7})

    obs = exec_bulk.run_bulk_case(session, case)

    assert obs.extra["imported_n"] == 7
    assert obs.extra["reported_n"] is None
