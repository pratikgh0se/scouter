"""Named presets (phase 3 design §8). A preset IS a darktable style.

Scouter adds a default module list (this session's edits), a durable copy in
`<library_root>/scouter/memory/presets/` (the directory phase 2 reserved,
inside the backup set) and apply-to-the-open-image through a bounce (task 8).

`preset.save` is gated the same way `style.extract` is (confirm required only
to replace an existing name), but the existing-name CHECK
(`style._existing_styles()`) happens before the darkroom image is resolved or
anything is flushed/committed (controller ruling): an existing name with no
`confirm` returns the dry-run plan without any darktable write or commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from scouter import __version__, config, metrics, paths
from scouter.core import darkroom, dtbridge, explain, journal, style
from scouter.core.moves import EDUCATIVE_ARG
from scouter.core.style import _now_z
from scouter.errors import ScouterError, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import PresetListResult, PresetResult

INDEX = "presets.json"

#: verbs (task 6) that never count as "this session's edits": a reset
#: discards the module rather than editing it, and `preset.apply` is not an
#: edit made by hand -- session_modules names only what a caller SET.
_EXCLUDED_VERBS = ("module.reset", "preset.apply")


def memory_dir(cfg: config.Config) -> Path | None:
    if not cfg.library_root:
        return None
    return Path(cfg.library_root).expanduser() / "scouter" / "memory" / "presets"


def session_modules(job: str, image_id: int) -> list[str]:
    """Modules this job actually changed on this image, first-touched order (P3-R9).

    Skips undone entries (a whole group `undo_last` rewound), entries whose
    write never landed (`outcome` starting `"error"`), `module.reset` /
    `preset.apply` (not a hand-made edit), and anything in
    `cfg.style.never_include` (per-image geometry/repairs never ride a style
    either way -- `style.extract` would refuse them explicitly).
    """
    never = set(config.load().style.never_include)
    out: list[str] = []
    for e in journal.read_all(job):
        if e.image_id != image_id or e.undone or not e.module:
            continue
        if e.verb in _EXCLUDED_VERBS:
            continue
        if (e.outcome or "").startswith("error"):
            continue
        if e.after is None:
            continue
        if e.module in never or e.module in out:
            continue
        out.append(e.module)
    return out


def _read_index(directory: Path) -> dict:
    path = directory / INDEX
    if not path.exists():
        return {"schema": 1, "presets": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise UsageError(f"{path} is not a schema-1 preset index", detail={"path": str(path)})
    return data


def _write_index(directory: Path, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".presets-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, directory / INDEX)


def _source_path(cfg: config.Config, path: str) -> str:
    if cfg.library_root:
        try:
            return str(Path(path).resolve().relative_to(Path(cfg.library_root).expanduser().resolve()))
        except ValueError:
            pass
    return path


@metrics.instrumented("preset.save")
def save(name: str, *, modules: list[str] | None = None, description: str = "",
         educative: str | None = None, confirm: bool = False, dry_run: bool = False,
         job: str | None = None) -> PresetResult:
    """Save the darkroom image's session edits as a named darktable style."""
    job = job or current_job()
    paths.validate_style_name(name)
    level = explain.resolve_level(educative)
    if dry_run:
        return PresetResult(
            outcome="dry_run", name=name, action="save", modules=list(modules or []),
            plan=[{"action": "preset.save", "name": name,
                   "modules": list(modules) if modules else "this session's edits"}],
            reason="dry run: no darktable calls; the darkroom image was not resolved",
        )
    # Gate check first (controller ruling): resolve nothing, flush nothing,
    # commit nothing until we know whether this name would replace one.
    replaces = name in style._existing_styles()
    if replaces and not confirm:
        return PresetResult(
            outcome="dry_run", name=name, action="save", modules=list(modules or []),
            plan=[{"action": "preset.save", "name": name, "replaces_existing": True}],
            reason=f"a preset named {name!r} already exists; re-run with confirm=true / "
            "--yes to replace it",
        )
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    warnings: list[str] = []
    try:
        darkroom._commit(job, resolved.image_id, strategy="auto", attempts=1, expect_change=False)
    except ScouterError as e:
        warnings.append(f"flush before saving did not confirm ({e.code}): the style reflects the "
                        "last committed sidecar")
    chosen = list(modules) if modules else session_modules(job, resolved.image_id)
    if not chosen:
        raise UsageError(
            "nothing was edited on this image in this session; pass --modules",
            detail={"image_id": resolved.image_id, "job": job},
        )
    extract = getattr(style.extract, "__wrapped__", style.extract)
    sr = extract(resolved.image_id, name, chosen,
                 description=description or f"Scouter preset from {Path(resolved.path).name}",
                 confirm=confirm, job=job)
    if sr.outcome == "dry_run":
        return PresetResult(outcome="dry_run", name=name, action="save", modules=chosen,
                            plan=sr.plan, reason=sr.reason or "a style of that name exists; "
                            "re-run with confirm=true / --yes to replace it")
    warnings.extend(sr.warnings)
    cfg = config.load()
    mem = memory_dir(cfg)
    memory_path: str | None = None
    replaced = False
    if mem is None:
        warnings.append("saved in darktable and in the styles cache only; set library_root to "
                        "keep presets in the backed-up library memory")
    elif sr.dtstyle_path:
        mem.mkdir(parents=True, exist_ok=True)
        dest = mem / f"{name}.dtstyle"
        if dest.exists():
            dest.rename(mem / f"{name}.{_now_z().replace(':', '')}.dtstyle")
            replaced = True
        shutil.copyfile(sr.dtstyle_path, dest)
        memory_path = str(dest)
        index = _read_index(mem)
        index["presets"] = [p for p in index["presets"] if p.get("name") != name]
        index["presets"].append({
            "name": name, "created_at": _now_z(),
            "source_path": _source_path(cfg, resolved.path), "image_id": resolved.image_id,
            "modules": list(sr.operations_included or chosen), "dtstyle": dest.name,
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "description": description, "scouter_version": __version__,
        })
        _write_index(mem, index)
    included = list(sr.operations_included or chosen)
    return PresetResult(
        image_id=resolved.image_id, path=resolved.path, name=name, action="save",
        modules=included, dtstyle_path=sr.dtstyle_path, memory_path=memory_path,
        replaced=replaced or sr.created is False, warnings=warnings,
        explain=explain.preset_lines("save", name, included, len(included), level=level, job=job),
    )


