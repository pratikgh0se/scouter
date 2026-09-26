"""The report (spec 2.5): report.md + report.json (S8 sign-off first, then
S1-S7), contact-sheet.html, and the fix list for the morning loop."""

# ruff: noqa: N806
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone

from gauntlet.adapter import Adapter
from gauntlet.case import Case, dumps
from gauntlet.evaluate import POINTS, canonical_outcome
from gauntlet.store import RunDir, atomic_write_text

SERVED = ("verified", "over_budget")
TRIAGE_CLASSES = ("product_bug", "harness_bug", "taxonomy_error", "vocab_gap", "upstream",
                  "accepted")


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return round(s[k], 3)


def _outcome(r: dict) -> str:
    return canonical_outcome(r["verdict"]["outcome"])


def layer_table(results: list[dict], adapter: Adapter) -> list[dict]:
    rows = []
    for spec in sorted(adapter.layers(), key=lambda s: s.order):
        rs = [r for r in results if r["layer"] == spec.name]
        n = len(rs)
        pts = sum(POINTS[r["verdict"]["score"]] for r in rs)
        p0 = [r for r in rs if r["tier"] == "P0"]
        p1 = [r for r in rs if r["tier"] == "P1"]
        p1_rate = (sum(POINTS[r["verdict"]["score"]] for r in p1) / len(p1)) if p1 else None
        gates = []
        for key, values, minimum in spec.check_gates:
            tagged = [c for r in rs for c, t in _tagged_checks(r) if t.get(key) in values]
            rate = (sum(1 for c in tagged if c["ok"]) / len(tagged)) if tagged else None
            gates.append({"tag": key, "values": list(values), "min": minimum, "rate": rate,
                          "n": len(tagged), "ok": rate is None or rate >= minimum})
        rows.append({
            "layer": spec.name, "cases": n, "points": pts,
            "rate": round(pts / n, 4) if n else None,
            "pass": sum(r["verdict"]["score"] == "pass" for r in rs),
            "partial": sum(r["verdict"]["score"] == "partial" for r in rs),
            "fail": sum(r["verdict"]["score"] == "fail" for r in rs),
            "p0_total": len(p0), "p0_pass": sum(r["verdict"]["score"] == "pass" for r in p0),
            "p1_rate": None if p1_rate is None else round(p1_rate, 4),
            "p1_gate": spec.p1_gate,
            "p1_ok": p1_rate is None or spec.p1_gate is None or p1_rate >= spec.p1_gate,
            "check_gates": gates,
        })
    return rows


def _tagged_checks(r: dict):
    case_checks = r.get("meta", {}).get("check_tags") or []
    for i, c in enumerate(r["verdict"].get("checks", [])):
        tags = case_checks[i] if i < len(case_checks) else {}
        yield c, tags


def timing(results: list[dict]) -> dict:
    per: dict[str, list[float]] = {}
    over: list[str] = []
    for r in results:
        for c in r.get("observation", {}).get("calls", []):
            per.setdefault(f"{r['layer']}:{c['tool']}", []).append(float(c.get("wall_s") or 0))
        if _outcome(r) == "over_budget":
            over.append(r["id"])
    return {"percentiles": {k: {"n": len(v), "p50": _pct(v, .5), "p90": _pct(v, .9),
                                "p99": _pct(v, .99)} for k, v in sorted(per.items())},
            "over_budget": over}


def roadmap(results: list[dict]) -> list[dict]:
    """Spec 7 demand score: requests + 3 x P0 + agent refusals + look gaps / 10."""
    keys: dict[str, dict] = {}
    for r in results:
        meta = r.get("meta") or {}
        out = _outcome(r)
        for gap in meta.get("gaps") or []:
            k = keys.setdefault(gap, {"requests": set(), "p0": set(), "agent": 0, "gaps": 0})
            k["gaps"] += 1
        key = meta.get("roadmap_key")
        if not key or out not in ("correct_refusal", "wrong_refusal", "needs_review"):
            continue
        k = keys.setdefault(key, {"requests": set(), "p0": set(), "agent": 0, "gaps": 0})
        req = (r.get("source") or {}).get("id")
        if r["mode"] == "agent":
            k["agent"] += 1
        elif req:
            k["requests"].add(req)
            if r["tier"] == "P0":
                k["p0"].add(req)
    rows = [{"key": key, "requests": len(v["requests"]), "p0": len(v["p0"]),
             "agent_refusals": v["agent"], "look_gaps": v["gaps"],
             "score": round(len(v["requests"]) + 3 * len(v["p0"]) + v["agent"] + v["gaps"] / 10, 1)}
            for key, v in keys.items()]
    return sorted(rows, key=lambda x: (-x["score"], x["key"]))


