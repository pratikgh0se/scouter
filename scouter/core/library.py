"""`library.import`, `library.verify`, `library.register`: the irreversible step.

Everything downstream (cull, style, export) addresses frames by the names this
module chooses, so the algorithm follows design section 6 exactly, in order:
load the config and check the backup gate BEFORE the card is read; scan; plan
names; dry-run/confirm; create the layout; copy with two sha256 passes,
resume-safely, chmod-ing originals read-only; write the checksum manifest and
`import.json` atomically; register the film roll (tolerating darktable being
down); write the vault note (tolerating the vault not existing yet).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from scouter import __version__, config, metrics, paths
from scouter.core import dtbridge, exif
from scouter.errors import (
    BridgeTimeout,
    ChecksumMismatch,
    DestinationCollision,
    ScouterError,
    UsageError,
)
from scouter.registry import ArgSpec, Verb
from scouter.registry import register as register_verb
from scouter.types import ImportResult, RegisterResult, VerifyResult

#: Recursion bound for `_scan`. A card is a flat DCIM tree at most a handful of
#: directories deep; 8 is generous headroom without an unbounded walk.
SCAN_MAX_DEPTH = 8

#: Streaming chunk size for both hash passes and the copy itself.
CHUNK = 1 << 20


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
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_existing_manifest(scouter_dir: Path) -> list[dict]:
    """The `files[]` of an existing `.scouter/import.json`, or `[]` when the
    job has never been imported before -- also `[]` (never a crash) if the
    file is unreadable or not valid JSON, exactly like a first import."""
    path = scouter_dir / "import.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, list) else []


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
) -> tuple[str, str, str | None, str | None, list[dict], list[dict]]:
    """Read EXIF for every candidate, derive `date`/`camera` when not given,
    sort by `capture_key`, and assign one `seq` per stem (so `P1.ORF` and
    `P1.JPG` share a number). Returns
    `(date, camera, camera_model, camera_make, plan, existing_files)` --
    `plan` entries carry `Path` objects, never serialised themselves (the
    caller builds both the dry-run preview and `import.json`'s `files[]`
    from them); `existing_files` is this job's prior `import.json['files']`
    (`[]` on a first import), returned so the caller doesn't have to re-read
    it a second time to merge the manifest (fix round 1, blocker 1).

    Fix round 1, blocker 2 (stable sequence): `seq` is seeded from the
    EXISTING manifest's `source_name -> seq` mapping, not restarted at 1 --
    so removing a frame from the card, or adding a second card later, never
    renames or collides with a frame this job already assigned a number.
    """
    tagged = [(f, exif.read_tags(f)) for f in files]

    if date is None:
        times = [t.captured_at for _, t in tagged if t.captured_at]
        if not times:
            raise UsageError(
                "no capture time could be read from any file in this card; pass "
                "--date explicitly",
                detail={"files": len(files)},
            )
        date = min(times)[:10]

    # Needs `date` (just resolved above) and `job_slug`; a plain read, so
    # this is safe to do even for a dry run (ruling 3 only forbids writes).
    job_scouter_dir = config.scouter_dir(config.job_dir(cfg, date, job_slug))
    existing_files = _load_existing_manifest(job_scouter_dir)
    stem_seq: dict[str, int] = {}
    for entry in existing_files:
        stem_seq.setdefault(Path(entry["source_name"]).stem, entry["seq"])
    next_seq = max(stem_seq.values(), default=0) + 1

    if camera is None:
        slugs = {exif.camera_slug(t, cfg) for _, t in tagged}
        if len(slugs) > 1:
            raise UsageError(
                f"this card holds {len(slugs)} distinct cameras "
                f"({', '.join(sorted(slugs))}); pass --camera to choose one",
                detail={"cameras": sorted(slugs)},
            )
        camera = next(iter(slugs))
    else:
        camera = paths.validate_slug(camera)

    models = {t.model for _, t in tagged if t.model}
    makes = {t.make for _, t in tagged if t.make}
    camera_model = next(iter(models)) if len(models) == 1 else None
    camera_make = next(iter(makes)) if len(makes) == 1 else None

    ordered = sorted(tagged, key=lambda pair: exif.capture_key(pair[1], pair[0]))
    seq_digits = cfg.import_.seq_digits
    dest_sources: dict[str, Path] = {}
    plan: list[dict] = []
    for path, tags in ordered:
        stem = path.stem
        if stem not in stem_seq:
            stem_seq[stem] = next_seq
            next_seq += 1
        seq = stem_seq[stem]
        ext = path.suffix.lower()
        dest_name = f"{date}_{job_slug}_{camera}_{seq:0{seq_digits}d}{ext}"
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
                "source": path,
                "source_name": path.name,
                "dest_name": dest_name,
                "bytes": path.stat().st_size,
                "captured_at": tags.captured_at,
                "subsec": tags.subsec,
                "exposure_time": tags.exposure_time,
                "iso": tags.iso,
                "f_number": tags.f_number,
            }
        )
    return date, camera, camera_model, camera_make, plan, existing_files


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

    resolved_date, resolved_camera, camera_model, camera_make, planned, existing_files = _plan(
        files, cfg, job_slug, date, camera
    )

    plan_view = [
        {
            "seq": p["seq"],
            "source": str(p["source"]),
            "dest": f"raw/{p['dest_name']}",
            "bytes": p["bytes"],
            "camera": resolved_camera,
            "captured_at": p["captured_at"],
        }
        for p in planned
    ]
    if dry_run or not confirm:
        return ImportResult(
            outcome="dry_run",
            job_slug=job_slug,
            date=resolved_date,
            camera=resolved_camera,
            camera_model=camera_model,
            camera_make=camera_make,
            source_root=str(source_path),
            plan=plan_view,
            reason=(
                "dry run: no files were hashed and no directories were created"
                if dry_run
                else f"{len(planned)} file(s) planned; re-run with --yes / confirm=true "
                "to copy them (an import always touches more than one file)"
            ),
        )

    job_path = config.job_dir(cfg, resolved_date, job_slug)
    raw = config.raw_dir(job_path)
    scouter_d = config.scouter_dir(job_path)
    raw.mkdir(parents=True, exist_ok=True)
    scouter_d.mkdir(parents=True, exist_ok=True)
    # Fix round 1, nit: sweep `.part` leftovers from a run that died mid-copy
    # (a hard kill, a power loss) before starting -- `_copy_verified` always
    # cleans up its own tmp file on a normal return or a checksum mismatch,
    # so anything still here is from a run that never got that far.
    for stale in raw.glob(".*.part"):
        stale.unlink(missing_ok=True)

    meta = {
        "version": 1,
        "job_slug": job_slug,
        "date": resolved_date,
        "camera": resolved_camera,
        "camera_model": camera_model,
        "camera_make": camera_make,
        "imported_at": _now_z(),
        "scouter_version": __version__,
        "source_root": str(source_path),
    }

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
                else:
                    raise DestinationCollision(
                        f"{dest} already exists with different content than {src}",
                        detail={"path": str(dest), "source": str(src)},
                    )
            else:
                sha, size = _copy_verified(src, dest)
                if cfg.import_.read_only_originals:
                    os.chmod(dest, 0o444)
                status = "copied"
                copied += 1
            total_bytes += size
            files_out.append(
                {
                    "seq": p["seq"],
                    "dest": f"raw/{p['dest_name']}",
                    "source_name": p["source_name"],
                    "bytes": size,
                    "sha256": sha,
                    "captured_at": p["captured_at"],
                    "subsec": p["subsec"],
                    "exposure_time": p["exposure_time"],
                    "iso": p["iso"],
                    "f_number": p["f_number"],
                    "status": status,
                }
            )
    except Exception as e:
        # Fix round 1, should-fix 5: whatever landed THIS run before the
        # failure is still real, and the manifest must not stay silent about
        # it -- write the merged manifest now (never shrinking what a prior
        # run already recorded), and name how far this run got in the
        # error's own detail, so the agent isn't left guessing.
        totals = {
            "planned": len(planned),
            "copied": copied,
            "skipped_identical": skipped,
            "bytes": total_bytes,
        }
        _persist_manifest(scouter_d, {**meta, "totals": totals}, existing_files, files_out)
        if isinstance(e, ScouterError):
            e.detail = {
                **e.detail,
                "files_landed_this_run": len(files_out),
                "manifest_written": True,
            }
        raise

    totals = {
        "planned": len(planned),
        "copied": copied,
        "skipped_identical": skipped,
        "bytes": total_bytes,
    }
    manifest = {**meta, "totals": totals}
    merged_files = _persist_manifest(scouter_d, manifest, existing_files, files_out)

    warnings: list[str] = []
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

        vault.write_job_note(cfg, {**manifest, "files": merged_files})
    except Exception as e:  # noqa: BLE001 - task 4 hasn't landed vault.py yet
        # Until task 4 lands, `scouter.core.vault` doesn't exist and this is
        # always an ImportError; caught the same way any later vault failure
        # (a missing $OBSIDIAN_VAULT_PATH, a write error) is: a warning, never
        # a rollback of the copy that already succeeded.
        warnings.append(f"vault note not written: {e}")

    return ImportResult(
        job_slug=job_slug,
        date=resolved_date,
        camera=resolved_camera,
        camera_model=camera_model,
        camera_make=camera_make,
        source_root=str(source_path),
        files=merged_files,
        totals=totals,
        film_roll_registered=film_roll_registered,
        imported=imported_count,
        scan_incomplete=scan_incomplete,
        recursive_honoured=recursive_honoured,
        path=str(job_path),
        warnings=warnings,
    )


@metrics.instrumented("library.verify")
def verify(job_slug: str, *, date: str | None = None, job: str | None = None) -> VerifyResult:
    """Re-hash raw/ against .scouter/checksums.sha256. `.xmp` sidecars are
    excluded by SUFFIX, not by silence: they are darktable's files, they change
    whenever an edit lands, and they are never in the manifest."""
    cfg = config.load()
    job_path = config.find_job(cfg, job_slug, date)
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

    manifest: dict[str, str] = {}
    if checksums_path.is_file():
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
            ArgSpec("date", "str", help="YYYY-MM-DD; default: the earliest capture time"),
            ArgSpec("camera", "str", help="camera slug override; required for a mixed-camera card"),
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
):
    register_verb(_verb)
