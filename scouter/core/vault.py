"""The vault job note: one AI-first Markdown note per job, append-never-overwrite.

Not a verb -- called by `library.import_card` (task 3) and, in later tasks,
by `cull.apply` / `style.apply_set` / `outliers.list` / `export.release`
through `append_stage`. `$OBSIDIAN_VAULT_PATH` is the only door in: absent or
missing, every write here becomes a `{"written": False, ...}` no-op rather
than a failure (user-level rule -- a job import must never fail because a
note could not be written).

The frontmatter block is written and re-parsed with a line-oriented reader,
not a YAML library (stdlib only): the block this module writes is flat (no
nesting), so a small serializer/parser pair is enough, and it never has to
understand arbitrary YAML someone else wrote.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scouter import __version__
from scouter.config import Config

#: The template's own vault knowledge-base links (design section 7). Static:
#: every job note points at the same project note and the same two reference
#: notes, which is what "project" and "related" mean for a photo job.
_RELATED = [
    "[[Specs/darkroom-agent]]",
    "[[Knowledge/Scouter - Phase 1 build and live findings 2026-09-14]]",
]

#: `## Stages` checklist items, in the order design section 7 lists them.
#: `cull.apply`, `style.apply_set`, `outliers.list` and `export.release` tick
#: these one at a time via `append_stage`'s `checklist_key`.
STAGE_CHECKLIST: tuple[str, ...] = (
    "cull proposed",
    "hero style extracted",
    "style applied to the set",
    "outliers reviewed",
    "proofs released",
    "finals released",
)

#: The degradation reason (user-level rule): unset or missing, never a failure.
_VAULT_ABSENT_REASON = "vault not present ($OBSIDIAN_VAULT_PATH unset or missing)"

#: Frontmatter keys that are summed on a second import rather than left alone.
_SUMMED_KEYS = ("frames", "bytes")
#: Frontmatter keys that are unioned (list-valued) rather than left alone.
_UNIONED_KEYS = ("tags", "related")


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


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _library_path(manifest: dict) -> str:
    """`<YYYY>/<YYYY-MM-DD job-slug>` -- RELATIVE, so the note survives a
    `library.relocate` (the only absolute path Scouter holds is the config's
    `library_root`, never repeated into the vault)."""
    date = manifest["date"]
    return f"{date[:4]}/{date} {manifest['job_slug']}"


# ---------------------------------------------------------------------------
# Frontmatter: flat-block serialize / parse
# ---------------------------------------------------------------------------


def _fm_scalar(value: str) -> str:
    """A bare YAML scalar when safe, else a JSON (== YAML flow) quoted string.

    Quoting is deliberately conservative: anything that could read as a YAML
    structural token (a leading `[[`, a `: ` inside the value, a `#` comment
    marker, leading/trailing whitespace, or emptiness) gets quoted; everything
    else -- including a value that merely CONTAINS a colon, like an ISO
    timestamp -- stays bare, matching the design's own template.
    """
    s = str(value)
    needs_quote = (
        s == "" or ": " in s or "#" in s or (s and s[0] in "[{*&!|>'\"%@`") or s != s.strip()
    )
    return json.dumps(s) if needs_quote else s


def _fm_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fm_scalar(str(v)) for v in value) + "]"
    return _fm_scalar(str(value))


def _render_frontmatter_block(fm: dict) -> str:
    lines = ["---"]
    for key, value in fm.items():
        if value is None:
            continue
        lines.append(f"{key}: {_fm_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _parse_fm_scalar(raw: str) -> Any:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null" or raw == "":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    try:
        return int(raw)
    except ValueError:
        return raw


def _parse_fm_list(raw: str) -> list:
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    buf = ""
    in_quotes = False
    for ch in inner:
        if ch == '"':
            in_quotes = not in_quotes
            buf += ch
        elif ch == "," and not in_quotes:
            items.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf.strip())
    return [_parse_fm_scalar(item) for item in items]


def _parse_frontmatter_block(lines: list[str]) -> dict:
    out: dict = {}
    for line in lines:
        if not line.strip() or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        value = rest.strip()
        if value.startswith("[") and value.endswith("]"):
            out[key] = _parse_fm_list(value)
        else:
            out[key] = _parse_fm_scalar(value)
    return out


def _split_frontmatter(text: str) -> tuple[list[str], str] | None:
    """`(frontmatter_lines, rest_of_the_file)`, or None when `text` doesn't
    start with a `---` block. `rest` is everything after the closing `---`
    line, byte for byte -- never touched by a merge."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            fm_lines = [line.rstrip("\n") for line in lines[1:i]]
            rest = "".join(lines[i + 1 :])
            return fm_lines, rest
    return None


