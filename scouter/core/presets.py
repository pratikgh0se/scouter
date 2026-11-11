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

import difflib
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
from scouter.errors import CommitTimeout, NoSidecar, NotInDarkroom, ScouterError, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import PresetListResult, PresetResult, StyleSetResult

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
            return str(
                Path(path).resolve().relative_to(Path(cfg.library_root).expanduser().resolve())
            )
        except ValueError:
            pass
    return path


@metrics.instrumented("preset.save")
def save(
    name: str,
    *,
    modules: list[str] | None = None,
    description: str = "",
    educative: str | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> PresetResult:
    """Save the darkroom image's session edits as a named darktable style."""
    job = job or current_job()
    paths.validate_style_name(name)
    level = explain.resolve_level(educative)
    if dry_run:
        return PresetResult(
            outcome="dry_run",
            name=name,
            action="save",
            modules=list(modules or []),
            plan=[
                {
                    "action": "preset.save",
                    "name": name,
                    "modules": list(modules) if modules else "this session's edits",
                }
            ],
            reason="dry run: no darktable calls; the darkroom image was not resolved",
        )
    # Gate check first (controller ruling): resolve nothing, flush nothing,
    # commit nothing until we know whether this name would replace one.
    replaces = name in style._existing_styles()
    if replaces and not confirm:
        return PresetResult(
            outcome="dry_run",
            name=name,
            action="save",
            modules=list(modules or []),
            plan=[{"action": "preset.save", "name": name, "replaces_existing": True}],
            reason=f"a preset named {name!r} already exists; re-run with confirm=true / "
            "--yes to replace it",
        )
    resolved = darkroom._current_image()
    darkroom._require_job_image(job, resolved.image_id)
    dtbridge.require_darkroom()
    warnings: list[str] = []
    # Final review #4: only a CommitTimeout (darktable not settling within
    # one bounce) is a warning, exactly as `moves._precommit`; a wrong view,
    # a dead bridge or a locked library is a real failure and propagates.
    try:
        darkroom._commit(job, resolved.image_id, strategy="auto", attempts=1, expect_change=False)
    except CommitTimeout as e:
        warnings.append(
            f"flush before saving did not confirm ({e.code}): the style reflects the "
            "last committed sidecar"
        )
    chosen = list(modules) if modules else session_modules(job, resolved.image_id)
    if not chosen:
        raise UsageError(
            "nothing was edited on this image in this session; pass --modules",
            detail={"image_id": resolved.image_id, "job": job},
        )
    extract = getattr(style.extract, "__wrapped__", style.extract)
    sr = extract(
        resolved.image_id,
        name,
        chosen,
        description=description or f"Scouter preset from {Path(resolved.path).name}",
        confirm=confirm,
        job=job,
    )
    if sr.outcome == "dry_run":
        return PresetResult(
            outcome="dry_run",
            name=name,
            action="save",
            modules=chosen,
            plan=sr.plan,
            reason=sr.reason
            or "a style of that name exists; " "re-run with confirm=true / --yes to replace it",
        )
    warnings.extend(sr.warnings)
    cfg = config.load()
    mem = memory_dir(cfg)
    memory_path: str | None = None
    replaced = False
    if mem is None:
        warnings.append(
            "saved in darktable and in the styles cache only; set library_root to "
            "keep presets in the backed-up library memory"
        )
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
        index["presets"].append(
            {
                "name": name,
                "created_at": _now_z(),
                "source_path": _source_path(cfg, resolved.path),
                "image_id": resolved.image_id,
                "modules": list(sr.operations_included or chosen),
                "dtstyle": dest.name,
                "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                "description": description,
                "scouter_version": __version__,
            }
        )
        _write_index(mem, index)
    included = list(sr.operations_included or chosen)
    return PresetResult(
        image_id=resolved.image_id,
        path=resolved.path,
        name=name,
        action="save",
        modules=included,
        dtstyle_path=sr.dtstyle_path,
        memory_path=memory_path,
        replaced=replaced or sr.created is False,
        warnings=warnings,
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
        rows.append(
            {
                "name": name,
                "scouter": name in ours,
                "in_darktable": name in in_dt,
                "modules": p.get("modules", []),
                "created_at": p.get("created_at"),
            }
        )
    missing = sorted(n for n in ours if n not in in_dt)
    warnings = (
        [
            f"{n} is in Scouter's index but not in darktable: re-import "
            f"{mem / (n + '.dtstyle')} in darktable's styles module, or save it again"
            for n in missing
        ]
        if mem
        else []
    )
    return PresetListResult(
        presets=rows,
        darktable_only=sorted(in_dt - set(ours)),
        missing_in_darktable=missing,
        warnings=warnings,
    )


def _history_end(path: str) -> int | None:
    parsed = darkroom._read_sidecar_parsed(path)
    return None if parsed is None else int(parsed.history_end)


def _require_style(name: str) -> None:
    existing = style._existing_styles()
    if name not in existing:
        suggestions = difflib.get_close_matches(name, existing, n=3, cutoff=0.6)
        raise UsageError(
            f"no darktable style named {name!r}",
            fix="`scouter preset list` shows every preset",
            detail={"name": name, "suggestions": suggestions},
        )


def _same_image(image: str | int, current: dtbridge.ResolvedImage) -> bool:
    """Fix round 1 (controller ruling): a caller can name the open image in
    any form that resolves to it -- a bare id, or a path in a different
    (relative, symlinked, `.`/`..`-laden) spelling than `darkroom.current`
    happens to report. An exact string match (the original implementation)
    would send a relative/alternate path down the LIBRARY path instead of the
    bounce, which is the in-memory-overwrite risk §8.3 exists to avoid. A
    numeric-looking `image` compares by id; anything else compares resolved
    (`Path.resolve()`) paths, falling back to a plain string match if
    resolution itself raises (e.g. a path component does not exist)."""
    s = str(image)
    if s.lstrip("+-").isdigit():
        try:
            return int(s) == int(current.image_id)
        except (TypeError, ValueError):
            return False
    try:
        return Path(s).expanduser().resolve() == Path(current.path).expanduser().resolve()
    except OSError:
        return s == current.path


def _current_image_or_none() -> dtbridge.ResolvedImage | None:
    """`darkroom._current_image()`, tolerant of nothing being loaded.

    Fix round 1 (blocker): the library path (§8.3) never touches the view
    and must work with no darkroom image loaded at all -- the caller named
    ANOTHER image explicitly. Only the open-image path actually needs
    `_current_image()`'s hard refusal, so that refusal is deferred until
    `apply` knows the target really is (or is meant to be) the open image.
    """
    try:
        return darkroom._current_image()
    except NotInDarkroom:
        return None


@metrics.instrumented("preset.apply")
def apply(
    name: str,
    image: str | int | None = None,
    *,
    educative: str | None = None,
    dry_run: bool = False,
    job: str | None = None,
) -> PresetResult:
    """Apply a preset to the open image (through a bounce, §8.3) or to another
    image (the library path, phase 2's `style.apply`)."""
    job = job or current_job()
    # Final review #3: no `paths.validate_style_name` here -- that is a
    # FILENAME rule (save writes `<name>.dtstyle`), and it rejected GUI
    # styles with spaces ("Portra 400") that `preset.list` shows and phase
    # 2's `style.apply` accepts. Apply never turns the name into a path;
    # `_require_style` below is the check that matters.
    if not isinstance(name, str) or not name.strip():
        raise UsageError("a preset name is required", detail={"name": name})
    level = explain.resolve_level(educative)
    if dry_run:
        return PresetResult(
            outcome="dry_run",
            name=name,
            action="apply",
            plan=[{"action": "preset.apply", "name": name, "image": image}],
            reason="dry run: no darktable calls were made",
        )
    _require_style(name)
    # Fix round 1: the library path (image given, and not the open one) must
    # never require a darkroom image at all -- look it up tolerantly, and
    # only demand one once we know the target really is the open image
    # (image is None, or it resolves to the one currently loaded).
    current = _current_image_or_none() if image is not None else darkroom._current_image()
    target_is_open = image is None or (current is not None and _same_image(image, current))
    if not target_is_open:
        applied = getattr(style.apply, "__wrapped__", style.apply)(image, name, job=job)
        return PresetResult(
            image_id=applied.image_id,
            path=applied.path,
            name=name,
            action="apply",
            applied_to="library",
            warnings=applied.warnings,
            explain=explain.preset_lines("apply", name, [], applied.applied, level=level, job=job),
        )
    assert current is not None  # target_is_open guarantees this
    darkroom._require_job_image(job, current.image_id)
    dtbridge.require_darkroom()
    before = _history_end(current.path)
    if before is None:
        # Fix round 1 (controller ruling): sending the style anyway and then
        # always reporting commit_timeout is a phantom outcome -- the real
        # problem is that Scouter cannot read the sidecar's history_end AT
        # ALL yet, not that a real apply's advance was never confirmed.
        # Refuse before any bridge call, with the honest fix.
        raise NoSidecar(
            f"cannot apply {name!r} to {current.path}: its history is not yet readable "
            "(no committed sidecar to verify the advance against)",
            detail={"path": current.path, "name": name},
        )
    entry = journal.begin(
        job,
        verb="preset.apply",
        image_id=current.image_id,
        path=current.path,
        module="",
        param="",
        instance=0,
        before={"history_end": before},
    )
    # Final review #2: only a failure of the bridge call ITSELF means the
    # style did not land -- `journal.fail` is for that alone. Once darktable
    # answers `applied: true`, the style IS in the history (and
    # `darkroom.apply_style`'s own bounce already made darktable write the
    # sidecar): failing the entry then would hide an applied style from
    # `latest_open`, so the next `undo_last` would silently restore a value
    # UNDER the style instead of giving the §8.4 refusal.
    # The Lua method raises on every path where the style did not land
    # (unknown style, wrong image, a failed apply in the lighttable, a
    # bounce that could not leave the darkroom), so a normal return IS its
    # `applied: true`.
    try:
        dtbridge.call("darkroom.apply_style", {"image_id": current.image_id, "name": name})
    except Exception as e:
        code = e.code if isinstance(e, ScouterError) else "error"
        journal.fail(job, entry.seq, outcome=f"error:{code}")
        raise
    warnings: list[str] = []
    after = _history_end(current.path)
    if after is None or after <= before:
        # The bounce's own sidecar write has not shown up yet: one more
        # commit (a second bounce), only in this case. Its timeout is a
        # warning, never a failure -- the style is applied either way.
        try:
            darkroom._commit(job, current.image_id, strategy="auto", expect_change=True)
        except ScouterError as e:
            warnings.append(f"commit after applying did not confirm ({e.code})")
        after = _history_end(current.path)
    if after is None or after <= before:
        warnings.append(
            f"style {name!r} was applied, but the sidecar's history_end did not advance "
            f"({before} -> {after}): the advance could not be verified; darktable holds the "
            "style, and undo still refuses it (history items cannot be removed)"
        )
        after = None
    journal.complete(job, entry.seq, after={"history_end": after, "style": name})
    return PresetResult(
        image_id=current.image_id,
        path=current.path,
        name=name,
        action="apply",
        applied_to="darkroom",
        history_end_before=before,
        history_end_after=after,
        warnings=warnings,
        explain=explain.preset_lines(
            "apply", name, [], None if after is None else after - before, level=level, job=job
        ),
    )


@metrics.instrumented("preset.apply_job")
def apply_job(
    name: str,
    *,
    job_slug: str,
    exclude: list[str] | None = None,
    force: bool = False,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> StyleSetResult:
    """A thin, always-gated delegate to `style.apply_set` (design §8.5)."""
    job = job or current_job()
    fn = getattr(style.apply_set, "__wrapped__", style.apply_set)
    return fn(
        name,
        job_slug=job_slug,
        exclude=exclude,
        force=force,
        confirm=confirm,
        dry_run=dry_run,
        job=job,
    )


PRESET_NAME_ARG = ArgSpec(
    "name", "str", required=True, positional=True, help="preset (darktable style) name"
)

for verb in (
    Verb(
        id="preset.save",
        group="preset",
        fn=save,
        args=(
            PRESET_NAME_ARG,
            ArgSpec(
                "modules",
                "list[str]",
                default=None,
                help="modules to keep (default: those changed in this session)",
            ),
            ArgSpec("description", "str", default="", help="style description"),
            EDUCATIVE_ARG,
            ArgSpec(
                "confirm",
                "bool",
                default=False,
                help="required only to replace an existing preset of this name",
            ),
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
    Verb(
        id="preset.apply",
        group="preset",
        fn=apply,
        args=(
            PRESET_NAME_ARG,
            ArgSpec(
                "image",
                "image",
                positional=True,
                help="path or image id; default: the image open in the darkroom",
            ),
            EDUCATIVE_ARG,
            ArgSpec("dry_run", "bool", default=False, help="plan only, send nothing"),
        ),
        writes=True,
        gated=False,
        summary="Apply a preset to the open image (live, via a bounce) or to another image.",
    ),
    Verb(
        id="preset.apply_job",
        group="preset",
        fn=apply_job,
        args=(
            PRESET_NAME_ARG,
            ArgSpec("job_slug", "str", required=True, help="job whose frames get the preset"),
            ArgSpec("exclude", "list[str]", default=None, help="paths or ids to leave out"),
            ArgSpec(
                "force",
                "bool",
                default=False,
                help="apply even where Scouter's ledger says it is already applied",
            ),
            ArgSpec("confirm", "bool", default=False, help="required: a set write is gated"),
            ArgSpec("dry_run", "bool", default=False, help="plan only"),
        ),
        writes=True,
        gated=True,
        summary="Apply a preset to every frame of a job (batched, gated).",
    ),
):
    register(verb)
