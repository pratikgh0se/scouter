#!/usr/bin/env python3
"""The Scouter editor benchmark runner (decision row 406). Standard library only.

It drives Scouter the way Claude would: it spawns the `scouter-mcp` server
(`python -m scouter.mcp.server`, the README's MCP entry point), speaks MCP
over stdio, and for every task in `editor_tasks.json` sends the tool calls a
competent agent would choose for the editor's sentence (the task's `plan`).
Every call and every envelope is captured. The outcome is read back
deterministically -- from the XMP sidecar darktable itself wrote, from the
tool envelopes, or from files on disk -- and scored pass / partial / fail by
the task's own rule. Nothing here runs a model.

Two modes:

- live (`--live`): stages scratch copies of the frames under
  `~/Pictures/ScouterScratch/bench-runs/<run>/`, launches the real darktable
  once per session (`darktable --library <bench-library> <frame>`, the way
  the host tests launch it, because darktable can only edit the frame it was
  launched with -- README honest limit R20), runs the session's tasks, and
  always quits darktable. The EditingTrial originals are only ever READ.
- fake: the unit tests inject a fake MCP client, a fake darktable host and a
  fake snapshotter (`tests/test_editor_benchmark.py`); nothing real is touched.

Outputs `results/<timestamp>-<body>.json` and `results/<timestamp>-<body>.md`.
`--compare OLD.json NEW.json` prints the per-task delta, so the same benchmark
re-run after phase 3 shows what changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import secrets
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TASKS_PATH = Path(__file__).with_name("editor_tasks.json")
RESULTS_DIR = Path(__file__).with_name("results")
DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
EDITING_TRIAL = Path("/Users/pratik/Pictures/EditingTrial")
SCRATCH = Path.home() / "Pictures" / "ScouterScratch"

TASKS_SCHEMA = "scouter-editor-tasks/1"
RESULT_SCHEMA = "scouter-editor-benchmark-result/1"
SCORES = ("pass", "partial", "fail")
OUTCOMES = (
    "verified",
    "correct_refusal",
    "over_budget",
    "partial",
    "honest_refusal",
    "reported_ok_unverified",
    "blocked",
    "failed",
)
CATEGORIES = (
    "exposure",
    "white_balance",
    "contrast_tone",
    "colour",
    "lens",
    "finishing",
    "workflow",
    "unreachable",
)
ASSERTION_TYPES = (
    "step_ok",
    "steps_ok",
    "step_error",
    "step_field",
    "param_delta",
    "param_direction",
    "param_restored",
    "slot_value",
    "wb_ratio",
    "wb_green",
    "wb_changed",
    "module_active",
    "history_grew",
    "history_unchanged",
    "tools_absent",
    "file_from_step",
    "export_file",
    "stats_delta",
)
#: Operations whose decoded state is kept in the result JSON's snapshots.
SNAPSHOT_OPS = (
    "exposure",
    "sigmoid",
    "temperature",
    "colorbalancergb",
    "colorzones",
    "lens",
    "sharpen",
    "denoiseprofile",
)
DEFAULT_CALL_TIMEOUT_S = 60.0
PLAN_VERSIONS = ("v1", "v2")
#: What a plan_v2 `assertion_overrides` entry may change: only WHERE a fact is
#: read (which call, which envelope field, where an expected value comes from),
#: never a threshold, tolerance, delta, frame or scoring group.
OVERRIDE_KEYS = ("step", "steps", "before_step", "after_step", "path", "value", "op")
OVERRIDE_OPS = ("set_eq",)
_STEP_KEYS = ("step", "before_step", "after_step")

# ---------------------------------------------------------------------------
# The task file
# ---------------------------------------------------------------------------


def load_tasks(path: Path = TASKS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_tasks(doc: dict) -> list[str]:
    """Every structural rule the runner relies on. Returns the problems found."""
    errors: list[str] = []
    if doc.get("schema") != TASKS_SCHEMA:
        errors.append(f"schema must be {TASKS_SCHEMA!r}")
    tasks = doc.get("tasks") or []
    ids = [t.get("id") for t in tasks]
    if len(ids) != len(set(ids)):
        errors.append("task ids are not unique")
    if not 24 <= len(tasks) <= 30:
        errors.append(f"expected 24-30 tasks, found {len(tasks)}")
    frames = doc.get("frames") or {}
    for body, spec in frames.items():
        for role in ("dark", "bright", "hero", "job"):
            if not spec.get(role):
                errors.append(f"frames.{body}.{role} missing")
        if len(spec.get("job") or []) < 6:
            errors.append(f"frames.{body}.job needs at least 6 frames")
    scheduled: list[str] = []
    for session in doc.get("sessions") or []:
        scheduled.extend(session.get("tasks") or [])
    if sorted(scheduled) != sorted(ids):
        errors.append("every task must be scheduled in exactly one session")
    unreachable = 0
    for t in tasks:
        tid = t.get("id")
        if t.get("category") not in CATEGORIES:
            errors.append(f"{tid}: unknown category {t.get('category')!r}")
        if t.get("category") == "unreachable":
            unreachable += 1
            if t.get("plan"):
                errors.append(
                    f"{tid}: an unreachable task's plan must be empty (the agent refuses)"
                )
        for key in ("sentence", "frames", "budget_s", "plan", "assertions", "scoring"):
            if key not in t:
                errors.append(f"{tid}: missing {key}")
        assertions = t.get("assertions") or {}
        for aid, a in assertions.items():
            if a.get("type") not in ASSERTION_TYPES:
                errors.append(f"{tid}.{aid}: unknown assertion type {a.get('type')!r}")
            for key in ("step",):
                if key in a and a[key] >= len(t.get("plan") or []):
                    errors.append(f"{tid}.{aid}: step {a[key]} is outside the plan")
        scoring = t.get("scoring") or {}
        if not scoring.get("pass"):
            errors.append(f"{tid}: scoring.pass is empty")
        if "partial_criteria" not in scoring:
            errors.append(f"{tid}: scoring.partial_criteria must spell out the partial rule")
        refs = list(scoring.get("pass") or []) + list(scoring.get("refusal") or [])
        for group in scoring.get("partial") or []:
            refs.extend(group)
        for ref in refs:
            if ref not in assertions:
                errors.append(f"{tid}: scoring names unknown assertion {ref!r}")
        for step in list(t.get("plan") or []) + list(t.get("context") or []):
            if not step.get("tool"):
                errors.append(f"{tid}: a plan step has no tool")
    if unreachable != 2:
        errors.append(f"expected exactly 2 unreachable tasks, found {unreachable}")
    for t in tasks:
        errors.extend(_validate_plan_v2(t))
    return errors


def _assertion_steps(a: dict) -> list[int]:
    out = [a[k] for k in _STEP_KEYS if k in a]
    out.extend(a.get("steps") or [])
    v = a.get("value")
    if isinstance(v, dict) and "$arg" in v:
        out.append(v["$arg"]["step"])
    return out


def _validate_plan_v2(t: dict) -> list[str]:
    """plan_v2 is optional. It may only re-address assertions (see OVERRIDE_KEYS)."""
    tid, v2 = t.get("id"), t.get("plan_v2")
    if v2 is None:
        return []
    errors: list[str] = []
    if not v2.get("why"):
        errors.append(f"{tid}: plan_v2 must say why")
    if v2.get("same_as_v1"):
        if any(k in v2 for k in ("steps", "context", "step_map", "assertion_overrides")):
            errors.append(f"{tid}: plan_v2 same_as_v1 cannot also carry steps")
        return errors
    steps = v2.get("steps")
    if not isinstance(steps, list):
        return errors + [f"{tid}: plan_v2 needs steps (or same_as_v1)"]
    if t.get("category") == "unreachable" and steps:
        errors.append(f"{tid}: an unreachable task's plan_v2 must be empty")
    for step in steps + list(v2.get("context") or []):
        if not step.get("tool"):
            errors.append(f"{tid}: a plan_v2 step has no tool")
    smap = v2.get("step_map")
    v1_len = len(t.get("plan") or [])
    if smap is not None:
        for k, v in smap.items():
            if not (k.isdigit() and int(k) < v1_len):
                errors.append(f"{tid}: plan_v2.step_map key {k!r} is not a plan v1 step")
            if not (isinstance(v, int) and 0 <= v < len(steps)):
                errors.append(f"{tid}: plan_v2.step_map value {v!r} is not a plan v2 step")
    overrides = v2.get("assertion_overrides") or {}
    for aid, o in overrides.items():
        if aid not in (t.get("assertions") or {}):
            errors.append(f"{tid}: plan_v2 overrides unknown assertion {aid!r}")
        bad = sorted(set(o) - set(OVERRIDE_KEYS))
        if bad:
            errors.append(f"{tid}.{aid}: plan_v2 may not override {bad} (thresholds are fixed)")
        if "op" in o and o["op"] not in OVERRIDE_OPS:
            errors.append(f"{tid}.{aid}: plan_v2 op override must be one of {OVERRIDE_OPS}")
    for aid, a in (t.get("assertions") or {}).items():
        if aid in overrides and any(k in overrides[aid] for k in (*_STEP_KEYS, "steps")):
            continue
        for s in _assertion_steps(a):
            if isinstance(a.get("value"), dict) and "value" in overrides.get(aid, {}):
                if s == (a["value"].get("$arg") or {}).get("step"):
                    continue
            mapped = s if smap is None else smap.get(str(s))
            if mapped is None or mapped >= len(steps):
                errors.append(
                    f"{tid}.{aid}: plan v1 step {s} has no plan v2 equivalent (add it to step_map)"
                )
    return errors


def _remap(a: dict, smap: dict | None) -> dict:
    if not smap:
        return dict(a)
    out = dict(a)
    for k in _STEP_KEYS:
        if k in out:
            out[k] = smap.get(str(out[k]), out[k])
    if "steps" in out:
        out["steps"] = [smap.get(str(s), s) for s in out["steps"]]
    v = out.get("value")
    if isinstance(v, dict) and "$arg" in v:
        arg = dict(v["$arg"])
        arg["step"] = smap.get(str(arg["step"]), arg["step"])
        out["value"] = {"$arg": arg}
    return out


def effective_task(task: dict, plan_version: str) -> dict:
    """The task as run under `plan_version`: plan v1 is the task itself; plan v2
    swaps in plan_v2's steps/context and re-addresses the assertions (step_map,
    assertion_overrides). Assertion thresholds and scoring are never touched."""
    if plan_version not in PLAN_VERSIONS:
        raise ValueError(f"unknown plan version {plan_version!r}")
    v2 = task.get("plan_v2")
    if plan_version == "v1" or not v2 or v2.get("same_as_v1"):
        used = "v1" if plan_version == "v1" else "v2=v1"
        return dict(task) | {"plan_used": used}
    smap = v2.get("step_map")
    overrides = v2.get("assertion_overrides") or {}
    assertions = {
        aid: _remap(a, smap) | (overrides.get(aid) or {})
        for aid, a in (task.get("assertions") or {}).items()
    }
    out = dict(task) | {"plan": v2["steps"], "assertions": assertions, "plan_used": "v2"}
    if "context" in v2:
        out["context"] = v2["context"]
    return out


def tasks_by_id(doc: dict) -> dict[str, dict]:
    return {t["id"]: t for t in doc["tasks"]}


# ---------------------------------------------------------------------------
# Templates and value resolvers
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([a-z_]+)(?::([a-z0-9_]+))?\}")


def render(obj: Any, ctx: dict) -> Any:
    """Replace `{key}` / `{key:arg}` placeholders in every string of `obj`.

    `ctx` maps `frame:<role>`, `job:<n>`, `jobname:<n>`, `framename:<role>`,
    `framebase:<role>` and plain keys (`preset`, `job_slug`, `card`,
    `out_dir`, `run_id`, `body`) to strings. An unknown placeholder is an
    error, never left in place: a literal `{frame:hero}` reaching Scouter
    would be a benchmark bug scored as a Scouter failure.
    """
    if isinstance(obj, str):

        def sub(m: re.Match) -> str:
            key = m.group(1) if m.group(2) is None else f"{m.group(1)}:{m.group(2)}"
            if key not in ctx:
                raise KeyError(f"unknown placeholder {{{key}}}")
            return str(ctx[key])

        return _PLACEHOLDER.sub(sub, obj)
    if isinstance(obj, list):
        return [render(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: render(v, ctx) for k, v in obj.items()}
    return obj


def dig(obj: Any, dotted: str) -> Any:
    """`dig(env, "data.stats.clipped_high_pct")`; None when any hop is missing."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def hist_value(envelope: dict | None, op: str, param: str) -> float | None:
    """What an agent reads off a `history_read` envelope: the newest ACTIVE
    entry of `op` (instance 0) whose decoded params name `param`."""
    entries = dig(envelope, "data.entries") or []
    best = None
    for e in entries:
        if e.get("operation") != op or not e.get("active") or e.get("multi_priority", 0) != 0:
            continue
        decoded = e.get("decoded") or {}
        value = decoded.get(param)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if best is None or e.get("num", 0) > best[0]:
            best = (e.get("num", 0), float(value))
    return None if best is None else best[1]


