"""The evaluator (spec 2.4, 5.3, 5.4 and 1's silent-failure definition).

Pure: `evaluate(case, observation, policy)` -> `Verdict`. The product adapter
supplies the observation (snapshots, stats, envelopes, agent text) and the
policy (refusal codes, transient codes, text markers).

Product param knowledge rides on the before/after snapshot dicts next to
`units` (all optional, all supplied by the adapter; the engine never imports
the product):
  `defaults`   {key: module default}; a key the adapter does not know has no
               default and never compares equal to an absent module.
  `verified_defaults` {key: module default} -- the SUBSET of `defaults` a real
               host check has established (review round 1, I1; never a
               product "no effect" belief). `_m1_default_fallback` reads only
               this narrower table, never the wider `defaults`.
  `derived`    {reported key: [stored keys it is decoded from]}; a move reported
               on the key owns those keys (e.g. a Kelvin read from WB coeffs).
  `null_equiv` {key: value}; an undecodable reading (None) of the key and this
               value mean the same thing (e.g. an unused coefficient).

Two equalities, never mixed (night-1 fix, bug 2):
  "did it land?"   `_same`: numeric within the M1 tolerance (spec 5.1).
  "did it change?" `_differs`: a strict float32-safe epsilon, so a one-notch
                   move smaller than the landed tolerance still counts."""

from __future__ import annotations

import dataclasses
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
    #: `evaluate()`'s `out_of_scope_policy` branch returns this outcome for
    #: a write before the user picks between offered options, distinct from
    #: the generic `unexpected_edit` every other refuse-kind write earns.
    "unrequested_edit": FAIL,
    "failed": FAIL, "failed_hang": FAIL, "crash": FAIL, "blocked": FAIL,
}
#: Old benchmark labels still readable (spec 1: renamed, alias kept).
ALIASES = {"reported_ok_unverified": "silent_failure", "honest_refusal": "wrong_refusal"}
POINTS = {PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}
NOOP_OUTCOMES = ("noop", "dry_run")

#: Zenkai Rules Z4 (docs/ZENKAI.md): every name a `Policy.flags` entry may
#: carry, added here in the SAME change that adds the `evaluate()` branch
#: reading it. `tests/gauntlet/policy.py::load_policy` refuses any
#: check_map.json "flags" entry whose name is not in this set, and refuses
#: (Z4: "reason, rule named, fix card, expiry") one missing a reason, a
#: card or an expiry -- a flag is a scoring BYPASS, so it needs the same
#: override record any other Zenkai exception does, never a bare name.
KNOWN_FLAGS = frozenset({"parity.same_failure_is_pass"})


@dataclass(frozen=True)
class Policy:
    refusal_codes: frozenset[str]
    transient_codes: frozenset[str]
    refusal_markers: tuple[str, ...]
    claim_markers: tuple[str, ...]
    #: Named, provisional overrides (row 488 pending Pratik), ACTIVE names
    #: only -- everything else (reason/card/expiry, Z4) lives in
    #: check_map.json's own "flags" entries and is checked by
    #: `tests/gauntlet/policy.py::load_policy` before a name ever lands
    #: here. A flag's mere PRESENCE turns it on; a flag never in this set
    #: is off -- so a fresh Policy (an empty set, the dataclass default)
    #: behaves exactly as before any flag existed. One flag is known today:
    #: "parity.same_failure_is_pass" (see `evaluate`'s PAR-hollow-pass
    #: check; the full name list is `KNOWN_FLAGS` above).
    flags: frozenset[str] = frozenset()
    #: Phrases that show a refusal named at least one alternative (the
    #: "refuse AND offer" shape). Defaulted so every other caller building
    #: a Policy by hand keeps working unchanged.
    offer_markers: tuple[str, ...] = ()


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
    adapter's "hang..." blocked reason.

    (night1-robustness-triage.md item 2): `inherited_wedge` is deliberately
    NOT a hang -- it is a case that started after darktable was already
    wedged by an EARLIER case's own hang (rob-r12..r15's shape, all inherited
    from rob-r11's single true hang). It falls through to `blocked` below
    (still a FAIL) without ever matching `b.startswith("hang")`."""
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
    NaN reads as missing (None), and None equals only None."""
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


def _m1_default_fallback(key: str, unit: str, after: dict, verified_defaults: dict,
                          obs: Observation) -> float | None:
    """M1's declared-default fallback (decision rows 548, 554): a scored key
    absent from `after` reads as the module's own declared default IF (a)
    the key has a REAL, host-probed default (`verified_defaults`, vocab's
    `default` field only -- never `neutral`, which is only the product's own
    "no effect" belief, not a measurement of darktable; review round 1, I1)
    and (b) the module carries no history entry in `after` at all -- together
    those mean the default is what darktable actually applies, not a guess.

    A second oracle (Z2) must then agree before the fallback is trusted: the
    product's own envelope needs EVERY call matching this key (review round
    1, I2 -- not just the last one, which a real, unverified write earlier in
    the same case could hide behind) to report `ok` and a `noop` whose
    `applied` equals the default (the sidecar side of that same evidence --
    "no history entry" -- is condition (b) already). Any disagreement -- a
    history entry in `after`, no matching call, a call that is not `ok`, did
    not report noop, or whose `applied` does not match -- fails loudly:
    returns None, same as the key staying absent today."""
    if key not in verified_defaults:
        return None
    module = _module(key)
    if not _module_absent(after, module):
        return None
    default_val = verified_defaults[key]
    param = key.split(".", 1)[1] if "." in key else key
    matched = [c for c in obs.calls
               if (c.args or {}).get("module") == module and (c.args or {}).get("param") == param]
    if not matched:
        return None
    for c in matched:
        env = c.envelope or {}
        data = env.get("data") or {}
        if not env.get("ok") or not data.get("noop"):
            return None
        applied = _num(data.get("applied"))
        if applied is None or not _same(unit, applied, default_val):
            return None
    return default_val