@metrics.instrumented("preset.list")
def list_presets(*, job: str | None = None) -> PresetListResult:
    """darktable's styles merged with Scouter's preset index (one bridge call)."""
    job = job or current_job()
    in_dt = set(style._existing_styles())
    mem = memory_dir(config.load())
    index = _read_index(mem) if mem else {"presets": []}
    ours = {p["name"]: p for p in index["presets"]}
    rows = []
    for name in sorted(set(ours) | in_dt):
        p = ours.get(name, {})
        rows.append({"name": name, "scouter": name in ours, "in_darktable": name in in_dt,
                     "modules": p.get("modules", []), "created_at": p.get("created_at")})
    missing = sorted(n for n in ours if n not in in_dt)
    warnings = [f"{n} is in Scouter's index but not in darktable: re-import "
                f"{mem / (n + '.dtstyle')} in darktable's styles module, or save it again"
                for n in missing] if mem else []
    return PresetListResult(presets=rows, darktable_only=sorted(in_dt - set(ours)),
                            missing_in_darktable=missing, warnings=warnings)


PRESET_NAME_ARG = ArgSpec("name", "str", required=True, positional=True,
                          help="preset (darktable style) name")

for verb in (
    Verb(
        id="preset.save",
        group="preset",
        fn=save,
        args=(
            PRESET_NAME_ARG,
            ArgSpec("modules", "list[str]", default=None,
                    help="modules to keep (default: those changed in this session)"),
            ArgSpec("description", "str", default="", help="style description"),
            EDUCATIVE_ARG,
            ArgSpec("confirm", "bool", default=False,
                    help="required only to replace an existing preset of this name"),
            ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing"),
        ),
        writes=True,
        gated=True,
        summary="Save this session's edits on the darkroom image as a named preset (style).",
    ),
    Verb(
        id="preset.list",
        group="preset",
        fn=list_presets,
        args=(),
        writes=False,
        gated=False,
        summary="List presets: darktable's styles merged with Scouter's index.",
    ),
):
    register(verb)
