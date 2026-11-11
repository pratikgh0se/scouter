"""`library.import`, `library.verify`, `library.register`: the irreversible step.

Everything downstream (cull, style, export) addresses frames by the names this
module chooses, so the algorithm follows design section 6 exactly, in order:
load the config and check the backup gate BEFORE the card is read; scan; plan
names; dry-run/confirm; create the layout; copy with two sha256 passes,
resume-safely, chmod-ing originals read-only; write the checksum manifest and
`import.json` atomically; register the film roll (tolerating darktable being
down); write the vault note (tolerating the vault not existing yet).

Resume identity is `(source_name, sha256)` together (ruling R17): the same
bytes under a different camera filename are a second frame, kept alongside
the first and reported in `duplicates`. The single exception (ruling R17b) is
an entry synthesised from `checksums.sha256` alone, whose `source_name` is a
placeholder (its own destination basename, `status: "recovered_from_checksums"`)
because the camera filename is genuinely unrecoverable -- for those entries,
and only those, the bytes are the whole identity, since otherwise a re-run
with the card still inserted would re-copy every frame of it under new names.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from scouter import __version__, config, metrics, paths
from scouter.core import dtbridge, exif, xmp
from scouter.errors import (
    BridgeTimeout,
    ChecksumMismatch,
    DestinationCollision,
    ExifUnreadable,
    ScouterError,
    UsageError,
)
from scouter.registry import ArgSpec, Verb
from scouter.registry import register as register_verb
from scouter.types import ImportResult, JobsResult, RegisterResult, RelocateResult, VerifyResult

#: Recursion bound for `_scan`. A card is a flat DCIM tree at most a handful of
#: directories deep; 8 is generous headroom without an unbounded walk.
SCAN_MAX_DEPTH = 8

#: Streaming chunk size for both hash passes and the copy itself.
CHUNK = 1 << 20

#: `.part` leftovers older than this are swept at the start of an import (fix
#: round 2, nit) -- anything younger might belong to a genuinely concurrent
#: import of a DIFFERENT job and must not be deleted out from under it.
STALE_PART_AGE_S = 3600


@dataclasses.dataclass
class _Plan:
    """What `_plan` resolved. A named record rather than the 7-tuple it used
    to return: fix round 4 needed to add three more fields to it, and a
    10-tuple unpacked at the call site is a defect waiting to happen."""

    date: str
    camera: str
    camera_model: str | None
    camera_make: str | None
    #: One entry per candidate file, carrying `Path` objects (never
    #: serialised directly -- the caller builds both the dry-run preview and
    #: `import.json`'s `files[]` from them).
    entries: list[dict]
    #: This job's prior manifest, recovered as far down
    #: `_recover_existing_files`'s chain as it had to go (`[]` on a genuine
    #: first import), returned so the caller need not re-read it to merge.
    existing_files: list[dict]
    #: The stems already in `existing_files` BEFORE this run added any of its
    #: own, so the copy loop can tell a stem reused across cards apart from a
    #: stem merely repeated inside this run's own plan.
    pre_existing_stems: set[str]
    warnings: list[str] = dataclasses.field(default_factory=list)
    #: The honest cost of the resume pre-check (fix round 4, item 3): how
    #: many candidate files `_plan` hashed, and how many bytes that read.
    hashed_files: int = 0
    hashed_bytes: int = 0
    #: D5, row 407 call 1: files `--camera` excluded because their own EXIF
    #: names a different body -- `{"source_name", "camera"}` per file, never
    #: silently dropped.
    skipped_other_camera: list[dict] = dataclasses.field(default_factory=list)
    #: Review round 1, ruling 2: a file with NO EXIF body at all, imported
    #: under an explicit `--camera X` anyway -- `{"source_name", "camera"}`
    #: per file, so the assumption is visible, never silent.
    assumed_camera: list[dict] = dataclasses.field(default_factory=list)
    #: Review round 1, ruling 2: a file with no EXIF body, skipped because
    #: there was no `--camera` to fall back on -- `{"source_name"}` per
    #: file. The card never fails over this one file.
    skipped_no_body: list[dict] = dataclasses.field(default_factory=list)


class ManifestCorrupt(UsageError):
    """`import.json` exists but is not valid JSON, or not shaped like a
    manifest Scouter itself would write (an object with a `files` list of
    complete entries). This must never be read as "no manifest yet, treat as
    a first import" -- that would silently orphan whatever the real manifest
    was protecting (fix round 2, item 1)."""

    fix = (
        "inspect (and fix or remove) the file named in `detail.path`; if it should be "
        "trusted, restore it from a backup before re-running the import"
    )


class SourceStemCollision(UsageError):
    """Fix round 2, item 4: a source file shares a stem with a frame this job
    already imported (typically a camera filename counter that reset on a
    second card), but the bytes differ. Identical content is an ordinary
    resume-skip (the copy loop's own hash comparison handles that); this is
    a genuine naming collision the plan cannot resolve on its own."""

    fix = (
        "rename the file on the card so its stem is unique for this job, or import "
        "it as a separate job"
    )


def _scan(source: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Recursive, depth-bounded, symlinks not followed. Sorted for determinism.

    `._`-prefixed entries are skipped: FAT-formatted cards routinely carry
    AppleDouble sidecar junk (`._P6281348.ORF`) written by macOS's Finder,
    which matches the wanted suffix just as well as the real file and is
    never something a camera wrote (fix round 1, nit)."""
    wanted = {("." + e.lower()) for e in extensions}
    found: list[Path] = []
    stack = [(source, 0)]
    while stack:
        directory, depth = stack.pop()
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink() or entry.name.startswith("._"):
                continue
            if entry.is_dir():
                if depth < SCAN_MAX_DEPTH:
                    stack.append((entry, depth + 1))
            elif entry.suffix.lower() in wanted:
                found.append(entry)
    return sorted(found)


def _hash_file(path: Path) -> str:
    """One sha256 pass over `path`. Used for the resume-safety comparison, and
    for `library.verify`'s re-hash -- never for the copy's own two passes,
    which hash inline as `_copy_verified` streams (kept as its own logic
    below, per the brief, so a corrupted re-read is caught by the exact code
    that ships)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(src: Path, dst: Path) -> tuple[str, int]:
    """Copy src -> dst, hashing the source as it streams, then re-hash the
    destination. Mismatch deletes the destination and raises: a bad card or a
    failing disk must not go on to produce 299 more files."""
    digest, size = hashlib.sha256(), 0
    tmp = dst.with_name(f".{dst.name}.part")
    with src.open("rb") as fin, tmp.open("wb") as fout:
        while chunk := fin.read(CHUNK):
            digest.update(chunk)
            fout.write(chunk)
            size += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    if dst.exists():
        # Cheap O_EXCL-equivalent (fix round 1, nit): the caller already
        # checked `dest.exists()` before calling this, so a destination that
        # exists NOW must have appeared during the copy -- a concurrent
        # import of the same job, not a resume. `os.replace` would silently
        # clobber it; refuse instead.
        tmp.unlink(missing_ok=True)
        raise DestinationCollision(
            f"{dst} appeared during the copy (a concurrent import of this job?)",
            detail={"path": str(dst), "source": str(src)},
        )
    os.replace(tmp, dst)
    written = hashlib.sha256()
    with dst.open("rb") as fin:
        while chunk := fin.read(CHUNK):
            written.update(chunk)
    if written.hexdigest() != digest.hexdigest():
        dst.unlink(missing_ok=True)
        raise ChecksumMismatch(
            f"{dst.name} does not match the bytes read from {src}",
            detail={
                "expected": digest.hexdigest(),
                "actual": written.hexdigest(),
                "source": str(src),
                "destination": str(dst),
            },
        )
    return digest.hexdigest(), size


def _write_atomic(path: Path, content: str) -> None:
    """Temp file + fsync + `os.replace` (fix round 2, item 2): fsync before
    the rename so the bytes are durable before the name that points at them
    is, not just before the next line of Python runs."""
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


#: `_REQUIRED_FILE_KEYS` is the shape check for `import.json['files'][i]`:
#: everything `_merge_manifest_files`, `_plan`'s stem/seq seeding and the
#: checksums-line writer dereference without a `.get()` guard. A manifest
#: entry missing one of these would otherwise surface as a bare `KeyError`
#: deep in an unrelated function instead of one named, typed refusal here.
_REQUIRED_FILE_KEYS = {"dest", "sha256", "seq", "source_name"}


def _validated_manifest(data: object, path: Path) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ManifestCorrupt(
            f"{path} is not shaped like a Scouter import manifest (expected an "
            "object with a `files` list)",
            detail={"path": str(path)},
        )
    for entry in data["files"]:
        if not isinstance(entry, dict) or not _REQUIRED_FILE_KEYS <= set(entry):
            raise ManifestCorrupt(
                f"{path} has a `files[]` entry missing one of {sorted(_REQUIRED_FILE_KEYS)}",
                detail={"path": str(path)},
            )
    return data


def _read_manifest(path: Path) -> dict | None:
    """The full parsed `import.json`, or `None` if it simply doesn't exist
    yet (a first import). Raises `ManifestCorrupt` for a file that exists
    but cannot be trusted -- never confused with "doesn't exist" (fix round
    2, item 1)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestCorrupt(f"{path} is not valid JSON: {e}", detail={"path": str(path)}) from e
    return _validated_manifest(data, path)


_SEQ_EXT_RE = re.compile(r"^(\d+)\.[A-Za-z0-9]+$")


def _seq_from_dest_name(name: str, date: str, job_slug: str) -> int | None:
    """`seq` recovered from a `<date>_<job_slug>_<camera>_<seq>.<ext>` name
    Scouter itself wrote. Parses from the RIGHT (fix round 3, should-fix 4):
    `paths.SLUG_RE` (`^[a-z0-9][a-z0-9-]{0,47}$`) forbids `_` in both the
    camera slug and the job slug, so `seq` sits after the LAST underscore in
    the name -- `rpartition("_")` on the part after the known
    `<date>_<job_slug>_` prefix finds it directly, with no need to already
    know the camera slug. `None` for anything that doesn't match this job's
    own names."""
    prefix = f"{date}_{job_slug}_"
    if not name.startswith(prefix):
        return None
    remainder = name[len(prefix) :]  # "<camera>_<seq>.<ext>"
    camera_part, sep, seq_and_ext = remainder.rpartition("_")
    if not sep:
        return None
    # Pin the invariant this parse depends on (should-fix 4): a camera slug
    # can never itself contain `_` (`paths.SLUG_RE`'s character class has
    # none), so splitting on the LAST `_` must land exactly on the
    # seq/extension suffix. If `camera_part` still holds one, something
    # upstream let a slug through that violates that invariant, and this
    # parse is not trustworthy -- fail loudly here rather than silently
    # mis-recovering a `seq`.
    assert "_" not in camera_part, f"camera slug {camera_part!r} contains '_': invariant violated"
    match = _SEQ_EXT_RE.match(seq_and_ext)
    return int(match.group(1)) if match else None


def _synthesize_from_checksums(checksums_path: Path, date: str, job_slug: str) -> list[dict]:
    """Fix round 2, item 1: `import.json` is missing (or is the stale half of
    a torn write -- item 2), but `checksums.sha256` survives. Its dest names
    carry `seq`, so entries rebuilt from it still keep `seq` from ever
    restarting, even though the richer EXIF fields (`source_name`,
    `captured_at`, ...) are gone for good -- `source_name` degrades to the
    destination's own name, which is honestly what is still known.

    Known limitation, stated plainly: because the ORIGINAL camera filename is
    gone, a file still sitting on the SAME card at recovery time will not
    stem-match its old entry (that mapping is genuinely unrecoverable), so it
    gets a fresh `seq`/name rather than being recognised as already-imported.
    `seq` still never restarts or collides -- the guarantee this fix makes --
    but a rescanned card can duplicate content under a second name. A missing
    manifest is already an abnormal state; this is the honest cost of it."""
    out: list[dict] = []
    if not checksums_path.is_file():
        return out
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, _, relpath = line.partition("  ")
        name = relpath[len("raw/") :] if relpath.startswith("raw/") else relpath
        out.append(
            {
                "seq": _seq_from_dest_name(name, date, job_slug),
                "dest": f"raw/{name}",
                "source_name": name,
                "bytes": None,
                "sha256": sha,
                "captured_at": None,
                "subsec": 0,
                "exposure_time": None,
                "iso": None,
                "f_number": None,
                "status": "recovered_from_checksums",
            }
        )
    return out


def _synthesize_from_raw_dir(raw_dir: Path, date: str, job_slug: str) -> list[dict]:
    """Fix round 2, item 1: the last resort -- both manifest files are gone,
    but `raw/` itself still holds the copies. Re-hashes them (there is no
    recorded sha256 left to trust instead), so `seq` still never restarts."""
    out: list[dict] = []
    if not raw_dir.is_dir():
        return out
    for entry in sorted(raw_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() == ".xmp":
            continue
        seq = _seq_from_dest_name(entry.name, date, job_slug)
        if seq is None:
            continue  # not one of this job's own names
        out.append(
            {
                "seq": seq,
                "dest": f"raw/{entry.name}",
                "source_name": entry.name,
                "bytes": entry.stat().st_size,
                "sha256": _hash_file(entry),
                "captured_at": None,
                "subsec": 0,
                "exposure_time": None,
                "iso": None,
                "f_number": None,
                "status": "recovered_from_raw_dir",
            }
        )
    return out


def _fill_manifest_gaps(existing: list[dict], extra: list[dict]) -> list[dict]:
    """Union `existing` and `extra` by destination name, but `existing` WINS
    for any destination present in both -- `extra` only fills in destinations
    `existing` doesn't already have an entry for.

    Fix round 3 blocker: `_recover_existing_files` used to call
    `_merge_manifest_files(files, recovered)`, whose LAST-writer-wins order
    let a degraded, `source_name`-less `recovered` entry (from
    `_synthesize_from_checksums`) silently overwrite a perfectly good
    `import.json` entry for the same destination. Combined with the stem
    match then failing on the degraded entry (see that function's documented
    limitation), a re-run after a torn write -- with the card still
    inserted -- would treat every already-imported frame as new and
    duplicate the whole card. `existing` (the parsed manifest) is always the
    richer, more trustworthy source when both have an entry for the same
    name, so it must never lose to a recovery fallback that only exists to
    fill in what `existing` is missing.
    """
    merged: dict[str, dict] = {f["dest"]: f for f in extra}
    merged.update({f["dest"]: f for f in existing})
    return sorted(merged.values(), key=lambda f: f["dest"])


def _recover_existing_files(
    scouter_dir: Path, raw_dir: Path, date: str, job_slug: str
) -> list[dict]:
    """The seed for both never-shrinking merge (fix round 1, blocker 1) and
    stable `seq` (fix round 1, blocker 2), recovered as far down this chain
    as it has to go: `import.json` -> `checksums.sha256` -> `raw/`'s own
    filenames (fix round 2, item 1) -- a lost or torn manifest degrades the
    historical richness of the record, never its safety.
    """
    import_json = scouter_dir / "import.json"
    checksums = scouter_dir / "checksums.sha256"

    manifest = _read_manifest(import_json)  # raises ManifestCorrupt, never a bare KeyError
    if manifest is not None:
        files = manifest["files"]
        if checksums.is_file() and checksums.stat().st_mtime > import_json.stat().st_mtime:
            # Fix round 2, item 2: `_persist_manifest` replaces checksums.sha256
            # FIRST, import.json second -- so a checksums.sha256 NEWER than
            # import.json means a crash landed between the two replaces, and
            # import.json is the stale half of the pair. It still parsed (this
            # branch), so its entries are trustworthy for anything already in
            # them (and WIN over the recovered ones for the same destination --
            # fix round 3 blocker, `_fill_manifest_gaps`); checksums.sha256 is
            # the authority for which `seq` values are actually in use, so its
            # dest-derived entries fill in whatever import.json is missing.
            recovered = _synthesize_from_checksums(checksums, date, job_slug)
            return _fill_manifest_gaps(files, recovered)
        return files

    if checksums.is_file():
        return _synthesize_from_checksums(checksums, date, job_slug)
    return _synthesize_from_raw_dir(raw_dir, date, job_slug)


def _merge_manifest_files(existing: list[dict], new: list[dict]) -> list[dict]:
    """Union `existing` and `new` manifest entries by destination name.

    Fix round 1, blocker 1: re-running an import must never SHRINK the
    manifest just because a source file left the card between runs -- its
    destination is still sitting in `raw/`, still real, still backed up. A
    destination this run recomputed with a DIFFERENT sha256 than the
    manifest already recorded is a typed error: the manifest disagreeing
    with itself (hand-edited, or a disk that changed under a 0444 file) is
    worth stopping for, never silently overwritten.
    """
    merged: dict[str, dict] = {f["dest"]: f for f in existing}
    for entry in new:
        dest = entry["dest"]
        prior = merged.get(dest)
        if prior is not None and prior.get("sha256") != entry["sha256"]:
            raise ChecksumMismatch(
                f"{dest} is already in the manifest with a different sha256 than "
                "this run computed",
                detail={
                    "destination": dest,
                    "expected": prior.get("sha256"),
                    "actual": entry["sha256"],
                },
            )
        merged[dest] = entry
    return sorted(merged.values(), key=lambda f: f["dest"])


def _persist_manifest(
    scouter_dir: Path, meta: dict, existing_files: list[dict], new_files: list[dict]
) -> list[dict]:
    """Merge `new_files` into `existing_files` and write both manifest files
    atomically. Returns the merged list (what `import.json` now holds), so
    the caller can report the real, merged state -- not just this run's."""
    merged = _merge_manifest_files(existing_files, new_files)
    checksum_lines = "".join(f"{f['sha256']}  {f['dest']}\n" for f in merged)
    _write_atomic(scouter_dir / "checksums.sha256", checksum_lines)
    _write_atomic(scouter_dir / "import.json", json.dumps({**meta, "files": merged}, indent=2))
    return merged


def _plan(
    files: list[Path],
    cfg: config.Config,
    job_slug: str,
    date: str | None,
    camera: str | None,
) -> _Plan:
    """Read EXIF for every candidate, derive `date`/`camera` when not given,
    sort by `capture_key`, and assign one `seq` per stem (so `P1.ORF` and
    `P1.JPG` share a number). Returns a `_Plan`; see its own field docs.

    Fix round 4, ruling R17: an existing entry recognises a candidate only
    by `(source_name, sha256)` TOGETHER. Nothing here refuses a duplicate
    and nothing here maps two candidates onto one destination -- identical
    bytes under two different camera filenames are two frames, and the
    caller reports the pair in `duplicates`.

    Fix round 1, blocker 2 (stable sequence): `seq` is seeded from the
    EXISTING manifest's `source_name -> seq` mapping, not restarted at 1 --
    so removing a frame from the card, or adding a second card later, never
    renames or collides with a frame this job already assigned a number.
    """
    tagged = [(f, exif.read_tags(f)) for f in files]

    # Fix round 2, item 5 / fix round 3, should-fix 1: a job's date is fixed
    # at its FIRST import and never re-derived -- a card spanning midnight,
    # or a re-run whose surviving frames' earliest capture time shifted,
    # must not create a SECOND job directory for the same slug. The job
    # directory's own name IS that fixed date (`<YYYY-MM-DD> <slug>`, design
    # 4.4), so finding it needs no manifest read at all. This check runs
    # even when `--date` IS given: an explicit `--date` that disagrees with
    # an already-existing job for this slug is refused by name, rather than
    # silently creating a second directory. `config.find_job` without a date
    # raises when more than one job already shares this slug -- in that
    # (rare, already-unusual) case, an explicit `--date` is trusted outright
    # rather than raising an ambiguity error the caller's own `--date` may
    # already resolve; only a MISSING `--date` still needs find_job's own
    # disambiguation to fail loudly.
    #
    # Fix round 4, item 5: when MORE THAN ONE job already shares this slug,
    # an explicit `--date` is no longer trusted outright. It is honoured only
    # when it names one of those existing jobs; a `--date` matching none of
    # them would silently create a THIRD directory for the same slug, which
    # is exactly the drift the fixed-date rule exists to prevent. Either way
    # the refusal lists the candidates, so the caller can pick one.
    try:
        existing_job_path = config.find_job(cfg, job_slug)
    except UsageError as e:
        existing_job_path = None
        candidates = e.detail.get("candidates")
        if candidates:
            matching = [c for c in candidates if c.startswith(f"{date} ")] if date else []
            if not matching:
                raise UsageError(
                    f"{len(candidates)} jobs already share the slug {job_slug!r} "
                    + (
                        f"and none of them is dated {date}; --date {date} would "
                        "create a third job directory for the same slug"
                        if date
                        else "and no --date was given to choose between them"
                    ),
                    fix=(
                        "pass --date naming one of the existing jobs in "
                        "`detail.candidates`, or pick a different --job-slug"
                    ),
                    detail={
                        "slug": job_slug,
                        "candidates": candidates,
                        "requested_date": date,
                    },
                ) from e
            existing_job_path = config.job_dir(cfg, date, job_slug)
    if existing_job_path is not None:
        existing_date = existing_job_path.name.rsplit(" ", 1)[0]
        if date is None:
            date = existing_date
        elif date != existing_date:
            raise UsageError(
                f"job {job_slug!r} already exists at {existing_job_path} "
                f"(date {existing_date}); --date {date} would create a second "
                "job directory for the same slug",
                fix=f"omit --date to reuse {existing_date}, or pick a different --job-slug",
                detail={
                    "existing_path": str(existing_job_path),
                    "existing_date": existing_date,
                    "requested_date": date,
                },
            )

    plan_warnings: list[str] = []
    if date is None:
        times = sorted(t.captured_at for _, t in tagged if t.captured_at)
        if not times:
            raise UsageError(
                "no capture time could be read from any file in this card; pass "
                "--date explicitly",
                detail={"files": len(files)},
            )
        # E12 D6, row 407: the earliest capture time still wins normally (a
        # card spanning midnight must still get the day the shoot started
        # on) -- but one bad clock (an unset RTC defaulting to 2013-01-01,
        # the E12 Sony card's own DSC01425) used to drag the WHOLE job's
        # date down to it, because it was always the earliest, no matter how
        # far from every other frame on the card. The median is robust to a
        # single such outlier; the earliest is only trusted when it is
        # within 30 days of the median, and the fallback is a warning, never
        # a silent switch.
        earliest, median = times[0], times[len(times) // 2]
        delta_days = abs(
            (
                datetime.strptime(earliest[:10], "%Y-%m-%d")
                - datetime.strptime(median[:10], "%Y-%m-%d")
            ).days
        )
        if delta_days > 30:
            date = median[:10]
            plan_warnings.append(
                f"the earliest capture time ({earliest}) is {delta_days} days from "
                f"the job's median date ({date}); used the median instead -- check "
                "that file's camera clock"
            )
        else:
            date = earliest[:10]

    # Needs `date` (just resolved above) and `job_slug`; a plain read (even
    # the raw-dir fallback tier only hashes files already on disk, not the
    # card), so this is safe to do even for a dry run (ruling 3 only forbids
    # writes and card-side hashing).
    job_path = config.job_dir(cfg, date, job_slug)
    existing_files = _recover_existing_files(
        config.scouter_dir(job_path), config.raw_dir(job_path), date, job_slug
    )

    # Fix round 4, item 4: a `seq` recovered from `checksums.sha256` can be
    # `None` (a destination name Scouter didn't write, or one this job's
    # `<date>_<slug>_` prefix doesn't match). Such an entry must never reach
    # `files_out` or seed a name, so repair it HERE, once, before anything
    # reads `seq`: give it a fresh number past every known one and say so.
    # The file on disk keeps the name it already has -- nothing is renamed;
    # the repair only stops `None` from travelling any further.
    known_seqs = [e["seq"] for e in existing_files if isinstance(e.get("seq"), int)]
    next_seq = max(known_seqs, default=0) + 1
    seqless = sorted(
        (e for e in existing_files if not isinstance(e.get("seq"), int)),
        key=lambda e: e["dest"],
    )
    for entry in seqless:
        entry["seq"] = next_seq
        next_seq += 1
    if seqless:
        plan_warnings.append(
            f"{len(seqless)} recovered manifest entr"
            f"{'y' if len(seqless) == 1 else 'ies'} had no recoverable seq "
            f"({', '.join(e['dest'] for e in seqless)}); assigned fresh "
            f"number(s) {', '.join(str(e['seq']) for e in seqless)} so no new "
            "frame can reuse them -- the files themselves were not renamed"
        )

    stem_seq: dict[str, int] = {}
    for entry in existing_files:
        stem_seq.setdefault(Path(entry["source_name"]).stem, entry["seq"])
    pre_existing_stems = set(stem_seq)

    # Fix round 4, item 1 (ruling R17): resume identity is
    # `(source_name, sha256)` -- BOTH. An existing entry recognises a
    # candidate only when the camera filename AND the bytes match; the same
    # bytes under a different filename are a NEW frame with a new `seq`
    # (keep-both), reported in `duplicates`. The old sha-only index made two
    # byte-identical frames on one card map to a single destination, which
    # turned every re-run of such a card into a hard `UsageError`.
    by_identity: dict[tuple[str, str], dict] = {
        (e["source_name"], e["sha256"]): e for e in existing_files if e.get("sha256")
    }
    # Ruling R17b: the one case where a source name is genuinely UNKNOWN
    # rather than different -- `_synthesize_from_checksums` has no camera
    # filename left to record, so it writes the destination's own name as a
    # placeholder. For those entries the bytes are the only identity that
    # exists, so a sha match IS a resume match -- otherwise a re-run with the
    # card still inserted would re-copy every frame of it under new names
    # (the fix round 3 blocker). Narrow by construction, and the ONLY
    # sha-only match in this module: just entries Scouter itself marked
    # `recovered_*` whose `source_name` is exactly their own dest.
    #
    # Carried finding 3: a QUEUE per sha, not one entry per sha. Two
    # byte-identical frames are two real files on disk and therefore two
    # recovered entries, and each must be claimed by a different candidate.
    # The old `dict[str, dict]` collapsed them to whichever entry came last
    # and handed it to BOTH candidates, which minted the same destination
    # twice and turned the re-run into the `UsageError` below. Sorted by
    # `dest` so the claim order is deterministic across runs.
    by_sha_unknown_source: dict[str, list[dict]] = {}
    for e in sorted(existing_files, key=lambda e: e["dest"]):
        if (
            e.get("sha256")
            and str(e.get("status", "")).startswith("recovered_")
            and e["source_name"] == Path(e["dest"]).name
        ):
            by_sha_unknown_source.setdefault(e["sha256"], []).append(e)
    known_source_names = {e["source_name"] for e in existing_files}

    # E12 D5, row 407 call 1: a card is never assumed to hold one body. Every
    # file is slugged from its OWN EXIF here, always -- `--camera` is a
    # FILTER (import only that body's files, report the rest as skipped),
    # never a label stamped onto files it does not belong to. The old
    # behaviour refused a mixed card outright, then (once `--camera` was
    # given to get past the refusal) mislabelled every OTHER body's files
    # with the chosen slug and cleared their `camera_model`.
    if camera is not None:
        camera = paths.validate_slug(camera)

    # Review round 1, ruling 2: `exif.camera_slug` raises `ExifUnreadable`
    # for a file with NO camera model in its EXIF at all -- this used to
    # abort the WHOLE card over that one file, and the raised message ("no
    # camera model ... and no --camera was given") was false the moment
    # `--camera` WAS given, since the old code never even reached this far
    # with one. An explicit `--camera X` is also a label for such a file
    # (imported under X, listed in `assumed_camera`); without one, the file
    # is skipped (`skipped_no_body`) and the rest of the card still imports.
    slug_by_path: dict[Path, str] = {}
    assumed_camera: list[dict] = []
    skipped_no_body: list[dict] = []
    readable_tagged: list[tuple[Path, exif.ExifTags]] = []
    for path, t in tagged:
        try:
            slug_by_path[path] = exif.camera_slug(t, cfg)
            readable_tagged.append((path, t))
        except ExifUnreadable:
            if camera is not None:
                slug_by_path[path] = camera
                assumed_camera.append({"source_name": path.name, "camera": camera})
                readable_tagged.append((path, t))
            else:
                skipped_no_body.append({"source_name": path.name})
    tagged = readable_tagged

    # Fix round 2: round 1 made a no-body FILE never fail the whole card
    # (skipped, listed in `skipped_no_body`, the rest still imports) -- but
    # a card where NOT A SINGLE file has a camera model, imported without
    # `--camera`, must still refuse rather than silently succeed with an
    # empty job directory. A card with SOME bodies present keeps round 1's
    # behaviour untouched (this only fires when nothing at all is left).
    if camera is None and not tagged and skipped_no_body:
        names = ", ".join(e["source_name"] for e in skipped_no_body)
        raise UsageError(
            f"no camera model in any file's EXIF on this card ({names}); "
            "nothing to import",
            fix="pass --camera X to import them all under one explicit body",
            detail={"skipped_no_body": skipped_no_body},
        )

    skipped_other_camera: list[dict] = []
    if camera is not None:
        matching = [(p, t) for p, t in tagged if slug_by_path[p] == camera]
        skipped_other_camera = [
            {"source_name": p.name, "camera": slug_by_path[p]}
            for p, t in tagged
            if slug_by_path[p] != camera
        ]
        if not matching:
            other = sorted({slug_by_path[p] for p, _ in tagged})
            raise UsageError(
                f"no files on this card belong to camera {camera!r}; this card "
                f"holds {', '.join(other)}",
                fix=f"drop --camera to import every body, or pass one of: {', '.join(other)}",
                detail={"camera": camera, "cameras_present": other},
            )
        tagged = matching

    slugs_in_plan = sorted({slug_by_path[p] for p, _ in tagged})
    # `camera`/`camera_model`/`camera_make` describe THIS run's plan for the
    # dry-run preview and `import.json`'s per-run `meta` -- "mixed" (never a
    # silently wrong single slug) when more than one body is actually
    # included; the per-file truth is `plan[i]["camera"]`, always.
    camera = camera or (slugs_in_plan[0] if len(slugs_in_plan) == 1 else "mixed")
    models = {t.model for _, t in tagged if t.model}
    makes = {t.make for _, t in tagged if t.make}
    camera_model = next(iter(models)) if len(models) == 1 else None
    camera_make = next(iter(makes)) if len(makes) == 1 else None

    ordered = sorted(tagged, key=lambda pair: exif.capture_key(pair[1], pair[0]))
    seq_digits = cfg.import_.seq_digits
    dest_sources: dict[str, Path] = {}
    plan: list[dict] = []
    hashed_files = 0
    hashed_bytes = 0
    for path, tags in ordered:
        stem = path.stem
        size = path.stat().st_size
        # Fix round 4, item 3: the resume pre-check is a NAME check first and
        # a hash only second. A candidate can only be recognised by an
        # existing entry through `(source_name, sha256)`, so a filename no
        # entry has ever recorded cannot possibly match and is never hashed
        # -- on a first import, and on any re-run with a fresh card, that is
        # zero hashing here. The exception is a job carrying
        # source-name-less recovery entries: for those the bytes are the only
        # identity left, so every candidate has to be hashed. Whatever it
        # cost is counted and reported (`hashed_files`/`hashed_bytes`), never
        # silently spent.
        reused = None
        if path.name in known_source_names or any(by_sha_unknown_source.values()):
            content_hash = _hash_file(path)
            hashed_files += 1
            hashed_bytes += size
            # Claimed, not merely looked up (carried finding 3): each
            # recovered entry stands for one file on disk, so it is popped
            # off its sha's queue and cannot be handed to a second
            # candidate. A card with two byte-identical frames therefore
            # resumes into its two recovered destinations instead of minting
            # one destination twice.
            reused = by_identity.get((path.name, content_hash))
            if reused is None:
                queue = by_sha_unknown_source.get(content_hash)
                if queue:
                    reused = queue.pop(0)
        if reused is not None:
            # Same name AND same bytes (or the same bytes under an entry with
            # no recorded name at all) -- this frame is already imported.
            # Reuse its `seq` and destination rather than minting a new name.
            seq = reused["seq"]
            dest_name = Path(reused["dest"]).name
        else:
            if stem not in stem_seq:
                stem_seq[stem] = next_seq
                next_seq += 1
            seq = stem_seq[stem]
            ext = path.suffix.lower()
            # This file's OWN slug, never the run-level `camera` summary
            # (which is "mixed" for a card with more than one body) -- D5's
            # whole point.
            dest_name = f"{date}_{job_slug}_{slug_by_path[path]}_{seq:0{seq_digits}d}{ext}"
        if dest_name in dest_sources:
            # Fix round 1, should-fix 3: two source files from different card
            # subdirectories sharing a stem+ext (the same camera filename
            # counter, recycled across folders/cards) would otherwise trip
            # the "impossible by construction" assertion below -- it is very
            # much possible, and it is a caller-facing usage problem, not an
            # internal bug.
            raise UsageError(
                f"two source files would both become {dest_name}: "
                f"{dest_sources[dest_name]} and {path}",
                detail={
                    "dest": dest_name,
                    "sources": [str(dest_sources[dest_name]), str(path)],
                },
            )
        dest_sources[dest_name] = path
        plan.append(
            {
                "seq": seq,
                "stem": stem,
                "source": path,
                "source_name": path.name,
                "dest_name": dest_name,
                "bytes": size,
                "captured_at": tags.captured_at,
                "subsec": tags.subsec,
                "exposure_time": tags.exposure_time,
                "iso": tags.iso,
                "f_number": tags.f_number,
                # This file's own body (D5): what `dest_name` was actually
                # built from, carried through to `files_out` so
                # `import.json` never disagrees with the filenames it wrote.
                "camera": slug_by_path[path],
                "camera_model": tags.model,
                "camera_make": tags.make,
            }
        )
    return _Plan(
        date=date,
        camera=camera,
        camera_model=camera_model,
        camera_make=camera_make,
        entries=plan,
        existing_files=existing_files,
        pre_existing_stems=pre_existing_stems,
        warnings=plan_warnings,
        hashed_files=hashed_files,
        hashed_bytes=hashed_bytes,
        skipped_other_camera=skipped_other_camera,
        assumed_camera=assumed_camera,
        skipped_no_body=skipped_no_body,
    )


@metrics.instrumented("library.import")
def import_card(
    source: str,
    job_slug: str,
    *,
    date: str | None = None,
    camera: str | None = None,
    extensions: list[str] | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> ImportResult:
    """Copy, checksum, rename and register a card into the library. Gated: an
    import always touches more than one file (design section 6, spec 6)."""
    cfg = config.load()
    config.require_backup(cfg)  # BEFORE the card is read -- the gate must not
    # depend on the card being present at all.

    source_path = Path(source)
    if not source_path.is_dir():
        # Fix round 1, should-fix 4: `_scan`'s own `iterdir()` would raise a
        # bare `FileNotFoundError`/`NotADirectoryError` here -- not one of
        # Scouter's typed errors, and not actionable.
        raise UsageError(
            f"{source_path} is not a directory",
            fix="check that the card is mounted at that path, or pass a different --source",
            detail={"source": str(source_path)},
        )
    exts = tuple(extensions) if extensions else cfg.import_.extensions
    files = _scan(source_path, exts)
    if not files:
        raise UsageError(
            f"no files with extension(s) {', '.join(exts)} found under "
            f"{source_path} (searched to depth {SCAN_MAX_DEPTH})",
            detail={
                "source": str(source_path),
                "extensions": list(exts),
                "max_depth": SCAN_MAX_DEPTH,
            },
        )

    resolved = _plan(files, cfg, job_slug, date, camera)
    resolved_date = resolved.date
    resolved_camera = resolved.camera
    camera_model = resolved.camera_model
    camera_make = resolved.camera_make
    planned = resolved.entries
    existing_files = resolved.existing_files
    pre_existing_stems = resolved.pre_existing_stems
    warnings: list[str] = list(resolved.warnings)
    if resolved.skipped_other_camera:
        skipped_names = ", ".join(
            f"{e['source_name']} ({e['camera']})" for e in resolved.skipped_other_camera
        )
        warnings.append(
            f"--camera {camera!r} matched {len(planned)} file(s); skipped "
            f"{len(resolved.skipped_other_camera)} from other bodies: {skipped_names}"
        )
    if resolved.assumed_camera:
        names = ", ".join(e["source_name"] for e in resolved.assumed_camera)
        warnings.append(
            f"{len(resolved.assumed_camera)} file(s) had no camera model in their EXIF; "
            f"imported under --camera {camera!r} anyway (see assumed_camera): {names}"
        )
    if resolved.skipped_no_body:
        names = ", ".join(e["source_name"] for e in resolved.skipped_no_body)
        warnings.append(
            f"{len(resolved.skipped_no_body)} file(s) had no camera model in their EXIF "
            f"and no --camera was given; skipped (see skipped_no_body): {names}"
        )

    plan_view = [
        {
            "seq": p["seq"],
            "source": str(p["source"]),
            "dest": f"raw/{p['dest_name']}",
            "bytes": p["bytes"],
            "camera": p["camera"],
            "captured_at": p["captured_at"],
        }
        for p in planned
    ]
    if dry_run or not confirm:
        # Fix round 4, item 3: the old reason claimed "no files were hashed",
        # which had stopped being true the moment the plan gained a
        # content-based resume check. The plan now hashes only candidates
        # whose filename an existing manifest entry already records (or, for
        # a job holding source-name-less recovery entries, all of them --
        # the bytes are the only identity left), and it says exactly what
        # that cost. A first import, or a re-run with a fresh card, still
        # hashes nothing at all (ruling 3's spirit) -- and when it does hash,
        # the number is in the reason rather than hidden.
        cost = (
            "no files were hashed"
            if resolved.hashed_files == 0
            else (
                f"hashed: {resolved.hashed_files} file(s), {resolved.hashed_bytes} "
                "bytes (the resume pre-check: candidates whose filename this job "
                "has already imported)"
            )
        )
        return ImportResult(
            outcome="dry_run",
            job_slug=job_slug,
            date=resolved_date,
            camera=resolved_camera,
            camera_model=camera_model,
            camera_make=camera_make,
            source_root=str(source_path),
            skipped_other_camera=resolved.skipped_other_camera,
            assumed_camera=resolved.assumed_camera,
            skipped_no_body=resolved.skipped_no_body,
            plan=plan_view,
            warnings=warnings,
            reason=(
                f"dry run: no directories were created and nothing was copied; {cost}"
                if dry_run
                else f"{len(planned)} file(s) planned; re-run with --yes / confirm=true "
                f"to copy them (an import always touches more than one file); {cost}"
            ),
        )

    job_path = config.job_dir(cfg, resolved_date, job_slug)
    raw = config.raw_dir(job_path)
    scouter_d = config.scouter_dir(job_path)
    raw.mkdir(parents=True, exist_ok=True)
    scouter_d.mkdir(parents=True, exist_ok=True)
    # Fix round 1, nit (fix round 2: age-guarded): sweep `.part` leftovers
    # from a run that died mid-copy (a hard kill, a power loss) before
    # starting -- `_copy_verified` always cleans up its own tmp file on a
    # normal return or a checksum mismatch, so anything still here is from a
    # run that never got that far. Only files older than an hour: a fresher
    # `.part` might belong to a genuinely concurrent import of this same job
    # (rare, but `_copy_verified`'s own O_EXCL-equivalent already covers the
    # final link -- this sweep must not race it by deleting live work).
    part_cutoff = time.time() - STALE_PART_AGE_S
    for stale in raw.glob(".*.part"):
        try:
            if stale.stat().st_mtime < part_cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    meta = {
        "version": 1,
        "job_slug": job_slug,
        "date": resolved_date,
        "job_date": resolved_date,
        "camera": resolved_camera,
        "camera_model": camera_model,
        "camera_make": camera_make,
        "imported_at": _now_z(),
        "scouter_version": __version__,
        "source_root": str(source_path),
    }

    # Fix round 3, should-fix 2: keyed by dest, so a missing raw/ file whose
    # destination the manifest already tracks (deleted by hand, or lost with
    # the disk) can be told apart from a genuinely new destination before
    # any bytes are written.
    existing_by_dest = {e["dest"]: e for e in existing_files}

    # Fix round 4, items 1-2: byte-identical frames are KEPT BOTH (they are
    # two real exposures as far as this verb is concerned -- an in-camera
    # copy, a doubled card, a genuinely repeated frame), and the pairs are
    # reported so the decision is visible instead of implied. Seeded from the
    # existing manifest (so a re-run reports the same pairs it reported the
    # first time) and filled in from THIS run's own hashes, which the copy
    # loop computes anyway: duplicate detection costs zero extra reads.
    first_dest_by_sha: dict[str, str] = {}
    for entry in sorted(existing_files, key=lambda e: e["dest"]):
        if entry.get("sha256"):
            first_dest_by_sha.setdefault(entry["sha256"], entry["dest"])
    duplicates: list[list[str]] = []

    files_out: list[dict] = []
    copied = 0
    skipped = 0
    total_bytes = 0
    try:
        for p in planned:
            src: Path = p["source"]
            dest = raw / p["dest_name"]
            if dest.exists():
                # Resume-safety: never overwrite. A 0444 destination from a
                # previous run is still readable, so it is hashable without
                # any special-casing -- and it is NOT re-chmod-ed here, only
                # in the `else` branch below where a fresh copy just landed.
                existing_hash = _hash_file(dest)
                source_hash = _hash_file(src)
                if existing_hash == source_hash:
                    sha = existing_hash
                    size = dest.stat().st_size
                    status = (
                        "skipped_identical" if cfg.import_.skip_identical else "already_present"
                    )
                    skipped += 1
                elif p["stem"] in pre_existing_stems:
                    # Fix round 2, item 4: this stem was already imported in
                    # an earlier run (usually a camera filename counter that
                    # reset on a second card) -- identical content is the
                    # ordinary resume-skip above; different content is a
                    # naming collision the plan cannot resolve on its own,
                    # distinct from a generic occupied destination.
                    raise SourceStemCollision(
                        f"{src} shares the stem {p['stem']!r} with {dest.name}, "
                        "already imported by an earlier run of this job, but the "
                        "bytes differ",
                        detail={
                            "stem": p["stem"],
                            "new_source": str(src),
                            "existing_dest": str(dest),
                        },
                    )
                else:
                    raise DestinationCollision(
                        f"{dest} already exists with different content than {src}",
                        detail={"path": str(dest), "source": str(src)},
                    )
            else:
                # Fix round 3, should-fix 2: the manifest may already have an
                # entry for this exact destination (a raw/ file deleted by
                # hand, or lost with a disk, while its record survived) --
                # compare the SOURCE's hash against what the manifest already
                # says BEFORE writing anything. A match means this is a
                # legitimate re-copy of a known frame, re-using the `seq`
                # `_plan` already resolved for it through `(source_name,
                # sha256)` -- the "sha-dedupe above" this comment used to
                # point at was removed by fix round 4 (ruling R17: identical
                # bytes under a different name are a new frame, reported in
                # `duplicates`, not deduplicated); a mismatch is a typed error, raised
                # before any byte lands, not a silent overwrite of what the
                # manifest already promised.
                tracked = existing_by_dest.get(f"raw/{p['dest_name']}")
                if tracked is not None and tracked.get("sha256"):
                    source_hash = _hash_file(src)
                    if source_hash != tracked["sha256"]:
                        raise ChecksumMismatch(
                            f"{dest} is missing, and the manifest already records "
                            f"{p['dest_name']} with a different sha256 than {src} "
                            "has now",
                            detail={
                                "destination": str(dest),
                                "source": str(src),
                                "expected": tracked["sha256"],
                                "actual": source_hash,
                            },
                        )
                sha, size = _copy_verified(src, dest)
                if cfg.import_.read_only_originals:
                    os.chmod(dest, 0o444)
                status = "copied"
                copied += 1
            total_bytes += size
            this_dest = f"raw/{p['dest_name']}"
            first_dest = first_dest_by_sha.setdefault(sha, this_dest)
            if first_dest != this_dest:
                duplicates.append([first_dest, this_dest])
            files_out.append(
                {
                    "seq": p["seq"],
                    "dest": this_dest,
                    "source_name": p["source_name"],
                    "bytes": size,
                    "sha256": sha,
                    "captured_at": p["captured_at"],
                    "subsec": p["subsec"],
                    "exposure_time": p["exposure_time"],
                    "iso": p["iso"],
                    "f_number": p["f_number"],
                    "status": status,
                    # D5: this file's OWN body, exactly what `dest_name` was
                    # built from -- never the job-level `camera` summary.
                    "camera": p["camera"],
                    "camera_model": p["camera_model"],
                    "camera_make": p["camera_make"],
                }
            )
    except BaseException as e:
        # Fix round 1, should-fix 5 (fix round 2: BaseException, item 2 --
        # so a Ctrl-C/KeyboardInterrupt mid-loop still persists what landed,
        # not just a typed ScouterError): whatever landed THIS run before
        # the failure is still real, and the manifest must not stay silent
        # about it -- write the merged manifest now (never shrinking what a
        # prior run already recorded), and name how far this run got in the
        # error's own detail, so the agent isn't left guessing.
        totals = {
            "planned": len(planned),
            "copied": copied,
            "skipped_identical": skipped,
            "bytes": total_bytes,
        }
        manifest_written = False
        persist_error: BaseException | None = None
        try:
            _persist_manifest(scouter_d, {**meta, "totals": totals}, existing_files, files_out)
            manifest_written = True
        except BaseException as persist_exc:
            # Fix round 2, item 3 (fix round 3, should-fix 3: BaseException
            # too, so a second Ctrl-C landing DURING this recovery write is
            # also recorded rather than propagating in place of the original):
            # a failure HERE (e.g. the merge's own differing-hash check) must
            # never replace or mask the ORIGINAL error as what the caller
            # sees -- attach it to that error's own detail instead, and
            # still raise the original.
            persist_error = persist_exc
        if isinstance(e, ScouterError):
            e.detail = {
                **e.detail,
                "files_landed_this_run": len(files_out),
                "manifest_written": manifest_written,
            }
            if persist_error is not None:
                e.detail["manifest_persist_error"] = (
                    f"{type(persist_error).__name__}: {persist_error}"
                )
        raise

    totals = {
        "planned": len(planned),
        "copied": copied,
        "skipped_identical": skipped,
        "bytes": total_bytes,
        # Fix round 4, item 3: the plan's resume pre-check, counted rather
        # than assumed. `plan_hashed_*` is ONLY that pre-check -- the copy's
        # own two sha256 passes per file are the verb's advertised work
        # (design section 6, step 7) and are not folded in here, which would
        # make the number mean nothing.
        "plan_hashed_files": resolved.hashed_files,
        "plan_hashed_bytes": resolved.hashed_bytes,
        "duplicate_pairs": len(duplicates),
    }

    # D8, row 407: `totals`/`camera`/`camera_model`/`source_root` used to be
    # THIS run's numbers only, silently overwriting an earlier import's on
    # every appending run -- `files` had all 14 frames of a two-body job, but
    # `totals`/`camera` described only the 6 imported last. Computed from the
    # MERGED set (every import this job has ever done), not just this run's.
    merged_preview = _merge_manifest_files(existing_files, files_out)
    job_cameras = sorted({f["camera"] for f in merged_preview if f.get("camera")})
    job_models = sorted({f["camera_model"] for f in merged_preview if f.get("camera_model")})
    job_makes = sorted({f["camera_make"] for f in merged_preview if f.get("camera_make")})
    # Review round 1 nit: no try/except here -- `_plan()` already read (and
    # would have raised `ManifestCorrupt` on) this same `import.json`
    # earlier in this run, via `_recover_existing_files`. Catching it again
    # here was dead code on the normal path, and on the vanishingly narrow
    # race where the file changed under us in between, swallowing it would
    # have silently dropped `source_roots` rather than surfacing the real
    # problem.
    prior_manifest = _read_manifest(scouter_d / "import.json")
    prior_roots = (prior_manifest or {}).get("source_roots") or (
        [prior_manifest["source_root"]] if (prior_manifest or {}).get("source_root") else []
    )
    source_roots = sorted(set(prior_roots) | {str(source_path)})
    totals["job_files"] = len(merged_preview)
    # Recovered entries (`_synthesize_from_checksums`) may carry `bytes: None`
    # -- an honest gap, not a zero -- so it is never allowed to poison the sum.
    totals["job_bytes"] = sum(f.get("bytes") or 0 for f in merged_preview)

    meta = {
        **meta,
        "camera": job_cameras[0] if len(job_cameras) == 1 else ("mixed" if job_cameras else None),
        "cameras": job_cameras,
        "camera_model": job_models[0] if len(job_models) == 1 else None,
        "camera_models": job_models,
        "camera_make": job_makes[0] if len(job_makes) == 1 else None,
        "source_roots": source_roots,
    }
    manifest = {**meta, "totals": totals, "duplicates": duplicates}
    merged_files = _persist_manifest(scouter_d, manifest, existing_files, files_out)

    if duplicates:
        warnings.append(
            f"{len(duplicates)} byte-identical pair(s) kept as separate frames "
            f"(see `duplicates`): {'; '.join(a + ' == ' + b for a, b in duplicates)}"
        )
    film_roll_registered = False
    imported_count = 0
    scan_incomplete = None
    recursive_honoured = None
    try:
        result = dtbridge.call("import_batch", {"source_path": str(raw), "recursive": False})
    except BridgeTimeout as e:
        # The copy is the irreversible part and it already succeeded -- a
        # darktable that is not running must not fail the import.
        film_roll_registered = False
        warnings.append(e.fix)
    else:
        imported_count = int(result.get("imported", 0))
        scan_incomplete = result.get("scan_incomplete")
        recursive_honoured = result.get("recursive_honoured")
        # Fix round 1, should-fix 7: a bridge call that returns without
        # raising but reports 0 images imported registered nothing -- that
        # is not honestly "film_roll_registered: true".
        film_roll_registered = imported_count > 0
        if not film_roll_registered:
            warnings.append(
                "import_batch reported 0 images imported; the film roll may not "
                "have registered -- re-run `library register` once darktable can "
                "see the card"
            )
        if result.get("note"):
            warnings.append(str(result["note"]))

    try:
        from scouter.core import vault

        note = vault.write_job_note(cfg, {**manifest, "files": merged_files})
        if not note.get("written"):
            warnings.append(f"vault note not written: {note.get('reason')}")
    except Exception as e:  # noqa: BLE001 - the copy already succeeded; a vault
        # write failure (a bad path, a permissions error, anything unexpected)
        # must never roll it back -- it is a warning, never a raise.
        warnings.append(f"vault note not written: {e}")

    return ImportResult(
        duplicates=duplicates,
        job_slug=job_slug,
        date=resolved_date,
        # Job-wide now (D8), not just this run's plan: `meta` was recomputed
        # from `merged_preview` above.
        camera=meta["camera"] or "",
        camera_model=meta["camera_model"],
        camera_make=meta["camera_make"],
        cameras=job_cameras,
        camera_models=job_models,
        source_root=str(source_path),
        source_roots=source_roots,
        files=merged_files,
        totals=totals,
        skipped_other_camera=resolved.skipped_other_camera,
        assumed_camera=resolved.assumed_camera,
        skipped_no_body=resolved.skipped_no_body,
        film_roll_registered=film_roll_registered,
        imported=imported_count,
        scan_incomplete=scan_incomplete,
        recursive_honoured=recursive_honoured,
        path=str(job_path),
        warnings=warnings,
    )


def _verify_at(job_slug: str, job_path: Path) -> VerifyResult:
    """The actual re-hash, unwrapped from `@metrics.instrumented`: shared by
    `verify()` (one event) and `relocate()` (which re-verifies every job under
    its own single `library.relocate` event -- calling the instrumented
    `verify()` in a loop there would emit one event per job instead)."""
    raw = config.raw_dir(job_path)
    checksums_path = config.scouter_dir(job_path) / "checksums.sha256"
    if not checksums_path.is_file():
        # Fix round 1, should-fix 6: no manifest is not the same fact as "an
        # empty job verified clean" -- `checked=0, ok=True` for a job that
        # was never imported (or whose manifest was lost) is a phantom pass.
        raise UsageError(
            f"no {checksums_path} for job {job_slug!r}: it has never been "
            "imported, or its manifest is missing",
            fix="run `library import` for this job first, or check the job slug/--date",
            detail={"job_slug": job_slug, "checksums_path": str(checksums_path)},
        )

    # Fix round 2, nit: the guard above already proved this file exists --
    # re-checking `.is_file()` here was dead, unreachable-false code.
    manifest: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, _, relpath = line.partition("  ")
        name = relpath[len("raw/") :] if relpath.startswith("raw/") else relpath
        manifest[name] = sha

    present = (
        {p.name: p for p in raw.iterdir() if p.is_file() and p.suffix.lower() != ".xmp"}
        if raw.is_dir()
        else {}
    )

    checked = 0
    mismatched: list[dict] = []
    missing: list[str] = []
    for name in sorted(manifest):
        expected = manifest[name]
        if name not in present:
            missing.append(name)
            continue
        checked += 1
        actual = _hash_file(present[name])
        if actual != expected:
            mismatched.append({"name": name, "expected": expected, "actual": actual})

    extra = sorted(set(present) - set(manifest))
    ok = not mismatched and not missing and not extra
    return VerifyResult(
        job_slug=job_slug,
        checked=checked,
        ok=ok,
        mismatched=mismatched,
        missing=missing,
        extra=extra,
        path=str(job_path),
    )


@metrics.instrumented("library.verify")
def verify(job_slug: str, *, date: str | None = None, job: str | None = None) -> VerifyResult:
    """Re-hash raw/ against .scouter/checksums.sha256. `.xmp` sidecars are
    excluded by SUFFIX, not by silence: they are darktable's files, they change
    whenever an edit lands, and they are never in the manifest."""
    cfg = config.load()
    job_path = config.find_job(cfg, job_slug, date)
    return _verify_at(job_slug, job_path)


@metrics.instrumented("library.register")
def register(
    job_slug: str,
    *,
    date: str | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> RegisterResult:
    """Re-run `import_batch` for a job whose import happened with darktable
    closed. Gated: it writes to darktable's own library."""
    cfg = config.load()
    job_path = config.find_job(cfg, job_slug, date)
    raw = config.raw_dir(job_path)
    plan = [{"action": "library.register", "source_path": str(raw), "recursive": False}]
    if dry_run or not confirm:
        return RegisterResult(
            outcome="dry_run",
            job_slug=job_slug,
            plan=plan,
            path=str(job_path),
            reason=(
                "dry run: no bridge call was made"
                if dry_run
                else "re-run with --yes / confirm=true to register the film roll"
            ),
        )
    result = dtbridge.call("import_batch", {"source_path": str(raw), "recursive": False})
    imported_count = int(result.get("imported", 0))
    # Fix round 1, should-fix 7: a call that didn't raise but reports 0
    # images imported registered nothing -- don't claim it did.
    film_roll_registered = imported_count > 0
    warnings = [str(result["note"])] if result.get("note") else []
    if not film_roll_registered:
        warnings.append(
            "import_batch reported 0 images imported; the film roll may not " "have registered"
        )
    return RegisterResult(
        job_slug=job_slug,
        film_roll_registered=film_roll_registered,
        imported=imported_count,
        scan_incomplete=result.get("scan_incomplete"),
        recursive_honoured=result.get("recursive_honoured"),
        path=str(job_path),
        warnings=warnings,
    )


def _list_jobs(root: Path) -> list[dict]:
    """Every `<YYYY>/<YYYY-MM-DD slug>` job under `root`. Shared by the
    `library.jobs` verb and `relocate`'s dry-run plan (which must not call the
    instrumented `jobs()` and double up on events)."""
    entries: list[dict] = []
    if not root.is_dir():
        return entries
    for year_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for job_path in sorted(p for p in year_dir.iterdir() if p.is_dir()):
            date, sep, slug = job_path.name.partition(" ")
            if not sep:
                continue  # not a "<date> <slug>" job directory -- ignore it
            raw = config.raw_dir(job_path)
            scouter_d = config.scouter_dir(job_path)
            files = (
                sorted(p for p in raw.iterdir() if p.is_file() and p.suffix.lower() != ".xmp")
                if raw.is_dir()
                else []
            )
            # Ratings come from `xmp.rating`: a file with no sidecar or no
            # `xmp:Rating` attribute buckets under 0 ("unrated"), the same
            # convention darktable's own rating widget starts from.
            ratings: dict[int, int] = {}
            for f in files:
                key = xmp.rating(f) or 0
                ratings[key] = ratings.get(key, 0) + 1
            manifests = (
                sorted(p.name for p in scouter_d.iterdir() if p.is_file())
                if scouter_d.is_dir()
                else []
            )
            entries.append(
                {
                    "date": date,
                    "slug": slug,
                    "path": str(job_path.relative_to(root)),
                    "frames": len(files),
                    "bytes": sum(f.stat().st_size for f in files),
                    "ratings": ratings,
                    "manifests": manifests,
                    # `cull.propose` writes `.scouter/cull-<ts>.json`
                    # (`scouter/core/cull.py`, design 4.4). The stale comment
                    # this replaces said cull had "not landed yet", and the
                    # substring it matched (`cull-proposal`) is not a name cull
                    # has ever written -- so this field read False even for a
                    # job with a proposal sitting right there. Matched against
                    # the real prefix now, and `tests/test_library.py` pins it
                    # against a file `cull.propose` itself produced.
                    "has_cull_proposal": any(
                        m.startswith("cull-") and m.endswith(".json") for m in manifests
                    ),
                    # No verb records a verify timestamp anywhere yet, so this
                    # stays None rather than inventing one.
                    "verified_at": None,
                }
            )
    return entries


@metrics.instrumented("library.jobs")
def jobs(*, job: str | None = None) -> JobsResult:
    """Every job under `library_root`, browsable without `ls`."""
    cfg = config.load()
    return JobsResult(jobs=_list_jobs(cfg.root()))


def _rewrite_library_root(config_path: Path, new_root: str) -> None:
    """Read the config file, mutate ONLY `library_root`, write it back
    atomically -- every other key and every `_comment` (top-level or nested)
    survives untouched because this never goes through the `Config`
    dataclass, which would drop them."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["library_root"] = new_root
    _write_atomic(config_path, json.dumps(raw, indent=2) + "\n")


@metrics.instrumented("library.relocate")
def relocate(
    new_root: str,
    *,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> RelocateResult:
    """Move the library to `new_root`: rewrite `library_root` in the config
    and re-verify every job there. Gated: it writes outside the library
    (the config file) and, per job, as many bytes as darktable's film rolls
    would need to catch up.

    This NEVER edits darktable's `library.db` -- film roll re-registration
    goes through the bridge (`library.register`, `import_batch`) per job,
    left to the operator, and `next_step` says so honestly rather than
    claiming a phantom success.
    """
    cfg = config.load()
    old_root = cfg.root()
    new_root_path = Path(new_root).expanduser()

    if not new_root_path.is_dir():
        raise UsageError(
            f"{new_root_path} does not exist or is not a directory",
            detail={"new_root": str(new_root_path)},
        )
    try:
        new_root_path.resolve().relative_to(old_root.resolve())
        nested = True
    except ValueError:
        nested = False
    if nested:
        raise UsageError(
            f"{new_root_path} is inside the current library root {old_root}; "
            "a nested move would recurse",
            detail={"old_root": str(old_root), "new_root": str(new_root_path)},
        )

    entries = _list_jobs(old_root)
    plan = [
        {
            "slug": e["slug"],
            "date": e["date"],
            "new_path": str(new_root_path / e["path"]),
            "exists_at_new_root": (new_root_path / e["path"]).exists(),
        }
        for e in entries
    ]

    if dry_run or not confirm:
        return RelocateResult(
            outcome="dry_run",
            old_root=str(old_root),
            new_root=str(new_root_path),
            plan=plan,
            reason=(
                "dry run: no config change and no bridge calls"
                if dry_run
                else f"{len(entries)} job(s) would relocate; re-run with --yes / "
                "confirm=true to rewrite library_root and re-verify"
            ),
        )

    if not cfg.exists or not cfg.path:
        raise UsageError(
            "no Scouter config file to rewrite; run `scouter config init` first",
            detail={"config": cfg.path},
        )
    _rewrite_library_root(Path(cfg.path), str(new_root_path))

    new_cfg = dataclasses.replace(cfg, library_root=str(new_root_path))
    results = []
    for e in entries:
        job_path = config.job_dir(new_cfg, e["date"], e["slug"])
        try:
            vr = _verify_at(e["slug"], job_path)
        except UsageError:
            # `_verify_at` refuses (rather than a phantom "ok") when there is
            # no checksums.sha256 to check against -- at a fresh new_root
            # that means this job hasn't actually been copied there yet, not
            # that its check passed vacuously.
            results.append(
                {
                    "slug": e["slug"],
                    "ok": False,
                    "checked": 0,
                    "mismatched": [],
                    "missing": ["job not found at new_root (not copied there yet)"],
                }
            )
            continue
        results.append(
            {
                "slug": e["slug"],
                "ok": vr.ok,
                "checked": vr.checked,
                "mismatched": vr.mismatched,
                "missing": vr.missing,
            }
        )

    return RelocateResult(
        old_root=str(old_root),
        new_root=str(new_root_path),
        jobs=results,
        darktable_rollpaths_stale=True,
        next_step=(
            "in darktable, import <new_root>/<YYYY>/<date slug>/raw for each job; the "
            ".xmp sidecars beside the raws carry every edit, so nothing is lost -- but "
            "darktable's film rolls hold absolute paths in library.db, which Scouter "
            "may not write"
        ),
    )


for _verb in (
    Verb(
        id="library.import",
        group="library",
        fn=import_card,
        args=(
            ArgSpec("source", "str", required=True, positional=True, help="the card's mount point"),
            ArgSpec(
                "job_slug", "str", required=True, help="job directory slug, e.g. harbour-wedding"
            ),
            ArgSpec(
                "date",
                "str",
                help="YYYY-MM-DD; default: the earliest capture time, unless it is "
                "more than 30 days from the card's median capture time (one bad "
                "clock), in which case the median is used instead, with a warning. "
                "Derived from the WHOLE card before --camera filters it, so "
                "--camera nex5n on a mixed card still takes its date from the "
                "other body's files too",
            ),
            ArgSpec(
                "camera",
                "str",
                help="import only this body's files from a mixed card; the rest are "
                "skipped and reported (every file is always labelled from its own "
                "EXIF). The job's date is still derived from the whole card, "
                "before this filter -- see --date",
            ),
            ArgSpec(
                "extensions",
                "list[str]",
                help="override import.extensions, e.g. ORF,ARW",
            ),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required: an import always touches more than one file",
            ),
        ),
        writes=True,
        gated=True,
        summary="Copy, checksum, rename and register a card into the library.",
    ),
    Verb(
        id="library.verify",
        group="library",
        fn=verify,
        args=(
            ArgSpec("job_slug", "str", required=True, positional=True, help="job slug to verify"),
            ArgSpec("date", "str", help="disambiguate when the slug matches more than one job"),
        ),
        writes=False,
        gated=False,
        summary="Re-hash raw/ against the job's checksum manifest.",
    ),
    Verb(
        id="library.register",
        group="library",
        fn=register,
        args=(
            ArgSpec("job_slug", "str", required=True, positional=True, help="job slug to register"),
            ArgSpec("date", "str", help="disambiguate when the slug matches more than one job"),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm", "bool", default=False, help="required: writes to darktable's library"
            ),
        ),
        writes=True,
        gated=True,
        summary="Re-register a job's film roll (e.g. after importing with darktable closed).",
    ),
    Verb(
        id="library.jobs",
        group="library",
        fn=jobs,
        args=(),
        writes=False,
        gated=False,
        summary="List every job under the library root with frame counts and rating histograms.",
    ),
    Verb(
        id="library.relocate",
        group="library",
        fn=relocate,
        args=(
            ArgSpec("new_root", "str", required=True, positional=True, help="new library root"),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required: rewrites the config and writes outside the library",
            ),
        ),
        writes=True,
        gated=True,
        summary="Move the library to a new root: rewrite library_root and re-verify every job.",
    ),
):
    register_verb(_verb)
