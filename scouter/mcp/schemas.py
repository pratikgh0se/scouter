"""Handwritten MCP tool schemas.

Handwritten on purpose: a schema derived from the Python signature injects
pydantic `title` keys and cannot express `minimum`/`maximum` or the
"path or id" union, so the agent-facing contract would drift with every
refactor. tests/test_scouter_mcp_schemas.py pins this table.
"""

from __future__ import annotations

IMAGE = {
    "type": ["string", "integer"],
    "description": "absolute path to the raw file, or a darktable image id",
}
PX = {
    "type": "integer",
    "minimum": 64,
    "maximum": 4096,
    "default": 1024,
    "description": "longest edge of the render in pixels",
}
DRY_RUN = {
    "type": "boolean",
    "default": False,
    "description": "plan only: return what would happen and send nothing",
}
INSTANCE = {
    "type": "integer",
    "minimum": 0,
    "default": 0,
    "description": "module instance (darktable's multi_priority)",
}
BUDGET_S = {
    "type": "number",
    "default": 3.0,
    "description": "loop budget; over it, re-render at 512 px",
}


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOL_SPECS: list[dict] = [
    {
        "name": "darkroom_open",
        "description": "Load an image in darktable's darkroom view, importing it into "
        "the library first if it is not there. Every other darkroom "
        "tool needs this first: slider actions only work in the darkroom.",
        "inputSchema": _schema({"image": IMAGE}, ["image"]),
    },
    {
        "name": "darkroom_set",
        "description": "Set one module parameter to an absolute value on the image "
        "currently open in the darkroom. Creates one darktable history "
        "item the user can see and undo. Values are read back from the "
        "XMP sidecar after the commit.",
        "inputSchema": _schema(
            {
                "module": {
                    "type": "string",
                    "description": "darktable module, e.g. exposure, sigmoid, "
                    "temperature, colorbalancergb",
                },
                "param": {
                    "type": "string",
                    "description": "parameter, e.g. exposure, contrast, temperature",
                },
                "value": {
                    "type": "number",
                    "description": "absolute value in the slider's own unit "
                    "(EV for exposure, K for temperature)",
                },
                "instance": INSTANCE,
                "commit": {
                    "type": "boolean",
                    "default": True,
                    "description": "commit history to the XMP after the write",
                },
                "dry_run": DRY_RUN,
            },
            ["module", "param", "value"],
        ),
    },
    {
        "name": "darkroom_nudge",
        "description": "Move a slider by darktable's own default step, up (positive "
        "steps) or down (negative). Use when the user says 'a bit more'.",
        "inputSchema": _schema(
            {
                "module": {"type": "string", "description": "darktable module"},
                "param": {"type": "string", "description": "parameter"},
                "steps": {
                    "type": "integer",
                    "default": 1,
                    "description": "steps up (+) or down (-)",
                },
                "instance": INSTANCE,
                "commit": {
                    "type": "boolean",
                    "default": True,
                    "description": "commit history to the XMP after the write",
                },
                "dry_run": DRY_RUN,
            },
            ["module", "param"],
        ),
    },
    {
        "name": "darkroom_enable",
        "description": "Enable a module on the darkroom image.",
        "inputSchema": _schema(
            {
                "module": {"type": "string", "description": "darktable module"},
                "instance": INSTANCE,
                "dry_run": DRY_RUN,
            },
            ["module"],
        ),
    },
    {
        "name": "darkroom_reset",
        "description": "Reset a module to its defaults. DESTRUCTIVE: the module's "
        "parameters are discarded and Scouter cannot undo it (only the "
        "user's own Ctrl-Z can). Requires confirm=true; without it you "
        "get the plan and nothing happens.",
        "inputSchema": _schema(
            {
                "module": {"type": "string", "description": "darktable module"},
                "instance": INSTANCE,
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required: authorises discarding this module's " "parameters",
                },
            },
            ["module"],
        ),
    },
    {
        "name": "darkroom_undo_last",
        "description": "Revert the newest Scouter write in this job by setting the "
        "parameter back to its previous value. This adds a new history "
        "item rather than deleting one, so the user's own undo still works.",
        "inputSchema": _schema({"dry_run": DRY_RUN}, []),
    },
    {
        "name": "darkroom_status",
        "description": "Current view plus normalised slider POSITIONS (0..1). These are "
        "positions, not parameter values: use history_read for values.",
        "inputSchema": _schema(
            {
                "module": {"type": "string", "description": "module to query"},
                "param": {"type": "string", "description": "parameter to query"},
                "instance": INSTANCE,
            },
            [],
        ),
    },
    {
        "name": "darkroom_describe_history",
        "description": "The darkroom image's committed history: every module, whether "
        "it is active, and its decoded parameters where a verified "
        "layout exists (otherwise opaque hex plus the module version).",
        "inputSchema": _schema({"image": IMAGE}, []),
    },
    {
        "name": "history_read",
        "description": "Read an image's history from its XMP sidecar. Returns "
        "confirmed=false when the sidecar is missing or mid-write.",
        "inputSchema": _schema({"image": IMAGE}, ["image"]),
    },
    {
        "name": "history_commit",
        "description": "Make darktable write the darkroom image's history to its XMP "
        "sidecar, then verify the file actually changed. darktable only "
        "commits when the darkroom is left, so this is what makes an "
        "edit readable.",
        "inputSchema": _schema(
            {
                "image": IMAGE,
                "timeout_s": {
                    "type": "number",
                    "default": 5.0,
                    "minimum": 0.1,
                    "description": "seconds to wait for the sidecar to change",
                },
                "strategy": {
                    "type": "string",
                    "enum": ["auto", "sidecar_action", "view_bounce"],
                    "default": "auto",
                    "description": "commit trigger; auto picks the cheapest "
                    "that works on this build",
                },
            },
            [],
        ),
    },
    {
        "name": "scout_stats",
        "description": "Render the image at N px through darktable-cli and return the "
        "per-channel histogram, clipping percentages, luminance "
        "percentiles, white-balance ratios and a sharpness score. "
        "Statistics are 8-bit and the luminance histogram is an estimate.",
        "inputSchema": _schema(
            {"image": IMAGE, "px": PX, "budget_s": BUDGET_S},
            ["image"],
        ),
    },
    {
        "name": "scout_preview",
        "description": "Render a viewable preview and return its FILE PATH. The pixels "
        "are never inlined; read the file at preview_path yourself.",
        "inputSchema": _schema(
            {
                "image": IMAGE,
                "px": PX,
                "fmt": {
                    "type": "string",
                    "enum": ["png", "ppm"],
                    "default": "png",
                    "description": "preview file format",
                },
            },
            ["image"],
        ),
    },
    {
        "name": "scout_compare",
        "description": "Numeric distance between two images' current renders "
        "(deltas are b minus a).",
        "inputSchema": _schema(
            {"a": IMAGE, "b": IMAGE, "px": PX, "budget_s": BUDGET_S},
            ["a", "b"],
        ),
    },
    {
        "name": "style_extract",
        "description": "Save the darkroom image's current history as a darktable style. "
        "NOTE: darktable's Lua API cannot filter a style by module, so "
        "`modules` is recorded but the style carries the whole history. "
        "Requires confirm=true when a style of that name already exists.",
        "inputSchema": _schema(
            {
                "image": IMAGE,
                "name": {"type": "string", "description": "style name"},
                "modules": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "modules the caller wants (recorded only)",
                },
                "description": {
                    "type": "string",
                    "default": "",
                    "description": "style description",
                },
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required to replace an existing style",
                },
            },
            ["image", "name", "modules"],
        ),
    },
    {
        "name": "style_apply",
        "description": "Apply a style to ONE image (applying to a set is phase 2). "
        "mode=append adds to the history; mode=overwrite is refused with "
        "unsupported_mode, because darktable's Lua API cannot replace history.",
        "inputSchema": _schema(
            {
                "image": IMAGE,
                "name": {"type": "string", "description": "style name"},
                "mode": {
                    "type": "string",
                    "enum": ["append", "overwrite"],
                    "default": "append",
                    "description": "append adds to history; overwrite is refused "
                    "(unsupported_mode), never silently downgraded to append",
                },
                "dry_run": DRY_RUN,
            },
            ["image", "name"],
        ),
    },
    {
        "name": "export_run",
        "description": "Export images through a preset. client-final is a full-size "
        "sRGB JPEG; preview is a 1024 px PNG. Requires confirm=true for "
        "more than one image or to overwrite an existing file.",
        "inputSchema": _schema(
            {
                "images": {
                    "type": "array",
                    "items": IMAGE,
                    "minItems": 1,
                    "description": "paths or image ids",
                },
                "preset": {
                    "type": "string",
                    "enum": ["preview", "client-proof", "client-final", "print-tiff"],
                    "description": "export preset",
                },
                "out_dir": {"type": "string", "description": "destination directory"},
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required for n>1 images or to overwrite files",
                },
            },
            ["images", "preset", "out_dir"],
        ),
    },
    {
        "name": "export_release",
        "description": "Export a job's selected frames through a preset, with a checksum "
        "manifest. Selection is --images if given, else every raw in "
        "import.json whose rating >= min_rating (client-proof/preview) "
        "or that carries the configured colour label "
        "(client-final/print-tiff). Requires confirm=true to overwrite "
        "an existing preset output.",
        "inputSchema": _schema(
            {
                "job_slug": {"type": "string", "description": "job slug to release"},
                "preset": {
                    "type": "string",
                    "enum": ["preview", "client-proof", "client-final", "print-tiff"],
                    "description": "export preset",
                },
                "date": {
                    "type": "string",
                    "description": "disambiguate when the slug matches more than one job",
                },
                "min_rating": {
                    "type": "integer",
                    "description": "override export.min_rating for rating-based presets",
                },
                "images": {
                    "type": "array",
                    "items": IMAGE,
                    "description": "explicit image list, overriding the selection rule",
                },
                "style": {
                    "type": "string",
                    "description": "apply this style at render time only; nothing in the "
                    "library changes",
                },
                "overwrite_style": {
                    "type": "boolean",
                    "default": False,
                    "description": "pass --style-overwrite to darktable-cli (render-only)",
                },
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required to overwrite an existing preset output",
                },
            },
            ["job_slug", "preset"],
        ),
    },
    {
        "name": "export_verify",
        "description": "Re-hash a release manifest's outputs against it. The manifest's own "
        ".sha256 sidecar is required, not optional -- a missing sidecar is "
        "manifest_unauthenticated. Read-only.",
        "inputSchema": _schema(
            {
                "manifest": {
                    "type": "string",
                    "description": "path to a manifest-<preset>-<ts>.json written by "
                    "export.release",
                },
                "root": {
                    "type": "string",
                    "description": "directory files[].output resolves against; default is "
                    "the manifest's own <job>/.scouter/ parent. Pass this to verify a "
                    "manifest shipped beside its exports/ folder instead",
                },
            },
            ["manifest"],
        ),
    },
    {
        "name": "library_import",
        "description": "Copy, checksum, rename and register a memory card into the library. "
        "Refuses with backup_not_configured before reading a single file "
        "unless the backup gate passes (config.show reports its verdict). "
        "Always requires confirm=true: an import is always more than one "
        "file. Originals are copied read-only (chmod 0444) and verified "
        "with two independent sha256 passes; a darktable that is not "
        "running does not fail the import (film_roll_registered: false, "
        "retry with library.register).",
        "inputSchema": _schema(
            {
                "source": {"type": "string", "description": "the card's mount point"},
                "job_slug": {
                    "type": "string",
                    "description": "job directory slug, e.g. harbour-wedding",
                },
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD; default: the earliest capture time",
                },
                "camera": {
                    "type": "string",
                    "description": "camera slug override; required for a mixed-camera card",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "override import.extensions, e.g. ORF,ARW",
                },
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required: an import always touches more than one file",
                },
            },
            ["source", "job_slug"],
        ),
    },
    {
        "name": "library_verify",
        "description": "Re-hash raw/ against the job's .scouter/checksums.sha256 manifest. "
        "Read-only. `.xmp` sidecars are excluded by suffix, not by silence.",
        "inputSchema": _schema(
            {
                "job_slug": {"type": "string", "description": "job slug to verify"},
                "date": {
                    "type": "string",
                    "description": "disambiguate when the slug matches more than one job",
                },
            },
            ["job_slug"],
        ),
    },
    {
        "name": "library_register",
        "description": "Re-register a job's film roll with darktable (e.g. after importing "
        "with darktable closed). Requires confirm=true: it writes to "
        "darktable's own library.",
        "inputSchema": _schema(
            {
                "job_slug": {"type": "string", "description": "job slug to register"},
                "date": {
                    "type": "string",
                    "description": "disambiguate when the slug matches more than one job",
                },
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required: writes to darktable's library",
                },
            },
            ["job_slug"],
        ),
    },
    {
        "name": "cull_propose",
        "description": "Propose a rating for every frame in a job (or an explicit image "
        "list), grouped into bursts by capture time and camera. Never "
        "proposes rating 0 or reject -- stats cannot see eyes, "
        "expression or a deliberate motion blur, so a flagged frame "
        "gets rating 1 with its numbers and the human decides. Every "
        "reason names the numbers behind it. Read-only: writes no "
        "rating and calls the bridge only for the read-only "
        "image.lookup behind image resolution.",
        "inputSchema": _schema(
            {
                "job_slug": {
                    "type": "string",
                    "description": "job slug; frames come from its import.json",
                },
                "images": {
                    "type": "array",
                    "items": IMAGE,
                    "description": "explicit image list, instead of (or alongside) " "--job-slug",
                },
                "px": {
                    "type": "integer",
                    "description": "stats render size (default: stats.set_px)",
                },
                "workers": {
                    "type": "integer",
                    "description": "thread pool size (default: stats.workers)",
                },
            },
            [],
        ),
    },
    {
        "name": "cull_apply",
        "description": "Apply a cull.propose proposal's ratings via the image.rate bridge "
        "verb, in batches of 50, verified by polling each changed "
        "frame's XMP sidecar. Refuses (usage) if any frame's sidecar "
        "changed since the proposal was written. Requires confirm=true: "
        "a cull touches many images.",
        "inputSchema": _schema(
            {
                "job_slug": {
                    "type": "string",
                    "description": "job slug; loads the newest cull-*.json unless "
                    "proposal is given",
                },
                "proposal": {
                    "type": "string",
                    "description": "path to a specific cull-<ts>.json",
                },
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required: writes ratings via image.rate",
                },
            },
            [],
        ),
    },
    {
        "name": "config_init",
        "description": "Write the starter Scouter config file (~/.config/scouter/config.json "
        "by default). backup.enabled is false in the starter file, so the "
        "first import fails loudly until a target is set. Requires "
        "confirm=true to write, or to replace an existing file.",
        "inputSchema": _schema(
            {
                "path": {"type": "string", "description": "override the config path"},
                "dry_run": DRY_RUN,
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "required to write, or to replace an existing file",
                },
            },
            [],
        ),
    },
    {
        "name": "config_show",
        "description": "Show the loaded Scouter config and whether the backup gate "
        "currently passes -- the one call that answers 'will an import "
        "work right now?' without attempting one.",
        "inputSchema": _schema(
            {"path": {"type": "string", "description": "override the config path"}}, []
        ),
    },
    {
        "name": "metrics_summary",
        "description": "Per-verb calls, error rate by class, p50/p95 latency and "
        "estimated tokens over a time window.",
        "inputSchema": _schema(
            {
                "since": {
                    "type": "string",
                    "default": "7d",
                    "description": "window, e.g. 7d / 12h / 30m",
                }
            },
            [],
        ),
    },
]


def tools() -> list:
    """`TOOL_SPECS` as mcp `Tool` objects."""
    from mcp.types import Tool

    return [
        Tool(name=spec["name"], description=spec["description"], inputSchema=spec["inputSchema"])
        for spec in TOOL_SPECS
    ]
