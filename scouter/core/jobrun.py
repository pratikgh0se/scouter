"""`job run`: a job's batch steps chained, honestly (product close, row 427
item 5 prep).

    cull      cull.propose (row 407's job-relative sharpness gate) -> cull.apply
    preset    preset.apply_job <preset>, excluding --exclude and the hero
    outliers  outliers.list against the hero
    export    export.release <export_preset>

Each step reports its counts. The chain stops at the first step that refuses
(a ScouterError) or does not come back `ok` (e.g. a partial apply-set), and
says where; it never papers over a step to reach the next. A refusal in the
very first step raises that step's own error, so the exit code is the
step's. `preset` is skipped (not failed) when no preset is given, `outliers`
when no hero is given -- unless the step was asked for by name.

Without confirm (or with dry_run) nothing is written: no renders, no ratings,
no styles, no exports. The plan shows each step and, where the step has a
cheap dry run of its own (apply-set, export), what it would touch as of now
-- before the cull and preset have run, so the export selection shown is
today's ratings, not the post-cull ones.

Every step calls the core function's unwrapped body, so one `job run` is one
metrics event, like `preset.apply_job`.
"""

from __future__ import annotations

import time

from scouter import metrics
from scouter.core import cull, export, presets, style
from scouter.errors import ScouterError, UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import JobRunResult

STEPS = ("cull", "preset", "outliers", "export")

#: `style.apply_set` returns before darktable has written every sidecar
#: (row 408 B7 measured 0.9-1.35 s of lag), and `outliers.list` reads those
#: sidecars' renders. Waiting this long after a preset step keeps the next
#: step from measuring the pre-style frames.
_SETTLE_AFTER_STYLE_S = 2.0


def _unwrapped(fn):
    return getattr(fn, "__wrapped__", fn)


def _row(step: str, verb: str, result, counts: dict) -> dict:
    return {
        "step": step,
        "verb": verb,
        "outcome": result.outcome,
        "counts": counts,
        "warnings": list(result.warnings or []),
    }


def _cull_counts(r) -> dict:
    return {
        **dict(r.counts or {}),
        "sharpness_threshold": (r.stats or {}).get("sharpness_threshold"),
        "stats_failed": (r.stats or {}).get("failed"),
    }


def _apply_counts(r) -> dict:
    return {
        "applied": r.applied,
        "unchanged": r.unchanged,
        "would_change": r.would_change,
        "verified": r.verified,
        "unverified": r.unverified,
        "stale": len(r.stale),
        "missed": len(r.missed),
    }


def _set_counts(r) -> dict:
    return {
        "requested": r.requested,
        "applied": r.applied,
        "failed": r.failed,
        "missed": len(r.missed),
    }


def _outlier_counts(r) -> dict:
    return {
        "checked": r.checked,
        "within_tolerance": r.within_tolerance,
        "outliers": len(r.outliers),
        "failed": len(r.failed),
        "wb_test": r.wb_test,
        "outlier_paths": [o.get("path") for o in r.outliers],
    }


def _release_counts(r) -> dict:
    files = r.files or []
    failed = sum(1 for f in files if not f.ok)
    return {
        "selected": r.selected,
        "of": r.of,
        "exported": len(files) - failed,
        "failed": failed,
        "manifest_path": r.manifest_path or None,
    }


def _resolve_steps(steps: list[str] | None, preset: str | None, hero) -> list[str]:
    if steps:
        unknown = [s for s in steps if s not in STEPS]
        if unknown:
            raise UsageError(
                f"unknown step(s) {unknown}; known: {', '.join(STEPS)}", detail={"steps": steps}
            )
        if "preset" in steps and not preset:
            raise UsageError("the preset step needs --preset <name>", detail={"steps": steps})
        if "outliers" in steps and hero is None:
            raise UsageError("the outliers step needs --hero <image>", detail={"steps": steps})
        return [s for s in STEPS if s in steps]
    return list(STEPS)


