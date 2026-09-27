"""Layer 1: coverage (spec 3.1). 648 cases per main body + 20 NEX-5N WB cases.

Every case runs on the body's `hero` frame. P0 core per body: every live
param x {set lo, set hi, set mid, adjust +1, adjust -1, undo after set} plus
the 27 phrase keys bare (159 per body)."""

from __future__ import annotations

import json

from gauntlet.case import Case, Check, Expect, case_id
from scouter.core import phrases, vocab
from tests.gauntlet import checkmap
from tests.gauntlet.layers.common import (
    BODIES,
    BUDGET_MOVE,
    BUDGET_SAY,
    CALIBRATED_BODIES,
    boundary_check,
    clamp_reported,
    frame,
    key,
    m1_delta,
    m1_equals,
    m1_restored,
    sidecar,
    step,
)
from tests.gauntlet.taxonomy import DATA

LAYER = "coverage"
PREFIX = "cov"
HERO = frame("hero")

#: Rows 458/461 (night-1 triage class COV-WB-preset4-expectation):
#: check_map.json's `wb_route_a` block decides what a write to the
#: temperature module must verify on the corpus hero (checkmap.HERO_WB_PRESET):
#: refused (`module_unavailable`) while `channelmixerrgb.temperature` is a
#: candidate action path, routed through color calibration once it is
#: pinned. Loaded once, module-level, exactly like policy.py's `POLICY`.
_CMAP = json.loads((DATA / "check_map.json").read_text(encoding="utf-8"))


def _wb_route_a(case_key: str) -> dict | None:
    """checkmap.wb_route_a_expect_for_hero for a hero-frame case (`case_key`
    a phrase key like "warmer", or a "module.param" key like
    "temperature.temperature")."""
    return checkmap.wb_route_a_expect_for_hero(_CMAP, case_key)


def _wb_route_a_tint() -> dict | None:
    """checkmap.wb_route_a_tint_expect_for_hero for a hero-frame
    temperature.tint case -- always a refusal on preset 4, pin or no pin
    (tint has no reroute)."""
    return checkmap.wb_route_a_tint_expect_for_hero(_CMAP)


def _wb_refusal_expect(route: dict) -> Expect:
    return Expect("typed_error", codes=list(route["codes"]))


#: Review round 2, finding 10 (GC-1): shared through checkmap.py so every
#: generator that wires wb_route_a_expect scores a pinned Route A the same
#: way, not only coverage's own cases (layers/guide.py's gd-12 now uses it
#: too).
_wb_route_a_pinned_edit = checkmap.wb_route_a_pinned_edit


def _wb_route_a_pinned_restore(route: dict) -> Expect:
    to_key = route["m1_key"]
    return Expect("workflow", [m1_restored([to_key], {to_key: route["unit"]})])
#: Cluster D / row 458 (2026-09-27): the olympus `hero` frame surveys near
#: the darkest in the corpus, so these 6 "touch" phrase cases -- olympus-body
#: rows of tests/test_touch_floor.py's KNOWN_RESIDUAL -- can't clear their
#: floor on it. They run on `hero_bright` (a normally-exposed stand-in;
#: see corpus.EXTRA_ROLES) instead. The 4 sony_a7 KNOWN_RESIDUAL rows are
#: unaffected -- no bright sony hero exists yet -- and stay on `hero`.
BRIGHT_HERO_TOUCH_KEYS = frozenset({
    "deepen_shadows", "lift_shadows", "more_vibrance", "less_vibrance", "cool_shadows",
    "teal_orange",
})
OPPOSED = (("brighter", "darker"), ("more_contrast", "less_contrast"), ("warmer", "cooler"),
           ("more_magenta", "more_green"), ("lift_shadows", "deepen_shadows"),
           ("brighter_midtones", "darker_midtones"), ("brighter_highlights", "tame_highlights"),
           ("deeper_blacks", "faded_blacks"), ("more_saturation", "less_saturation"),
           ("more_vibrance", "less_vibrance"), ("more_chroma", "less_chroma"),
           ("stronger_grade", "weaker_grade"))
COMMUTATIVE = (("brighter", "warmer"), ("more_contrast", "more_saturation"),
               ("lift_shadows", "cooler"), ("deeper_blacks", "more_vibrance"),
               ("tame_highlights", "more_chroma"), ("brighter_midtones", "warmer"))
#: phrase key -> (metric, stat, region, sign) for the stat check on phrase cases.
PHRASE_STAT = {
    "brighter": ("M2", "L", "frame", 1), "darker": ("M2", "L", "frame", -1),
    "more_contrast": ("M6", "spread", "frame", 1), "less_contrast": ("M6", "spread", "frame", -1),
    "warmer": ("M4", "rb", "frame", 1), "cooler": ("M4", "rb", "frame", -1),
    "lift_shadows": ("M2", "L", "shadows", 1), "deepen_shadows": ("M2", "L", "shadows", -1),
    "brighter_midtones": ("M2", "L", "frame", 1), "darker_midtones": ("M2", "L", "frame", -1),
    "brighter_highlights": ("M2", "L", "highlights", 1),
    "tame_highlights": ("M2", "L", "highlights", -1),
    "deeper_blacks": ("M3", "p01L", "frame", -1), "faded_blacks": ("M3", "p01L", "frame", 1),
    "more_saturation": ("M5", "C", "frame", 1), "less_saturation": ("M5", "C", "frame", -1),
    "more_vibrance": ("M5", "C", "frame", 1), "less_vibrance": ("M5", "C", "frame", -1),
    "more_chroma": ("M5", "C", "frame", 1), "less_chroma": ("M5", "C", "frame", -1),
    "cool_shadows": ("M4", "b", "shadows", -1), "warm_highlights": ("M4", "b", "highlights", 1),
    "teal_orange": ("M4", "b", "shadows", -1),
}
TINT_KEYS = {"more_magenta", "more_green"}
EXPLICIT_BY_UNIT = {"EV": ("0.5 ev", 0.5), "K": ("500k", 500.0), "%": ("5%", 0.05),
                    "ratio": ("0.2", 0.2), "°": ("20°", 20.0)}


