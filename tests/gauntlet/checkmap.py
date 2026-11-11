"""requests.json check strings -> engine checks + Scouter capabilities (spec 2.1).

Pure. The rules live in data/check_map.json; this module only applies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gauntlet.case import Check, Expect


@dataclass(frozen=True)
class PartCheck:
    text: str
    concept: str
    sign: int
    kind: str
    metric: str
    stat: str | None
    region: str
    capability: str | None


@dataclass(frozen=True)
class RequestPlan:
    request_id: str
    klass: str  # in | partial | out | impossible | workflow
    parts: tuple[PartCheck, ...]
    capabilities: tuple[str, ...]
    workflow_verb: str | None = None
    step_back: int = 0
    unmapped: tuple[str, ...] = field(default=())


def _strip_parens(s: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def split_parts(stat: str, cmap: dict) -> list[str]:
    s = stat.strip().lower()
    s = cmap["rewrites"].get(s, s)
    s = _strip_parens(s)
    parts = [s]
    for sep in cmap["separators"]:
        nxt: list[str] = []
        for p in parts:
            nxt.extend(x.strip() for x in p.split(sep) if x.strip())
        parts = nxt
    return parts


def direction_of(text: str, cmap: dict) -> dict:
    t = text.strip().lower()
    for rule in cmap["direction_rules"]:
        if rule["match"] in t:
            return rule
    return {"match": "", "kind": "none", "sign": 0}


def _embedded_sign(part: str, cmap: dict) -> tuple[str, int | None]:
    for w in cmap["part_direction_words"]:
        if part.endswith(w["suffix"]):
            return part[: -len(w["suffix"])].strip(), w["sign"]
    return part, None


def concept_of(part: str, cmap: dict) -> tuple[str, str | None]:
    """(concept, region override) for one stat part, or ("unmapped", None)."""
    for rule in cmap["stat_rules"]:
        if rule["match"] in part:
            region = None
            for rr in rule.get("region_rules", []):
                if rr["match"] in part:
                    region = rr["region"]
            return rule["concept"], region
    return "unmapped", None


def _capability(concept: str, sign: int, cmap: dict) -> str | None:
    caps = cmap["concepts"][concept]["caps"]
    if "*" in caps:
        return caps["*"]
    return caps.get(str(sign))


def plan_request(req: dict, cmap: dict) -> RequestPlan:
    rid = req["id"]
    kind = req["expected"]["kind"]
    check = req["expected"]["check"]
    if req["intent"] == "impossible" or kind == "refuse":
        return RequestPlan(rid, "impossible", (), ())
    if kind == "workflow":
        stat = check["stat_or_param"].lower()
        #: (row 501 item I, Pratik's ruling): r0066 ("client proof gallery")
        #: matches `workflow_rules`' "gallery"/"watermark" -> "out" rule
        #: before it ever reaches the "export" rule, so it generated as an
        #: out-of-scope refusal even though export_release has a
        #: client-proof preset (night-1 triage rank 11). A per-request
        #: override lets THIS ruled request (and only it) skip the general
        #: keyword match, without touching workflow_rules for every other
        #: gallery/watermark/comparison request that is genuinely out of
        #: scope.
        verb = cmap.get("workflow_verb_overrides", {}).get(rid)
        if verb is None:
            verb = "out"
            for rule in cmap["workflow_rules"]:
                if rule["match"] in stat:
                    verb = rule["verb"]
                    break
        d = direction_of(check["direction"], cmap)
        klass = "out" if verb == "out" else "workflow"
        return RequestPlan(rid, klass, (), (), workflow_verb=verb, step_back=int(d.get("steps", 0)))
    overrides = cmap["request_overrides"].get(rid)
    if overrides:
        raw = [(o["concept"], o["concept"], o["sign"], "delta", None) for o in overrides]
    else:
        parts = split_parts(check["stat_or_param"], cmap)
        dir_text = check["direction"]
        dir_parts = [x.strip() for x in dir_text.split(",") if x.strip()]
        whole = direction_of(dir_text, cmap)
        raw = []
        for i, p in enumerate(parts):
            core, emb = _embedded_sign(p, cmap)
            concept, region = concept_of(core, cmap)
            if emb is not None:
                sign, kind_ = emb, "delta"
            elif len(dir_parts) == len(parts) and len(parts) > 1:
                d = direction_of(dir_parts[i], cmap)
                sign, kind_ = d["sign"], d["kind"]
            else:
                sign, kind_ = whole["sign"], whole["kind"]
            raw.append((p, concept, sign, kind_, region))
    out: list[PartCheck] = []
    unmapped: list[str] = []
    for text, concept, sign, kind_, region in raw:
        if concept == "unmapped":
            unmapped.append(text)
            continue
        c = cmap["concepts"][concept]
        if "fixed_sign" in c:
            sign = c["fixed_sign"]
        cap = _capability(concept, sign, cmap) if concept != "none" else None
        out.append(
            PartCheck(
                text=text,
                concept=concept,
                sign=sign,
                kind=kind_,
                metric=c["metric"],
                stat=c.get("stat"),
                region=region or c.get("region", "frame"),
                capability=cap,
            )
        )
    caps = tuple(dict.fromkeys(p.capability for p in out if p.capability))
    served = [p for p in out if p.capability]
    if unmapped:
        klass = "unmapped"
    elif served and len(served) == len(out):
        klass = "in"
    elif served:
        klass = "partial"
    else:
        klass = "out"
    return RequestPlan(rid, klass, tuple(out), caps, unmapped=tuple(unmapped))


def wb_route_a_expect(
    cmap: dict, case_key: str, preset: int | None, *, pinned: bool
) -> dict | None:
    """What a warmth case must verify on a frame in camera reference (late)
    mode, from check_map.json's `wb_route_a` block (Scouter decision rows
    458/461). `case_key` is a phrase key ("warmer") or a module.param key
    ("temperature.temperature"); `pinned` is whether Scouter's
    `channelmixerrgb.temperature` action path is pinned (see
    `route_a_pinned`). None when the block does not apply (another preset,
    another case)."""
    r = cmap["wb_route_a"]
    if preset != r["applies_when"]["equals"]:
        return None
    if case_key not in r["phrase_keys"] and case_key not in r["param_keys"]:
        return None
    if not pinned:
        u = r["until_pinned"]
        return {"expect": u["expect"], "codes": list(u["accept_codes"])}
    return {
        "expect": "edit",
        "m1_key": r["m1_key"]["to"],
        "replaces": r["m1_key"]["from"],
        "unit": r["m1_key"]["unit"],
        "allowed": list(r["derived_keys"]),
        "unchanged": list(r["unchanged_keys"]),
        "direction_verified": bool(r["direction_verified"]),
    }


def wb_route_a_pinned_edit(
    route: dict, *, sign: int | None = None, value: float | None = None, start: int = 0
) -> Expect:
    """Review round 2, finding 10 (GC-1): shared with `coverage.py` (its own
    `_wb_route_a_pinned_edit`, fix round 1 review I4) so every generator
    that wires `wb_route_a_expect`/`_expect_for_hero` scores the SAME way
    once `channelmixerrgb.temperature` is pinned, not just coverage's. Once
    pinned, a warmth write actually lands there, not on
    `temperature.temperature` -- build the Expect from the dict
    `wb_route_a_expect`/`_expect_for_hero` returns (`m1_key`, `derived_keys`,
    `unchanged_keys`), rather than falling through to the ordinary
    `temperature.temperature` checks (which the write never touches once
    pinned) or a permissive `Expect("edit", [])` placeholder.
    `direction_verified` is False in check_map.json until the host probe,
    so a sign is asserted only when it is True; otherwise the case still
    verifies that the value changed (never a specific value: an unverified
    sign means "moved, direction unknown") plus the derived keys and the
    untouched WB preset, with a `sign_unverified` tag on the `changed`
    check instead of guessing a sign. `route["unit"]` is always
    check_map.json's own canonical unit string (wb_route_a.m1_key.unit is
    "K") -- no further unit-name normalization needed here, unlike
    layers/common.py's generic `sidecar()` (which this module cannot
    import: `layers/common.py` itself imports from here).

    Review round 3, GR-1 (P1 blocker): the tag key here is `sign_unverified`,
    deliberately never `needs_review` or `disputed`. gauntlet/evaluate.py's
    typed needs_review path (added review round 2, GD-2) reads only the
    `disputed` key, which the install layer uses. Before that evaluator
    change, this tag was read by nothing (a comment only); had it shared the
    evaluator's key, every pinned Route-A `equals` case (an explicit
    `value=`, no sign involved at all) would have been opted into the same
    "never hard-fail" path -- a wrong Kelvin value from a real product bug
    would have silently scored `needs_review` instead of `failed`. So the
    tag is inert to the evaluator and applies ONLY to the `changed`-kind
    branch below, never to `equals`."""
    to_key = route["m1_key"]
    to_module, to_param = to_key.split(".", 1)
    unit = route["unit"]
    #: `start`: snapshot index the move is measured from (0 = case start; 1
    #: after one prep/setup step, so the move check does not span the prep).
    if value is not None:
        move = Check("M1", kind="equals", target={"key": to_key, "value": value, "unit": unit})
    else:
        tags = (
            {}
            if route["direction_verified"]
            else {
                "sign_unverified": "route A pinned but direction_verified is False in "
                "check_map.json -- sign unverified until the host probe"
            }
        )
        s = sign if route["direction_verified"] else 0
        target = {"key": to_key, "unit": unit}
        if start:
            target["from"] = start
        move = Check("M1", kind="changed", sign=s or 0, target=target, tags=tags)
    unchanged = Check("M1", kind="unchanged", target={"keys": list(route["unchanged"])})
    side = {
        "changed": True,
        "key": to_key,
        "units": {to_key: unit},
        "allowed": list(route["allowed"]),
    }
    return Expect("edit", [move, unchanged], side)


def route_a_pinned(cmap: dict) -> bool:
    """Is route A's action path pinned in Scouter's whitelist right now?"""
    from scouter.core import dtbridge

    module, param = cmap["wb_route_a"]["until_pinned"]["pin"]
    return (module, param) in dtbridge.ACTION_PATHS


#: Rows 458/461 (night-1 triage class COV-WB-preset4-expectation): every
#: corpus hero frame, every body (olympus, sony_a7, sony_nex5n), is
#: darktable's temperature preset 4 -- confirmed by the recorded night-1
#: run. `wb_route_a_expect_for_hero`/`wb_route_a_tint_expect_for_hero` are
#: the convenience form the generators (coverage.py, scenarios.py) actually
#: call; the preset-taking functions above stay generic for anything that
#: is not a hero-frame case (there is none today, but the taxonomy layer
#: should not have to know the corpus fact to use the check map correctly).
HERO_WB_PRESET = 4


def wb_route_a_expect_for_hero(cmap: dict, case_key: str) -> dict | None:
    return wb_route_a_expect(cmap, case_key, HERO_WB_PRESET, pinned=route_a_pinned(cmap))


def wb_route_a_tint_expect_for_hero(cmap: dict) -> dict | None:
    return wb_route_a_tint_expect(cmap, HERO_WB_PRESET)


def wb_route_a_tint_expect(cmap: dict, preset: int | None) -> dict | None:
    """What a `temperature.tint` case (set, adjust, or a tint phrase such as
    `tint_green`/`tint_magenta`) must verify on a preset-4 frame: tint has no
    color-calibration reroute target (check_map.json's `wb_route_a.tint_note`),
    so `scouter.core.darkroom.refuse_wb_late` refuses it unconditionally --
    unlike Kelvin/warmer/cooler, pinning `channelmixerrgb.temperature` makes
    no difference here. None when the block does not apply (another preset)."""
    r = cmap["wb_route_a"]
    if preset != r["applies_when"]["equals"] or not r.get("tint_always_unavailable"):
        return None
    u = r["until_pinned"]
    return {"expect": "typed_error", "codes": list(u["accept_codes"])}


def wb_route_a_nudge_expect(cmap: dict, preset: int | None) -> dict | None:
    """What a `darkroom_nudge` on `temperature.temperature` must verify on a
    preset-4 frame (fix round 1, review I4): unlike `darkroom_set`/`adjust`,
    `darkroom.nudge` never re-routes onto color calibration at all -- it
    always calls `refuse_wb_late` unconditionally -- so it refuses
    `module_unavailable` whether or not `channelmixerrgb.temperature` is
    pinned. Like `wb_route_a_tint_expect`, pin status is irrelevant, so this
    does not take one. None when the block does not apply (another preset)."""
    r = cmap["wb_route_a"]
    if preset != r["applies_when"]["equals"]:
        return None
    u = r["until_pinned"]
    return {"expect": "typed_error", "codes": list(u["accept_codes"])}


def wb_route_a_nudge_expect_for_hero(cmap: dict) -> dict | None:
    return wb_route_a_nudge_expect(cmap, HERO_WB_PRESET)


def wb_route_a_undo_expect(cmap: dict, antecedent: dict | None) -> dict | None:
    """What `darkroom_undo_last` must verify right after a WB route-A case
    (a set/adjust/nudge on `temperature.temperature`, or any `temperature.tint`
    move) that was itself refused: `antecedent` is that move's own
    `wb_route_a_expect`/`wb_route_a_tint_expect` result. When it was a
    refusal, no write ever reached the journal (`refuse_wb_late` runs before
    `journal.begin`), so the undo sees an EMPTY journal and answers its own
    fixed code -- `darkroom_undo_last`'s "noop" outcome (fix round 1, review
    I2; NOT the antecedent's own code -- the undo call never reaches
    `refuse_wb_late` itself, so it cannot answer `module_unavailable`), from
    check_map.json's `wb_route_a.undo_after_refused_codes`. The antecedent's
    refused call must still run as a real step first (it is what leaves the
    journal empty for the undo to find) -- this only decides the Expect, not
    whether the case keeps that step. None when the antecedent was not a
    refusal (route A pinned and live) or was not itself a WB route-A case --
    the ordinary `m1_restored` undo check applies there."""
    if antecedent is None or antecedent.get("expect") != "typed_error":
        return None
    r = cmap["wb_route_a"]
    return {"expect": r["undo_after_refused_expect"], "codes": list(r["undo_after_refused_codes"])}
