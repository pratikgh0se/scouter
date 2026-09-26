"""The CLI: the same verbs as the core, JSON on stdout, one exit code per class.

Built FROM the registry, so a verb cannot exist in the core and be missing here
(tests/test_layer_parity.py enforces the other direction too).
"""

from __future__ import annotations

import argparse
import json
import sys

from scouter import __version__, paths, registry
from scouter.cli import tables
from scouter.errors import EXIT_CODES, ScouterError, exit_code_for

#: Verbs excluded from the parity check because they are diagnostics, not verbs.
NON_REGISTRY_COMMANDS = ("doctor",)

_TYPES = {"str": str, "int": int, "float": float, "image": str}

#: verb id -> the required-positional arg name the README / phase 2 spec
#: also document as an option (E12 D2: `style apply-set --name ..`,
#: `export release --job-slug ..`, `export verify --manifest ..` all exited 2
#: because only the positional form was ever wired up). Both forms are
#: accepted; the positional wins if a caller somehow passes both.
_DUAL_MODE_POSITIONALS = {
    "style.apply_set": "name",
    "export.release": "job_slug",
    "export.verify": "manifest",
}


def _add_arg(parser: argparse.ArgumentParser, spec, verb_id: str | None = None) -> None:
    kwargs: dict = {"help": spec.help}
    if spec.choices:
        kwargs["choices"] = list(spec.choices)
    if spec.type == "bool":
        if spec.name == "dry_run":
            parser.add_argument("--dry-run", dest="dry_run", action="store_true", help=spec.help)
        elif spec.name == "confirm":
            parser.add_argument("--yes", dest="confirm", action="store_true", help=spec.help)
        elif spec.name == "commit" and spec.default is True:
            parser.add_argument(
                "--no-commit",
                dest="commit",
                action="store_false",
                help="do not commit history after the write",
            )
        else:
            parser.add_argument(
                f"--{spec.name.replace('_', '-')}",
                dest=spec.name,
                action="store_true",
                help=spec.help,
            )
        return
    if spec.type == "list[str]":
        if spec.positional:
            parser.add_argument(spec.name, nargs="+", **kwargs)
        else:
            parser.add_argument(
                f"--{spec.name.replace('_', '-')}",
                dest=spec.name,
                required=spec.required,
                default=spec.default,
                type=lambda v: [p for p in v.split(",") if p],
                **kwargs,
            )
        return
    kwargs["type"] = _TYPES[spec.type]
    if spec.positional:
        if not spec.required:
            kwargs["nargs"] = "?"
            kwargs["default"] = spec.default
            parser.add_argument(spec.name, **kwargs)
        elif _DUAL_MODE_POSITIONALS.get(verb_id) == spec.name:
            # Accept it positionally (README's original form) OR as
            # --<name> (the form README/spec also show): the positional
            # becomes optional here and an aliased option is added; main()
            # merges the two and raises usage if neither was given.
            parser.add_argument(spec.name, nargs="?", default=None, **kwargs)
            parser.add_argument(
                f"--{spec.name.replace('_', '-')}",
                dest=f"_dual_{spec.name}",
                default=None,
                **kwargs,
            )
        else:
            parser.add_argument(spec.name, **kwargs)
    else:
        parser.add_argument(
            f"--{spec.name.replace('_', '-')}",
            dest=spec.name,
            required=spec.required,
            default=spec.default,
            **kwargs,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scouter",
        description="Live darktable editing core. Every command prints one JSON "
        "object on stdout; logs go to stderr.",
    )
    parser.add_argument("--version", action="version", version=f"scouter {__version__}")
    parser.add_argument("--job", help="job id for the audit log and the undo journal")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="for humans: indent the JSON, or swap in an aligned table for the few "
        "verbs whose payload is one (cull.propose, outliers.list, library.jobs). "
        "A plain invocation (no --pretty) is the machine contract: compact JSON, always",
    )
    parser.add_argument(
        "--json", action="store_true", help="accepted and ignored: output is always JSON"
    )
    groups = parser.add_subparsers(dest="group", required=True)

    verbs = registry.load_verbs()
    by_group: dict[str, list] = {}
    for verb in verbs.values():
        by_group.setdefault(verb.group, []).append(verb)

    for group_name in sorted(by_group):
        group_parser = groups.add_parser(group_name, help=f"{group_name} verbs")
        commands = group_parser.add_subparsers(dest="command", required=True)
        for verb in sorted(by_group[group_name], key=lambda v: v.cli_command):
            sub = commands.add_parser(verb.cli_command, help=verb.summary, description=verb.summary)
            sub.set_defaults(_verb=verb.id, _args=[a.name for a in verb.args])
            for spec in verb.args:
                _add_arg(sub, spec, verb.id)
            # `--pretty` (and `--job`) are documented as belonging to `scouter`
            # itself, "before the group and command" -- but the README's own
            # phase 2 examples put `--pretty` AFTER the group/verb too (E12
            # D2: `cull propose --job-slug .. --pretty`, `outliers list ..
            # --pretty`). argparse subparsers don't inherit the parent's
            # optionals, so both positions are wired here; `default=SUPPRESS`
            # means "not given here" leaves whatever the top-level parser
            # already put in the namespace alone.
            sub.add_argument("--pretty", dest="pretty", action="store_true", default=argparse.SUPPRESS)

    doctor = groups.add_parser(
        "doctor",
        help="check darktable, the plugin and the view",
        description="Exits 0 when everything checks out, 3 when the bridge timed "
        "out reaching darktable, 2 for a usage error (a bad flag or "
        "job id), 1 for every other failure (wrong view, missing "
        "plugin, etc.).",
    )
    doctor.set_defaults(_verb=None, _args=[])
    return parser