def _first_move_previous(obs: Observation, key: str) -> float | None:
    """The `previous` the FIRST envelope move for `key` reports (a later move's
    `previous` is the earlier writes' landing, not the case-start state)."""
    module, _, param = key.partition(".")
    for c in obs.calls:
        moves = ((c.envelope or {}).get("data") or {}).get("moves")
        for m in moves if isinstance(moves, list) else ():
            if isinstance(m, dict) and m.get("module") == module and m.get("param") == param:
                return _num(m.get("previous"))
    return None


def _before_value(snap: dict, key: str, unit: str, obs: Observation) -> tuple[float | None, str]:
    """A scored key's value in a `before`-side snapshot, or the module's
    DECLARED default when the snapshot shows the module ABSENT (night 3: a frame
    re-staged clean has no history entry for the module, so every key reads
    None -- which is the module's default, not "unreadable").

    Same trust as M1's declared-default fallback: only `verified_defaults` (the
    vocab's real, host-probed `default`, never `neutral`), only when the
    snapshot itself is readable (it holds values for OTHER modules, so an empty
    or failed read never counts as "absent") and the module has no value in it
    at all. A second oracle (Z2) must not disagree: the product's own first
    reported `previous` for the key, when it reports one, must equal that
    default. Anything else returns (None, "") exactly as before: unreadable."""
    b = _num(snap.get(key)) if snap else None
    if b is not None:
        return b, ""
    verified = _meta(obs, "verified_defaults")
    if not snap or key not in verified or not any(v is not None for v in snap.values()):
        return None, ""
    if not _module_absent(snap, _module(key)):
        return None, ""
    default = _num(verified[key])
    if default is None:
        return None, ""
    prev = _first_move_previous(obs, key)
    if prev is not None and not _same(unit, prev, default):
        return None, ""
    return default, "declared_default_before"


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
        note = ""
        if got is None:
            got = _m1_default_fallback(t["key"], unit, after, _meta(obs, "verified_defaults"), obs)
            if got is not None:
                note = "declared_default"
        ok = got is not None and _same(unit, got, t["value"], t.get("tol"))
        return CheckResult(i, "M1", ok, got, t["value"], note=note)
    if kind == "delta" and "delta" in t:
        b, b_note = _before_value(snaps[t.get("from", 0)], t["key"], unit, obs)
        a = _num(snaps[t.get("to", -1)].get(t["key"]))
        if a is None or b is None:
            return CheckResult(i, "M1", False, None, t["delta"], note="param unreadable")
        d = a - b
        if unit in ("°", "deg"):
            d = (d + 180.0) % 360.0 - 180.0
        tol = t.get("tol", abs(t.get("step", t["delta"])) * 0.1)
        ok = abs(d - t["delta"]) <= tol
        if not ok and _delta_clamp_credited(t, b, a, tol, obs, ch.step):
            return CheckResult(i, "M1", True, d, t["delta"], note="clamped_at_bound")
        if not ok and _delta_unreported_clamp(t, b, a, tol, obs, ch.step):
            return CheckResult(i, "M1", False, d, t["delta"], note="unreported_clamp",
                               magnitude_only=True)
        wrong = (d * t["delta"]) < 0
        return CheckResult(i, "M1", ok, d, t["delta"], wrong_direction=wrong,
                           magnitude_only=not ok and not wrong and abs(d) > 0, note=b_note)
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
            x, _ = _before_value(snaps[pair[0]], k, unit, obs)
            y = _num(snaps[pair[1]].get(k))
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
        floor = floor / abs(sb)  # a fractional delta meets a fractional floor
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


def _boundary_clamped(obs: Observation, idx: int, key_: str) -> bool:
    """Was `key_` (module.param) reported clamped by call `idx`? A single
    `darkroom_set`/`darkroom_adjust` envelope carries a flat `data.clamped`;
    a `darkroom_say` envelope nests it per move in `data.moves[j].clamped`
    (also summarised in `data.moved_to_limit`) -- both shapes must be read."""
    if not (obs.calls and -len(obs.calls) <= idx < len(obs.calls)):
        return False
    data = (obs.calls[idx].envelope or {}).get("data") or {}
    if data.get("clamped"):
        return True
    for m in data.get("moves") or ():
        if isinstance(m, dict) and m.get("clamped") and \
                f"{m.get('module')}.{m.get('param')}" == key_:
            return True
    return any(isinstance(m, dict) and f"{m.get('module')}.{m.get('param')}" == key_
              for m in data.get("moved_to_limit") or ())