def _dry_run(
    job_slug: str, chosen: list[str], *, preset, hero, exclude, export_preset, min_rating
) -> JobRunResult:
    plan: list[dict] = []
    for step in chosen:
        if step == "cull":
            plan.append(
                {
                    "step": "cull",
                    "verbs": ["cull.propose", "cull.apply"],
                    "would": "render every frame, propose ratings with the job-relative "
                    "sharpness gate, then apply them (verified from the XMP)",
                }
            )
        elif step == "preset":
            if not preset:
                plan.append({"step": "preset", "outcome": "skipped", "reason": "no --preset"})
                continue
            row = {
                "step": "preset",
                "verbs": ["preset.apply_job"],
                "would": f"append preset {preset!r} to every frame except {exclude}",
            }
            try:
                r = _unwrapped(presets.apply_job)(
                    preset, job_slug=job_slug, exclude=exclude, dry_run=True
                )
                row["counts"] = _set_counts(r)
            except ScouterError as e:
                row["refused"] = e.to_dict()
            plan.append(row)
        elif step == "outliers":
            if hero is None:
                plan.append({"step": "outliers", "outcome": "skipped", "reason": "no --hero"})
                continue
            plan.append(
                {
                    "step": "outliers",
                    "verbs": ["outliers.list"],
                    "would": f"list frames outside tolerance against {hero} (read-only)",
                }
            )
        elif step == "export":
            row = {
                "step": "export",
                "verbs": ["export.release"],
                "would": f"export the selection through {export_preset!r} with a manifest",
            }
            try:
                r = _unwrapped(export.release)(
                    job_slug, export_preset, min_rating=min_rating, dry_run=True
                )
                row["counts"] = {
                    "selected": r.selected,
                    "of": r.of,
                    "as_of": "current ratings, before this run's cull",
                }
            except ScouterError as e:
                row["refused"] = e.to_dict()
            plan.append(row)
    return JobRunResult(
        outcome="dry_run",
        job_slug=job_slug,
        plan=plan,
        reason=f"dry run: would run {', '.join(chosen)} on job {job_slug!r}; nothing was "
        "written. Re-run with --yes / confirm=true",
    )


