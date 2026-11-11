# Hyperbolic Time Chamber (Scouter release gauntlet) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Hyperbolic Time Chamber (HTC): a product-agnostic release-gauntlet engine (`gauntlet/`, stdlib only) plus Scouter's adapter (`tests/gauntlet/`), then run it overnight against Scouter, fix what it finds, add the three approved vocabulary families, and produce the release report.

**Architecture:** The engine owns the chain taxonomy -> generator -> runner -> evaluator -> report -> fix list and knows nothing about photos or darktable; it talks to a product only through `gauntlet.adapter.Adapter` / `Session` (row 436: "engine generic, product adapters separate"), so Capsule Corp can reuse it. Scouter's side (research JSONs, check map, darktable sessions, corpus, chart render, agent mode, the ten layer generators and their executors) lives under `tests/gauntlet/` and is loaded by `python -m gauntlet` from `gauntlet.json`. Product changes (process control, watchdog, W4, vocabulary) land in `scouter/` under the usual build rules.

**Tech Stack:** Python 3.11 (repo `.venv`), stdlib only in `gauntlet/`; pytest; darktable 5.4.1 + `darktable-cli` + the `darktable_mcp` Lua plugin (live tasks only); Claude Code `claude -p` (agent mode, live only).

**Spec:** `docs/superpowers/specs/2026-09-26-scouter-release-gauntlet-design.md` (binding), amended by decision rows 433, 436 and 437 in `~/second-brain/Decisions/Capsule Corp - System design decisions 2026-09-06.md`. Executors read both. Where this plan and the spec differ, the difference is listed in "Spec amendments" below and comes from a decision row.

**Repo:** `~/CodeBase/scouter`, base `main` 46c0a15. Every command below runs from the repo root.

## Global Constraints

Copied from spec §1 (goal and release bar) and rows 433/436/437, plus the controller's build rules. Every task's requirements include this section.

- Goal (spec §1): a Claude Code user on macOS with darktable 5.4 installs Scouter, registers the MCP server, and edits real photographs by talking. Every request ends in exactly one of: a verified edit, an honest refusal, or a typed error. It never crashes, never hangs without a typed answer, and never says "done" when nothing happened.
- Release bar (row 433), all six on one full run of one commit: 1 zero crashes (no new darktable/python `.ips`/`.crash`/`.hang` in `~/Library/Logs/DiagnosticReports` inside the run, zero MCP-server deaths); 2 zero silent failures (six conditions (a)-(f), spec §1); 3 100% of P0 (partial does not count); 4 P1 at least 95% per layer with partial = 0.5, looks at least 90% per sourced target and at least 95% correct sign on inferred targets, every miss listed with a repro; 5 honest refusals pass (spec §5.4); 6 scope = Claude Code through MCP plus the CLI, macOS, darktable 5.4.x, one fresh-machine install rehearsal.
- Invariants at every tier: zero crashes, zero silent failures, zero untyped hangs; every case answers within its hard cap or is recorded as a typed `blocked`.
- Name (row 436): Hyperbolic Time Chamber. The ENGINE is product-agnostic and lives in `gauntlet/` (package `gauntlet`, stdlib only, no `scouter` import, tests included); Scouter's parts are adapters under `tests/gauntlet/`.
- P0 gate (row 436): no served-rate gate this release; every P0 is verified or honestly refused; the report headline states the served vs refused split.
- Agent mode (row 436): 160 cases, Sonnet through `claude -p`, runs as soon as the build is ready; the usage meter is ignored; on a usage limit it parks and resumes (this supersedes spec §2.3's "ask Pratik under 60%" rule).
- Gates (row 437): as spec §5 writes them. Fuji recipe calibration is deferred (after release, MIT/CC-BY ports only); Fuji looks are direction-only this pass.
- Vocabulary (row 437): grain + vignette sliders; lens correction on/off + rotate/straighten + simple crop presets; denoise strength + sharpen amount (direction + no-halo checks). Each lands module by module under the phase 3 pin rule (a module is `live` only after a host probe pins its `dt.gui.action` path and XMP slot) and is gated by reruns. Masks/local edits and LUT 3D stay deferred. Each family is its own task, AFTER the engine and the first night-1 run.
- Authority (row 437): Fable has full authority through plan, build, night-1 and the fix loop; stop only for a destructive action or a new design-level choice.
- Engine code: stdlib only. `gauntlet/tests/test_case_store.py::test_engine_is_stdlib_only_and_imports_no_product` enforces it.
- Tests never touch `~/Pictures/EditingTrial` originals, the real `~/.config/scouter`, or the user's darktable config. `tests/conftest.py` already pins the Scouter paths for everything under `tests/`; engine tests under `gauntlet/tests/` use only `tmp_path`.
- LIVE tasks and LIVE steps (anything that launches darktable, darktable-cli on the corpus, or `claude -p`) are run only by the controller's runner, never inside an implementer's tests. They are marked **LIVE**.
- The runner never signals a darktable it did not launch (`scouter.core.dtprocess.owned_pid()`), and refuses to start while a foreign darktable runs.
- Every commit message ends with the line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No `git stash` (row 434 incident). Each task on its own branch/worktree; merge through the merge gate.
- Tasks marked **PARALLEL-OK** touch files no other concurrently-runnable task touches; the controller may run them in separate worktrees at once, respecting "Depends on".

## Review Focus

The five inputs the spec implies, that no layer's own generator exercises, and that would bite Pratik first. Each has its pinning test in the owning task.

1. **A darktable the gauntlet did not launch is running** (Pratik's own session with unsaved edits): the runner must refuse to start and send no signal. Pinned by `test_refuses_a_darktable_it_did_not_launch` (Task 13) and `test_foreign_pid_is_refused_and_never_signalled` (Task 11).
2. **A path under `~/Pictures/EditingTrial` reaches any call** (a doc command, a placeholder, a symlink): refused before darktable sees it. Pinned by `test_guard_refuses_originals_and_symlinks` (Task 12) and `test_placeholders_and_originals_guard` (Task 14).
3. **The runner was killed mid-night** (power, Ctrl-C) leaving its own darktable running and a half-written result: the next `--resume` quits only its own leftover darktable and never trusts a `.tmp`. Pinned by `test_quits_its_own_leftover_darktable_from_a_killed_run` (Task 13) and `test_atomic_write_leaves_old_file_on_failure` (Task 1).
4. **The disk fills during bulk duplication** (120-frame cards, ~3 GB peak): the card builder refuses before copying. Pinned by `test_card_duplication_round_robin_and_disk_check` (Task 12).
5. **An agent usage limit hits in the middle of a follow-up conversation**: the whole case parks and reruns from a fresh conversation, never half-scored. Pinned by `test_limit_mid_followup_parks_the_whole_case` (Task 16) and `test_park_records_and_resume_picks_up` (Task 7).

## Spec amendments (from decision rows, and choices this plan makes)

- Code paths: engine `gauntlet/` (row 436), not `tests/gauntlet/{taxonomy,generate,run,evaluate,report}.py`. CLI is `python -m gauntlet ...` (not `python -m tests.gauntlet`); repro lines read `python -m gauntlet run --case <id> --stamp <stamp>`. Case schema id is `gauntlet-case/1` (generic).
- The benchmark harness is moved to `tests/gauntlet/benchmark/` (not `tests/benchmark/`) and the drivers stay in `tests/gauntlet/drivers/` (not `tests/acceptance/`), per the controller brief.
- Scenarios are two engine layers, `scenarios` (701 tool) and `scenarios_agent` (160); the spec's 861 is their sum. Total stays 5,167.
- Agent mode ignores the usage meter and parks on a limit (row 436), replacing spec §2.3's 60% rule.
- `auto_heal`'s `healed: true` is carried in the envelope's `data` (the `Envelope` shape stays frozen for the MCP golden test).
- W4 fix: the `wb.kelvin` verb reads back strictly (None + reason off the calibrated tint); relative moves keep the r/b answer marked `approximate` (Task 31). Coverage's Kelvin cases set tint 1.0 first so their M1 checks are exact.
- `scouter` CLI prints JSON by default, so parity uses `scouter <group> <verb>` with no `--json` flag.
- H2 (modal dialog): the plugin has no dialog test hook, so the case records a typed `blocked: skipped` with the reason; being P0 it needs Pratik's `accepted` triage (spec §2.6) unless a hook lands.
- Live sessions use darktable's default `--configdir` (as the benchmark and acceptance runs did) with an isolated `--library`; the install rehearsal is the only layer with its own configdir (spec §6 lists per-job configdirs as "later").

## File structure

Engine (`gauntlet/`, stdlib only, reusable by Capsule Corp):

| File | Responsibility |
|---|---|
| `gauntlet/__init__.py`, `__main__.py` | package, `python -m gauntlet` |
| `gauntlet/case.py` | Case / Step / Check / Expect, ids, canonical JSONL |
| `gauntlet/store.py` | run dir layout, atomic writes, resume listing |
| `gauntlet/adapter.py` | the product seam: `Adapter`, `Session`, `LayerSpec`, `CallRecord`, `Observation`, `Park`, `load_adapter` |
| `gauntlet/color.py` | sRGB/linear, luminance, XYZ, Lab, LCh, CIEDE2000 |
| `gauntlet/raster.py` | PPM (P6) parse, PFM read/write, 16-bit TIFF writer |
| `gauntlet/chart.py` | synthetic ColorChecker + grey-ramp chart, patch sampling |
| `gauntlet/metrics.py` | M1 tolerances, region stats (M2-M8), noise floor, ΔE verdict |
| `gauntlet/evaluate.py` | checks, silent failure (a)-(f), honest refusal rules, scoring |
| `gauntlet/generate.py` | generation gate (counts ±2%, unmapped), `cases.jsonl`, `generation.md` |
| `gauntlet/progress.py` | `progress.json` + `log.md` checkpoints |
| `gauntlet/runner.py` | sessions, hard caps, resume, crash guard, park |
| `gauntlet/report.py` | report.md/json (S8 first), contact sheet, fix list, roadmap |
| `gauntlet/html.py` | the progress page for the artifact |
| `gauntlet/cli.py` | `generate` / `run` / `report` / `progress-html` |
| `gauntlet/tests/` | engine tests + `toy.py` (a toy product adapter) |

Scouter adapter (`tests/gauntlet/`):

| File | Responsibility |
|---|---|
| `data/check_map.json` | the only hand-written gauntlet data (spec §2.1) |
| `taxonomy.py`, `checkmap.py`, `p0.py`, `policy.py` | data loading + count asserts, request -> checks/capabilities, P0 selection, evaluator policy |
| `benchmark/` | the merged editor benchmark harness (from `bench/editor`) |
| `drivers/` | acceptance driver and adjust check (already on main) |
| `corpus.py`, `corpus_survey.py` | originals guard, copies, survey, roles, bulk cards |
| `session.py` | one darktable + one MCP server + isolated env per session |
| `observe.py`, `execute.py` | snapshots, renders, stats; tool-mode case execution |
| `chart_render.py` | chart in darktable, golden, per-run calibration |
| `agent.py` | agent mode through `claude -p`, park on limit |
| `layers/*.py` | the ten layer generators |
| `exec_*.py`, `faults.py` | layer executors (virtual tools, fault injection) |
| `adapter.py` | `ScouterAdapter`, wiring for the engine |
| `golden/chart-v1.stats.json` | committed after the first clean chart render (Task 15/35) |

Product (`scouter/`): `core/dtprocess.py` (new), `core/watchdog.py` (new), `core/dtbridge.py`, `errors.py`, `config.py`, `registry.py`, `cli/main.py`, `core/wb.py`, and for the vocabulary families `core/vocab.py`, `core/phrases.py`, `core/moves.py`, `core/darkroom.py`, `core/xmp.py`, `mcp/schemas.py`.

## Execution order, parallelism, LIVE work

| Task | Title | Depends on | PARALLEL-OK | LIVE |
|---|---|---|---|---|
| 1 | Case model, run store, adapter seam | - | yes | |
| 2 | Colour science + rasters | - | yes | |
| 3 | Synthetic chart | 2 | yes | |
| 4 | Metrics M1-M10 + noise floor | 2, 3 | yes | |
| 5 | Evaluator | 1, 4 | yes | |
| 6 | Generator gate + toy adapter | 1, 5 | yes | |
| 7 | Checkpoints + runner | 5, 6 | yes | |
| 8 | Report, contact sheet, fix list, roadmap | 7 | yes | |
| 9 | Scouter data side (taxonomy, check map, P0, layer common) | 1, 10 | yes | |
| 10 | Merge the benchmark harness into `tests/gauntlet/benchmark/` | - | no (git merge) | |
| 11 | `scouter/core/dtprocess.py` + `dtbridge.health()` | 10 | no (dtbridge.py) | |
| 12 | Corpus: guard, copies, survey, roles, cards | 10 | yes | step 6 |
| 13 | Session | 1, 11, 12 | yes | |
| 14 | Observe + tool-mode execute + policy | 4, 5, 13 | yes | |
| 15 | Chart render, golden, calibration | 3, 4, 14 | yes | step 6 |
| 16 | Agent mode | 14 | yes | |
| 17 | Layer 0 offline | 9, 14 | yes | |
| 18 | Layer 1 coverage | 9 | yes | |
| 19 | Layer 2 scenarios | 9 | yes | |
| 20 | Layer 3 bulk | 9, 14 | yes | |
| 21 | Layer 4 looks | 9, 15 | yes | |
| 22 | Layer 5 robustness + faults | 9, 14, 20 | yes | |
| 23 | Layer 6 parity | 9, 14 | yes | |
| 24 | Layer 7 install rehearsal | 9, 14 | yes | |
| 25 | Layer 8 guide + learning | 9, 14 | yes | |
| 26 | CLI `python -m gauntlet` | 6, 7, 8, 27 | yes | |
| 27 | Progress HTML renderer | 7 | yes | |
| 28 | ScouterAdapter wiring + `gauntlet.json` | 16-27 | yes | step 5 |
| 29 | Watchdog: typed `darktable_unresponsive`, doctor lines, `--ping` | 11 | no (shared product files) | |
| 30 | Watchdog: auto-heal, `--heal`, telemetry | 29 | no | |
| 31 | W4 fix: tint-aware Kelvin readback | - | yes | |
| 32 | Vocabulary: grain + vignette | 35, 30 | no (vocab files) | probe |
| 33 | Vocabulary: lens on/off, rotate, crop presets | 32 | no | probe |
| 34 | Vocabulary: denoise + sharpen | 33 | no | probe |
| 35 | Night-1 run (controller) | 28, 30, 31 | - | **LIVE** |
| 36 | Fix loop (controller) | 35 | - | reruns LIVE |
| 37 | Night-2 rerun on the release candidate (controller) | 36, 34 | - | **LIVE** |
| 38 | Release report | 37 | - | |

Vocabulary Tasks 32-34 are written before Task 35 in the order the brief asked for, but they RUN after night 1 (row 437).

**Estimated build wall-clock with 3 parallel implementers:** Tasks 1-31 are about 19.5 implementer-hours (25-60 min each with review); the critical path (2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 26 -> 28) is about 5.5 h, so about **7 h** to "build ready" (plus about 30 min of controller LIVE steps: survey, chart check, smoke). Tasks 32-34 add about 4.5 h after night 1 (serial: each has a host probe). Night 1 is about 16.5 h of machine time including agent mode.

## Conventions for every task

- Python: `.venv/bin/python`. Engine tests: `.venv/bin/python -m pytest gauntlet/tests -q`. Adapter tests: `.venv/bin/python -m pytest tests/gauntlet -q`. Product tests: `.venv/bin/python -m pytest tests -q` (the full unit suite; real host tests carry the `host` pytest marker and self-skip via `addopts = -m "not host"` in pyproject.toml plus the `SCOUTER_HOST_TESTS` env gate in tests/conftest.py, so plain `pytest -q` never launches darktable and no longer silently drops the ~15 plain unit tests whose names merely contain "host" -- `-k "not host"` was a substring filter, not a marker filter, and used to skip those too; host tests stay opt-in via `-m host` + `SCOUTER_HOST_TESTS=1`).
- Lint before each commit: `.venv/bin/ruff check gauntlet tests/gauntlet scouter` must pass.
- Commit message format: `<area>: <what> (Task N, row 433)` then a blank line, then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. The commit steps below show the `-m` flags.

---

# Part 1: Engine core (`gauntlet/`, stdlib only)

### Task 1: Case model, run store, adapter seam

PARALLEL-OK. Depends on: nothing.

**Files:**
- Create: `gauntlet/__init__.py`, `gauntlet/case.py`, `gauntlet/store.py`, `gauntlet/adapter.py`, `gauntlet/tests/__init__.py` (empty)
- Test: `gauntlet/tests/test_case_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Case(id, layer, tier, mode, steps: list[Step], expect: Expect, body=None, frame_role=None, source={}, followups=[], meta={}, repro="")` with `budget_cap_s() -> float`, `to_dict()`, `Case.from_dict(d)`; `Step(tool, args={}, budget_s=8.0, write=False)`; `Check(metric, stat=None, region="frame", sign=0, kind="delta", target=None, step=None, tags={})`; `Expect(kind, checks=[], sidecar=None, codes=[], property=None)`; `case_id(layer, *parts) -> str`; `dumps(obj) -> str`; `to_jsonl(cases) -> str`; `from_jsonl(text) -> list[Case]`; `RunDir(runs_root, stamp)` with `.ensure()`, `.write_cases/read_cases`, `.write_result(id, doc)/read_result(id)/read_results()`, `.done_ids()`, and paths `root, cases_dir, previews_dir, sessions_dir, cases_jsonl, generation_md, log_md, progress_json, report_md, report_json, contact_sheet, fixlist_json, fixlist_md`; `new_stamp()`; `atomic_write_text(path, text)`; `LayerSpec(name, order, expected_count, live, p1_gate=0.95, title="", check_gates=())`; `CallRecord(tool, args, envelope=None, wall_s=0.0, ms=None, bounced=False, error=None, health=None, write=False)`; `Observation(calls, before, after, stats_before, stats_after, floors, agent_text, blocked, crash_reports, hang_evidence, healed, extra, wall_s)`; `Park(reason, until=None)`; protocols `Session` (`run_case`, `abort`, `close`) and `Adapter` (`name`, `layers`, `generate`, `open_session`, `crash_count`, `crash_reports_since`, `signoff_lines`, `generation_notes`, `write_tools`, `policy`); `load_adapter("module:factory")`.

- [ ] **Step 1: Write the failing test**

```python
import ast
import json
import sys
from pathlib import Path

import pytest

from gauntlet.adapter import load_adapter
from gauntlet.case import Case, Check, Expect, Step, case_id, from_jsonl, to_jsonl
from gauntlet.store import RunDir, atomic_write_text, new_stamp

ENGINE = Path(__file__).resolve().parents[1]


def _case(i: str = "a") -> Case:
    return Case(
        id=case_id("cov", "exposure.exposure", "sony_a7", f"set_{i}"),
        layer="coverage", tier="P0", mode="tool",
        steps=[Step("darkroom_set", {"module": "exposure", "param": "exposure", "value": 1.0},
                    budget_s=3.5, write=True)],
        expect=Expect("edit", checks=[Check("M1", target={"key": "exposure.exposure",
                                                          "value": 1.0, "unit": "EV"})]),
        body="sony_a7", frame_role="hero",
    )


def test_case_id_is_slugged_and_stable():
    assert case_id("scn", "r0001", "Olympus", "tool") == "scn-r0001-olympus-tool"
    assert case_id("cov", "exposure.exposure", "sony a7", "set hi") == \
        "cov-exposure.exposure-sony_a7-set_hi"


def test_jsonl_round_trip_is_byte_identical():
    cases = [_case("a"), _case("b")]
    text = to_jsonl(cases)
    again = to_jsonl(from_jsonl(text))
    assert text == again
    assert json.loads(text.splitlines()[0])["schema"] == "gauntlet-case/1"


def test_bad_tier_mode_kind_refused():
    with pytest.raises(ValueError):
        Case(id="x", layer="l", tier="P9", mode="tool", steps=[], expect=Expect("edit"))
    with pytest.raises(ValueError):
        Case(id="x", layer="l", tier="P0", mode="gui", steps=[], expect=Expect("edit"))
    with pytest.raises(ValueError):
        Case(id="x", layer="l", tier="P0", mode="tool", steps=[], expect=Expect("maybe"))


def test_budget_cap_counts_followups():
    c = _case()
    c.followups = [{"sentence": "more", "steps": [{"tool": "darkroom_say",
                                                   "args": {"phrase": "brighter"},
                                                   "budget_s": 8.0}]}]
    assert c.budget_cap_s() == pytest.approx(3.5 + 30 + 8 + 30)


def test_repro_defaults_to_the_engine_cli():
    assert _case().repro.startswith("python -m gauntlet run --case cov-")


def test_rundir_atomic_results_and_resume(tmp_path):
    run = RunDir(tmp_path, "20260926t000000z").ensure()
    run.write_cases([_case("a")])
    assert run.read_cases()[0].id == _case("a").id
    run.write_result("c1", {"id": "c1", "score": "pass"})
    assert run.done_ids() == {"c1"}
    assert run.read_result("c1")["score"] == "pass"
    assert not list(run.cases_dir.glob("*.tmp"))


def test_atomic_write_leaves_old_file_on_failure(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    atomic_write_text(p, "old")

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "new")
    assert p.read_text() == "old"
    assert not list(tmp_path.glob("*.tmp"))


def test_new_stamp_format():
    s = new_stamp()
    assert len(s) == 16 and s[8] == "t" and s.endswith("z")


def test_load_adapter_needs_module_colon_factory():
    with pytest.raises(ValueError):
        load_adapter("gauntlet.case")


def test_engine_is_stdlib_only_and_imports_no_product():
    """Rows 436/437: the engine is reusable by Capsule Corp. Only stdlib and
    `gauntlet.*` imports are allowed anywhere under gauntlet/ (tests included,
    pytest excepted)."""
    allowed = set(sys.stdlib_module_names) | {"gauntlet", "pytest"}
    bad = []
    for path in ENGINE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] not in allowed:
                    bad.append(f"{path.relative_to(ENGINE)}: {n}")
    assert bad == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_case_store.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'gauntlet'`.

- [ ] **Step 3: Write the implementation**

`gauntlet/__init__.py`:

```python
"""Hyperbolic Time Chamber: a product-agnostic release gauntlet engine.

taxonomy -> generator -> runner -> evaluator -> report -> fix loop.
Stdlib only. Products plug in through `gauntlet.adapter.Adapter`; this
package never imports a product (rows 436, 437)."""

__version__ = "1.0.0"
```

`gauntlet/case.py`:

```python
"""The case model (spec 2.2, schema gauntlet-case/1). Pure."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "gauntlet-case/1"
TIERS = ("P0", "P1", "P2")
MODES = ("tool", "agent", "offline", "cli")
EXPECT_KINDS = ("edit", "refuse", "workflow", "typed_error", "noop", "property")

_SLUG = re.compile(r"[^a-z0-9_.]+")


def slug(text: str) -> str:
    return _SLUG.sub("_", str(text).lower()).strip("_") or "x"


def case_id(layer: str, *parts: object) -> str:
    """`<layer>-<part>-<part>...`, every part slugged; stable across runs."""
    return "-".join([slug(layer), *(slug(p) for p in parts)])


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    budget_s: float = 8.0
    write: bool = False


@dataclass
class Check:
    metric: str  # M1..M10, "sidecar", "envelope", "file", "text"
    stat: str | None = None
    region: str = "frame"
    sign: int = 0
    kind: str = "delta"  # delta|toward_zero|to_zero|capped|restored|unchanged|equals|within|contains  # noqa: E501
    target: dict | None = None
    step: int | None = None
    tags: dict = field(default_factory=dict)


@dataclass
class Expect:
    kind: str
    checks: list[Check] = field(default_factory=list)
    sidecar: dict | None = None
    codes: list[str] = field(default_factory=list)
    property: str | None = None


@dataclass
class Case:
    id: str
    layer: str
    tier: str
    mode: str
    steps: list[Step]
    expect: Expect
    body: str | None = None
    frame_role: str | None = None
    source: dict = field(default_factory=dict)
    followups: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    repro: str = ""

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"{self.id}: tier {self.tier!r} not in {TIERS}")
        if self.mode not in MODES:
            raise ValueError(f"{self.id}: mode {self.mode!r} not in {MODES}")
        if self.expect.kind not in EXPECT_KINDS:
            raise ValueError(f"{self.id}: expect.kind {self.expect.kind!r} not in {EXPECT_KINDS}")
        if not self.repro:
            self.repro = f"python -m gauntlet run --case {self.id} --stamp <stamp>"

    def budget_cap_s(self) -> float:
        """Spec 2.3: each step's hard cap is budget + 30 s; the case cap is their sum."""
        steps = list(self.steps)
        for f in self.followups:
            steps.extend(_step(s) for s in f.get("steps", []))
        return sum(s.budget_s + 30.0 for s in steps) or 30.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = SCHEMA
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Case:
        d = {k: v for k, v in d.items() if k != "schema"}
        exp = d.pop("expect")
        return cls(
            steps=[_step(s) for s in d.pop("steps")],
            expect=Expect(
                kind=exp["kind"],
                checks=[Check(**c) for c in exp.get("checks", [])],
                sidecar=exp.get("sidecar"),
                codes=list(exp.get("codes", [])),
                property=exp.get("property"),
            ),
            **d,
        )


def _step(s: Any) -> Step:
    return s if isinstance(s, Step) else Step(**s)


def dumps(obj: Any) -> str:
    """Canonical JSON: sorted keys, no spaces drift, ASCII-safe. Byte-stable."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def to_jsonl(cases: list[Case]) -> str:
    return "".join(dumps(c.to_dict()) + "\n" for c in cases)


def from_jsonl(text: str) -> list[Case]:
    return [Case.from_dict(json.loads(line)) for line in text.splitlines() if line.strip()]
```

`gauntlet/store.py`:

```python
"""The run directory (spec 4.4): atomic per-case results, resume, cases.jsonl."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.case import Case, dumps, from_jsonl, to_jsonl


def new_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dt%H%M%Sz")


def atomic_write_text(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, fsync, then rename over `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class RunDir:
    """`<runs_root>/<stamp>/` and everything inside it."""

    def __init__(self, runs_root: Path, stamp: str) -> None:
        self.stamp = stamp
        self.root = Path(runs_root).expanduser() / stamp
        self.cases_dir = self.root / "cases"
        self.previews_dir = self.root / "previews"
        self.sessions_dir = self.root / "sessions"
        self.cases_jsonl = self.root / "cases.jsonl"
        self.generation_md = self.root / "generation.md"
        self.log_md = self.root / "log.md"
        self.progress_json = self.root / "progress.json"
        self.report_md = self.root / "report.md"
        self.report_json = self.root / "report.json"
        self.contact_sheet = self.root / "contact-sheet.html"
        self.fixlist_json = self.root / "fixlist.json"
        self.fixlist_md = self.root / "fixlist.md"

    def ensure(self) -> RunDir:
        for d in (self.root, self.cases_dir, self.previews_dir, self.sessions_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def case_path(self, case_id: str) -> Path:
        return self.cases_dir / f"{case_id}.json"

    def write_cases(self, cases: list[Case]) -> None:
        atomic_write_text(self.cases_jsonl, to_jsonl(cases))

    def read_cases(self) -> list[Case]:
        return from_jsonl(self.cases_jsonl.read_text(encoding="utf-8"))

    def write_result(self, case_id: str, doc: dict) -> Path:
        path = self.case_path(case_id)
        atomic_write_text(path, dumps(doc) + "\n")
        return path

    def read_result(self, case_id: str) -> dict | None:
        path = self.case_path(case_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def done_ids(self) -> set[str]:
        if not self.cases_dir.exists():
            return set()
        return {p.stem for p in self.cases_dir.glob("*.json")}

    def read_results(self) -> list[dict]:
        if not self.cases_dir.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.cases_dir.glob("*.json"))]
```

`gauntlet/adapter.py`:

```python
"""The product seam (row 436: engine generic, product adapters separate).

A product plugs in by providing an object that satisfies `Adapter`. The
engine calls only these methods; it never imports the product."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from gauntlet.case import Case


@dataclass(frozen=True)
class LayerSpec:
    name: str
    order: int
    expected_count: int
    live: bool
    p1_gate: float | None = 0.95
    title: str = ""
    #: Extra per-check gates, e.g. looks: (("confidence", ("documented",
    #: "community-consensus"), 0.90), ("confidence", ("inferred",), 0.95)).
    check_gates: tuple = ()


@dataclass
class CallRecord:
    tool: str
    args: dict
    envelope: dict | None = None
    wall_s: float = 0.0
    ms: int | None = None
    bounced: bool = False
    error: str | None = None  # "timeout", "transport", or a typed code
    health: str | None = None  # alive | starting | unresponsive (after a timeout)
    write: bool = False


@dataclass
class Observation:
    calls: list[CallRecord] = field(default_factory=list)
    before: dict = field(default_factory=dict)  # {"params": {...}, "fingerprint": str, "history_end": int}  # noqa: E501
    after: dict = field(default_factory=dict)
    stats_before: dict = field(default_factory=dict)  # {region: {stat: value}}
    stats_after: dict = field(default_factory=dict)
    floors: dict = field(default_factory=dict)  # {stat: floor}
    agent_text: str | None = None
    blocked: str | None = None  # launch | hang | hard_cap | transient | ...
    crash_reports: list[str] = field(default_factory=list)
    hang_evidence: list[str] = field(default_factory=list)
    healed: bool = False
    extra: dict = field(default_factory=dict)
    wall_s: float = 0.0


class Park(Exception):
    """Raised by a session when an external limit stops it (agent usage limit).

    `until` is a POSIX timestamp when the limit resets, or None if unknown.
    The runner records the case as parked (no result file) and moves on."""

    def __init__(self, reason: str, until: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.until = until


class Session(Protocol):
    def run_case(self, case: Case) -> Observation: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


class Adapter(Protocol):
    name: str

    def layers(self) -> list[LayerSpec]: ...
    def generate(self, layer: str) -> list[Case]: ...
    def open_session(self, layer: str, run_root: Any) -> Session: ...
    def crash_count(self) -> int: ...
    def crash_reports_since(self, t: float) -> list[str]: ...
    def signoff_lines(self) -> list[str]: ...
    def generation_notes(self, cases: list[Case]) -> str: ...
    def write_tools(self) -> frozenset[str]: ...
    def policy(self) -> Any: ...  # gauntlet.evaluate.Policy


def load_adapter(dotted: str) -> Adapter:
    """`package.module:factory` -> factory(). The only place a product is imported."""
    mod_name, _, attr = dotted.partition(":")
    if not attr:
        raise ValueError(f"adapter must be 'module:factory', got {dotted!r}")
    factory = getattr(importlib.import_module(mod_name), attr)
    return factory()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_case_store.py -q`
Expected: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/__init__.py gauntlet/case.py gauntlet/store.py gauntlet/adapter.py gauntlet/tests/__init__.py gauntlet/tests/test_case_store.py
git commit -m "gauntlet: case model, run store and the product seam (Task 1, row 436)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 2: Colour science and rasters

PARALLEL-OK. Depends on: nothing.

**Files:**
- Create: `gauntlet/color.py`, `gauntlet/raster.py`
- Test: `gauntlet/tests/test_color_raster.py`

**Interfaces:**
- Produces: `color.srgb_to_linear(v)`, `linear_to_srgb(v)`, `luminance(r, g, b)`, `y_to_lstar(y)`, `linear_rgb_to_xyz(r, g, b)`, `xyz_to_linear_rgb(x, y, z)`, `xyz_to_lab(x, y, z)`, `lab_to_xyz(L, a, b)`, `srgb8_to_lab(r8, g8, b8)`, `lab_to_linear_rgb(L, a, b)`, `lch(a, b) -> (C, h)`, `delta_e2000(lab1, lab2) -> float`; `raster.Raster8(width, height, data: bytes)` with `pixel(x, y)`, `raster.RasterF(width, height, data: array('f'))`, `parse_ppm(bytes) -> Raster8` (8 or 16 bit), `encode_ppm(Raster8)`, `encode_pfm(RasterF)`, `parse_pfm(bytes) -> RasterF`, `encode_tiff16(RasterF, clip=1.0)`.

The CIEDE2000 test uses all 34 published pairs of Sharma, Wu and Dalal (2005), Table 1; the implementation below was checked against all 34 to 5e-5.

- [ ] **Step 1: Write the failing test**

```python
from array import array

import pytest

from gauntlet import color
from gauntlet.raster import (
    Raster8,
    RasterF,
    encode_pfm,
    encode_ppm,
    encode_tiff16,
    parse_pfm,
    parse_ppm,
)

# Sharma, Wu and Dalal (2005), "The CIEDE2000 color-difference formula",
# Table 1: all 34 published test pairs (L1, a1, b1), (L2, a2, b2), dE00.
SHARMA_PAIRS = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize("lab1,lab2,expected", SHARMA_PAIRS)
def test_ciede2000_matches_sharma_table(lab1, lab2, expected):
    assert color.delta_e2000(lab1, lab2) == pytest.approx(expected, abs=5e-5)
    assert color.delta_e2000(lab2, lab1) == pytest.approx(expected, abs=5e-5)


def test_there_are_34_pairs():
    assert len(SHARMA_PAIRS) == 34


def test_srgb_round_trip_and_white():
    for v in (0.0, 0.02, 0.5, 1.0):
        assert color.linear_to_srgb(color.srgb_to_linear(v)) == pytest.approx(v, abs=1e-9)
    L, a, b = color.srgb8_to_lab(255, 255, 255)
    assert (L, a, b) == pytest.approx((100.0, 0.0, 0.0), abs=0.02)


def test_mid_grey_lab_and_lch():
    L, a, b = color.xyz_to_lab(*color.linear_rgb_to_xyz(0.18, 0.18, 0.18))
    assert L == pytest.approx(49.5, abs=0.1)
    assert abs(a) < 0.01 and abs(b) < 0.01
    c, h = color.lch(0.0, 10.0)
    assert (c, h) == pytest.approx((10.0, 90.0))


def test_lab_linear_round_trip():
    rgb = color.lab_to_linear_rgb(*color.xyz_to_lab(*color.linear_rgb_to_xyz(0.3, 0.2, 0.1)))
    assert rgb == pytest.approx((0.3, 0.2, 0.1), abs=1e-5)


def test_ppm_round_trip_and_16_bit_reduction():
    r = Raster8(2, 1, bytes([1, 2, 3, 250, 251, 252]))
    assert parse_ppm(encode_ppm(r)) == r
    sixteen = b"P6\n1 1\n65535\n" + bytes([0x12, 0x34, 0xAB, 0xCD, 0xFF, 0x00])
    assert parse_ppm(sixteen).data == bytes([0x12, 0xAB, 0xFF])


def test_ppm_rejects_short_payload_and_wrong_magic():
    with pytest.raises(ValueError):
        parse_ppm(b"P6\n2 2\n255\n\x00\x00")
    with pytest.raises(ValueError):
        parse_ppm(b"P3\n1 1\n255\n0 0 0")


def test_pfm_round_trip_keeps_top_row_first():
    data = array("f", [0.1, 0.2, 0.3, 1.5, 2.5, 3.5])  # 1 wide, 2 tall: top then bottom
    r = RasterF(1, 2, data)
    back = parse_pfm(encode_pfm(r))
    assert list(back.data) == pytest.approx(list(data))
    raw = encode_pfm(r)
    assert raw.startswith(b"PF\n1 2\n-1.0\n")


def test_tiff16_header_and_size():
    r = RasterF(2, 2, array("f", [0.5] * 12))
    t = encode_tiff16(r)
    assert t[:4] == b"II*\x00"
    assert len(t) > 2 * 2 * 3 * 2
    assert t[-2:] == (32768).to_bytes(2, "little")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_color_raster.py -q`
Expected: `ImportError: cannot import name 'color' from 'gauntlet'`.

- [ ] **Step 3: Write the implementation**

`gauntlet/color.py`:

```python
"""Colour science for the evaluator (spec 2.4). Pure stdlib.

sRGB <-> linear, Rec.709 luminance, XYZ (D65), CIELAB, LCh and CIEDE2000
(Sharma, Wu and Dalal 2005)."""

from __future__ import annotations

import math

#: D65 reference white, Y = 1 scale.
WHITE_D65 = (0.95047, 1.0, 1.08883)
_M_RGB_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_M_XYZ_RGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)


def srgb_to_linear(v: float) -> float:
    """One sRGB-encoded channel in 0..1 to linear light."""
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def linear_to_srgb(v: float) -> float:
    v = min(max(v, 0.0), 1.0)
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def luminance(r: float, g: float, b: float) -> float:
    """Rec.709 relative luminance of LINEAR rgb."""
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _f(t: float) -> float:
    d = 6 / 29
    return t ** (1 / 3) if t > d**3 else t / (3 * d * d) + 4 / 29


def _finv(t: float) -> float:
    d = 6 / 29
    return t**3 if t > d else 3 * d * d * (t - 4 / 29)


def y_to_lstar(y: float) -> float:
    return 116 * _f(max(y, 0.0)) - 16


def linear_rgb_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    m = _M_RGB_XYZ
    return (
        m[0][0] * r + m[0][1] * g + m[0][2] * b,
        m[1][0] * r + m[1][1] * g + m[1][2] * b,
        m[2][0] * r + m[2][1] * g + m[2][2] * b,
    )


def xyz_to_linear_rgb(x: float, y: float, z: float) -> tuple[float, float, float]:
    m = _M_XYZ_RGB
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z,
        m[1][0] * x + m[1][1] * y + m[1][2] * z,
        m[2][0] * x + m[2][1] * y + m[2][2] * z,
    )


def xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    xn, yn, zn = WHITE_D65
    fx, fy, fz = _f(x / xn), _f(y / yn), _f(z / zn)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lab_to_xyz(L: float, a: float, b: float) -> tuple[float, float, float]:
    xn, yn, zn = WHITE_D65
    fy = (L + 16) / 116
    return xn * _finv(fy + a / 500), yn * _finv(fy), zn * _finv(fy - b / 200)


def srgb8_to_lab(r8: int, g8: int, b8: int) -> tuple[float, float, float]:
    r, g, b = (srgb_to_linear(c / 255.0) for c in (r8, g8, b8))
    return xyz_to_lab(*linear_rgb_to_xyz(r, g, b))


def lab_to_linear_rgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    return xyz_to_linear_rgb(*lab_to_xyz(L, a, b))


def lch(a: float, b: float) -> tuple[float, float]:
    """(C*, h in degrees 0..360)."""
    return math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360.0


def delta_e2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float],
                kL: float = 1.0, kC: float = 1.0, kH: float = 1.0) -> float:
    """CIEDE2000 per Sharma, Wu and Dalal (2005), including their hue rules."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb**7 / (Cb**7 + 25.0**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = 0.0 if (a1p == 0 and b1 == 0) else math.degrees(math.atan2(b1, a1p)) % 360
    h2p = 0.0 if (a2p == 0 and b2 == 0) else math.degrees(math.atan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2))
    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    T = (1 - 0.17 * math.cos(math.radians(hbp - 30)) + 0.24 * math.cos(math.radians(2 * hbp))
         + 0.32 * math.cos(math.radians(3 * hbp + 6)) - 0.20 * math.cos(math.radians(4 * hbp - 63)))
    dtheta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * math.sqrt(Cbp**7 / (Cbp**7 + 25.0**7))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / math.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -math.sin(math.radians(2 * dtheta)) * Rc
    return math.sqrt((dLp / (kL * Sl)) ** 2 + (dCp / (kC * Sc)) ** 2 + (dHp / (kH * Sh)) ** 2
                     + Rt * (dCp / (kC * Sc)) * (dHp / (kH * Sh)))
```

`gauntlet/raster.py`:

```python
"""Minimal image containers and codecs for the evaluator. Pure stdlib.

`Raster8` holds interleaved 8-bit sRGB-encoded RGB (what a PPM export gives).
`RasterF` holds interleaved linear float RGB (what the synthetic chart is).
"""

from __future__ import annotations

import struct
import sys
from array import array
from dataclasses import dataclass


@dataclass(frozen=True)
class Raster8:
    width: int
    height: int
    data: bytes  # len == width * height * 3

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]


@dataclass(frozen=True)
class RasterF:
    width: int
    height: int
    data: array  # array('f'), len == width * height * 3, row 0 = top

    def pixel(self, x: int, y: int) -> tuple[float, float, float]:
        i = (y * self.width + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]


def _header_tokens(data: bytes, count: int) -> tuple[list[bytes], int]:
    out: list[bytes] = []
    i = 0
    while len(out) < count:
        while i < len(data) and data[i : i + 1].isspace():
            i += 1
        if i < len(data) and data[i : i + 1] == b"#":
            while i < len(data) and data[i : i + 1] not in (b"\n", b"\r"):
                i += 1
            continue
        start = i
        while i < len(data) and not data[i : i + 1].isspace():
            i += 1
        if start == i:
            raise ValueError("truncated header")
        out.append(data[start:i])
    return out, i + 1


def parse_ppm(data: bytes) -> Raster8:
    """Binary PPM (P6), maxval 255 or 65535 (reduced to 8 bits, high byte)."""
    (magic, w, h, maxval), off = _header_tokens(data, 4)
    if magic != b"P6":
        raise ValueError(f"not a binary PPM: {magic!r}")
    width, height, mv = int(w), int(h), int(maxval)
    stride = 1 if mv == 255 else 2 if mv == 65535 else 0
    if not stride:
        raise ValueError(f"unsupported maxval {mv}")
    need = width * height * 3 * stride
    payload = data[off : off + need]
    if len(payload) < need:
        raise ValueError(f"PPM payload short: {len(payload)} < {need}")
    return Raster8(width, height, payload[0::2] if stride == 2 else payload)


def encode_ppm(r: Raster8) -> bytes:
    return f"P6\n{r.width} {r.height}\n255\n".encode() + r.data


def encode_pfm(r: RasterF) -> bytes:
    """PFM colour ('PF'), little-endian (scale -1.0), rows stored bottom-to-top."""
    rows = []
    row_len = r.width * 3
    for y in range(r.height - 1, -1, -1):
        row = r.data[y * row_len : (y + 1) * row_len]
        if sys.byteorder != "little":
            row = array("f", row)
            row.byteswap()
        rows.append(row.tobytes())
    return f"PF\n{r.width} {r.height}\n-1.0\n".encode() + b"".join(rows)


def parse_pfm(data: bytes) -> RasterF:
    (magic, w, h, scale), off = _header_tokens(data, 4)
    if magic != b"PF":
        raise ValueError(f"not a colour PFM: {magic!r}")
    width, height = int(w), int(h)
    little = float(scale) < 0
    row_len = width * 3
    need = width * height * 3 * 4
    payload = data[off : off + need]
    if len(payload) < need:
        raise ValueError("PFM payload short")
    out = array("f")
    for y in range(height - 1, -1, -1):
        row = array("f")
        row.frombytes(payload[y * row_len * 4 : (y + 1) * row_len * 4])
        if little != (sys.byteorder == "little"):
            row.byteswap()
        out.extend(row)
    return RasterF(width, height, out)


def encode_tiff16(r: RasterF, *, clip: float = 1.0) -> bytes:
    """Uncompressed baseline RGB TIFF, 16 bits/sample, LINEAR values scaled by
    65535/clip. The chart's fallback when darktable rejects PFM (spec 4.3)."""
    w, h = r.width, r.height
    pix = array("H", (min(65535, max(0, int(round(v / clip * 65535)))) for v in r.data))
    if sys.byteorder != "little":
        pix.byteswap()
    body = pix.tobytes()
    entries = [
        (256, 3, 1, w), (257, 3, 1, h), (258, 3, 3, None), (259, 3, 1, 1),
        (262, 3, 1, 2), (273, 4, 1, None), (277, 3, 1, 3), (278, 3, 1, h),
        (279, 4, 1, len(body)), (284, 3, 1, 1),
    ]
    ifd_off = 8
    ifd_size = 2 + len(entries) * 12 + 4
    bps_off = ifd_off + ifd_size
    data_off = bps_off + 6
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd_off))
    out += struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        if tag == 258:
            out += struct.pack("<HHII", tag, typ, count, bps_off)
        elif tag == 273:
            out += struct.pack("<HHII", tag, typ, count, data_off)
        elif typ == 3:
            out += struct.pack("<HHIHH", tag, typ, count, value, 0)
        else:
            out += struct.pack("<HHII", tag, typ, count, value)
    out += struct.pack("<I", 0)
    out += struct.pack("<HHH", 16, 16, 16)
    out += body
    return bytes(out)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_color_raster.py -q`
Expected: `42 passed` (34 parametrised pairs + 8).

- [ ] **Step 5: Commit**

```bash
git add gauntlet/color.py gauntlet/raster.py gauntlet/tests/test_color_raster.py
git commit -m "gauntlet: colour science (CIEDE2000, Sharma 34 pairs) and PPM/PFM/TIFF rasters (Task 2, row 433)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 3: Synthetic chart

PARALLEL-OK. Depends on: Task 2.

**Files:**
- Create: `gauntlet/chart.py`
- Test: `gauntlet/tests/test_chart.py`

**Interfaces:**
- Consumes: `gauntlet.color`, `gauntlet.raster.Raster8/RasterF`.
- Produces: `chart.WIDTH=1536`, `HEIGHT=1152`, `VERSION="chart-v1"`, `CHROMATIC` (18 names), `Patch(name, x, y, w, h, linear)`, `layout() -> list[Patch]` (24 ColorChecker + 11 ramp `ramp_-5`..`ramp_+5`), `build() -> RasterF`, `sample(raster, inner=0.6) -> {name: Lab}`, `reference_labs()`, `patch_stats(raster) -> {"patch:<name>": {L,a,b,C,ev}, "chart_chromatic": {C}, "chart_tone": {slope}}`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from gauntlet import chart, color
from gauntlet.raster import Raster8, encode_pfm, parse_pfm


def test_layout_has_24_patches_and_an_11_step_ramp_inside_the_frame():
    pts = chart.layout()
    assert len(pts) == 35
    assert sum(p.name.startswith("ramp_") for p in pts) == 11
    for p in pts:
        assert 0 <= p.x and p.x + p.w <= chart.WIDTH
        assert 0 <= p.y and p.y + p.h <= chart.HEIGHT


def test_patches_do_not_overlap():
    pts = chart.layout()
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            assert (a.x + a.w <= b.x or b.x + b.w <= a.x or
                    a.y + a.h <= b.y or b.y + b.h <= a.y), (a.name, b.name)


def test_ramp_includes_the_looks_tone_points():
    names = {p.name: p.linear[0] for p in chart.layout()}
    assert names["ramp_+0"] == pytest.approx(0.18)
    assert names["ramp_-4"] == pytest.approx(0.18 / 16)
    assert names["ramp_+2"] == pytest.approx(0.72)


def test_build_samples_back_to_reference_labs():
    r = chart.build()
    got = chart.sample(r)
    ref = chart.reference_labs()
    for name, lab in ref.items():
        assert color.delta_e2000(got[name], lab) < 0.01, name


def test_pfm_round_trip_of_the_chart_is_exact():
    r = chart.build()
    assert parse_pfm(encode_pfm(r)).data == r.data


def test_sample_scales_to_a_smaller_8_bit_render():
    small_w, small_h = chart.WIDTH // 2, chart.HEIGHT // 2
    grey = color.linear_to_srgb(0.18)
    v = int(round(grey * 255))
    r8 = Raster8(small_w, small_h, bytes([v, v, v]) * (small_w * small_h))
    labs = chart.sample(r8)
    assert labs["red"][0] == pytest.approx(labs["ramp_+0"][0], abs=0.01)


def test_patch_stats_regions():
    st = chart.patch_stats(chart.build())
    assert st["patch:ramp_+0"]["ev"] == pytest.approx(0.18, abs=1e-4)
    assert abs(st["patch:ramp_+0"]["a"]) < 0.01
    assert st["chart_chromatic"]["C"] > 20
    assert st["chart_tone"]["slope"] > 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_chart.py -q`
Expected: `ImportError: cannot import name 'chart'`.

- [ ] **Step 3: Write the implementation**

```python
"""The synthetic chart (spec 4.3): 24 ColorChecker patches + an 11-step grey
ramp on an 18% grey surround, as linear Rec.709 float (PFM). Pure stdlib.

Patch reference values are the published post-2009 X-Rite sRGB (D65) 8-bit
values, converted to linear light. The chart is scene-referred input; every
look delta is measured against the untouched render of this same chart."""

from __future__ import annotations

from array import array
from dataclasses import dataclass

from gauntlet import color
from gauntlet.raster import Raster8, RasterF

WIDTH, HEIGHT = 1536, 1152
PATCH = 128
SURROUND = 0.18
VERSION = "chart-v1"

#: X-Rite ColorChecker Classic, post-November-2009 formulation, sRGB 8-bit.
COLORCHECKER_SRGB: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("dark_skin", (115, 82, 68)), ("light_skin", (194, 150, 130)),
    ("blue_sky", (98, 122, 157)), ("foliage", (87, 108, 67)),
    ("blue_flower", (133, 128, 177)), ("bluish_green", (103, 189, 170)),
    ("orange", (214, 126, 44)), ("purplish_blue", (80, 91, 166)),
    ("moderate_red", (193, 90, 99)), ("purple", (94, 60, 108)),
    ("yellow_green", (157, 188, 64)), ("orange_yellow", (224, 163, 46)),
    ("blue", (56, 61, 150)), ("green", (70, 148, 73)),
    ("red", (175, 54, 60)), ("yellow", (231, 199, 31)),
    ("magenta", (187, 86, 149)), ("cyan", (8, 133, 161)),
    ("white_9_5", (243, 243, 242)), ("neutral_8", (200, 200, 200)),
    ("neutral_6_5", (160, 160, 160)), ("neutral_5", (122, 122, 121)),
    ("neutral_3_5", (85, 85, 85)), ("black_2", (52, 52, 52)),
)
#: The 18 chromatic patches (the looks' saturation target set).
CHROMATIC = tuple(name for name, _ in COLORCHECKER_SRGB[:18])
#: Grey ramp exponents: 18% x 2^k.
RAMP_K = tuple(range(-5, 6))


@dataclass(frozen=True)
class Patch:
    name: str
    x: int
    y: int
    w: int
    h: int
    linear: tuple[float, float, float]


def layout() -> list[Patch]:
    patches: list[Patch] = []
    for i, (name, rgb8) in enumerate(COLORCHECKER_SRGB):
        row, col = divmod(i, 6)
        lin = tuple(color.srgb_to_linear(c / 255.0) for c in rgb8)
        patches.append(Patch(name, 128 + col * 192, 128 + row * 192, PATCH, PATCH, lin))
    for j, k in enumerate(RAMP_K):
        v = SURROUND * (2.0 ** k)
        patches.append(Patch(f"ramp_{k:+d}", 24 + j * 136, 896, PATCH, PATCH, (v, v, v)))
    return patches


def build() -> RasterF:
    data = array("f", [SURROUND]) * (WIDTH * HEIGHT * 3)
    for p in layout():
        for y in range(p.y, p.y + p.h):
            base = (y * WIDTH + p.x) * 3
            data[base : base + p.w * 3] = array("f", p.linear) * p.w
    return RasterF(WIDTH, HEIGHT, data)


def _inner(p: Patch, sx: float, sy: float, inner: float) -> tuple[int, int, int, int]:
    mx, my = p.w * (1 - inner) / 2, p.h * (1 - inner) / 2
    x0, y0 = int((p.x + mx) * sx), int((p.y + my) * sy)
    x1, y1 = int((p.x + p.w - mx) * sx), int((p.y + p.h - my) * sy)
    return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def sample(r: Raster8 | RasterF, inner: float = 0.6) -> dict[str, tuple[float, float, float]]:
    """Mean Lab of each patch's centre `inner` fraction, scaled to `r`'s size.
    Raster8 is sRGB-encoded (decoded first); RasterF is linear already."""
    sx, sy = r.width / WIDTH, r.height / HEIGHT
    lut = [color.srgb_to_linear(i / 255.0) for i in range(256)]
    out: dict[str, tuple[float, float, float]] = {}
    for p in layout():
        x0, y0, x1, y1 = _inner(p, sx, sy, inner)
        acc = [0.0, 0.0, 0.0]
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                px = r.pixel(x, y)
                if isinstance(r, Raster8):
                    px = (lut[px[0]], lut[px[1]], lut[px[2]])
                acc[0] += px[0]
                acc[1] += px[1]
                acc[2] += px[2]
                n += 1
        mean = (acc[0] / n, acc[1] / n, acc[2] / n)
        out[p.name] = color.xyz_to_lab(*color.linear_rgb_to_xyz(*mean))
    return out


def reference_labs() -> dict[str, tuple[float, float, float]]:
    return {p.name: color.xyz_to_lab(*color.linear_rgb_to_xyz(*p.linear)) for p in layout()}


def patch_stats(r: Raster8 | RasterF) -> dict[str, dict[str, float]]:
    """Per-patch regions for the evaluator: `patch:<name>` -> {L, a, b, C, ev}
    (ev = linear Y, so metrics.delta('ev') gives stops), plus
    `chart_chromatic` -> {C: mean C* of the 18 chromatic patches} and
    `chart_tone` -> {slope: L*(ramp_+2) - L*(ramp_-2)}."""
    labs = sample(r)
    out: dict[str, dict[str, float]] = {}
    for name, (L, a, b) in labs.items():
        c, _h = color.lch(a, b)
        _x, y, _z = color.lab_to_xyz(L, a, b)
        out[f"patch:{name}"] = {"L": L, "a": a, "b": b, "C": c, "ev": y}
    out["chart_chromatic"] = {"C": sum(out[f"patch:{n}"]["C"] for n in CHROMATIC) / len(CHROMATIC)}
    out["chart_tone"] = {"slope": out["patch:ramp_+2"]["L"] - out["patch:ramp_-2"]["L"]}
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_chart.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/chart.py gauntlet/tests/test_chart.py
git commit -m "gauntlet: synthetic ColorChecker + grey-ramp chart (Task 3, spec 4.3)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 4: Metrics (M1-M10), region stats, noise floor

PARALLEL-OK. Depends on: Tasks 2, 3.

**Files:**
- Create: `gauntlet/metrics.py`
- Test: `gauntlet/tests/test_metrics.py`

**Interfaces:**
- Produces: `REGIONS`, `STATS` (`ev, L, p01L, p99L, spread, clip_high, clip_low, rb, rg, bg, gm, a, b, C, noise, sharp`), `FLOOR_MIN`, `tolerance(unit, value)`, `param_equal(unit, a, b, tol=None)` (hue wraps), `Masks`, `build_masks(before: Raster8, max_samples=60000)`, `region_stats(raster, masks, regions=REGIONS) -> {region: {stat: value}}`, `delta(stat, before, after)` (`ev` in stops), `sharpness(raster)` (M8), `noise(raster)` (M7), `noise_floor(first, second) -> (floors, deterministic)`, `de_verdict(values) -> "pass"|"drift"|"fail"`, `patch_de(labs_a, labs_b)`.
- The measurement JSON's M1-M10 map onto these: M1 `tolerance/param_equal`; M2 `ev`/`L`; M3 `p01L/p99L/clip_*`; M4 `rb/rg/bg/gm` and chart `a/b`; M5 `C`; M6 `spread` (chart: `slope`); M7 `noise`; M8 `sharp`; M9 `patch_de` + `de_verdict`; M10 is `gauntlet.chart`.

- [ ] **Step 1: Write the failing test**

```python
import math

import pytest

from gauntlet import metrics
from gauntlet.raster import Raster8


def _flat(v, w=64, h=48, rgb=None):
    px = bytes(rgb or (v, v, v))
    return Raster8(w, h, px * (w * h))


def _gradient(w=64, h=48):
    data = bytearray()
    for y in range(h):
        for x in range(w):
            v = int(255 * x / (w - 1))
            data += bytes((v, v, v))
    return Raster8(w, h, bytes(data))


def test_masks_cover_expected_fractions():
    m = metrics.build_masks(_gradient())
    n = len(m.regions["frame"])
    assert n == 64 * 48
    assert len(m.regions["shadows"]) == n // 4
    assert len(m.regions["highlights"]) == n // 4
    assert len(m.regions["centre"]) == pytest.approx(n / 4, rel=0.05)
    assert 0 < len(m.regions["corners"]) < n * 0.05


def test_brighter_frame_has_positive_ev_and_L_delta():
    before, after = _flat(100), _flat(140)
    m = metrics.build_masks(before)
    sb = metrics.region_stats(before, m)["frame"]
    sa = metrics.region_stats(after, m)["frame"]
    assert metrics.delta("ev", sb["ev"], sa["ev"]) > 0.5
    assert metrics.delta("L", sb["L"], sa["L"]) > 10


def test_warmer_raises_rb_and_b():
    before = _flat(0, rgb=(120, 120, 120))
    after = _flat(0, rgb=(140, 120, 100))
    m = metrics.build_masks(before)
    sb, sa = (metrics.region_stats(x, m)["frame"] for x in (before, after))
    assert sa["rb"] > sb["rb"] and sa["b"] > sb["b"]


def test_clip_percentages():
    s = metrics.region_stats(_flat(255), metrics.build_masks(_flat(255)))["frame"]
    assert s["clip_high"] == 100.0 and s["clip_low"] == 0.0


def test_mask_size_mismatch_is_refused():
    with pytest.raises(ValueError):
        metrics.region_stats(_flat(1, 10, 10), metrics.build_masks(_flat(1)))


def test_ev_delta_of_zero_is_nan_not_crash():
    assert math.isnan(metrics.delta("ev", 0.0, 0.5))


def test_tolerances_follow_spec_5_1():
    assert metrics.tolerance("EV", 1.0) == 0.02
    assert metrics.tolerance("K", 5500) == 55.0
    assert metrics.tolerance("K", 3000) == 50.0
    assert metrics.param_equal("°", 359.5, 0.3)
    assert not metrics.param_equal("EV", 1.0, 1.05)
    assert metrics.param_equal("bool", 1, 1) and not metrics.param_equal("bool", 1, 0)


def test_noise_floor_minimums_and_non_determinism():
    s = {"frame": {"ev": 0.18, "L": 50.0}}
    floors, det = metrics.noise_floor(s, s)
    assert floors["ev"] == 0.02 and floors["L"] == 0.5 and det
    noisy = {"frame": {"ev": 0.18, "L": 52.0}}
    floors, det = metrics.noise_floor(s, noisy)
    assert floors["L"] == pytest.approx(6.0) and not det


def test_de_verdict_bands():
    assert metrics.de_verdict([0.5, 1.0]) == "pass"
    assert metrics.de_verdict([2.0, 2.0]) == "drift"  # mean over 2.3/3
    assert metrics.de_verdict([4.9]) == "drift"
    assert metrics.de_verdict([5.1]) == "fail"


def test_sharpness_and_noise_rank_images():
    flat = _flat(128)
    assert metrics.sharpness(flat) == 0.0
    assert metrics.sharpness(_gradient()) >= 0.0
    assert metrics.noise(flat) == 0.0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_metrics.py -q`
Expected: `ImportError: cannot import name 'metrics'`.

- [ ] **Step 3: Write the implementation**

```python
"""The measurement contract (spec 5, measurement JSON M1-M10). Pure stdlib.

Stats are computed on 8-bit sRGB rasters (a PPM export) over named regions.
Deltas are always after-minus-before on the SAME pixel set: region masks are
built from the before image and reused for the after image."""

from __future__ import annotations

import math
from dataclasses import dataclass

from gauntlet import color
from gauntlet.raster import Raster8

REGIONS = ("frame", "shadows", "highlights", "top", "centre", "corners")
STATS = ("ev", "L", "p01L", "p99L", "spread", "clip_high", "clip_low",
         "rb", "rg", "bg", "gm", "a", "b", "C", "noise", "sharp")
CLIP_HIGH, CLIP_LOW = 253, 2

#: Spec 5.2 minimum floors (never below these).
FLOOR_MIN = {"ev": 0.02, "L": 0.5, "p01L": 0.5, "p99L": 0.5, "spread": 0.5,
             "rb": 0.002, "gm": 0.002, "a": 0.5, "b": 0.5, "C": 0.5,
             "clip_high": 0.05, "clip_low": 0.05, "noise": 0.05, "sharp": 0.05,
             "de00": 0.2}

#: Spec 5.1 M1 tolerances by unit.
def tolerance(unit: str, value: float) -> float:
    if unit == "EV":
        return 0.02
    if unit == "K":
        return max(50.0, abs(value) * 0.01)
    if unit == "%":
        return 0.005  # 0.5 percentage points in internal (fraction) units
    if unit in ("°", "deg"):
        return 1.0
    if unit == "bool":
        return 0.0
    return 0.01  # ratio and anything unitless


def param_equal(unit: str, a: float | None, b: float | None, tol: float | None = None) -> bool:
    if a is None or b is None:
        return a is b
    t = tolerance(unit, b) if tol is None else tol
    if unit in ("°", "deg"):
        d = abs((a - b + 180.0) % 360.0 - 180.0)
        return d <= t
    return abs(a - b) <= t


@dataclass(frozen=True)
class Masks:
    """Pixel index lists per region, built once from the before image."""
    width: int
    height: int
    step: int
    regions: dict[str, list[int]]


_LUT = [color.srgb_to_linear(i / 255.0) for i in range(256)]


def _grid(width: int, height: int, max_samples: int) -> int:
    return max(1, int(math.sqrt(width * height / max_samples)))


def build_masks(before: Raster8, max_samples: int = 60_000) -> Masks:
    w, h = before.width, before.height
    step = _grid(w, h, max_samples)
    frame: list[int] = []
    top: list[int] = []
    centre: list[int] = []
    corners: list[int] = []
    Ls: list[tuple[float, int]] = []
    cw, ch = w * 0.1, h * 0.1
    for y in range(0, h, step):
        for x in range(0, w, step):
            idx = y * w + x
            frame.append(idx)
            if y < h / 3:
                top.append(idx)
            if w * 0.25 <= x < w * 0.75 and h * 0.25 <= y < h * 0.75:
                centre.append(idx)
            if (x < cw or x >= w - cw) and (y < ch or y >= h - ch):
                corners.append(idx)
            r, g, b = before.pixel(x, y)
            Ls.append((color.y_to_lstar(color.luminance(_LUT[r], _LUT[g], _LUT[b])), idx))
    Ls.sort()
    q = max(1, len(Ls) // 4)
    shadows = sorted(i for _, i in Ls[:q])
    highlights = sorted(i for _, i in Ls[-q:])
    return Masks(w, h, step, {"frame": frame, "shadows": shadows, "highlights": highlights,
                              "top": top, "centre": centre, "corners": corners})


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def region_stats(r: Raster8, masks: Masks, regions: tuple[str, ...] = REGIONS) -> dict:
    """{region: {stat: value}} for the requested regions."""
    if (r.width, r.height) != (masks.width, masks.height):
        raise ValueError(f"raster {r.width}x{r.height} does not match masks "
                         f"{masks.width}x{masks.height}")
    out: dict[str, dict[str, float]] = {}
    data = r.data
    for name in regions:
        idxs = masks.regions[name]
        n = len(idxs)
        if n == 0:
            out[name] = {s: float("nan") for s in STATS}
            continue
        sr = sg = sb = sy = sa = sbb = sc = 0.0
        hi = lo = 0
        Ls: list[float] = []
        for idx in idxs:
            i = idx * 3
            r8, g8, b8 = data[i], data[i + 1], data[i + 2]
            lr, lg, lb = _LUT[r8], _LUT[g8], _LUT[b8]
            sr += lr
            sg += lg
            sb += lb
            yv = color.luminance(lr, lg, lb)
            sy += yv
            L, a, b = color.xyz_to_lab(*color.linear_rgb_to_xyz(lr, lg, lb))
            Ls.append(L)
            sa += a
            sbb += b
            sc += math.hypot(a, b)
            if r8 >= CLIP_HIGH or g8 >= CLIP_HIGH or b8 >= CLIP_HIGH:
                hi += 1
            if r8 <= CLIP_LOW and g8 <= CLIP_LOW and b8 <= CLIP_LOW:
                lo += 1
        Ls.sort()
        out[name] = {
            "ev": sy / n,
            "L": sum(Ls) / n,
            "p01L": _pct(Ls, 0.01),
            "p99L": _pct(Ls, 0.99),
            "spread": _pct(Ls, 0.95) - _pct(Ls, 0.05),
            "clip_high": 100.0 * hi / n,
            "clip_low": 100.0 * lo / n,
            "rb": (sr / sb) if sb else float("nan"),
            "rg": (sr / sg) if sg else float("nan"),
            "bg": (sb / sg) if sg else float("nan"),
            "gm": (sg / ((sr + sb) / 2)) if (sr + sb) else float("nan"),
            "a": sa / n,
            "b": sbb / n,
            "C": sc / n,
        }
    if "frame" in out:
        out["frame"]["sharp"] = sharpness(r)
        out["frame"]["noise"] = noise(r)
    return out


def delta(stat: str, before: float, after: float) -> float:
    """after - before; for `ev` the stops ratio log2(after/before) of linear means."""
    if stat == "ev":
        if before <= 0 or after <= 0:
            return float("nan")
        return math.log2(after / before)
    return after - before


def _green(r: Raster8, x: int, y: int) -> int:
    return r.data[(y * r.width + x) * 3 + 1]


def sharpness(r: Raster8, step: int = 4) -> float:
    """M8: variance of the 3x3 discrete Laplacian of green, sampled every `step`."""
    vals: list[float] = []
    for y in range(1, r.height - 1, step):
        for x in range(1, r.width - 1, step):
            lap = (4 * _green(r, x, y) - _green(r, x - 1, y) - _green(r, x + 1, y)
                   - _green(r, x, y - 1) - _green(r, x, y + 1))
            vals.append(float(lap))
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def noise(r: Raster8, block: int = 16) -> float:
    """M7: median std-dev of green over the flattest 10% of `block`x`block` tiles."""
    sds: list[tuple[float, float]] = []
    for by in range(0, r.height - block + 1, block):
        for bx in range(0, r.width - block + 1, block):
            vals = [_green(r, x, y) for y in range(by, by + block, 2)
                    for x in range(bx, bx + block, 2)]
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
            grad = abs(_green(r, bx, by) - _green(r, bx + block - 1, by + block - 1))
            sds.append((grad + sd, sd))
    if not sds:
        return 0.0
    sds.sort()
    flat = [sd for _, sd in sds[: max(1, len(sds) // 10)]]
    flat.sort()
    return flat[len(flat) // 2]


def noise_floor(first: dict, second: dict) -> tuple[dict[str, float], bool]:
    """Spec 5.2: floor = max(3 x |repeat difference|, minimum) per stat, from two
    exports of the untouched frame. Returns (floors, deterministic)."""
    floors: dict[str, float] = {}
    deterministic = True
    for stat, minimum in FLOOR_MIN.items():
        if stat == "de00":
            continue
        a = first.get("frame", {}).get(stat)
        b = second.get("frame", {}).get(stat)
        if a is None or b is None or any(map(math.isnan, (a, b))):
            floors[stat] = minimum
            continue
        observed = abs(delta(stat, a, b)) * 3.0
        floors[stat] = max(observed, minimum)
        if floors[stat] > 2 * minimum:
            deterministic = False
    floors["de00"] = FLOOR_MIN["de00"]
    return floors, deterministic


def de_verdict(values: list[float]) -> str:
    """Spec 5.1 'should not change': max < 2.3 and mean < 2.3/3 -> pass;
    max <= 5 -> drift; else fail."""
    if not values:
        return "pass"
    mx, mean = max(values), sum(values) / len(values)
    if mx < 2.3 and mean < 2.3 / 3:
        return "pass"
    if mx <= 5.0:
        return "drift"
    return "fail"


def patch_de(before: dict, after: dict) -> dict[str, float]:
    return {k: color.delta_e2000(before[k], after[k]) for k in before if k in after}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_metrics.py -q`
Expected: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/metrics.py gauntlet/tests/test_metrics.py
git commit -m "gauntlet: metrics M1-M10, region stats and the noise floor (Task 4, spec 5)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 5: Evaluator

PARALLEL-OK. Depends on: Tasks 1, 4.

**Files:**
- Create: `gauntlet/evaluate.py`
- Test: `gauntlet/tests/test_evaluate.py`

**Interfaces:**
- Consumes: `Case/Check`, `CallRecord/Observation`, `gauntlet.metrics`.
- Produces: `Policy(refusal_codes, transient_codes, refusal_markers, claim_markers)`; `evaluate(case, obs, policy) -> Verdict(score, outcome, checks, silent_failure, notes)` with `Verdict.to_dict()`; `OUTCOMES`, `POINTS`, `ALIASES` (`reported_ok_unverified` -> `silent_failure`), `canonical_outcome(label)`; `silent_failure(case, obs, results, policy) -> str|None`.
- Check kinds understood (the adapter's generators rely on these exact names): M1 `equals|within|delta` (target `key, value|delta, unit, tol, step, from, to`), `changed` (sign, `from`, `to`), `restored|unchanged` (`keys`, `units`, `from`), `step_equal` (`key|keys, a, b`), `step_ratio` (`key, base, double, ratio, tol`), `monotonic` (`key, pairs`); stat metrics M2-M8 on `obs.stats_*[region][stat]` with `sign`, `kind` `delta|unchanged|frac`, optional numeric `target {value, tol}`; `M9` chart ΔE from `obs.extra["patch_de"]`; `envelope` and `extra` checks with ops `eq, contains, exists, truthy, abs_within, in, gte, regex, len_eq, len_gte`; `file`; `text`. Check tag `silent_if_fail` turns a failed check into a silent failure; case meta `accept_refusal`, `allow_heal`, `substitute_ok`, `partial_min` tune the verdict.

- [ ] **Step 1: Write the failing test**

```python

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check, Expect, Step
from gauntlet.evaluate import POINTS, Policy, canonical_outcome, evaluate

POLICY = Policy(
    refusal_codes=frozenset({"usage", "value_unknown", "not_pinned"}),
    transient_codes=frozenset({"gui_not_ready", "bridge_timeout", "commit_timeout"}),
    refusal_markers=("can't", "cannot", "not able"),
    claim_markers=(r"\bdone\b", r"\bi've\b"),
)


def _ok(data=None, **kw):
    return {"ok": True, "outcome": "ok", "data": data or {}, **kw}


def _err(code, fix="try x instead"):
    return {"ok": False, "outcome": f"error:{code}", "error": {"code": code, "fix": fix,
                                                                "message": "no"}}


def _snap(fp, **params):
    return {"fingerprint": fp, "history_end": 1, "params": params}


def _edit_case(checks=None, sidecar=None, kind="edit", mode="tool", steps=None, meta=None):
    return Case(id="c", layer="coverage", tier="P0", mode=mode,
                steps=steps or [Step("darkroom_set", {}, budget_s=3.5, write=True)],
                expect=Expect(kind, checks=checks or [], sidecar=sidecar),
                meta=meta or {})


M1_SET = Check("M1", kind="equals", target={"key": "exposure.exposure", "value": 1.0,
                                            "unit": "EV"})
SIDECAR = {"changed": True, "key": "exposure.exposure", "units": {"exposure.exposure": "EV"}}


def _obs(env, before, after, wall=1.0, **kw):
    return Observation(calls=[CallRecord("darkroom_set", {}, env, wall_s=wall, write=True)],
                       before=before, after=after, **kw)


def test_verified_set():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                      _snap("b", **{"exposure.exposure": 1.0})), POLICY)
    assert (v.score, v.outcome) == ("pass", "verified")


def test_silent_failure_a_sidecar_unchanged():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                      _snap("a", **{"exposure.exposure": 0.7})), POLICY)
    assert v.outcome == "silent_failure" and v.silent_failure.startswith("a:")
    assert POINTS[v.score] == 0.0


def test_silent_failure_b_wrong_param_moved():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7, "sigmoid.contrast": 1.5}),
                      _snap("b", **{"exposure.exposure": 0.7, "sigmoid.contrast": 1.8})), POLICY)
    assert v.silent_failure.startswith("b:")


def test_silent_failure_c_stat_wrong_direction():
    chk = Check("M2", stat="L", region="frame", sign=1)
    obs = _obs(_ok(), _snap("a"), _snap("b"),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 45.0}})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.silent_failure.startswith("c:")


def test_below_floor_is_silent_c_not_partial():
    chk = Check("M2", stat="L", region="frame", sign=1)
    obs = _obs(_ok(), _snap("a"), _snap("b"),
               stats_before={"frame": {"L": 50.0}}, stats_after={"frame": {"L": 50.1}},
               floors={"L": 0.5})
    assert evaluate(_edit_case([chk], {"changed": True}), obs, POLICY).outcome == "silent_failure"


def test_silent_failure_d_bulk_counts():
    obs = _obs(_ok(), _snap("a"), _snap("b"), extra={"reported_n": 7, "changed_n": 6,
                                                     "verified_n": 7})
    v = evaluate(_edit_case([], {"changed": True}, kind="workflow"), obs, POLICY)
    assert v.silent_failure.startswith("d:")


def test_silent_failure_e_undo_not_restored():
    chk = Check("M1", kind="restored", target={"keys": ["exposure.exposure"], "unit": "EV"})
    obs = Observation(
        calls=[CallRecord("darkroom_undo_last", {}, _ok(), write=True)],
        before=_snap("a", **{"exposure.exposure": 0.7}),
        after=_snap("b", **{"exposure.exposure": 1.0}))
    v = evaluate(_edit_case([chk], None, kind="workflow",
                            steps=[Step("darkroom_undo_last", write=True)]), obs, POLICY)
    assert v.silent_failure.startswith("e:")


def test_silent_failure_f_noop_but_changed():
    env = _ok({"noop": True})
    v = evaluate(_edit_case([], {"changed": False}),
                 _obs(env, _snap("a"), _snap("b")), POLICY)
    assert v.silent_failure.startswith("f:")


def test_wrong_refusal_of_an_in_vocabulary_edit():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_err("usage"), _snap("a"), _snap("a")), POLICY)
    assert v.outcome == "wrong_refusal"


def test_honest_tool_refusal_passes():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    v = evaluate(case, _obs(_err("usage"), _snap("a"), _snap("a")), POLICY)
    assert (v.score, v.outcome) == ("pass", "correct_refusal")


def test_refusal_with_half_applied_edit_fails():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    v = evaluate(case, _obs(_err("usage"), _snap("a"), _snap("b")), POLICY)
    assert v.score == "fail"


def test_refusal_code_outside_the_set_fails_and_transient_blocks():
    case = _edit_case(kind="refuse", steps=[Step("darkroom_say", write=True)])
    assert evaluate(case, _obs(_err("error"), _snap("a"), _snap("a")), POLICY).outcome == "failed"
    assert evaluate(case, _obs(_err("gui_not_ready"), _snap("a"), _snap("a")),
                    POLICY).outcome == "blocked"


def test_agent_refusal_rules():
    case = _edit_case(kind="refuse", mode="agent", steps=[])
    base = dict(calls=[], before=_snap("a"), after=_snap("a"))
    ok = evaluate(case, Observation(agent_text="I can't do that here.", **base), POLICY)
    assert ok.outcome == "correct_refusal"
    both = evaluate(case, Observation(agent_text="I can't, but done.", **base), POLICY)
    assert both.outcome == "needs_review" and both.score == "partial"
    neither = evaluate(case, Observation(agent_text="Sure.", **base), POLICY)
    assert neither.outcome == "needs_review"
    wrote = evaluate(case, Observation(agent_text="I can't.", calls=[], before=_snap("a"),
                                       after=_snap("b")), POLICY)
    assert wrote.outcome == "unexpected_edit"


def test_agent_claim_without_change_is_silent():
    case = _edit_case([], {"changed": True}, mode="agent", steps=[])
    v = evaluate(case, Observation(agent_text="Done, I've brightened it.", before=_snap("a"),
                                   after=_snap("a")), POLICY)
    assert v.outcome == "silent_failure"


def test_typed_error_expected():
    case = _edit_case(kind="typed_error", steps=[Step("darkroom_set", write=True)])
    case.expect.codes = ["value_unknown"]
    assert evaluate(case, _obs(_err("value_unknown"), _snap("a"), _snap("a")),
                    POLICY).outcome == "verified"
    assert evaluate(case, _obs(_ok(), _snap("a"), _snap("b")), POLICY).outcome == "failed"


def test_crash_and_block_and_heal():
    case = _edit_case([M1_SET], SIDECAR)
    assert evaluate(case, Observation(crash_reports=["x.ips"]), POLICY).outcome == "crash"
    assert evaluate(case, Observation(blocked="hang_twice"), POLICY).outcome == "failed_hang"
    assert evaluate(case, Observation(blocked="launch"), POLICY).outcome == "blocked"
    healed = _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                  _snap("b", **{"exposure.exposure": 1.0}), healed=True)
    assert evaluate(case, healed, POLICY).outcome == "failed"


def test_over_budget_is_a_pass():
    v = evaluate(_edit_case([M1_SET], SIDECAR),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                      _snap("b", **{"exposure.exposure": 1.0}), wall=9.0), POLICY)
    assert (v.score, v.outcome) == ("pass", "over_budget")


def test_collateral_is_partial():
    side = dict(SIDECAR, collateral_check=True)
    v = evaluate(_edit_case([M1_SET], side),
                 _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7, "exposure.black": 0.0}),
                      _snap("b", **{"exposure.exposure": 1.0, "exposure.black": 0.05})),
                 POLICY)
    assert v.outcome == "collateral" and v.score == "partial"


def test_right_direction_wrong_magnitude_is_partial():
    chk = Check("M2", stat="ev", region="frame", sign=1, target={"value": 1.0, "tol": 0.15})
    obs = _obs(_ok(), _snap("a"), _snap("b"),
               stats_before={"frame": {"ev": 0.1}}, stats_after={"frame": {"ev": 0.141}})
    v = evaluate(_edit_case([chk], {"changed": True}), obs, POLICY)
    assert v.outcome == "partial"


def test_property_kinds():
    snaps = [{"k": 0.0}, {"k": 0.3}, {"k": 0.3}, {"k": 0.9}]
    obs = Observation(calls=[CallRecord("s", {}, _ok(), write=True)] * 3,
                      before=_snap("a"), after=_snap("b"), extra={"snapshots": snaps})
    checks = [
        Check("M1", kind="step_equal", target={"key": "k", "a": 1, "b": 2}),
        Check("M1", kind="step_ratio", target={"key": "k", "base": [0, 1], "double": [0, 3],
                                               "ratio": 3.0}),
        Check("M1", kind="monotonic", target={"key": "k", "pairs": [[0, 1], [0, 3]]}),
    ]
    case = _edit_case(checks, None, kind="property",
                      steps=[Step("s", write=True)] * 3)
    v = evaluate(case, obs, POLICY)
    assert v.outcome == "verified", v


def test_hue_wrap_equality():
    chk = Check("M1", kind="equals", target={"key": "cb.h", "value": 10.0, "unit": "°"})
    obs = _obs(_ok(), _snap("a", **{"cb.h": 350.0}), _snap("b", **{"cb.h": 370.4 - 360}))
    assert evaluate(_edit_case([chk], {"changed": True}), obs, POLICY).outcome == "verified"


def test_legacy_alias():
    assert canonical_outcome("reported_ok_unverified") == "silent_failure"


def test_boundary_set_accepts_a_typed_refusal_but_not_a_silent_clamp():
    clamp = Check("envelope", target={"path": "data.clamped", "op": "truthy"},
                  tags={"silent_if_fail": True, "why": "clamp not reported"})
    chk = Check("M1", kind="equals", target={"key": "exposure.exposure", "value": -3.0,
                                             "unit": "EV"})
    case = _edit_case([chk, clamp], SIDECAR, meta={"accept_refusal": True})
    refused = evaluate(case, _obs(_err("usage"), _snap("a"), _snap("a")), POLICY)
    assert refused.outcome == "correct_refusal"
    silent = evaluate(case, _obs(_ok(), _snap("a", **{"exposure.exposure": 0.7}),
                                 _snap("b", **{"exposure.exposure": -3.0})), POLICY)
    assert silent.outcome == "silent_failure" and "clamp" in silent.silent_failure
    reported = evaluate(case, _obs(_ok({"clamped": True}), _snap("a", **{"exposure.exposure": 0.7}),
                                   _snap("b", **{"exposure.exposure": -3.0})), POLICY)
    assert reported.outcome == "verified"


def test_extra_and_envelope_ops():
    obs = Observation(calls=[CallRecord("t", {}, _ok({"n": 3, "k": "warmer"}))],
                      extra={"offline_ok": True, "count": 5})
    checks = [Check("extra", target={"path": "offline_ok", "op": "truthy"}),
              Check("extra", target={"path": "count", "op": "gte", "value": 5}),
              Check("envelope", target={"path": "data.k", "op": "in", "value": ["warmer"]}),
              Check("envelope", target={"path": "data.k", "op": "regex", "value": "^warm"}),
              Check("envelope", target={"path": "data.n", "op": "abs_within", "value": 3.2,
                                        "tol": 0.5})]
    case = _edit_case(checks, None, kind="workflow", steps=[Step("t")])
    assert evaluate(case, obs, POLICY).outcome == "verified"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_evaluate.py -q`
Expected: `ModuleNotFoundError: No module named 'gauntlet.evaluate'`.

- [ ] **Step 3: Write the implementation**

```python
"""The evaluator (spec 2.4, 5.3, 5.4 and 1's silent-failure definition).

Pure: `evaluate(case, observation, policy)` -> `Verdict`. The product adapter
supplies the observation (snapshots, stats, envelopes, agent text) and the
policy (refusal codes, transient codes, text markers)."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from gauntlet import metrics
from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case, Check

PASS, PARTIAL, FAIL = "pass", "partial", "fail"
OUTCOMES = {
    "verified": PASS, "correct_refusal": PASS, "over_budget": PASS,
    "partial": PARTIAL, "collateral": PARTIAL, "drift": PARTIAL, "needs_review": PARTIAL,
    "silent_failure": FAIL, "wrong_refusal": FAIL, "unexpected_edit": FAIL,
    "failed": FAIL, "failed_hang": FAIL, "crash": FAIL, "blocked": FAIL,
}
#: Old benchmark labels still readable (spec 1: renamed, alias kept).
ALIASES = {"reported_ok_unverified": "silent_failure", "honest_refusal": "wrong_refusal"}
POINTS = {PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}
NOOP_OUTCOMES = ("noop", "dry_run")


@dataclass(frozen=True)
class Policy:
    refusal_codes: frozenset[str]
    transient_codes: frozenset[str]
    refusal_markers: tuple[str, ...]
    claim_markers: tuple[str, ...]


@dataclass
class CheckResult:
    index: int
    metric: str
    ok: bool
    measured: object = None
    expected: object = None
    wrong_direction: bool = False
    magnitude_only: bool = False
    note: str = ""


@dataclass
class Verdict:
    score: str
    outcome: str
    checks: list[CheckResult] = field(default_factory=list)
    silent_failure: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def canonical_outcome(label: str) -> str:
    return ALIASES.get(label, label)


def _num(v: object) -> float | None:
    if isinstance(v, bool) or v is None:
        return None if v is None else float(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _params(snap: dict) -> dict:
    return snap.get("params", {}) if snap else {}


def _env_ok(c: CallRecord) -> bool:
    env = c.envelope or {}
    return bool(env.get("ok")) and env.get("outcome", "ok") not in NOOP_OUTCOMES \
        and not (env.get("data") or {}).get("noop")


def _code(c: CallRecord) -> str | None:
    env = c.envelope or {}
    if env.get("ok"):
        return None
    err = env.get("error") or {}
    return err.get("code") or c.error


def _dig(obj: object, dotted: str) -> object:
    for part in dotted.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


# ---------------------------------------------------------------- checks


def _check_m1(i: int, ch: Check, obs: Observation) -> CheckResult:
    t = ch.target or {}
    unit = t.get("unit", "ratio")
    snaps = obs.extra.get("snapshots") or [_params(obs.before), _params(obs.after)]
    before, after = snaps[0], snaps[-1]
    kind = ch.kind
    if kind in ("delta", "equals", "within") and "value" in t:
        got = _num(after.get(t["key"]))
        ok = got is not None and metrics.param_equal(unit, got, float(t["value"]), t.get("tol"))
        return CheckResult(i, "M1", ok, got, t["value"])
    if kind == "delta" and "delta" in t:
        b = _num(snaps[t.get("from", 0)].get(t["key"]))
        a = _num(snaps[t.get("to", -1)].get(t["key"]))
        if a is None or b is None:
            return CheckResult(i, "M1", False, None, t["delta"], note="param unreadable")
        d = a - b
        if unit in ("°", "deg"):
            d = (d + 180.0) % 360.0 - 180.0
        tol = t.get("tol", abs(t.get("step", t["delta"])) * 0.1)
        ok = abs(d - t["delta"]) <= tol
        wrong = (d * t["delta"]) < 0
        return CheckResult(i, "M1", ok, d, t["delta"], wrong_direction=wrong,
                           magnitude_only=not ok and not wrong and abs(d) > 0)
    if kind == "changed":
        b = _num(snaps[t.get("from", 0)].get(t["key"]))
        a = _num(snaps[t.get("to", -1)].get(t["key"]))
        ok = a is not None and not metrics.param_equal(unit, a, b)
        if ok and ch.sign:
            ok = (a - (b or 0.0)) * ch.sign > 0
        return CheckResult(i, "M1", ok, a, "changed")
    if kind in ("restored", "unchanged"):
        before = snaps[t.get("from", 0)]
        keys = t.get("keys") or sorted(set(before) | set(after))
        units = t.get("units") or (obs.before or {}).get("units", {})
        moved = [k for k in keys
                 if not metrics.param_equal(units.get(k, unit), _num(after.get(k)),
                                            _num(before.get(k)))]
        return CheckResult(i, "M1", not moved, moved, [], note=kind)
    if kind == "step_equal":
        keys = t.get("keys") or [t["key"]]
        a, b = snaps[t["a"]], snaps[t["b"]]
        moved = [k for k in keys if not metrics.param_equal(unit, _num(a.get(k)), _num(b.get(k)))]
        return CheckResult(i, "M1", not moved, moved, [], note="step_equal")
    if kind == "step_ratio":
        k = t["key"]

        def d(pair: list[int]) -> float | None:
            x, y = _num(snaps[pair[0]].get(k)), _num(snaps[pair[1]].get(k))
            return None if x is None or y is None else y - x

        base, dbl = d(t["base"]), d(t["double"])
        if not base or dbl is None:
            return CheckResult(i, "M1", False, (base, dbl), t["ratio"], note="no base delta")
        r = dbl / base
        return CheckResult(i, "M1", abs(r - t["ratio"]) <= t.get("tol", 0.1) * t["ratio"],
                           r, t["ratio"])
    if kind == "monotonic":
        k = t["key"]
        mags = []
        for pair in t["pairs"]:
            x, y = _num(snaps[pair[0]].get(k)), _num(snaps[pair[1]].get(k))
            mags.append(None if x is None or y is None else abs(y - x))
        ok = None not in mags and all(a < b for a, b in zip(mags, mags[1:]))
        return CheckResult(i, "M1", ok, mags, "strictly increasing")
    return CheckResult(i, "M1", False, None, None, note=f"unknown M1 kind {kind!r}")


def _check_stat(i: int, ch: Check, obs: Observation) -> CheckResult:
    sb = (obs.stats_before.get(ch.region) or {}).get(ch.stat or "")
    sa = (obs.stats_after.get(ch.region) or {}).get(ch.stat or "")
    if sb is None or sa is None:
        return CheckResult(i, ch.metric, False, None, ch.sign, note="stat missing")
    d = (sa / sb - 1.0) if ch.kind == "frac" and sb else metrics.delta(ch.stat or "", sb, sa)
    if math.isnan(d):
        return CheckResult(i, ch.metric, False, d, ch.sign, note="stat not finite")
    floor = obs.floors.get(ch.stat or "", metrics.FLOOR_MIN.get(ch.stat or "", 0.0))
    if ch.kind == "unchanged" or ch.sign == 0:
        return CheckResult(i, ch.metric, abs(d) <= floor, d, 0)
    wrong = d * ch.sign < -floor
    ok = d * ch.sign > floor
    t = ch.target or {}
    if ok and "value" in t:
        within = abs(d - t["value"]) <= t.get("tol", 0.0)
        return CheckResult(i, ch.metric, within, d, t["value"], magnitude_only=not within)
    return CheckResult(i, ch.metric, ok, d, ch.sign, wrong_direction=wrong,
                       note="" if ok or wrong else "below noise floor")


def _check_chart(i: int, ch: Check, obs: Observation) -> CheckResult:
    des = obs.extra.get("patch_de") or []
    v = metrics.de_verdict(list(des))
    return CheckResult(i, "M9", v == "pass", max(des) if des else 0.0, "<2.3",
                       magnitude_only=v == "drift", note=v)


def _check_envelope(i: int, ch: Check, obs: Observation) -> CheckResult:
    t = ch.target or {}
    if ch.metric == "extra":
        got = _dig(obs.extra, t.get("path", ""))
    else:
        idx = ch.step if ch.step is not None else -1
        if not obs.calls or abs(idx) > len(obs.calls) or idx >= len(obs.calls):
            return CheckResult(i, "envelope", False, None, t, note="no such step")
        got = _dig(obs.calls[idx].envelope or {}, t.get("path", "ok"))
    op = t.get("op", "eq")
    if op == "eq":
        ok = got == t.get("value")
    elif op == "contains":
        ok = isinstance(got, (str, list)) and str(t["value"]).lower() in \
            (got.lower() if isinstance(got, str) else [str(x).lower() for x in got])
    elif op == "exists":
        ok = got is not None
    elif op == "truthy":
        ok = bool(got)
    elif op == "regex":
        ok = isinstance(got, str) and re.search(str(t["value"]), got) is not None
    elif op == "len_eq":
        ok = isinstance(got, list) and len(got) == int(t["value"])
    elif op == "len_gte":
        ok = isinstance(got, list) and len(got) >= int(t["value"])
    elif op == "in":
        ok = got in (t.get("value") or [])
    elif op == "gte":
        g = _num(got)
        ok = g is not None and g >= float(t["value"])
    elif op == "abs_within":
        g = _num(got)
        ok = g is not None and abs(g - float(t["value"])) <= float(t["tol"])
    else:
        ok = False
    return CheckResult(i, ch.metric, ok, got, t.get("value"))


def _check_file(i: int, ch: Check, obs: Observation) -> CheckResult:
    name = (ch.target or {}).get("name", "")
    files = obs.extra.get("files") or {}
    return CheckResult(i, "file", bool(files.get(name)), files.get(name), True)


def _check_text(i: int, ch: Check, obs: Observation) -> CheckResult:
    pat = (ch.target or {}).get("regex", "")
    text = obs.agent_text or obs.extra.get("stdout") or ""
    found = re.search(pat, text, re.I) is not None
    want = (ch.target or {}).get("present", True)
    return CheckResult(i, "text", found == want, found, want)


def run_check(i: int, ch: Check, obs: Observation) -> CheckResult:
    if ch.metric == "M1":
        return _check_m1(i, ch, obs)
    if ch.metric in ("M2", "M3", "M4", "M5", "M6", "M7", "M8"):
        return _check_stat(i, ch, obs)
    if ch.metric == "M9":
        return _check_chart(i, ch, obs)
    if ch.metric in ("envelope", "extra"):
        return _check_envelope(i, ch, obs)
    if ch.metric == "file":
        return _check_file(i, ch, obs)
    if ch.metric == "text":
        return _check_text(i, ch, obs)
    return CheckResult(i, ch.metric, False, note=f"unknown metric {ch.metric!r}")


# ---------------------------------------------------------------- verdict


def _fingerprint_changed(obs: Observation) -> bool:
    return (obs.before or {}).get("fingerprint") != (obs.after or {}).get("fingerprint")


def _changed_keys(obs: Observation, units: dict) -> list[str]:
    b, a = _params(obs.before), _params(obs.after)
    return sorted(k for k in set(a) | set(b)
                  if not metrics.param_equal(units.get(k, "ratio"), _num(a.get(k)), _num(b.get(k))))


def _claims(text: str, markers: tuple[str, ...]) -> bool:
    return any(re.search(m, text, re.I) for m in markers)


def _refuses(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


def silent_failure(case: Case, obs: Observation, results: list[CheckResult],
                   policy: Policy) -> str | None:
    """Spec 1 conditions (a)-(f). Only asked when the product reported success."""
    writes = [c for c in obs.calls if c.write]
    reported = any(_env_ok(c) for c in writes)
    if case.mode == "agent" and obs.agent_text:
        reported = reported or _claims(obs.agent_text, policy.claim_markers)
    noop_calls = [c for c in writes if (c.envelope or {}).get("ok")
                  and ((c.envelope or {}).get("data") or {}).get("noop")]
    if noop_calls and _fingerprint_changed(obs):
        return "f: answered noop but the sidecar changed"
    if noop_calls and case.expect.kind == "edit" and (case.expect.sidecar or {}).get("changed"):
        return "f: answered noop for a request that needed a change"
    if not reported:
        return None
    sidecar = case.expect.sidecar or {}
    if sidecar.get("changed") and not _fingerprint_changed(obs):
        return "a: reported ok but the sidecar history did not change"
    units = sidecar.get("units", {})
    key = sidecar.get("key")
    if key:
        changed = _changed_keys(obs, units)
        if changed and key not in changed:
            return f"b: reported {key} but {', '.join(changed)} changed"
        if sidecar.get("changed") and key not in changed:
            return f"a: reported ok but {key} did not change"
    for r, ch in zip(results, case.expect.checks):
        if ch.tags.get("silent_if_fail") and not r.ok:
            return f"c: {ch.tags.get('why', 'a required report is missing')}"
        if ch.metric in ("M2", "M3", "M4", "M5", "M6") and ch.sign and not r.ok \
                and not r.magnitude_only:
            return f"c: {ch.metric} {ch.stat} moved {r.measured!r}, expected sign {ch.sign}"
        if ch.kind == "restored" and not r.ok and any(c.tool.endswith("undo_last") for c in writes):
            return f"e: undo reported ok but {r.measured} were not restored"
    rep, chg, ver = (obs.extra.get(k) for k in ("reported_n", "changed_n", "verified_n"))
    if isinstance(rep, int) and ((isinstance(chg, int) and chg < rep) or
                                 (isinstance(ver, int) and ver < rep)):
        return f"d: reported {rep} frames, {chg} sidecars changed, {ver} exports verified"
    return None


def _honest_tool_refusal(obs: Observation, policy: Policy) -> tuple[bool, str]:
    refused = [c for c in obs.calls if not (c.envelope or {}).get("ok")]
    if not refused:
        return False, "no step refused"
    c = refused[0]
    code = _code(c)
    if code in policy.transient_codes:
        return False, f"transient {code}"
    if code not in policy.refusal_codes:
        return False, f"code {code!r} is not a refusal code"
    err = (c.envelope or {}).get("error") or {}
    if not (err.get("fix") or err.get("message")):
        return False, "refusal names nothing"
    if _fingerprint_changed(obs):
        return False, "refused but the sidecar changed (half-applied edit)"
    return True, code or ""


def _transient_block(obs: Observation, policy: Policy) -> str | None:
    for c in obs.calls:
        if _code(c) in policy.transient_codes or c.error == "timeout":
            return _code(c) or "timeout"
    return None


def evaluate(case: Case, obs: Observation, policy: Policy) -> Verdict:
    if obs.crash_reports:
        return Verdict(FAIL, "crash", notes=list(obs.crash_reports))
    if obs.blocked:
        outcome = "failed_hang" if obs.blocked == "hang_twice" else "blocked"
        return Verdict(FAIL, outcome, notes=[obs.blocked, *obs.hang_evidence])
    notes: list[str] = []
    if obs.healed and not case.meta.get("allow_heal"):
        return Verdict(FAIL, "failed", notes=["watchdog heal inside a case (spec 6.4)"])
    results = [run_check(i, ch, obs) for i, ch in enumerate(case.expect.checks)]
    kind = case.expect.kind

    if kind == "refuse":
        if case.mode == "agent":
            text = obs.agent_text or ""
            refuses = _refuses(text, policy.refusal_markers)
            claims = _claims(text, policy.claim_markers)
            wrote = _fingerprint_changed(obs)
            if wrote and not case.meta.get("substitute_ok"):
                return Verdict(FAIL, "unexpected_edit", results, notes=["sidecar changed"])
            if refuses and not claims:
                return Verdict(PASS, "correct_refusal", results)
            return Verdict(PARTIAL, "needs_review", results,
                           notes=[f"refusal marker={refuses} claim marker={claims}"])
        honest, why = _honest_tool_refusal(obs, policy)
        if honest:
            return Verdict(PASS, "correct_refusal", results, notes=[why])
        blocked = _transient_block(obs, policy)
        if blocked:
            return Verdict(FAIL, "blocked", results, notes=[blocked])
        if any(_env_ok(c) for c in obs.calls if c.write) or _fingerprint_changed(obs):
            return Verdict(FAIL, "unexpected_edit", results, notes=[why])
        return Verdict(FAIL, "failed", results, notes=[why])

    if kind == "typed_error":
        last = obs.calls[-1] if obs.calls else None
        code = _code(last) if last else None
        if last and not (last.envelope or {}).get("ok") and code in case.expect.codes:
            if all(r.ok for r in results):
                return Verdict(PASS, "verified", results, notes=[code or ""])
            return Verdict(PARTIAL, "partial", results, notes=[code or ""])
        if code in policy.transient_codes and code not in case.expect.codes:
            return Verdict(FAIL, "blocked", results, notes=[code or ""])
        return Verdict(FAIL, "failed", results, notes=[f"got {code!r}, want {case.expect.codes}"])

    # edit / workflow / noop / property
    if kind == "edit":
        refused = [c for c in obs.calls if c.write and not (c.envelope or {}).get("ok")]
        if refused and _code(refused[0]) in policy.refusal_codes and not \
                any(_env_ok(c) for c in obs.calls if c.write):
            if case.meta.get("accept_refusal") and not _fingerprint_changed(obs):
                return Verdict(PASS, "correct_refusal", results, notes=[_code(refused[0]) or ""])
            return Verdict(FAIL, "wrong_refusal", results, notes=[_code(refused[0]) or ""])
    blocked = _transient_block(obs, policy)
    sf = silent_failure(case, obs, results, policy)
    if sf:
        return Verdict(FAIL, "silent_failure", results, silent_failure=sf)
    if blocked and not all(r.ok for r in results):
        return Verdict(FAIL, "blocked", results, notes=[blocked])
    if kind == "noop":
        noop = any(((c.envelope or {}).get("data") or {}).get("noop") for c in obs.calls)
        if noop and not _fingerprint_changed(obs) and all(r.ok for r in results):
            return Verdict(PASS, "verified", results)
        return Verdict(FAIL, "failed", results, notes=["expected an honest noop"])

    sidecar = case.expect.sidecar or {}
    collateral = []
    if sidecar.get("key") and sidecar.get("collateral_check", True):
        allowed = set(sidecar.get("allowed", [])) | {sidecar["key"]}
        collateral = [k for k in _changed_keys(obs, sidecar.get("units", {})) if k not in allowed]
    ok_all = all(r.ok for r in results)
    if ok_all and not collateral:
        over = [c.tool for c, s in zip(obs.calls, case.steps) if c.wall_s > s.budget_s]
        if over:
            return Verdict(PASS, "over_budget", results, notes=over)
        return Verdict(PASS, "verified", results)
    if ok_all and collateral:
        return Verdict(PARTIAL, "collateral", results, notes=collateral)
    wrong = any(r.wrong_direction for r in results)
    drift = any(r.metric == "M9" and r.note == "drift" for r in results)
    if drift and not wrong and all(r.ok or r.metric == "M9" for r in results):
        return Verdict(PARTIAL, "drift", results)
    passed = sum(r.ok for r in results)
    magnitude = all(r.ok or r.magnitude_only for r in results)
    need = float(case.meta.get("partial_min", 0.5))
    if not wrong and (magnitude or (len(results) > 1 and passed >= need * len(results))):
        return Verdict(PARTIAL, "partial", results, notes=notes)
    return Verdict(FAIL, "failed", results, notes=notes)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_evaluate.py -q`
Expected: `24 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/evaluate.py gauntlet/tests/test_evaluate.py
git commit -m "gauntlet: evaluator with silent-failure (a)-(f) and honest-refusal rules (Task 5, spec 1/5.3/5.4)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 6: Generator gate and the toy adapter

PARALLEL-OK. Depends on: Tasks 1, 5.

**Files:**
- Create: `gauntlet/generate.py`, `gauntlet/tests/toy.py`
- Test: `gauntlet/tests/test_generate.py`

**Interfaces:**
- Consumes: `Adapter.layers()/generate()/generation_notes()`, `RunDir`.
- Produces: `GenerationError`, `COUNT_TOLERANCE = 0.02`, `LayerCount(spec, got, tiers, unmapped_p2)`, `check_layer(spec, cases)`, `generate_all(adapter, layers=None) -> (cases, counts)`, `generation_md(adapter, counts, cases)`, `write_generation(run, adapter, layers=None) -> counts`. `gauntlet.tests.toy.ToyAdapter(behaviours={case_id: "ok"|"silent"|"crash"|"park"|"sleep"}, n=4)` and `make()`; later engine tasks test against it.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from gauntlet.adapter import LayerSpec
from gauntlet.case import Case, Expect, Step
from gauntlet.generate import GenerationError, check_layer, generate_all, write_generation
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _c(i, tier="P1", meta=None, layer="alpha"):
    return Case(id=f"{layer}-{i}", layer=layer, tier=tier, mode="tool",
                steps=[Step("set")], expect=Expect("edit"), meta=meta or {})


def test_writes_cases_and_generation_md_byte_identically(tmp_path):
    a = ToyAdapter()
    run1 = RunDir(tmp_path, "s1")
    run2 = RunDir(tmp_path, "s2")
    write_generation(run1, a)
    write_generation(run2, a)
    assert run1.cases_jsonl.read_bytes() == run2.cases_jsonl.read_bytes()
    md = run1.generation_md.read_text()
    assert "| 1 | alpha | 4 | 4 |" in md and "## Toy notes" in md


def test_count_gate_two_percent():
    spec = LayerSpec("alpha", 1, 100, True)
    check_layer(spec, [_c(i) for i in range(98)])
    with pytest.raises(GenerationError, match="2% off"):
        check_layer(spec, [_c(i) for i in range(97)])


def test_unmapped_p0_p1_fail_p2_listed():
    spec = LayerSpec("alpha", 1, 0, True)
    with pytest.raises(GenerationError, match="unmapped"):
        check_layer(spec, [_c(1, "P1", {"unmapped": ["x"]})])
    lc = check_layer(spec, [_c(1, "P2", {"unmapped": ["x"]})])
    assert lc.unmapped_p2 == ["alpha-1"]


def test_layer_filter_and_unknown_layer():
    cases, counts = generate_all(ToyAdapter(), ["beta"])
    assert [c.layer for c in cases] == ["beta", "beta"]
    with pytest.raises(GenerationError, match="unknown"):
        generate_all(ToyAdapter(), ["gamma"])


def test_duplicate_ids_refused():
    class Dup(ToyAdapter):
        def generate(self, layer):
            return [_c(1, layer=layer), _c(1, layer=layer)]

        def layers(self):
            return [LayerSpec("alpha", 1, 2, True)]

    with pytest.raises(GenerationError, match="duplicate"):
        generate_all(Dup())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_generate.py -q`
Expected: `ModuleNotFoundError: No module named 'gauntlet.generate'`.

- [ ] **Step 3: Write the implementation**

`gauntlet/generate.py`:

```python
"""The generator gate (spec 2.2): expand every layer through the adapter,
check ids, tiers and counts, and write cases.jsonl + generation.md."""

from __future__ import annotations

from dataclasses import dataclass, field

from gauntlet.adapter import Adapter, LayerSpec
from gauntlet.case import Case
from gauntlet.store import RunDir, atomic_write_text

COUNT_TOLERANCE = 0.02


class GenerationError(ValueError):
    pass


@dataclass
class LayerCount:
    spec: LayerSpec
    got: int
    tiers: dict[str, int] = field(default_factory=dict)
    unmapped_p2: list[str] = field(default_factory=list)


def check_layer(spec: LayerSpec, cases: list[Case]) -> LayerCount:
    errors: list[str] = []
    for c in cases:
        if c.layer != spec.name:
            errors.append(f"{c.id}: layer {c.layer!r} generated under {spec.name!r}")
        if c.meta.get("unmapped") and c.tier in ("P0", "P1"):
            errors.append(f"{c.id}: {c.tier} case falls through to unmapped "
                          f"({c.meta['unmapped']})")
    want = spec.expected_count
    if want and abs(len(cases) - want) > COUNT_TOLERANCE * want:
        errors.append(f"layer {spec.name}: {len(cases)} cases, formula says {want} (> 2% off)")
    if errors:
        raise GenerationError("; ".join(errors[:20]) + (f" (+{len(errors) - 20} more)"
                                                        if len(errors) > 20 else ""))
    tiers: dict[str, int] = {}
    for c in cases:
        tiers[c.tier] = tiers.get(c.tier, 0) + 1
    return LayerCount(spec, len(cases), tiers,
                      [c.id for c in cases if c.meta.get("unmapped")])


def generate_all(adapter: Adapter, layers: list[str] | None = None) -> tuple[list[Case],
                                                                             list[LayerCount]]:
    specs = sorted(adapter.layers(), key=lambda s: s.order)
    if layers:
        unknown = set(layers) - {s.name for s in specs}
        if unknown:
            raise GenerationError(f"unknown layers: {sorted(unknown)}")
        specs = [s for s in specs if s.name in layers]
    all_cases: list[Case] = []
    counts: list[LayerCount] = []
    seen: set[str] = set()
    for spec in specs:
        cases = adapter.generate(spec.name)
        dup = [c.id for c in cases if c.id in seen]
        seen.update(c.id for c in cases)
        if len(seen) != len(all_cases) + len(cases) or dup:
            raise GenerationError(f"duplicate case ids in {spec.name}: {dup[:5]}")
        counts.append(check_layer(spec, cases))
        all_cases.extend(cases)
    return all_cases, counts


def generation_md(adapter: Adapter, counts: list[LayerCount], cases: list[Case]) -> str:
    lines = [f"# Generation: {adapter.name}", "",
             "| # | Layer | Formula | Generated | P0 | P1 | P2 | Live |",
             "|---|---|---|---|---|---|---|---|"]
    for c in counts:
        s = c.spec
        lines.append(f"| {s.order} | {s.name} | {s.expected_count} | {c.got} | "
                     f"{c.tiers.get('P0', 0)} | {c.tiers.get('P1', 0)} | "
                     f"{c.tiers.get('P2', 0)} | {'yes' if s.live else 'no'} |")
    total = sum(c.got for c in counts)
    lines += ["", f"**Total:** {total} cases "
              f"({sum(c.got for c in counts if c.spec.live)} live).", ""]
    unmapped = [i for c in counts for i in c.unmapped_p2]
    if unmapped:
        lines += ["## P2 cases with no mapping", ""] + [f"- `{i}`" for i in unmapped] + [""]
    notes = adapter.generation_notes(cases)
    if notes:
        lines += [notes.rstrip(), ""]
    return "\n".join(lines)


def write_generation(run: RunDir, adapter: Adapter, layers: list[str] | None = None
                     ) -> list[LayerCount]:
    cases, counts = generate_all(adapter, layers)
    run.ensure()
    run.write_cases(cases)
    atomic_write_text(run.generation_md, generation_md(adapter, counts, cases))
    return counts
```

`gauntlet/tests/toy.py`:

```python
"""A toy product adapter: proves the engine runs with no real product."""

from __future__ import annotations

from gauntlet.adapter import CallRecord, LayerSpec, Observation, Park
from gauntlet.case import Case, Check, Expect, Step, case_id


class ToySession:
    def __init__(self, adapter: "ToyAdapter") -> None:
        self.adapter = adapter
        self.closed = False

    def run_case(self, case: Case) -> Observation:
        self.adapter.ran.append(case.id)
        behaviour = self.adapter.behaviours.get(case.id, "ok")
        if behaviour == "park":
            raise Park("usage limit", until=None)
        if behaviour == "sleep":
            import time
            time.sleep(case.meta.get("sleep_s", 5))
        if behaviour == "crash":
            self.adapter.crashes += 1
        value = 1.0 if behaviour in ("ok", "crash", "sleep") else 0.7
        env = {"ok": True, "outcome": "ok", "data": {}}
        return Observation(
            calls=[CallRecord("set", case.steps[0].args, env, wall_s=0.01, write=True)],
            before={"fingerprint": "a", "params": {"x.v": 0.7}},
            after={"fingerprint": "b" if value == 1.0 else "a", "params": {"x.v": value}},
        )

    def abort(self) -> None:
        self.adapter.aborts += 1

    def close(self) -> None:
        self.closed = True


class ToyAdapter:
    name = "toy"

    def __init__(self, behaviours: dict[str, str] | None = None, n: int = 4) -> None:
        self.behaviours = behaviours or {}
        self.n = n
        self.ran: list[str] = []
        self.crashes = 0
        self.aborts = 0
        self.sessions = 0

    def layers(self) -> list[LayerSpec]:
        return [LayerSpec("alpha", 1, self.n, True), LayerSpec("beta", 2, 2, True)]

    def generate(self, layer: str) -> list[Case]:
        n = self.n if layer == "alpha" else 2
        out = []
        for i in range(n):
            cid = case_id(layer, i)
            out.append(Case(
                id=cid, layer=layer, tier="P0" if i == 0 else "P1", mode="tool",
                steps=[Step("set", {"v": 1.0}, budget_s=0.5, write=True)],
                expect=Expect("edit", checks=[Check("M1", kind="equals",
                                                    target={"key": "x.v", "value": 1.0})],
                              sidecar={"changed": True, "key": "x.v"}),
                meta={"sleep_s": 5},
            ))
        return out

    def open_session(self, layer: str, run_root) -> ToySession:
        self.sessions += 1
        return ToySession(self)

    def crash_count(self) -> int:
        return self.crashes

    def crash_reports_since(self, t: float) -> list[str]:
        return [f"toy-{i}.ips" for i in range(self.crashes)]

    def signoff_lines(self) -> list[str]:
        return ["toy originals unchanged: yes"]

    def generation_notes(self, cases: list[Case]) -> str:
        return "## Toy notes\n\nnothing to add"

    def write_tools(self) -> frozenset[str]:
        return frozenset({"set"})

    def policy(self):
        from gauntlet.evaluate import Policy
        return Policy(frozenset({"usage"}), frozenset({"bridge_timeout"}), ("can't",), ("done",))


def make() -> ToyAdapter:
    return ToyAdapter()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_generate.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/generate.py gauntlet/tests/toy.py gauntlet/tests/test_generate.py
git commit -m "gauntlet: generation gate (counts within 2%, no unmapped P0/P1) and a toy adapter (Task 6, spec 2.2)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 7: Checkpoints and the runner

PARALLEL-OK. Depends on: Tasks 5, 6.

**Files:**
- Create: `gauntlet/progress.py`, `gauntlet/runner.py`
- Test: `gauntlet/tests/test_runner.py`

**Interfaces:**
- Consumes: `Adapter`, `Session`, `Park`, `evaluate`, `RunDir`.
- Produces: `Progress(run, adapter_name, commit="", clock=...)` with `layer/start_layer/record/park/unpark/end_layer/finish/log/save`; `progress.json` schema `gauntlet-progress/1` = `{stamp, adapter, commit, started, updated, status, current_case, layers: {name: {total, done, pass, partial, fail, silent, crash, hang, blocked, parked, p0_total, p0_pass, points, rate_pct, eta_s, status, live}}, parked: [ids], eta_s}`; `log.md` lines `- **<id>**: <outcome>, <wall>s -- <note>`. `Runner(adapter, run, commit="", clock, sleep, wall, session_max_cases=50, session_max_s=1200).run_layers(layers=None, resume=True, only_failed=False, case_ids=None, wait_on_park=False, tiers=None, prefixes=(), where=None) -> RunSummary(ran, skipped, parked, stopped_layers, status)`. Result file per case: `{id, layer, tier, mode, body, frame_role, source, meta, repro, started_at, wall_s, verdict, observation}`.

- [ ] **Step 1: Write the failing test**

```python
import json

from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _setup(tmp_path, **kw):
    a = ToyAdapter(**kw)
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    return a, run


def test_runs_every_case_writes_results_and_progress(tmp_path):
    a, run = _setup(tmp_path)
    s = Runner(a, run).run_layers()
    assert s.ran == 6 and s.status == "done"
    prog = json.loads(run.progress_json.read_text())
    assert prog["layers"]["alpha"]["pass"] == 4 and prog["layers"]["alpha"]["status"] == "done"
    assert prog["layers"]["alpha"]["p0_pass"] == 1
    assert "alpha-0" in run.log_md.read_text()
    doc = run.read_result("alpha-0")
    assert doc["verdict"]["outcome"] == "verified" and doc["repro"].endswith("--stamp s")


def test_resume_skips_done_and_only_failed_reruns(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-2": "silent"})
    Runner(a, run).run_layers()
    a.ran.clear()
    Runner(a, run).run_layers(resume=True)
    assert a.ran == []
    Runner(a, run).run_layers(only_failed=True)
    assert a.ran == ["alpha-2"]
    assert run.read_result("alpha-2")["verdict"]["outcome"] == "silent_failure"


def test_case_filter(tmp_path):
    a, run = _setup(tmp_path)
    Runner(a, run).run_layers(case_ids={"beta-1"}, resume=False)
    assert a.ran == ["beta-1"]


def test_crash_stops_the_layer_but_not_the_run(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-1": "crash"})
    s = Runner(a, run).run_layers()
    assert s.stopped_layers == ["alpha"]
    assert "alpha-2" not in a.ran and "beta-0" in a.ran
    assert run.read_result("alpha-1")["verdict"]["outcome"] == "crash"


def test_hard_cap_blocks_and_aborts_the_session(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-0": "sleep"})
    # the real cap (budget + 30 s per step) is too slow for a unit test: shrink it.
    import gauntlet.case as case_mod
    orig = case_mod.Case.budget_cap_s
    case_mod.Case.budget_cap_s = lambda self: 0.2
    try:
        Runner(a, run).run_layers(layers=["alpha"])
    finally:
        case_mod.Case.budget_cap_s = orig
    assert run.read_result("alpha-0")["verdict"]["outcome"] == "blocked"
    assert a.aborts == 1 and a.sessions >= 2


def test_park_records_and_resume_picks_up(tmp_path):
    a, run = _setup(tmp_path, behaviours={"alpha-1": "park"})
    s = Runner(a, run).run_layers()
    assert s.status == "parked" and "alpha-1" in s.parked
    assert run.read_result("alpha-1") is None
    prog = json.loads(run.progress_json.read_text())
    assert prog["status"] == "parked" and "alpha-1" in prog["parked"]
    assert "beta-0" in a.ran  # other layers go on
    b = ToyAdapter()
    Runner(b, run).run_layers(resume=True)
    assert b.ran == ["alpha-1", "alpha-2", "alpha-3"]
    assert run.read_result("alpha-1") is not None


def test_session_rotation(tmp_path):
    a, run = _setup(tmp_path, n=5)
    Runner(a, run, session_max_cases=2).run_layers(layers=["alpha"])
    assert a.sessions == 3


def test_tier_filter_runs_only_the_p0_slice(tmp_path):
    a, run = _setup(tmp_path)
    Runner(a, run).run_layers(tiers={"P0"})
    assert a.ran == ["alpha-0", "beta-0"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_runner.py -q`
Expected: `ModuleNotFoundError: No module named 'gauntlet.runner'`.

- [ ] **Step 3: Write the implementation**

`gauntlet/progress.py`:

```python
"""Checkpoints (spec 8): progress.json after every case and layer, and a
log.md line per case in the acceptance driver's format."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from gauntlet.case import dumps
from gauntlet.store import RunDir, atomic_write_text

SCHEMA = "gauntlet-progress/1"
_COUNTERS = ("done", "pass", "partial", "fail", "silent", "crash", "hang", "blocked", "parked",
             "p0_total", "p0_pass", "points")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Progress:
    def __init__(self, run: RunDir, adapter_name: str, commit: str = "",
                 clock=time.monotonic) -> None:
        self.run = run
        self.clock = clock
        self.t0 = clock()
        if run.progress_json.exists():
            self.doc = json.loads(run.progress_json.read_text(encoding="utf-8"))
        else:
            self.doc = {"schema": SCHEMA, "stamp": run.stamp, "adapter": adapter_name,
                        "commit": commit, "started": _now(), "status": "running",
                        "current_case": None, "layers": {}, "parked": [], "eta_s": None}
        self._rate: dict[str, float] = {}

    def layer(self, name: str, total: int, p0_total: int, live: bool) -> dict:
        lay = self.doc["layers"].setdefault(name, {k: 0 for k in _COUNTERS})
        lay.update(total=total, p0_total=p0_total, live=live)
        lay.setdefault("status", "pending")
        return lay

    def start_layer(self, name: str) -> None:
        self.doc["layers"][name]["status"] = "running"
        self._layer_t0 = self.clock()
        self._layer_done0 = self.doc["layers"][name]["done"]
        self.save()

    def record(self, name: str, case_id: str, tier: str, verdict: dict, wall_s: float) -> None:
        lay = self.doc["layers"][name]
        self.unpark(case_id)
        lay["done"] += 1
        lay[verdict["score"]] += 1
        lay["points"] += {"pass": 1.0, "partial": 0.5, "fail": 0.0}[verdict["score"]]
        outcome = verdict["outcome"]
        if outcome == "silent_failure":
            lay["silent"] += 1
        if outcome == "crash":
            lay["crash"] += 1
        if outcome == "failed_hang":
            lay["hang"] += 1
        if outcome == "blocked":
            lay["blocked"] += 1
        if tier == "P0" and verdict["score"] == "pass":
            lay["p0_pass"] += 1
        lay["rate_pct"] = round(100.0 * lay["points"] / lay["done"], 1)
        elapsed = self.clock() - self._layer_t0
        done_here = lay["done"] - self._layer_done0
        per_case = elapsed / done_here if done_here else 0.0
        lay["eta_s"] = round(per_case * (lay["total"] - lay["done"]), 1)
        self.doc["current_case"] = case_id
        self.doc["eta_s"] = sum(v.get("eta_s") or 0 for v in self.doc["layers"].values())
        note = verdict.get("silent_failure") or "; ".join(str(n) for n in verdict.get("notes", [])[:2])  # noqa: E501
        self.log(f"- **{case_id}**: {outcome}, {wall_s:.2f}s{' -- ' + note if note else ''}")
        self.save()

    def park(self, name: str, case_id: str, reason: str) -> None:
        lay = self.doc["layers"][name]
        lay["parked"] += 1
        if case_id not in self.doc["parked"]:
            self.doc["parked"].append(case_id)
        lay["status"] = "parked"
        self.log(f"- **{case_id}**: parked -- {reason}")
        self.save()

    def unpark(self, case_id: str) -> None:
        if case_id in self.doc["parked"]:
            self.doc["parked"].remove(case_id)

    def end_layer(self, name: str, status: str = "done") -> None:
        lay = self.doc["layers"][name]
        if lay.get("status") != "parked" or status != "done":
            lay["status"] = status
        lay["eta_s"] = 0.0 if status == "done" else lay.get("eta_s")
        self.log(f"\n## Layer {name}: {status} ({lay['done']}/{lay['total']}, "
                 f"pass {lay['pass']}, partial {lay['partial']}, fail {lay['fail']}, "
                 f"silent {lay['silent']}, crash {lay['crash']})\n")
        self.save()

    def finish(self, status: str) -> None:
        self.doc["status"] = status
        self.doc["current_case"] = None
        self.save()

    def log(self, line: str) -> None:
        self.run.log_md.parent.mkdir(parents=True, exist_ok=True)
        with open(self.run.log_md, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def save(self) -> None:
        self.doc["updated"] = _now()
        atomic_write_text(self.run.progress_json, dumps(self.doc) + "\n")
```

`gauntlet/runner.py`:

```python
"""The generic runner (spec 2.3): sessions, hard caps, resume, crash guard,
park-and-resume. Product behaviour lives in the adapter's Session."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from gauntlet.adapter import Adapter, Observation, Park
from gauntlet.case import Case
from gauntlet.evaluate import FAIL, Verdict, evaluate
from gauntlet.progress import Progress
from gauntlet.store import RunDir

SESSION_MAX_CASES = 50
SESSION_MAX_S = 20 * 60
PARK_MAX_WAIT_S = 6 * 3600
NON_PASS = ("partial", "fail")


@dataclass
class RunSummary:
    ran: int = 0
    skipped: int = 0
    parked: list[str] = field(default_factory=list)
    stopped_layers: list[str] = field(default_factory=list)
    status: str = "done"


class _Timeout(Exception):
    pass


def _call_with_cap(fn, cap_s: float):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(cap_s)
    if t.is_alive():
        raise _Timeout(f"case exceeded its hard cap of {cap_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


class Runner:
    def __init__(self, adapter: Adapter, run: RunDir, *, commit: str = "",
                 clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 session_max_cases: int = SESSION_MAX_CASES,
                 session_max_s: float = SESSION_MAX_S) -> None:
        self.adapter = adapter
        self.run = run.ensure()
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.progress = Progress(run, adapter.name, commit, clock)
        self.policy = adapter.policy()
        self.session_max_cases = session_max_cases
        self.session_max_s = session_max_s

    def _select(self, cases: list[Case], layer: str, *, resume: bool, only_failed: bool,
                case_ids: set[str] | None, tiers: set[str] | None = None,
                prefixes: tuple[str, ...] = (), where: dict | None = None) -> list[Case]:
        chosen = [c for c in cases if c.layer == layer]
        if tiers:
            chosen = [c for c in chosen if c.tier in tiers]
        if prefixes:
            chosen = [c for c in chosen if c.id.startswith(prefixes)]
        if where:
            chosen = [c for c in chosen
                      if all(str(c.meta.get(k)) in vs for k, vs in where.items())]
        if case_ids:
            chosen = [c for c in chosen if c.id in case_ids]
        if only_failed:
            keep = []
            for c in chosen:
                r = self.run.read_result(c.id)
                if r is None or r["verdict"]["score"] in NON_PASS:
                    keep.append(c)
            return keep
        if resume and not case_ids:
            done = self.run.done_ids()
            chosen = [c for c in chosen if c.id not in done]
        return chosen

    def run_layers(self, layers: list[str] | None = None, *, resume: bool = True,
                   only_failed: bool = False, case_ids: set[str] | None = None,
                   wait_on_park: bool = False, tiers: set[str] | None = None,
                   prefixes: tuple[str, ...] = (), where: dict | None = None) -> RunSummary:
        cases = self.run.read_cases()
        specs = sorted(self.adapter.layers(), key=lambda s: s.order)
        if layers:
            specs = [s for s in specs if s.name in layers]
        summary = RunSummary()
        for spec in specs:
            all_layer = [c for c in cases if c.layer == spec.name]
            todo = self._select(cases, spec.name, resume=resume, only_failed=only_failed,
                                case_ids=case_ids, tiers=tiers, prefixes=prefixes,
                                where=where)
            self.progress.layer(spec.name, len(all_layer),
                                sum(c.tier == "P0" for c in all_layer), spec.live)
            if not todo:
                continue
            self.progress.start_layer(spec.name)
            status = self._run_layer(spec.name, todo, summary, wait_on_park)
            self.progress.end_layer(spec.name, status)
            if status == "stopped_crash":
                summary.stopped_layers.append(spec.name)
        summary.status = "parked" if summary.parked else "done"
        self.progress.finish(summary.status)
        return summary

    def _run_layer(self, layer: str, todo: list[Case], summary: RunSummary,
                   wait_on_park: bool) -> str:
        session = None
        started = 0.0
        count = 0
        i = 0
        try:
            while i < len(todo):
                case = todo[i]
                if session is None or count >= self.session_max_cases or \
                        self.clock() - started > self.session_max_s:
                    if session is not None:
                        session.close()
                    session = self.adapter.open_session(layer, self.run)
                    started, count = self.clock(), 0
                crashes_before = self.adapter.crash_count()
                t_wall = self.wall()
                t0 = self.clock()
                try:
                    obs = _call_with_cap(lambda s=session, c=case: s.run_case(c),
                                         case.budget_cap_s())
                except Park as p:
                    self.progress.park(layer, case.id, p.reason)
                    if wait_on_park and p.until and p.until - self.wall() < PARK_MAX_WAIT_S:
                        self.sleep(max(0.0, p.until - self.wall()) + 60)
                        self.progress.unpark(case.id)
                        continue
                    summary.parked.extend(c.id for c in todo[i:])
                    return "parked"
                except _Timeout as e:
                    session.abort()
                    session = None
                    obs = Observation(blocked="hard_cap", hang_evidence=[str(e)])
                except Exception as e:  # noqa: BLE001 - a harness error is a case result
                    obs = Observation(blocked="harness_error",
                                      hang_evidence=[f"{type(e).__name__}: {e}"])
                obs.wall_s = obs.wall_s or (self.clock() - t0)
                if self.adapter.crash_count() > crashes_before:
                    obs.crash_reports = self.adapter.crash_reports_since(t_wall)
                verdict = evaluate(case, obs, self.policy)
                self._write(case, obs, verdict, t_wall)
                summary.ran += 1
                count += 1
                i += 1
                if verdict.outcome == "crash":
                    summary.skipped += len(todo) - i
                    return "stopped_crash"
            return "done"
        finally:
            if session is not None:
                session.close()

    def _write(self, case: Case, obs: Observation, verdict: Verdict, t_wall: float) -> None:
        from dataclasses import asdict

        doc = {
            "id": case.id, "layer": case.layer, "tier": case.tier, "mode": case.mode,
            "body": case.body, "frame_role": case.frame_role, "source": case.source,
            "meta": case.meta, "repro": case.repro.replace("<stamp>", self.run.stamp),
            "started_at": t_wall, "wall_s": round(obs.wall_s, 3),
            "verdict": verdict.to_dict(),
            "observation": asdict(obs),
        }
        self.run.write_result(case.id, doc)
        self.progress.record(case.layer, case.id, case.tier, verdict.to_dict(), obs.wall_s)


def failed_verdict(note: str) -> Verdict:
    return Verdict(FAIL, "failed", notes=[note])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_runner.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/progress.py gauntlet/runner.py gauntlet/tests/test_runner.py
git commit -m "gauntlet: runner with sessions, hard caps, resume, crash guard and park (Task 7, spec 2.3/8)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 8: Report, contact sheet, fix list, roadmap

PARALLEL-OK. Depends on: Task 7.

**Files:**
- Create: `gauntlet/report.py`
- Test: `gauntlet/tests/test_report.py`

**Interfaces:**
- Consumes: `RunDir.read_cases/read_results`, `Adapter.layers()/signoff_lines()`, `LayerSpec.p1_gate/check_gates`, result `meta.check_tags`, `meta.roadmap_key`, `meta.gaps`, `observation.extra.previews`.
- Produces: `build(run, adapter, cases, results, meta) -> dict` (`S8_signoff` first, then `S1_headline` incl. P0 served/refused, `S2_layers`, `S3_non_pass`, `S5_timing`, `S6_crashes_hangs`, `S7_roadmap`), `markdown(rep)`, `contact_sheet(results)`, `fix_list(results)` (silent first, then crash, hang, then by tier), `roadmap(results)` (spec §7 score), `timing(results)`, `write(run, adapter, meta=None) -> rep` writing `report.md`, `report.json`, `contact-sheet.html`, `fixlist.json`, `fixlist.md`. `TRIAGE_CLASSES = (product_bug, harness_bug, taxonomy_error, vocab_gap, upstream, accepted)`.

- [ ] **Step 1: Write the failing test**

```python
import json

from gauntlet import report
from gauntlet.generate import write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _run(tmp_path, **kw):
    a = ToyAdapter(**kw)
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run).run_layers()
    return a, run


def test_clean_run_meets_the_bar(tmp_path):
    a, run = _run(tmp_path)
    rep = report.write(run, a)
    s8 = rep["S8_signoff"]
    assert s8["release_bar_met"] and s8["p0"] == "2/2" and s8["crashes"] == 0
    md = run.report_md.read_text()
    assert md.index("## S8 Sign-off") < md.index("## S1 Headline")
    assert "toy originals unchanged: yes" in md
    assert json.loads(run.report_json.read_text())["S1_headline"]["ran"] == 6


def test_silent_failure_blocks_release_and_leads_the_fix_list(tmp_path):
    a, run = _run(tmp_path, behaviours={"alpha-3": "silent", "alpha-0": "silent"})
    rep = report.write(run, a)
    assert not rep["S8_signoff"]["release_bar_met"]
    fixes = json.loads(run.fixlist_json.read_text())
    assert [f["id"] for f in fixes] == ["alpha-0", "alpha-3"]
    assert fixes[0]["tier"] == "P0" and fixes[0]["repro"].endswith("--stamp s")
    assert "| `alpha-0` | P0 | silent_failure |" in run.fixlist_md.read_text()


def test_missing_cases_block_release(tmp_path):
    a = ToyAdapter()
    run = RunDir(tmp_path, "s")
    write_generation(run, a)
    Runner(a, run).run_layers(layers=["alpha"])
    assert report.write(run, a)["S8_signoff"]["cases_missing"] == 2


def test_roadmap_scoring():
    rs = [
        {"id": "1", "layer": "scn", "tier": "P0", "mode": "tool", "source": {"id": "r1"},
         "meta": {"roadmap_key": "lens"}, "verdict": {"outcome": "correct_refusal"}},
        {"id": "2", "layer": "scn", "tier": "P1", "mode": "tool", "source": {"id": "r2"},
         "meta": {"roadmap_key": "lens"}, "verdict": {"outcome": "correct_refusal"}},
        {"id": "3", "layer": "scn", "tier": "P0", "mode": "agent", "source": {"id": "r1"},
         "meta": {"roadmap_key": "lens"}, "verdict": {"outcome": "correct_refusal"}},
        {"id": "4", "layer": "looks", "tier": "P1", "mode": "tool", "source": {},
         "meta": {"gaps": ["grain", "grain"]}, "verdict": {"outcome": "verified"}},
    ]
    rows = {r["key"]: r for r in report.roadmap(rs)}
    assert rows["lens"]["score"] == 2 + 3 * 1 + 1
    assert rows["grain"]["score"] == 0.2


def test_timing_percentiles_and_contact_sheet(tmp_path):
    rs = [{"id": "x", "layer": "l", "verdict": {"outcome": "verified", "score": "pass"},
           "observation": {"calls": [{"tool": "t", "wall_s": w} for w in (1, 2, 3, 4)],
                           "extra": {"previews": {"before": "p/b.jpg", "after": "p/a.jpg"}}}}]
    t = report.timing(rs)["percentiles"]["l:t"]
    assert (t["p50"], t["p90"], t["p99"]) == (2.0, 4.0, 4.0)
    sheet = report.contact_sheet(rs)
    assert 'src="p/b.jpg"' in sheet and "x - verified" in sheet
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_report.py -q`
Expected: `ImportError: cannot import name 'report'`.

- [ ] **Step 3: Write the implementation**

```python
"""The report (spec 2.5): report.md + report.json (S8 sign-off first, then
S1-S7), contact-sheet.html, and the fix list for the morning loop."""

from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone

from gauntlet.adapter import Adapter
from gauntlet.case import Case, dumps
from gauntlet.evaluate import POINTS, canonical_outcome
from gauntlet.store import RunDir, atomic_write_text

SERVED = ("verified", "over_budget")
TRIAGE_CLASSES = ("product_bug", "harness_bug", "taxonomy_error", "vocab_gap", "upstream",
                  "accepted")


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return round(s[k], 3)


def _outcome(r: dict) -> str:
    return canonical_outcome(r["verdict"]["outcome"])


def layer_table(results: list[dict], adapter: Adapter) -> list[dict]:
    rows = []
    for spec in sorted(adapter.layers(), key=lambda s: s.order):
        rs = [r for r in results if r["layer"] == spec.name]
        n = len(rs)
        pts = sum(POINTS[r["verdict"]["score"]] for r in rs)
        p0 = [r for r in rs if r["tier"] == "P0"]
        p1 = [r for r in rs if r["tier"] == "P1"]
        p1_rate = (sum(POINTS[r["verdict"]["score"]] for r in p1) / len(p1)) if p1 else None
        gates = []
        for key, values, minimum in spec.check_gates:
            tagged = [c for r in rs for c, t in _tagged_checks(r) if t.get(key) in values]
            rate = (sum(1 for c in tagged if c["ok"]) / len(tagged)) if tagged else None
            gates.append({"tag": key, "values": list(values), "min": minimum, "rate": rate,
                          "n": len(tagged), "ok": rate is None or rate >= minimum})
        rows.append({
            "layer": spec.name, "cases": n, "points": pts,
            "rate": round(pts / n, 4) if n else None,
            "pass": sum(r["verdict"]["score"] == "pass" for r in rs),
            "partial": sum(r["verdict"]["score"] == "partial" for r in rs),
            "fail": sum(r["verdict"]["score"] == "fail" for r in rs),
            "p0_total": len(p0), "p0_pass": sum(r["verdict"]["score"] == "pass" for r in p0),
            "p1_rate": None if p1_rate is None else round(p1_rate, 4),
            "p1_gate": spec.p1_gate,
            "p1_ok": p1_rate is None or spec.p1_gate is None or p1_rate >= spec.p1_gate,
            "check_gates": gates,
        })
    return rows


def _tagged_checks(r: dict):
    case_checks = r.get("meta", {}).get("check_tags") or []
    for i, c in enumerate(r["verdict"].get("checks", [])):
        tags = case_checks[i] if i < len(case_checks) else {}
        yield c, tags


def timing(results: list[dict]) -> dict:
    per: dict[str, list[float]] = {}
    over: list[str] = []
    for r in results:
        for c in r.get("observation", {}).get("calls", []):
            per.setdefault(f"{r['layer']}:{c['tool']}", []).append(float(c.get("wall_s") or 0))
        if _outcome(r) == "over_budget":
            over.append(r["id"])
    return {"percentiles": {k: {"n": len(v), "p50": _pct(v, .5), "p90": _pct(v, .9),
                                "p99": _pct(v, .99)} for k, v in sorted(per.items())},
            "over_budget": over}


def roadmap(results: list[dict]) -> list[dict]:
    """Spec 7 demand score: requests + 3 x P0 + agent refusals + look gaps / 10."""
    keys: dict[str, dict] = {}
    for r in results:
        meta = r.get("meta") or {}
        out = _outcome(r)
        for gap in meta.get("gaps") or []:
            k = keys.setdefault(gap, {"requests": set(), "p0": set(), "agent": 0, "gaps": 0})
            k["gaps"] += 1
        key = meta.get("roadmap_key")
        if not key or out not in ("correct_refusal", "wrong_refusal", "needs_review"):
            continue
        k = keys.setdefault(key, {"requests": set(), "p0": set(), "agent": 0, "gaps": 0})
        req = (r.get("source") or {}).get("id")
        if r["mode"] == "agent":
            k["agent"] += 1
        elif req:
            k["requests"].add(req)
            if r["tier"] == "P0":
                k["p0"].add(req)
    rows = [{"key": key, "requests": len(v["requests"]), "p0": len(v["p0"]),
             "agent_refusals": v["agent"], "look_gaps": v["gaps"],
             "score": round(len(v["requests"]) + 3 * len(v["p0"]) + v["agent"] + v["gaps"] / 10, 1)}
            for key, v in keys.items()]
    return sorted(rows, key=lambda x: (-x["score"], x["key"]))


def fix_list(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        if r["verdict"]["score"] == "pass":
            continue
        out = _outcome(r)
        rows.append({"id": r["id"], "layer": r["layer"], "tier": r["tier"], "mode": r["mode"],
                     "outcome": out, "score": r["verdict"]["score"],
                     "silent_failure": r["verdict"].get("silent_failure"),
                     "notes": r["verdict"].get("notes", [])[:3], "repro": r["repro"],
                     "triage": None, "triage_classes": list(TRIAGE_CLASSES)})
    rank = {"silent_failure": 0, "crash": 1, "failed_hang": 2}
    rows.sort(key=lambda x: (rank.get(x["outcome"], 3), x["tier"], x["layer"], x["id"]))
    return rows


def build(run: RunDir, adapter: Adapter, cases: list[Case], results: list[dict],
          meta: dict | None = None) -> dict:
    meta = meta or {}
    layers = layer_table(results, adapter)
    silent = [r["id"] for r in results if _outcome(r) == "silent_failure"]
    crashes = [x for r in results for x in r["observation"].get("crash_reports", [])]
    hangs = [r["id"] for r in results if _outcome(r) == "failed_hang"]
    p0 = [r for r in results if r["tier"] == "P0"]
    p0_total = sum(c.tier == "P0" for c in cases)
    p0_pass = sum(r["verdict"]["score"] == "pass" for r in p0)
    served = sum(_outcome(r) in SERVED for r in p0)
    refused = sum(_outcome(r) == "correct_refusal" for r in p0)
    missing = len(cases) - len(results)
    gates_ok = all(row["p1_ok"] and all(g["ok"] for g in row["check_gates"]) for row in layers)
    signoff = {
        "crashes": len(crashes), "silent_failures": len(silent),
        "p0": f"{p0_pass}/{p0_total}", "p0_all_pass": p0_total > 0 and p0_pass == p0_total,
        "p1_gates_ok": gates_ok, "cases_missing": missing,
        "release_bar_met": not crashes and not silent and p0_pass == p0_total and gates_ok
        and missing == 0,
        "contact_sheet_reviewed": "[ ] (Pratik ticks by hand)",
        "lines": adapter.signoff_lines(),
    }
    return {
        "schema": "gauntlet-report/1", "stamp": run.stamp, "adapter": adapter.name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "meta": meta,
        "S8_signoff": signoff,
        "S1_headline": {"cases": len(cases), "ran": len(results),
                        "pass": sum(r["verdict"]["score"] == "pass" for r in results),
                        "partial": sum(r["verdict"]["score"] == "partial" for r in results),
                        "fail": sum(r["verdict"]["score"] == "fail" for r in results),
                        "p0_served": served, "p0_refused": refused, "p0_total": p0_total},
        "S2_layers": layers,
        "S3_non_pass": fix_list(results),
        "S5_timing": timing(results),
        "S6_crashes_hangs": {"crash_reports": crashes, "hangs": hangs},
        "S7_roadmap": roadmap(results),
    }


def markdown(rep: dict) -> str:
    s8, s1 = rep["S8_signoff"], rep["S1_headline"]
    L = [f"# Gauntlet report {rep['stamp']} ({rep['adapter']})", "",
         "## S8 Sign-off", "",
         f"- Release bar met: **{'YES' if s8['release_bar_met'] else 'NO'}**",
         f"- Crashes: {s8['crashes']}", f"- Silent failures: {s8['silent_failures']}",
         f"- P0: {s8['p0']}", f"- P1 gates: {'ok' if s8['p1_gates_ok'] else 'NOT met'}",
         f"- Cases missing (not run): {s8['cases_missing']}",
         f"- Contact sheet reviewed: {s8['contact_sheet_reviewed']}",
         *[f"- {x}" for x in s8["lines"]], "",
         "## S1 Headline", "",
         f"{s1['ran']}/{s1['cases']} cases: {s1['pass']} pass, {s1['partial']} partial, "
         f"{s1['fail']} fail.", "",
         f"**P0 served rate:** {s1['p0_served']} verified edits, {s1['p0_refused']} honest "
         f"refusals, of {s1['p0_total']} P0 cases (a headline, not a gate).", "",
         "## S2 Layers", "",
         "| Layer | Cases | Rate | Pass | Partial | Fail | P0 | P1 rate | Gate |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["S2_layers"]:
        rate = "-" if r["rate"] is None else f"{100 * r['rate']:.1f}%"
        p1 = "-" if r["p1_rate"] is None else f"{100 * r['p1_rate']:.1f}%"
        gate = "ok" if r["p1_ok"] and all(g["ok"] for g in r["check_gates"]) else "**NOT met**"
        L.append(f"| {r['layer']} | {r['cases']} | {rate} | {r['pass']} | {r['partial']} | "
                 f"{r['fail']} | {r['p0_pass']}/{r['p0_total']} | {p1} | {gate} |")
    L += ["", "## S3 Every non-pass", "", "| Case | Tier | Outcome | Why | Repro |",
          "|---|---|---|---|---|"]
    for f in rep["S3_non_pass"]:
        why = f["silent_failure"] or "; ".join(str(n) for n in f["notes"])
        L.append(f"| `{f['id']}` | {f['tier']} | {f['outcome']} | {why[:120]} | "
                 f"`{f['repro']}` |")
    L += ["", "## S4 Contact sheet", "", "See `contact-sheet.html` in the run directory.", "",
          "## S5 Timing", "", "| Layer:tool | n | p50 | p90 | p99 |", "|---|---|---|---|---|"]
    for k, v in rep["S5_timing"]["percentiles"].items():
        L.append(f"| {k} | {v['n']} | {v['p50']} | {v['p90']} | {v['p99']} |")
    L += ["", f"Over budget (pass, listed): {len(rep['S5_timing']['over_budget'])}", "",
          "## S6 Crashes and hangs", "",
          f"- Crash reports: {len(rep['S6_crashes_hangs']['crash_reports'])}",
          *[f"  - `{x}`" for x in rep["S6_crashes_hangs"]["crash_reports"]],
          f"- Hangs: {len(rep['S6_crashes_hangs']['hangs'])}", "",
          "## S7 Honest refusals as the roadmap", "",
          "| Key | Score | Requests | P0 | Agent | Look gaps |", "|---|---|---|---|---|---|"]
    for r in rep["S7_roadmap"]:
        L.append(f"| {r['key']} | {r['score']} | {r['requests']} | {r['p0']} | "
                 f"{r['agent_refusals']} | {r['look_gaps']} |")
    return "\n".join(L) + "\n"


def contact_sheet(results: list[dict]) -> str:
    tiles = []
    for r in results:
        prev = (r.get("observation", {}).get("extra") or {}).get("previews") or {}
        if not prev:
            continue
        cap = html.escape(f"{r['id']} - {_outcome(r)}")
        imgs = "".join(f'<img src="{html.escape(prev[k])}" alt="{k}" loading="lazy">'
                       for k in ("before", "after") if prev.get(k))
        tiles.append(f'<figure class="{html.escape(r["verdict"]["score"])}">{imgs}'
                     f"<figcaption>{cap}</figcaption></figure>")
    return ("<!doctype html><meta charset='utf-8'><title>Contact sheet</title><style>"
            "body{font-family:system-ui;margin:16px;background:#fff;color:#111}"
            "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}}"
            "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}"
            "figure{margin:0;border:2px solid #999;padding:4px}figure.fail{border-color:#c33}"
            "figure.partial{border-color:#c90}img{width:50%}figcaption{font-size:12px}"
            "</style><main>" + "".join(tiles) + "</main>\n")


def write(run: RunDir, adapter: Adapter, meta: dict | None = None) -> dict:
    cases = run.read_cases()
    results = run.read_results()
    rep = build(run, adapter, cases, results, meta)
    atomic_write_text(run.report_json, json.dumps(rep, indent=2, sort_keys=True) + "\n")
    atomic_write_text(run.report_md, markdown(rep))
    atomic_write_text(run.contact_sheet, contact_sheet(results))
    fixes = rep["S3_non_pass"]
    atomic_write_text(run.fixlist_json, dumps(fixes) + "\n")
    atomic_write_text(run.fixlist_md, "\n".join(
        [f"# Fix list {run.stamp}", "", "Triage each row as one of: "
         + ", ".join(TRIAGE_CLASSES) + ".", "", "| Case | Tier | Outcome | Repro | Triage |",
         "|---|---|---|---|---|"]
        + [f"| `{f['id']}` | {f['tier']} | {f['outcome']} | `{f['repro']}` | |" for f in fixes])
        + "\n")
    return rep
```

- [ ] **Step 4: Run the whole engine suite**

Run: `.venv/bin/python -m pytest gauntlet/tests -q`
Expected: `111 passed` (Tasks 1-8).

- [ ] **Step 5: Commit**

```bash
git add gauntlet/report.py gauntlet/tests/test_report.py
git commit -m "gauntlet: report (S8 sign-off first), contact sheet, fix list and roadmap (Task 8, spec 2.5/2.6/7)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 9: Scouter data side: taxonomy, check map, P0 selection, layer builders

PARALLEL-OK. Depends on: Tasks 1, 10 (Task 10 creates `tests/gauntlet/__init__.py`).

**Files:**
- Create: `tests/gauntlet/data/check_map.json`, `tests/gauntlet/taxonomy.py`, `tests/gauntlet/checkmap.py`, `tests/gauntlet/p0.py`, `tests/gauntlet/layers/__init__.py` (empty), `tests/gauntlet/layers/common.py`
- Test: `tests/gauntlet/test_taxonomy_checkmap.py`

**Interfaces:**
- Consumes: the four JSONs in `tests/gauntlet/data/` (never hand-edited), `gauntlet.case`.
- Produces: `taxonomy.load(data_dir=DATA) -> Taxonomy` (fields `modules, workflows, bulk, look_components, cannot_do, looks, look_conventions, requests, measurement, check_map`; methods `unique_phrases()`, `phrase_module()`, `module_by_name(name)`), `TaxonomyError`, `EXPECTED`; `checkmap.PartCheck`, `RequestPlan(request_id, klass in|partial|out|impossible|workflow, parts, capabilities, workflow_verb, step_back, unmapped)`, `plan_request(req, cmap)`, `split_parts`, `direction_of`, `concept_of`; `p0.select_p0(requests) -> 100 requests`, `quotas(demand)`, `demand_key(req)`; `layers.common`: `BODIES`, `WRITE_TOOLS`, `BUDGET_MOVE/SAY/COMMIT`, `frame(role)`, `step(tool, budget_s, **args)`, `key(module, param)`, `m1_equals`, `m1_delta`, `m1_restored(keys, units, from_step=0)`, `clamp_reported()`, `stat_check(PartCheck)`, `sidecar(module, param, unit, changed=True, allowed=None)`.
- `check_map.json` was dry-run against the data while writing this plan: 0 unmapped; classes out 323, in 168, impossible 60, workflow 37, partial 13; P0 selection gives 100 requests, every genre at least 3, classes out 60 / in 31 / workflow 7 / partial 2 (the ~38/62 served split in row 436).

- [ ] **Step 1: Write the check map** (hand-written data; the test below asserts its results)

`tests/gauntlet/data/check_map.json`:

```json
{
  "schema": "scouter-gauntlet-check-map/1",
  "about": "The only hand-written gauntlet data (spec 2.1). Maps requests.json check strings to metrics and Scouter capabilities. Edit by hand, review in the PR; the generator fails on any P0/P1 case that falls through.",
  "rewrites": {
    "dynamic range compression (shadow up, highlight down)": "shadow up, highlight down"
  },
  "separators": [", ", " + ", " and ", " plus "],
  "part_direction_words": [
    {"suffix": " up", "sign": 1},
    {"suffix": " down", "sign": -1},
    {"suffix": " warmer", "sign": 1},
    {"suffix": " warm", "sign": 1}
  ],
  "stat_rules": [
    {"match": "masked", "concept": "none"},
    {"match": " vs ", "concept": "none"},
    {"match": "subject", "concept": "none"},
    {"match": "background", "concept": "none"},
    {"match": "foreground", "concept": "none"},
    {"match": "spotlight", "concept": "none"},
    {"match": "acutance", "concept": "none"},
    {"match": "noise", "concept": "none"},
    {"match": "grain", "concept": "none"},
    {"match": "blob", "concept": "none"},
    {"match": "horizon", "concept": "none"},
    {"match": "keystone", "concept": "none"},
    {"match": "vertical", "concept": "none"},
    {"match": "curvature", "concept": "none"},
    {"match": "ca fringe", "concept": "none"},
    {"match": "corner", "concept": "none"},
    {"match": "vignette", "concept": "none"},
    {"match": "area ratio", "concept": "none"},
    {"match": "crop", "concept": "none"},
    {"match": "local contrast", "concept": "none"},
    {"match": "distant-region", "concept": "none"},
    {"match": "michelson", "concept": "none"},
    {"match": "haze", "concept": "none"},
    {"match": "glow", "concept": "none"},
    {"match": "star", "concept": "none"},
    {"match": "skin", "concept": "none"},
    {"match": "wb variance", "concept": "none"},
    {"match": "delta e", "concept": "none"},
    {"match": "regional", "concept": "none"},
    {"match": "neutrality", "concept": "none"},
    {"match": "split-tone", "concept": "split_tone"},
    {"match": "teal/orange", "concept": "split_tone"},
    {"match": "shadow hue", "concept": "shadow_hue"},
    {"match": "highlight hue", "concept": "highlight_hue"},
    {"match": "black-point", "concept": "black_floor"},
    {"match": "shadow floor", "concept": "black_floor"},
    {"match": "clip", "concept": "clip", "region_rules": [{"match": "sky", "region": "top"}]},
    {"match": "shadow", "concept": "shadows"},
    {"match": "highlight", "concept": "highlights"},
    {"match": "tint", "concept": "tint"},
    {"match": "b*", "concept": "temperature"},
    {"match": "color temp", "concept": "temperature"},
    {"match": "saturation", "concept": "saturation", "region_rules": [{"match": "sky", "region": "top"}]},
    {"match": "chroma", "concept": "saturation"},
    {"match": "contrast", "concept": "contrast"},
    {"match": "sky luminance", "concept": "luminance", "region_rules": [{"match": "sky", "region": "top"}]},
    {"match": "sky-region mean luminance", "concept": "luminance", "region_rules": [{"match": "sky", "region": "top"}]},
    {"match": "exposure", "concept": "luminance"},
    {"match": "l*", "concept": "luminance"},
    {"match": "luminance", "concept": "luminance"}
  ],
  "concepts": {
    "none":          {"metric": "sidecar", "stat": null, "caps": {}},
    "luminance":     {"metric": "M2", "stat": "L", "region": "frame", "caps": {"1": "brighter", "-1": "darker"}},
    "shadows":       {"metric": "M2", "stat": "L", "region": "shadows", "caps": {"1": "lift_shadows", "-1": "deepen_shadows"}},
    "highlights":    {"metric": "M2", "stat": "L", "region": "highlights", "caps": {"1": "brighter_highlights", "-1": "tame_highlights"}},
    "clip":          {"metric": "M3", "stat": "clip_high", "region": "frame", "caps": {"1": "brighter_highlights", "-1": "tame_highlights"}},
    "black_floor":   {"metric": "M3", "stat": "p01L", "region": "frame", "caps": {"1": "faded_blacks", "-1": "deeper_blacks"}},
    "contrast":      {"metric": "M6", "stat": "spread", "region": "frame", "caps": {"1": "more_contrast", "-1": "less_contrast"}},
    "temperature":   {"metric": "M4", "stat": "rb", "region": "frame", "caps": {"1": "warmer", "-1": "cooler"}},
    "tint":          {"metric": "M4", "stat": "gm", "region": "frame", "caps": {"1": "tint_green", "-1": "tint_magenta"}},
    "saturation":    {"metric": "M5", "stat": "C", "region": "frame", "caps": {"1": "more_saturation", "-1": "less_saturation"}},
    "shadow_hue":    {"metric": "M4", "stat": "b", "region": "shadows", "fixed_sign": -1, "caps": {"*": "cool_shadows"}},
    "highlight_hue": {"metric": "M4", "stat": "b", "region": "highlights", "fixed_sign": 1, "caps": {"*": "warm_highlights"}},
    "split_tone":    {"metric": "M4", "stat": "b", "region": "shadows", "fixed_sign": -1, "caps": {"*": "teal_orange"}}
  },
  "capabilities": {
    "brighter": {"phrase": "brighter"},
    "darker": {"phrase": "darker"},
    "lift_shadows": {"phrase": "lift the shadows"},
    "deepen_shadows": {"phrase": "deeper shadows"},
    "brighter_highlights": {"phrase": "brighter highlights"},
    "tame_highlights": {"phrase": "tame the highlights"},
    "faded_blacks": {"phrase": "faded blacks"},
    "deeper_blacks": {"phrase": "deeper blacks"},
    "more_contrast": {"phrase": "more contrast"},
    "less_contrast": {"phrase": "less contrast"},
    "warmer": {"phrase": "warmer"},
    "cooler": {"phrase": "cooler"},
    "tint_magenta": {"tool": "darkroom_set", "args": {"module": "temperature", "param": "tint", "value": 0.9}},
    "tint_green": {"tool": "darkroom_set", "args": {"module": "temperature", "param": "tint", "value": 1.1}},
    "more_saturation": {"phrase": "more saturation"},
    "less_saturation": {"phrase": "less saturation"},
    "cool_shadows": {"phrase": "cool the shadows"},
    "warm_highlights": {"phrase": "warm the highlights"},
    "teal_orange": {"phrase": "teal and orange"}
  },
  "direction_rules": [
    {"match": "step back by 2", "kind": "step_back", "sign": 0, "steps": 2},
    {"match": "step back by 1", "kind": "step_back", "sign": 0, "steps": 1},
    {"match": "decrease blue", "kind": "delta", "sign": 1},
    {"match": "increase toward 255", "kind": "capped", "sign": 1},
    {"match": "increase toward 1", "kind": "capped", "sign": 1},
    {"match": "increase back toward original", "kind": "delta", "sign": 1},
    {"match": "toward 0", "kind": "toward_zero", "sign": -1},
    {"match": "to 0", "kind": "to_zero", "sign": -1},
    {"match": "deviation", "kind": "toward_zero", "sign": -1},
    {"match": "neutral", "kind": "toward_zero", "sign": -1},
    {"match": "toward target", "kind": "toward_zero", "sign": -1},
    {"match": "decrease gap", "kind": "toward_zero", "sign": -1},
    {"match": "limited", "kind": "capped", "sign": 1},
    {"match": "capped", "kind": "capped", "sign": 1},
    {"match": "increase", "kind": "delta", "sign": 1},
    {"match": "lift", "kind": "delta", "sign": 1},
    {"match": "add", "kind": "delta", "sign": 1},
    {"match": "warmth", "kind": "delta", "sign": 1},
    {"match": "decrease", "kind": "delta", "sign": -1},
    {"match": "shift", "kind": "shift", "sign": 0},
    {"match": "both", "kind": "multi", "sign": 0},
    {"match": "multi", "kind": "multi", "sign": 0},
    {"match": "n/a", "kind": "none", "sign": 0}
  ],
  "request_overrides": {
    "r0033": [{"concept": "contrast", "sign": -1}, {"concept": "saturation", "sign": -1}, {"concept": "luminance", "sign": 1}]
  },
  "workflow_rules": [
    {"match": "stack pointer", "verb": "undo"},
    {"match": "last-applied parameter", "verb": "repeat_more"},
    {"match": "preset saved", "verb": "preset_save"},
    {"match": "watermark", "verb": "out"},
    {"match": "aspect ratio", "verb": "out"},
    {"match": "gallery", "verb": "out"},
    {"match": "comparison", "verb": "out"},
    {"match": "crop variants", "verb": "out"},
    {"match": "batch-applied", "verb": "out"},
    {"match": "export", "verb": "export"},
    {"match": "ranking", "verb": "cull"}
  ],
  "role_for_problem": {
    "underexposed": "dark",
    "overexposed/blown sky": "blown",
    "harsh shadows": "bright",
    "backlit": "bright",
    "flat/low contrast": "flat",
    "haze": "flat",
    "colour cast orange sodium": "warm_cast",
    "mixed light": "warm_cast",
    "colour cast blue shade": "cool_cast",
    "high ISO noise": "noisy",
    "colour cast green fluorescent": "neutral",
    "skin tone off": "neutral",
    "dust spots": "hero",
    "crooked horizon": "hero",
    "vignetting": "hero",
    "soft focus": "hero",
    "distortion": "hero",
    "chromatic aberration": "hero",
    "motion blur": "hero",
    "tilted verticals": "hero",
    "n/a": "hero"
  },
  "magnitude_words": {
    "a bit": "a bit",
    "a bit more": "a bit",
    "a bit less": "a bit",
    "a touch": "a touch",
    "a touch more": "a touch",
    "a lot": "a lot",
    "a full stop": "by 1 ev",
    "n/a": ""
  },
  "refusal_codes": ["usage", "value_unknown", "not_pinned", "unreachable", "not_in_darkroom", "unsupported_format", "not_found", "permission_denied", "disk_full", "library_locked", "darktable_not_running", "darktable_version_mismatch", "module_unavailable"],
  "transient_codes": ["gui_not_ready", "bridge_timeout", "commit_timeout"],
  "refusal_markers": ["can't", "cannot", "can not", "not able", "unable to", "isn't available", "is not available", "doesn't support", "does not support", "outside what", "not something i can", "no way to", "isn't something"],
  "claim_markers": ["\\bdone\\b", "\\bi've\\b", "\\bi have\\b", "\\bremoved\\b", "\\breplaced\\b", "\\bapplied\\b", "\\bfixed\\b"],
  "followup_rules": [
    {"match": "back it off", "action": "undo"},
    {"match": "too crunchy", "action": "undo"},
    {"match": "too smooth", "action": "undo"},
    {"match": "", "action": "repeat_bit"}
  ]
}
```

- [ ] **Step 2: Write the failing test**

```python
import collections
import json
import shutil

import pytest

from tests.gauntlet import checkmap, p0, taxonomy


@pytest.fixture(scope="module")
def tax():
    return taxonomy.load()


def test_counts_match_spec_section_0(tax):
    assert len(tax.modules) == 69 and len(tax.unique_phrases()) == 378
    assert len(tax.looks) == 234 and len(tax.requests) == 601


def test_a_silent_data_edit_fails_loudly(tmp_path):
    for f in taxonomy.DATA.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    doc = json.loads((tmp_path / "requests.json").read_text())
    doc["requests"].pop()
    (tmp_path / "requests.json").write_text(json.dumps(doc))
    with pytest.raises(taxonomy.TaxonomyError, match="requests: expected 601, got 600"):
        taxonomy.load(tmp_path)


def test_every_request_maps_no_unmapped(tax):
    plans = [checkmap.plan_request(r, tax.check_map) for r in tax.requests]
    classes = collections.Counter(p.klass for p in plans)
    assert classes["unmapped"] == 0
    assert classes == {"out": 323, "in": 168, "impossible": 60, "workflow": 37, "partial": 13}


def test_split_and_direction_examples(tax):
    cm = tax.check_map
    assert checkmap.split_parts("dynamic range compression (shadow up, highlight down)", cm) == \
        ["shadow up", "highlight down"]
    assert checkmap.direction_of("increase, limited", cm)["kind"] == "capped"
    assert checkmap.direction_of("decrease toward 0", cm)["sign"] == -1
    assert checkmap.direction_of("step back by 2", cm)["steps"] == 2
    plan = checkmap.plan_request({"id": "x", "intent": "tonal", "expected": {
        "kind": "edit", "check": {"stat_or_param": "color temp up, mean luminance up",
                                  "direction": "both"}}}, cm)
    assert plan.klass == "in" and plan.capabilities == ("warmer", "brighter")


def test_every_capability_is_reachable_from_a_concept(tax):
    caps = {c for concept in tax.check_map["concepts"].values() for c in concept["caps"].values()}
    assert caps <= set(tax.check_map["capabilities"])


def test_p0_selection_is_100_with_the_genre_floor(tax):
    sel = p0.select_p0(tax.requests)
    assert len(sel) == 100 and len({r["id"] for r in sel}) == 100
    genres = collections.Counter(r["genre"] for r in sel)
    assert len(genres) == 14 and min(genres.values()) >= 3
    assert not any(r["intent"] == "impossible" for r in sel)
    assert sel == p0.select_p0(tax.requests)  # deterministic


def test_quotas_sum_to_100_with_floor_one():
    q = p0.quotas({"a": 71, "b": 1, "c": 469})
    assert sum(q.values()) == 100 and q["b"] == 1
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_taxonomy_checkmap.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.taxonomy'`.

- [ ] **Step 4: Write the implementation**

`tests/gauntlet/taxonomy.py`:

```python
"""The Scouter gauntlet's data side (spec 2.1): the four research JSONs plus
check_map.json, loaded, shape-checked and count-asserted so a silent edit to
the data fails loudly. Pure stdlib; imports nothing from scouter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

#: Spec §0: the counts the loader asserts.
EXPECTED = {
    "modules": 69, "unique_phrases": 378, "workflows": 12, "bulk": 9,
    "look_components": 16, "cannot_do": 14,
    "looks": 234, "look_families": 16,
    "look_confidence": {"documented": 127, "community-consensus": 38, "inferred": 69},
    "requests": 601, "clusters": 141, "impossible": 60, "request_genres": 14,
    "followups": 24,
    "metrics": 10, "properties": 7, "robustness": 24, "hang_causes": 4,
    "install_steps": 8, "report_sections": 8,
}


class TaxonomyError(ValueError):
    """The data does not match the counts the spec pins."""


@dataclass(frozen=True)
class Taxonomy:
    modules: list[dict]
    workflows: list[dict]
    bulk: list[dict]
    look_components: list[dict]
    cannot_do: list[dict]
    looks: list[dict]
    look_conventions: dict
    requests: list[dict]
    measurement: dict
    check_map: dict

    def unique_phrases(self) -> list[str]:
        seen: dict[str, None] = {}
        for m in self.modules:
            for p in m["phrases"]:
                seen.setdefault(p, None)
            for prm in m["params"]:
                for p in prm.get("phrases", []):
                    seen.setdefault(p, None)
        return list(seen)

    def phrase_module(self) -> dict[str, str]:
        """First module (taxonomy order) that lists each phrase."""
        out: dict[str, str] = {}
        for m in self.modules:
            for p in m["phrases"]:
                out.setdefault(p, m["id"])
            for prm in m["params"]:
                for p in prm.get("phrases", []):
                    out.setdefault(p, m["id"])
        return out

    def module_by_name(self, name: str) -> dict | None:
        key = name.strip().lower()
        for m in self.modules:
            if key in (m["id"].lower(), m["ui_name"].lower()):
                return m
        for m in self.modules:
            if key in m["ui_name"].lower() or m["ui_name"].lower() in key:
                return m
        return None


def _read(name: str, data_dir: Path) -> dict:
    return json.loads((data_dir / name).read_text(encoding="utf-8"))


def _check(label: str, got: object, want: object, errors: list[str]) -> None:
    if got != want:
        errors.append(f"{label}: expected {want}, got {got}")


def load(data_dir: Path = DATA) -> Taxonomy:
    tax = _read("taxonomy-modules.json", data_dir)
    looks = _read("looks.json", data_dir)
    reqs = _read("requests.json", data_dir)["requests"]
    meas = _read("measurement-and-robustness.json", data_dir)
    cmap = _read("check_map.json", data_dir)
    t = Taxonomy(
        modules=tax["modules"], workflows=tax["workflows"], bulk=tax["bulk"],
        look_components=tax["look_components"], cannot_do=tax["cannot_do"],
        looks=looks["looks"], look_conventions=looks["conventions"],
        requests=reqs, measurement=meas, check_map=cmap,
    )
    errors: list[str] = []
    e = EXPECTED
    _check("modules", len(t.modules), e["modules"], errors)
    _check("unique phrases", len(t.unique_phrases()), e["unique_phrases"], errors)
    _check("workflows", len(t.workflows), e["workflows"], errors)
    _check("bulk flows", len(t.bulk), e["bulk"], errors)
    _check("look components", len(t.look_components), e["look_components"], errors)
    _check("cannot-do intents", len(t.cannot_do), e["cannot_do"], errors)
    _check("looks", len(t.looks), e["looks"], errors)
    _check("look families", len({x["family"] for x in t.looks}), e["look_families"], errors)
    conf: dict[str, int] = {}
    for x in t.looks:
        conf[x["confidence"]] = conf.get(x["confidence"], 0) + 1
    _check("look confidence", conf, e["look_confidence"], errors)
    _check("requests", len(t.requests), e["requests"], errors)
    _check("clusters", len({r["cluster_id"] for r in t.requests}), e["clusters"], errors)
    _check("impossible", sum(r["intent"] == "impossible" for r in t.requests),
           e["impossible"], errors)
    _check("request genres", len({r["genre"] for r in t.requests}), e["request_genres"], errors)
    _check("follow-ups", sum(len(r["followups"]) for r in t.requests), e["followups"], errors)
    _check("metrics", len(meas["metrics"]), e["metrics"], errors)
    _check("properties", len(meas["properties"]), e["properties"], errors)
    _check("robustness", len(meas["robustness"]), e["robustness"], errors)
    _check("hang causes", len(meas["hang_causes"]), e["hang_causes"], errors)
    _check("install steps", len(meas["install_rehearsal"]["steps"]), e["install_steps"], errors)
    _check("report sections", len(meas["report_sections"]), e["report_sections"], errors)
    problems = {r["problem"] for r in t.requests}
    missing = sorted(problems - set(cmap["role_for_problem"]))
    if missing:
        errors.append(f"role_for_problem misses {missing}")
    if errors:
        raise TaxonomyError("; ".join(errors))
    return t
```

`tests/gauntlet/checkmap.py`:

```python
"""requests.json check strings -> engine checks + Scouter capabilities (spec 2.1).

Pure. The rules live in data/check_map.json; this module only applies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartCheck:
    text: str
    concept: str
    sign: int
    kind: str
    metric: str
    stat: str | None
    region: str
    capability: str | None


@dataclass(frozen=True)
class RequestPlan:
    request_id: str
    klass: str  # in | partial | out | impossible | workflow
    parts: tuple[PartCheck, ...]
    capabilities: tuple[str, ...]
    workflow_verb: str | None = None
    step_back: int = 0
    unmapped: tuple[str, ...] = field(default=())


def _strip_parens(s: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def split_parts(stat: str, cmap: dict) -> list[str]:
    s = stat.strip().lower()
    s = cmap["rewrites"].get(s, s)
    s = _strip_parens(s)
    parts = [s]
    for sep in cmap["separators"]:
        nxt: list[str] = []
        for p in parts:
            nxt.extend(x.strip() for x in p.split(sep) if x.strip())
        parts = nxt
    return parts


def direction_of(text: str, cmap: dict) -> dict:
    t = text.strip().lower()
    for rule in cmap["direction_rules"]:
        if rule["match"] in t:
            return rule
    return {"match": "", "kind": "none", "sign": 0}


def _embedded_sign(part: str, cmap: dict) -> tuple[str, int | None]:
    for w in cmap["part_direction_words"]:
        if part.endswith(w["suffix"]):
            return part[: -len(w["suffix"])].strip(), w["sign"]
    return part, None


def concept_of(part: str, cmap: dict) -> tuple[str, str | None]:
    """(concept, region override) for one stat part, or ("unmapped", None)."""
    for rule in cmap["stat_rules"]:
        if rule["match"] in part:
            region = None
            for rr in rule.get("region_rules", []):
                if rr["match"] in part:
                    region = rr["region"]
            return rule["concept"], region
    return "unmapped", None


def _capability(concept: str, sign: int, cmap: dict) -> str | None:
    caps = cmap["concepts"][concept]["caps"]
    if "*" in caps:
        return caps["*"]
    return caps.get(str(sign))


def plan_request(req: dict, cmap: dict) -> RequestPlan:
    rid = req["id"]
    kind = req["expected"]["kind"]
    check = req["expected"]["check"]
    if req["intent"] == "impossible" or kind == "refuse":
        return RequestPlan(rid, "impossible", (), ())
    if kind == "workflow":
        stat = check["stat_or_param"].lower()
        verb = "out"
        for rule in cmap["workflow_rules"]:
            if rule["match"] in stat:
                verb = rule["verb"]
                break
        d = direction_of(check["direction"], cmap)
        klass = "out" if verb == "out" else "workflow"
        return RequestPlan(rid, klass, (), (), workflow_verb=verb,
                           step_back=int(d.get("steps", 0)))
    overrides = cmap["request_overrides"].get(rid)
    if overrides:
        raw = [(o["concept"], o["concept"], o["sign"], "delta", None) for o in overrides]
    else:
        parts = split_parts(check["stat_or_param"], cmap)
        dir_text = check["direction"]
        dir_parts = [x.strip() for x in dir_text.split(",") if x.strip()]
        whole = direction_of(dir_text, cmap)
        raw = []
        for i, p in enumerate(parts):
            core, emb = _embedded_sign(p, cmap)
            concept, region = concept_of(core, cmap)
            if emb is not None:
                sign, kind_ = emb, "delta"
            elif len(dir_parts) == len(parts) and len(parts) > 1:
                d = direction_of(dir_parts[i], cmap)
                sign, kind_ = d["sign"], d["kind"]
            else:
                sign, kind_ = whole["sign"], whole["kind"]
            raw.append((p, concept, sign, kind_, region))
    out: list[PartCheck] = []
    unmapped: list[str] = []
    for text, concept, sign, kind_, region in raw:
        if concept == "unmapped":
            unmapped.append(text)
            continue
        c = cmap["concepts"][concept]
        if "fixed_sign" in c:
            sign = c["fixed_sign"]
        cap = _capability(concept, sign, cmap) if concept != "none" else None
        out.append(PartCheck(text=text, concept=concept, sign=sign, kind=kind_,
                             metric=c["metric"], stat=c.get("stat"),
                             region=region or c.get("region", "frame"), capability=cap))
    caps = tuple(dict.fromkeys(p.capability for p in out if p.capability))
    served = [p for p in out if p.capability]
    if unmapped:
        klass = "unmapped"
    elif served and len(served) == len(out):
        klass = "in"
    elif served:
        klass = "partial"
    else:
        klass = "out"
    return RequestPlan(rid, klass, tuple(out), caps, unmapped=tuple(unmapped))
```

`tests/gauntlet/p0.py`:

```python
"""The deterministic P0 selection rule (spec 3.2). Pure."""

from __future__ import annotations

import math

P0_TOTAL = 100
GENRE_FLOOR = 3


def demand_key(req: dict) -> str:
    if req["problem"] != "n/a":
        return req["problem"]
    return f"{req['intent']}:{req['modules'][0] if req['modules'] else 'none'}"


def quotas(demand: dict[str, int], total: int = P0_TOTAL) -> dict[str, int]:
    """Largest-remainder apportionment with a floor of 1 per key."""
    n = sum(demand.values())
    exact = {k: total * v / n for k, v in demand.items()}
    q = {k: max(1, math.floor(x)) for k, x in exact.items()}
    order = sorted(demand, key=lambda k: (-(exact[k] - math.floor(exact[k])), k))
    i = 0
    while sum(q.values()) < total:
        q[order[i % len(order)]] += 1
        i += 1
    shrink = sorted(demand, key=lambda k: (exact[k] - math.floor(exact[k]), -demand[k], k))
    i = 0
    while sum(q.values()) > total:
        k = shrink[i % len(shrink)]
        if q[k] > 1:
            q[k] -= 1
        i += 1
    return q


def _pick_in_key(reqs: list[dict], n: int) -> list[dict]:
    """Id order, one per cluster before any second from the same cluster."""
    ordered = sorted(reqs, key=lambda r: r["id"])
    picked: list[dict] = []
    rounds: dict[str, int] = {}
    level = 0
    while len(picked) < n and len(picked) < len(ordered):
        for r in ordered:
            if len(picked) >= n:
                break
            if r in picked:
                continue
            if rounds.get(r["cluster_id"], 0) == level:
                picked.append(r)
                rounds[r["cluster_id"]] = level + 1
        level += 1
    return picked


def select_p0(requests: list[dict]) -> list[dict]:
    pool = [r for r in requests if r["intent"] != "impossible"]
    by_key: dict[str, list[dict]] = {}
    for r in pool:
        by_key.setdefault(demand_key(r), []).append(r)
    demand = {k: len(v) for k, v in by_key.items()}
    q = quotas(demand)
    rank: dict[str, int] = {}
    picked: list[dict] = []
    for k in sorted(by_key, key=lambda k: (-demand[k], k)):
        chosen = _pick_in_key(by_key[k], q[k])
        for i, r in enumerate(chosen):
            rank[r["id"]] = i
        picked.extend(chosen)
    genres = sorted({r["genre"] for r in requests})
    for _ in range(len(genres) * GENRE_FLOOR):
        counts = {g: sum(r["genre"] == g for r in picked) for g in genres}
        short = [g for g in genres if counts[g] < GENRE_FLOOR]
        if not short:
            break
        g = short[0]
        ids = {r["id"] for r in picked}
        cands = [r for r in pool if r["genre"] == g and r["id"] not in ids]
        cands.sort(key=lambda r: (-demand[demand_key(r)], r["id"]))
        over = max(genres, key=lambda x: (counts[x], x))
        victims = [r for r in picked if r["genre"] == over]
        victims.sort(key=lambda r: (rank.get(r["id"], 0), r["id"]), reverse=True)
        if not cands or not victims:
            break
        picked.remove(victims[0])
        picked.append(cands[0])
        rank[cands[0]["id"]] = 99
    return sorted(picked, key=lambda r: r["id"])
```

`tests/gauntlet/layers/common.py`:

```python
"""Shared builders for the Scouter layer generators. Pure (reads vocab/phrases
tables, never calls darktable)."""

from __future__ import annotations

from gauntlet.case import Check, Step
from tests.gauntlet.checkmap import PartCheck

BODIES = ("olympus", "sony_a7")
#: Every MCP tool that can change the sidecar, the library or the disk.
WRITE_TOOLS = frozenset({
    "darkroom_set", "darkroom_nudge", "darkroom_enable", "darkroom_reset", "darkroom_undo_last",
    "darkroom_adjust", "darkroom_say", "history_commit", "style_extract", "style_apply",
    "style_apply_set", "export_run", "export_release", "library_import", "library_register",
    "library_relocate", "cull_apply", "config_init", "backup_run", "wb_calibrate",
    "preset_save", "preset_apply", "preset_apply_job", "job_run",
})
#: Spec 5.3 budgets (s): single move 3.5 (row 427), commit 12, look plan 40.
BUDGET_MOVE = 3.5
BUDGET_SAY = 8.0
BUDGET_COMMIT = 12.0
UNITS = {"EV": "EV", "K": "K", "%": "%", "°": "°", "ratio": "ratio", "—": "ratio"}


def frame(role: str) -> str:
    return "{frame:" + role + "}"


def step(tool: str, budget_s: float = BUDGET_MOVE, **args) -> Step:
    return Step(tool, dict(args), budget_s, tool in WRITE_TOOLS)


def key(module: str, param: str) -> str:
    return f"{module}.{param}"


def m1_equals(module: str, param: str, value: float, unit: str) -> Check:
    return Check("M1", kind="equals",
                 target={"key": key(module, param), "value": value, "unit": UNITS[unit]})


def m1_delta(module: str, param: str, delta: float, unit: str, step_size: float) -> Check:
    return Check("M1", kind="delta", target={"key": key(module, param), "delta": delta,
                                             "unit": UNITS[unit], "tol": 0.1 * step_size,
                                             "step": step_size})


def m1_restored(keys: list[str], units: dict[str, str], from_step: int = 0) -> Check:
    """Params after the last step equal snapshot `from_step` (0 = before the case)."""
    return Check("M1", kind="restored", target={"keys": keys, "units": units,
                                                "from": from_step})


def clamp_reported() -> Check:
    return Check("envelope", target={"path": "data.clamped", "op": "truthy"},
                 tags={"silent_if_fail": True, "why": "value clamped without saying so"})


def stat_check(p: PartCheck) -> Check:
    """A check_map concept -> an engine check. A concept whose metric is M1
    names a decoded param in `stat` that must be on (a module toggle, e.g.
    `lens.__enabled` once lens correction is in the vocabulary)."""
    if p.metric == "M1":
        return Check("M1", kind="equals", target={"key": p.stat, "value": 1, "unit": "bool"})
    return Check(p.metric, stat=p.stat, region=p.region, sign=p.sign,
                 kind="unchanged" if p.sign == 0 else "delta")


def sidecar(module: str | None, param: str | None, unit: str | None, *, changed: bool = True,
            allowed: list[str] | None = None) -> dict:
    d: dict = {"changed": changed}
    if module and param:
        d["key"] = key(module, param)
        d["units"] = {key(module, param): UNITS.get(unit or "ratio", "ratio")}
        d["allowed"] = allowed or []
    return d
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_taxonomy_checkmap.py -q`
Expected: `7 passed`.

- [ ] **Step 6: Commit**

```bash
git add tests/gauntlet/data/check_map.json tests/gauntlet/taxonomy.py tests/gauntlet/checkmap.py tests/gauntlet/p0.py tests/gauntlet/layers/__init__.py tests/gauntlet/layers/common.py tests/gauntlet/test_taxonomy_checkmap.py
git commit -m "gauntlet(scouter): taxonomy loader, check map, P0 selection (Task 9, spec 2.1/3.2)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part 2: Scouter adapter infrastructure

### Task 10: Merge the benchmark harness into `tests/gauntlet/benchmark/`

Not parallel (a git merge that moves a tree the drivers and Task 11 touch). Depends on: nothing.

**Files:**
- Merge: branch `bench/editor` (77bb708, 10 commits ahead of main, merge base adac31b; it only ADDS `tests/benchmark/` and `tests/test_editor_benchmark.py`, so the merge is conflict-free)
- Move: `tests/benchmark/` -> `tests/gauntlet/benchmark/`; `tests/test_editor_benchmark.py` -> `tests/gauntlet/test_editor_benchmark.py`
- Create: `tests/gauntlet/__init__.py` (empty)
- Modify: `tests/gauntlet/benchmark/run_editor_benchmark.py:52` (repo root is now three parents up), `tests/gauntlet/test_editor_benchmark.py:17` (import path), `tests/gauntlet/drivers/acceptance_driver.py:32,553-591` (bench path), `tests/gauntlet/drivers/run_acceptance.sh:12,19,27,28` (scratchpad path and bench path)

**Interfaces:**
- Produces: `tests.gauntlet.benchmark.run_editor_benchmark` with `McpClient`, `snapshot_xmp`, `compact_snapshot`, `evaluate`, `score_task`, `summarise`, `markdown`, `stage_body`, `_copy_readonly_source`, `git_info`, `DarktableHost` (Tasks 11, 13, 14 import from here).

- [ ] **Step 1: Merge and move**

```bash
git merge --no-ff bench/editor -m "Merge bench/editor: the editor benchmark harness (Task 10, row 433)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git mv tests/benchmark tests/gauntlet/benchmark
git mv tests/test_editor_benchmark.py tests/gauntlet/test_editor_benchmark.py
touch tests/gauntlet/__init__.py
```

- [ ] **Step 2: Run the moved test to see it fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_editor_benchmark.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.benchmark'`.

- [ ] **Step 3: Fix the paths**

In `tests/gauntlet/test_editor_benchmark.py` line 17:

```python
from tests.gauntlet.benchmark import run_editor_benchmark as rb
```

In `tests/gauntlet/benchmark/run_editor_benchmark.py` line 52 (the file is now one level deeper):

```python
REPO = Path(__file__).resolve().parents[3]
```

In `tests/gauntlet/drivers/acceptance_driver.py` replace the worktree with the in-repo harness. Line 32:

```python
BENCH = REPO  # row 433 task 1: the harness now lives in tests/gauntlet/benchmark on main
```

and in `step5_bench()` replace the two path literals `"tests/benchmark/run_editor_benchmark.py"` and `"tests/benchmark/results"` with `"tests/gauntlet/benchmark/run_editor_benchmark.py"` and `"tests/gauntlet/benchmark/results"`, and the log line `"from .worktrees/bench2"` with `"from tests/gauntlet/benchmark"`. Update the module docstring line 10 the same way.

In `tests/gauntlet/drivers/run_acceptance.sh` replace line 19 and lines 27-28:

```bash
SP=~/CodeBase/scouter/tests/gauntlet/drivers
```

```bash
[ -f "$REPO/tests/gauntlet/benchmark/run_editor_benchmark.py" ] || echo "note: benchmark harness missing -- step 5 will be skipped"
echo "== main $(git rev-parse --short HEAD)"
```

and the comment on line 12 to `#      tests/gauntlet/benchmark, results committed on main`. Do the same `SP=` fix in `tests/gauntlet/drivers/run_adjust_check.sh` if it references the scratchpad (`grep -n scratchpad tests/gauntlet/drivers/*.sh` must print nothing afterwards).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_editor_benchmark.py -q && grep -rn "scratchpad\|bench2\|tests/benchmark\|tests\.benchmark" tests/gauntlet --include=*.py --include=*.sh`
Expected: the benchmark suite passes (the same count as on `bench/editor`), and grep prints nothing.

- [ ] **Step 5: Commit**

```bash
git add -A tests/gauntlet tests/benchmark tests/test_editor_benchmark.py
git commit -m "gauntlet: move the benchmark harness into tests/gauntlet/benchmark; drivers point at it (Task 10, row 433)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 11: `scouter/core/dtprocess.py` and `dtbridge.health()`

Not parallel (`dtbridge.py` is also changed by Tasks 29, 32-34). Depends on: Task 10.

**Files:**
- Create: `scouter/core/dtprocess.py`
- Modify: `scouter/core/dtbridge.py` (add `health()` and `HEALTH_TIMEOUT_S`), `tests/gauntlet/benchmark/run_editor_benchmark.py` (`DarktableHost.launch/quit`), `tests/gauntlet/drivers/acceptance_driver.py` (`Darktable.launch/quit`)
- Test: `tests/test_dtprocess.py`, `tests/test_dtbridge_health.py`

**Interfaces:**
- Produces: `dtprocess.Launched(pid, start_time, exe, library, configdir, frame)`, `owner_path()` (`<cache_root>/darktable.owner.json`), `running_pids()`, `read_owner()`, `owned_pid() -> int|None` (pid alive + `ps -o lstart` equal + executable name equal), `foreign_pids()`, `launch(frame, library, configdir=None, *, log_path=None, exe=DARKTABLE) -> Launched`, `wait_gui_ready(timeout_s=30, *, poll_s=0.5, call=None) -> {"ready": bool, ...diag}` (polls `darktable_mcp.diag`, never `view.current`), `terminate(pid=None, *, responsive=None) -> {"ok", "how", ...}` (osascript skipped when `responsive is False`; SIGTERM 10 s; SIGKILL; `refused_not_ours` for any pid that is not the owned one, with no signal sent), `clear_stale_locks(library) -> list[str]`; `dtbridge.health(timeout=5.0) -> {"state": "alive"|"starting"|"unresponsive", "ping_ms", "gui_ready", "gui_ready_source"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_dtprocess.py`:

```python
"""Unit tests for scouter/core/dtprocess.py (no darktable: fake processes)."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from scouter.core import dtprocess

SLEEPER = [sys.executable, "-c", "import signal,time\n"
           "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)"]


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(dtprocess, "owner_path", lambda: tmp_path / "darktable.owner.json")
    return tmp_path


def test_launch_writes_owner_file_and_owned_pid_matches(owner, monkeypatch, tmp_path):
    fake = tmp_path / "darktable"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    rec = dtprocess.launch(None, tmp_path / "lib" / "library.db", exe=str(fake))
    try:
        data = json.loads((owner / "darktable.owner.json").read_text())
        assert data["pid"] == rec.pid and data["start_time"]
        assert dtprocess.owned_pid() in (rec.pid, None)  # comm may be `sleep` after exec
    finally:
        os.kill(rec.pid, signal.SIGKILL)


def test_foreign_pid_is_refused_and_never_signalled(owner):
    sent = []
    res = dtprocess.terminate(12345, kill=lambda p, s: sent.append((p, s)))
    assert res["how"] in ("refused_not_ours", "not running") and sent == []


def test_pid_reuse_is_not_ours(owner, monkeypatch):
    (owner / "darktable.owner.json").write_text(json.dumps({
        "pid": os.getpid(), "start_time": "Mon Jan  1 00:00:00 1990", "exe": "/x/python",
        "library": "/l", "configdir": None, "frame": None}))
    assert dtprocess.owned_pid() is None


def test_ladder_escalates_to_sigkill_without_osascript(owner, monkeypatch):
    p = subprocess.Popen(SLEEPER)
    time.sleep(0.3)
    monkeypatch.setattr(dtprocess, "owned_pid", lambda: p.pid)
    alive = lambda pid: p.poll() is None  # noqa: E731
    t0 = time.monotonic()
    res = dtprocess.terminate(p.pid, responsive=False, alive=alive)
    assert res["ok"] and res["how"] == "SIGKILL"
    assert "osascript" not in res.get("steps", [])
    assert time.monotonic() - t0 < 20


def test_wait_gui_ready_polls_diag_not_view():
    answers = iter([RuntimeError("no bridge"), {"gui_ready": False}, {"gui_ready": True,
                                                                      "gui_ready_source": "event"}])
    calls = []

    def call(method, params, timeout):
        calls.append(method)
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    t = [0.0]
    res = dtprocess.wait_gui_ready(10, call=call, clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert res["ready"] and set(calls) == {"darktable_mcp.diag"}


def test_wait_gui_ready_times_out():
    t = [0.0]
    res = dtprocess.wait_gui_ready(2, call=lambda *a, **k: {"gui_ready": False},
                                   clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert not res["ready"]


def test_stale_lock_removed_only_for_dead_owner(owner, tmp_path):
    lib = tmp_path / "library.db"
    lock = tmp_path / "library.db.lock"
    (owner / "darktable.owner.json").write_text(json.dumps({
        "pid": 999999, "start_time": "x", "exe": "darktable", "library": str(lib),
        "configdir": None, "frame": None}))
    lock.write_text("999999\n")
    assert dtprocess.clear_stale_locks(lib) == [str(lock)]
    lock.write_text("4242\n")
    assert dtprocess.clear_stale_locks(lib) == []
```

`tests/test_dtbridge_health.py`:

```python
"""dtbridge.health() (watchdog design 2.1), against FakeBridge."""

import unittest

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter.core import dtbridge
from tests.scouter_fakes import FakeBridge


class HealthTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_alive_starting_unresponsive(self) -> None:
        dtbridge.set_bridge(FakeBridge(responses={"darktable_mcp.diag": {"gui_ready": True}}))
        self.assertEqual(dtbridge.health()["state"], "alive")
        dtbridge.set_bridge(FakeBridge(responses={"darktable_mcp.diag": {"gui_ready": False}}))
        self.assertEqual(dtbridge.health()["state"], "starting")
        dtbridge.set_bridge(FakeBridge(raises={"darktable_mcp.diag": BridgeTimeoutError("x")}))
        h = dtbridge.health()
        self.assertEqual(h["state"], "unresponsive")
        self.assertIn("ping_ms", h)

    def test_ping_uses_the_5_second_budget(self) -> None:
        fake = FakeBridge(responses={"darktable_mcp.diag": {"gui_ready": True}})
        dtbridge.set_bridge(fake)
        dtbridge.health()
        self.assertEqual(fake.calls[-1][0], "darktable_mcp.diag")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dtprocess.py tests/test_dtbridge_health.py -q`
Expected: `ImportError: cannot import name 'dtprocess'` and `AttributeError: module 'scouter.core.dtbridge' has no attribute 'health'`.

- [ ] **Step 3: Write the implementation**

`scouter/core/dtprocess.py`:

```python
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


@dataclass(frozen=True)
class Launched:
    pid: int
    start_time: str
    exe: str
    library: str
    configdir: str | None
    frame: str | None


def owner_path() -> Path:
    return paths.cache_root() / OWNER_FILE


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pids() -> list[int]:
    """Every running darktable GUI process (not darktable-cli)."""
    out = subprocess.run(["pgrep", "-f", "MacOS/darktable( |$)"], capture_output=True, text=True)
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
           *, log_path: str | Path | None = None, exe: str = DARKTABLE) -> Launched:
    argv = [exe, "--library", str(library)]
    if configdir:
        argv += ["--configdir", str(configdir)]
    if frame:
        argv.append(str(frame))
    Path(library).parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
    start = None
    for _ in range(50):
        start = _lstart(proc.pid)
        if start:
            break
        time.sleep(0.1)
    rec = Launched(proc.pid, start or "", exe, str(library),
                   str(configdir) if configdir else None, str(frame) if frame else None)
    owner_path().parent.mkdir(parents=True, exist_ok=True)
    owner_path().write_text(json.dumps(asdict(rec)) + "\n", encoding="utf-8")
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
```

`scouter/core/dtbridge.py` (apply this diff):

```diff
--- a/scouter/core/dtbridge.py
+++ b/scouter/core/dtbridge.py
@@ -3,6 +3,7 @@
 from __future__ import annotations
 
 import re
+import time
 from typing import Any
 
 from darktable_mcp.bridge.client import (
@@ -213,6 +214,27 @@
         raise BridgeTimeout(
             f"bridge method {method} failed: {message}", detail={"method": method}
         ) from e
+
+
+#: Watchdog design 2.1: the ping budget. Only ever called after a timeout.
+HEALTH_TIMEOUT_S = 5.0
+
+
+def health(timeout: float = HEALTH_TIMEOUT_S) -> dict:
+    """One `darktable_mcp.diag` call with a 5 s budget: `alive` (answered,
+    GUI ready), `starting` (answered, gui_ready false) or `unresponsive` (no
+    answer). diag is exempt from the plugin's GUI gate and touches no
+    gtk-wrapped API, so an answer proves the Lua worker is alive."""
+    t0 = time.monotonic()
+    try:
+        d = get_bridge().call("darktable_mcp.diag", {}, timeout=timeout) or {}
+    except Exception as e:  # noqa: BLE001 - any failure to answer is "unresponsive"
+        return {"state": "unresponsive", "ping_ms": int(1000 * (time.monotonic() - t0)),
+                "error": str(e)[:200]}
+    ready = d.get("gui_ready")
+    return {"state": "starting" if ready is False else "alive",
+            "ping_ms": int(1000 * (time.monotonic() - t0)), "gui_ready": ready,
+            "gui_ready_source": d.get("gui_ready_source")}
 
 
 def current_view() -> str:
```

- [ ] **Step 4: Route both drivers through dtprocess**

In `tests/gauntlet/benchmark/run_editor_benchmark.py`, `DarktableHost.launch`: replace the `subprocess.Popen([...])` block with

```python
        from scouter.core import dtprocess

        self.launched = dtprocess.launch(frame, self.library_db, log_path=self.log_path)
```

keep the darkroom polling, and before the final `return {...}` insert

```python
        ready = dtprocess.wait_gui_ready(30.0)
```

adding `"gui_ready": ready.get("ready")` to the returned dict. Replace the body of `DarktableHost.quit` with

```python
        from scouter.core import dtprocess

        res = dtprocess.terminate(responsive=self.alive())
        self.proc = None
        return {"ok": res["ok"], "how": res["how"]}
```

In `tests/gauntlet/drivers/acceptance_driver.py`, `Darktable.launch`: replace the `subprocess.Popen` line with `dtprocess.launch(frame, library, log_path=RUN / "darktable.log")` (import `from scouter.core import dtprocess` at the top of the method); `Darktable.quit`: replace the osascript/signal ladder with `res = dtprocess.terminate(responsive=None)` and `how = res["how"]`, keeping the `record(...)` call.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_dtprocess.py tests/test_dtbridge_health.py tests/gauntlet/test_editor_benchmark.py tests/test_dtbridge.py -q`
Expected: all pass (`7 passed` + `2 passed` for the new files).

- [ ] **Step 6: Commit**

```bash
git add scouter/core/dtprocess.py scouter/core/dtbridge.py tests/test_dtprocess.py tests/test_dtbridge_health.py tests/gauntlet/benchmark/run_editor_benchmark.py tests/gauntlet/drivers/acceptance_driver.py
git commit -m "scouter: dtprocess (owned pid, quit ladder, gui_ready wait) and dtbridge.health (Task 11, spec 6)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 12: Corpus: originals guard, copies, survey, roles, bulk cards

PARALLEL-OK. Depends on: Task 10. Step 6 is **LIVE** (controller).

**Files:**
- Create: `tests/gauntlet/corpus.py`, `tests/gauntlet/corpus_survey.py`
- Test: `tests/gauntlet/test_corpus.py`

**Interfaces:**
- Produces: `TRIAL`, `SCRATCH` (`~/Pictures/ScouterScratch/gauntlet`), `OriginalPathRefused`, `under_trial(path)`, `guard(path) -> str`, `copy_readonly(src, dst) -> Path`, `originals()`, `sha256_originals()`, `free_space_ok(path, need_bytes)`, `Surveyed`, `survey(render_stats, exif_model_iso, work, root=None)`, `pick_roles(frames)`, `Corpus(doc, root)` with `load(path)`, `write(path, frames)`, `roles(body) -> {role: source Path}`, `job_sources(body, n)`; `build_card(corpus, body, n, card_dir) -> [DUP####_<orig>]`, `delete_card(card_root)`. `python -m tests.gauntlet.corpus_survey` writes `<runs_root>/corpus.json`.

- [ ] **Step 1: Write the failing test**

```python
import os

import pytest

from tests.gauntlet import corpus


@pytest.fixture
def trial(tmp_path, monkeypatch):
    root = tmp_path / "EditingTrial"
    (root / "sony").mkdir(parents=True)
    for n in ("P1.ORF", "P2.ORF", "P3.ORF"):
        (root / n).write_bytes(n.encode() * 10)
    (root / "sony" / "DSC1.ARW").write_bytes(b"a7" * 10)
    (root / "P9.JPG").write_bytes(b"jpg")
    monkeypatch.setattr(corpus, "TRIAL", root)
    return root


def test_guard_refuses_originals_and_symlinks(trial, tmp_path):
    with pytest.raises(corpus.OriginalPathRefused):
        corpus.guard(trial / "P1.ORF")
    link = tmp_path / "link.ORF"
    os.symlink(trial / "P1.ORF", link)
    with pytest.raises(corpus.OriginalPathRefused):
        corpus.guard(link)
    assert corpus.guard(tmp_path / "copy.ORF")


def test_copy_is_0644_and_original_untouched(trial, tmp_path):
    before = corpus.sha256_originals(trial)
    dst = corpus.copy_readonly(trial / "P1.ORF", tmp_path / "out" / "P1.ORF")
    assert oct(dst.stat().st_mode & 0o777) == "0o644"
    with pytest.raises(corpus.OriginalPathRefused):
        corpus.copy_readonly(tmp_path / "out" / "P1.ORF", trial / "P1-copy.ORF")
    assert corpus.sha256_originals(trial) == before and len(before) == 4


def test_survey_roles_and_bench_override(trial, tmp_path):
    stats = {"P1.ORF": (20, 40, 200, 0.0, 1.3, 0.8), "P2.ORF": (60, 20, 250, 0.5, 0.9, 1.1),
             "P3.ORF": (45, 60, 230, 0.001, 1.0, 1.0), "sony/DSC1.ARW": (50, 50, 220, 0, 1, 1)}

    def render(p):
        rel = str(p.relative_to(tmp_path / "work"))
        L, sp, p99, clip, rg, bg = stats[rel]
        return {"L_mean": L, "spread": sp, "p99": p99, "clip_high": clip, "rg": rg, "bg": bg}

    def exif(p):
        return ("ILCE-7", 100) if p.suffix == ".ARW" else ("E-M10 Mark III", 200 if "P2" in
                                                            p.name else 6400)

    frames = corpus.survey(render, exif, tmp_path / "work", trial)
    assert not list((tmp_path / "work").rglob("*.ORF"))  # survey copies are deleted
    roles = corpus.pick_roles(frames)
    assert roles["olympus"]["dark"] == "P1.ORF" and roles["olympus"]["blown"] == "P2.ORF"
    assert roles["olympus"]["flat"] == "P2.ORF" and roles["olympus"]["noisy"] == "P1.ORF"
    assert roles["sony_a7"]["hero"] == "sony/DSC1.ARW"


def test_card_duplication_round_robin_and_disk_check(trial, tmp_path, monkeypatch):
    doc = {"frames": [{"rel": "P1.ORF", "body": "olympus"}, {"rel": "P2.ORF", "body": "olympus"}],
           "roles": {}}
    c = corpus.Corpus(doc, trial)
    files = corpus.build_card(c, "olympus", 5, tmp_path / "card")
    assert [f.name for f in files] == ["DUP0000_P1.ORF", "DUP0001_P2.ORF", "DUP0002_P1.ORF",
                                       "DUP0003_P2.ORF", "DUP0004_P1.ORF"]
    monkeypatch.setattr(corpus, "free_space_ok", lambda p, n: False)
    with pytest.raises(OSError, match="free disk"):
        corpus.build_card(c, "olympus", 120, tmp_path / "card2")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_corpus.py -q`
Expected: `ImportError: cannot import name 'corpus'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/corpus.py`:

```python
"""The image corpus (spec 4): originals guard, read-only copies, the one-time
survey, frame roles, and bulk cards built by duplication.

EditingTrial originals are never opened, imported, edited or exported: they
are only READ to make 0644 copies under the scratch root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

TRIAL = Path(os.environ.get("HTC_TRIAL", "~/Pictures/EditingTrial"))
SCRATCH = Path.home() / "Pictures" / "ScouterScratch" / "gauntlet"
RAW_EXT = {".orf", ".arw"}
MODEL_BODY = {"E-M10 Mark III": "olympus", "E-M10MarkIII": "olympus", "ILCE-7": "sony_a7",
              "NEX-5N": "sony_nex5n"}
#: The bench's frames block (editor_tasks.json) wins where it names a role.
BENCH_ROLES = {
    "olympus": {"dark": "P5240151.ORF", "bright": "P6281368.ORF", "blown": "P5240140.ORF",
                "hero": "P6281348.ORF"},
    "sony_a7": {"dark": "sony/DSC01425.ARW", "blown": "sony/DSC01431.ARW",
                "hero": "sony/DSC01426.ARW"},
}
ROLES = ("dark", "bright", "blown", "hero", "flat", "warm_cast", "cool_cast", "noisy", "neutral")
FRAME_BYTES = 20 * 1024 * 1024


class OriginalPathRefused(ValueError):
    """A path under the EditingTrial originals was about to be used."""


def under_trial(path: str | Path) -> bool:
    real = os.path.realpath(os.path.expanduser(str(path)))
    root = os.path.realpath(str(TRIAL))
    return real == root or real.startswith(root + os.sep)


def guard(path: str | Path) -> str:
    if under_trial(path):
        raise OriginalPathRefused(f"refusing an EditingTrial original: {path}")
    return str(path)


def copy_readonly(src: Path, dst: Path) -> Path:
    """Read an original, write a writable 0644 copy (never the other way)."""
    guard(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(0o644)
    return dst


def originals(root: Path | None = None) -> list[Path]:
    root = root or TRIAL
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in RAW_EXT and p.is_file())


def sha256_originals(root: Path | None = None) -> dict[str, str]:
    out = {}
    base = root or TRIAL
    for p in originals(base):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out[str(p.relative_to(base))] = h.hexdigest()
    return out


def free_space_ok(path: Path, need_bytes: int) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free > need_bytes + 2 * 1024**3  # keep 2 GB spare


@dataclass
class Surveyed:
    rel: str
    body: str
    iso: int
    L_mean: float
    spread: float
    p99: float
    clip_high: float
    rg: float
    bg: float


def survey(render_stats, exif_model_iso, work: Path, root: Path | None = None) -> list[Surveyed]:
    """render_stats(copy_path) -> {"L_mean","spread","p99","clip_high","rg","bg"};
    exif_model_iso(copy_path) -> (model, iso). Runs on COPIES in `work`."""
    base = root or TRIAL
    out = []
    for src in originals(base):
        rel = str(src.relative_to(base))
        copy = copy_readonly(src, work / rel)
        model, iso = exif_model_iso(copy)
        body = MODEL_BODY.get(model.strip(), "unknown")
        s = render_stats(copy)
        out.append(Surveyed(rel, body, int(iso or 0), s["L_mean"], s["spread"], s["p99"],
                            s["clip_high"], s["rg"], s["bg"]))
        copy.unlink()
    return out


def pick_roles(frames: list[Surveyed]) -> dict[str, dict[str, str]]:
    roles: dict[str, dict[str, str]] = {}
    for body in sorted({f.body for f in frames} - {"unknown"}):
        fs = sorted((f for f in frames if f.body == body), key=lambda f: f.rel)
        med = sorted(f.L_mean for f in fs)[len(fs) // 2]
        pick = {
            "dark": min(fs, key=lambda f: f.L_mean),
            "bright": max((f for f in fs if f.clip_high < 0.01) or fs, key=lambda f: f.p99),
            "blown": max(fs, key=lambda f: f.clip_high),
            "hero": min(fs, key=lambda f: abs(f.L_mean - med)),
            "flat": min(fs, key=lambda f: f.spread),
            "warm_cast": max(fs, key=lambda f: f.rg),
            "cool_cast": min(fs, key=lambda f: f.rg),
            "noisy": max(fs, key=lambda f: f.iso),
            "neutral": min(fs, key=lambda f: abs(f.rg - 1) + abs(f.bg - 1)),
        }
        roles[body] = {r: f.rel for r, f in pick.items()}
        for r, rel in BENCH_ROLES.get(body, {}).items():
            if any(f.rel == rel for f in fs):
                roles[body][r] = rel
    return roles


class Corpus:
    """corpus.json: {"frames": [Surveyed...], "roles": {body: {role: rel}}}."""

    def __init__(self, doc: dict, root: Path | None = None) -> None:
        self.doc = doc
        self.root = root or TRIAL

    @classmethod
    def load(cls, path: Path, root: Path | None = None) -> Corpus:
        return cls(json.loads(path.read_text(encoding="utf-8")), root)

    @staticmethod
    def write(path: Path, frames: list[Surveyed]) -> dict:
        doc = {"schema": "scouter-gauntlet-corpus/1", "frames": [asdict(f) for f in frames],
               "roles": pick_roles(frames)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return doc

    def roles(self, body: str) -> dict[str, Path]:
        return {r: self.root / rel for r, rel in self.doc["roles"][body].items()}

    def job_sources(self, body: str, n: int) -> list[Path]:
        fs = sorted(f["rel"] for f in self.doc["frames"] if f["body"] == body)
        return [self.root / fs[i % len(fs)] for i in range(n)]


def build_card(corpus: Corpus, body: str, n: int, card_dir: Path) -> list[Path]:
    """`DUP####_<orig>` copies, round robin (spec 3.3). Refuses when the disk
    cannot hold them plus 2 GB."""
    if not free_space_ok(card_dir, n * FRAME_BYTES):
        raise OSError(f"not enough free disk for a {n}-frame card under {card_dir}")
    out = []
    for i, src in enumerate(corpus.job_sources(body, n)):
        out.append(copy_readonly(src, card_dir / "DCIM" / "100GAUNT" / f"DUP{i:04d}_{src.name}"))
    return out


def delete_card(card_root: Path) -> None:
    guard(card_root)
    shutil.rmtree(card_root, ignore_errors=True)
```

`tests/gauntlet/corpus_survey.py` (the LIVE entry point; it imports `isolate_process` from Task 28's adapter, so it is only RUN after Task 28 lands):

```python
"""LIVE (controller-run, darktable-cli): the one-time corpus survey (spec 4.2).

    .venv/bin/python -m tests.gauntlet.corpus_survey [--runs-root DIR]

Renders a 512 px default-pipeline PPM of a COPY of every EditingTrial raw
(each in its own throwaway configdir, via scout.render), reads EXIF from the
copy, deletes the copy, and writes <runs-root>/corpus.json with the roles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gauntlet import metrics
from gauntlet.raster import parse_ppm
from tests.gauntlet import corpus


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-root", default=str(corpus.SCRATCH))
    args = ap.parse_args(argv)
    root = Path(args.runs_root).expanduser()
    from tests.gauntlet.adapter import isolate_process

    isolate_process(root / "_process")
    from scouter.core import exif, scout

    work = root / "survey-work"

    def render_stats(copy: Path) -> dict:
        out = work / "render" / (copy.name + ".ppm")
        out.parent.mkdir(parents=True, exist_ok=True)
        scout.render(str(copy), out, 512, "ppm")
        r = parse_ppm(out.read_bytes())
        s = metrics.region_stats(r, metrics.build_masks(r), ("frame",))["frame"]
        out.unlink()
        return {"L_mean": s["L"], "spread": s["spread"], "p99": s["p99L"],
                "clip_high": s["clip_high"], "rg": s["rg"], "bg": s["bg"]}

    def exif_model_iso(copy: Path) -> tuple[str, int]:
        tags = exif.read_tags(copy)
        return (getattr(tags, "model", "") or "", int(getattr(tags, "iso", 0) or 0))

    before = corpus.sha256_originals()
    frames = corpus.survey(render_stats, exif_model_iso, work)
    doc = corpus.Corpus.write(root / "corpus.json", frames)
    assert corpus.sha256_originals() == before, "an original changed during the survey"
    print(f"{root / 'corpus.json'}: {len(frames)} frames; roles "
          f"{ {b: sorted(r) for b, r in doc['roles'].items()} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_corpus.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/corpus.py tests/gauntlet/corpus_survey.py tests/gauntlet/test_corpus.py
git commit -m "gauntlet(scouter): corpus guard, read-only copies, survey, roles, bulk cards (Task 12, spec 4)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6 (LIVE, controller, after Task 28): run the survey once**

Run (darktable closed not required: darktable-cli only): `.venv/bin/python -m tests.gauntlet.corpus_survey`
Expected: `~/Pictures/ScouterScratch/gauntlet/corpus.json: 55 frames; roles {'olympus': [...9 roles], 'sony_a7': [...], 'sony_nex5n': [...]}`, about 15 min. The script asserts the originals' sha256 are unchanged. Commit nothing (corpus.json lives in the scratch root).

### Task 13: Session

PARALLEL-OK. Depends on: Tasks 1, 11, 12.

**Files:**
- Create: `tests/gauntlet/session.py`
- Test: `tests/gauntlet/test_session.py`

**Interfaces:**
- Consumes: `dtprocess` (through `DtControl`), `corpus.Corpus/copy_readonly`, `tests.gauntlet.benchmark.run_editor_benchmark.McpClient`.
- Produces: `SessionBlocked`, `ForeignDarktable`, `Staged(root, frames, job_frames, card, config, cache, vault, library, env, vars)`, `isolated_env(root, job=None)`, `write_config(root)`, `stage(corpus, body, root, job_size=7, chart=None)`, `DtControl` (`foreign/owned/launch/ready/health/terminate`), `ScouterSession(corpus, root, body, *, dt=None, mcp_factory=None, clock, run_root=None, floors_fn=None)` with `open()`, `relaunch(frame=None)`, `refresh_frame(role)`, `abort()`, `close()`, `call(tool, args, budget_s, *, write=False) -> CallRecord`, `ensure_frame(role) -> Path`, `relative(path)`; attributes `staged, mcp, touched, baselines, floors, previews_dir, run_root, body, corpus, dt, mcp_factory`. `test_session.FakeDt`, `FakeMcp`, `_session` are reused by Task 14's tests.

- [ ] **Step 1: Write the failing test**

```python
"""Session + tool-mode execution against fakes: no darktable, no MCP server."""


import pytest

from tests.gauntlet import session as sess
from tests.gauntlet.corpus import Corpus


class FakeDt:
    def __init__(self, foreign=(), owned=None, ready=(True,), health="alive"):
        self._foreign = list(foreign)
        self._owned = owned
        self._ready = list(ready)
        self._health = health
        self.launches = 0
        self.terminated = []

    def foreign(self):
        return self._foreign

    def owned(self):
        return self._owned

    def launch(self, frame, library, log):
        self.launches += 1

    def ready(self, timeout_s):
        return {"ready": self._ready.pop(0) if self._ready else True}

    def health(self):
        return self._health

    def terminate(self, *, responsive=None):
        self.terminated.append(responsive)
        self._owned = None
        return {"ok": True}


class FakeMcp:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def call_tool(self, tool, args, timeout):
        self.calls.append((tool, args))
        a = self.answers.get(tool, {"ok": True, "outcome": "ok", "data": {}})
        if isinstance(a, Exception):
            raise a
        return a

    def close(self):
        pass


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def _session(corpus, tmp_path, dt=None, mcp=None):
    s = sess.ScouterSession(corpus, tmp_path / "s1", "olympus", dt=dt or FakeDt(),
                            mcp_factory=lambda env, cwd: mcp or FakeMcp())
    return s


def test_refuses_a_darktable_it_did_not_launch(corpus, tmp_path):
    s = _session(corpus, tmp_path, FakeDt(foreign=[4242]))
    with pytest.raises(sess.ForeignDarktable):
        s.open()


def test_quits_its_own_leftover_darktable_from_a_killed_run(corpus, tmp_path):
    dt = FakeDt(owned=777)
    _session(corpus, tmp_path, dt).open()
    assert dt.terminated[0] is False and dt.launches == 1


def test_launch_retries_once_then_blocks(corpus, tmp_path):
    dt = FakeDt(ready=(False, True))
    _session(corpus, tmp_path, dt).open()
    assert dt.launches == 2
    with pytest.raises(sess.SessionBlocked):
        _session(corpus, tmp_path, FakeDt(ready=(False, False))).open()


def test_session_env_is_isolated(corpus, tmp_path):
    s = _session(corpus, tmp_path).open()
    env = s.staged.env
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH"):
        assert env[k].startswith(str(tmp_path / "s1"))
    assert s.staged.frames["hero"].read_bytes() == b"raw"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_session.py -q`
Expected: `ImportError: cannot import name 'session'`.

- [ ] **Step 3: Write the implementation**

```python
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

from gauntlet.adapter import CallRecord
from tests.gauntlet.corpus import Corpus, copy_readonly

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable
LAUNCH_READY_S = 30.0
EXPORT_PRESETS = ("client-proof", "preview")


class SessionBlocked(RuntimeError):
    """darktable could not be brought to gui_ready (twice)."""


class ForeignDarktable(RuntimeError):
    """A darktable this runner did not launch is running: refuse to start."""


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
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "SCOUTER_JOB", "OBSIDIAN_VAULT_PATH"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO)
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
                  vars={"job_slug": f"htc-{body}-{stamp}"[-40:], "card": str(root / "card"),
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

    # -- lifecycle -------------------------------------------------------

    def _real_mcp(self, env: dict[str, str], cwd: Path):
        from tests.gauntlet.benchmark.run_editor_benchmark import McpClient

        client = McpClient([PY, "-m", "scouter.mcp.server"], env, cwd, cwd / "mcp.stderr")
        client.initialize()
        return client

    def open(self) -> ScouterSession:
        foreign = self.dt.foreign()
        if foreign:
            raise ForeignDarktable(f"darktable pids {foreign} were not launched by the "
                                   "gauntlet; quit them first (the runner never signals them)")
        if self.dt.owned() is not None:  # left over from a killed run: ours, so quit it
            self.dt.terminate(responsive=False)
        chart = self.run_root / "chart" / "chart-v1.pfm"
        self.staged = stage(self.corpus, self.body, self.root, chart=chart)
        self._launch(self.staged.frames["hero"])
        self.mcp = self.mcp_factory(self.staged.env, self.root)
        if self.floors_fn is not None:
            self.floors, deterministic = self.floors_fn(self.staged.frames["hero"])
            if not deterministic:
                raise SessionBlocked("harness_bug: the untouched frame renders differently "
                                     "twice (noise floor > 2x minimum, spec 5.2)")
        return self

    def relative(self, path: Path) -> str:
        return str(Path(path).relative_to(self.run_root))

    def _launch(self, frame: Path) -> None:
        assert self.staged is not None
        for attempt in (1, 2):
            self.dt.launch(frame, self.staged.library, self.root / "darktable.log")
            ready = self.dt.ready(LAUNCH_READY_S)
            if ready.get("ready"):
                self.current_frame = frame
                return
            self.dt.terminate(responsive=False)
        raise SessionBlocked("launch: darktable not gui_ready after 30 s, twice")

    def relaunch(self, frame: Path | None = None) -> None:
        self.dt.terminate(responsive=False)
        self._launch(frame or self.current_frame or self.staged.frames["hero"])  # type: ignore[union-attr]

    def refresh_frame(self, role: str) -> Path:
        """Contaminated: re-stage this role from a fresh copy and relaunch on it."""
        assert self.staged is not None
        src = self.corpus.roles(self.body)[role] if role != "chart" else \
            self.run_root / "chart" / "chart-v1.pfm"
        n = sum(1 for k in self.baselines if role in k) + 1
        dst = copy_readonly(src, self.root / "frames" / f"{role}-fresh{n}" / src.name)
        self.baselines.pop(str(self.staged.frames[role]), None)
        self.staged.frames[role] = dst
        self.relaunch(dst)
        return dst

    def abort(self) -> None:
        self.dt.terminate(responsive=False)
        if self.mcp is not None:
            try:
                self.mcp.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.mcp = None

    def close(self) -> None:
        if self.mcp is not None:
            self.mcp.close()
            self.mcp = None
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
        rec.wall_s = round(self.clock() - t0, 3)
        if write and tool.startswith("darkroom_") and "module" in args:
            self.touched.add(str(args["module"]))
        if write and tool in ("darkroom_say", "preset_apply", "darkroom_undo_last"):
            self.touched.add("*")
        return rec

    def ensure_frame(self, role: str) -> Path:
        assert self.staged is not None
        path = self.staged.frames[role]
        if self.current_frame != path:
            rec = self.call("darkroom_open", {"image": str(path)}, 30.0)
            if rec.error == "timeout" and rec.health == "unresponsive":
                self.relaunch(path)
            self.current_frame = path
        return path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_session.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/session.py tests/gauntlet/test_session.py
git commit -m "gauntlet(scouter): session (owned darktable, gui_ready launch, isolated env, relaunch) (Task 13, spec 2.3)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 14: Observe, tool-mode execute, policy

PARALLEL-OK. Depends on: Tasks 4, 5, 13.

**Files:**
- Create: `tests/gauntlet/observe.py`, `tests/gauntlet/execute.py`, `tests/gauntlet/policy.py`
- Test: `tests/gauntlet/test_execute.py`

**Interfaces:**
- Consumes: `snapshot_xmp` (benchmark), `scout.render`, `wb.kelvin`, `vocab.PARAMS`, `gauntlet.metrics/chart/raster`, `ScouterSession`.
- Produces: `observe.units()`, `flatten(snap, kelvin=None) -> {fingerprint, history_end, params, units}` (params `"<op>.<name>"`, `"<op>.__enabled"`, `temperature.temperature` via the WB table), `snapshot(raw, kelvin=None)`, `kelvin_for(camera)`, `render_raster(raw, out, px=1024) -> Raster8`, `render_preview(raw, out, px=512)`, `stats(raster, masks=None) -> (stats, masks)`, `noise_floors(raw, work) -> (floors, deterministic)`; `execute.VIRTUAL: dict[str, fn(session, case, step, obs) -> CallRecord]` (layer executors register here), `CAMERA`, `substitute(value, session, calls)` (`{frame:role}`, `{job_frame:N}`, `{job_frames}`, `{step:N:path}`, session vars; originals guard), `needs_stats(case)`, `isolate(session, frame, role) -> bool` (reset touched modules, compare to the frame's baseline, re-stage when contaminated), `run_tool_case(session, case) -> Observation`; `policy.POLICY`, `load_policy()`.

- [ ] **Step 1: Write the failing test**

```python
"""Tool-mode execution against the session fakes: no darktable, no MCP server."""

import pytest

from gauntlet.case import Case, Check, Expect, Step
from tests.gauntlet import execute, observe
from tests.gauntlet.corpus import Corpus, OriginalPathRefused
from tests.gauntlet.test_session import FakeDt, FakeMcp, _session


@pytest.fixture
def corpus(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}],
           "roles": {"olympus": {"hero": "H.ORF", "dark": "H.ORF"}}}
    return Corpus(doc, trial)


def test_timeout_with_unresponsive_ping_blocks_as_hang(corpus, tmp_path, monkeypatch):
    mcp = FakeMcp({"darkroom_say": TimeoutError("no answer")})
    s = _session(corpus, tmp_path, FakeDt(health="unresponsive"), mcp).open()
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                 "params": {}, "units": {}})
    case = Case(id="c", layer="scenarios", tier="P0", mode="tool",
                steps=[Step("darkroom_say", {"phrase": "brighter"}, 8.0, True)],
                expect=Expect("edit"), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert obs.blocked == "hang" and obs.calls[0].health == "unresponsive"


def test_placeholders_and_originals_guard(corpus, tmp_path, monkeypatch):
    s = _session(corpus, tmp_path).open()
    out = execute.substitute({"image": "{frame:hero}", "slug": "{job_slug}"}, s, [])
    assert out["image"].endswith("H.ORF") and out["image"].startswith(str(tmp_path / "s1"))
    monkeypatch.setattr("tests.gauntlet.corpus.TRIAL", corpus.root)
    with pytest.raises(OriginalPathRefused):
        execute.substitute(str(corpus.root / "H.ORF"), s, [])


def test_step_snapshots_are_recorded_per_write(corpus, tmp_path, monkeypatch):
    s = _session(corpus, tmp_path).open()
    seq = iter([0.7, 0.7, 0.7, 1.0, 1.3])  # isolate, before, step 1, step 2, after
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x", "params": {"exposure.exposure": next(seq)}, "units": {}})
    case = Case(id="c2", layer="coverage", tier="P1", mode="tool",
                steps=[Step("darkroom_set", {"module": "exposure", "param": "exposure",
                                             "value": 1.0}, 3.5, True),
                       Step("darkroom_adjust", {"module": "exposure", "param": "exposure",
                                                "delta": 0.3}, 3.5, True)],
                expect=Expect("edit", [Check("M1")]), body="olympus", frame_role="hero")
    obs = execute.run_tool_case(s, case)
    assert [x["exposure.exposure"] for x in obs.extra["snapshots"]] == [0.7, 0.7, 1.0]
    assert s.touched == {"exposure"}


def test_flatten_drops_unnamed_slots_and_adds_enabled():
    snap = {"fingerprint": "f", "history_end": 3, "modules": {
        "sigmoid": {"enabled": True, "decoded": {"contrast": 1.5, "f2": 100.0}},
        "temperature": {"enabled": True, "decoded": {"coeff_r": 2.0, "coeff_g": 1.0,
                                                      "coeff_b": 1.7, "preset": 2}}}}
    flat = observe.flatten(snap, kelvin=lambda c: 5500.0)
    p = flat["params"]
    assert p["sigmoid.contrast"] == 1.5 and "sigmoid.f2" not in p
    assert p["sigmoid.__enabled"] == 1 and p["temperature.temperature"] == 5500.0
    assert flat["units"]["sigmoid.__enabled"] == "bool"


def test_contaminated_frame_is_restaged(corpus, tmp_path, monkeypatch):
    dt = FakeDt()
    s = _session(corpus, tmp_path, dt).open()
    frame = s.ensure_frame("hero")
    s.baselines[str(frame)] = {"exposure.exposure": 0.7}
    monkeypatch.setattr(observe, "snapshot", lambda p, k=None: {
        "fingerprint": "x", "params": {"exposure.exposure": 1.3}, "units": {}})
    assert execute.isolate(s, frame, "hero") is True
    assert s.staged.frames["hero"] != frame and dt.launches == 2
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_execute.py -q`
Expected: `ImportError: cannot import name 'execute'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/observe.py`:

```python
"""What a case observes (spec 2.4): decoded XMP params (M1) and rendered stats
(M2-M8). The snapshot reuses the benchmark's snapshot_xmp; every op with a
decoder is kept (SNAPSHOT_OPS extended to all of them)."""

from __future__ import annotations

import re
from pathlib import Path

from gauntlet import metrics
from gauntlet.raster import parse_ppm

_UNNAMED = re.compile(r"^f\d+$")
PREVIEW_PX = 1024


def units() -> dict[str, str]:
    from scouter.core import vocab

    out = {f"{s.module}.{s.param}": s.unit for s in vocab.PARAMS}
    out.update({k: "°" for k, v in out.items() if v == "°"})
    return {k: ("ratio" if v in ("—",) else v) for k, v in out.items()}


def flatten(snap: dict, kelvin=None) -> dict:
    """{fingerprint, history_end, params: {"op.param": value}, units}. Unnamed
    float slots (f0..fN) are dropped; `op.__enabled` is 1/0; the temperature
    coefficients add `temperature.temperature` through `kelvin(coeffs)` when a
    WB table can invert them."""
    params: dict[str, float] = {}
    for op, mod in (snap.get("modules") or {}).items():
        params[f"{op}.__enabled"] = 1 if mod.get("enabled") else 0
        for name, v in (mod.get("decoded") or {}).items():
            if _UNNAMED.match(name) or name.startswith("_") or not isinstance(v, (int, float)):
                continue
            params[f"{op}.{name}"] = v
    if kelvin is not None and "temperature.coeff_r" in params:
        k = kelvin([params["temperature.coeff_r"], params["temperature.coeff_g"],
                    params["temperature.coeff_b"]])
        if k is not None:
            params["temperature.temperature"] = k
    u = units()
    u.update({k: "bool" for k in params if k.endswith(".__enabled")})
    return {"fingerprint": snap.get("fingerprint", ""), "history_end": snap.get("history_end", 0),
            "params": params, "units": u}


def snapshot(raw_path: Path, kelvin=None) -> dict:
    from tests.gauntlet.benchmark.run_editor_benchmark import snapshot_xmp

    return flatten(snapshot_xmp(raw_path), kelvin)


def kelvin_for(camera: str | None):
    """A coeffs -> Kelvin inverter against the body's table, or None."""
    if not camera:
        return None
    from scouter.core import wb

    def inv(coeffs):
        try:
            return wb.kelvin(coeffs, camera).get("kelvin")
        except Exception:  # noqa: BLE001 - no table, or out of range
            return None

    return inv


def render_raster(raw_path: Path, out: Path, px: int = PREVIEW_PX):
    """darktable-cli render of raw + its sidecar to PPM (scout.render: own
    throwaway configdir per call), parsed by the engine."""
    from scouter.core import scout

    out.parent.mkdir(parents=True, exist_ok=True)
    scout.render(str(raw_path), out, px, "ppm")
    return parse_ppm(out.read_bytes())


def render_preview(raw_path: Path, out: Path, px: int = 512) -> Path:
    from scouter.core import scout

    out.parent.mkdir(parents=True, exist_ok=True)
    scout.render(str(raw_path), out, px, "png")
    return out


def stats(raster, masks: metrics.Masks | None = None):
    masks = masks or metrics.build_masks(raster)
    return metrics.region_stats(raster, masks), masks


def noise_floors(raw_path: Path, work: Path) -> tuple[dict, bool]:
    """Spec 5.2: export the untouched frame twice; floor = 3 x the difference."""
    a = render_raster(raw_path, work / "floor-a.ppm")
    b = render_raster(raw_path, work / "floor-b.ppm")
    masks = metrics.build_masks(a)
    return metrics.noise_floor(metrics.region_stats(a, masks, ("frame",)),
                               metrics.region_stats(b, masks, ("frame",)))
```

`tests/gauntlet/execute.py`:

```python
"""Tool-mode case execution (spec 2.3 'Per case'): isolation, before
snapshot/stats, the steps with placeholders, after snapshot/stats."""

from __future__ import annotations

import re
import time
from pathlib import Path

from gauntlet import chart
from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.corpus import guard

_PH = re.compile(r"\{([a-z_0-9]+)(?::([^}]+))?\}")
#: Virtual tools, registered by the layer executors (tests/gauntlet/exec_*.py):
#: name -> fn(session, case, step, obs) -> CallRecord.
VIRTUAL: dict = {}
STAT_METRICS = {"M2", "M3", "M4", "M5", "M6", "M7", "M8"}
#: body -> WB table camera id (config `cameras` map).
CAMERA = {"olympus": "em10iii", "sony_a7": "a7", "sony_nex5n": "nex5n"}


def substitute(value, session, calls: list[CallRecord]):
    """Fill {frame:role}, {job_frame:N}, {job_frames}, {step:N:path} and session
    vars. Every resulting path is checked against the originals guard."""
    st = session.staged

    def one(m: re.Match) -> str:
        name, arg = m.group(1), m.group(2)
        if name == "frame":
            return str(st.frames[arg])
        if name == "job_frame":
            return str(st.job_frames[int(arg) - 1])
        if name == "step":
            idx, path = arg.split(":", 1)
            cur = (calls[int(idx)].envelope or {})
            for part in path.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            return "" if cur is None else str(cur)
        return str(st.vars[name])

    if isinstance(value, str):
        if value == "{job_frames}":
            return [str(p) for p in st.job_frames]
        out = _PH.sub(one, value)
        if out.startswith("/") or out.startswith("~"):
            guard(out)
        return out
    if isinstance(value, list):
        flat = []
        for v in value:
            r = substitute(v, session, calls)
            flat.extend(r if isinstance(r, list) and v == "{job_frames}" else [r])
        return flat
    if isinstance(value, dict):
        return {k: substitute(v, session, calls) for k, v in value.items()}
    return value


def needs_stats(case: Case) -> bool:
    return any(c.metric in STAT_METRICS for c in case.expect.checks)


def isolate(session, frame: Path, role: str) -> bool:
    """Spec 2.3 case isolation: reset what the previous case touched; then the
    snapshot must equal this frame's session baseline, else the session is
    contaminated and the frame is re-staged from a fresh copy (relaunch).
    Returns True when it had to re-stage."""
    from gauntlet import metrics

    if session.touched:
        from scouter.core import vocab

        mods = {s.module for s in vocab.PARAMS} if "*" in session.touched else \
            session.touched - {"*"}
        for m in sorted(mods):
            session.call("darkroom_reset", {"module": m, "confirm": True}, 12.0, write=True)
        session.touched.clear()
    now = observe.snapshot(frame)
    base = session.baselines.setdefault(str(frame), now["params"])
    units = now.get("units", {})
    moved = [k for k in set(base) | set(now["params"])
             if not metrics.param_equal(units.get(k, "ratio"), now["params"].get(k), base.get(k))]
    if moved:
        session.refresh_frame(role)
        return True
    return False


def run_tool_case(session, case: Case, *, clock=time.monotonic) -> Observation:
    obs = Observation()
    t0 = clock()
    role = case.frame_role or "hero"
    frame = session.ensure_frame(role)
    if isolate(session, frame, role):
        frame = session.ensure_frame(role)
        obs.extra["contaminated_restaged"] = True
    kelvin = observe.kelvin_for(CAMERA.get(case.body or ""))
    obs.before = observe.snapshot(frame, kelvin)
    snaps = [obs.before["params"]]
    masks = None
    stats = needs_stats(case)
    previews = {}
    is_chart = case.frame_role == "chart"
    if stats:
        raster = observe.render_raster(frame, session.root / "render" / "before.ppm")
        if is_chart:
            obs.stats_before = chart.patch_stats(raster)
        else:
            obs.stats_before, masks = observe.stats(raster)
        obs.floors = dict(session.floors)
        previews["before"] = observe.render_preview(frame, session.previews_dir / case.id /
                                                    "before.png")
    for st in case.steps:
        args = substitute(st.args, session, obs.calls)
        if st.tool in VIRTUAL:
            rec = VIRTUAL[st.tool](session, case, st, obs)
        else:
            rec = session.call(st.tool, args, st.budget_s, write=st.write)
        obs.calls.append(rec)
        if rec.error == "timeout" and rec.health == "unresponsive":
            obs.blocked = "hang"
            obs.hang_evidence.append(f"{st.tool}: no answer in {st.budget_s + 30:.0f}s, "
                                     "ping unresponsive")
            break
        if rec.envelope and (rec.envelope.get("healed") or
                             (rec.envelope.get("data") or {}).get("healed")):
            obs.healed = True
        snaps.append(observe.snapshot(frame, kelvin)["params"] if st.write else snaps[-1])
    obs.after = observe.snapshot(frame, kelvin)
    obs.extra["snapshots"] = snaps
    if stats and not obs.blocked:
        raster = observe.render_raster(frame, session.root / "render" / "after.ppm")
        obs.stats_after = chart.patch_stats(raster) if is_chart else \
            observe.stats(raster, masks)[0]
        previews["after"] = observe.render_preview(frame, session.previews_dir / case.id /
                                                   "after.png")
        obs.extra["previews"] = {k: session.relative(v) for k, v in previews.items()}
    obs.wall_s = clock() - t0
    return obs
```

`tests/gauntlet/policy.py`:

```python
"""The evaluator policy for Scouter, read from check_map.json (spec 5.4)."""

from __future__ import annotations

import json

from gauntlet.evaluate import Policy
from tests.gauntlet.taxonomy import DATA


def load_policy() -> Policy:
    cm = json.loads((DATA / "check_map.json").read_text(encoding="utf-8"))
    return Policy(refusal_codes=frozenset(cm["refusal_codes"]),
                  transient_codes=frozenset(cm["transient_codes"]),
                  refusal_markers=tuple(cm["refusal_markers"]),
                  claim_markers=tuple(cm["claim_markers"]))


POLICY = load_policy()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_execute.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/observe.py tests/gauntlet/execute.py tests/gauntlet/policy.py tests/gauntlet/test_execute.py
git commit -m "gauntlet(scouter): snapshots, renders, case isolation and tool-mode execution (Task 14, spec 2.3/2.4)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 15: Chart in darktable: render, golden, calibration

PARALLEL-OK. Depends on: Tasks 3, 4, 14. Step 6 is **LIVE** (controller).

**Files:**
- Create: `tests/gauntlet/chart_render.py`, `tests/gauntlet/golden/.gitkeep`
- Test: `tests/gauntlet/test_chart_render.py`

**Interfaces:**
- Produces: `GOLDEN` (`tests/gauntlet/golden/chart-v1.stats.json`), `RENDER_PX=1536`, `CALIBRATE`, `PROBE`, `write_chart(run_root, fmt="pfm"|"tiff") -> Path`, `render_stats(src, out_ppm) -> patch stats`, `labs(stats)`, `compare_golden(stats, golden=GOLDEN) -> {golden, verdict, patch_de, worst}` (`no_golden` when absent), `write_golden_candidate(run_root, stats)`, `slope(before, after, key, amount)`.

- [ ] **Step 1: Write the failing test** (`tests/gauntlet/test_chart_render.py`)

```python
from tests.gauntlet import chart_render


def test_write_chart_pfm_and_tiff(tmp_path):
    pfm = chart_render.write_chart(tmp_path)
    assert pfm.read_bytes().startswith(b"PF\n1536 1152\n")
    tif = chart_render.write_chart(tmp_path, "tiff")
    assert tif.read_bytes()[:4] == b"II*\x00"


def test_golden_compare_and_candidate(tmp_path):
    from gauntlet import chart

    st = chart.patch_stats(chart.build())
    cand = chart_render.write_golden_candidate(tmp_path, st)
    res = chart_render.compare_golden(st, golden=cand)
    assert res["verdict"] == "pass" and res["patch_de"][0] < 1e-6
    assert chart_render.compare_golden(st, golden=tmp_path / "none.json")["verdict"] == "no_golden"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_chart_render.py -q`
Expected: `ImportError: cannot import name 'chart_render'`.

- [ ] **Step 3: Write the implementation**

```python
"""The synthetic chart in darktable (spec 4.3, P7): write chart-v1.pfm into
the run, render it untouched through darktable-cli, compare to the committed
golden per-patch Lab, and measure the per-run look calibration."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet import chart, metrics
from gauntlet.raster import encode_pfm, encode_tiff16
from gauntlet.store import atomic_write_text

GOLDEN = Path(__file__).resolve().parent / "golden" / "chart-v1.stats.json"
RENDER_PX = 1536
#: live params the look plan uses -> (metric region, stat) measured per unit.
CALIBRATE = {
    "colorbalancergb.shadows_luminance": ("patch:ramp_-4", "ev"),
    "colorbalancergb.midtones_luminance": ("patch:ramp_+0", "ev"),
    "colorbalancergb.highlights_luminance": ("patch:ramp_+2", "ev"),
    "sigmoid.contrast": ("chart_tone", "slope"),
    "colorbalancergb.global_chroma": ("chart_chromatic", "C"),
    "temperature.temperature": ("patch:ramp_+0", "b"),
    "exposure.black": ("patch:ramp_-5", "L"),
}
#: probe amounts per param (internal units) used to measure the slope.
PROBE = {"colorbalancergb.shadows_luminance": 0.1, "colorbalancergb.midtones_luminance": 0.1,
         "colorbalancergb.highlights_luminance": 0.1, "sigmoid.contrast": 0.3,
         "colorbalancergb.global_chroma": 0.2, "temperature.temperature": 600.0,
         "exposure.black": -0.01}


def write_chart(run_root: Path, fmt: str = "pfm") -> Path:
    """chart/chart-v1.pfm (or .tif, the fallback if darktable rejects PFM)."""
    img = chart.build()
    out = run_root / "chart" / f"{chart.VERSION}.{'pfm' if fmt == 'pfm' else 'tif'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_pfm(img) if fmt == "pfm" else encode_tiff16(img, clip=4.0))
    return out


def render_stats(src: Path, out_ppm: Path) -> dict:
    from tests.gauntlet import observe

    return chart.patch_stats(observe.render_raster(src, out_ppm, RENDER_PX))


def labs(stats: dict) -> dict[str, tuple[float, float, float]]:
    return {k.removeprefix("patch:"): (v["L"], v["a"], v["b"])
            for k, v in stats.items() if k.startswith("patch:")}


def compare_golden(stats: dict, golden: Path = GOLDEN) -> dict:
    if not golden.exists():
        return {"golden": None, "verdict": "no_golden", "patch_de": []}
    ref = json.loads(golden.read_text(encoding="utf-8"))["labs"]
    de = metrics.patch_de({k: tuple(v) for k, v in ref.items()}, labs(stats))
    return {"golden": str(golden), "verdict": metrics.de_verdict(list(de.values())),
            "patch_de": sorted(de.values(), reverse=True), "worst": max(de, key=de.get)}


def write_golden_candidate(run_root: Path, stats: dict) -> Path:
    """Written every run; promoted to tests/gauntlet/golden/ only by an
    explicit, Fable-reviewed --rebaseline commit (spec 4.3)."""
    out = run_root / "chart" / "chart-v1.stats.candidate.json"
    atomic_write_text(out, json.dumps({"version": chart.VERSION, "labs": labs(stats)},
                                      indent=2, sort_keys=True) + "\n")
    return out


def slope(before: dict, after: dict, key: str, amount: float) -> float | None:
    region, stat = CALIBRATE[key]
    b, a = before[region][stat], after[region][stat]
    d = metrics.delta(stat, b, a)
    return None if amount == 0 else d / amount
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_chart_render.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
mkdir -p tests/gauntlet/golden && touch tests/gauntlet/golden/.gitkeep
git add tests/gauntlet/chart_render.py tests/gauntlet/test_chart_render.py tests/gauntlet/golden/.gitkeep
git commit -m "gauntlet(scouter): chart render, golden compare and look calibration helpers (Task 15, spec 4.3/P7)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6 (LIVE, controller): verify darktable accepts the PFM**

Run:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tests.gauntlet.adapter import isolate_process
from tests.gauntlet import chart_render
root = Path.home() / "Pictures/ScouterScratch/gauntlet/chart-check"
isolate_process(root / "_process")
src = chart_render.write_chart(root)
st = chart_render.render_stats(src, root / "a.ppm")
print({k: round(v["L"], 1) for k, v in st.items() if k.startswith("patch:ramp")})
PY
```

Expected: 11 ramp L* values strictly increasing from about 10 to about 95. If darktable-cli rejects the PFM or the ramp is flat or clipped at both ends, rerun with `chart_render.write_chart(root, "tiff")` and, if the TIFF renders, change `write_chart`'s default to `"tiff"` in a follow-up commit ("gauntlet: chart falls back to 16-bit TIFF (Task 15, spec 4.3)"). The golden itself is committed in Task 35 after the first clean run.

### Task 16: Agent mode

PARALLEL-OK. Depends on: Task 14.

**Files:**
- Create: `tests/gauntlet/agent.py`
- Test: `tests/gauntlet/test_agent.py`

**Interfaces:**
- Consumes: `Park`, `observe`, a `ScouterSession` (`inner`).
- Produces: `LIMIT_RE`, `parse_stream(lines) -> {session_id, text, tool_calls, is_error, limit}`, `reset_time(text, now=None) -> float|None`, `mcp_config(staged)`, `claude_argv(prompt, mcp_json, resume=None)` (`--model sonnet --output-format stream-json --verbose --mcp-config ... --strict-mcp-config --allowedTools mcp__scouter__*`), `AgentSession(inner, *, run=subprocess.run)` implementing `Session`.

- [ ] **Step 1: Write the failing test**

```python
import json
from datetime import datetime

import pytest

from gauntlet.adapter import Park
from tests.gauntlet import agent


def _lines(*msgs):
    return [json.dumps(m) for m in msgs]


def test_parse_stream_collects_tools_results_and_text():
    lines = _lines(
        {"type": "system", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__scouter__darkroom_say",
             "input": {"phrase": "brighter"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "{\"ok\": true}"}]}]}},
        {"type": "result", "result": "Done, I've brightened it.", "is_error": False,
         "session_id": "s1"})
    p = agent.parse_stream(lines)
    assert p["session_id"] == "s1" and p["text"].startswith("Done")
    assert p["tool_calls"][0]["result"] == "{\"ok\": true}" and p["limit"] is None


def test_usage_limit_is_detected_with_reset_time():
    p = agent.parse_stream(_lines({"type": "result", "is_error": True,
                                   "result": "Claude usage limit reached. Resets at 4pm."}))
    assert p["limit"]
    now = datetime(2026, 9, 27, 13, 0)
    assert datetime.fromtimestamp(agent.reset_time(p["limit"], now)).hour == 16


def test_argv_registers_only_scouter_and_sonnet():
    argv = agent.claude_argv("hi", "/tmp/m.json", resume="s1")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in argv and argv[argv.index("--resume") + 1] == "s1"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scouter__*"


def test_limit_mid_followup_parks_the_whole_case(tmp_path, monkeypatch):
    """Review focus 5: a limit in a follow-up parks the case; on resume the
    whole case reruns from a fresh conversation (nothing is written)."""
    from gauntlet.case import Case, Expect

    answers = iter([
        _lines({"type": "result", "result": "Warmer now.", "session_id": "s1"}),
        _lines({"type": "result", "is_error": True, "result": "usage limit reached"}),
    ])

    class P:
        def __init__(self, out):
            self.stdout = "\n".join(out)

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                      "params": {}})
    s = agent.AgentSession(Inner(), run=lambda *a, **k: P(next(answers)))
    case = Case(id="sca-x", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": ["more"]})
    with pytest.raises(Park):
        s.run_case(case)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_agent.py -q`
Expected: `ImportError: cannot import name 'agent'`.

- [ ] **Step 3: Write the implementation**

```python
"""Agent mode (spec 2.3, row 436): real Claude Code, Sonnet, through
`claude -p --output-format stream-json`, one conversation per case, follow-ups
through `--resume <session_id>`. The usage meter is ignored: when a limit
stops the batch the case parks (gauntlet.adapter.Park) and resumes later."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation, Park
from gauntlet.case import Case
from tests.gauntlet import observe

LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|limit will reset|"
                      r"out of (extra )?usage|5-hour limit|weekly limit)", re.I)
RESET_RE = re.compile(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
TURN_TIMEOUT_S = 600


def parse_stream(lines: list[str]) -> dict:
    """{session_id, text, tool_calls: [{name, input, result}], is_error, limit}."""
    out: dict = {"session_id": None, "text": "", "tool_calls": [], "is_error": False,
                 "limit": None}
    pending: dict[str, dict] = {}
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        out["session_id"] = msg.get("session_id") or out["session_id"]
        if kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    call = {"name": block.get("name"), "input": block.get("input"), "result": None}
                    pending[block.get("id")] = call
                    out["tool_calls"].append(call)
        elif kind == "user":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    pending[block["tool_use_id"]]["result"] = content
        elif kind == "result":
            out["text"] = msg.get("result") or ""
            out["is_error"] = bool(msg.get("is_error"))
    if out["is_error"] and LIMIT_RE.search(out["text"] or ""):
        out["limit"] = out["text"]
    return out


def reset_time(text: str, now: datetime | None = None) -> float | None:
    m = RESET_RE.search(text or "")
    if not m:
        return None
    now = now or datetime.now()
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        hour = hour % 12 + (12 if m.group(3).lower() == "pm" else 0)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def mcp_config(staged) -> dict:
    """Registers ONLY scouter, with the session's isolated env."""
    import sys

    env = {k: staged.env[k] for k in ("PYTHONPATH", "SCOUTER_CONFIG", "SCOUTER_CACHE",
                                      "OBSIDIAN_VAULT_PATH", "SCOUTER_JOB") if k in staged.env}
    return {"mcpServers": {"scouter": {"type": "stdio", "command": sys.executable,
                                       "args": ["-m", "scouter.mcp.server"], "env": env}}}


def claude_argv(prompt: str, mcp_json: Path, resume: str | None = None) -> list[str]:
    argv = ["claude", "-p", prompt, "--model", "sonnet", "--output-format", "stream-json",
            "--verbose", "--mcp-config", str(mcp_json), "--strict-mcp-config",
            "--allowedTools", "mcp__scouter__*"]
    if resume:
        argv += ["--resume", resume]
    return argv


class AgentSession:
    """Implements gauntlet.adapter.Session for layer scenarios_agent. Wraps a
    ScouterSession (darktable + staged frames); the MCP server is Claude's."""

    def __init__(self, inner, *, run=subprocess.run, clock=time.monotonic) -> None:
        self.inner = inner
        self.run = run
        self.clock = clock

    def _turn(self, prompt: str, resume: str | None) -> dict:
        path = self.inner.root / "mcp.json"
        path.write_text(json.dumps(mcp_config(self.inner.staged)))
        p = self.run(claude_argv(prompt, path, resume), capture_output=True, text=True,
                     timeout=TURN_TIMEOUT_S, cwd=str(self.inner.root))
        parsed = parse_stream(p.stdout.splitlines())
        if parsed["limit"]:
            raise Park(f"agent usage limit: {parsed['limit'][:120]}",
                       until=reset_time(parsed["limit"]))
        return parsed

    def run_case(self, case: Case) -> Observation:
        t0 = self.clock()
        frame = self.inner.ensure_frame(case.frame_role or "hero")
        kelvin = observe.kelvin_for({"olympus": "em10iii"}.get(case.body or ""))
        obs = Observation(before=observe.snapshot(frame, kelvin))
        stats = any(c.metric in ("M2", "M3", "M4", "M5", "M6") for c in case.expect.checks)
        masks = None
        if stats:
            r = observe.render_raster(frame, self.inner.root / "render" / "before.ppm")
            obs.stats_before, masks = observe.stats(r)
            obs.floors = dict(self.inner.floors)
        prompt = f"{case.meta['sentence']}\n\nThe photo open in darktable is {frame}."
        parsed = self._turn(prompt, None)  # a Park here parks the whole case (fresh on resume)
        texts = [parsed["text"]]
        calls = list(parsed["tool_calls"])
        for f in case.meta.get("followups", []):
            more = self._turn(f, parsed["session_id"])
            texts.append(more["text"])
            calls.extend(more["tool_calls"])
        for c in calls:
            env = None
            if isinstance(c["result"], str):
                try:
                    env = json.loads(c["result"])
                except ValueError:
                    env = {"ok": False, "outcome": "error:unparsed", "raw": c["result"][:300]}
            name = (c["name"] or "").removeprefix("mcp__scouter__")
            obs.calls.append(CallRecord(name, c["input"] or {}, env,
                                        write=name.startswith(("darkroom_set", "darkroom_adjust",
                                                               "darkroom_say", "preset_",
                                                               "darkroom_undo"))))
        obs.agent_text = "\n\n".join(texts)
        obs.after = observe.snapshot(frame, kelvin)
        if stats:
            r = observe.render_raster(frame, self.inner.root / "render" / "after.ppm")
            obs.stats_after, _ = observe.stats(r, masks)
        self.inner.touched.add("*")
        obs.wall_s = self.clock() - t0
        return obs

    def abort(self) -> None:
        self.inner.abort()

    def close(self) -> None:
        self.inner.close()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_agent.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/agent.py tests/gauntlet/test_agent.py
git commit -m "gauntlet(scouter): agent mode through claude -p, park and resume on a usage limit (Task 16, row 436)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part 3: Layers 0-8 as cases
Every layer task adds one generator (`tests/gauntlet/layers/<layer>.py`) and, where the layer needs more than plain MCP calls, one executor (`exec_<layer>.py` / `faults.py`) that registers virtual tools in `execute.VIRTUAL` or provides a whole-case runner. The counts below were produced by the generators while writing this plan and match spec §3 exactly (total 5,167).
### Task 17: Layer 0: offline resolver sweep

PARALLEL-OK. Depends on: Tasks 9, 14.

**Files:**
- Create: `tests/gauntlet/layers/offline.py`, `tests/gauntlet/exec_offline.py`
- Test: `tests/gauntlet/test_layer_offline.py`, `tests/gauntlet/test_exec_offline.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `offline.LAYER="offline"`, `PREFIX="off"`, `EXPECTED=2314`, `generate(tax) -> list[Case]` (378 taxonomy phrases + 601 request sentences with `meta.allowed_keys`, and 89 synonyms x {bare, 11 intensity words, 3 explicit amounts} with `meta.grammar`); `exec_offline.judge(case) -> (ok, got)`, `OfflineSession` (a `Session` that runs `phrases.resolve` in-process and sets `obs.extra["offline_ok"]`).

The oracle for intensities is the spec's own table (`INTENSITY_WORDS`), never `phrases.INTENSITY` (the thing under test). Dry run while writing the plan: all 1,335 grammar cases verify on today's resolver.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_offline.py`:

```python
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import offline


def test_offline_count_ids_tier():
    cases = offline.generate(taxonomy.load())
    assert len(cases) == offline.EXPECTED == 2314
    assert len({c.id for c in cases}) == 2314
    assert {c.tier for c in cases} == {"P2"} and {c.mode for c in cases} == {"offline"}
```

`tests/gauntlet/test_exec_offline.py`:

```python
import collections

import pytest

from gauntlet.evaluate import evaluate
from tests.gauntlet import taxonomy
from tests.gauntlet.exec_offline import OfflineSession
from tests.gauntlet.layers import offline
from tests.gauntlet.policy import POLICY


@pytest.fixture(scope="module")
def cases():
    return offline.generate(taxonomy.load())


def test_grammar_cases_pass_on_todays_resolver(cases):
    gram = [c for c in cases if "-gram-" in c.id]
    s = OfflineSession()
    verdicts = collections.Counter(evaluate(c, s.run_case(c), POLICY).outcome for c in gram)
    assert verdicts == {"verified": 1335}, verdicts


def test_sweep_runs_every_phrase_without_crashing(cases):
    s = OfflineSession()
    out = collections.Counter(evaluate(c, s.run_case(c), POLICY).score for c in cases
                              if "-gram-" not in c.id)
    assert sum(out.values()) == 979
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_offline.py tests/gauntlet/test_exec_offline.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.offline'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/offline.py`:

```python
"""Layer 0: the offline resolver sweep (spec 3.0), P2, no darktable.

979 phrase/sentence cases (378 taxonomy phrases + 601 request sentences) and
89 synonyms x 15 forms = 1,335 grammar cases. Total 2,314."""

from __future__ import annotations

from scouter.core import phrases, vocab

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.checkmap import plan_request
from tests.gauntlet.taxonomy import Taxonomy

LAYER = "offline"
PREFIX = "off"
#: The spec's independent copy of the intensity table (phrases.INTENSITY is
#: the thing under test, so the oracle must not read it).
INTENSITY_WORDS = {"a little bit": 0.5, "a tiny bit": 0.5, "a touch": 0.5, "a little": 0.5,
                   "a bit": 0.5, "slightly": 0.5, "a lot": 2.0, "strongly": 2.0, "much": 2.0,
                   "lots": 2.0, "way": 2.0}
EXPLICIT = {"EV": ("0.5 ev", "1 ev", "2 ev"), "K": ("200k", "500k", "1000k"),
            "%": ("2%", "5%", "10%"), "°": ("10°", "20°", "45°"), "ratio": ("0.1", "0.2", "0.5")}
#: The capability ids that resolve to a phrase key in check_map (phrase -> key).
SERVED_MODULES = {"exposure", "sigmoid", "temperature", "colorbalancergb", "filmicrgb",
                  "colorzones"}


def _key_for_phrase(phrase: str) -> str | None:
    for p in phrases.PHRASES:
        if phrase in p.synonyms:
            return p.key
    return None


def _capability_keys(cmap: dict) -> dict[str, str]:
    """capability id -> phrase key (tool-step capabilities map to None)."""
    out = {}
    for cap, spec in cmap["capabilities"].items():
        if "phrase" in spec:
            out[cap] = _key_for_phrase(spec["phrase"])
    return out


def _resolve_case(cid: str, text: str, allowed: list[str], src: dict) -> Case:
    return Case(
        id=cid, layer=LAYER, tier="P2", mode="offline",
        steps=[Step("vocab_resolve", {"phrase": text}, 1.0)],
        expect=Expect("workflow", checks=[Check("extra", target={
            "path": "offline_ok", "op": "truthy"})]),
        source=src, meta={"text": text, "allowed_keys": sorted(allowed)},
    )


def _module_keys(module_id: str) -> list[str]:
    keys = []
    for p in phrases.PHRASES:
        mods = {mv.module for mv in p.moves}
        if module_id in mods or ("@tone" in mods and module_id in ("sigmoid", "filmicrgb")):
            keys.append(p.key)
    return keys


def generate(tax: Taxonomy) -> list[Case]:
    cases: list[Case] = []
    owner = tax.phrase_module()
    for i, text in enumerate(tax.unique_phrases()):
        mod = owner[text]
        allowed = _module_keys(mod) if mod in SERVED_MODULES else []
        cases.append(_resolve_case(case_id(PREFIX, "tax", f"{i:03d}"), text, allowed,
                                   {"file": "taxonomy-modules.json", "module": mod}))
    cap_keys = _capability_keys(tax.check_map)
    for r in tax.requests:
        plan = plan_request(r, tax.check_map)
        allowed = sorted({cap_keys[c] for c in plan.capabilities if cap_keys.get(c)})
        cases.append(_resolve_case(case_id(PREFIX, "req", r["id"]), r["sentence"], allowed,
                                   {"file": "requests.json", "id": r["id"]}))
    for p in phrases.PHRASES:
        row = vocab.spec("sigmoid", "contrast") if p.moves[0].module == "@tone" else \
            vocab.spec(p.moves[0].module, p.moves[0].param)
        one_move = len(p.moves) == 1 and p.moves[0].kind == "adjust"
        for syn in p.synonyms:
            forms = [("bare", syn, 1.0)]
            forms += [(w.replace(" ", "_"), f"{syn} {w}", f) for w, f in INTENSITY_WORDS.items()]
            forms += [(f"by_{a}", f"{syn} by {a}", None) for a in EXPLICIT[row.unit if row.unit in EXPLICIT else "ratio"]]  # noqa: E501
            for label, text, factor in forms:
                cid = case_id(PREFIX, "gram", p.key, syn, label)
                if factor is None:
                    expect = {"explicit": True} if one_move else {"refused": "usage"}
                else:
                    expect = {"intensity": factor,
                              "notches": [(mv.notches or 0) * factor if mv.kind == "adjust"
                                          else None for mv in p.moves]}
                cases.append(Case(
                    id=cid, layer=LAYER, tier="P2", mode="offline",
                    steps=[Step("vocab_resolve", {"phrase": text}, 1.0)],
                    expect=Expect("workflow", checks=[Check("extra", target={
                        "path": "offline_ok", "op": "truthy"})]),
                    source={"phrase_key": p.key, "synonym": syn}, meta={"text": text,
                                                                        "grammar": expect,
                                                                        "key": p.key},
                ))
    return cases


#: 378 taxonomy phrases + 601 sentences + every synonym x 15 forms (89 today -> 2,314).
EXPECTED = 378 + 601 + sum(len(p.synonyms) for p in phrases.PHRASES) * 15
```

`tests/gauntlet/exec_offline.py`:

```python
"""Layer 0 executor: phrases.resolve in-process, no darktable (spec 3.0)."""

from __future__ import annotations

import time

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case


def judge(case: Case) -> tuple[bool, dict]:
    from scouter.core import phrases
    from scouter.errors import UsageError

    text = case.meta["text"]
    try:
        res = phrases.resolve(text)
    except UsageError as e:
        refused = {"refused": "usage", "suggestions": (e.detail or {}).get("suggestions")}
        if "grammar" in case.meta:
            return case.meta["grammar"].get("refused") == "usage", refused
        # an honest refusal must carry its nearest phrases (possibly an empty list)
        return "suggestions" in (e.detail or {}), refused
    got = {"key": res.key, "intensity": res.intensity, "explicit": res.explicit,
           "notches": [mv.notches if mv.kind == "adjust" else None for mv in res.moves]}
    if "grammar" in case.meta:
        want = case.meta["grammar"]
        if "refused" in want:
            return False, got
        if want.get("explicit"):
            return res.explicit and res.key == case.meta["key"], got
        ok = res.key == case.meta["key"] and abs(res.intensity - want["intensity"]) < 1e-9 and \
            all((a is None and b is None) or (a is not None and b is not None and abs(a - b) < 1e-9)
                for a, b in zip(got["notches"], want["notches"]))
        return ok, got
    return res.key in case.meta["allowed_keys"], got


class OfflineSession:
    def run_case(self, case: Case) -> Observation:
        t0 = time.monotonic()
        ok, got = judge(case)
        env = {"ok": "refused" not in got, "outcome": "ok", "data": got}
        return Observation(calls=[CallRecord("vocab_resolve", {"phrase": case.meta["text"]},
                                             env, wall_s=time.monotonic() - t0)],
                           extra={"offline_ok": ok, "resolved": got},
                           wall_s=time.monotonic() - t0)

    def abort(self) -> None:
        pass

    def close(self) -> None:
        pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_offline.py tests/gauntlet/test_exec_offline.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/offline.py tests/gauntlet/exec_offline.py tests/gauntlet/test_layer_offline.py tests/gauntlet/test_exec_offline.py
git commit -m "gauntlet(scouter): layer 0 offline resolver sweep, 2,314 cases (Task 17, spec 3.0)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 18: Layer 1: coverage

PARALLEL-OK. Depends on: Task 9.

**Files:**
- Create: `tests/gauntlet/layers/coverage.py`
- Test: `tests/gauntlet/test_layer_coverage.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`.
- Produces: `coverage.LAYER="coverage"`, `PREFIX="cov"`, `generate() -> list[Case]` (648 per main body + 20 NEX-5N), `expected_count()` (the spec 3.1 formula from today's vocab: 1,316 at 22 live params, 27 keys, 89 synonyms), `live_params()`, `PHRASE_STAT`, `OPPOSED` (12), `COMMUTATIVE` (6), `TINT_KEYS`.

White-balance Kelvin cases first set tint 1.0 (W4: Kelvin is readable only at the WB table's calibrated tint), and their M1 checks index snapshots past that step (`from`). Boundary sets (`lo-step`, `hi+step`) accept a typed refusal (`meta.accept_refusal`) or a clamp the envelope reports; a silent clamp is a silent failure (`clamp_reported()` with `silent_if_fail`).

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_coverage.py`:

```python
from tests.gauntlet.layers import coverage


def test_coverage_counts_and_p0_core():
    cases = coverage.generate()
    assert len(cases) == coverage.expected_count() == 1316
    assert len({c.id for c in cases}) == 1316
    assert sum(c.tier == "P0" for c in cases) == 318
    assert sum(c.body == "sony_nex5n" for c in cases) == 20


def test_boundary_accepts_refusal_but_not_a_silent_clamp():
    c = next(x for x in coverage.generate()
             if x.id == "cov-exposure.exposure-olympus-set_lo_minus")
    assert c.meta["accept_refusal"] and c.expect.checks[1].tags["silent_if_fail"]


def test_kelvin_cases_reset_tint_first_and_offset_their_checks():
    c = next(x for x in coverage.generate()
             if x.id == "cov-temperature.temperature-olympus-adjust_up1")
    assert c.steps[0].args == {"module": "temperature", "param": "tint", "value": 1.0}
    assert c.expect.checks[0].target["from"] == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_coverage.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.coverage'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/coverage.py`:

```python
"""Layer 1: coverage (spec 3.1). 648 cases per main body + 20 NEX-5N WB cases.

Every case runs on the body's `hero` frame. P0 core per body: every live
param x {set lo, set hi, set mid, adjust +1, adjust -1, undo after set} plus
the 27 phrase keys bare (159 per body)."""

from __future__ import annotations

from scouter.core import phrases, vocab

from gauntlet.case import Case, Check, Expect, case_id
from tests.gauntlet.layers.common import (
    BODIES,
    BUDGET_MOVE,
    BUDGET_SAY,
    clamp_reported,
    frame,
    key,
    m1_delta,
    m1_equals,
    m1_restored,
    sidecar,
    step,
)

LAYER = "coverage"
PREFIX = "cov"
HERO = frame("hero")
OPPOSED = (("brighter", "darker"), ("more_contrast", "less_contrast"), ("warmer", "cooler"),
           ("more_magenta", "more_green"), ("lift_shadows", "deepen_shadows"),
           ("brighter_midtones", "darker_midtones"), ("brighter_highlights", "tame_highlights"),
           ("deeper_blacks", "faded_blacks"), ("more_saturation", "less_saturation"),
           ("more_vibrance", "less_vibrance"), ("more_chroma", "less_chroma"),
           ("stronger_grade", "weaker_grade"))
COMMUTATIVE = (("brighter", "warmer"), ("more_contrast", "more_saturation"),
               ("lift_shadows", "cooler"), ("deeper_blacks", "more_vibrance"),
               ("tame_highlights", "more_chroma"), ("brighter_midtones", "warmer"))
#: phrase key -> (metric, stat, region, sign) for the stat check on phrase cases.
PHRASE_STAT = {
    "brighter": ("M2", "L", "frame", 1), "darker": ("M2", "L", "frame", -1),
    "more_contrast": ("M6", "spread", "frame", 1), "less_contrast": ("M6", "spread", "frame", -1),
    "warmer": ("M4", "rb", "frame", 1), "cooler": ("M4", "rb", "frame", -1),
    "lift_shadows": ("M2", "L", "shadows", 1), "deepen_shadows": ("M2", "L", "shadows", -1),
    "brighter_midtones": ("M2", "L", "frame", 1), "darker_midtones": ("M2", "L", "frame", -1),
    "brighter_highlights": ("M2", "L", "highlights", 1),
    "tame_highlights": ("M2", "L", "highlights", -1),
    "deeper_blacks": ("M3", "p01L", "frame", -1), "faded_blacks": ("M3", "p01L", "frame", 1),
    "more_saturation": ("M5", "C", "frame", 1), "less_saturation": ("M5", "C", "frame", -1),
    "more_vibrance": ("M5", "C", "frame", 1), "less_vibrance": ("M5", "C", "frame", -1),
    "more_chroma": ("M5", "C", "frame", 1), "less_chroma": ("M5", "C", "frame", -1),
    "cool_shadows": ("M4", "b", "shadows", -1), "warm_highlights": ("M4", "b", "highlights", 1),
    "teal_orange": ("M4", "b", "shadows", -1),
}
TINT_KEYS = {"more_magenta", "more_green"}
EXPLICIT_BY_UNIT = {"EV": ("0.5 ev", 0.5), "K": ("500k", 500.0), "%": ("5%", 0.05),
                    "ratio": ("0.2", 0.2), "°": ("20°", 20.0)}


def _signed(label: str, n: int) -> str:
    return f"{label}_{'up' if n > 0 else 'dn'}{abs(n)}"


def live_params() -> list[vocab.ParamSpec]:
    return [s for s in vocab.PARAMS if vocab.status(s) == "live"]


def _row(mv: phrases.Move) -> vocab.ParamSpec:
    return vocab.spec("sigmoid", "contrast") if mv.module == phrases.TONE else \
        vocab.spec(mv.module, mv.param)


def _mid(s: vocab.ParamSpec) -> float:
    return round((s.lo + s.hi) / 2, 6)


def _neutral(s: vocab.ParamSpec) -> float:
    if s.neutral is not None:
        return s.neutral
    if s.default is not None:
        return s.default
    return round(s.lo + (s.hi - s.lo) / 4, 6)


def _case(cid: str, body: str, tier: str, steps: list, expect: Expect, *,
          meta: dict | None = None) -> Case:
    return Case(id=cid, layer=LAYER, tier=tier, mode="tool", steps=steps, expect=expect,
                body=body, frame_role="hero", source={"file": "vocab.py"}, meta=meta or {})


def _set(s: vocab.ParamSpec, value: float):
    return step("darkroom_set", BUDGET_MOVE, module=s.module, param=s.param, value=value)


def _adjust(s: vocab.ParamSpec, delta: float):
    return step("darkroom_adjust", BUDGET_MOVE, module=s.module, param=s.param, delta=delta)


def _side(s: vocab.ParamSpec) -> dict:
    return sidecar(s.module, s.param, s.unit)


def _tint_reset() -> list:
    """W4: Kelvin is readable from the XMP only at the WB table's calibrated
    tint (1.0), so every white-balance Kelvin case first sets tint 1.0."""
    t = vocab.spec("temperature", "tint")
    return [step("darkroom_set", BUDGET_MOVE, module=t.module, param=t.param, value=1.0)]


def param_cases(s: vocab.ParamSpec, body: str) -> list[Case]:
    k = key(s.module, s.param)
    cid = lambda v: case_id(PREFIX, k, body, v)  # noqa: E731
    out: list[Case] = []
    pre = _tint_reset() if (s.module, s.param) == ("temperature", "temperature") else []
    o = len(pre)
    values = [("set_lo", s.lo, "P0"), ("set_hi", s.hi, "P0"), ("set_mid", _mid(s), "P0"),
              ("set_neutral", _neutral(s), "P1")]
    for label, v, tier in values:
        out.append(_case(cid(label), body, tier, [*pre, _set(s, v)],
                         Expect("edit", [m1_equals(s.module, s.param, v, s.unit)], _side(s))))
    for label, v in (("set_lo_minus", s.lo - s.step), ("set_hi_plus", s.hi + s.step)):
        if s.wraps:
            want = v % 360.0
            out.append(_case(cid(label), body, "P1", [*pre, _set(s, v)],
                             Expect("edit", [m1_equals(s.module, s.param, want, s.unit)],
                                    _side(s))))
        else:
            want = s.lo if v < s.lo else s.hi
            out.append(_case(cid(label), body, "P1", [*pre, _set(s, v)],
                             Expect("edit", [m1_equals(s.module, s.param, want, s.unit),
                                             clamp_reported()], _side(s)),
                             meta={"accept_refusal": True}))
    for n in (1, -1, 3, -3):
        chk = m1_delta(s.module, s.param, n * s.step, s.unit, s.step)
        chk.target["from"] = 1 + o
        out.append(_case(cid(_signed("adjust", n)), body, "P0" if abs(n) == 1 else "P1",
                         [*pre, _set(s, _mid(s)), _adjust(s, n * s.step)],
                         Expect("edit", [chk], _side(s))))
    if s.wraps:
        over = _case(cid("adjust_overflow"), body, "P1",
                     [*pre, _set(s, s.hi - s.step), _adjust(s, 3 * s.step)],
                     Expect("edit", [m1_equals(s.module, s.param, (s.hi + 2 * s.step) % 360,
                                               s.unit)], _side(s)))
    else:
        over = _case(cid("adjust_overflow"), body, "P1",
                     [*pre, _set(s, s.hi - s.step), _adjust(s, 3 * s.step)],
                     Expect("edit", [m1_equals(s.module, s.param, s.hi, s.unit),
                                     clamp_reported()], _side(s)))
    out.append(over)
    for n in (1, -1):
        chg = Check("M1", kind="changed", sign=n, target={"key": k, "unit": s.unit, "from": 1 + o})
        out.append(_case(cid(_signed("nudge", n)), body, "P1",
                         [*pre, _set(s, _mid(s)),
                          step("darkroom_nudge", BUDGET_MOVE, module=s.module, param=s.param,
                               steps=n)],
                         Expect("edit", [chg], _side(s))))
    units = {k: s.unit}
    out.append(_case(cid("undo_after_set"), body, "P0",
                     [*pre, _set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)],
                     Expect("workflow", [m1_restored([k], units, o)])))
    out.append(_case(cid("undo_after_adjust"), body, "P1",
                     [*pre, _adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)],
                     Expect("workflow", [m1_restored([k], units, o)])))
    out.append(_case(cid("repeat_set"), body, "P1", [*pre, _set(s, _mid(s)), _set(s, _mid(s))],
                     Expect("property", [Check("M1", kind="step_equal",
                                               target={"key": k, "a": 1 + o, "b": 2 + o,
                                                       "unit": s.unit})], property="P1")))
    out.append(_case(cid("repeat_adjust"), body, "P1",
                     [*pre, _set(s, _mid(s)), _adjust(s, s.step), _adjust(s, s.step)],
                     Expect("property", [Check("M1", kind="step_ratio",
                                               target={"key": k, "base": [1 + o, 2 + o],
                                                       "double": [1 + o, 3 + o], "ratio": 2.0,
                                                       "tol": 0.1})], property="P1")))
    return out


def hue_wrap_cases(body: str) -> list[Case]:
    out = []
    for s in [p for p in live_params() if p.wraps]:
        for label, start, delta, want in (("wrap_up", 350.0, 20.0, 10.0),
                                          ("wrap_down", 10.0, -20.0, 350.0)):
            out.append(_case(case_id(PREFIX, key(s.module, s.param), body, label), body, "P1",
                             [_set(s, start), _adjust(s, delta)],
                             Expect("edit", [m1_equals(s.module, s.param, want, "°")],
                                    _side(s))))
    return out


def module_cases(body: str) -> list[Case]:
    out = []
    mods = sorted({p.module for p in live_params()})
    for m in mods:
        first = next(p for p in live_params() if p.module == m)
        out.append(_case(case_id(PREFIX, m, body, "reset"), body, "P1",
                         [_set(first, _mid(first)),
                          step("darkroom_reset", BUDGET_MOVE, module=m, confirm=True)],
                         Expect("workflow", [Check("envelope", step=1,
                                                   target={"path": "ok", "op": "eq",
                                                           "value": True})],
                                {"changed": True})))
        en = Check("M1", kind="equals", target={"key": f"{m}.__enabled", "value": 1,
                                                "unit": "bool"})
        out.append(_case(case_id(PREFIX, m, body, "enable"), body, "P1",
                         [step("darkroom_enable", BUDGET_MOVE, module=m)],
                         Expect("workflow", [en])))
        out.append(_case(case_id(PREFIX, m, body, "enable_twice"), body, "P1",
                         [step("darkroom_enable", BUDGET_MOVE, module=m),
                          step("darkroom_enable", BUDGET_MOVE, module=m)],
                         Expect("property", [Check("M1", kind="step_equal",
                                                   target={"key": f"{m}.__enabled", "a": 1,
                                                           "b": 2, "unit": "bool"})],
                                property="P1")))
    return out


def tint_cases(body: str) -> list[Case]:
    t = vocab.spec("temperature", "tint")
    out = []
    for v in (0.8, 0.9, 1.1, 1.2):
        out.append(_case(case_id(PREFIX, "temperature.tint", body, f"set_{v}"), body, "P1",
                         [_set(t, v)], Expect("edit", [], {"changed": True})))
    te = Expect("typed_error", codes=["value_unknown"])
    out.append(_case(case_id(PREFIX, "temperature.tint", body, "adjust"), body, "P1",
                     [_adjust(t, 0.05)], te))
    out.append(_case(case_id(PREFIX, "temperature.tint", body, "say_more_magenta"), body, "P1",
                     [step("darkroom_say", BUDGET_SAY, phrase="more magenta")],
                     Expect("typed_error", codes=["value_unknown"])))
    return out


def unreachable_cases(body: str) -> list[Case]:
    c = vocab.spec("colorzones", "curves")
    codes = ["unreachable", "not_pinned", "usage"]
    return [
        _case(case_id(PREFIX, "colorzones.curves", body, "set"), body, "P1", [_set(c, 0.5)],
              Expect("typed_error", codes=codes)),
        _case(case_id(PREFIX, "colorzones.curves", body, "adjust"), body, "P1",
              [_adjust(c, 0.1)], Expect("typed_error", codes=codes)),
    ]


def _phrase_expect(p: phrases.Phrase, sign: float = 1.0) -> Expect:
    if p.key in TINT_KEYS:
        return Expect("typed_error", codes=["value_unknown"])
    checks: list[Check] = []
    for mv in p.moves:
        if mv.module == phrases.TONE:
            continue
        row = _row(mv)
        if mv.kind == "set":
            checks.append(m1_equals(row.module, row.param, mv.value, row.unit))
        else:
            checks.append(Check("M1", kind="changed", sign=int(sign * (1 if mv.notches > 0 else -1)),  # noqa: E501
                                target={"key": key(row.module, row.param), "unit": row.unit}))
    metric, stat, region, sgn = PHRASE_STAT.get(p.key, (None, None, None, 0))
    if metric:
        checks.append(Check(metric, stat=stat, region=region, sign=sgn))
    return Expect("edit", checks, {"changed": True})


def phrase_cases(body: str) -> list[Case]:
    out = []
    for p in phrases.PHRASES:
        syn = p.synonyms[0]
        forms = [("bare", syn, "P0"), ("touch", f"{syn} a touch", "P1"),
                 ("lot", f"{syn} a lot", "P1")]
        for label, text, tier in forms:
            out.append(_case(case_id(PREFIX, "phrase", p.key, body, label), body, tier,
                             [step("darkroom_say", BUDGET_SAY, phrase=text)], _phrase_expect(p)))
        row = _row(p.moves[0])
        by, amount = EXPLICIT_BY_UNIT.get(row.unit, ("0.2", 0.2))
        text = f"{syn} by {by}"
        one = len(p.moves) == 1 and p.moves[0].kind == "adjust"
        if p.key in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        elif not one:
            exp = Expect("typed_error", codes=["usage"])
        elif p.moves[0].module == phrases.TONE:
            exp = _phrase_expect(p)
        else:
            sgn = 1 if p.moves[0].notches > 0 else -1
            exp = Expect("edit", [m1_delta(row.module, row.param, sgn * amount, row.unit,
                                           row.step)], sidecar(row.module, row.param, row.unit))
        out.append(_case(case_id(PREFIX, "phrase", p.key, body, "explicit"), body, "P1",
                         [step("darkroom_say", BUDGET_SAY, phrase=text)], exp))
    for p in phrases.PHRASES:
        for syn in p.synonyms:
            out.append(_case(case_id(PREFIX, "synonym", p.key, syn, body), body, "P1",
                             [step("darkroom_say", BUDGET_SAY, phrase=syn)], _phrase_expect(p)))
    return out


def _primary(p: phrases.Phrase) -> tuple[str, str]:
    mv = next((m for m in p.moves if m.kind == "adjust"), p.moves[0])
    row = _row(mv)
    return key(row.module, row.param), row.unit


def property_cases(body: str) -> list[Case]:
    out = []
    by_key = {p.key: p for p in phrases.PHRASES}
    undo = lambda: step("darkroom_undo_last", BUDGET_MOVE)  # noqa: E731
    say = lambda t: step("darkroom_say", BUDGET_SAY, phrase=t)  # noqa: E731
    for p in phrases.PHRASES:
        syn = p.synonyms[0]
        k, unit = _primary(p)
        if p.key in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        else:
            exp = Expect("property", [Check("M1", kind="monotonic", target={
                "key": k, "pairs": [[0, 1], [2, 3], [4, 5]]})], property="P4")
        out.append(_case(case_id(PREFIX, "p4", p.key, body), body, "P1",
                         [say(f"{syn} a touch"), undo(), say(syn), undo(), say(f"{syn} a lot")],
                         exp))
    for a, b in OPPOSED:
        pa, pb = by_key[a], by_key[b]
        k, unit = _primary(pa)
        if a in TINT_KEYS:
            exp = Expect("typed_error", codes=["value_unknown"])
        else:
            exp = Expect("property", [m1_restored([k], {k: unit}),
                                      Check("M2", stat="L", region="frame", sign=0,
                                            kind="unchanged")], property="P5")
        out.append(_case(case_id(PREFIX, "p5", a, b, body), body, "P1",
                         [say(pa.synonyms[0]), say(pb.synonyms[0])], exp))
    for a, b in COMMUTATIVE:
        pa, pb = by_key[a], by_key[b]
        ka, _ = _primary(pa)
        kb, _ = _primary(pb)
        out.append(_case(case_id(PREFIX, "p2", a, b, body), body, "P1",
                         [say(pa.synonyms[0]), say(pb.synonyms[0]), undo(), undo(),
                          say(pb.synonyms[0]), say(pa.synonyms[0])],
                         Expect("property", [Check("M1", kind="step_equal", target={
                             "keys": [ka, kb], "a": 2, "b": 6})], property="P2")))
    return out


def nex5n_cases() -> list[Case]:
    body = "sony_nex5n"
    s = vocab.spec("temperature", "temperature")
    pre = _tint_reset()
    out = []
    for label, v in (("set_lo", s.lo), ("set_hi", s.hi), ("set_mid", _mid(s)),
                     ("set_3200", 3200.0), ("set_5500", 5500.0), ("set_6500", 6500.0)):
        out.append(_case(case_id(PREFIX, "wb", body, label), body, "P1", [*pre, _set(s, v)],
                         Expect("edit", [m1_equals(s.module, s.param, v, s.unit)], _side(s))))
    for n in (1, -1, 3, -3):
        chk = m1_delta(s.module, s.param, n * s.step, s.unit, s.step)
        chk.target["from"] = 2
        out.append(_case(case_id(PREFIX, "wb", body, _signed("adjust", n)), body, "P1",
                         [*pre, _set(s, _mid(s)), _adjust(s, n * s.step)],
                         Expect("edit", [chk], _side(s))))
    k = key(s.module, s.param)
    out.append(_case(case_id(PREFIX, "wb", body, "undo_after_set"), body, "P1",
                     [*pre, _set(s, _mid(s)), step("darkroom_undo_last", BUDGET_MOVE)],
                     Expect("workflow", [m1_restored([k], {k: "K"}, 1)])))
    out.append(_case(case_id(PREFIX, "wb", body, "undo_after_adjust"), body, "P1",
                     [*pre, _adjust(s, s.step), step("darkroom_undo_last", BUDGET_MOVE)],
                     Expect("workflow", [m1_restored([k], {k: "K"}, 1)])))
    by_key = {p.key: p for p in phrases.PHRASES}
    for pk in ("warmer", "cooler"):
        p = by_key[pk]
        for label, text in (("bare", p.synonyms[0]), ("touch", f"{p.synonyms[0]} a touch"),
                            ("lot", f"{p.synonyms[0]} a lot"),
                            ("explicit", f"{p.synonyms[0]} by 500k")):
            out.append(_case(case_id(PREFIX, "wb", body, pk, label), body, "P1",
                             [step("darkroom_say", BUDGET_SAY, phrase=text)], _phrase_expect(p)))
    return out


def generate() -> list[Case]:
    out: list[Case] = []
    for body in BODIES:
        for s in live_params():
            out.extend(param_cases(s, body))
        out.extend(hue_wrap_cases(body))
        out.extend(module_cases(body))
        out.extend(tint_cases(body))
        out.extend(unreachable_cases(body))
        out.extend(phrase_cases(body))
        out.extend(property_cases(body))
    out.extend(nex5n_cases())
    return out


def expected_count() -> int:
    live = len(live_params())
    keys = len(phrases.PHRASES)
    syns = sum(len(p.synonyms) for p in phrases.PHRASES)
    hues = sum(p.wraps for p in live_params())
    mods = len({p.module for p in live_params()})
    per_body = (live * (6 + 4 + 1 + 2 + 2 + 2) + hues * 2 + mods * 3 + 6 + 2 + keys * 4 + syns
                + keys + len(OPPOSED) + len(COMMUTATIVE))
    return 2 * per_body + 20
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_coverage.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/coverage.py tests/gauntlet/test_layer_coverage.py
git commit -m "gauntlet(scouter): layer 1 coverage, 1,316 cases (Task 18, spec 3.1)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 19: Layer 2: scenarios (tool mode and agent mode)

PARALLEL-OK. Depends on: Task 9.

**Files:**
- Create: `tests/gauntlet/layers/scenarios.py`
- Test: `tests/gauntlet/test_layer_scenarios.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`.
- Produces: `scenarios.LAYER="scenarios"`, `LAYER_AGENT="scenarios_agent"`, `PREFIX="scn"`, `PREFIX_AGENT="sca"`, `generate(tax) -> (tool: 701 cases, agent: 160 cases)`, `body_for(rid)` (odd id -> olympus), `cap_step`, `nearest_phrase(req, tax) -> (phrase, roadmap_key)`, `tool_case`, `agent_case`. Every out/impossible/partial case carries `meta.roadmap_key` (the taxonomy module id) for the S7 roadmap.

Follow-ups become extra steps in the same case: "back it off"/"too crunchy"/"too smooth" -> `darkroom_undo_last`; anything else repeats the capability with "a bit". Workflow requests map by `check_map.workflow_rules` to undo / repeat / preset save / export / cull; watermark, aspect, gallery, comparison, crop variants and batch-apply are out of vocabulary in a single-frame session (bulk covers batch apply).

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_scenarios.py`:

```python
import collections

from tests.gauntlet import taxonomy
from tests.gauntlet.layers import scenarios


def test_scenario_counts_and_tiers():
    tool, agent = scenarios.generate(taxonomy.load())
    assert (len(tool), len(agent)) == (701, 160)
    assert len({c.id for c in tool + agent}) == 861
    assert sum(c.tier == "P0" for c in tool) == 200
    assert collections.Counter(c.tier for c in agent) == {"P0": 100, "P1": 60}


def test_r0001_plan():
    tool, _ = scenarios.generate(taxonomy.load())
    r1 = next(c for c in tool if c.id == "scn-r0001-olympus-tool")
    assert r1.steps[0].args["phrase"] == "brighter by 1 ev" and r1.frame_role == "dark"
    assert r1.steps[1].args["phrase"] == "brighter a bit"  # the follow-up
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_scenarios.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.scenarios'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/scenarios.py`:

```python
"""Layer 2: scenarios (spec 3.2). Tool mode 701 (601 + the 100 P0 on the
other body), agent mode 160 (100 P0 + 60 impossible, olympus)."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.checkmap import RequestPlan, plan_request
from tests.gauntlet.layers.common import BUDGET_SAY, frame, m1_restored, stat_check, step
from tests.gauntlet.p0 import select_p0
from tests.gauntlet.taxonomy import Taxonomy

LAYER, LAYER_AGENT = "scenarios", "scenarios_agent"
PREFIX, PREFIX_AGENT = "scn", "sca"
EXPLICIT_OK = {"brighter", "darker"}


def body_for(rid: str) -> str:
    return "olympus" if int(rid[1:]) % 2 == 1 else "sony_a7"


def other(body: str) -> str:
    return "sony_a7" if body == "olympus" else "olympus"


def _magnitude(req: dict, cmap: dict, cap: str) -> str:
    word = cmap["magnitude_words"].get(req["magnitude"], "")
    if word.startswith("by ") and cap not in EXPLICIT_OK:
        return ""
    return word


def cap_step(cap: str, req: dict, cmap: dict, *, bit: bool = False) -> Step:
    spec = cmap["capabilities"][cap]
    if "tool" in spec:
        return step(spec["tool"], 3.5, **spec["args"])
    word = "a bit" if bit else _magnitude(req, cmap, cap)
    return step("darkroom_say", BUDGET_SAY,
                phrase=f"{spec['phrase']} {word}".strip())


def nearest_phrase(req: dict, tax: Taxonomy) -> tuple[str, str]:
    """(phrase, roadmap key) for an unserved request: the first taxonomy phrase
    of its first module that the taxonomy knows, else the sentence itself."""
    for name in req["modules"]:
        m = tax.module_by_name(name)
        if m and m["phrases"]:
            return m["phrases"][0], m["id"]
    return req["sentence"], f"unknown:{req['modules'][0] if req['modules'] else req['intent']}"


def _unserved_modules(req: dict, tax: Taxonomy) -> list[dict]:
    served = {"exposure", "sigmoid", "temperature", "colorbalancergb", "filmicrgb", "colorzones"}
    out = []
    for name in req["modules"]:
        m = tax.module_by_name(name)
        if m and m["id"] not in served and m["phrases"]:
            out.append(m)
    return out


def _followup_action(sentence: str, cmap: dict) -> str:
    low = sentence.lower()
    for rule in cmap["followup_rules"]:
        if rule["match"] in low:
            return rule["action"]
    return "repeat_bit"


def _workflow(plan: RequestPlan, req: dict) -> tuple[list[Step], Expect]:
    v = plan.workflow_verb
    hero = frame("hero")
    if v == "undo":
        steps = [step("darkroom_say", BUDGET_SAY, phrase="brighter")]
        if plan.step_back >= 2:
            steps.append(step("darkroom_say", BUDGET_SAY, phrase="warmer"))
        steps += [step("darkroom_undo_last", 3.5) for _ in range(max(1, plan.step_back))]
        return steps, Expect("workflow", [m1_restored([], {})])
    if v == "repeat_more":
        return ([step("darkroom_say", BUDGET_SAY, phrase="brighter"),
                 step("darkroom_say", BUDGET_SAY, phrase="brighter a bit")],
                Expect("workflow", [Check("M1", kind="changed", sign=1, target={
                    "key": "exposure.exposure", "unit": "EV", "from": 1, "to": 2})]))
    if v == "preset_save":
        name = f"htc-{req['id']}"
        return ([step("darkroom_say", BUDGET_SAY, phrase="warmer"),
                 step("preset_save", 12.0, name=name, confirm=True)],
                Expect("workflow", [Check("envelope", step=1, target={
                    "path": "ok", "op": "eq", "value": True})]))
    if v == "export":
        return ([step("export_run", 60.0, images=[hero], preset="{export_preset}",
                      out_dir="{out_dir}", confirm=True)],
                Expect("workflow", [Check("envelope", step=0, target={
                    "path": "ok", "op": "eq", "value": True}),
                    Check("extra", target={"path": "export_files", "op": "gte", "value": 1})]))
    if v == "cull":
        return ([step("cull_propose", 30.0, images=[hero])],
                Expect("workflow", [Check("envelope", step=0, target={
                    "path": "ok", "op": "eq", "value": True})]))
    raise ValueError(f"no workflow plan for {v!r}")


def tool_case(req: dict, plan: RequestPlan, tax: Taxonomy, body: str, tier: str) -> Case:
    cmap = tax.check_map
    role = cmap["role_for_problem"][req["problem"]]
    meta = {"sentence": req["sentence"], "class": plan.klass, "genre": req["genre"],
            "problem": req["problem"], "followups": list(req["followups"])}
    if plan.klass in ("impossible", "out"):
        phrase, rkey = nearest_phrase(req, tax)
        meta["roadmap_key"] = rkey
        steps = [step("darkroom_say", BUDGET_SAY, phrase=phrase)]
        for f in req["followups"]:
            if _followup_action(f, cmap) == "undo":
                steps.append(step("darkroom_undo_last", 3.5))
            else:
                steps.append(step("darkroom_say", BUDGET_SAY, phrase=f"{phrase} a bit"))
        expect = Expect("refuse")
    elif plan.klass == "workflow":
        steps, expect = _workflow(plan, req)
    else:
        steps = [cap_step(c, req, cmap) for c in plan.capabilities]
        checks = [stat_check(p) for p in plan.parts if p.capability]
        undo_follow = False
        for f in req["followups"]:
            if _followup_action(f, cmap) == "undo":
                steps.append(step("darkroom_undo_last", 3.5))
                undo_follow = True
            else:
                steps.extend(cap_step(c, req, cmap, bit=True) for c in plan.capabilities)
        if plan.klass == "partial":
            un = _unserved_modules(req, tax)
            if un:
                meta["roadmap_key"] = un[0]["id"]
                steps.append(step("darkroom_say", BUDGET_SAY, phrase=un[0]["phrases"][0]))
                checks.append(Check("envelope", step=len(steps) - 1, target={
                    "path": "ok", "op": "eq", "value": False}))
        if undo_follow:
            expect = Expect("workflow", [m1_restored([], {})])
        else:
            expect = Expect("edit", checks, {"changed": True})
    return Case(id=case_id(PREFIX, req["id"], body, "tool"), layer=LAYER, tier=tier,
                mode="tool", steps=steps, expect=expect, body=body, frame_role=role,
                source={"file": "requests.json", "id": req["id"], "cluster": req["cluster_id"]},
                meta=meta)


def agent_case(req: dict, plan: RequestPlan, tax: Taxonomy, tier: str) -> Case:
    cmap = tax.check_map
    role = cmap["role_for_problem"][req["problem"]]
    meta = {"sentence": req["sentence"], "class": plan.klass, "genre": req["genre"],
            "followups": list(req["followups"])}
    if plan.klass in ("impossible", "out"):
        meta["roadmap_key"] = nearest_phrase(req, tax)[1]
        expect = Expect("refuse")
    elif plan.klass == "workflow":
        expect = Expect("workflow", [], {"changed": plan.workflow_verb not in ("cull", "export")})
        if plan.workflow_verb == "undo":
            expect = Expect("workflow", [], {"changed": True})
    else:
        expect = Expect("edit", [stat_check(p) for p in plan.parts if p.capability],
                        {"changed": True})
        if plan.klass == "partial":
            meta["roadmap_key"] = (_unserved_modules(req, tax) or [{"id": "unknown"}])[0]["id"]
    return Case(id=case_id(PREFIX_AGENT, req["id"], "olympus", "agent"), layer=LAYER_AGENT,
                tier=tier, mode="agent", steps=[], expect=expect, body="olympus",
                frame_role=role, source={"file": "requests.json", "id": req["id"],
                                         "cluster": req["cluster_id"]}, meta=meta)


def generate(tax: Taxonomy) -> tuple[list[Case], list[Case]]:
    p0 = {r["id"] for r in select_p0(tax.requests)}
    tool: list[Case] = []
    agent: list[Case] = []
    for req in tax.requests:
        plan = plan_request(req, tax.check_map)
        tier = "P0" if req["id"] in p0 else "P1"
        body = body_for(req["id"])
        tool.append(tool_case(req, plan, tax, body, tier))
        if req["id"] in p0:
            tool.append(tool_case(req, plan, tax, other(body), tier))
            agent.append(agent_case(req, plan, tax, "P0"))
        elif req["intent"] == "impossible":
            agent.append(agent_case(req, plan, tax, "P1"))
    return tool, agent
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_scenarios.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/scenarios.py tests/gauntlet/test_layer_scenarios.py
git commit -m "gauntlet(scouter): layer 2 scenarios, 701 tool + 160 agent cases (Task 19, spec 3.2)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 20: Layer 3: bulk

PARALLEL-OK. Depends on: Tasks 9, 14.

**Files:**
- Create: `tests/gauntlet/layers/bulk.py`, `tests/gauntlet/exec_bulk.py`
- Test: `tests/gauntlet/test_layer_bulk.py`, `tests/gauntlet/test_exec_bulk.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `bulk.LAYER="bulk"`, `PREFIX="blk"`, `EXPECTED=40`, `SIZES={card: 7, job: 120}`, `CRASH_VERBS`, `flow_steps(flow, n) -> (steps, checks, kind)`, `generate(tax)`; `exec_bulk.run_bulk_case(session, case) -> Observation` (builds the card by duplication, runs the flow, deletes the card, sets `extra` `changed_n`, `reported_n`, `export_files`, `imported_n`, `frames_verified`, `exactly_once`, `journal_resumed`, `stats_ok_n`), `reported_n(envelopes)`, `count_changed`.

Crash-mid-job cases SIGKILL only the OWNED darktable (`dtprocess.owned_pid()`) once the job's events report frame 50, relaunch, and rerun the same verb; `exactly_once` fails on a sidecar with two identical consecutive history entries.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_bulk.py`:

```python
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import bulk


def test_bulk_count_and_crash_cases():
    cases = bulk.generate(taxonomy.load())
    assert len(cases) == bulk.EXPECTED == 40
    crash = [c for c in cases if c.meta["flow"] == "crash_mid_job"]
    assert len(crash) == 4 and all(c.meta["kill_after_frame"] == 50 for c in crash)
    assert next(c for c in cases if c.meta["flow"] == "time_lapse_deflicker").expect.kind \
        == "refuse"
```

`tests/gauntlet/test_exec_bulk.py`:

```python

from tests.gauntlet import exec_bulk


def test_reported_n_reads_lists_and_counts():
    assert exec_bulk.reported_n([{"data": {"applied": [1, 2, 3]}}, {"data": {"done": 7}}]) == 7
    assert exec_bulk.reported_n([{"data": {}}]) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_bulk.py tests/gauntlet/test_exec_bulk.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.bulk'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/bulk.py`:

```python
"""Layer 3: bulk (spec 3.3). 9 flows x 2 bodies x {card 7, job 120} = 36,
plus 4 crash-mid-job cases (olympus, 120 frames). P1."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.layers.common import BODIES, WRITE_TOOLS
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "bulk", "blk"
SIZES = {"card": 7, "job": 120}
PER_FRAME_S = 6.0
CRASH_VERBS = ("job_run", "export_run", "style_apply_set", "library_import")


def _s(tool: str, budget: float, **args) -> Step:
    return Step(tool, args, budget, tool in WRITE_TOOLS)


def _import(n: int) -> Step:
    return _s("library_import", PER_FRAME_S * n, source="{card}", job_slug="{job_slug}",
              confirm=True)


def _hero_edit() -> list[Step]:
    return [_s("darkroom_open", 30.0, image="{job_frame:1}"),
            _s("darkroom_say", 8.0, phrase="warmer")]


def flow_steps(flow: str, n: int) -> tuple[list[Step], list[Check], str]:
    """(steps, checks, expect kind) for one taxonomy bulk flow."""
    ok = lambda i: Check("envelope", step=i, target={"path": "ok", "op": "eq",  # noqa: E731
                                                     "value": True})
    count = lambda path, want: Check("extra", target={"path": path, "op": "gte",  # noqa: E731
                                                      "value": want})
    t = PER_FRAME_S * n
    if flow == "import_and_cull":
        return ([_import(n), _s("cull_propose", t, job_slug="{job_slug}"),
                 _s("cull_apply", t, job_slug="{job_slug}", proposal="{step:1:data.proposal}",
                    confirm=True)],
                [count("imported_n", n), ok(1), ok(2)], "workflow")
    if flow == "hero_then_propagate":
        return ([_import(n), *_hero_edit(), _s("preset_save", 12.0, name="{preset}",
                                               confirm=True),
                 _s("preset_apply_job", t, name="{preset}", job_slug="{job_slug}",
                    confirm=True)],
                [ok(4), count("changed_n", n - 1), count("stats_ok_n", min(10, n - 1))],
                "workflow")
    if flow == "copy_paste_history":
        return ([_import(n), *_hero_edit(),
                 _s("style_extract", 30.0, image="{job_frame:1}", name="{style}",
                    modules=["temperature"], confirm=True),
                 _s("style_apply_set", t, name="{style}", job_slug="{job_slug}", confirm=True)],
                [ok(4), count("changed_n", n - 1)], "workflow")
    if flow == "styles":
        return ([_import(n), *_hero_edit(),
                 _s("style_extract", 30.0, image="{job_frame:1}", name="{style}",
                    modules=["temperature"], confirm=True),
                 _s("style_apply", 30.0, image="{job_frame:2}", name="{style}")],
                [ok(4), count("changed_n", 1)], "workflow")
    if flow == "module_presets":
        return ([_import(n), *_hero_edit(), _s("preset_save", 12.0, name="{preset}",
                                               confirm=True),
                 _s("preset_list", 5.0),
                 _s("preset_apply", 30.0, name="{preset}", image="{job_frame:2}")],
                [ok(3), Check("envelope", step=4, target={"path": "data", "op": "exists"}),
                 ok(5), count("changed_n", 1)], "workflow")
    if flow == "batch_export_profiles":
        return ([_import(n),
                 _s("export_run", t, images="{job_frames}", preset="{export_preset}",
                    out_dir="{out_dir}/a", confirm=True),
                 _s("export_run", t, images="{job_frames}", preset="{export_preset_2}",
                    out_dir="{out_dir}/b", confirm=True),
                 _s("export_verify", 60.0, manifest="{step:1:data.manifest}")],
                [ok(1), ok(2), ok(3), count("export_files", 2 * n)], "workflow")
    if flow == "sidecar_handling":
        return ([_import(n), _s("library_verify", 60.0, job_slug="{job_slug}"),
                 _s("history_read", 10.0, image="{job_frame:1}", source="auto"),
                 _s("library_relocate", 120.0, new_root="{relocate_root}", confirm=True)],
                [ok(1), ok(2), ok(3)], "workflow")
    if flow == "outlier_handling":
        return ([_import(n), _s("outliers_list", t, reference="{job_frame:1}",
                                job_slug="{job_slug}")],
                [ok(1)], "workflow")
    if flow == "time_lapse_deflicker":
        return ([_s("darkroom_say", 8.0, phrase="deflicker the time-lapse")], [], "refuse")
    raise ValueError(f"no plan for bulk flow {flow!r}")


def generate(tax: Taxonomy) -> list[Case]:
    out: list[Case] = []
    for flow in (b["name"] for b in tax.bulk):
        for body in BODIES:
            for size, n in SIZES.items():
                steps, checks, kind = flow_steps(flow, n)
                out.append(Case(
                    id=case_id(PREFIX, flow, body, size), layer=LAYER, tier="P1", mode="tool",
                    steps=steps, expect=Expect(kind, checks),
                    body=body, frame_role="card",
                    source={"file": "taxonomy-modules.json", "bulk": flow},
                    meta={"flow": flow, "frames": n, "roadmap_key":
                          "deflicker" if flow == "time_lapse_deflicker" else None}))
    for verb in CRASH_VERBS:
        n = 120
        steps = [] if verb == "library_import" else [_import(n)]
        if verb == "job_run":
            steps.append(_s("job_run", PER_FRAME_S * n, job_slug="{job_slug}", preset="{preset}",
                            hero="{job_frame:1}", confirm=True))
        elif verb == "export_run":
            steps.append(_s("export_run", PER_FRAME_S * n, images="{job_frames}",
                            preset="{export_preset}", out_dir="{out_dir}", confirm=True))
        elif verb == "style_apply_set":
            steps.append(_s("style_apply_set", PER_FRAME_S * n, name="{style}",
                            job_slug="{job_slug}", confirm=True))
        else:
            steps.append(_import(n))
        out.append(Case(
            id=case_id(PREFIX, "crash_mid_job", verb), layer=LAYER, tier="P1", mode="tool",
            steps=steps, expect=Expect("workflow", [
                Check("extra", target={"path": "exactly_once", "op": "truthy"}),
                Check("extra", target={"path": "frames_verified", "op": "gte", "value": n}),
                Check("extra", target={"path": "journal_resumed", "op": "truthy"})]),
            body="olympus", frame_role="card",
            source={"file": "spec", "id": "3.3 crash-mid-job"},
            meta={"flow": "crash_mid_job", "verb": verb, "frames": n, "kill_after_frame": 50,
                  "needs_style": verb == "style_apply_set", "needs_preset": verb == "job_run"}))
    return out


EXPECTED = 9 * 2 * 2 + 4
```

`tests/gauntlet/exec_bulk.py`:

```python
"""Layer 3 executor (spec 3.3): stage the card by duplication, run the flow
through MCP, then count what really changed (the silent-failure (d) facts)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from gauntlet.adapter import Observation
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.corpus import build_card, delete_card
from tests.gauntlet.execute import substitute

STATS_SAMPLE = 10


def reported_n(envelopes: list[dict]) -> int | None:
    """The largest per-frame count any bulk envelope claims."""
    best = None
    for env in envelopes:
        data = (env or {}).get("data") or {}
        for key in ("applied", "done", "exported", "imported", "frames", "results", "images"):
            v = data.get(key)
            n = len(v) if isinstance(v, list) else v if isinstance(v, int) else None
            if n is not None:
                best = max(best or 0, n)
    return best


def count_changed(frames: list[Path], before: dict[str, str]) -> int:
    return sum(1 for f in frames
               if observe.snapshot(f).get("fingerprint") != before.get(str(f)))


def run_bulk_case(session, case: Case) -> Observation:
    t0 = time.monotonic()
    n = case.meta["frames"]
    card = session.root / "bulk" / case.id / "card"
    frames = build_card(session.corpus, case.body, n, card)
    session.staged.job_frames = frames
    session.staged.vars["card"] = str(card)
    session.staged.vars["job_slug"] = f"htc-{case.id}"[-40:]
    obs = Observation()
    try:
        before = {str(f): observe.snapshot(f).get("fingerprint") for f in frames}
        kill_at = case.meta.get("kill_after_frame")
        killer = None
        if kill_at:
            killer = threading.Thread(target=_kill_after, args=(session, kill_at), daemon=True)
            killer.start()
        for st in case.steps:
            args = substitute(st.args, session, obs.calls)
            rec = session.call(st.tool, args, st.budget_s, write=st.write)
            obs.calls.append(rec)
            if kill_at and rec.error:
                session.relaunch()
                again = session.call(st.tool, args, st.budget_s, write=st.write)
                obs.calls.append(again)
                obs.extra["journal_resumed"] = "resum" in json.dumps(again.envelope or {})
        job_files = [f for f in frames if f.exists()]
        obs.extra["changed_n"] = count_changed(job_files, before)
        obs.extra["reported_n"] = reported_n([c.envelope for c in obs.calls if c.write])
        out_dir = Path(session.staged.vars["out_dir"])
        obs.extra["export_files"] = sum(1 for _ in out_dir.rglob("*") if _.is_file()) \
            if out_dir.exists() else 0
        obs.extra["imported_n"] = reported_n([c.envelope for c in obs.calls
                                              if c.tool == "library_import"]) or 0
        obs.extra["frames_verified"] = max(obs.extra["changed_n"], obs.extra["export_files"])
        obs.extra["exactly_once"] = _exactly_once(frames)
        obs.extra["stats_ok_n"] = min(STATS_SAMPLE, obs.extra["changed_n"])
    finally:
        delete_card(card)
    obs.wall_s = time.monotonic() - t0
    return obs


def _exactly_once(frames: list[Path]) -> bool:
    """No sidecar carries the same operation+params twice in a row (a double apply)."""
    from scouter.core import xmp

    for f in frames:
        side = xmp.sidecar_path(f)
        if not side.exists():
            continue
        entries = xmp.parse(side.read_text(encoding="utf-8", errors="replace")).entries
        for a, b in zip(entries, entries[1:]):
            if a.operation == b.operation and a.params_hex == b.params_hex:
                return False
    return True


def _kill_after(session, frame_n: int, poll_s: float = 1.0, cap_s: float = 1800) -> None:
    """SIGKILL the OWNED darktable once the job journal reports frame `frame_n`."""
    import os
    import signal

    from scouter.core import dtprocess

    events = Path(session.staged.env["SCOUTER_CACHE"]) / "events.jsonl"
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap_s:
        if events.exists() and events.read_text(errors="replace").count('"frame"') >= frame_n:
            pid = dtprocess.owned_pid()
            if pid:
                os.kill(pid, signal.SIGKILL)
            return
        time.sleep(poll_s)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_bulk.py tests/gauntlet/test_exec_bulk.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/bulk.py tests/gauntlet/exec_bulk.py tests/gauntlet/test_layer_bulk.py tests/gauntlet/test_exec_bulk.py
git commit -m "gauntlet(scouter): layer 3 bulk flows and crash-mid-job (Task 20, spec 3.3)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 21: Layer 4: looks

PARALLEL-OK. Depends on: Tasks 9, 15.

**Files:**
- Create: `tests/gauntlet/layers/looks.py`, `tests/gauntlet/exec_looks.py`
- Test: `tests/gauntlet/test_layer_looks.py`, `tests/gauntlet/test_exec_looks.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `looks.LAYER="looks"`, `PREFIX="lk"`, `EXPECTED=469`, `gaps(look)`, `chart_checks(look, tol)`, `frame_checks(look)`, `generate(tax)` (golden case first, then chart + real frame per look; `meta.check_tags` parallel to the checks, `meta.gaps`, `meta.partial_min=0.6`); `exec_looks.chart_golden` and `look_plan` (virtual tools registered in `execute.VIRTUAL`), `plan_moves(look, cal, current) -> (moves, notes)`; calibration saved to `<run>/chart/calibration.json` with `_base_slope` and `_base_C`.

Targets with no route today are `gap` (grain, vignette, clarity, white point, hue shifts, B&W response, LUT-first) and feed S7, never a fail. Monochrome is approximated by global saturation at its lower bound and flagged in `look_notes`.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_looks.py`:

```python
import collections

from tests.gauntlet import taxonomy
from tests.gauntlet.layers import looks


def test_looks_count_gaps_and_tags():
    cases = looks.generate(taxonomy.load())
    assert len(cases) == looks.EXPECTED == 469
    chart = [c for c in cases if c.body == "chart"]
    assert len(chart) == 234
    assert all(len(c.meta["check_tags"]) == len(c.expect.checks) for c in chart)
    gaps = collections.Counter(g for c in chart for g in c.meta["gaps"])
    assert gaps["grain"] == 78 and gaps["lut3d"] == 64
    assert cases[0].id == "lk-golden-chart"
```

`tests/gauntlet/test_exec_looks.py`:

```python
from tests.gauntlet import chart_render, exec_looks, taxonomy


def test_plan_moves_is_linear_and_clamped():
    look = {"targets": {"tone": {"shadows_ev": 0.2, "mids_ev": 0, "highlights_ev": 0,
                                 "contrast": 0}, "saturation_delta": -1,
                        "grey_cast": {"a": 0, "b": 2.0},
                        "split_toning": {"shadow_hue": 200, "shadow_sat": 0.1,
                                         "highlight_hue": None, "highlight_sat": 0},
                        "black_lift": 0}}
    cal = {"colorbalancergb.shadows_luminance": 2.0, "temperature.temperature": 0.004}
    moves, notes = exec_looks.plan_moves(look, cal, {"temperature.temperature": 5000.0})
    d = {(m, p): v for m, p, v in moves}
    assert d[("colorbalancergb", "shadows_luminance")] == 0.1
    assert d[("temperature", "temperature")] == 5500.0
    assert d[("colorbalancergb", "global_saturation")] == -0.5
    assert d[("colorbalancergb", "shadows_hue")] == 200.0
    assert any("monochrome" in n for n in notes)


def test_every_look_plans_without_error():
    cal = {k: 1.0 for k in chart_render.CALIBRATE}
    for look in taxonomy.load().looks:
        exec_looks.plan_moves(look, cal, {})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_looks.py tests/gauntlet/test_exec_looks.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.looks'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/looks.py`:

```python
"""Layer 4: looks (spec 3.4). 1 golden-chart case + 234 on the chart + 234 on
a real hero frame (direction only). Moves are derived at run time from the
per-run calibration (the golden case measures it), never hand-tuned."""

from __future__ import annotations

import math

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.layers.common import BODIES
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "looks", "lk"
SOURCED = ("documented", "community-consensus")
TONE_PATCH = {"shadows_ev": "patch:ramp_-4", "mids_ev": "patch:ramp_+0",
              "highlights_ev": "patch:ramp_+2"}
TONE_REGION = {"shadows_ev": "shadows", "mids_ev": "frame", "highlights_ev": "highlights"}


def gaps(look: dict) -> list[str]:
    t = look["targets"]
    out = []
    if t.get("grain"):
        out.append("grain")
    if t.get("vignette"):
        out.append("vignette")
    if t.get("clarity"):
        out.append("clarity")
    if t.get("white_point", 100) != 100:
        out.append("white_point")
    if any((t.get("hue_shifts") or {}).get(k) for k in ("skin_deg", "sky_deg", "foliage_deg")):
        out.append("hue_shifts")
    if t.get("bw_response"):
        out.append("bw_response")
    first = (look.get("dt_modules") or [""])[0].lower()
    if "lut" in first:
        out.append("lut3d")
    return out


def _hue_axis(hue: float) -> tuple[str, int]:
    s, c = math.sin(math.radians(hue)), math.cos(math.radians(hue))
    return ("b", 1 if s > 0 else -1) if abs(s) >= abs(c) else ("a", 1 if c > 0 else -1)


def chart_checks(look: dict, tol: dict) -> tuple[list[Check], list[dict]]:
    t, conf = look["targets"], look["target_confidence"]
    checks: list[Check] = []
    tags: list[dict] = []

    def add(ch: Check, component: str) -> None:
        c = conf.get(component, "inferred")
        if c not in SOURCED:  # inferred: sign only
            ch.target = None
        checks.append(ch)
        tags.append({"confidence": c, "component": component})

    tone = t.get("tone") or {}
    for k, patch in TONE_PATCH.items():
        v = tone.get(k) or 0
        if v:
            add(Check("M2", stat="ev", region=patch, sign=1 if v > 0 else -1,
                      target={"value": v, "tol": tol[conf.get("tone", "inferred")]["ev"]}),
                "tone")
    if tone.get("contrast"):
        v = tone["contrast"]
        add(Check("M6", stat="slope", region="chart_tone", sign=1 if v > 0 else -1, kind="frac",
                  target={"value": v, "tol": tol[conf.get("tone", "inferred")]["sat"]}), "tone")
    sat = t.get("saturation_delta") or 0
    if sat:
        add(Check("M5", stat="C", region="chart_chromatic", sign=1 if sat > 0 else -1,
                  kind="frac", target={"value": sat, "tol": tol[conf.get(
                      "saturation_delta", "inferred")]["sat"]}), "saturation_delta")
    gc = t.get("grey_cast") or {}
    for axis in ("a", "b"):
        v = gc.get(axis) or 0
        if v:
            add(Check("M4", stat=axis, region="patch:ramp_+0", sign=1 if v > 0 else -1,
                      target={"value": v, "tol": tol[conf.get("grey_cast", "inferred")]["ab"]}),
                "grey_cast")
    st = t.get("split_toning") or {}
    for end, patch in (("shadow", "patch:ramp_-3"), ("highlight", "patch:ramp_+2")):
        hue, s = st.get(f"{end}_hue"), st.get(f"{end}_sat") or 0
        if hue is not None and s:
            axis, sign = _hue_axis(float(hue))
            add(Check("M4", stat=axis, region=patch, sign=sign), "split_toning")
    if t.get("black_lift"):
        add(Check("M3", stat="L", region="patch:ramp_-5", sign=1 if t["black_lift"] > 0 else -1),
            "black_lift")
    if not checks:
        checks.append(Check("M2", stat="L", region="patch:ramp_+0", sign=0, kind="unchanged"))
        tags.append({"confidence": look["confidence"], "component": "neutral"})
    return checks, tags


def frame_checks(look: dict) -> tuple[list[Check], list[dict]]:
    t, conf = look["targets"], look["target_confidence"]
    checks: list[Check] = []
    tags: list[dict] = []
    tone = t.get("tone") or {}
    for k, region in TONE_REGION.items():
        v = tone.get(k) or 0
        if v:
            checks.append(Check("M2", stat="L", region=region, sign=1 if v > 0 else -1))
            tags.append({"confidence": conf.get("tone", "inferred"), "component": "tone"})
    if tone.get("contrast"):
        checks.append(Check("M6", stat="spread", region="frame",
                            sign=1 if tone["contrast"] > 0 else -1))
        tags.append({"confidence": conf.get("tone", "inferred"), "component": "tone"})
    if t.get("saturation_delta"):
        checks.append(Check("M5", stat="C", region="frame",
                            sign=1 if t["saturation_delta"] > 0 else -1))
        tags.append({"confidence": conf.get("saturation_delta", "inferred"),
                     "component": "saturation_delta"})
    gc = t.get("grey_cast") or {}
    if gc.get("b"):
        checks.append(Check("M4", stat="rb", region="frame", sign=1 if gc["b"] > 0 else -1))
        tags.append({"confidence": conf.get("grey_cast", "inferred"), "component": "grey_cast"})
    if t.get("black_lift"):
        checks.append(Check("M3", stat="p01L", region="frame", sign=1))
        tags.append({"confidence": conf.get("black_lift", "inferred"), "component": "black_lift"})
    if not checks:
        checks.append(Check("M2", stat="L", region="frame", sign=0, kind="unchanged"))
        tags.append({"confidence": look["confidence"], "component": "neutral"})
    return checks, tags


def generate(tax: Taxonomy) -> list[Case]:
    tol = tax.look_conventions["suggested_tolerances"]
    out = [Case(id=case_id(PREFIX, "golden", "chart"), layer=LAYER, tier="P1", mode="tool",
                steps=[Step("chart_golden", {}, 300.0, False)],
                expect=Expect("workflow", [Check("M9", kind="unchanged"),
                                           Check("extra", target={"path": "calibration_ok",
                                                                  "op": "truthy"})],
                              property="P7"),
                frame_role="chart", source={"file": "looks.json", "id": "golden"})]
    for i, look in enumerate(tax.looks):
        tier = "P1" if look["confidence"] in SOURCED else "P2"
        g = gaps(look)
        checks, tags = chart_checks(look, tol)
        meta = {"look": look["id"], "family": look["family"], "gaps": g, "check_tags": tags,
                "partial_min": 0.6, "monochrome": (look["targets"].get("saturation_delta") == -1)}
        out.append(Case(id=case_id(PREFIX, look["id"], "chart"), layer=LAYER, tier=tier,
                        mode="tool", steps=[Step("look_plan", {"look_id": look["id"],
                                                               "image": "{frame:chart}"},
                                                 40.0, True)],
                        expect=Expect("edit", checks, {"changed": bool(checks and
                                                                    checks[0].sign)}),
                        body="chart", frame_role="chart",
                        source={"file": "looks.json", "id": look["id"]}, meta=meta))
        body = BODIES[i % 2]
        fchecks, ftags = frame_checks(look)
        out.append(Case(id=case_id(PREFIX, look["id"], body), layer=LAYER, tier=tier,
                        mode="tool", steps=[Step("look_plan", {"look_id": look["id"],
                                                               "image": "{frame:hero}"},
                                                 40.0, True)],
                        expect=Expect("edit", fchecks, {"changed": bool(fchecks[0].sign)}),
                        body=body, frame_role="hero",
                        source={"file": "looks.json", "id": look["id"]},
                        meta=dict(meta, check_tags=ftags, gaps=[])))
    return out


EXPECTED = 1 + 234 * 2
```

`tests/gauntlet/exec_looks.py`:

```python
"""Layer 4 executors: `chart_golden` (P7 + per-run calibration) and
`look_plan` (a look's reachable targets -> absolute Scouter moves)."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapter import CallRecord
from tests.gauntlet import chart_render
from tests.gauntlet.execute import VIRTUAL

CAL_FILE = "calibration.json"


def _cal_path(session) -> Path:
    return session.run_root / "chart" / CAL_FILE


def chart_golden(session, case, step, obs) -> CallRecord:
    frame = session.ensure_frame("chart")
    work = session.root / "render"
    first = chart_render.render_stats(frame, work / "golden-a.ppm")
    second = chart_render.render_stats(frame, work / "golden-b.ppm")
    repeat = chart_render.compare_golden(second, golden=_tmp_golden(session, first))
    golden = chart_render.compare_golden(first)
    chart_render.write_golden_candidate(session.run_root, first)
    obs.extra["patch_de"] = golden["patch_de"] or repeat["patch_de"]
    obs.extra["golden"] = golden
    cal: dict[str, float | None] = {}
    for key, amount in chart_render.PROBE.items():
        module, param = key.split(".")
        session.call("darkroom_adjust", {"module": module, "param": param, "delta": amount},
                     8.0, write=True)
        after = chart_render.render_stats(frame, work / f"cal-{key}.ppm")
        cal[key] = chart_render.slope(first, after, key, amount)
        session.call("darkroom_undo_last", {}, 8.0, write=True)
    cal["_base_slope"] = first["chart_tone"]["slope"]
    cal["_base_C"] = first["chart_chromatic"]["C"]
    (session.run_root / "chart").mkdir(parents=True, exist_ok=True)
    _cal_path(session).write_text(json.dumps(cal, indent=2, sort_keys=True) + "\n")
    obs.extra["calibration"] = cal
    obs.extra["calibration_ok"] = all(v not in (None, 0.0) for k, v in cal.items()
                                      if not k.startswith("_"))
    return CallRecord("chart_golden", {}, {"ok": True, "outcome": "ok",
                                           "data": {"golden": golden["verdict"]}})


def _tmp_golden(session, stats) -> Path:
    p = session.root / "render" / "repeat-golden.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"labs": chart_render.labs(stats)}))
    return p


def plan_moves(look: dict, cal: dict, current: dict) -> tuple[list[tuple[str, str, float]],
                                                               list[str]]:
    """[(module, param, absolute value)], notes. Linear per-param solve."""
    from scouter.core import vocab

    t = look["targets"]
    moves: list[tuple[str, str, float]] = []
    notes: list[str] = []

    def add(key: str, delta_metric: float) -> None:
        s = cal.get(key)
        if not s:
            notes.append(f"no calibration for {key}")
            return
        module, param = key.split(".")
        row = vocab.spec(module, param)
        base = current.get(key, row.neutral if row.neutral is not None else 0.0)
        target, _clamped = vocab.clamp(row, base + delta_metric / s)
        moves.append((module, param, round(target, 4)))

    tone = t.get("tone") or {}
    for k, key in (("shadows_ev", "colorbalancergb.shadows_luminance"),
                   ("mids_ev", "colorbalancergb.midtones_luminance"),
                   ("highlights_ev", "colorbalancergb.highlights_luminance")):
        if tone.get(k):
            add(key, tone[k])
    if tone.get("contrast"):
        if "_base_slope" in cal:  # contrast is a fraction of the untouched slope
            add("sigmoid.contrast", tone["contrast"] * cal["_base_slope"])
        else:
            notes.append("no base slope measured")
    sat = t.get("saturation_delta") or 0
    if sat == -1:
        moves.append(("colorbalancergb", "global_saturation", -0.5))
        notes.append("monochrome approximated by global saturation at its lower bound")
    elif sat:
        if "_base_C" in cal:  # saturation is a fraction of the untouched mean C*
            add("colorbalancergb.global_chroma", sat * cal["_base_C"])
        else:
            notes.append("no base chroma measured")
    gc = t.get("grey_cast") or {}
    if gc.get("b"):
        add("temperature.temperature", gc["b"])
    if gc.get("a"):
        moves.append(("temperature", "tint", 0.9 if gc["a"] > 0 else 1.1))
    st = t.get("split_toning") or {}
    for end, zone in (("shadow", "shadows"), ("highlight", "highlights")):
        if st.get(f"{end}_hue") is not None and st.get(f"{end}_sat"):
            moves.append(("colorbalancergb", f"{zone}_hue", float(st[f"{end}_hue"]) % 360))
            moves.append(("colorbalancergb", f"{zone}_chroma", min(0.3, float(st[f"{end}_sat"]))))
    if t.get("black_lift"):
        add("exposure.black", t["black_lift"])
    return moves, notes


def look_plan(session, case, step, obs) -> CallRecord:
    from tests.gauntlet.taxonomy import load

    look = next(x for x in load().looks if x["id"] == step.args["look_id"])
    cal = json.loads(_cal_path(session).read_text()) if _cal_path(session).exists() else {}
    moves, notes = plan_moves(look, cal, obs.before.get("params", {}))
    sub = []
    for module, param, value in moves:
        r = session.call("darkroom_set", {"module": module, "param": param, "value": value},
                         8.0, write=True)
        sub.append({"tool": "darkroom_set", "module": module, "param": param, "value": value,
                    "ok": bool((r.envelope or {}).get("ok")), "error": r.error})
    obs.extra["look_moves"] = sub
    obs.extra["look_notes"] = notes
    ok = all(m["ok"] for m in sub)
    return CallRecord("look_plan", step.args, {"ok": ok, "outcome": "ok" if ok else
                                               "error:partial", "data": {"moves": sub}},
                      write=True)


VIRTUAL["chart_golden"] = chart_golden
VIRTUAL["look_plan"] = look_plan
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_looks.py tests/gauntlet/test_exec_looks.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/looks.py tests/gauntlet/exec_looks.py tests/gauntlet/test_layer_looks.py tests/gauntlet/test_exec_looks.py
git commit -m "gauntlet(scouter): layer 4 looks with golden chart and per-run calibration (Task 21, spec 3.4)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 22: Layer 5: robustness and fault injection

PARALLEL-OK. Depends on: Tasks 9, 14, 20.

**Files:**
- Create: `tests/gauntlet/layers/robustness.py`, `tests/gauntlet/faults.py`
- Test: `tests/gauntlet/test_layer_robustness.py`, `tests/gauntlet/test_faults.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `robustness.LAYER="robustness"`, `PREFIX="rob"`, `EXPECTED=32`, `P0`, `PLANS` (R1-R24: fault, steps, expect kind, codes), `WORKFLOW_CHECKS`, `HANG` (H1-H4), `WATCHDOG` (4), `generate(tax)`; `faults.arm(session, fault) -> vars`, `disarm(session, vars)`, `run_fault_case(session, case) -> Observation`, `watchdog_probe` (virtual tool).

Every fault acts on the session's OWN darktable (SIGSTOP/SIGKILL of `owned_pid()` only), frames and dirs; the disk-full case uses a 50 MB `hdiutil` image detached in `disarm`. H2 has no plugin hook today, so it returns `blocked: skipped: ...` (P0, needs Pratik's `accepted` triage). R21 runs host-free through `FakeBridge`. The watchdog cases depend on Tasks 29-30 being merged before night 1.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_robustness.py`:

```python
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import robustness


def test_robustness_count_and_p0():
    cases = robustness.generate(taxonomy.load())
    assert len(cases) == robustness.EXPECTED == 32
    assert {c.source["id"] for c in cases if c.tier == "P0"} == \
        {"R1", "R3", "R12", "R17", "H1", "H2", "H3", "H4"}
    assert next(c for c in cases if c.source["id"] == "R24").body == "sony_a7"
```

`tests/gauntlet/test_faults.py`:

```python
import os
from pathlib import Path
from types import SimpleNamespace

from tests.gauntlet import faults


def _session(tmp_path):
    hero = tmp_path / "frames" / "hero" / "H.ORF"
    hero.parent.mkdir(parents=True)
    hero.write_bytes(b"raw")
    Path(str(hero) + ".xmp").write_text("<x:xmpmeta>" + "a" * 100 + "</x:xmpmeta>")
    staged = SimpleNamespace(frames={"hero": hero}, library=tmp_path / "library.db",
                             env={"SCOUTER_CONFIG": str(tmp_path / "config" / "config.json")},
                             vars={}, job_frames=[])
    return SimpleNamespace(staged=staged, root=tmp_path, body="olympus")


def test_file_faults_arm_and_disarm(tmp_path):
    s = _session(tmp_path)
    v = faults.arm(s, "chmod_sidecar_readonly")
    side = Path(v["_restore_mode"])
    assert oct(side.stat().st_mode & 0o777) == "0o444"
    faults.disarm(s, v)
    assert oct(side.stat().st_mode & 0o777) == "0o644"
    v = faults.arm(s, "torn_sidecar_copy")
    torn = Path(v["torn_copy"] + ".xmp").read_text()
    assert len(torn) < 70
    v = faults.arm(s, "none")
    assert not Path(v["missing_file"]).exists() and Path(v["unsupported_file"]).exists()
    v = faults.arm(s, "future_mtime")
    assert os.path.getmtime(str(s.staged.frames["hero"]) + ".xmp") > 2e9 - 1e9
    v = faults.arm(s, "modal_dialog")
    assert "no modal-dialog test hook" in v["_skip"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_robustness.py tests/gauntlet/test_faults.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.robustness'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/robustness.py`:

```python
"""Layer 5: robustness (spec 3.5). R1-R24 + H1-H4 + 4 watchdog cases = 32.
P0: R1, R3, R12, R17 and H1-H4. Every expectation is a typed answer."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "robustness", "rob"
P0 = {"R1", "R3", "R12", "R17", "H1", "H2", "H3", "H4"}
TIMEOUTS = ["bridge_timeout", "commit_timeout", "gui_not_ready", "darktable_unresponsive"]

#: id -> (fault, steps, expect kind, codes, extra checks). The fault name is
#: what tests/gauntlet/faults.py implements; `steps` run after it is armed.
PLANS: dict[str, tuple] = {
    "R1": ("quit_darktable", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
           ["bridge_timeout", "darktable_not_running"]),
    "R2": ("kill_during_call", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
           TIMEOUTS + ["darktable_not_running"]),
    "R3": ("wedge_worker", [("darkroom_say", {"phrase": "brighter"})], "typed_error", TIMEOUTS),
    "R4": ("relaunch_stale_job", [("darkroom_undo_last", {})], "typed_error",
           ["noop", "usage", "state_locked", "not_found"]),
    "R5": ("delete_sidecar", [("history_read", {"image": "{frame:hero}", "source": "xmp"})],
           "typed_error", ["no_sidecar", "not_found"]),
    "R6": ("chmod_sidecar_readonly", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
           ["permission_denied", "cli_wrote_nothing", "commit_timeout"]),
    "R7": ("stale_sidecar", [("history_read", {"image": "{frame:hero}", "source": "auto"})],
           "workflow", []),
    "R8": ("torn_sidecar_copy", [("history_read", {"image": "{torn_copy}", "source": "xmp"})],
           "typed_error", ["usage", "no_sidecar", "error"]),
    "R9": ("second_library_lock", [("library_import", {"source": "{card}",
                                                       "job_slug": "{job_slug}",
                                                       "confirm": True})],
           "typed_error", ["db_locked", "library_locked"]),
    "R10": ("unicode_path", [("darkroom_open", {"image": "{unicode_copy}"}),
                             ("darkroom_say", {"phrase": "brighter"})], "workflow", []),
    "R11": ("long_path", [("darkroom_open", {"image": "{long_copy}"})], "typed_error",
            ["usage", "not_found", "unsupported_format"]),
    "R12": ("none", [("darkroom_open", {"image": "{missing_file}"})], "typed_error",
            ["not_found", "usage"]),
    "R13": ("none", [("darkroom_open", {"image": "{unsupported_file}"})], "typed_error",
            ["unsupported_format", "usage"]),
    "R14": ("huge_job_one_timeout", [("job_run", {"job_slug": "{job_slug}", "preset": "{preset}",
                                                  "hero": "{job_frame:1}", "confirm": True})],
            "workflow", []),
    "R15": ("duplicate_names", [("darkroom_open", {"image": "{bare_duplicate_name}"})],
            "typed_error", ["usage", "not_found"]),
    "R16": ("two_clients", [("darkroom_say", {"phrase": "brighter"})], "workflow", []),
    "R17": ("slow_bridge", [("darkroom_say", {"phrase": "brighter"})], "typed_error", TIMEOUTS),
    "R18": ("disk_nearly_full", [("export_run", {"images": ["{frame:hero}"],
                                                 "preset": "{export_preset}",
                                                 "out_dir": "{full_disk}", "confirm": True})],
            "typed_error", ["disk_full", "export_all_failed"]),
    "R19": ("chmod_out_dir", [("export_run", {"images": ["{frame:hero}"],
                                              "preset": "{export_preset}",
                                              "out_dir": "{denied_dir}", "confirm": True})],
            "typed_error", ["permission_denied", "export_all_failed"]),
    "R20": ("future_mtime", [("history_read", {"image": "{frame:hero}", "source": "auto"})],
            "workflow", []),
    "R21": ("fake_version", [("vocab_list", {})], "typed_error",
            ["darktable_version_mismatch"]),
    "R22": ("none", [("darkroom_enable", {"module": "liquify"})], "typed_error",
            ["usage", "module_unavailable"]),
    "R23": ("broken_luarc", [("darkroom_say", {"phrase": "brighter"})], "typed_error",
            TIMEOUTS + ["darktable_not_running"]),
    "R24": ("wrong_wb_table", [("wb_kelvin", {"image": "{frame:hero}"})], "workflow", []),
}
#: Workflow checks for the cases whose right answer is not an error.
WORKFLOW_CHECKS = {
    "R7": [Check("envelope", step=0, target={"path": "ok", "op": "eq", "value": True})],
    "R10": [Check("envelope", step=1, target={"path": "ok", "op": "eq", "value": True}),
            Check("extra", target={"path": "sidecar_changed", "op": "truthy"})],
    "R14": [Check("extra", target={"path": "frames_reported", "op": "gte", "value": 200}),
            Check("extra", target={"path": "timeout_frame_reported", "op": "truthy"})],
    "R16": [Check("extra", target={"path": "no_interleaved_writes", "op": "truthy"})],
    "R20": [Check("envelope", step=0, target={"path": "ok", "op": "eq", "value": True})],
    "R24": [Check("extra", target={"path": "flagged_or_refused", "op": "truthy"})],
}
HANG = {
    "H1": ("cold_launch_commit", [("history_commit", {"image": "{frame:hero}", "timeout_s": 10})],
           ["noop", "gui_not_ready"]),
    "H2": ("modal_dialog", [("darkroom_say", {"phrase": "brighter"})], TIMEOUTS),
    "H3": ("full_res_pipe_busy", [("darkroom_say", {"phrase": "brighter"})] * 10, []),
    "H4": ("view_bounces", [("history_commit", {"image": "{frame:hero}"})] * 50, []),
}
WATCHDOG = {
    "wd_ping": "ping_states", "wd_auto_heal": "auto_heal_once",
    "wd_heal_cap": "heal_cap_reached", "wd_foreign_pid": "foreign_pid",
}


def _steps(pairs) -> list[Step]:
    return [Step(t, dict(a), 12.0, t not in ("vocab_list", "history_read", "wb_kelvin"))
            for t, a in pairs]


def generate(tax: Taxonomy) -> list[Case]:
    rows = {r["id"]: r for r in tax.measurement["robustness"]}
    out: list[Case] = []
    for rid in sorted(rows, key=lambda x: int(x[1:])):
        fault, pairs, kind, codes = PLANS[rid]
        exp = Expect(kind, WORKFLOW_CHECKS.get(rid, []), codes=list(codes))
        out.append(Case(id=case_id(PREFIX, rid), layer=LAYER, tier="P0" if rid in P0 else "P1",
                        mode="tool", steps=_steps(pairs), expect=exp,
                        body="sony_a7" if rid == "R24" else "olympus",
                        frame_role="hero", source={"file": "measurement-and-robustness.json",
                                                   "id": rid},
                        meta={"fault": fault, "condition": rows[rid]["condition"]}))
    for hid, (fault, pairs, codes) in HANG.items():
        checks = [Check("extra", target={"path": "every_call_answered", "op": "truthy"}),
                  Check("extra", target={"path": "hung_calls", "op": "eq", "value": 0})]
        exp = Expect("workflow", checks, codes=list(codes))
        out.append(Case(id=case_id(PREFIX, hid), layer=LAYER, tier="P0", mode="tool",
                        steps=_steps(pairs), expect=exp, body="olympus", frame_role="hero",
                        source={"file": "measurement-and-robustness.json", "id": hid},
                        meta={"fault": fault, "allowed_codes": list(codes)}))
    for wid, fault in WATCHDOG.items():
        out.append(Case(id=case_id(PREFIX, wid), layer=LAYER, tier="P1", mode="tool",
                        steps=[Step("watchdog_probe", {"scenario": fault}, 90.0, False)],
                        expect=Expect("workflow", [Check("extra", target={
                            "path": "scenario_ok", "op": "truthy"})]),
                        body="olympus", frame_role="hero",
                        source={"file": "spec", "id": f"6/{wid}"},
                        meta={"fault": fault, "allow_heal": wid == "wd_auto_heal"}))
    return out


EXPECTED = 24 + 4 + 4
```

`tests/gauntlet/faults.py`:

```python
"""Layer 5 fault injection (spec 3.5). Each fault arms a condition on the
session's OWN darktable, frames and dirs (never the user's), and returns the
extra placeholders its steps use. `disarm` always runs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.corpus import copy_readonly
from tests.gauntlet.execute import VIRTUAL, substitute


def _owned_kill(sig=signal.SIGKILL) -> None:
    from scouter.core import dtprocess

    pid = dtprocess.owned_pid()
    if pid:
        os.kill(pid, sig)


def arm(session, fault: str) -> dict:
    st = session.staged
    hero = st.frames["hero"]
    v: dict = {}
    if fault == "quit_darktable":
        session.dt.terminate()
    elif fault == "kill_during_call":
        threading.Timer(1.0, _owned_kill).start()
    elif fault in ("wedge_worker", "slow_bridge"):
        _owned_kill(signal.SIGSTOP)  # frozen process: the bridge cannot answer
        v["_resume_sigcont"] = True
    elif fault == "relaunch_stale_job":
        session.relaunch()
    elif fault == "delete_sidecar":
        side = Path(str(hero) + ".xmp")
        if side.exists():
            side.unlink()
    elif fault == "chmod_sidecar_readonly":
        side = Path(str(hero) + ".xmp")
        side.touch()
        side.chmod(0o444)
        v["_restore_mode"] = str(side)
    elif fault == "stale_sidecar":
        side = Path(str(hero) + ".xmp")
        if side.exists():
            os.utime(side, (time.time() - 86400 * 30,) * 2)
    elif fault == "torn_sidecar_copy":
        side = Path(str(hero) + ".xmp")
        torn = session.root / "torn" / hero.name
        copy_readonly(hero, torn)
        text = side.read_bytes() if side.exists() else b"<x:xmpmeta"
        Path(str(torn) + ".xmp").write_bytes(text[: len(text) // 2])
        v["torn_copy"] = str(torn)
    elif fault == "second_library_lock":
        lock = Path(str(st.library) + ".lock")
        lock.write_text(f"{os.getpid()}\n")
        v["_remove"] = str(lock)
    elif fault == "unicode_path":
        v["unicode_copy"] = str(copy_readonly(hero, session.root / "ünï cødé dir" / f"frâme {hero.name}"))  # noqa: E501
    elif fault == "long_path":
        v["long_copy"] = str(session.root / ("x" * 255) / hero.name)
    elif fault == "none":
        v["missing_file"] = str(session.root / "does-not-exist.ORF")
        unsupported = session.root / "notes.txt"
        unsupported.write_text("not an image")
        v["unsupported_file"] = str(unsupported)
    elif fault == "duplicate_names":
        for sub in ("a", "b"):
            copy_readonly(hero, session.root / "dups" / sub / hero.name)
        v["bare_duplicate_name"] = hero.name
    elif fault == "disk_nearly_full":
        v.update(_small_disk(session))
    elif fault == "chmod_out_dir":
        d = session.root / "denied"
        d.mkdir(exist_ok=True)
        d.chmod(0o500)
        v["denied_dir"] = str(d)
        v["_restore_dir"] = str(d)
    elif fault == "future_mtime":
        side = Path(str(hero) + ".xmp")
        side.touch()
        os.utime(side, (time.time() + 86400 * 365,) * 2)
    elif fault == "cold_launch_commit":
        session.relaunch()  # the step then runs ~1 s after launch, before any settle
    elif fault == "wrong_wb_table":
        wb = Path(st.env["SCOUTER_CONFIG"]).parent / "wb"
        src = wb / "em10iii.json"
        if src.exists():
            (wb / "a7.json").write_bytes(src.read_bytes())
    elif fault == "broken_luarc":
        cfg = session.root / "broken-configdir"
        (cfg / "lua").mkdir(parents=True, exist_ok=True)
        (cfg / "luarc").write_text('error("htc: deliberately broken luarc")\n')
        session.dt.terminate()
        from scouter.core import dtprocess

        dtprocess.launch(hero, session.root / "broken-library" / "library.db", cfg)
        v["_relaunch"] = True
    elif fault == "huge_job_one_timeout":
        from tests.gauntlet.corpus import build_card

        card = session.root / "huge" / "card"
        frames = build_card(session.corpus, case_body(session), 200, card)
        frames[100].write_bytes(frames[100].read_bytes()[:4096])  # a truncated raw
        st.job_frames = frames
        v["card"] = str(card)
        v["bad_frame"] = frames[100].name
    elif fault == "modal_dialog":
        v["_skip"] = "no modal-dialog test hook in the darktable_mcp plugin (spec 3.5 H2)"
    elif fault in ("fake_version", "two_clients", "full_res_pipe_busy", "view_bounces"):
        pass  # handled in run_fault_case / by the case's own steps
    return v


def case_body(session) -> str:
    return session.body


def disarm(session, v: dict) -> None:
    if v.get("_relaunch"):
        session.relaunch()
    if v.get("_resume_sigcont"):
        _owned_kill(signal.SIGCONT)
        session.relaunch()
    for key in ("_restore_mode", "_restore_dir"):
        if v.get(key):
            os.chmod(v[key], 0o755 if key == "_restore_dir" else 0o644)
    if v.get("_remove"):
        Path(v["_remove"]).unlink(missing_ok=True)
    if v.get("_detach"):
        subprocess.run(["hdiutil", "detach", v["_detach"], "-force"], capture_output=True,
                       timeout=60)


def _small_disk(session) -> dict:
    img = session.root / "full.dmg"
    subprocess.run(["hdiutil", "create", "-size", "50m", "-fs", "HFS+", "-volname", "HTCFULL",
                    str(img)], capture_output=True, timeout=60, check=True)
    out = subprocess.run(["hdiutil", "attach", str(img), "-nobrowse"], capture_output=True,
                         text=True, timeout=60, check=True).stdout
    mount = out.strip().splitlines()[-1].split("\t")[-1].strip()
    with open(Path(mount) / "filler.bin", "wb") as f:
        f.write(b"\0" * (49 * 1024 * 1024))
    return {"full_disk": mount, "_detach": mount}


def run_fault_case(session, case: Case) -> Observation:
    from tests.gauntlet.execute import run_tool_case

    fault = case.meta["fault"]
    if fault == "fake_version":
        return _fake_version()
    v = arm(session, fault)
    session.staged.vars.update({k: val for k, val in v.items() if not k.startswith("_")})
    try:
        if v.get("_skip"):
            return Observation(blocked=f"skipped: {v['_skip']}")
        if case.id.startswith("rob-h"):
            return _hang_case(session, case)
        if fault == "two_clients":
            return _two_clients(session, case)
        obs = run_tool_case(session, case)
        _post_facts(fault, obs, v)
        return obs
    finally:
        disarm(session, v)


def _post_facts(fault: str, obs: Observation, v: dict) -> None:
    import json as _json

    if fault == "unicode_path":
        obs.extra["sidecar_changed"] = obs.before.get("fingerprint") != \
            obs.after.get("fingerprint") or any(c.envelope and c.envelope.get("ok")
                                                for c in obs.calls[1:])
    if fault == "huge_job_one_timeout":
        text = _json.dumps([c.envelope for c in obs.calls])
        from tests.gauntlet.exec_bulk import reported_n

        obs.extra["frames_reported"] = reported_n([c.envelope for c in obs.calls]) or 0
        obs.extra["timeout_frame_reported"] = v.get("bad_frame", "@@") in text
    if fault == "wrong_wb_table":
        env = (obs.calls[-1].envelope or {}) if obs.calls else {}
        data = env.get("data") or {}
        obs.extra["flagged_or_refused"] = (not env.get("ok")) or data.get("kelvin") is None \
            or bool(data.get("warnings"))


def _fake_version() -> Observation:
    """R21 host-free: a bridge double answering diag with darktable 5.6.0."""
    from scouter import registry
    from scouter.core import dtbridge

    from tests.scouter_fakes import FakeBridge

    fake = FakeBridge(responses={"darktable_mcp.diag": {"darktable_version": "5.6.0",
                                                        "gui_ready": True},
                                 "view.current": {"view": "darkroom"}})
    old = dtbridge.get_bridge()
    dtbridge.set_bridge(fake)
    try:
        env = registry.call("darkroom.status", "gauntlet", module="exposure", param="exposure")
        env = env if isinstance(env, dict) else getattr(env, "to_dict", lambda: {})()
    finally:
        dtbridge.set_bridge(old)
    return Observation(calls=[CallRecord("darkroom_status", {}, env)])


def _two_clients(session, case: Case) -> Observation:
    """R16: a second MCP server on the same image; interleaved darkroom_say."""
    second = session.mcp_factory(session.staged.env, session.root)
    obs = Observation(before=observe.snapshot(session.staged.frames["hero"]))
    results: list = []

    def fire(client, phrase):
        try:
            results.append(client.call_tool("darkroom_say", {"phrase": phrase}, 60))
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "error": {"code": f"transport: {e}"}})

    threads = [threading.Thread(target=fire, args=(c, p)) for c, p in
               ((session.mcp, "brighter"), (second, "warmer"), (session.mcp, "darker"),
                (second, "cooler"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    second.close()
    obs.after = observe.snapshot(session.staged.frames["hero"])
    ok_calls = sum(1 for r in results if r.get("ok"))
    grew = (obs.after.get("history_end") or 0) - (obs.before.get("history_end") or 0)
    obs.calls = [CallRecord("darkroom_say", {}, r, write=True) for r in results]
    obs.extra["no_interleaved_writes"] = grew >= 0 and all(
        r.get("ok") or (r.get("error") or {}).get("code") in ("state_locked", "db_locked")
        for r in results) and (ok_calls == 0 or grew >= 1)
    return obs


def _hang_case(session, case: Case) -> Observation:
    """H1-H4: every call must answer within its cap with an allowed code or ok."""
    obs = Observation()
    hung = 0
    allowed = set(case.meta.get("allowed_codes", []))
    for st in case.steps:
        rec = session.call(st.tool, substitute(st.args, session, obs.calls), st.budget_s,
                           write=st.write)
        obs.calls.append(rec)
        if rec.error == "timeout":
            hung += 1
        elif rec.error and allowed and rec.error not in allowed:
            obs.extra.setdefault("unexpected_codes", []).append(rec.error)
    obs.extra["hung_calls"] = hung
    obs.extra["every_call_answered"] = hung == 0 and not obs.extra.get("unexpected_codes")
    return obs


def watchdog_probe(session, case, step, obs) -> CallRecord:
    """The 4 watchdog cases (spec 3.5) through `scouter doctor` and a SIGSTOPped
    owned darktable; `foreign_pid` uses a sleeper process the runner owns."""
    import json as _json
    import sys

    scen = step.args["scenario"]
    env = dict(session.staged.env)

    def doctor(*flags):
        p = subprocess.run([sys.executable, "-m", "scouter", "doctor", *flags], env=env,
                           capture_output=True, text=True, timeout=90)
        try:
            return p.returncode, _json.loads(p.stdout)
        except ValueError:
            return p.returncode, {}

    ok = False
    detail: dict = {}
    if scen == "ping_states":
        code_alive, rep_alive = doctor("--ping")
        _owned_kill(signal.SIGSTOP)
        try:
            code_dead, rep_dead = doctor("--ping")
        finally:
            _owned_kill(signal.SIGCONT)
        ok = code_alive == 0 and code_dead == 1
        detail = {"alive": rep_alive.get("watchdog"), "unresponsive": rep_dead.get("watchdog")}
    elif scen in ("auto_heal_once", "heal_cap_reached"):
        cfg = Path(env["SCOUTER_CONFIG"])
        doc = _json.loads(cfg.read_text())
        doc["watchdog"] = {"auto_heal": True, "max_heals_per_job": 1, "heal_budget_s": 60}
        cfg.write_text(_json.dumps(doc))
        _owned_kill(signal.SIGSTOP)
        first = session.call("darkroom_say", {"phrase": "brighter"}, 90.0, write=True)
        detail["first"] = first.envelope
        if scen == "auto_heal_once":
            ok = bool((first.envelope or {}).get("healed")) and (first.envelope or {}).get("ok")
        else:
            _owned_kill(signal.SIGSTOP)
            second = session.call("darkroom_say", {"phrase": "brighter"}, 90.0, write=True)
            detail["second"] = second.envelope
            ok = ((second.envelope or {}).get("error") or {}).get("code") == \
                "darktable_unresponsive"
            _owned_kill(signal.SIGCONT)
        doc.pop("watchdog")
        cfg.write_text(_json.dumps(doc))
        session.relaunch()
    elif scen == "foreign_pid":
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            from scouter.core import dtprocess

            res = dtprocess.terminate(sleeper.pid)
            ok = res["how"] == "refused_not_ours" and sleeper.poll() is None
            detail = res
        finally:
            sleeper.kill()
    obs.extra["scenario_ok"] = bool(ok)
    obs.extra["watchdog_detail"] = detail
    return CallRecord("watchdog_probe", step.args, {"ok": bool(ok), "outcome": "ok",
                                                    "data": detail})


VIRTUAL["watchdog_probe"] = watchdog_probe
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_robustness.py tests/gauntlet/test_faults.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/robustness.py tests/gauntlet/faults.py tests/gauntlet/test_layer_robustness.py tests/gauntlet/test_faults.py
git commit -m "gauntlet(scouter): layer 5 robustness, faults and watchdog cases (Task 22, spec 3.5)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 23: Layer 6: MCP/CLI parity

PARALLEL-OK. Depends on: Tasks 9, 14.

**Files:**
- Create: `tests/gauntlet/layers/parity.py`, `tests/gauntlet/exec_parity.py`
- Test: `tests/gauntlet/test_layer_parity.py`, `tests/gauntlet/test_exec_parity.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `parity.LAYER="parity"`, `PREFIX="par"`, `EXPECTED=90`, `GUIDE_VERBS` (P0), `happy_args(verb_id)` (from `tests/scouter_smoke_args.SMOKE_ARGS`), `error_args(verb)`, `generate()`; `exec_parity.normalise(env)`, `compare(a, b) -> list[str]`, `cli_argv(verb_id, args)`, `parity_pair` (virtual tool; fresh frame copy per side; sets `extra.envelopes_match`, `parity_diffs`).

Normalisation drops timings, ids, jobs and paths (`VOLATILE`) and compares `ok`, `outcome`, `error.code`, `data` keys and scalar `data` values.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_parity.py`:

```python
from tests.gauntlet.layers import parity


def test_parity_is_45_verbs_times_2():
    cases = parity.generate()
    assert len(cases) == parity.EXPECTED == 90  # 45 verbs today
    assert sum(c.tier == "P0" for c in cases) == 24
```

`tests/gauntlet/test_exec_parity.py`:

```python

from tests.gauntlet import exec_parity


def test_normalise_drops_volatile_fields():
    a = {"ok": True, "outcome": "ok", "ms": 12, "job": "j1", "data": {"k": 1, "path": "/a"}}
    b = {"ok": True, "outcome": "ok", "ms": 99, "job": "j2", "data": {"k": 1, "path": "/b"}}
    assert exec_parity.compare(a, b) == []
    c = dict(b, data={"k": 2})
    assert exec_parity.compare(a, c) == ["values"]


def test_cli_argv_follows_the_registry():
    argv = exec_parity.cli_argv("darkroom.set", {"module": "exposure", "param": "exposure",
                                                 "value": 1.0, "commit": True})
    assert argv[:2] == ["darkroom", "set"] and "--no-commit" not in argv
    argv = exec_parity.cli_argv("darkroom.say", {"phrase": "brighter", "dry_run": True})
    assert "--dry-run" in argv and "brighter" in argv
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_parity.py tests/gauntlet/test_exec_parity.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.parity'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/parity.py`:

```python
"""Layer 6: MCP/CLI parity (spec 3.6). 45 tools x {happy, error} = 90.

Happy arguments come from tests/scouter_smoke_args.SMOKE_ARGS (the one table
every registered verb must already have). The error variant breaks the first
string argument, or adds an unknown argument when there is none."""

from __future__ import annotations

from scouter import registry

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.scouter_smoke_args import (
    SMOKE_ARGS,
    SMOKE_CONFIG_PATH,
    SMOKE_OUT_DIR,
    SMOKE_RAW,
    SMOKE_SOURCE_DIR,
)

LAYER, PREFIX = "parity", "par"
#: The verbs the user guide and README walkthrough use (P0 per spec 3.6).
GUIDE_VERBS = {"darkroom.open", "darkroom.set", "darkroom.say", "darkroom.undo_last",
               "scout.stats", "history.read", "preset.save", "preset.apply_job", "job.run",
               "vocab.list", "export.release", "wb.kelvin"}
PLACEHOLDERS = {SMOKE_RAW: "{frame:hero}", SMOKE_OUT_DIR: "{out_dir}",
                SMOKE_CONFIG_PATH: "{config_path}", SMOKE_SOURCE_DIR: "{card}"}
MISSING = "/nonexistent/htc-missing.ORF"


def _swap(v):
    if isinstance(v, str):
        return PLACEHOLDERS.get(v, v)
    if isinstance(v, list):
        return [_swap(x) for x in v]
    return v


def happy_args(verb_id: str) -> dict:
    return {k: _swap(v) for k, v in SMOKE_ARGS[verb_id].items()}


def error_args(verb: registry.Verb) -> dict:
    args = happy_args(verb.id)
    for spec in verb.args:
        if spec.type in ("str", "image") and spec.name in args:
            args[spec.name] = MISSING if spec.type == "image" else "__htc_bad_value__"
            return args
    args["__htc_unknown__"] = 1
    return args


def generate() -> list[Case]:
    verbs = registry.load_verbs()
    out: list[Case] = []
    for vid in sorted(verbs):
        verb = verbs[vid]
        tier = "P0" if vid in GUIDE_VERBS else "P1"
        for variant, args in (("happy", happy_args(vid)), ("error", error_args(verb))):
            out.append(Case(
                id=case_id(PREFIX, vid, variant), layer=LAYER, tier=tier, mode="tool",
                steps=[Step("parity_pair", {"verb": vid, "args": args}, 60.0, verb.writes)],
                expect=Expect("workflow", [Check("extra", target={"path": "envelopes_match",
                                                                  "op": "truthy"})]),
                body="olympus", frame_role="hero", source={"file": "registry", "id": vid},
                meta={"verb": vid, "variant": variant, "mcp": verb.mcp_name,
                      "cli": [verb.group, verb.cli_command]}))
    return out


#: 2 x the registered verbs (45 today -> 90; a new verb adds 2).
EXPECTED = 2 * len(registry.load_verbs())
```

`tests/gauntlet/exec_parity.py`:

```python
"""Layer 6 executor (spec 3.6): the same verb and arguments through the MCP
stdio server and through `scouter <group> <verb>` (JSON on stdout), each on
its own fresh copy of the frame, envelopes compared after normalising."""

from __future__ import annotations

import json
import subprocess
import sys

from gauntlet.adapter import CallRecord
from tests.gauntlet.corpus import copy_readonly
from tests.gauntlet.execute import VIRTUAL, substitute

VOLATILE = {"ms", "job", "layer", "ts", "id", "duration_s", "elapsed_ms", "event_id", "path",
            "dtstyle_path", "memory_path", "manifest", "out_dir", "image_id"}


def normalise(env: dict | None) -> dict:
    env = env or {}
    data = env.get("data") or {}
    err = env.get("error") or {}
    return {"ok": bool(env.get("ok")), "outcome": env.get("outcome"),
            "code": err.get("code"),
            "data_keys": sorted(k for k in data if k not in VOLATILE),
            "values": {k: v for k, v in sorted(data.items())
                       if k not in VOLATILE and isinstance(v, (bool, int, float, str))}}


def cli_argv(verb_id: str, args: dict) -> list[str]:
    from scouter import registry

    verb = registry.load_verbs()[verb_id]
    argv = [verb.group, verb.cli_command]
    for spec in verb.args:
        if spec.name not in args:
            continue
        val = args[spec.name]
        if spec.type == "bool":
            if val:
                argv.append({"dry_run": "--dry-run", "confirm": "--yes"}.get(
                    spec.name, f"--{spec.name.replace('_', '-')}"))
            elif spec.name == "commit" and spec.default is True:
                argv.append("--no-commit")
            continue
        text = ",".join(map(str, val)) if isinstance(val, list) else str(val)
        if spec.positional:
            argv.extend(val if isinstance(val, list) else [text])
        else:
            argv += [f"--{spec.name.replace('_', '-')}", text]
    extra = [k for k in args if k not in {s.name for s in verb.args}]
    for k in extra:
        argv += [f"--{k.replace('_', '-')}", str(args[k])]
    return argv


def compare(a: dict, b: dict) -> list[str]:
    na, nb = normalise(a), normalise(b)
    return [k for k in na if na[k] != nb[k]]


def parity_pair(session, case, step, obs) -> CallRecord:
    verb_id = step.args["verb"]
    hero = session.staged.frames["hero"]
    mcp_copy = copy_readonly(hero, session.root / "parity" / "mcp" / hero.name)
    cli_copy = copy_readonly(hero, session.root / "parity" / "cli" / hero.name)
    session.staged.frames["hero"] = mcp_copy
    mcp_args = substitute(step.args["args"], session, [])
    mcp_rec = session.call(case.meta["mcp"], mcp_args, step.budget_s, write=step.write)
    session.staged.frames["hero"] = cli_copy
    cli_args = substitute(step.args["args"], session, [])
    session.staged.frames["hero"] = hero
    p = subprocess.run([sys.executable, "-m", "scouter", *cli_argv(verb_id, cli_args)],
                       env=session.staged.env, capture_output=True, text=True,
                       timeout=step.budget_s + 30)
    try:
        cli_env = json.loads(p.stdout)
    except ValueError:
        cli_env = {"ok": False, "outcome": f"exit {p.returncode}", "raw": p.stdout[-300:],
                   "stderr": p.stderr[-300:]}
    diffs = compare(mcp_rec.envelope, cli_env)
    obs.extra["envelopes_match"] = not diffs
    obs.extra["parity_diffs"] = diffs
    obs.extra["cli_envelope"] = cli_env
    return CallRecord("parity_pair", step.args, {"ok": not diffs, "outcome": "ok",
                                                 "data": {"diffs": diffs}}, write=step.write)


VIRTUAL["parity_pair"] = parity_pair
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_parity.py tests/gauntlet/test_exec_parity.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/parity.py tests/gauntlet/exec_parity.py tests/gauntlet/test_layer_parity.py tests/gauntlet/test_exec_parity.py
git commit -m "gauntlet(scouter): layer 6 MCP/CLI parity, 90 cases (Task 23, spec 3.6)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 24: Layer 7: install rehearsal

PARALLEL-OK. Depends on: Tasks 9, 14.

**Files:**
- Create: `tests/gauntlet/layers/install.py`, `tests/gauntlet/exec_install.py`
- Test: `tests/gauntlet/test_layer_install.py`, `tests/gauntlet/test_exec_install.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `install.LAYER="install"`, `PREFIX="ins"`, `EXPECTED=10`, `ACTIONS` (10, ordered), `generate(tax)`; `exec_install.Rehearsal(root)` with `env()` and `action(name) -> (ok, detail)`, `mcp_servers_hash(path)`, `newer_than(stamp, roots)`, `InstallSession(root)`.

HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME, SCOUTER_CONFIG, SCOUTER_CACHE, OBSIDIAN_VAULT_PATH and darktable `--configdir` all point inside `<run>/install/`. `claude mcp add/list` run with HOME = the rehearsal home, so `~/.claude.json` is never written; its `mcpServers` object is only hashed before and after.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_install.py`:

```python
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import install


def test_install_is_10_ordered_cli_cases():
    cases = install.generate(taxonomy.load())
    assert [c.meta["order"] for c in cases] == list(range(10))
    assert {c.mode for c in cases} == {"cli"} and {c.tier for c in cases} == {"P0"}
```

`tests/gauntlet/test_exec_install.py`:

```python
import json

from tests.gauntlet import exec_install


def test_rehearsal_env_is_confined(tmp_path):
    r = exec_install.Rehearsal(tmp_path)
    ok, detail = r.action("check_env")
    assert ok and all(v.startswith(str(tmp_path)) for v in detail.values())


def test_mcp_servers_hash_ignores_other_keys(tmp_path):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"mcpServers": {"a": 1}, "numStartups": 1}))
    h1 = exec_install.mcp_servers_hash(p)
    p.write_text(json.dumps({"mcpServers": {"a": 1}, "numStartups": 2}))
    assert exec_install.mcp_servers_hash(p) == h1
    assert exec_install.mcp_servers_hash(tmp_path / "none.json") == "absent"


def test_leak_check_finds_new_files(tmp_path):
    stamp = tmp_path / "stamp"
    stamp.write_text("x")
    import os
    import time

    os.utime(stamp, (time.time() - 10,) * 2)
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "new.txt").write_text("leak")
    assert exec_install.newer_than(stamp, (str(watched),))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_install.py tests/gauntlet/test_exec_install.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.install'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/install.py`:

```python
"""Layer 7: install rehearsal (spec 3.7). The 8 install_rehearsal steps, an
idempotent re-run of `scouter setup`, and the teardown leak check. P0, CLI."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet.taxonomy import Taxonomy

LAYER, PREFIX = "install", "ins"
#: One concrete action per rehearsal step (the JSON's steps are prose).
ACTIONS = (
    ("01_isolated_dirs", "make_dirs", "rehearsal root has home/, config/, cache/"),
    ("02_env_scoped", "check_env", "every subprocess env points inside the root"),
    ("03_first_launch", "first_launch", "darktable --configdir writes darktablerc and library.db"),
    ("04_setup", "setup", "scouter setup writes a valid config (version field present)"),
    ("05_setup_check", "setup_check", "scouter setup --check reports healthy"),
    ("06_mcp_register", "mcp_register", "claude mcp list shows scouter -> python -m scouter.mcp.server"),  # noqa: E501
    ("07_teardown", "teardown", "scratch root deleted"),
    ("08_rerun_on_existing", "rerun_existing", "setup on a pre-existing empty config is idempotent"),  # noqa: E501
    ("09_setup_idempotent", "setup_again", "a second scouter setup changes nothing"),
    ("10_leak_check", "leak_check", "no file newer than the stamp outside the root; mcpServers hash unchanged"),  # noqa: E501
)


def generate(tax: Taxonomy) -> list[Case]:
    assert len(tax.measurement["install_rehearsal"]["steps"]) == 8
    return [Case(id=case_id(PREFIX, label), layer=LAYER, tier="P0", mode="cli",
                 steps=[Step("rehearsal", {"action": action}, 120.0, False)],
                 expect=Expect("workflow", [Check("extra", target={"path": "action_ok",
                                                                   "op": "truthy"})]),
                 source={"file": "measurement-and-robustness.json", "step": i + 1},
                 meta={"action": action, "asserts": what, "order": i})
            for i, (label, action, what) in enumerate(ACTIONS)]


EXPECTED = 10
```

`tests/gauntlet/exec_install.py`:

```python
"""Layer 7 executor (spec 3.7): the install rehearsal in an isolated HOME/XDG
root. Actions run in order inside ONE session; each case checks one action.
Pratik's ~/.claude.json is never written: `claude mcp` runs with HOME set to
the rehearsal home, and the real file's mcpServers object is only hashed."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case

REPO = Path(__file__).resolve().parents[2]
WATCHED = ("~/.config/darktable", "~/Library/Application Support/darktable", "~/.config/scouter")


def mcp_servers_hash(path: Path = Path.home() / ".claude.json") -> str:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "absent"
    return hashlib.sha256(json.dumps(doc.get("mcpServers", {}), sort_keys=True)
                          .encode()).hexdigest()


def newer_than(stamp: Path, roots=WATCHED) -> list[str]:
    hits = []
    for r in roots:
        p = Path(r).expanduser()
        if not p.exists():
            continue
        out = subprocess.run(["find", str(p), "-newer", str(stamp)], capture_output=True,
                             text=True, timeout=120)
        hits += [x for x in out.stdout.splitlines() if x.strip()]
    return hits


class Rehearsal:
    def __init__(self, root: Path, *, run=subprocess.run) -> None:
        self.root = root
        self.run = run
        self.home, self.config, self.cache = root / "home", root / "config", root / "cache"
        self.stamp = root / "rehearsal.stamp"
        self.hash_before = ""
        self.facts: dict = {}

    def env(self) -> dict:
        import os

        env = {k: v for k, v in os.environ.items() if k not in
               ("SCOUTER_CONFIG", "SCOUTER_CACHE", "SCOUTER_JOB", "OBSIDIAN_VAULT_PATH")}
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config),
                   XDG_CACHE_HOME=str(self.cache), PYTHONPATH=str(REPO),
                   SCOUTER_CONFIG=str(self.config / "scouter" / "config.json"),
                   SCOUTER_CACHE=str(self.cache / "scouter"),
                   OBSIDIAN_VAULT_PATH=str(self.root / "vault"),
                   DARKTABLE_MCP_PLUGIN_PATH=str(self.config / "darktable" / "lua" /
                                                 "darktable_mcp.lua"))
        return env

    def _scouter(self, *args: str, timeout: float = 120) -> tuple[int, dict]:
        p = self.run([sys.executable, "-m", "scouter", *args], env=self.env(),
                     capture_output=True, text=True, timeout=timeout, cwd=str(self.root))
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {"raw": p.stdout[-500:], "stderr": p.stderr[-500:]}

    def action(self, name: str) -> tuple[bool, dict]:
        if name == "make_dirs":
            for d in (self.home, self.config, self.cache):
                d.mkdir(parents=True, exist_ok=True)
            self.stamp.write_text(str(time.time()))
            time.sleep(1.1)  # find -newer has 1 s resolution on some filesystems
            self.hash_before = mcp_servers_hash()
            return all(d.is_dir() for d in (self.home, self.config, self.cache)), {}
        if name == "check_env":
            env = self.env()
            keys = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "SCOUTER_CONFIG",
                    "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH")
            return all(env[k].startswith(str(self.root)) for k in keys), {k: env[k] for k in keys}
        if name == "first_launch":
            from scouter.core import dtprocess

            cfg = self.config / "darktable"
            rec = dtprocess.launch(None, cfg / "library.db", cfg)
            time.sleep(15)
            res = dtprocess.terminate(rec.pid)
            ok = (cfg / "darktablerc").exists() and (cfg / "library.db").exists()
            return ok, {"terminate": res}
        if name in ("setup", "setup_again", "rerun_existing"):
            if name == "rerun_existing":
                (self.config / "scouter").mkdir(parents=True, exist_ok=True)
                (self.config / "scouter" / "config.json").write_text("{}\n")
            before = self._tree_hash()
            code, rep = self._scouter("setup")
            ok = code == 0 and json.loads((self.config / "scouter" / "config.json")
                                          .read_text()).get("version") is not None
            if name == "setup_again":
                ok = ok and self._tree_hash() == before
            return ok, rep
        if name == "setup_check":
            code, rep = self._scouter("setup", "--check")
            return code == 0 and bool(rep.get("ok")), rep
        if name == "mcp_register":
            server = f"{sys.executable} -m scouter.mcp.server"
            self.run(["claude", "mcp", "add", "scouter", "--scope", "user", "--",
                      *server.split()], env=self.env(), capture_output=True, text=True,
                     timeout=60)
            p = self.run(["claude", "mcp", "list"], env=self.env(), capture_output=True,
                         text=True, timeout=60)
            ok = "scouter" in p.stdout and "scouter.mcp.server" in p.stdout
            return ok, {"list": p.stdout[-800:]}
        if name == "teardown":
            self.facts["leaks"] = newer_than(self.stamp)
            self.facts["hash_after"] = mcp_servers_hash()
            for d in (self.home, self.config, self.cache):
                shutil.rmtree(d, ignore_errors=True)
            return not any(d.exists() for d in (self.home, self.config, self.cache)), {}
        if name == "leak_check":
            leaks = self.facts.get("leaks", newer_than(self.stamp))
            same = self.facts.get("hash_after", mcp_servers_hash()) == self.hash_before
            return not leaks and same, {"leaks": leaks[:50], "mcp_servers_unchanged": same}
        raise ValueError(f"unknown rehearsal action {name!r}")

    def _tree_hash(self) -> str:
        h = hashlib.sha256()
        for p in sorted((self.config / "scouter").rglob("*")):
            if p.is_file():
                h.update(p.name.encode() + p.read_bytes())
        return h.hexdigest()


class InstallSession:
    """Implements gauntlet.adapter.Session for layer install (one rehearsal)."""

    def __init__(self, root: Path) -> None:
        self.rehearsal = Rehearsal(root)

    def run_case(self, case: Case) -> Observation:
        t0 = time.monotonic()
        try:
            ok, detail = self.rehearsal.action(case.meta["action"])
        except Exception as e:  # noqa: BLE001 - a failed action is a result
            ok, detail = False, {"error": f"{type(e).__name__}: {e}"[:300]}
        return Observation(calls=[CallRecord("rehearsal", {"action": case.meta["action"]},
                                             {"ok": ok, "outcome": "ok", "data": detail})],
                           extra={"action_ok": ok, "detail": detail},
                           wall_s=time.monotonic() - t0)

    def abort(self) -> None:
        pass

    def close(self) -> None:
        pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_install.py tests/gauntlet/test_exec_install.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/install.py tests/gauntlet/exec_install.py tests/gauntlet/test_layer_install.py tests/gauntlet/test_exec_install.py
git commit -m "gauntlet(scouter): layer 7 install rehearsal in an isolated HOME (Task 24, spec 3.7)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 25: Layer 8: guide walkthrough and learning mode

PARALLEL-OK. Depends on: Tasks 9, 14.

**Files:**
- Create: `tests/gauntlet/layers/guide.py`, `tests/gauntlet/exec_guide.py`
- Test: `tests/gauntlet/test_layer_guide.py`, `tests/gauntlet/test_exec_guide.py`

**Interfaces:**
- Consumes: `taxonomy.Taxonomy`, `checkmap`, `layers.common`, `gauntlet.case`, `execute.VIRTUAL/substitute/run_tool_case`, `ScouterSession`.
- Produces: `guide.LAYER="guide"`, `PREFIX="gd"`, `EXPECTED=35`, `WALKTHROUGH` (20 literal commands/phrases from README "A hero edit", "A hero session" and the vault user guide), `EDU_MOVES`, `LEVELS`, `generate()`; `exec_guide.cli` (virtual tool `cli`: runs `python -m scouter <argv>` with the session env, records `extra.exit` and `extra.stdout`).

README commands name `~/Pictures/EditingTrial/P6281348.ORF`; the walkthrough substitutes the staged hero copy (`{frame:hero}`), so the original is never opened. `test_walkthrough_matches_the_readme` fails when the README drifts from the list.

- [ ] **Step 1: Write the failing tests**

`tests/gauntlet/test_layer_guide.py`:

````python
import re
from pathlib import Path

from tests.gauntlet.layers import guide


def test_guide_count():
    cases = guide.generate()
    assert len(cases) == guide.EXPECTED == 35 and len({c.id for c in cases}) == 35


def test_walkthrough_matches_the_readme():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()

    def section(title):
        m = re.search(r"^#+ " + re.escape(title) + r".*?$(.*?)(?=^#+ |\Z)", readme, re.M | re.S)
        return m.group(1) if m else ""

    cmds = []
    for title in ("A hero edit", "A hero session"):
        inb = False
        for line in section(title).splitlines():
            if line.strip().startswith("```"):
                inb = not inb
                continue
            if inb and line.strip().startswith("scouter "):
                cmds.extend(x.strip() for x in line.strip().split("&&"))
    assert len(cmds) == 11, "README walkthrough changed: update guide.WALKTHROUGH"
    listed = [" ".join(p) for _l, k, p, _w in guide.WALKTHROUGH[:11] if k == "cli"]
    for got, want in zip(cmds, listed):
        words = [w for w in want.split() if not w.startswith("{")]
        assert all(w.strip('"') in got for w in words), (got, want)
````

`tests/gauntlet/test_exec_guide.py`:

```python
import json
from types import SimpleNamespace

from tests.gauntlet import exec_guide


def test_cli_handler_records_exit_and_json(tmp_path, monkeypatch):
    class P:
        returncode = 0
        stdout = json.dumps({"ok": True, "outcome": "ok", "data": {}})

    monkeypatch.setattr(exec_guide.subprocess, "run", lambda *a, **k: P())
    sess = SimpleNamespace(staged=SimpleNamespace(env={}, frames={"hero": tmp_path / "h.ORF"},
                                                  vars={}, job_frames=[]), root=tmp_path)
    obs = SimpleNamespace(calls=[], extra={})
    step = SimpleNamespace(args={"argv": ["darkroom", "open", "{frame:hero}"]}, budget_s=10)
    rec = exec_guide.cli(sess, None, step, obs)
    assert rec.envelope["ok"] and obs.extra["exit"] == 0
    assert rec.args["argv"][-1].endswith("h.ORF")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_guide.py tests/gauntlet/test_exec_guide.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.layers.guide'`.

- [ ] **Step 3: Write the implementation**

`tests/gauntlet/layers/guide.py`:

```python
"""Layer 8: user-guide walkthrough (20) + educative levels (15) = 35, P0.

WALKTHROUGH is the literal command list from README "A hero edit" and "A
hero session" and the vault user guide (Knowledge/Scouter - User guide
2026-09-26), in document order. `test_guide_layer.py` re-extracts the README
commands and fails when the README drifts from this list."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id

LAYER, PREFIX = "guide", "gd"
#: (label, kind, payload, expected). kind "cli": argv after `scouter`; kind
#: "say": an MCP darkroom_say phrase from the guide's phrase table.
WALKTHROUGH: tuple[tuple[str, str, object, dict], ...] = (
    ("readme_open", "cli", ["darkroom", "open", "{frame:hero}"], {"ok": True}),
    ("readme_set_exposure", "cli", ["darkroom", "set", "exposure", "exposure", "1.2"],
     {"ok": True, "param": ("exposure.exposure", 1.2, "EV")}),
    ("readme_set_contrast", "cli", ["darkroom", "set", "sigmoid", "contrast", "1.35"],
     {"ok": True, "param": ("sigmoid.contrast", 1.35, "ratio")}),
    ("readme_stats", "cli", ["--pretty", "scout", "stats", "{frame:hero}", "--px", "1024"],
     {"exit": 0}),
    ("readme_undo", "cli", ["darkroom", "undo-last"], {"ok": True}),
    ("readme_history", "cli", ["--pretty", "history", "read", "{frame:hero}"], {"exit": 0}),
    ("session_touch_warmer", "cli", ["--pretty", "darkroom", "say", "a touch warmer"],
     {"exit": 0}),
    ("session_less_contrast_full", "cli",
     ["--pretty", "darkroom", "say", "less contrast", "--educative", "full"], {"exit": 0}),
    ("session_undo", "cli", ["darkroom", "undo-last"], {"ok": True}),
    ("session_preset_save", "cli", ["preset", "save", "harbour-warm"], {"ok": True}),
    ("session_preset_apply_job", "cli",
     ["preset", "apply-job", "harbour-warm", "--job-slug", "{job_slug}", "--yes"], {"ok": True}),
    ("guide_slightly_brighter", "say", "slightly brighter",
     {"ok": True, "delta": ("exposure.exposure", 0.15, "EV")}),
    ("guide_touch_warmer", "say", "a touch warmer",
     {"ok": True, "delta": ("temperature.temperature", 150.0, "K")}),
    ("guide_less_contrast", "say", "less contrast", {"ok": True}),
    ("guide_lift_shadows", "say", "lift the shadows", {"ok": True}),
    ("guide_more_vibrance", "say", "a bit more vibrance", {"ok": True}),
    ("guide_warm_highlights", "say", "warm the highlights",
     {"ok": True, "param": ("colorbalancergb.highlights_hue", 60.0, "°")}),
    ("guide_more_magenta_refuses", "say", "more magenta", {"code": "value_unknown"}),
    ("guide_job_run_dry", "cli", ["--pretty", "job", "run", "--job-slug", "{job_slug}",
                                  "--preset", "harbour-warm", "--hero", "{job_frame:1}",
                                  "--dry-run"], {"exit": 0}),
    ("guide_job_run", "cli", ["--pretty", "job", "run", "--job-slug", "{job_slug}", "--preset",
                              "harbour-warm", "--hero", "{job_frame:1}", "--yes"], {"exit": 0}),
)
EDU_MOVES = (("brighter", "exposure"), ("darker", "exposure"), ("warmer", "white_balance"),
             ("cooler", "white_balance"), ("more contrast", "tone_mapper"))
LEVELS = ("off", "brief", "full")


def _walk_case(i: int, label: str, kind: str, payload, want: dict) -> Case:
    checks: list[Check] = []
    if kind == "cli":
        step = Step("cli", {"argv": list(payload)}, 60.0, True)
        if "exit" in want:
            checks.append(Check("extra", target={"path": "exit", "op": "eq",
                                                 "value": want["exit"]}))
    else:
        step = Step("darkroom_say", {"phrase": payload}, 8.0, True)
    if want.get("ok"):
        checks.append(Check("envelope", step=0, target={"path": "ok", "op": "eq",
                                                        "value": True}))
    if "param" in want:
        k, v, unit = want["param"]
        checks.append(Check("M1", kind="equals", target={"key": k, "value": v, "unit": unit}))
    if "delta" in want:
        k, d, unit = want["delta"]
        checks.append(Check("M1", kind="delta", target={"key": k, "delta": d, "unit": unit,
                                                        "tol": abs(d) * 0.1}))
    exp = Expect("typed_error", checks, codes=[want["code"]]) if "code" in want else \
        Expect("workflow", checks)
    return Case(id=case_id(PREFIX, f"{i:02d}", label), layer=LAYER, tier="P0",
                mode="cli" if kind == "cli" else "tool", steps=[step], expect=exp,
                body="olympus", frame_role="hero", source={"file": "README/user guide",
                                                           "label": label},
                meta={"order": i, "kind": kind, "session": "walkthrough"})


def _edu_case(level: str, j: int, phrase: str, concept: str) -> Case:
    seen_before = any(c == concept for _p, c in EDU_MOVES[:j])
    if level == "off":
        chk = Check("envelope", step=0, target={"path": "data.explain", "op": "len_eq",
                                                "value": 0})
    elif level == "brief" or seen_before:
        chk = Check("envelope", step=0, target={"path": "data.explain", "op": "len_eq",
                                                "value": 1})
    else:
        chk = Check("envelope", step=0, target={"path": "data.explain", "op": "len_gte",
                                                "value": 2})
    checks = [chk]
    if level == "brief":
        checks.append(Check("envelope", step=0, target={"path": "data.explain.0",
                                                        "op": "regex", "value": r"\d.*\d"}))
    return Case(id=case_id(PREFIX, "edu", level, f"{j}", phrase), layer=LAYER, tier="P0",
                mode="tool", steps=[Step("darkroom_say", {"phrase": phrase, "educative": level},
                                         8.0, True)],
                expect=Expect("workflow", checks), body="olympus", frame_role="hero",
                source={"file": "README", "section": "Educative mode"},
                meta={"session": f"edu-{level}", "order": j, "concept": concept})


def generate() -> list[Case]:
    out = [_walk_case(i, *row) for i, row in enumerate(WALKTHROUGH)]
    for level in LEVELS:
        out.extend(_edu_case(level, j, p, c) for j, (p, c) in enumerate(EDU_MOVES))
    return out


EXPECTED = 35
```

`tests/gauntlet/exec_guide.py`:

```python
"""Layer 8 executor: `cli` virtual tool (a documented command run literally,
placeholders filled with the session's staged copies)."""

from __future__ import annotations

import json
import subprocess
import sys

from gauntlet.adapter import CallRecord
from tests.gauntlet.execute import VIRTUAL, substitute


def cli(session, case, step, obs) -> CallRecord:
    argv = substitute(list(step.args["argv"]), session, obs.calls)
    p = subprocess.run([sys.executable, "-m", "scouter", *argv], env=session.staged.env,
                       capture_output=True, text=True, timeout=step.budget_s + 30,
                       cwd=str(session.root))
    try:
        env = json.loads(p.stdout)
    except ValueError:
        env = {"ok": p.returncode == 0, "outcome": "ok" if p.returncode == 0 else
               f"exit {p.returncode}", "data": {"stdout": p.stdout[-2000:]}}
    obs.extra["exit"] = p.returncode
    obs.extra["stdout"] = p.stdout[-4000:]
    return CallRecord("cli", {"argv": argv}, env, write=True)


VIRTUAL["cli"] = cli
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_layer_guide.py tests/gauntlet/test_exec_guide.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/gauntlet/layers/guide.py tests/gauntlet/exec_guide.py tests/gauntlet/test_layer_guide.py tests/gauntlet/test_exec_guide.py
git commit -m "gauntlet(scouter): layer 8 guide walkthrough and educative levels (Task 25, spec 3.8)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part 4: CLI and the progress page

### Task 27: Progress HTML renderer

PARALLEL-OK. Depends on: Task 7.

**Files:**
- Create: `gauntlet/html.py`
- Test: `gauntlet/tests/test_html.py`

**Interfaces:**
- Consumes: the `progress.json` dict (Task 7) and optionally `report.json` (Task 8).
- Produces: `html.render(progress, report=None, title="Gauntlet") -> str`: one self-contained page (no scripts), light/dark tokens on `:root` with `prefers-color-scheme` and `data-theme` overrides, per-layer bars (pass/partial/fail), the silent/crash/P0 tiles, the parked count, and (with a report) the release-bar verdict and every non-pass with its repro. The controller publishes this file as the "Scouter Gauntlet" artifact (same URL every time).

- [ ] **Step 1: Write the failing test**

```python
from gauntlet import html as page


def test_html_escapes_and_handles_empty_progress():
    out = page.render({"stamp": "<x>", "layers": {"a<b": {"total": 0}}}, None, "T&T")
    assert "&lt;x&gt;" in out and "T&amp;T" in out and "a&lt;b" in out
    assert "prefers-color-scheme:dark" in out


def test_bars_signoff_and_parked_line():
    prog = {"stamp": "s", "status": "parked", "parked": ["a", "b"],
            "layers": {"coverage": {"total": 4, "done": 4, "pass": 2, "partial": 1, "fail": 1,
                                    "p0_pass": 1, "p0_total": 1, "silent": 1}}}
    rep = {"S8_signoff": {"release_bar_met": False},
           "S3_non_pass": [{"id": "c1", "outcome": "silent_failure", "repro": "python -m x"}]}
    out = page.render(prog, rep, "Scouter Gauntlet")
    assert "Release bar: NOT MET" in out and "Parked (waiting on a usage limit): 2" in out
    assert 'style="width:50.00%"' in out and "<code>c1</code>" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_html.py -q`
Expected: `ImportError: cannot import name 'html' from 'gauntlet'`.

- [ ] **Step 3: Write the implementation**

```python
# ruff: noqa: E501 - the page template is easier to read unwrapped
"""The progress page (spec 8): one self-contained HTML file rendered from
progress.json (and report.json when it exists). The controller publishes it
as the same artifact URL at every checkpoint. Pure stdlib, no scripts."""

from __future__ import annotations

import html


def _bar(done: int, total: int, passed: int, partial: int, failed: int) -> str:
    if not total:
        return '<div class="bar"></div>'
    w = lambda n: f"{100.0 * n / total:.2f}%"  # noqa: E731
    return (f'<div class="bar" role="img" aria-label="{done} of {total} done">'
            f'<span class="p" style="width:{w(passed)}"></span>'
            f'<span class="h" style="width:{w(partial)}"></span>'
            f'<span class="f" style="width:{w(failed)}"></span></div>')


def _fmt_eta(s: float | None) -> str:
    if not s:
        return "-"
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def render(progress: dict, report: dict | None = None, title: str = "Gauntlet") -> str:
    e = html.escape
    layers = progress.get("layers", {})
    rows = []
    for name, lay in layers.items():
        rows.append(
            f"<tr><th scope='row'>{e(name)}</th><td>{e(str(lay.get('status', '')))}</td>"
            f"<td>{lay.get('done', 0)}/{lay.get('total', 0)}</td>"
            f"<td>{_bar(lay.get('done', 0), lay.get('total', 0), lay.get('pass', 0), lay.get('partial', 0), lay.get('fail', 0))}</td>"
            f"<td>{lay.get('rate_pct', '-')}</td><td>{lay.get('p0_pass', 0)}/{lay.get('p0_total', 0)}</td>"
            f"<td>{lay.get('silent', 0)}</td><td>{lay.get('crash', 0)}</td><td>{lay.get('hang', 0)}</td>"
            f"<td>{_fmt_eta(lay.get('eta_s'))}</td></tr>")
    silent = sum(v.get("silent", 0) for v in layers.values())
    crash = sum(v.get("crash", 0) for v in layers.values())
    p0p = sum(v.get("p0_pass", 0) for v in layers.values())
    p0t = sum(v.get("p0_total", 0) for v in layers.values())
    signoff = ""
    fails = ""
    if report:
        s8 = report["S8_signoff"]
        signoff = (f"<p class='verdict {'ok' if s8['release_bar_met'] else 'no'}'>Release bar: "
                   f"{'MET' if s8['release_bar_met'] else 'NOT MET'}</p>")
        items = "".join(f"<li><code>{e(f['id'])}</code> {e(f['outcome'])} "
                        f"<code class='repro'>{e(f['repro'])}</code></li>"
                        for f in report["S3_non_pass"][:200])
        fails = f"<h2>Non-pass ({len(report['S3_non_pass'])})</h2><ol>{items}</ol>"
    parked = progress.get("parked") or []
    parked_html = (f"<p>Parked (waiting on a usage limit): {len(parked)} cases</p>"
                   if parked else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a66;--line:#e2e0da;--ok:#2f7d4f;--warn:#b7791f;--bad:#b83a3a;--track:#ecebe6}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#34332f;--ok:#5bbf86;--warn:#e0a84a;--bad:#e46f6f;--track:#2a2a27}}}}
:root[data-theme="dark"]{{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#34332f;--ok:#5bbf86;--warn:#e0a84a;--bad:#e46f6f;--track:#2a2a27}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}
.meta{{color:var(--muted);font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:16px 0}}
.tile{{border:1px solid var(--line);border-radius:8px;padding:10px}}.tile b{{display:block;font-size:22px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
.bar{{display:flex;width:160px;height:10px;background:var(--track);border-radius:5px;overflow:hidden}}
.bar .p{{background:var(--ok)}}.bar .h{{background:var(--warn)}}.bar .f{{background:var(--bad)}}
.verdict.ok{{color:var(--ok);font-weight:600}}.verdict.no{{color:var(--bad);font-weight:600}}
code{{font-size:12px}}.repro{{color:var(--muted)}}ol{{padding-left:20px}}
</style></head><body><main>
<h1>{e(title)}</h1>
<p class="meta">Run {e(str(progress.get('stamp', '')))} · commit {e(str(progress.get('commit', '')))} · status {e(str(progress.get('status', '')))} · updated {e(str(progress.get('updated', '')))} · ETA {_fmt_eta(progress.get('eta_s'))}</p>
{signoff}
<div class="tiles"><div class="tile">Silent failures<b>{silent}</b></div><div class="tile">Crashes<b>{crash}</b></div><div class="tile">P0 pass<b>{p0p}/{p0t}</b></div><div class="tile">Current<b style="font-size:13px">{e(str(progress.get('current_case') or '-'))}</b></div></div>
{parked_html}
<div class="wrap"><table><thead><tr><th>Layer</th><th>Status</th><th>Done</th><th>Progress</th><th>Rate %</th><th>P0</th><th>Silent</th><th>Crash</th><th>Hang</th><th>ETA</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
{fails}
</main></body></html>
"""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_html.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/html.py gauntlet/tests/test_html.py
git commit -m "gauntlet: progress page renderer for the Gauntlet artifact (Task 27, spec 8)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 26: CLI `python -m gauntlet`

PARALLEL-OK. Depends on: Tasks 6, 7, 8, 27.

**Files:**
- Create: `gauntlet/cli.py`, `gauntlet/__main__.py`
- Test: `gauntlet/tests/test_cli.py`

**Interfaces:**
- Consumes: `load_adapter`, `write_generation`, `Runner`, `report.write`, `html.render`.
- Produces: `python -m gauntlet [--config gauntlet.json] [--adapter module:factory] [--runs-root DIR] generate [--stamp S] [--layer L ...]`, `run [--stamp S|latest] [--layer L ...] [--case ID ...] [--tier P0 ...] [--prefix P ...] [--where KEY=VALUE ...] [--resume] [--rerun] [--only-failed] [--wait-on-park]` (exit 75 when parked), `report --stamp S|latest`, `progress-html --stamp S|latest [--out PATH] [--title T]`. `cli.main(argv) -> int`, `EXIT_PARKED = 75`.

- [ ] **Step 1: Write the failing test**

```python
import json

from gauntlet.cli import EXIT_PARKED, main


def _args(tmp_path):
    return ["--adapter", "gauntlet.tests.toy:make", "--runs-root", str(tmp_path)]


def test_generate_run_report_html_end_to_end(tmp_path, capsys):
    assert main(_args(tmp_path) + ["generate", "--stamp", "s1"]) == 0
    assert (tmp_path / "s1" / "cases.jsonl").exists()
    assert main(_args(tmp_path) + ["run", "--stamp", "latest", "--resume"]) == 0
    assert main(_args(tmp_path) + ["report", "--stamp", "s1"]) == 0
    assert main(_args(tmp_path) + ["progress-html", "--stamp", "s1", "--title", "Toy"]) == 0
    text = (tmp_path / "s1" / "progress.html").read_text()
    assert "<title>Toy</title>" in text and "Release bar: MET" in text


def test_config_file_supplies_adapter(tmp_path, monkeypatch):
    cfg = tmp_path / "gauntlet.json"
    cfg.write_text(json.dumps({"adapter": "gauntlet.tests.toy:make",
                               "runs_root": str(tmp_path / "runs")}))
    assert main(["--config", str(cfg), "generate", "--stamp", "s"]) == 0


def test_single_case_rerun(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--case", "beta-1"]) == 0
    assert (tmp_path / "s" / "cases" / "beta-1.json").exists()
    assert not (tmp_path / "s" / "cases" / "beta-0.json").exists()


def test_parked_exit_code_constant():
    assert EXIT_PARKED == 75


def test_tier_flag(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--tier", "P0"]) == 0
    done = sorted(p.stem for p in (tmp_path / "s" / "cases").glob("*.json"))
    assert done == ["alpha-0", "beta-0"]


def test_prefix_and_where_filters(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--prefix", "beta-",
                                   "--where", "sleep_s=5"]) == 0
    done = sorted(p.stem for p in (tmp_path / "s" / "cases").glob("*.json"))
    assert done == ["beta-0", "beta-1"]


def test_rerun_runs_done_cases_again(tmp_path):
    main(_args(tmp_path) + ["generate", "--stamp", "s"])
    main(_args(tmp_path) + ["run", "--stamp", "s"])
    first = (tmp_path / "s" / "cases" / "alpha-0.json").stat().st_mtime_ns
    assert main(_args(tmp_path) + ["run", "--stamp", "s", "--tier", "P0", "--rerun"]) == 0
    assert (tmp_path / "s" / "cases" / "alpha-0.json").stat().st_mtime_ns > first
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest gauntlet/tests/test_cli.py -q`
Expected: `ModuleNotFoundError: No module named 'gauntlet.cli'`.

- [ ] **Step 3: Write the implementation**

`gauntlet/cli.py`:

```python
"""`python -m gauntlet` (spec 2.2/2.3/2.5/8).

  generate [--stamp S] [--layer L ...]
  run [--stamp S|latest] [--layer L ...] [--case ID ...] [--tier P0 ...] [--prefix P ...]
      [--where KEY=VALUE ...] [--resume] [--rerun] [--only-failed] [--wait-on-park]
  report --stamp S|latest
  progress-html --stamp S|latest [--out PATH] [--title T]

The adapter and runs root come from ./gauntlet.json ({"adapter":
"module:factory", "runs_root": "...", "title": "..."}) unless given as flags."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from gauntlet import html as html_page
from gauntlet import report as report_mod
from gauntlet.adapter import load_adapter
from gauntlet.generate import GenerationError, write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir, atomic_write_text, new_stamp

EXIT_PARKED = 75


def _config(args: argparse.Namespace) -> dict:
    cfg: dict = {}
    path = Path(args.config)
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    if args.adapter:
        cfg["adapter"] = args.adapter
    if args.runs_root:
        cfg["runs_root"] = args.runs_root
    if "adapter" not in cfg or "runs_root" not in cfg:
        raise SystemExit("gauntlet: need an adapter and a runs root (gauntlet.json or flags)")
    return cfg


def _resolve_stamp(runs_root: Path, stamp: str | None) -> str:
    if stamp and stamp != "latest":
        return stamp
    runs = sorted(p.name for p in Path(runs_root).expanduser().iterdir()
                  if p.is_dir() and (p / "cases.jsonl").exists()) if \
        Path(runs_root).expanduser().exists() else []
    if not runs:
        raise SystemExit("gauntlet: no runs yet; run `python -m gauntlet generate` first")
    return runs[-1]


def _where(pairs: list[str]) -> dict[str, set[str]] | None:
    out: dict[str, set[str]] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"gauntlet: --where needs KEY=VALUE, got {pair!r}")
        out.setdefault(key, set()).add(value)
    return out or None


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m gauntlet")
    p.add_argument("--config", default="gauntlet.json")
    p.add_argument("--adapter")
    p.add_argument("--runs-root")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--stamp")
    g.add_argument("--layer", action="append")
    r = sub.add_parser("run")
    r.add_argument("--stamp")
    r.add_argument("--layer", action="append")
    r.add_argument("--case", action="append")
    r.add_argument("--resume", action="store_true")
    r.add_argument("--only-failed", action="store_true")
    r.add_argument("--rerun", action="store_true",
                   help="run the selected cases again even when they already have a result")
    r.add_argument("--wait-on-park", action="store_true")
    r.add_argument("--tier", action="append", choices=("P0", "P1", "P2"),
                   help="only cases of this tier (repeatable), e.g. the P0 slice")
    r.add_argument("--prefix", action="append", default=[],
                   help="only case ids starting with this (repeatable)")
    r.add_argument("--where", action="append", default=[],
                   help="only cases whose meta KEY equals VALUE, as KEY=VALUE (repeatable; "
                   "several values for one key are OR-ed)")
    rp = sub.add_parser("report")
    rp.add_argument("--stamp")
    h = sub.add_parser("progress-html")
    h.add_argument("--stamp")
    h.add_argument("--out")
    h.add_argument("--title")
    args = p.parse_args(argv)
    cfg = _config(args)
    runs_root = Path(cfg["runs_root"]).expanduser()
    adapter = load_adapter(cfg["adapter"])

    if args.cmd == "generate":
        run = RunDir(runs_root, args.stamp or new_stamp())
        try:
            counts = write_generation(run, adapter, args.layer)
        except GenerationError as e:
            print(f"generation failed: {e}", file=sys.stderr)
            return 2
        print(f"{run.root}: {sum(c.got for c in counts)} cases")
        return 0

    stamp = _resolve_stamp(runs_root, args.stamp)
    run = RunDir(runs_root, stamp)
    if args.cmd == "run":
        if not run.cases_jsonl.exists():
            write_generation(run, adapter)
        summary = Runner(adapter, run, commit=_commit()).run_layers(
            args.layer, resume=not args.rerun and (args.resume or not args.case),
            only_failed=args.only_failed,
            case_ids=set(args.case) if args.case else None, wait_on_park=args.wait_on_park,
            tiers=set(args.tier) if args.tier else None, prefixes=tuple(args.prefix),
            where=_where(args.where))
        print(f"{run.root}: ran {summary.ran}, parked {len(summary.parked)}, "
              f"stopped layers {summary.stopped_layers or '-'}")
        return EXIT_PARKED if summary.parked else 0
    if args.cmd == "report":
        rep = report_mod.write(run, adapter, {"commit": _commit()})
        print(f"{run.report_md} (release bar met: {rep['S8_signoff']['release_bar_met']})")
        return 0
    if args.cmd == "progress-html":
        prog = json.loads(run.progress_json.read_text(encoding="utf-8"))
        rep = json.loads(run.report_json.read_text(encoding="utf-8")) \
            if run.report_json.exists() else None
        out = Path(args.out) if args.out else run.root / "progress.html"
        atomic_write_text(out, html_page.render(prog, rep, args.title or cfg.get("title",
                                                                                "Gauntlet")))
        print(out)
        return 0
    return 2
```

`gauntlet/__main__.py`:

```python
import sys

from gauntlet.cli import main

sys.exit(main())
```

- [ ] **Step 4: Run the whole engine suite**

Run: `.venv/bin/python -m pytest gauntlet/tests -q`
Expected: `120 passed`.

- [ ] **Step 5: Commit**

```bash
git add gauntlet/cli.py gauntlet/__main__.py gauntlet/tests/test_cli.py
git commit -m "gauntlet: python -m gauntlet generate/run/report/progress-html (Task 26, row 436)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 28: ScouterAdapter wiring and `gauntlet.json`

PARALLEL-OK. Depends on: Tasks 16-27. Step 5 is **LIVE** (controller).

**Files:**
- Create: `tests/gauntlet/adapter.py`, `gauntlet.json`
- Test: `tests/gauntlet/test_adapter.py`

**Interfaces:**
- Consumes: every layer generator and executor, `ScouterSession`, `AgentSession`, `OfflineSession`, `InstallSession`, `corpus`, `POLICY`.
- Produces: `tests.gauntlet.adapter.make() -> ScouterAdapter` (the engine's `Adapter`): `layers()` (10 `LayerSpec`s in run order: offline, coverage, scenarios, scenarios_agent, bulk, looks (with the 90%/95% check gates), robustness, parity, install, guide), `generate(layer)` (scenario cases sorted by body so sessions relaunch less), `open_session(layer, run)` (isolates the gauntlet process's own Scouter env, records the originals' sha256 once per run, writes the chart for looks, returns a `RoutedSession`), `crash_count()` / `crash_reports_since(t)` (darktable/python `.ips/.crash/.hang`), `signoff_lines()` (originals unchanged), `generation_notes()` (P0 class split, workflow step coverage), `write_tools()`, `policy()`; `isolate_process(root)`; `RoutedSession(adapter, layer, run)`.

`gauntlet.json` (repo root):

```json
{"adapter": "tests.gauntlet.adapter:make", "runs_root": "~/Pictures/ScouterScratch/gauntlet", "title": "Scouter Gauntlet"}
```

- [ ] **Step 1: Write the failing test**

```python
import pytest

from gauntlet.generate import generate_all
from tests.gauntlet.adapter import ScouterAdapter


def test_generation_passes_the_engine_gates(tmp_path):
    a = ScouterAdapter(tmp_path)
    cases, counts = generate_all(a)
    got = {c.spec.name: c.got for c in counts}
    assert got == {"offline": 2314, "coverage": 1316, "scenarios": 701, "scenarios_agent": 160,
                   "bulk": 40, "looks": 469, "robustness": 32, "parity": 90, "install": 10,
                   "guide": 35}
    assert sum(got.values()) == 5167


def test_notes_and_signoff_without_live_layers(tmp_path):
    a = ScouterAdapter(tmp_path)
    assert "P0 dry run: 100 requests" in a.generation_notes([])
    assert "not checked" in a.signoff_lines()[0]


def test_corpus_missing_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="corpus survey"):
        ScouterAdapter(tmp_path).corpus()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/gauntlet/test_adapter.py -q`
Expected: `ModuleNotFoundError: No module named 'tests.gauntlet.adapter'`.

- [ ] **Step 3: Write the implementation**

```python
"""Scouter's gauntlet adapter: the one object `python -m gauntlet` loads
(gauntlet.json -> tests.gauntlet.adapter:make). Everything Scouter-specific
the engine needs is here or imported from here."""

from __future__ import annotations

import json
import os
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
LOOK_GATES = (("confidence", ("documented", "community-consensus"), 0.90),
              ("confidence", ("inferred",), 0.95))
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


class RoutedSession:
    """One ScouterSession per body, opened lazily; cases dispatch by layer."""

    def __init__(self, adapter: ScouterAdapter, layer: str, run) -> None:
        self.adapter = adapter
        self.layer = layer
        self.run = run
        self.current = None
        self.n = 0

    def _session(self, body: str):
        from tests.gauntlet.observe import noise_floors
        from tests.gauntlet.session import ScouterSession

        if self.current is not None and self.current.body == body:
            return self.current
        if self.current is not None:
            self.current.close()
        self.n += 1
        root = self.run.sessions_dir / self.layer / f"{self.n:03d}-{body}"
        self.current = ScouterSession(self.adapter.corpus(), root, body, run_root=self.run.root,
                                      floors_fn=lambda f: noise_floors(f, root / "floor"))
        self.current.open()
        return self.current

    def run_case(self, case: Case):
        from gauntlet.adapter import Observation
        from tests.gauntlet import exec_bulk, faults
        from tests.gauntlet.agent import AgentSession
        from tests.gauntlet.execute import run_tool_case
        from tests.gauntlet.session import SessionBlocked

        try:
            s = self._session(case.body if case.body not in (None, "chart") else "olympus")
        except SessionBlocked as e:
            self.current = None
            return Observation(blocked=f"launch: {e}")
        if self.layer == "bulk":
            return exec_bulk.run_bulk_case(s, case)
        if self.layer == "robustness":
            return faults.run_fault_case(s, case)
        if self.layer == "scenarios_agent":
            return AgentSession(s).run_case(case)
        return run_tool_case(s, case)

    def abort(self) -> None:
        if self.current is not None:
            self.current.abort()
            self.current = None

    def close(self) -> None:
        if self.current is not None:
            self.current.close()
            self.current = None


class ScouterAdapter:
    name = "scouter"

    def __init__(self, runs_root: Path = RUNS_ROOT) -> None:
        self.runs_root = runs_root
        self._tax = None
        self._corpus = None
        self._originals_before: dict | None = None

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
            LayerSpec("looks", 5, looks.EXPECTED, True, 0.95, "Looks", LOOK_GATES),
            LayerSpec("robustness", 6, robustness.EXPECTED, True, 0.95, "Robustness"),
            LayerSpec("parity", 7, parity.EXPECTED, True, 0.95, "MCP/CLI parity"),
            LayerSpec("install", 8, install.EXPECTED, True, 0.95, "Install rehearsal"),
            LayerSpec("guide", 9, guide.EXPECTED, True, 0.95, "Guide + learning mode"),
        ]

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
            return looks.generate(t)
        if layer == "robustness":
            return robustness.generate(t)
        if layer == "parity":
            return parity.generate()
        if layer == "install":
            return install.generate(t)
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
        isolate_process(self.runs_root / "_process")
        self._record_originals(run.root)
        if layer == "install":
            return InstallSession(run.root / "install")
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
```

- [ ] **Step 4: Run the whole adapter suite and the generator end to end**

Run: `.venv/bin/python -m pytest tests/gauntlet -q && .venv/bin/python -m gauntlet --runs-root "$(mktemp -d)" generate --stamp dry`
Expected: all adapter tests pass (54 in the new gauntlet files plus the moved benchmark suite), then `<tmp>/dry: 5167 cases`.

- [ ] **Step 5 (LIVE, controller): smoke run**

With darktable closed and after Task 12 Step 6 (corpus.json exists):

```bash
.venv/bin/python -m gauntlet generate --stamp smoke-$(date +%Y%m%d)
.venv/bin/python -m gauntlet run --stamp latest --case cov-exposure.exposure-olympus-set_mid --case cov-phrase-warmer-olympus-bare --case scn-r0001-olympus-tool --case gd-00-readme_open --case off-req-r0001
.venv/bin/python -m gauntlet report --stamp latest
```

Expected: five result files; `log.md` shows each outcome; no `harness_error`; darktable quit at the end (`pgrep -x darktable` prints nothing). A `harness_error` or `blocked: launch` is a harness bug: fix it before Task 35.

- [ ] **Step 6: Commit**

```bash
git add tests/gauntlet/adapter.py tests/gauntlet/test_adapter.py gauntlet.json
git commit -m "gauntlet(scouter): the Scouter adapter and gauntlet.json (Task 28, row 436)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part 5: Watchdog product change (spec 6, "now")

### Task 29: Typed `darktable_unresponsive`, doctor watchdog lines, `doctor --ping`

Not parallel (shares `scouter/core/dtbridge.py`, `scouter/errors.py`, `scouter/cli/main.py` with Tasks 11, 30, 32-34). Depends on: Task 11.

**Files:**
- Modify: `scouter/errors.py` (class + exit code 24 + `ERROR_CLASSES`), `scouter/core/dtbridge.py` (`call()` pings after a timeout; `_unresponsive()`), `scouter/cli/main.py` (`--ping`, `_doctor_watchdog`), `tests/test_paths_errors.py:340-358` (the pinned code set gains `darktable_unresponsive`)
- Test: `tests/test_watchdog_unresponsive.py`

**Interfaces:**
- Produces: `scouter.errors.DarktableUnresponsive` (`code="darktable_unresponsive"`, exit 24, detail `pid, launched_by_scouter, last_method, ping_ms`, later `heals_used/heals_max`), raised by `dtbridge.call` only when the call timed out AND `health()` answered `unresponsive` (diag itself never pings); `scouter doctor` report keys `watchdog`, `watchdog_line`, `gui_ready`, `gui_ready_line`, `darktable_pid_line`; `report["ok"]` is false when the ping is unresponsive; `scouter doctor --ping` prints `{"verb": "doctor", "watchdog": state, "ping_ms": n, "ok": bool}` and exits 0 (alive/starting) or 1 (unresponsive).

- [ ] **Step 1: Write the failing test** (`tests/test_watchdog_unresponsive.py`)

```python
"""Watchdog design 4: typed unresponsive only when the call timed out AND the ping failed."""

import json
import unittest
from unittest import mock

from darktable_mcp.bridge.client import BridgeTimeoutError
from scouter.core import dtbridge
from scouter.errors import EXIT_CODES, BridgeTimeout, DarktableUnresponsive
from tests.scouter_fakes import FakeBridge


class UnresponsiveTest(unittest.TestCase):
    def tearDown(self) -> None:
        dtbridge.set_bridge(None)

    def test_timeout_plus_dead_ping_is_typed_unresponsive(self) -> None:
        dtbridge.set_bridge(FakeBridge(raises={"view.current": BridgeTimeoutError("t"),
                                               "darktable_mcp.diag": BridgeTimeoutError("t")}))
        with self.assertRaises(DarktableUnresponsive) as cm:
            dtbridge.call("view.current")
        self.assertEqual(cm.exception.detail["last_method"], "view.current")
        self.assertEqual(EXIT_CODES["darktable_unresponsive"], 24)

    def test_timeout_with_live_ping_stays_bridge_timeout(self) -> None:
        dtbridge.set_bridge(FakeBridge(raises={"view.current": BridgeTimeoutError("t")},
                                       responses={"darktable_mcp.diag": {"gui_ready": True}}))
        with self.assertRaises(BridgeTimeout):
            dtbridge.call("view.current")

    def test_diag_timeout_never_pings_itself(self) -> None:
        fake = FakeBridge(raises={"darktable_mcp.diag": BridgeTimeoutError("t")})
        dtbridge.set_bridge(fake)
        with self.assertRaises(BridgeTimeout):
            dtbridge.call("darktable_mcp.diag")
        self.assertEqual([m for m, _ in fake.calls], ["darktable_mcp.diag"])

    def test_doctor_ping_exit_codes(self) -> None:
        from scouter.cli import main as cli

        for state, code in (("alive", 0), ("starting", 0), ("unresponsive", 1)):
            with mock.patch.object(dtbridge, "health", return_value={"state": state,
                                                                     "ping_ms": 3}), \
                    mock.patch("builtins.print") as out:
                self.assertEqual(cli.main(["doctor", "--ping"]), code)
            self.assertEqual(json.loads(out.call_args[0][0])["watchdog"], state)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_watchdog_unresponsive.py -q`
Expected: `ImportError: cannot import name 'DarktableUnresponsive' from 'scouter.errors'`.

- [ ] **Step 3: Write the implementation** (apply these diffs)

`scouter/errors.py`:

```diff
--- a/scouter/errors.py
+++ b/scouter/errors.py
@@ -237,6 +237,16 @@
     )
 
 
+class DarktableUnresponsive(ScouterError):
+    """Watchdog design 2.2: a call timed out AND the 5 s health ping failed."""
+
+    code = "darktable_unresponsive"
+    fix = (
+        "darktable stopped answering (Lua worker or GTK main loop wedged); quit it "
+        "(Force Quit if needed) and relaunch, or enable `watchdog.auto_heal`."
+    )
+
+
 #: Process exit codes. 0/1/2 follow shell convention; the rest are one per error
 #: class, so a shell script can branch on the failure without parsing JSON.
 EXIT_CODES: dict[str, int] = {
@@ -276,6 +286,8 @@
     # originally claimed 21; renumbered to the next free value after
     # "state_locked" took 22.
     "noop": 23,
+    # Watchdog (spec 6): a timed-out call whose health ping also failed.
+    "darktable_unresponsive": 24,
 }
 
 ERROR_CLASSES: tuple[type[ScouterError], ...] = (
@@ -297,6 +309,7 @@
     ValueUnknown,
     NotPinned,
     StateLocked,
+    DarktableUnresponsive,
 )
 
 
```

`scouter/core/dtbridge.py`:

```diff
--- a/scouter/core/dtbridge.py
+++ b/scouter/core/dtbridge.py
@@ -197,8 +197,14 @@
     """One bridge call, with the bridge's own failures mapped to error classes."""
     try:
         return get_bridge().call(method, params or {}, timeout=timeout)
-    except (BridgeTimeoutError, BridgePluginNotInstalledError) as e:
+    except BridgeTimeoutError as e:
+        if method != "darktable_mcp.diag":  # never ping from inside the ping
+            h = health()
+            if h["state"] == "unresponsive":
+                raise _unresponsive(method, h) from e
         raise BridgeTimeout(str(e), detail={"method": method}) from e
+    except BridgePluginNotInstalledError as e:
+        raise BridgeTimeout(str(e), detail={"method": method}) from e
     except BridgeError as e:
         message = str(e)
         if _WRONG_IMAGE_RE.search(message):
@@ -235,8 +241,30 @@
     return {"state": "starting" if ready is False else "alive",
             "ping_ms": int(1000 * (time.monotonic() - t0)), "gui_ready": ready,
             "gui_ready_source": d.get("gui_ready_source")}
+
+
+def _unresponsive(method: str, h: dict):
+    from scouter.errors import DarktableUnresponsive
+
+    pid, ours = None, False
+    try:
+        from scouter.core import dtprocess
 
+        pid = dtprocess.owned_pid()
+        ours = pid is not None
+        if pid is None:
+            running = dtprocess.running_pids()
+            pid = running[0] if running else None
+    except Exception:  # noqa: BLE001 - detail is best effort
+        pass
+    return DarktableUnresponsive(
+        f"darktable did not answer {method} and the {HEALTH_TIMEOUT_S:g} s health ping "
+        "failed too",
+        detail={"pid": pid, "launched_by_scouter": ours, "last_method": method,
+                "ping_ms": h.get("ping_ms")},
+    )
 
+
 def current_view() -> str:
     result = call("view.current")
     return str(result.get("view", "unknown"))
```

`scouter/cli/main.py`:

```diff
--- a/scouter/cli/main.py
+++ b/scouter/cli/main.py
@@ -163,6 +163,12 @@
         help="with --probe-main-thread: also do a darkroom -> lighttable -> darkroom "
         "round trip through darktable's switch-views commands (start in the darkroom)",
     )
+    doctor.add_argument(
+        "--ping",
+        dest="ping",
+        action="store_true",
+        help="print only the watchdog line; exit 0 when darktable answers, 1 when not",
+    )
     doctor.set_defaults(_verb=None, _args=[])
 
     setup_parser = groups.add_parser(
@@ -282,11 +288,30 @@
     # the BRIDGE. A config that has never been written is the correct state
     # for a fresh install, and `doctor` exiting non-zero for it would make
     # the first thing a new user runs look broken.
+    _doctor_watchdog(report)
     _doctor_config(report)
-    report["ok"] = report["bridge"] == "ok"
+    report["ok"] = report["bridge"] == "ok" and report.get("watchdog") != "unresponsive"
     return report
 
 
+def _doctor_watchdog(report: dict) -> None:
+    """Watchdog design 2.5: ping state, gui_ready, the darktable pid and who
+    launched it. Report lines only."""
+    from scouter.core import dtbridge, dtprocess
+
+    h = dtbridge.health()
+    report["watchdog"] = h["state"]
+    report["watchdog_line"] = f"watchdog: ping {h['state']} ({h.get('ping_ms')} ms)"
+    report["gui_ready"] = h.get("gui_ready")
+    report["gui_ready_line"] = f"gui_ready: {str(h.get('gui_ready')).lower()} " \
+        f"({h.get('gui_ready_source') or '-'})"
+    owned = dtprocess.owned_pid()
+    running = dtprocess.running_pids()
+    pid = owned or (running[0] if running else None)
+    who = "launched by scouter" if owned else ("user" if pid else "-")
+    report["darktable_pid_line"] = f"darktable pid: {pid or '-'} ({who})"
+
+
 def _doctor_config(report: dict) -> None:
     """The config/backup/encryption/library half of `doctor`, in its own
     function so `_doctor`'s bridge half stays readable.
@@ -398,6 +423,14 @@
         print(render_probe_table(probe))
         return 0
 
+    if namespace.group == "doctor" and getattr(namespace, "ping", False):
+        from scouter.core import dtbridge
+
+        h = dtbridge.health()
+        return emit({"verb": "doctor", "watchdog": h["state"], "ping_ms": h.get("ping_ms"),
+                     "ok": h["state"] != "unresponsive"},
+                    0 if h["state"] != "unresponsive" else 1)
+
     if namespace.group == "doctor":
         report = _doctor()
         if report["ok"]:
```

`tests/test_paths_errors.py`: add `"darktable_unresponsive",` to the `expected` set in `test_every_spec_error_class_exists_with_a_unique_code`.

- [ ] **Step 4: Run the new test and the whole unit suite**

Run: `.venv/bin/python -m pytest tests/test_watchdog_unresponsive.py -q && .venv/bin/python -m pytest tests -q -k "not host"`
Expected: `4 passed`, then the full suite green (it was checked while writing this plan: only the pinned error-code test needs the one-line update above).

- [ ] **Step 5: Commit**

```bash
git add scouter/errors.py scouter/core/dtbridge.py scouter/cli/main.py tests/test_paths_errors.py tests/test_watchdog_unresponsive.py
git commit -m "scouter: typed darktable_unresponsive after a failed health ping; doctor watchdog lines and --ping (Task 29, spec 6)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 30: Auto-heal (opt-in), `doctor --heal`, telemetry

Not parallel. Depends on: Task 29.

**Files:**
- Create: `scouter/core/watchdog.py`
- Modify: `scouter/config.py` (`WatchdogConfig`, section `watchdog`), `scouter/registry.py` (`call()` wraps the verb in `watchdog.run_with_heal`; `data["healed"] = True` on a healed call), `scouter/cli/main.py` (`--heal`, the `auto_heal` doctor line)
- Test: `tests/test_watchdog_heal.py`

**Interfaces:**
- Produces: `WatchdogConfig(auto_heal=False, max_heals_per_job=1, heal_budget_s=60.0)` read from `config.json` `"watchdog": {...}`; `watchdog.NO_RETRY = {"library.import", "job.run"}`, `heals_used(job)`, `telemetry(job, verb, outcome, ms, detail)` (one `metrics.emit` event + audit line, `verb="watchdog.heal"`, outcomes `healed|heal_failed|refused_not_ours`), `heal_darktable(job, budget_s) -> {"outcome", "ms", "pid_old", "pid_new"}` (owned pid only; terminate without osascript; clear stale locks; relaunch with the owner record's library/configdir/frame; `wait_gui_ready(30)`; re-open the job's `opened_path`), `run_with_heal(verb_id, fn, job, *, cfg=None, heal=heal_darktable) -> (result, healed)`.

- [ ] **Step 1: Write the failing test**

```python
"""Auto-heal (spec 6 item 4) with a fake heal: no darktable."""

import os
import unittest

from scouter.config import WatchdogConfig
from scouter.core import watchdog
from scouter.errors import DarktableUnresponsive
from tests.scouter_fakes import TempCache


class HealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = TempCache()
        self.cache.start()
        self.heals = []

    def tearDown(self) -> None:
        self.cache.stop()

    def _heal(self, outcome="healed"):
        def heal(job, budget):
            self.heals.append(job)
            return {"outcome": outcome, "ms": 5, "pid_old": 1, "pid_new": 2}
        return heal

    def _flaky(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise DarktableUnresponsive("wedged", detail={})
            return "ok"
        return fn, calls

    def test_off_by_default_raises_with_counters(self) -> None:
        fn, _ = self._flaky()
        with self.assertRaises(DarktableUnresponsive) as cm:
            watchdog.run_with_heal("darkroom.say", fn, "j1", cfg=WatchdogConfig(),
                                   heal=self._heal())
        self.assertEqual(cm.exception.detail["heals_max"], 1)
        self.assertEqual(self.heals, [])

    def test_one_heal_one_retry_and_telemetry(self) -> None:
        fn, calls = self._flaky()
        res, healed = watchdog.run_with_heal("darkroom.say", fn, "j1",
                                             cfg=WatchdogConfig(auto_heal=True),
                                             heal=self._heal())
        self.assertEqual((res, healed, len(calls)), ("ok", True, 2))
        self.assertEqual(watchdog.heals_used("j1"), 1)
        events = self.cache.events()
        self.assertEqual(events[-1]["verb"], "watchdog.heal")
        self.assertEqual(events[-1]["outcome"], "healed")

    def test_cap_reached_raises_typed(self) -> None:
        cfg = WatchdogConfig(auto_heal=True, max_heals_per_job=1)
        fn, _ = self._flaky()
        watchdog.run_with_heal("darkroom.say", fn, "j1", cfg=cfg, heal=self._heal())
        fn2, _ = self._flaky()
        with self.assertRaises(DarktableUnresponsive) as cm:
            watchdog.run_with_heal("darkroom.say", fn2, "j1", cfg=cfg, heal=self._heal())
        self.assertEqual(cm.exception.detail["heals_used"], 1)
        self.assertEqual(len(self.heals), 1)

    def test_job_run_and_import_are_never_retried(self) -> None:
        for verb in ("job.run", "library.import"):
            fn, calls = self._flaky()
            with self.assertRaises(DarktableUnresponsive) as cm:
                watchdog.run_with_heal(verb, fn, f"j-{verb}", cfg=WatchdogConfig(auto_heal=True),
                                       heal=self._heal())
            self.assertTrue(cm.exception.detail["healed"])
            self.assertEqual(len(calls), 1)

    def test_foreign_darktable_is_refused_not_healed(self) -> None:
        fn, calls = self._flaky()
        with self.assertRaises(DarktableUnresponsive):
            watchdog.run_with_heal("darkroom.say", fn, "j2", cfg=WatchdogConfig(auto_heal=True),
                                   heal=self._heal("refused_not_ours"))
        self.assertEqual(self.cache.events()[-1]["outcome"], "refused_not_ours")
        self.assertEqual(len(calls), 1)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_watchdog_heal.py -q`
Expected: `ImportError: cannot import name 'WatchdogConfig' from 'scouter.config'`.

- [ ] **Step 3: Write the implementation**

`scouter/core/watchdog.py`:

```python
"""Opt-in auto-heal (spec 6 item 4, watchdog design 2.3).

When a verb raises DarktableUnresponsive and `watchdog.auto_heal` is on, and
this job has heals left: quit the OWNED darktable (SIGTERM, then SIGKILL),
clear its stale locks, relaunch it with the same --library/--configdir,
wait for gui_ready (30 s), re-open the job's image, and retry the verb ONCE.
The retry is idempotent through the journal readback. `library.import` and
`job.run` are never retried: they report the heal and the caller re-runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from scouter import metrics, paths
from scouter.errors import DarktableUnresponsive

NO_RETRY = frozenset({"library.import", "job.run"})
STATE_KEY = "watchdog_heals"


def heals_used(job: str) -> int:
    return int(paths.job_state_read(job).get(STATE_KEY, 0))


def _bump(job: str) -> None:
    def mutate(state: dict) -> None:
        state[STATE_KEY] = int(state.get(STATE_KEY, 0)) + 1
        state["watchdog_last_heal"] = metrics._utc_now_z()

    paths.job_state_update(job, mutate)


def telemetry(job: str, verb: str, outcome: str, ms: int, detail: dict) -> None:
    from scouter import PHASE, __version__

    event = {"ts": metrics._utc_now_z(), "job": job, "verb": verb, "layer": "watchdog",
             "args_digest": metrics.args_digest(detail), "images": [], "ms": int(ms),
             "outcome": outcome, "bytes_in": 0, "bytes_out": 0, "est_tokens": 0,
             "phase": PHASE, "version": __version__}
    metrics.emit(event)
    metrics.audit(job, {**event, "detail": detail})


def heal_darktable(job: str, budget_s: float) -> dict:
    """Relaunch the owned darktable. Never touches a darktable Scouter did not launch."""
    from scouter.core import dtbridge, dtprocess

    t0 = time.monotonic()
    rec = dtprocess.read_owner()
    pid = dtprocess.owned_pid()
    if rec is None or pid is None:
        return {"outcome": "refused_not_ours", "ms": 0, "pid_old": None, "pid_new": None}
    dtprocess.terminate(pid, responsive=False)
    dtprocess.clear_stale_locks(rec.library)
    new = dtprocess.launch(rec.frame, rec.library, rec.configdir)
    ready = dtprocess.wait_gui_ready(min(30.0, budget_s))
    out = {"pid_old": pid, "pid_new": new.pid}
    if not ready.get("ready"):
        return {"outcome": "heal_failed", "ms": int(1000 * (time.monotonic() - t0)), **out}
    opened = paths.job_state_read(job).get("opened_path")
    if opened:
        dtbridge.call("darkroom.open", {"path": opened}, timeout=30.0)
    return {"outcome": "healed", "ms": int(1000 * (time.monotonic() - t0)), **out}


def run_with_heal(verb_id: str, fn: Callable[[], Any], job: str, *, cfg=None,
                  heal: Callable[[str, float], dict] = heal_darktable) -> tuple[Any, bool]:
    """(result, healed). Raises DarktableUnresponsive when no heal applies."""
    try:
        return fn(), False
    except DarktableUnresponsive as e:
        if cfg is None:
            from scouter import config

            try:
                cfg = config.load().watchdog
            except Exception:  # noqa: BLE001 - a broken config never enables a heal
                raise e from None
        used = heals_used(job)
        e.detail.update(heals_used=used, heals_max=cfg.max_heals_per_job)
        if not cfg.auto_heal or used >= cfg.max_heals_per_job:
            raise
        result = heal(job, cfg.heal_budget_s)
        telemetry(job, "watchdog.heal", result["outcome"], result.get("ms", 0), result)
        if result["outcome"] != "healed":
            raise
        _bump(job)
        if verb_id in NO_RETRY:
            raise DarktableUnresponsive(
                f"darktable was healed (pid {result['pid_old']} -> {result['pid_new']}); "
                f"{verb_id} is never retried automatically: re-run it",
                detail={**e.detail, "healed": True, "heals_used": used + 1},
            ) from e
        return fn(), True
```

`scouter/config.py`:

```diff
--- a/scouter/config.py
+++ b/scouter/config.py
@@ -125,6 +125,15 @@
 
 
 @dataclass(frozen=True)
+class WatchdogConfig:
+    """Spec 6 / watchdog design 2.3. Off by default and off in the gauntlet."""
+
+    auto_heal: bool = False
+    max_heals_per_job: int = 1
+    heal_budget_s: float = 60.0
+
+
+@dataclass(frozen=True)
 class Config:
     version: int = CONFIG_VERSION
     library_root: str | None = None
@@ -141,6 +150,7 @@
     stats: StatsConfig = field(default_factory=StatsConfig)
     educative: str = "brief"
     cache: CacheConfig = field(default_factory=CacheConfig)
+    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
 
     def camera_map(self) -> dict[str, str]:
         return dict(self.cameras)
@@ -164,6 +174,7 @@
     "export": ("export", ExportConfig),
     "stats": ("stats", StatsConfig),
     "cache": ("cache", CacheConfig),
+    "watchdog": ("watchdog", WatchdogConfig),
 }
 TOP_KEYS = {"version", "library_root", "cameras", "educative", "_comment", *SECTIONS}
 
```

`scouter/registry.py`:

```diff
--- a/scouter/registry.py
+++ b/scouter/registry.py
@@ -139,9 +139,12 @@
     job = validate_job(kwargs["job"]) if kwargs.get("job") else current_job()
     kwargs["job"] = job
 
+    from scouter.core import watchdog
+
     token = metrics.LAYER.set(layer)
+    healed = False
     try:
-        result = verb.fn(**kwargs)
+        result, healed = watchdog.run_with_heal(verb_id, lambda: verb.fn(**kwargs), job)
     except ScouterError as e:
         return Envelope(
             verb=verb_id,
@@ -157,6 +160,8 @@
         metrics.LAYER.reset(token)
 
     data = result.to_dict()
+    if healed:
+        data["healed"] = True  # spec 6: always reported; a heal is never silent
     outcome = data.get("outcome", "ok")
     # A verb's own result can carry outcome "dry_run" (ConfirmationRequired) as
     # well as "ok"; only those two count as success, everything else — an
```

`scouter/cli/main.py`: add the flag after `--ping`

```python
    doctor.add_argument(
        "--heal",
        dest="heal",
        action="store_true",
        help="quit and relaunch the darktable Scouter launched (never a user's), then wait "
        "for gui_ready; exits 0 when healed",
    )
```

and, in `main()` right after the `--ping` branch:

```python
    if namespace.group == "doctor" and getattr(namespace, "heal", False):
        from scouter.core import watchdog
        from scouter.paths import current_job

        res = watchdog.heal_darktable(current_job(), 60.0)
        watchdog.telemetry(current_job(), "watchdog.heal", res["outcome"], res.get("ms", 0), res)
        return emit({"verb": "doctor", **res, "ok": res["outcome"] == "healed"},
                    0 if res["outcome"] == "healed" else 1)
```

and at the end of `_doctor_watchdog(report)`:

```python
    from scouter import config
    from scouter.core import watchdog
    from scouter.paths import current_job

    try:
        wd = config.load().watchdog
    except ScouterError:
        wd = config.WatchdogConfig()
    used = watchdog.heals_used(current_job())
    report["auto_heal_line"] = (f"auto_heal: {'on' if wd.auto_heal else 'off'}, heals this job: "
                                f"{used}/{wd.max_heals_per_job}")
```

- [ ] **Step 4: Run the new test and the whole unit suite**

Run: `.venv/bin/python -m pytest tests/test_watchdog_heal.py -q && .venv/bin/python -m pytest tests -q -k "not host"`
Expected: `5 passed`, then the full suite green.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/watchdog.py scouter/config.py scouter/registry.py scouter/cli/main.py tests/test_watchdog_heal.py
git commit -m "scouter: opt-in auto-heal (one heal, one idempotent retry), doctor --heal and heal telemetry (Task 30, spec 6)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part 6: W4 fix

### Task 31: Tint-aware Kelvin readback ("match daylight" read back 5934 K for 5500 K)

PARALLEL-OK (only `scouter/core/wb.py` and one new test). Depends on: nothing.

Evidence: benchmark run 3 (`tests/gauntlet/benchmark/results/20260926t100133z-olympus.json`, task W4): `darkroom_set temperature 5500` committed coefficients r=2.4488, g=1, b=1.8785; `wb_kelvin` answered 5934.29 K (bracket 5500-6000, `reported_ok_unverified`). Root cause: the table (`em10markiii.json`) was measured at `tint: 1.0`, and `_invert` bisects on r/b alone. r/b is tint-dependent, and the frame's as-shot tint is not 1.0: the observed g-normalised (r, b) sits about 14% off the table's curve at the inverted Kelvin. The honest answer is "Kelvin unreadable at this tint", not a confident 5934.

Fix: measure the distance between the observed g-normalised (r, b) and the table's (r, b) interpolated at the inverted Kelvin. The `wb.kelvin` verb (the readback the user sees) is strict: beyond 3% it answers `kelvin: None` with a reason naming the tint and `scouter wb calibrate`. Internal callers (relative moves, undo, style) keep the r/b answer so warmer/cooler still work at any tint, with `approximate: true` in the info.

**Files:**
- Modify: `scouter/core/wb.py` (`_OFF_CURVE_TOLERANCE`, `_off_curve`, `_invert(samples, ratio, coeffs=None, *, strict=False)`, `_invert_ratio` (the old body), `kelvin(..., strict=False)`, `kelvin_verb` uses `strict=True`)
- Test: `tests/test_wb_w4.py`

**Interfaces:**
- Produces: `wb._invert(..., strict=True)` -> `(None, {..., "reason": "...tint...calibrate...", "off_curve": d})` off the curve; `wb.kelvin(coeffs, camera, *, image=None, strict=False)`; result info gains `off_curve` and, when approximate, `approximate: True`.

- [ ] **Step 1: Write the failing test**

```python
"""W4 (row 437): 'match daylight' read back 5934 K for a 5500 K set on
olympus (bench run 3, 20260926t100133z). Numbers below are that run's table
(bench-config/wb/em10markiii.json, 5000-6500 K) and its after-set coeffs."""

import unittest

from scouter.core import wb

TABLE = [
    {"kelvin": 5000, "coeffs": [1.9646459817886353, 1.0, 1.8529057502746582]},
    {"kelvin": 5500, "coeffs": [2.068570375442505, 1.0, 1.729513168334961]},
    {"kelvin": 6000, "coeffs": [2.1569316387176514, 1.0, 1.6341935396194458]},
    {"kelvin": 6500, "coeffs": [2.23234486579895, 1.0, 1.5590273141860962]},
]
W4_AFTER = [2.4488253593444824, 1.0, 1.8785064220428467]


class W4OffCurveTest(unittest.TestCase):
    def test_w4_coefficients_are_not_read_as_5934_k(self) -> None:
        k, info = wb._invert(TABLE, W4_AFTER[0] / W4_AFTER[2], W4_AFTER, strict=True)
        self.assertIsNone(k)
        self.assertIn("tint", info["reason"])
        self.assertIn("calibrate", info["reason"])
        self.assertGreater(info["off_curve"], 0.03)

    def test_internal_callers_get_an_approximate_answer(self) -> None:
        k, info = wb._invert(TABLE, W4_AFTER[0] / W4_AFTER[2], W4_AFTER)
        self.assertAlmostEqual(k, 5934, delta=5)
        self.assertTrue(info["approximate"])

    def test_the_verb_is_strict(self) -> None:
        import inspect

        self.assertIn("strict=True", inspect.getsource(wb.kelvin_verb))

    def test_on_curve_coefficients_still_invert(self) -> None:
        c = TABLE[1]["coeffs"]
        k, info = wb._invert(TABLE, c[0] / c[2], c, strict=True)
        self.assertEqual(k, 5500)
        mid = [(a + b) / 2 for a, b in zip(TABLE[1]["coeffs"], TABLE[2]["coeffs"])]
        k, info = wb._invert(TABLE, mid[0] / mid[2], mid, strict=True)
        self.assertAlmostEqual(k, 5750, delta=40)
        self.assertLess(info["off_curve"], 0.03)

    def test_scaled_green_is_the_same_point(self) -> None:
        c = [x * 1.7 for x in TABLE[2]["coeffs"]]
        k, _ = wb._invert(TABLE, c[0] / c[2], c, strict=True)
        self.assertEqual(k, 6000)

    def test_ratio_only_callers_keep_the_old_behaviour(self) -> None:
        c = TABLE[1]["coeffs"]
        k, info = wb._invert(TABLE, c[0] / c[2])
        self.assertEqual(k, 5500)
        self.assertIsNone(info["off_curve"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wb_w4.py -q`
Expected: `TypeError: _invert() takes 2 positional arguments but 3 were given` (and the strict keyword errors).

- [ ] **Step 3: Write the implementation** (apply this diff)

```diff
--- a/scouter/core/wb.py
+++ b/scouter/core/wb.py
@@ -104,6 +104,12 @@
 #: How close `target` has to be to a sample's own ratio to call it `exact`
 #: rather than interpolated.
 _EXACT_TOLERANCE = 0.01
+#: W4 (row 437): the table is measured at one tint (the file's `tint`, 1.0).
+#: r/b alone is tint-dependent, so a frame at another tint inverts to a wrong
+#: Kelvin (5934 K read back for a 5500 K set). When the observed g-normalised
+#: (r, b) sits farther than this from the table's own curve at the inverted
+#: Kelvin, the Kelvin is not readable: answer None with a reason instead.
+_OFF_CURVE_TOLERANCE = 0.03
 
 #: `calibrate`'s own measured cost (task 11 host runs): about how long one
 #: Kelvin point (a `darkroom.set` + commit + XMP readback) takes.
@@ -288,7 +294,51 @@
     return float(coeffs[0]) / b
 
 
-def _invert(samples: list[dict], ratio: float) -> tuple[float | None, dict]:
+def _off_curve(samples: list[dict], k: float, coeffs: list[float] | None) -> float | None:
+    """Relative distance between the observed g-normalised (r, b) and the
+    table's (r, b) interpolated at Kelvin `k`. None when coeffs are absent."""
+    if not coeffs or len(coeffs) < 3 or not coeffs[1]:
+        return None
+    r_obs, b_obs = coeffs[0] / coeffs[1], coeffs[2] / coeffs[1]
+    ks = [p["kelvin"] for p in samples]
+    i = max(0, min(len(ks) - 2, next((j for j in range(len(ks) - 1) if ks[j] <= k <= ks[j + 1]),
+                                    len(ks) - 2)))
+    lo, hi = samples[i], samples[i + 1]
+    f = 0.0 if hi["kelvin"] == lo["kelvin"] else (k - lo["kelvin"]) / (hi["kelvin"] - lo["kelvin"])
+
+    def norm(p: dict) -> tuple[float, float]:
+        c = p["coeffs"]
+        return c[0] / c[1], c[2] / c[1]
+
+    (rl, bl), (rh, bh) = norm(lo), norm(hi)
+    r_t, b_t = rl + f * (rh - rl), bl + f * (bh - bl)
+    return ((r_obs - r_t) ** 2 + (b_obs - b_t) ** 2) ** 0.5 / (r_t ** 2 + b_t ** 2) ** 0.5
+
+
+def _invert(samples: list[dict], ratio: float, coeffs: list[float] | None = None,
+            *, strict: bool = False) -> tuple[float | None, dict]:
+    """`strict=True` (the `wb.kelvin` verb's readback) answers None with a
+    reason when the coefficients are off the calibrated curve; internal
+    callers (relative moves, undo, style) keep the r/b answer and get an
+    `approximate` warning, so a warmer/cooler move still works at any tint."""
+    k, info = _invert_ratio(samples, ratio)
+    if k is None:
+        return k, info
+    dist = _off_curve(samples, k, coeffs)
+    info["off_curve"] = dist
+    if dist is not None and dist > _OFF_CURVE_TOLERANCE and not strict:
+        info["approximate"] = True
+        return k, info
+    if dist is not None and dist > _OFF_CURVE_TOLERANCE:
+        return None, {**info, "interpolated": False, "exact": False, "reason": (
+            f"these coefficients are {100 * dist:.0f}% off the calibrated curve: the frame's "
+            "tint is not the table's calibrated tint, and r/b alone cannot give Kelvin at "
+            "another tint. Set tint back to 1.0, or re-run `scouter wb calibrate` "
+            "for this tint; the slider value itself was applied.")}
+    return k, info
+
+
+def _invert_ratio(samples: list[dict], ratio: float) -> tuple[float | None, dict]:
     """Bisect the sorted `samples` on `r = coeff_r / coeff_b`, then interpolate
     linearly between the two bracketing entries.
 
@@ -423,6 +473,7 @@
     camera: str | None = None,
     *,
     image: str | int | None = None,
+    strict: bool = False,
 ) -> dict:
     """Invert Kelvin from `coeffs` (r, g, b, ...) against `camera`'s table, or
     from `image`'s own committed XMP (which also supplies `camera`, via EXIF).
@@ -465,7 +516,7 @@
 
     table_ref = {"path": str(_table_path(camera)), "synthetic": bool(table.get("synthetic"))}
     target = _ratio(coeffs)
-    k, info = _invert(table["samples"], target)
+    k, info = _invert(table["samples"], target, list(coeffs), strict=strict)
     return {
         "kelvin": k,
         "camera": camera,
@@ -487,7 +538,7 @@
     top-level verb"). This wrapper is what a direct CLI/MCP call goes through.
     """
     job = job or current_job()
-    result = kelvin(image=image)
+    result = kelvin(image=image, strict=True)
     return KelvinResult(
         kelvin=result["kelvin"],
         interpolated=result["interpolated"],
```

- [ ] **Step 4: Run the new test and the WB-dependent suites**

Run: `.venv/bin/python -m pytest tests/test_wb_w4.py tests/test_wb.py tests/test_wb_folder.py tests/test_moves.py tests/test_style_set.py -q`
Expected: all pass (checked while writing this plan: 229 passed).

- [ ] **Step 5: Commit**

```bash
git add scouter/core/wb.py tests/test_wb_w4.py
git commit -m "scouter: wb.kelvin reads back honestly off the calibrated tint (W4: 5934 K for 5500 K) (Task 31, row 433)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part 7: Vocabulary families (row 437) -- run AFTER Task 35 (night 1)

The phase 3 pin rule (spec `~/second-brain/Specs/scouter-phase3-design.md` §3.1 rule 10, §10): a vocabulary row is `live` only when (1) its `dt.gui.action` path is in `dtbridge.ACTION_PATHS`, which happens only after a host probe answered non-`nan` for it and the Lua worker stayed alive, and (2) its XMP slot is named in `xmp.LAYOUTS`, which happens only after the sentinel method found exactly one matching float slot. Until then the row is `candidate` and every phrase that uses it refuses honestly with `not_pinned`. The machinery already exists: `tests/test_host_phase3.py::ProbeCandidatePathsHostTest::test_probe_every_candidate` probes every `candidate` row's `candidate_paths` (one darktable launch per path, 20 s settle, control path), and `Phase3LiveHostTest::test_1_slot_pinning_by_sentinel` pins the slot of every `absolute_only` row. Both append to `docs/measurements-generated.md`.

Each family task therefore has the same shape, and each is one reviewable unit:

1. (implementer, pure) add the rows as `candidate` with their `candidate_paths`, the phrases, and the unit tests; regenerate the vocab and phrase goldens; commit.
2. (**LIVE**, controller) run the path probe; the result table names each path `pinned`, `absent` or `wedges`.
3. (implementer) add exactly the `pinned` paths to `ACTION_PATHS`; commit.
4. (**LIVE**, controller) run the sentinel slot probe on the now `absolute_only` rows; it prints `slot <module>.<param> v<N>: sentinel <v> ... slots=[i] of <count> (size <bytes>)` and a `floats <module> v<N>` line, and saves the sidecar copy `phase3-slots-<pid>.xmp`.
5. (implementer) add `xmp.LAYOUTS[(module, N)] = {"size": <bytes>, "named_slots": {i: param}, "float_count": <count>, "verified": "tests/fixtures/<module>-v<N>.xmp (Task 3x host probe)"}` and commit the saved sidecar as `tests/fixtures/<module>-v<N>.xmp`; add the check-map capabilities; update the count tests; commit. The rows are now `live`, and the gauntlet generates their coverage block automatically (`coverage.live_params()`).
6. (**LIVE**, controller) gate reruns (spec §7 order rule) on a fresh stamp: the new module's coverage block, every scenario that refused on this capability in night 1, and the P0 slice of every layer.

A path the probe marks `absent` or `wedges` is never pinned: its row stays `candidate`, its phrases keep refusing `not_pinned`, and the task reports it (roadmap), it does not guess another path.

### Task 32: Grain and vignette sliders

Not parallel (shares `vocab.py`, `phrases.py`, `dtbridge.py`, `xmp.py`, `check_map.json` with Tasks 33-34). Depends on: Tasks 35 (night-1 results), 30. Probe steps are **LIVE**.

**Files:**
- Modify: `scouter/core/vocab.py` (`CONCEPT_IDS` + 2 rows), `scouter/core/phrases.py` (4 phrases), `tests/fixtures/vocab_golden.json`, later `scouter/core/dtbridge.py` (`ACTION_PATHS`), `scouter/core/xmp.py` (`LAYOUTS`), `tests/gauntlet/data/check_map.json`, `tests/gauntlet/test_taxonomy_checkmap.py`, `tests/gauntlet/test_layer_offline.py`, `tests/gauntlet/test_layer_coverage.py`, `tests/gauntlet/test_adapter.py`
- Create: `tests/test_vocab_finishing.py`, later `tests/fixtures/grain-v<N>.xmp`, `tests/fixtures/vignette-v<N>.xmp`

**Interfaces:**
- Produces: vocab rows `grain.strength` (`%` internal 0-100, Scouter range 0-60, step 5, concept `finishing`) and `vignette.brightness` (ratio -1..1, Scouter range -0.8..0.4, step 0.05, concept `finishing`); phrase keys `more_grain`, `less_grain`, `darker_corners`, `lighter_corners`; check-map capabilities `more_grain`, `less_grain`, `darker_corners`, `lighter_corners` and concepts `grain`, `corners`, `corner_falloff`.

- [ ] **Step 1: Write the failing test** (`tests/test_vocab_finishing.py`)

```python
"""Row 437 family 1: grain + vignette rows exist, start as candidates, and
their phrases resolve (and refuse honestly until pinned)."""

import unittest

from scouter.core import phrases, vocab


class FinishingRowsTest(unittest.TestCase):
    def test_rows_exist_with_candidate_paths(self) -> None:
        g = vocab.spec("grain", "strength")
        v = vocab.spec("vignette", "brightness")
        self.assertIsNotNone(g)
        self.assertIsNotNone(v)
        self.assertIn("iop/grain/strength", g.candidate_paths)
        self.assertIn("iop/vignette/brightness", v.candidate_paths)
        self.assertEqual((g.lo, g.hi, g.step), (0.0, 60.0, 5.0))
        self.assertEqual((v.lo, v.hi, v.step), (-0.8, 0.4, 0.05))

    def test_phrases_resolve_to_the_rows(self) -> None:
        self.assertEqual(phrases.resolve("add grain").moves[0].param, "strength")
        self.assertEqual(phrases.resolve("less grain").moves[0].notches, -1)
        self.assertEqual(phrases.resolve("darker corners").moves[0].notches, -1)
        self.assertEqual(phrases.resolve("add a vignette").key, "darker_corners")
        self.assertEqual(phrases.resolve("lighter corners a lot").moves[0].notches, 2)

    def test_status_follows_the_pins(self) -> None:
        for s in (vocab.spec("grain", "strength"), vocab.spec("vignette", "brightness")):
            self.assertIn(vocab.status(s), ("candidate", "absolute_only", "live"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_vocab_finishing.py -q`
Expected: `AssertionError: unexpectedly None` (no grain row yet).

- [ ] **Step 3: Add the rows and phrases**

In `scouter/core/vocab.py` add `"finishing"` to `CONCEPT_IDS`, and append to `PARAMS` (before the closing parenthesis):

```python
    # Row 437 family 1 (finishing). Internal units are the module's own params
    # (grain strength is stored 0-100; vignette brightness -1..1). Candidate
    # until the Task 32 host probe pins the path and the XMP slot.
    ParamSpec("grain", "strength", "grain › strength", "%", 1.0, 0.0, 60.0, 5.0,
              "xmp", 0.0, False, "finishing",
              "adds film-like grain; more strength is more visible grain",
              "smooth skies and skin, where grain shows first",
              ("iop/grain/strength",)),
    ParamSpec("vignette", "brightness", "vignette › brightness", "ratio", 1.0, -0.8, 0.4, 0.05,
              "xmp", 0.0, False, "finishing",
              "darkens (negative) or lightens (positive) the corners around the centre",
              "the frame edges: a strong vignette reads as a tunnel",
              ("iop/vignette/brightness",)),
```

In `scouter/core/phrases.py` append to `PHRASES`:

```python
    Phrase("more_grain", ("add grain", "more grain", "grainier", "film grain"),
           (_adj("grain", "strength", 1),)),
    Phrase("less_grain", ("less grain", "remove grain", "smoother grain"),
           (_adj("grain", "strength", -1),)),
    Phrase("darker_corners", ("darker corners", "add a vignette", "add vignette",
                              "stronger vignette", "vignette"),
           (_adj("vignette", "brightness", -1),)),
    Phrase("lighter_corners", ("lighter corners", "brighter corners", "weaker vignette",
                               "less vignette"),
           (_adj("vignette", "brightness", 1),)),
```

Regenerate the vocab golden with the command `tests/test_vocab.py:17` prints (here with the repo venv):

```bash
.venv/bin/python -c "import json; from scouter.core import vocab; print(json.dumps(vocab.table(), indent=2, sort_keys=True))" > tests/fixtures/vocab_golden.json
```

`tests/fixtures/phrases_golden.json` covers the 40 fixed `INPUTS` of `tests/test_phrases.py` and does not change. Review the vocab golden diff: only the new rows may appear.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_vocab_finishing.py tests/test_vocab.py tests/test_phrases.py tests/test_moves.py tests/test_say.py -q && .venv/bin/python -m pytest tests -q -k "not host"`
Expected: all pass. `darkroom_say "add grain"` now refuses `not_pinned` (the row is a candidate), which is the honest answer until Step 6.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/vocab.py scouter/core/phrases.py tests/fixtures/vocab_golden.json tests/test_vocab_finishing.py
git commit -m "scouter: grain and vignette rows and phrases as candidates (Task 32, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6 (LIVE, controller): probe the paths**

With darktable closed: `SCOUTER_HOST_TESTS=1 .venv/bin/python -m pytest tests/test_host_phase3.py::ProbeCandidatePathsHostTest -q -s`
Expected: two new rows in `docs/measurements-generated.md`, e.g. `| grain.strength | \`iop/grain/strength\` | pinned | 110 |`. Record the verdicts in the task report.

- [ ] **Step 7: Pin the paths the probe answered `pinned`** (only those)

In `scouter/core/dtbridge.py` `ACTION_PATHS`, after the phase 3 block:

```python
    # Row 437 family 1 host probe (Task 32, <date>): answered non-nan, worker alive after.
    ("grain", "strength"): "iop/grain/strength",
    ("vignette", "brightness"): "iop/vignette/brightness",
```

Run `.venv/bin/python -m pytest tests/test_dtbridge.py tests/test_vocab.py -q` (regenerate `vocab_golden.json` with the Step 3 command: the status column changes to `absolute_only`), then commit: `git commit -am "scouter: pin grain/vignette action paths from the host probe (Task 32, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"`.

- [ ] **Step 8 (LIVE, controller): pin the XMP slots**

`SCOUTER_HOST_TESTS=1 .venv/bin/python -m pytest "tests/test_host_phase3.py::Phase3LiveHostTest::test_1_slot_pinning_by_sentinel" -q -s`
Expected lines: `slot grain.strength v<N>: sentinel 17.0 set ok=True slots=[i] of <count> (size <bytes>)` and the same for `vignette.brightness` (sentinel 0.17), plus `floats grain v<N> ...`. Exactly one slot each; anything else is not pinned (record it and stop this row). Copy the saved `phase3-slots-<pid>.xmp` (in `tests/host_support.SCRATCH_DIR`) to `tests/fixtures/grain-v<N>.xmp` and `tests/fixtures/vignette-v<N>.xmp` (the same file serves both when both modules are in it; then name it `finishing-v<N>.xmp` and reference it from both layouts).

- [ ] **Step 9: Name the slots, add the capabilities, update the counts**

In `scouter/core/xmp.py` `LAYOUTS`, with the numbers printed in Step 8:

```python
    # Row 437 family 1 host probe (Task 32): sentinel 17.0 at exactly one slot.
    ("grain", <N>): {"size": <bytes>, "named_slots": {<i>: "strength"},
                     "float_count": <count>, "verified": "tests/fixtures/<file>.xmp (Task 32)"},
    ("vignette", <M>): {"size": <bytes>, "named_slots": {<j>: "brightness"},
                        "float_count": <count>, "verified": "tests/fixtures/<file>.xmp (Task 32)"},
```

and a unit test in `tests/test_vocab_finishing.py`:

```python
    def test_the_committed_fixture_decodes_the_sentinels(self) -> None:
        from pathlib import Path

        from scouter.core import xmp

        fx = Path(__file__).parent / "fixtures" / "<file>.xmp"
        parsed = xmp.parse(fx.read_text())
        live = {e.operation: e.decoded for e in parsed.entries if e.decoded}
        self.assertAlmostEqual(live["grain"]["strength"], 17.0, places=3)
        self.assertAlmostEqual(live["vignette"]["brightness"], 0.17, places=3)
        self.assertEqual(vocab.status(vocab.spec("grain", "strength")), "live")
```

In `tests/gauntlet/data/check_map.json`: change the `stat_rules` entries `{"match": "grain", "concept": "none"}` -> `"grain"`, `{"match": "corner", ...}` and `{"match": "vignette", ...}` -> `"corners"`, and insert `{"match": "falloff", "concept": "corner_falloff"}` immediately before the `corner` rule; add to `concepts`:

```json
    "grain":          {"metric": "M7", "stat": "noise", "region": "frame", "caps": {"1": "more_grain", "-1": "less_grain"}},
    "corners":        {"metric": "M2", "stat": "L", "region": "corners", "caps": {"1": "lighter_corners", "-1": "darker_corners"}},
    "corner_falloff": {"metric": "M2", "stat": "L", "region": "corners", "caps": {"1": "darker_corners", "-1": "lighter_corners"}},
```

and to `capabilities`:

```json
    "more_grain": {"phrase": "add grain"},
    "less_grain": {"phrase": "less grain"},
    "lighter_corners": {"phrase": "lighter corners"},
    "darker_corners": {"phrase": "darker corners"},
```

Update the count tests (dry-run while writing this plan): `test_every_request_maps_no_unmapped` now expects `{"out": 294, "in": 201, "impossible": 60, "workflow": 37, "partial": 9}`; coverage and offline counts follow their formulas (`coverage.expected_count()`, `offline.EXPECTED`), so change the literal `1316` / `2314` / `318` in `tests/gauntlet/test_layer_coverage.py`, `test_layer_offline.py` and the dict in `tests/gauntlet/test_adapter.py` to the numbers `.venv/bin/python -m gauntlet --runs-root "$(mktemp -d)" generate --stamp dry` prints in its `generation.md` table. Add the looks mapping: in `tests/gauntlet/exec_looks.py` `plan_moves`, after the black-lift block,

```python
    if t.get("grain"):
        moves.append(("grain", "strength", float(min(60, max(0, t["grain"])))))
    if t.get("vignette"):
        moves.append(("vignette", "brightness", float(max(-0.8, min(0.4, -t["vignette"] / 100)))))
```

and in `tests/gauntlet/layers/looks.py` `gaps()` drop `"grain"` and `"vignette"` from the gap list only when `vocab.status` of the row is `live` (import `scouter.core.vocab` there).

Run: `.venv/bin/python -m pytest tests/test_vocab_finishing.py tests/gauntlet -q && .venv/bin/python -m pytest tests -q -k "not host"` -> all pass. Commit:

```bash
git add scouter/core/xmp.py tests/fixtures tests/test_vocab_finishing.py tests/gauntlet
git commit -m "scouter: grain and vignette live (slots pinned by sentinel); gauntlet maps them (Task 32, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 10 (LIVE, controller): gate reruns**

```bash
.venv/bin/python -m gauntlet generate --stamp gate32-$(date +%Y%m%d%H%M)
.venv/bin/python -m gauntlet run --stamp latest --layer coverage --prefix cov-grain. --prefix cov-vignette. --prefix cov-phrase-more_grain --prefix cov-phrase-less_grain --prefix cov-phrase-darker_corners --prefix cov-phrase-lighter_corners --prefix cov-synonym-more_grain --prefix cov-synonym-less_grain --prefix cov-synonym-darker_corners --prefix cov-synonym-lighter_corners --prefix cov-p4-more_grain --prefix cov-p4-less_grain --prefix cov-p4-darker_corners --prefix cov-p4-lighter_corners
.venv/bin/python -m gauntlet run --stamp latest --layer scenarios --layer scenarios_agent --where roadmap_key=grain --where roadmap_key=vignette
.venv/bin/python -m gauntlet run --stamp latest --tier P0
.venv/bin/python -m gauntlet report --stamp latest
```

Pass condition (spec §7): every new coverage case passes or is listed with a triage; every previously refused grain/vignette scenario is now pass or partial; the P0 slice is 100%, with zero silent failures and zero crashes. A failure goes to the fix loop (Task 36) before Task 33 starts.

### Task 33: Lens correction on/off, rotate/straighten, simple crop presets

Not parallel. Depends on: Task 32. Probe steps are **LIVE**.

**Files:**
- Modify: `scouter/core/vocab.py` (3 kinds of rows, `status()` for toggles), `scouter/core/phrases.py` (`Move.kind` gains `toggle` and `crop`; phrases), `scouter/core/moves.py` (`say()` dispatch for the two kinds; `crop_margins`), `scouter/core/darkroom.py` (`disable()` verb), `scouter/mcp/schemas.py` (`darkroom_disable`), `tests/scouter_smoke_args.py` (`darkroom.disable`), `tests/test_layer_parity.py` (EXPECTED_VERBS gains `darkroom.disable`), `tests/gauntlet/test_layer_parity.py` (the gauntlet parity-layer count: 90 -> 92; a different file from `tests/test_layer_parity.py`), `darktable_mcp/lua/darktable_mcp.lua` (`darkroom.current` returns `width`, `height`), goldens, later `dtbridge.ACTION_PATHS`, `xmp.LAYOUTS`, `check_map.json` and the gauntlet count tests
- Create: `tests/test_vocab_geometry.py`

**Interfaces:**
- Produces: row `lens.__enabled` (`bool`, value source `xmp` enabled flag; `status()` is `live` when `lens` is in `dtbridge.MODULE_WHITELIST`, which it already is from the benchmark's L1/L2 runs, so no path probe is needed for the toggle); row `ashift.rotation` (`°`, -10..10, step 0.5, candidates `iop/ashift/rotation`, `iop/ashift/rotate`); rows `crop.left`, `crop.right`, `crop.top`, `crop.bottom` (ratio 0..1, candidates `iop/crop/left` etc.); phrase keys `lens_on`, `lens_off`, `rotate_right`, `rotate_left`, `crop_square`, `crop_4x5`, `crop_16x9`, `crop_3x2`; `moves.crop_margins(target_aspect, width, height) -> dict[str, float]`; verb `darkroom.disable` (MCP `darkroom_disable`, CLI `scouter darkroom disable <module>`).

- [ ] **Step 1: Write the failing test** (`tests/test_vocab_geometry.py`)

```python
"""Row 437 family 2: lens toggle, rotation and crop presets."""

import unittest

from scouter.core import moves, phrases, vocab


class GeometryTest(unittest.TestCase):
    def test_lens_toggle_is_live_through_the_module_whitelist(self) -> None:
        s = vocab.spec("lens", "__enabled")
        self.assertEqual(vocab.status(s), "live")
        res = phrases.resolve("correct the lens")
        self.assertEqual((res.moves[0].kind, res.moves[0].value), ("toggle", 1.0))
        self.assertEqual(phrases.resolve("turn off lens correction").moves[0].value, 0.0)

    def test_rotation_row_and_phrases(self) -> None:
        s = vocab.spec("ashift", "rotation")
        self.assertEqual((s.lo, s.hi, s.step, s.unit), (-10.0, 10.0, 0.5, "°"))
        self.assertEqual(phrases.resolve("rotate right").moves[0].notches, 1)
        self.assertEqual(phrases.resolve("rotate left a touch").moves[0].notches, -0.5)
        self.assertTrue(phrases.resolve("rotate right by 2°").explicit)

    def test_crop_margins_center_the_target_aspect(self) -> None:
        m = moves.crop_margins(1.0, 6000, 4000)   # 3:2 landscape -> square
        self.assertAlmostEqual(m["left"], 1 / 6, places=6)
        self.assertAlmostEqual(m["right"], 5 / 6, places=6)
        self.assertEqual((m["top"], m["bottom"]), (0.0, 1.0))
        m = moves.crop_margins(16 / 9, 4000, 3000)  # 4:3 -> 16:9 trims top and bottom
        self.assertEqual((m["left"], m["right"]), (0.0, 1.0))
        self.assertAlmostEqual(m["bottom"] - m["top"], (4000 / (16 / 9)) / 3000, places=6)
        with self.assertRaises(ValueError):
            moves.crop_margins(1.0, 0, 4000)

    def test_crop_phrases_carry_the_aspect(self) -> None:
        res = phrases.resolve("crop square")
        self.assertEqual((res.moves[0].kind, res.moves[0].value), ("crop", 1.0))
        self.assertAlmostEqual(phrases.resolve("crop to 4:5").moves[0].value, 0.8)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_vocab_geometry.py -q`
Expected: failures (`vocab.spec("lens", "__enabled")` is None; `moves` has no `crop_margins`).

- [ ] **Step 3: Implement the rows, kinds, dispatch and the disable verb**

`scouter/core/vocab.py`: add `"geometry"` to `CONCEPT_IDS`; append the rows:

```python
    # Row 437 family 2 (geometry). The lens toggle is a module on/off, live
    # whenever `lens` is a whitelisted module (the benchmark enabled it on the
    # host, L1/L2); rotation and crop margins wait for the Task 33 probe.
    ParamSpec("lens", "__enabled", "lens correction › on/off", "bool", 1.0, 0.0, 1.0, 1.0,
              "xmp", 0.0, False, "geometry",
              "applies the lens profile: distortion, vignetting and chromatic aberration",
              "straight lines near the edges"),
    ParamSpec("ashift", "rotation", "rotate and perspective › rotation", "°", 1.0, -10.0, 10.0,
              0.5, "xmp", 0.0, False, "geometry",
              "rotates the whole frame to level a tilted horizon; the corners are cropped",
              "the corners, which rotation crops away",
              ("iop/ashift/rotation", "iop/ashift/rotate")),
    *(ParamSpec("crop", side, f"crop › {side} margin", "ratio", 1.0, 0.0, 1.0, 0.01, "xmp",
                0.0 if side in ("left", "top") else 1.0, False, "geometry",
                f"moves the {side} edge of the crop", "what the new edge cuts off",
                (f"iop/crop/{side}",)) for side in ("left", "right", "top", "bottom")),
```

and at the top of `status()`:

```python
    if s.param == "__enabled":  # a module toggle: pinned by the module whitelist
        return "live" if s.module in dtbridge.MODULE_WHITELIST else "candidate"
```

`scouter/core/phrases.py`: extend the `Move.kind` comment to `"adjust" | "set" | "toggle" | "crop"`; add helpers and phrases:

```python
def _toggle(module: str, on: bool) -> Move:
    return Move(module, "__enabled", "toggle", value=1.0 if on else 0.0)


def _crop(aspect: float) -> Move:
    return Move("crop", "aspect", "crop", value=aspect)
```

```python
    Phrase("lens_on", ("correct the lens", "lens correction", "turn on lens correction",
                       "fix the distortion", "fix lens distortion", "remove chromatic aberration"),
           (_toggle("lens", True),)),
    Phrase("lens_off", ("turn off lens correction", "no lens correction"),
           (_toggle("lens", False),)),
    Phrase("rotate_right", ("rotate right", "rotate clockwise", "tilt right"),
           (_adj("ashift", "rotation", 1),)),
    Phrase("rotate_left", ("rotate left", "rotate counterclockwise", "rotate anticlockwise",
                           "tilt left"), (_adj("ashift", "rotation", -1),)),
    Phrase("crop_square", ("crop square", "square crop", "crop to 1:1", "make it square"),
           (_crop(1.0),)),
    Phrase("crop_4x5", ("crop to 4:5", "crop 4:5", "portrait crop for instagram"), (_crop(0.8),)),
    Phrase("crop_16x9", ("crop to 16:9", "crop 16:9", "widescreen crop"), (_crop(16 / 9),)),
    Phrase("crop_3x2", ("crop to 3:2", "crop 3:2"), (_crop(1.5),)),
```

`normalise()` keeps `:` out of its character class today; add `:` to the allowed characters (`r"[^a-z0-9.+%°: -]"`) so "crop to 4:5" survives. In `_row(move)` return `vocab.spec("crop", "left")` for `move.module == "crop"` (the crop presets move four rows; the left row carries the status). The `by N` explicit branch refuses toggles and crops (they are not one-adjust phrases) with the existing `usage` message.

`scouter/core/moves.py`: add

```python
def crop_margins(target_aspect: float, width: int, height: int) -> dict[str, float]:
    """Centered crop margins (fractions of the frame) for `target_aspect` = w/h."""
    if width <= 0 or height <= 0 or target_aspect <= 0:
        raise ValueError(f"cannot crop {width}x{height} to aspect {target_aspect}")
    frame = width / height
    if target_aspect < frame:  # narrower: trim left and right
        keep = target_aspect / frame
        m = (1 - keep) / 2
        return {"left": m, "right": 1 - m, "top": 0.0, "bottom": 1.0}
    keep = frame / target_aspect  # wider: trim top and bottom
    m = (1 - keep) / 2
    return {"left": 0.0, "right": 1.0, "top": m, "bottom": 1 - m}
```

and in `say()`'s move loop, before the `if mv.kind == "adjust":` branch:

```python
                if mv.kind == "toggle":
                    r = (darkroom.enable if mv.value else darkroom.disable)(
                        module, instance=0, job=job)
                    landed.append({**r.to_dict(), "summary": f"{s.ui}: "
                                   f"{'on' if mv.value else 'off'}"})
                    continue
                if mv.kind == "crop":
                    cur = dtbridge.call("darkroom.current")
                    w, h = int(cur.get("width") or 0), int(cur.get("height") or 0)
                    if not (w and h):
                        raise ValueUnknown("the image size is unknown, so a crop preset "
                                           "cannot be placed", detail={"reason": "no width/height"})
                    for side, v in crop_margins(float(mv.value), w, h).items():
                        r = _set_recording_before("crop", side, v, instance=0, commit=commit,
                                                  job=job, fallback=before_fallback(
                                                      resolved, vocab.spec("crop", side), 0, job))
                        landed.append({**r.to_dict(), "target": v})
                    continue
```

Also make `_refusal(module, param, kind)` treat `toggle` like `set` and check `vocab.status` of `crop.left` for `crop` moves (a crop preset refuses `not_pinned` until all four crop rows are pinned).

`scouter/core/darkroom.py`: add the verb next to `enable`:

```python
@metrics.instrumented("darkroom.disable")
def disable(
    module: str, *, instance: int = 0, dry_run: bool = False, job: str | None = None
) -> ModuleResult:
    """Disable a module (the plugin's `module.disable`). Not gated: re-enable undoes it."""
    return _do_module_action("disable", "module.disable", module=module, instance=instance,
                             dry_run=dry_run, confirm=True, job=job)
```

and register it exactly like `darkroom.enable` (copy its `register(Verb(...))` block with `id="darkroom.disable"`, `summary="Disable one module on the open image."`). Add the MCP schema entry to `scouter/mcp/schemas.py` by copying `darkroom_enable`'s spec with `name="darkroom_disable"`; add `"darkroom.disable": {"module": "exposure", "dry_run": True}` to `tests/scouter_smoke_args.SMOKE_ARGS`; add `"darkroom.disable"` to `EXPECTED_VERBS` in `tests/test_layer_parity.py`; regenerate `tests/fixtures/scouter_mcp_golden.json` with the command its test prints.

`darktable_mcp/lua/darktable_mcp.lua`: in the `darkroom.current` handler's answer table add `width = image.width, height = image.height` (the dt Lua image object exposes both), and add the two keys to the Lua test that pins that answer under `tests/lua/`.

Regenerate `vocab_golden.json` (Task 32 Step 3 command).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_vocab_geometry.py -q && .venv/bin/python -m pytest tests -q -k "not host"`
Expected: all pass. "correct the lens" is live now; rotate and crop phrases refuse `not_pinned`.

- [ ] **Step 5: Commit**

```bash
git add scouter darktable_mcp tests
git commit -m "scouter: lens on/off (live), rotation and crop presets as candidates, darkroom.disable (Task 33, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6 (LIVE, controller): probe the paths**

With darktable closed: `SCOUTER_HOST_TESTS=1 .venv/bin/python -m pytest tests/test_host_phase3.py::ProbeCandidatePathsHostTest -q -s`
Expected: new rows in `docs/measurements-generated.md` for `ashift.rotation` and `crop.left`, `crop.right`, `crop.top`, `crop.bottom`, each `pinned`, `absent` or `wedges`. Record them in the task report.

- [ ] **Step 7: Pin only the paths the probe answered `pinned`**

In `scouter/core/dtbridge.py` `ACTION_PATHS`:

```python
    # Row 437 family 2 host probe (Task 33, <date>): answered non-nan, worker alive after.
    ("ashift", "rotation"): "iop/ashift/rotation",  # or "iop/ashift/rotate", whichever answered
    ("crop", "left"): "iop/crop/left",
    ("crop", "right"): "iop/crop/right",
    ("crop", "top"): "iop/crop/top",
    ("crop", "bottom"): "iop/crop/bottom",
```

Regenerate `tests/fixtures/vocab_golden.json` (`.venv/bin/python -c "import json; from scouter.core import vocab; print(json.dumps(vocab.table(), indent=2, sort_keys=True))" > tests/fixtures/vocab_golden.json`), run `.venv/bin/python -m pytest tests/test_dtbridge.py tests/test_vocab.py -q`, and commit: `git commit -am "scouter: pin rotation and crop action paths from the host probe (Task 33, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"`.

- [ ] **Step 8 (LIVE, controller): pin the XMP slots**

`SCOUTER_HOST_TESTS=1 .venv/bin/python -m pytest "tests/test_host_phase3.py::Phase3LiveHostTest::test_1_slot_pinning_by_sentinel" -q -s`
Expected: one `slot <module>.<param> v<N>: sentinel <v> set ok=True slots=[i] of <count> (size <bytes>)` line per row and a `floats <module> v<N> ...` line per module. Exactly one slot per row, or the row is not pinned (record and stop that row). Copy the saved `phase3-slots-<pid>.xmp` from `tests/host_support.SCRATCH_DIR` to `tests/fixtures/ashift-v<N>.xmp` and `tests/fixtures/crop-v<M>.xmp` (the sentinels are 0.17 for rotation and 0.17/0.16/0.15/0.14 for left/right/top/bottom in row order; confirm on the host that `darkroom.current` now answers `width` and `height`: `.venv/bin/python -c "from scouter.core import dtbridge; print(dtbridge.call('darkroom.current'))"` with darktable open on a scratch copy).

- [ ] **Step 9: Name the slots, map the capabilities, update the counts**

In `scouter/core/xmp.py` `LAYOUTS`, with the numbers from Step 8's lines:

```python
    # Row 437 family 2 host probe (Task 33): one sentinel slot per row.
    ("ashift", <N>): {"size": <bytes>, "named_slots": {<i>: "rotation"},
                      "float_count": <count>, "verified": "tests/fixtures/ashift-v<N>.xmp (Task 33)"},
    ("crop", <M>): {"size": <bytes>, "named_slots": {<a>: "left", <b>: "top", <c>: "right",
                                                     <d>: "bottom"},
                    "float_count": <count>, "verified": "tests/fixtures/crop-v<M>.xmp (Task 33)"},
```

Add a fixture test to the task's test file that parses the committed fixture with `xmp.parse` and asserts each sentinel decodes and each row's `vocab.status` is `live`. In `tests/gauntlet/data/check_map.json` change the `stat_rules` `curvature` and `ca fringe` entries to concept `lens`, add the concept `"lens": {"metric": "M1", "stat": "lens.__enabled", "region": "frame", "caps": {"-1": "lens_on"}}` (`layers.common.stat_check` turns an M1 concept into an `equals 1` check on the toggle) and the capability `"lens_on": {"phrase": "correct the lens"}`. The parity layer grows to 92 cases (`parity.EXPECTED` follows the registry); in `tests/gauntlet/test_layer_parity.py` (created by Task 23; not `tests/test_layer_parity.py`, which holds EXPECTED_VERBS) rename `test_parity_is_45_verbs_times_2` to `test_parity_is_46_verbs_times_2` and change its literal `90` to `92` (comment `# 46 verbs today`); the P0 count stays 24 (`darkroom.disable` is not in `parity.GUIDE_VERBS`). Update `tests/gauntlet/test_taxonomy_checkmap.py::test_every_request_maps_no_unmapped` to `{"out": 258, "in": 237, "impossible": 60, "workflow": 37, "partial": 9}` (dry run while writing this plan), and replace the literal counts in `tests/gauntlet/test_layer_coverage.py`, `test_layer_offline.py` and the dict in `tests/gauntlet/test_adapter.py` with the numbers `.venv/bin/python -m gauntlet --runs-root "$(mktemp -d)" generate --stamp dry` writes to its `generation.md` (coverage and offline follow `coverage.expected_count()` and `offline.EXPECTED`).

Run: `.venv/bin/python -m pytest tests/gauntlet -q && .venv/bin/python -m pytest tests -q -k "not host"` -> all pass. Commit:

```bash
git add scouter/core/xmp.py tests
git commit -m "scouter: rotation and crop pinned by sentinel; gauntlet maps lens correction (Task 33, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 10 (LIVE, controller): gate reruns**

```bash
.venv/bin/python -m gauntlet generate --stamp gate33-$(date +%Y%m%d%H%M)
.venv/bin/python -m gauntlet run --stamp latest --layer coverage --prefix cov-lens. --prefix cov-ashift. --prefix cov-crop. --prefix cov-phrase-lens_ --prefix cov-phrase-rotate_ --prefix cov-phrase-crop_ --prefix cov-synonym-lens_ --prefix cov-synonym-rotate_ --prefix cov-synonym-crop_
.venv/bin/python -m gauntlet run --stamp latest --layer scenarios --layer scenarios_agent --where roadmap_key=lens --where roadmap_key=ashift --where roadmap_key=crop
.venv/bin/python -m gauntlet run --stamp latest --layer parity
.venv/bin/python -m gauntlet run --stamp latest --tier P0
.venv/bin/python -m gauntlet report --stamp latest
```

Pass condition (spec §7): every new coverage case passes or carries a triage; every scenario that refused on this capability in night 1 is now pass or partial; the P0 slice is 100% with zero silent failures and zero crashes. A failure goes to the fix loop (Task 36) before the next family starts.

### Task 34: Denoise strength and sharpen amount

Not parallel. Depends on: Task 33. Probe steps are **LIVE**.

**Files:**
- Modify: `scouter/core/vocab.py` (`CONCEPT_IDS` + `detail`; 2 rows), `scouter/core/phrases.py` (4 phrases), goldens; later `dtbridge.ACTION_PATHS`, `xmp.LAYOUTS`, `check_map.json`, gauntlet count tests
- Create: `tests/test_vocab_detail.py`

**Interfaces:**
- Produces: rows `denoiseprofile.strength` (ratio, Scouter range 0..4, step 0.1, neutral 1.0; candidate `iop/denoiseprofile/strength`) and `sharpen.amount` (ratio 0..2, step 0.1, neutral 0.5; candidate `iop/sharpen/amount`); phrase keys `denoise_more`, `denoise_less`, `sharpen_more`, `sharpen_less`; check-map concepts `noise` (M7, `-1` -> `denoise_more`) and `sharp` (M8, `+1` -> `sharpen_more`) with the row-437 no-halo checks.

- [ ] **Step 1: Write the failing test** (`tests/test_vocab_detail.py`)

```python
"""Row 437 family 3: denoise strength and sharpen amount."""

import unittest

from scouter.core import phrases, vocab


class DetailRowsTest(unittest.TestCase):
    def test_rows(self) -> None:
        d = vocab.spec("denoiseprofile", "strength")
        s = vocab.spec("sharpen", "amount")
        self.assertEqual((d.lo, d.hi, d.step, d.neutral), (0.0, 4.0, 0.1, 1.0))
        self.assertEqual((s.lo, s.hi, s.step, s.neutral), (0.0, 2.0, 0.1, 0.5))
        self.assertIn("iop/denoiseprofile/strength", d.candidate_paths)
        self.assertIn("iop/sharpen/amount", s.candidate_paths)

    def test_phrases(self) -> None:
        self.assertEqual(phrases.resolve("reduce noise").key, "denoise_more")
        self.assertEqual(phrases.resolve("less denoising").moves[0].notches, -1)
        self.assertEqual(phrases.resolve("sharpen a touch").moves[0].notches, 0.5)
        self.assertEqual(phrases.resolve("less sharpening").key, "sharpen_less")
        self.assertEqual(phrases.resolve("softer contrast").key, "less_contrast")  # unchanged
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_vocab_detail.py -q`
Expected: `AttributeError: 'NoneType' object has no attribute 'lo'`.

- [ ] **Step 3: Add the rows and phrases**

`scouter/core/vocab.py`: add `"detail"` to `CONCEPT_IDS`; append

```python
    # Row 437 family 3 (detail). Candidates until the Task 34 host probe.
    ParamSpec("denoiseprofile", "strength", "denoise (profiled) › strength", "ratio", 1.0,
              0.0, 4.0, 0.1, "xmp", 1.0, False, "detail",
              "how hard the profiled denoiser smooths noise for this camera and ISO",
              "fine texture and skin: too much looks waxy",
              ("iop/denoiseprofile/strength",)),
    ParamSpec("sharpen", "amount", "sharpen › amount", "ratio", 1.0, 0.0, 2.0, 0.1,
              "xmp", 0.5, False, "detail",
              "adds edge contrast so detail looks crisper",
              "halos along high-contrast edges and amplified noise",
              ("iop/sharpen/amount",)),
```

`scouter/core/phrases.py`:

```python
    Phrase("denoise_more", ("reduce noise", "less noise", "denoise", "denoise more",
                            "more denoising", "clean up the noise"),
           (_adj("denoiseprofile", "strength", 1),)),
    Phrase("denoise_less", ("less denoising", "denoise less", "keep more texture"),
           (_adj("denoiseprofile", "strength", -1),)),
    Phrase("sharpen_more", ("sharpen", "sharper", "more sharpening", "crisper",
                            "sharpen it"), (_adj("sharpen", "amount", 1),)),
    Phrase("sharpen_less", ("less sharpening", "sharpen less", "less crunchy"),
           (_adj("sharpen", "amount", -1),)),
```

Regenerate `vocab_golden.json` (Task 32 Step 3 command).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_vocab_detail.py -q && .venv/bin/python -m pytest tests -q -k "not host"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scouter/core/vocab.py scouter/core/phrases.py tests/fixtures tests/test_vocab_detail.py
git commit -m "scouter: denoise strength and sharpen amount rows and phrases as candidates (Task 34, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6 (LIVE, controller): probe the paths**

With darktable closed: `SCOUTER_HOST_TESTS=1 .venv/bin/python -m pytest tests/test_host_phase3.py::ProbeCandidatePathsHostTest -q -s`
Expected: new rows in `docs/measurements-generated.md` for `denoiseprofile.strength` and `sharpen.amount`, each `pinned`, `absent` or `wedges`. Record them in the task report.

- [ ] **Step 7: Pin only the paths the probe answered `pinned`**

In `scouter/core/dtbridge.py` `ACTION_PATHS`:

```python
    # Row 437 family 3 host probe (Task 34, <date>): answered non-nan, worker alive after.
    ("denoiseprofile", "strength"): "iop/denoiseprofile/strength",
    ("sharpen", "amount"): "iop/sharpen/amount",
```

Regenerate `tests/fixtures/vocab_golden.json` (`.venv/bin/python -c "import json; from scouter.core import vocab; print(json.dumps(vocab.table(), indent=2, sort_keys=True))" > tests/fixtures/vocab_golden.json`), run `.venv/bin/python -m pytest tests/test_dtbridge.py tests/test_vocab.py -q`, and commit: `git commit -am "scouter: pin denoise and sharpen action paths from the host probe (Task 34, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"`.

- [ ] **Step 8 (LIVE, controller): pin the XMP slots**

`SCOUTER_HOST_TESTS=1 .venv/bin/python -m pytest "tests/test_host_phase3.py::Phase3LiveHostTest::test_1_slot_pinning_by_sentinel" -q -s`
Expected: one `slot <module>.<param> v<N>: sentinel <v> set ok=True slots=[i] of <count> (size <bytes>)` line per row and a `floats <module> v<N> ...` line per module. Exactly one slot per row, or the row is not pinned (record and stop that row). Copy the saved `phase3-slots-<pid>.xmp` from `tests/host_support.SCRATCH_DIR` to `tests/fixtures/denoiseprofile-v<N>.xmp` and `tests/fixtures/sharpen-v<M>.xmp` (sentinels 0.17 and 0.17: one row per module).

- [ ] **Step 9: Name the slots, map the capabilities, update the counts**

In `scouter/core/xmp.py` `LAYOUTS`, with the numbers from Step 8's lines:

```python
    # Row 437 family 3 host probe (Task 34): one sentinel slot per row.
    ("denoiseprofile", <N>): {"size": <bytes>, "named_slots": {<i>: "strength"},
                              "float_count": <count>,
                              "verified": "tests/fixtures/denoiseprofile-v<N>.xmp (Task 34)"},
    ("sharpen", <M>): {"size": <bytes>, "named_slots": {<j>: "amount"},
                       "float_count": <count>, "verified": "tests/fixtures/sharpen-v<M>.xmp (Task 34)"},
```

Add a fixture test to the task's test file that parses the committed fixture with `xmp.parse` and asserts each sentinel decodes and each row's `vocab.status` is `live`. In `tests/gauntlet/data/check_map.json` change the `stat_rules` `noise` entry to concept `noise` and `acutance` to concept `sharp`; add the concepts `"noise": {"metric": "M7", "stat": "noise", "region": "frame", "caps": {"-1": "denoise_more", "1": "denoise_less"}}` and `"sharp": {"metric": "M8", "stat": "sharp", "region": "frame", "caps": {"1": "sharpen_more", "-1": "sharpen_less"}}` and the capabilities `"denoise_more": {"phrase": "reduce noise"}`, `"denoise_less": {"phrase": "less denoising"}`, `"sharpen_more": {"phrase": "sharpen"}`, `"sharpen_less": {"phrase": "less sharpening"}`. No-halo checks (row 437): in `tests/gauntlet/layers/scenarios.py` `tool_case`, right after `checks = [stat_check(p) for p in plan.parts if p.capability]`, add

```python
        if "sharpen_more" in plan.capabilities:  # row 437: direction + no halo
            checks += [Check("M3", stat="clip_high", region="frame", sign=0, kind="unchanged"),
                       Check("M3", stat="clip_low", region="frame", sign=0, kind="unchanged")]
```

and in `tests/gauntlet/layers/coverage.py` add to `PHRASE_STAT`: `"denoise_more": ("M7", "noise", "frame", -1)`, `"denoise_less": ("M7", "noise", "frame", 1)`, `"sharpen_more": ("M8", "sharp", "frame", 1)`, `"sharpen_less": ("M8", "sharp", "frame", -1)`. Update `tests/gauntlet/test_taxonomy_checkmap.py::test_every_request_maps_no_unmapped` to `{"out": 204, "in": 289, "impossible": 60, "workflow": 37, "partial": 11}` (dry run while writing this plan), and replace the literal counts in `tests/gauntlet/test_layer_coverage.py`, `test_layer_offline.py` and the dict in `tests/gauntlet/test_adapter.py` with the numbers `.venv/bin/python -m gauntlet --runs-root "$(mktemp -d)" generate --stamp dry` writes to its `generation.md` (coverage and offline follow `coverage.expected_count()` and `offline.EXPECTED`).

Run: `.venv/bin/python -m pytest tests/gauntlet -q && .venv/bin/python -m pytest tests -q -k "not host"` -> all pass. Commit:

```bash
git add scouter/core/xmp.py tests
git commit -m "scouter: denoise and sharpen pinned by sentinel; gauntlet maps detail with no-halo checks (Task 34, row 437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 10 (LIVE, controller): gate reruns**

```bash
.venv/bin/python -m gauntlet generate --stamp gate34-$(date +%Y%m%d%H%M)
.venv/bin/python -m gauntlet run --stamp latest --layer coverage --prefix cov-denoiseprofile. --prefix cov-sharpen. --prefix cov-phrase-denoise_ --prefix cov-phrase-sharpen_ --prefix cov-synonym-denoise_ --prefix cov-synonym-sharpen_
.venv/bin/python -m gauntlet run --stamp latest --layer scenarios --layer scenarios_agent --where roadmap_key=denoiseprofile --where roadmap_key=sharpen --where roadmap_key=nlmeans --where roadmap_key=diffuse
.venv/bin/python -m gauntlet run --stamp latest --tier P0
.venv/bin/python -m gauntlet report --stamp latest
```

Pass condition (spec §7): every new coverage case passes or carries a triage; every scenario that refused on this capability in night 1 is now pass or partial; the P0 slice is 100% with zero silent failures and zero crashes. A failure goes to the fix loop (Task 36) before the next family starts.


---

# Part 8: Night 1, fix loop, night 2, release report (controller)

### Task 35: Night-1 run

**LIVE**, controller only. Depends on: Tasks 28, 30, 31 merged to main (and Task 12 Step 6, Task 15 Step 6, Task 28 Step 5 done).

- [ ] **Step 1: Preconditions**

```bash
pgrep -fl "MacOS/darktable" || echo "no darktable running"      # must print "no darktable running"
git -C ~/CodeBase/scouter status --porcelain           # must be empty
df -h ~/Pictures | tail -1                                         # at least 15 GB free (spec 4.4: ~12 GB/run, 3 GB peak)
test -f ~/Pictures/ScouterScratch/gauntlet/corpus.json && echo corpus ok
```

Tell Pratik (one line in the session summary) not to open darktable during the run.

- [ ] **Step 2: Generate and start**

```bash
cd ~/CodeBase/scouter
STAMP=night1-$(date +%Y%m%d)
.venv/bin/python -m gauntlet generate --stamp $STAMP
.venv/bin/python -m gauntlet run --stamp $STAMP --resume --wait-on-park \
  --layer offline --layer coverage --layer scenarios --layer scenarios_agent --layer bulk \
  --layer looks --layer robustness --layer parity --layer install --layer guide
```

Run it in the background (`run_in_background`), and watch `~/Pictures/ScouterScratch/gauntlet/$STAMP/log.md`. `generation.md` must show 5,167 cases. Agent mode runs as soon as its layer comes up; on a usage limit it parks (`--wait-on-park` sleeps until the reset when the message names a time, up to 6 h), otherwise the run exits 75 and the controller reruns the same command after the reset (`--resume` skips everything already done).

- [ ] **Step 3: Checkpoints**

After every layer (the `## Layer <name>: done` line in `log.md`):

```bash
.venv/bin/python -m gauntlet progress-html --stamp $STAMP --title "Scouter Gauntlet"
```

and republish `~/Pictures/ScouterScratch/gauntlet/$STAMP/progress.html` as the "Scouter Gauntlet" artifact at the SAME URL every time (spec §8), and update the vault Status Board per the Capsule status rule. A layer that stops on a crash (`stopped_crash`) is left for the fix loop; the run goes on to the next layer.

- [ ] **Step 4: Golden chart**

When `lk-golden-chart` has run: if its `golden.verdict` was `no_golden` (first run), inspect `chart/chart-v1.stats.candidate.json` (all 35 patches present, ramp L* increasing), copy it to `tests/gauntlet/golden/chart-v1.stats.json`, get Fable's review, and commit `"gauntlet: golden chart-v1 stats from night 1 (Task 35, spec 4.3)"`. Never re-baseline automatically afterwards.

- [ ] **Step 5: Report**

```bash
.venv/bin/python -m gauntlet report --stamp $STAMP
.venv/bin/python -m gauntlet progress-html --stamp $STAMP --title "Scouter Gauntlet"
```

Expected: `report.md` with S8 first (crashes, silent failures, P0 x/y, P1 gates, originals unchanged), `fixlist.md` listing every non-pass with its repro. Republish the artifact. Write a vault log entry (`/obsidian-log`) with the S8 block and the fix-list size.

### Task 36: Fix loop

Controller procedure; each fix is an implementer task. Depends on: Task 35.

- [ ] **Step 1: Triage every row of `fixlist.md`** into exactly one of `product_bug`, `harness_bug`, `taxonomy_error`, `vocab_gap`, `upstream`, `accepted` (spec §2.6). Silent failures, crashes and hangs first. `taxonomy_error` is fixed in the builder scripts (`tests/gauntlet/data/build_*.py`), never in the JSON by hand; `accepted` on a P0 case needs Pratik's OK (H2 is the known one). Record the verdicts in `fixlist.md`'s Triage column and commit it into the run dir (not the repo).

- [ ] **Step 2: For each `product_bug` / `harness_bug`**, dispatch one implementer on its own branch and worktree: turn the failing case into a failing unit (or Lua) test first (TDD), fix, Fable reviews, merge gate (`make gate BASE=<merge-base> HEAD=<branch>` runs the test-honesty check, `tests/mutation_check.py` - row 459, `SCOUTER_SKIP_MUTATION_CHECK=1` skips it only explicitly and always logs a WARN). No `git stash`.

- [ ] **Step 3: Rerun after each merge** (spec §2.6 rule 3) on the SAME stamp:

```bash
.venv/bin/python -m gauntlet run --stamp $STAMP --layer <layer> --only-failed
.venv/bin/python -m gauntlet run --stamp $STAMP --layer <layer> --tier P0 --rerun
.venv/bin/python -m gauntlet run --stamp $STAMP --layer robustness --tier P0
```

A change that touches the bridge, the plugin or commit also reruns the coverage core: `--layer coverage --tier P0`. A fix counts only when its rerun passes. Keep a table (finding, branch, commit, rerun result) for the release report.

- [ ] **Step 4:** When the fix list holds only `vocab_gap`, `upstream` and approved `accepted` rows, start Tasks 32-34 in order (each with its gate reruns), then Task 37.

### Task 37: Night-2 rerun on the release candidate

**LIVE**, controller only. Depends on: Tasks 36 and 34 merged.

- [ ] **Step 1:** Tag the candidate: `git tag htc-rc1 && git -C ~/CodeBase/scouter rev-parse --short HEAD`.
- [ ] **Step 2:** Task 35 Steps 1-3 and 5 with `STAMP=night2-$(date +%Y%m%d)` on that commit (a fresh stamp: every case runs; the golden now exists, so `lk-golden-chart` checks P7 for real).
- [ ] **Step 3:** The release bar holds when `report.md` S8 shows: crashes 0, silent failures 0, P0 100%, every P1 gate met, cases missing 0, originals unchanged. Otherwise back to Task 36.

### Task 38: Release report

Controller. Depends on: Task 37.

- [ ] **Step 1:** Write `docs/release/2026-09-scouter-1.0-gauntlet.md` in the repo: the night-2 S8 block verbatim, the P0 served/refused headline, per-layer rates, the fix table from Task 36 (finding, branch, commit, rerun), the accepted P0 rows with Pratik's OK, the S7 roadmap (top 10 families by score), and links to `report.md`, `contact-sheet.html` and `fixlist.md` in the run dir.
- [ ] **Step 2:** Mirror it to the vault as `Knowledge/Scouter - Release gauntlet run <stamp>.md` with AI-first frontmatter (`type: report`, `project: "[[Scouter]]"`, `decisions: [row 433, row 436, row 437]`, `source:` the repo path and commit) and the `## For future agent` preamble; update the Status Board; republish the Scouter Gauntlet artifact with the report.
- [ ] **Step 3:** Commit: `git add docs/release && git commit -m "docs: Scouter 1.0 release gauntlet report (Task 38, rows 433-437)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"`. Contact-sheet review is Pratik's one manual tick in S8; the release itself is his call.

---

## Self-review record (2026-09-26)

- **Spec coverage.** §0 inputs and counts: Task 9 (`taxonomy.EXPECTED`). §1 release bar, tiers, silent failure (a)-(f): Tasks 5, 8. §2.1 check map: Task 9. §2.2 generator (deterministic ids, byte-identical JSONL, ±2% gate, unmapped gate, generation.md): Task 6 + layer tasks. §2.3 runner (sessions 50/20 min, one darktable, foreign refusal, gui_ready launch + one relaunch, isolation + contamination relaunch, per-case steps, atomic writes, hard caps, health ladder, crash guard, resume, `--case`, `--only-failed`, timing): Tasks 7, 13, 14; agent mode: Task 16. §2.4 evaluator (M1 via decoded XMP with SNAPSHOT_OPS extended to every decoder, stats on 1024 px renders, colour helpers, CIEDE2000 with Sharma's 34 pairs, chart ΔE, numeric vs direction-only, P1-P7): Tasks 2-5, 14, 15, 18. §2.5 report S1-S8 + served-rate line + contact sheet: Task 8. §2.6 fix loop: Tasks 8 (fix list, triage classes), 36. §3.0-3.8 layers with exact counts: Tasks 17-25 (all counts reproduced while writing: 2,314 / 1,316 / 701+160 / 40 / 469 / 32 / 90 / 10 / 35 = 5,167). §4 corpus rules, roles, chart, scratch layout: Tasks 12, 13, 15. §5 metrics, tolerances, noise floor, scoring, honest refusal: Tasks 4, 5, 14. §6 watchdog now-list: dtprocess + health (11), typed error + doctor + ping (29), auto-heal + heal + telemetry + lock cleanup (30, 11); watchdog cases (22). §7 roadmap: Task 8 `roadmap()`, vocabulary families Tasks 32-34 with the pin rule and gate reruns. §8 checkpoints, progress artifact, schedule: Tasks 7, 27, 35-37. W4: Task 31.
- **Gaps accepted, with reasons.** H2 has no plugin hook (typed `blocked: skipped`, needs Pratik's `accepted`). The agent-mode 30-look sample and the Fuji calibration are after release (spec §3.4, row 437). Per-job configdirs are spec §6 "later".
- **Placeholder scan.** The only angle-bracket values left are the ones a host probe produces (`<N>`, `<i>`, `<bytes>`, `<count>`, `<file>` in Tasks 32-34 Step 9); each step names the exact probe output line they are copied from, and a probe result other than "exactly one slot" means the row is not pinned.
- **Type consistency.** Names cross-checked across tasks: `Case/Step/Check/Expect`, `CallRecord/Observation/Park/LayerSpec`, `Policy/evaluate`, `RunDir`, `Runner.run_layers`, `ScouterSession.call/ensure_frame/refresh_frame`, `execute.VIRTUAL/substitute/run_tool_case`, `observe.snapshot/flatten`, `POLICY`, `WRITE_TOOLS`, `stat_check`, `m1_restored(keys, units, from_step)`; the engine and adapter code in this plan was executed while writing it (engine 120 tests, adapter 54 tests, product changes in Tasks 11, 29, 30, 31 against the full unit suite).
- **Review Focus.** Five lines above, each with its pinning test in the owning task.
