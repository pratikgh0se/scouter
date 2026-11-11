# tests/test_host_colorcal.py
"""Route A host probe: color calibration's illuminant temperature (rows 458/461).

Gated: SCOUTER_HOST_TESTS=1 + darktable installed. Run the whole probe with:

    SCOUTER_HOST_TESTS=1 ~/CodeBase/scouter/.venv/bin/python -m pytest -q -s \
        tests/test_host_colorcal.py

ISOLATION (standing rule): every launch gets its OWN `--library` under
`~/Pictures/ScouterScratch/colorcal-probe/<run-id>/<body>/<A|B>/library.db`
on a scratch COPY of a hero frame staged in that same dir. Pratik's real
library.db and the real photos (read-only copy source) are never opened by
darktable. `--library` moves library.db ONLY: darktable still reads and
rewrites the real ~/.config/darktable/darktablerc and uses the real data.db
(as every phase 2/3 host probe does). The Lua plugin is loaded from
~/.config/darktable (PluginInstall, restored).

Per body (Olympus hero, Sony hero), two independent launches:
- Oracle A (sidecar): path answers non-nan; a warmer then a cooler move (the
  second consecutive move proves the route survives its own first write) land
  at exactly slot 36; a repeat set of the same value is a noop; a fresh
  sentinel changes slot 36 again (a dead or zero-filled slot cannot pass).
  Requested vs landed K is logged per move; a mismatch is CLAMPED, not "dead".
- Oracle B (pixels, `scout.stats` centre r/b): higher K must raise r/b by more
  than a measured floor (two renders of the pristine frame). B verifies its
  own writes landed at slot 36. Contamination guard: B's pristine temperature
  and colour calibration history entry count must equal A's.
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
from tests.colorcal_probe_logic import Evidence, noise_floor, route_verdict, unique_run_dir
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
        self.deadline, self.tag = deadline, tag
        self.log: list[str] = []
        self.flushed = 0
        self.moves: list[tuple] = []
        self.tick()
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

    def cc_count(self) -> int:
        return sum(e.operation == "channelmixerrgb" for e in self.history.read(self.image).entries)

    def move(self, label: str, k: float) -> dict:
        """One action + commit; log requested vs landed K, illuminant and x/y."""
        from scouter.core import dtbridge

        before, _ = self.colorcal()
        dtbridge.action(PATH, value=float(k))
        self.commit()
        after, floats = self.colorcal()
        b, a = before or {}, after or {}
        landed = a.get("temperature")
        slots = sentinel_slots(floats, float(k), tol=1.0)
        self.log.append(f"{label}: requested {k:.0f} K, landed {_fmt(landed)} K, slots {slots} "
                        f"(expect [{SLOT}]), illuminant {b.get('illuminant')} -> "
                        f"{a.get('illuminant')}, x/y {_fmt(b.get('x'))}/{_fmt(b.get('y'))} -> "
                        f"{_fmt(a.get('x'))}/{_fmt(a.get('y'))}")
        self.moves.append((label, float(k), landed, slots))
        return {"params": after, "floats": floats, "k": float(k), "slots": slots}

    def clamp_note(self) -> str:
        """Moves whose landed K differs from the request, both numbers named."""
        return "; ".join(f"{label}: requested {k:.0f} K, landed {_fmt(landed)} K"
                         for label, k, landed, _ in self.moves
                         if not isinstance(landed, (int, float)) or abs(landed - k) > 1.0)

    def flush(self) -> None:
        """Record log lines not yet recorded (also used on the error path)."""
        for line in self.log[self.flushed:]:
            _record(f"- {self.tag} {line}")
        self.flushed = len(self.log)

    def run(self, work) -> object:
        """Launch isolated, wait, run `work(self)`, always quit darktable."""
        from scouter.core import dtbridge

        self.tick()
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
    a: dict = {"pinned": verdict == "pinned", "ms": ms, "k0": start["temperature"],
               "illuminants": [start["illuminant"]], "preset_start": s.darkroom.wb_preset(s.image),
               "cc_count": s.cc_count()}
    s.log.append(f"start: illuminant {start['illuminant']}, temperature "
                 f"{_fmt(start['temperature'])}, x/y {_fmt(start.get('x'))}/"
                 f"{_fmt(start.get('y'))}, WB preset {a['preset_start']}")
    warm, cool = _targets(start["temperature"])
    slots_ok = True
    last = {}
    for label, k in (("warmer", warm), ("cooler (2nd consecutive)", cool)):
        last = s.move(label, k)
        slots_ok &= last["slots"] == [SLOT]
        a["illuminants"].append((last["params"] or {}).get("illuminant"))
    # live sentinel: same value again is a noop; a fresh value moves slot 36 again.
    held = list(last["floats"])
    try:
        again = s.move("repeat (noop expected)", cool)
        noop_ok = again["floats"] == held
    except RuntimeError as e:  # a no-change commit may time out; the sidecar decides
        s.log.append(f"repeat commit raised: {e}")
        noop_ok = s.colorcal()[1] == held
    fresh = s.move("fresh sentinel", round(start["temperature"] + 500.0))
    live = fresh["slots"] == [SLOT] and abs(fresh["floats"][SLOT] - held[SLOT]) > 1.0
    a["illuminants"].append((fresh["params"] or {}).get("illuminant"))
    a.update(slots_ok=slots_ok, noop_ok=noop_ok, sentinel_live=live,
             preset=s.darkroom.wb_preset(s.image))
    return a


def _oracle_b(s: _Session, k0: float, cc_count_a: int) -> dict:
    s.commit()
    start, _ = s.colorcal()
    if not start:
        raise RuntimeError("no colorcal params at start (B)")
    b: dict = {"k0": start["temperature"], "illuminants": [start["illuminant"]],
               "preset_start": s.darkroom.wb_preset(s.image), "cc_count": s.cc_count()}
    base, base2 = s.rb(), s.rb()
    floor = noise_floor(base, base, base2)
    s.log.append(f"start: pristine r/b {base:.4f} and {base2:.4f}, measured floor {floor:.4f}, "
                 f"temperature {_fmt(start['temperature'])}")
    warm, cool = _targets(start["temperature"])
    rbs = []
    landed = True
    for label, k in (("warmer", warm), ("cooler (2nd consecutive)", cool)):
        got = s.move(label, k)
        landed &= got["slots"] == [SLOT]
        rbs.append(s.rb())
        s.log.append(f"  r/b base {base:.4f} -> {rbs[-1]:.4f}")
        b["illuminants"].append((got["params"] or {}).get("illuminant"))
    b.update(base=base, warm=rbs[0], cool=rbs[1], floor=floor, b_landed=landed,
             contaminated=abs(start["temperature"] - k0) > 1.0 or b["cc_count"] != cc_count_a,
             preset=s.darkroom.wb_preset(s.image))
    return b


def probe_body(run_dir: Path, body: str, deadline: float) -> tuple[str, str]:
    """Both oracles for one body; always returns a recorded verdict."""
    _record(f"\n### {body}\n")
    sessions: list[_Session] = []
    try:
        sa = _Session(run_dir, body, "A", deadline)
        sessions.append(sa)
        a = sa.run(_oracle_a)
        sa.flush()
        _record(f"| channelmixerrgb.temperature | `{PATH}` | "
                f"{'pinned' if a['pinned'] else 'NOT pinned'} | {a['ms']} |")
        sb = _Session(run_dir, body, "B", deadline)
        sessions.append(sb)
        b = sb.run(lambda s: _oracle_b(s, a["k0"], a["cc_count"]))
        sb.flush()
        presets = (a["preset_start"], a["preset"], b["preset_start"], b["preset"])
        ev = Evidence(pinned=a["pinned"], slots_ok=a["slots_ok"], sentinel_live=a["sentinel_live"],
                      noop_ok=a["noop_ok"], illuminants=tuple(a["illuminants"] + b["illuminants"]),
                      preset=next((p for p in presets if p != 4), 4),
                      contaminated=b["contaminated"], base_rb=b["base"], warm_rb=b["warm"],
                      cool_rb=b["cool"], b_landed=b["b_landed"], floor=b["floor"],
                      clamped="; ".join(filter(None, (x.clamp_note() for x in sessions))))
        verdict, reason = route_verdict(ev)
    except Exception as e:  # noqa: BLE001 -- the verdict must still be recorded
        verdict, reason = "DO_NOT_PROMOTE", f"probe error {type(e).__name__}: {e}"
    for sess in sessions:
        sess.flush()  # a failed run still leaves its per-move evidence
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