def _signed(label: str, n: int) -> str:
    return f"{label}_{'up' if n > 0 else 'dn'}{abs(n)}"


def live_params() -> list[vocab.ParamSpec]:
    # `__enabled` toggle rows are not numeric set/adjust targets (there is no
    # lo/hi/mid to sweep) -- they are exercised by module_cases' enable/
    # disable checks and by their own toggle phrases (phrase_cases).
    return [s for s in vocab.PARAMS if vocab.status(s) == "live" and s.param != "__enabled"]


# Fix round 1, M6: `phrases.row_for` is the single place that maps a
# `@tone`/`crop` move to its representative vocabulary row.
_row = phrases.row_for


def _default(s: vocab.ParamSpec) -> float | None:
    """The frame's own starting value for `s`, when the vocabulary declares
    one (darktable's module default, or Scouter's neutral -- the hero frame
    starts at darktable defaults, so either names the same point)."""
    if s.default is not None:
        return s.default
    if s.neutral is not None:
        return s.neutral
    return None


def _at_baseline(s: vocab.ParamSpec, value: float) -> bool:
    """Item 1: does requesting `value` ask for what the param already holds
    at the hero frame's start (wrap-aware for hue)? When it does, an honest
    write is a noop, not a silent failure."""
    base = _default(s)
    if base is None:
        return False
    if s.wraps:
        return abs((value % 360.0) - (base % 360.0)) < 1e-9
    return abs(value - base) < 1e-9


def _mid(s: vocab.ParamSpec) -> float:
    """The midpoint of `s`'s range -- moved off the default when the range
    is symmetric around it (item 1: `set_mid` must be a real move; `neutral`
    is the probe allowed to legitimately equal the default)."""
    m = round((s.lo + s.hi) / 2, 6)
    if _at_baseline(s, m):
        base = _default(s) or 0.0
        m = round((base + s.hi) / 2, 6)
    return m


#: COV-weaker-at-zero (night-1 row 471, cov-colorzones.strength-sony_a7-
#: nudge_dn1): `colorzones.strength` declares no `default`/`neutral` (its
#: row's `_at_baseline` can never see darktable's own default for it), and
#: `nudge` moves the slider by DARKTABLE'S OWN gui step (scouter/core/
#: darkroom.py `nudge`'s docstring), not `s.step` -- coarse enough here that
#: a single `nudge_dn1` from `_mid(s)` (1.0) lands exactly back on the
#: frame's own baseline reading (0.0): a real, correctly-signed decrease
#: that reads identically to "nothing happened" once only the sidecar is
#: compared. Only DOWN is at risk (a param can't nudge UP past `_mid(s)`
#: back onto a baseline at `s.lo`), so only the down-direction precondition
#: moves, to `s.hi` -- the farthest point a single native step down could
#: still not reach zero from.
NUDGE_PRECONDITION_OVERRIDE_DN: dict[tuple[str, str], str] = {
    ("colorzones", "strength"): "hi",
}


def _nudge_precondition(s: vocab.ParamSpec, n: int) -> float:
    if n < 0:
        which = NUDGE_PRECONDITION_OVERRIDE_DN.get((s.module, s.param))
        if which:
            return getattr(s, which)
    return _mid(s)


def _neutral(s: vocab.ParamSpec) -> float:
    if s.neutral is not None:
        return s.neutral
    if s.default is not None:
        return s.default
    return round(s.lo + (s.hi - s.lo) / 4, 6)


#: Item 4b: the WB calibration table's sampled Kelvin range (measured for
#: the em10iii body, `~/.config/scouter/wb/em10iii.json`; read once to pick
#: these constants, never at generation time). Kelvin decodes honestly only
#: inside a table's sampled range -- even on a calibrated body -- so
#: `temperature.temperature`'s own boundary probes (set_lo/set_hi and their
#: +-step siblings) are narrowed to land inside it; outside it they would
#: read back `None`, which is not what a boundary probe is for.
WB_SAMPLED_K = (3000.0, 12000.0)


def _probe_lo_hi(s: vocab.ParamSpec) -> tuple[float, float]:
    if (s.module, s.param) == ("temperature", "temperature"):
        lo, hi = WB_SAMPLED_K
        return lo + s.step, hi - s.step
    return s.lo, s.hi


def _case(cid: str, body: str, tier: str, steps: list, expect: Expect, *,
          meta: dict | None = None, frame_role: str = "hero") -> Case:
    return Case(id=cid, layer=LAYER, tier=tier, mode="tool", steps=steps, expect=expect,
                body=body, frame_role=frame_role, source={"file": "vocab.py"}, meta=meta or {})


def _set(s: vocab.ParamSpec, value: float):
    return step("darkroom_set", BUDGET_MOVE, module=s.module, param=s.param, value=value)


def _adjust(s: vocab.ParamSpec, delta: float):
    return step("darkroom_adjust", BUDGET_MOVE, module=s.module, param=s.param, delta=delta)


def _side(s: vocab.ParamSpec, *, changed: bool = True) -> dict:
    return sidecar(s.module, s.param, s.unit, changed=changed)


def _wb_relative_ok(s: vocab.ParamSpec, body: str) -> bool:
    """Item 4a: a Kelvin-RELATIVE move (adjust/nudge, a warmer/cooler
    phrase) needs a calibration table to read the param's current value;
    an absolute `set` does not."""
    return s.value_source != "wb_table" or body in CALIBRATED_BODIES


