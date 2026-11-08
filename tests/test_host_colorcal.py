# tests/test_host_colorcal.py
"""Route A host probe: color calibration's illuminant temperature (rows 458/461).

Gated: SCOUTER_HOST_TESTS=1 + darktable installed. Run the whole probe with:

    SCOUTER_HOST_TESTS=1 /Users/pratik/CodeBase/scouter/.venv/bin/python -m pytest -q -s \
        tests/test_host_colorcal.py

ISOLATION (standing rule): every launch gets its OWN `--library` under
`~/Pictures/ScouterScratch/colorcal-probe/<run-id>/<body>/<A|B>/library.db`
on a scratch COPY of a hero frame staged in that same dir. The real library
and the real photos (read-only copy source) are never opened by darktable.
The Lua plugin is loaded from ~/.config/darktable (PluginInstall, restored).

Per body (Olympus hero, Sony hero), two independent launches:
- Oracle A (sidecar): path answers non-nan; a warmer then a cooler move (the
  second consecutive move proves the route survives its own first write) land
  at exactly slot 36; a repeat set of the same value is a noop; a fresh
  sentinel changes slot 36 again (a dead or zero-filled slot cannot pass).
- Oracle B (pixels, `scout.stats` centre r/b): higher K must raise r/b.
  Contamination guard: B's pristine temperature must equal A's.
Illuminant and x/y are logged before and after every move; illuminant must
stay in (2, 6); WB preset must stay 4.

Results go to docs/measurements-generated.md and prose to docs/measurements.md
BEFORE any assertion that can fail, plus one line per body:
`COLORCAL_ROUTE_A <body> PROMOTE|DO_NOT_PROMOTE|FLIP_SIGN reason=...`.
Nothing is pinned here: on PROMOTE a human adds the ACTION_PATHS entry.
"""

from __future__ import annotations

import os
import shutil
import time
import unittest
from pathlib import Path

from tests import host_support as hs
from tests.colorcal_probe_logic import Evidence, route_verdict, unique_run_dir
from tests.test_host_phase3 import (
    CONTROL_PATH,
    REASON,
    RESULTS,
    RUN,
    _record,
    entry_floats,
    probe_verdict,
    sentinel_slots,
    settle,
)

PATH = "iop/channelmixerrgb/temperature"
SLOT = 36  # byte 144 / 4
PROSE = RESULTS.parent / "measurements.md"
PROBE_ROOT = hs.SCRATCH_DIR / "colorcal-probe"
#: Hard ceiling for the whole probe (about 4 launches of ~3 min each).
TOTAL_BUDGET_S = 1500.0
READY_BUDGET_S = 90.0
#: body -> hero frame (the benchmark's hero roles, tests/gauntlet/benchmark/editor_tasks.json).
BODIES = {"olympus": hs.HERO, "sony": hs.EDITING_TRIAL / "sony" / "DSC01426.ARW"}


def _prose(text: str) -> None:
    with PROSE.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _fmt(v: object) -> str:
    return f"{v:.4f}" if isinstance(v, float) else str(v)


