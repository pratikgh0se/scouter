"""Shared builders for the Scouter layer generators. Pure (reads vocab/phrases
tables, never calls darktable)."""

from __future__ import annotations

from gauntlet.case import Check, Step
from tests.gauntlet.checkmap import PartCheck

BODIES = ("olympus", "sony_a7")
#: Every MCP tool that can change the sidecar, the library or the disk.
WRITE_TOOLS = frozenset({
    "darkroom_set", "darkroom_nudge", "darkroom_enable", "darkroom_reset", "darkroom_undo_last",
    "darkroom_adjust", "darkroom_say", "history_commit", "style_extract", "style_apply",
    "style_apply_set", "export_run", "export_release", "library_import", "library_register",
    "library_relocate", "cull_apply", "config_init", "backup_run", "wb_calibrate",
    "preset_save", "preset_apply", "preset_apply_job", "job_run",
})
#: Spec 5.3 budgets (s): single move 3.5 (row 427), commit 12, look plan 40.
BUDGET_MOVE = 3.5
BUDGET_SAY = 8.0
BUDGET_COMMIT = 12.0
UNITS = {"EV": "EV", "K": "K", "%": "%", "°": "°", "ratio": "ratio", "—": "ratio"}


def frame(role: str) -> str:
    return "{frame:" + role + "}"


def step(tool: str, budget_s: float = BUDGET_MOVE, **args) -> Step:
    return Step(tool, dict(args), budget_s, tool in WRITE_TOOLS)


def key(module: str, param: str) -> str:
    return f"{module}.{param}"


def m1_equals(module: str, param: str, value: float, unit: str) -> Check:
    return Check("M1", kind="equals",
                 target={"key": key(module, param), "value": value, "unit": UNITS[unit]})


def m1_delta(module: str, param: str, delta: float, unit: str, step_size: float) -> Check:
    return Check("M1", kind="delta", target={"key": key(module, param), "delta": delta,
                                             "unit": UNITS[unit], "tol": 0.1 * step_size,
                                             "step": step_size})


def m1_restored(keys: list[str], units: dict[str, str], from_step: int = 0) -> Check:
    """Params after the last step equal snapshot `from_step` (0 = before the case)."""
    return Check("M1", kind="restored", target={"keys": keys, "units": units,
                                                "from": from_step})


def clamp_reported() -> Check:
    return Check("envelope", target={"path": "data.clamped", "op": "truthy"},
                 tags={"silent_if_fail": True, "why": "value clamped without saying so"})


def stat_check(p: PartCheck) -> Check:
    """A check_map concept -> an engine check. A concept whose metric is M1
    names a decoded param in `stat` that must be on (a module toggle, e.g.
    `lens.__enabled` once lens correction is in the vocabulary)."""
    if p.metric == "M1":
        return Check("M1", kind="equals", target={"key": p.stat, "value": 1, "unit": "bool"})
    return Check(p.metric, stat=p.stat, region=p.region, sign=p.sign,
                 kind="unchanged" if p.sign == 0 else "delta")


def sidecar(module: str | None, param: str | None, unit: str | None, *, changed: bool = True,
            allowed: list[str] | None = None) -> dict:
    d: dict = {"changed": changed}
    if module and param:
        d["key"] = key(module, param)
        d["units"] = {key(module, param): UNITS.get(unit or "ratio", "ratio")}
        d["allowed"] = allowed or []
    return d
