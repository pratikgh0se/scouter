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


def _render_outliers_list(payload: dict) -> str:
    """One row per flagged frame -- `outliers`, already sorted worst-first by
    the verb itself (re-sorted here so a hand-built test payload need not
    pre-sort it)."""
    outliers = sorted(
        payload.get("outliers", []), key=lambda r: (-r.get("severity", 0.0), r.get("seq", 0))
    )
    headers = ["seq", "file", "ev_proxy", "ΔK", "Δrg", "Δbg", "clip %", "reasons"]
    rows = []
    for r in outliers:
        path = r.get("path") or ""
        file_name = path.rsplit("/", 1)[-1] if path else ""
        k_delta = r.get("temperature_k_delta")
        rows.append(
            [
                str(r.get("seq", "")),
                file_name,
                f"{r.get('ev_proxy_delta', 0.0):+.2f}",
                f"{k_delta:+.0f}" if isinstance(k_delta, (int, float)) else "-",
                f"{r.get('wb_ratio_rg_delta', 0.0):+.3f}",
                f"{r.get('wb_ratio_bg_delta', 0.0):+.3f}",
                f"{r.get('clipped_high_pct', 0.0):.2f}",
                "; ".join(r.get("reasons", [])),
            ]
        )
    table = _align(headers, rows)
    footer = (
        f"{len(outliers)} outlier(s) of {payload.get('checked', 0)} checked "
        f"({payload.get('within_tolerance', 0)} within tolerance, "
        f"{len(payload.get('failed', []))} failed), wb_test={payload.get('wb_test', 'ratio')}"
    )
    return f"{table}\n\n{footer}"


#: Renderers land here as the verbs they cover are built. `library.jobs` adds
#: its own entry in its own task.
RENDERERS: dict[str, Callable[[dict], str]] = {
    "cull.propose": _render_cull_propose,
    "outliers.list": _render_outliers_list,
}


def render(verb_id: str, payload: dict) -> str | None:
    """A table for `verb_id`'s `payload` (the envelope's `data`), or `None`
    when this verb has no table renderer -- the caller falls back to JSON."""
    renderer = RENDERERS.get(verb_id)
    if renderer is None:
        return None
    return renderer(payload)
