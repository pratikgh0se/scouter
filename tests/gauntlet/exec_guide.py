"""Layer 8 executor: `cli` virtual tool (a documented command run literally,
placeholders filled with the session's staged copies)."""

from __future__ import annotations

import json
import subprocess
import sys

from gauntlet.adapter import CallRecord
from tests.gauntlet.execute import VIRTUAL, substitute


def cli(session, case, step, obs) -> CallRecord:
    argv = substitute(list(step.args["argv"]), session, obs.calls)
    try:
        p = subprocess.run(
            [sys.executable, "-m", "scouter", *argv],
            env=session.staged.env,
            capture_output=True,
            text=True,
            timeout=step.budget_s + 30,
            cwd=str(session.root),
        )
    except subprocess.TimeoutExpired:
        obs.blocked = "hang: scouter cli exceeded budget"
        obs.hang_evidence.append(
            f"cli {' '.join(argv)}: no answer in " f"{step.budget_s + 30:.0f}s"
        )
        return CallRecord("cli", {"argv": argv}, None, error="timeout", write=True)
    try:
        env = json.loads(p.stdout)
    except ValueError:
        env = {
            "ok": p.returncode == 0,
            "outcome": "ok" if p.returncode == 0 else f"exit {p.returncode}",
            "data": {"stdout": p.stdout[-2000:]},
        }
    obs.extra["exit"] = p.returncode
    obs.extra["stdout"] = p.stdout[-4000:]
    return CallRecord("cli", {"argv": argv}, env, write=True)


def reset_explained(session, case, step, obs) -> CallRecord:
    """Round 1 C18 (gd-edu-full-*): precondition step. `--educative full`
    explains a concept once per job (`explain._new_concepts` reads
    `explained_concepts` from the job's state file), so a case's expected
    paragraph count depends on whatever ran before it in the session. This
    clears ONLY that one key in the session's own scratch state file (never
    the image/watermark keys), so each case starts from a known "nothing
    explained yet" state and no longer depends on case order or on a
    selective re-run."""
    job = session.staged.env.get("SCOUTER_JOB")
    path = session.staged.cache / "state" / f"{job}.json" if job else None
    cleared: list = []
    if path is not None and path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
        cleared = list(state.pop("explained_concepts", []))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    obs.extra.setdefault("explained_reset", []).append(cleared)
    return CallRecord(
        "reset_explained",
        {},
        {"ok": True, "outcome": "ok", "data": {"cleared": cleared}},
        write=False,
    )


VIRTUAL["cli"] = cli
VIRTUAL["reset_explained"] = reset_explained
