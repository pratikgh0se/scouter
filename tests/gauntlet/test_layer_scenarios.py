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


def test_agent_cases_get_a_real_cap():
    """(final fix C1): an agent case's cap covers every claude turn (initial
    + each follow-up) at TURN_TIMEOUT_S, not the 30 s fallback."""
    from tests.gauntlet.agent import TURN_TIMEOUT_S

    _, agent = scenarios.generate(taxonomy.load())
    for c in agent:
        turns = 1 + len(c.meta["followups"])
        assert c.budget_cap_s() >= turns * TURN_TIMEOUT_S, c.id
