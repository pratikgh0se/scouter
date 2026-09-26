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


#: Kinds a `blocked` observation can fall into, in the fixed order S8 reports them.
BLOCKED_KINDS = ("hard_cap", "hang", "session_open", "session", "harness_error", "other")


def _blocked(r: dict) -> str | None:
    return (r.get("observation") or {}).get("blocked")


def _blocked_kind(blocked: str | None) -> str | None:
    if not blocked:
        return None
    if blocked == "hard_cap":
        return "hard_cap"
    if blocked.startswith("hang"):
        return "hang"
    if blocked.startswith("session_open"):
        return "session_open"
    if blocked.startswith("session"):
        return "session"
    if blocked.startswith("harness_error"):
        return "harness_error"
    return "other"


def _is_hang(r: dict) -> bool:
    """(fix round 1, I1): failed_hang only ever comes from blocked == 'hang_twice';
    the runner's own hard_cap timeout, and an adapter's 'hang: ...' blocked
    reason, are hangs too and must not read as 0."""
    blocked = _blocked(r) or ""
    return _outcome(r) == "failed_hang" or blocked == "hard_cap" or blocked.startswith("hang")


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
            #: (fix round 1, I5): a declared gate nobody tagged (n=0) is not
            #: vacuously ok -- it blocks the bar and is shown "n=0".
            gates.append({"tag": key, "values": list(values), "min": minimum, "rate": rate,
                          "n": len(tagged), "ok": rate is not None and rate >= minimum})
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
                     #: (fix round 1, I1): hang membership by outcome-or-blocked, not
                     #: outcome alone -- see `_is_hang`.
                     "hang": _is_hang(r),
                     "triage": None, "triage_classes": list(TRIAGE_CLASSES)})

    def rank(x: dict) -> int:
        if x["outcome"] == "silent_failure":
            return 0
        if x["outcome"] == "crash":
            return 1
        if x["hang"]:
            return 2
        return 3

    rows.sort(key=lambda x: (rank(x), x["tier"], x["layer"], x["id"]))
    return rows


