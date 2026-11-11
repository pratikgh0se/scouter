"""The CLI: the same verbs as the core, JSON on stdout, one exit code per class.

Built FROM the registry, so a verb cannot exist in the core and be missing here
(tests/test_layer_parity.py enforces the other direction too).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from scouter import __version__, paths, registry
from scouter.cli import tables
from scouter.errors import EXIT_CODES, ScouterError, exit_code_for

#: Verbs excluded from the parity check because they are diagnostics, not verbs.
NON_REGISTRY_COMMANDS = ("doctor", "setup")

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


class _JsonErrorParser(argparse.ArgumentParser):
    """PAR-cli-argparse-not-json (triage rank 3): argparse's OWN `error()` --
    reached for an unknown flag, a missing required argument, or (the top
    parser's own `parse_args`) leftover unrecognized arguments -- printed
    plain text to stderr and exited 2, bypassing the one machine contract
    `main()` otherwise guarantees ("a plain invocation always gets JSON").
    The MCP side of the same mistake answers a typed `error:usage` envelope
    (`scouter/mcp/server.py`'s TypeError branch), so the two disagreed on
    shape (parity layer 6: par-vocab.list-error, par-darkroom.undo_last-error
    and 6 more, `envelope, code` diffs).

    Every subparser this module builds inherits this class: argparse's own
    `add_subparsers` defaults `parser_class` to `type(self)`, so a bad flag
    anywhere in the tree -- top-level, group (`scouter vocab ...`) or verb
    (`scouter vocab list ...`) -- answers the same envelope.

    Review fix round 2, C-1 (finding 12): an UNRECOGNIZED argument (any
    flag argparse's whole tree does not define, e.g. `vocab list --bogus`)
    is caught by `ArgumentParser.parse_args` itself, on the TOP-level
    parser -- `parse_known_args` has already filled the verb into the
    namespace by the time this runs, but the top parser's OWN `_defaults`
    never had `_verb` (only the verb's own subparser does), so `error()`
    used to report `"verb": null` for every such case. `parse_args` is
    overridden to stash the parsed namespace first, so `error()` can read
    the real verb off it."""

    def parse_args(self, args=None, namespace=None):  # noqa: D102 - mirrors argparse's own
        ns, extras = self.parse_known_args(args, namespace)
        if extras:
            self._htc_ns = ns
            self.error(argparse._("unrecognized arguments: %s") % " ".join(extras))
        return ns

    def error(self, message: str) -> None:
        ns = getattr(self, "_htc_ns", None)
        verb = getattr(ns, "_verb", None) if ns is not None else self._defaults.get("_verb")
        payload = {
            "verb": verb,
            "layer": "cli",
            "ok": False,
            "outcome": "error:usage",
            "error": {
                "code": "usage",
                "message": message,
                "fix": "fix the argument and re-run; `--help` lists the accepted values",
                "detail": {},
            },
        }
        print(json.dumps(payload, sort_keys=True))
        self.exit(EXIT_CODES["usage"])


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonErrorParser(
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
            sub.add_argument(
                "--pretty", dest="pretty", action="store_true", default=argparse.SUPPRESS
            )

    doctor = groups.add_parser(
        "doctor",
        help="check darktable, the plugin and the view",
        description="Exits 0 when everything checks out, 3 when the bridge timed "
        "out reaching darktable, 2 for a usage error (a bad flag or "
        "job id), 1 for every other failure (wrong view, missing "
        "plugin, etc.).",
    )
    doctor.add_argument(
        "--probe-main-thread",
        dest="probe_main_thread",
        action="store_true",
        help="ask the plugin which routes reach darktable's GTK main thread "
        "(darktable_mcp.probe_main_thread) and print the table",
    )
    doctor.add_argument(
        "--switch-views",
        dest="switch_views",
        action="store_true",
        help="with --probe-main-thread: also do a darkroom -> lighttable -> darkroom "
        "round trip through darktable's switch-views commands (start in the darkroom)",
    )
    doctor.add_argument(
        "--ping",
        dest="ping",
        action="store_true",
        help="print only the watchdog line; exit 0 when darktable answers, 1 when not",
    )
    doctor.add_argument(
        "--heal",
        dest="heal",
        action="store_true",
        help="quit and relaunch the darktable Scouter launched (never a user's), then wait "
        "for gui_ready; exits 0 when healed",
    )
    doctor.set_defaults(_verb=None, _args=[])

    setup_parser = groups.add_parser(
        "setup",
        help="install the Lua plugin persistently, check doctor, print the MCP registration",
        description="Idempotent. Installs the darktable Lua plugin and its luarc line "
        "(and leaves them installed), copies the measured white-balance tables into "
        "the config's wb/ dir when missing, runs doctor, and prints the exact "
        "`claude mcp add` command plus the equivalent JSON. --check changes nothing. "
        "Exits 0 when every required item is ok, 1 otherwise.",
    )
    setup_parser.add_argument(
        "--check", action="store_true", help="report each item ok/missing; change nothing"
    )
    setup_parser.set_defaults(_verb=None, _args=[])
    return parser


def _yes_no(value) -> str:
    if value is None:
        return "-"
    return "yes" if value else "no"


def render_probe_table(probe: dict) -> str:
    """Plain-text table for `doctor --probe-main-thread`: one line per route,
    with the source-derived thread and expectation underneath."""
    rows = probe.get("rows") or []
    header = ("ROUTE", "CANDIDATE", "RAN", "VIEW-READ", "GUI-TOUCH", "DETAIL")
    table = [header]
    for r in rows:
        ran = "skip" if r.get("skipped") else _yes_no(r.get("ran"))
        table.append(
            (
                str(r.get("route", "")),
                str(r.get("candidate", "")),
                ran,
                _yes_no(r.get("view_read_ok")) if not r.get("skipped") else "-",
                _yes_no(r.get("gui_touch_ok")) if not r.get("skipped") else "-",
                str(r.get("detail", "")),
            )
        )
    widths = [max(len(row[i]) for row in table) for i in range(5)]
    recommended = probe.get("recommended_view_transport") or "none (run with --switch-views)"
    lines = [
        f"darktable Lua API {probe.get('api_version') or '?'}; view {probe.get('view', '?')}; "
        f"current view transport {probe.get('view_switch_transport', '?')}; "
        f"recommended {recommended}",
        "",
    ]
    for i, row in enumerate(table):
        lines.append("  ".join(row[j].ljust(widths[j]) for j in range(5)) + "  " + row[5])
        if i > 0:
            r = rows[i - 1]
            lines.append(f"    thread: {r.get('thread', '?')}")
            lines.append(f"    expect: {r.get('expect', '?')}")
    return "\n".join(lines)


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

    # Row 422/429: the plugin's view-switch transport. "action" is the only
    # one since row 429 removed the main-thread proxy; anything else (or no
    # answer) means an old plugin install -- `scouter setup` reinstalls it.
    # A report line, never a reason to fail the whole check.
    try:
        diag = dtbridge.diag()
        report["view_transport"] = str(diag.get("view_switch_transport") or "unknown")
    except ScouterError:
        report["view_transport"] = "unknown"

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
    _doctor_watchdog(report)
    _doctor_config(report)
    report["ok"] = report["bridge"] == "ok" and report.get("watchdog") != "unresponsive"
    return report


def _doctor_watchdog(report: dict) -> None:
    """Watchdog design 2.5: ping state, gui_ready, the darktable pid and who
    launched it. Report lines only."""
    from scouter.core import dtprocess

    h = _watchdog_health()
    report["watchdog"] = h["state"]
    report["watchdog_line"] = f"watchdog: ping {h['state']} ({h.get('ping_ms')} ms)"
    report["gui_ready"] = h.get("gui_ready")
    report["gui_ready_line"] = (
        f"gui_ready: {str(h.get('gui_ready')).lower()} " f"({h.get('gui_ready_source') or '-'})"
    )
    owned = dtprocess.owned_pid()
    try:
        running: list[int] | None = dtprocess.running_pids()
    except (OSError, subprocess.SubprocessError):
        # Fix round 1 (I2): pgrep could not answer -- "unknown", never
        # "none running", and never a traceback that loses the config lines.
        running = None
    if owned:
        pid, who = owned, "launched by scouter"
    elif running is None:
        pid, who = "unknown", "pid lookup failed"
    elif running:
        pid, who = running[0], "user"
    else:
        pid, who = None, "-"
    report["darktable_pid_line"] = f"darktable pid: {pid or '-'} ({who})"

    from scouter import config
    from scouter.core import watchdog
    from scouter.paths import current_job

    try:
        wd = config.load().watchdog
    except ScouterError:
        wd = config.WatchdogConfig()
    used = watchdog.heals_used(current_job())
    report["auto_heal_line"] = (
        f"auto_heal: {'on' if wd.auto_heal else 'off'}, heals this job: "
        f"{used}/{wd.max_heals_per_job}"
    )


def _watchdog_health() -> dict:
    """`dtbridge.health()`, except that no answer with NO darktable process
    reads `not_running`, not `unresponsive` (Task 29 review carry-over). A
    pgrep that cannot answer leaves `unresponsive` as it was."""
    from scouter.core import dtbridge, dtprocess

    h = dtbridge.health()
    if h["state"] == "unresponsive":
        try:
            running = dtprocess.running_pids()
        except (OSError, subprocess.SubprocessError):
            running = None
        if running == []:
            h = {**h, "state": "not_running"}
    return h


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

    if namespace.group == "doctor" and getattr(namespace, "probe_main_thread", False):
        from scouter.core import dtbridge

        try:
            probe = dtbridge.probe_main_thread(
                switch_views=bool(getattr(namespace, "switch_views", False))
            )
        except ScouterError as e:
            return emit({"verb": "doctor", "ok": False, "error": e.to_dict()}, 1)
        print(render_probe_table(probe))
        return 0

    if namespace.group == "doctor" and getattr(namespace, "ping", False):
        h = _watchdog_health()
        return emit(
            {
                "verb": "doctor",
                "watchdog": h["state"],
                "ping_ms": h.get("ping_ms"),
                "ok": h["state"] not in ("unresponsive", "not_running"),
            },
            0 if h["state"] not in ("unresponsive", "not_running") else 1,
        )

    if namespace.group == "doctor" and getattr(namespace, "heal", False):
        from scouter.core import watchdog
        from scouter.paths import current_job

        res = watchdog.heal_darktable(current_job(), 60.0)
        watchdog.telemetry(current_job(), "watchdog.heal", res["outcome"], res.get("ms", 0), res)
        return emit(
            {"verb": "doctor", **res, "ok": res["outcome"] == "healed"},
            0 if res["outcome"] == "healed" else 1,
        )

    if namespace.group == "doctor":
        report = _doctor()
        if report["ok"]:
            code = 0
        elif report["bridge"] == "bridge_timeout":
            code = 3
        else:
            code = 1
        return emit(report, code)

    if namespace.group == "setup":
        from scouter.cli import setup as setup_module

        report = setup_module.run(check=bool(namespace.check))
        return emit(report, 0 if report["ok"] else 1)

    verb_id = namespace._verb
    dual_name = _DUAL_MODE_POSITIONALS.get(verb_id)
    if dual_name is not None:
        positional_val = getattr(namespace, dual_name, None)
        option_val = getattr(namespace, f"_dual_{dual_name}", None)
        if positional_val is None and option_val is not None:
            setattr(namespace, dual_name, option_val)
        elif positional_val is None and option_val is None:
            # C-1: `namespace` already has the real `_verb` (parse_args
            # succeeded; this is an application-level check, not argparse's
            # own parsing) -- stash it so `_JsonErrorParser.error()` reports
            # it instead of `null`.
            parser._htc_ns = namespace
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
        # Triage item 14 / review fix round 2, finding 12: this used to say
        # outcome "usage", the one place in the CLI that broke the
        # "error:<code>" shape every other typed failure follows -- the MCP
        # side of the same case (server.py's TypeError branch) already
        # answers "error:usage", so this was its own parity diff
        # (PAR-cli-usage-outcome).
        payload = {
            "verb": verb_id,
            "layer": "cli",
            "ok": False,
            "outcome": "error:usage",
            "error": {"code": "usage", "message": str(e)},
        }
        return emit(payload, EXIT_CODES["usage"])
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