def _tint_reset() -> list:
    """W4: Kelvin is readable from the XMP only at the WB table's calibrated
    tint (1.0), so every white-balance Kelvin case first sets tint 1.0."""
    t = vocab.spec("temperature", "tint")
    return [step("darkroom_set", BUDGET_MOVE, module=t.module, param=t.param, value=1.0)]


def param_cases(s: vocab.ParamSpec, body: str) -> list[Case]:
    k = key(s.module, s.param)
    cid = lambda v: case_id(PREFIX, k, body, v)  # noqa: E731
    out: list[Case] = []
    is_wb = (s.module, s.param) == ("temperature", "temperature")
    #: Rows 458/461 (COV-WB-preset4-expectation): on the corpus hero's preset
    #: 4, every temperature.temperature write refuses (module_unavailable)
    #: until channelmixerrgb.temperature is pinned -- this preempts the
    #: item-4a "no calibration table" refusal below (`relative_ok`), which
    #: never gets a chance to fire on a preset-4 frame in practice. When
    #: `wb_route` is not None the whole case shrinks to its single action
    #: step, same as the item-4a refusal cases already did (no prep write,
    #: so the sidecar stays untouched for an honest refusal).
    wb_route = _wb_route_a(k) if is_wb else None
    wb_refused = wb_route is not None and wb_route["expect"] == "typed_error"
    #: Fix round 1 (review I4): pinned-and-live is a real, distinct third
    #: state (`wb_route` is not None but its own `expect` is "edit"), not
    #: the ordinary temperature.temperature path -- the write lands on
    #: `channelmixerrgb.temperature`, not this param, so it needs its own
    #: Expect (`_wb_route_a_pinned_edit`/`_wb_route_a_pinned_restore`), not
    #: a fallthrough to the checks below (which the write never touches).
    wb_pinned_live = wb_route is not None and not wb_refused
    #: A tint reset is itself refused unconditionally on preset 4 (I1/I4) --
    #: pinned or not -- so it is dropped whenever the block applies at all,
    #: not only on the refused branch.
    pre = [] if (not is_wb or wb_route is not None) else _tint_reset()
    o = len(pre)
    plo, phi = _probe_lo_hi(s)
    relative_ok = _wb_relative_ok(s, body)
    values = [("set_lo", plo, "P0"), ("set_hi", phi, "P0"), ("set_mid", _mid(s), "P0"),
              ("set_neutral", _neutral(s), "P1")]
    for label, v, tier in values:
        if label == "set_hi" and s.wraps:
            # item 1: 360 wraps to 0, the module default -- move it off, so
            # `set_hi` stays a real probe distinct from `set_lo`/`set_neutral`.
            v = s.hi - s.step
        if wb_refused:
            out.append(_case(cid(label), body, tier, [_set(s, v)], _wb_refusal_expect(wb_route)))
            continue
        if wb_pinned_live:
            out.append(_case(cid(label), body, tier, [_set(s, v)],
                             _wb_route_a_pinned_edit(wb_route, value=v)))
            continue
        at_base = _at_baseline(s, v)
        out.append(_case(cid(label), body, tier, [*pre, _set(s, v)],
                         Expect("edit", [m1_equals(s.module, s.param, v, s.unit)],
                                _side(s, changed=not at_base))))
    for label, v in (("set_lo_minus", plo - s.step), ("set_hi_plus", phi + s.step)):
        if wb_refused:
            out.append(_case(cid(label), body, "P1", [_set(s, v)], _wb_refusal_expect(wb_route)))
        elif wb_pinned_live:
            out.append(_case(cid(label), body, "P1", [_set(s, v)],
                             _wb_route_a_pinned_edit(wb_route, value=v)))
        elif s.wraps:
            want = v % 360.0
            out.append(_case(cid(label), body, "P1", [*pre, _set(s, v)],
                             Expect("edit", [m1_equals(s.module, s.param, want, s.unit)],
                                    _side(s))))
        else:
            bound = plo if v < plo else phi
            out.append(_case(cid(label), body, "P1", [*pre, _set(s, v)],
                             Expect("edit", [boundary_check(s.module, s.param, s.unit,
                                                             request=v, bound=bound)],
                                    _side(s)),
                             meta={"accept_refusal": True}))
    for n in (1, -1, 3, -3):
        tier = "P0" if abs(n) == 1 else "P1"
        if wb_refused:
            out.append(_case(cid(_signed("adjust", n)), body, tier, [_adjust(s, n * s.step)],
                             _wb_refusal_expect(wb_route)))
        elif wb_pinned_live:
            out.append(_case(cid(_signed("adjust", n)), body, tier, [_adjust(s, n * s.step)],
                             _wb_route_a_pinned_edit(wb_route, sign=(1 if n > 0 else -1))))
        elif relative_ok:
            chk = m1_delta(s.module, s.param, n * s.step, s.unit, s.step)
            chk.target["from"] = 1 + o
            out.append(_case(cid(_signed("adjust", n)), body, tier,
                             [*pre, _set(s, _mid(s)), _adjust(s, n * s.step)],
                             Expect("edit", [chk], _side(s))))
        else:
            # item 4a: no calibration table to read the current Kelvin from
            # -> an honest `value_unknown` refusal, with no prep write (a
            # prep set would leave the sidecar changed, and an honest
            # refusal needs it untouched for `_honest_tool_refusal` to see).
            out.append(_case(cid(_signed("adjust", n)), body, tier, [_adjust(s, n * s.step)],
                             Expect("edit", []), meta={"accept_refusal": True}))
    if wb_refused:
        over = _case(cid("adjust_overflow"), body, "P1", [_adjust(s, 3 * s.step)],
                     _wb_refusal_expect(wb_route))
    elif wb_pinned_live:
        over = _case(cid("adjust_overflow"), body, "P1", [_adjust(s, 3 * s.step)],
                     _wb_route_a_pinned_edit(wb_route, sign=1))
    elif s.wraps:
        over = _case(cid("adjust_overflow"), body, "P1",
                     [*pre, _set(s, s.hi - s.step), _adjust(s, 3 * s.step)],
                     Expect("edit", [m1_equals(s.module, s.param, (s.hi + 2 * s.step) % 360,
                                               s.unit)], _side(s)))
    elif relative_ok:
        over = _case(cid("adjust_overflow"), body, "P1",
                     [*pre, _set(s, s.hi - s.step), _adjust(s, 3 * s.step)],
                     Expect("edit", [m1_equals(s.module, s.param, s.hi, s.unit),
                                     clamp_reported()], _side(s)))
    else:
        over = _case(cid("adjust_overflow"), body, "P1", [_adjust(s, 3 * s.step)],
                     Expect("edit", []), meta={"accept_refusal": True})
    out.append(over)
    for n in (1, -1):
        #: Fix round 1 (review I4): darkroom.nudge never re-routes onto
        #: color calibration -- it refuses unconditionally on preset 4,
        #: pinned or not, unlike set/adjust (checkmap.wb_route_a_nudge_expect).
        if wb_route is not None:
            nudge_route = checkmap.wb_route_a_nudge_expect_for_hero(_CMAP)
            out.append(_case(cid(_signed("nudge", n)), body, "P1",
                             [step("darkroom_nudge", BUDGET_MOVE, module=s.module, param=s.param,
                                   steps=n)],
                             _wb_refusal_expect(nudge_route)))
        elif relative_ok:
            chg = Check("M1", kind="changed", sign=n,
                       target={"key": k, "unit": s.unit, "from": 1 + o})
            out.append(_case(cid(_signed("nudge", n)), body, "P1",
                             [*pre, _set(s, _nudge_precondition(s, n)),
                              step("darkroom_nudge", BUDGET_MOVE, module=s.module, param=s.param,
                                   steps=n)],
                             Expect("edit", [chg], _side(s))))
        else:
            out.append(_case(cid(_signed("nudge", n)), body, "P1",
                             [step("darkroom_nudge", BUDGET_MOVE, module=s.module, param=s.param,
                                   steps=n)],
                             Expect("edit", []), meta={"accept_refusal": True}))
    units = {k: s.unit}
    if wb_refused:
        # Fix round 1 (review I2): keep the antecedent's own refused
        # set/adjust as a real step -- session.call runs every step
        # regardless of an earlier one's outcome (tests/gauntlet/execute.py),
        # and it is what leaves the journal empty for THIS job, which is
        # exactly why the undo answers "noop" (checkmap.wb_route_a_undo_expect),
        # not module_unavailable -- the undo call itself never reaches
        # refuse_wb_late.
        undo_expect = checkmap.wb_route_a_undo_expect(_CMAP, wb_route)
        undo_exp = Expect("typed_error", codes=list(undo_expect["codes"]))
        out.append(_case(cid("undo_after_set"), body, "P0",
                         [_set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)], undo_exp))
        out.append(_case(cid("undo_after_adjust"), body, "P1",
                         [_adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)], undo_exp))
        out.append(_case(cid("repeat_set"), body, "P1",
                         [_set(s, _mid(s)), _set(s, _mid(s))], _wb_refusal_expect(wb_route)))
        out.append(_case(cid("repeat_adjust"), body, "P1",
                         [_adjust(s, s.step), _adjust(s, s.step)], _wb_refusal_expect(wb_route)))
    elif wb_pinned_live:
        restore = _wb_route_a_pinned_restore(wb_route)
        out.append(_case(cid("undo_after_set"), body, "P0",
                         [_set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)], restore))
        out.append(_case(cid("undo_after_adjust"), body, "P1",
                         [_adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)], restore))
        out.append(_case(cid("repeat_set"), body, "P1",
                         [_set(s, _mid(s)), _set(s, _mid(s))],
                         _wb_route_a_pinned_edit(wb_route, value=_mid(s))))
        out.append(_case(cid("repeat_adjust"), body, "P1",
                         [_adjust(s, s.step), _adjust(s, s.step)],
                         _wb_route_a_pinned_edit(wb_route, sign=1)))
    else:
        out.append(_case(cid("undo_after_set"), body, "P0",
                         [*pre, _set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)],
                         Expect("workflow", [m1_restored([k], units, o)])))
        out.append(_case(cid("undo_after_adjust"), body, "P1",
                         [*pre, _adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)],
                         Expect("workflow", [m1_restored([k], units, o)])))
        out.append(_case(cid("repeat_set"), body, "P1",
                         [*pre, _set(s, _mid(s)), _set(s, _mid(s))],
                         Expect("property", [Check("M1", kind="step_equal",
                                                   target={"key": k, "a": 1 + o, "b": 2 + o,
                                                           "unit": s.unit})], property="P1")))
        out.append(_case(cid("repeat_adjust"), body, "P1",
                         [*pre, _set(s, _mid(s)), _adjust(s, s.step), _adjust(s, s.step)],
                         Expect("property", [Check("M1", kind="step_ratio",
                                                   target={"key": k, "base": [1 + o, 2 + o],
                                                           "double": [1 + o, 3 + o], "ratio": 2.0,
                                                           "tol": 0.1})], property="P1")))
    return out


