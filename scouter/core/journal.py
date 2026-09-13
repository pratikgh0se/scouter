"""The undo journal: what Scouter changed, and what it was before.

Design 8.2: `undo_last` re-issues the inverse ABSOLUTE set, because the only
write primitive phase 0 proved is `dt.gui.action(..., "set", value)`. Writing
darktable's history_end or rewriting the XMP under a live GUI is forbidden by
the system spec, so the previous value has to come from somewhere we own. This
is that somewhere.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from scouter import paths


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
    after: dict = field(default_factory=dict)
    undone: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def read_all(job: str) -> list[Entry]:
    path = paths.journal_path(job)
    if not path.exists():
        return []
    out: list[Entry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(Entry(**json.loads(line)))
    return out


def append(job: str, entry: Entry) -> Entry:
    with paths.ensure(paths.journal_path(job)).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
    return entry


def next_seq(job: str) -> int:
    entries = read_all(job)
    return (entries[-1].seq + 1) if entries else 1


def record(job: str, *, verb: str, image_id: int, path: str, module: str, param: str,
           instance: int, before: dict, after: dict) -> Entry:
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
            after=after,
        ),
    )


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
    path = paths.ensure(paths.journal_path(job))
    path.write_text(
        "".join(json.dumps(e.to_dict(), sort_keys=True) + "\n" for e in entries),
        encoding="utf-8",
    )
