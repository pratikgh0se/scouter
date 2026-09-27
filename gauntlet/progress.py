"""Checkpoints (spec 8): progress.json after every case and layer, and a
log.md line per case in the acceptance driver's format."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from gauntlet.case import dumps
from gauntlet.evaluate import is_hang
from gauntlet.store import RunDir, atomic_write_text

SCHEMA = "gauntlet-progress/1"
#: (fix round 1 review L1): `inherited_wedge` gets its own counter, not just
#: the generic `blocked` -- it is deliberately excluded from `hang`
#: (`evaluate.is_hang`), and folding it into `blocked` alone hid whether a
#: layer's blocked cases were wedges it inherited or something else entirely.
_COUNTERS = ("done", "pass", "partial", "fail", "silent", "crash", "hang", "inherited_wedge",
             "blocked", "parked", "p0_total", "p0_pass", "points")


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
        #: (row 501 item H, fix round 1): a generate --subset's own record,
        #: folded in once so it survives every later progress.json
        #: save() -- "recorded in progress.json" (the ruling), read here
        #: rather than duplicating the write in the runner.
        if "subset" not in self.doc and run.subset_json.exists():
            try:
                self.doc["subset"] = json.loads(run.subset_json.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
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

    def _apply(self, lay: dict, tier: str, verdict: dict, sign: int,
               blocked: str | None = None) -> None:
        """Add (sign=+1) or remove (sign=-1) one verdict's contribution to a
        layer's counters (fix round 1, I3: reruns must not double-count).
        (final fix I6): hangs by the shared `is_hang(outcome, blocked)`, and
        `blocked` counts every blocked observation (as the report does)."""
        lay[verdict["score"]] += sign
        lay["points"] += sign * {"pass": 1.0, "partial": 0.5, "fail": 0.0}[verdict["score"]]
        outcome = verdict["outcome"]
        if outcome == "silent_failure":
            lay["silent"] += sign
        if outcome == "crash":
            lay["crash"] += sign
        if is_hang(outcome, blocked):
            lay["hang"] += sign
        if (blocked or "") == "inherited_wedge":
            lay["inherited_wedge"] += sign
        if blocked or outcome == "blocked":
            lay["blocked"] += sign
        if tier == "P0" and verdict["score"] == "pass":
            lay["p0_pass"] += sign

    def record(self, name: str, case_id: str, tier: str, verdict: dict, wall_s: float,
              prev_verdict: dict | None = None, *, blocked: str | None = None,
              prev_blocked: str | None = None) -> None:
        """`prev_verdict` (fix round 1, I3): when this case already had a
        result (an `only_failed` or explicit-id rerun), the runner passes the
        verdict it is replacing so its old contribution is undone first --
        otherwise a rerun double-counts `done` and every counter."""
        lay = self.doc["layers"][name]
        self.unpark(case_id, name)
        if prev_verdict is not None:
            self._apply(lay, tier, prev_verdict, -1, prev_blocked)
        else:
            lay["done"] += 1
        self._apply(lay, tier, verdict, 1, blocked)
        outcome = verdict["outcome"]
        lay["rate_pct"] = round(100.0 * lay["points"] / lay["done"], 1) if lay["done"] else 0.0
        elapsed = self.clock() - self._layer_t0
        done_here = lay["done"] - self._layer_done0
        per_case = elapsed / done_here if done_here else 0.0
        lay["eta_s"] = max(0.0, round(per_case * (lay["total"] - lay["done"]), 1))
        self.doc["current_case"] = case_id
        self.doc["eta_s"] = max(0.0, sum(v.get("eta_s") or 0 for v in
                                         self.doc["layers"].values()))
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

    def unpark(self, case_id: str, layer: str | None = None) -> None:
        """(fix round 1, I4): also restores the layer's own parked counter and
        status -- previously only the run-level `parked` list was cleared, so
        a layer that resolved its last park in-process (`wait_on_park`) was
        left reporting `status: "parked"` forever."""
        if case_id in self.doc["parked"]:
            self.doc["parked"].remove(case_id)
        if layer is not None:
            lay = self.doc["layers"].get(layer)
            if lay is not None and lay.get("parked", 0) > 0:
                lay["parked"] -= 1
                if lay["parked"] <= 0 and lay.get("status") == "parked":
                    lay["status"] = "running"

    def end_layer(self, name: str, status: str = "done") -> None:
        lay = self.doc["layers"][name]
        still_parked = lay.get("parked", 0) > 0
        if not (still_parked and status == "done"):
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