def hue_wrap_cases(body: str) -> list[Case]:
    out = []
    for s in [p for p in live_params() if p.wraps]:
        for label, start, delta, want in (("wrap_up", 350.0, 20.0, 10.0),
                                          ("wrap_down", 10.0, -20.0, 350.0)):
            out.append(_case(case_id(PREFIX, key(s.module, s.param), body, label), body, "P1",
                             [_set(s, start), _adjust(s, delta)],
                             Expect("edit", [m1_equals(s.module, s.param, want, "°")],
                                    _side(s))))
    return out


def module_cases(body: str) -> list[Case]:
    out = []
    mods = sorted({p.module for p in live_params()})
    for m in mods:
        first = next(p for p in live_params() if p.module == m)
        out.append(_case(case_id(PREFIX, m, body, "reset"), body, "P1",
                         [_set(first, _mid(first)),
                          step("darkroom_reset", BUDGET_MOVE, module=m, confirm=True)],
                         Expect("workflow", [Check("envelope", step=1,
                                                   target={"path": "ok", "op": "eq",
                                                           "value": True})],
                                {"changed": True})))
        en = Check("M1", kind="equals", target={"key": f"{m}.__enabled", "value": 1,
                                                "unit": "bool"})
        out.append(_case(case_id(PREFIX, m, body, "enable"), body, "P1",
                         [step("darkroom_enable", BUDGET_MOVE, module=m)],
                         Expect("workflow", [en])))
        out.append(_case(case_id(PREFIX, m, body, "enable_twice"), body, "P1",
                         [step("darkroom_enable", BUDGET_MOVE, module=m),
                          step("darkroom_enable", BUDGET_MOVE, module=m)],
                         Expect("property", [Check("M1", kind="step_equal",
                                                   target={"key": f"{m}.__enabled", "a": 1,
                                                           "b": 2, "unit": "bool"})],
                                property="P1")))
    return out