def _delta_clamp_credited(t: dict, b: float, a: float, tol: float, obs: Observation,
                          step: int | None) -> bool:
    """A relative move that stops at a declared bound AND says so is correct.
    Oracle 1: the committed value equals the bound (`lo`/`hi` in the target)
    that the full ask would overshoot. Oracle 2: the call's envelope reports
    the clamp (`_boundary_clamped`). A stop short of the bound, or a landing
    at the bound with no clamp reported, is not credited."""
    lo, hi = t.get("lo"), t.get("hi")
    if lo is None or hi is None:
        return False
    bound = min(max(b + t["delta"], lo), hi)
    return abs(a - bound) <= tol and _boundary_clamped(obs, -1 if step is None else step, t["key"])


def _delta_unreported_clamp(t: dict, b: float, a: float, tol: float, obs: Observation,
                            step: int | None) -> bool:
    """N1: the mirror of `_delta_clamp_credited`. A relative move whose full
    ask overshoots a declared bound, that LANDED on that bound (the product
    clamped), whose own call answered a plain ok (not a noop, so an honest
    `soft_limit` noop never matches) with no clamp reported, is a silent
    failure: the user was told "done" and got a different value. Oracle 1: the
    committed value equals the bound the ask overshoots AND moved off the
    before value. Oracle 2: the call's envelope reports no clamp."""
    lo, hi = t.get("lo"), t.get("hi")
    if lo is None or hi is None:
        return False
    want = b + t["delta"]
    bound = min(max(want, lo), hi)
    if abs(want - bound) <= tol or abs(a - bound) > tol or abs(a - b) <= tol:
        return False
    idx = -1 if step is None else step
    if not (obs.calls and -len(obs.calls) <= idx < len(obs.calls)):
        return False
    return _env_ok(obs.calls[idx]) and not _boundary_clamped(obs, idx, t["key"])


def _check_boundary(i: int, ch: Check, obs: Observation) -> CheckResult:
    """Soft-range boundary probe (2026-09-26 fix pass, item 2): pass when the
    decoded value landed exactly where it was asked (no clamp needed) OR it
    landed at the declared bound AND the envelope reports the clamp, per
    move (`_boundary_clamped`). `target["request"]` is an absolute value;
    `target["delta"]` resolves against the case's own before-snapshot (a
    phrase's `by N`, whose start point the generator does not know) with
    `target["lo"]`/`target["hi"]` giving the bound to clamp toward. Landing
    anywhere else -- including at the bound without reporting it -- fails."""
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
        b, _ = _before_value(before, key_, unit, obs)
        if b is None:
            return CheckResult(i, "boundary", False, got, t, note="before unreadable")
        requested = b + t["delta"]
    bound = t.get("bound")
    if bound is None and t.get("lo") is not None and t.get("hi") is not None:
        bound = min(max(requested, t["lo"]), t["hi"])
    landed_request = _same(unit, got, requested, tol)
    landed_bound = bound is not None and _same(unit, got, bound, tol)
    idx = ch.step if ch.step is not None else -1
    clamped = _boundary_clamped(obs, idx, key_)
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


def _check_verb(i: int, ch: Check, obs: Observation) -> CheckResult:
    """"Did the agent actually call the tool this workflow verb needs" --
    not "was the agent's LAST recorded call, whatever it was, ok". A
    clarify-only conversation whose last call is any successful READ
    (`scout_stats`, `library_jobs`, `darkroom_describe_history`, ...) must
    not pass just because that read succeeded. `target["any_of"]` names
    the envelope `verb` ids this
    workflow verb accepts (e.g. `cull.propose`, `export.release`); scans
    every call, not only the last one. `target["require_write"]` is a
    looser variant for a verb with no single canonical tool (repeat_more):
    any successful WRITE call (CallRecord.write, from WRITE_TOOLS) counts."""
    t = ch.target or {}
    if t.get("require_write"):
        for c in obs.calls:
            if c.write and (c.envelope or {}).get("ok"):
                return CheckResult(i, "verb", True, c.tool, "any write call ok")
        return CheckResult(i, "verb", False, None, "any write call ok",
                           note="no successful write call")
    verbs = set(t.get("any_of") or [])
    for c in obs.calls:
        env = c.envelope or {}
        if env.get("verb") in verbs and env.get("ok"):
            return CheckResult(i, "verb", True, env.get("verb"), sorted(verbs))
    return CheckResult(i, "verb", False, None, sorted(verbs), note="no matching verb call")


#: The envelope `verb` id a genuine undo call reports
#: (`scouter/core/darkroom.py`'s `id="darkroom.undo_last"`).
UNDO_VERB = "darkroom.undo_last"


