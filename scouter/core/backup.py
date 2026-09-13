"""Nightly backup: stdlib `tarfile` -> `age` or `openssl enc` -> `rsync`.

Two halves, because the data has two shapes (design 14.1):

- **Small and precious**, tarred, hashed and encrypted into
  `<target>/archives/<ts>/`: every `.xmp` under the library, every job's
  `.scouter/`, `~/.config/scouter/` (config + the WB tables), `metrics.db` and
  `events.jsonl`, `<library>/scouter/memory/` (empty in phase 2, reserved for
  `Dragon Radar`), and darktable's `library.db` when `backup.include_darktable_db`
  -- copied as a file, never opened.
- **Large and reproducible-but-not-really**, rsynced straight to
  `<target>/library/`: the raws.

`openssl enc` gives confidentiality but not authentication, so the manifest
carries the plaintext sha256 and `backup.verify` decrypts and re-hashes --
that is the integrity check `openssl enc` does not provide.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scouter import __version__, metrics, paths
from scouter.config import load as load_config
from scouter.config import require_backup
from scouter.errors import EncryptionUnavailable, ScouterError, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import BackupResult, BackupVerifyResult

#: The six named checks `backup.verify` always reports, in this order,
#: whatever else does or does not run (design 14.2).
CHECK_NAMES = (
    "ciphertext_sha256",
    "decrypt",
    "plaintext_sha256",
    "member_count",
    "sampled_members",
    "sampled_raws",
)

OPENSSL_ARGS_DESC = "enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha512 -salt"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_ts() -> str:
    """Directory-safe timestamp: `2026-09-14T033000Z` (no colons)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _utc_now_z() -> str:
    """Manifest `created` field: standard ISO with colons."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _darktable_library_db() -> Path:
    """The GUI darktable's own `library.db` -- never opened, only copied.

    `$SCOUTER_DARKTABLE_LIBRARY_DB` overrides it (tests); the real default is
    darktable's macOS config location.
    """
    explicit = os.environ.get("SCOUTER_DARKTABLE_LIBRARY_DB")
    if explicit:
        return Path(explicit)
    return Path.home() / "Library" / "Application Support" / "darktable" / "library.db"


# ---------------------------------------------------------------------------
# Step 1: the member list
# ---------------------------------------------------------------------------


def _members(cfg) -> list[tuple[Path, str]]:
    """(absolute source, name inside the archive), SORTED so the archive is
    deterministic and two nights' manifests are diffable.

    Small and precious: every .xmp under the library, every job's .scouter/,
    ~/.config/scouter/ (config + the WB tables), metrics.db, events.jsonl, and
    <library>/scouter/memory/ (empty in phase 2, reserved for Dragon Radar --
    it is in the set now so phase 3 does not have to remember).
    """
    pairs: list[tuple[Path, str]] = []
    root = cfg.root()

    if root.is_dir():
        # every .xmp under the library (raw sidecars, wherever they live)
        for xmp in root.rglob("*.xmp"):
            if xmp.is_file():
                pairs.append((xmp, f"library/{xmp.relative_to(root)}"))

        # every job's .scouter/
        for scouter_dir in root.glob("*/*/.scouter"):
            if not scouter_dir.is_dir():
                continue
            for f in scouter_dir.rglob("*"):
                if f.is_file():
                    pairs.append((f, f"library/{f.relative_to(root)}"))

        # <library>/scouter/memory/ -- reserved, usually empty
        memory_dir = root / "scouter" / "memory"
        if memory_dir.is_dir():
            for f in memory_dir.rglob("*"):
                if f.is_file():
                    pairs.append((f, f"library/{f.relative_to(root)}"))

    # ~/.config/scouter/ (config + WB tables)
    cfg_dir = paths.config_dir()
    if cfg_dir.is_dir():
        for f in cfg_dir.rglob("*"):
            if f.is_file():
                pairs.append((f, f"config/{f.relative_to(cfg_dir)}"))

    # metrics.db, events.jsonl
    db_path = paths.db_path()
    if db_path.is_file():
        pairs.append((db_path, "cache/metrics.db"))
    events_path = paths.events_path()
    if events_path.is_file():
        pairs.append((events_path, "cache/events.jsonl"))

    # darktable's library.db, when configured: copied as a file, never opened.
    # sqlite3's own backup API is deliberately not used -- that would be
    # reading darktable's database, which the invariant forbids.
    if cfg.backup.include_darktable_db:
        dt_db = _darktable_library_db()
        if dt_db.is_file():
            pairs.append((dt_db, "darktable/library.db"))

    pairs.sort(key=lambda t: t[1])
    return pairs


def _member_entries(members: list[tuple[Path, str]]) -> list[dict]:
    """Per-member `{name, bytes, sha256}`, for the manifest and for `verify`'s sampling."""
    entries = []
    for src, name in members:
        entries.append({"name": name, "bytes": src.stat().st_size, "sha256": _sha256_file(src)})
    return entries


