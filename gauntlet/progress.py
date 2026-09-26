"""Checkpoints (spec 8): progress.json after every case and layer, and a
log.md line per case in the acceptance driver's format."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from gauntlet.case import dumps
from gauntlet.store import RunDir, atomic_write_text

SCHEMA = "gauntlet-progress/1"
_COUNTERS = ("done", "pass", "partial", "fail", "silent", "crash", "hang", "blocked", "parked",
             "p0_total", "p0_pass", "points")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Progress:
    def __init__(self, run: RunDir, adapter_name: str, commit: str = "",
                 clock=time.monotonic) -> None:
        self.run = run
        self.clock = clock
        self.t0 = clock()
        if run.progress_json.exists():
            self.doc = json.loads(run.progress_json.read_text(encoding="utf-8"))
        else:
            self.doc = {"schema": SCHEMA, "stamp": run.stamp, "adapter": adapter_name,
                        "commit": commit, "started": _now(), "status": "running",
                        "current_case": None, "layers": {}, "parked": [], "eta_s": None}
        self._rate: dict[str, float] = {}

    def layer(self, name: str, total: int, p0_total: int, live: bool) -> dict:
        lay = self.doc["layers"].setdefault(name, {k: 0 for k in _COUNTERS})
        lay.update(total=total, p0_total=p0_total, live=live)
        lay.setdefault("status", "pending")
        return lay

    def start_layer(self, name: str) -> None:
        self.doc["layers"][name]["status"] = "running"
        self._layer_t0 = self.clock()
        self._layer_done0 = self.doc["layers"][name]["done"]
        self.save()

    def record(self, name: str, case_id: str, tier: str, verdict: dict, wall_s: float) -> None:
        lay = self.doc["layers"][name]
        self.unpark(case_id)
        lay["done"] += 1
        lay[verdict["score"]] += 1
        lay["points"] += {"pass": 1.0, "partial": 0.5, "fail": 0.0}[verdict["score"]]
        outcome = verdict["outcome"]
        if outcome == "silent_failure":
            lay["silent"] += 1
        if outcome == "crash":
            lay["crash"] += 1
        if outcome == "failed_hang":
            lay["hang"] += 1
        if outcome == "blocked":
            lay["blocked"] += 1
        if tier == "P0" and verdict["score"] == "pass":
            lay["p0_pass"] += 1
        lay["rate_pct"] = round(100.0 * lay["points"] / lay["done"], 1)
        elapsed = self.clock() - self._layer_t0
        done_here = lay["done"] - self._layer_done0
        per_case = elapsed / done_here if done_here else 0.0
        lay["eta_s"] = round(per_case * (lay["total"] - lay["done"]), 1)
        self.doc["current_case"] = case_id
        self.doc["eta_s"] = sum(v.get("eta_s") or 0 for v in self.doc["layers"].values())
        note = verdict.get("silent_failure") or "; ".join(str(n) for n in verdict.get("notes", [])[:2])  # noqa: E501
        self.log(f"- **{case_id}**: {outcome}, {wall_s:.2f}s{' -- ' + note if note else ''}")
        self.save()

    def park(self, name: str, case_id: str, reason: str) -> None:
        lay = self.doc["layers"][name]
        lay["parked"] += 1
        if case_id not in self.doc["parked"]:
            self.doc["parked"].append(case_id)
        lay["status"] = "parked"
        self.log(f"- **{case_id}**: parked -- {reason}")
        self.save()

    def unpark(self, case_id: str) -> None:
        if case_id in self.doc["parked"]:
            self.doc["parked"].remove(case_id)

    def end_layer(self, name: str, status: str = "done") -> None:
        lay = self.doc["layers"][name]
        if lay.get("status") != "parked" or status != "done":
            lay["status"] = status
        lay["eta_s"] = 0.0 if status == "done" else lay.get("eta_s")
        self.log(f"\n## Layer {name}: {status} ({lay['done']}/{lay['total']}, "
                 f"pass {lay['pass']}, partial {lay['partial']}, fail {lay['fail']}, "
                 f"silent {lay['silent']}, crash {lay['crash']})\n")
        self.save()

    def finish(self, status: str) -> None:
        self.doc["status"] = status
        self.doc["current_case"] = None
        self.save()

    def log(self, line: str) -> None:
        self.run.log_md.parent.mkdir(parents=True, exist_ok=True)
        with open(self.run.log_md, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def save(self) -> None:
        self.doc["updated"] = _now()
        atomic_write_text(self.run.progress_json, dumps(self.doc) + "\n")
