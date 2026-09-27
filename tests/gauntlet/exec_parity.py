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


def _fixture_errors(session) -> list[str]:
    """Review fix round 2, A-4/A-5 (finding 13): a session-scoped log of
    fixture staging failures (a failed import, a failed smoke-style write),
    so the report can tell "fixture never staged" apart from "verb broken"
    instead of the pair silently falling back to a guessed/raw path."""
    errs = getattr(session, "_parity_fixture_errors", None)
    if errs is None:
        errs = []
        session._parity_fixture_errors = errs
    return errs


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


def _stage_side(session, path, side: str):
    """PAR-hollow-pass 'not in library' cases (triage: 10 happy variants, 2
    P0 -- scout.stats, wb.kelvin): a per-side parity copy that nothing ever
    imported answers "not in library" (or an equivalent honest miss) on
    BOTH sides, so the verb never really runs even though the pair agrees.
    Import it into the library, under job `smoke-job`, via `library_import`
    -- never `darkroom_open` (LK-frame-switch-relaunch: darktable cannot
    load a second, differently-named raw into the darkroom while another is
    loaded, in the same running instance). Falls back to the raw copy when
    the import did not run or named nothing usable, and records the failure
    (review fix round 2, A-5): a downstream verb whose own fixture still
    needs the library then fails honestly on its own terms, never on a
    silently guessed path."""
    rec = session.call("library_import", {"source": str(path.parent),
                                          "job_slug": SMOKE_JOB_SLUG}, 30.0, write=True)
    resolved = _imported_path(session, rec.envelope, path.name)
    if resolved is None:
        err = ((rec.envelope or {}).get("error") or {}).get("code") or rec.error or "no_files_named"
        _fixture_errors(session).append(f"{side}_import_failed: {err}")
        return path
    return resolved


def _ensure_smoke_style(session, path) -> None:
    """PAR-hollow-pass 'fixture missing' cases (triage: 6 happy variants, 2
    P0 -- export.release, preset.apply_job -- need a real darktable style
    named 'smoke' to already exist). Once per session, ONLY on success
    (review fix round 2, A-4: a failed write must stay retryable and
    reported, never marked done): `style.extract` with `modules=[]`
    (SMOKE_ARGS's own style.extract args) writes an empty-history style of
    that name without needing the darkroom to show `path` (style.py
    extract's filtered path takes no `dtbridge.require_darkroom()`)."""
    if getattr(session, "_parity_smoke_style_done", False):
        return
    rec = session.call("style_extract", {"image": str(path), "name": SMOKE_STYLE_NAME,
                                         "modules": []}, 30.0, write=True)
    if (rec.envelope or {}).get("ok"):
        session._parity_smoke_style_done = True
        return
    err = ((rec.envelope or {}).get("error") or {}).get("code") or rec.error or "unknown"
    _fixture_errors(session).append(f"smoke_style_failed: {err}")


def _staged_dests(session):
    """Review fix round 2, A-1/A-2 (finding 4): import the pair ONCE per
    session, under DISTINCT per-side source names, and cache the two
    destinations on the session.

    Two bugs this closes together:
    - `library_import`'s resume identity is `(source_name, sha256)`
      (`scouter/core/library.py`). The old code copied both sides to the
      SAME file name (`hero.name`), so byte-identical copies resumed into
      the ONE dest -- both "sides" of every pair silently shared one
      library frame, and the CLI side ran on whatever the MCP side just
      wrote. Distinct names (`<stem>-mcp<ext>` / `<stem>-cli<ext>`) keep
      them two real, separate library frames even though the bytes match.
    - Importing per case was ~184 real bridge round trips per parity layer
      (2 x 92 cases). Importing once and caching removes that cost.

    Per-case isolation for the (now-shared-across-cases) destinations comes
    from `_restore_sidecars`, called on every lookup -- pure file I/O, so it
    is independent of which verb wrote to them last."""
    cached = getattr(session, "_parity_staged_dests", None)
    if cached is None:
        hero = session.staged.frames["hero"]
        stem, ext = hero.stem, hero.suffix
        mcp_src = copy_readonly(hero, session.root / "parity" / "mcp" / f"{stem}-mcp{ext}")
        cli_src = copy_readonly(hero, session.root / "parity" / "cli" / f"{stem}-cli{ext}")
        mcp_dest = _stage_side(session, mcp_src, "mcp")
        cli_dest = _stage_side(session, cli_src, "cli")
        _ensure_smoke_style(session, mcp_dest)
        cached = (mcp_dest, cli_dest)
        session._parity_staged_dests = cached
        session._parity_sidecar_baseline = {str(p): _read_sidecar_bytes(p) for p in cached}
    _restore_sidecars(session, cached)
    return cached


