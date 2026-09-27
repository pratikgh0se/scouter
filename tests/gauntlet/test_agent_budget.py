"""Row 501 item H: the usage-meter source/park, and the scouter binding
over the generic engine's stratified subset (gauntlet/tests/test_subset.py
covers the generic algorithm itself; this file tests the scouter-specific
wiring: the two axes, and the real usage-window.json source)."""

from __future__ import annotations

import json
import os

import pytest

from gauntlet.adapter import Park
from tests.gauntlet import agent_budget


class Item:
    def __init__(self, id_, klass, genre):
        self.id = id_
        self.meta = {"class": klass, "genre": genre}


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


def test_scouter_stratified_subset_covers_every_class_and_genre():
    """(fix round 1, Opus finding 5): the two-axis greedy cover, bound to
    the ruling's own scouter meta keys."""
    pool = _pool()
    sub = agent_budget.scouter_stratified_subset(pool, 40, seed=345)
    assert len(sub) == 40
    assert {i.meta["class"] for i in sub} == {"in", "partial", "out", "impossible", "workflow"}
    assert {i.meta["genre"] for i in sub} == {"portrait", "landscape", "wedding and events",
                                              "product"}


def test_scouter_stratified_subset_covers_the_real_generator_over_200_seeds():
    """(fix round 1, Opus finding 5): the first cut's single composite-key
    pass filled 40 slots one (class, genre) PAIR at a time and truncated
    before every genre (or class) was ever seen -- measured on the real
    160-case scenarios_agent pool, up to 19 of 14 genres missing over 200
    seeds. Reproduced here against the REAL generator, not a synthetic
    pool that hid it, over the full 200-seed range the review checked."""
    from tests.gauntlet import taxonomy
    from tests.gauntlet.layers import scenarios

    _, agent = scenarios.generate(taxonomy.load())
    classes = {c.meta["class"] for c in agent}
    genres = {c.meta["genre"] for c in agent}
    assert len(classes) == 5 and len(genres) == 14  # the review's own count
    for seed in range(200):
        sub = agent_budget.scouter_stratified_subset(agent, 40, seed)
        assert len(sub) == 40, seed
        assert {c.meta["class"] for c in sub} == classes, seed
        assert {c.meta["genre"] for c in sub} == genres, seed


def test_usage_fraction_reads_the_env_override():
    os.environ[agent_budget.USAGE_ENV] = "0.42"
    try:
        util, reset_at = agent_budget.usage_fraction()
        assert util == pytest.approx(0.42)
    finally:
        del os.environ[agent_budget.USAGE_ENV]


def test_usage_fraction_reads_the_real_ledger_path(tmp_path, monkeypatch):
    """(fix round 1, Opus finding 5): the real source -- capsule-corp's
    usage_guard tick, written to usage-window.json -- not the earlier
    invented, never-written USAGE_LEDGER_PATH."""
    monkeypatch.delenv(agent_budget.USAGE_ENV, raising=False)
    path = tmp_path / "usage-window.json"
    now = 1_000_000.0
    path.write_text(json.dumps({"utilization": 0.71, "reset_at": now + 1800,
                                "written_at": now - 60}))
    monkeypatch.setattr(agent_budget, "USAGE_WINDOW_PATH", path)
    util, reset_at = agent_budget.usage_fraction(now=now)
    assert util == pytest.approx(0.71) and reset_at == pytest.approx(now + 1800)


def test_usage_fraction_is_none_when_the_reading_is_stale(tmp_path, monkeypatch):
    """(fix round 1, Opus finding 5): a reading older than STALE_S must
    never silently gate a live run on a stopped writer's last number."""
    monkeypatch.delenv(agent_budget.USAGE_ENV, raising=False)
    path = tmp_path / "usage-window.json"
    now = 1_000_000.0
    stale_written_at = now - agent_budget.STALE_S - 1
    path.write_text(json.dumps({"utilization": 0.95, "reset_at": now + 1800,
                                "written_at": stale_written_at}))
    monkeypatch.setattr(agent_budget, "USAGE_WINDOW_PATH", path)
    assert agent_budget.usage_fraction(now=now) is None


def test_usage_fraction_is_none_when_the_writer_does_not_exist_yet(tmp_path, monkeypatch):
    """(fix round 1, Opus finding 5): until capsule-corp's writer exists,
    the file is simply absent -- the sanctioned "no source" case, checked
    against a path this test controls, never the real $HOME (the earlier
    vacuous version of this test read the real filesystem and asserted
    something always true)."""
    monkeypatch.delenv(agent_budget.USAGE_ENV, raising=False)
    monkeypatch.setattr(agent_budget, "USAGE_WINDOW_PATH", tmp_path / "does-not-exist.json")
    assert agent_budget.usage_fraction() is None


def test_check_usage_budget_parks_at_the_threshold_with_until_the_reset():
    """(fix round 1, Opus finding 5): Park carries `until=reset_at`, so the
    runner resumes after the reset instead of stopping the layer outright
    (the ruling's own "parked at 60 percent... resuming after the reset")."""
    with pytest.raises(Park, match="usage_meter") as exc_info:
        agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: (0.6, 12345.0))
    assert exc_info.value.until == pytest.approx(12345.0)
    with pytest.raises(Park, match="usage_meter"):
        agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: (0.75, 12345.0))


def test_check_usage_budget_never_parks_below_threshold_or_with_no_source():
    calls = []
    orig = Park.__init__

    def spy_init(self, *a, **kw):
        calls.append(a)
        orig(self, *a, **kw)

    Park.__init__ = spy_init
    try:
        agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: (0.5, 12345.0))
        agent_budget.check_usage_budget(threshold=0.6, usage_fn=lambda: None)
    finally:
        Park.__init__ = orig
    assert calls == []  # Park was never constructed in either call
