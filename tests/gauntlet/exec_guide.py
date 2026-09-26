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
        p = subprocess.run([sys.executable, "-m", "scouter", *argv], env=session.staged.env,
                           capture_output=True, text=True, timeout=step.budget_s + 30,
                           cwd=str(session.root))
    except subprocess.TimeoutExpired:
        obs.blocked = "hang: scouter cli exceeded budget"
        obs.hang_evidence.append(f"cli {' '.join(argv)}: no answer in "
                                 f"{step.budget_s + 30:.0f}s")
        return CallRecord("cli", {"argv": argv}, None, error="timeout", write=True)
    try:
        env = json.loads(p.stdout)
    except ValueError:
        env = {"ok": p.returncode == 0, "outcome": "ok" if p.returncode == 0 else
               f"exit {p.returncode}", "data": {"stdout": p.stdout[-2000:]}}
    obs.extra["exit"] = p.returncode
    obs.extra["stdout"] = p.stdout[-4000:]
    return CallRecord("cli", {"argv": argv}, env, write=True)


VIRTUAL["cli"] = cli
