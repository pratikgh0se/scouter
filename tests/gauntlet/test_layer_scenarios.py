import collections

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
    that refuses (module_unavailable, candidate until pinned), so neither
    the tool nor the agent case should assert a stat change that cannot
    happen."""
    tool, agent = scenarios.generate(taxonomy.load())
    t = next(c for c in tool if c.id.startswith("scn-r0089-") and c.id.endswith("-tool"))
    assert t.expect.kind == "typed_error"
    assert t.expect.codes == ["module_unavailable"]
    a = next(c for c in agent if c.id == "sca-r0089-olympus-agent")
    assert a.expect.kind == "typed_error"
    assert a.expect.codes == ["module_unavailable"]


def test_mixed_wb_and_other_capability_drops_only_the_wb_stat_check():
    """r0005 combines "brighter" (unaffected) with "warmer" (refused on the
    corpus hero's preset 4): the case stays an edit (brighter still moves),
    but must not carry a stat check for the rb/temperature concept, since
    that sub-move never lands."""
    tool, _ = scenarios.generate(taxonomy.load())
    t = next(c for c in tool if c.id.startswith("scn-r0005-") and c.id.endswith("-tool"))
    assert t.expect.kind == "edit"
    stats = {(c.metric, c.stat) for c in t.expect.checks}
    assert ("M4", "rb") not in stats
    assert any(c.metric == "M2" for c in t.expect.checks)  # brighter's own check survives


def test_agent_cases_get_a_real_cap():
    """(final fix C1): an agent case's cap covers every claude turn (initial
    + each follow-up) at TURN_TIMEOUT_S, not the 30 s fallback."""
    from tests.gauntlet.agent import TURN_TIMEOUT_S

    _, agent = scenarios.generate(taxonomy.load())
    for c in agent:
        turns = 1 + len(c.meta["followups"])
        assert c.budget_cap_s() >= turns * TURN_TIMEOUT_S, c.id
