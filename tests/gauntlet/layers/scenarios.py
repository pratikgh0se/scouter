"""Layer 2: scenarios (spec 3.2). Tool mode 701 (601 + the 100 P0 on the
other body), agent mode 160 (100 P0 + 60 impossible, olympus)."""

from __future__ import annotations

import re

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet import checkmap
from tests.gauntlet.checkmap import RequestPlan, plan_request
from tests.gauntlet.layers.common import (
    AGENT_TURN_TIMEOUT_S,
    BUDGET_SAY,
    frame,
    m1_restored,
    stat_check,
    step,
)
from tests.gauntlet.p0 import select_p0
from tests.gauntlet.taxonomy import Taxonomy

LAYER, LAYER_AGENT = "scenarios", "scenarios_agent"
PREFIX, PREFIX_AGENT = "scn", "sca"
EXPLICIT_OK = {"brighter", "darker"}


def body_for(rid: str) -> str:
    return "olympus" if int(rid[1:]) % 2 == 1 else "sony_a7"


def other(body: str) -> str:
    return "sony_a7" if body == "olympus" else "olympus"


def _magnitude(req: dict, cmap: dict, cap: str) -> str:
    word = cmap["magnitude_words"].get(req["magnitude"], "")
    if word.startswith("by ") and cap not in EXPLICIT_OK:
        return ""
    return word


def cap_step(cap: str, req: dict, cmap: dict, *, bit: bool = False) -> Step:
    spec = cmap["capabilities"][cap]
    if "tool" in spec:
        return step(spec["tool"], 3.5, **spec["args"])
    word = "a bit" if bit else _magnitude(req, cmap, cap)
    return step("darkroom_say", BUDGET_SAY,
                phrase=f"{spec['phrase']} {word}".strip())


def nearest_phrase(req: dict, tax: Taxonomy) -> tuple[str, str]:
    """(phrase, roadmap key) for an unserved request: the first taxonomy phrase
    of its first module that the taxonomy knows, else the sentence itself."""
    for name in req["modules"]:
        m = tax.module_by_name(name)
        if m and m["phrases"]:
            return m["phrases"][0], m["id"]
    return req["sentence"], f"unknown:{req['modules'][0] if req['modules'] else req['intent']}"


def _unserved_modules(req: dict, tax: Taxonomy) -> list[dict]:
    served = {"exposure", "sigmoid", "temperature", "colorbalancergb", "filmicrgb", "colorzones"}
    out = []
    for name in req["modules"]:
        m = tax.module_by_name(name)
        if m and m["id"] not in served and m["phrases"]:
            out.append(m)
    return out


def _wb_capability_expect(cap: str, cmap: dict) -> dict | None:
    """checkmap's wb_route_a expectation (rows 458/461) for a scenario
    capability that writes the temperature module on the corpus hero:
    warmer/cooler (Kelvin, via `wb_route_a_expect_for_hero`) or
    tint_magenta/tint_green (tint, via `wb_route_a_tint_expect_for_hero`,
    always a refusal -- tint has no color-calibration reroute). None for
    every other capability."""
    if cap in ("warmer", "cooler"):
        return checkmap.wb_route_a_expect_for_hero(cmap, cap)
    if cap in ("tint_magenta", "tint_green"):
        return checkmap.wb_route_a_tint_expect_for_hero(cmap)
    return None


def _followup_action(sentence: str, cmap: dict) -> str:
    low = sentence.lower()
    for rule in cmap["followup_rules"]:
        if rule["match"] in low:
            return rule["action"]
    return "repeat_bit"