def _read_sidecar_bytes(path) -> bytes | None:
    from scouter.core import xmp

    sidecar = xmp.sidecar_path(path)
    return sidecar.read_bytes() if sidecar.exists() else None


def _restore_sidecars(session, dests) -> None:
    """Review fix round 2, A-1: per-case isolation for the once-per-session
    imported parity frames. Restores each dest's XMP sidecar to the
    byte-exact snapshot taken right after import, before this case's own
    writes -- so a style.extract/cull.apply/etc from an EARLIER case cannot
    leak into this one (the triage's own complaint: "the frame's sidecar
    accumulates all 92 cases' writes"). Pure file I/O: it isolates every
    write-capable verb, not only the ones `session.touched` tracks."""
    from scouter.core import xmp

    baseline = getattr(session, "_parity_sidecar_baseline", {})
    for p in dests:
        snap = baseline.get(str(p))
        sidecar = xmp.sidecar_path(p)
        if snap is None:
            sidecar.unlink(missing_ok=True)
        else:
            sidecar.write_bytes(snap)


def _call_with_frame_switch(session, obs, tool: str, args: dict, path, budget_s: float,
                            write: bool, side: str) -> CallRecord:
    """LK-frame-switch-relaunch (triage, PAR-hollow-pass note 3:
    'darkroom.open-happy ... fails on both sides with "wrong image loaded"
    ... known darkroom.open image-switch limit'): darktable cannot load a
    second, differently-named raw into the darkroom while another is
    loaded, in the SAME running instance. Retried once via a full relaunch
    (mirrors `ScouterSession._relaunch_then_reopen`) before the pair is
    scored, so a genuine open gets a real second chance rather than being
    scored on a harness artefact. Review fix round 2, A-3: the retry is now
    RECORDED (`obs.extra["frame_switch_relaunch"]`), so a pair where both
    sides needed it can be scored `needs_review` instead of a silent pass
    that proves nothing about actually switching frames."""
    rec = session.call(tool, args, budget_s, write=write)
    if rec.error == "not_in_darkroom":
        obs.extra.setdefault("frame_switch_relaunch", []).append(side)
        session.relaunch(path)
        rec = session.call(tool, args, budget_s, write=write)
    if rec.error is None and (rec.envelope or {}).get("ok"):
        session.current_frame = path
    return rec


def _run_cli(argv: list[str], env: dict, budget_s: float):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=budget_s + 30)


def _run_cli_with_frame_switch(session, obs, argv: list[str], path, budget_s: float):
    """The CLI side of `_call_with_frame_switch`: the CLI subprocess talks to
    the SAME running darktable through the shared `session.staged.env`, so it
    can hit the identical frame-switch limit. One retry, after a relaunch
    onto `path`, on the same terms as the MCP side; recorded the same way."""
    p = _run_cli(argv, session.staged.env, budget_s)
    try:
        env = json.loads(p.stdout)
    except ValueError:
        env = None
    if isinstance(env, dict) and (env.get("error") or {}).get("code") == "not_in_darkroom":
        obs.extra.setdefault("frame_switch_relaunch", []).append("cli")
        session.relaunch(path)
        p = _run_cli(argv, session.staged.env, budget_s)
    return p


