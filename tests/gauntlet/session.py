"""One Scouter gauntlet session (spec 2.3): one darktable process, one fresh
--library, one body, one long-lived MCP stdio server with isolated
SCOUTER_CONFIG / SCOUTER_CACHE / OBSIDIAN_VAULT_PATH. At most 50 cases or 20
minutes (the engine rotates sessions). Only one darktable runs at a time,
and never one this runner did not launch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from gauntlet.adapter import CallRecord, RefuseRun
from scouter.core.dtprocess import LaunchError
from tests.gauntlet.corpus import Corpus, copy_readonly
from tests.gauntlet.jobslug import job_slug_for

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable
LAUNCH_READY_S = 30.0
EXPORT_PRESETS = ("client-proof", "preview")
#: Night-1 triage rank 9 (SCA-no-lock-breaker): darktable's own "database
#: lock file contains a pid that seems to be alive" and sqlite's "database
#: is locked" are the two ways an orphaned darktable (one this runner no
#: longer owns but never quit) makes every later launch against the SAME
#: session's library fail identically. Left undetected, this cost 22 cases
#: at ~40 s each on 2026-09-26 (each burning a full two-attempt launch
#: retry before SessionBlocked).
#: darktable's real wording is "the database lock file contains a pid that
#: seems to be alive in your system: 69648" -- the gap between "pid" and
#: the digits is far more than 10 non-digit characters, so a naive
#: `\bpid\D{0,10}(\d+)` never matches it.
_LOCK_RE = re.compile(r"database lock file contains a pid|database is locked", re.I)
_LOCK_PID_RE = re.compile(r"alive in your system:\s*(\d+)", re.I)


class SessionBlocked(RuntimeError):
    """darktable could not be brought to gui_ready (twice)."""


class LibraryLocked(SessionBlocked):
    """(night-1 triage rank 9): the session's own darktable.log shows the
    library db is wedged -- a launch that will fail identically every time
    it is retried within this session. Fatal fast, instead of letting the
    ordinary two-attempt launch retry (and every later case in the session)
    burn its own ~40 s finding the same thing out."""


def _log_size(log_path: Path) -> int:
    """The log's current byte size, or 0 if it does not exist yet -- the
    offset to scan FROM after the next launch, so an old lock line already
    on disk (a previous case, or a previous session's leftover) is never
    re-attributed to a launch that never repeated it."""
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def _check_library_lock(log_path: Path, since: int = 0) -> int | None:
    """The offending pid parsed from the bytes of `log_path` written AT OR
    AFTER `since` (or -1 if the log names the condition but no pid is found
    in it), or None when no lock line is present in that range.

    `dtprocess.launch` opens darktable.log in append mode, so the file
    accumulates across every launch in a session. Scanning the WHOLE file
    would mean one real lock line, once written, marks every LATER launch
    in the session as locked too, including a clean one that reached
    gui_ready -- so only the bytes this specific launch appended are ever
    in scope."""
    if not log_path.exists():
        return None
    with log_path.open("rb") as f:
        f.seek(since)
        text = f.read().decode("utf-8", errors="replace")
    if not _LOCK_RE.search(text):
        return None
    m = _LOCK_PID_RE.search(text)
    return int(m.group(1)) if m else -1


class ForeignDarktable(RefuseRun):
    """A darktable this runner did not launch is running: refuse to start.
    (final fix I2): a RefuseRun, so the engine stops the run and writes no
    case result instead of blocking every case."""


@dataclass
class Staged:
    root: Path
    frames: dict[str, Path]
    job_frames: list[Path]
    card: Path
    config: Path
    cache: Path
    vault: Path
    library: Path
    env: dict[str, str]
    vars: dict[str, object] = field(default_factory=dict)


def isolated_env(root: Path, *, job: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "SCOUTER_JOB", "OBSIDIAN_VAULT_PATH",
              "SCOUTER_DARKTABLE_LIBRARY_DB"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO)
    # Pin the library: without it history/backup fall back to a running darktable's
    # --library, then ~/.config/darktable/library.db (the user's real library).
    env["SCOUTER_DARKTABLE_LIBRARY_DB"] = str(root / "library" / "library.db")
    env["SCOUTER_CONFIG"] = str(root / "config" / "config.json")
    env["SCOUTER_CACHE"] = str(root / "cache")
    env["OBSIDIAN_VAULT_PATH"] = str(root / "vault")
    if job:
        env["SCOUTER_JOB"] = job
    return env


def reset_journal(staged: Staged) -> int:
    """`SCOUTER_JOB` is pinned once per MCP server process for the whole session (`isolated_env` above / phase 3 task 9's
    `scouter/mcp/server.py::_pin_session_job`), so by default every case in
    a session shares one job's undo journal. A case whose own write was
    refused (nothing landed) must see an EMPTY journal so
    `darkroom_undo_last` honestly answers `noop` (`scouter/core/darkroom.py`'s
    "journal is empty" branch) -- not an earlier case's still-open entry
    (e.g. a `darkroom_reset`, which `undo_last` then refuses as
    `unsupported_undo`, or a plain param write it silently reverts,
    changing THIS case's own sidecar and scoring `unexpected_edit`/
    `half-applied`).

    The CALLER must run this AFTER `execute.isolate()` (and any re-stage it
    triggers), not before: `isolate()` itself calls `darkroom_reset` for
    every module the PREVIOUS case touched, and that reset writes its own
    `module.reset` journal entry -- resetting the journal before `isolate()`
    runs would leave that entry in place for THIS case's own
    `darkroom_undo_last` to wrongly find (an `unsupported_undo` that has
    nothing to do with anything this case itself attempted).

    Truncating the job's journal file right before each case's own steps
    gives every case its own effectively-fresh journal without paying for a
    new darktable/MCP server session (spec 2.3's up-to-50-cases-per-session
    budget). Pure file I/O against the session's own isolated
    `SCOUTER_CACHE` -- never touches darktable, and never races a call in
    flight (the harness is single-threaded and strictly sequential between
    cases). Returns the number of entries found (and discarded) -- callers
    keep it in `obs.extra` so a leak (from `isolate()`'s own resets, or
    anything else) stays visible rather than silently disappearing; a
    nonzero count is not itself a bug (a module reset between cases is
    routine), only a fact worth keeping.

    Agent-mode cases (`mode="agent"`, no `case.steps`, driven by
    `tests/gauntlet/agent.py`, never `execute.run_tool_case`) get no
    reset -- none of the ids this fixed (the 6 undo, 4 P4 warmer/cooler, 6
    mixed P2) are agent cases, and agent mode has no per-case journal
    dependency of its own to isolate."""
    job = staged.env.get("SCOUTER_JOB")
    if not job:
        return 0
    path = staged.cache / "journal" / f"{job}.jsonl"
    if not path.exists():
        return 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if lines:
        path.write_text("", encoding="utf-8")
    return len(lines)


def write_config(root: Path) -> Path:
    """The run-level config, stage_body's pattern: library root and backup under the stamp."""
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    passfile = cfg_dir / "backup.pass"
    if not passfile.exists():
        passfile.write_text(secrets.token_hex(24) + "\n")
    passfile.chmod(0o600)
    for d in ("jobs", "backup", "cache", "vault"):
        (root / d).mkdir(parents=True, exist_ok=True)
    cfg = {"version": 1, "library_root": str(root / "jobs"),
           "cameras": {"E-M10 Mark III": "em10iii", "ILCE-7": "a7", "NEX-5N": "nex5n"},
           "educative": "brief",
           "backup": {"enabled": True, "target": str(root / "backup"), "encryption": "openssl",
                      "openssl_passfile": str(passfile)},
           "vault": {"jobs_dir": "Projects/Photo jobs"}}
    path = cfg_dir / "config.json"
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    wb_src = Path(os.environ.get("HTC_WB_TABLES", str(Path.home() / ".config/scouter/wb")))
    if wb_src.is_dir():  # read-only copy of the measured WB tables; never written back
        shutil.copytree(wb_src, cfg_dir / "wb", dirs_exist_ok=True)
    return path


def _neutral_role_dir(root: Path, role: str) -> Path:
    """Several corpus roles literally spell the diagnosis (`warm_cast`, `cool_cast`, `blown`,
    `flat`, `noisy`, `dark`, `bright`) -- r0037's agent read its own
    tool-result path and used the folder name as evidence. A symlink alias
    (the earlier fix, bb4f13f) only hid the leak from the PROMPT: the
    library still holds the real path (darktable's Lua matches paths by
    exact string, `_image_full_path(image) == path`, no realpath) and every
    MCP envelope (`darkroom_open`, `scout_preview`, `history_read`, ...)
    still echoed the real, role-named `path` field back. Staging every
    role's frame under a hashed, role-blind directory HERE -- the one path
    the library, the envelopes and the prompt all ever see -- removes the
    leak at its only source instead of hiding it after the fact."""
    return root / "frames" / ("f" + hashlib.sha1(role.encode()).hexdigest()[:6])


def stage(corpus: Corpus, body: str, root: Path, *, job_size: int = 7,
          chart: Path | None = None) -> Staged:
    """Fresh 0644 copies of every role frame and a small card for this session.
    `chart` (the run's synthetic chart) is copied in as role `chart`. Every
    role's frame is staged under a hashed, role-blind directory
    (`_neutral_role_dir`) -- the disk path, and so every MCP envelope and
    the agent's own prompt, never spells the role/diagnosis name."""
    root.mkdir(parents=True, exist_ok=True)
    frames: dict[str, Path] = {}
    if chart is not None and chart.exists():
        frames["chart"] = copy_readonly(chart, _neutral_role_dir(root, "chart") / chart.name)
    for role, src in corpus.roles(body).items():
        dst = _neutral_role_dir(root, role) / src.name
        if not dst.exists():
            copy_readonly(src, dst)
        frames[role] = dst
    card = root / "card" / "DCIM" / "100GAUNT"
    job_frames = []
    for i, src in enumerate(corpus.job_sources(body, job_size)):
        dst = card / f"DUP{i:04d}_{src.name}"
        if not dst.exists():
            copy_readonly(src, dst)
        job_frames.append(dst)
    config = write_config(root)
    stamp = root.name
    env = isolated_env(root, job=f"htc-{stamp}")
    return Staged(root=root, frames=frames, job_frames=job_frames, card=root / "card",
                  config=config, cache=root / "cache", vault=root / "vault",
                  library=root / "library" / "library.db", env=env,
                  vars={"job_slug": job_slug_for(f"{body}-{stamp}"), "card": str(root / "card"),
                        "out_dir": str(root / "exports"), "export_preset": EXPORT_PRESETS[0],
                        "export_preset_2": EXPORT_PRESETS[1], "preset": f"htc-{stamp}",
                        "style": f"htc-style-{stamp}", "config_path": str(config),
                        "relocate_root": str(root / "relocated")})


class DtControl:
    """The seam over scouter.core.dtprocess + dtbridge (a fake in unit tests).

    `cache_root` (fix round 2 review N2): bound by `ScouterSession.open`
    (before anything else runs, to `self.root / "cache"` -- known from
    construction, never waits on `staged`) to THIS session's own cache.
    Every ownership check below then reads/writes through it, never through
    this process's ambient `os.environ["SCOUTER_CACHE"]`, which another
    session's late close/abort or a concurrent `point_cache` can drift out
    from under this thread mid-session (N2's production shape, pid 69648)."""

    def __init__(self, cache_root: Path | None = None) -> None:
        self.cache_root = cache_root

    def foreign(self) -> list[int]:
        from scouter.core import dtprocess

        return dtprocess.foreign_pids(self.cache_root)

    def owned(self) -> int | None:
        from scouter.core import dtprocess

        return dtprocess.owned_pid(self.cache_root)

    def launch(self, frame: Path, library: Path, log: Path, env: dict[str, str] | None = None):
        from scouter.core import dtprocess

        return dtprocess.launch(frame, library, log_path=log, env=env)

    def ready(self, timeout_s: float, *, stop_if=None) -> dict:
        from scouter.core import dtprocess

        return dtprocess.wait_gui_ready(timeout_s, stop_if=stop_if)

    def health(self) -> str:
        from scouter.core import dtbridge

        return dtbridge.health()["state"]

    def terminate(self, *, responsive: bool | None = None) -> dict:
        from scouter.core import dtprocess

        return dtprocess.terminate(responsive=responsive, cache_root=self.cache_root)

    def kill_launched(self, rec) -> bool:
        """(pre-night-1 N1): SIGKILL the exact darktable `rec` recorded at
        launch (pid + start time), whatever SCOUTER_CACHE says now. True when
        a signal was sent."""
        import signal

        from scouter.core import dtprocess

        if rec is None or dtprocess._lstart(rec.pid) != rec.start_time:
            return False
        try:
            os.kill(rec.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return False
        return True


class ScouterSession:
    """Implements gauntlet.adapter.Session for tool-mode layers."""

    def __init__(self, corpus: Corpus, root: Path, body: str, *, dt: DtControl | None = None,
                 mcp_factory=None, clock=time.monotonic, run_root: Path | None = None,
                 floors_fn=None) -> None:
        self.corpus = corpus
        self.root = root
        self.run_root = run_root or root
        self.previews_dir = self.run_root / "previews"
        self.floors_fn = floors_fn
        self.body = body
        self.dt = dt or DtControl()
        self.mcp_factory = mcp_factory or self._real_mcp
        self.clock = clock
        self.staged: Staged | None = None
        self.mcp = None
        self.current_frame: Path | None = None
        self.touched: set[str] = set()
        self.baselines: dict[str, dict] = {}
        #: Fix A (night-1 triage rank 6, GD-walkthrough-state): set once
        #: execute.run_tool_case has run the FIRST case of the user-guide
        #: walkthrough (case.meta["session"] == "walkthrough") in this
        #: session, so every later walkthrough case skips isolate() and
        #: reset_journal() -- the walkthrough is one scripted session
        #: sharing darktable state and the undo journal (gd-04's undo must
        #: see gd-02/03's history, not a reset frame). Never set for any
        #: other meta["session"] value (edu-* cases stay isolated per case).
        self.walkthrough_started: bool = False
        #: Review round 2, finding 6 (GB-1): set by adapter._stage_guide_job
        #: when the guide layer's own job import fails; execute.run_tool_case
        #: reads it (via getattr, finding 14) to block a case that needs the
        #: job instead of letting it fail on the product's own terms.
        self.guide_job_stage_error: str | None = None
        #: Review round 2, finding 9 (GA-1): the id of the first walkthrough
        #: case that broke the script's own intended state (blocked, or an
        #: unscripted write refusal). execute.run_tool_case reads and sets
        #: this via getattr for consistency with minimal session doubles.
        self.walkthrough_broken: str | None = None
        self.floors: dict[str, float] = {}
        #: (final fix I3): "mcp_server_death: exit N" notes not yet handed to
        #: an observation; mcp_dead marks the session for reopen.
        self.mcp_deaths: list[str] = []
        self.mcp_dead = False
        #: (pre-night-1 N1): what this session launched, so an abort from the
        #: runner thread kills exactly that, even mid-open.
        self.aborted = False
        self.launched = None  # dtprocess.Launched of our darktable
        self._mcp_pending = None  # the MCP client while it initialises
        #: (row 501 item I): set by stage_job(); a failed import's reason,
        #: read-only for callers that only run a case needing a real job
        #: when this is still None.
        self.job_stage_error: str | None = None

    # -- lifecycle -------------------------------------------------------

    def _real_mcp(self, env: dict[str, str], cwd: Path):
        from tests.gauntlet.benchmark.run_editor_benchmark import McpClient

        client = McpClient([PY, "-m", "scouter.mcp.server"], env, cwd, cwd / "mcp.stderr")
        self._mcp_pending = client  # (N1): killable while initialize() hangs
        client.initialize()
        return client

    def open(self) -> ScouterSession:
        #: (fix round 2 review N2): bind ownership checks to THIS session's
        #: own cache before anything else -- `self.root` is known from
        #: construction, never waits on `self.staged`. `foreign()`/`owned()`
        #: below must never read the ambient `os.environ["SCOUTER_CACHE"]`,
        #: which a concurrent session's late close/abort can drift out from
        #: under this thread mid-open. Only touches a real `DtControl`; a
        #: test double without `cache_root` is left alone.
        if hasattr(self.dt, "cache_root"):
            self.dt.cache_root = self.root / "cache"
        foreign = self.dt.foreign()
        if foreign:
            raise ForeignDarktable(f"darktable pids {foreign} were not launched by the "
                                   "gauntlet; quit them first (the runner never signals them)")
        if self.dt.owned() is not None:  # left over from a killed run: ours, so quit it
            self._quit("leftover darktable from a killed run")
        chart = self.run_root / "chart" / "chart-v1.pfm"
        self.staged = stage(self.corpus, self.body, self.root, chart=chart)
        self._launch(self.staged.frames["hero"])
        try:
            self.mcp = self.mcp_factory(self.staged.env, self.root)
            self._mcp_pending = None
            self._check_aborted()
            if self.floors_fn is not None:
                self.floors, deterministic = self.floors_fn(self.staged.frames["hero"])
                if not deterministic:
                    raise SessionBlocked("harness_bug: the untouched frame renders differently "
                                         "twice (noise floor > 2x minimum, spec 5.2)")
        except BaseException:  # never leave our darktable or MCP child running
            self.abort()
            raise
        return self

    def _check_aborted(self) -> None:
        if self.aborted:
            raise SessionBlocked("session aborted by the runner (open cap)")

    def _quit(self, why: str) -> None:
        """Quit the owned darktable; refuse to go on (and so to launch a second
        one) unless the quit ladder reports ok."""
        res = self.dt.terminate(responsive=False)
        if not (res or {}).get("ok"):
            raise SessionBlocked(f"{why}: darktable did not quit ({res}); "
                                 "refusing to launch another")

    def relative(self, path: Path) -> str:
        return str(Path(path).relative_to(self.run_root))

    def _raise_library_locked(self, pid: int) -> None:
        """Quit -- or, mid-session, abort -- whatever THIS launch started
        before raising, so a locked library never leaves an owned darktable
        orphaned behind a fatal case."""
        try:
            self.abort()
        except Exception:  # noqa: BLE001 - best effort; the raise below is what matters
            pass
        raise LibraryLocked(f"library_lock {pid if pid >= 0 else 'unknown'}")

    def _launch(self, frame: Path) -> None:
        assert self.staged is not None
        log_path = self.root / "darktable.log"
        failures: list[str] = []
        for _attempt in (1, 2):
            self._check_aborted()
            #: The offset THIS launch's own bytes start at -- darktable.log
            #: is opened in append mode, so an old lock line already on disk
            #: from an earlier launch must never be re-attributed to a
            #: later, clean one.
            since = _log_size(log_path)
            try:
                #: (fix round 1 review H3c): `env=self.staged.env` so the
                #: owner record's cache root comes from THIS session, never
                #: whatever `os.environ["SCOUTER_CACHE"]` happens to be on
                #: this thread when a mid-session relaunch fires -- see
                #: `dtprocess.owner_path`'s docstring.
                rec = self.dt.launch(frame, self.staged.library, log_path, env=self.staged.env)
            except LaunchError as e:  # dtprocess already stopped the unowned process
                failures.append(f"LaunchError: {e}")
                continue
            self.launched = rec or self.launched
            if self.aborted:  # (N1): the abort landed during the launch
                self.abort()
                self._check_aborted()
            #: darktable's own lock-file error lands DURING the ready wait,
            #: not before it (the launched process has its own start time
            #: well before `dt_init`/database init writes anything) -- catch
            #: it at the ready poll's own cadence via `stop_if`, under about
            #: 6 s, instead of waiting out the full LAUNCH_READY_S (30 s).
            ready = self.dt.ready(
                LAUNCH_READY_S,
                stop_if=lambda: _check_library_lock(log_path, since=since) is not None)
            #: Defense in depth: a lock line landing in the narrow window
            #: between the last stop_if check and wait_gui_ready returning
            #: (or a caller that never wires stop_if) is still caught here.
            pid = _check_library_lock(log_path, since=since)
            if pid is not None:
                self._raise_library_locked(pid)
            if ready.get("ready"):
                self.current_frame = frame
                return
            failures.append("not gui_ready after 30 s")
            self._quit("launch: not gui_ready after 30 s")
        raise SessionBlocked("launch: darktable failed twice: " + "; ".join(failures))

    def relaunch(self, frame: Path | None = None) -> None:
        self._quit("relaunch")
        self._launch(frame or self.current_frame or self.staged.frames["hero"])  # type: ignore[union-attr]

    def reopen_mcp(self) -> None:
        """(night1-robustness-triage.md item 1): a hang can leave the MCP
        server's own queue backlogged with the abandoned call, so a fresh
        `relaunch()` of darktable alone is not enough -- the next case's
        first call would still queue behind it. Close the old client (best
        effort) and open a fresh one.

        (fix round 3 review N7): clears `mcp_dead` -- `fence_and_recover`
        (`tests/gauntlet/faults.py`) sets it right after its own deliberate
        SIGKILL of the OLD client, before this method ever runs, and left
        uncleared here it survives the fresh client this method just
        installed. `RoutedSession._session` reads `mcp_dead` to decide
        whether to reopen (`final fix I3`); left set, it would throw away
        this healthy, just-fenced session and open a brand new one -- one
        more relaunch and a full `open()` (staging, launch, noise-floor
        renders) for every fenced case, and one more chance for a
        `ForeignDarktable` refusal to stop the whole layer."""
        old, self.mcp = self.mcp, None
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001 - best effort; a fresh client follows anyway
                pass
        self.mcp = self.mcp_factory(self.staged.env, self.root)
        self.mcp_dead = False

    def purge_spool(self) -> None:
        """(fix round 1 review H2): delete every `request-*.json`/`*.json.tmp`
        the just-fenced (SIGKILLed) MCP server may have left in the bridge
        spool. The plugin's own stale-request sweep is 600s, far slower than
        a fenced recovery -- without this, a fresh darktable can still poll
        up a request file meant for the darktable that is gone (the r17
        ghost-edit shape: an abandoned call's request survives its own
        client and lands on whichever darktable reads it next). The spool
        location honours THIS session's own env (`SCOUTER_CACHE`/`HOME`),
        mirroring `darktable_mcp.bridge.client._cache_dir()`, never just the
        harness process's own ambient `os.environ` (same reasoning as
        `dtprocess.owner_path`'s `cache_root` override)."""
        env = self.staged.env
        base = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME", str(Path.home()))) /
                                                 ".cache")
        spool = Path(base) / "darktable-mcp"
        for pattern in ("request-*.json", "*.json.tmp"):
            for f in spool.glob(pattern):
                f.unlink(missing_ok=True)

    def refresh_frame(self, role: str) -> Path:
        """Contaminated: re-stage this role from a fresh copy and relaunch on it.

        (night-1 fix, class LK-refresh-no-open): `relaunch`/`_launch` marks
        `dst` `current_frame` the moment darktable is gui_ready with it, but
        darktable displaying a frame is not the JOB recording it as opened --
        only a successful `darkroom_open` does that (scouter.core.darkroom.
        `_record_opened_image`). Left as `_launch` sets it, the very next
        `ensure_frame(role)` saw `current_frame == staged.frames[role]` and
        skipped the open call, so the job kept the OLD image's id while
        darktable showed the new one: every write then refused
        "darkroom.current reports N+1, job opened N". State machine: launched
        (this method) -> opened (the next real `darkroom_open`) -> current
        (`ensure_frame` sets it back, only on that call's success). Never
        launched -> current."""
        assert self.staged is not None
        src = self.corpus.roles(self.body)[role] if role != "chart" else \
            self.run_root / "chart" / "chart-v1.pfm"
        n = 1
        while (self.root / "frames" / f"{role}-fresh{n}").exists():
            n += 1  # never reuse a dir: its raw and .xmp sidecar may be contaminated
        dst = copy_readonly(src, self.root / "frames" / f"{role}-fresh{n}" / src.name)
        self.baselines.pop(str(self.staged.frames[role]), None)
        self.staged.frames[role] = dst
        self.relaunch(dst)
        self.current_frame = None  # not current until ensure_frame opens it for real
        return dst

    def abort(self) -> dict:
        """Kill our darktable and MCP child. (pre-night-1 N1): a repeated
        abort (the late open thread after the runner's) never re-reads the
        owner record -- SCOUTER_CACHE may name the next session's by then --
        and only kills the pids this session recorded. Returns a typed result."""
        again, self.aborted = self.aborted, True
        res = {"ok": True, "how": "repeat_abort"} if again else \
            (self.dt.terminate(responsive=False) or {})
        killed = []
        kill = getattr(self.dt, "kill_launched", None)
        if self.launched is not None and kill is not None and kill(self.launched):
            killed.append(getattr(self.launched, "pid", self.launched))
        for client in (self.mcp, self._mcp_pending):
            proc = getattr(client, "proc", None)
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    killed.append(proc.pid)
            except Exception:  # noqa: BLE001
                pass
        self.mcp = self._mcp_pending = None
        return {"ok": bool(res.get("ok")) or bool(killed), "darktable": res, "killed": killed}

    def close(self) -> None:
        # (final fix M2): darktable is quit even when the MCP close raises.
        try:
            if self.mcp is not None:
                mcp, self.mcp = self.mcp, None
                mcp.close()
        finally:
            self.dt.terminate()

    # -- calls -----------------------------------------------------------

    def call(self, tool: str, args: dict, budget_s: float, *, write: bool = False) -> CallRecord:
        """One MCP call with the hard cap budget + 30 s and the health ladder
        on a timeout (spec 2.3)."""
        rec = CallRecord(tool=tool, args=args, write=write)
        t0 = self.clock()
        try:
            env = self.mcp.call_tool(tool, args, budget_s + 30.0)
            rec.envelope = env
            rec.ms = env.get("ms") if isinstance(env, dict) else None
            rec.bounced = bool(((env or {}).get("data") or {}).get("plan"))
            if env and not env.get("ok"):
                rec.error = ((env.get("error") or {}).get("code")) or env.get("outcome")
        except TimeoutError:
            rec.error = "timeout"
            rec.health = self.dt.health()
        except Exception as e:  # noqa: BLE001 - transport failure is a result
            rec.error = f"transport: {type(e).__name__}: {e}"[:300]
        if rec.error in ("timeout",) or (rec.error or "").startswith("transport:"):
            self._check_mcp_alive()
        rec.wall_s = round(self.clock() - t0, 3)
        if write and tool.startswith("darkroom_") and "module" in args:
            self.touched.add(str(args["module"]))
        if write and tool in ("darkroom_say", "preset_apply", "darkroom_undo_last"):
            self.touched.add("*")
        return rec

    def _check_mcp_alive(self) -> None:
        """(final fix I3): after a transport error or a timeout, an exited MCP
        server is a crash (release bar 1), recorded once per session."""
        proc = getattr(self.mcp, "proc", None)
        if proc is None or self.mcp_dead:
            return
        try:
            rc = proc.poll()
        except Exception:  # noqa: BLE001 - cannot tell: not a recorded death
            return
        if rc is not None:
            self.mcp_dead = True
            self.mcp_deaths.append(f"mcp_server_death: exit {rc}")

    def take_mcp_deaths(self) -> list[str]:
        out, self.mcp_deaths = self.mcp_deaths, []
        return out

    def _relaunch_then_reopen(self, role: str, path: Path) -> None:
        """(night-1 fix, class LK-refresh-no-open, sibling gap): relaunch is
        `launched`, never `current` -- the job only records an image as
        opened inside a real `darkroom_open` (scouter.core.darkroom.
        `_record_opened_image`). Used by every ensure_frame recovery path
        that relaunches, so none of them can skip the follow-up open the way
        the original bug did."""
        self.relaunch(path)
        again = self.call("darkroom_open", {"image": str(path)}, 30.0)
        if again.error is None and (again.envelope or {}).get("ok"):
            self.current_frame = path
        else:
            raise SessionBlocked(
                f"darkroom_open did not open frame {role} ({path}) after a relaunch "
                f"on it: error={again.error!r} health={again.health!r}")

    def ensure_frame(self, role: str) -> Path:
        assert self.staged is not None
        path = self.staged.frames[role]
        if self.current_frame != path:
            rec = self.call("darkroom_open", {"image": str(path)}, 30.0)
            if rec.error is None and (rec.envelope or {}).get("ok"):
                self.current_frame = path
            elif rec.error == "timeout" and rec.health == "unresponsive":
                self._relaunch_then_reopen(role, path)
            elif rec.error == "not_in_darkroom":
                # Smoke S2: darktable cannot load a frame imported into the
                # SAME running session (product honest limit R20, "wrong image
                # loaded"). Relaunch on the frame, then open it once more so the
                # job records it as its opened image.
                self._relaunch_then_reopen(role, path)
            else:
                raise SessionBlocked(f"darkroom_open did not open frame {role} ({path}): "
                                     f"error={rec.error!r} health={rec.health!r}")
        return path

    def stage_job(self) -> None:
        """(row 501 item I, Pratik's ruling: the client proof gallery, r0066,
        is in scope): mirrors fix/n1-guide-install-harness's own
        `_stage_guide_job` (adapter.py) -- a real `library_import` of the
        session's staged card into `staged.vars["job_slug"]`, so a case
        whose product tool (`library_jobs`, `export_release`) genuinely
        needs a staged job to find sees a real one, not the empty library
        `stage()` alone leaves behind (night-1 triage rank 11: r0066's agent
        checked `library_jobs`, found it empty, and had nothing to point
        `export_release` at). Idempotent (a repeat call just re-imports into
        the same slug, harmless); failure is recorded on
        `self.job_stage_error`, never raised -- callers that only need this
        for one case must not fail every other case in the session over it.

        Shared-helper opportunity for the integrator: this is the same
        shape as `_stage_guide_job`; a future pass could fold both into one
        helper `session.py` and `adapter.py` share, rather than each
        re-implementing the same `library_import` call. Not done here --
        this branch does not depend on fix/n1-guide-install-harness."""
        self.job_stage_error = None
        if self.staged is None or not self.staged.job_frames:
            return
        rec = self.call("library_import", {"source": str(self.staged.card),
                                           "job_slug": self.staged.vars["job_slug"],
                                           "confirm": True}, 60.0, write=True)
        if rec.error:
            self.job_stage_error = rec.error