def _workflow(plan: RequestPlan, req: dict) -> tuple[list[Step], Expect]:
    v = plan.workflow_verb
    hero = frame("hero")
    if v == "undo":
        steps = [step("darkroom_say", BUDGET_SAY, phrase="brighter")]
        if plan.step_back >= 2:
            steps.append(step("darkroom_say", BUDGET_SAY, phrase="warmer"))
        steps += [step("darkroom_undo_last", 3.5) for _ in range(max(1, plan.step_back))]
        return steps, Expect("workflow", [m1_restored([], {})])
    if v == "repeat_more":
        return ([step("darkroom_say", BUDGET_SAY, phrase="brighter"),
                 step("darkroom_say", BUDGET_SAY, phrase="brighter a bit")],
                Expect("workflow", [Check("M1", kind="changed", sign=1, target={
                    "key": "exposure.exposure", "unit": "EV", "from": 1, "to": 2})]))
    if v == "preset_save":
        name = f"htc-{req['id']}"
        return ([step("darkroom_say", BUDGET_SAY, phrase="warmer"),
                 step("preset_save", 12.0, name=name, confirm=True)],
                Expect("workflow", [Check("envelope", step=1, target={
                    "path": "ok", "op": "eq", "value": True})]))
    if v == "export":
        return ([step("export_run", 60.0, images=[hero], preset="{export_preset}",
                      out_dir="{out_dir}", confirm=True)],
                Expect("workflow", [Check("envelope", step=0, target={
                    "path": "ok", "op": "eq", "value": True}),
                    Check("extra", target={"path": "export_files", "op": "gte", "value": 1})]))
    if v == "cull":
        return ([step("cull_propose", 30.0, images=[hero])],
                Expect("workflow", [Check("envelope", step=0, target={
                    "path": "ok", "op": "eq", "value": True})]))
    raise ValueError(f"no workflow plan for {v!r}")


def tool_case(req: dict, plan: RequestPlan, tax: Taxonomy, body: str, tier: str) -> Case:
    cmap = tax.check_map
    role = cmap["role_for_problem"][req["problem"]]
    meta = {"sentence": req["sentence"], "class": plan.klass, "genre": req["genre"],
            "problem": req["problem"], "followups": list(req["followups"])}
    if plan.klass in ("impossible", "out"):
        phrase, rkey = nearest_phrase(req, tax)
        meta["roadmap_key"] = rkey
        steps = [step("darkroom_say", BUDGET_SAY, phrase=phrase)]
        for f in req["followups"]:
            if _followup_action(f, cmap) == "undo":
                steps.append(step("darkroom_undo_last", 3.5))
            else:
                steps.append(step("darkroom_say", BUDGET_SAY, phrase=f"{phrase} a bit"))
        expect = Expect("refuse")
    elif plan.klass == "workflow":
        steps, expect = _workflow(plan, req)
    else:
        #: Rows 458/461 (COV-WB-preset4-expectation, wired into scenarios
        #: too): a capability writing the temperature module refuses
        #: (module_unavailable) on the corpus hero's preset 4, candidate
        #: until channelmixerrgb.temperature is pinned. When every
        #: capability this request would exercise is one of those, the
        #: request as a whole cannot serve anything and the case becomes an
        #: honest refusal instead of an edit whose stat never moves.
        wb_refusals = {c: r for c in plan.capabilities
                      if (r := _wb_capability_expect(c, cmap)) is not None
                      and r["expect"] == "typed_error"}
        if plan.klass == "partial":
            un = _unserved_modules(req, tax)
            meta["roadmap_key"] = (un or [{"id": "unknown"}])[0]["id"]
        else:
            un = []
        steps: list[Step] = []
        refused_steps: list[tuple[int, dict]] = []
        for c in plan.capabilities:
            steps.append(cap_step(c, req, cmap))
            if c in wb_refusals:
                refused_steps.append((len(steps) - 1, wb_refusals[c]))
        if wb_refusals and len(wb_refusals) == len(plan.capabilities):
            codes = sorted({code for r in wb_refusals.values() for code in r["codes"]})
            return Case(id=case_id(PREFIX, req["id"], body, "tool"), layer=LAYER, tier=tier,
                        mode="tool", steps=steps, expect=Expect("typed_error", codes=codes),
                        body=body, frame_role=role,
                        source={"file": "requests.json", "id": req["id"],
                                "cluster": req["cluster_id"]}, meta=meta)
        # Mixed (some capabilities WB-refused, some not) or none refused:
        # keep the case as an edit, but never assert a stat check for a
        # capability that will not actually move.
        checks = [stat_check(p) for p in plan.parts
                 if p.capability and p.capability not in wb_refusals]
        undo_follow = False
        for f in req["followups"]:
            if _followup_action(f, cmap) == "undo":
                steps.append(step("darkroom_undo_last", 3.5))
                undo_follow = True
            else:
                for c in plan.capabilities:
                    steps.append(cap_step(c, req, cmap, bit=True))
                    if c in wb_refusals:
                        refused_steps.append((len(steps) - 1, wb_refusals[c]))
        if un:
            steps.append(step("darkroom_say", BUDGET_SAY, phrase=un[0]["phrases"][0]))
            checks.append(Check("envelope", step=len(steps) - 1, target={
                "path": "ok", "op": "eq", "value": False}))
        if undo_follow:
            expect = Expect("workflow", [m1_restored([], {})])
        else:
            #: Fix round 1 (review I5): a mixed request that never asserts
            #: the refused sub-move's own step is a silent failure waiting
            #: to happen -- the M2 check for the working capability passes,
            #: no reported key is unmoved, so nothing catches the refused
            #: warmth silently doing nothing. Assert it explicitly, on
            #: every step this capability was exercised (the initial call
            #: and any "a bit" follow-up), and declare it a roadmap gap
            #: like the `un` (unserved-module) case above.
            if wb_refusals:
                for idx, r in refused_steps:
                    codes = list(r["codes"])
                    code_target = ({"path": "error.code", "op": "eq", "value": codes[0]} if
                                   len(codes) == 1 else
                                   {"path": "error.code", "op": "in", "value": codes})
                    checks.append(Check("envelope", step=idx, target={
                        "path": "ok", "op": "eq", "value": False}))
                    checks.append(Check("envelope", step=idx, target=code_target))
                meta["roadmap_key"] = meta.get("roadmap_key", "wb_route_a")
            expect = Expect("edit", checks, {"changed": True})
    return Case(id=case_id(PREFIX, req["id"], body, "tool"), layer=LAYER, tier=tier,
                mode="tool", steps=steps, expect=expect, body=body, frame_role=role,
                source={"file": "requests.json", "id": req["id"], "cluster": req["cluster_id"]},
                meta=meta)


