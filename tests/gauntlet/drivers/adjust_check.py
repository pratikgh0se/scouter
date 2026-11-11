"""Row 430/431 live check: does a relative move / enable really reach the
sidecar, and does a commit with nothing to commit answer promptly?

Run via run_adjust_check.sh with darktable CLOSED. Reuses acceptance_driver's
Darktable (launch/quit), Mcp (stdio JSON-RPC) and acc_env (scratch config,
scratch cache, scratch vault). Never touches EditingTrial or ~/.config/scouter.

Steps: back up the installed darktable_mcp.lua, install the plugin from the
fix worktree with the repo's own `darktable_mcp install-plugin`, copy one
ScouterScratch bench frame (raw only) into a fresh run dir, launch darktable
on it with a fresh library, then over MCP: darkroom_open, history_commit on
the still-unedited frame (row 431: timeout_s=10 must answer within 12s with
noop=true), history_read (before), darkroom_say "brighter by 1 ev", history_read, darkroom_enable
denoiseprofile, history_read; print exposure + history length at each point;
quit darktable; restore the previous plugin unless KEEP_FIX_PLUGIN=1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance_driver as drv  # noqa: E402

FIX = Path.home() / "CodeBase/scouter"
SOURCE_FRAME = (
    Path.home() / "Pictures/ScouterScratch/bench-runs/20260926t073459z/olympus/frames/P5240151.ORF"
)
CHECK_ROOT = Path.home() / "Pictures" / "ScouterScratch" / "adjust-check"

# Point the driver at the fix worktree and at this check's own run dir. Its
# helpers read these module globals at call time.
drv.REPO = FIX
drv.RUN = CHECK_ROOT / "runs" / drv.STAMP
drv.LOG = CHECK_ROOT / "log.md"
sys.path.insert(0, str(FIX))  # the bridge the driver imports comes from the fix


def env() -> dict:
    e = drv.acc_env()
    e["PYTHONPATH"] = str(FIX)
    e.pop("SCOUTER_JOB", None)
    return e


def install_fix_plugin() -> tuple[Path, Path | None]:
    out = subprocess.run(
        [drv.PY, "-c", "from darktable_mcp.bridge.client import plugin_path; print(plugin_path())"],
        env=env(),
        cwd=str(FIX),
        capture_output=True,
        text=True,
        check=True,
    )
    plugin = Path(out.stdout.strip())
    backup = None
    if plugin.exists():
        backup = drv.RUN / "darktable_mcp.lua.before"
        shutil.copy2(plugin, backup)
    subprocess.run(
        [drv.PY, "-m", "darktable_mcp", "install-plugin"], env=env(), cwd=str(FIX), check=True
    )
    fix_bytes = (FIX / "darktable_mcp" / "lua" / "darktable_mcp.lua").read_bytes()
    if plugin.read_bytes() != fix_bytes:
        raise SystemExit(f"installed plugin at {plugin} is not the fix worktree's copy")
    if b"sidecar_history_signature" not in fix_bytes:
        raise SystemExit("fix worktree plugin lacks the row 430 change")
    if b"budget_remaining_ms" not in fix_bytes:
        raise SystemExit("fix worktree plugin lacks the row 431 change")
    if b"GUI_GATE_WAIT_MS" not in fix_bytes:
        raise SystemExit("plugin lacks the row 433 gui-ready gate")
    print(f"installed fix plugin -> {plugin} (backup: {backup})")
    return plugin, backup


def exposure_and_length(envelope: dict) -> tuple[float | None, int | None, int | None]:
    d = envelope.get("data") or {}
    end = d.get("history_end")
    entries = d.get("entries") or []
    exp = None
    best = -1
    for e in entries:
        num = e.get("num", -1)
        if e.get("operation") == "exposure" and (end is None or num < end) and num > best:
            best = num
            exp = (e.get("decoded") or {}).get("exposure")
    return exp, end, len(entries)


def main() -> int:
    if drv.darktable_running():
        print("darktable is running -- quit it first")
        return 1
    drv.RUN.mkdir(parents=True, exist_ok=True)
    # This process talks to the bridge too (the driver's launch polling):
    # same scratch config/cache as the MCP server, never ~/.config/scouter.
    os.environ.update(env())
    frame_dir = drv.RUN / "frame"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame = frame_dir / SOURCE_FRAME.name
    shutil.copyfile(SOURCE_FRAME, frame)  # raw only: a fresh, unedited import
    frame.chmod(0o644)
    plugin, backup = install_fix_plugin()
    dt = drv.Darktable()
    mcp = None
    rows: list[tuple[str, float | None, int | None, str]] = []
    noop_ok: bool | None = None
    try:
        if not dt.launch(
            "launch darktable on the check frame",
            frame,
            drv.RUN / "library" / "library.db",
            settle_s=5.0,
        ):
            return 2
        mcp = drv.Mcp(
            [drv.PY, "-m", "scouter.mcp.server"], env(), drv.RUN / "mcp-server.stderr.log"
        )
        mcp.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "adjust-check", "version": "1"},
            },
            30,
        )
        mcp._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        def call(label: str, tool: str, args: dict, timeout: float = 90) -> dict:
            t0 = time.monotonic()
            out = mcp.call(tool, args, timeout)
            secs = time.monotonic() - t0
            (drv.RUN / "steps").mkdir(exist_ok=True)
            (drv.RUN / "steps" / f"{len(rows):02d}-{tool}.json").write_text(
                json.dumps(out, indent=2, default=str)
            )
            print(f"{label}: ok={out.get('ok')} outcome={out.get('outcome')} {secs:.1f}s")
            return out

        def snapshot(label: str) -> None:
            out = call(
                f"history_read ({label})", "history_read", {"image": str(frame), "source": "xmp"}
            )
            exp, end, n = exposure_and_length(out)
            rows.append((label, exp, end, f"{n} entries"))

        call("darkroom_open", "darkroom_open", {"image": str(frame)})
        # Row 431: the bench's session setup call, on a frame with nothing
        # pending. Must answer (transport limit 30s, like the bench) within
        # 12s, as a no-op -- never hang, never commit_timeout.
        t0 = time.monotonic()
        try:
            nc = call(
                "history_commit (unedited frame, timeout_s=10)",
                "history_commit",
                {"image": str(frame), "timeout_s": 10},
                timeout=30,
            )
        except Exception as e:  # the bench saw TimeoutError from the transport
            nc = {"ok": False, "outcome": f"transport:{type(e).__name__}", "error": str(e)}
            print(f"  transport error: {e}")
        noop_secs = time.monotonic() - t0
        nd = nc.get("data") or {}
        print(
            f"  noop commit: {noop_secs:.1f}s noop={nd.get('noop')} "
            f"committed={nd.get('committed')} changed={nd.get('changed')} "
            f"strategy={nd.get('strategy_used')}"
        )
        if not nc.get("ok"):
            print("  error:", json.dumps(nc.get("error"), default=str)[:600])
        noop_ok = (
            bool(nc.get("ok"))
            and noop_secs < 12.0
            and nd.get("noop") is True
            and nd.get("committed") is False
        )
        snapshot("before")
        say = call(
            "darkroom_say 'brighter by 1 ev'", "darkroom_say", {"phrase": "brighter by 1 ev"}
        )
        for mv in (say.get("data") or {}).get("moves") or []:
            print(
                f"  move {mv.get('module')}.{mv.get('param')}: previous={mv.get('previous')} "
                f"applied={mv.get('applied')} target={mv.get('target')} "
                f"committed={mv.get('committed')} source={mv.get('source')}"
            )
        if not say.get("ok"):
            print("  error:", json.dumps(say.get("error"))[:600])
        snapshot("after say")
        en = call("darkroom_enable denoiseprofile", "darkroom_enable", {"module": "denoiseprofile"})
        ed = en.get("data") or {}
        print(f"  enable: committed={ed.get('committed')} enabled={ed.get('enabled')}")
        if not en.get("ok"):
            print("  error:", json.dumps(en.get("error"))[:600])
        snapshot("after enable")
    finally:
        if mcp is not None:
            mcp.close()
        dt.quit("quit darktable")
        if os.environ.get("KEEP_FIX_PLUGIN") != "1" and backup is not None:
            shutil.copy2(backup, plugin)
            print(f"restored the previous plugin to {plugin} (KEEP_FIX_PLUGIN=1 keeps the fix)")
    print()
    print(f"{'point':<14} {'exposure EV':>12} {'history_end':>12}  entries")
    for label, exp, end, n in rows:
        e = "-" if exp is None else f"{exp:.3f}"
        print(f"{label:<14} {e:>12} {str(end):>12}  {n}")
    if len(rows) == 3:
        b, s, e = rows
        ok_say = s[1] is not None and b[1] is not None and abs(s[1] - b[1] - 1.0) < 0.05
        ok_len = (s[2] or 0) > (b[2] or 0) and (e[2] or 0) > (s[2] or 0)
        print(f"\nsay moved exposure by +1 EV on disk: {ok_say}; history grew twice: {ok_len}")
        print(f"no-op commit answered within 12s as noop: {noop_ok}")
        print("PASS" if ok_say and ok_len and noop_ok else "FAIL")
    else:
        print(f"\nno-op commit answered within 12s as noop: {noop_ok}")
        print("FAIL")
    print(f"run dir: {drv.RUN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
