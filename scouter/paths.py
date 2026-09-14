"""Every path Scouter writes to. One place, so tests can redirect them all."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
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


#: A style name becomes `<cache>/styles/<name>.dtstyle` (fix round 1, blocker
#: 1): reuses JOB_RE (same "it becomes a file name" property as a job id) so
#: `../../x` or `/etc/x` can never escape `styles_dir()`.
STYLE_NAME_RE = JOB_RE


def validate_style_name(name: str) -> str:
    """Return `name` unchanged, or raise `usage` if it is not a safe file name."""
    if not isinstance(name, str) or not STYLE_NAME_RE.match(name):
        raise UsageError(
            f"invalid style name {name!r}: a style name is 1-64 characters of "
            "[A-Za-z0-9._-] starting with a letter or a digit (it becomes a "
            "file name under the styles cache)",
            detail={"name": name, "pattern": STYLE_NAME_RE.pattern},
        )
    return name


#: A job slug becomes a DIRECTORY NAME inside the library and a vault note name,
#: so it is validated like a job id: lowercase, no separators, no leading dot.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise UsageError(
            f"invalid job slug {slug!r}: 1-48 characters of [a-z0-9-] starting with a "
            "letter or a digit (it becomes a directory name and a vault note name)",
            detail={"slug": slug, "pattern": SLUG_RE.pattern},
        )
    return slug


def config_dir() -> Path:
    """`$SCOUTER_CONFIG`'s parent, else `$XDG_CONFIG_HOME/scouter`, else `~/.config/scouter`."""
    explicit = os.environ.get("SCOUTER_CONFIG")
    if explicit:
        return Path(explicit).parent
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "scouter"


def config_path() -> Path:
    explicit = os.environ.get("SCOUTER_CONFIG")
    return Path(explicit) if explicit else config_dir() / "config.json"


def wb_dir() -> Path:
    return config_dir() / "wb"


def styles_dir() -> Path:
    return cache_root() / "styles"


def stats_dir() -> Path:
    return cache_root() / "stats"


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
    """Per-job state Scouter owns: which image this job last opened (ruling 30),
    and (task 12) the per-image sidecar watermarks `history.py` records."""
    return cache_root() / "state" / f"{validate_job(job)}.json"


def _job_state_lock_path(job: str) -> Path:
    return Path(str(job_state_path(job)) + ".lock")


@contextlib.contextmanager
def _locked_job_state(job: str) -> Iterator[None]:
    """Hold an exclusive advisory lock for the whole read-modify-write critical
    section on this job's state file (fix round 1, should-fix 5).

    `darkroom.py` (`opened_image_id`/`opened_path`, ruling 30) and
    `history.py` (`watermarks`, task 12) both read-modify-write the SAME
    file. Without a lock spanning the whole read+merge+write, two writers
    racing -- e.g. `darkroom.open` recording the newly opened image while
    `history.commit` stamps a watermark for the write that just landed --
    can interleave so one write's read predates the other's write, and the
    later `os.replace` silently discards it. Losing `opened_image_id` that
    way would silently disable ruling 30's guard, which is the failure mode
    this exists to close off.

    `fcntl.flock` on a sibling `.lock` file (POSIX only, matching this
    codebase's macOS/Linux target -- darktable itself does not run on
    Windows): the lock file's own contents are never read, only its
    existence and the OS-level lock on its file descriptor.
    """
    lock_path = ensure(_job_state_lock_path(job))
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _read_job_state_unlocked(job: str) -> dict:
    path = job_state_path(job)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_job_state_unlocked(job: str, state: dict) -> None:
    """Replace this job's state file atomically. Best-effort by design."""
    path = ensure(job_state_path(job))
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def job_state_read(job: str) -> dict:
    """This job's small on-disk state dict, or `{}` when there is none yet.

    Lock-guarded (the same lock `job_state_update` holds for its own
    read-modify-write) so a read never lands between another writer's read
    and its write -- a torn-looking read is never possible, only "before"
    or "after" a given update.
    """
    with _locked_job_state(job):
        return _read_job_state_unlocked(job)


def job_state_update(job: str, mutate: Callable[[dict], None]) -> dict:
    """Read-modify-write this job's state file under one exclusive lock.

    The ONE seam both `darkroom.py` (`opened_image_id`/`opened_path`) and
    `history.py` (`watermarks`) go through (fix round 1, should-fix 5) --
    replacing each module's own private, unlocked `_job_state_read`/`_write`
    pair, which could lose either side's update under concurrent writers.
    `mutate` receives the CURRENT on-disk dict (read while holding the lock)
    and edits it in place; the result is written back before the lock
    releases, so no other writer's update is ever silently discarded.
    """
    with _locked_job_state(job):
        state = _read_job_state_unlocked(job)
        mutate(state)
        _write_job_state_unlocked(job, state)
        return state


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
