# Scouter Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not start task N+1 until task N's tests pass. One git worktree per task (phase 1's shared checkout caused two clobbers); **`git stash` is forbidden**.

**Goal:** Make a *job* deliverable. A card becomes a named, checksummed, backed-up film roll; a stats-only proposal culls it; one hero's recipe becomes a real filtered `.dtstyle` that lands on the whole set; the frames outside tolerance are listed by number; proofs and finals leave with a checksum manifest; a nightly encrypted backup protects all of it. Every step is a deterministic core function that runs with **no model**, and the whole path is proven by a 30-frame synthetic job driven from the CLI on this Mac.

**Architecture:** Phase 1's three layers over one core are unchanged. Phase 2 adds a configuration layer (`scouter/config.py`) above the core, a library layer beside it (`scouter/core/{exif,library,vault}.py`), the volume engines (`scouter/core/{cull,dtstyle,wb,backup}.py`), and extends `core/{style,export,scout,history,darkroom,xmp}.py`. Three new Lua bridge verbs (`style.export_file`, `style.import_file`, `image.rate`) and two new params on `history.commit`. Edits still go out through `dt.gui.action` in the running darktable, values still come back from the XMP sidecar, pixels still come from `darktable-cli` in an isolated configdir, and `library.db` is still never read or written. Spec: `docs/superpowers/specs/2026-09-14-scouter-phase2-design.md`.

**Tech Stack:** Python 3.10+ (**stdlib only** — `json`, `sqlite3`, `struct`, `zlib`, `hashlib`, `shutil`, `tarfile`, `concurrent.futures`, `argparse`, `xml.etree`, `subprocess`, `dataclasses`, `unittest`) plus `mcp>=2,<3` for the MCP adapter only. **No** Pillow, rawpy, numpy, tomllib (3.11+, above our 3.10 floor), exiftool. External binaries used through `subprocess`: `darktable-cli`, `rsync` (`/usr/bin/rsync`), `openssl` (`/opt/homebrew/bin/openssl`, OpenSSL 3.6.3), `age` when present (it is not, today), `plutil` (template lint only). Lua 5.4 (whatever darktable ships). darktable 5.4.1 (Lua API 9.6.0) at `/Applications/darktable.app/Contents/MacOS/darktable`.

**Spec:** `docs/superpowers/specs/2026-09-14-scouter-phase2-design.md` (this phase), `docs/superpowers/specs/2026-09-13-scouter-phase1-design.md` (what exists), `~/second-brain/Specs/darkroom-agent.md` (system spec, approved row 275), `~/second-brain/Knowledge/Scouter - Phase 1 build and live findings 2026-09-14.md` (proven darktable facts), `.superpowers/sdd/2026-09-13-scouter-phase1/progress.md` (rulings 1-35, binding).

---

## Global Constraints

These are binding on every task. They are reproduced verbatim from the system spec, phase 1's rulings and Pratik's standing rules; do not "improve" them mid-implementation.

- Three layers over one core: a `scouter/` Python package of plain typed functions (no model calls), a `scouter` CLI (argparse, JSON on stdout, exit codes, `--dry-run` on every writer), and the MCP server as a thin adapter. Same verb names/args/result shape in all three. Nothing is CLI-only or MCP-only.
- Dependencies: stdlib plus `mcp>=2,<3` only. No Pillow/rawpy/numpy. Previews and stats come from `darktable-cli` PPM/PNG renders parsed in pure Python.
- Every core function emits one metrics event (JSON line to `~/.cache/scouter/events.jsonl` and a row in `~/.cache/scouter/metrics.db` via sqlite3) with: tool, layer, args digest, image ids, ms, outcome (ok / error class / dry-run), bytes in/out, estimated tokens (bytes/4), job id. A test asserts every registered tool emits an event (a tool without an event fails CI). Audit log per job (`~/.cache/scouter/audit/<job>.jsonl`). One event per **top-level** verb: nested core calls go through `__wrapped__` (ruling 23).
- Confirm gates: any write to more than one image, history overwrite, delete/reject, export over existing files require `confirm=True` (MCP) / `--yes` (CLI); dry-run output first. Phase 2 adds: anything that writes **outside the library** is gated too.
- Dry runs never open, import or write (ruling 24). A dry run resolves images through `image.lookup` only and never moves the darkroom.
- The agent never renders its own pixels; darktable is reached only via the Lua API and darktable-cli; never write `library.db` directly. XMP sidecars are the read path for history/params.
- Originals are read-only: the import sets the copied raws to `0444` (spec 6). Their `.xmp` sidecars are left writable — darktable must write those.
- `library.import` **refuses** unless a backup target is configured (spec 4.6). The refusal happens before any file on the card is read.
- Honest limits are surfaced in the result, never hidden: no guessed parameter value, no phantom success, no invented capability. When a capability does not exist, the verb refuses with a typed code and names what does exist. Never overwrite blind — an existing file, manifest, style or vault note is appended to or refused, never silently replaced.
- Error classes are typed, each with a stable `code`, a `fix` string, an `EXIT_CODES` row and an error-path test: phase 1's seven plus `backup_not_configured`, `checksum_mismatch`, `destination_collision`, `exif_unreadable`, `style_write_failed`, `encryption_unavailable`.
- Ruling 30 stands: a darkroom write refuses when `darkroom.current` differs from the job's last opened image.
- Tests: `unittest.TestCase` classes run by `uv run --extra dev python -m pytest tests -q`. New tests must not require darktable. Host-only tests are gated by `SCOUTER_HOST_TESTS=1` **and** the darktable binary's presence (ruling 17), print their skip reason, launch darktable with the file argument, wait for the bridge, `history.commit` before quitting (ruling 16), and quit gracefully (`osascript` → `SIGTERM` → `SIGKILL`) — never leaving darktable running.
- The golden MCP schema snapshot stays, and is regenerated exactly **once**, deliberately, in task 14, with the diff reviewed in that commit.
- Storage: phase 2 runs on the internal disk with `~/Pictures/EditingTrial` as the raw source until the SSD arrives. The job layout must move to the SSD without re-importing Scouter's state.
- `docs/measurements.md` is updated **by hand**, in a commit that explains the change; host runs write `docs/measurements-generated.md`.

---

## File map

**Create:**

- `scouter/config.py`
- `scouter/core/exif.py`, `library.py`, `vault.py`, `cull.py`, `dtstyle.py`, `wb.py`, `backup.py`
- `scouter/cli/tables.py`
- `ops/launchd/com.pratik.scouter-backup.plist.template`, `ops/install-backup-job.sh`
- `spike/scouter_wb_probe.lua`
- `tests/make_synthetic_job.py`
- `tests/fixtures/exif-orf-header.bin`, `tests/fixtures/hero.dtstyle`, `tests/fixtures/wb-em10iii.json`
- `tests/test_config.py`, `test_exif.py`, `test_library.py`, `test_vault.py`, `test_stats_many.py`, `test_cull.py`, `test_style_dtstyle.py`, `test_style_set.py`, `test_outliers.py`, `test_wb.py`, `test_export_release.py`, `test_history_changed.py`, `test_backup.py`, `test_ops_launchd.py`, `test_host_phase2.py`

**Modify:**

- `scouter/__init__.py` — `PHASE = 2`, `__version__ = "0.2.0"`.
- `scouter/paths.py` — `config_path`, `config_dir`, `styles_dir`, `stats_dir`, `wb_dir`, `validate_slug`.
- `scouter/errors.py` — six new classes, `EXIT_CODES` rows 11-16 plus `"partial": 9`.
- `scouter/types.py` — 16 new result dataclasses; `SetResult.noop`; `StyleResult.dtstyle_path/operations_included/modules_missing`.
- `scouter/registry.py` — `_CORE_MODULES` gains `scouter.config` and the four new verb-registering core modules (`library`, `cull`, `wb`, `backup`; `exif`, `vault` and `dtstyle` register no verbs and are imported by their callers).
- `scouter/core/xmp.py` — `latest_params`, `active_entries`, `rating`.
- `scouter/core/scout.py` — `stats_many`, cache generalisation of `_prune_preview_cache`.
- `scouter/core/history.py` — `commit(attempts=, expect_change=)`, `changed`, watermark helpers.
- `scouter/core/darkroom.py` — no-op pre-check in `set_param`; full coefficient vector in `_before_state`; temperature undo.
- `scouter/core/style.py` — real filter in `extract`; `apply_set`; `outliers`.
- `scouter/core/export.py` — two new presets, `preset_args`, `release`, `verify_manifest`.
- `scouter/core/dtbridge.py` — `ACTION_PATHS` entries for the WB coefficient sliders (task 11 pins them).
- `scouter/mcp/schemas.py` — 18 new tools, four new shared fragments.
- `scouter/cli/main.py` — `--pretty` table rendering for three verbs; `doctor` gains four lines.
- `darktable_mcp/lua/darktable_mcp.lua` — `style.export_file`, `style.import_file`, `image.rate`; `history.commit` params.
- `darktable_mcp/bridge/client.py` — three new `DEFAULT_TIMEOUTS` entries; `style.apply` 60 → 300.
- `darktable_mcp/darktable/cli_wrapper.py` — `export_image(..., extra_args=None)`.
- `tests/lua/test_dispatcher.lua`, `tests/test_lua_dispatcher.py` — stubs and assertions for the new methods.
- `tests/scouter_fakes.py` — `FakeBridge` gains the new methods and a call counter; `fake_cli` gains `extra_args` capture; a `config_fixture()` helper.
- `tests/fixtures/scouter_mcp_schemas.json` — regenerated in task 14 only.
- `docs/measurements.md`, `README.md` — task 14.
- `pyproject.toml` — nothing required; verify `scouter*` still covers the new modules.

---

### Task 1: Config, the backup gate, the job layout, and the new error classes

**Why first:** every other task needs `Config`, and the import gate is the one refusal the system spec states as a hard requirement. Nothing may read a card before this exists.

**Files:**
- Create: `scouter/config.py`, `tests/test_config.py`
- Modify: `scouter/paths.py`, `scouter/errors.py`, `scouter/types.py`, `scouter/registry.py`, `scouter/__init__.py`, `tests/scouter_fakes.py`

**Interfaces:**
- Produces: `config.load() -> Config`, `config.require_backup(cfg) -> BackupConfig`, `config.job_dir(cfg, date, slug) -> Path`, `config.find_job(cfg, slug, date=None) -> Path`, `paths.validate_slug(slug) -> str`, verbs `config.init` and `config.show`, and `errors.BackupNotConfigured` + five siblings.
- Consumes: `scouter.paths`, `scouter.errors`, `scouter.registry`.

- [ ] **Step 1: Add the six error classes and the exit-code rows**

In `scouter/errors.py`, after `CommitTimeout`:

```python
class BackupNotConfigured(ScouterError):
    code = "backup_not_configured"
    fix = (
        "configure a backup target before importing: run `scouter config init`, then set "
        "`backup.enabled` and `backup.target` in the config file (the check that failed is "
        "in `detail.check`)"
    )


class ChecksumMismatch(ScouterError):
    code = "checksum_mismatch"
    fix = (
        "the bytes on disk do not match the recorded sha256; see `detail.expected` and "
        "`detail.actual`. Re-copy from the source, or check the disk before trusting the job"
    )


class DestinationCollision(ScouterError):
    code = "destination_collision"
    fix = (
        "a different file already occupies that name (see `detail.path`); nothing was "
        "overwritten. Import with a different --job-slug, or move the existing file aside"
    )


class ExifUnreadable(ScouterError):
    code = "exif_unreadable"
    fix = (
        "pass --camera <slug> for this card, or add the model to `cameras` in the config; "
        "`detail.magic` shows what the file's TIFF header actually said"
    )


class StyleWriteFailed(ScouterError):
    code = "style_write_failed"
    fix = (
        "darktable refused the .dtstyle at `detail.path`, which is kept for inspection; "
        "re-run `style extract` with --no-filter for a whole-history style"
    )


class EncryptionUnavailable(ScouterError):
    code = "encryption_unavailable"
    fix = (
        "install age (`brew install age`) and set `backup.age_recipient`, or create the "
        "openssl passfile at `detail.passfile` with mode 0600"
    )
```

Extend the tables (keep the existing rows untouched):

```python
EXIT_CODES: dict[str, int] = {
    ...,
    "style_partial": 9,
    # `style.apply_set` reports a partial set as a RESULT (outcome="partial") rather
    # than an exception, so the successes survive; it shares 9 so one shell branch
    # covers "some of it worked" however it arrived.
    "partial": 9,
    "commit_timeout": 10,
    "backup_not_configured": 11,
    "checksum_mismatch": 12,
    "destination_collision": 13,
    "exif_unreadable": 14,
    "style_write_failed": 15,
    "encryption_unavailable": 16,
}

ERROR_CLASSES: tuple[type[ScouterError], ...] = (
    ..., BackupNotConfigured, ChecksumMismatch, DestinationCollision,
    ExifUnreadable, StyleWriteFailed, EncryptionUnavailable,
)
```

- [ ] **Step 2: Add the new paths and the slug validator**

In `scouter/paths.py`:

```python
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
```

- [ ] **Step 3: Write `scouter/config.py` — the frozen dataclass tree and `DEFAULTS`**