# ---------------------------------------------------------------------------
# Step 2: tar with a hash in the same pass
# ---------------------------------------------------------------------------


def _write_archive(members, out: Path) -> tuple[str, int, int]:
    """tar.gz the members, hashing the plaintext as it streams.

    stdlib `tarfile`, not the `tar` binary: we already have the exact file list,
    a stream wrapper gives us the sha256 in the same pass instead of a second
    read, and there is no shell quoting of the job directory names -- which
    contain spaces by design (spec 4.6).
    """
    digest = hashlib.sha256()

    class _Hashing:
        def __init__(self, fh):
            self.fh = fh

        def write(self, data):
            digest.update(data)
            return self.fh.write(data)

    count = 0
    with out.open("wb") as raw:
        with tarfile.open(fileobj=_Hashing(raw), mode="w:gz") as tar:
            for source, name in members:
                tar.add(source, arcname=name, recursive=False)
                count += 1
    return digest.hexdigest(), out.stat().st_size, count


# ---------------------------------------------------------------------------
# Step 3: encryption
# ---------------------------------------------------------------------------


def _openssl() -> str:
    preferred = Path("/opt/homebrew/bin/openssl")
    if preferred.exists():
        return str(preferred)
    found = shutil.which("openssl")
    if found:
        return found
    raise EncryptionUnavailable(
        "no `openssl` binary found (looked at /opt/homebrew/bin/openssl and $PATH)",
        detail={"binary": "openssl", "check": "openssl_missing"},
    )


def _run(args: list[str], *, what: str) -> subprocess.CompletedProcess:
    """Run a subprocess, turning "the binary is not there" and "it failed"
    into a typed `EncryptionUnavailable` -- never a traceback."""
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise EncryptionUnavailable(
            f"could not run {args[0]!r} to {what}: {e}",
            detail={"binary": args[0], "what": what},
        ) from e
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-500:]
        raise EncryptionUnavailable(
            f"{args[0]} failed to {what} (exit {result.returncode}): {tail}",
            detail={
                "binary": args[0],
                "what": what,
                "exit": result.returncode,
                "stderr_tail": tail,
            },
        )
    return result


def _encrypt(plain: Path, cfg) -> tuple[Path, list[str]]:
    """age when configured (authenticated, and a PUBLIC recipient key means the
    nightly launchd job holds no secret at all), else openssl enc.

    openssl enc gives CONFIDENTIALITY BUT NOT AUTHENTICATION: it cannot detect
    tampering or bit rot. That gap is closed explicitly -- the manifest records
    the sha256 of the PLAINTEXT tar and `backup.verify` decrypts and re-hashes,
    which is the integrity check openssl enc does not provide. Nothing here
    implies AEAD.
    """
    b = cfg.backup
    if b.encryption == "age":
        if not shutil.which("age"):
            raise EncryptionUnavailable(
                "`age` is not on PATH (brew install age)",
                detail={"binary": "age", "check": "age_missing"},
            )
        out = plain.with_suffix(plain.suffix + ".age")
        args = ["age", "-r", b.age_recipient, "-o", str(out), str(plain)]
    elif b.encryption == "openssl":
        out = plain.with_suffix(plain.suffix + ".enc")
        args = [
            _openssl(),
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "600000",
            "-md",
            "sha512",
            "-salt",
            "-in",
            str(plain),
            "-out",
            str(out),
            "-pass",
            f"file:{b.openssl_passfile}",
        ]
    else:
        return plain, []
    _run(args, what="encrypt")
    return out, args