def _workflow_agent_expect(verb: str) -> Expect:
    """(row 501 item B, night-1 triage rank 10, SCA-hollow-workflow-pass):
    `agent_case`'s workflow Expect used to carry `checks=[]` for every verb
    -- a genuine sidecar-unchanged expectation (cull/export) then scores
    `verified` with ZERO MCP calls and nothing else asserted (sca-r0159: a
    cull request the agent answered with clarifying questions only, never
    calling `cull_propose`, still passed). A workflow case whose expectation
    carries no checks can never score `verified` again: every verb below
    returns at least one, so an r0159-style hollow pass is impossible by
    construction, and an unresolvable verb fails generation outright rather
    than silently reaching evaluate.py with nothing to check.

    `undo` reuses `m1_restored` (a snapshot comparison over every key the
    observation saw, not tied to a specific call index -- works whether the
    agent needed one `darkroom_undo_last` or two). The other verbs assert
    the agent's own LAST recorded MCP call answered `ok: true`: agent mode
    has no fixed step sequence to index into like tool mode's `_workflow`,
    so the last call is the only call the harness can honestly tie to "did
    the verb actually run" -- an empty `obs.calls` (r0159's own case) fails
    this check immediately (`_check_envelope`: "no such step")."""
    if verb == "undo":
        checks = [m1_restored([], {})]
    elif verb in ("repeat_more", "preset_save", "export", "cull"):
        checks = [Check("envelope", target={"path": "ok", "op": "eq", "value": True})]
    else:
        raise ValueError(f"no agent workflow expectation for verb {verb!r}")
    changed = verb not in ("cull", "export")
    if verb == "undo":
        changed = True
    expect = Expect("workflow", checks, {"changed": changed})
    if not expect.checks:  # defence in depth: never reach evaluate.py hollow
        raise ValueError(f"workflow agent expectation for verb {verb!r} has no checks")
    return expect