def resolve_value(value: Any, steps: list[dict]) -> tuple[Any, dict | None]:
    """Resolve `$hist` / `$ref` against earlier steps' envelopes.

    Returns (value, provenance). Provenance says whether the number came from
    a tool result or from the plan's documented fallback, so the report can
    tell "the agent read 0.7 and added 1.0" from "the agent had to guess".
    """
    if isinstance(value, dict) and "$hist" in value:
        spec = value["$hist"]
        env = steps[spec["step"]]["envelope"] if spec["step"] < len(steps) else None
        read = hist_value(env, spec["op"], spec["param"])
        if read is None:
            return float(spec["fallback"]), {"source": "fallback", "read": None}
        return round(read + float(spec.get("add", 0.0)), 6), {
            "source": "history_read",
            "read": read,
        }
    if isinstance(value, dict) and "$ref" in value:
        spec = value["$ref"]
        env = steps[spec["step"]]["envelope"] if spec["step"] < len(steps) else None
        read = dig(env, spec["path"])
        if not isinstance(read, (int, float)) or isinstance(read, bool):
            return float(spec["fallback"]), {"source": "fallback", "read": read}
        return round(float(read) + float(spec.get("add", 0.0)), 6), {
            "source": spec["path"],
            "read": read,
        }
    if isinstance(value, dict):
        out: dict = {}
        prov: dict = {}
        for k, v in value.items():
            out[k], p = resolve_value(v, steps)
            if p:
                prov[k] = p
        return out, (prov or None)
    return value, None


# ---------------------------------------------------------------------------
# XMP snapshots (what darktable itself wrote)
# ---------------------------------------------------------------------------