def _decrypt(path: Path, cfg, out: Path) -> None:
    """The reverse of `_encrypt`. Never puts a passphrase on argv."""
    b = cfg.backup
    if path.suffix == ".age":
        if not shutil.which("age"):
            raise EncryptionUnavailable(
                "`age` is not on PATH (brew install age)",
                detail={"binary": "age", "check": "age_missing"},
            )
        # age_recipient is a PUBLIC key and cannot decrypt; an identity
        # (private key) file is needed and `backup.age_identity` is not a
        # config key yet (design's open question 2 is unresolved). This is
        # never exercised on this Mac -- age is not installed here -- but it
        # must fail with a typed error, not guess at a wrong key.
        identity = getattr(b, "age_identity", None)
        if not identity:
            raise EncryptionUnavailable(
                "decrypting an age archive needs an identity (private key) file; "
                "`backup.age_identity` is not configured (age_recipient is a "
                "PUBLIC key and cannot decrypt)",
                detail={"check": "age_identity_missing"},
            )
        args = ["age", "-d", "-i", str(identity), "-o", str(out), str(path)]
    elif path.suffix == ".enc":
        args = [
            _openssl(),
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "600000",
            "-md",
            "sha512",
            "-salt",
            "-in",
            str(path),
            "-out",
            str(out),
            "-pass",
            f"file:{b.openssl_passfile}",
        ]
    else:
        shutil.copyfile(path, out)
        return
    _run(args, what="decrypt")


# ---------------------------------------------------------------------------
# Step 4: rsync
# ---------------------------------------------------------------------------


def _rsync_args(library_root: Path, target_library: Path) -> list[str]:
    return [
        "rsync",
        "-a",
        "--partial",
        "--human-readable",
        "--stats",
        "--exclude",
        ".scouter/",
        "--exclude",
        "exports/",
        f"{library_root}/",
        f"{target_library}/",
    ]


@dataclass
class _RsyncOutcome:
    ran: bool
    exit_code: int | None
    files_transferred: int
    bytes: int
    warning: str | None


_RE_FILES_TRANSFERRED = re.compile(r"Number of (?:regular )?files transferred:\s*([\d,]+)")
_RE_BYTES_TRANSFERRED = re.compile(r"Total transferred file size:\s*([\d,]+)")


def _parse_rsync_stats(stdout: str) -> tuple[int, int]:
    files_m = _RE_FILES_TRANSFERRED.search(stdout)
    bytes_m = _RE_BYTES_TRANSFERRED.search(stdout)
    files = int(files_m.group(1).replace(",", "")) if files_m else 0
    total = int(bytes_m.group(1).replace(",", "")) if bytes_m else 0
    return files, total


def _run_rsync(library_root: Path, target_library: Path) -> _RsyncOutcome:
    """rsync the raws. A non-zero exit is a warning, not a failed backup --
    the archive is the precious half."""
    target_library.mkdir(parents=True, exist_ok=True)
    args = _rsync_args(library_root, target_library)
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError as e:
        return _RsyncOutcome(
            ran=False, exit_code=None, files_transferred=0, bytes=0, warning=f"rsync: {e}"
        )
    files, total_bytes = _parse_rsync_stats(result.stdout or "")
    warning = None
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-500:]
        warning = f"rsync exited {result.returncode}: {tail}"
    return _RsyncOutcome(
        ran=True,
        exit_code=result.returncode,
        files_transferred=files,
        bytes=total_bytes,
        warning=warning,
    )


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def _existing_archive_dirs(target: Path) -> list[Path]:
    archives = target / "archives"
    if not archives.is_dir():
        return []
    return sorted((p for p in archives.iterdir() if p.is_dir()), key=lambda p: p.name)


def _dirs_to_prune(dirs: list[Path], keep_archives: int) -> list[Path]:
    """Oldest first, and NEVER the newest two, whatever `keep_archives` says."""
    effective_keep = max(keep_archives, 2)
    if len(dirs) <= effective_keep:
        return []
    n_remove = len(dirs) - effective_keep
    return dirs[:n_remove]


def _prune_archives(target: Path, keep_archives: int) -> list[str]:
    dirs = _existing_archive_dirs(target)
    doomed = _dirs_to_prune(dirs, keep_archives)
    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)
    return [d.name for d in doomed]