class _Session:
    """One isolated darktable launch on a staged copy of one hero frame."""

    def __init__(self, run_dir: Path, body: str, tag: str, deadline: float) -> None:
        from scouter import registry
        from scouter.core import darkroom, history, xmp

        self.registry, self.darkroom, self.history, self.xmp = registry, darkroom, history, xmp
        self.deadline = deadline
        src = BODIES[body]
        folder = run_dir / body / tag
        self.frame = folder / f"frame{src.suffix}"
        self.library = folder / "library.db"
        folder.mkdir(parents=True, exist_ok=False)
        shutil.copy(src, self.frame)
        if Path(str(self.frame) + ".xmp").exists() or self.library.exists():
            raise RuntimeError(f"stale sidecar or library in fresh run dir {folder}")
        real = hs.REAL_LIBRARY.resolve()
        hs.assert_isolated_library(self.library)
        assert self.library.resolve() != real
        assert hs.EDITING_TRIAL.resolve() not in self.frame.resolve().parents
        self.image = str(self.frame)

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise TimeoutError(f"probe exceeded its {TOTAL_BUDGET_S:.0f}s budget")

    def commit(self) -> None:
        self.tick()
        out = self.registry.call("history.commit", "cli", image=self.image)
        if not out.ok:
            raise RuntimeError(f"history.commit failed: {out.error}")

    def rb(self) -> float:
        self.tick()
        st = self.registry.call("scout.stats", "cli", image=self.image, px=512).data["stats"]
        ch = st["channels"]
        return ch["r"]["mean"] / max(ch["b"]["mean"], 1e-6)

    def colorcal(self) -> tuple[dict | None, list[float]]:
        parsed = self.darkroom._read_sidecar_parsed(self.image)
        params = self.xmp.latest_params(parsed, "channelmixerrgb") if parsed else None
        entries = [e for e in self.history.read(self.image).entries
                   if e.operation == "channelmixerrgb"]
        last = max(entries, key=lambda e: e.num) if entries else None
        return params, entry_floats(last.params_hex) if last else []

    def move(self, label: str, k: float, log: list[str]) -> dict:
        """One action + commit; log illuminant and x/y before and after."""
        from scouter.core import dtbridge

        before, _ = self.colorcal()
        dtbridge.action(PATH, value=float(k))
        self.commit()
        after, floats = self.colorcal()
        b, a = before or {}, after or {}
        log.append(f"{label}: set {k:.0f} K -> temperature {_fmt(a.get('temperature'))}, "
                   f"illuminant {b.get('illuminant')} -> {a.get('illuminant')}, "
                   f"x/y {_fmt(b.get('x'))}/{_fmt(b.get('y'))} -> "
                   f"{_fmt(a.get('x'))}/{_fmt(a.get('y'))}")
        return {"params": after, "floats": floats, "k": float(k)}

    def run(self, work) -> object:
        """Launch isolated, wait, run `work(self)`, always quit darktable."""
        from scouter.core import dtbridge

        if hs._darktable_is_running():
            raise RuntimeError("a darktable is still running before launch")
        proc = hs.launch(self.frame, self.library)
        try:
            hs.wait_ready(READY_BUDGET_S)
            if not settle(dtbridge.call):
                raise RuntimeError(f"control path {CONTROL_PATH} did not answer after settle")
            return work(self)
        finally:
            hs.quit_darktable(proc)


def _targets(k0: float) -> tuple[float, float]:
    warm = round(k0 + 1000.0)
    cool = min(max(round(k0 - 1000.0), 2000), round(k0 - 300.0))  # vocab clamp is 2000 K
    return float(warm), float(cool)


def _oracle_a(s: _Session) -> dict:
    from scouter.core import dtbridge

    verdict, ms = probe_verdict(dtbridge.call, PATH)
    s.commit()
    start, _ = s.colorcal()
    if not start:
        raise RuntimeError("no colorcal params at start")
    a: dict = {"pinned": verdict == "pinned", "ms": ms, "log": [], "k0": start["temperature"],
               "illuminants": [start["illuminant"]], "preset_start": s.darkroom.wb_preset(s.image)}
    a["log"].append(f"start: illuminant {start['illuminant']}, temperature "
                    f"{_fmt(start['temperature'])}, x/y {_fmt(start.get('x'))}/"
                    f"{_fmt(start.get('y'))}, WB preset {a['preset_start']}")
    warm, cool = _targets(start["temperature"])
    slots_ok = True
    last = {}
    for label, k in (("warmer", warm), ("cooler (2nd consecutive)", cool)):
        last = s.move(label, k, a["log"])
        slots = sentinel_slots(last["floats"], k, tol=1.0)
        slots_ok &= slots == [SLOT]
        a["log"].append(f"  slots matching {k:.0f}: {slots} (expect [{SLOT}])")
        a["illuminants"].append((last["params"] or {}).get("illuminant"))
    # live sentinel: same value again is a noop; a fresh value moves slot 36 again.
    held = list(last["floats"])
    try:
        again = s.move("repeat (noop expected)", cool, a["log"])
        noop_ok = again["floats"] == held
    except RuntimeError as e:  # a no-change commit may time out; the sidecar decides
        a["log"].append(f"repeat commit raised: {e}")
        noop_ok = s.colorcal()[1] == held
    fresh = s.move("fresh sentinel", round(start["temperature"] + 500.0), a["log"])
    live = (sentinel_slots(fresh["floats"], fresh["k"], tol=1.0) == [SLOT]
            and abs(fresh["floats"][SLOT] - held[SLOT]) > 1.0)
    a["illuminants"].append((fresh["params"] or {}).get("illuminant"))
    a.update(slots_ok=slots_ok, noop_ok=noop_ok, sentinel_live=live,
             preset=s.darkroom.wb_preset(s.image))
    return a


