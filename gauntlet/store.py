"""The run directory (spec 4.4): atomic per-case results, resume, cases.jsonl."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.case import Case, dumps, from_jsonl, to_jsonl


def new_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dt%H%M%Sz")


def atomic_write_text(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, fsync, then rename over `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class RunDir:
    """`<runs_root>/<stamp>/` and everything inside it."""

    def __init__(self, runs_root: Path, stamp: str) -> None:
        self.stamp = stamp
        self.root = Path(runs_root).expanduser() / stamp
        self.cases_dir = self.root / "cases"
        self.previews_dir = self.root / "previews"
        self.sessions_dir = self.root / "sessions"
        #: (t_90e75c20): scratch space a session may stage a case's heavy
        #: disk artifacts under (imported originals, library copies, full-size
        #: renders) -- `retain()` prunes this per case after scoring, and
        #: nothing else in the run dir (verdict json, log, previews) lives
        #: here, so retention can never reach them.
        self.work_dir = self.root / "work"
        self.cases_jsonl = self.root / "cases.jsonl"
        self.generation_md = self.root / "generation.md"
        self.log_md = self.root / "log.md"
        self.progress_json = self.root / "progress.json"
        #: (row 501 item H, fix round 1): a stratified generate --subset's
        #: own record ({"layer", "size", "seed", "keys"}), read once by
        #: Progress and folded into progress.json's own "subset" field.
        self.subset_json = self.root / "subset.json"
        self.report_md = self.root / "report.md"
        self.report_json = self.root / "report.json"
        self.contact_sheet = self.root / "contact-sheet.html"
        self.fixlist_json = self.root / "fixlist.json"
        self.fixlist_md = self.root / "fixlist.md"

    def ensure(self) -> RunDir:
        for d in (self.root, self.cases_dir, self.previews_dir, self.sessions_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def case_path(self, case_id: str) -> Path:
        return self.cases_dir / f"{case_id}.json"

    def write_cases(self, cases: list[Case]) -> None:
        atomic_write_text(self.cases_jsonl, to_jsonl(cases))

    def read_cases(self) -> list[Case]:
        return from_jsonl(self.cases_jsonl.read_text(encoding="utf-8"))

    def write_result(self, case_id: str, doc: dict) -> Path:
        path = self.case_path(case_id)
        atomic_write_text(path, dumps(doc) + "\n")
        return path

    def read_result(self, case_id: str) -> dict | None:
        path = self.case_path(case_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def done_ids(self) -> set[str]:
        if not self.cases_dir.exists():
            return set()
        return {p.stem for p in self.cases_dir.glob("*.json")}

    def read_results(self) -> list[dict]:
        if not self.cases_dir.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.cases_dir.glob("*.json"))]

    def case_workdir(self, case_id: str) -> Path:
        return case_workdir(self.root, case_id)

    def retain(self, case_id: str, keep: tuple[str, ...] = ()) -> int:
        """Per-case retention (t_90e75c20): delete everything under this
        case's workdir except files matching a `keep` glob pattern (relative
        to the workdir), and any directory left empty by that. Only ever
        touches paths under `case_workdir(case_id)` -- the verdict json, the
        log and the previews all live elsewhere in the run dir and this never
        walks above the workdir root. Returns bytes freed."""
        d = self.case_workdir(case_id)
        if not d.exists():
            return 0
        keep_paths = {p for pattern in keep for p in d.rglob(pattern)}
        freed = 0
        # Deepest paths first, so a directory is only removed once everything
        # under it (kept files included) has already been decided.
        for p in sorted(d.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if p in keep_paths:
                continue
            if p.is_dir():
                if not any(p.iterdir()):
                    p.rmdir()
            else:
                freed += p.stat().st_size
                p.unlink()
        return freed

    def du(self) -> dict[str, int]:
        """Bytes on disk per layer (`gauntlet du`): each case's workdir, its
        verdict json and its previews, summed by `case.layer`. A run with no
        cases.jsonl yet reports {}."""
        if not self.cases_jsonl.exists():
            return {}
        totals: dict[str, int] = {}
        for c in self.read_cases():
            n = _dir_size(self.case_workdir(c.id)) + _dir_size(self.case_path(c.id)) + \
                sum(p.stat().st_size for p in self.previews_dir.glob(f"{c.id}.*"))
            if n:
                totals[c.layer] = totals.get(c.layer, 0) + n
        return totals


def case_workdir(run_root: Path, case_id: str) -> Path:
    """(t_90e75c20 round 2): the per-case scratch dir under a run root, as a
    free function -- callers that only have the run's root path (a product
    adapter's own session code, never a full RunDir instance) can compute
    the same location `RunDir.case_workdir` does, with no RunDir import
    cycle and no duplicated `"work"` literal to drift out of sync."""
    return Path(run_root) / "work" / case_id


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
