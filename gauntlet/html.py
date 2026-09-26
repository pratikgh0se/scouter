# ruff: noqa: E501 - the page template is easier to read unwrapped
"""The progress page (spec 8): one self-contained HTML file rendered from
progress.json (and report.json when it exists). The controller publishes it
as the same artifact URL at every checkpoint. Pure stdlib, no scripts."""

from __future__ import annotations

import html


def _bar(done: int, total: int, passed: int, partial: int, failed: int) -> str:
    if not total:
        return '<div class="bar"></div>'
    w = lambda n: f"{100.0 * n / total:.2f}%"  # noqa: E731
    return (f'<div class="bar" role="img" aria-label="{done} of {total} done">'
            f'<span class="p" style="width:{w(passed)}"></span>'
            f'<span class="h" style="width:{w(partial)}"></span>'
            f'<span class="f" style="width:{w(failed)}"></span></div>')


def _fmt_eta(s: float | None) -> str:
    if not s:
        return "-"
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def render(progress: dict, report: dict | None = None, title: str = "Gauntlet") -> str:
    e = html.escape
    layers = progress.get("layers", {})
    rows = []
    for name, lay in layers.items():
        rows.append(
            f"<tr><th scope='row'>{e(name)}</th><td>{e(str(lay.get('status', '')))}</td>"
            f"<td>{lay.get('done', 0)}/{lay.get('total', 0)}</td>"
            f"<td>{_bar(lay.get('done', 0), lay.get('total', 0), lay.get('pass', 0), lay.get('partial', 0), lay.get('fail', 0))}</td>"
            f"<td>{lay.get('rate_pct', '-')}</td><td>{lay.get('p0_pass', 0)}/{lay.get('p0_total', 0)}</td>"
            f"<td>{lay.get('silent', 0)}</td><td>{lay.get('crash', 0)}</td><td>{lay.get('hang', 0)}</td>"
            f"<td>{_fmt_eta(lay.get('eta_s'))}</td></tr>")
    silent = sum(v.get("silent", 0) for v in layers.values())
    crash = sum(v.get("crash", 0) for v in layers.values())
    p0p = sum(v.get("p0_pass", 0) for v in layers.values())
    p0t = sum(v.get("p0_total", 0) for v in layers.values())
    signoff = ""
    fails = ""
    if report:
        s8 = report["S8_signoff"]
        signoff = (f"<p class='verdict {'ok' if s8['release_bar_met'] else 'no'}'>Release bar: "
                   f"{'MET' if s8['release_bar_met'] else 'NOT MET'}</p>")
        items = "".join(f"<li><code>{e(f['id'])}</code> {e(f['outcome'])} "
                        f"<code class='repro'>{e(f['repro'])}</code></li>"
                        for f in report["S3_non_pass"][:200])
        fails = f"<h2>Non-pass ({len(report['S3_non_pass'])})</h2><ol>{items}</ol>"
    parked = progress.get("parked") or []
    parked_html = (f"<p>Parked (waiting on a usage limit): {len(parked)} cases</p>"
                   if parked else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a66;--line:#e2e0da;--ok:#2f7d4f;--warn:#b7791f;--bad:#b83a3a;--track:#ecebe6}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#34332f;--ok:#5bbf86;--warn:#e0a84a;--bad:#e46f6f;--track:#2a2a27}}}}
:root[data-theme="dark"]{{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#34332f;--ok:#5bbf86;--warn:#e0a84a;--bad:#e46f6f;--track:#2a2a27}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}
.meta{{color:var(--muted);font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:16px 0}}
.tile{{border:1px solid var(--line);border-radius:8px;padding:10px}}.tile b{{display:block;font-size:22px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
.bar{{display:flex;width:160px;height:10px;background:var(--track);border-radius:5px;overflow:hidden}}
.bar .p{{background:var(--ok)}}.bar .h{{background:var(--warn)}}.bar .f{{background:var(--bad)}}
.verdict.ok{{color:var(--ok);font-weight:600}}.verdict.no{{color:var(--bad);font-weight:600}}
code{{font-size:12px}}.repro{{color:var(--muted)}}ol{{padding-left:20px}}
</style></head><body><main>
<h1>{e(title)}</h1>
<p class="meta">Run {e(str(progress.get('stamp', '')))} · commit {e(str(progress.get('commit', '')))} · status {e(str(progress.get('status', '')))} · updated {e(str(progress.get('updated', '')))} · ETA {_fmt_eta(progress.get('eta_s'))}</p>
{signoff}
<div class="tiles"><div class="tile">Silent failures<b>{silent}</b></div><div class="tile">Crashes<b>{crash}</b></div><div class="tile">P0 pass<b>{p0p}/{p0t}</b></div><div class="tile">Current<b style="font-size:13px">{e(str(progress.get('current_case') or '-'))}</b></div></div>
{parked_html}
<div class="wrap"><table><thead><tr><th>Layer</th><th>Status</th><th>Done</th><th>Progress</th><th>Rate %</th><th>P0</th><th>Silent</th><th>Crash</th><th>Hang</th><th>ETA</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
{fails}
</main></body></html>
"""