def _json_num(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def snapshot_xmp(raw_path: str | Path) -> dict:
    """Read one frame's sidecar into the plain dict the assertions consume.

    `modules[op]` is the newest ACTIVE instance-0 entry of that operation
    (darktable's own "what is live" rule), with its decoded params when the
    repo's pinned layout decodes it. The fingerprint covers every entry, so
    `history_unchanged` notices any write, not just the ops kept here.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from scouter.core import xmp  # the same decoder Scouter uses; pure stdlib

    sidecar = xmp.sidecar_path(raw_path)
    if not sidecar.exists():
        return {"exists": False, "history_end": 0, "entries": 0, "fingerprint": "", "modules": {}}
    parsed = xmp.parse(sidecar.read_text(encoding="utf-8", errors="replace"))
    h = hashlib.sha256()
    for e in parsed.entries:
        h.update(f"{e.num}|{e.operation}|{e.enabled}|{e.multi_priority}|{e.params_hex}\n".encode())
    h.update(f"end={parsed.history_end}".encode())
    modules: dict[str, dict] = {}
    for e in sorted(parsed.entries, key=lambda x: x.num, reverse=True):
        if not e.active or e.multi_priority != 0 or e.operation in modules:
            continue
        decoded = None
        if e.decoded is not None:
            decoded = {k: _json_num(v) for k, v in e.decoded.items() if k != "named"}
        modules[e.operation] = {"num": e.num, "enabled": bool(e.enabled), "decoded": decoded}
    return {
        "exists": True,
        "history_end": parsed.history_end,
        "entries": len(parsed.entries),
        "fingerprint": h.hexdigest()[:16],
        "modules": modules,
    }


def compact_snapshot(snap: dict) -> dict:
    """What goes into the result JSON: the kept ops only."""
    return {
        "exists": snap.get("exists"),
        "history_end": snap.get("history_end"),
        "entries": snap.get("entries"),
        "fingerprint": snap.get("fingerprint"),
        "modules": {k: v for k, v in (snap.get("modules") or {}).items() if k in SNAPSHOT_OPS},
    }


def _param(snap: dict | None, op: str, param: str) -> float | None:
    decoded = (((snap or {}).get("modules") or {}).get(op) or {}).get("decoded") or {}
    v = decoded.get(param)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _coeffs(snap: dict | None) -> tuple[float, float, float] | None:
    r, g, b = (_param(snap, "temperature", k) for k in ("coeff_r", "coeff_g", "coeff_b"))
    if None in (r, g, b) or not b or not r:
        return None
    return r, g, b  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Assertions and scoring (pure; unit-tested)
# ---------------------------------------------------------------------------


def _jpeg_long_edge(path: Path) -> int | None:
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (seg_len,) = struct.unpack_from(">H", data, i + 2)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            _p, height, width = struct.unpack_from(">BHH", data, i + 4)
            return max(height, width)
        i += 2 + seg_len
    return None


def _env(ctx: dict, step: int) -> dict | None:
    steps = ctx.get("steps") or []
    return steps[step].get("envelope") if step < len(steps) else None


def _expected(value: Any, ctx: dict) -> Any:
    if isinstance(value, dict) and "$env" in value:
        spec = value["$env"]
        return dig(_env(ctx, spec["step"]), spec["path"])
    if isinstance(value, dict) and "$arg" in value:
        spec = value["$arg"]
        steps = ctx.get("steps") or []
        if spec["step"] >= len(steps):
            return None
        return (steps[spec["step"]].get("args") or {}).get(spec["arg"])
    return value


def evaluate(a: dict, ctx: dict) -> dict:
    """One assertion -> {ok, measured, expected}. Never raises on missing data:
    a missing step, sidecar or field is a plain `ok: False` with the reason."""
    t = a["type"]
    before, after, pre = ctx.get("before") or {}, ctx.get("after") or {}, ctx.get("pre") or {}
    try:
        if t == "step_ok":
            env = _env(ctx, a["step"])
            ok = bool(env) and env.get("ok") is True and env.get("outcome") == "ok"
            return {"ok": ok, "measured": None if not env else env.get("outcome")}
        if t == "steps_ok":
            outs = [(_env(ctx, s) or {}).get("outcome") for s in a["steps"]]
            oks = [(_env(ctx, s) or {}).get("ok") is True for s in a["steps"]]
            return {"ok": all(oks) and all(o == "ok" for o in outs), "measured": outs}
        if t == "step_error":
            env = _env(ctx, a["step"])
            if not env:
                return {"ok": False, "measured": "no envelope (step not run or transport failure)"}
            code = (env.get("error") or {}).get("code")
            codes = a.get("codes") or []
            ok = env.get("ok") is False and code is not None and (not codes or code in codes)
            return {"ok": ok, "measured": code, "expected": codes or "any typed error"}
        if t == "step_field":
            got = dig(_env(ctx, a["step"]), a["path"])
            want = _expected(a.get("value"), ctx)
            op = a["op"]
            if op == "eq":
                ok = got == want
            elif op == "abs_within":
                ok = (
                    isinstance(got, (int, float))
                    and isinstance(want, (int, float))
                    and abs(float(got) - float(want)) <= float(a["tol"])
                )
            elif op == "contains":
                ok = isinstance(got, str) and str(want).lower() in got.lower()
            elif op == "endswith":
                ok = isinstance(got, str) and got.endswith(str(want))
            elif op == "not_null":
                ok = got is not None
            elif op == "is_null":
                ok = got is None
            elif op == "set_eq":
                ok = isinstance(got, list) and sorted(map(str, got)) == sorted(map(str, want))
            else:
                raise ValueError(f"unknown step_field op {op!r}")
            return {"ok": bool(ok), "measured": got, "expected": want}
        if t in ("param_delta", "param_direction"):
            b = _param(before.get(a["frame"]), a["module"], a["param"])
            if b is None:
                b = a.get("before_default")
            v = _param(after.get(a["frame"]), a["module"], a["param"])
            if b is None or v is None:
                return {
                    "ok": False,
                    "measured": {"before": b, "after": v},
                    "note": "value not readable",
                }
            delta = round(v - b, 6)
            if t == "param_delta":
                ok = abs(delta - float(a["delta"])) <= float(a["tol"])
                return {"ok": ok, "measured": delta, "expected": a["delta"]}
            return {
                "ok": a["sign"] * delta > 1e-6,
                "measured": delta,
                "expected": f"sign {a['sign']}",
            }
        if t == "param_restored":
            p = _param(pre.get(a["frame"]), a["module"], a["param"])
            v = _param(after.get(a["frame"]), a["module"], a["param"])
            if p is None or v is None:
                return {
                    "ok": False,
                    "measured": {"pre": p, "after": v},
                    "note": "value not readable",
                }
            return {"ok": abs(v - p) <= float(a["tol"]), "measured": v, "expected": p}
        if t == "slot_value":
            mod = ((after.get(a["frame"]) or {}).get("modules") or {}).get(a["module"]) or {}
            decoded = mod.get("decoded") or {}
            hits = [
                k
                for k, v in decoded.items()
                if isinstance(v, (int, float))
                and not isinstance(v, bool)
                and abs(float(v) - float(a["value"])) <= float(a["tol"])
            ]
            return {"ok": bool(hits), "measured": hits, "expected": a["value"]}
        if t in ("wb_ratio", "wb_green", "wb_changed"):
            cb, ca = _coeffs(before.get(a["frame"])), _coeffs(after.get(a["frame"]))
            if cb is None or ca is None:
                return {
                    "ok": False,
                    "measured": {"before": cb, "after": ca},
                    "note": "coefficients unreadable",
                }
            if t == "wb_changed":
                rels = [abs(x / y - 1.0) for x, y in zip(ca, cb) if y]
                return {"ok": max(rels) >= float(a["min_rel"]), "measured": round(max(rels), 5)}
            if t == "wb_ratio":
                rb, ra = cb[0] / cb[2], ca[0] / ca[2]
            else:
                rb, ra = cb[1] / math.sqrt(cb[0] * cb[2]), ca[1] / math.sqrt(ca[0] * ca[2])
            rel = round(ra / rb - 1.0, 5)
            return {
                "ok": a["sign"] * rel >= float(a["min_rel"]),
                "measured": rel,
                "expected": f"sign {a['sign']}",
            }
        if t == "module_active":
            mod = ((after.get(a["frame"]) or {}).get("modules") or {}).get(a["module"])
            was = ((before.get(a["frame"]) or {}).get("modules") or {}).get(a["module"])
            return {
                "ok": bool(mod) and mod.get("enabled") is True,
                "measured": (
                    None if not mod else {"enabled": mod.get("enabled"), "num": mod.get("num")}
                ),
                "note": "already active before the task" if was and was.get("enabled") else None,
            }
        if t in ("history_grew", "history_unchanged"):
            rows = {}
            for f in a["frames"]:
                b, v = before.get(f) or {}, after.get(f) or {}
                rows[f] = {
                    "before": b.get("history_end", 0),
                    "after": v.get("history_end", 0),
                    "changed": b.get("fingerprint", "") != v.get("fingerprint", ""),
                }
            if t == "history_unchanged":
                return {"ok": not any(r["changed"] for r in rows.values()), "measured": rows}
            grew = sum(1 for r in rows.values() if r["changed"] and r["after"] > r["before"])
            return {"ok": grew >= int(a.get("min_frames", len(a["frames"]))), "measured": rows}
        if t == "tools_absent":
            names = [n.lower() for n in ctx.get("tools") or []]
            hits = sorted({n for n in names for p in a["patterns"] if p.lower() in n})
            return {"ok": bool(names) and not hits, "measured": hits}
        if t == "file_from_step":
            path = dig(_env(ctx, a["step"]), a["path"])
            if not path or not Path(path).is_file():
                return {"ok": False, "measured": path, "note": "no such file"}
            data = Path(path).read_bytes()
            ok = len(data) >= int(a.get("min_bytes", 1)) and all(
                s.encode() in data for s in a.get("contains") or []
            )
            return {"ok": ok, "measured": {"path": path, "bytes": len(data)}}
        if t == "export_file":
            files = dig(_env(ctx, a["step"]), "data.files") or []
            outs = []
            ok = False
            for f in files:
                out = f.get("output")
                if not out or not Path(out).is_file():
                    outs.append({"output": out, "exists": False})
                    continue
                size = Path(out).stat().st_size
                edge = _jpeg_long_edge(Path(out)) if a.get("jpeg_long_edge") else None
                good = size >= int(a.get("min_bytes", 1)) and (
                    not a.get("jpeg_long_edge") or edge == int(a["jpeg_long_edge"])
                )
                outs.append({"output": out, "bytes": size, "long_edge": edge})
                ok = ok or good
            return {"ok": ok, "measured": outs}
        if t == "stats_delta":
            b = dig(_env(ctx, a["before_step"]), a["path"])
            v = dig(_env(ctx, a["after_step"]), a["path"])
            if not isinstance(b, (int, float)) or not isinstance(v, (int, float)):
                return {"ok": False, "measured": {"before": b, "after": v}}
            return {"ok": a["sign"] * (v - b) > 0, "measured": {"before": b, "after": v}}
    except (KeyError, TypeError, ValueError, OSError) as e:
        return {"ok": False, "measured": None, "note": f"assertion error: {e}"}
    raise ValueError(f"unknown assertion type {t!r}")


def score_task(
    scoring: dict,
    results: dict[str, dict],
    *,
    wall_s: float,
    budget_s: float,
    category: str,
    blocked: bool = False,
    last_envelope_ok: bool | None = None,
) -> tuple[str, str]:
    """(score, outcome). The rule is `editor_tasks.json`'s `scoring_rules`."""
    if blocked:
        return "fail", "blocked"

    def holds(ids: list[str]) -> bool:
        return bool(ids) and all(results.get(i, {}).get("ok") for i in ids)

    if holds(scoring.get("pass") or []):
        if wall_s > budget_s:
            return "partial", "over_budget"
        return "pass", ("correct_refusal" if category == "unreachable" else "verified")
    for group in scoring.get("partial") or []:
        if holds(group):
            return "partial", "partial"
    if holds(scoring.get("refusal") or []):
        return "fail", "honest_refusal"
    if last_envelope_ok:
        return "fail", "reported_ok_unverified"
    return "fail", "failed"


def summarise(task_results: list[dict]) -> dict:
    counts = {s: 0 for s in SCORES}
    outcomes: dict[str, int] = {}
    by_cat: dict[str, dict] = {}
    for r in task_results:
        counts[r["score"]] += 1
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        c = by_cat.setdefault(r["category"], {s: 0 for s in SCORES} | {"tasks": 0, "points": 0.0})
        c[r["score"]] += 1
        c["tasks"] += 1
        c["points"] += {"pass": 1.0, "partial": 0.5, "fail": 0.0}[r["score"]]
    for c in by_cat.values():
        c["score_pct"] = round(100.0 * c["points"] / c["tasks"], 1) if c["tasks"] else 0.0
    loop = [r["wall_s"] for r in task_results if r.get("calls") and r["outcome"] != "blocked"]
    sets = [
        c["wall_s"]
        for r in task_results
        for c in r.get("calls") or []
        if c["tool"] == "darkroom_set" and (c.get("envelope") or {}).get("ok")
    ]
    return {
        "tasks": len(task_results),
        "counts": counts,
        "outcomes": outcomes,
        "by_category": by_cat,
        "median_task_s": round(statistics.median(loop), 3) if loop else None,
        "median_darkroom_set_s": round(statistics.median(sets), 3) if sets else None,
        "points": sum({"pass": 1.0, "partial": 0.5, "fail": 0.0}[r["score"]] for r in task_results),
    }


def validate_result(doc: dict) -> list[str]:
    """The result JSON contract (unit-tested against a fake run)."""
    errors: list[str] = []
    for key in (
        "schema",
        "run_id",
        "body",
        "label",
        "git",
        "started",
        "finished",
        "tasks",
        "summary",
        "sessions",
        "environment",
    ):
        if key not in doc:
            errors.append(f"missing {key}")
    if doc.get("schema") != RESULT_SCHEMA:
        errors.append("wrong schema")
    for r in doc.get("tasks") or []:
        for key in (
            "id",
            "category",
            "sentence",
            "frames",
            "plan",
            "calls",
            "assertions",
            "score",
            "outcome",
            "wall_s",
            "budget_s",
        ):
            if key not in r:
                errors.append(f"task {r.get('id')}: missing {key}")
        if r.get("score") not in SCORES:
            errors.append(f"task {r.get('id')}: bad score {r.get('score')!r}")
        if r.get("outcome") not in OUTCOMES:
            errors.append(f"task {r.get('id')}: bad outcome {r.get('outcome')!r}")
        for c in r.get("calls") or []:
            for key in ("tool", "args", "envelope", "wall_s"):
                if key not in c:
                    errors.append(f"task {r.get('id')}: a call is missing {key}")
    s = doc.get("summary") or {}
    if s and sum((s.get("counts") or {}).values()) != len(doc.get("tasks") or []):
        errors.append("summary counts do not add up to the task count")
    return errors


# ---------------------------------------------------------------------------
# MCP over stdio (newline-delimited JSON-RPC 2.0)
# ---------------------------------------------------------------------------


class McpError(RuntimeError):
    pass


class McpClient:
    """A minimal MCP client: initialize, tools/list, tools/call. Stdlib only."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, stderr_path: Path) -> None:
        self._stderr = open(stderr_path, "ab")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
            cwd=str(cwd),
        )
        self._next = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            mid = msg.get("id")
            with self._lock:
                q = self._pending.pop(mid, None) if mid is not None else None
            if q is not None:
                q.put(msg)

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._next += 1
            mid = self._next
            q: queue.Queue = queue.Queue()
            self._pending[mid] = q
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(mid, None)
            raise TimeoutError(f"{method} did not answer within {timeout}s") from None
        if "error" in msg:
            raise McpError(json.dumps(msg["error"]))
        return msg.get("result") or {}

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "scouter-editor-benchmark", "version": "1"},
            },
            timeout=30,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    def list_tools(self) -> list[str]:
        return [t["name"] for t in self.request("tools/list", {}, timeout=30).get("tools", [])]

    def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        content = result.get("content") or []
        text = content[0].get("text") if content else None
        return json.loads(text) if text else {"ok": False, "outcome": "error:empty", "data": {}}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - shutdown must never mask results
            self.proc.kill()
        finally:
            self._stderr.close()


# ---------------------------------------------------------------------------
# The live darktable host (same launch and quit as tests/test_host_*.py)
# ---------------------------------------------------------------------------


def darktable_running() -> list[str]:
    out = subprocess.run(["pgrep", "-fl", "MacOS/darktable"], capture_output=True, text=True)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


class DarktableHost:
    """Launch darktable on one frame, wait for the darkroom, always quit it."""

    def __init__(self, library_db: Path, log_path: Path) -> None:
        self.library_db = library_db
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def _bridge(self):
        from scouter.core import dtbridge

        return dtbridge

    def launch(self, frame: Path) -> dict:
        started = time.monotonic()
        self.library_db.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab") as log:
            self.proc = subprocess.Popen(
                [str(DARKTABLE), "--library", str(self.library_db), str(frame)],
                stdout=log,
                stderr=log,
            )
        dtb = self._bridge()
        last = "never answered"
        deadline = time.monotonic() + 120
        in_darkroom = False
        while time.monotonic() < deadline:
            try:
                last = dtb.current_view()
            except Exception as e:  # noqa: BLE001 - bridge not up yet
                last = str(e)[:200]
            else:
                if last == "darkroom":
                    in_darkroom = True
                    break
            time.sleep(1.0)
        if not in_darkroom:
            return {
                "ok": False,
                "error": f"never reached the darkroom: {last}",
                "launch_s": time.monotonic() - started,
            }
        to_darkroom = time.monotonic() - started
        deadline = time.monotonic() + 60
        current: dict = {}
        while time.monotonic() < deadline:
            try:
                current = dtb.call("darkroom.current", timeout=10.0)
            except Exception as e:  # noqa: BLE001
                current = {"error": str(e)[:200]}
            if current.get("image_id") is not None:
                break
            time.sleep(0.5)
        loaded = current.get("path")
        ok = loaded is not None and os.path.realpath(loaded) == os.path.realpath(frame)
        return {
            "ok": ok,
            "launch_to_darkroom_s": round(to_darkroom, 2),
            "ready_s": round(time.monotonic() - started, 2),
            "loaded": current,
            "error": None if ok else f"darkroom shows {loaded!r}, expected {str(frame)!r}",
        }

    def alive(self) -> bool:
        try:
            self._bridge().call("view.current", timeout=10.0)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete_style(self, name: str) -> dict:
        try:
            return {
                "ok": True,
                "result": self._bridge().call(
                    "style.delete", {"name": name, "confirm": True}, timeout=30.0
                ),
            }
        except Exception as e:  # noqa: BLE001 - cleanup is best effort, and reported
            return {"ok": False, "error": str(e)[:300]}

    def quit(self) -> dict:
        proc = self.proc
        if proc is None:
            return {"ok": True, "how": "not running"}
        how = "osascript"
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "darktable" to quit'],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass
        for wait, sig in ((20, None), (10, signal.SIGTERM), (10, signal.SIGKILL)):
            if sig is not None:
                how = sig.name
                proc.send_signal(sig) if sig == signal.SIGTERM else proc.kill()
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        self.proc = None
        return {"ok": proc.poll() is not None, "how": how}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _strip_heavy(obj: Any) -> Any:
    """Drop hex param blobs from envelopes: they are in the XMP, and 27 tasks of
    history_read entries would otherwise make the result JSON megabytes."""
    if isinstance(obj, dict):
        return {
            k: _strip_heavy(v)
            for k, v in obj.items()
            if k not in ("params_hex", "blendop_params_hex")
        }
    if isinstance(obj, list):
        return [_strip_heavy(x) for x in obj]
    return obj


def git_info() -> dict:
    def g(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True, text=True
        ).stdout.strip()

    return {
        "sha": g("rev-parse", "HEAD"),
        "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(g("status", "--porcelain")),
    }


class BenchRun:
    """One body's benchmark. Every collaborator is injectable for the fake run."""

    def __init__(
        self,
        doc: dict,
        body: str,
        *,
        run_id: str,
        label: str,
        mcp: Any,
        host: Any,
        snapshot: Callable[[str], dict],
        paths: dict[str, str],
        job_frames: Callable[[dict], dict[str, str]] | None = None,
        log: Callable[[str], None] = print,
        sessions: list[str] | None = None,
        settle_poll_s: float = 0.5,
        plan_version: str = "v1",
        relaunch_on_hang: bool = True,
    ) -> None:
        self.relaunch_on_hang = relaunch_on_hang
        if plan_version not in PLAN_VERSIONS:
            raise ValueError(f"unknown plan version {plan_version!r}")
        self.plan_version = plan_version
        self.only_sessions = sessions
        self.settle_poll_s = settle_poll_s
        self.doc = doc
        self.body = body
        self.run_id = run_id
        self.label = label
        self.mcp = mcp
        self.host = host
        self.snapshot_fn = snapshot
        self.ctx: dict[str, str] = dict(paths)
        self.job_frames = job_frames
        self.log = log
        self.tools: list[str] = []
        self.sessions: list[dict] = []
        self.results: list[dict] = []
        self.cleanup: list[dict] = []

    # -- calls ---------------------------------------------------------------

    def _call(self, step: dict, prior: list[dict]) -> dict:
        args = render(step.get("args") or {}, self.ctx)
        args, provenance = resolve_value(args, prior)
        timeout = float(step.get("timeout_s", DEFAULT_CALL_TIMEOUT_S))
        t0 = time.monotonic()
        envelope = None
        transport_error = None
        try:
            envelope = self.mcp.call_tool(step["tool"], args, timeout)
        except Exception as e:  # noqa: BLE001 - a transport failure is a result
            transport_error = f"{type(e).__name__}: {e}"[:400]
        wall = round(time.monotonic() - t0, 3)
        rec = {
            "tool": step["tool"],
            "args": args,
            "envelope": _strip_heavy(envelope),
            "wall_s": wall,
        }
        if provenance:
            rec["value_provenance"] = provenance
        if transport_error:
            rec["transport_error"] = transport_error
        return rec

    def _run_steps(self, steps: list[dict], *, stop_on_error: bool = True) -> list[dict]:
        out: list[dict] = []
        for step in steps:
            rec = self._call(step, out)
            out.append(rec)
            env = rec["envelope"]
            if stop_on_error and (env is None or env.get("ok") is not True):
                break
        return out

    def _snap(self, roles: list[str]) -> dict[str, dict]:
        out = {}
        for role in roles:
            key = f"frame:{role}" if not role.startswith("job:") else role
            path = self.ctx.get(key)
            out[role] = (
                self.snapshot_fn(path)
                if path
                else {"exists": False, "history_end": 0, "fingerprint": "", "modules": {}}
            )
        return out

    # -- tasks ---------------------------------------------------------------

    def run_task(self, task: dict, *, blocked: bool = False) -> dict:
        task = effective_task(task, self.plan_version)
        roles = list(task["frames"])
        try:
            sentence = render(task["sentence"], self.ctx)
        except KeyError:
            sentence = task["sentence"]
        base = {
            "id": task["id"],
            "category": task["category"],
            "sentence": sentence,
            "frames": {
                r: Path(self.ctx.get(r if r.startswith("job:") else f"frame:{r}", "")).name
                for r in roles
            },
            "plan": [{"tool": s["tool"], "args": s.get("args") or {}} for s in task["plan"]],
            "plan_used": task["plan_used"],
            "revised_in": task.get("revised_in"),
            "agent_note": task.get("agent_note"),
            "agent_response": task.get("agent_response"),
            "budget_s": task["budget_s"],
        }
        if blocked:
            return base | {
                "calls": [],
                "context_calls": [],
                "assertions": {},
                "score": "fail",
                "outcome": "blocked",
                "wall_s": 0.0,
                "snapshots": {},
            }
        try:
            assertions_spec = render(task["assertions"], self.ctx)
            render(task["plan"], self.ctx)
            render(task.get("context") or [], self.ctx)
        except KeyError as e:
            # A precondition never happened (e.g. the job import failed, so
            # `{job:1}` has no path). Not Scouter's answer to THIS sentence.
            return base | {
                "calls": [],
                "context_calls": [],
                "assertions": {},
                "score": "fail",
                "outcome": "blocked",
                "wall_s": 0.0,
                "snapshots": {},
                "note": f"precondition missing: {e}",
            }
        pre = None
        context_calls: list[dict] = []
        if task.get("context"):
            pre = self._snap(roles)
            context_calls = self._run_steps(task["context"])
        before = self._snap(roles)
        t0 = time.monotonic()
        calls = self._run_steps(task["plan"])
        wall = round(time.monotonic() - t0, 3)
        after = self._snap(roles)
        settle = None
        if task.get("settle_s"):
            # darktable writes a style application's sidecar LATER than the
            # bridge call returns. The immediate snapshot is kept (it is what
            # a read right after the call would see); scoring uses the state
            # once the sidecars stop changing, and the lag is recorded.
            immediate = after
            settle_t0 = time.monotonic()
            lag = None
            stable_since = None
            last_fp = {k: v.get("fingerprint") for k, v in after.items()}
            while time.monotonic() - settle_t0 < float(task["settle_s"]):
                time.sleep(self.settle_poll_s)
                now = self._snap(roles)
                fp = {k: v.get("fingerprint") for k, v in now.items()}
                if fp != last_fp:
                    lag = round(time.monotonic() - t0, 2)
                    stable_since = time.monotonic()
                    last_fp = fp
                    after = now
                elif stable_since is not None and time.monotonic() - stable_since >= 2.0:
                    break
            settle = {
                "settle_s": task["settle_s"],
                "last_change_s_after_call_start": lag,
                "changed_after_call_returned": lag is not None,
                "immediate": {k: compact_snapshot(v) for k, v in immediate.items()},
            }
        ctx = {
            "before": before,
            "after": after,
            "pre": pre or {},
            "steps": calls,
            "tools": self.tools,
        }
        results = {
            aid: evaluate(a, ctx) | {"type": a["type"]} for aid, a in assertions_spec.items()
        }
        last_ok = (calls[-1]["envelope"] or {}).get("ok") if calls else None
        score, outcome = score_task(
            task["scoring"],
            results,
            wall_s=wall,
            budget_s=float(task["budget_s"]),
            category=task["category"],
            last_envelope_ok=last_ok,
        )
        snaps = {
            "before": {k: compact_snapshot(v) for k, v in before.items()},
            "after": {k: compact_snapshot(v) for k, v in after.items()},
        }
        if pre:
            snaps["pre_context"] = {k: compact_snapshot(v) for k, v in pre.items()}
        return base | {
            "calls": calls,
            "context_calls": context_calls,
            "assertions": results,
            "score": score,
            "outcome": outcome,
            "wall_s": wall,
            "snapshots": snaps,
            "settle": settle,
        }

    def _setup_hung(self, setup: list[dict]) -> bool:
        for rec in setup:
            code = ((rec.get("envelope") or {}).get("error") or {}).get("code")
            if rec.get("transport_error") or code in ("bridge_timeout", "commit_timeout"):
                return not self.host.alive()
        return False

    def _session_blocked(self, task_result: dict) -> bool:
        for c in task_result.get("calls") or []:
            code = ((c.get("envelope") or {}).get("error") or {}).get("code")
            if c.get("transport_error") or code == "bridge_timeout":
                return not self.host.alive()
        return False

    # -- sessions ------------------------------------------------------------

    def run(self) -> dict:
        started = datetime.now(timezone.utc).isoformat()
        self.mcp.initialize()
        self.tools = self.mcp.list_tools()
        by_id = tasks_by_id(self.doc)
        frames = self.doc["frames"][self.body]
        for session in self.doc["sessions"]:
            if self.only_sessions and session["id"] not in self.only_sessions:
                continue
            if session.get("only_if") and not frames.get(session["only_if"]):
                self.sessions.append(
                    {"id": session["id"], "skipped": f"{session['only_if']} is off for {self.body}"}
                )
                continue
            if session.get("skip_if_ctx") and self.ctx.get(session["skip_if_ctx"]):
                self.sessions.append(
                    {"id": session["id"], "skipped": f"{session['skip_if_ctx']} already present"}
                )
                continue
            self.log(
                f"[{self.body}] session {session['id']}: launching darktable on {session['role']}"
            )
            launch = self.host.launch(Path(self.ctx[f"frame:{session['role']}"]))
            srec: dict = {
                "id": session["id"],
                "role": session["role"],
                "launch": launch,
                "setup": [],
                "tasks": [],
            }
            blocked = not launch.get("ok")
            try:
                if not blocked:
                    srec["setup"] = self._run_steps(session.get("setup") or [], stop_on_error=False)
                    if self._setup_hung(srec["setup"]) and self.relaunch_on_hang:
                        # Known B1: the first history_commit after a launch can
                        # wedge the bridge. Quit, relaunch ONCE, redo the setup
                        # steps that did not answer ok, and record it.
                        self.log(f"[{self.body}] {session['id']}: setup hung, relaunch once")
                        first = {"setup": srec["setup"], "quit": self.host.quit()}
                        launch = self.host.launch(Path(self.ctx[f"frame:{session['role']}"]))
                        first["relaunch"] = launch
                        srec["hung_first_launch"] = first
                        blocked = not launch.get("ok")
                        if not blocked:
                            redo = [
                                st
                                for st, rec in zip(session.get("setup") or [], srec["setup"])
                                if (rec.get("envelope") or {}).get("ok") is not True
                            ]
                            again = self._run_steps(redo, stop_on_error=False)
                            kept = [
                                rec
                                for rec in srec["setup"]
                                if (rec.get("envelope") or {}).get("ok") is True
                            ]
                            srec["setup"] = kept + again
                            if self._setup_hung(again):
                                blocked = True
                                srec["hung_again"] = True
                    for rec in srec["setup"]:
                        if rec["tool"] == "library_import" and self.job_frames:
                            self.ctx.update(self.job_frames(rec.get("envelope") or {}))
                for tid in session.get("tasks") or []:
                    task = by_id[tid]
                    self.log(f"[{self.body}]   {tid} {task['category']}")
                    result = self.run_task(task, blocked=blocked)
                    self.results.append(result)
                    srec["tasks"].append(tid)
                    self.log(
                        f"[{self.body}]   -> {result['score']} ({result['outcome']}) {result['wall_s']}s"
                    )
                    if not blocked and self._session_blocked(result):
                        blocked = True
                        srec["wedged_after"] = tid
                if not blocked and session.get("cleanup_styles"):
                    for name_key in session["cleanup_styles"]:
                        name = self.ctx.get(name_key)
                        if name:
                            self.cleanup.append({"style": name} | self.host.delete_style(name))
            finally:
                srec["quit"] = self.host.quit()
            self.sessions.append(srec)
        order = {t["id"]: i for i, t in enumerate(self.doc["tasks"])}
        self.results.sort(key=lambda r: order[r["id"]])
        return {
            "schema": RESULT_SCHEMA,
            "run_id": self.run_id,
            "label": self.label,
            "body": self.body,
            "plan_version": self.plan_version,
            "tasks_revision": self.doc.get("revision", 1),
            "camera": frames.get("camera"),
            "git": git_info(),
            "started": started,
            "finished": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "mcp_server": "python -m scouter.mcp.server (README: command scouter-mcp), SCOUTER_JOB unset",
                "tools": self.tools,
                "paths": {k: v for k, v in self.ctx.items() if not k.startswith("jobname")},
            },
            "sessions": self.sessions,
            "cleanup": self.cleanup,
            "tasks": self.results,
            "summary": summarise(self.results),
        }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _plan_text(r: dict) -> str:
    if not r["plan"]:
        return "(no call: refuse)"
    return ", ".join(s["tool"] for s in r["plan"])