def _frontmatter_for(cfg: Config, manifest: dict) -> dict:
    totals = manifest.get("totals", {})
    frames = totals.get("planned", len(manifest.get("files", [])))
    return {
        "type": "photo-job",
        "date": manifest["date"],
        "tags": ["photo-job", "scouter", "darktable", "capsule-corp"],
        "ai-first": True,
        "project": "[[Capsule Corp]]",
        "related": list(_RELATED),
        "job_slug": manifest["job_slug"],
        "camera": manifest["camera"],
        "camera_model": manifest.get("camera_model"),
        "frames": int(frames),
        "bytes": int(totals.get("bytes", 0)),
        "library_path": _library_path(manifest),
        "imported_at": manifest.get("imported_at") or _now_z(),
        "scouter_version": manifest.get("scouter_version", __version__),
        "status": "imported",
    }


def _merge_frontmatter(text: str, new_fm: dict) -> tuple[str, dict]:
    """Additively merge `new_fm` into the frontmatter block at the top of
    `text`. `frames`/`bytes` are summed, `tags`/`related` are unioned, every
    other key already present is left untouched -- its would-be new value is
    returned in `notes` so the caller can report it in the appended section
    instead of silently dropping it. Never rewrites anything after the block.
    """
    split = _split_frontmatter(text)
    if split is None:
        # No parseable frontmatter to merge into (a human replaced it, or the
        # note predates this format) -- prepend a fresh block rather than
        # guess at merging into something we can't parse.
        return _render_frontmatter_block(new_fm) + "\n" + text, {}
    fm_lines, rest = split
    old = _parse_frontmatter_block(fm_lines)
    merged = dict(old)
    notes: dict = {}
    for key, value in new_fm.items():
        if key in _SUMMED_KEYS:
            merged[key] = int(old.get(key, 0) or 0) + int(value)
        elif key in _UNIONED_KEYS:
            existing = list(old.get(key) or [])
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged[key] = existing
        elif key in old:
            if old[key] != value:
                notes[key] = value
            # else: identical -- nothing to report, nothing to change.
        else:
            merged[key] = value
    return _render_frontmatter_block(merged) + "\n" + rest, notes


# ---------------------------------------------------------------------------
# Body sections
# ---------------------------------------------------------------------------


def _render_preamble(manifest: dict) -> str:
    totals = manifest.get("totals", {})
    frames = totals.get("planned", len(manifest.get("files", [])))
    mib = totals.get("bytes", 0) / (1024 * 1024)
    camera_model = manifest.get("camera_model") or manifest["camera"]
    imported_at = manifest.get("imported_at") or _now_z()
    return (
        "## For future agent\n"
        f"Photo job `{manifest['job_slug']}`, shot {manifest['date']} on the {camera_model} "
        f"({frames} frames, {mib:.0f} MiB), imported by Scouter "
        f"{manifest.get('scouter_version', __version__)} on {imported_at[:10]}. The raws are "
        f"read-only at `<library_root>/{_library_path(manifest)}/raw` with sha256 in "
        "`.scouter/checksums.sha256`; `library_root` lives in the Scouter config "
        "(`~/.config/scouter/config.json` by default) -- read it from there rather than "
        "assuming a path, since `library.relocate` can change it. Edits are darktable "
        "history in the `.xmp` beside each raw. Stages below are appended as they happen -- "
        "a stage with no section has not run. Verify anything numeric with "
        f"`scouter library verify {manifest['job_slug']} --json`.\n"
    )


