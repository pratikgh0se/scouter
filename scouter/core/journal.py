"""The undo journal: what Scouter changed, and what it was before.

Design 8.2: `undo_last` re-issues the inverse ABSOLUTE set, because the only
write primitive phase 0 proved is `dt.gui.action(..., "set", value)`. Writing
darktable's history_end or rewriting the XMP under a live GUI is forbidden by
the system spec, so the previous value has to come from somewhere we own. This
is that somewhere.

Fix round 1 (blocker 4): a write verb appends its journal line *before* it
sends the bridge action, with `after=null` -- the entry exists the instant
Scouter commits to the write, not after it succeeds. `complete()` patches the
real `after` in once the action lands; `fail()` patches only `outcome` in when
it doesn't, leaving `after` null so a reader can tell "attempted, unknown
outcome recorded honestly" apart from "attempted and this is what happened."
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from scouter import paths
from scouter.errors import ScouterError


class JournalCorrupt(ScouterError):
    """A journal line failed to parse. Generic exit code 1 (fix round 1, blocker 12):

    this is a local, narrow condition -- not one of the fixed exit-code table's
    bridge/darktable failure classes -- so it gets its own `code` without a new
    row in `scouter.errors.EXIT_CODES`.
    """

    code = "journal_corrupt"
    fix = "inspect the journal file by hand; a line failed to parse as JSON"


@dataclass
class Entry:
    seq: int
    ts: str
    verb: str
    image_id: int
    path: str
    module: str
    param: str
    instance: int
    before: dict = field(default_factory=dict)
    after: dict | None = None
    undone: bool = False
    outcome: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def read_all(job: str) -> list[Entry]:
    path = paths.journal_path(job)
    if not path.exists():
        return []
    out: list[Entry] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise JournalCorrupt(
                f"journal for job {job!r} is corrupt at line {lineno}: {e}",
                detail={"job": job, "line": lineno},
            ) from e
        try:
            out.append(Entry(**data))
        except TypeError as e:
            raise JournalCorrupt(
                f"journal for job {job!r} is corrupt at line {lineno}: {e}",
                detail={"job": job, "line": lineno},
            ) from e
    return out


def _write_all(job: str, entries: list[Entry]) -> None:
    """Rewrite the whole journal via temp file + `os.replace` (blocker 3).

    A bare `write_text` can leave a torn file behind if the process dies
    mid-write; the journal is exactly the thing `undo_last` trusts, so it
    cannot itself be the thing left half-written.
    """
    path = paths.ensure(paths.journal_path(job))
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(e.to_dict(), sort_keys=True) + "\n" for e in entries))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def append(job: str, entry: Entry) -> Entry:
    with paths.ensure(paths.journal_path(job)).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
    return entry


def next_seq(job: str) -> int:
    entries = read_all(job)
    return (entries[-1].seq + 1) if entries else 1


def begin(job: str, *, verb: str, image_id: int, path: str, module: str, param: str,
         instance: int, before: dict) -> Entry:
    """Append the journal line for a write *before* the bridge action runs.

    `after` starts `None`: the entry says "Scouter is about to do this" and
    only becomes a record of what happened once `complete()` or `fail()` runs.
    """
    return append(
        job,
        Entry(
            seq=next_seq(job),
            ts=datetime.now(timezone.utc).isoformat(),
            verb=verb,
            image_id=image_id,
            path=path,
            module=module,
            param=param,
            instance=instance,
            before=before,
            after=None,
        ),
    )


def _find_or_raise(entries: list[Entry], job: str, seq: int) -> None:
    if not any(e.seq == seq for e in entries):
        raise JournalCorrupt(
            f"no journal entry seq {seq} in job {job!r} to patch",
            detail={"job": job, "seq": seq},
        )


def complete(job: str, seq: int, *, after: dict) -> None:
    """Patch the real `after` onto an entry once the write actually landed."""
    entries = read_all(job)
    _find_or_raise(entries, job, seq)
    for entry in entries:
        if entry.seq == seq:
            entry.after = after
    _write_all(job, entries)


def fail(job: str, seq: int, *, outcome: str) -> None:
    """Patch only `outcome` onto an entry whose action did not land.

    `after` stays `None` -- a failed write has no "what it became."
    """
    entries = read_all(job)
    _find_or_raise(entries, job, seq)
    for entry in entries:
        if entry.seq == seq:
            entry.outcome = outcome
    _write_all(job, entries)


def record(job: str, *, verb: str, image_id: int, path: str, module: str, param: str,
           instance: int, before: dict, after: dict) -> Entry:
    """Convenience: `begin` + `complete` in one call.

    For a caller with no intermediate state between "about to write" and
    "wrote" to track (tests, and any future one-shot writer). The write verbs
    in `darkroom.py` use `begin`/`complete`/`fail` directly instead, so a
    failure between the two is visible in the journal (blocker 4).
    """
    entry = begin(job, verb=verb, image_id=image_id, path=path, module=module, param=param,
                 instance=instance, before=before)
    complete(job, entry.seq, after=after)
    entry.after = after
    return entry


def latest_open(job: str) -> Entry | None:
    """The newest entry not yet undone."""
    for entry in reversed(read_all(job)):
        if not entry.undone:
            return entry
    return None


def mark_undone(job: str, seq: int) -> None:
    """Rewrite the journal with `seq` marked undone.

    The journal is small (one line per agent write in one job) and is Scouter's
    own file, so a full rewrite is cheaper than an index.
    """
    entries = read_all(job)
    for entry in entries:
        if entry.seq == seq:
            entry.undone = True
    _write_all(job, entries)
