import collections

import pytest

from tests.gauntlet import taxonomy
from tests.gauntlet.layers import scenarios


def test_scenario_counts_and_tiers():
    tool, agent = scenarios.generate(taxonomy.load())
    assert (len(tool), len(agent)) == (701, 160)
    assert len({c.id for c in tool + agent}) == 861
    assert sum(c.tier == "P0" for c in tool) == 200
    assert collections.Counter(c.tier for c in agent) == {"P0": 100, "P1": 60}


def test_r0001_plan():
    tool, _ = scenarios.generate(taxonomy.load())
    r1 = next(c for c in tool if c.id == "scn-r0001-olympus-tool")
    assert r1.steps[0].args["phrase"] == "brighter by 1 ev" and r1.frame_role == "dark"
    assert r1.steps[1].args["phrase"] == "brighter a bit"  # the follow-up


def test_out_impossible_partial_carry_roadmap_key():
    tool, agent = scenarios.generate(taxonomy.load())
    for c in tool + agent:
        if c.meta["class"] in ("out", "impossible", "partial"):
            assert c.meta.get("roadmap_key"), c.id


def test_wb_only_request_becomes_a_typed_error_tool_and_agent_case():
    """Rows 458/461 (COV-WB-preset4-expectation, wired into scenarios):
    r0089's only capability is "warmer" -- on the corpus hero's preset 4
    that refuses (module_unavailable, candidate until pinned), so the tool
    case should assert a refusal, not a stat change that cannot happen.

    Fix round 1 (review I7): the agent case uses `Expect("refuse")`, not
    `typed_error` -- `typed_error` looks only at the agent's last tool
    call, the wrong oracle for a multi-turn agent conversation (an agent
    that retries, or that claims success while its last call happened to
    be refused, would wrongly pass). The refused codes are kept in meta."""
    tool, agent = scenarios.generate(taxonomy.load())
    t = next(c for c in tool if c.id.startswith("scn-r0089-") and c.id.endswith("-tool"))
    assert t.expect.kind == "typed_error"
    assert t.expect.codes == ["module_unavailable"]
    a = next(c for c in agent if c.id == "sca-r0089-olympus-agent")
    assert a.expect.kind == "refuse"
    assert a.meta["codes"] == ["module_unavailable"]


def test_mixed_wb_and_other_capability_drops_only_the_wb_stat_check():
    """r0005 combines "brighter" (unaffected) with "warmer" (refused on the
    corpus hero's preset 4): the case stays an edit (brighter still moves),
    but must not carry a stat check for the rb/temperature concept, since
    that sub-move never lands.

    Fix round 1 (review I5): dropping the check silently is not enough --
    a request combining a WB-refused capability with a working one must
    also assert the refused sub-move's own envelope (ok == false) and error
    code on its own step, so a live rerun cannot silently score `verified`
    while the requested warmth did nothing. It is a declared gap
    (meta["roadmap_key"])."""
    tool, _ = scenarios.generate(taxonomy.load())
    t = next(c for c in tool if c.id.startswith("scn-r0005-") and c.id.endswith("-tool"))
    assert t.expect.kind == "edit"
    stats = {(c.metric, c.stat) for c in t.expect.checks}
    assert ("M4", "rb") not in stats
    assert any(c.metric == "M2" for c in t.expect.checks)  # brighter's own check survives
    env_checks = [c for c in t.expect.checks if c.metric == "envelope"]
    assert any(c.target == {"path": "ok", "op": "eq", "value": False} for c in env_checks)
    assert any(c.target.get("path") == "error.code" for c in env_checks)
    assert t.meta["roadmap_key"] == "wb_route_a"


def test_mixed_wb_agent_case_requires_the_refusal_named_in_the_agent_text():
    """Fix round 1 (review I5): r0372's agent case mixes a WB-refused
    capability with a working one -- the agent must name the refusal in its
    own reply text (a "text" check against the refusal-marker regex),
    otherwise an agent that silently drops the WB half still scores
    verified."""
    _, agent = scenarios.generate(taxonomy.load())
    a = next(c for c in agent if c.id == "sca-r0372-olympus-agent")
    assert a.expect.kind == "edit"
    assert any(c.metric == "text" for c in a.expect.checks)
    assert a.meta["roadmap_key"] == "wb_route_a"


