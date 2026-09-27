"""gauntlet/subset.py: the generic, product-agnostic, multi-axis stratified
subset (row 501 item H, fix round 1, Opus review finding 5)."""

from __future__ import annotations

from gauntlet.subset import stratified_subset


class Item:
    def __init__(self, id_, a, b):
        self.id, self.a, self.b = id_, a, b


def _pool():
    axis_a = ["x1", "x2", "x3", "x4", "x5"]
    axis_b = [f"y{i}" for i in range(14)]
    items = []
    n = 0
    for a in axis_a:
        for b in axis_b:
            items.append(Item(f"r{n:04d}", a, b))
            n += 1
    return items  # 70 items, 5 x 14 = 70 distinct (a, b) pairs -- every pair unique


def test_covers_every_value_on_every_axis_not_only_the_composite_pair():
    """The bug the fix replaces: a single composite-key pass fills its
    quota one PAIR at a time and truncates before every value of one axis
    is ever seen. Two independent key_fns must cover BOTH axes."""
    pool = _pool()
    sub = stratified_subset(pool, 20, seed=7, key_fns=[lambda i: i.a, lambda i: i.b])
    assert len(sub) == 20
    assert {i.a for i in sub} == {f"x{n}" for n in range(1, 6)}
    assert {i.b for i in sub} == {f"y{n}" for n in range(14)}


def test_single_composite_key_would_miss_axis_coverage_at_this_size():
    """Control, reproducing the review's own finding: a single COMPOSITE
    key (a, b) pass-1-fill-then-truncate misses axis b at size 20 on this
    70-pair pool (20 < 70 distinct pairs, so pass 1 alone decides
    everything) -- proving the two-key fix is not just cosmetically
    different, it changes the outcome."""
    pool = _pool()
    composite = stratified_subset(pool, 20, seed=7, key_fns=[lambda i: (i.a, i.b)])
    seen_b = {i.b for i in composite}
    assert len(seen_b) < 14  # the single-composite-key pass misses axis b


def test_deterministic_on_the_seed_and_independent_of_input_order():
    pool = _pool()
    a = [i.id for i in stratified_subset(pool, 20, 1, [lambda i: i.a, lambda i: i.b])]
    b = [i.id for i in stratified_subset(pool, 20, 1, [lambda i: i.a, lambda i: i.b])]
    c = [i.id for i in stratified_subset(pool, 20, 2, [lambda i: i.a, lambda i: i.b])]
    assert a == b
    assert a != c
    shuffled = list(reversed(pool))
    d = [i.id for i in stratified_subset(shuffled, 20, 1, [lambda i: i.a, lambda i: i.b])]
    assert sorted(a) == sorted(d)


def test_never_exceeds_the_pool():
    pool = _pool()[:10]
    sub = stratified_subset(pool, 40, seed=1, key_fns=[lambda i: i.a])
    assert len(sub) == 10


def test_no_key_fns_is_a_plain_seeded_fill():
    pool = _pool()
    sub = stratified_subset(pool, 5, seed=1, key_fns=[])
    assert len(sub) == 5
