"""Scouter's gauntlet adapter: the one object `python -m gauntlet` loads
(gauntlet.json -> tests.gauntlet.adapter:make). Everything Scouter-specific
the engine needs is here or imported from here."""

from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from pathlib import Path

from gauntlet.adapter import LayerSpec
from gauntlet.case import Case
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
LIVE_LAYERS = {"coverage", "scenarios", "scenarios_agent", "bulk", "looks", "robustness",
               "parity", "guide"}


def isolate_process(root: Path) -> None:
    """The gauntlet process itself never uses Pratik's Scouter config, cache or
    vault (the owner file, darktable-cli configdirs and WB lookups live here)."""
    from tests.gauntlet.session import isolated_env, write_config

    write_config(root)
    env = isolated_env(root)
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH"):
        os.environ[k] = env[k]


def point_cache(cache: Path, *gauntlet_roots: Path) -> None:
    """Point this process's SCOUTER_CACHE at `cache` (carry 1).

    dtprocess.owner_path() is cache_root()/darktable.owner.json, so the runner
    process and the session's MCP server child (session.isolated_env pins its
    SCOUTER_CACHE to <session root>/cache) must share one cache root, or a heal
    inside the MCP server writes an owner record the runner cannot see. The
    current owner record is carried over so a darktable an earlier session
    failed to quit stays "ours" (quit, never a second launch) -- but only from
    a cache inside a gauntlet root (runs root, run dir), never from Pratik's
    real cache."""
    from scouter.core import dtprocess

    old = dtprocess.owner_path()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["SCOUTER_CACHE"] = str(cache)
    new = dtprocess.owner_path()
    if old != new and old.exists() and any(
            old.resolve().is_relative_to(Path(r).expanduser().resolve()) for r in gauntlet_roots):
        shutil.copyfile(old, new)


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


class RoutedSession:
    """One ScouterSession per body, opened lazily; cases dispatch by layer."""

    def __init__(self, adapter: ScouterAdapter, layer: str, run, *,
                 session_factory=None) -> None:
        self.adapter = adapter
        self.layer = layer
        self.run = run
        self.current = None
        self.n = 0
        self.session_factory = session_factory or self._scouter_session

    @staticmethod
    def _scouter_session(corpus, root: Path, body: str, run_root: Path):
        from tests.gauntlet.observe import noise_floors
        from tests.gauntlet.session import ScouterSession

        return ScouterSession(corpus, root, body, run_root=run_root,
                              floors_fn=lambda f: noise_floors(f, root / "floor"))

    def _session(self, body: str):
        if self.current is not None and self.current.body == body:
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
        self.current = self.session_factory(self.adapter.corpus(), root, body, self.run.root)
        self.current.open()
        return self.current

    def _roots(self) -> tuple[Path, Path]:
        return self.adapter.runs_root, self.run.root

    def _back_to_process_cache(self) -> None:
        point_cache(self.adapter.runs_root / PROCESS_DIR / "cache", *self._roots())

    def run_case(self, case: Case):
        from gauntlet.adapter import Observation
        from tests.gauntlet import exec_bulk, faults
        from tests.gauntlet.agent import AgentSession
        from tests.gauntlet.execute import run_tool_case
        from tests.gauntlet.session import ForeignDarktable, SessionBlocked

        try:
            s = self._session(case.body if case.body not in (None, "chart") else "olympus")
        except (SessionBlocked, ForeignDarktable) as e:
            self.current = None
            return Observation(blocked=f"session: {e}", hang_evidence=[f"session: {e}"])
        # Park (agent usage limit) is deliberately not caught: the runner parks.
        if self.layer == "bulk":
            return exec_bulk.run_bulk_case(s, case)
        if self.layer == "robustness":
            return faults.run_fault_case(s, case)
        if self.layer == "scenarios_agent":
            return AgentSession(s).run_case(case)
        return run_tool_case(s, case)

    def abort(self) -> None:
        if self.current is not None:
            old, self.current = self.current, None
            try:
                old.abort()
            finally:
                self._back_to_process_cache()

    def close(self) -> None:
        if self.current is not None:
            old, self.current = self.current, None
            try:
                old.close()
            finally:
                self._back_to_process_cache()


class ScouterAdapter:
    name = "scouter"

    def __init__(self, runs_root: Path = RUNS_ROOT) -> None:
        self.runs_root = runs_root
        self._tax = None
        self._corpus = None
        self._originals_before: dict | None = None
        self._look_gates: tuple | None = None
        self._install: dict[str, object] = {}

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
            return bulk.generate(t)
        if layer == "looks":
            return tag_sourced_components(looks.generate(t))
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

    def open_session(self, layer: str, run):
        import tests.gauntlet.exec_guide  # noqa: F401 - registers the cli virtual tool
        import tests.gauntlet.exec_looks  # noqa: F401 - registers look_plan/chart_golden
        import tests.gauntlet.exec_parity  # noqa: F401 - registers parity_pair
        import tests.gauntlet.faults  # noqa: F401 - registers watchdog_probe
        from tests.gauntlet.exec_install import InstallSession
        from tests.gauntlet.exec_offline import OfflineSession

        if layer == "offline":
            return OfflineSession()
        isolate_process(self.runs_root / PROCESS_DIR)
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
        return RoutedSession(self, layer, run)

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
            return sorted(p for p in CRASH_DIR.iterdir()
                          if p.suffix in (".ips", ".crash", ".hang")
                          and any(n in p.name.lower() for n in ("darktable", "python"))
                          and p.stat().st_mtime >= since)
        except OSError:
            return []

    def signoff_lines(self) -> list[str]:
        if not self._originals_before:
            return ["EditingTrial originals: not checked (no live layer ran)"]
        after = corpus_mod.sha256_originals()
        same = after == self._originals_before
        return [f"EditingTrial originals unchanged (sha256 of {len(after)}): "
                f"{'yes' if same else 'NO'}"]

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
