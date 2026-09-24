"""Scouter's configuration: JSON, because tomllib is 3.11+ and our floor is 3.10.

One frozen dataclass tree (each field defaulted on the dataclass itself, so
there is no separate `DEFAULTS` dict to drift out of sync) and a loader that
REJECTS unknown keys. A typo in `backup.target` must not silently disarm the
import gate, so an unrecognised key is a `usage` error naming the nearest
known one.
"""

from __future__ import annotations

import datetime
import difflib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scouter import metrics, paths
from scouter.errors import BackupNotConfigured, UsageError
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ConfigResult

CONFIG_VERSION = 1

#: Row 346: the educative mode's three levels (design §7.1).
EDUCATIVE_LEVELS: tuple[str, ...] = ("off", "brief", "full")


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
    encryption: str = "openssl"  # "age" | "openssl" | "none"
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
        "crop",
        "clipping",
        "retouch",
        "spots",
        "flip",
        "rotatepixels",
        "censorize",
        "watermark",
        "ashift",
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
    path: str | None = None  # where this config was loaded from, None = defaults
    exists: bool = False
    import_: ImportConfig = field(default_factory=ImportConfig)
    cameras: tuple[tuple[str, str], ...] = ()  # tuple of pairs: frozen and ordered
    vault: VaultConfig = field(default_factory=VaultConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    tolerances: Tolerances = field(default_factory=Tolerances)
    cull: CullConfig = field(default_factory=CullConfig)
    style: StyleConfig = field(default_factory=StyleConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    educative: str = "brief"

    def camera_map(self) -> dict[str, str]:
        return dict(self.cameras)

    def root(self) -> Path:
        if not self.library_root:
            raise UsageError(
                "no `library_root` in the config: every library verb needs one",
                detail={"config": self.path or str(paths.config_path())},
            )
        return Path(self.library_root).expanduser()


SECTIONS = {
    "import": ("import_", ImportConfig),
    "vault": ("vault", VaultConfig),
    "backup": ("backup", BackupConfig),
    "tolerances": ("tolerances", Tolerances),
    "cull": ("cull", CullConfig),
    "style": ("style", StyleConfig),
    "export": ("export", ExportConfig),
    "stats": ("stats", StatsConfig),
}
TOP_KEYS = {"version", "library_root", "cameras", "educative", "_comment", *SECTIONS}


def _reject_unknown(where: str, given: set[str], known: set[str]) -> None:
    unknown = sorted(given - known)
    if not unknown:
        return
    guesses = {k: (difflib.get_close_matches(k, sorted(known), n=1) or [None])[0] for k in unknown}
    raise UsageError(
        f"unknown config key(s) in {where}: "
        + ", ".join(
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

    kwargs: dict = {
        "path": str(cfg_path),
        "exists": True,
        "version": int(raw.get("version", CONFIG_VERSION)),
    }
    if raw.get("library_root"):
        kwargs["library_root"] = str(Path(str(raw["library_root"])).expanduser())
    if "cameras" in raw:
        cameras = raw["cameras"]
        if not isinstance(cameras, dict):
            raise UsageError(
                "`cameras` must be an object of model -> slug", detail={"config": str(cfg_path)}
            )
        kwargs["cameras"] = tuple(
            (str(k), paths.validate_slug(str(v))) for k, v in sorted(cameras.items())
        )
    if "educative" in raw:
        level = raw["educative"]
        if level not in EDUCATIVE_LEVELS:
            raise UsageError(
                f"`educative` must be one of {', '.join(EDUCATIVE_LEVELS)}, got {level!r}",
                detail={"config": str(cfg_path), "educative": level},
            )
        kwargs["educative"] = level
    for name, (attr, klass) in SECTIONS.items():
        if name not in raw:
            continue
        section = raw[name]
        if not isinstance(section, dict):
            raise UsageError(
                f"config section `{name}` must be an object", detail={"config": str(cfg_path)}
            )
        fields = {f.name for f in klass.__dataclass_fields__.values()}
        _reject_unknown(f"{cfg_path}:{name}", set(section) - {"_comment"}, fields)
        built = {}
        for key, value in section.items():
            if key == "_comment":
                continue
            built[key] = tuple(value) if isinstance(value, list) else value
        kwargs[attr] = klass(**built)
    return Config(**kwargs)


#: Every check the gate runs, in order. `library.import` and `backup.run` call it;
#: reads, cull, style, outliers and export deliberately DO NOT -- blocking a proof
#: export because a backup disk is unplugged would be gate theatre (design 4.3).
def require_backup(cfg: Config) -> BackupConfig:
    b = cfg.backup

    def refuse(check: str, message: str, **detail) -> None:
        raise BackupNotConfigured(
            message,
            detail={"check": check, "config": cfg.path or str(paths.config_path()), **detail},
        )

    if not cfg.exists:
        refuse("config_missing", f"no Scouter config at {cfg.path}: nothing says where backups go")
    if not b.enabled:
        refuse("disabled", "`backup.enabled` is false: importing would leave the job unprotected")
    if not b.target:
        refuse("no_target", "`backup.target` is not set")
    target = Path(b.target).expanduser()
    if not target.parent.exists():
        refuse(
            "target_parent_missing",
            f"{target}'s parent does not exist (an unplugged disk looks exactly like this)",
            target=str(target),
        )
    probe = target if target.exists() else target.parent
    if not os.access(probe, os.W_OK):
        refuse("target_not_writable", f"{probe} is not writable", target=str(target))
    if b.encryption not in ("age", "openssl", "none"):
        # A typo here (e.g. "opnessl") must not silently pass the gate as if it
        # were a configured, working encryption -- fail closed, not open.
        raise UsageError(
            f"unknown `backup.encryption` {b.encryption!r}: must be one of " "age, openssl, none",
            detail={"check": "unknown_encryption", "encryption": b.encryption},
        )
    if b.encryption == "none" and not b.allow_unencrypted:
        refuse(
            "unencrypted",
            '`backup.encryption` is "none"; the spec calls for an encrypted archive. '
            "Set `backup.allow_unencrypted` to true to accept that deliberately",
        )
    if b.encryption == "age":
        if not b.age_recipient:
            refuse(
                "age_recipient_missing",
                '`backup.encryption` is "age" but ' "`backup.age_recipient` is not set",
            )
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
            refuse(
                "passfile_mode",
                f"{pf} has mode {mode:04o}; it must be 0600",
                passfile=str(pf),
                mode=f"{mode:04o}",
            )
    return b


def job_dir(cfg: Config, date: str, slug: str) -> Path:
    """`<library_root>/<YYYY>/<YYYY-MM-DD slug>` -- a SPACE, exactly as spec 4.6 writes it."""
    paths.validate_slug(slug)
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise UsageError(f"date must be YYYY-MM-DD, got {date!r}", detail={"date": date})
    try:
        # len/dash check above catches shape; this catches garbage inside the
        # shape ("2026-13-40") that would otherwise build a directory name
        # for a date that never happened.
        datetime.date.fromisoformat(date)
    except ValueError as e:
        raise UsageError(f"date must be YYYY-MM-DD, got {date!r}", detail={"date": date}) from e
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
    matches = sorted(
        p for p in cfg.root().glob(f"*/* {slug}") if p.is_dir() and p.name.endswith(f" {slug}")
    )
    if not matches:
        raise UsageError(
            f"no job with slug {slug!r} under {cfg.root()}",
            detail={"slug": slug, "library_root": str(cfg.root())},
        )
    if len(matches) > 1:
        raise UsageError(
            f"{len(matches)} jobs share the slug {slug!r}; pass --date",
            detail={"slug": slug, "candidates": [p.name for p in matches]},
        )
    return matches[0]


@metrics.instrumented("config.init")
def init(
    path: str | None = None, *, confirm: bool = False, dry_run: bool = False, job: str | None = None
) -> ConfigResult:
    target = Path(path).expanduser() if path else paths.config_path()
    exists = target.is_file()
    has_age = bool(shutil.which("age"))
    starter = {
        "_comment": "Scouter config. Docs: docs/superpowers/specs/"
        "2026-09-14-scouter-phase2-design.md section 4. `educative` (added phase 3, "
        "design section 7.1) controls how much explanation write verbs add to their "
        "summaries: off, brief, full",
        "version": CONFIG_VERSION,
        "library_root": str(Path.home() / "Pictures" / "EditingTrial-library"),
        "cameras": {"E-M10 Mark III": "em10iii", "ILCE-7": "a7"},
        "educative": "brief",
        "backup": {
            "_comment": "library.import REFUSES until enabled is true and target passes the gate",
            "enabled": False,
            "target": None,
            "rsync_raws": True,
            "encryption": "age" if has_age else "openssl",
            "age_recipient": None,
            "openssl_passfile": str(paths.config_dir() / "backup.pass"),
            "keep_archives": 14,
        },
    }
    plan = [
        {
            "action": "config.init",
            "path": str(target),
            "exists": exists,
            "encryption": starter["backup"]["encryption"],
        }
    ]
    if dry_run or not confirm:
        return ConfigResult(
            outcome="dry_run",
            config_path=str(target),
            config_exists=exists,
            plan=plan,
            reason=(
                f"{target} already exists and would be replaced; re-run with --yes"
                if exists
                else "re-run with --yes to write the starter config"
            ),
            config=starter,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.part")
    tmp.write_text(json.dumps(starter, indent=2) + "\n")
    os.replace(tmp, target)
    return ConfigResult(
        config_path=str(target),
        config_exists=True,
        written=True,
        config=starter,
        warnings=["backup.enabled is false: set a target and flip it before importing"],
    )


@metrics.instrumented("config.show")
def show(path: str | None = None, *, job: str | None = None) -> ConfigResult:
    """The loaded config plus the backup gate's verdict, so `config show` answers
    "will an import work right now?" without actually attempting one."""
    cfg = load(path)
    try:
        require_backup(cfg)
        gate = "ok"
    except BackupNotConfigured as e:
        gate = f"{e.code}: {e.message}"
    return ConfigResult(
        config_path=cfg.path or "",
        config_exists=cfg.exists,
        backup_gate=gate,
        config=asdict(cfg),
    )


for verb in (
    Verb(
        id="config.init",
        group="config",
        fn=init,
        args=(
            ArgSpec(
                "path",
                "str",
                help="override the config path (default: $SCOUTER_CONFIG "
                "or ~/.config/scouter/config.json)",
            ),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required to write, or to replace an existing file",
            ),
        ),
        writes=True,
        gated=True,
        summary="Write the starter Scouter config file.",
    ),
    Verb(
        id="config.show",
        group="config",
        fn=show,
        args=(ArgSpec("path", "str", help="override the config path"),),
        writes=False,
        gated=False,
        summary="Show the loaded config and whether the backup gate currently passes.",
    ),
):
    register(verb)
