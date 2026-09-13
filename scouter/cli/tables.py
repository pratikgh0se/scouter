"""Aligned tables for the three verbs whose payload Pratik reads directly.

JSON stays the default and the only machine contract; `--pretty` swaps in a
table for `cull.propose`, `outliers.list` and `library.jobs` because a 300-row
JSON array is not something a human culls from. Keyed by verb id, so a verb
without a renderer simply gets indented JSON.
"""

from __future__ import annotations

from collections.abc import Callable


def _align(headers: list[str], rows: list[list[str]]) -> str:
    """Two-space-gapped, left-aligned columns. Plain text, no box drawing --
    this is read in a terminal, piped through `less`, or pasted into chat."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


def _render_cull_propose(payload: dict) -> str:
    """One row per frame -- `proposals`, sorted by `seq` (already deterministic
    from `cull.propose` itself, sorted again here so a hand-built payload in a
    test is not required to pre-sort it)."""
    proposals = sorted(payload.get("proposals", []), key=lambda p: p.get("seq", 0))
    headers = ["seq", "burst", "rating", "sharpness", "reasons"]
    rows = []
    for p in proposals:
        sharpness = p.get("sharpness")
        rows.append(
            [
                str(p.get("seq", "")),
                str(p.get("burst_id", "")),
                f"{p.get('rating_now', 0)} -> {p.get('rating_proposed', 0)}",
                f"{sharpness:.2f}" if isinstance(sharpness, (int, float)) else "-",
                "; ".join(p.get("reasons", [])),
            ]
        )
    table = _align(headers, rows)
    counts = payload.get("counts", {})
    footer = (
        f"{len(proposals)} frame(s): pick {counts.get('pick', 0)}, "
        f"keep {counts.get('keep', 0)}, unchanged {counts.get('unchanged', 0)}, "
        f"no-time {counts.get('no_time', 0)}"
    )
    return f"{table}\n\n{footer}"


#: Renderers land here as the verbs they cover are built. Only `cull.propose`
#: exists yet (task 6); `outliers.list` and `library.jobs` add their own
#: entries in their own tasks.
RENDERERS: dict[str, Callable[[dict], str]] = {
    "cull.propose": _render_cull_propose,
}


def render(verb_id: str, payload: dict) -> str | None:
    """A table for `verb_id`'s `payload` (the envelope's `data`), or `None`
    when this verb has no table renderer -- the caller falls back to JSON."""
    renderer = RENDERERS.get(verb_id)
    if renderer is None:
        return None
    return renderer(payload)