@metrics.instrumented("job.run")
def run(
    *,
    job_slug: str,
    preset: str | None = None,
    hero: str | int | None = None,
    exclude: list[str] | None = None,
    export_preset: str = "client-proof",
    min_rating: int | None = None,
    steps: list[str] | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    job: str | None = None,
) -> JobRunResult:
    """Run a job's batch chain; stop honestly at the first step that refuses."""
    job = job or current_job()
    chosen = _resolve_steps(steps, preset, hero)
    exclude_all = list(exclude or [])
    if hero is not None and str(hero) not in [str(x) for x in exclude_all]:
        exclude_all.append(str(hero))
    if dry_run or not confirm:
        return _dry_run(
            job_slug,
            chosen,
            preset=preset,
            hero=hero,
            exclude=exclude_all,
            export_preset=export_preset,
            min_rating=min_rating,
        )

    rows: list[dict] = []
    warnings: list[str] = []
    completed = 0
    stopped_at: str | None = None
    styled = False

    def attempt(step: str, verb: str, fn, counts_of) -> object | None:
        nonlocal completed, stopped_at
        try:
            result = fn()
        except ScouterError as e:
            if completed == 0:
                raise
            rows.append(
                {
                    "step": step,
                    "verb": verb,
                    "outcome": f"error:{e.code}",
                    "counts": {},
                    "warnings": [],
                    "error": e.to_dict(),
                }
            )
            stopped_at = step
            return None
        rows.append(_row(step, verb, result, counts_of(result)))
        warnings.extend(f"{verb}: {w}" for w in (result.warnings or []))
        if result.outcome != "ok":
            stopped_at = step
            return None
        completed += 1
        return result

    for step in chosen:
        if step == "cull":
            proposal = attempt(
                "cull.propose",
                "cull.propose",
                lambda: _unwrapped(cull.propose)(job_slug=job_slug, job=job),
                _cull_counts,
            )
            if proposal is None:
                break
            applied = attempt(
                "cull.apply",
                "cull.apply",
                lambda: _unwrapped(cull.apply)(
                    proposal=proposal.proposal_path, confirm=True, job=job
                ),
                _apply_counts,
            )
            if applied is None:
                stopped_at = "cull"
                break
        elif step == "preset":
            if not preset:
                rows.append(
                    {
                        "step": "preset",
                        "verb": "preset.apply_job",
                        "outcome": "skipped",
                        "counts": {},
                        "warnings": [],
                        "reason": "no --preset given",
                    }
                )
                continue
            if (
                attempt(
                    "preset",
                    "preset.apply_job",
                    lambda: _unwrapped(presets.apply_job)(
                        preset, job_slug=job_slug, exclude=exclude_all, confirm=True, job=job
                    ),
                    _set_counts,
                )
                is None
            ):
                break
            styled = True
        elif step == "outliers":
            if hero is None:
                rows.append(
                    {
                        "step": "outliers",
                        "verb": "outliers.list",
                        "outcome": "skipped",
                        "counts": {},
                        "warnings": [],
                        "reason": "no --hero given",
                    }
                )
                continue
            if styled and _SETTLE_AFTER_STYLE_S > 0:
                time.sleep(_SETTLE_AFTER_STYLE_S)
                warnings.append(
                    f"waited {_SETTLE_AFTER_STYLE_S:g} s after the preset for "
                    "darktable to write the sidecars (row 408 B7)"
                )
            if (
                attempt(
                    "outliers",
                    "outliers.list",
                    lambda: _unwrapped(style.outliers)(hero, job_slug=job_slug, job=job),
                    _outlier_counts,
                )
                is None
            ):
                break
        elif step == "export":
            if (
                attempt(
                    "export",
                    "export.release",
                    lambda: _unwrapped(export.release)(
                        job_slug, export_preset, min_rating=min_rating, confirm=True, job=job
                    ),
                    _release_counts,
                )
                is None
            ):
                break

    reason = None
    if stopped_at:
        last = rows[-1]
        why = (last.get("error") or {}).get("message") or f"outcome {last['outcome']}"
        reason = (
            f"stopped at {stopped_at}: {why}; the steps before it completed and are "
            "not rolled back"
        )
    return JobRunResult(
        outcome="partial" if stopped_at else "ok",
        job_slug=job_slug,
        steps=rows,
        completed=completed,
        stopped_at=stopped_at,
        warnings=warnings,
        reason=reason,
    )


register(
    Verb(
        id="job.run",
        group="job",
        fn=run,
        args=(
            ArgSpec("job_slug", "str", required=True, help="the job to run the chain on"),
            ArgSpec(
                "preset",
                "str",
                default=None,
                help="hero preset (darktable style) to apply to the set; omit to skip",
            ),
            ArgSpec(
                "hero",
                "image",
                default=None,
                help="the hero frame: outliers reference, and left out of the preset step",
            ),
            ArgSpec(
                "exclude",
                "list[str]",
                default=None,
                help="paths or ids the preset step leaves out (comma-separated)",
            ),
            ArgSpec(
                "export_preset",
                "str",
                default="client-proof",
                choices=tuple(export.PRESETS),
                help="export release preset",
            ),
            ArgSpec(
                "min_rating",
                "int",
                default=None,
                help="export selection floor for proof presets (default: config)",
            ),
            ArgSpec(
                "steps",
                "list[str]",
                default=None,
                help="subset of cull,preset,outliers,export (default: all, in that order)",
            ),
            ArgSpec("confirm", "bool", default=False, help="required: the chain writes"),
            ArgSpec("dry_run", "bool", default=False, help="plan only, write nothing"),
        ),
        writes=True,
        gated=True,
        summary="Run a job's batch chain: cull, preset, outliers, export; stops at a refusal.",
    )
)