```python
"""Scouter's configuration: JSON, because tomllib is 3.11+ and our floor is 3.10.

One frozen dataclass tree, one `DEFAULTS` dict, and a loader that REJECTS unknown
keys. A typo in `backup.target` must not silently disarm the import gate, so an
unrecognised key is a `usage` error naming the nearest known one.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from scouter import paths
from scouter.errors import BackupNotConfigured, EncryptionUnavailable, UsageError

CONFIG_VERSION = 1


@dataclass(frozen=True)
class ImportConfig:
    extensions: tuple[str, ...] = ("ORF", "ARW", "DNG", "JPG", "JPEG")
    read_only_originals: bool = True
    seq_digits: int = 4
    skip_identical: bool = True


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = False
    target: str | None = None
    rsync_raws: bool = True
    encryption: str = "openssl"          # "age" | "openssl" | "none"
    age_recipient: str | None = None
    openssl_passfile: str | None = None
    keep_archives: int = 14
    allow_unencrypted: bool = False
    include_darktable_db: bool = True


@dataclass(frozen=True)
class Tolerances:
    exposure_ev: float = 0.3
    temperature_k: int = 300
    clipped_high_pct: float = 1.0
    wb_ratio: float = 0.03


@dataclass(frozen=True)
class CullConfig:
    burst_gap_s: float = 2.0
    sharpness_floor: float = 0.20
    luminance_low: int = 25
    luminance_high: int = 230
    clipped_flag_pct: float = 5.0
    pick_rating: int = 2
    keep_rating: int = 1


@dataclass(frozen=True)
class StyleConfig:
    never_include: tuple[str, ...] = (
        "crop", "clipping", "retouch", "spots", "flip",
        "rotatepixels", "censorize", "watermark", "ashift",
    )


@dataclass(frozen=True)
class ExportConfig:
    min_rating: int = 2
    final_label: str | None = None


@dataclass(frozen=True)
class StatsConfig:
    set_px: int = 512
    workers: int = 4
    cache_max_age_days: int = 30


@dataclass(frozen=True)
class VaultConfig:
    jobs_dir: str = "Projects/Photo jobs"


@dataclass(frozen=True)
class Config:
    version: int = CONFIG_VERSION
    library_root: str | None = None
    path: str | None = None              # where this config was loaded from, None = defaults
    exists: bool = False
    import_: ImportConfig = field(default_factory=ImportConfig)
    cameras: tuple[tuple[str, str], ...] = ()   # tuple of pairs: frozen and ordered
    vault: VaultConfig = field(default_factory=VaultConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    tolerances: Tolerances = field(default_factory=Tolerances)
    cull: CullConfig = field(default_factory=CullConfig)
    style: StyleConfig = field(default_factory=StyleConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)

    def camera_map(self) -> dict[str, str]:
        return dict(self.cameras)

    def root(self) -> Path:
        if not self.library_root:
            raise UsageError(
                "no `library_root` in the config: every library verb needs one",
                detail={"config": self.path or str(paths.config_path())},
            )
        return Path(self.library_root).expanduser()
```

`SECTIONS` maps each section name to its dataclass so the loader is table-driven:

```python
SECTIONS = {
    "import": ("import_", ImportConfig), "vault": ("vault", VaultConfig),
    "backup": ("backup", BackupConfig), "tolerances": ("tolerances", Tolerances),
    "cull": ("cull", CullConfig), "style": ("style", StyleConfig),
    "export": ("export", ExportConfig), "stats": ("stats", StatsConfig),
}
TOP_KEYS = {"version", "library_root", "cameras", "_comment", *SECTIONS}
```

- [ ] **Step 4: Write the loader with unknown-key rejection**

```python
def _reject_unknown(where: str, given: set[str], known: set[str]) -> None:
    unknown = sorted(given - known)
    if not unknown:
        return
    guesses = {k: (difflib.get_close_matches(k, sorted(known), n=1) or [None])[0]
               for k in unknown}
    raise UsageError(
        f"unknown config key(s) in {where}: " + ", ".join(
            f"{k!r}" + (f" (did you mean {g!r}?)" if g else "") for k, g in guesses.items()
        ),
        detail={"section": where, "unknown": unknown, "known": sorted(known)},
    )


def load(path: str | None = None) -> Config:
    """Read the config, or return defaults when the file is absent (not an error)."""
    cfg_path = Path(path).expanduser() if path else paths.config_path()
    if not cfg_path.is_file():
        return Config(path=str(cfg_path), exists=False)
    try:
        raw = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        raise UsageError(
            f"{cfg_path} is not valid JSON: {e}",
            detail={"config": str(cfg_path), "line": e.lineno, "column": e.colno},
        ) from e
    if not isinstance(raw, dict):
        raise UsageError(f"{cfg_path} must hold a JSON object", detail={"config": str(cfg_path)})
    _reject_unknown(str(cfg_path), set(raw) - {"_comment"}, TOP_KEYS)

    kwargs: dict = {"path": str(cfg_path), "exists": True,
                    "version": int(raw.get("version", CONFIG_VERSION))}
    if raw.get("library_root"):
        kwargs["library_root"] = str(Path(str(raw["library_root"])).expanduser())
    if "cameras" in raw:
        cameras = raw["cameras"]
        if not isinstance(cameras, dict):
            raise UsageError("`cameras` must be an object of model -> slug",
                             detail={"config": str(cfg_path)})
        kwargs["cameras"] = tuple((str(k), paths.validate_slug(str(v)))
                                  for k, v in sorted(cameras.items()))
    for name, (attr, klass) in SECTIONS.items():
        if name not in raw:
            continue
        section = raw[name]
        if not isinstance(section, dict):
            raise UsageError(f"config section `{name}` must be an object",
                             detail={"config": str(cfg_path)})
        fields = {f.name for f in klass.__dataclass_fields__.values()}
        _reject_unknown(f"{cfg_path}:{name}", set(section) - {"_comment"}, fields)
        built = {}
        for key, value in section.items():
            if key == "_comment":
                continue
            built[key] = tuple(value) if isinstance(value, list) else value
        kwargs[attr] = klass(**built)
    return Config(**kwargs)
```

- [ ] **Step 5: Write the backup gate — one code, a named check per condition**

```python
#: Every check the gate runs, in order. `library.import` and `backup.run` call it;
#: reads, cull, style, outliers and export deliberately DO NOT -- blocking a proof
#: export because a backup disk is unplugged would be gate theatre (design 4.3).
def require_backup(cfg: Config) -> BackupConfig:
    b = cfg.backup

    def refuse(check: str, message: str, **detail) -> None:
        raise BackupNotConfigured(
            message, detail={"check": check, "config": cfg.path or str(paths.config_path()),
                             **detail})

    if not cfg.exists:
        refuse("config_missing",
               f"no Scouter config at {cfg.path}: nothing says where backups go")
    if not b.enabled:
        refuse("disabled", "`backup.enabled` is false: importing would leave the job unprotected")
    if not b.target:
        refuse("no_target", "`backup.target` is not set")
    target = Path(b.target).expanduser()
    if not target.parent.exists():
        refuse("target_parent_missing",
               f"{target}'s parent does not exist (an unplugged disk looks exactly like this)",
               target=str(target))
    probe = target if target.exists() else target.parent
    if not os.access(probe, os.W_OK):
        refuse("target_not_writable", f"{probe} is not writable", target=str(target))
    if b.encryption == "none" and not b.allow_unencrypted:
        refuse("unencrypted",
               "`backup.encryption` is \"none\"; the spec calls for an encrypted archive. "
               "Set `backup.allow_unencrypted` to true to accept that deliberately")
    if b.encryption == "age":
        if not b.age_recipient:
            refuse("age_recipient_missing", "`backup.encryption` is \"age\" but "
                   "`backup.age_recipient` is not set")
        if not shutil.which("age"):
            refuse("age_missing", "`age` is not on PATH (brew install age)")
    elif b.encryption == "openssl":
        if not b.openssl_passfile:
            refuse("passfile_missing", "`backup.openssl_passfile` is not set")
        pf = Path(b.openssl_passfile).expanduser()
        if not pf.is_file() or pf.stat().st_size == 0:
            refuse("passfile_absent", f"{pf} is missing or empty", passfile=str(pf))
        mode = pf.stat().st_mode & 0o777
        if mode & 0o077:
            refuse("passfile_mode", f"{pf} has mode {mode:04o}; it must be 0600",
                   passfile=str(pf), mode=f"{mode:04o}")
    return b
```

- [ ] **Step 6: Job-layout helpers**

```python
def job_dir(cfg: Config, date: str, slug: str) -> Path:
    """`<library_root>/<YYYY>/<YYYY-MM-DD slug>` -- a SPACE, exactly as spec 4.6 writes it."""
    paths.validate_slug(slug)
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise UsageError(f"date must be YYYY-MM-DD, got {date!r}", detail={"date": date})
    return cfg.root() / date[:4] / f"{date} {slug}"


def scouter_dir(job_path: Path) -> Path:
    return job_path / ".scouter"


def raw_dir(job_path: Path) -> Path:
    return job_path / "raw"


def exports_dir(job_path: Path, preset: str) -> Path:
    return job_path / "exports" / preset


def find_job(cfg: Config, slug: str, date: str | None = None) -> Path:
    """Locate a job by slug when the date is not given: one match, or a usage error."""
    if date:
        return job_dir(cfg, date, slug)
    paths.validate_slug(slug)
    matches = sorted(p for p in cfg.root().glob(f"*/* {slug}")
                     if p.is_dir() and p.name.endswith(f" {slug}"))
    if not matches:
        raise UsageError(f"no job with slug {slug!r} under {cfg.root()}",
                         detail={"slug": slug, "library_root": str(cfg.root())})
    if len(matches) > 1:
        raise UsageError(
            f"{len(matches)} jobs share the slug {slug!r}; pass --date",
            detail={"slug": slug, "candidates": [p.name for p in matches]})
    return matches[0]
```

- [ ] **Step 7: The two verbs, `config.init` and `config.show`**

`init` writes the starter file with `backup.enabled: false` (so the very first import fails loudly), auto-selects `age` when it is on `PATH`, and refuses to clobber an existing file without `confirm`:

```python
@metrics.instrumented("config.init")
def init(path: str | None = None, *, confirm: bool = False, dry_run: bool = False,
         job: str | None = None) -> ConfigResult:
    target = Path(path).expanduser() if path else paths.config_path()
    exists = target.is_file()
    has_age = bool(shutil.which("age"))
    starter = {
        "_comment": "Scouter config. Docs: docs/superpowers/specs/2026-09-14-scouter-phase2-design.md section 4",
        "version": CONFIG_VERSION,
        "library_root": str(Path.home() / "Pictures" / "EditingTrial-library"),
        "cameras": {"E-M10 Mark III": "em10iii", "ILCE-7": "a7"},
        "backup": {
            "_comment": "library.import REFUSES until enabled is true and target passes the gate",
            "enabled": False, "target": None, "rsync_raws": True,
            "encryption": "age" if has_age else "openssl",
            "age_recipient": None,
            "openssl_passfile": str(paths.config_dir() / "backup.pass"),
            "keep_archives": 14,
        },
    }
    plan = [{"action": "config.init", "path": str(target), "exists": exists,
             "encryption": starter["backup"]["encryption"]}]
    if dry_run or not confirm:
        return ConfigResult(
            outcome="dry_run", config_path=str(target), config_exists=exists, plan=plan,
            reason=(f"{target} already exists and would be replaced; re-run with --yes"
                    if exists else "re-run with --yes to write the starter config"),
            config=starter)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.part")
    tmp.write_text(json.dumps(starter, indent=2) + "\n")
    os.replace(tmp, target)
    return ConfigResult(config_path=str(target), config_exists=True, written=True,
                        config=starter,
                        warnings=["backup.enabled is false: set a target and flip it before importing"])
```

`show` returns the loaded `Config` as a dict plus the gate's verdict (`backup_gate: "ok"` or `"<code>: <message>"`), so `scouter config show` is the one command that answers "will an import work right now?".

- [ ] **Step 8: Register the verbs and wire the registry**

Add `Verb(id="config.init", group="config", writes=True, gated=True, ...)` with `ArgSpec("path", "str")`, `dry_run`, `confirm`; and `Verb(id="config.show", group="config", writes=False, gated=False, args=(ArgSpec("path","str"),))`. Append `"scouter.config"` and the four new verb-registering core modules to `registry._CORE_MODULES` (`exif`, `vault` and `dtstyle` register no verbs, so they stay out of this list and are imported by their callers). The lazy loader tolerates the ones that do not exist yet — ruling 1:

```python
_CORE_MODULES: tuple[str, ...] = (
    "scouter.config",
    "scouter.core.darkroom", "scouter.core.export", "scouter.core.history",
    "scouter.core.scout", "scouter.core.style", "scouter.core.library",
    "scouter.core.cull", "scouter.core.wb", "scouter.core.backup",
    "scouter.metrics",
)
```

- [ ] **Step 9: `ConfigResult` in `types.py`, `PHASE = 2`**

```python
@dataclass
class ConfigResult(Payload):
    config_path: str = ""
    config_exists: bool = False
    written: bool = False
    backup_gate: str = "unknown"
    config: dict = field(default_factory=dict)
```

and in `scouter/__init__.py`: `__version__ = "0.2.0"`, `PHASE = 2`.

- [ ] **Step 10: `tests/scouter_fakes.py` — a config fixture**

```python
def config_fixture(tmp: Path, *, backup: bool = True, encryption: str = "openssl") -> Config:
    """A Config pointing at temp directories, with the gate satisfiable when backup=True."""
    root = tmp / "library"; root.mkdir(parents=True, exist_ok=True)
    target = tmp / "backup"; target.mkdir(parents=True, exist_ok=True)
    passfile = tmp / "backup.pass"; passfile.write_text("test-passphrase\n"); passfile.chmod(0o600)
    return Config(
        library_root=str(root), path=str(tmp / "config.json"), exists=True,
        backup=BackupConfig(enabled=backup, target=str(target), encryption=encryption,
                            openssl_passfile=str(passfile)),
        cameras=(("E-M10 Mark III", "em10iii"),))
```

- [ ] **Step 11: `tests/test_config.py`**

- [ ] `test_missing_file_returns_defaults` — `exists is False`, no raise.
- [ ] `test_unknown_top_key_rejected` / `test_unknown_section_key_rejected` — `usage`, and the message names the near match (`"targt"` → suggests `"target"`).
- [ ] `test_bad_json_names_the_line` — `usage` with `detail.line`.
- [ ] `test_import_refuses_without_backup_target` **(E1)** — table-driven, one subtest per gate condition (`config_missing`, `disabled`, `no_target`, `target_parent_missing`, `target_not_writable`, `unencrypted`, `age_recipient_missing`, `age_missing`, `passfile_missing`, `passfile_absent`, `passfile_mode`): a config that trips exactly that one raises `BackupNotConfigured` with `detail.check` equal to the expected name, and `library.import` on a nonexistent source raises the same before touching the filesystem.
- [ ] `test_passfile_mode_must_be_0600` — a 0644 passfile trips `passfile_mode` and the message carries the octal.
- [ ] `test_age_selected_when_present` — `config.init` with a patched `shutil.which` returning a path picks `"age"`.
- [ ] `test_job_dir_shape` — `job_dir(cfg, "2026-06-28", "harbour-wedding")` ends with `2026/2026-06-28 harbour-wedding`; a bad slug and a bad date both raise `usage`.
- [ ] `test_find_job_ambiguous` — two years holding the same slug → `usage` listing both.
- [ ] `test_init_refuses_to_clobber` — existing file, no `--yes` → `outcome="dry_run"` and the file is byte-identical afterwards.
- [ ] `test_every_new_error_class_has_an_exit_code` — every class in `ERROR_CLASSES` has a row in `EXIT_CODES` and a non-empty `fix`.

