"""Layer 4: looks (spec 3.4). 1 golden-chart case + 234 on the chart + 234 on
a real hero frame (direction only). Moves are derived at run time from the
per-run calibration (the golden case measures it), never hand-tuned."""

from __future__ import annotations

import math

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.layers.common import BODIES
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "looks", "lk"
SOURCED = ("documented", "community-consensus")
#: (night-1 fix, class LK-frame-switch-relaunch): darkroom.open cannot switch
#: onto a frame imported earlier in the same darktable session (product
#: honest limit R20, "wrong image loaded"), so every chart<->hero switch
#: costs a relaunch (about 45-60 s). The interleaved generation order below
#: (chart, hero, chart, hero, ...) switched on every single case; grouping by
#: role removes all but one switch per session.
FRAME_ROLE_ORDER = ("chart", "hero")
TONE_PATCH = {"shadows_ev": "patch:ramp_-4", "mids_ev": "patch:ramp_+0",
              "highlights_ev": "patch:ramp_+2"}
TONE_REGION = {"shadows_ev": "shadows", "mids_ev": "frame", "highlights_ev": "highlights"}


def gaps(look: dict) -> list[str]:
    t = look["targets"]
    out = []
    if t.get("grain"):
        out.append("grain")
    if t.get("vignette"):
        out.append("vignette")
    if t.get("clarity"):
        out.append("clarity")
    if t.get("white_point", 100) != 100:
        out.append("white_point")
    if any((t.get("hue_shifts") or {}).get(k) for k in ("skin_deg", "sky_deg", "foliage_deg")):
        out.append("hue_shifts")
    if t.get("bw_response"):
        out.append("bw_response")
    first = (look.get("dt_modules") or [""])[0].lower()
    if "lut" in first:
        out.append("lut3d")
    return out


def _hue_axis(hue: float) -> tuple[str, int]:
    s, c = math.sin(math.radians(hue)), math.cos(math.radians(hue))
    return ("b", 1 if s > 0 else -1) if abs(s) >= abs(c) else ("a", 1 if c > 0 else -1)


def chart_checks(look: dict, tol: dict) -> tuple[list[Check], list[dict]]:
    t, conf = look["targets"], look["target_confidence"]
    checks: list[Check] = []
    tags: list[dict] = []

    def add(ch: Check, component: str) -> None:
        c = conf.get(component, "inferred")
        if c not in SOURCED:  # inferred: sign only
            ch.target = None
        checks.append(ch)
        tags.append({"confidence": c, "component": component})

    tone = t.get("tone") or {}
    for k, patch in TONE_PATCH.items():
        v = tone.get(k) or 0
        if v:
            add(Check("M2", stat="ev", region=patch, sign=1 if v > 0 else -1,
                      target={"value": v, "tol": tol[conf.get("tone", "inferred")]["ev"]}),
                "tone")
    if tone.get("contrast"):
        v = tone["contrast"]
        add(Check("M6", stat="slope", region="chart_tone", sign=1 if v > 0 else -1, kind="frac",
                  target={"value": v, "tol": tol[conf.get("tone", "inferred")]["sat"]}), "tone")
    sat = t.get("saturation_delta") or 0
    if sat:
        add(Check("M5", stat="C", region="chart_chromatic", sign=1 if sat > 0 else -1,
                  kind="frac", target={"value": sat, "tol": tol[conf.get(
                      "saturation_delta", "inferred")]["sat"]}), "saturation_delta")
    gc = t.get("grey_cast") or {}
    for axis in ("a", "b"):
        v = gc.get(axis) or 0
        if v:
            add(Check("M4", stat=axis, region="patch:ramp_+0", sign=1 if v > 0 else -1,
                      target={"value": v, "tol": tol[conf.get("grey_cast", "inferred")]["ab"]}),
                "grey_cast")
    st = t.get("split_toning") or {}
    for end, patch in (("shadow", "patch:ramp_-3"), ("highlight", "patch:ramp_+2")):
        hue, s = st.get(f"{end}_hue"), st.get(f"{end}_sat") or 0
        if hue is not None and s:
            axis, sign = _hue_axis(float(hue))
            add(Check("M4", stat=axis, region=patch, sign=sign), "split_toning")
    if t.get("black_lift"):
        add(Check("M3", stat="L", region="patch:ramp_-5", sign=1 if t["black_lift"] > 0 else -1),
            "black_lift")
    if not checks:
        checks.append(Check("M2", stat="L", region="patch:ramp_+0", sign=0, kind="unchanged"))
        tags.append({"confidence": look["confidence"], "component": "neutral"})
    return checks, tags


