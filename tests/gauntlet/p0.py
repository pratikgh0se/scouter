"""The deterministic P0 selection rule (spec 3.2). Pure."""

from __future__ import annotations

import math

P0_TOTAL = 100
GENRE_FLOOR = 3


def demand_key(req: dict) -> str:
    if req["problem"] != "n/a":
        return req["problem"]
    return f"{req['intent']}:{req['modules'][0] if req['modules'] else 'none'}"


def quotas(demand: dict[str, int], total: int = P0_TOTAL) -> dict[str, int]:
    """Largest-remainder apportionment with a floor of 1 per key."""
    n = sum(demand.values())
    exact = {k: total * v / n for k, v in demand.items()}
    q = {k: max(1, math.floor(x)) for k, x in exact.items()}
    order = sorted(demand, key=lambda k: (-(exact[k] - math.floor(exact[k])), k))
    i = 0
    while sum(q.values()) < total:
        q[order[i % len(order)]] += 1
        i += 1
    shrink = sorted(demand, key=lambda k: (exact[k] - math.floor(exact[k]), -demand[k], k))
    i = 0
    while sum(q.values()) > total:
        k = shrink[i % len(shrink)]
        if q[k] > 1:
            q[k] -= 1
        i += 1
    return q


def _pick_in_key(reqs: list[dict], n: int) -> list[dict]:
    """Id order, one per cluster before any second from the same cluster."""
    ordered = sorted(reqs, key=lambda r: r["id"])
    picked: list[dict] = []
    rounds: dict[str, int] = {}
    level = 0
    while len(picked) < n and len(picked) < len(ordered):
        for r in ordered:
            if len(picked) >= n:
                break
            if r in picked:
                continue
            if rounds.get(r["cluster_id"], 0) == level:
                picked.append(r)
                rounds[r["cluster_id"]] = level + 1
        level += 1
    return picked


def select_p0(requests: list[dict]) -> list[dict]:
    pool = [r for r in requests if r["intent"] != "impossible"]
    by_key: dict[str, list[dict]] = {}
    for r in pool:
        by_key.setdefault(demand_key(r), []).append(r)
    demand = {k: len(v) for k, v in by_key.items()}
    q = quotas(demand)
    rank: dict[str, int] = {}
    picked: list[dict] = []
    for k in sorted(by_key, key=lambda k: (-demand[k], k)):
        chosen = _pick_in_key(by_key[k], q[k])
        for i, r in enumerate(chosen):
            rank[r["id"]] = i
        picked.extend(chosen)
    genres = sorted({r["genre"] for r in requests})
    for _ in range(len(genres) * GENRE_FLOOR):
        counts = {g: sum(r["genre"] == g for r in picked) for g in genres}
        short = [g for g in genres if counts[g] < GENRE_FLOOR]
        if not short:
            break
        g = short[0]
        ids = {r["id"] for r in picked}
        cands = [r for r in pool if r["genre"] == g and r["id"] not in ids]
        cands.sort(key=lambda r: (-demand[demand_key(r)], r["id"]))
        over = max(genres, key=lambda x: (counts[x], x))
        victims = [r for r in picked if r["genre"] == over]
        victims.sort(key=lambda r: (rank.get(r["id"], 0), r["id"]), reverse=True)
        if not cands or not victims:
            break
        picked.remove(victims[0])
        picked.append(cands[0])
        rank[cands[0]["id"]] = 99
    return sorted(picked, key=lambda r: r["id"])