def tint_cases(body: str) -> list[Case]:
    t = vocab.spec("temperature", "tint")
    out = []
    #: Row 461 (wb_route_a extended to tint), fix round 1 (I1): this applies
    #: ONLY to the absolute `darkroom_set` -- it reaches
    #: `scouter.core.darkroom.refuse_wb_late` directly and refuses
    #: module_unavailable on preset 4. `adjust` and the `say` phrase are
    #: RELATIVE moves; `temperature.tint` is vocab status `absolute_only`
    #: (value_source "none"), so `scouter.core.moves.gate(relative=True)`
    #: raises `ValueUnknown` before `refuse_wb_late` (or even
    #: `_current_image`) ever runs -- unconditionally, on every preset, not
    #: just preset 4. That ordering is unchanged by route A and is NOT part
    #: of the wb_route_a block; keep it hardcoded, as before.
    wb_route = _wb_route_a_tint()
    set_exp = _wb_refusal_expect(wb_route) if wb_route else Expect("edit", [], {"changed": True})
    for v in (0.8, 0.9, 1.1, 1.2):
        out.append(_case(case_id(PREFIX, "temperature.tint", body, f"set_{v}"), body, "P1",
                         [_set(t, v)], set_exp))
    out.append(_case(case_id(PREFIX, "temperature.tint", body, "adjust"), body, "P1",
                     [_adjust(t, 0.05)], Expect("typed_error", codes=["value_unknown"])))
    out.append(_case(case_id(PREFIX, "temperature.tint", body, "say_more_magenta"), body, "P1",
                     [step("darkroom_say", BUDGET_SAY, phrase="more magenta")],
                     Expect("typed_error", codes=["value_unknown"])))
    return out


def unreachable_cases(body: str) -> list[Case]:
    c = vocab.spec("colorzones", "curves")
    codes = ["unreachable", "not_pinned", "usage"]
    return [
        _case(case_id(PREFIX, "colorzones.curves", body, "set"), body, "P1", [_set(c, 0.5)],
              Expect("typed_error", codes=codes)),
        _case(case_id(PREFIX, "colorzones.curves", body, "adjust"), body, "P1",
              [_adjust(c, 0.1)], Expect("typed_error", codes=codes)),
    ]


def _phrase_wb_expect(p: phrases.Phrase) -> dict | None:
    """checkmap's wb_route_a expectation for "warmer"/"cooler" (Kelvin, via
    `wb_route_a_expect`) -- the only phrases whose refusal on preset 4
    check_map.json's wb_route_a block actually governs.

    Fix round 1 (I1): `more_magenta`/`more_green` (TINT_KEYS) are NOT
    included, even though their only move is also on the temperature
    module. Their move kind is `adjust` (`phrases.py`'s `_adj`), and
    `temperature.tint`'s vocab status is `absolute_only`
    (`value_source == "none"`, vocab.py:150) -- so `moves.say`'s pre-bridge
    gate (`moves.gate(s, relative=True)`, called from `_refusal` before
    `_current_image`/`_route_wb`/`_refuse_wb_late` ever run) raises
    `ValueUnknown` first, on every preset, not just preset 4. The WB guard
    never gets a turn. Only an ABSOLUTE tint `darkroom_set` reaches
    `refuse_wb_late` (`tint_cases`' own set cases call
    `wb_route_a_tint_expect_for_hero` directly, not through this function).
    None for every other phrase, or if the block does not apply."""
    if p.key in WB_RELATIVE_PHRASE_KEYS:
        return _wb_route_a(p.key)
    return None