def test_agent_cases_get_a_real_cap():
    """(final fix C1): an agent case's cap covers every claude turn (initial
    + each follow-up) at TURN_TIMEOUT_S, not the 30 s fallback."""
    from tests.gauntlet.agent import TURN_TIMEOUT_S

    _, agent = scenarios.generate(taxonomy.load())
    for c in agent:
        turns = 1 + len(c.meta["followups"])
        assert c.budget_cap_s() >= turns * TURN_TIMEOUT_S, c.id


def test_workflow_agent_cases_never_generate_with_no_checks():
    """(row 501 item B, night-1 rank 10, SCA-hollow-workflow-pass): every
    workflow agent case (sca-r0159's cull included) must carry at least one
    check -- a checkless workflow Expect is exactly what let r0159 score
    `verified` with zero MCP calls."""
    _, agent = scenarios.generate(taxonomy.load())
    workflow_cases = [c for c in agent if c.expect.kind == "workflow"]
    assert workflow_cases, "expected at least one workflow agent case"
    for c in workflow_cases:
        assert c.expect.checks, c.id
    r0159 = next(c for c in agent if c.id == "sca-r0159-olympus-agent")
    assert r0159.expect.checks


def test_workflow_agent_cases_check_the_real_verb_not_a_generic_last_call():
    """(row 501 item B, fix round 1, Opus finding 3): sca-r0159 (cull) must
    check for cull.propose specifically, not just "the last call was ok" --
    the generic check let a clarify-only, read-only conversation pass."""
    _, agent = scenarios.generate(taxonomy.load())
    r0159 = next(c for c in agent if c.id == "sca-r0159-olympus-agent")
    assert r0159.expect.checks[0].metric == "verb"
    assert r0159.expect.checks[0].target["any_of"] == ["cull.propose"]
    for c in agent:
        if c.expect.kind == "workflow" and c.meta.get("class") == "workflow":
            metric = c.expect.checks[0].metric
            assert metric in ("verb", "undo"), c.id


def test_workflow_agent_expect_fails_generation_for_an_unknown_verb():
    """A checkless (or unresolvable) workflow expectation fails generation
    outright, rather than reaching evaluate.py as a hollow pass waiting to
    happen."""
    with pytest.raises(ValueError, match="no agent workflow expectation") as exc_info:
        scenarios._workflow_agent_expect("no-such-verb")
    assert "no-such-verb" in str(exc_info.value)


def test_out_of_scope_agent_cases_carry_the_refuse_and_offer_policy():
    """(row 501 item G, Pratik's ruling row 501): every "out" agent case
    documents the refuse-and-offer-only policy explicitly, so a write
    before the user picks is a named policy failure (unrequested_edit),
    not an accident of an unset default."""
    _, agent = scenarios.generate(taxonomy.load())
    out_cases = [c for c in agent if c.meta["class"] == "out"]
    assert out_cases
    for c in out_cases:
        assert c.meta["out_of_scope_policy"] == "refuse_and_offer_only", c.id
        assert c.meta["substitute_ok"] is False, c.id
        assert c.meta["unrequested_write_reason"] == "unrequested_edit", c.id


def test_client_proof_gallery_is_in_scope_and_stages_a_job():
    """(row 501 item I, Pratik's ruling): r0066 is a workflow/export case,
    not "out", and its agent case asks the session to stage a real job."""
    tool, agent = scenarios.generate(taxonomy.load())
    a = next(c for c in agent if c.id == "sca-r0066-olympus-agent")
    assert a.meta["class"] == "workflow"
    assert a.expect.kind == "workflow"
    assert a.meta["stage_job"] is True
    t = next(c for c in tool if c.id.startswith("scn-r0066-") and c.id.endswith("-tool"))
    assert t.meta["class"] == "workflow"


def test_no_followup_turn_after_an_out_or_impossible_agent_case():
    """(row 501 item C, night-1 rank 3, SCA-refuse-case-followup-
    presupposes-edit, r0019): a scripted follow-up after an honest, no-write
    refusal presupposes an edit was made. r0019 itself has a scripted
    follow-up in requests.json ("That's too crunchy now, back it off a
    bit."); the agent case must not carry it."""
    _, agent = scenarios.generate(taxonomy.load())
    r0019 = next(c for c in agent if c.id == "sca-r0019-olympus-agent")
    assert r0019.meta["class"] in ("impossible", "out")
    assert r0019.meta["followups"] == []
    for c in agent:
        if c.meta["class"] in ("impossible", "out"):
            assert c.meta["followups"] == [], c.id


