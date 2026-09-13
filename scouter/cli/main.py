"""The CLI: the same verbs as the core, JSON on stdout, one exit code per class.

Built FROM the registry, so a verb cannot exist in the core and be missing here
(tests/test_layer_parity.py enforces the other direction too).
"""

from __future__ import annotations

import argparse
import json
import sys

from scouter import __version__, registry
from scouter.errors import EXIT_CODES, ScouterError, exit_code_for

#: Verbs excluded from the parity check because they are diagnostics, not verbs.
NON_REGISTRY_COMMANDS = ("doctor",)

_TYPES = {"str": str, "int": int, "float": float, "image": str}


def _add_arg(parser: argparse.ArgumentParser, spec) -> None:
    kwargs: dict = {"help": spec.help}
    if spec.choices:
        kwargs["choices"] = list(spec.choices)
    if spec.type == "bool":
        if spec.name == "dry_run":
            parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                                help=spec.help)
        elif spec.name == "confirm":
            parser.add_argument("--yes", dest="confirm", action="store_true",
                                help=spec.help)
        elif spec.name == "commit" and spec.default is True:
            parser.add_argument("--no-commit", dest="commit", action="store_false",
                                help="do not commit history after the write")
        else:
            parser.add_argument(f"--{spec.name.replace('_', '-')}", dest=spec.name,
                                action="store_true", help=spec.help)
        return
    if spec.type == "list[str]":
        if spec.positional:
            parser.add_argument(spec.name, nargs="+", **kwargs)
        else:
            parser.add_argument(f"--{spec.name.replace('_', '-')}", dest=spec.name,
                                required=spec.required, default=spec.default,
                                type=lambda v: [p for p in v.split(",") if p], **kwargs)
        return
    kwargs["type"] = _TYPES[spec.type]
    if spec.positional:
        if not spec.required:
            kwargs["nargs"] = "?"
            kwargs["default"] = spec.default
        parser.add_argument(spec.name, **kwargs)
    else:
        parser.add_argument(f"--{spec.name.replace('_', '-')}", dest=spec.name,
                            required=spec.required, default=spec.default, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scouter",
        description="Live darktable editing core. Every command prints one JSON "
                    "object on stdout; logs go to stderr.",
    )
    parser.add_argument("--version", action="version", version=f"scouter {__version__}")
    parser.add_argument("--job", help="job id for the audit log and the undo journal")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    parser.add_argument("--json", action="store_true",
                        help="accepted and ignored: output is always JSON")
    groups = parser.add_subparsers(dest="group", required=True)

    verbs = registry.load_verbs()
    by_group: dict[str, list] = {}
    for verb in verbs.values():
        by_group.setdefault(verb.group, []).append(verb)

    for group_name in sorted(by_group):
        group_parser = groups.add_parser(group_name, help=f"{group_name} verbs")
        commands = group_parser.add_subparsers(dest="command", required=True)
        for verb in sorted(by_group[group_name], key=lambda v: v.cli_command):
            sub = commands.add_parser(verb.cli_command, help=verb.summary,
                                      description=verb.summary)
            sub.set_defaults(_verb=verb.id, _args=[a.name for a in verb.args])
            for spec in verb.args:
                _add_arg(sub, spec)

    doctor = groups.add_parser(
        "doctor",
        help="check darktable, the plugin and the view",
        description="Exits 0 when everything checks out, 3 when the bridge timed "
                    "out reaching darktable, 1 for every other failure (wrong "
                    "view, missing plugin, etc.).",
    )
    doctor.set_defaults(_verb=None, _args=[])
    return parser


def _doctor() -> dict:
    import shutil
    from pathlib import Path

    from scouter import paths
    from scouter.core import dtbridge, scout

    plugin = Path.home() / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    report: dict = {
        "verb": "doctor",
        "darktable": "/Applications/darktable.app/Contents/MacOS/darktable",
        "darktable_present": Path(
            "/Applications/darktable.app/Contents/MacOS/darktable"
        ).is_file() or bool(shutil.which("darktable")),
        "plugin_installed": plugin.is_file(),
        "plugin_path": str(plugin),
        "cache_root": str(paths.cache_root()),
        "events": str(paths.events_path()),
        "metrics_db": str(paths.db_path()),
        "cli_configdir": str(paths.cli_configdir()),
    }
    try:
        report["darktable_cli"] = scout.cli_binary()
    except ScouterError as e:
        report["darktable_cli"] = None
        report["darktable_cli_error"] = e.message
    try:
        report["view"] = dtbridge.current_view()
        report["bridge"] = "ok"
    except ScouterError as e:
        report["view"] = None
        report["bridge"] = e.code
        report["bridge_fix"] = e.fix
    report["ok"] = report["bridge"] == "ok"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    pretty = bool(getattr(namespace, "pretty", False))

    def emit(payload: dict, code: int) -> int:
        print(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))
        return code

    if namespace.group == "doctor":
        report = _doctor()
        if report["ok"]:
            code = 0
        elif report["bridge"] == "bridge_timeout":
            code = 3
        else:
            code = 1
        return emit(report, code)

    verb_id = namespace._verb
    kwargs = {name: getattr(namespace, name) for name in namespace._args
              if getattr(namespace, name, None) is not None}
    if namespace.job:
        kwargs["job"] = namespace.job
    try:
        envelope = registry.call(verb_id, "cli", **kwargs)
    except ScouterError as e:
        payload = {"verb": verb_id, "layer": "cli", "ok": False,
                   "outcome": f"error:{e.code}", "error": e.to_dict()}
        return emit(payload, exit_code_for(f"error:{e.code}"))
    except ValueError as e:
        payload = {"verb": verb_id, "layer": "cli", "ok": False, "outcome": "usage",
                   "error": {"code": "usage", "message": str(e)}}
        return emit(payload, 2)
    except KeyboardInterrupt:
        payload = {"verb": verb_id, "layer": "cli", "ok": False, "outcome": "error",
                   "error": {"code": "interrupted", "message": "interrupted by the user"}}
        return emit(payload, 130)
    except Exception as e:  # noqa: BLE001 - last resort so the CLI always emits JSON
        payload = {"verb": verb_id, "layer": "cli", "ok": False, "outcome": "error",
                   "error": {"code": "error", "message": str(e)}}
        return emit(payload, EXIT_CODES["error"])
    return emit(envelope.to_dict(), exit_code_for(envelope.outcome))


if __name__ == "__main__":
    sys.exit(main())