# ---------------------------------------------------------------------------
# backup.run
# ---------------------------------------------------------------------------


@metrics.instrumented("backup.run")
def run(*, confirm: bool = False, dry_run: bool = False, job: str | None = None) -> BackupResult:
    job = job or current_job()
    cfg = load_config()
    b = require_backup(cfg)  # nothing is read from the library before this passes

    members = _members(cfg)
    target = Path(b.target).expanduser()
    ts = _utc_ts()
    archive_dir = target / "archives" / ts
    total_bytes = sum(src.stat().st_size for src, _ in members)

    plan = [
        {
            "action": "backup.run",
            "members": len(members),
            "plaintext_bytes_estimate": total_bytes,
            "archive_dir": str(archive_dir),
            "rsync": _rsync_args(cfg.root(), target / "library") if b.rsync_raws else None,
            "would_prune": [
                d.name for d in _dirs_to_prune(_existing_archive_dirs(target), b.keep_archives)
            ],
        }
    ]

    if dry_run or not confirm:
        return BackupResult(
            outcome="dry_run",
            members=len(members),
            plaintext_bytes=total_bytes,
            plan=plan,
            reason=(
                "dry run: nothing was archived, encrypted, rsynced or pruned"
                if dry_run
                else "re-run with --yes to back up for real"
            ),
        )

    archive_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="scouter-backup-") as scratch_name:
        scratch = Path(scratch_name)
        plain = scratch / f"scouter-{ts}.tar.gz"
        plaintext_sha256, plaintext_bytes, member_count = _write_archive(members, plain)
        member_entries = _member_entries(members)
        ms_tar = int((time.monotonic() - t0) * 1000)

        t1 = time.monotonic()
        encrypted, _enc_args = _encrypt(plain, cfg)
        ms_encrypt = int((time.monotonic() - t1) * 1000)

        suffix = ".age" if b.encryption == "age" else ".enc" if b.encryption == "openssl" else ""
        final_name = f"scouter-{ts}.tar.gz{suffix}"
        final_path = archive_dir / final_name
        tmp_final = archive_dir / f".{final_name}.part"
        shutil.copyfile(encrypted, tmp_final)
        os.replace(tmp_final, final_path)

    ciphertext_sha256 = _sha256_file(final_path)
    ciphertext_bytes = final_path.stat().st_size

    ms_rsync = 0
    rsync_outcome: _RsyncOutcome | None = None
    if b.rsync_raws:
        t2 = time.monotonic()
        rsync_outcome = _run_rsync(cfg.root(), target / "library")
        ms_rsync = int((time.monotonic() - t2) * 1000)
        if rsync_outcome.warning:
            warnings.append(rsync_outcome.warning)

    dt_db = _darktable_library_db()
    dt_db_included = b.include_darktable_db and dt_db.is_file()
    darktable_db_info = {
        "included": dt_db_included,
        "bytes": dt_db.stat().st_size if dt_db_included else None,
        "consistency": "file-copy, may be mid-write",
    }

    note = (
        "age encryption is authenticated (ChaCha20-Poly1305)"
        if b.encryption == "age"
        else (
            "openssl enc gives confidentiality, not authentication; integrity is the "
            "plaintext sha256 above"
            if b.encryption == "openssl"
            else "backup.encryption is 'none': the archive is not encrypted"
        )
    )

    manifest = {
        "version": 1,
        "created": _utc_now_z(),
        "scouter_version": __version__,
        "library_root": str(cfg.root()),
        "encryption": b.encryption,
        "openssl_args": OPENSSL_ARGS_DESC if b.encryption == "openssl" else None,
        "archive": {
            "name": final_name,
            "plaintext_sha256": plaintext_sha256,
            "plaintext_bytes": plaintext_bytes,
            "ciphertext_sha256": ciphertext_sha256,
            "ciphertext_bytes": ciphertext_bytes,
            "members": member_count,
            "authenticated": b.encryption == "age",
            "note": note,
        },
        "members": member_entries,
        "rsync": {
            "ran": bool(rsync_outcome and rsync_outcome.ran),
            "target": str(target / "library"),
            "files_transferred": rsync_outcome.files_transferred if rsync_outcome else 0,
            "bytes": rsync_outcome.bytes if rsync_outcome else 0,
            "exit": rsync_outcome.exit_code if rsync_outcome else None,
        },
        "darktable_db": darktable_db_info,
    }
    manifest_path = archive_dir / "backup.json"
    tmp_manifest = archive_dir / ".backup.json.part"
    tmp_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_manifest, manifest_path)

    pruned = _prune_archives(target, b.keep_archives)

    return BackupResult(
        archive_dir=str(archive_dir),
        manifest_path=str(manifest_path),
        members=member_count,
        plaintext_bytes=plaintext_bytes,
        ciphertext_bytes=ciphertext_bytes,
        encryption=b.encryption,
        rsync_ran=bool(rsync_outcome and rsync_outcome.ran),
        rsync_files=rsync_outcome.files_transferred if rsync_outcome else 0,
        rsync_bytes=rsync_outcome.bytes if rsync_outcome else 0,
        rsync_exit=rsync_outcome.exit_code if rsync_outcome else None,
        pruned=pruned,
        ms_tar=ms_tar,
        ms_encrypt=ms_encrypt,
        ms_rsync=ms_rsync,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# backup.verify
# ---------------------------------------------------------------------------


def _newest_archive_ts(archives_dir: Path) -> str:
    if not archives_dir.is_dir():
        raise UsageError(
            f"no archives directory at {archives_dir}: run `backup run --yes` first",
            detail={"archives_dir": str(archives_dir)},
        )
    names = sorted(p.name for p in archives_dir.iterdir() if p.is_dir())
    if not names:
        raise UsageError(
            f"no archives under {archives_dir}: run `backup run --yes` first",
            detail={"archives_dir": str(archives_dir)},
        )
    return names[-1]


def _collect_raw_checksums(root: Path, n: int) -> list[tuple[str, str]]:
    """N raw checksums from the jobs' `import.json`, relative to `library_root`."""
    if not root.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    for import_json in root.glob("*/*/.scouter/import.json"):
        try:
            data = json.loads(import_json.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        job_dir = import_json.parent.parent
        try:
            rel_job = job_dir.relative_to(root)
        except ValueError:
            continue
        for f in data.get("files", []):
            dest, sha = f.get("dest"), f.get("sha256")
            if dest and sha:
                entries.append((str(rel_job / dest), sha))
    if not entries:
        return []
    return random.sample(entries, min(n, len(entries)))


@metrics.instrumented("backup.verify")
def verify(
    *, archive: str | None = None, sample: int = 10, job: str | None = None
) -> BackupVerifyResult:
    """Read-only outside its own temp directory."""
    job = job or current_job()
    cfg = load_config()
    b = cfg.backup
    if not b.target:
        raise UsageError(
            "`backup.target` is not set: nothing to verify",
            detail={"check": "no_target"},
        )
    target = Path(b.target).expanduser()
    archives_dir = target / "archives"
    ts = archive or _newest_archive_ts(archives_dir)
    archive_dir = archives_dir / ts
    manifest_path = archive_dir / "backup.json"

    checks: dict[str, dict] = {
        name: {"name": name, "ok": False, "detail": {}} for name in CHECK_NAMES
    }
    sampled_members: list[dict] = []
    sampled_raws: list[dict] = []

    if not manifest_path.is_file():
        for name in CHECK_NAMES:
            checks[name]["detail"] = {"reason": f"no manifest at {manifest_path}"}
        ordered = [checks[name] for name in CHECK_NAMES]
        return BackupVerifyResult(
            outcome="ok",
            verified=False,
            archive=ts,
            checks=ordered,
            sampled_members=sampled_members,
            sampled_raws=sampled_raws,
            reason=f"no manifest at {manifest_path}",
        )

    manifest = json.loads(manifest_path.read_text())
    archive_info = manifest.get("archive", {})
    ciphertext_path = archive_dir / archive_info.get("name", "")

    decrypt_ok = False
    with tempfile.TemporaryDirectory(prefix="scouter-verify-") as scratch_name:
        scratch = Path(scratch_name)

        if not ciphertext_path.is_file():
            checks["ciphertext_sha256"]["detail"] = {"reason": f"missing: {ciphertext_path}"}
        else:
            actual_cipher = _sha256_file(ciphertext_path)
            expected_cipher = archive_info.get("ciphertext_sha256")
            checks["ciphertext_sha256"]["ok"] = actual_cipher == expected_cipher
            checks["ciphertext_sha256"]["detail"] = {
                "expected": expected_cipher,
                "actual": actual_cipher,
            }

            plain_out = scratch / "plain.tar.gz"
            try:
                _decrypt(ciphertext_path, cfg, plain_out)
                decrypt_ok = True
                checks["decrypt"]["ok"] = True
            except ScouterError as e:
                checks["decrypt"]["detail"] = {"error": e.message}

            if decrypt_ok:
                actual_plain = _sha256_file(plain_out)
                expected_plain = archive_info.get("plaintext_sha256")
                checks["plaintext_sha256"]["ok"] = actual_plain == expected_plain
                checks["plaintext_sha256"]["detail"] = {
                    "expected": expected_plain,
                    "actual": actual_plain,
                }

                try:
                    with tarfile.open(plain_out, "r:gz") as tar:
                        names = tar.getnames()
                    expected_members = archive_info.get("members")
                    checks["member_count"]["ok"] = len(names) == expected_members
                    checks["member_count"]["detail"] = {
                        "expected": expected_members,
                        "actual": len(names),
                    }

                    manifest_members = manifest.get("members", [])
                    sample_n = min(sample, len(manifest_members))
                    chosen = random.sample(manifest_members, sample_n) if sample_n else []
                    with tarfile.open(plain_out, "r:gz") as tar:
                        for entry in chosen:
                            try:
                                fh = tar.extractfile(entry["name"])
                                data = fh.read() if fh is not None else b""
                                actual = _sha256_bytes(data)
                                ok = actual == entry.get("sha256")
                            except KeyError:
                                ok, actual = False, None
                            sampled_members.append(
                                {
                                    "name": entry["name"],
                                    "ok": ok,
                                    "expected": entry.get("sha256"),
                                    "actual": actual,
                                }
                            )
                    checks["sampled_members"]["ok"] = all(m["ok"] for m in sampled_members)
                    checks["sampled_members"]["detail"] = {"count": len(sampled_members)}
                except tarfile.TarError as e:
                    checks["member_count"]["detail"] = {"error": str(e)}
                    checks["sampled_members"]["detail"] = {"error": str(e)}
            else:
                for name in ("plaintext_sha256", "member_count", "sampled_members"):
                    checks[name]["detail"] = {"reason": "decrypt failed"}

    raws = _collect_raw_checksums(cfg.root(), sample)
    for rel_name, expected_sha in raws:
        copy_path = target / "library" / rel_name
        if not copy_path.is_file():
            sampled_raws.append({"name": rel_name, "ok": False, "reason": "missing"})
            continue
        actual = _sha256_file(copy_path)
        sampled_raws.append(
            {
                "name": rel_name,
                "ok": actual == expected_sha,
                "expected": expected_sha,
                "actual": actual,
            }
        )
    checks["sampled_raws"]["ok"] = all(r["ok"] for r in sampled_raws) if sampled_raws else True
    checks["sampled_raws"]["detail"] = {"count": len(sampled_raws)}

    ordered = [checks[name] for name in CHECK_NAMES]
    verified = all(c["ok"] for c in ordered)
    failing = [c["name"] for c in ordered if not c["ok"]]

    return BackupVerifyResult(
        outcome="ok",
        verified=verified,
        archive=ts,
        checks=ordered,
        sampled_members=sampled_members,
        sampled_raws=sampled_raws,
        warnings=[f"failing checks: {', '.join(failing)}"] if failing else [],
    )


register(
    Verb(
        id="backup.run",
        group="backup",
        fn=run,
        args=(
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required: writes outside the library and prunes old archives",
            ),
        ),
        writes=True,
        gated=True,
        summary="Tar + encrypt the small-and-precious set, rsync the raws, prune old archives.",
    )
)
register(
    Verb(
        id="backup.verify",
        group="backup",
        fn=verify,
        args=(
            ArgSpec("archive", "str", help="archive timestamp to verify (default: newest)"),
            ArgSpec("sample", "int", default=10, help="how many members/raws to re-hash"),
        ),
        writes=False,
        gated=False,
        summary="Decrypt an archive, re-hash it, and sample-check the rsync copy.",
    )
)