def _phrase_expect(p: phrases.Phrase, sign: float = 1.0, from_step: int = 0) -> Expect:
    wb = _phrase_wb_expect(p)
    if wb is not None:
        if wb["expect"] == "typed_error":
            return _wb_refusal_expect(wb)
        # Fix round 1 (review I4): route A pinned and live -- build the
        # real Expect from the check-map dict (target key, derived keys,
        # unchanged preset) instead of a permissive `Expect("edit", [])`
        # placeholder. The M4 rb pixel stat is a physical, oracle-
        # independent second check (Zenkai Z2) that still applies whatever
        # param the write actually lands on, so it is kept.
        sgn = 1 if p.key == "warmer" else -1 if p.key == "cooler" else 0
        exp = _wb_route_a_pinned_edit(wb, sign=sgn)
        metric, stat, region, msgn = PHRASE_STAT.get(p.key, (None, None, None, 0))
        if metric:
            exp.checks.append(Check(metric, stat=stat, region=region, sign=msgn))
        return exp
    if p.key in TINT_KEYS:
        return Expect("typed_error", codes=["value_unknown"])
    checks: list[Check] = []
    for mv in p.moves:
        if mv.module == phrases.TONE:
            continue
        row = _row(mv)
        if mv.kind == "set":
            checks.append(m1_equals(row.module, row.param, mv.value, row.unit))
        elif mv.kind == "toggle":
            checks.append(m1_equals(row.module, row.param, mv.value, row.unit))
        elif mv.kind == "crop":
            # A crop preset moves four rows at once; the "changed": True on
            # the Expect already covers it, so no single-key M1 check here.
            continue
        else:
            checks.append(Check("M1", kind="changed", sign=int(sign * (1 if mv.notches > 0 else -1)),  # noqa: E501
                                target={"key": key(row.module, row.param), "unit": row.unit,
                                        "from": from_step}))
    metric, stat, region, sgn = PHRASE_STAT.get(p.key, (None, None, None, 0))
    if metric:
        checks.append(Check(metric, stat=stat, region=region, sign=sgn))
    return Expect("edit", checks, {"changed": True})


#: COV-weaker-at-zero (night-1 row 471, cov-phrase-weaker_grade-*-bare/
#: touch/lot/synonym): the hero frame's colour-balance chroma rows all
#: start at their declared neutral (0%, no grade applied yet) -- every
#: "weaker colour grade" case asked to weaken a grade that was never
#: there. `scouter/core/moves.py:_adjust_core`'s own "already at the
#: limit" early return (correctly) answers noop for each of the three
#: chroma moves, clamped straight back to 0%: an honest noop, but the
#: case's own `Expect("edit", ..., {"changed": True})` still asked for a
#: change, so it was scored a silent failure (rule a) instead of the noop
#: it is. Precede weaker_grade with a `stronger_grade` step so it has a
#: real grade to weaken, and check "changed" from the snapshot AFTER that
#: precondition (`from_step`), not from the frame's own start -- otherwise
#: a same-sized weaken cancels the grade step exactly and looks unchanged
#: from step 0 again.
GRADED_PRECONDITION_KEYS = frozenset({"weaker_grade"})


def _graded_precondition(p: phrases.Phrase) -> list:
    if p.key not in GRADED_PRECONDITION_KEYS:
        return []
    return [step("darkroom_say", BUDGET_SAY, phrase="stronger colour grade a lot")]


#: Item 4a: these two phrases' only move is a Kelvin-relative temperature
#: adjust, so on a body with no calibration table they refuse honestly the
#: same way the bare adjust/nudge cases do (spec item 4).
WB_RELATIVE_PHRASE_KEYS = frozenset({"warmer", "cooler"})


def _wb_phrase_meta(p: phrases.Phrase, body: str) -> dict | None:
    """Item 4a's "no calibration table" refusal -- superseded for
    warmer/cooler/tint phrases by `_phrase_wb_expect`/checkmap's wb_route_a
    block (rows 458/461), which fires first on the corpus hero's preset 4
    and decides the expectation directly (`_phrase_expect` already calls
    it). This only still applies when the check-map block does not (a
    non-preset-4 frame, which does not exist in today's corpus, or a body
    with no calibration table on a param outside route A)."""
    if _phrase_wb_expect(p) is not None:
        return None
    if p.key in WB_RELATIVE_PHRASE_KEYS and not _wb_relative_ok(vocab.spec("temperature",
                                                                            "temperature"), body):
        return {"accept_refusal": True}
    return None


def phrase_cases(body: str) -> list[Case]:
    out = []
    for p in phrases.PHRASES:
        syn = p.synonyms[0]
        forms = [("bare", syn, "P0"), ("touch", f"{syn} a touch", "P1"),
                 ("lot", f"{syn} a lot", "P1")]
        wb_meta = _wb_phrase_meta(p, body)
        pre = _graded_precondition(p)
        for label, text, tier in forms:
            exp = Expect("edit", []) if wb_meta else _phrase_expect(p, from_step=len(pre))
            role = "hero_bright" if (label == "touch" and body == "olympus" and
                                      p.key in BRIGHT_HERO_TOUCH_KEYS) else "hero"
            out.append(_case(case_id(PREFIX, "phrase", p.key, body, label), body, tier,
                             [*pre, step("darkroom_say", BUDGET_SAY, phrase=text)], exp,
                             meta=wb_meta, frame_role=role))
        row = _row(p.moves[0])
        by, amount = EXPLICIT_BY_UNIT.get(row.unit, ("0.2", 0.2))
        text = f"{syn} by {by}"
        one = len(p.moves) == 1 and p.moves[0].kind == "adjust"
        wb = _phrase_wb_expect(p)
        if wb_meta:
            exp = Expect("edit", [])
        elif wb is not None:
            if wb["expect"] == "typed_error":
                exp = _wb_refusal_expect(wb)
            else:
                sgn = 1 if p.key == "warmer" else -1 if p.key == "cooler" else 0
                exp = _wb_route_a_pinned_edit(wb, sign=sgn)
        elif p.key in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        elif not one:
            exp = Expect("typed_error", codes=["usage"])
        elif p.moves[0].module == phrases.TONE:
            exp = _phrase_expect(p)
        else:
            sgn = 1 if p.moves[0].notches > 0 else -1
            exp = Expect("edit", [boundary_check(row.module, row.param, row.unit,
                                                 delta=sgn * amount, lo=row.lo, hi=row.hi)],
                         sidecar(row.module, row.param, row.unit))
        out.append(_case(case_id(PREFIX, "phrase", p.key, body, "explicit"), body, "P1",
                         [step("darkroom_say", BUDGET_SAY, phrase=text)], exp, meta=wb_meta))
    for p in phrases.PHRASES:
        wb_meta = _wb_phrase_meta(p, body)
        pre = _graded_precondition(p)
        for syn in p.synonyms:
            exp = Expect("edit", []) if wb_meta else _phrase_expect(p, from_step=len(pre))
            out.append(_case(case_id(PREFIX, "synonym", p.key, syn, body), body, "P1",
                             [*pre, step("darkroom_say", BUDGET_SAY, phrase=syn)], exp,
                             meta=wb_meta))
    return out