def _doctor() -> dict:
    import shutil
    from pathlib import Path

    from darktable_mcp.bridge.client import plugin_path as bridge_plugin_path
    from scouter import paths
    from scouter.core import dtbridge, scout

    # Through the bridge's OWN resolver (fix round 2, item 2): this used to
    # recompute the path from `Path.home()`, which ignores
    # `DARKTABLE_MCP_PLUGIN_PATH` -- so `doctor` could report
    # `plugin_installed: true` about a file the bridge was not looking at, or
    # false about one it was. A doctor line that contradicts the thing it is
    # reporting on is worse than no line.
    plugin = bridge_plugin_path()
    report: dict = {
        "verb": "doctor",
        "darktable": "/Applications/darktable.app/Contents/MacOS/darktable",
        "darktable_present": Path("/Applications/darktable.app/Contents/MacOS/darktable").is_file()
        or bool(shutil.which("darktable")),
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

    # Row 422: surface the main-thread proxy's resolved action path (or its
    # absence) directly, instead of only ever discovering a wrong path when
    # some later write verb's `history.commit` refuses mid-session. `doctor`
    # never raises for this -- an unresolved proxy is a report line, not a
    # reason to fail the whole check, same rule as the config half below.
    try:
        diag = dtbridge.diag()
        path = diag.get("main_thread_proxy_path")
        fired = diag.get("main_thread_proxy_fired")
        report["main_thread_proxy"] = path if (path and fired) else "not firing"
    except ScouterError:
        report["main_thread_proxy"] = "not firing"

    # Phase 2's four lines (task 14 step 4). `doctor` is the one verb whose
    # job is to answer "why did that refuse?" before the refusal happens,
    # and phase 2 added four ways to be mis-set up that phase 1 had no way
    # to report: no config file, a disarmed backup gate, a missing
    # encryption binary, and a library root that is absent or full.
    #
    # NONE of these change `report["ok"]`, which stays a statement about
    # the BRIDGE. A config that has never been written is the correct state
    # for a fresh install, and `doctor` exiting non-zero for it would make
    # the first thing a new user runs look broken.
    _doctor_config(report)
    report["ok"] = report["bridge"] == "ok"
    return report


def _doctor_config(report: dict) -> None:
    """The config/backup/encryption/library half of `doctor`, in its own
    function so `_doctor`'s bridge half stays readable.

    Every branch here is a report line, never a raise: `doctor` must be
    runnable when the config is broken -- that is exactly when it is most
    needed.
    """
    import shutil
    from pathlib import Path

    from scouter import config as config_module
    from scouter import paths

    report["config_path"] = str(paths.config_path())
    report["config_exists"] = Path(paths.config_path()).is_file()
    cfg = None
    try:
        cfg = config_module.load()
        report["config_parses"] = True
        report["config_error"] = None
    except ScouterError as e:
        report["config_parses"] = False
        report["config_error"] = f"{e.code}: {e.message}"

    if cfg is None:
        report["backup_gate"] = "config_unreadable: see config_error"
        report["encryption_tool"] = None
        report["library_root"] = None
        report["library_root_exists"] = False
        report["library_free_bytes"] = None
        return

    try:
        config_module.require_backup(cfg)
        report["backup_gate"] = "ok"
    except ScouterError as e:
        report["backup_gate"] = f"{e.code}: {e.message}"

    # The resolved PATH of the tool this config would actually use, not just
    # its name -- "encryption: openssl" is not the same fact as "the openssl
    # this will run is /opt/homebrew/bin/openssl".
    # `backup._openssl` is the resolver `backup.run` ACTUALLY uses, so
    # `doctor` calls it rather than re-deriving the same preference order
    # inline (fix round 1, minor a). A second copy of "prefer
    # /opt/homebrew/bin/openssl, else $PATH" is a doctor line that can drift
    # away from the thing it is reporting on -- which is the one way a doctor
    # line is worse than no doctor line. `EncryptionUnavailable` means no
    # binary was found, which is a report value (`None`), not a raise.
    from scouter.core import backup as backup_module

    encryption = cfg.backup.encryption
    if encryption == "age":
        report["encryption_tool"] = shutil.which("age")
    elif encryption == "openssl":
        try:
            report["encryption_tool"] = backup_module._openssl()
        except ScouterError:
            report["encryption_tool"] = None
    else:
        report["encryption_tool"] = None
    report["encryption"] = encryption

    root = cfg.library_root
    report["library_root"] = root
    if not root:
        report["library_root_exists"] = False
        report["library_free_bytes"] = None
        return
    root_path = Path(root).expanduser()
    report["library_root_exists"] = root_path.is_dir()
    try:
        report["library_free_bytes"] = shutil.disk_usage(
            root_path if root_path.is_dir() else root_path.parent
        ).free
    except OSError as e:
        report["library_free_bytes"] = None
        report["library_free_bytes_error"] = str(e)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    pretty = bool(getattr(namespace, "pretty", False))

    def emit(payload: dict, code: int, verb_for_table: str | None = None) -> int:
        # `--pretty` is for humans: for the handful of verbs whose payload IS
        # a table (cull.propose, outliers.list, library.jobs), it swaps the
        # JSON out for an aligned table read straight off stdout. A plain
        # invocation (no --pretty) always gets JSON -- that stays the one
        # machine contract, documented in --help.
        if pretty and verb_for_table is not None:
            rendered = tables.render(verb_for_table, payload.get("data", {}))
            if rendered is not None:
                print(rendered)
                return code
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
    dual_name = _DUAL_MODE_POSITIONALS.get(verb_id)
    if dual_name is not None:
        positional_val = getattr(namespace, dual_name, None)
        option_val = getattr(namespace, f"_dual_{dual_name}", None)
        if positional_val is None and option_val is not None:
            setattr(namespace, dual_name, option_val)
        elif positional_val is None and option_val is None:
            parser.error(f"the following arguments are required: {dual_name}")
    kwargs = {
        name: getattr(namespace, name)
        for name in namespace._args
        if getattr(namespace, name, None) is not None
    }
    try:
        # `--job` becomes a file name under the cache root, so it is validated
        # before anything opens a file with it. Inside the try so the refusal
        # comes back as the same JSON envelope every other failure does.
        if namespace.job:
            kwargs["job"] = paths.validate_job(namespace.job)
        envelope = registry.call(verb_id, "cli", **kwargs)
    except ScouterError as e:
        payload = {
            "verb": verb_id,
            "layer": "cli",
            "ok": False,
            "outcome": f"error:{e.code}",
            "error": e.to_dict(),
        }
        return emit(payload, exit_code_for(f"error:{e.code}"))
    except ValueError as e:
        payload = {
            "verb": verb_id,
            "layer": "cli",
            "ok": False,
            "outcome": "usage",
            "error": {"code": "usage", "message": str(e)},
        }
        return emit(payload, 2)
    except KeyboardInterrupt:
        payload = {
            "verb": verb_id,
            "layer": "cli",
            "ok": False,
            "outcome": "error",
            "error": {"code": "interrupted", "message": "interrupted by the user"},
        }
        return emit(payload, 130)
    except Exception as e:  # noqa: BLE001 - last resort so the CLI always emits JSON
        payload = {
            "verb": verb_id,
            "layer": "cli",
            "ok": False,
            "outcome": "error",
            "error": {"code": "error", "message": str(e)},
        }
        return emit(payload, EXIT_CODES["error"])
    return emit(envelope.to_dict(), exit_code_for(envelope.outcome), verb_id)


if __name__ == "__main__":
    sys.exit(main())
