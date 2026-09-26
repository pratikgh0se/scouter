"""Layer 1: coverage (spec 3.1). 648 cases per main body + 20 NEX-5N WB cases.

Every case runs on the body's `hero` frame. P0 core per body: every live
param x {set lo, set hi, set mid, adjust +1, adjust -1, undo after set} plus
the 27 phrase keys bare (159 per body)."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, case_id
from scouter.core import phrases, vocab
from tests.gauntlet.layers.common import (
    BODIES,
    BUDGET_MOVE,
    BUDGET_SAY,
    clamp_reported,
    frame,
    key,
    m1_delta,
    m1_equals,
    m1_restored,
    sidecar,
    step,
)

LAYER = "coverage"
PREFIX = "cov"
HERO = frame("hero")
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


def _mid(s: vocab.ParamSpec) -> float:
    return round((s.lo + s.hi) / 2, 6)


def _neutral(s: vocab.ParamSpec) -> float:
    if s.neutral is not None:
        return s.neutral
    if s.default is not None:
        return s.default
    return round(s.lo + (s.hi - s.lo) / 4, 6)


def _case(cid: str, body: str, tier: str, steps: list, expect: Expect, *,
          meta: dict | None = None) -> Case:
    return Case(id=cid, layer=LAYER, tier=tier, mode="tool", steps=steps, expect=expect,
                body=body, frame_role="hero", source={"file": "vocab.py"}, meta=meta or {})


def _set(s: vocab.ParamSpec, value: float):
    return step("darkroom_set", BUDGET_MOVE, module=s.module, param=s.param, value=value)


def _adjust(s: vocab.ParamSpec, delta: float):
    return step("darkroom_adjust", BUDGET_MOVE, module=s.module, param=s.param, delta=delta)


def _side(s: vocab.ParamSpec) -> dict:
    return sidecar(s.module, s.param, s.unit)


def _tint_reset() -> list:
    """W4: Kelvin is readable from the XMP only at the WB table's calibrated
    tint (1.0), so every white-balance Kelvin case first sets tint 1.0."""
    t = vocab.spec("temperature", "tint")
    return [step("darkroom_set", BUDGET_MOVE, module=t.module, param=t.param, value=1.0)]


def param_cases(s: vocab.ParamSpec, body: str) -> list[Case]:
    k = key(s.module, s.param)
    cid = lambda v: case_id(PREFIX, k, body, v)  # noqa: E731
    out: list[Case] = []
    pre = _tint_reset() if (s.module, s.param) == ("temperature", "temperature") else []
    o = len(pre)
    values = [("set_lo", s.lo, "P0"), ("set_hi", s.hi, "P0"), ("set_mid", _mid(s), "P0"),
              ("set_neutral", _neutral(s), "P1")]
    for label, v, tier in values:
        out.append(_case(cid(label), body, tier, [*pre, _set(s, v)],
                         Expect("edit", [m1_equals(s.module, s.param, v, s.unit)], _side(s))))
    for label, v in (("set_lo_minus", s.lo - s.step), ("set_hi_plus", s.hi + s.step)):
        if s.wraps:
            want = v % 360.0
            out.append(_case(cid(label), body, "P1", [*pre, _set(s, v)],
                             Expect("edit", [m1_equals(s.module, s.param, want, s.unit)],
                                    _side(s))))
        else:
            want = s.lo if v < s.lo else s.hi
            out.append(_case(cid(label), body, "P1", [*pre, _set(s, v)],
                             Expect("edit", [m1_equals(s.module, s.param, want, s.unit),
                                             clamp_reported()], _side(s)),
                             meta={"accept_refusal": True}))
    for n in (1, -1, 3, -3):
        chk = m1_delta(s.module, s.param, n * s.step, s.unit, s.step)
        chk.target["from"] = 1 + o
        out.append(_case(cid(_signed("adjust", n)), body, "P0" if abs(n) == 1 else "P1",
                         [*pre, _set(s, _mid(s)), _adjust(s, n * s.step)],
                         Expect("edit", [chk], _side(s))))
    if s.wraps:
        over = _case(cid("adjust_overflow"), body, "P1",
                     [*pre, _set(s, s.hi - s.step), _adjust(s, 3 * s.step)],
                     Expect("edit", [m1_equals(s.module, s.param, (s.hi + 2 * s.step) % 360,
                                               s.unit)], _side(s)))
    else:
        over = _case(cid("adjust_overflow"), body, "P1",
                     [*pre, _set(s, s.hi - s.step), _adjust(s, 3 * s.step)],
                     Expect("edit", [m1_equals(s.module, s.param, s.hi, s.unit),
                                     clamp_reported()], _side(s)))
    out.append(over)
    for n in (1, -1):
        chg = Check("M1", kind="changed", sign=n, target={"key": k, "unit": s.unit, "from": 1 + o})
        out.append(_case(cid(_signed("nudge", n)), body, "P1",
                         [*pre, _set(s, _mid(s)),
                          step("darkroom_nudge", BUDGET_MOVE, module=s.module, param=s.param,
                               steps=n)],
                         Expect("edit", [chg], _side(s))))
    units = {k: s.unit}
    out.append(_case(cid("undo_after_set"), body, "P0",
                     [*pre, _set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)],
                     Expect("workflow", [m1_restored([k], units, o)])))
    out.append(_case(cid("undo_after_adjust"), body, "P1",
                     [*pre, _adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)],
                     Expect("workflow", [m1_restored([k], units, o)])))
    out.append(_case(cid("repeat_set"), body, "P1", [*pre, _set(s, _mid(s)), _set(s, _mid(s))],
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
    for v in (0.8, 0.9, 1.1, 1.2):
        out.append(_case(case_id(PREFIX, "temperature.tint", body, f"set_{v}"), body, "P1",
                         [_set(t, v)], Expect("edit", [], {"changed": True})))
    te = Expect("typed_error", codes=["value_unknown"])
    out.append(_case(case_id(PREFIX, "temperature.tint", body, "adjust"), body, "P1",
                     [_adjust(t, 0.05)], te))
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


def _phrase_expect(p: phrases.Phrase, sign: float = 1.0) -> Expect:
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
                                target={"key": key(row.module, row.param), "unit": row.unit}))
    metric, stat, region, sgn = PHRASE_STAT.get(p.key, (None, None, None, 0))
    if metric:
        checks.append(Check(metric, stat=stat, region=region, sign=sgn))
    return Expect("edit", checks, {"changed": True})


def phrase_cases(body: str) -> list[Case]:
    out = []
    for p in phrases.PHRASES:
        syn = p.synonyms[0]
        forms = [("bare", syn, "P0"), ("touch", f"{syn} a touch", "P1"),
                 ("lot", f"{syn} a lot", "P1")]
        for label, text, tier in forms:
            out.append(_case(case_id(PREFIX, "phrase", p.key, body, label), body, tier,
                             [step("darkroom_say", BUDGET_SAY, phrase=text)], _phrase_expect(p)))
        row = _row(p.moves[0])
        by, amount = EXPLICIT_BY_UNIT.get(row.unit, ("0.2", 0.2))
        text = f"{syn} by {by}"
        one = len(p.moves) == 1 and p.moves[0].kind == "adjust"
        if p.key in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        elif not one:
            exp = Expect("typed_error", codes=["usage"])
        elif p.moves[0].module == phrases.TONE:
            exp = _phrase_expect(p)
        else:
            sgn = 1 if p.moves[0].notches > 0 else -1
            exp = Expect("edit", [m1_delta(row.module, row.param, sgn * amount, row.unit,
                                           row.step)], sidecar(row.module, row.param, row.unit))
        out.append(_case(case_id(PREFIX, "phrase", p.key, body, "explicit"), body, "P1",
                         [step("darkroom_say", BUDGET_SAY, phrase=text)], exp))
    for p in phrases.PHRASES:
        for syn in p.synonyms:
            out.append(_case(case_id(PREFIX, "synonym", p.key, syn, body), body, "P1",
                             [step("darkroom_say", BUDGET_SAY, phrase=syn)], _phrase_expect(p)))
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
        if p.key in TINT_KEYS:
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
        if a in TINT_KEYS:
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
        out.append(_case(case_id(PREFIX, "p2", a, b, body), body, "P1",
                         [say(pa.synonyms[0]), say(pb.synonyms[0]), undo(), undo(),
                          say(pb.synonyms[0]), say(pa.synonyms[0])],
                         Expect("property", [Check("M1", kind="step_equal", target={
                             "keys": [ka, kb], "a": 2, "b": 6})], property="P2")))
    return out


def nex5n_cases() -> list[Case]:
    body = "sony_nex5n"
    s = vocab.spec("temperature", "temperature")
    pre = _tint_reset()
    out = []
    for label, v in (("set_lo", s.lo), ("set_hi", s.hi), ("set_mid", _mid(s)),
                     ("set_3200", 3200.0), ("set_5500", 5500.0), ("set_6500", 6500.0)):
        out.append(_case(case_id(PREFIX, "wb", body, label), body, "P1", [*pre, _set(s, v)],
                         Expect("edit", [m1_equals(s.module, s.param, v, s.unit)], _side(s))))
    for n in (1, -1, 3, -3):
        chk = m1_delta(s.module, s.param, n * s.step, s.unit, s.step)
        chk.target["from"] = 2
        out.append(_case(case_id(PREFIX, "wb", body, _signed("adjust", n)), body, "P1",
                         [*pre, _set(s, _mid(s)), _adjust(s, n * s.step)],
                         Expect("edit", [chk], _side(s))))
    k = key(s.module, s.param)
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
