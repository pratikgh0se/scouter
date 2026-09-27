"""The evaluator (spec 2.4, 5.3, 5.4 and 1's silent-failure definition).

Pure: `evaluate(case, observation, policy)` -> `Verdict`. The product adapter
supplies the observation (snapshots, stats, envelopes, agent text) and the
policy (refusal codes, transient codes, text markers).

Product param knowledge rides on the before/after snapshot dicts next to
`units` (all optional, all supplied by the adapter; the engine never imports
the product):
  `defaults`   {key: module default}; a key the adapter does not know has no
               default and never compares equal to an absent module.
  `derived`    {reported key: [stored keys it is decoded from]}; a move reported
               on the key owns those keys (e.g. a Kelvin read from WB coeffs).
  `null_equiv` {key: value}; an undecodable reading (None) of the key and this
               value mean the same thing (e.g. an unused coefficient).

Two equalities, never mixed (night-1 fix, bug 2):
  "did it land?"   `_same`: numeric within the M1 tolerance (spec 5.1).
  "did it change?" `_differs`: a strict float32-safe epsilon, so a one-notch
                   move smaller than the landed tolerance still counts."""

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
    #: Row 475 (I2 ruling, provisional -- Pratik to confirm): a plain `ok`
    #: move that landed exactly the requested (unclamped) step, whose only
    #: failing check reads "below noise floor", is neither `verified`
    #: (the oracle cannot see it), `correct_refusal` (nothing was refused --
    #: it moved) nor `silent_failure` (the product claimed nothing it did
    #: not do). Its own outcome, its own report column.
    "imperceptible_step": PARTIAL,
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


def is_hang(outcome: str | None, blocked: str | None) -> bool:
    """(final fix I6): the ONE definition of a hang, shared by the report and
    the progress page: a failed_hang verdict, the runner's own hard_cap, or an
    adapter's "hang..." blocked reason."""
    b = blocked or ""
    return canonical_outcome(outcome or "") == "failed_hang" or b == "hard_cap" \
        or b.startswith("hang")


def canonical_outcome(label: str) -> str:
    return ALIASES.get(label, label)


