"""Layer 6 executor (spec 3.6): the same verb and arguments through the MCP
stdio server and through `scouter <group> <verb>` (JSON on stdout), each on
its own fresh copy of the frame, envelopes compared after normalising."""

from __future__ import annotations

import json
import subprocess
import sys

from gauntlet.adapter import CallRecord
from tests.gauntlet.corpus import copy_readonly
from tests.gauntlet.execute import VIRTUAL, substitute

VOLATILE = {"ms", "job", "layer", "ts", "id", "duration_s", "elapsed_ms", "event_id", "path",
            "dtstyle_path", "memory_path", "manifest", "out_dir", "image_id"}


def normalise(env: dict | None) -> dict:
    env = env or {}
    data = env.get("data") or {}
    err = env.get("error") or {}
    return {"ok": bool(env.get("ok")), "outcome": env.get("outcome"),
            "code": err.get("code"),
            "data_keys": sorted(k for k in data if k not in VOLATILE),
            "values": {k: v for k, v in sorted(data.items())
                       if k not in VOLATILE and isinstance(v, (bool, int, float, str))}}


def cli_argv(verb_id: str, args: dict) -> list[str]:
    from scouter import registry

    verb = registry.load_verbs()[verb_id]
    argv = [verb.group, verb.cli_command]
    for spec in verb.args:
        if spec.name not in args:
            continue
        val = args[spec.name]
        if spec.type == "bool":
            if val:
                if spec.name == "commit" and spec.default is True:
                    # PAR-set-commit-flag: `commit=True` is already the
                    # default (the CLI defines only `--no-commit`, for a
                    # default-True commit, never a `--commit` flag), so
                    # nothing is emitted here.
                    pass
                else:
                    argv.append({"dry_run": "--dry-run", "confirm": "--yes"}.get(
                        spec.name, f"--{spec.name.replace('_', '-')}"))
            elif spec.name == "commit" and spec.default is True:
                argv.append("--no-commit")
            continue
        text = ",".join(map(str, val)) if isinstance(val, list) else str(val)
        if spec.positional:
            argv.extend(val if isinstance(val, list) else [text])
        else:
            argv += [f"--{spec.name.replace('_', '-')}", text]
    extra = [k for k in args if k not in {s.name for s in verb.args}]
    for k in extra:
        argv += [f"--{k.replace('_', '-')}", str(args[k])]
    return argv


def compare(a: dict, b: dict) -> list[str]:
    na, nb = normalise(a), normalise(b)
    return [k for k in na if na[k] != nb[k]]


def parity_pair(session, case, step, obs) -> CallRecord:
    verb_id = step.args["verb"]
    hero = session.staged.frames["hero"]
    mcp_copy = copy_readonly(hero, session.root / "parity" / "mcp" / hero.name)
    cli_copy = copy_readonly(hero, session.root / "parity" / "cli" / hero.name)
    session.staged.frames["hero"] = mcp_copy
    mcp_args = substitute(step.args["args"], session, [])
    mcp_rec = session.call(case.meta["mcp"], mcp_args, step.budget_s, write=step.write)
    session.staged.frames["hero"] = cli_copy
    cli_args = substitute(step.args["args"], session, [])
    session.staged.frames["hero"] = hero
    argv = [sys.executable, "-m", "scouter", *cli_argv(verb_id, cli_args)]
    try:
        p = subprocess.run(argv, env=session.staged.env, capture_output=True, text=True,
                           timeout=step.budget_s + 30)
    except subprocess.TimeoutExpired:
        obs.blocked = "hang: scouter cli exceeded budget"
        obs.hang_evidence.append(f"parity_pair: scouter cli {verb_id} exceeded "
                                 f"{step.budget_s + 30:.0f}s")
        obs.extra["envelopes_match"] = False
        obs.extra["parity_diffs"] = ["timeout"]
        return CallRecord("parity_pair", step.args, error="timeout", write=step.write)
    except OSError as e:
        obs.extra["envelopes_match"] = False
        obs.extra["parity_diffs"] = ["transport"]
        return CallRecord("parity_pair", step.args, error=f"transport: {e}", write=step.write)
    try:
        cli_env = json.loads(p.stdout)
    except ValueError:
        cli_env = {"ok": False, "outcome": f"exit {p.returncode}", "raw": p.stdout[-300:],
                   "stderr": p.stderr[-300:]}
    diffs = compare(mcp_rec.envelope, cli_env)
    obs.extra["envelopes_match"] = not diffs
    obs.extra["parity_diffs"] = diffs
    obs.extra["cli_envelope"] = cli_env
    # PAR-hollow-pass (triage rank 2): the pair agreeing is not, on its own,
    # evidence the verb ran -- both sides answering the SAME failure means it
    # never did. `gauntlet.evaluate.evaluate` scores this `needs_review`
    # unless the policy flag `parity.same_failure_is_pass` is set.
    obs.extra["parity_both_failed"] = (not diffs) and \
        not normalise(mcp_rec.envelope)["ok"] and not normalise(cli_env)["ok"]
    return CallRecord("parity_pair", step.args, {"ok": not diffs, "outcome": "ok",
                                                 "data": {"diffs": diffs}}, write=step.write)


VIRTUAL["parity_pair"] = parity_pair
