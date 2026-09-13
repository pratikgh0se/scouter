"""Task 1: measure the commit trigger and the Scouter loop on this Mac.

Host-only. Writes docs/measurements.md. Never leaves darktable running.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
DARKTABLE_CLI = Path("/Applications/darktable.app/Contents/MacOS/darktable-cli")
HERO = Path("/Users/pratik/Pictures/EditingTrial/P6281348.ORF")
MEASUREMENTS = REPO / "docs" / "measurements.md"
LOOP_BUDGET_S = 3.0

SKIP_REASON = (
    f"host-only: {DARKTABLE} not present (darktable is not installed on this "
    "machine, so the commit trigger and the Scouter loop cannot be measured)"
)


def _quit_darktable(proc: subprocess.Popen[bytes]) -> None:
    """Graceful quit, then SIGTERM, then SIGKILL. Never leaves it running."""
    subprocess.run(
        ["osascript", "-e", 'tell application "darktable" to quit'],
        capture_output=True,
        timeout=30,
        check=False,
    )
    try:
        proc.wait(timeout=20)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    proc.wait(timeout=10)


@unittest.skipUnless(DARKTABLE.is_file() and HERO.is_file(), SKIP_REASON)
class CommitTriggerMeasurement(unittest.TestCase):
    maxDiff = None

    def test_probe_records_a_commit_strategy_and_a_loop_time(self) -> None:
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"scouter-probe-{os.getpid()}"
        configdir = tmp / "config"
        (configdir / "lua").mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "spike" / "scouter_commit_probe.lua", configdir / "lua")
        (configdir / "luarc").write_text('require "scouter_commit_probe"\n')
        out = tmp / "probe.jsonl"

        xmp = Path(str(HERO) + ".xmp")
        mtime_before = xmp.stat().st_mtime_ns if xmp.exists() else 0

        env = dict(os.environ, SCOUTER_PROBE_OUT=str(out))
        proc = subprocess.Popen(
            [str(DARKTABLE), "--configdir", str(configdir), str(HERO)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if out.exists() and '"probe":"done"' in out.read_text():
                    break
                time.sleep(0.5)
            self.assertTrue(out.exists(), "probe wrote nothing: plugin did not load")
            records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        finally:
            _quit_darktable(proc)

        by_name = {r["probe"]: r for r in records if "value" in r}
        self.assertIn("set_exposure_0_9", by_name)
        self.assertNotEqual(
            by_name["set_exposure_0_9"]["value"], "nan",
            "absolute set returned nan: darkroom was not active (phase 0 finding 2)",
        )

        # Which candidate returned a real value (not nan, not raised)?
        accepted = [
            name
            for name in ("c1_selected_images", "c2_metadata", "c2_image")
            if name in by_name and by_name[name]["value"] not in (None, "nan")
        ]
        strategy = "sidecar_action" if accepted else "view_bounce"

        # The loop measurement: warm configdir, PPM at 1024 px, then stats.
        cli_cfg = tmp / "cli-config"
        cli_cfg.mkdir(parents=True, exist_ok=True)
        ppm = tmp / "loop.ppm"
        timings = []
        for _ in range(3):
            t0 = time.monotonic()
            subprocess.run(
                [
                    str(DARKTABLE_CLI), str(HERO), str(ppm),
                    "--width", "1024", "--height", "1024", "--out-ext", "ppm",
                    "--core", "--configdir", str(cli_cfg), "--library", ":memory:",
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
            self.assertTrue(ppm.exists() and ppm.stat().st_size > 0, "darktable-cli wrote nothing")
            data = ppm.read_bytes()
            head = data.split(b"\n", 3)
            payload = data[len(head[0]) + len(head[1]) + len(head[2]) + 3:]
            hi = payload[0::2] if b"65535" in head[2] else payload
            from collections import Counter

            for offset in range(3):
                Counter(hi[offset::3])
            timings.append(time.monotonic() - t0)
            ppm.unlink()

        best = min(timings)
        MEASUREMENTS.parent.mkdir(parents=True, exist_ok=True)
        MEASUREMENTS.write_text(
            "# Scouter measurements\n\n"
            "Written by `tests/test_commit_trigger_host.py` (task 1). Host: this Mac, "
            "darktable 5.4.1, Apple M4.\n\n"
            "## Commit trigger (phase 1 design 8.1)\n\n"
            f"- accepted candidates: {accepted or 'none'}\n"
            f"- **strategy adopted for `history.commit(strategy=\"auto\")`: `{strategy}`**\n"
            f"- view after C1: {by_name.get('view_after_c1', {}).get('value')}\n"
            f"- view bounce (C3) cost: {by_name.get('c3_view_bounce', {}).get('ms')} ms\n"
            f"- global undo action probe: {by_name.get('undo_global', {}).get('value')} "
            f"/ darkroom undo: {by_name.get('undo_darkroom', {}).get('value')} "
            "(phase-2 note only; phase 1 uses the journal, design 8.2)\n"
            f"- XMP mtime advanced during the run: {xmp.stat().st_mtime_ns > mtime_before}\n\n"
            "## Scouter loop, 20 MP ORF at 1024 px (design 6.1)\n\n"
            f"- runs (s): {[round(t, 3) for t in timings]}\n"
            f"- **best: {best:.3f} s** (budget {LOOP_BUDGET_S} s)\n\n"
            # Indented block, not a fenced one: a fence inside a Python string
            # inside this plan's own code fence would close the fence.
            "## Raw probe records\n\n"
            + "\n".join("    " + json.dumps(r) for r in records)
            + "\n"
        )
        self.assertLess(best, LOOP_BUDGET_S, f"Scouter loop {best:.3f}s over the 3s budget")


if __name__ == "__main__":
    unittest.main()
