"""Layer 2: scenarios (spec 3.2). Tool mode 701 (601 + the 100 P0 on the
other body), agent mode 160 (100 P0 + 60 impossible, olympus)."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
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
        steps = [cap_step(c, req, cmap) for c in plan.capabilities]
        checks = [stat_check(p) for p in plan.parts if p.capability]
        undo_follow = False
        for f in req["followups"]:
            if _followup_action(f, cmap) == "undo":
                steps.append(step("darkroom_undo_last", 3.5))
                undo_follow = True
            else:
                steps.extend(cap_step(c, req, cmap, bit=True) for c in plan.capabilities)
        if plan.klass == "partial":
            un = _unserved_modules(req, tax)
            meta["roadmap_key"] = (un or [{"id": "unknown"}])[0]["id"]
            if un:
                steps.append(step("darkroom_say", BUDGET_SAY, phrase=un[0]["phrases"][0]))
                checks.append(Check("envelope", step=len(steps) - 1, target={
                    "path": "ok", "op": "eq", "value": False}))
        if undo_follow:
            expect = Expect("workflow", [m1_restored([], {})])
        else:
            expect = Expect("edit", checks, {"changed": True})
    return Case(id=case_id(PREFIX, req["id"], body, "tool"), layer=LAYER, tier=tier,
                mode="tool", steps=steps, expect=expect, body=body, frame_role=role,
                source={"file": "requests.json", "id": req["id"], "cluster": req["cluster_id"]},
                meta=meta)


def agent_case(req: dict, plan: RequestPlan, tax: Taxonomy, tier: str) -> Case:
    cmap = tax.check_map
    role = cmap["role_for_problem"][req["problem"]]
    meta = {"sentence": req["sentence"], "class": plan.klass, "genre": req["genre"],
            "followups": list(req["followups"])}
    # (final fix C1): the case has no steps; its hard cap is every claude turn
    # (initial + each follow-up) at its timeout plus the usual 30 s slack.
    meta["cap_s"] = (1 + len(meta["followups"])) * (AGENT_TURN_TIMEOUT_S + 30.0)
    if plan.klass in ("impossible", "out"):
        meta["roadmap_key"] = nearest_phrase(req, tax)[1]
        expect = Expect("refuse")
    elif plan.klass == "workflow":
        expect = Expect("workflow", [], {"changed": plan.workflow_verb not in ("cull", "export")})
        if plan.workflow_verb == "undo":
            expect = Expect("workflow", [], {"changed": True})
    else:
        expect = Expect("edit", [stat_check(p) for p in plan.parts if p.capability],
                        {"changed": True})
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