def _num(v: object) -> float | None:
    if isinstance(v, bool) or v is None:
        return None if v is None else float(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _same(unit: str, a: object, b: object, tol: float | None = None) -> bool:
    """Param equality: numeric within the M1 tolerance, anything else exact.
    NaN reads as missing (None), and None equals only None (fix round 1, I4)."""
    a = None if isinstance(a, float) and math.isnan(a) else a
    b = None if isinstance(b, float) and math.isnan(b) else b
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return metrics.param_equal(unit, na, nb, tol)
    return a == b


def _params(snap: dict) -> dict:
    return snap.get("params", {}) if snap else {}


#: "Did it change?" epsilon: float32 carries ~7 significant digits, so a
#: stored value round-trips within ~6e-8 relative; 1e-6 abs + 1e-6 rel is well
#: above that noise and far below any real step (spec 5.1 steps >= 0.005).
CHANGE_ABS, CHANGE_REL = 1e-6, 1e-6


def _meta(obs: Observation, name: str) -> dict:
    """Adapter-supplied param knowledge on the snapshots (see module docstring)."""
    for snap in (obs.before, obs.after):
        v = (snap or {}).get(name)
        if isinstance(v, dict):
            return v
    return {}


def _units(obs: Observation, extra: dict | None = None) -> dict:
    return {**((obs.before or {}).get("units") or {}), **(extra or {})}


def _differs(unit: str, a: object, b: object, null_equiv: object = None) -> bool:
    """Strict change test (night-1 fix, bug 2): numeric beyond a float32-safe
    epsilon (hue wrap-aware), anything else by inequality. NaN reads as None;
    `null_equiv` is a value that an undecodable (None) reading also means."""
    a = None if isinstance(a, float) and math.isnan(a) else a
    b = None if isinstance(b, float) and math.isnan(b) else b
    if null_equiv is not None:
        a = null_equiv if a is None else a
        b = null_equiv if b is None else b
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        d = abs(na - nb)
        if unit in ("°", "deg"):
            d = abs((na - nb + 180.0) % 360.0 - 180.0)
        return d > CHANGE_ABS + CHANGE_REL * max(abs(na), abs(nb))
    return a != b


def _diff_keys(b: dict, a: dict, units: dict, null_equiv: dict) -> list[str]:
    return sorted(k for k in set(a) | set(b)
                  if _differs(units.get(k, "ratio"), a.get(k), b.get(k), null_equiv.get(k)))


def _module(key: str) -> str:
    return key.split(".", 1)[0]


def _module_absent(params: dict, module: str) -> bool:
    return not any(v is not None for k, v in params.items() if _module(k) == module)


def _module_at_default(params: dict, module: str, defaults: dict) -> bool:
    """The module is in the history enabled with every key at its known default:
    equivalent to the module being absent. An unknown default is a mismatch."""
    keys = [k for k in params if _module(k) == module]
    if not keys:
        return False
    for k in keys:
        v = params[k]
        if k.endswith(".__enabled"):
            if v != 1:
                return False
        elif k not in defaults or _differs("ratio", v, defaults[k]):
            return False
    return True


def _absent_equiv(before: dict, after: dict, key: str, defaults: dict) -> bool:
    """None-before vs a value after for a key whose module was absent before and
    is at its defaults after (night-1 fix, bug 3)."""
    m = _module(key)
    return before.get(key) is None and after.get(key) is not None \
        and _module_absent(before, m) and _module_at_default(after, m, defaults)


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
        got = after.get(t["key"])
        ok = got is not None and _same(unit, got, t["value"], t.get("tol"))
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
        b = snaps[t.get("from", 0)].get(t["key"])
        a = snaps[t.get("to", -1)].get(t["key"])
        ok = a is not None and _differs(unit, a, b, _meta(obs, "null_equiv").get(t["key"]))
        if ok and ch.sign:
            na, nb = _num(a), _num(b)
            ok = na is not None and (na - (nb or 0.0)) * ch.sign > 0
        return CheckResult(i, "M1", ok, a, "changed")
    if kind in ("restored", "unchanged"):
        before = snaps[t.get("from", 0)]
        keys = t.get("keys") or sorted(set(before) | set(after))
        # a derived key is restored only when the stored keys it is decoded
        # from are (bug 4): an undecodable Kelvin (None == None) proves nothing
        keys = list(keys) + sorted(_owned(list(keys), obs) - set(keys))
        units = t.get("units") or (obs.before or {}).get("units", {})
        defaults, null_equiv = _meta(obs, "defaults"), _meta(obs, "null_equiv")

        def eq(k: str) -> bool:
            a, b = after.get(k), before.get(k)
            if k in null_equiv:
                a = null_equiv[k] if a is None else a
                b = null_equiv[k] if b is None else b
            return _same(units.get(k, unit), a, b) or _absent_equiv(before, after, k, defaults)

        moved = [k for k in keys if not eq(k)]
        return CheckResult(i, "M1", not moved, moved, [], note=kind)
    if kind == "step_equal":
        keys = t.get("keys") or [t["key"]]
        a, b = snaps[t["a"]], snaps[t["b"]]
        moved = [k for k in keys if not _same(unit, a.get(k), b.get(k))]
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
    if ch.kind == "frac" and sb:
        floor = floor / abs(sb)  # a fractional delta meets a fractional floor (fix round 1, I3)
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
    if not des:
        return CheckResult(i, "M9", False, None, "<2.3", note="unmeasured")
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


def _check_boundary(i: int, ch: Check, obs: Observation) -> CheckResult:
    """Soft-range boundary probe (2026-09-26 fix pass, item 2): pass when the
    decoded value landed exactly where it was asked (no clamp needed) OR it
    landed at the declared bound AND the envelope's `data.clamped` says so.
    `target["request"]` is an absolute value; `target["delta"]` resolves
    against the case's own before-snapshot (a phrase's `by N`, whose start
    point the generator does not know) with `target["lo"]`/`target["hi"]`
    giving the bound to clamp toward. Landing anywhere else -- including
    at the bound without reporting it -- fails."""
    t = ch.target or {}
    unit = t.get("unit", "ratio")
    tol = t.get("tol")
    snaps = obs.extra.get("snapshots") or [_params(obs.before), _params(obs.after)]
    before, after = snaps[0], snaps[-1]
    key_ = t["key"]
    got = after.get(key_)
    if got is None:
        return CheckResult(i, "boundary", False, None, t, note="param unreadable")
    if "request" in t:
        requested = t["request"]
    else:
        b = _num(before.get(key_))
        if b is None:
            return CheckResult(i, "boundary", False, got, t, note="before unreadable")
        requested = b + t["delta"]
    bound = t.get("bound")
    if bound is None and t.get("lo") is not None and t.get("hi") is not None:
        bound = min(max(requested, t["lo"]), t["hi"])
    landed_request = _same(unit, got, requested, tol)
    landed_bound = bound is not None and _same(unit, got, bound, tol)
    idx = ch.step if ch.step is not None else -1
    clamped = False
    if obs.calls and -len(obs.calls) <= idx < len(obs.calls):
        clamped = bool(_dig(obs.calls[idx].envelope or {}, "data.clamped"))
    ok = landed_request or (landed_bound and clamped)
    return CheckResult(i, "boundary", ok, got, {"request": requested, "bound": bound})


def _check_file(i: int, ch: Check, obs: Observation) -> CheckResult:
    name = (ch.target or {}).get("name", "")
    files = obs.extra.get("files")
    if files is None:
        return CheckResult(i, "file", False, None, True, note="unmeasured")
    return CheckResult(i, "file", bool(files.get(name)), files.get(name), True)


def _check_text(i: int, ch: Check, obs: Observation) -> CheckResult:
    pat = (ch.target or {}).get("regex", "")
    if obs.agent_text is None and obs.extra.get("stdout") is None:
        return CheckResult(i, "text", False, None, (ch.target or {}).get("present", True),
                           note="unmeasured")
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
    if ch.metric == "boundary":
        return _check_boundary(i, ch, obs)
    if ch.metric == "file":
        return _check_file(i, ch, obs)
    if ch.metric == "text":
        return _check_text(i, ch, obs)
    return CheckResult(i, ch.metric, False, note=f"unknown metric {ch.metric!r}")


# ---------------------------------------------------------------- verdict


def _fingerprint_changed(obs: Observation) -> bool:
    return (obs.before or {}).get("fingerprint") != (obs.after or {}).get("fingerprint")


def _changed_keys(obs: Observation, units: dict) -> list[str]:
    """Keys whose value changed before -> after (strict epsilon, bug 2)."""
    return _diff_keys(_params(obs.before), _params(obs.after), _units(obs, units),
                      _meta(obs, "null_equiv"))


def _step_snaps(obs: Observation) -> list[dict] | None:
    """Per-step snapshots aligned with obs.calls (snaps[i + 1] is the state after
    call i), or None when the adapter recorded none or they do not line up."""
    snaps = obs.extra.get("snapshots")
    if isinstance(snaps, list) and len(snaps) == len(obs.calls) + 1:
        return snaps
    return None


def _step_changed(obs: Observation, snaps: list[dict], i: int, units: dict) -> bool:
    """Did call i itself change params (snaps[i] -> snaps[i + 1], strict)?"""
    return bool(_diff_keys(snaps[i] or {}, snaps[i + 1] or {}, _units(obs, units),
                           _meta(obs, "null_equiv")))


def _owned(keys: list[str], obs: Observation) -> set[str]:
    """Reported keys plus the stored keys each is derived from (bug 4)."""
    derived = _meta(obs, "derived")
    out = set(keys)
    for k in keys:
        out.update(derived.get(k) or ())
    return out


def _reported_keys(writes: list[CallRecord], every: bool = False) -> list[str] | None:
    """The module.param keys the last ok write's envelope reports moving, or
    with `every` the union over every ok write that names something (night-1
    fix, bug 2: rule (b) must not call a change reported by an earlier write
    of a multi-write case "a different param").

    A multi-move envelope (darkroom_say) reports its moves' module.param,
    noop and uncommitted moves excluded; a single-move one its data.module/data.param.
    `data.key` is never read as a param: darkroom_say puts the PHRASE key
    there ("warmer"), which is not a module.param (smoke S1-A). An envelope
    that names something but no param gives [] (skip rule (b)); one that
    names nothing gives None (the case's sidecar key may stand in)."""
    out: set[str] | None = None
    for c in (writes if every else reversed(writes)):
        if not _env_ok(c):
            continue
        data = (c.envelope or {}).get("data") or {}
        moves = data.get("moves")
        if isinstance(moves, list) and moves:
            found = {f"{m['module']}.{m['param']}" for m in moves
                     if isinstance(m, dict) and not m.get("noop")
                     and m.get("committed") is not False
                     and isinstance(m.get("module"), str)
                     and isinstance(m.get("param"), str)}
        elif isinstance(data.get("module"), str) and isinstance(data.get("param"), str):
            found = {f"{data['module']}.{data['param']}"}
        elif "key" in data:
            found = set()
        elif every:
            continue
        else:
            return None
        out = found if out is None else out | found
        if not every:
            break
    return None if out is None else sorted(out)


def _all_moves_noop(c: CallRecord) -> bool:
    """An ok envelope whose moves list is non-empty and every move answered noop."""
    moves = ((c.envelope or {}).get("data") or {}).get("moves")
    return (_env_ok(c) and isinstance(moves, list) and bool(moves)
            and all(isinstance(m, dict) and m.get("noop") for m in moves))


def _m1_key_unchanged(case: Case, obs: Observation) -> str | None:
    """An M1 target key that a reported-ok write should have moved but did not."""
    snaps = obs.extra.get("snapshots") or [_params(obs.before), _params(obs.after)]
    for ch in case.expect.checks:
        t = ch.target or {}
        if ch.metric != "M1" or ch.kind not in ("equals", "within", "delta", "changed") \
                or "key" not in t:
            continue
        try:
            b, a = snaps[t.get("from", 0)], snaps[t.get("to", -1)]
        except (IndexError, TypeError):
            continue
        k, unit = t["key"], t.get("unit", "ratio")
        if k not in b and k not in a:
            continue
        if "value" in t and _same(unit, b.get(k), t["value"], t.get("tol")):
            continue  # the request asked for the value it already had
        if "delta" in t and not t["delta"]:
            continue
        if not _differs(unit, a.get(k), b.get(k), _meta(obs, "null_equiv").get(k)):
            return k
    return None


def _claims(text: str, markers: tuple[str, ...]) -> bool:
    return any(re.search(m, text, re.I) for m in markers)


def _refuses(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


def _is_noop(c: CallRecord) -> bool:
    env = c.envelope or {}
    return bool(env.get("ok")) and bool((env.get("data") or {}).get("noop"))


def _noop_rule(case: Case, obs: Observation, units: dict) -> str | None:
    """Spec 1 (f), attributed per step (night-1 fix, bug 1). With per-step
    snapshots a noop answer is a silent failure only when ITS OWN step changed
    params; without them only when every write answered noop and the sidecar
    changed. "Noop for a request that needed a change" fires only when no write
    in the case produced the change."""
    idx = [i for i, c in enumerate(obs.calls) if c.write]
    noop = [i for i in idx if _is_noop(obs.calls[i])]
    allnoop = [i for i in idx if _all_moves_noop(obs.calls[i])]
    snaps = _step_snaps(obs)
    silent = set(noop) | set(allnoop)
    whole = _fingerprint_changed(obs) or bool(_changed_keys(obs, units))
    if snaps is not None:
        noop_changed = [i for i in noop if _step_changed(obs, snaps, i, units)]
        allnoop_changed = [i for i in allnoop if _step_changed(obs, snaps, i, units)]
        produced = any(_step_changed(obs, snaps, i, units) for i in idx if i not in silent)
    else:
        every = bool(idx) and set(idx) <= silent
        noop_changed = noop if every and _fingerprint_changed(obs) else []
        allnoop_changed = allnoop if every and whole else []
        produced = whole and any(i not in silent for i in idx)
    if noop_changed:
        return "f: answered noop but the sidecar changed"
    if noop and not produced and case.expect.kind == "edit" and \
            (case.expect.sidecar or {}).get("changed"):
        return "f: answered noop for a request that needed a change"
    if allnoop_changed:
        return "f: every move answered noop but the sidecar changed"
    return None


def _moved_to_limit_claims(obs: Observation) -> list[dict]:
    """Every {module, param, cap} claim from a `moved_to_limit` write's own
    envelope data (row 471): a single `darkroom.adjust`'s own
    module/param/cap, or a `darkroom.say`'s `moved_to_limit` list. Review
    round 1 (I1): `moved_to_limit` is a CLAIM, exactly like a plain `ok` --
    it is verified against the after snapshot below, never taken on its
    own word."""
    claims: list[dict] = []
    for c in obs.calls:
        if not c.write:
            continue
        env = c.envelope or {}
        if env.get("outcome") != "moved_to_limit":
            continue
        data = env.get("data") or {}
        mtl = data.get("moved_to_limit")
        if isinstance(mtl, list):
            claims.extend(m for m in mtl if isinstance(m, dict))
        elif data.get("module") and data.get("param"):
            claims.append({"module": data.get("module"), "param": data.get("param"),
                           "cap": data.get("cap")})
    return claims


def _any_moved_to_limit(obs: Observation) -> bool:
    """Row 471: any write call whose own envelope `outcome` is the typed
    partial `moved_to_limit` (scouter/core/moves.py `_adjust_core`/`say`)
    -- a move that CLAIMS it genuinely moved but stopped at its own
    declared clamp. Keys off the outcome itself, not whether the claim's
    module/param/cap happen to be parseable (review round 1, I1: an
    envelope can say `moved_to_limit` with data the evaluator cannot read
    -- that is still a claim needing verification, not a free pass to
    skip rules (a)-(f) the way an unrecognised envelope shape would).
    Used only to decide whether the claim needs verifying (`reported`
    below) -- never, on its own, to score a verdict."""
    return any((c.envelope or {}).get("outcome") == "moved_to_limit"
               for c in obs.calls if c.write)


def _verified_clamped_params(obs: Observation, units: dict) -> set[str]:
    """Review round 1 (I1): a `moved_to_limit` claim only counts once the
    after snapshot shows that `module.param` actually landed at its
    claimed `cap` (within the M1 tolerance) and differs from `before` --
    an unverified claim of movement is exactly what silent-failure rule
    (a) catches for a plain `ok`, and `moved_to_limit` earns no exemption
    from that. Returns the `module.param` keys that verify, so a
    below-floor stat check can be matched to the ONE clamped param it is
    actually about (probe P3/P4, and the mixed clamped+plain-move case)."""
    before, after = _params(obs.before), _params(obs.after)
    verified: set[str] = set()
    for claim in _moved_to_limit_claims(obs):
        module, param, cap = claim.get("module"), claim.get("param"), claim.get("cap")
        if not module or not param or cap is None:
            continue
        key_ = f"{module}.{param}"
        unit = units.get(key_, "")
        a = _num(after.get(key_))
        if a is None or not _same(unit, a, cap):
            continue
        b = _num(before.get(key_))
        if b is not None and not _differs(unit, b, a):
            continue
        verified.add(key_)
    return verified


def _writes_landed_as_requested(obs: Observation, units: dict) -> bool:
    """Row 475 (I2 ruling), tightened by review round 2 (R1): every
    non-noop write's claimed `module.param` (`AdjustResult`'s own
    `module`/`param`/`requested`, flat on a single `darkroom.adjust`'s
    `data`, or per-entry in a `darkroom.say`'s `data.moves` list) must
    actually show `requested` in the AFTER snapshot -- never just the
    envelope's own `applied` field, which is only the product's word for
    it (the exact gap `_verified_clamped_params` already closes for a
    `moved_to_limit` claim; the same XMP check applies to a plain-ok claim
    of a full, unclamped step). It must also differ from `before`: a
    request that claims it moved but the XMP shows unchanged is a genuine
    silent failure, caught elsewhere -- this only excuses a real, verified,
    merely-too-small-to-measure move."""
    before, after = _params(obs.before), _params(obs.after)
    any_move = False
    for c in obs.calls:
        if not c.write:
            continue
        data = (c.envelope or {}).get("data") or {}
        moves = data["moves"] if isinstance(data.get("moves"), list) else [data]
        for m in moves:
            if not isinstance(m, dict) or m.get("noop"):
                continue
            module, param, requested = m.get("module"), m.get("param"), m.get("requested")
            if not module or not param or requested is None:
                return False
            key_ = f"{module}.{param}"
            unit = units.get(key_, m.get("unit", ""))
            a = _num(after.get(key_))
            if a is None or not _same(unit, a, requested):
                return False
            b = _num(before.get(key_))
            if b is not None and not _differs(unit, b, a):
                return False
            any_move = True
    return any_move


def _imperceptible_step_residual(case: Case, results: list[CheckResult],
                                 obs: Observation) -> Verdict | None:
    """Row 475 (I2 ruling, option A, provisional -- Pratik to confirm): the
    real night-1 D-bare-step-below-perceptual-floor rows are plain `ok`,
    never `moved_to_limit` (I2's finding -- KNOWN_RESIDUAL's "cap" is the
    fixture's step-size invariant, not a runtime clamp any of these moves
    ever reach). When every non-passing check on an `edit` case reads
    "below noise floor" and every write's own claimed module.param
    VERIFIABLY landed at its own requested, unclamped step in the after
    snapshot (`_writes_landed_as_requested`, review round 2 R1), that is a
    real, complete edit the check floor simply cannot see -- scored its
    own outcome, `imperceptible_step` (never verified, refused, or
    silent), tagged `residual: step-below-floor` so the report can name it
    and carry the recalibration card."""
    if case.expect.kind != "edit" or _any_moved_to_limit(obs):
        return None
    non_passing = [r for r in results if not r.ok]
    if not non_passing or any(r.note != "below noise floor" for r in non_passing):
        return None
    sidecar = case.expect.sidecar or {}
    if not _writes_landed_as_requested(obs, sidecar.get("units", {})):
        return None
    return Verdict(PARTIAL, "imperceptible_step", results, notes=["residual: step-below-floor"])


def _moved_to_limit_residual(case: Case, results: list[CheckResult],
                             obs: Observation) -> Verdict | None:
    """Row 471 (COV-D-residual-clamp-capped, Pratik's ruling), tightened by
    review round 1 (I1): score `correct_refusal` only when every
    non-passing check reads "below noise floor" AND names (via
    `Check.tags["clamp_param"]`) a `module.param` this observation actually
    verified as clamped (`_verified_clamped_params`) -- a below-floor check
    that belongs to some OTHER, unclamped move must not ride along on an
    unrelated clamp. Tagged `residual: clamp` so the report can name it
    (gauntlet/report.py)."""
    if case.expect.kind != "edit":
        return None
    sidecar = case.expect.sidecar or {}
    verified = _verified_clamped_params(obs, sidecar.get("units", {}))
    if not verified:
        return None
    non_passing = [(r, ch) for r, ch in zip(results, case.expect.checks) if not r.ok]
    if not non_passing:
        return None
    for r, ch in non_passing:
        if r.note != "below noise floor" or ch.tags.get("clamp_param") not in verified:
            return None
    return Verdict(PASS, "correct_refusal", results, notes=["residual: clamp"])


def silent_failure(case: Case, obs: Observation, results: list[CheckResult],
                   policy: Policy) -> str | None:
    """Spec 1 conditions (a)-(f). Only asked when the product reported success."""
    writes = [c for c in obs.calls if c.write]
    # Review round 1 (I1): `moved_to_limit` is a claim of movement, exactly
    # like a plain `ok` -- it must not short-circuit `reported` to False
    # (registry.py's `ok = outcome in ("ok", "dry_run")` makes the envelope
    # itself `ok: False`) and skip rules (a)-(f) entirely. `_any_moved_to_limit`
    # only says "something claimed a clamped move"; whether that claim
    # actually verifies is rule (c)'s job below, via `_verified_clamped_params`.
    reported = any(_env_ok(c) for c in writes) or _any_moved_to_limit(obs)
    # Row 459 (night-1, COV-undo-first-introduction): rule (e) below must ask
    # the undo call's OWN ok/refusal, not "was any write ok" -- an earlier ok
    # `set` (introducing the module) must not make an honestly-refused
    # `undo_last` look like a silent failure.
    undo_reported_ok = any(_env_ok(c) for c in writes if c.tool.endswith("undo_last"))
    if case.mode == "agent" and obs.agent_text:
        reported = reported or _claims(obs.agent_text, policy.claim_markers)
    sidecar = case.expect.sidecar or {}
    units = sidecar.get("units", {})
    rule_f = _noop_rule(case, obs, units)
    if rule_f:
        return rule_f
    if not reported:
        return None
    if sidecar.get("changed") and not _fingerprint_changed(obs):
        return "a: reported ok but the sidecar history did not change"
    fallback = [sidecar["key"]] if sidecar.get("key") else []
    keys = _reported_keys(writes)
    keys = fallback if keys is None else keys
    every = _reported_keys(writes, every=True)
    every = fallback if every is None else every
    changed = _changed_keys(obs, units)
    if every and changed and not _owned(every, obs) & set(changed):
        return f"b: reported {', '.join(every)} but {', '.join(changed)} changed"
    if keys:
        named = ", ".join(keys)
        owned = _owned(keys, obs)
        if sidecar.get("changed") and not owned & set(changed):
            return f"a: reported ok but {named} did not change"
        # per key: every committed, non-noop move the snapshots can see must have moved
        seen = set(_params(obs.before)) | set(_params(obs.after))
        unmoved_keys = [k for k in keys if k in seen and k not in changed]
        if sidecar.get("changed") and unmoved_keys:
            return f"a: reported ok but {', '.join(unmoved_keys)} did not change"
    unmoved = _m1_key_unchanged(case, obs)
    if unmoved:
        return f"a: reported ok but {unmoved} did not change"
    # Row 471 (COV-D-residual-clamp-capped), tightened by review round 1
    # (I1): a stat check that reads "below noise floor" because the move
    # VERIFIABLY stopped at its own declared clamp (`_verified_clamped_params`
    # -- the after snapshot actually shows that module.param at its claimed
    # cap) is not a silent bug -- it is the clamp being honest about its own
    # limit. An unverified `moved_to_limit` claim, or a below-floor check
    # that belongs to some OTHER, unclamped move (`Check.tags["clamp_param"]`
    # not in the verified set), earns no exception: rule (c) still fires.
    #
    # Row 475 (I2 ruling): a plain `ok` (never claimed a clamp at all) whose
    # every write VERIFIABLY landed exactly its own requested step in the
    # after snapshot (review round 2, R1) also earns the exception --
    # `_imperceptible_step_residual` (below `evaluate()`) scores its own
    # outcome for it; here we only must not let rule (c) call it a silent
    # bug first. Review round 2 (R2): scoped to `edit` cases only, the one
    # kind `_imperceptible_step_residual` ever scores -- a `workflow` case
    # (or any other kind) keeps its ordinary rule (c) scoring, not a silent
    # exception with nothing to label it.
    verified_clamped = _verified_clamped_params(obs, units)
    landed_as_requested = case.expect.kind == "edit" and not _any_moved_to_limit(obs) \
        and _writes_landed_as_requested(obs, units)
    for r, ch in zip(results, case.expect.checks):
        if ch.tags.get("silent_if_fail") and not r.ok:
            return f"c: {ch.tags.get('why', 'a required report is missing')}"
        if ch.metric in ("M2", "M3", "M4", "M5", "M6") and ch.sign and not r.ok \
                and not r.magnitude_only:
            if r.note == "below noise floor" and (
                ch.tags.get("clamp_param") in verified_clamped or landed_as_requested
            ):
                continue
            return f"c: {ch.metric} {ch.stat} moved {r.measured!r}, expected sign {ch.sign}"
        if ch.kind == "restored" and not r.ok and undo_reported_ok:
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


def _sidecar_miss(case: Case, obs: Observation) -> str | None:
    """`expect.sidecar` as an implicit check (fix round 1, C1)."""
    sc = case.expect.sidecar or {}
    moved = _fingerprint_changed(obs)
    if sc.get("changed") is True and not moved:
        return "sidecar expected to change but did not"
    if sc.get("changed") is False and moved:
        return "sidecar expected unchanged but changed"
    key = sc.get("key")
    if key and sc.get("changed") and \
            not _owned([key], obs) & set(_changed_keys(obs, sc.get("units", {}))):
        return f"sidecar {key} expected to change but did not"
    return None


def _refuse_write_failure(case: Case, results: list[CheckResult]) -> Verdict | None:
    """On a refuse case that wrote, the checks decide (fix round 1, I1)."""
    for r, ch in zip(results, case.expect.checks):
        if ch.tags.get("silent_if_fail") and not r.ok:
            why = ch.tags.get("why", "a required report is missing")
            return Verdict(FAIL, "silent_failure", results, silent_failure=f"c: {why}")
    if any(not r.ok for r in results):
        return Verdict(FAIL, "failed", results, notes=["substitute edit does not match its checks"])
    return None


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
            if wrote:
                bad = _refuse_write_failure(case, results)
                if bad:
                    return bad
                if not results:
                    return Verdict(PARTIAL, "needs_review", results,
                                   notes=["substitute unverifiable: no checks"])
            if claims and not refuses and not wrote:
                return Verdict(FAIL, "silent_failure", results,
                               silent_failure="a: claimed the edit was made but the sidecar "
                                              "did not change")
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
        sf = silent_failure(case, obs, results, policy)
        if sf:
            return Verdict(FAIL, "silent_failure", results, silent_failure=sf)
        if _fingerprint_changed(obs):
            bad = _refuse_write_failure(case, results)
            if bad and bad.outcome == "silent_failure":
                return bad
            return Verdict(FAIL, "unexpected_edit", results, notes=[why])
        if any(_env_ok(c) for c in obs.calls if c.write):
            return Verdict(FAIL, "silent_failure", results,
                           silent_failure="a: reported ok on a refused request but the sidecar "
                                          "did not change")
        return Verdict(FAIL, "failed", results, notes=[why])

    if kind == "typed_error":
        last = obs.calls[-1] if obs.calls else None
        code = _code(last) if last else None
        if last and not (last.envelope or {}).get("ok") and code in case.expect.codes:
            if _fingerprint_changed(obs):
                return Verdict(FAIL, "failed", results,
                               notes=[f"half-applied: sidecar changed before {code}"])
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
            if case.meta.get("accept_refusal"):
                honest, why = _honest_tool_refusal(obs, policy)
                if honest:
                    return Verdict(PASS, "correct_refusal", results, notes=[why])
                return Verdict(FAIL, "failed", results, notes=[why])
            return Verdict(FAIL, "wrong_refusal", results, notes=[_code(refused[0]) or ""])
    blocked = _transient_block(obs, policy)
    sf = silent_failure(case, obs, results, policy)
    if sf:
        return Verdict(FAIL, "silent_failure", results, silent_failure=sf)
    #: Review round 2, finding 3 (GD-2, install config-version-assert): a
    #: Check tagged {"needs_review": "<reason>"} opts a case's disputed
    #: assertion out of the ordinary partial-credit threshold below, which
    #: would otherwise score a genuine product failure elsewhere in the same
    #: case 0.5 and label it the same as an assertion still awaiting a
    #: decision. An untagged check failing is still a real failure --
    #: "failed", never softened. A tagged check failing, with every
    #: untagged ("hard") check passing, is "needs_review" with the tag's own
    #: reason text as the note. All checks passing falls through unchanged
    #: (usually "verified" below) -- the tag can neither lift a failure nor
    #: drag a pass down.
    review = [(r, ch) for r, ch in zip(results, case.expect.checks) if ch.tags.get("needs_review")]
    if review:
        hard = [r for r, ch in zip(results, case.expect.checks) if not ch.tags.get("needs_review")]
        if not all(r.ok for r in hard):
            return Verdict(FAIL, "failed", results,
                           notes=[f"check {r.index}: {r.note}" for r in hard if not r.ok])
        pending = [ch.tags["needs_review"] for r, ch in review if not r.ok]
        if pending:
            return Verdict(PARTIAL, "needs_review", results, notes=pending)
    miss = _sidecar_miss(case, obs) if kind != "noop" else None
    if not miss and not blocked:
        residual = _moved_to_limit_residual(case, results, obs)
        if residual:
            return residual
        imperceptible = _imperceptible_step_residual(case, results, obs)
        if imperceptible:
            return imperceptible
    if blocked and (miss or not all(r.ok for r in results)):
        return Verdict(FAIL, "blocked", results, notes=[blocked])
    if kind == "noop":
        noop = any(((c.envelope or {}).get("data") or {}).get("noop") for c in obs.calls)
        if noop and not _fingerprint_changed(obs) and all(r.ok for r in results):
            return Verdict(PASS, "verified", results)
        return Verdict(FAIL, "failed", results, notes=["expected an honest noop"])

    if miss:
        return Verdict(FAIL, "failed", results, notes=[miss])
    if kind == "edit" and not results and not case.expect.sidecar:
        return Verdict(PARTIAL, "needs_review", results, notes=["unverifiable: no checks"])
    sidecar = case.expect.sidecar or {}
    collateral = []
    if sidecar.get("key") and sidecar.get("collateral_check", True):
        allowed = _owned(list(sidecar.get("allowed", [])) + [sidecar["key"]], obs)
        before = _params(obs.before)
        # a module absent before that the reported write introduced brings its
        # sibling keys with it (night-1 fix, bug 3)
        introduced = {_module(k) for k in allowed if _module_absent(before, _module(k))}
        collateral = [k for k in _changed_keys(obs, sidecar.get("units", {}))
                      if k not in allowed
                      and not (before.get(k) is None and _module(k) in introduced)]
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
