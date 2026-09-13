"""Every path Scouter writes to. One place, so tests can redirect them all."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from scouter.errors import UsageError

#: A job id becomes a FILENAME (`audit/<job>.jsonl`, `journal/<job>.jsonl`,
#: `state/<job>.json`), so it is validated the moment it enters -- otherwise
#: `--job ../../x` writes outside the cache root. One character class, one
#: length bound, no path separators and no leading dot.
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_job(job: str) -> str:
    """Return `job` unchanged, or raise `usage` if it is not a safe file name."""
    if not isinstance(job, str) or not JOB_RE.match(job):
        raise UsageError(
            f"invalid job id {job!r}: a job id is 1-64 characters of "
            "[A-Za-z0-9._-] starting with a letter or a digit (it becomes a "
            "file name under the cache root)",
            detail={"job": job, "pattern": JOB_RE.pattern},
        )
    return job


def cache_root() -> Path:
    """`$SCOUTER_CACHE`, else `$XDG_CACHE_HOME/scouter`, else `~/.cache/scouter`."""
    explicit = os.environ.get("SCOUTER_CACHE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "scouter"


def events_path() -> Path:
    return cache_root() / "events.jsonl"


def db_path() -> Path:
    return cache_root() / "metrics.db"


def audit_path(job: str) -> Path:
    return cache_root() / "audit" / f"{validate_job(job)}.jsonl"


def journal_path(job: str) -> Path:
    return cache_root() / "journal" / f"{validate_job(job)}.jsonl"


def job_state_path(job: str) -> Path:
    """Per-job state Scouter owns: which image this job last opened (ruling 30)."""
    return cache_root() / "state" / f"{validate_job(job)}.json"


def preview_dir() -> Path:
    return cache_root() / "previews"


def cli_configdir() -> Path:
    """Isolated darktable configdir for darktable-cli, so the GUI keeps its lock."""
    return cache_root() / "cli-config"


def ensure(path: Path) -> Path:
    """Create `path`'s parent directory and return `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def current_job() -> str:
    """The ambient job id: `$SCOUTER_JOB`, else one derived from the clock + pid."""
    explicit = os.environ.get("SCOUTER_JOB")
    if explicit:
        return validate_job(explicit)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"job-{stamp}-{os.getpid()}"
