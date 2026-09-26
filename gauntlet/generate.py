"""The generator gate (spec 2.2): expand every layer through the adapter,
check ids, tiers and counts, and write cases.jsonl + generation.md."""

from __future__ import annotations

from dataclasses import dataclass, field

from gauntlet.adapter import Adapter, LayerSpec
from gauntlet.case import Case
from gauntlet.store import RunDir, atomic_write_text

COUNT_TOLERANCE = 0.02


class GenerationError(ValueError):
    pass


@dataclass
class LayerCount:
    spec: LayerSpec
    got: int
    tiers: dict[str, int] = field(default_factory=dict)
    unmapped_p2: list[str] = field(default_factory=list)


def check_layer(spec: LayerSpec, cases: list[Case]) -> LayerCount:
    errors: list[str] = []
    for c in cases:
        if c.layer != spec.name:
            errors.append(f"{c.id}: layer {c.layer!r} generated under {spec.name!r}")
        if c.meta.get("unmapped") and c.tier in ("P0", "P1"):
            errors.append(f"{c.id}: {c.tier} case falls through to unmapped "
                          f"({c.meta['unmapped']})")
    want = spec.expected_count
    if want and abs(len(cases) - want) > COUNT_TOLERANCE * want:
        errors.append(f"layer {spec.name}: {len(cases)} cases, formula says {want} (> 2% off)")
    if errors:
        raise GenerationError("; ".join(errors[:20]) + (f" (+{len(errors) - 20} more)"
                                                        if len(errors) > 20 else ""))
    tiers: dict[str, int] = {}
    for c in cases:
        tiers[c.tier] = tiers.get(c.tier, 0) + 1
    return LayerCount(spec, len(cases), tiers,
                      [c.id for c in cases if c.meta.get("unmapped")])


def generate_all(adapter: Adapter, layers: list[str] | None = None) -> tuple[list[Case],
                                                                             list[LayerCount]]:
    specs = sorted(adapter.layers(), key=lambda s: s.order)
    if layers:
        unknown = set(layers) - {s.name for s in specs}
        if unknown:
            raise GenerationError(f"unknown layers: {sorted(unknown)}")
        specs = [s for s in specs if s.name in layers]
    all_cases: list[Case] = []
    counts: list[LayerCount] = []
    seen: set[str] = set()
    for spec in specs:
        cases = adapter.generate(spec.name)
        dup = [c.id for c in cases if c.id in seen]
        seen.update(c.id for c in cases)
        if len(seen) != len(all_cases) + len(cases) or dup:
            raise GenerationError(f"duplicate case ids in {spec.name}: {dup[:5]}")
        counts.append(check_layer(spec, cases))
        all_cases.extend(cases)
    return all_cases, counts


def generation_md(adapter: Adapter, counts: list[LayerCount], cases: list[Case]) -> str:
    lines = [f"# Generation: {adapter.name}", "",
             "| # | Layer | Formula | Generated | P0 | P1 | P2 | Live |",
             "|---|---|---|---|---|---|---|---|"]
    for c in counts:
        s = c.spec
        lines.append(f"| {s.order} | {s.name} | {s.expected_count} | {c.got} | "
                     f"{c.tiers.get('P0', 0)} | {c.tiers.get('P1', 0)} | "
                     f"{c.tiers.get('P2', 0)} | {'yes' if s.live else 'no'} |")
    total = sum(c.got for c in counts)
    lines += ["", f"**Total:** {total} cases "
              f"({sum(c.got for c in counts if c.spec.live)} live).", ""]
    unmapped = [i for c in counts for i in c.unmapped_p2]
    if unmapped:
        lines += ["## P2 cases with no mapping", ""] + [f"- `{i}`" for i in unmapped] + [""]
    notes = adapter.generation_notes(cases)
    if notes:
        lines += [notes.rstrip(), ""]
    return "\n".join(lines)


def write_generation(run: RunDir, adapter: Adapter, layers: list[str] | None = None
                     ) -> list[LayerCount]:
    cases, counts = generate_all(adapter, layers)
    run.ensure()
    run.write_cases(cases)
    atomic_write_text(run.generation_md, generation_md(adapter, counts, cases))
    return counts