def _oracle_b(s: _Session, k0: float) -> dict:
    s.commit()
    start, _ = s.colorcal()
    if not start:
        raise RuntimeError("no colorcal params at start (B)")
    b: dict = {"log": [], "k0": start["temperature"], "illuminants": [start["illuminant"]]}
    base = s.rb()
    warm, cool = _targets(start["temperature"])
    rbs = []
    for label, k in (("warmer", warm), ("cooler (2nd consecutive)", cool)):
        got = s.move(label, k, b["log"])
        rbs.append(s.rb())
        b["log"].append(f"  r/b base {base:.4f} -> {rbs[-1]:.4f}")
        b["illuminants"].append((got["params"] or {}).get("illuminant"))
    b.update(base=base, warm=rbs[0], cool=rbs[1], contaminated=abs(start["temperature"] - k0) > 1.0,
             preset=s.darkroom.wb_preset(s.image))
    return b


def probe_body(run_dir: Path, body: str, deadline: float) -> tuple[str, str]:
    """Both oracles for one body; always returns a recorded verdict."""
    _record(f"\n### {body}\n")
    a: dict = {}
    try:
        a = _Session(run_dir, body, "A", deadline).run(_oracle_a)
        for line in a["log"]:
            _record(f"- A {line}")
        _record(f"| channelmixerrgb.temperature | `{PATH}` | "
                f"{'pinned' if a['pinned'] else 'NOT pinned'} | {a['ms']} |")
        b = _Session(run_dir, body, "B", deadline).run(lambda s: _oracle_b(s, a["k0"]))
        for line in b["log"]:
            _record(f"- B {line}")
        ev = Evidence(pinned=a["pinned"], slots_ok=a["slots_ok"], sentinel_live=a["sentinel_live"],
                      noop_ok=a["noop_ok"], illuminants=tuple(a["illuminants"] + b["illuminants"]),
                      preset=a["preset"] if a["preset"] == b["preset"] else -1,
                      contaminated=b["contaminated"], base_rb=b["base"], warm_rb=b["warm"],
                      cool_rb=b["cool"])
        verdict, reason = route_verdict(ev)
    except Exception as e:  # noqa: BLE001 -- the verdict must still be recorded
        verdict, reason = "DO_NOT_PROMOTE", f"probe error {type(e).__name__}: {e}"
    line = f"COLORCAL_ROUTE_A {body} {verdict} reason={reason}"
    _record(line)
    _prose(f"- Route A colour calibration probe {time.strftime('%Y-%m-%d %H:%M')}, {body}: "
           f"{verdict} ({reason}). Isolated library under {run_dir}.")
    return verdict, reason


@unittest.skipUnless(RUN, REASON)
class ColorCalWarmthHostTest(unittest.TestCase):
    def test_route_a_probe_both_bodies(self) -> None:
        hs.assert_no_concurrent_darktable()
        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        PROBE_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = unique_run_dir(PROBE_ROOT, os.getpid(), time.strftime("%Y%m%dT%H%M%S"))
        run_dir.mkdir()
        os.environ.setdefault("SCOUTER_JOB", f"colorcal-host-{os.getpid()}")
        deadline = time.monotonic() + TOTAL_BUDGET_S
        _record(f"\n## Route A color calibration probe ({time.strftime('%Y-%m-%d %H:%M')}, "
                f"run {run_dir.name})\n")
        _prose(f"\n### Route A colour calibration probe, run {run_dir.name}\n")
        install = hs.PluginInstall()
        install.install()
        try:
            results = {body: probe_body(run_dir, body, deadline) for body in BODIES}
        finally:
            install.restore()
        bad = {b: v for b, v in results.items() if v[0] != "PROMOTE"}
        self.assertFalse(bad, f"route A not promotable: {bad}")
