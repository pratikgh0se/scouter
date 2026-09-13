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
from scouter.errors import BridgeTimeout, ChecksumMismatch, DestinationCollision, UsageError
from scouter.registry import ArgSpec, Verb
from scouter.registry import register as register_verb
from scouter.types import ImportResult, RegisterResult, VerifyResult

#: Recursion bound for `_scan`. A card is a flat DCIM tree at most a handful of
#: directories deep; 8 is generous headroom without an unbounded walk.
SCAN_MAX_DEPTH = 8

#: Streaming chunk size for both hash passes and the copy itself.
CHUNK = 1 << 20


def _scan(source: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Recursive, depth-bounded, symlinks not followed. Sorted for determinism."""
    wanted = {("." + e.lower()) for e in extensions}
    found: list[Path] = []
    stack = [(source, 0)]
    while stack:
        directory, depth = stack.pop()
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink():
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


def _plan(
    files: list[Path],
    cfg: config.Config,
    job_slug: str,
    date: str | None,
    camera: str | None,
) -> tuple[str, str, str | None, str | None, list[dict]]:
    """Read EXIF for every candidate, derive `date`/`camera` when not given,
    sort by `capture_key`, and assign one `seq` per stem (so `P1.ORF` and
    `P1.JPG` share a number). Returns
    `(date, camera, camera_model, camera_make, plan)` -- `plan` entries carry
    `Path` objects, never serialised themselves (the caller builds both the
    dry-run preview and `import.json`'s `files[]` from them)."""
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
    stem_seq: dict[str, int] = {}
    seen_dest: set[str] = set()
    plan: list[dict] = []
    for path, tags in ordered:
        stem = path.stem
        if stem not in stem_seq:
            stem_seq[stem] = len(stem_seq) + 1
        seq = stem_seq[stem]
        ext = path.suffix.lower()
        dest_name = f"{date}_{job_slug}_{camera}_{seq:0{seq_digits}d}{ext}"
        # Impossible by construction (one name per (date, job_slug, camera, seq,
        # ext) and ext is lowercased once here) -- asserted anyway, per the brief.
        assert dest_name not in seen_dest, f"duplicate destination name in plan: {dest_name}"
        seen_dest.add(dest_name)
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
    return date, camera, camera_model, camera_make, plan


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

    resolved_date, resolved_camera, camera_model, camera_make, planned = _plan(
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

    files_out: list[dict] = []
    copied = 0
    skipped = 0
    total_bytes = 0
    for p in planned:
        src: Path = p["source"]
        dest = raw / p["dest_name"]
        if dest.exists():
            # Resume-safety: never overwrite. A 0444 destination from a
            # previous run is still readable, so it is hashable without any
            # special-casing -- and it is NOT re-chmod-ed here, only in the
            # `else` branch below where a fresh copy just landed.
            existing_hash = _hash_file(dest)
            source_hash = _hash_file(src)
            if existing_hash == source_hash:
                sha = existing_hash
                size = dest.stat().st_size
                status = "skipped_identical" if cfg.import_.skip_identical else "already_present"
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

    checksum_lines = "".join(
        f"{f['sha256']}  {f['dest']}\n" for f in sorted(files_out, key=lambda f: f["dest"])
    )
    _write_atomic(scouter_d / "checksums.sha256", checksum_lines)

    totals = {
        "planned": len(planned),
        "copied": copied,
        "skipped_identical": skipped,
        "bytes": total_bytes,
    }
    manifest = {
        "version": 1,
        "job_slug": job_slug,
        "date": resolved_date,
        "camera": resolved_camera,
        "camera_model": camera_model,
        "camera_make": camera_make,
        "imported_at": _now_z(),
        "scouter_version": __version__,
        "source_root": str(source_path),
        "files": files_out,
        "totals": totals,
    }
    _write_atomic(scouter_d / "import.json", json.dumps(manifest, indent=2))

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
        film_roll_registered = True
        imported_count = int(result.get("imported", 0))
        scan_incomplete = result.get("scan_incomplete")
        recursive_honoured = result.get("recursive_honoured")
        if result.get("note"):
            warnings.append(str(result["note"]))

    try:
        from scouter.core import vault

        vault.write_job_note(cfg, manifest)
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
        files=files_out,
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
    warnings = [str(result["note"])] if result.get("note") else []
    return RegisterResult(
        job_slug=job_slug,
        film_roll_registered=True,
        imported=int(result.get("imported", 0)),
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