def _render_import_section(manifest: dict, notes: dict | None = None) -> str:
    ts = manifest.get("imported_at") or _now_z()
    totals = manifest.get("totals", {})
    rows = [
        ("source", f"`{manifest.get('source_root', '')}`"),
        ("copied / skipped", f"{totals.get('copied', 0)} / {totals.get('skipped_identical', 0)}"),
        ("bytes", str(totals.get("bytes", 0))),
        ("checksums", "`.scouter/checksums.sha256`"),
    ]
    lines = [f"## Import {ts}", "| | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    if notes:
        lines.append("")
        lines.append(
            "Frontmatter fields already present were left alone; this import saw a "
            "different value for:"
        )
        for key, value in notes.items():
            lines.append(f"- `{key}`: {value!r}")
    return "\n".join(lines) + "\n"


def _render_stages_checklist() -> str:
    lines = ["## Stages"] + [f"- [ ] {item}" for item in STAGE_CHECKLIST]
    return "\n".join(lines) + "\n"


def _render_new_note(cfg: Config, manifest: dict) -> str:
    header = _render_frontmatter_block(_frontmatter_for(cfg, manifest))
    return (
        header
        + "\n"
        + _render_preamble(manifest)
        + "\n"
        + _render_import_section(manifest)
        + "\n"
        + _render_stages_checklist()
    )


def _render_rows_table(rows: list[tuple[str, str]]) -> str:
    lines = ["| | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_job_note(cfg: Config, manifest: dict) -> dict:
    root = vault_root()
    if root is None:
        return {"written": False, "reason": _VAULT_ABSENT_REASON}
    directory = root / cfg.vault.jobs_dir
    directory.mkdir(parents=True, exist_ok=True)
    note = directory / f"{manifest['date']} {manifest['job_slug']}.md"
    if not note.exists():
        note.write_text(_render_new_note(cfg, manifest), encoding="utf-8")
        return {"written": True, "path": str(note), "mode": "created"}
    # Never rewrite a body a human may have edited: append one section and
    # additively merge the frontmatter (sum frames/bytes, union lists, leave
    # scalars that already exist alone and note the new value in the section).
    text = note.read_text(encoding="utf-8")
    merged, notes = _merge_frontmatter(text, _frontmatter_for(cfg, manifest))
    merged += "\n" + _render_import_section(manifest, notes)
    note.write_text(merged, encoding="utf-8")
    return {"written": True, "path": str(note), "mode": "appended"}


def append_stage(
    cfg: Config,
    job_slug: str,
    stage: str,
    rows: list[tuple[str, str]],
    checklist_key: str | None = None,
) -> dict:
    """Append `## <stage> <ISO ts>` plus a two-column table of `rows` to the
    job's note, and tick `- [ ] <checklist_key>` if it is there. One writer
    for `cull.apply`, `style.apply_set`, `outliers.list` and `export.release`,
    so the format cannot drift between verbs."""
    root = vault_root()
    if root is None:
        return {"written": False, "reason": _VAULT_ABSENT_REASON}
    directory = root / cfg.vault.jobs_dir
    matches = sorted(directory.glob(f"*{job_slug}.md")) if directory.is_dir() else []
    if not matches:
        return {"written": False, "reason": "no job note"}
    note = matches[0]
    text = note.read_text(encoding="utf-8")

    ticked = False
    note_msg: str | None = None
    if checklist_key:
        unticked = f"- [ ] {checklist_key}"
        if unticked in text:
            text = text.replace(unticked, f"- [x] {checklist_key}", 1)
            ticked = True
        else:
            note_msg = (
                f"checklist item {checklist_key!r} not found in the note; "
                "appended without ticking"
            )

    section = f"\n## {stage} {_now_z()}\n" + _render_rows_table(rows) + "\n"
    text += section
    note.write_text(text, encoding="utf-8")

    result: dict = {"written": True, "path": str(note), "ticked": ticked}
    if note_msg:
        result["note"] = note_msg
    return result