def _headline(r: dict) -> str:
    """The one measured fact that decided the score, for the table."""
    if r["outcome"] == "blocked":
        return "session blocked"
    for c in r.get("calls") or []:
        env = c.get("envelope") or {}
        if c.get("transport_error"):
            return f"{c['tool']}: {c['transport_error'][:80]}"
        if env.get("ok") is False:
            err = env.get("error") or {}
            return f"{c['tool']}: {err.get('code')} - {str(err.get('message'))[:90]}"
    bits = []
    for aid, a in (r.get("assertions") or {}).items():
        if a.get("type") in (
            "param_delta",
            "wb_ratio",
            "wb_green",
            "step_field",
            "stats_delta",
            "slot_value",
            "param_restored",
        ):
            bits.append(
                f"{aid}={json.dumps(a.get('measured'))[:40]}{'' if a.get('ok') else ' (x)'}"
            )
    return "; ".join(bits[:3])


def markdown(doc: dict) -> str:
    s = doc["summary"]
    lines = [
        f"# Scouter editor benchmark: {doc['body']} ({doc['label']})",
        "",
        f"- run `{doc['run_id']}`, git `{doc['git']['sha'][:10]}` on `{doc['git']['branch']}`"
        f"{' (dirty)' if doc['git'].get('dirty') else ''}, {doc['started']} to {doc['finished']}",
        f"- camera: {doc.get('camera')}",
        f"- agent plan {doc.get('plan_version', 'v1')},"
        f" task file revision {doc.get('tasks_revision', 1)}",
        f"- **{s['counts']['pass']} pass / {s['counts']['partial']} partial / {s['counts']['fail']} fail** of {s['tasks']} "
        f"({s['points']:.1f} points); median task {s['median_task_s']}s, median darkroom_set {s['median_darkroom_set_s']}s",
        f"- outcomes: {', '.join(f'{k} {v}' for k, v in sorted(s['outcomes'].items()))}",
        "",
        "## Sessions",
        "",
    ]
    for ses in doc["sessions"]:
        if ses.get("skipped"):
            lines.append(f"- {ses['id']}: skipped ({ses['skipped']})")
            continue
        la = ses.get("launch") or {}
        setup = ", ".join(
            f"{c['tool']}={(c.get('envelope') or {}).get('outcome', c.get('transport_error'))}"
            for c in ses.get("setup") or []
        )
        lines.append(
            f"- {ses['id']} ({ses['role']}): launch ok={la.get('ok')} ready {la.get('ready_s')}s; setup: {setup or 'none'}; "
            f"quit {ses.get('quit', {}).get('how')}{'; WEDGED after ' + ses['wedged_after'] if ses.get('wedged_after') else ''}"
        )
    lines += [
        "",
        "## Category scores",
        "",
        "| category | pass | partial | fail | score |",
        "|---|---|---|---|---|",
    ]
    for cat in CATEGORIES:
        c = s["by_category"].get(cat)
        if c:
            lines.append(
                f"| {cat} | {c['pass']} | {c['partial']} | {c['fail']} | {c['score_pct']}% |"
            )
    lines += [
        "",
        "## Tasks",
        "",
        "| id | category | sentence | plan | outcome | score | s | measured |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in doc["tasks"]:
        sentence = r["sentence"].replace("|", "/")
        lines.append(
            f"| {r['id']} | {r['category']} | {sentence} | {_plan_text(r)} | {r['outcome']} | **{r['score']}** | {r['wall_s']} | {_headline(r).replace('|', '/')} |"
        )
    if doc.get("cleanup"):
        lines += ["", "## Cleanup", ""] + [f"- {c}" for c in doc["cleanup"]]
    return "\n".join(lines) + "\n"


def compare(old: dict, new: dict) -> str:
    """Per-task and per-category delta between two result files (the phase 3
    re-run). A task revised after the old file's task revision is marked not
    comparable and left out of the comparable points."""
    o = {r["id"]: r for r in old["tasks"]}
    old_rev = old.get("tasks_revision", 1)
    pts = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
    lines = [
        f"# Benchmark delta {old['body']}: {old['label']} -> {new['label']}",
        "",
        f"points {old['summary']['points']:.1f} -> {new['summary']['points']:.1f}"
        f" (plan {old.get('plan_version', 'v1')} rev {old_rev} -> plan"
        f" {new.get('plan_version', 'v1')} rev {new.get('tasks_revision', 1)})",
        "",
        "| id | category | before | after | delta |",
        "|---|---|---|---|---|",
    ]
    cats: dict[str, list[float]] = {}
    not_comparable = []
    for r in new["tasks"]:
        before = o.get(r["id"])
        b = f"{before['score']} ({before['outcome']})" if before else "-"
        a = f"{r['score']} ({r['outcome']})"
        if (r.get("revised_in") or 0) > old_rev:
            not_comparable.append(r["id"])
            lines.append(f"| {r['id']} | {r['category']} | {b} | {a} | not comparable (revised) |")
            continue
        d = pts[r["score"]] - (pts[before["score"]] if before else 0.0)
        c = cats.setdefault(r["category"], [0.0, 0.0, 0])
        c[0] += pts[before["score"]] if before else 0.0
        c[1] += pts[r["score"]]
        c[2] += 1
        mark = "" if not d else f" **{d:+.1f}**"
        lines.append(f"| {r['id']} | {r['category']} | {b} | {a} |{mark} |")
    lines += ["", "| category | tasks | before | after |", "|---|---|---|---|"]
    for cat in CATEGORIES:
        if cat in cats:
            b, a, n = cats[cat]
            lines.append(f"| {cat} | {n} | {100 * b / n:.1f}% | {100 * a / n:.1f}% |")
    if not_comparable:
        lines += ["", f"not comparable (task revised): {', '.join(not_comparable)}"]
    return "\n".join(lines) + "\n"


def merge(
    base: dict, *overrides: dict, label: str | None = None, only: set[str] | None = None
) -> dict:
    """Replace `base`'s task results by id with the ones from `overrides`
    (a re-run of one session), recompute the summary, and say where each task
    came from. The base's own record of the replaced tasks is kept under
    `replaced`, so nothing measured is thrown away."""
    by_id = {r["id"]: r for r in base["tasks"]}
    source = {r["id"]: base["run_id"] for r in base["tasks"]}
    replaced = []
    for o in overrides:
        if o["body"] != base["body"]:
            raise ValueError(f"cannot merge {o['body']} into {base['body']}")
        for r in o["tasks"]:
            if only and r["id"] not in only:
                continue
            if r["id"] in by_id:
                replaced.append(by_id[r["id"]] | {"from_run": source[r["id"]]})
            by_id[r["id"]] = r
            source[r["id"]] = o["run_id"]
    order = [r["id"] for r in base["tasks"]]
    tasks = [by_id[i] | {"from_run": source[i]} for i in order]
    out = dict(base)
    out["label"] = label or f"{base['label']}+merged"
    out["tasks"] = tasks
    out["replaced"] = replaced
    out["merged_from"] = [base["run_id"], *[o["run_id"] for o in overrides]]
    out["sessions"] = list(base["sessions"]) + [
        s | {"from_run": o["run_id"]}
        for o in overrides
        for s in o["sessions"]
        if not only or set(s.get("tasks") or []) & only
    ]
    out["summary"] = summarise(tasks)
    return out


def write_results(doc: dict, out_dir: Path, stamp: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / f"{stamp}-{doc['body']}.json"
    mpath = out_dir / f"{stamp}-{doc['body']}.md"
    jpath.write_text(
        json.dumps(doc, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8"
    )
    mpath.write_text(markdown(doc), encoding="utf-8")
    return jpath, mpath


# ---------------------------------------------------------------------------
# Live staging
# ---------------------------------------------------------------------------


def _copy_readonly_source(src: Path, dst: Path) -> None:
    """Copy an original (read only, never moved) to a writable scratch path."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o644)


def stage_body(
    doc: dict, body: str, run_dir: Path, run_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Scratch copies + the bench config. Returns (template ctx, env for Scouter)."""
    spec = doc["frames"][body]
    bdir = run_dir / body
    ctx: dict[str, str] = {"body": body, "run_id": run_id}
    for role in ("dark", "bright", "blown", "hero", "calib"):
        name = spec.get(role)
        if not name:
            continue
        sub = "calib" if role == "calib" else "frames"
        dst = bdir / sub / Path(name).name
        if not dst.exists():
            _copy_readonly_source(EDITING_TRIAL / name, dst)
        ctx[f"frame:{role}"] = str(dst.resolve())
        ctx[f"framename:{role}"] = Path(name).name
        ctx[f"framebase:{role}"] = dst.name
    card = bdir / "card" / "DCIM" / "100BENCH"
    for i, name in enumerate(spec["job"], start=1):
        dst = card / Path(name).name
        if not dst.exists():
            _copy_readonly_source(EDITING_TRIAL / name, dst)
        ctx[f"jobname:{i}"] = Path(name).name
    short = run_id.replace("t", "").replace("z", "")[-10:]
    ctx["card"] = str((bdir / "card").resolve())
    ctx["job_slug"] = f"bench-{body}-{short}"
    ctx["preset"] = f"scouter-bench-{body}-{short}"
    ctx["out_dir"] = str((bdir / "exports").resolve())

    cfg_dir = SCRATCH / "bench-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    passfile = cfg_dir / "backup.pass"
    if not passfile.exists():
        passfile.write_text(secrets.token_hex(24) + "\n")  # never printed
    passfile.chmod(0o600)
    (SCRATCH / "bench-backup").mkdir(parents=True, exist_ok=True)
    (SCRATCH / "bench-jobs").mkdir(parents=True, exist_ok=True)
    cfg = {
        "version": 1,
        "library_root": str((SCRATCH / "bench-jobs").resolve()),
        "backup": {
            "enabled": True,
            "target": str((SCRATCH / "bench-backup").resolve()),
            "encryption": "openssl",
            "openssl_passfile": str(passfile.resolve()),
        },
        "vault": {"jobs_dir": "Projects/Photo jobs"},
    }
    (cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (bdir / "cache").mkdir(parents=True, exist_ok=True)
    (bdir / "vault").mkdir(parents=True, exist_ok=True)
    env = {
        "PYTHONPATH": str(REPO),
        "SCOUTER_CONFIG": str((cfg_dir / "config.json").resolve()),
        "SCOUTER_CACHE": str((bdir / "cache").resolve()),
        # library.import writes a job note: never into the real vault.
        "OBSIDIAN_VAULT_PATH": str((bdir / "vault").resolve()),
    }
    return ctx, env


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--live", action="store_true", help="drive the real darktable (required to run)")
    p.add_argument("--body", choices=("olympus", "sony", "all"), default="all")
    p.add_argument("--label", default="phase2-main")
    p.add_argument(
        "--plan",
        choices=PLAN_VERSIONS,
        default="v1",
        help="agent plan: v1 (revision 1, phase 2 verbs) or v2 (phase 3 verbs where they apply)",
    )
    p.add_argument("--tasks", type=Path, default=TASKS_PATH)
    p.add_argument("--out", type=Path, default=RESULTS_DIR)
    p.add_argument(
        "--recalibrate", action="store_true", help="re-measure the WB table even if one exists"
    )
    p.add_argument("--compare", nargs=2, type=Path, metavar=("OLD", "NEW"))
    p.add_argument("--validate", action="store_true", help="validate the task file and exit")
    p.add_argument(
        "--merge",
        nargs="+",
        metavar="JSON",
        help="BASE.json then re-run files whose tasks replace it",
    )
    p.add_argument("--merge-tasks", help="with --merge: only take these task ids from the re-runs")
    p.add_argument("--sessions", help="comma-separated session ids to run (default: all)")
    args = p.parse_args(argv)

    if args.merge:
        docs = [json.loads(Path(x).read_text()) for x in args.merge]
        only = set(args.merge_tasks.split(",")) if args.merge_tasks else None
        merged = merge(docs[0], *docs[1:], label=args.label, only=only)
        j, m = write_results(merged, args.out, f"{docs[0]['run_id']}-merged")
        print(j)
        print(m)
        return 0
    if args.compare:
        print(
            compare(
                json.loads(args.compare[0].read_text()), json.loads(args.compare[1].read_text())
            )
        )
        return 0
    doc = load_tasks(args.tasks)
    problems = validate_tasks(doc)
    if problems or args.validate:
        print("\n".join(problems) or "task file OK")
        return 1 if problems else 0
    if not args.live:
        print("refusing to run without --live (this launches the real darktable)")
        return 2
    running = darktable_running()
    if running:
        print("refusing: a darktable is already running:\n" + "\n".join(running))
        return 3
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from darktable_mcp.bridge.client import plugin_path

    if not plugin_path().exists():
        print(
            "refusing: the darktable_mcp Lua plugin is not installed (run `darktable-mcp install-plugin`)"
        )
        return 4

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    run_dir = SCRATCH / "bench-runs" / stamp
    bodies = ["olympus", "sony"] if args.body == "all" else [args.body]
    written = []
    for body in bodies:
        ctx, env = stage_body(doc, body, run_dir, stamp)
        os.environ.update(env)  # the runner's own bridge reads (host readiness, style cleanup)
        wb_table = Path(env["SCOUTER_CONFIG"]).parent / "wb"
        if not args.recalibrate and any(wb_table.glob("*.json")) and body == "olympus":
            ctx["_wb_table_present"] = "1"
        for session in doc["sessions"]:
            if session["id"] == "calib":
                session["skip_if_ctx"] = "_wb_table_present"
            if session["id"] == "C":
                session["cleanup_styles"] = ["preset"]
        server_env = dict(os.environ) | env
        server_env.pop("SCOUTER_JOB", None)  # README config: the server derives its own job
        mcp = McpClient(
            [sys.executable, "-m", "scouter.mcp.server"],
            env=server_env,
            cwd=REPO,
            stderr_path=run_dir / body / "mcp-server.stderr.log",
        )

        def job_frames(envelope: dict, _ctx=ctx) -> dict[str, str]:
            data = (envelope or {}).get("data") or {}
            job_path = data.get("path")
            if not job_path:
                return {}
            by_source = {f.get("source_name"): f.get("dest") for f in data.get("files") or []}
            out = {}
            for i in range(1, len(doc["frames"][body]["job"]) + 1):
                dest = by_source.get(_ctx[f"jobname:{i}"])
                if dest:
                    out[f"job:{i}"] = str(Path(job_path) / dest)
            return out

        host = DarktableHost(
            SCRATCH / "bench-library" / "library.db", run_dir / body / "darktable.log"
        )
        runner = BenchRun(
            doc,
            body,
            run_id=stamp,
            label=args.label,
            mcp=mcp,
            host=host,
            snapshot=snapshot_xmp,
            paths=ctx,
            job_frames=job_frames,
            sessions=args.sessions.split(",") if args.sessions else None,
            plan_version=args.plan,
        )
        try:
            result = runner.run()
        finally:
            mcp.close()
            if host.proc is not None:
                host.quit()
        result["environment"]["run_dir"] = str(run_dir / body)
        j, m = write_results(result, args.out, stamp)
        written.append((j, m))
        s = result["summary"]["counts"]
        print(f"{body}: {s['pass']} pass / {s['partial']} partial / {s['fail']} fail -> {j}")
    for j, m in written:
        print(j)
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
