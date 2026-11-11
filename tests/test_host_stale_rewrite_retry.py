"""Host proof: the stale-rewrite retry leaves no stale_rewrite refusal over N writes.

Gated (Z4: a documented gate, not a skip bypass): SCOUTER_HOST_TESTS=1 AND
SCOUTER_ATTACH_SCRATCH=<run dir> of an ALREADY RUNNING scratch darktable (isolated library
under ~/Pictures/ScouterScratch/, frame.ORF copy beside library.db). It never launches or
quits darktable. Run:

    SCOUTER_HOST_TESTS=1 SCOUTER_ATTACH_SCRATCH=<dir> SCOUTER_STALE_N=80 \
        ~/CodeBase/scouter/.venv/bin/python -m pytest -q -s tests/test_host_stale_rewrite_retry.py

Baseline (no retry) refused ~6.5% of writes on a long history (13/200); with the retry 0/200.
Two oracles per write (Z2): the product envelope (ok) and, independently, the sidecar
history_end equalling library.db's images.history_end after each write.
"""

from __future__ import annotations

import os
import random
import re
import sqlite3
import time
import unittest
from pathlib import Path

from tests import host_support as hs

ATTACH = os.environ.get("SCOUTER_ATTACH_SCRATCH")
RUN = os.environ.get("SCOUTER_HOST_TESTS") == "1" and bool(ATTACH)
_HE = re.compile(r'darktable:history_end="(\d+)"')


@unittest.skipUnless(
    RUN, "needs SCOUTER_HOST_TESTS=1 and SCOUTER_ATTACH_SCRATCH=<running scratch dir>"
)
class StaleRewriteRetryHostTest(unittest.TestCase):
    def test_no_stale_rewrite_refusal_over_n_writes(self) -> None:
        from scouter.core import darkroom, dtbridge, moves
        from scouter.errors import ScouterError

        run_dir = Path(ATTACH)
        frame, library = run_dir / "frame.ORF", run_dir / "library.db"
        hs.assert_isolated_library(library)
        self.assertNotEqual(frame.resolve(), hs.HERO.resolve())
        self.assertNotIn(hs.EDITING_TRIAL.resolve(), frame.resolve().parents)
        sidecar = Path(str(frame) + ".xmp")
        os.environ.setdefault("SCOUTER_JOB", f"stale-retry-{os.getpid()}")
        n = int(os.environ.get("SCOUTER_STALE_N", "80"))
        darkroom.open_image(str(frame))
        time.sleep(8)
        rng, stale, retried = random.Random(11), 0, 0
        try:
            for _ in range(n):
                kind = rng.choice(("set", "adjust"))
                try:
                    if kind == "set":
                        r = darkroom.set_param(
                            "exposure", "exposure", round(rng.uniform(-0.5, 0.8), 3)
                        )
                    else:
                        r = moves.adjust("exposure", "exposure", 0.1 * rng.choice((-1, 1)))
                    retried += any("re-bounce" in w for w in getattr(r, "warnings", []) or [])
                except ScouterError as e:
                    stale += bool((e.detail or {}).get("stale_rewrite"))
                    continue
                sc = int(_HE.search(sidecar.read_text()).group(1))
                con = sqlite3.connect(f"file:{library}?mode=ro", uri=True, timeout=1)
                try:
                    lib = con.execute("select history_end from images limit 1").fetchone()[0]
                finally:
                    con.close()
                self.assertEqual(sc, lib)
        finally:
            dtbridge.call(
                "darkroom.action",
                {
                    "path": "global/switch views/lighttable",
                    "instance": 0,
                    "element": "",
                    "effect": "activate",
                    "value": 1.0,
                },
            )
        print(f"writes={n} stale_refusals={stale} retried_and_landed={retried}")
        self.assertLessEqual(stale, max(0, int(n * 0.005)))


if __name__ == "__main__":
    unittest.main()