def _check_undo(i: int, ch: Check, obs: Observation) -> CheckResult:
    """An undo case is verified when the agent made a SUCCESSFUL undo call
    (`darkroom_undo_last`, verb `darkroom.undo_last`, envelope `ok`) AND
    history_end DECREMENTED, OR the snapshotted params are restored to
    their before-value -- not by a sidecar change, which a fresh frame
    with nothing of the agent's own to undo may never produce
    (`darkroom_undo_last` on an empty journal is an honest noop, not a
    failure to detect). Either signal alone is enough (OR, not AND with a
    `changed: True` sidecar expectation), but only once a real undo call
    is on the record -- without one, before/after snapshots are trivially
    equal to themselves (zero calls, or only a successful READ), which
    would otherwise score a hollow pass. This check fails then;
    `evaluate()`'s workflow needs_review fallback (no verb/undo call and
    no non-noop write at all) turns that into `needs_review`, never a
    silent pass and never a hard fail either."""
    undo_called = any(
        (c.envelope or {}).get("ok") and (c.envelope or {}).get("verb") == UNDO_VERB
        for c in obs.calls
    )
    if not undo_called:
        return CheckResult(i, "undo", False, {"undo_called": False}, "a successful undo call",
                           note="no successful darkroom.undo_last call")
    before_end = (obs.before or {}).get("history_end")
    after_end = (obs.after or {}).get("history_end")
    decremented = (before_end is not None and after_end is not None and after_end < before_end)
    restored = _check_m1(i, Check("M1", kind="restored", target={"keys": [], "units": {}}), obs)
    ok = decremented or restored.ok
    return CheckResult(i, "undo", ok, {"history_end": [before_end, after_end],
                                       "restored": restored.ok}, "decremented OR restored",
                       note=None if ok else "neither history_end decremented nor params restored")


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
    if ch.metric == "verb":
        return _check_verb(i, ch, obs)
    if ch.metric == "undo":
        return _check_undo(i, ch, obs)
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


def _last_write_changed_keys(obs: Observation, units: dict) -> set[str] | None:
    """Keys the per-step snapshots show the LAST write call changed, or
    `None` with no per-step evidence -- the second oracle rule (a) needs
    beside the whole-case fingerprint (a net-zero precondition-then-write
    case, e.g. set +0.25 then adjust -0.25, nets to 0 -> 0 there)."""
    snaps = _step_snaps(obs)
    if snaps is None:
        return None
    idx = [i for i, c in enumerate(obs.calls) if c.write]
    if not idx:
        return set()
    return set(_diff_keys(snaps[idx[-1]] or {}, snaps[idx[-1] + 1] or {},
                          _units(obs, units), _meta(obs, "null_equiv")))


def _relevant_changed_keys(obs: Observation, units: dict) -> list[str]:
    """`_changed_keys` across the whole case, unioned with the last
    write's own per-step delta."""
    return sorted(set(_changed_keys(obs, units)) | (_last_write_changed_keys(obs, units) or set()))


def _net_zero_write_landed(obs: Observation, units: dict) -> bool:
    """True when the last write's own per-step delta is non-empty, even
    though the whole case nets to zero."""
    return bool(_last_write_changed_keys(obs, units))


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


def _noop_case_unchanged(case: Case, obs: Observation) -> bool:
    """Nothing changed in a noop case. Whole case (fingerprint) when no step is
    marked `setup`; otherwise only the action steps' own snapshot diffs, since a
    prep set legitimately changes the sidecar before the action under test."""
    if not any(st.setup for st in case.steps):
        return not _fingerprint_changed(obs)
    snaps = _step_snaps(obs)
    if snaps is None:
        return False  # cannot attribute the change to a step: do not credit the noop
    units = _units(obs)
    return not any(_step_changed(obs, snaps, i, units)
                   for i, st in enumerate(case.steps) if not st.setup and i < len(obs.calls))


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
    #: Night 3 (looks cluster B, scenario r0031): only the LAST write per
    #: `module.param` is a claim about the final state -- an earlier step of a
    #: repeat sequence (0.0625 then 0.125) is legitimately overwritten, so
    #: comparing it with the final after snapshot would deny a real landing.
    last: dict[str, tuple[object, str]] = {}
    for c in obs.calls:
        if not c.write:
            continue
        data = (c.envelope or {}).get("data") or {}
        moves = data["moves"] if isinstance(data.get("moves"), list) else [data]
        for m in moves:
            if not isinstance(m, dict) or m.get("noop"):
                continue
            if c.tool == "look_plan":
                #: `look_plan` is a harness-synthesised verb: its sub-moves carry
                #: the asked `value` (no `requested`); a refused sub-move
                #: (`ok` False) claimed nothing and is judged by its own code.
                if m.get("ok") is False:
                    continue
                requested = m.get("requested", m.get("value"))
            else:
                requested = m.get("requested")
            module, param = m.get("module"), m.get("param")
            if not module or not param or requested is None:
                return False
            last[f"{module}.{param}"] = (requested, m.get("unit", ""))
    for key_, (requested, munit) in last.items():
        unit = units.get(key_, munit)
        a = _num(after.get(key_))
        if a is None or not _same(unit, a, requested):
            return False
        b = _num(before.get(key_))
        if b is not None and not _differs(unit, b, a):
            return False
    return bool(last)