def build(run: RunDir, adapter: Adapter, cases: list[Case], results: list[dict],
          meta: dict | None = None) -> dict:
    meta = meta or {}
    #: (fix round 1, I3): missing/valid by id, not by count -- a stale orphan
    #: result (an id no longer in cases.jsonl) must not mask a case that never
    #: ran, and every stat below is computed only over results that still
    #: name a real case.
    case_ids = {c.id for c in cases}
    result_ids = {r["id"] for r in results}
    valid = [r for r in results if r["id"] in case_ids]
    missing_ids = sorted(case_ids - result_ids)
    orphan_ids = sorted(result_ids - case_ids)

    layers = layer_table(valid, adapter)
    silent = [r["id"] for r in valid if _outcome(r) == "silent_failure"]
    #: (fix round 1, I2): per-case crash reports (attributed to the case
    #: whose window they fell in) union'd with run-level reports -- a crash on
    #: session open/close or the final close belongs to no case and was
    #: previously dropped. `meta["crash_reports_run"]` lets the caller supply
    #: the run-level list directly; otherwise ask the adapter for everything
    #: since the earliest case in this report started.
    crashes_percase = [x for r in valid for x in (r.get("observation") or {})
                       .get("crash_reports", [])]
    started = [r["started_at"] for r in valid if r.get("started_at") is not None]
    earliest = min(started) if started else 0.0
    crashes_run = (meta["crash_reports_run"] if "crash_reports_run" in meta
                   else adapter.crash_reports_since(earliest))
    crashes = list(dict.fromkeys([*crashes_percase, *crashes_run]))
    #: (fix round 1, I1): a hang is failed_hang OR blocked == hard_cap OR a
    #: "hang..." blocked reason -- see `_is_hang`.
    hangs = [{"id": r["id"], "blocked": _blocked(r),
             "evidence": (r.get("observation") or {}).get("hang_evidence", [])}
            for r in valid if _is_hang(r)]
    blocked_counts = dict.fromkeys(BLOCKED_KINDS, 0)
    for r in valid:
        kind = _blocked_kind(_blocked(r))
        if kind:
            blocked_counts[kind] += 1
    p0 = [r for r in valid if r["tier"] == "P0"]
    p0_total = sum(c.tier == "P0" for c in cases)
    p0_pass = sum(r["verdict"]["score"] == "pass" for r in p0)
    served = sum(_outcome(r) in SERVED for r in p0)
    refused = sum(_outcome(r) == "correct_refusal" for r in p0)
    missing = len(missing_ids)
    gates_ok = all(row["p1_ok"] and all(g["ok"] for g in row["check_gates"]) for row in layers)
    #: (Task 28 fix round 1, I1): an adapter may name product sign-off items
    #: that fail the bar (e.g. originals changed); optional, default none.
    blockers_fn = getattr(adapter, "signoff_blockers", None)
    blockers = list(blockers_fn(run)) if blockers_fn else []
    signoff = {
        "crashes": len(crashes), "silent_failures": len(silent), "hangs": len(hangs),
        #: (fix round 1, I4): zero P0 cases must not read as a met bar.
        "p0": f"{p0_pass}/{p0_total}", "p0_all_pass": p0_total > 0 and p0_pass == p0_total,
        "p1_gates_ok": gates_ok, "cases_missing": missing, "missing_ids": missing_ids,
        "orphan_result_ids": orphan_ids, "blocked": blocked_counts,
        "release_bar_met": not crashes and not silent and not hangs and p0_total > 0
        and p0_pass == p0_total and gates_ok and missing == 0 and not blockers,
        "blockers": blockers,
        "contact_sheet_reviewed": "[ ] (Pratik ticks by hand)",
        "lines": adapter.signoff_lines(run),
    }
    return {
        "schema": "gauntlet-report/1", "stamp": run.stamp, "adapter": adapter.name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "meta": meta,
        "S8_signoff": signoff,
        "S1_headline": {"cases": len(cases), "ran": len(valid),
                        "pass": sum(r["verdict"]["score"] == "pass" for r in valid),
                        "partial": sum(r["verdict"]["score"] == "partial" for r in valid),
                        "fail": sum(r["verdict"]["score"] == "fail" for r in valid),
                        "p0_served": served, "p0_refused": refused, "p0_total": p0_total},
        "S2_layers": layers,
        "S3_non_pass": fix_list(valid),
        "S5_timing": timing(valid),
        "S6_crashes_hangs": {"crash_reports": crashes, "hangs": hangs},
        "S7_roadmap": roadmap(valid),
    }


def markdown(rep: dict) -> str:
    s8, s1 = rep["S8_signoff"], rep["S1_headline"]
    L = [f"# Gauntlet report {rep['stamp']} ({rep['adapter']})", "",
         "## S8 Sign-off", "",
         f"- Release bar met: **{'YES' if s8['release_bar_met'] else 'NO'}**",
         f"- Crashes: {s8['crashes']}", f"- Silent failures: {s8['silent_failures']}",
         f"- Hangs: {s8['hangs']}",
         "- Blocked: " + ", ".join(f"{k}={v}" for k, v in s8["blocked"].items()),
         f"- P0: {s8['p0']}", f"- P1 gates: {'ok' if s8['p1_gates_ok'] else 'NOT met'}",
         f"- Cases missing (not run): {s8['cases_missing']}",
         *[f"  - `{i}`" for i in s8["missing_ids"]],
         f"- Orphan result files (stale, not in cases.jsonl): {len(s8['orphan_result_ids'])}",
         *[f"  - `{i}`" for i in s8["orphan_result_ids"]],
         f"- Adapter blockers: {len(s8.get('blockers', []))}",
         *[f"  - {b}" for b in s8.get("blockers", [])],
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
        for g in r["check_gates"]:
            if g["ok"]:
                continue
            got = "n=0" if g["n"] == 0 else f"{100 * g['rate']:.1f}%"
            L.append(f"  - gate NOT met: {r['layer']} {g['tag']}={g['values']} "
                     f"min={100 * g['min']:.0f}% got {got} (n={g['n']})")
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
          f"- Hangs: {len(rep['S6_crashes_hangs']['hangs'])}",
          *[f"  - `{h['id']}` ({h['blocked']}): {'; '.join(str(e) for e in h['evidence'])}"
            for h in rep["S6_crashes_hangs"]["hangs"]], "",
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
