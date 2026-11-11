#!/usr/bin/env python3
"""Regenerate tests/fixtures/touch_floor_sensitivity.json (row: htc-touch-floor).

Reads the recorded night-1 gauntlet evidence for the 25 P0 bare-phrase cases
in the D-bare-step-below-perceptual-floor cluster (see
`~/second-brain`-adjacent scratchpad `htc-triage-coverage.md` and
`htc_triage_clusters.json`) and computes each (phrase, body)'s measured
stat-per-param sensitivity: dstat / dparam, read straight from
`observation.stats_before` / `stats_after` and the applied move, at the
region/stat/param `tests/gauntlet/layers/coverage.py:PHRASE_STAT` names for
that phrase. This is the SOURCE the touch-floor tests check vocab.py's step
sizes against -- not hand-copied numbers.

Usage: uv run python tools/gen_touch_floor_sensitivity.py > tests/fixtures/touch_floor_sensitivity.json
(requires the night-1 case evidence at CASES_DIR below; read-only)
"""  # noqa: E501

from __future__ import annotations

import json
import sys
from pathlib import Path

CASES_DIR = Path.home() / "Pictures/ScouterScratch/gauntlet/night1-20260926/cases"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.gauntlet.layers.coverage import PHRASE_STAT  # noqa: E402

# phrase key -> (module, param) of the ONE adjustable (ladder-scaled) move.
PHRASE_PARAM = {
    "brighter_highlights": ("colorbalancergb", "highlights_luminance"),
    "tame_highlights": ("colorbalancergb", "highlights_luminance"),
    "brighter_midtones": ("colorbalancergb", "midtones_luminance"),
    "darker_midtones": ("colorbalancergb", "midtones_luminance"),
    "deepen_shadows": ("colorbalancergb", "shadows_luminance"),
    "lift_shadows": ("colorbalancergb", "shadows_luminance"),
    "less_chroma": ("colorbalancergb", "global_chroma"),
    "more_chroma": ("colorbalancergb", "global_chroma"),
    "less_saturation": ("colorbalancergb", "global_saturation"),
    "more_saturation": ("colorbalancergb", "global_saturation"),
    "less_vibrance": ("colorbalancergb", "global_vibrance"),
    "more_vibrance": ("colorbalancergb", "global_vibrance"),
    "cool_shadows": ("colorbalancergb", "shadows_chroma"),
    "teal_orange": ("colorbalancergb", "shadows_chroma"),
}


def _applied_delta(obs: dict, module: str, param: str) -> float | None:
    dparam = None
    for c in obs["calls"]:
        env = c.get("envelope") or {}
        data = env.get("data") or {}
        moves = data.get("moves")
        if isinstance(moves, list):
            for m in moves:
                if m.get("module") == module and m.get("param") == param:
                    dparam = m.get("applied")
        elif data.get("module") == module and data.get("param") == param:
            dparam = data.get("applied")
    return dparam


def main() -> None:
    out: dict[str, dict] = {}
    for key, (module, param) in PHRASE_PARAM.items():
        region, stat, _sign = None, None, None
        if key in PHRASE_STAT:
            metric, stat, region, sign = PHRASE_STAT[key]
        else:
            continue
        out[key] = {"module": module, "param": param, "region": region, "stat": stat, "bodies": {}}
        for body in ("olympus", "sony_a7"):
            fp = CASES_DIR / f"cov-phrase-{key}-{body}-bare.json"
            if not fp.exists():
                continue
            d = json.loads(fp.read_text())
            obs = d["observation"]
            sb = obs["stats_before"][region][stat]
            sa = obs["stats_after"][region][stat]
            floor = obs["floors"].get(stat)
            dparam = _applied_delta(obs, module, param)
            if not dparam:
                continue
            dstat = sa - sb
            sens = dstat / dparam
            out[key]["bodies"][body] = {
                "dparam": dparam,
                "dstat": round(dstat, 6),
                "floor": floor,
                "sensitivity": round(sens, 6),
                "case": fp.stem,
            }
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
