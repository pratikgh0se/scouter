"""Scouter's gauntlet adapter: the one object `python -m gauntlet` loads
(gauntlet.json -> tests.gauntlet.adapter:make). Everything Scouter-specific
the engine needs is here or imported from here."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from gauntlet.adapter import LayerSpec, RefuseRun
from gauntlet.case import Case
from tests.gauntlet import agent_budget
from tests.gauntlet import corpus as corpus_mod
from tests.gauntlet import taxonomy
from tests.gauntlet.checkmap import plan_request
from tests.gauntlet.layers import (
    bulk,
    coverage,
    guide,
    install,
    looks,
    offline,
    parity,
    robustness,
    scenarios,
)
from tests.gauntlet.layers.common import WRITE_TOOLS
from tests.gauntlet.p0 import select_p0
from tests.gauntlet.policy import POLICY

RUNS_ROOT = Path(os.environ.get("HTC_RUNS_ROOT", str(corpus_mod.SCRATCH))).expanduser()
CRASH_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"
#: Looks gates (carry 2): every sourced check (documented / community-consensus)
#: is gated >= 0.90 per component via the derived tag `sourced_component`
#: (one gate per component present); inferred (sign-only) checks >= 0.95.
SOURCED_GATE_MIN = 0.90
INFERRED_GATE = ("confidence", ("inferred",), 0.95)
PROCESS_DIR = "_process"
#: (final fix I7) the harness's own time inside a case, added to its hard cap
#: by the engine (Runner.case_cap_s): isolation resets / a contaminated-frame
#: relaunch (base), plus a share per render a stats case does (before/after
#: raster + before/after preview). Harness slowness must not read as a hang.
HARNESS_BASE_S = 90.0
HARNESS_PER_RENDER_S = 45.0
RENDERS_PER_STATS_CASE = 4
LIVE_LAYERS = {"coverage", "scenarios", "scenarios_agent", "bulk", "looks", "robustness",
               "parity", "guide"}


def isolate_process(root: Path) -> None:
    """The gauntlet process itself never uses Pratik's Scouter config, cache or
    vault (the owner file, darktable-cli configdirs and WB lookups live here)."""
    from tests.gauntlet.session import isolated_env, write_config

    write_config(root)
    env = isolated_env(root)
    # (fix round 1, N3) the library too, as isolated_env pins it for children:
    # never fall back to a running darktable's or ~/.config/darktable's library.
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH",
              "SCOUTER_DARKTABLE_LIBRARY_DB"):
        os.environ[k] = env[k]


def routed_body(case: Case) -> str:
    """The body a RoutedSession opens for this case (chart / no body -> olympus)."""
    return case.body if case.body not in (None, "chart") else "olympus"


def point_cache(cache: Path, *gauntlet_roots: Path, carry: bool = True) -> None:
    """Point this process's SCOUTER_CACHE at `cache` (carry 1).

    dtprocess.owner_path() is cache_root()/darktable.owner.json, so the runner
    process and the session's MCP server child (session.isolated_env pins its
    SCOUTER_CACHE to <session root>/cache) must share one cache root, or a heal
    inside the MCP server writes an owner record the runner cannot see. The
    current owner record is carried over so a darktable an earlier session
    failed to quit stays "ours" (quit, never a second launch) -- but only from
    a cache inside a gauntlet root (runs root, run dir), never from Pratik's
    real cache. carry=False only re-points (the record at `cache` is newer)."""
    from scouter.core import dtprocess

    old = dtprocess.owner_path()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["SCOUTER_CACHE"] = str(cache)
    new = dtprocess.owner_path()
    if carry and old != new and old.exists() and any(
            old.resolve().is_relative_to(Path(r).expanduser().resolve()) for r in gauntlet_roots):
        shutil.copyfile(old, new)


def adopt_leftover_owner(run, process_cache: Path, *, owned_pid=None) -> Path | None:
    """(final fix I1): after a killed run, the owner record of the darktable it
    left running sits in the killed session's cache (it reaches the process
    cache only through abort/close). Point the process cache at the newest
    session record that owned_pid() still accepts, so the next session quits
    that darktable as ours instead of refusing it as foreign. Returns the
    adopted record, or None (the process cache is then left as it was)."""
    from scouter.core import dtprocess

    owned_pid = owned_pid or dtprocess.owned_pid
    process_cache.mkdir(parents=True, exist_ok=True)
    os.environ["SCOUTER_CACHE"] = str(process_cache)
    target = process_cache / dtprocess.OWNER_FILE
    if owned_pid() is not None:
        return None  # the process record is already live and ours
    original = target.read_bytes() if target.exists() else None

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    records = sorted(Path(run.sessions_dir).glob(f"*/*/cache/{dtprocess.OWNER_FILE}"),
                     key=mtime, reverse=True)
    for rec in records:
        try:
            shutil.copyfile(rec, target)
        except OSError:
            continue
        if owned_pid() is not None:
            return rec
    if original is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(original)
    return None


_SESSION_DIR = re.compile(r"^(\d+)-")


def highest_session_n(layer_dir: Path) -> int:
    """(final fix I1): the highest NNN of `<layer_dir>/NNN-body` (0 if none)."""
    try:
        names = [p.name for p in layer_dir.iterdir() if p.is_dir()]
    except OSError:
        return 0
    return max((int(m.group(1)) for n in names if (m := _SESSION_DIR.match(n))), default=0)


def tag_sourced_components(cases: list[Case]) -> list[Case]:
    """Add `sourced_component` to every sourced looks check tag (carry 2)."""
    for c in cases:
        for t in c.meta.get("check_tags") or []:
            if t.get("confidence") in looks.SOURCED:
                t["sourced_component"] = t["component"]
    return cases


def look_gates(cases: list[Case]) -> tuple:
    """Only gates whose tags exist in the generated cases (an n=0 gate fails the bar)."""
    tags = [t for c in cases for t in c.meta.get("check_tags") or []]
    comps = sorted({t["sourced_component"] for t in tags if "sourced_component" in t})
    gates = tuple(("sourced_component", (comp,), SOURCED_GATE_MIN) for comp in comps)
    if any(t.get("confidence") in INFERRED_GATE[1] for t in tags):
        gates += (INFERRED_GATE,)
    return gates


def _stage_guide_job(session) -> None:
    """Fix B (night-1 triage rank 9, GD-job-not-staged): the guide layer's
    `{job_slug}`/`{job_frame:N}` placeholders (layers/guide.py
    session_preset_apply_job, guide_job_run(_dry)) point at a job
    `session.py`'s own `stage()` only ever predicted the slug for and copied
    card frames into -- it never ran `library_import`, so `job run` and
    `preset apply-job` honestly (but wrongly, from the harness's own gap)
    answer "no job with slug ...". Mirrors exec_bulk.run_bulk_case's own
    import step: a real `library_import` of the staged card into that slug,
    then `execute._imported_frames` resolves `job_frames` to the paths the
    import actually wrote (never the card copies, which stay outside the
    library the product's own job/preset verbs read from).

    Review round 2, finding 6 (GB-1, P1): a failed import used to be a
    silent no-op, so gd-10/18/19 would go on to fail as "product" failures
    ("no job with slug ...") -- exactly the harness-mis-scoring-as-product
    class this whole branch exists to remove. Any failure now records the
    reason on `session.guide_job_stage_error`; `execute.run_tool_case`
    reads it and blocks (never runs) any later case whose own steps
    reference `{job_slug}`/`{job_frame:N}`/`{job_frames}`, with a typed
    `fixture: guide job import failed: <reason>` reason instead of letting
    it fail on the product's own terms for a fixture gap. Re-opening a
    guide session on an MCP reopen re-imports into the same slug (a
    resume-skip on the product side, harmless) and clears any earlier
    error."""
    from tests.gauntlet.execute import ImportIncomplete, _imported_frames

    staged = session.staged
    session.guide_job_stage_error = None
    if staged is None or not staged.job_frames:
        return
    rec = session.call("library_import", {"source": str(staged.card),
                                          "job_slug": staged.vars["job_slug"],
                                          "confirm": True}, 60.0, write=True)
    if rec.error:
        session.guide_job_stage_error = rec.error
        return
    try:
        resolved = _imported_frames(session, rec.envelope, staged.job_frames)
    except ImportIncomplete as e:
        session.guide_job_stage_error = str(e)
        return
    if resolved is None:
        session.guide_job_stage_error = "library_import answered nothing usable"
        return
    staged.job_frames = resolved


class RoutedSession:
    """One ScouterSession per body, opened lazily; cases dispatch by layer."""

    def __init__(self, adapter: ScouterAdapter, layer: str, run, *,
                 session_factory=None, agent_factory=None) -> None:
        self.adapter = adapter
        self.layer = layer
        self.run = run
        self.current = None
        self.agent = None  # the AgentSession of the running agent case (final fix C1)
        self.aborted = False  # (pre-night-1 N1/N3): set once, never cleared
        # (final fix I1): a resumed run numbers after the dirs already there,
        # never reusing one whose sidecars/library an earlier session edited.
        self.n = highest_session_n(run.sessions_dir / layer)
        self.session_factory = session_factory or self._scouter_session
        self.agent_factory = agent_factory or self._agent_session

    @staticmethod
    def _scouter_session(corpus, root: Path, body: str, run_root: Path):
        from tests.gauntlet.observe import noise_floors
        from tests.gauntlet.session import ScouterSession

        return ScouterSession(corpus, root, body, run_root=run_root,
                              floors_fn=lambda f: noise_floors(f, root / "floor"))

    @staticmethod
    def _agent_session(inner):
        from tests.gauntlet.agent import AgentSession

        return AgentSession(inner)

    def _session(self, body: str):
        from tests.gauntlet.session import SessionBlocked

        if self.aborted:  # (N1): an aborted routed session opens nothing new
            raise SessionBlocked("routed session aborted")
        if self.current is not None and self.current.body == body and \
                not getattr(self.current, "mcp_dead", False):  # (final fix I3): reopen
            return self.current
        if self.current is not None:
            old, self.current = self.current, None
            try:
                old.close()
            except Exception:  # noqa: BLE001 - best effort; open() quits our leftover
                old.abort()
        self.n += 1
        root = self.run.sessions_dir / self.layer / f"{self.n:03d}-{body}"
        point_cache(root / "cache", *self._roots())  # == the MCP child's cache
        s = self.session_factory(self.adapter.corpus(), root, body, self.run.root)
        self.current = s
        if self.aborted:  # (N1): abort() set the flag before it read current
            raise SessionBlocked("routed session aborted")
        s.open()
        if self.aborted:
            s.abort()  # a repeat abort: kills only s's recorded pids
            raise SessionBlocked("routed session aborted during open")
        if self.layer == "guide":
            _stage_guide_job(s)
        return s

    def open_body(self, body: str):
        """(final fix I7): open the session for `body` now (eager open). On a
        failure nothing is left half-open and the process cache is restored;
        the error reaches the runner (a typed session_open result, or a
        RefuseRun for a foreign darktable)."""
        try:
            return self._session(body)
        except BaseException:
            if not self.aborted:  # (N1): after an abort the cache may be the next session's
                self.current = None
                self._back_to_process_cache()
            raise

    def _roots(self) -> tuple[Path, Path]:
        return self.adapter.runs_root, self.run.root

    def _back_to_process_cache(self) -> None:
        point_cache(self.adapter.runs_root / PROCESS_DIR / "cache", *self._roots())

    def _back_to_process_cache_if_mine(self, old_cache: Path) -> None:
        """(fix round 2 review N2): re-point ambient SCOUTER_CACHE to the
        process cache only when it STILL names `old_cache` -- the JUST-
        CLOSED session's own cache. A close/abort that finishes late (its
        own darktable quit/MCP teardown can take a while) must never stomp
        on a NEWER session's cache pointer that already replaced it in the
        meantime: that orphans whatever the newer session just launched
        (`point_cache`'s own carry step then reads the wrong "old" owner
        record) -- the production shape, pid 69648 (N2)."""
        if os.environ.get("SCOUTER_CACHE") == str(old_cache):
            self._back_to_process_cache()

    def run_case(self, case: Case):
        from gauntlet.adapter import Observation
        from tests.gauntlet import exec_bulk, faults
        from tests.gauntlet.execute import run_tool_case
        from tests.gauntlet.session import SessionBlocked

        if self.layer == "scenarios_agent":
            # (card t_e82c2951): before the session even opens, so a case at
            # or past the usage threshold never pays for a darktable launch
            # first -- covers every case, and (being the first thing this
            # layer's first case does) the layer's own start too.
            agent_budget.check_usage_budget()
        if self.current is not None:
            # (fix round 1, N1): a capped abort/close of an earlier session that
            # finished late may have reset SCOUTER_CACHE; re-point, never carry
            # (the record in this session's cache is the live one).
            mine = self.current.root / "cache"
            if os.environ.get("SCOUTER_CACHE") != str(mine):
                point_cache(mine, *self._roots(), carry=False)
        try:
            s = self._session(routed_body(case))
        except SessionBlocked as e:
            self.current = None
            return Observation(blocked=f"session: {e}", hang_evidence=[f"session: {e}"])
        except RefuseRun:
            # (final fix I2): ForeignDarktable -- the engine stops the run and
            # writes no case result.
            self.current = None
            raise
        # Park (agent usage limit) is deliberately not caught: the runner parks.
        if self.layer == "bulk":
            obs = exec_bulk.run_bulk_case(s, case)
        elif self.layer == "robustness":
            obs = faults.run_fault_case(s, case)
        elif self.layer == "scenarios_agent":
            # (final fix C1): keep the handle so abort() can kill its claude.
            agent = self.agent_factory(s)
            self.agent = agent
            if self.aborted:  # (pre-night-1 N3): abort ran before the handle existed
                agent.aborted = True
                agent.stop_claude()
            obs = agent.run_case(case)
        else:
            obs = run_tool_case(s, case)
        # (final fix I3): an MCP-server death inside this case is a crash.
        take = getattr(s, "take_mcp_deaths", None)
        deaths = take() if take is not None else []
        if deaths and isinstance(obs, Observation):
            obs.crash_reports.extend(deaths)
        #: (fix round 1 review M1): hang recovery lived only in the
        #: robustness layer (`faults.run_fault_case`'s own finally/disarm
        #: already fence_and_recover there -- never doubled up here). A hang
        #: in ANY other layer (scenarios, looks, coverage, bulk, ...) left
        #: darktable wedged with nothing to recover it; with Fix B, every
        #: later case in that session then reads `inherited_wedge` forever.
        #: Recover once, right here, on the SAME two conditions
        #: `evaluate.is_hang`/Fix B key on: a `hang*` blocked reason, or an
        #: `inherited_wedge` this session already carries into the next case.
        if self.layer != "robustness" and isinstance(obs, Observation):
            blocked = obs.blocked or ""
            if blocked.startswith("hang") or blocked == "inherited_wedge":
                recover_errors = faults.fence_and_recover(s)
                if recover_errors:
                    obs.extra.setdefault("disarm_errors", []).extend(recover_errors)
        return obs

    def _stop_agent(self) -> None:
        """Kill a running agent case's claude process group (final fix C1)."""
        agent, self.agent = self.agent, None
        if agent is not None:
            agent.aborted = True
            agent.stop_claude()

    def abort(self) -> dict:
        self.aborted = True
        res = {"ok": True, "how": "nothing_open", "killed": []}
        try:
            self._stop_agent()
        finally:
            if self.current is not None:
                old, self.current = self.current, None
                old_cache = old.root / "cache"
                try:
                    res = old.abort() or res
                finally:
                    self._back_to_process_cache_if_mine(old_cache)
        return res

    def close(self) -> None:
        self._stop_agent()
        if self.current is not None:
            old, self.current = self.current, None
            old_cache = old.root / "cache"
            try:
                old.close()
            finally:
                self._back_to_process_cache_if_mine(old_cache)


class ScouterAdapter:
    name = "scouter"
    #: (pre-night-1 N1): a cold launch + gui_ready + MCP initialize + the
    #: noise-floor renders; the engine default (180 s) is too tight.
    session_open_cap_s = 420.0

    def __init__(self, runs_root: Path = RUNS_ROOT) -> None:
        self.runs_root = runs_root
        self._tax = None
        self._corpus = None
        self._originals_before: dict | None = None
        self._look_gates: tuple | None = None
        self._install: dict[str, object] = {}
        self._originals_now: dict[str | None, dict] = {}
        #: RoutedSession's ScouterSession factory (None = the real one); a seam
        #: for tests, which never launch darktable.
        self.session_factory = None
        self._opening: RoutedSession | None = None  # (N1): the in-flight eager open

    # -- data --------------------------------------------------------------

    def tax(self):
        if self._tax is None:
            self._tax = taxonomy.load()
        return self._tax

    def corpus(self):
        if self._corpus is None:
            path = self.runs_root / "corpus.json"
            if not path.exists():
                raise FileNotFoundError(f"{path} missing: run the corpus survey first "
                                        "(python -m tests.gauntlet.corpus_survey)")
            self._corpus = corpus_mod.Corpus.load(path)
        return self._corpus

    # -- engine protocol ---------------------------------------------------

    def layers(self) -> list[LayerSpec]:
        return [
            LayerSpec("offline", 0, offline.EXPECTED, False, None, "Offline resolver sweep"),
            LayerSpec("coverage", 1, coverage.expected_count(), True, 0.95, "Coverage"),
            LayerSpec("scenarios", 2, 701, True, 0.95, "Scenarios (tool mode)"),
            LayerSpec("scenarios_agent", 3, 160, True, 0.95, "Scenarios (agent mode)"),
            LayerSpec("bulk", 4, bulk.EXPECTED, True, 0.95, "Bulk"),
            LayerSpec("looks", 5, looks.EXPECTED, True, 0.95, "Looks", self.look_gates()),
            LayerSpec("robustness", 6, robustness.EXPECTED, True, 0.95, "Robustness"),
            LayerSpec("parity", 7, parity.EXPECTED, True, 0.95, "MCP/CLI parity"),
            LayerSpec("install", 8, install.EXPECTED, True, 0.95, "Install rehearsal"),
            LayerSpec("guide", 9, guide.EXPECTED, True, 0.95, "Guide + learning mode"),
        ]

    def look_gates(self) -> tuple:
        if self._look_gates is None:
            self._look_gates = look_gates(self.generate("looks"))
        return self._look_gates

    def generate(self, layer: str) -> list[Case]:
        t = self.tax()
        if layer == "offline":
            return offline.generate(t)
        if layer == "coverage":
            return coverage.generate()
        if layer in ("scenarios", "scenarios_agent"):
            tool, agent = scenarios.generate(t)
            cases = tool if layer == "scenarios" else agent
            return sorted(cases, key=lambda c: (c.body or "", c.id))  # fewer relaunches
        if layer == "bulk":
            # Not body-sorted (fix round 1, I2): cases of one session share its
            # out_dir, so export_files / frames_verified would count earlier
            # cases' exports. Today a bulk session holds at most 2 flow cases.
            return bulk.generate(t)
        if layer == "looks":
            # (fix round 1, I2): group by routed body so sessions relaunch less
            # (233 switches -> 1). Stable on generator order within a body, not
            # by id: the chart golden case writes the per-run calibration every
            # look_plan reads, so it must stay first.
            return sorted(tag_sourced_components(looks.generate(t)), key=routed_body)
        if layer == "robustness":
            # Generator order is load-bearing (carry 5): wd_auto_heal runs
            # before wd_heal_cap in one session/job. Never re-sort this layer.
            return robustness.generate(t)
        if layer == "parity":
            return parity.generate()
        if layer == "install":
            # One Rehearsal, in step order (carry 4): leak_check reads teardown's facts.
            return sorted(install.generate(t), key=lambda c: c.meta["order"])
        if layer == "guide":
            return guide.generate()
        raise ValueError(layer)

    def open_session(self, layer: str, run, next_case: Case | None = None):
        import tests.gauntlet.exec_guide  # noqa: F401 - registers the cli virtual tool
        import tests.gauntlet.exec_looks  # noqa: F401 - registers look_plan/chart_golden
        import tests.gauntlet.exec_parity  # noqa: F401 - registers parity_pair
        import tests.gauntlet.faults  # noqa: F401 - registers watchdog_probe
        from tests.gauntlet.exec_install import InstallSession
        from tests.gauntlet.exec_offline import OfflineSession

        if layer == "offline":
            return OfflineSession()
        isolate_process(self.runs_root / PROCESS_DIR)
        adopt_leftover_owner(run, self.runs_root / PROCESS_DIR / "cache")
        self._record_originals(run.root)
        if layer == "install":
            # The runner may reopen (hard cap, rotation): hand back the same
            # rehearsal so the 10 steps share one Rehearsal (carry 4).
            key = str(run.root)
            if key not in self._install:
                self._install[key] = InstallSession(run.root / "install")
            return self._install[key]
        if layer == "looks":
            from tests.gauntlet.chart_render import write_chart

            if not (run.root / "chart" / "chart-v1.pfm").exists():
                write_chart(run.root)
        routed = RoutedSession(self, layer, run, session_factory=self.session_factory)
        # (final fix I7): open the product session HERE, under the runner's
        # session-open cap, not lazily inside the first case's hard cap.
        # (pre-night-1 N2): the runner names the case it opens for.
        body = routed_body(next_case) if next_case is not None else self._next_body(layer, run)
        if body is not None:
            self._opening = routed
            try:
                routed.open_body(body)
            finally:
                if self._opening is routed:
                    self._opening = None
        return routed

    def abort_open(self) -> dict:
        """(pre-night-1 N1): engine hook, called from the runner thread when
        open_session exceeds session_open_cap_s. Aborts only the in-flight
        RoutedSession (the pids it launched); typed result."""
        routed, self._opening = self._opening, None
        if routed is None:
            return {"ok": True, "how": "nothing_opening", "killed": []}
        return routed.abort()

    @staticmethod
    def _next_body(layer: str, run) -> str | None:
        """The routed body of the layer's first case without a result (the
        case the runner runs next), else of its first case; None when the run
        has no cases for it (the session then opens lazily)."""
        try:
            cases = [c for c in run.read_cases() if c.layer == layer]
        except (OSError, ValueError):
            return None
        if not cases:
            return None
        done = run.done_ids()
        nxt = next((c for c in cases if c.id not in done), cases[0])
        return routed_body(nxt)

    def harness_allowance_s(self, case: Case) -> float:
        """(final fix I7): engine hook -- see HARNESS_BASE_S."""
        if case.layer not in LIVE_LAYERS:
            return 0.0
        from tests.gauntlet.execute import STAT_METRICS

        renders = RENDERS_PER_STATS_CASE if any(
            c.metric in STAT_METRICS for c in case.expect.checks) else 0
        return HARNESS_BASE_S + renders * HARNESS_PER_RENDER_S

    def foreign_pids(self) -> list[int]:
        from scouter.core import dtprocess

        return dtprocess.foreign_pids()

    def preflight(self, run, layer: str) -> str | None:
        """(final fix I2): refuse a live layer while a darktable this runner did
        not launch is running (or when that cannot be told). Our own leftover
        from a killed run is adopted first (I1), so it reads as ours."""
        if layer not in LIVE_LAYERS:
            return None
        isolate_process(self.runs_root / PROCESS_DIR)
        adopt_leftover_owner(run, self.runs_root / PROCESS_DIR / "cache")
        try:
            foreign = self.foreign_pids()
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"cannot tell whether a foreign darktable runs ({type(e).__name__}: {e})"
        if foreign:
            return (f"foreign darktable: pids {foreign} were not launched by the gauntlet; "
                    "quit them first (the runner never signals them)")
        return None

    def _record_originals(self, run_root: Path) -> None:
        path = run_root / "originals.before.json"
        if not path.exists():
            path.write_text(json.dumps(corpus_mod.sha256_originals(), sort_keys=True))
        self._originals_before = json.loads(path.read_text())
        self._run_root = run_root

    def crash_count(self) -> int:
        return len(self._crash_files(0.0))

    def crash_reports_since(self, t: float) -> list[str]:
        return [str(p) for p in self._crash_files(t)]

    def _crash_files(self, since: float) -> list[Path]:
        try:
            entries = list(CRASH_DIR.iterdir())
        except OSError:
            return []
        out = []
        for p in entries:
            if p.suffix not in (".ips", ".crash", ".hang") or \
                    not any(n in p.name.lower() for n in ("darktable", "python")):
                continue
            try:  # (fix round 1, N2): one vanished file must not hide the others
                if p.stat().st_mtime >= since:
                    out.append(p)
            except OSError:
                continue
        return sorted(out)

    def _originals(self, run=None) -> tuple[dict, dict] | None:
        """(before, now) sha256 maps, or None when no live layer ran. `gauntlet
        report` is a fresh process (fix round 1, I1): read the run's
        originals.before.json. The now-hash is computed once per run."""
        before = self._originals_before
        key = str(run.root) if run is not None else None
        if not before and run is not None:
            path = Path(run.root) / "originals.before.json"
            if path.exists():
                before = json.loads(path.read_text())
        if not before:
            return None
        if key not in self._originals_now:
            self._originals_now[key] = corpus_mod.sha256_originals()
        return before, self._originals_now[key]

    def signoff_lines(self, run=None) -> list[str]:
        got = self._originals(run)
        if got is None:
            return ["EditingTrial originals: not checked (no live layer ran)"]
        before, after = got
        return [f"EditingTrial originals unchanged (sha256 of {len(after)}): "
                f"{'yes' if after == before else 'NO'}"]

    def signoff_blockers(self, run=None) -> list[str]:
        got = self._originals(run)
        return ["EditingTrial originals changed"] if got and got[0] != got[1] else []

    def generation_notes(self, cases: list[Case]) -> str:
        t = self.tax()
        plans = {r["id"]: plan_request(r, t.check_map) for r in t.requests}
        p0 = select_p0(t.requests)
        by_class = Counter(plans[r["id"]].klass for r in p0)
        lines = ["## Scouter notes", "",
                 f"P0 dry run: {len(p0)} requests; by class {dict(sorted(by_class.items()))}.",
                 "Served = in + partial + workflow; the rest pass only as honest refusals.",
                 "", "### Genre workflow step coverage (12 taxonomy workflows)", ""]
        served = {"exposure", "sigmoid", "temperature", "colorbalancergb", "filmicrgb",
                  "colorzones"}
        for wf in t.workflows:
            steps = wf["steps"]
            ok = sum(1 for s in steps if (s.get("module") or "").split("|")[0] in served)
            lines.append(f"- {wf['genre']}: {ok}/{len(steps)} steps served today")
        return "\n".join(lines) + "\n"

    def write_tools(self) -> frozenset[str]:
        return WRITE_TOOLS | {"look_plan", "parity_pair", "cli"}

    def policy(self):
        return POLICY


def make() -> ScouterAdapter:
    return ScouterAdapter()
