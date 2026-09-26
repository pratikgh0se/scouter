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
        self.cases_jsonl = self.root / "cases.jsonl"
        self.generation_md = self.root / "generation.md"
        self.log_md = self.root / "log.md"
        self.progress_json = self.root / "progress.json"
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
