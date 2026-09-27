"""darktable process control, owned by Scouter (spec 6, watchdog design 3).

One place for launch, readiness, ownership and the quit ladder, so the
benchmark, the acceptance driver, the gauntlet and the watchdog share it.

Ownership rule: Scouter only ever signals a darktable it launched. The
owner file `cache_root/darktable.owner.json` records {pid, start_time, exe,
library, configdir, frame}; `owned_pid()` checks the pid is alive, its `ps -o
lstart` equals the recorded start_time (pid reuse), and its executable matches.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from scouter import paths

DARKTABLE = os.environ.get("SCOUTER_DARKTABLE",
                           "/Applications/darktable.app/Contents/MacOS/darktable")
OWNER_FILE = "darktable.owner.json"

#: Children this process launched, so `_alive` can reap them: a discarded
#: Popen leaves an exited child as a zombie that `kill(pid, 0)` still sees.
_children: dict[int, subprocess.Popen] = {}


class LaunchError(RuntimeError):
    """darktable started but could not be recorded as owned (it was stopped)."""


@dataclass(frozen=True)
class Launched:
    pid: int
    start_time: str
    exe: str
    library: str
    configdir: str | None
    frame: str | None


def owner_path(cache_root: Path | None = None) -> Path:
    """(fix round 1 review H3c): `cache_root`, when given, is used instead of
    `paths.cache_root()` (which reads `$SCOUTER_CACHE` from THIS process's
    own ambient `os.environ`). A relaunch mid-session can run on a thread/
    moment where the harness's own `os.environ["SCOUTER_CACHE"]` has drifted
    from the session's (an earlier session's late abort re-points it --
    `tests/gauntlet/adapter.py`'s own `point_cache` comment); the owner
    record then lands in a cache root other than the one the MCP SERVER
    subprocess reads, so its `owned_pid()` never matches the just-launched
    pid (the plausible cause of rob-r11/r12/r13/r15 falling through to the
    generic `bridge_timeout` instead of `darktable_unresponsive` -- H3
    review). `launch` passes this explicitly from its own `env` argument
    when given, closing the race outright."""
    return (cache_root or paths.cache_root()) / OWNER_FILE


def _lstart(pid: int) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def _comm(pid: int) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def _alive(pid: int) -> bool:
    child = _children.get(pid)
    if child is not None:
        return child.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pids() -> list[int]:
    """Every running darktable GUI process (not darktable-cli).

    Raises `subprocess.TimeoutExpired` / `OSError` when pgrep cannot answer:
    callers must treat that as "unknown", never as "none running"."""
    out = subprocess.run(["pgrep", "-f", "MacOS/darktable( |$)"], capture_output=True, text=True,
                         timeout=5)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def read_owner() -> Launched | None:
    try:
        return Launched(**json.loads(owner_path().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def owned_pid() -> int | None:
    """The pid of the darktable Scouter launched, if it is still that process."""
    rec = read_owner()
    if rec is None or not _alive(rec.pid):
        return None
    if _lstart(rec.pid) != rec.start_time:
        return None
    comm = _comm(rec.pid) or ""
    if not comm or Path(comm).name != Path(rec.exe).name:
        return None
    return rec.pid


def foreign_pids() -> list[int]:
    mine = owned_pid()
    return [p for p in running_pids() if p != mine]


def launch(frame: str | Path | None, library: str | Path, configdir: str | Path | None = None,
           *, log_path: str | Path | None = None, exe: str = DARKTABLE,
           env: dict[str, str] | None = None) -> Launched:
    argv = [exe, "--library", str(library)]
    if configdir:
        argv += ["--configdir", str(configdir)]
    if frame:
        argv.append(str(frame))
    Path(library).parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True, env=env)
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    _children[proc.pid] = proc
    start = None
    for _ in range(50):
        start = _lstart(proc.pid)
        if start:
            break
        time.sleep(0.1)
    if not start:
        # Without a start time owned_pid() can never match, so this darktable
        # could never be quit. It is ours (we hold its Popen): stop it now.
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        else:
            _children.pop(proc.pid, None)
        raise LaunchError(f"could not read the start time of darktable pid {proc.pid}; "
                          "stopped it rather than leave an unowned process")
    rec = Launched(proc.pid, start, exe, str(library),
                   str(configdir) if configdir else None, str(frame) if frame else None)
    #: (fix round 1 review H3c): the owner record's cache root comes from
    #: THIS launch's own `env`, when given -- never just the ambient
    #: `os.environ` `owner_path()` would otherwise fall back to. See
    #: `owner_path`'s docstring.
    cache_root = Path(env["SCOUTER_CACHE"]) if env and env.get("SCOUTER_CACHE") else None
    op = owner_path(cache_root)
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(asdict(rec)) + "\n", encoding="utf-8")
    return rec


def wait_gui_ready(timeout_s: float = 30.0, *, poll_s: float = 0.5, call=None,
                   clock=time.monotonic, sleep=time.sleep) -> dict:
    """Poll `darktable_mcp.diag` until `gui_ready` is true (row 434). Never
    treats `view.current` as readiness (stall write-up 5)."""
    if call is None:
        from scouter.core import dtbridge

        call = dtbridge.call
    deadline = clock() + timeout_s
    last: dict = {"gui_ready": False, "error": "never answered"}
    while clock() < deadline:
        try:
            last = call("darktable_mcp.diag", {}, timeout=5.0) or {}
        except Exception as e:  # noqa: BLE001 - not up yet
            last = {"gui_ready": False, "error": str(e)[:200]}
        if last.get("gui_ready") is True:
            return {"ready": True, **last}
        sleep(poll_s)
    return {"ready": False, **last}


def terminate(pid: int | None = None, *, responsive: bool | None = None,
              kill=os.kill, alive=_alive, sleep=time.sleep) -> dict:
    """The quit ladder on an OWNED pid: osascript quit (skipped when the GUI is
    known not to answer), SIGTERM 10 s, SIGKILL. Refuses a pid Scouter did not
    launch with `refused_not_ours` and sends no signal."""
    mine = owned_pid()
    target = pid if pid is not None else mine
    if target is None:
        return {"ok": True, "how": "not running"}
    if target != mine:
        return {"ok": False, "how": "refused_not_ours", "pid": target}
    steps = []
    # osascript addresses darktable by application name, not pid, so with a
    # foreign darktable also running it could quit the user's session. Skip
    # it then and go straight to signals, which only ever reach `target`.
    # A pgrep that cannot answer is "unknown" and fails closed.
    if responsive is not False:
        try:
            foreign = foreign_pids()
        except (OSError, subprocess.TimeoutExpired):
            steps.append("osascript_skipped_unknown")
            responsive = False
        else:
            if foreign:
                steps.append("osascript_skipped_foreign")
                responsive = False
    if responsive is not False:
        try:
            subprocess.run(["osascript", "-e", 'tell application "darktable" to quit'],
                           capture_output=True, timeout=20, check=False)
            steps.append("osascript")
        except (OSError, subprocess.TimeoutExpired):
            steps.append("osascript_timeout")
        for _ in range(40):
            if not alive(target):
                return {"ok": True, "how": "osascript", "pid": target}
            sleep(0.5)
    for sig, wait_s in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        # Re-check ownership before every signal: after the waits above the
        # pid may have exited and been reused by another process.
        if owned_pid() != target:
            return {"ok": not alive(target), "how": "gone_or_reused", "pid": target,
                    "steps": steps}
        try:
            kill(target, sig)
        except ProcessLookupError:
            return {"ok": True, "how": steps[-1] if steps else "gone", "pid": target}
        steps.append(sig.name)
        waited = 0.0
        while waited < wait_s:
            if not alive(target):
                return {"ok": True, "how": sig.name, "pid": target, "steps": steps}
            sleep(0.25)
            waited += 0.25
    return {"ok": not alive(target), "how": steps[-1], "pid": target, "steps": steps}


def clear_stale_locks(library: str | Path) -> list[str]:
    """Remove library.db.lock / data.db.lock only when the owner pid is dead
    and the lock names that pid (watchdog design 5)."""
    rec = read_owner()
    if rec is None or _alive(rec.pid):
        return []
    removed = []
    for lock in (Path(str(library) + ".lock"), Path(library).with_name("data.db.lock")):
        try:
            text = lock.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if str(rec.pid) in text.split():
            lock.unlink()
            removed.append(str(lock))
    return removed
