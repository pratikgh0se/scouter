"""Opt-in auto-heal (spec 6 item 4, watchdog design 2.3).

When a verb raises DarktableUnresponsive and `watchdog.auto_heal` is on, and
this job has heals left: quit the OWNED darktable (SIGTERM, then SIGKILL),
clear its stale locks, relaunch it with the same --library/--configdir,
wait for gui_ready (30 s), re-open the job's image, and retry the verb ONCE.
Only a RETRY-SAFE verb is retried (fix round 1, I1): a read-only verb (the
registry's `writes=False`) or an absolute setter in `RETRY_SAFE`. Every other
verb -- a nudge, say/adjust, style/preset/cull/export, `library.import`,
`job.run` -- could land twice, so it reports the heal (`healed: True`) and the
caller re-runs it."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from scouter import metrics, paths
from scouter.errors import DarktableUnresponsive, ScouterError

#: Kept for compatibility: the two verbs the design names as never retried.
#: Semantically "no retry" is now everything `retry_safe()` rejects.
NO_RETRY = frozenset({"library.import", "job.run"})
#: Writes that are absolute (setting them twice gives the same state), so a
#: retry after a heal is idempotent. Read-only verbs are retry-safe too.
RETRY_SAFE = frozenset({"darkroom.set", "darkroom.enable", "darkroom.reset", "darkroom.open"})
STATE_KEY = "watchdog_heals"


def retry_safe(verb_id: str) -> bool:
    """True for a verb whose retry cannot apply an edit twice. An unknown verb
    fails closed (no retry)."""
    if verb_id in NO_RETRY:
        return False
    if verb_id in RETRY_SAFE:
        return True
    from scouter import registry

    verb = registry.load_verbs().get(verb_id)
    return verb is not None and not verb.writes


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


def heal_darktable(job: str, budget_s: float, *, clock: Callable[[], float] = time.monotonic
                   ) -> dict:
    """Relaunch the owned darktable. Never touches a darktable Scouter did not launch.

    The whole heal is bounded by `budget_s` (fix round 1, I2): one deadline,
    and the gui_ready wait and the re-open get only what is left of it."""
    from scouter.core import dtbridge, dtprocess

    t0 = clock()
    deadline = t0 + budget_s

    def ms() -> int:
        return int(1000 * (clock() - t0))

    def failed(**extra) -> dict:
        return {"outcome": "heal_failed", "ms": ms(), "pid_old": pid, "pid_new": None, **extra}

    def over_budget(**extra) -> dict | None:
        if clock() < deadline:
            return None
        return {"outcome": "heal_failed", "ms": ms(), "pid_old": pid, "pid_new": None,
                "budget_exceeded": True, **extra}

    rec = dtprocess.read_owner()
    pid = dtprocess.owned_pid()
    if rec is None or pid is None:
        return {"outcome": "refused_not_ours", "ms": 0, "pid_old": None, "pid_new": None}
    term = dtprocess.terminate(pid, responsive=False)
    if term.get("how") == "refused_not_ours":
        return {"outcome": "refused_not_ours", "ms": 0, "pid_old": pid, "pid_new": None}
    if not term.get("ok"):
        # Still alive: a second darktable on the same library would fight it.
        return failed(terminate=term)
    late = over_budget()
    if late:
        return late
    try:
        dtprocess.clear_stale_locks(rec.library)
    except OSError as e:
        return failed(error=f"clear_stale_locks: {str(e)[:200]}")
    try:
        new = dtprocess.launch(rec.frame, rec.library, rec.configdir)
    except (dtprocess.LaunchError, OSError) as e:
        return failed(error=str(e)[:200])
    out = {"pid_old": pid, "pid_new": new.pid}
    late = over_budget(**out)
    if late:
        return late
    ready = dtprocess.wait_gui_ready(min(30.0, max(0.0, deadline - clock())))
    if not ready.get("ready"):
        return over_budget(**out) or {"outcome": "heal_failed", "ms": ms(), **out}
    opened = paths.job_state_read(job).get("opened_path")
    if opened:
        late = over_budget(**out)
        if late:
            return late
        try:
            dtbridge.call("darkroom.open", {"path": opened},
                          timeout=min(30.0, max(0.0, deadline - clock())))
        except Exception as e:  # noqa: BLE001 - a retry on an unopened image is unsafe
            return (over_budget(**out, error=f"re-open {opened}: {str(e)[:200]}")
                    or {"outcome": "heal_failed", "ms": ms(), **out,
                        "error": f"re-open {opened}: {str(e)[:200]}"})
    return {"outcome": "healed", "ms": ms(), **out}


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
        if not retry_safe(verb_id):
            raise DarktableUnresponsive(
                f"darktable was healed (pid {result['pid_old']} -> {result['pid_new']}); "
                f"{verb_id} is not retried automatically (it could apply twice): re-run it",
                detail={**e.detail, "healed": True, "heals_used": used + 1},
            ) from e
    # The retry runs outside the except block so its own failure is not
    # chained to the first one; it is still reported as healed (I4).
    try:
        return fn(), True
    except ScouterError as e2:
        e2.detail.update(healed=True, heals_used=used + 1,
                         heals_max=cfg.max_heals_per_job)
        raise
