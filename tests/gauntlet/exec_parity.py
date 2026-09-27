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


#: PAR-hollow-pass fixture staging (triage rank 4): the literal job slug and
#: style name SMOKE_ARGS's own fixture-needing verbs already expect (cull.
#: apply, export.release, library.register/verify, preset.apply_job,
#: style.apply_set).
SMOKE_JOB_SLUG = "smoke-job"
SMOKE_STYLE_NAME = "smoke"


def _imported_path(session, envelope: dict | None, source_name: str):
    """The absolute path `library_import` actually wrote `source_name` to,
    using ITS OWN envelope (`data.files[].source_name` -> `.dest`, relative to
    the job directory -- spec section 6, ImportResult); mirrors
    tests/gauntlet/exec_bulk.py's `_imported_frames`. `None` when the
    envelope carries nothing usable (the import did not run or errored)."""
    data = (envelope or {}).get("data") or {}
    files = data.get("files") or []
    dest_by_source = {f.get("source_name"): f.get("dest") for f in files if f.get("source_name")}
    dest = dest_by_source.get(source_name)
    if not dest or not data.get("job_slug") or not data.get("date"):
        return None
    from scouter import config as scouter_config

    cfg = scouter_config.load(str(session.staged.config))
    job_path = scouter_config.job_dir(cfg, data["date"], data["job_slug"])
    return job_path / dest


def _stage_side(session, path):
    """PAR-hollow-pass 'not in library' cases (triage: 10 happy variants, 2
    P0 -- scout.stats, wb.kelvin): a per-side parity copy that nothing ever
    imported answers "not in library" (or an equivalent honest miss) on
    BOTH sides, so the verb never really runs even though the pair agrees.
    Import it into the library, under job `smoke-job`, via `library_import`
    -- never `darkroom_open` (LK-frame-switch-relaunch: darktable cannot
    load a second, differently-named raw into the darkroom while another is
    loaded, in the same running instance). Falls back to the raw copy when
    the import did not run or named nothing usable: a downstream verb whose
    own fixture still needs the library then fails honestly on its own
    terms, never on a guessed path."""
    rec = session.call("library_import", {"source": str(path.parent),
                                          "job_slug": SMOKE_JOB_SLUG}, 30.0, write=True)
    resolved = _imported_path(session, rec.envelope, path.name)
    return resolved if resolved is not None else path


def _ensure_smoke_style(session, path) -> None:
    """PAR-hollow-pass 'fixture missing' cases (triage: 6 happy variants, 2
    P0 -- export.release, preset.apply_job -- need a real darktable style
    named 'smoke' to already exist). Once per session (idempotent: a
    session-scoped flag, since every parity case would otherwise re-stage
    it): `style.extract` with `modules=[]` (SMOKE_ARGS's own style.extract
    args) writes an empty-history style of that name without needing the
    darkroom to show `path` (style.py extract's filtered path takes no
    `dtbridge.require_darkroom()`)."""
    if getattr(session, "_parity_smoke_style_done", False):
        return
    session.call("style_extract", {"image": str(path), "name": SMOKE_STYLE_NAME, "modules": []},
                 30.0, write=True)
    session._parity_smoke_style_done = True


def _call_with_frame_switch(session, tool: str, args: dict, path, budget_s: float,
                            write: bool) -> CallRecord:
    """LK-frame-switch-relaunch (triage, PAR-hollow-pass note 3:
    'darkroom.open-happy ... fails on both sides with "wrong image loaded"
    ... known darkroom.open image-switch limit'): darktable cannot load a
    second, differently-named raw into the darkroom while another is
    loaded, in the SAME running instance. Retried once via a full relaunch
    (mirrors `ScouterSession._relaunch_then_reopen`) before the pair is
    scored, so a genuine open gets a real second chance rather than being
    scored on a harness artefact."""
    rec = session.call(tool, args, budget_s, write=write)
    if rec.error == "not_in_darkroom":
        session.relaunch(path)
        rec = session.call(tool, args, budget_s, write=write)
    if rec.error is None and (rec.envelope or {}).get("ok"):
        session.current_frame = path
    return rec


def _run_cli(argv: list[str], env: dict, budget_s: float):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=budget_s + 30)


def _run_cli_with_frame_switch(session, argv: list[str], path, budget_s: float):
    """The CLI side of `_call_with_frame_switch`: the CLI subprocess talks to
    the SAME running darktable through the shared `session.staged.env`, so it
    can hit the identical frame-switch limit. One retry, after a relaunch
    onto `path`, on the same terms as the MCP side."""
    p = _run_cli(argv, session.staged.env, budget_s)
    try:
        env = json.loads(p.stdout)
    except ValueError:
        env = None
    if isinstance(env, dict) and (env.get("error") or {}).get("code") == "not_in_darkroom":
        session.relaunch(path)
        p = _run_cli(argv, session.staged.env, budget_s)
    return p


def parity_pair(session, case, step, obs) -> CallRecord:
    verb_id = step.args["verb"]
    hero = session.staged.frames["hero"]
    mcp_copy = copy_readonly(hero, session.root / "parity" / "mcp" / hero.name)
    cli_copy = copy_readonly(hero, session.root / "parity" / "cli" / hero.name)
    is_open = verb_id == "darkroom.open"
    if not is_open:
        mcp_copy = _stage_side(session, mcp_copy)
        cli_copy = _stage_side(session, cli_copy)
        _ensure_smoke_style(session, mcp_copy)
    session.staged.frames["hero"] = mcp_copy
    mcp_args = substitute(step.args["args"], session, [])
    if is_open:
        mcp_rec = _call_with_frame_switch(session, case.meta["mcp"], mcp_args, mcp_copy,
                                          step.budget_s, step.write)
    else:
        mcp_rec = session.call(case.meta["mcp"], mcp_args, step.budget_s, write=step.write)
    session.staged.frames["hero"] = cli_copy
    cli_args = substitute(step.args["args"], session, [])
    session.staged.frames["hero"] = hero
    argv = [sys.executable, "-m", "scouter", *cli_argv(verb_id, cli_args)]
    try:
        if is_open:
            p = _run_cli_with_frame_switch(session, argv, cli_copy, step.budget_s)
        else:
            p = _run_cli(argv, session.staged.env, step.budget_s)
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
