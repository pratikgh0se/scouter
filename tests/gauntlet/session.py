"""One Scouter gauntlet session (spec 2.3): one darktable process, one fresh
--library, one body, one long-lived MCP stdio server with isolated
SCOUTER_CONFIG / SCOUTER_CACHE / OBSIDIAN_VAULT_PATH. At most 50 cases or 20
minutes (the engine rotates sessions). Only one darktable runs at a time,
and never one this runner did not launch."""

from __future__ import annotations

import json
import os
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


class SessionBlocked(RuntimeError):
    """darktable could not be brought to gui_ready (twice)."""


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


def stage(corpus: Corpus, body: str, root: Path, *, job_size: int = 7,
          chart: Path | None = None) -> Staged:
    """Fresh 0644 copies of every role frame and a small card for this session.
    `chart` (the run's synthetic chart) is copied in as role `chart`."""
    root.mkdir(parents=True, exist_ok=True)
    frames: dict[str, Path] = {}
    if chart is not None and chart.exists():
        frames["chart"] = copy_readonly(chart, root / "frames" / "chart" / chart.name)
    for role, src in corpus.roles(body).items():
        dst = root / "frames" / role / src.name
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
    """The seam over scouter.core.dtprocess + dtbridge (a fake in unit tests)."""

    def foreign(self) -> list[int]:
        from scouter.core import dtprocess

        return dtprocess.foreign_pids()

    def owned(self) -> int | None:
        from scouter.core import dtprocess

        return dtprocess.owned_pid()

    def launch(self, frame: Path, library: Path, log: Path):
        from scouter.core import dtprocess

        return dtprocess.launch(frame, library, log_path=log)

    def ready(self, timeout_s: float) -> dict:
        from scouter.core import dtprocess

        return dtprocess.wait_gui_ready(timeout_s)

    def health(self) -> str:
        from scouter.core import dtbridge

        return dtbridge.health()["state"]

    def terminate(self, *, responsive: bool | None = None) -> dict:
        from scouter.core import dtprocess

        return dtprocess.terminate(responsive=responsive)

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

    # -- lifecycle -------------------------------------------------------

    def _real_mcp(self, env: dict[str, str], cwd: Path):
        from tests.gauntlet.benchmark.run_editor_benchmark import McpClient

        client = McpClient([PY, "-m", "scouter.mcp.server"], env, cwd, cwd / "mcp.stderr")
        self._mcp_pending = client  # (N1): killable while initialize() hangs
        client.initialize()
        return client

    def open(self) -> ScouterSession:
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

    def _launch(self, frame: Path) -> None:
        assert self.staged is not None
        failures: list[str] = []
        for _attempt in (1, 2):
            self._check_aborted()
            try:
                rec = self.dt.launch(frame, self.staged.library, self.root / "darktable.log")
            except LaunchError as e:  # dtprocess already stopped the unowned process
                failures.append(f"LaunchError: {e}")
                continue
            self.launched = rec or self.launched
            if self.aborted:  # (N1): the abort landed during the launch
                self.abort()
                self._check_aborted()
            ready = self.dt.ready(LAUNCH_READY_S)
            if ready.get("ready"):
                self.current_frame = frame
                return
            failures.append("not gui_ready after 30 s")
            self._quit("launch: not gui_ready after 30 s")
        raise SessionBlocked("launch: darktable failed twice: " + "; ".join(failures))

    def relaunch(self, frame: Path | None = None) -> None:
        self._quit("relaunch")
        self._launch(frame or self.current_frame or self.staged.frames["hero"])  # type: ignore[union-attr]

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