def _primary(p: phrases.Phrase) -> tuple[str, str]:
    mv = next((m for m in p.moves if m.kind == "adjust"), p.moves[0])
    row = _row(mv)
    return key(row.module, row.param), row.unit


def property_cases(body: str) -> list[Case]:
    out = []
    by_key = {p.key: p for p in phrases.PHRASES}
    undo = lambda: step("darkroom_undo_last", BUDGET_MOVE)  # noqa: E731
    say = lambda t: step("darkroom_say", BUDGET_SAY, phrase=t)  # noqa: E731
    for p in phrases.PHRASES:
        syn = p.synonyms[0]
        k, unit = _primary(p)
        wb = _phrase_wb_expect(p)
        if wb is not None:
            if wb["expect"] == "typed_error":
                # rows 458/461: a "say" in this sequence refuses before the
                # monotonic property could ever be observed on `k`.
                exp = _wb_refusal_expect(wb)
            else:
                # Fix round 1 (review I4): pinned and live -- the monotonic
                # property is observed on the real target key, not `k`
                # (which the write never touches once pinned).
                exp = Expect("property", [Check("M1", kind="monotonic", target={
                    "key": wb["m1_key"], "pairs": [[0, 1], [2, 3], [4, 5]]})], property="P4")
        elif p.key in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        else:
            exp = Expect("property", [Check("M1", kind="monotonic", target={
                "key": k, "pairs": [[0, 1], [2, 3], [4, 5]]})], property="P4")
        out.append(_case(case_id(PREFIX, "p4", p.key, body), body, "P1",
                         [say(f"{syn} a touch"), undo(), say(syn), undo(), say(f"{syn} a lot")],
                         exp))
    for a, b in OPPOSED:
        pa, pb = by_key[a], by_key[b]
        k, unit = _primary(pa)
        wb = _phrase_wb_expect(pa)
        if wb is not None:
            if wb["expect"] == "typed_error":
                exp = _wb_refusal_expect(wb)
            else:
                # Fix round 1 (review I4): restore-check the real target
                # key, not `k` (unaffected once pinned).
                tk = wb["m1_key"]
                exp = Expect("property", [m1_restored([tk], {tk: wb["unit"]}),
                                          Check("M2", stat="L", region="frame", sign=0,
                                                kind="unchanged")], property="P5")
        elif a in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        else:
            exp = Expect("property", [m1_restored([k], {k: unit}),
                                      Check("M2", stat="L", region="frame", sign=0,
                                            kind="unchanged")], property="P5")
        out.append(_case(case_id(PREFIX, "p5", a, b, body), body, "P1",
                         [say(pa.synonyms[0]), say(pb.synonyms[0])], exp))
    for a, b in COMMUTATIVE:
        pa, pb = by_key[a], by_key[b]
        ka, _ = _primary(pa)
        kb, _ = _primary(pb)
        wb_a, wb_b = _phrase_wb_expect(pa), _phrase_wb_expect(pb)
        refused_a = wb_a is not None and wb_a["expect"] == "typed_error"
        refused_b = wb_b is not None and wb_b["expect"] == "typed_error"
        if refused_a and refused_b:
            # Both sides refuse (neither say in the sequence ever lands):
            # the whole sequence is honestly a single refusal.
            exp = _wb_refusal_expect(wb_a)
        elif refused_a or refused_b:
            # Fix round 1 (review I3): exactly one side refuses -- the
            # OTHER side's say still succeeds both times it runs (steps 0
            # and 5, or 1 and 4), so the whole six-step sequence is not
            # honestly `typed_error` (that was I3's bug: it turned an
            # honest partial pass into a deterministic `failed`). Keep the
            # case a "property" P2, scoped to the surviving non-WB key
            # only (the refused key never moves, so comparing it would be
            # vacuous), plus an explicit envelope `ok == false` and error
            # code check on each of the refused side's own two say steps
            # -- the honest, checked version of "this half refuses".
            if refused_a:
                ok_key, refused_route, refused_steps = kb, wb_a, (0, 5)
            else:
                ok_key, refused_route, refused_steps = ka, wb_b, (1, 4)
            codes = list(refused_route["codes"])
            code_target = ({"path": "error.code", "op": "eq", "value": codes[0]} if
                           len(codes) == 1 else
                           {"path": "error.code", "op": "in", "value": codes})
            checks = [Check("M1", kind="step_equal", target={"keys": [ok_key], "a": 2, "b": 6})]
            for i in refused_steps:
                checks.append(Check("envelope", step=i, target={
                    "path": "ok", "op": "eq", "value": False}))
                checks.append(Check("envelope", step=i, target=code_target))
            exp = Expect("property", checks, property="P2")
        elif wb_b is not None:
            # Fix round 1 (review I4): pinned and live -- compare the real
            # target key, not `kb` (unaffected once pinned).
            exp = Expect("property", [Check("M1", kind="step_equal", target={
                "keys": [ka, wb_b["m1_key"]], "a": 2, "b": 6})], property="P2")
        else:
            exp = Expect("property", [Check("M1", kind="step_equal", target={
                "keys": [ka, kb], "a": 2, "b": 6})], property="P2")
        out.append(_case(case_id(PREFIX, "p2", a, b, body), body, "P1",
                         [say(pa.synonyms[0]), say(pb.synonyms[0]), undo(), undo(),
                          say(pb.synonyms[0]), say(pa.synonyms[0])], exp))
    return out