def _imperceptible_step_residual(case: Case, results: list[CheckResult],
                                 obs: Observation) -> Verdict | None:
    """Row 475 (I2 ruling, option A, provisional -- Pratik to confirm): the
    real night-1 D-bare-step-below-perceptual-floor rows are plain `ok`,
    never `moved_to_limit` (KNOWN_RESIDUAL's "cap" is the
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


def _honest_noop_at_target(case: Case, obs: Observation) -> bool:
    """Prove the target was already met from the case-start snapshot alone
    (`snaps[0]`, or `obs.before` with no per-step snapshots), not from
    "every observed check happened to already pass" (which a future
    envelope-only edit case, with no value-reading check, would pass
    vacuously)."""
    checks = case.expect.checks
    snaps = _step_snaps(obs)
    before = dict(snaps[0]) if snaps else dict(_params(obs.before))
    if not checks or not before:
        return False
    synthetic = Observation(calls=obs.calls, before={"params": before}, after={"params": before})
    return all(run_check(i, ch, synthetic).ok for i, ch in enumerate(checks))


def _action_writes(case: Case, obs: Observation) -> list[CallRecord]:
    """The case's own action writes: write calls whose step is not marked
    `setup`. Calls align 1:1 with `case.steps` (tool mode). With no setup
    step marked (or no step alignment, e.g. agent mode) this is every write,
    so only cases that opt in change behaviour."""
    writes = [c for c in obs.calls if c.write]
    steps = case.steps
    if not any(st.setup for st in steps):
        return writes
    action = [c for i, c in enumerate(obs.calls)
              if c.write and not (i < len(steps) and steps[i].setup)]
    return action or writes


def _claimed_moves(c: CallRecord) -> list[tuple[str, float]] | None:
    """(module.param, applied) for every committed, non-noop move an envelope
    claims landed, or None when it claims something this cannot verify (an ok
    write that names no numeric landing)."""
    data = (c.envelope or {}).get("data") or {}
    moves = data.get("moves")
    if isinstance(moves, list):
        out = []
        for m in moves:
            if not isinstance(m, dict) or m.get("noop") or m.get("committed") is False:
                continue
            applied = _num(m.get("applied"))
            if applied is None or not isinstance(m.get("module"), str) \
                    or not isinstance(m.get("param"), str):
                return None
            out.append((f"{m['module']}.{m['param']}", applied))
        return out
    if data.get("noop"):
        return []
    applied = _num(data.get("applied"))
    if applied is None or not isinstance(data.get("module"), str) \
            or not isinstance(data.get("param"), str):
        return None
    return [(f"{data['module']}.{data['param']}", applied)]


def _honest_final_step(case: Case, obs: Observation) -> bool:
    """Night 3 (weaker_grade-olympus-lot, weaker_colour_grade-olympus): a
    multi-step case whose LAST action write answered an honest non-ok
    (`partial` with the refused move named, or a typed error) must not be read
    as "the product reported ok" because an EARLIER action write was ok -- the
    same class as row 459's `undo_reported_ok` and `_action_writes`, for an
    antecedent that is an action step (a phrase's "stronger" before "weaker").

    True only when the final step's non-ok is its own answer AND the sidecar
    (Z2's other oracle) agrees with everything the case's writes claimed: every
    committed move any action write reported landed in that step's own
    post-snapshot, and the final step changed nothing it did not claim. Without
    per-step snapshots, or on any disagreement, False (the old reading)."""
    acts = _action_writes(case, obs)
    if len(acts) < 2:
        return False
    final = acts[-1]
    env = final.envelope or {}
    if env.get("ok") or env.get("outcome") in (*NOOP_OUTCOMES, "moved_to_limit", "ok"):
        return False
    if env.get("outcome") != "partial" and not _code(final):
        return False
    snaps = _step_snaps(obs)
    if snaps is None:
        return False
    units = {**_units(obs), **((case.expect.sidecar or {}).get("units") or {})}
    null_equiv = _meta(obs, "null_equiv")
    idx = {id(c): i for i, c in enumerate(obs.calls)}
    for c in acts:
        claimed = _claimed_moves(c)
        if claimed is None:
            return False
        snap = snaps[idx[id(c)] + 1] or {}
        if any(not _same(units.get(k, "ratio"), snap.get(k), v) for k, v in claimed):
            return False
    i = idx[id(final)]
    allowed = _owned([k for k, _ in _claimed_moves(final) or []], obs)
    stray = [k for k in _diff_keys(snaps[i] or {}, snaps[i + 1] or {}, units, null_equiv)
             if k not in allowed and not k.endswith(".__enabled")]
    return not stray


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
    # Setup writes (Step.setup, e.g. the `darkroom_set` that puts the frame at
    # a start value) are not the product's claim about the action under test:
    # an ok setup must not make an honest action error look silent (same class
    # as row 459's `undo_reported_ok`, generalised).
    #: Generalised again (night 3): an earlier ok action step does not make the
    #: failing LAST step's honest partial/refusal look like a claim (see
    #: `_honest_final_step`, which also re-verifies every claim in the sidecar).
    reported = (any(_env_ok(c) for c in _action_writes(case, obs))
                and not _honest_final_step(case, obs)) or _any_moved_to_limit(obs)
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
    if rule_f == "f: answered noop for a request that needed a change" and \
            _honest_noop_at_target(case, obs):
        # C5: the noop is honest (the pre-write snapshot already met the
        # target) -- return None straight away, not just skip this rule,
        # so (a)-(e) below (which all assume something SHOULD have moved)
        # never get a turn either.
        return None
    if rule_f:
        return rule_f
    if not reported:
        return None
    if sidecar.get("changed") and not _fingerprint_changed(obs) and \
            not _net_zero_write_landed(obs, units):
        return "a: reported ok but the sidecar history did not change"
    fallback = [sidecar["key"]] if sidecar.get("key") else []
    keys = _reported_keys(writes)
    keys = fallback if keys is None else keys
    every = _reported_keys(writes, every=True)
    every = fallback if every is None else every
    changed = _relevant_changed_keys(obs, units)
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
        # N1: only action steps count; a setup step's own landing is not the
        # product's claim about the request under test.
        sidx = ch.step if ch.step is not None else len(case.steps) - 1
        if ch.metric == "M1" and r.note == "unreported_clamp" and not (
                0 <= sidx < len(case.steps) and case.steps[sidx].setup):
            return (f"g: unreported clamp: {ch.target.get('key')} landed at its bound "
                    f"({r.measured!r} of the asked {r.expected!r}) with ok and no clamp report")
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


def _refusal_matches_meta(case: Case, call: CallRecord) -> bool:
    """`accept_refusal` must not wave through any refusal code -- only one
    the case names (`meta["refusal_codes"]`), and, when the case names its
    own candidate keys, only a refusal whose `error.detail.refused[*]`
    actually names one of them."""
    meta = case.meta or {}
    codes = meta.get("refusal_codes")
    if codes is not None and _code(call) not in codes:
        return False
    keys = meta.get("candidate_keys")
    if keys:
        detail = ((call.envelope or {}).get("error") or {}).get("detail") or {}
        named = {f"{r.get('module')}.{r.get('param')}" for r in detail.get("refused") or ()
                if isinstance(r, dict)}
        if not named & set(keys):
            return False
    return True


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
    """`expect.sidecar` as an implicit check. `_honest_noop_at_target`
    (same test `silent_failure`'s rule (f) uses) exempts a case whose
    target was already met before it ran (e.g. cov-phrase-lens_on-olympus-
    bare) from "expected to change but did not"."""
    sc = case.expect.sidecar or {}
    units = sc.get("units", {})
    moved = _fingerprint_changed(obs) or _net_zero_write_landed(obs, units)
    honest_noop = _noop_rule(case, obs, units) == \
        "f: answered noop for a request that needed a change" and \
        _honest_noop_at_target(case, obs)
    if sc.get("changed") is True and not moved and not honest_noop:
        return "sidecar expected to change but did not"
    if sc.get("changed") is False and moved:
        return "sidecar expected unchanged but changed"
    key = sc.get("key")
    if key and sc.get("changed") and not honest_noop and \
            not _owned([key], obs) & set(_relevant_changed_keys(obs, units)):
        return f"sidecar {key} expected to change but did not"
    return None


def _refuse_write_failure(case: Case, results: list[CheckResult]) -> Verdict | None:
    """On a refuse case that wrote, the checks decide."""
    for r, ch in zip(results, case.expect.checks):
        if ch.tags.get("silent_if_fail") and not r.ok:
            why = ch.tags.get("why", "a required report is missing")
            return Verdict(FAIL, "silent_failure", results, silent_failure=f"c: {why}")
    if any(not r.ok for r in results):
        return Verdict(FAIL, "failed", results, notes=["substitute edit does not match its checks"])
    return None


def _nested_codes(c: CallRecord) -> list[str]:
    """Typed codes of the moves a NON-ok envelope refused inside itself: a
    `darkroom_say` partial carries `data.refused[*].code` while its own
    call-level code is just "partial"."""
    env = c.envelope or {}
    if env.get("ok"):
        return []
    out: list[str] = []
    for src in (env.get("data") or {}, (env.get("error") or {}).get("detail") or {}):
        for r in src.get("refused") or () if isinstance(src, dict) else ():
            if isinstance(r, dict) and isinstance(r.get("code"), str):
                out.append(r["code"])
    return out


def _transient_block(obs: Observation, policy: Policy) -> str | None:
    for c in obs.calls:
        if _code(c) in policy.transient_codes or c.error == "timeout":
            return _code(c) or "timeout"
        for code in _nested_codes(c):
            if code in policy.transient_codes:
                return code
    return None


#: Night 3 (looks cluster A and the 86 tint refusals): the look planner could not
#: plan a component (no calibration for sigmoid.contrast / temperature.temperature)
#: or Scouter honestly refused it (temperature.tint while WB is late). Such a
#: component is a declared gap -- the check behind it is not asserted, and the
#: verdict names it.
_NO_CAL = re.compile(r"^(?:no calibration for (\S+)|(\S+) calibration unverified\b.*)$")


def _look_check_key(case: Case, i: int, ch: Check) -> str | None:
    """The `module.param` a looks check depends on: its own `look_key` tag, else
    derived (stored night-3 cases carry no tag) from the metric/stat and the
    component named in `meta["check_tags"]`."""
    if ch.tags.get("look_key"):
        return ch.tags["look_key"]
    tags = case.meta.get("check_tags") or []
    comp = (tags[i] if i < len(tags) else {}).get("component")
    if ch.metric == "M6":
        return "sigmoid.contrast"
    if comp == "grey_cast" and ch.metric == "M4":
        return "temperature.tint" if ch.stat == "a" else "temperature.temperature"
    if comp == "saturation_delta":
        return "colorbalancergb.global_chroma"
    if comp == "black_lift":
        return "exposure.black"
    return None


def _look_gap_keys(case: Case, obs: Observation, policy: Policy) -> dict[str, str]:
    """{module.param: reason} for every look component that was not planned
    (`look_notes`) or whose move Scouter honestly refused (a typed refusal
    code on the sub-move)."""
    if not case.meta.get("look"):
        return {}
    out: dict[str, str] = {}
    for note in obs.extra.get("look_notes") or []:
        m = _NO_CAL.match(str(note))
        if m:
            out[m.group(1) or m.group(2)] = "no calibration"
    for c in obs.calls:
        if c.tool != "look_plan":
            continue
        for m in ((c.envelope or {}).get("data") or {}).get("moves") or []:
            if isinstance(m, dict) and m.get("ok") is False and m.get("error") in policy.refusal_codes:
                out[f"{m.get('module')}.{m.get('param')}"] = f"refused {m['error']}"
    return out


def evaluate(case: Case, obs: Observation, policy: Policy) -> Verdict:
    gaps = _look_gap_keys(case, obs, policy)
    if gaps:
        keep, dropped = [], []
        for i, ch in enumerate(case.expect.checks):
            key = _look_check_key(case, i, ch)
            (dropped if key in gaps else keep).append((ch, key))
        if dropped:
            notes = [f"declared gap: look component not plannable: {k} ({gaps[k]})"
                     for k in sorted({k for _, k in dropped})]
            if not keep and not obs.crash_reports and not obs.blocked \
                    and not _transient_block(obs, policy):
                return Verdict(PARTIAL, "needs_review", [], notes=notes)
            slim = dataclasses.replace(case, expect=dataclasses.replace(
                case.expect, checks=[ch for ch, _ in keep]),
                meta=dict(case.meta, check_tags=[
                    t for i, t in enumerate(case.meta.get("check_tags") or [])
                    if i < len(case.expect.checks)
                    and _look_check_key(case, i, case.expect.checks[i]) not in gaps]))
            v = _evaluate(slim, obs, policy)
            v.notes = [*v.notes, *notes]
            return v
    return _evaluate(case, obs, policy)


def _evaluate(case: Case, obs: Observation, policy: Policy) -> Verdict:
    if obs.crash_reports:
        return Verdict(FAIL, "crash", notes=list(obs.crash_reports))
    if obs.blocked:
        outcome = "failed_hang" if obs.blocked == "hang_twice" else "blocked"
        return Verdict(FAIL, outcome, notes=[obs.blocked, *obs.hang_evidence])
    notes: list[str] = []
    if obs.healed and not case.meta.get("allow_heal"):
        return Verdict(FAIL, "failed", notes=["watchdog heal inside a case (spec 6.4)"])
    results = [run_check(i, ch, obs) for i, ch in enumerate(case.expect.checks)]
    # PAR-hollow-pass (night-1 triage rank 2, row 488; Pratik's ruling row
    # 496 item 5, review fix round 2 D-1): a layer 6 parity pair (tests/
    # gauntlet/exec_parity.py parity_pair) where both sides answer the SAME
    # thing is not, on its own, evidence the verb ran as intended --
    # `envelopes_match` alone would read it as a pass. `parity_pair`
    # computes the (variant-aware) reason and records it in
    # `obs.extra["parity_needs_review"]`; ONLY those cases are downgraded:
    #   - a HAPPY variant where both sides failed the same way (the verb
    #     never really ran);
    #   - an ERROR variant where a side ACCEPTED the deliberately-bad
    #     input instead of refusing it, or where the shared refusal code is
    #     transient (not a typed refusal the verb chose);
    #   - a pair where BOTH sides needed a frame-switch relaunch
    #     (LK-frame-switch-relaunch) to open the frame at all.
    # An error variant where both sides refuse the SAME typed way is a
    # genuine pass -- that is the point of the error variant -- and is never
    # touched here. `parity.same_failure_is_pass` restores the old (lenient)
    # scoring with a one-line config flip (tests/gauntlet/data/
    # check_map.json's "flags" list; see Policy.flags/KNOWN_FLAGS), and
    # defaults off.
    if obs.extra.get("parity_needs_review") and \
            "parity.same_failure_is_pass" not in policy.flags:
        return Verdict(PARTIAL, "needs_review", results,
                       notes=[obs.extra["parity_needs_review"]])
    kind = case.expect.kind

    if kind == "refuse":
        if case.mode == "agent":
            text = obs.agent_text or ""
            refuses = _refuses(text, policy.refusal_markers)
            claims = _claims(text, policy.claim_markers)
            wrote = _fingerprint_changed(obs)
            #: The out-of-scope policy (refuse_and_offer_only) is read here,
            #: not just labelled on the case -- a write before the user
            #: picks between offered options is unrequested_edit, not the
            #: generic unexpected_edit every other refuse-kind write earns.
            policy_name = case.meta.get("out_of_scope_policy")
            unrequested = policy_name == "refuse_and_offer_only"
            if wrote and not case.meta.get("substitute_ok"):
                outcome = "unrequested_edit" if unrequested else "unexpected_edit"
                return Verdict(FAIL, outcome, results, notes=["sidecar changed"])
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
                #: A bare refusal with no offered alternative is not the
                #: ruling's shape ("refuse AND offer") -- needs_review, not
                #: a silent pass, when the policy is on this case.
                if unrequested and not _refuses(text, policy.offer_markers):
                    return Verdict(PARTIAL, "needs_review", results,
                                   notes=["refused but named no alternative (offer required)"])
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

    # edit / noop / property
    #: The honest-refusal-is-a-pass rule the `edit` branch applies also
    #: covers a `property` Expect carrying `accept_refusal` (a P4 monotonic
    #: case whose sequence honestly refuses before any value moves). Gated
    #: on `accept_refusal` so no other case changes behaviour.
    if kind in ("edit", "property") and case.meta.get("accept_refusal"):
        refused = [c for c in obs.calls if c.write and not (c.envelope or {}).get("ok")]
        if refused and _code(refused[0]) in policy.refusal_codes and not \
                any(_env_ok(c) for c in obs.calls if c.write):
            if not _refusal_matches_meta(case, refused[0]):
                return Verdict(FAIL, "wrong_refusal", results, notes=[_code(refused[0]) or ""])
            honest, why = _honest_tool_refusal(obs, policy)
            if honest:
                return Verdict(PASS, "correct_refusal", results, notes=[why])
            return Verdict(FAIL, "failed", results, notes=[why])
    if kind == "edit":
        refused = [c for c in obs.calls if c.write and not (c.envelope or {}).get("ok")]
        if refused and _code(refused[0]) in policy.refusal_codes and not \
                any(_env_ok(c) for c in obs.calls if c.write):
            return Verdict(FAIL, "wrong_refusal", results, notes=[_code(refused[0]) or ""])
    blocked = _transient_block(obs, policy)
    sf = silent_failure(case, obs, results, policy)
    if sf:
        return Verdict(FAIL, "silent_failure", results, silent_failure=sf)
    #: Review round 2, finding 3 (GD-2, install config-version-assert);
    #: review round 3, GR-1: a Check tagged {"disputed": "<reason>"} opts a
    #: case's disputed assertion out of the ordinary partial-credit
    #: threshold below, which would otherwise score a genuine product
    #: failure elsewhere in the same case 0.5 and label it the same as an
    #: assertion still awaiting a decision. An untagged check failing is
    #: still a real failure -- "failed", never softened. A tagged check
    #: failing, with every untagged ("hard") check passing, is
    #: "needs_review" (the OUTCOME string) with the tag's own reason text as
    #: the note. All checks passing falls through unchanged (usually
    #: "verified" below) -- the tag can neither lift a failure nor drag a
    #: pass down. The tag KEY is deliberately `disputed`, never
    #: `needs_review`: `checkmap.wb_route_a_pinned_edit` already uses the
    #: key `sign_unverified` for an unrelated purpose (a comment-only tag
    #: before this evaluator change existed, now read by nothing) -- a
    #: shared key would have opted every Route-A move check with an
    #: unverified sign into this path too, including `equals` checks where
    #: a real wrong value is a genuine product failure, not a disputed spec
    #: question (GR-1's own probe: a pinned `equals` miss silently scored
    #: `needs_review` instead of `failed`).
    review = [(r, ch) for r, ch in zip(results, case.expect.checks) if ch.tags.get("disputed")]
    if review:
        hard = [r for r, ch in zip(results, case.expect.checks) if not ch.tags.get("disputed")]
        if not all(r.ok for r in hard):
            return Verdict(FAIL, "failed", results,
                           notes=[f"check {r.index}: {r.note}" for r in hard if not r.ok])
        pending = [ch.tags["disputed"] for r, ch in review if not r.ok]
        if pending:
            return Verdict(PARTIAL, "needs_review", results, notes=pending)
    ok_all = all(r.ok for r in results)
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
        noops = [c for c in _action_writes(case, obs) + [c for c in obs.calls if not c.write]
                 if ((c.envelope or {}).get("data") or {}).get("noop")]
        reason_ok = not case.expect.codes or any(
            ((c.envelope or {}).get("data") or {}).get("reason") in case.expect.codes
            for c in noops)
        if noops and reason_ok and _noop_case_unchanged(case, obs) and \
                all(r.ok for r in results):
            return Verdict(PASS, "verified", results)
        return Verdict(FAIL, "failed", results, notes=["expected an honest noop"])

    #: A workflow case whose verb/undo check failed because the agent never
    #: actually called the tool this verb needs (a clarify-only
    #: conversation, no non-noop write at all) is not a confident FAIL --
    #: the agent may be waiting on the user's answer to a real question.
    #: Must run before the sidecar-must-change gate (`if miss:` below):
    #: `preset_save` (r0348) and `repeat_more` (r0364, "More.") both carry
    #: `sidecar={"changed": True}`, which would otherwise fail a
    #: clarify-only conversation on "sidecar expected to change but did
    #: not" before this branch is ever reached. Placed after the
    #: blocked/noop returns above so a genuinely transient/blocked call
    #: still scores `blocked`, not a clarify-only needs_review it never was.
    if kind == "workflow" and not ok_all and \
            any(r.metric in ("verb", "undo") and not r.ok for r in results) and \
            not any(c.write and (c.envelope or {}).get("outcome") not in NOOP_OUTCOMES
                    for c in obs.calls):
        return Verdict(PARTIAL, "needs_review", results,
                       notes=["asked before acting: no verb/write call"])

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