- [ ] **Step 12: Run the suite and commit**

`uv run --extra dev python -m pytest tests -q` (all phase-1 tests must still pass; the parity test will flag `config.*` as missing from the CLI/MCP tables until task 14 — if it fails now, add the two `config` tools to `scouter/mcp/schemas.py` in this task and leave the golden regeneration for task 14, noting it in the commit).

---

### Task 2: The stdlib EXIF reader

**Why here:** the import's rename, its date and its burst grouping are all EXIF. This is pure computation over bytes, testable with a committed header slice and no darktable.

**Files:**
- Create: `scouter/core/exif.py`, `tests/test_exif.py`, `tests/fixtures/exif-orf-header.bin`
- Modify: none

**Interfaces:**
- Produces: `exif.read_tags(path) -> ExifTags`, `exif.camera_slug(tags, cfg) -> str`, `exif.capture_key(tags, path) -> tuple`, `exif.ifd0_offsets(path) -> dict[int, tuple[int, int]]` (tag → (offset, length), used by the synthetic-job builder in task 14 to patch capture times in place).
- Consumes: `scouter.config.Config`, `scouter.errors.ExifUnreadable`.

- [ ] **Step 1: Commit the fixture**

```bash
head -c 65536 ~/Pictures/EditingTrial/P6281348.ORF > tests/fixtures/exif-orf-header.bin
```

64 KiB carries IFD0 (at offset 8) and the Exif IFD, verified 2026-09-14. Note in the test module's docstring that this is a byte slice of Pratik's real ORF, kept so the reader is tested against a real camera's header and not a synthetic one.

- [ ] **Step 2: The TIFF walker**

```python
"""EXIF from the raw itself, with `struct` and nothing else.

Why not darktable-cli: it has no metadata dump mode, so the only way to read a
camera model through it is a ~2 s render per frame -- 10 minutes for a 300-frame
card to learn something that takes 2 ms here. Why not exiftool: it is not stdlib
and it is not installed on this Mac (checked 2026-09-14).

ORF, ARW and DNG are all TIFF containers, so one IFD0 walk covers every format
phase 2 accepts. PINNED FACT: ORF's TIFF magic is 0x4F52 (20306), NOT 42 -- a
strict `magic == 42` check rejects every Olympus raw. Verified on
P6281348.ORF: byte order II, magic 20306, IFD0 at offset 8, 23 entries,
Make "OLYMPUS CORPORATION", Model "E-M10 Mark III".
"""

MAKE, MODEL, DATETIME = 0x010F, 0x0110, 0x0132
EXIF_IFD, DATETIME_ORIGINAL, SUBSEC_ORIGINAL = 0x8769, 0x9003, 0x9291
EXPOSURE_TIME, F_NUMBER, ISO = 0x829A, 0x829D, 0x8827

#: 42 is the TIFF standard (ARW, DNG); 20306 (0x4F52) is Olympus ORF; 21330
#: (0x5352) is the other ORF spelling in the wild; 85 (0x55) is Panasonic RW2.
#: Accepting the extra two costs one set member and stops the magic check from
#: silently becoming a format whitelist.
ACCEPTED_MAGIC = frozenset({42, 20306, 21330, 85})

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _read_ifd(data: bytes, endian: str, offset: int) -> dict[int, tuple[int, int, bytes, int]]:
    """tag -> (type, count, value_bytes, value_offset). value_offset is where the
    bytes live in `data` (the entry itself for <=4 bytes), so a caller can patch
    a fixed-length ASCII value in place without re-encoding anything."""
    if offset <= 0 or offset + 2 > len(data):
        return {}
    (count,) = struct.unpack_from(endian + "H", data, offset)
    out: dict[int, tuple[int, int, bytes, int]] = {}
    for i in range(count):
        entry = offset + 2 + i * 12
        if entry + 12 > len(data):
            break
        tag, typ, n = struct.unpack_from(endian + "HHI", data, entry)
        size = _TYPE_SIZE.get(typ, 1) * n
        if size <= 4:
            value_at = entry + 8
        else:
            (value_at,) = struct.unpack_from(endian + "I", data, entry + 8)
        if value_at < 0 or value_at + size > len(data):
            continue                      # value lives past our header slice: not a guess
        out[tag] = (typ, n, data[value_at:value_at + size], value_at)
    return out
```

- [ ] **Step 3: `read_tags`**

- [ ] Read `min(limit, filesize)` bytes; byte order from `b"II"`/`b"MM"` (anything else → `ExifUnreadable` with `detail.header` = the first 4 bytes hex).
- [ ] Magic must be in `ACCEPTED_MAGIC`, else `ExifUnreadable` with `detail.magic`.
- [ ] Walk IFD0; follow `EXIF_IFD` when present.
- [ ] `_ascii(blob)` = `blob.rstrip(b"\x00 ").decode("ascii", "replace")` or `None` when empty — the real Olympus Make is space-padded to 24 bytes.
- [ ] `captured_at` = `DATETIME_ORIGINAL` from the Exif IFD, else IFD0 `DATETIME`, normalised `"2026:06:28 15:49:28"` → `"2026-06-28 15:49:28"`; malformed → `None` (never a guess).
- [ ] `_rational(endian, blob)` → `num/den` with a `den == 0` guard returning `None`.
- [ ] `source` is `"ifd0+exififd"`, `"ifd0"` or `"none"` so a degraded read is visible.

- [ ] **Step 4: `camera_slug` and `capture_key`**

```python
def camera_slug(tags: ExifTags, cfg) -> str:
    mapped = cfg.camera_map().get(tags.model or "")
    if mapped:
        return mapped
    if tags.model:
        slug = "".join(c for c in tags.model.lower() if c.isalnum())
        return paths.validate_slug(slug[:48])
    raise ExifUnreadable(
        "no camera model in this file's EXIF, and no --camera was given",
        detail={"magic": tags.magic, "byte_order": tags.byte_order, "source": tags.source})


def capture_key(tags: ExifTags, path) -> tuple[str, int, str]:
    """Rename and burst-grouping sort key. No capture time sorts LAST, by filename,
    and the import warns -- it never invents a time."""
    return (tags.captured_at or "9999-99-99 99:99:99", tags.subsec, Path(path).name)
```

`camera_slug` never returns `"unknown"`: a wrong camera name is baked into 300 filenames forever, so a missing model is a refusal the caller answers with `--camera`.

- [ ] **Step 5: `ifd0_offsets`, for task 14's fixture builder**

```python
def datetime_offsets(path: str | Path, *, limit: int = 1 << 20) -> list[tuple[int, int]]:
    """Byte ranges of every DateTime/DateTimeOriginal ASCII value in the header.

    The strings are fixed-length ("2026:06:28 15:49:28\\0", 20 bytes), so the
    synthetic-job builder can overwrite them with an equal-length string in
    place: no re-encoding, no EXIF library, and the reader that finds the
    offsets is the reader under test.
    """
```

Returns `(offset, length)` for IFD0 `0x0132` and Exif `0x9003`.

- [ ] **Step 6: `tests/test_exif.py`**

- [ ] `test_real_orf_header` — against the fixture: `byte_order == "II"`, `magic == 20306`, `make == "OLYMPUS CORPORATION"`, `model == "E-M10 Mark III"`, `captured_at == "2026-06-28 15:49:28"`, `exposure_time == 0.02`, `source == "ifd0+exififd"`.
- [ ] `test_magic_42_accepted` — a synthesised little-endian magic-42 header with Make/Model parses.
- [ ] `test_big_endian` — the same header built `MM` parses identically.
- [ ] `test_unknown_magic_refused` — magic 1234 → `ExifUnreadable`, `detail.magic == 1234`.
- [ ] `test_truncated_header_degrades` — the fixture truncated to 64 bytes returns `source == "none"` and `model is None`, and does not raise.
- [ ] `test_value_past_the_limit_is_none_not_a_guess` — `read_tags(fixture, limit=512)` gives `model is None`, never a partial string.
- [ ] `test_camera_slug_mapping_and_fallback` — mapped model → `"em10iii"`; unmapped `"E-M10 Mark III"` → `"em10markiii"`; `model=None` → `ExifUnreadable`.
- [ ] `test_capture_key_orders_missing_time_last`.
- [ ] `test_datetime_offsets_round_trip` — patch the fixture's bytes at the returned offsets with an equal-length string, re-read, and get the new time back.

---

### Task 3: `library.import` — copy, checksum, rename, film roll, read-only

**Why here:** with config and EXIF in place this is the phase's irreversible step, and everything downstream addresses frames by the names it chooses.

**Files:**
- Create: `scouter/core/library.py`, `tests/test_library.py`
- Modify: `scouter/types.py`, `tests/scouter_fakes.py`

**Interfaces:**
- Produces: `library.import_card`, `library.verify`, `library.register`; `ImportResult`, `VerifyResult`, `RegisterResult`.
- Consumes: `scouter.config`, `scouter.core.exif`, `scouter.core.dtbridge` (`import_batch`), `scouter.core.vault` (task 4 — imported lazily and tolerated as absent until then, the same way `core.darkroom` lazily imports `history`, ruling 21).

- [ ] **Step 1: Scan and plan**

```python
SCAN_MAX_DEPTH = 8


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
```

- [ ] `_plan(files, cfg, date, camera)`: read EXIF per file; derive `date` from the earliest `captured_at` when not given; derive `camera` from the set (two distinct slugs and no `--camera` → `UsageError` naming both); sort by `capture_key`; assign `seq` from 1 with `seq_digits` padding, **sharing one seq across files with the same stem**; name `f"{date}_{slug}_{camera}_{seq:0{n}d}{ext.lower()}"`.
- [ ] Assert no duplicate destination inside the plan (`RuntimeError` if the construction ever breaks — it cannot, and the assertion says so).

- [ ] **Step 2: Copy with two hash passes**

```python
CHUNK = 1 << 20


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
            detail={"expected": digest.hexdigest(), "actual": written.hexdigest(),
                    "source": str(src), "destination": str(dst)})
    return digest.hexdigest(), size
```

- [ ] **Step 3: Resume-safety and read-only originals**

- [ ] Destination exists → hash it: equal to the source's hash → status `"skipped_identical"` (when `cfg.import_.skip_identical`); different → `DestinationCollision` naming the path. Never overwrite.
- [ ] After a successful copy, `os.chmod(dst, 0o444)` when `cfg.import_.read_only_originals`. Sidecars are untouched.
- [ ] A read-only destination from a previous run must still be **hashable** (it is; 0444 is readable) and must not be re-chmod-ed.

- [ ] **Step 4: `import_card`, in the design's order**

- [ ] `cfg = config.load()`; `config.require_backup(cfg)` **first**, before `_scan` — the gate must not depend on the card being present.
- [ ] `_scan` → empty is `UsageError` naming the extensions and the depth searched.
- [ ] `_plan`.
- [ ] Dry run / no confirm → `ImportResult(outcome="dry_run", plan=[...])`, hashing nothing and creating no directories (ruling 3). An import is always many files, so it is always gated.
- [ ] Create `raw/` and `.scouter/` (not `exports/`).
- [ ] Copy loop, accumulating `files[]` entries in the design's `import.json` shape.
- [ ] `_write_atomic(scouter_dir / "checksums.sha256", ...)` — `f"{sha}  raw/{name}\n"`, two spaces, sorted by name, coreutils-compatible; and `import.json` via `json.dumps(..., indent=2)`; both temp-file + `os.replace`.
- [ ] Film roll: `dtbridge.call("import_batch", {"source_path": str(raw), "recursive": False})`, wrapped so a `BridgeTimeout` becomes `film_roll_registered: False` + the error's `fix` in `warnings` — **the copy is the irreversible part and it succeeded**, so a darktable that is not running must not fail the import.
- [ ] Vault note: `vault.write_job_note(cfg, manifest)` in a `try/except` that turns any failure into a warning (task 4 lands the writer; until then the lazy import raises `ImportError`, which is caught and warned identically).

- [ ] **Step 5: `library.verify` and `library.register`**

```python
@metrics.instrumented("library.verify")
def verify(job_slug: str, *, date: str | None = None, job: str | None = None) -> VerifyResult:
    """Re-hash raw/ against .scouter/checksums.sha256. `.xmp` sidecars are
    excluded by SUFFIX, not by silence: they are darktable's files, they change
    whenever an edit lands, and they are never in the manifest."""
```

