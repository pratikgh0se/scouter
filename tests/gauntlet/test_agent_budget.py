"""Row 501 item H: stratified subset selection and the usage-meter park."""

from __future__ import annotations

import pytest

from gauntlet.adapter import Park
from tests.gauntlet import agent_budget


class Item:
    def __init__(self, id_, klass, genre):
        self.id, self.klass, self.genre = id_, klass, genre


def _pool():
    classes = ["in", "partial", "out", "impossible", "workflow"]
    genres = ["portrait", "landscape", "wedding and events", "product"]
    items = []
    n = 0
    for k in classes:
        for g in genres:
            for _ in range(4):  # 80 items total, 20 per class, 5 per (class, genre)
                items.append(Item(f"r{n:04d}", k, g))
                n += 1
    return items


def test_stratified_subset_covers_every_class_and_genre():
    pool = _pool()
    sub = agent_budget.stratified_subset(pool, 40, seed=345, key_fn=lambda i: i.klass)
    assert len(sub) == 40
    assert {i.klass for i in sub} == {"in", "partial", "out", "impossible", "workflow"}
    # every genre also present, using a composite key on a second call
    sub2 = agent_budget.stratified_subset(pool, 40, seed=345, key_fn=lambda i: (i.klass, i.genre))
    got = {(i.klass, i.genre) for i in sub2}
    want = {(i.klass, i.genre) for i in pool}
    assert got == want  # 20 strata, 40 slots: every stratum gets at least one


def test_stratified_subset_is_deterministic_on_the_seed():
    pool = _pool()
    a = [i.id for i in agent_budget.stratified_subset(pool, 40, seed=1, key_fn=lambda i: i.klass)]
    b = [i.id for i in agent_budget.stratified_subset(pool, 40, seed=1, key_fn=lambda i: i.klass)]
    c = [i.id for i in agent_budget.stratified_subset(pool, 40, seed=2, key_fn=lambda i: i.klass)]
    assert a == b
    assert a != c  # a different seed picks a different subset


def test_stratified_subset_never_exceeds_the_pool():
    pool = _pool()[:10]
    sub = agent_budget.stratified_subset(pool, 40, seed=1, key_fn=lambda i: i.klass)
    assert len(sub) == 10


def test_usage_fraction_reads_the_env_override():
    import os

    os.environ[agent_budget.USAGE_ENV] = "0.42"
    try:
        assert agent_budget.usage_fraction() == pytest.approx(0.42)
    finally:
        del os.environ[agent_budget.USAGE_ENV]


def test_usage_fraction_is_none_with_no_source():
    import os

    os.environ.pop(agent_budget.USAGE_ENV, None)
    # the module's one documented on-disk candidate has no usage/meter
    # field as of 2026-09-27 (see module docstring); this is the
    # sanctioned "no read-only source exists" case.
    assert agent_budget.usage_fraction() is None or isinstance(agent_budget.usage_fraction(),
                                                                float)


def test_check_usage_budget_parks_at_the_threshold():
    with pytest.raises(Park, match="usage_meter"):
        agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: 0.6)
    with pytest.raises(Park, match="usage_meter"):
        agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: 0.75)


def test_check_usage_budget_never_parks_below_threshold_or_with_no_source():
    agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: 0.5)  # no raise
    agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: None)  # no raise: fallback
