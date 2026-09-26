"""Opt-in auto-heal (spec 6 item 4, watchdog design 2.3).

When a verb raises DarktableUnresponsive and `watchdog.auto_heal` is on, and
this job has heals left: quit the OWNED darktable (SIGTERM, then SIGKILL),
clear its stale locks, relaunch it with the same --library/--configdir,
wait for gui_ready (30 s), re-open the job's image, and retry the verb ONCE.
The retry is idempotent through the journal readback. `library.import` and
`job.run` are never retried: they report the heal and the caller re-runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from scouter import metrics, paths
from scouter.errors import DarktableUnresponsive

NO_RETRY = frozenset({"library.import", "job.run"})
STATE_KEY = "watchdog_heals"


def heals_used(job: str) -> int:
    return int(paths.job_state_read(job).get(STATE_KEY, 0))


def _bump(job: str) -> None:
    def mutate(state: dict) -> None:
        state[STATE_KEY] = int(state.get(STATE_KEY, 0)) + 1
        state["watchdog_last_heal"] = metrics._utc_now_z()

    paths.job_state_update(job, mutate)


def telemetry(job: str, verb: str, outcome: str, ms: int, detail: dict) -> None:
    from scouter import PHASE, __version__

    event = {"ts": metrics._utc_now_z(), "job": job, "verb": verb, "layer": "watchdog",
             "args_digest": metrics.args_digest(detail), "images": [], "ms": int(ms),
             "outcome": outcome, "bytes_in": 0, "bytes_out": 0, "est_tokens": 0,
             "phase": PHASE, "version": __version__}
    metrics.emit(event)
    metrics.audit(job, {**event, "detail": detail})


def heal_darktable(job: str, budget_s: float) -> dict:
    """Relaunch the owned darktable. Never touches a darktable Scouter did not launch."""
    from scouter.core import dtbridge, dtprocess

    t0 = time.monotonic()
    rec = dtprocess.read_owner()
    pid = dtprocess.owned_pid()
    if rec is None or pid is None:
        return {"outcome": "refused_not_ours", "ms": 0, "pid_old": None, "pid_new": None}
    term = dtprocess.terminate(pid, responsive=False)
    if term.get("how") == "refused_not_ours":
        return {"outcome": "refused_not_ours", "ms": 0, "pid_old": pid, "pid_new": None}
    if not term.get("ok"):
        # Still alive: a second darktable on the same library would fight it.
        return {"outcome": "heal_failed", "ms": int(1000 * (time.monotonic() - t0)),
                "pid_old": pid, "pid_new": None, "terminate": term}
    dtprocess.clear_stale_locks(rec.library)
    try:
        new = dtprocess.launch(rec.frame, rec.library, rec.configdir)
    except (dtprocess.LaunchError, OSError) as e:
        return {"outcome": "heal_failed", "ms": int(1000 * (time.monotonic() - t0)),
                "pid_old": pid, "pid_new": None, "error": str(e)[:200]}
    ready = dtprocess.wait_gui_ready(min(30.0, budget_s))
    out = {"pid_old": pid, "pid_new": new.pid}
    if not ready.get("ready"):
        return {"outcome": "heal_failed", "ms": int(1000 * (time.monotonic() - t0)), **out}
    opened = paths.job_state_read(job).get("opened_path")
    if opened:
        try:
            dtbridge.call("darkroom.open", {"path": opened}, timeout=30.0)
        except Exception as e:  # noqa: BLE001 - a retry on an unopened image is unsafe
            return {"outcome": "heal_failed", "ms": int(1000 * (time.monotonic() - t0)),
                    **out, "error": f"re-open {opened}: {str(e)[:200]}"}
    return {"outcome": "healed", "ms": int(1000 * (time.monotonic() - t0)), **out}


def run_with_heal(verb_id: str, fn: Callable[[], Any], job: str, *, cfg=None,
                  heal: Callable[[str, float], dict] = heal_darktable) -> tuple[Any, bool]:
    """(result, healed). Raises DarktableUnresponsive when no heal applies."""
    try:
        return fn(), False
    except DarktableUnresponsive as e:
        if cfg is None:
            from scouter import config

            try:
                cfg = config.load().watchdog
            except Exception:  # noqa: BLE001 - a broken config never enables a heal
                raise e from None
        used = heals_used(job)
        e.detail.update(heals_used=used, heals_max=cfg.max_heals_per_job)
        if not cfg.auto_heal or used >= cfg.max_heals_per_job:
            raise
        result = heal(job, cfg.heal_budget_s)
        telemetry(job, "watchdog.heal", result["outcome"], result.get("ms", 0), result)
        if result["outcome"] == "heal_failed":
            _bump(job)  # an attempted heal spends the budget: no relaunch loops
        if result["outcome"] != "healed":
            raise
        _bump(job)
        if verb_id in NO_RETRY:
            raise DarktableUnresponsive(
                f"darktable was healed (pid {result['pid_old']} -> {result['pid_new']}); "
                f"{verb_id} is never retried automatically: re-run it",
                detail={**e.detail, "healed": True, "heals_used": used + 1},
            ) from e
        return fn(), True