def fix_list(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        if r["verdict"]["score"] == "pass":
            continue
        out = _outcome(r)
        rows.append({"id": r["id"], "layer": r["layer"], "tier": r["tier"], "mode": r["mode"],
                     "outcome": out, "score": r["verdict"]["score"],
                     "silent_failure": r["verdict"].get("silent_failure"),
                     "notes": r["verdict"].get("notes", [])[:3], "repro": r["repro"],
                     "triage": None, "triage_classes": list(TRIAGE_CLASSES)})
    rank = {"silent_failure": 0, "crash": 1, "failed_hang": 2}
    rows.sort(key=lambda x: (rank.get(x["outcome"], 3), x["tier"], x["layer"], x["id"]))
    return rows


def build(run: RunDir, adapter: Adapter, cases: list[Case], results: list[dict],
          meta: dict | None = None) -> dict:
    meta = meta or {}
    layers = layer_table(results, adapter)
    silent = [r["id"] for r in results if _outcome(r) == "silent_failure"]
    crashes = [x for r in results for x in r["observation"].get("crash_reports", [])]
    hangs = [r["id"] for r in results if _outcome(r) == "failed_hang"]
    p0 = [r for r in results if r["tier"] == "P0"]
    p0_total = sum(c.tier == "P0" for c in cases)
    p0_pass = sum(r["verdict"]["score"] == "pass" for r in p0)
    served = sum(_outcome(r) in SERVED for r in p0)
    refused = sum(_outcome(r) == "correct_refusal" for r in p0)
    missing = len(cases) - len(results)
    gates_ok = all(row["p1_ok"] and all(g["ok"] for g in row["check_gates"]) for row in layers)
    signoff = {
        "crashes": len(crashes), "silent_failures": len(silent),
        "p0": f"{p0_pass}/{p0_total}", "p0_all_pass": p0_total > 0 and p0_pass == p0_total,
        "p1_gates_ok": gates_ok, "cases_missing": missing,
        "release_bar_met": not crashes and not silent and p0_pass == p0_total and gates_ok
        and missing == 0,
        "contact_sheet_reviewed": "[ ] (Pratik ticks by hand)",
        "lines": adapter.signoff_lines(),
    }
    return {
        "schema": "gauntlet-report/1", "stamp": run.stamp, "adapter": adapter.name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "meta": meta,
        "S8_signoff": signoff,
        "S1_headline": {"cases": len(cases), "ran": len(results),
                        "pass": sum(r["verdict"]["score"] == "pass" for r in results),
                        "partial": sum(r["verdict"]["score"] == "partial" for r in results),
                        "fail": sum(r["verdict"]["score"] == "fail" for r in results),
                        "p0_served": served, "p0_refused": refused, "p0_total": p0_total},
        "S2_layers": layers,
        "S3_non_pass": fix_list(results),
        "S5_timing": timing(results),
        "S6_crashes_hangs": {"crash_reports": crashes, "hangs": hangs},
        "S7_roadmap": roadmap(results),
    }


def markdown(rep: dict) -> str:
    s8, s1 = rep["S8_signoff"], rep["S1_headline"]
    L = [f"# Gauntlet report {rep['stamp']} ({rep['adapter']})", "",
         "## S8 Sign-off", "",
         f"- Release bar met: **{'YES' if s8['release_bar_met'] else 'NO'}**",
         f"- Crashes: {s8['crashes']}", f"- Silent failures: {s8['silent_failures']}",
         f"- P0: {s8['p0']}", f"- P1 gates: {'ok' if s8['p1_gates_ok'] else 'NOT met'}",
         f"- Cases missing (not run): {s8['cases_missing']}",
         f"- Contact sheet reviewed: {s8['contact_sheet_reviewed']}",
         *[f"- {x}" for x in s8["lines"]], "",
         "## S1 Headline", "",
         f"{s1['ran']}/{s1['cases']} cases: {s1['pass']} pass, {s1['partial']} partial, "
         f"{s1['fail']} fail.", "",
         f"**P0 served rate:** {s1['p0_served']} verified edits, {s1['p0_refused']} honest "
         f"refusals, of {s1['p0_total']} P0 cases (a headline, not a gate).", "",
         "## S2 Layers", "",
         "| Layer | Cases | Rate | Pass | Partial | Fail | P0 | P1 rate | Gate |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["S2_layers"]:
        rate = "-" if r["rate"] is None else f"{100 * r['rate']:.1f}%"
        p1 = "-" if r["p1_rate"] is None else f"{100 * r['p1_rate']:.1f}%"
        gate = "ok" if r["p1_ok"] and all(g["ok"] for g in r["check_gates"]) else "**NOT met**"
        L.append(f"| {r['layer']} | {r['cases']} | {rate} | {r['pass']} | {r['partial']} | "
                 f"{r['fail']} | {r['p0_pass']}/{r['p0_total']} | {p1} | {gate} |")
    L += ["", "## S3 Every non-pass", "", "| Case | Tier | Outcome | Why | Repro |",
          "|---|---|---|---|---|"]
    for f in rep["S3_non_pass"]:
        why = f["silent_failure"] or "; ".join(str(n) for n in f["notes"])
        L.append(f"| `{f['id']}` | {f['tier']} | {f['outcome']} | {why[:120]} | "
                 f"`{f['repro']}` |")
    L += ["", "## S4 Contact sheet", "", "See `contact-sheet.html` in the run directory.", "",
          "## S5 Timing", "", "| Layer:tool | n | p50 | p90 | p99 |", "|---|---|---|---|---|"]
    for k, v in rep["S5_timing"]["percentiles"].items():
        L.append(f"| {k} | {v['n']} | {v['p50']} | {v['p90']} | {v['p99']} |")
    L += ["", f"Over budget (pass, listed): {len(rep['S5_timing']['over_budget'])}", "",
          "## S6 Crashes and hangs", "",
          f"- Crash reports: {len(rep['S6_crashes_hangs']['crash_reports'])}",
          *[f"  - `{x}`" for x in rep["S6_crashes_hangs"]["crash_reports"]],
          f"- Hangs: {len(rep['S6_crashes_hangs']['hangs'])}", "",
          "## S7 Honest refusals as the roadmap", "",
          "| Key | Score | Requests | P0 | Agent | Look gaps |", "|---|---|---|---|---|---|"]
    for r in rep["S7_roadmap"]:
        L.append(f"| {r['key']} | {r['score']} | {r['requests']} | {r['p0']} | "
                 f"{r['agent_refusals']} | {r['look_gaps']} |")
    return "\n".join(L) + "\n"


def contact_sheet(results: list[dict]) -> str:
    tiles = []
    for r in results:
        prev = (r.get("observation", {}).get("extra") or {}).get("previews") or {}
        if not prev:
            continue
        cap = html.escape(f"{r['id']} - {_outcome(r)}")
        imgs = "".join(f'<img src="{html.escape(prev[k])}" alt="{k}" loading="lazy">'
                       for k in ("before", "after") if prev.get(k))
        tiles.append(f'<figure class="{html.escape(r["verdict"]["score"])}">{imgs}'
                     f"<figcaption>{cap}</figcaption></figure>")
    return ("<!doctype html><meta charset='utf-8'><title>Contact sheet</title><style>"
            "body{font-family:system-ui;margin:16px;background:#fff;color:#111}"
            "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}}"
            "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}"
            "figure{margin:0;border:2px solid #999;padding:4px}figure.fail{border-color:#c33}"
            "figure.partial{border-color:#c90}img{width:50%}figcaption{font-size:12px}"
            "</style><main>" + "".join(tiles) + "</main>\n")


def write(run: RunDir, adapter: Adapter, meta: dict | None = None) -> dict:
    cases = run.read_cases()
    results = run.read_results()
    rep = build(run, adapter, cases, results, meta)
    atomic_write_text(run.report_json, json.dumps(rep, indent=2, sort_keys=True) + "\n")
    atomic_write_text(run.report_md, markdown(rep))
    atomic_write_text(run.contact_sheet, contact_sheet(results))
    fixes = rep["S3_non_pass"]
    atomic_write_text(run.fixlist_json, dumps(fixes) + "\n")
    atomic_write_text(run.fixlist_md, "\n".join(
        [f"# Fix list {run.stamp}", "", "Triage each row as one of: "
         + ", ".join(TRIAGE_CLASSES) + ".", "", "| Case | Tier | Outcome | Repro | Triage |",
         "|---|---|---|---|---|"]
        + [f"| `{f['id']}` | {f['tier']} | {f['outcome']} | `{f['repro']}` | |" for f in fixes])
        + "\n")
    return rep