def nex5n_cases() -> list[Case]:
    body = "sony_nex5n"
    s = vocab.spec("temperature", "temperature")
    k = key(s.module, s.param)
    #: Rows 458/461: the sony_nex5n hero is preset 4 too, so this whole
    #: hand-built WB set (like param_cases' generic one) refuses now.
    wb_route = _wb_route_a(k)
    wb_refused = wb_route is not None and wb_route["expect"] == "typed_error"
    wb_pinned_live = wb_route is not None and not wb_refused
    pre = [] if wb_route is not None else _tint_reset()
    out = []
    for label, v in (("set_lo", s.lo), ("set_hi", s.hi), ("set_mid", _mid(s)),
                     ("set_3200", 3200.0), ("set_5500", 5500.0), ("set_6500", 6500.0)):
        if wb_refused:
            out.append(_case(case_id(PREFIX, "wb", body, label), body, "P1", [_set(s, v)],
                             _wb_refusal_expect(wb_route)))
            continue
        if wb_pinned_live:
            out.append(_case(case_id(PREFIX, "wb", body, label), body, "P1", [_set(s, v)],
                             _wb_route_a_pinned_edit(wb_route, value=v)))
            continue
        out.append(_case(case_id(PREFIX, "wb", body, label), body, "P1", [*pre, _set(s, v)],
                         Expect("edit", [m1_equals(s.module, s.param, v, s.unit)], _side(s))))
    for n in (1, -1, 3, -3):
        if wb_refused:
            out.append(_case(case_id(PREFIX, "wb", body, _signed("adjust", n)), body, "P1",
                             [_adjust(s, n * s.step)], _wb_refusal_expect(wb_route)))
            continue
        if wb_pinned_live:
            out.append(_case(case_id(PREFIX, "wb", body, _signed("adjust", n)), body, "P1",
                             [_adjust(s, n * s.step)],
                             _wb_route_a_pinned_edit(wb_route, sign=(1 if n > 0 else -1))))
            continue
        chk = m1_delta(s.module, s.param, n * s.step, s.unit, s.step)
        chk.target["from"] = 2
        out.append(_case(case_id(PREFIX, "wb", body, _signed("adjust", n)), body, "P1",
                         [*pre, _set(s, _mid(s)), _adjust(s, n * s.step)],
                         Expect("edit", [chk], _side(s))))
    if wb_refused:
        # Fix round 1 (review I2): see param_cases' matching comment -- keep
        # the antecedent's own refused write as a real step, expect the
        # undo's own "noop" (an empty journal), not module_unavailable.
        undo_expect = checkmap.wb_route_a_undo_expect(_CMAP, wb_route)
        undo_exp = Expect("typed_error", codes=list(undo_expect["codes"]))
        out.append(_case(case_id(PREFIX, "wb", body, "undo_after_set"), body, "P1",
                         [_set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)], undo_exp))
        out.append(_case(case_id(PREFIX, "wb", body, "undo_after_adjust"), body, "P1",
                         [_adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)], undo_exp))
    elif wb_pinned_live:
        restore = _wb_route_a_pinned_restore(wb_route)
        out.append(_case(case_id(PREFIX, "wb", body, "undo_after_set"), body, "P1",
                         [_set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)], restore))
        out.append(_case(case_id(PREFIX, "wb", body, "undo_after_adjust"), body, "P1",
                         [_adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)], restore))
    else:
        out.append(_case(case_id(PREFIX, "wb", body, "undo_after_set"), body, "P1",
                         [*pre, _set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)],
                         Expect("workflow", [m1_restored([k], {k: "K"}, 1)])))
        out.append(_case(case_id(PREFIX, "wb", body, "undo_after_adjust"), body, "P1",
                         [*pre, _adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)],
                         Expect("workflow", [m1_restored([k], {k: "K"}, 1)])))
    by_key = {p.key: p for p in phrases.PHRASES}
    for pk in ("warmer", "cooler"):
        p = by_key[pk]
        for label, text in (("bare", p.synonyms[0]), ("touch", f"{p.synonyms[0]} a touch"),
                            ("lot", f"{p.synonyms[0]} a lot"),
                            ("explicit", f"{p.synonyms[0]} by 500k")):
            out.append(_case(case_id(PREFIX, "wb", body, pk, label), body, "P1",
                             [step("darkroom_say", BUDGET_SAY, phrase=text)], _phrase_expect(p)))
    return out


def generate() -> list[Case]:
    out: list[Case] = []
    for body in BODIES:
        for s in live_params():
            out.extend(param_cases(s, body))
        out.extend(hue_wrap_cases(body))
        out.extend(module_cases(body))
        out.extend(tint_cases(body))
        out.extend(unreachable_cases(body))
        out.extend(phrase_cases(body))
        out.extend(property_cases(body))
    out.extend(nex5n_cases())
    return out


def expected_count() -> int:
    live = len(live_params())
    keys = len(phrases.PHRASES)
    syns = sum(len(p.synonyms) for p in phrases.PHRASES)
    hues = sum(p.wraps for p in live_params())
    mods = len({p.module for p in live_params()})
    per_body = (live * (6 + 4 + 1 + 2 + 2 + 2) + hues * 2 + mods * 3 + 6 + 2 + keys * 4 + syns
                + keys + len(OPPOSED) + len(COMMUTATIVE))
    return 2 * per_body + 20