def agent_case(req: dict, plan: RequestPlan, tax: Taxonomy, tier: str) -> Case:
    cmap = tax.check_map
    role = cmap["role_for_problem"][req["problem"]]
    meta = {"sentence": req["sentence"], "class": plan.klass, "genre": req["genre"],
            "followups": list(req["followups"])}
    if plan.klass in ("impossible", "out"):
        #: (row 501 item C, night-1 triage rank 3, SCA-refuse-case-followup-
        #: presupposes-edit): a scripted follow-up (e.g. r0019's "That's too
        #: crunchy now, back it off a bit.") presupposes an edit was made --
        #: scripting it after an honest, no-write refusal induces exactly
        #: the edit the refusal correctly declined to make. Agent mode has
        #: no per-request follow-up wording of its own (unlike tool mode's
        #: dry-run "a bit" steps), so the only sound choice for a case whose
        #: own turn 1 is scored `refuse` is no follow-up turn at all.
        meta["followups"] = []
    # (final fix C1): the case has no steps; its hard cap is every claude turn
    # (initial + each follow-up) at its timeout plus the usual 30 s slack.
    meta["cap_s"] = (1 + len(meta["followups"])) * (AGENT_TURN_TIMEOUT_S + 30.0)
    if plan.klass in ("impossible", "out"):
        meta["roadmap_key"] = nearest_phrase(req, tax)[1]
        expect = Expect("refuse")
    elif plan.klass == "workflow":
        expect = _workflow_agent_expect(plan.workflow_verb)
    else:
        # Rows 458/461: agent mode drives the same product, so the same
        # preset-4 WB refusal applies -- an agent asked to warm/cool/tint an
        # "as-shot" hero should also be scored against a refusal, not a
        # "changed: True" edit whose stat cannot move.
        wb_refusals = {c: r for c in plan.capabilities
                      if (r := _wb_capability_expect(c, cmap)) is not None
                      and r["expect"] == "typed_error"}
        if wb_refusals and len(wb_refusals) == len(plan.capabilities):
            #: Fix round 1 (review I7): `typed_error` looks only at the
            #: agent's LAST tool call (evaluate.py), which is the wrong
            #: oracle for an agent conversation -- an agent that retries,
            #: reads state after the refusal, or tries tint via a say
            #: fails wrongly; worse, one that CLAIMS success while its last
            #: tool call happened to be refused still PASSES. `Expect
            #: ("refuse")` checks the refusal marker and no claim marker
            #: instead, and requires the sidecar unchanged. The codes stay
            #: recorded in meta for documentation.
            codes = sorted({code for r in wb_refusals.values() for code in r["codes"]})
            meta["codes"] = codes
            expect = Expect("refuse")
        else:
            checks = [stat_check(p) for p in plan.parts
                     if p.capability and p.capability not in wb_refusals]
            if wb_refusals:
                #: Fix round 1 (review I5): a mixed agent request must also
                #: name the refusal in its own text -- otherwise "brighter
                #: and warmer" with warmer silently dropped still scores
                #: verified.
                pattern = "|".join(re.escape(m) for m in cmap["refusal_markers"])
                checks.append(Check("text", target={"regex": pattern, "present": True}))
                meta["roadmap_key"] = meta.get("roadmap_key", "wb_route_a")
            expect = Expect("edit", checks, {"changed": True})
        if plan.klass == "partial":
            meta["roadmap_key"] = (_unserved_modules(req, tax) or [{"id": "unknown"}])[0]["id"]
    return Case(id=case_id(PREFIX_AGENT, req["id"], "olympus", "agent"), layer=LAYER_AGENT,
                tier=tier, mode="agent", steps=[], expect=expect, body="olympus",
                frame_role=role, source={"file": "requests.json", "id": req["id"],
                                         "cluster": req["cluster_id"]}, meta=meta)


def generate(tax: Taxonomy) -> tuple[list[Case], list[Case]]:
    p0 = {r["id"] for r in select_p0(tax.requests)}
    tool: list[Case] = []
    agent: list[Case] = []
    for req in tax.requests:
        plan = plan_request(req, tax.check_map)
        tier = "P0" if req["id"] in p0 else "P1"
        body = body_for(req["id"])
        tool.append(tool_case(req, plan, tax, body, tier))
        if req["id"] in p0:
            tool.append(tool_case(req, plan, tax, other(body), tier))
            agent.append(agent_case(req, plan, tax, "P0"))
        elif req["intent"] == "impossible":
            agent.append(agent_case(req, plan, tax, "P1"))
    return tool, agent