Returns `{checked, ok, mismatched: [{name, expected, actual}], missing: [...], extra: [...]}`. `register` re-runs `import_batch` for a job whose import happened with darktable closed, and is gated (it writes to darktable's library).

- [ ] **Step 6: `tests/test_library.py`**

Fixture: `_make_card(tmp, n)` writes `n` small files, each = the committed 64 KiB ORF header (so EXIF parses) + `os.urandom(2048)` (so the hashes differ), with capture times patched via `exif.datetime_offsets`.

- [ ] `test_gate_runs_before_the_card_is_read` — a nonexistent source plus a gate-failing config raises `BackupNotConfigured`, not a file error.
- [ ] `test_plan_names_and_sequences` — names match `2026-06-28_harbour-wedding_em10iii_0001.orf`; a `.JPG` sharing a stem gets the same seq; the order follows capture time, not filename.
- [ ] `test_mixed_camera_card_refused_without_camera_flag`; `test_camera_flag_overrides`.
- [ ] `test_dry_run_creates_nothing` — no directory exists afterwards, and `FakeBridge` recorded zero calls.
- [ ] `test_copy_verifies_and_chmods` — every destination is `0o444` and its sha256 matches `checksums.sha256`.
- [ ] `test_checksum_mismatch_deletes_and_stops` — patch `_copy_verified`'s re-read to flip a byte; the destination is gone, `ChecksumMismatch` is raised, and files after it in the plan were never written.
- [ ] `test_resume_skips_identical_and_refuses_collision`.
- [ ] `test_import_succeeds_with_darktable_down` — `FakeBridge` raising `BridgeTimeoutError` → `outcome="ok"`, `film_roll_registered is False`, and a warning carrying the `fix`.
- [ ] `test_checksums_file_is_coreutils_format` — `shasum -c` shape: two spaces, relative `raw/` paths, sorted.
- [ ] `test_verify_detects_mismatch_missing_extra` and `test_verify_ignores_xmp_sidecars`.
- [ ] `test_import_emits_exactly_one_event` — one line in `events.jsonl` for a multi-file import (ruling 23: the nested `scout`/bridge calls must not emit).

---

### Task 4: The vault job note, `library.jobs`, `library.relocate`

**Why here:** the import's last step needs the note writer, and `relocate` is what makes the SSD move a config change instead of a migration.

**Files:**
- Create: `scouter/core/vault.py`, `tests/test_vault.py`
- Modify: `scouter/core/library.py`, `scouter/types.py`, `tests/test_library.py`

**Interfaces:**
- Produces: `vault.write_job_note(cfg, manifest) -> dict`, `vault.append_stage(cfg, job_slug, stage, rows, checklist_key) -> dict`, verbs `library.jobs`, `library.relocate`.
- Consumes: `$OBSIDIAN_VAULT_PATH`, `cfg.vault.jobs_dir`.

- [ ] **Step 1: Locate the vault, and degrade silently when it is absent**

```python
def vault_root() -> Path | None:
    """`$OBSIDIAN_VAULT_PATH` if it is set AND the directory exists, else None.

    The user-level rule: on another machine, in CI, or in a sandbox the vault is
    simply not there, and every vault write becomes a no-op warning rather than
    a failure. A job import must never fail because a note could not be written.
    """
    raw = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not raw:
        return None
    root = Path(raw).expanduser()
    return root if root.is_dir() else None
```

- [ ] **Step 2: The note body, from the design's template**

`_render_new_note(cfg, manifest) -> str` builds the frontmatter (`type: photo-job`, `date`, `tags`, `ai-first: true`, `project: "[[Capsule Corp]]"`, `related: [...]`, `job_slug`, `camera`, `camera_model`, `frames`, `bytes`, `library_path` (**relative**, so the note survives the SSD move), `imported_at`, `scouter_version`, `status: imported`), the `## For future agent` preamble (which tells the next agent to read `library_root` out of the config rather than assume a path, and to verify numbers with `scouter library verify --json`), the `## Import <ts>` table, and the `## Stages` checklist.

- [ ] **Step 3: Append, never overwrite**

```python
def write_job_note(cfg, manifest: dict) -> dict:
    root = vault_root()
    if root is None:
        return {"written": False, "reason": "vault not present ($OBSIDIAN_VAULT_PATH unset or missing)"}
    directory = root / cfg.vault.jobs_dir
    directory.mkdir(parents=True, exist_ok=True)
    note = directory / f"{manifest['date']} {manifest['job_slug']}.md"
    if not note.exists():
        note.write_text(_render_new_note(cfg, manifest))
        return {"written": True, "path": str(note), "mode": "created"}
    # Never rewrite a body a human may have edited: append one section and
    # additively merge the frontmatter (sum frames/bytes, union lists, leave
    # scalars that already exist alone and note the new value in the section).
    text = note.read_text()
    merged, notes = _merge_frontmatter(text, _frontmatter_for(cfg, manifest))
    merged += "\n" + _render_import_section(manifest, notes)
    note.write_text(merged)
    return {"written": True, "path": str(note), "mode": "appended"}
```

- [ ] `_merge_frontmatter` parses only the leading `---` block with a line-oriented reader (no YAML library: stdlib only, and the block we write is flat), sums `frames`/`bytes`, unions `tags`/`related`, and returns the conflicting scalars so the appended section can report them.
- [ ] `append_stage(cfg, job_slug, stage, rows, checklist_key)` finds the note by slug (glob `*<slug>.md`), appends `## <stage> <ISO ts>` plus a two-column table of `rows`, and ticks `- [ ] <checklist_key>` → `- [x]`. Missing note → `{"written": False, "reason": "no job note"}`; missing checklist line → appended without ticking, and says so.

- [ ] **Step 4: `library.jobs`**

Walks `<library_root>/*/* */`, and per job reports `{date, slug, path (relative), frames, bytes, ratings: {0: n, 1: n, …}, manifests: [...], has_cull_proposal, verified_at}`. Ratings come from `xmp.rating(sidecar)` — a single regex over `xmp:Rating="N"`, cheap enough for 300 sidecars.

- [ ] **Step 5: `library.relocate`**

- [ ] Dry run lists every job, the new absolute path, and whether the job exists at the new root.
- [ ] With `--yes`: rewrite **only** `library_root` in the config (read, mutate one key, atomic write — preserving every other key and every `_comment`), then run `verify` per job at the new root.
- [ ] Result: `{old_root, new_root, jobs: [{slug, ok, checked, mismatched, missing}], darktable_rollpaths_stale: true, next_step: "in darktable, import <new_root>/<YYYY>/<date slug>/raw for each job; the .xmp sidecars beside the raws carry every edit, so nothing is lost -- but darktable's film rolls hold absolute paths in library.db, which Scouter may not write"}`.
- [ ] Refuse (`usage`) when the new root does not exist or is not a directory; refuse when it is **inside** the old root (a nested move would recurse).

- [ ] **Step 6: `tests/test_vault.py`**

- [ ] `test_no_vault_is_a_warning_not_a_failure` — `OBSIDIAN_VAULT_PATH` unset → `{"written": False}`, and `import_card` still returns `outcome="ok"`.
- [ ] `test_new_note_has_ai_first_frontmatter_and_preamble` — asserts `ai-first: true`, a `## For future agent` section, and that `library_path` is relative.
- [ ] `test_second_import_appends_and_merges` — frames 30 + 12 = 42, tags unioned, the original body text still present byte-for-byte, one extra `## Import` section.
- [ ] `test_append_stage_ticks_the_checklist`.
- [ ] `test_relocate_rewrites_only_library_root` — every other key and every `_comment` survives; a JSON diff shows exactly one changed value.
- [ ] `test_relocate_refuses_nested_root`.
- [ ] `test_jobs_lists_ratings_histogram`.

---

### Task 5: The set-stats engine and the `image.rate` bridge verb

**Why here:** cull and outliers both need per-frame stats over a whole set, cached and parallel, and cull needs per-image rating writes with honest misses.

**Files:**
- Create: `tests/test_stats_many.py`
- Modify: `scouter/core/scout.py`, `darktable_mcp/lua/darktable_mcp.lua`, `darktable_mcp/bridge/client.py`, `tests/lua/test_dispatcher.lua`, `tests/test_lua_dispatcher.py`, `scouter/core/xmp.py`, `tests/scouter_fakes.py`

**Interfaces:**
- Produces: `scout.stats_many(paths_in, *, px, workers, job_dir=None) -> list[dict]`, `xmp.rating(path) -> int | None`, bridge verb `image.rate`.
- Consumes: the existing `scout.render` and `scout.stats`.

- [ ] **Step 1: The stats cache**

```python
def _stats_cache_path(raw_path: str, px: int) -> Path:
    """Key: absolute path + the XMP's mtime_ns + px + Scouter's version.

    The XMP mtime is in the key on purpose: after `style.apply_set` the frames
    whose history changed get fresh numbers and the rest are read from cache,
    which is what makes "stats before the style, stats after the style"
    affordable over 300 frames.
    """
    from scouter import __version__
    mtime = xmp.sidecar_path(raw_path)
    stamp = mtime.stat().st_mtime_ns if mtime.exists() else 0
    key = hashlib.sha1(f"{Path(raw_path).resolve()}|{stamp}|{px}|{__version__}".encode()).hexdigest()
    return paths.stats_dir() / f"{key}.json"
```

- [ ] Generalise phase 1's `_prune_preview_cache` into `_prune_cache(directory, max_age_s, max_bytes)` and call it for both `previews/` and `stats/` (`stats.cache_max_age_days`, 200 MB budget). Keep the old name as a thin wrapper so nothing else changes.

- [ ] **Step 2: `stats_many`**

```python
def stats_many(paths_in: list[str], *, px: int, workers: int,
               job_dir: Path | None = None) -> list[dict]:
    """Stats for a set, cached on disk and rendered by a small thread pool.

    Each worker calls `render`, which already builds its OWN isolated configdir
    per call (`_render_configdir` uses a per-call token) and deletes it after,
    so K concurrent darktable-cli processes cannot contend for a library lock.
    One frame that fails does not fail the set: it comes back as
    {"path": ..., "error": {"code", "message"}} and the caller reports `failed`.
    """
    workers = max(1, min(workers, os.cpu_count() or 2))
    ...
```

- [ ] Cache hit → read the JSON, mark `cached: True`; miss → `stats.__wrapped__(path, px=px)` (ruling 23: **no nested event**), write the cache entry atomically, mark `cached: False`.
- [ ] `ThreadPoolExecutor(max_workers=workers)`, results re-ordered to match `paths_in`.
- [ ] Mirror the whole list into `job_dir / f"stats-{px}.json"` when `job_dir` is given.

- [ ] **Step 3: `xmp.rating`**

```python
_RATING_RE = re.compile(r'xmp:Rating="(-?\d+)"')


def rating(raw_path: str | Path) -> int | None:
    """The sidecar's xmp:Rating, or None when there is no sidecar / no attribute.

    A regex, not a parse: this is called for 300 sidecars in a cull and it reads
    one attribute out of the rdf:Description header. Verified present in
    Pratik's real sidecar (xmp:Rating="1", 2026-09-14).
    """
```

- [ ] **Step 4: The Lua `image.rate` verb**

```lua
-- Per-image rating writes with HONEST misses.
--
-- w1ne's rate_photos returns only {updated = n} and silently skips ids
-- darktable does not know. For a 300-frame cull that is exactly the case that
-- must be visible, so this verb reports the before/after rating per image and
-- lists the ids it could not find. rate_photos is left untouched for its own
-- MCP tool.
methods["image.rate"] = function(p)
  p = p or {}
  local ids = p.image_ids or {}
  if #ids == 0 then error("image.rate: image_ids required and non-empty") end
  local rating = tonumber(p.rating)
  if rating == nil or rating < -1 or rating > 5 then
    error("image.rate: rating must be -1..5, got " .. tostring(p.rating))
  end
  local rated, missed, per_image = 0, {}, {}
  for _, id in ipairs(ids) do
    local image = dt.database[tonumber(id)]
    if not image then
      table.insert(missed, tonumber(id))
    else
      local before = image.rating or 0
      local ok, err = pcall(function() image.rating = rating end)
      if ok then
        rated = rated + 1
        table.insert(per_image, {image_id = tonumber(id), rating_before = before,
                                 rating_after = rating})
      else
        table.insert(missed, tonumber(id))
        table.insert(per_image, {image_id = tonumber(id), rating_before = before,
                                 rating_after = before, error = tostring(err)})
      end
    end
  end
  return {rating = rating, rated = rated, missed = missed, per_image = per_image}
end
```

- [ ] Add `"image.rate": 60.0` to `DEFAULT_TIMEOUTS` (50 ids per call, each a library write).
- [ ] Extend `tests/lua/test_dispatcher.lua` with a stub `dt.database` whose `[id]` returns a table carrying a settable `rating`, and assert: ids rated, unknown ids in `missed`, a bad rating raising, and `per_image` carrying `rating_before`.
- [ ] Extend `tests/test_lua_dispatcher.py`'s registration assertion (it parses the `methods` table names out of the Lua source, ruling 13) with `image.rate`.

- [ ] **Step 5: `FakeBridge` gains the new method and a call counter**

```python
class FakeBridge:
    def __init__(self, ...):
        self.calls: list[tuple[str, dict]] = []      # NEW: every call, in order
    def call(self, method, params=None, timeout=None):
        self.calls.append((method, dict(params or {})))
        ...
    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)
```

`count()` is what task 12's E8 test uses to assert **zero** write calls on a no-op set.

- [ ] **Step 6: `tests/test_stats_many.py`**

- [ ] `test_cache_hit_on_second_call` — patch `scout.render` with a counter; two calls, one render.
- [ ] `test_cache_miss_when_xmp_mtime_advances` — touch the sidecar, get a second render.
- [ ] `test_one_failing_frame_does_not_fail_the_set` — 5 paths, one raising; result has 5 entries, one with `error.code`.
- [ ] `test_workers_do_not_share_a_configdir` — patch `render` to record `_render_configdir()`; with `workers=4` the recorded set has 4 distinct paths.
- [ ] `test_order_is_preserved`.
- [ ] `test_job_dir_mirror_written`.
- [ ] `test_rating_regex` — the committed real sidecar returns 1; a sidecar with no attribute returns `None`.

---

### Task 6: `cull.propose` and `cull.apply`

**Why here:** the first volume verb the numbers are for, and the first one Pratik reads as a table.

**Files:**
- Create: `scouter/core/cull.py`, `tests/test_cull.py`
- Modify: `scouter/types.py`, `scouter/cli/tables.py` (created here), `scouter/cli/main.py`

**Interfaces:**
- Produces: verbs `cull.propose`, `cull.apply`; `CullResult`, `CullApplyResult`; `cli/tables.py::render(verb_id, payload) -> str`.
- Consumes: `scout.stats_many`, `import.json`'s EXIF, `xmp.rating`, bridge `image.rate`.

- [ ] **Step 1: Burst grouping**

```python
def _group_bursts(frames: list[dict], gap_s: float) -> list[dict]:
    """Consecutive frames within `gap_s` of each other, same camera, one burst.

    Frames with no capture time cannot be ordered against the rest, so they form
    one `no-time` group and are never proposed for a rating change -- their
    reason says exactly that. Inventing a time to group them would be a guess
    about when they were shot.
    """
```

- [ ] Parse `captured_at` with `datetime.strptime(..., "%Y-%m-%d %H:%M:%S")` plus `subsec / 100` as the fractional part (SubSecTimeOriginal is hundredths on this body; the code divides by `10 ** len(str(subsec))` so it is unit-agnostic, and the result records `subsec_scale`).
- [ ] Burst ids `b1`, `b2`, …, each `{burst_id, frames: [seq...], span_s, camera}`.

- [ ] **Step 2: The proposal rules, exactly as the design's table**

```python
def _propose_one(frame: dict, burst: dict, best_seq: int, cfg) -> tuple[int, list[str]]:
    reasons: list[str] = []
    flagged = False
    if frame["clipped_high_pct"] > cfg.cull.clipped_flag_pct:
        reasons.append(f"clipped {frame['clipped_high_pct']:.1f} % > "
                       f"{cfg.cull.clipped_flag_pct:.1f} %")
        flagged = True
    lum = frame["luminance_mean"]
    if lum < cfg.cull.luminance_low:
        reasons.append(f"mean luminance {lum:.0f} < {cfg.cull.luminance_low}")
        flagged = True
    elif lum > cfg.cull.luminance_high:
        reasons.append(f"mean luminance {lum:.0f} > {cfg.cull.luminance_high}")
        flagged = True
    ...
```

- [ ] Multi-frame burst: `best_seq` (max sharpness, ties to the lowest seq) → `pick_rating` with `"sharpest of N in burst bK (0.52 vs 0.31 next)"`; the rest → `keep_rating` with `"softer than the pick of burst bK (0.31 vs 0.52)"`.
- [ ] Singleton: `pick_rating` when `sharpness >= cfg.cull.sharpness_floor` and not flagged, else `keep_rating`.
- [ ] A flagged frame never goes above `keep_rating`.
- [ ] `rating_now > proposed` → proposed becomes `rating_now`, reason `"keeping your existing rating N"`.
- [ ] **No frame is ever proposed 0 or reject.** A comment says why: stats cannot see eyes, expression, or deliberate motion blur, and spec 13.4 declined the blink model; flagged frames get a reason and Pratik decides.
- [ ] When the burst's top two sharpness values are equal, the burst records `tie_broken_by: "seq"`.

- [ ] **Step 3: `propose` persists the proposal**

- [ ] Frames from `--job-slug` (every file in `import.json`) or from `--images`; stats from `stats_many(px=cfg.stats.set_px, workers=cfg.stats.workers, job_dir=...)`; EXIF from `import.json` (no re-read) with a fallback to `exif.read_tags` for an explicit image list.
- [ ] Resolve image ids through `dtbridge.resolve_image` (a pure read — `image.lookup`; it never moves the darkroom, final review blocker 2).
- [ ] Write `<job>/.scouter/cull-<ts>.json` atomically: `{version, job_slug, created, px, tolerances_used, frames: [...], groups: [...], xmp_mtimes: {name: mtime_ns}}` — `xmp_mtimes` is what makes `apply` able to detect staleness.
- [ ] Result carries `proposal_path`, `counts`, `stats` (`cached`/`computed`/`failed`/`ms`).

- [ ] **Step 4: `apply` consumes that file**

- [ ] Load `--proposal` or the newest `cull-*.json` for the job. Compare every frame's current XMP `mtime_ns` to the recorded one; any change → `UsageError` naming the frames and telling Pratik to re-run `cull propose` (the numbers he read are no longer the numbers on disk).
- [ ] Group the frames by proposed rating, skip frames whose `rating_now == rating_proposed`, and call `image.rate` with at most 50 ids per call.
- [ ] **Verify** from the XMP: poll each changed frame's sidecar for up to 2.0 s (50 ms) for `xmp:Rating == proposed`. Report `verified` and `unverified` separately; the warning for the unverified set says the rating is in darktable's database and the sidecar will catch up, and never claims a write we cannot see.
- [ ] `vault.append_stage(cfg, slug, "Cull", rows, "cull proposed")`.
- [ ] Always gated; the dry run returns the same table with `would_change`.

- [ ] **Step 5: `cli/tables.py`**

```python
"""Aligned tables for the three verbs whose payload Pratik reads directly.

JSON stays the default and the only machine contract; `--pretty` swaps in a
table for `cull.propose`, `outliers.list` and `library.jobs` because a 300-row
JSON array is not something a human culls from. Keyed by verb id, so a verb
without a renderer simply gets indented JSON.
"""

RENDERERS: dict[str, Callable[[dict], str]] = {...}


def render(verb_id: str, payload: dict) -> str | None: ...
```

In `cli/main.py`, when `--pretty` and `tables.render(verb, data)` returns a string, print it to **stdout** in place of the JSON (documented in `--help`: `--pretty` is for humans, plain invocation is the contract).

- [ ] **Step 6: `tests/test_cull.py`**

- [ ] `test_burst_boundary_at_the_gap` — frames 1.0 s apart are one burst; 2.5 s apart are two; exactly 2.0 s joins (`<=`).
- [ ] `test_one_pick_per_burst` — 6 bursts of 5 → exactly 6 frames at `pick_rating`.
- [ ] `test_tie_breaks_by_seq_and_says_so`.
- [ ] `test_never_proposes_zero` — a frame that is dark, clipped and soft still gets `keep_rating`, with three reasons.
- [ ] `test_existing_higher_rating_kept`.
- [ ] `test_no_time_frames_are_untouched`.
- [ ] `test_reasons_carry_numbers` — every reason string contains at least one digit (a reason without a number is not a reason).
- [ ] `test_apply_refuses_stale_proposal` — bump one sidecar's mtime → `usage` naming that frame.
- [ ] `test_apply_batches_at_fifty` — 130 frames → `FakeBridge.count("image.rate") == 3`.
- [ ] `test_apply_reports_unverified_separately` — a fake sidecar that never updates → `verified == 0`, `unverified == n`, `outcome="ok"`, and a warning that does not claim success.
- [ ] `test_apply_skips_unchanged_frames`.
- [ ] `test_pretty_table_has_one_row_per_frame`.

---

### Task 7: The `.dtstyle` writer — `style.extract` with a real module filter

**Why here:** the style engine is the phase's centre, and the filter is what phase 1 could not do. The file format is pinned by a probe before the writer is trusted.

**Files:**
- Create: `scouter/core/dtstyle.py`, `tests/test_style_dtstyle.py`, `tests/fixtures/hero.dtstyle`
- Modify: `scouter/core/style.py`, `scouter/core/xmp.py`, `darktable_mcp/lua/darktable_mcp.lua`, `darktable_mcp/bridge/client.py`, `tests/lua/test_dispatcher.lua`, `tests/test_lua_dispatcher.py`, `scouter/types.py`

**Interfaces:**
- Produces: `dtstyle.build/write/read_operations`, bridge verbs `style.export_file` and `style.import_file`, `xmp.active_entries`, `style.extract` with `modules_honoured=True`.
- Consumes: `xmp.parse`, `cfg.style.never_include`.

- [ ] **Step 1: The two Lua verbs**

```lua
-- Export an existing style to a .dtstyle file. Its first job is to PIN the file
-- format: a style made by style.create_from_current is exported here and the
-- real file is committed as tests/fixtures/hero.dtstyle, so the writer in
-- scouter/core/dtstyle.py is asserted against darktable's own output rather
-- than against an assumption about another project's XML.
methods["style.export_file"] = function(p)
  p = p or {}
  if type(p.name) ~= "string" or p.name == "" then error("style.export_file: name required") end
  if type(p.dir) ~= "string" or p.dir == "" then error("style.export_file: dir required") end
  local style = nil
  for _, s in ipairs(dt.styles) do if s.name == p.name then style = s; break end end
  if not style then error(string.format("style.export_file: style %q not found", p.name)) end
  local ok, err = pcall(dt.styles.export, style, p.dir)
  if not ok then error("style.export_file: " .. tostring(err)) end
  local path = p.dir .. "/" .. p.name .. ".dtstyle"
  return {name = p.name, path = path, exported = true, bytes = file_size(path)}
end

methods["style.import_file"] = function(p)
  p = p or {}
  if type(p.path) ~= "string" or p.path == "" then error("style.import_file: path required") end
  local name = p.name
  local before, existing = 0, nil
  for _, s in ipairs(dt.styles) do
    before = before + 1
    if name and s.name == name then existing = s end
  end
  if existing and not p.overwrite then
    error(string.format("style.import_file: a style named %q already exists; pass "
          .. "overwrite=true to replace it", name))
  end
  local replaced = false
  if existing and p.overwrite then
    local ok, err = pcall(dt.styles.delete, existing)
    if not ok then error("style.import_file: could not delete the existing style: "
                         .. tostring(err)) end
    replaced = true
  end
  local ok, err = pcall(dt.styles.import, p.path)
  if not ok then error("style.import_file: " .. tostring(err)) end
  local after = 0
  for _ in ipairs(dt.styles) do after = after + 1 end
  return {name = name, imported = true, replaced = replaced,
          styles_before = before, styles_after = after}
end
```

- [ ] `file_size(path)` uses the plugin's existing `stat_format_args()` helper (already present for the sidecar signature), not a new shell form.
- [ ] `DEFAULT_TIMEOUTS`: `"style.export_file": 30.0`, `"style.import_file": 60.0`.
- [ ] Dispatcher tests: stub `dt.styles.import/export/delete`; assert the not-found error, the exists-without-overwrite refusal, the delete-then-import path, and `styles_after`.

- [ ] **Step 2: Pin the format (host probe, `SCOUTER_HOST_TESTS=1`)**

`tests/test_host_phase2.py::test_pin_dtstyle_format` (written now, run now, and kept): open the EditingTrial ORF, `darkroom set exposure 0.5`, `style.create_from_current` a throwaway style, `style.export_file` it into a temp dir, print the file, and **commit it** as `tests/fixtures/hero.dtstyle`. Then:

- [ ] Record in `docs/measurements-generated.md`: the root element and version attribute, the `<info>` children, the `<plugin>` children **in order**, and whether `<iop_order>` is present.
- [ ] If the real element names differ from the design's section 9.2, **the fixture wins**: update `dtstyle.build` and correct section 9.2 in this task's commit (the phase 1 precedent: the `colorbalancergb` slot map was pinned the same way).

- [ ] **Step 3: `xmp.active_entries` and `dtstyle.build`**

```python
def active_entries(parsed: ParsedXmp) -> list[HistoryEntry]:
    """Entries with num < history_end, newest-wins per (operation, multi_priority).

    history_end is darktable's cut: everything at or past it is a redo-able
    tail and is not part of the image's current look, so it must never ride
    into a style.
    """
```

```python
def build(parsed, name: str, description: str, operations: list[str]) -> str:
    """Render a .dtstyle carrying only `operations`, in the XMP's own order.

    `params` and `blendop_params` are copied VERBATIM from the XMP attribute
    strings -- the same hex or `gz<level><base64>` text. Phase 1's decoder
    exists for reading; a round trip through it would risk changing a single
    float, and a style is the one artefact that lands on 300 frames.
    `iop_order_version` comes from the sidecar (4 on this camera);
    `iop_order_list` is copied when the sidecar has one and emitted empty when
    it does not -- verified absent from Pratik's real sidecar 2026-09-14, which
    carries iop_order_version="4" (a built-in order) instead.
    """
```

- [ ] Build with `xml.etree.ElementTree` and `ET.tostring(..., encoding="unicode")`, prefixed by the XML declaration. Escape via ElementTree (never string concatenation) so a description with `&` cannot produce a broken file.
- [ ] `read_operations(path)` parses a `.dtstyle` back to its `<operation>` list — used by the test and by `style.extract`'s post-import verification.

- [ ] **Step 4: `style.extract`, rewritten around the filter**

- [ ] Keep the signature. New kwargs: `no_filter: bool = False` (the honest escape hatch named in `StyleWriteFailed.fix`).
- [ ] Requested module in `cfg.style.never_include` → `UsageError` naming it and why (per-image geometry and repairs must never ride a style).
- [ ] Requested module absent from the history → `modules_missing`; **all** absent → `UsageError` ("an empty style is not a style").
- [ ] Write `<cache>/styles/<name>.dtstyle`, then `style.import_file {path, name, overwrite: confirm}`.
- [ ] Verify: `read_operations(path)` must equal the requested-and-present set; otherwise `StyleWriteFailed`, keeping the file for inspection (its path is in `detail`).
- [ ] `no_filter=True` or the `<iop_order>` fallback (step 2) → the phase 1 path (`style.create_from_current`) with `modules_honoured=False` and a warning naming the limitation. The gate is unchanged: `confirm` is required only when a style of that name exists.
- [ ] Keep phase 1's `_require_darkroom_shows` guard for the `no_filter` path (the Lua side builds from the *loaded* image); the filtered path reads the XMP of the *resolved* image and therefore does **not** need the darkroom at all — note that in the docstring, and keep `dtbridge.require_darkroom()` off that path so a style can be extracted from the lighttable.

- [ ] **Step 5: `tests/test_style_dtstyle.py`**

- [ ] `test_fixture_parses_and_lists_operations` — the pinned real `.dtstyle` round-trips through `read_operations`.
- [ ] `test_build_matches_the_fixtures_element_shape` — the writer's output has the same root tag, the same `<info>` children and the same `<plugin>` child names as the fixture (set comparison, so a new darktable version adding a child fails loudly instead of silently).
- [ ] `test_only_requested_operations_included` — from the real XMP with `["exposure", "sigmoid"]`, exactly those two `<operation>` elements.
- [ ] `test_params_are_byte_identical_to_the_xmp` — the `<op_params>` text equals the sidecar's `darktable:params` attribute, character for character, including a `gz`-prefixed blendop.
- [ ] `test_inactive_tail_excluded` — an entry with `num >= history_end` never appears.
- [ ] `test_never_include_module_refused`.
- [ ] `test_all_modules_missing_refused`.
- [ ] `test_import_failure_keeps_the_file` — `FakeBridge` raising → `StyleWriteFailed` and the `.dtstyle` still on disk.
- [ ] `test_description_with_ampersand_is_escaped`.

---

### Task 8: `style.apply_set` and the `partial` outcome

**Why here:** the set apply is the payoff of task 7 and the first verb whose partial result must survive.

**Files:**
- Create: `tests/test_style_set.py`
- Modify: `scouter/core/style.py`, `scouter/types.py`, `darktable_mcp/bridge/client.py`, `tests/test_lua_dispatcher.py`

**Interfaces:**
- Produces: verb `style.apply_set`, `StyleSetResult`.
- Consumes: bridge `style.apply` (existing), `list_styles` (existing), `import.json`.

- [ ] **Step 1: Raise the `style.apply` timeout, and check the invariant**

- [ ] `DEFAULT_TIMEOUTS["style.apply"] = 300.0` (25 ids per call, each a history write inside the worker loop).
- [ ] Read `tests/test_lua_dispatcher.py`'s `STALE_AGE_SECONDS` assertion. If it asserts `>=` against the largest budget, 300 == 300 passes and nothing changes. If it asserts strict `>`, raise the plugin's `STALE_AGE_SECONDS` to 600 in the same commit and record which case held in the commit message. Do not change the assertion itself.

- [ ] **Step 2: `apply_set`**

```python
@metrics.instrumented("style.apply_set")
def apply_set(name: str, *, job_slug: str | None = None, images: list[str | int] | None = None,
              exclude: list[str | int] | None = None, mode: str = "append", batch: int = 25,
              confirm: bool = False, dry_run: bool = False,
              job: str | None = None) -> StyleSetResult:
    """Apply one style to a whole set, in batches, keeping per-image results.

    A partial set is a RESULT, not an exception: 280 of 300 applied is
    information the caller needs, and phase 1's single-image `apply` raises
    StylePartial, which would discard the payload. So `outcome="partial"` when
    applied > 0 and failed > 0 (exit 9, the same code as style_partial, so one
    shell branch covers "some of it worked"), and StylePartial only when
    NOTHING applied.
    """
```

- [ ] `mode != "append"` → the existing `UnsupportedMode`, with the message extended to name the one place overwrite exists: `export.release --overwrite-style` (render-only).
- [ ] Resolve the set: `--images`, else every file in `import.json` (minus `--exclude`), resolved through `image.lookup`. Empty → `usage`.
- [ ] Dry run verifies the style exists via `list_styles` (a typo costs no round trips) and returns the full plan with a count.
- [ ] Batch loop over `style.apply {image_ids, name, mode}` with `batch` ids; accumulate `applied`, `missed`, `per_image`; a `BridgeTimeout` on one batch marks that batch's ids failed and **continues** (a stalled worker on frame 140 must not lose frames 1-139) up to two consecutive batch failures, then stops with what it has.
- [ ] `vault.append_stage(cfg, slug, "Style applied", rows, "style applied to the set")`.

- [ ] **Step 3: `StyleSetResult`**

```python
@dataclass
class StyleSetResult(Payload):
    name: str = ""
    mode: str = "append"
    requested: int = 0
    applied: int = 0
    failed: int = 0
    batches: int = 0
    missed: list[int] = field(default_factory=list)
    per_image: list[dict] = field(default_factory=list)
```

- [ ] **Step 4: `tests/test_style_set.py`**

- [ ] `test_batches_at_the_batch_size` — 60 images, `batch=25` → 3 bridge calls with 25/25/10 ids.
- [ ] `test_partial_keeps_successes` — `FakeBridge` failing 4 of 60 → `outcome="partial"`, `applied == 56`, `per_image` has 60 rows, `exit_code_for("partial") == 9`.
- [ ] `test_nothing_applied_raises` — all fail → `StylePartial`.
- [ ] `test_one_batch_timeout_does_not_lose_earlier_batches`.
- [ ] `test_two_consecutive_batch_failures_stop_the_run` — and the result says how many were never attempted.
- [ ] `test_dry_run_checks_the_style_exists` — unknown name → `usage`, zero `style.apply` calls.
- [ ] `test_overwrite_refused_names_the_render_path` — the message contains `export.release`.
- [ ] `test_exclude_removes_the_hero`.
- [ ] `test_gated_without_confirm` — `outcome="dry_run"`, zero bridge writes.

---

### Task 9: `outliers.list`

**Why here:** it closes spec 4.4's loop, and it is the verb that turns "the style landed" into "these six frames need you".

**Files:**
- Create: `tests/test_outliers.py`
- Modify: `scouter/core/style.py`, `scouter/types.py`, `scouter/cli/tables.py`

**Interfaces:**
- Produces: verb `outliers.list`, `OutliersResult`.
- Consumes: `scout.stats_many`, `wb.kelvin` (task 11 — imported lazily; until it lands, the Kelvin test degrades to the ratio test, which is exactly its designed fallback).

- [ ] **Step 1: The four measures**

```python
def _ev_proxy(frame_lum: float, ref_lum: float) -> float:
    """log2 of the rendered-luminance ratio. Named ev_proxy, NEVER ev.

    It is a ratio of mean luminance on a 512/1024 px render, not a metered
    exposure difference: scene content moves it, and two frames of different
    subjects can differ by 0.4 "EV" with identical camera settings. The name
    and the result's own label say so, so no caller mistakes it for a reading.
    """
    return math.log2((frame_lum + 0.5) / (ref_lum + 0.5))
```

- [ ] `temperature_k_delta`: `wb.kelvin(coeffs=frame_coeffs, camera=cam)` minus the same for the reference, both read from the XMPs' `temperature` params via `xmp.latest_params`. No table, or either value `None` → `temperature_k_delta = None` with `reason: "no wb calibration table for em10iii; run scouter wb calibrate"`.
- [ ] `wb_ratio_rg_delta` / `wb_ratio_bg_delta`: always available, from `stats`.
- [ ] `wb_test`: `"kelvin"` when both Kelvin values resolved, else `"ratio"` — and the result says which ran.
- [ ] `clipped_high_pct`: absolute, against `tolerances.clipped_high_pct`.

- [ ] **Step 2: `outliers`**

- [ ] Reference = `--reference` (the hero). Set = `--job-slug` or `--images`, reference excluded from its own comparison.
- [ ] Stats via `stats_many`; a frame whose stats failed goes into `failed`, never into `outliers` (an unmeasured frame is not a passing frame).
- [ ] A frame is an outlier when **any** enabled test breaches; `reasons` carries one sentence per breach with both numbers and the tolerance (`"ev_proxy +0.41 outside ±0.30 (mean luminance 118 vs hero 89)"`).
- [ ] Sort worst-first by `max(|breach| / tolerance)` so the live pass has an order.
- [ ] `vault.append_stage(cfg, slug, "Outliers", rows, "outliers reviewed")`.

- [ ] **Step 3: `OutliersResult` and its table renderer**

```python
@dataclass
class OutliersResult(Payload):
    reference: dict = field(default_factory=dict)
    tolerances: dict = field(default_factory=dict)
    wb_test: str = "ratio"
    checked: int = 0
    within_tolerance: int = 0
    outliers: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
```

Table columns: `seq | file | ev_proxy | ΔK | Δrg | Δbg | clip % | reasons`.

- [ ] **Step 4: `tests/test_outliers.py`**

- [ ] `test_each_tolerance_boundary` — four subtests: a frame exactly at ±0.3 EV is **within**, at 0.31 is out; 300 K in, 301 out; 1.0 % in, 1.01 out; ratio 0.03 in, 0.031 out.
- [ ] `test_kelvin_null_falls_back_to_ratio` — no table → `wb_test == "ratio"`, every `temperature_k_delta is None`, each with a reason naming `wb calibrate`.
- [ ] `test_kelvin_used_when_the_table_exists` — with `tests/fixtures/wb-em10iii.json` → `wb_test == "kelvin"`.
- [ ] `test_ev_is_labelled_proxy` — the field is `ev_proxy_delta` and the payload carries the explanatory label; no key called `ev`.
- [ ] `test_failed_frames_are_not_within_tolerance` — `checked == within + len(outliers)` and failed frames are in neither bucket.
- [ ] `test_sorted_worst_first`.
- [ ] `test_reference_excluded_from_its_own_comparison`.
- [ ] `test_empty_set_refused`.

---

### Task 10: Export presets, `export.release`, the delivery manifest

**Why here:** it is the last stage of the volume path, and it needs nothing from the WB or backup work.

**Files:**
- Create: `tests/test_export_release.py`
- Modify: `scouter/core/export.py`, `darktable_mcp/darktable/cli_wrapper.py`, `scouter/types.py`, `tests/scouter_fakes.py`

**Interfaces:**
- Produces: presets `client-proof` and `print-tiff`, `export.preset_args`, verbs `export.release` and `export.verify`; `ReleaseResult`.
- Consumes: `scout.render`, `import.json`, `xmp.rating`.

- [ ] **Step 1: `CLIWrapper.export_image(..., extra_args=None)`**

```python
def export_image(self, source, destination, *, format_type="jpg", max_width=0,
                 max_height=0, timeout=180, extra_args: list[str] | None = None):
    """...

    `extra_args` (new, optional) is appended after this wrapper's own flags and
    before the paths, so a caller can pass preset flags -- --icc-type,
    --core --conf plugins/imageio/format/jpeg/quality=90, --style,
    --style-overwrite -- without this wrapper learning about presets. Additive:
    every existing call site and the golden schema test are unaffected.
    """
```

- [ ] Verify by reading the method that the existing exit-code-lies-about-success check and the returned-path normalisation still wrap the new args.

- [ ] **Step 2: The preset table and its flags**

```python
PRESETS: dict[str, dict] = {
    "preview": {"ext": "png", "px": 1024, "note": "1024 px PNG for review"},
    "client-proof": {"ext": "jpg", "px": 2048, "quality": 90, "icc": "SRGB",
                     "note": "2048 px sRGB JPEG q90 -- client proof"},
    "client-final": {"ext": "jpg", "px": 0, "quality": 95, "icc": "SRGB",
                     "note": "full-size sRGB JPEG q95"},
    "print-tiff": {"ext": "tif", "px": 0, "bpp": 16, "icc": "ADOBERGB",
                   "note": "full-size 16-bit AdobeRGB TIFF, deflate"},
}


def preset_args(spec: dict, *, style: str | None = None, overwrite_style: bool = False) -> list[str]:
    """Every darktable-cli flag a preset needs, built in ONE place.

    --core must come last: everything after it is darktable's own argument
    list, so --conf entries follow it and nothing of ours may.
    """
    args: list[str] = []
    if spec.get("icc"):
        args += ["--icc-type", spec["icc"]]
    if style:
        args += ["--style", style]
        if overwrite_style:
            args += ["--style-overwrite"]
    conf: list[str] = []
    if "quality" in spec:
        conf += ["--conf", f"plugins/imageio/format/jpeg/quality={spec['quality']}"]
    if "bpp" in spec:
        conf += ["--conf", f"plugins/imageio/format/tiff/bpp={spec['bpp']}",
                 "--conf", "plugins/imageio/format/tiff/compress=5"]
    return args + (["--core", *conf] if conf else [])
```

Watermark is deliberately absent: spec 4.7 calls it optional, it needs a darktable watermark preset plus an SVG, and Pratik has not asked for one. Each preset's `note` lands in `warnings`, so the result always says what was actually rendered.

- [ ] **Step 3: `export.release`**

- [ ] Selection: `--images`, else every raw in `import.json` whose `xmp.rating >= min_rating` (default `cfg.export.min_rating`). Empty → `UsageError` naming the threshold **and the histogram it found** — never a zero-file "success".
- [ ] Destination `<job>/exports/<preset>/`; an existing output is a gate (phase 1's rule) and, with `--yes`, is replaced by `render`'s existing temp-then-`os.replace` path.
- [ ] Render each file with `preset_args`; one failure keeps the rest (phase 1's `files[].error` shape).
- [ ] Manifest `<job>/.scouter/manifest-<preset>-<ts>.json` in the design's shape, plus `manifest-<preset>-<ts>.sha256` over it (so the manifest is itself verifiable). `input_sha256` is **read from `import.json`** and the manifest carries `"input_sha256_source": "import.json"` so the shortcut is stated, not hidden. `output_sha256` is computed.
- [ ] Result: `out_dir` (the folder to share), `manifest_path`, `selected`, `exported`, `failed`, `bytes`; `vault.append_stage`.

- [ ] **Step 4: `export.verify`**

Re-hash every output in a manifest against it; return `library.verify`'s shape (`checked`, `ok`, `mismatched`, `missing`), plus a first check that the manifest matches its own `.sha256`.

- [ ] **Step 5: `tests/test_export_release.py`**

Fake `CLIWrapper` capturing `extra_args` and writing a 1 KB file.

- [ ] `test_preset_flags` — four subtests, one per preset, asserting the exact `extra_args` list, including that `--core` is last before any `--conf`.
- [ ] `test_client_proof_is_2048_px` — `max_width == max_height == 2048`.
- [ ] `test_empty_selection_refused_with_histogram` — `usage`, and the message contains the ratings histogram.
- [ ] `test_manifest_shape_and_checksum` — every key in the design's example is present; the `.sha256` verifies; `input_sha256_source == "import.json"`.
- [ ] `test_input_sha_read_not_recomputed` — patch `hashlib.sha256` to explode on the input path; the release still succeeds.
- [ ] `test_existing_output_is_gated`.
- [ ] `test_one_render_failure_keeps_the_rest`.
- [ ] `test_style_overwrite_only_reaches_the_cli` — `--style-overwrite` appears in `extra_args` and **no** bridge call was made (the library is untouched).
- [ ] `test_export_verify_detects_a_flipped_byte`.

---

### Task 11: White balance — journalled coefficients, the slider probe, `wb.calibrate`, `wb.kelvin`

**Why here:** `outliers.list` wants real Kelvin, and phase 1's honest refusal on `temperature` undo is the one limit this phase can remove with evidence.

**Files:**
- Create: `scouter/core/wb.py`, `spike/scouter_wb_probe.lua`, `tests/test_wb.py`, `tests/fixtures/wb-em10iii.json`
- Modify: `scouter/core/xmp.py`, `scouter/core/darkroom.py`, `scouter/core/dtbridge.py`, `scouter/types.py`

**Interfaces:**
- Produces: `xmp.latest_params`, verbs `wb.calibrate` and `wb.kelvin`, `ACTION_PATHS` entries for the coefficient sliders, a real `temperature` undo.
- Consumes: `darkroom.set_param`, `history.commit`, the journal.

- [ ] **Step 1: State the fact in code, once**

At the top of `scouter/core/wb.py`:

```python
"""Kelvin <-> coefficients, honestly.

THE FACT (phase 1 design 7.2, verified against the real sidecar): darktable's
`temperature` module stores modversion 4 params as `<4f i` =
coeff_r, coeff_g, coeff_b, coeff_g2, preset -- camera-relative multipliers, with
coeff_g2 = inf on this body. The UI slider is Kelvin. darktable converts between
them inside src/iop/temperature.c using (a) the camera's XYZ->camera matrix from
its own camera-support data and (b) a blackbody/daylight illuminant model.
NEITHER input is in the XMP, and Scouter may not read darktable's internal data
files as an API.

SO: a faithful closed-form conversion from the XMP alone is NOT POSSIBLE, and
nothing here pretends otherwise. What is possible, and is what this module does:
  1. journal the whole coefficient vector before every temperature write;
  2. restore it through the coefficient sliders, whose action paths are PINNED by
     a host probe (spike/scouter_wb_probe.lua) -- an exact undo;
  3. invert Kelvin from a MEASURED per-camera table (`wb.calibrate`), because
     the coefficient ratio is monotone in Kelvin at fixed tint;
  4. and when none of that is available, refuse -- handing over the exact
     coefficients, which is a different thing from shrugging.
"""
```

- [ ] **Step 2: `xmp.latest_params` and the journalled vector**

```python
def latest_params(parsed: ParsedXmp, operation: str, instance: int = 0) -> dict | None:
    """The newest ACTIVE entry's whole decoded param dict, plus `params_hex`.

    `latest_value` returns one named float; a temperature undo needs all four
    coefficients at once, and a caller that has to make four calls to get one
    module's state will eventually make three.
    """
```

- [ ] In `darkroom._before_state`, when `module == "temperature"`, record `params` (the whole dict) and `params_hex` alongside the existing single `value`. Ruling 20 still holds: `enabled` travels with it, so a disabled module is never reported as live.

- [ ] **Step 3: The coefficient-slider probe (host, `SCOUTER_HOST_TESTS=1`)**

`tests/test_host_phase2.py::test_pin_wb_coefficient_sliders` — with the ORF in the darkroom, `darkroom.status` each candidate and record `nan` / non-`nan`:

```
iop/temperature/red                          iop/temperature/green
iop/temperature/blue                         iop/temperature/emerald
iop/temperature/channel coefficients/red     …/green   …/blue   …/emerald
iop/temperature/scene illuminant temp        iop/temperature/settings
```

- [ ] Record every result in `docs/measurements-generated.md`, including the ones that returned `nan` — the negative result is the evidence for tier 4.
- [ ] Add the non-`nan` paths to `dtbridge.ACTION_PATHS` as `("temperature", "coeff_r")` etc.
- [ ] If **none** answer: skip step 4's undo change, leave phase 1's refusal in place with the improved message (step 6), and say so in the task's commit. Do not guess a path.

- [ ] **Step 4: The temperature undo (only if step 3 pinned sliders)**

In `darkroom.undo_last`, when the entry's module is `temperature` and `before.params` is present: issue one absolute `set` per pinned coefficient, **one** `history.commit`, then verify the whole vector from the XMP with `_close()` per component (`coeff_g2 = inf` compares as "both non-finite", never as equal floats). Any component that does not come back → the entry stays `undone: false` and the result reports which component failed. Same journal mechanism as every other module (phase 1 design 8.2), with four values instead of one.

- [ ] **Step 5: `wb.calibrate`**

- [ ] Refuses when the image is inside a job's `raw/` unless `allow_in_job=True`, and the refusal says it would add ~21 history items to a client frame.
- [ ] Loop `k_from..k_to` step `step`: `darkroom.set_param.__wrapped__("temperature", "temperature", k)` (ruling 23: no nested events) → `history.commit` → `xmp.latest_params` → append `{k, coeff_r, coeff_g, coeff_b}`.
- [ ] **Monotonicity check:** `r = coeff_r / coeff_b` must be strictly increasing in `k`. Non-monotone → refuse to write the table, `UsageError` naming the breaking pair and its two ratios (a non-monotone table cannot be inverted, and writing it would produce confident nonsense later).
- [ ] Write `~/.config/scouter/wb/<camera>.json` in the design's shape, atomically; never overwrite an existing table without `confirm`.
- [ ] Gated; the dry run prints the K values it would set and the estimated time (`len(ks) * 1.8 s`).

- [ ] **Step 6: `wb.kelvin`**

```python
def _invert(samples: list[dict], ratio: float) -> tuple[int | None, dict]:
    """Bisect the sorted samples on r = coeff_r / coeff_b, then interpolate
    linearly between the two bracketing entries.

    Outside the sampled range the answer is None with a reason: extrapolating a
    blackbody curve past the measurements would be exactly the invented value
    this project refuses to produce.
    """
```

- [ ] Accepts `image` (reads its XMP), or `coeffs` + `camera` directly.
- [ ] Result: `{kelvin, interpolated, bracket, residual_ratio, exact, camera, table, reason}`. No table → `kelvin: None`, `reason` naming `scouter wb calibrate`.
- [ ] Improve the tier-4 refusal in `darkroom.undo_last`: `unsupported_undo` now carries the journalled coefficient vector, the target Kelvin, and "darktable stores white balance as camera-relative coefficients; run `scouter wb calibrate` to make this undoable, or restore these coefficients in the module's coefficients tab".

- [ ] **Step 7: Commit the table (host)**

Copy one EditingTrial ORF to `~/Pictures/ScouterScratch/em10iii.ORF`, run `wb calibrate` on it, and commit the result as `tests/fixtures/wb-em10iii.json` — ~20 numbers, and they make `wb.kelvin`'s and `outliers.list`'s unit tests run against real measurements with no darktable. (Open question 5 asks Pratik whether to do the same for the A7; phase 2 ships with the ORF table and `outliers.list` falls back to the ratio test for any other body, saying so.)

- [ ] **Step 8: `tests/test_wb.py`**

- [ ] `test_inverts_the_real_table` — three hand-computed points from the committed table, including one exactly on a sample (`interpolated is False`) and one between two (`interpolated is True`, `bracket` correct).
- [ ] `test_out_of_range_is_none_with_a_reason`.
- [ ] `test_non_monotone_table_refused` — a hand-built sample list with one inverted pair → `usage` naming both ratios; nothing written.
- [ ] `test_calibrate_refuses_inside_a_job`.
- [ ] `test_calibrate_dry_run_lists_the_k_values_and_estimate`.
- [ ] `test_calibrate_does_not_overwrite_without_confirm`.
- [ ] `test_latest_params_returns_the_whole_vector` — against the real sidecar: `coeff_r == 2.0125747`, `coeff_g == 1.0`, `coeff_b == 1.7052786`, `coeff_g2 is None` (inf → null, phase 1's rule), `params_hex` present.
- [ ] `test_undo_restores_every_coefficient` — `FakeBridge` with pinned paths: four `set` actions, **one** commit, and a verification failure on one component leaves the journal entry `undone: false`.
- [ ] `test_refusal_carries_the_coefficients` — with no pinned paths and no table, the `unsupported_undo` detail contains all four values.

---

### Task 12: The repeat-set cost — watermark, `history.changed`, one bounce instead of two

**Why here:** it is a change to `darkroom.set`, the most-exercised live verb, so it lands after the volume path is built and before the end-to-end run measures it.

**Files:**
- Create: `tests/test_history_changed.py`
- Modify: `scouter/core/history.py`, `scouter/core/darkroom.py`, `darktable_mcp/lua/darktable_mcp.lua`, `scouter/types.py`, `tests/lua/test_dispatcher.lua`, `docs/measurements-generated.md` (host run)

**Interfaces:**
- Produces: `history.commit(attempts=, expect_change=)`, verb `history.changed`, `history.watermark_write/watermark_fresh`, `SetResult.noop`.
- Consumes: `paths.job_state_path` (phase 1's per-job state file, ruling 30).

- [ ] **Step 1: The Lua side — `attempts` and `expect_change`**

In `methods["history.commit"]`:

```lua
  -- COMMIT_BOUNCE_ATTEMPTS is now the DEFAULT for `attempts`, not a constant.
  -- A caller who already knows the history may not change (the Python layer's
  -- no-op pre-check, design section 13) asks for one attempt and pays one
  -- bounce (~0.78 s) instead of two bounces plus two sidecar waits (~5.5 s,
  -- phase 1's measured worst case).
  local attempts = tonumber(p.attempts) or COMMIT_BOUNCE_ATTEMPTS
  if attempts < 1 then attempts = 1 elseif attempts > 3 then attempts = 3 end
  local expect_change = p.expect_change
  if expect_change == nil then expect_change = true end
  if not expect_change then attempts = 1 end
```

and in the return: `attempts_used = attempt_count`, `expect_change = expect_change`, and

```lua
    -- `ok` means "this image's history is now on disk". When the caller said it
    -- does not expect a change, a bounce that wrote nothing is still a success:
    -- sidecar_written = false reports the fact, and the Python readback resolves
    -- whether the requested state is the state on disk.
    ok = result.ok and (written ~= false or not expect_change),
```

- [ ] Dispatcher tests: `attempts` clamped to 1..3; `expect_change=false` forces one bounce and `ok` true with `sidecar_written=false`; the default path is unchanged (two attempts).

- [ ] **Step 2: The watermark**

In `scouter/core/history.py`:

```python
def _signature(xmp_path: str) -> dict | None:
    p = Path(xmp_path)
    if not p.exists():
        return None
    st = p.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def watermark_write(job: str, image_id: int, xmp_path: str) -> dict:
    """Record the sidecar's signature after a CONFIRMED commit.

    This is what makes the no-op pre-check safe. The XMP can be stale: an
    uncommitted darkroom change means the file on disk is older than
    darktable's in-memory history, and skipping a write on a stale read would
    silently drop a real edit. The watermark says "the last time Scouter
    committed this image, the sidecar looked exactly like this" -- so a sidecar
    that still looks like that is known-fresh, and anything else is not
    trusted.
    """


def watermark_fresh(job: str, image_id: int, xmp_path: str) -> tuple[bool, str | None]:
    """(fresh, reason_when_not). No watermark -> (False, "no watermark for this
    image in job <job>"): a fresh session, or an edit Pratik made by hand, both
    correctly fall through to the safe path."""
```

Stored in the existing per-job state file (`paths.job_state_path`) under a `watermarks` key, so no new file and no new pruning.

- [ ] Call `watermark_write` at the end of every successful `commit` **and** after a `_commit_with_readback` that resolved by readback.

- [ ] **Step 3: `history.changed`**

```python
@metrics.instrumented("history.changed")
def changed(image: str | int, *, job: str | None = None) -> ChangedResult:
    """Is this image's sidecar the one Scouter last committed?

    A pure read: `image.lookup` plus one `stat`. It moves nothing and costs no
    bounce, which is the whole point -- a script calls this before deciding to
    spend one.
    """
```

Result: `{image_id, path, fresh, watermark, sidecar, reason}`.

- [ ] **Step 4: The no-op pre-check in `darkroom.set_param`**

Insert after the existing `_before_state` read and **before** the action:

```python
    fresh, why = _history.watermark_fresh(job, resolved.image_id, resolved.xmp_path)
    if (fresh and before.get("enabled") is not False
            and _close(value, before.get("value"))):
        # Zero bridge writes, zero bounces. darktable writes a sidecar only when
        # the history actually CHANGES (phase 1 live finding 4), so asking for
        # the value the XMP already carries used to cost two bounces and ~5.5 s
        # discovering that nothing happened.
        return SetResult(
            image_id=resolved.image_id, path=resolved.path, module=module, param=param,
            instance=instance, requested=value, applied=before.get("value"),
            previous=before.get("value"), committed=True, noop=True, source="xmp",
            warnings=[f"no-op: {module}.{param} is already "
                      f"{before.get('value')} in the sidecar, which Scouter last "
                      f"committed; nothing was sent to darktable"])
```

- [ ] When the pre-check matches the value but the watermark is **stale**, issue the action and call `_commit(..., expect_change=False, attempts=1)` — one bounce, ~1.0 s.
- [ ] A set that really moves is untouched: `expect_change=True`, default attempts, ~1.8 s.
- [ ] `nudge` gets the same treatment via the same helper (`_noop_shortcut(...)`), since `nudge` computes an absolute target before writing.
- [ ] `SetResult.noop: bool = False` in `types.py`.

- [ ] **Step 5: `tests/test_history_changed.py`**

- [ ] `test_fresh_watermark_noop_makes_zero_write_calls` **(E8)** — `FakeBridge.count("darkroom.action") == 0` and `count("history.commit") == 0`; `noop is True`; `committed is True`.
- [ ] `test_stale_watermark_uses_one_attempt` — the recorded `history.commit` params carry `attempts == 1, expect_change is False`, and exactly one commit call.
- [ ] `test_real_change_is_unaffected` — `expect_change is True` and the default attempts.
- [ ] `test_no_watermark_is_not_fresh` — and the reason says so.
- [ ] `test_disabled_module_is_never_a_noop` — `enabled: False` with a matching value still writes.
- [ ] `test_watermark_written_after_readback_resolution`.
- [ ] `test_changed_is_a_pure_read` — only `image.lookup` in `FakeBridge.calls`.
- [ ] `test_nudge_shortcut`.

- [ ] **Step 6: Measure it (host)**

Add to `tests/test_host_phase2.py::test_repeat_set_cost`: set exposure to a fresh value, then set it again twice; assert the second and third calls are **< 1.5 s** and write the three timings plus the phase 1 baseline (5.5-5.7 s) to `docs/measurements-generated.md`.

---

### Task 13: Backup — `backup.run`, `backup.verify`, the launchd job

**Why here:** it needs the library layout to exist, and it is the last piece the import gate promises.

**Files:**
- Create: `scouter/core/backup.py`, `ops/launchd/com.pratik.scouter-backup.plist.template`, `ops/install-backup-job.sh`, `tests/test_backup.py`, `tests/test_ops_launchd.py`
- Modify: `scouter/types.py`

**Interfaces:**
- Produces: verbs `backup.run` and `backup.verify`; `BackupResult`, `BackupVerifyResult`.
- Consumes: `config.require_backup`, `tarfile`, `subprocess` (`openssl`/`age`/`rsync`).

- [ ] **Step 1: The member list**

```python
def _members(cfg) -> list[tuple[Path, str]]:
    """(absolute source, name inside the archive), SORTED so the archive is
    deterministic and two nights' manifests are diffable.

    Small and precious: every .xmp under the library, every job's .scouter/,
    ~/.config/scouter/ (config + the WB tables), metrics.db, events.jsonl, and
    <library>/scouter/memory/ (empty in phase 2, reserved for Dragon Radar --
    it is in the set now so phase 3 does not have to remember).
    """
```

- [ ] darktable's `library.db`, when `cfg.backup.include_darktable_db`: **copied as a file, never opened.** The manifest records `"consistency": "file-copy, may be mid-write"`, because darktable may be writing it and the XMPs are the authoritative record either way. `sqlite3`'s backup API is deliberately not used — that would be reading darktable's database, which the invariant forbids.

- [ ] **Step 2: Tar with a hash in the same pass**

```python
def _write_archive(members, out: Path) -> tuple[str, int, int]:
    """tar.gz the members, hashing the plaintext as it streams.

    stdlib `tarfile`, not the `tar` binary: we already have the exact file list,
    a stream wrapper gives us the sha256 in the same pass instead of a second
    read, and there is no shell quoting of the job directory names -- which
    contain spaces by design (spec 4.6).
    """
    digest = hashlib.sha256()

    class _Hashing:
        def __init__(self, fh): self.fh = fh
        def write(self, data): digest.update(data); return self.fh.write(data)

    count = 0
    with out.open("wb") as raw:
        with tarfile.open(fileobj=_Hashing(raw), mode="w:gz") as tar:
            for source, name in members:
                tar.add(source, arcname=name, recursive=False)
                count += 1
    return digest.hexdigest(), out.stat().st_size, count
```

- [ ] **Step 3: Encryption — `age` if present, else `openssl enc`**

```python
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
        out = plain.with_suffix(plain.suffix + ".age")
        args = ["age", "-r", b.age_recipient, "-o", str(out), str(plain)]
    elif b.encryption == "openssl":
        out = plain.with_suffix(plain.suffix + ".enc")
        args = [_openssl(), "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                "-md", "sha512", "-salt", "-in", str(plain), "-out", str(out),
                "-pass", f"file:{b.openssl_passfile}"]
    else:
        return plain, []
    _run(args, what="encrypt")
    return out, args
```

- [ ] `_openssl()` prefers `/opt/homebrew/bin/openssl` (OpenSSL 3.6.3, verified 2026-09-14), falls back to `shutil.which("openssl")`, and raises `EncryptionUnavailable` when neither exists.
- [ ] The passfile never appears in `argv` — only `-pass file:<path>` does, so it cannot leak through `ps`.

- [ ] **Step 4: rsync, manifest, pruning**

- [ ] `rsync -a --partial --human-readable --stats --exclude '.scouter/' --exclude 'exports/' <library>/ <target>/library/`. Exports are excluded: they regenerate from raw + XMP and they are the biggest thing on disk. Raws are `0444`, which `-a` preserves. Parse `--stats` for `files_transferred` and `bytes`; a non-zero exit is a warning with the tail of stderr, not a failed backup (the archive is the precious half).
- [ ] Manifest `<target>/archives/<ts>/backup.json` in the design's shape.
- [ ] Prune archive directories beyond `keep_archives`, oldest first, and **never the newest two whatever `keep_archives` says** — a `keep_archives: 1` typo must not leave one copy of a broken night.
- [ ] Gated: it writes outside the library and deletes old archives. Dry run lists the members, the totals, the rsync command and what pruning would remove.

- [ ] **Step 5: `backup.verify`**

Read-only outside its temp directory. Checks, each named in the result: `ciphertext_sha256`, `decrypt`, `plaintext_sha256`, `member_count`, `sampled_members` (N random members re-hashed against the manifest), `sampled_raws` (N raw checksums from the jobs' `import.json` compared against `<target>/library/`). `verified` is false if **any** check fails, and the failing check is named.

- [ ] **Step 6: launchd**

`ops/launchd/com.pratik.scouter-backup.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.pratik.scouter-backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>@PYTHON@</string><string>-m</string><string>scouter</string>
    <string>--job</string><string>backup-nightly</string>
    <string>backup</string><string>run</string><string>--yes</string>
  </array>
  <key>WorkingDirectory</key><string>@REPO@</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SCOUTER_JOB</key><string>backup-nightly</string>
    <key>OBSIDIAN_VAULT_PATH</key><string>@VAULT@</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>@LOG@</string>
  <key>StandardErrorPath</key><string>@LOG@</string>
</dict>
</plist>
```

`ops/install-backup-job.sh` renders the placeholders, runs `plutil -lint` on the result, `launchctl bootstrap gui/$UID`s it, and prints the next run time; `--uninstall` does `bootout` and removes the plist. `set -euo pipefail`; it refuses to install when `scouter config show` reports the gate is not `ok`.

- [ ] **Step 7: `tests/test_backup.py` and `tests/test_ops_launchd.py`**

- [ ] `test_member_list_is_sorted_and_covers_the_set` — xmp, `.scouter/`, config, `wb/`, metrics.db, `memory/`.
- [ ] `test_exports_and_raws_not_in_the_archive`.
- [ ] `test_plaintext_hash_round_trips` — tar, encrypt with real `openssl`, decrypt, re-hash, equal (skipped with a printed reason if no `openssl`).
- [ ] `test_verify_fails_on_one_flipped_byte` — flip a byte of the ciphertext → `verified is False` and the failing check is `ciphertext_sha256`.
- [ ] `test_verify_names_every_check`.
- [ ] `test_pruning_never_removes_the_newest_two` — five archives, `keep_archives=1` → three removed, two kept.
- [ ] `test_gate_refusal_before_any_work` — a gate-failing config → `BackupNotConfigured` and nothing written to the target.
- [ ] `test_rsync_failure_is_a_warning_not_a_failure` — a fake `rsync` exiting 23 → `outcome="ok"` with a warning carrying the stderr tail.
- [ ] `test_passfile_not_in_argv`.
- [ ] `test_dry_run_writes_nothing`.
- [ ] `test_template_lints` — render with fake values and run `plutil -lint` (skipped with a reason if `plutil` is absent); assert the label, the 03:30 schedule, `RunAtLoad false`, and that no placeholder `@…@` survives. **Installs nothing.**

---

### Task 14: The 30-frame synthetic job, end to end on this Mac

**Why last:** it is the phase's exit criterion, and it is the only test that can prove the stages compose.

**Files:**
- Create: `tests/make_synthetic_job.py`, `tests/test_host_phase2.py` (completed here; tasks 7, 11 and 12 added their probes to it)
- Modify: `scouter/mcp/schemas.py`, `tests/fixtures/scouter_mcp_schemas.json`, `scouter/cli/main.py`, `docs/measurements.md`, `README.md`

**Interfaces:**
- Produces: the synthetic-job builder, the end-to-end host test, the regenerated golden snapshot, the 18 new MCP tools, `doctor`'s four new lines.
- Consumes: everything above.

- [ ] **Step 1: The synthetic card builder**

```python
"""Build a fake 30-frame card from the four EditingTrial ORFs.

The copies are byte-identical apart from their EXIF capture times, which are
patched IN PLACE: DateTime (0x0132) and DateTimeOriginal (0x9003) are
fixed-length ASCII ("2026:06:28 15:49:28", 20 bytes with the NUL), so
`core.exif.datetime_offsets` locates them and an equal-length string is written
over them. No re-encoding, no EXIF library, and the reader that finds the
offsets is the reader under test.

Layout: 6 bursts of 5 frames, 1 s apart inside a burst, 45 s between bursts --
so `cull.propose` has real bursts to find with the 2.0 s default gap.

LIMITATION, stated rather than hidden: copies of the same ORF have identical
pixels, so `sharpness` ties inside a burst. The test asserts ONE pick per burst,
not which frame wins, and asserts the result reports tie_broken_by == "seq".
"""

SOURCES = ("P6281348.ORF", "P6281361.ORF", "P6281368.ORF", "P6281373.ORF")
BURSTS, PER_BURST, GAP_S, BURST_GAP_S = 6, 5, 1, 45


def build(out: Path, *, source_dir: Path = Path("~/Pictures/EditingTrial")) -> dict:
    """Write <out>/card/DCIM/100OLYMP/*.ORF and return a manifest of the plan."""
```

- [ ] A `__main__` guard so it is runnable by hand: `python3 -m tests.make_synthetic_job --out ~/Pictures/ScouterSynthetic`.
- [ ] `--seed` is not needed: the builder is fully deterministic, and the test asserts that building twice produces identical bytes.

- [ ] **Step 2: The MCP tools**

- [ ] Add the four shared fragments to `scouter/mcp/schemas.py`: `JOB_SLUG` (`{"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,47}$", "description": "..."}`), `PRESET` (enum built from `export.PRESETS`), `RATING` (`0..5`), `IMAGES` (array of `IMAGE`, `minItems: 1`).
- [ ] Add all 18 tools, each with `additionalProperties: false`, explicit `required`, `dry_run` on writers and `confirm` on gated tools whose **description names what it authorises** (`"authorises copying <n> files into the library and writing ratings"` — phase 1's convention).
- [ ] Run `tests/test_layer_parity.py` and fix whatever it reports until the registry, the CLI tree and the tool table cover the same 35 verbs with the same argument names.

- [ ] **Step 3: Regenerate the golden snapshot — once, deliberately**

- [ ] Regenerate `tests/fixtures/scouter_mcp_schemas.json`, then **read the diff** and confirm it contains only: 18 added tools, `history_commit` gaining `attempts` and `expect_change`, `export_run`'s `preset` enum gaining `client-proof` and `print-tiff`, and `style_extract` gaining `no_filter`. Anything else in the diff is a bug in an earlier task, not a snapshot to accept.
- [ ] Record the reviewed diff summary in the commit message.

- [ ] **Step 4: `doctor`'s four new lines**

`config_path` + whether it parses, `backup_gate` (`"ok"` or `"<code>: <message>"` from `config.require_backup`), `encryption_tool` (the resolved `age`/`openssl` path or `None`), `library_root` + `shutil.disk_usage` free bytes.

- [ ] **Step 5: The end-to-end host test**

`tests/test_host_phase2.py`, one `setUpClass` darktable session, `SCOUTER_JOB=phase2-e2e`, `SCOUTER_CACHE` and `SCOUTER_CONFIG` pointed at a temp tree, the backup target a temp directory, `$OBSIDIAN_VAULT_PATH` pointed at a temp vault (**the real vault is never written by a test**):

- [ ] `test_00_gate_refuses_first` (E1) — with `backup.enabled: false`, `library import` exits 11 and nothing was created.
- [ ] `test_01_import_30_frames` (E2) — 30 files in `raw/`, all `0444`, `library verify` clean, `import.json` and `checksums.sha256` present, the film roll registered, the temp vault note written with `ai-first: true`.
- [ ] `test_02_cull` (E3) — `cull propose` finds 6 bursts and proposes 6 picks with `tie_broken_by == "seq"`; `cull apply --yes` writes the ratings and reports `verified + unverified == changed`.
- [ ] `test_03_hero_edit_and_repeat_set` (E8) — open the hero, `darkroom set exposure 0.6`, `darkroom set sigmoid contrast 1.3`, then repeat `exposure 0.6` twice; both repeats **< 1.5 s** and `noop is True` on at least the second.
- [ ] `test_04_style_extract_filtered` (E4) — `--modules exposure,sigmoid`; the `.dtstyle` holds exactly two operations; `modules_honoured is True`; darktable's `list_styles` shows it.
- [ ] `test_05_style_apply_set` (E5) — 29 frames, `failed == 0`, `per_image` has 29 rows.
- [ ] `test_06_outliers` (E6) — against the hero; every row carries `ev_proxy_delta` and the tolerances used; `checked == 29`.
- [ ] `test_07_release` (E7) — `--preset client-proof --min-rating 1 --yes`; every JPEG's longest edge is 2048 (read from the JPEG SOF header with `struct`, 20 lines, no Pillow); `export verify` clean.
- [ ] `test_08_backup` (E9) — `backup run --yes` then `backup verify` → `verified is True`, every check named.
- [ ] `test_09_metrics` — `scouter metrics summary --since 1d` shows one event per verb invoked, `phase == 2`, and no `error:` outcome other than the deliberate gate refusal in `test_00`.
- [ ] `tearDownClass`: `history.commit` (ruling 16) → `osascript` quit → 20 s → `SIGTERM` → 10 s → `SIGKILL`; assert darktable is gone (ruling 14: a real assertion); delete the synthetic tree unless `SCOUTER_KEEP_SYNTHETIC=1`.
- [ ] Per-stage wall times to `docs/measurements-generated.md`.

- [ ] **Step 6: `docs/measurements.md` and `README.md`**

- [ ] Copy the numbers from `docs/measurements-generated.md` into `docs/measurements.md` **by hand** (phase 1's convention: the test no longer rewrites the reviewed record), in new sections: "Phase 2: import throughput", "Set stats, 30 frames at 512 px", "Repeat-set cost (before: 5.5-5.7 s)", "style.apply_set", "export.release client-proof", "backup.run / verify", ".dtstyle format as exported by darktable 5.4.1", "WB coefficient slider probe", each with the host, the date and what produced it.
- [ ] `README.md`: a phase 2 section — the config file, the job layout, the six new CLI groups, the backup gate, how to install the launchd job, and the honest limits (darktable film rolls after a move; `openssl enc` is unauthenticated and why that is closed; `ev_proxy` is not EV; Kelvin needs a calibration table).

- [ ] **Step 7: Full suite, lint, and the exit check**

- [ ] `uv run --extra dev python -m pytest tests -q` — green without darktable.
- [ ] `SCOUTER_HOST_TESTS=1 uv run --extra dev python -m pytest tests/test_host_phase2.py -q` — green, **twice in a row** (phase 1's live runs only stabilised on the second pass; a single green run is not evidence).
- [ ] `uv run --extra dev ruff check . && uv run --extra dev black --check .`
- [ ] Confirm E1-E11 each have a named passing test, and record in the ledger that E12 (the 300-frame job) is Pratik's to run.
