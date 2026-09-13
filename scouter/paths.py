"""Every path Scouter writes to. One place, so tests can redirect them all."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


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
    return cache_root() / "audit" / f"{job}.jsonl"


def journal_path(job: str) -> Path:
    return cache_root() / "journal" / f"{job}.jsonl"


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
        return explicit
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"job-{stamp}-{os.getpid()}"