def frame_checks(look: dict) -> tuple[list[Check], list[dict]]:
    t, conf = look["targets"], look["target_confidence"]
    checks: list[Check] = []
    tags: list[dict] = []
    tone = t.get("tone") or {}
    for k, region in TONE_REGION.items():
        v = tone.get(k) or 0
        if v:
            checks.append(Check("M2", stat="L", region=region, sign=1 if v > 0 else -1))
            tags.append({"confidence": conf.get("tone", "inferred"), "component": "tone"})
    if tone.get("contrast"):
        checks.append(Check("M6", stat="spread", region="frame",
                            sign=1 if tone["contrast"] > 0 else -1))
        tags.append({"confidence": conf.get("tone", "inferred"), "component": "tone"})
    if t.get("saturation_delta"):
        checks.append(Check("M5", stat="C", region="frame",
                            sign=1 if t["saturation_delta"] > 0 else -1))
        tags.append({"confidence": conf.get("saturation_delta", "inferred"),
                     "component": "saturation_delta"})
    gc = t.get("grey_cast") or {}
    if gc.get("b"):
        checks.append(Check("M4", stat="rb", region="frame", sign=1 if gc["b"] > 0 else -1))
        tags.append({"confidence": conf.get("grey_cast", "inferred"), "component": "grey_cast"})
    if t.get("black_lift"):
        checks.append(Check("M3", stat="p01L", region="frame", sign=1))
        tags.append({"confidence": conf.get("black_lift", "inferred"), "component": "black_lift"})
    if not checks:
        checks.append(Check("M2", stat="L", region="frame", sign=0, kind="unchanged"))
        tags.append({"confidence": look["confidence"], "component": "neutral"})
    return checks, tags


def generate(tax: Taxonomy) -> list[Case]:
    tol = tax.look_conventions["suggested_tolerances"]
    out = [Case(id=case_id(PREFIX, "golden", "chart"), layer=LAYER, tier="P1", mode="tool",
                steps=[Step("chart_golden", {}, 300.0, False)],
                expect=Expect("workflow", [Check("M9", kind="unchanged"),
                                           Check("extra", target={"path": "calibration_ok",
                                                                  "op": "truthy"})],
                              property="P7"),
                frame_role="chart", source={"file": "looks.json", "id": "golden"})]
    for i, look in enumerate(tax.looks):
        tier = "P1" if look["confidence"] in SOURCED else "P2"
        g = gaps(look)
        checks, tags = chart_checks(look, tol)
        meta = {"look": look["id"], "family": look["family"], "gaps": g, "check_tags": tags,
                "partial_min": 0.6, "monochrome": (look["targets"].get("saturation_delta") == -1)}
        out.append(Case(id=case_id(PREFIX, look["id"], "chart"), layer=LAYER, tier=tier,
                        mode="tool", steps=[Step("look_plan", {"look_id": look["id"],
                                                               "image": "{frame:chart}"},
                                                 40.0, True)],
                        expect=Expect("edit", checks, {"changed": bool(checks and
                                                                    checks[0].sign)}),
                        body="chart", frame_role="chart",
                        source={"file": "looks.json", "id": look["id"]}, meta=meta))
        body = BODIES[i % 2]
        fchecks, ftags = frame_checks(look)
        out.append(Case(id=case_id(PREFIX, look["id"], body), layer=LAYER, tier=tier,
                        mode="tool", steps=[Step("look_plan", {"look_id": look["id"],
                                                               "image": "{frame:hero}"},
                                                 40.0, True)],
                        expect=Expect("edit", fchecks, {"changed": bool(fchecks[0].sign)}),
                        body=body, frame_role="hero",
                        source={"file": "looks.json", "id": look["id"]},
                        meta=dict(meta, check_tags=ftags, gaps=[])))
    return order_by_frame_role(out)


def order_by_frame_role(cases: list[Case]) -> list[Case]:
    """A pure, stable sort: every case keeps its relative position among cases
    of the same `frame_role`, so a session that hands these out in order does
    at most one chart<->hero switch instead of one per case."""
    return sorted(cases, key=lambda c: FRAME_ROLE_ORDER.index(c.frame_role))


EXPECTED = 1 + 234 * 2