def _parity_needs_review(variant: str | None, mcp_norm: dict, cli_norm: dict,
                         same: bool, transient_codes: frozenset[str]) -> str | None:
    """Pratik's ruling (row 496 item 5, review fix round 2 D-1): a HAPPY
    variant where both sides fail the same way never ran the verb -- always
    needs_review. An ERROR variant is the harness deliberately breaking the
    call so both sides SHOULD refuse identically; both refusing the SAME
    TYPED way is exactly the point of the error variant, and a genuine
    pass. It needs review only when a side ACCEPTED the bad input instead
    of refusing it (the case the triage called `PAR-error-variant-
    accepted`), or when the shared code is transient (a flaky call, not a
    typed refusal the verb chose)."""
    if not same:
        return None
    if variant == "happy":
        if not mcp_norm["ok"] and not cli_norm["ok"]:
            return "happy variant: both sides failed the same way -- the verb never ran"
        return None
    if variant == "error":
        if mcp_norm["ok"] or cli_norm["ok"]:
            return "error variant: a side accepted the bad input instead of refusing it"
        code = mcp_norm["code"]
        if code in transient_codes:
            return f"error variant: the shared code {code!r} is transient, not a typed refusal"
    return None


def parity_pair(session, case, step, obs) -> CallRecord:
    from tests.gauntlet.policy import POLICY as _policy

    verb_id = step.args["verb"]
    hero = session.staged.frames["hero"]
    is_open = verb_id == "darkroom.open"
    if is_open:
        stem, ext = hero.stem, hero.suffix
        mcp_copy = copy_readonly(hero, session.root / "parity" / "mcp" / f"{stem}-mcp{ext}")
        cli_copy = copy_readonly(hero, session.root / "parity" / "cli" / f"{stem}-cli{ext}")
    else:
        mcp_copy, cli_copy = _staged_dests(session)
    session.staged.frames["hero"] = mcp_copy
    mcp_args = substitute(step.args["args"], session, [])
    if is_open:
        mcp_rec = _call_with_frame_switch(session, obs, case.meta["mcp"], mcp_args, mcp_copy,
                                          step.budget_s, step.write, "mcp")
    else:
        mcp_rec = session.call(case.meta["mcp"], mcp_args, step.budget_s, write=step.write)
    session.staged.frames["hero"] = cli_copy
    cli_args = substitute(step.args["args"], session, [])
    session.staged.frames["hero"] = hero
    argv = [sys.executable, "-m", "scouter", *cli_argv(verb_id, cli_args)]
    try:
        if is_open:
            p = _run_cli_with_frame_switch(session, obs, argv, cli_copy, step.budget_s)
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
    mcp_norm = normalise(mcp_rec.envelope)
    cli_norm = normalise(cli_env)
    obs.extra["envelopes_match"] = not diffs
    obs.extra["parity_diffs"] = diffs
    obs.extra["cli_envelope"] = cli_env
    variant = case.meta.get("variant")
    reason = _parity_needs_review(variant, mcp_norm, cli_norm, not diffs,
                                  _policy.transient_codes)
    fsr = obs.extra.get("frame_switch_relaunch") or []
    if set(fsr) >= {"mcp", "cli"}:
        # Review fix round 2, A-3: both sides needing the relaunch proves
        # nothing about a genuine frame switch -- the second answer is
        # "open the image darktable already shows".
        reason = "LK-frame-switch-relaunch: both sides needed a relaunch to open the frame"
    obs.extra["parity_needs_review"] = reason
    fixture_errors = getattr(session, "_parity_fixture_errors", None)
    if fixture_errors:
        obs.extra["fixture_errors"] = list(fixture_errors)
    return CallRecord("parity_pair", step.args, {"ok": not diffs, "outcome": "ok",
                                                 "data": {"diffs": diffs}}, write=step.write)


VIRTUAL["parity_pair"] = parity_pair
