# tests/test_host_colorcal.py
"""Route A host probe: color calibration's illuminant temperature (decision
rows 458/461). Gated: SCOUTER_HOST_TESTS=1 + darktable. ONE darktable launch.

Deferred while a gauntlet run owns darktable. Run it with:

    SCOUTER_HOST_TESTS=1 uv run --extra dev python -m pytest -q tests/test_host_colorcal.py -s

It pins nothing by itself (phase 3 §10 whitelist rule): on a pass, hand-edit
`dtbridge.ACTION_PATHS[("channelmixerrgb", "temperature")] =
"iop/channelmixerrgb/temperature"` citing the run in
docs/measurements-generated.md, and update tests/test_vocab.py's status set
and the vocab golden in the same commit.

What it establishes, on a fresh scratch copy of the hero ORF (darktable 5.4
default: temperature preset 4 + color calibration, daylight illuminant):
1. the candidate path answers non-nan and the worker stays alive (§10.1);
2. sentinel: a set of the illuminant temperature lands at exactly one float
   slot, slot 36 = byte 144 of channelmixerrgb v3 (`xmp.LAYOUTS`) (§10.2);
3. second oracle (Zenkai Z2): the rendered centre r/b ratio (`scout.stats`)
   goes UP for a higher illuminant temperature and DOWN for a lower one.
   This is the step that VERIFIES route A's direction assumption (higher
   illuminant K = warmer); until it passes, that sign is unverified;
4. the WB module is untouched: temperature preset still 4 after both moves.
"""

from __future__ import annotations

import os
import shutil
import time
import unittest
from pathlib import Path

from tests import host_support as hs
from tests.test_host_phase3 import (
    REASON,
    RUN,
    _record,
    entry_floats,
    probe_verdict,
    sentinel_slots,
    settle,
)

SCRATCH = hs.SCRATCH_DIR / "colorcal-probe.ORF"
PATH = "iop/channelmixerrgb/temperature"
SLOT = 36  # byte 144 / 4


@unittest.skipUnless(RUN, REASON)
class ColorCalWarmthHostTest(unittest.TestCase):
    def test_probe_slot_sign_and_wb_untouched(self) -> None:
        from scouter import registry
        from scouter.core import darkroom, dtbridge, history, xmp

        hs.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        Path(str(SCRATCH) + ".xmp").unlink(missing_ok=True)
        shutil.copy(hs.HERO, SCRATCH)
        os.environ.setdefault("SCOUTER_JOB", f"colorcal-host-{os.getpid()}")

        def commit() -> None:
            registry.call("history.commit", "cli", image=str(SCRATCH))

        def rb() -> float:
            st = registry.call("scout.stats", "cli", image=str(SCRATCH), px=512).data["stats"]
            ch = st["channels"]
            return ch["r"]["mean"] / max(ch["b"]["mean"], 1e-6)

        def colorcal() -> tuple[dict | None, list[float]]:
            parsed = darkroom._read_sidecar_parsed(str(SCRATCH))
            params = xmp.latest_params(parsed, "channelmixerrgb") if parsed else None
            entries = [e for e in history.read(str(SCRATCH)).entries
                       if e.operation == "channelmixerrgb"]
            last = max(entries, key=lambda e: e.num) if entries else None
            return params, entry_floats(last.params_hex) if last else []

        install = hs.PluginInstall()
        install.install()
        proc = hs.launch(SCRATCH)
        _record(f"\n## Route A color calibration probe ({time.strftime('%Y-%m-%d %H:%M')})\n")
        try:
            hs.wait_ready()
            self.assertTrue(settle(dtbridge.call), "control path did not answer after settle")
            verdict, ms = probe_verdict(dtbridge.call, PATH)
            _record(f"| channelmixerrgb.temperature | `{PATH}` | {verdict} | {ms} |")
            self.assertEqual(verdict, "pinned")

            commit()
            preset0 = darkroom.wb_preset(str(SCRATCH))
            before, _ = colorcal()
            shown = {k: before[k] for k in ("illuminant", "adaptation", "temperature")} \
                if before else None
            _record(f"start: temperature preset {preset0}, colorcal {shown}")
            self.assertEqual(preset0, 4, "hero scratch is not in camera reference (late) mode")
            self.assertIn(before["illuminant"], (2, 6))
            base = rb()

            results = {}
            for label, k in (("warmer", round(before["temperature"] + 1000.0)),
                             ("cooler", round(before["temperature"] - 1000.0))):
                dtbridge.action(PATH, value=float(k))
                commit()
                params, floats = colorcal()
                slots = sentinel_slots(floats, float(k), tol=1.0)
                ratio = rb()
                preset = darkroom.wb_preset(str(SCRATCH))
                results[label] = (slots, ratio, preset, params)
                # Review M2: log x/y too -- the fixture's stored (x, y) sits off
                # the daylight locus, so a first slider move may also re-derive
                # them (a green/magenta shift r/b alone would not show).
                _record(f"{label}: set {k} K -> slots {slots} (expect [{SLOT}]), decoded "
                        f"{params and params.get('temperature')}, x/y "
                        f"{before.get('x'):.4f}/{before.get('y'):.4f} -> "
                        f"{params and params.get('x')}/{params and params.get('y')}, "
                        f"r/b {base:.4f} -> {ratio:.4f}, WB preset {preset}")
            self.assertEqual(results["warmer"][0], [SLOT])
            self.assertEqual(results["cooler"][0], [SLOT])
            self.assertGreater(results["warmer"][1], base, "warmer must raise r/b")
            self.assertLess(results["cooler"][1], base, "cooler must lower r/b")
            self.assertEqual({results["warmer"][2], results["cooler"][2]}, {4})
        finally:
            hs.quit_darktable(proc)
            install.restore()
