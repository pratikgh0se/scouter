import collections
import json
import shutil

import pytest

from tests.gauntlet import checkmap, p0, taxonomy


@pytest.fixture(scope="module")
def tax():
    return taxonomy.load()


def test_counts_match_spec_section_0(tax):
    assert len(tax.modules) == 69 and len(tax.unique_phrases()) == 378
    assert len(tax.looks) == 234 and len(tax.requests) == 601


def test_a_silent_data_edit_fails_loudly(tmp_path):
    for f in taxonomy.DATA.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    doc = json.loads((tmp_path / "requests.json").read_text())
    doc["requests"].pop()
    (tmp_path / "requests.json").write_text(json.dumps(doc))
    with pytest.raises(taxonomy.TaxonomyError, match="requests: expected 601, got 600"):
        taxonomy.load(tmp_path)


def test_every_request_maps_no_unmapped(tax):
    plans = [checkmap.plan_request(r, tax.check_map) for r in tax.requests]
    classes = collections.Counter(p.klass for p in plans)
    assert classes["unmapped"] == 0
    assert classes == {"out": 323, "in": 168, "impossible": 60, "workflow": 37, "partial": 13}


def test_split_and_direction_examples(tax):
    cm = tax.check_map
    assert checkmap.split_parts("dynamic range compression (shadow up, highlight down)", cm) == \
        ["shadow up", "highlight down"]
    assert checkmap.direction_of("increase, limited", cm)["kind"] == "capped"
    assert checkmap.direction_of("decrease toward 0", cm)["sign"] == -1
    assert checkmap.direction_of("step back by 2", cm)["steps"] == 2
    plan = checkmap.plan_request({"id": "x", "intent": "tonal", "expected": {
        "kind": "edit", "check": {"stat_or_param": "color temp up, mean luminance up",
                                  "direction": "both"}}}, cm)
    assert plan.klass == "in" and plan.capabilities == ("warmer", "brighter")


def test_every_capability_is_reachable_from_a_concept(tax):
    caps = {c for concept in tax.check_map["concepts"].values() for c in concept["caps"].values()}
    assert caps <= set(tax.check_map["capabilities"])


def test_p0_selection_is_100_with_the_genre_floor(tax):
    sel = p0.select_p0(tax.requests)
    assert len(sel) == 100 and len({r["id"] for r in sel}) == 100
    genres = collections.Counter(r["genre"] for r in sel)
    assert len(genres) == 14 and min(genres.values()) >= 3
    assert not any(r["intent"] == "impossible" for r in sel)
    assert sel == p0.select_p0(tax.requests)  # deterministic


def test_quotas_sum_to_100_with_floor_one():
    q = p0.quotas({"a": 71, "b": 1, "c": 469})
    assert sum(q.values()) == 100 and q["b"] == 1


def test_wb_route_a_block_loads_and_resolves(tax):
    """Route A (decision rows 458/461): the check map carries the preset-4
    warmth expectations as data, and they resolve both before and after the
    pin."""
    cmap = tax.check_map
    assert checkmap.route_a_pinned(cmap) is False  # candidate until the host probe
    before = checkmap.wb_route_a_expect(cmap, "warmer", 4, pinned=False)
    assert before == {"expect": "typed_error", "codes": ["module_unavailable"]}
    after = checkmap.wb_route_a_expect(cmap, "temperature.temperature", 4, pinned=True)
    assert after["m1_key"] == "channelmixerrgb.temperature"
    assert after["replaces"] == "temperature.temperature" and after["unit"] == "K"
    assert after["allowed"] == ["channelmixerrgb.x", "channelmixerrgb.y"]
    assert after["unchanged"] == ["temperature.preset"]
    assert after["direction_verified"] is False
    assert checkmap.wb_route_a_expect(cmap, "warmer", 2, pinned=True) is None
    assert checkmap.wb_route_a_expect(cmap, "brighter", 4, pinned=True) is None
    from scouter.core import vocab
    assert vocab.spec("channelmixerrgb", "temperature") is not None
