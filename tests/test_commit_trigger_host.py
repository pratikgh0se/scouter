"""Task 1: measure the commit trigger and the Scouter loop on this Mac.

Host-only. Writes `docs/measurements-generated.md` -- NOT
`docs/measurements.md`, which is the committed, reviewed record and is no
longer rewritten wholesale by a test run (final review, should-fix 3). Never
leaves darktable running.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
DARKTABLE_CLI = Path("/Applications/darktable.app/Contents/MacOS/darktable-cli")
HERO = Path("/Users/pratik/Pictures/EditingTrial/P6281348.ORF")
#: Generated, git-ignored-by-convention sibling of the committed
#: docs/measurements.md. A test run must not rewrite a reviewed document.
MEASUREMENTS = REPO / "docs" / "measurements-generated.md"
LOOP_BUDGET_S = 3.0

_MISSING = []
if os.environ.get("SCOUTER_HOST_TESTS") != "1":
    _MISSING.append(
        "SCOUTER_HOST_TESTS=1 not set (this launches darktable and writes "
        "docs/measurements-generated.md)"
    )
if not DARKTABLE.is_file():
    _MISSING.append(f"darktable app ({DARKTABLE} not present)")
if not HERO.is_file():
    _MISSING.append(f"sample ORF ({HERO} not present)")
SKIP_REASON = (
    "host-only: missing " + ", ".join(_MISSING) + " — commit trigger and "
    "Scouter loop cannot be measured"
    if _MISSING
    else ""
)


def _quit_darktable(proc: subprocess.Popen[bytes]) -> None:
    """Graceful quit, then SIGTERM, then SIGKILL. Never leaves it running.

    EVERY subprocess call here is guarded against TimeoutExpired (final
    review, should-fix 3). `osascript` can hang on a stuck AppleEvent -- this
    happened for real on this Mac during task 12 -- and `subprocess.run`'s
    timeout raises rather than returning, so an unguarded call would abort
    this function before the SIGTERM/SIGKILL escalation ever ran and leave
    darktable alive. The final `proc.wait` is guarded too: a process wedged in
    the kernel cannot be killed, and this function must still return so the
    caller's own assertions (and config restore) run.
    """
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "darktable" to quit'],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass
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
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


@unittest.skipUnless(not _MISSING, SKIP_REASON)
@pytest.mark.host
class CommitTriggerMeasurement(unittest.TestCase):
    maxDiff = None

    def test_probe_records_a_commit_strategy_and_a_loop_time(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix=f"scouter-probe-{os.getpid()}-"))
        try:
            self._run(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run(self, tmp: Path) -> None:
        configdir = tmp / "config"
        (configdir / "lua").mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "spike" / "scouter_commit_probe.lua", configdir / "lua")
        (configdir / "luarc").write_text('require "scouter_commit_probe"\n')
        out = tmp / "probe.jsonl"
        bounce_before = Path(str(out) + ".bounce_before")
        bounce_after = Path(str(out) + ".bounce_after")

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
            by_name["set_exposure_0_9"]["value"],
            "nan",
            "absolute set returned nan: darkroom was not active (phase 0 finding 2)",
        )

        # Which candidate returned a real value (not nan, not raised)?
        accepted = [
            name
            for name in ("c1_selected_images", "c2_metadata", "c2_image")
            if name in by_name and by_name[name]["value"] not in (None, "nan")
        ]
        strategy = "sidecar_action" if accepted else "view_bounce"

        # Wall-clock cost of the C3 view bounce: Lua's os.clock() is CPU time,
        # not wall time, so the probe only drops marker files immediately
        # before/after the bounce and Python computes the real delta.
        bounce_wall_ms = None
        if bounce_before.exists() and bounce_after.exists():
            bounce_wall_ms = (
                bounce_after.stat().st_mtime_ns - bounce_before.stat().st_mtime_ns
            ) / 1e6

        # The loop measurement: warm configdir, PPM at 1024 px, then stats.
        # Render (subprocess + read) and the stats pass are timed separately
        # so task 8 gets honest numbers instead of one blended figure.
        cli_cfg = tmp / "cli-config"
        cli_cfg.mkdir(parents=True, exist_ok=True)
        ppm = tmp / "loop.ppm"
        timings = []  # (render_s, stats_s)
        for _ in range(3):
            t0 = time.monotonic()
            subprocess.run(
                [
                    str(DARKTABLE_CLI),
                    str(HERO),
                    str(ppm),
                    "--width",
                    "1024",
                    "--height",
                    "1024",
                    "--out-ext",
                    "ppm",
                    "--core",
                    "--configdir",
                    str(cli_cfg),
                    "--library",
                    ":memory:",
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
            self.assertTrue(ppm.exists() and ppm.stat().st_size > 0, "darktable-cli wrote nothing")
            data = ppm.read_bytes()
            render_s = time.monotonic() - t0

            t1 = time.monotonic()
            head = data.split(b"\n", 3)
            payload = data[len(head[0]) + len(head[1]) + len(head[2]) + 3 :]
            hi = payload[0::2] if b"65535" in head[2] else payload
            from collections import Counter

            for offset in range(3):
                Counter(hi[offset::3])
            stats_s = time.monotonic() - t1

            timings.append((render_s, stats_s))
            ppm.unlink()

        render_s, stats_s = min(timings, key=lambda t: t[0] + t[1])
        best_total = render_s + stats_s
        MEASUREMENTS.parent.mkdir(parents=True, exist_ok=True)
        MEASUREMENTS.write_text(
            "# Scouter measurements (generated)\n\n"
            "GENERATED FILE -- rewritten in full by "
            "`tests/test_commit_trigger_host.py` on every host run. The reviewed, "
            "committed record is `docs/measurements.md`; copy numbers across "
            "deliberately, do not point anything at this file.\n\n"
            f"Run at {time.strftime('%Y-%m-%d %H:%M:%S %z')}. Host: this Mac, "
            "darktable 5.4.1, Apple M4.\n\n"
            "## Commit trigger (phase 1 design 8.1)\n\n"
            f"- accepted candidates: {accepted or 'none'}\n"
            f'- **strategy adopted for `history.commit(strategy="auto")`: `{strategy}`**\n'
            f"- view after C1: {by_name.get('view_after_c1', {}).get('value')}\n"
            f"- view bounce (C3) cost, wall clock: "
            f"{round(bounce_wall_ms) if bounce_wall_ms is not None else 'unknown'} ms\n"
            f"- view bounce (C3) cost, CPU time (os.clock, not wall clock): "
            f"{by_name.get('c3_view_bounce_cpu', {}).get('ms')} ms\n"
            f"- global undo action probe: {by_name.get('undo_global', {}).get('value')} "
            f"/ darkroom undo: {by_name.get('undo_darkroom', {}).get('value')} "
            "(phase-2 note only; phase 1 uses the journal, design 8.2)\n"
            f"- XMP mtime advanced during the run: {xmp.stat().st_mtime_ns > mtime_before}\n\n"
            "## Scouter loop, 20 MP ORF at 1024 px (design 6.1)\n\n"
            f"- runs, (render+read s, stats s): "
            f"{[(round(r, 3), round(s, 3)) for r, s in timings]}\n"
            f"- best run: render+read: {render_s:.3f} s, stats pass: {stats_s:.3f} s\n"
            f"- **loop total: {best_total:.3f} s** (budget {LOOP_BUDGET_S} s)\n\n"
            # Indented block, not a fenced one: a fence inside a Python string
            # inside this plan's own code fence would close the fence.
            "## Raw probe records\n\n" + "\n".join("    " + json.dumps(r) for r in records) + "\n"
        )
        self.assertLess(
            best_total,
            LOOP_BUDGET_S,
            f"Scouter loop {best_total:.3f}s over the {LOOP_BUDGET_S}s budget",
        )


if __name__ == "__main__":
    unittest.main()
