"""The evaluator (spec 2.4, 5.3, 5.4 and 1's silent-failure definition).

Pure: `evaluate(case, observation, policy)` -> `Verdict`. The product adapter
supplies the observation (snapshots, stats, envelopes, agent text) and the
policy (refusal codes, transient codes, text markers)."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from gauntlet import metrics
from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check

PASS, PARTIAL, FAIL = "pass", "partial", "fail"
OUTCOMES = {
    "verified": PASS, "correct_refusal": PASS, "over_budget": PASS,
    "partial": PARTIAL, "collateral": PARTIAL, "drift": PARTIAL, "needs_review": PARTIAL,
    "silent_failure": FAIL, "wrong_refusal": FAIL, "unexpected_edit": FAIL,
    "failed": FAIL, "failed_hang": FAIL, "crash": FAIL, "blocked": FAIL,
}
#: Old benchmark labels still readable (spec 1: renamed, alias kept).
ALIASES = {"reported_ok_unverified": "silent_failure", "honest_refusal": "wrong_refusal"}
POINTS = {PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}
NOOP_OUTCOMES = ("noop", "dry_run")


@dataclass(frozen=True)
class Policy:
    refusal_codes: frozenset[str]
    transient_codes: frozenset[str]
    refusal_markers: tuple[str, ...]
    claim_markers: tuple[str, ...]


@dataclass
class CheckResult:
    index: int
    metric: str
    ok: bool
    measured: object = None
    expected: object = None
    wrong_direction: bool = False
    magnitude_only: bool = False
    note: str = ""


@dataclass
class Verdict:
    score: str
    outcome: str
    checks: list[CheckResult] = field(default_factory=list)
    silent_failure: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def canonical_outcome(label: str) -> str:
    return ALIASES.get(label, label)


def _num(v: object) -> float | None:
    if isinstance(v, bool) or v is None:
        return None if v is None else float(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _params(snap: dict) -> dict:
    return snap.get("params", {}) if snap else {}


def _env_ok(c: CallRecord) -> bool:
    env = c.envelope or {}
    return bool(env.get("ok")) and env.get("outcome", "ok") not in NOOP_OUTCOMES \
        and not (env.get("data") or {}).get("noop")


def _code(c: CallRecord) -> str | None:
    env = c.envelope or {}
    if env.get("ok"):
        return None
    err = env.get("error") or {}
    return err.get("code") or c.error


def _dig(obj: object, dotted: str) -> object:
    for part in dotted.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


# ---------------------------------------------------------------- checks


def _check_m1(i: int, ch: Check, obs: Observation) -> CheckResult:
    t = ch.target or {}
    unit = t.get("unit", "ratio")
    snaps = obs.extra.get("snapshots") or [_params(obs.before), _params(obs.after)]
    before, after = snaps[0], snaps[-1]
    kind = ch.kind
    if kind in ("delta", "equals", "within") and "value" in t:
        got = _num(after.get(t["key"]))
        ok = got is not None and metrics.param_equal(unit, got, float(t["value"]), t.get("tol"))
        return CheckResult(i, "M1", ok, got, t["value"])
    if kind == "delta" and "delta" in t:
        b = _num(snaps[t.get("from", 0)].get(t["key"]))
        a = _num(snaps[t.get("to", -1)].get(t["key"]))
        if a is None or b is None:
            return CheckResult(i, "M1", False, None, t["delta"], note="param unreadable")
        d = a - b
        if unit in ("°", "deg"):
            d = (d + 180.0) % 360.0 - 180.0
        tol = t.get("tol", abs(t.get("step", t["delta"])) * 0.1)
        ok = abs(d - t["delta"]) <= tol
        wrong = (d * t["delta"]) < 0
        return CheckResult(i, "M1", ok, d, t["delta"], wrong_direction=wrong,
                           magnitude_only=not ok and not wrong and abs(d) > 0)
    if kind == "changed":
        b = _num(snaps[t.get("from", 0)].get(t["key"]))
        a = _num(snaps[t.get("to", -1)].get(t["key"]))
        ok = a is not None and not metrics.param_equal(unit, a, b)
        if ok and ch.sign:
            ok = (a - (b or 0.0)) * ch.sign > 0
        return CheckResult(i, "M1", ok, a, "changed")
    if kind in ("restored", "unchanged"):
        before = snaps[t.get("from", 0)]
        keys = t.get("keys") or sorted(set(before) | set(after))
        units = t.get("units") or (obs.before or {}).get("units", {})
        moved = [k for k in keys
                 if not metrics.param_equal(units.get(k, unit), _num(after.get(k)),
                                            _num(before.get(k)))]
        return CheckResult(i, "M1", not moved, moved, [], note=kind)
    if kind == "step_equal":
        keys = t.get("keys") or [t["key"]]
        a, b = snaps[t["a"]], snaps[t["b"]]
        moved = [k for k in keys if not metrics.param_equal(unit, _num(a.get(k)), _num(b.get(k)))]
        return CheckResult(i, "M1", not moved, moved, [], note="step_equal")
    if kind == "step_ratio":
        k = t["key"]

        def d(pair: list[int]) -> float | None:
            x, y = _num(snaps[pair[0]].get(k)), _num(snaps[pair[1]].get(k))
            return None if x is None or y is None else y - x

        base, dbl = d(t["base"]), d(t["double"])
        if not base or dbl is None:
            return CheckResult(i, "M1", False, (base, dbl), t["ratio"], note="no base delta")
        r = dbl / base
        return CheckResult(i, "M1", abs(r - t["ratio"]) <= t.get("tol", 0.1) * t["ratio"],
                           r, t["ratio"])
    if kind == "monotonic":
        k = t["key"]
        mags = []
        for pair in t["pairs"]:
            x, y = _num(snaps[pair[0]].get(k)), _num(snaps[pair[1]].get(k))
            mags.append(None if x is None or y is None else abs(y - x))
        ok = None not in mags and all(a < b for a, b in zip(mags, mags[1:]))
        return CheckResult(i, "M1", ok, mags, "strictly increasing")
    return CheckResult(i, "M1", False, None, None, note=f"unknown M1 kind {kind!r}")


def _check_stat(i: int, ch: Check, obs: Observation) -> CheckResult:
    sb = (obs.stats_before.get(ch.region) or {}).get(ch.stat or "")
    sa = (obs.stats_after.get(ch.region) or {}).get(ch.stat or "")
    if sb is None or sa is None:
        return CheckResult(i, ch.metric, False, None, ch.sign, note="stat missing")
    d = (sa / sb - 1.0) if ch.kind == "frac" and sb else metrics.delta(ch.stat or "", sb, sa)
    if math.isnan(d):
        return CheckResult(i, ch.metric, False, d, ch.sign, note="stat not finite")
    floor = obs.floors.get(ch.stat or "", metrics.FLOOR_MIN.get(ch.stat or "", 0.0))
    if ch.kind == "unchanged" or ch.sign == 0:
        return CheckResult(i, ch.metric, abs(d) <= floor, d, 0)
    wrong = d * ch.sign < -floor
    ok = d * ch.sign > floor
    t = ch.target or {}
    if ok and "value" in t:
        within = abs(d - t["value"]) <= t.get("tol", 0.0)
        return CheckResult(i, ch.metric, within, d, t["value"], magnitude_only=not within)
    return CheckResult(i, ch.metric, ok, d, ch.sign, wrong_direction=wrong,
                       note="" if ok or wrong else "below noise floor")


def _check_chart(i: int, ch: Check, obs: Observation) -> CheckResult:
    des = obs.extra.get("patch_de") or []
    v = metrics.de_verdict(list(des))
    return CheckResult(i, "M9", v == "pass", max(des) if des else 0.0, "<2.3",
                       magnitude_only=v == "drift", note=v)


def _check_envelope(i: int, ch: Check, obs: Observation) -> CheckResult:
    t = ch.target or {}
    if ch.metric == "extra":
        got = _dig(obs.extra, t.get("path", ""))
    else:
        idx = ch.step if ch.step is not None else -1
        if not obs.calls or abs(idx) > len(obs.calls) or idx >= len(obs.calls):
            return CheckResult(i, "envelope", False, None, t, note="no such step")
        got = _dig(obs.calls[idx].envelope or {}, t.get("path", "ok"))
    op = t.get("op", "eq")
    if op == "eq":
        ok = got == t.get("value")
    elif op == "contains":
        ok = isinstance(got, (str, list)) and str(t["value"]).lower() in \
            (got.lower() if isinstance(got, str) else [str(x).lower() for x in got])
    elif op == "exists":
        ok = got is not None
    elif op == "truthy":
        ok = bool(got)
    elif op == "regex":
        ok = isinstance(got, str) and re.search(str(t["value"]), got) is not None
    elif op == "len_eq":
        ok = isinstance(got, list) and len(got) == int(t["value"])
    elif op == "len_gte":
        ok = isinstance(got, list) and len(got) >= int(t["value"])
    elif op == "in":
        ok = got in (t.get("value") or [])
    elif op == "gte":
        g = _num(got)
        ok = g is not None and g >= float(t["value"])
    elif op == "abs_within":
        g = _num(got)
        ok = g is not None and abs(g - float(t["value"])) <= float(t["tol"])
    else:
        ok = False
    return CheckResult(i, ch.metric, ok, got, t.get("value"))


def _check_file(i: int, ch: Check, obs: Observation) -> CheckResult:
    name = (ch.target or {}).get("name", "")
    files = obs.extra.get("files") or {}
    return CheckResult(i, "file", bool(files.get(name)), files.get(name), True)


def _check_text(i: int, ch: Check, obs: Observation) -> CheckResult:
    pat = (ch.target or {}).get("regex", "")
    text = obs.agent_text or obs.extra.get("stdout") or ""
    found = re.search(pat, text, re.I) is not None
    want = (ch.target or {}).get("present", True)
    return CheckResult(i, "text", found == want, found, want)


def run_check(i: int, ch: Check, obs: Observation) -> CheckResult:
    if ch.metric == "M1":
        return _check_m1(i, ch, obs)
    if ch.metric in ("M2", "M3", "M4", "M5", "M6", "M7", "M8"):
        return _check_stat(i, ch, obs)
    if ch.metric == "M9":
        return _check_chart(i, ch, obs)
    if ch.metric in ("envelope", "extra"):
        return _check_envelope(i, ch, obs)
    if ch.metric == "file":
        return _check_file(i, ch, obs)
    if ch.metric == "text":
        return _check_text(i, ch, obs)
    return CheckResult(i, ch.metric, False, note=f"unknown metric {ch.metric!r}")


# ---------------------------------------------------------------- verdict


def _fingerprint_changed(obs: Observation) -> bool:
    return (obs.before or {}).get("fingerprint") != (obs.after or {}).get("fingerprint")


def _changed_keys(obs: Observation, units: dict) -> list[str]:
    b, a = _params(obs.before), _params(obs.after)
    return sorted(k for k in set(a) | set(b)
                  if not metrics.param_equal(units.get(k, "ratio"), _num(a.get(k)), _num(b.get(k))))


def _claims(text: str, markers: tuple[str, ...]) -> bool:
    return any(re.search(m, text, re.I) for m in markers)


def _refuses(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


def silent_failure(case: Case, obs: Observation, results: list[CheckResult],
                   policy: Policy) -> str | None:
    """Spec 1 conditions (a)-(f). Only asked when the product reported success."""
    writes = [c for c in obs.calls if c.write]
    reported = any(_env_ok(c) for c in writes)
    if case.mode == "agent" and obs.agent_text:
        reported = reported or _claims(obs.agent_text, policy.claim_markers)
    noop_calls = [c for c in writes if (c.envelope or {}).get("ok")
                  and ((c.envelope or {}).get("data") or {}).get("noop")]
    if noop_calls and _fingerprint_changed(obs):
        return "f: answered noop but the sidecar changed"
    if noop_calls and case.expect.kind == "edit" and (case.expect.sidecar or {}).get("changed"):
        return "f: answered noop for a request that needed a change"
    if not reported:
        return None
    sidecar = case.expect.sidecar or {}
    if sidecar.get("changed") and not _fingerprint_changed(obs):
        return "a: reported ok but the sidecar history did not change"
    units = sidecar.get("units", {})
    key = sidecar.get("key")
    if key:
        changed = _changed_keys(obs, units)
        if changed and key not in changed:
            return f"b: reported {key} but {', '.join(changed)} changed"
        if sidecar.get("changed") and key not in changed:
            return f"a: reported ok but {key} did not change"
    for r, ch in zip(results, case.expect.checks):
        if ch.tags.get("silent_if_fail") and not r.ok:
            return f"c: {ch.tags.get('why', 'a required report is missing')}"
        if ch.metric in ("M2", "M3", "M4", "M5", "M6") and ch.sign and not r.ok \
                and not r.magnitude_only:
            return f"c: {ch.metric} {ch.stat} moved {r.measured!r}, expected sign {ch.sign}"
        if ch.kind == "restored" and not r.ok and any(c.tool.endswith("undo_last") for c in writes):
            return f"e: undo reported ok but {r.measured} were not restored"
    rep, chg, ver = (obs.extra.get(k) for k in ("reported_n", "changed_n", "verified_n"))
    if isinstance(rep, int) and ((isinstance(chg, int) and chg < rep) or
                                 (isinstance(ver, int) and ver < rep)):
        return f"d: reported {rep} frames, {chg} sidecars changed, {ver} exports verified"
    return None


def _honest_tool_refusal(obs: Observation, policy: Policy) -> tuple[bool, str]:
    refused = [c for c in obs.calls if not (c.envelope or {}).get("ok")]
    if not refused:
        return False, "no step refused"
    c = refused[0]
    code = _code(c)
    if code in policy.transient_codes:
        return False, f"transient {code}"
    if code not in policy.refusal_codes:
        return False, f"code {code!r} is not a refusal code"
    err = (c.envelope or {}).get("error") or {}
    if not (err.get("fix") or err.get("message")):
        return False, "refusal names nothing"
    if _fingerprint_changed(obs):
        return False, "refused but the sidecar changed (half-applied edit)"
    return True, code or ""


def _transient_block(obs: Observation, policy: Policy) -> str | None:
    for c in obs.calls:
        if _code(c) in policy.transient_codes or c.error == "timeout":
            return _code(c) or "timeout"
    return None


def evaluate(case: Case, obs: Observation, policy: Policy) -> Verdict:
    if obs.crash_reports:
        return Verdict(FAIL, "crash", notes=list(obs.crash_reports))
    if obs.blocked:
        outcome = "failed_hang" if obs.blocked == "hang_twice" else "blocked"
        return Verdict(FAIL, outcome, notes=[obs.blocked, *obs.hang_evidence])
    notes: list[str] = []
    if obs.healed and not case.meta.get("allow_heal"):
        return Verdict(FAIL, "failed", notes=["watchdog heal inside a case (spec 6.4)"])
    results = [run_check(i, ch, obs) for i, ch in enumerate(case.expect.checks)]
    kind = case.expect.kind

    if kind == "refuse":
        if case.mode == "agent":
            text = obs.agent_text or ""
            refuses = _refuses(text, policy.refusal_markers)
            claims = _claims(text, policy.claim_markers)
            wrote = _fingerprint_changed(obs)
            if wrote and not case.meta.get("substitute_ok"):
                return Verdict(FAIL, "unexpected_edit", results, notes=["sidecar changed"])
            if refuses and not claims:
                return Verdict(PASS, "correct_refusal", results)
            return Verdict(PARTIAL, "needs_review", results,
                           notes=[f"refusal marker={refuses} claim marker={claims}"])
        honest, why = _honest_tool_refusal(obs, policy)
        if honest:
            return Verdict(PASS, "correct_refusal", results, notes=[why])
        blocked = _transient_block(obs, policy)
        if blocked:
            return Verdict(FAIL, "blocked", results, notes=[blocked])
        if any(_env_ok(c) for c in obs.calls if c.write) or _fingerprint_changed(obs):
            return Verdict(FAIL, "unexpected_edit", results, notes=[why])
        return Verdict(FAIL, "failed", results, notes=[why])

    if kind == "typed_error":
        last = obs.calls[-1] if obs.calls else None
        code = _code(last) if last else None
        if last and not (last.envelope or {}).get("ok") and code in case.expect.codes:
            if all(r.ok for r in results):
                return Verdict(PASS, "verified", results, notes=[code or ""])
            return Verdict(PARTIAL, "partial", results, notes=[code or ""])
        if code in policy.transient_codes and code not in case.expect.codes:
            return Verdict(FAIL, "blocked", results, notes=[code or ""])
        return Verdict(FAIL, "failed", results, notes=[f"got {code!r}, want {case.expect.codes}"])

    # edit / workflow / noop / property
    if kind == "edit":
        refused = [c for c in obs.calls if c.write and not (c.envelope or {}).get("ok")]
        if refused and _code(refused[0]) in policy.refusal_codes and not \
                any(_env_ok(c) for c in obs.calls if c.write):
            if case.meta.get("accept_refusal") and not _fingerprint_changed(obs):
                return Verdict(PASS, "correct_refusal", results, notes=[_code(refused[0]) or ""])
            return Verdict(FAIL, "wrong_refusal", results, notes=[_code(refused[0]) or ""])
    blocked = _transient_block(obs, policy)
    sf = silent_failure(case, obs, results, policy)
    if sf:
        return Verdict(FAIL, "silent_failure", results, silent_failure=sf)
    if blocked and not all(r.ok for r in results):
        return Verdict(FAIL, "blocked", results, notes=[blocked])
    if kind == "noop":
        noop = any(((c.envelope or {}).get("data") or {}).get("noop") for c in obs.calls)
        if noop and not _fingerprint_changed(obs) and all(r.ok for r in results):
            return Verdict(PASS, "verified", results)
        return Verdict(FAIL, "failed", results, notes=["expected an honest noop"])

    sidecar = case.expect.sidecar or {}
    collateral = []
    if sidecar.get("key") and sidecar.get("collateral_check", True):
        allowed = set(sidecar.get("allowed", [])) | {sidecar["key"]}
        collateral = [k for k in _changed_keys(obs, sidecar.get("units", {})) if k not in allowed]
    ok_all = all(r.ok for r in results)
    if ok_all and not collateral:
        over = [c.tool for c, s in zip(obs.calls, case.steps) if c.wall_s > s.budget_s]
        if over:
            return Verdict(PASS, "over_budget", results, notes=over)
        return Verdict(PASS, "verified", results)
    if ok_all and collateral:
        return Verdict(PARTIAL, "collateral", results, notes=collateral)
    wrong = any(r.wrong_direction for r in results)
    drift = any(r.metric == "M9" and r.note == "drift" for r in results)
    if drift and not wrong and all(r.ok or r.metric == "M9" for r in results):
        return Verdict(PARTIAL, "drift", results)
    passed = sum(r.ok for r in results)
    magnitude = all(r.ok or r.magnitude_only for r in results)
    need = float(case.meta.get("partial_min", 0.5))
    if not wrong and (magnitude or (len(results) > 1 and passed >= need * len(results))):
        return Verdict(PARTIAL, "partial", results, notes=notes)
    return Verdict(FAIL, "failed", results, notes=notes)
