import re
from pathlib import Path

import pytest

from tests.gauntlet.layers import guide


def test_guide_count():
    cases = guide.generate()
    assert len(cases) == guide.EXPECTED == 35 and len({c.id for c in cases}) == 35


def test_walkthrough_matches_the_readme():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()

    def section(title):
        m = re.search(r"^#+ " + re.escape(title) + r".*?$(.*?)(?=^#+ |\Z)", readme, re.M | re.S)
        return m.group(1) if m else ""

    cmds = []
    for title in ("A hero edit", "A hero session"):
        inb = False
        for line in section(title).splitlines():
            if line.strip().startswith("```"):
                inb = not inb
                continue
            if inb and line.strip().startswith("scouter "):
                cmds.extend(x.strip() for x in line.strip().split("&&"))
    assert len(cmds) == 11, "README walkthrough changed: update guide.WALKTHROUGH"
    listed = [" ".join(p) for _l, k, p, _w in guide.WALKTHROUGH[:11] if k == "cli"]
    for got, want in zip(cmds, listed):
        words = [w for w in want.split() if not w.startswith("{")]
        assert all(w.strip('"') in got for w in words), (got, want)


# -- Fix C (night-1 triage rank 7, GD-WB-preset4) ----------------------------


def _by_id(cases, case_id):
    return next(c for c in cases if c.id == case_id)


@pytest.mark.usefixtures("route_a_unpinned")
def test_touch_warmer_expects_the_wb_route_a_refusal():
    """The corpus hero is darktable's temperature preset 4 (check_map.json's
    wb_route_a block, rows 458/461): every warmth move there honestly
    refuses module_unavailable until channelmixerrgb.temperature is pinned.
    gd-12 ("a touch warmer") must expect that refusal, not a landed +150 K
    move that never happens on the real corpus."""
    cases = guide.generate()
    c = _by_id(cases, "gd-12-guide_touch_warmer")
    assert c.expect.kind == "typed_error"
    assert c.expect.codes == ["module_unavailable"]
    assert c.expect.checks == []  # no M1/param checks: the write never lands


def test_session_touch_brighter_is_an_ordinary_live_move():
    """Pratik's ruling, row 496 item 8: the README's hero session now
    teaches "a touch brighter" first (gd-06) -- an ordinary exposure move
    that never hits the WB guard, unlike gd-12's "a touch warmer" above."""
    cases = guide.generate()
    c = _by_id(cases, "gd-06-session_touch_brighter")
    assert c.expect.kind == "workflow"
    assert c.steps[0].args["argv"][-1] == "a touch brighter"


@pytest.mark.usefixtures("route_a_unpinned")
def test_edu_warmer_and_cooler_expect_the_wb_route_a_refusal():
    cases = guide.generate()
    for level in guide.LEVELS:
        for phrase in ("warmer", "cooler"):
            c = next(
                c
                for c in cases
                if c.meta.get("session") == f"edu-{level}" and c.steps[-1].args["phrase"] == phrase
            )
            assert c.expect.kind == "typed_error"
            assert c.expect.codes == ["module_unavailable"]


def test_edu_brighter_and_darker_are_unaffected():
    """The wb wiring must only touch warmer/cooler -- brighter/darker (and
    more_contrast) keep their ordinary educative-explain workflow check."""
    cases = guide.generate()
    for level in guide.LEVELS:
        for phrase in ("brighter", "darker", "more contrast"):
            c = next(
                c
                for c in cases
                if c.meta.get("session") == f"edu-{level}" and c.steps[-1].args["phrase"] == phrase
            )
            assert c.expect.kind == "workflow"
            # C18: only the `full` level carries the explained-concept reset
            assert (c.steps[0].tool == "reset_explained") == (level == "full")


def test_more_magenta_stays_value_unknown_not_wb_route_a():
    """gd-17 refuses before the WB guard ever runs (tint is relative ->
    ValueUnknown), unrelated to the wb_route_a block -- the "magenta" text
    must never match the warmer/cooler word-boundary regex."""
    cases = guide.generate()
    c = _by_id(cases, "gd-17-guide_more_magenta_refuses")
    assert c.expect.codes == ["value_unknown"]
