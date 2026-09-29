"""The run directory (spec 4.4): atomic per-case results, resume, cases.jsonl."""

from __future__ import annotations

import fnmatch
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
        case's workdir except files matching a `keep` glob pattern (by
        basename, matching every depth), and any directory left empty by
        that. `case_workdir` already refuses an unsafe `case_id`; this also
        asserts the resolved target is really one child of `<run>/work`
        before touching anything (fix round 1, I1: defense in depth against
        a future `case_workdir` change). Walks with `os.walk(followlinks=
        False)`, never `Path.rglob` (which follows symlinked directories),
        so a symlink is always deleted as itself -- never resolved, never
        recursed into (fix round 1, I2) -- and a path that vanishes under us
        (a race with the case's own still-finishing writes) is skipped, not
        fatal. Returns bytes freed."""
        d = self.case_workdir(case_id)
        if not d.exists():
            return 0
        work_root = (self.root / "work").resolve()
        assert d.resolve().parent == work_root, f"retain refuses a workdir outside {work_root}: {d}"  # noqa: E501
        freed = 0
        for dirpath, dirnames, filenames in os.walk(d, topdown=False, followlinks=False):
            base = Path(dirpath)
            for name in filenames:
                if any(fnmatch.fnmatch(name, pattern) for pattern in keep):
                    continue
                try:
                    size = (base / name).lstat().st_size
                    (base / name).unlink()
                    freed += size
                except FileNotFoundError:
                    pass
            for name in dirnames:
                p = base / name
                try:
                    if p.is_symlink():
                        p.unlink()
                    elif not any(p.iterdir()):
                        p.rmdir()
                except (FileNotFoundError, OSError):
                    pass
        return freed

    def du(self) -> dict[str, int]:
        """Bytes on disk per layer (`gauntlet du`): each case's workdir, its
        verdict json and its previews -- both the `<id>.*` file shape and
        Scouter's own real `previews/<id>/before.png`+`after.png` directory
        shape (fix round 1, I4) -- summed by `case.layer`. A run with no
        cases.jsonl yet reports {}."""
        if not self.cases_jsonl.exists():
            return {}
        totals: dict[str, int] = {}
        for c in self.read_cases():
            n = (_dir_size(self.case_workdir(c.id)) + _dir_size(self.case_path(c.id)) +
                 _dir_size(self.previews_dir / c.id) +
                 sum(p.stat().st_size for p in self.previews_dir.glob(f"{c.id}.*")))
            if n:
                totals[c.layer] = totals.get(c.layer, 0) + n
        return totals


def case_workdir(run_root: Path, case_id: str) -> Path:
    """The per-case scratch dir under a run root, as a free function so code
    that only has the run's root (a product adapter's own session code) can
    compute the same location `RunDir.case_workdir` does. Refuses (fix
    round 1, I1) a `case_id` that could ever point `retain()` outside
    `<run_root>/work`: empty, `.`/`..`, or carrying a path separator."""
    if not case_id or case_id in (".", "..") or "/" in case_id or os.sep in case_id:
        raise ValueError(f"unsafe case id for a workdir: {case_id!r}")
    return Path(run_root) / "work" / case_id


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
