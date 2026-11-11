# Scouter release gauntlet: design

**Date:** 2026-09-26
**Working name:** Hyperbolic Time Chamber (HTC). Pratik picks the final name (open question Q1). The code path is `tests/gauntlet/` whatever the name.
**Decision rows:** 433 (brainstorm answers and scope), 434 (the deadlock fix and the watchdog design), 432 (hang class named), 427 (the release checklist this extends).
**Base:** `main` adac31b, plus the research-data commit cc66aaf (`tests/gauntlet/data/`, `docs/research/`).
**Status:** design only. Nothing here is built. The plan is written from this spec.

## 0. Inputs (single source of truth)

| Input | Path (in repo) | Counts the generator asserts |
|---|---|---|
| Editing universe | `tests/gauntlet/data/taxonomy-modules.json` | 69 modules, 378 unique phrases, 12 genre workflows, 9 bulk flows, 16 look components, 14 cannot-do intents |
| Looks | `tests/gauntlet/data/looks.json` (built by `build_looks*.py`) | 234 looks in 16 families; target confidence 127 documented / 38 community-consensus / 69 inferred |
| Requests | `tests/gauntlet/data/requests.json` | 601 sentences, 141 clusters, 9 intents (60 impossible), 14 genres, 24 follow-ups |
| Measurement and robustness | `tests/gauntlet/data/measurement-and-robustness.json` | 10 metrics (M1-M10), 7 properties (P1-P7), 24 robustness cases (R1-R24), 4 hang causes (H1-H4), install rehearsal (8 steps), 8 report sections (S1-S8) |
| Watchdog design | `docs/research/scouter-watchdog-design.md` | |
| Run-3 stall root cause | `docs/research/scouter-bench-run3-stall.md` | |
| Vault dossiers | `Knowledge/Research - {darktable editing universe, the universe of photographic looks, what photographers ask for, measuring photo edits and release testing} 2026-09-26` | narrative and sources for the JSON |

Scouter today, which is what the gauntlet tests first (row 433: "gauntlet first against today's vocabulary"):
- `scouter/core/vocab.py`: 24 params over 6 modules (exposure 2, sigmoid 2, temperature 2, colorbalancergb 13, filmicrgb 3, colorzones 2). 22 are `live`, `temperature.tint` is `absolute_only`, and `colorzones.curves` is `unreachable`.
- `scouter/core/phrases.py`: 27 phrase keys, 89 synonyms, and 11 intensity words (0.5x or 2x) plus explicit `by N <unit>`.
- `scouter/mcp/schemas.py`: 45 MCP tools. The CLI exposes the same registry verbs (`scouter <group> <verb>`).
- `scouter/core/ppm.py`: stdlib P6 parser, per-channel and luminance histograms, percentiles, clip percentages, grey-world `wb_ratio_rg/bg`, and a scene-relative `sharpness`.

## 1. Goal and release bar

**Goal.** A Claude Code user on macOS with darktable 5.4 installs Scouter, registers the MCP server, and edits real photographs (one frame, a whole job, or a named look) by talking. Every request ends in exactly one of three ways: a verified edit, an honest refusal, or a typed error. It never crashes, never hangs without a typed answer, and never says "done" when nothing happened.

**Release bar (row 433).** All six must hold on one full run of one commit:
1. **Zero crashes.** No new darktable or Python crash report in `~/Library/Logs/DiagnosticReports` (`.ips`, `.crash`, `.hang` whose process is `darktable` or `python*`) with a timestamp inside the run. Also zero MCP-server deaths (the stdio server process exits or breaks the pipe).
2. **Zero silent failures** in any layer and at any tier (definition below).
3. **100% of P0** cases pass. Partial does not count.
4. **Tolerance on looks and edge cases, with every miss listed.** P1 and P2 use the thresholds in the tier rules below. Every non-pass appears in the report with a repro command.
5. **Honest refusals pass.** A request outside today's vocabulary passes when Scouter refuses it honestly (§5.4).
6. **Scope:** Claude Code through MCP, plus the CLI, on macOS with darktable 5.4.x, including one fresh-machine install rehearsal (§3.7).

**Silent failure (definition).** A step is a silent failure when Scouter reports success and the world did not change the way it said. "Reports success" means one of: an MCP envelope with `ok: true`; a CLI exit code of 0 whose outcome is not `noop`/`dry_run`; or, in agent mode, a final assistant message that claims the edit was made. "The world did not change" means one of:
- (a) a write verb left the decoded XMP history of the target module and param unchanged, beyond the M1 tolerance;
- (b) the change landed on a different module or param than the one the envelope reports;
- (c) the case has a stat check, and the measured stat moved against the expected direction, or moved less than the metric's noise floor (§5.2);
- (d) a bulk verb reported N frames done, but fewer than N sidecars changed or fewer than N export files verify;
- (e) `darkroom_undo_last` reported ok, but the params were not restored within tolerance;
- (f) an answer of `noop: true` while the sidecar did change, or an answer of `noop: true` for a request that needed a change.

A noop answer is not a silent failure when the measured state equals the reported state. The benchmark's `reported_ok_unverified` outcome is renamed `silent_failure`, and the old label is kept as an alias when old results are read.

**Tiers.** The generator assigns one tier to each case. The rules:

| Tier | What it is | Members | Gate |
|---|---|---|---|
| **P0** | What a new user hits in the first hour | the 100 P0 scenario requests (§3.2), in both tool mode and agent mode; the coverage core (set / adjust / say bare / undo for every live param, on both main bodies); install rehearsal; the user-guide walkthrough; MCP/CLI parity for the verbs the guide uses; robustness R1, R3, R12, R17 and H1-H4 | 100% pass. Any non-pass blocks release. |
| **P1** | The rest of the real-world surface | all other scenarios; coverage boundaries and properties; bulk; the other robustness cases; looks with a `documented` or `community-consensus` target; parity for the other verbs | Pass rate at least 95% per layer, with partial scored 0.5. Every miss is listed with a repro and a triage verdict (fix / roadmap / accepted). |
| **P2** | Estimates and stretch | looks targets marked `inferred` (direction only); agent-mode looks; the offline resolver sweep | No rate gate. Every miss is listed. |

Invariants at every tier: zero crashes, zero silent failures, and zero untyped hangs. Every case either answers within its hard cap or is recorded as a typed `blocked`.

## 2. Architecture

```
taxonomy (4 JSONs + check_map.json)
   -> generator  -> runs/<stamp>/cases.jsonl + generation.md
   -> runner     -> runs/<stamp>/cases/<case_id>.json, log.md, progress.json
   -> evaluator  -> verdict inside each case JSON
   -> report     -> report.md, report.json, contact-sheet.html
   -> fix loop   -> finding -> fix branch -> rerun the affected layer
```

### 2.1 Taxonomy (`tests/gauntlet/taxonomy.py`)

- Loads the four JSONs. Validates their shape, and asserts the counts in §0, so a silent edit to the data fails loudly. Exposes typed accessors.
- The JSON files are never hand-edited. `looks.json` is rebuilt from `build_looks*.py` and the taxonomy from `build_taxonomy.py`, as the dossiers require.
- **New file `tests/gauntlet/data/check_map.json`** is the only hand-written data. Its checked-in contents:
  - `stat_rules`: an ordered list of `{match (lower-case substring or regex on requests[].expected.check.stat_or_param), metric (M1-M10 or "sidecar"), region, capability}`. The corpus has 223 distinct stat strings. These rules cover them by keyword ("luminance"/"L*" -> M2, "temperature"/"cast" -> M4, "tint" -> M4 green-magenta, "contrast" -> M6, "noise" -> M7, "acutance"/"sharp" -> M8, "saturation"/"chroma" -> M5). Stats Scouter cannot measure with ppm (spot blob count, horizon angle, line curvature, CA fringe width, corner luminance for vignette) map to `metric: "sidecar"` and `capability: none`.
  - `direction_rules`: normalises the 59 direction strings to `{sign: +1|-1|0, kind: delta|toward_zero|to_zero|capped|step_back|multi}`. For example, "increase, limited" becomes `{+1, capped}`, "decrease toward 0" becomes `{-1, toward_zero}`, "step back by 1" becomes `{step_back}`, and "multi" plus any string with a comma is split into several checks.
  - `capabilities`: maps each capability id to a Scouter phrase key or vocab row. `shadow_luminance_up` -> `lift_shadows`, `warmer` -> `warmer`, `contrast_up` -> `more_contrast`, `tint_magenta` -> the absolute `darkroom_set temperature tint`, and so on. Every capability not listed is out of vocabulary.
  - `role_for_problem`: maps each of the 21 `problem` values to a frame role (§4.2).
- The generator fails when any P0 or P1 case falls through to "unmapped". P2 cases that fall through are listed in `generation.md`.

### 2.2 Generator (`tests/gauntlet/generate.py`)

- It is pure and deterministic. It needs no darktable, and it runs in seconds.
- It expands taxonomy × corpus roles × magnitudes × paraphrases × follow-ups into `cases.jsonl`.
- Case ids are stable: `<layer>-<source-id>-<body>-<variant>`, for example `scn-r0001-olympus-tool` or `cov-exposure.exposure-sony_a7-set_hi`. A rerun of the same data produces byte-identical output, so the fix loop can rerun exactly the failed ids.

Case schema (`scouter-gauntlet-case/1`):

```json
{"id": "scn-r0001-olympus-tool", "layer": "scenarios", "tier": "P0",
 "source": {"file": "requests.json", "id": "r0001", "cluster": "c01"},
 "body": "olympus", "frame_role": "dark", "mode": "tool",
 "steps": [{"tool": "darkroom_say", "args": {"image": "{frame:dark}", "phrase": "brighter a lot"}, "budget_s": 8}],
 "followups": [{"sentence": "A little more than that, still looks dim.", "steps": [...], "expect": {...}}],
 "expect": {"kind": "edit|refuse|workflow|typed_error",
            "checks": [{"metric": "M2", "stat": "mean_L", "region": "frame", "sign": 1, "kind": "delta"}],
            "sidecar": {"module": "colorbalancergb", "changed": true}},
 "repro": "python -m tests.gauntlet run --case scn-r0001-olympus-tool --stamp <stamp>"}
```

In **tool mode**, the generator writes the tool calls itself:
- In-vocabulary requests go to `darkroom_say` with the capability's phrase plus the request's magnitude word. "a full stop" becomes the explicit `by 1 ev`.
- Out-of-vocabulary requests go to `darkroom_say` with the request's nearest taxonomy phrase for its first module, for example "sharpen it". They expect a typed `usage` refusal.
- Workflow requests use the matching verb (`darkroom_undo_last`, `preset_save`, `export_run`, …).

In **agent mode**, the case carries only the sentence, the frame, and the checks (§3.2).

Expected counts per layer are in §3. The generator prints them in `generation.md` and fails when any layer is off by more than 2% from its formula. That catches a data change nobody meant to make.

### 2.3 Runner (`tests/gauntlet/run.py`)

**Reuse, do not reinvent.**
- The benchmark harness lives on the unmerged branch `bench/editor` (10 commits ahead of main, worktree `.worktrees/bench2`). Plan task 1 merges its `tests/benchmark/` into main. From `run_editor_benchmark.py` the gauntlet then imports `McpClient`, `snapshot_xmp`, `compact_snapshot`, the `evaluate()` assertion types, `score_task`, `summarise`, `markdown`, `stage_body`'s read-only copy, and `git_info`.
- From the acceptance driver (`scratchpad/acceptance_driver.py`, copied into `tests/acceptance/` by plan task 1 because the scratchpad is reaped) it takes `log()`, `record()`, `crash_count()`, `base_env()`/`acc_env()` isolation, and the `cli()` wrapper.
- `DarktableHost.launch/alive/quit` moves into the product module `scouter/core/dtprocess.py` (§6), and both drivers import it from there.

**Sessions.**
- A session is one darktable process, one `--library` (fresh per session, under the run dir), one body, one long-lived MCP stdio server with isolated `SCOUTER_CONFIG`, `SCOUTER_CACHE` and `OBSIDIAN_VAULT_PATH`, and at most 50 cases or 20 minutes, whichever comes first.
- Only one darktable runs at a time.
- The runner refuses to start when a darktable it did not launch is running. It uses `dtprocess.owned_pid()` for this, so a Pratik session is never touched.

**Launch.**
1. `dtprocess.launch(frame, library)`.
2. Wait until `darktable_mcp.diag` answers `gui_ready: true` (row 434). The cap is 30 s. The runner never treats `view.current` as proof of readiness (stall write-up §5).
3. A launch that is not ready after 30 s is `blocked:launch`, and the runner relaunches once.

**Case isolation.** Before each case, `darkroom_reset` runs on every module the previous case touched. The snapshot must then equal the session baseline snapshot, or the session is marked contaminated and the runner relaunches it on a fresh copy of the frame. The before-snapshot and before-stats of a case are always taken fresh, so every check is a delta against that case's own starting point.

**Per case.**
1. before snapshot (`history_read` + `snapshot_xmp`) and before stats (only when a check needs them);
2. the steps, each with its `budget_s`;
3. after snapshot and after stats;
4. the evaluator;
5. write `cases/<id>.json` atomically: write to `.tmp`, then rename.

**Timeouts and the watchdog.**
- Each step has a hard cap of `budget_s + 30 s`, and each case has a hard cap equal to the sum of its step caps.
- On a bridge timeout, the runner calls `dtbridge.health()` (§6).
  - `alive`: record `over_budget` or the typed error and continue.
  - `starting`: wait 3 s and retry the step once.
  - `unresponsive`: record the case as `blocked` with hang evidence (any `darktable*.hang` newer than the case start). Quit with the shortened ladder: skip osascript and go straight to SIGTERM, because the GUI is known not to answer. Relaunch, and rerun the case once. The bench's `_setup_hung` logic does exactly this.
  - A second hang on the same case is `failed:hang`, and the session moves on.
- `gui_not_ready` and `bridge_timeout` are transient typed answers, not refusals.
- Product `auto_heal` stays OFF during the gauntlet, except in the robustness cases that test it. A heal would hide the bug being measured, which is the watchdog design's own rule.

**Crash guard.** `crash_count()` runs before and after every case. When the count grows, the new report is attached to the case, which fails, and the global crash count increments. After a crash the layer stops, the run goes on to the next layer, and the morning fix loop takes it from there. One crash does not burn the night.

**Resumable.** `run --stamp <stamp> --resume` skips every id that already has a `cases/<id>.json`. `run --case <id>` and `run --layer <name> --only-failed` exist for the fix loop.

**Timing.** Every step records `wall_s`, the envelope's `ms`, and whether the step bounced (the `plan` field). This feeds the S5 percentiles.

**Agent-mode runner (`tests/gauntlet/agent.py`).** Each case, or each cluster when it has follow-ups, runs as:

```
claude -p --model sonnet --output-format stream-json --mcp-config <run>/mcp.json --allowedTools "mcp__scouter__*"
```

- The prompt is the sentence plus "The photo open in darktable is <path>."
- Follow-ups go into the same conversation through `--resume <session_id>`.
- `mcp.json` registers only Scouter, with the session's isolated env.
- The runner parses the tool calls and the final text from the stream, then evaluates them exactly like tool mode, plus the claim check in §5.4.
- Budget rule: the runner starts an agent batch only after Pratik has confirmed weekly usage is under 60%, because only he can see it (memory rule: Capsule comes before Scouter, and 80% stops everything). It asks through `log.md` and the progress artifact and does not guess.

### 2.4 Evaluator (`tests/gauntlet/evaluate.py`, `color.py`, `chart.py`)

- **Sidecar diff (M1).** Reuses `snapshot_xmp`. `SNAPSHOT_OPS` is extended from 8 ops to every op with a decoder, and the extension must not break existing decoders. The evaluator asserts the target param changed (or not) within tolerance, and that no other decoded param moved. When one did, the case records `collateral` (a partial, listed).
- **Decoded params.** Absolute values come from the decoded XMP, never from the envelope's claim, for set, adjust, phrase and undo.
- **Stats on exported previews.** The preview is `scout_preview` or `export_run` at 1024 px long edge, parsed by `ppm.py`. The gauntlet adds its own stdlib helpers in `color.py`:
  - sRGB decode to linear, and EV = `log2(mean_linear_after / mean_linear_before)`;
  - Rec.709 relative luminance to L*;
  - sRGB -> XYZ (D65) -> CIELAB, with C\* and h;
  - regions: the whole frame, the centre 50%, the four 10% corners, and (on the chart) the known patch rectangles;
  - CIEDE2000, per Sharma, Wu and Dalal 2005, validated against their 34 published test pairs in a unit test.
- **ΔE2000 against a rendered chart (M9, M10).** The chart is described in §4.3. The untouched chart render of the run is compared per patch against the committed golden stats (P7), and every look and colour check on the chart is measured against the run's own untouched render.
- **Direction-only vs numeric targets.** A check is numeric only when its source says so: a requests `check` with an explicit amount, a vocab set or adjust, or a look target whose `target_confidence` is `documented` or `community-consensus`. Every other check is direction-only: the sign must be right and the size must exceed the noise floor.
- **Property checks.** These run on coverage and scenario cases the generator tagged for them:
  - **P1 repeat:** an absolute set applied twice leaves the second step's sidecar unchanged. A relative adjust applied twice gives 2× the delta ±10%.
  - **P2 commutativity:** on six independent-axis pairs the final params are equal in either order.
  - **P3 undo inverts:** the params are restored within M1 tolerance, and on the chart the ΔE00 is under 2.3.
  - **P4 monotonic magnitudes:** "a touch" < bare < "a lot" in measured |Δ|, strictly.
  - **P5 round trip:** after "brighter" then "darker" (or any opposed pair), luminance stats are back within the M2 floor.
  - **P6 geometry is colour-neutral:** refusal-only today, because geometry is out of vocabulary. It is kept for when geometry lands.
  - **P7 golden chart:** the untouched chart render stays within ΔE00 < 2.3 of the golden.

### 2.5 Report (`tests/gauntlet/report.py`)

Outputs: `runs/<stamp>/report.md` and `report.json`, plus `contact-sheet.html`. The contact sheet is a plain HTML grid of before and after JPEG previews with a caption on each tile. It is stdlib only and does no image compositing.

The sections are S1-S8 from the measurement JSON, in this order:
- **S8 sign-off block first:** crashes 0, silent failures 0, P0 100%, and P1 at or above its gate per layer. It also records "contact sheet reviewed", the one box Pratik ticks by hand.
- **S1 headline.**
- **S2 per-layer and per-category pass rates.** This reuses `summarise()` and `markdown()`.
- **S3 every non-pass,** with its id, sentence, calls, outcome, measured vs expected values, and the one-line `repro`.
- **S4 contact sheet of looks,** plus every scenario that has a stat check.
- **S5 timing percentiles:** p50, p90 and p99 per tool and per layer. `over_budget` is listed separately from fail.
- **S6 crash count and hang count,** with links to the reports.
- **S7 honest refusals as the vocabulary roadmap,** ranked by demand (§7).
- **P0 served-rate line:** the share of P0 cases that were verified edits rather than honest refusals. This is a headline number, not a gate (Q3).

### 2.6 Fix loop

1. **Triage.** Each non-pass gets exactly one class:
   - `product_bug` (Scouter or the plugin);
   - `harness_bug` (the generator, the runner, or the check map);
   - `taxonomy_error` (wrong expectation in the data: fix the builder script, never the JSON by hand);
   - `vocab_gap` (goes to the roadmap, not a fix);
   - `upstream` (darktable itself, filed with a repro);
   - `accepted` (a documented limit, which needs Pratik's OK when it is P0).
2. **Fix.** `product_bug` and `harness_bug` go to a subagent under the usual build rules. Each gets its own branch and worktree. The failing case is turned into a failing unit or Lua test first (TDD). Fable reviews, then it goes through the merge gate. No `git stash` (row 434 incident).
3. **Rerun.** After each merge, rerun the affected layer's failed ids, plus that layer's whole P0 slice, plus the robustness P0 slice. A product change that touches the bridge, the plugin or commit also reruns the coverage core. The fix counts only when the rerun passes.
4. **Report.** The final release report lists every fix: finding, branch, commit, and the rerun result.

## 3. Layers

Counts are what the generator must produce, per §2.2. "Bodies" means `olympus` (E-M10 III, 39 ORF) and `sony_a7` (A7, ARW). `sony_nex5n` (NEX-5N, ARW) gets only the WB-table slice.

| # | Layer | Cases | Mode |
|---|---|---|---|
| 0 | Offline resolver sweep | 2,314 | pure, no darktable |
| 1 | Coverage | 1,316 | tool |
| 2 | Scenarios | 861 (701 tool + 160 agent) | tool + agent |
| 3 | Bulk | 40 | tool |
| 4 | Looks | 469 | tool |
| 5 | Robustness | 32 | tool + fault injection |
| 6 | MCP/CLI parity | 90 | tool |
| 7 | Install rehearsal | 10 | CLI |
| 8 | Learning mode + guide walkthrough | 35 | tool |
| | **Total** | **5,167** (2,853 live, 2,314 offline) | |

### 3.0 Offline resolver sweep (P2, seconds)

This is `vocab_resolve` and the phrase grammar, pure, with no darktable:
- all 378 taxonomy phrases plus all 601 request sentences (979 cases). Each case expects either a resolution to the capability the check map gives it, or a `usage` refusal that names the nearest phrases. A resolution to the wrong capability is a fail.
- every synonym (89) × {bare, each of the 11 intensity words, 3 explicit amounts} = 89 × 15 = **1,335**. Each asserts the parsed intensity and moves.

It also serves as the cheap canary in CI: `pytest -m gauntlet_offline`.

### 3.1 Coverage (P0 core, P1 rest): 1,316 cases

The same set runs on each main body (648 per body):

| Group | Formula | Per body |
|---|---|---|
| set, per live param (22) | 6 values: lo, hi, lo−step, hi+step (expect a clamp reported in the envelope, or a typed range refusal; a silent clamp is a silent failure), neutral/default, mid | 132 |
| adjust ±1, ±3 notches | 22 × 4 | 88 |
| adjust overflow from hi−step by +3 | 22 | 22 |
| nudge ±1 (darktable step) | 22 × 2 | 44 |
| undo after set, undo after adjust (P3) | 22 × 2 | 44 |
| repeat set, repeat adjust (P1) | 22 × 2 | 44 |
| hue wrap (350° + 20° -> 10°), 3 hue params × 2 | | 6 |
| reset per module, enable/disable per module | 6 + 12 | 18 |
| per-param P0 get/read-back is inside each set case | | 0 |
| `tint` (absolute_only): set ×4; adjust -> `value_unknown`; "more magenta" -> `value_unknown` | | 6 |
| `colorzones.curves` (unreachable): set and adjust -> typed refusal | | 2 |
| phrases: 27 keys × {bare, "a touch", "a lot", explicit `by N unit`} | | 108 |
| synonyms bare | 89 | 89 |
| P4 ladder per key (27), P5 opposed pairs (12), P2 commutative pairs (6) | | 45 |
| **per body** | | **648** |

Two main bodies give 1,296 cases. The NEX-5N WB slice adds 20: temperature set ×6, adjust ×4, undo ×2, and warmer/cooler × 4 forms. Total **1,316**.

**P0 core** is, per body, 22 live params × {set lo, set hi, set mid, adjust +1, adjust −1, undo after set} plus the 27 phrase keys bare: 159 per body, 318 in total. Everything else in coverage is P1.

Frame: the `hero` role (§4.2). Checks: M1 on every case. M2, M4, M5 or M6 by concept on the phrase and property cases (the concept to metric map is in `check_map.json`).

### 3.2 Scenarios (P0 top 100, P1 rest): 861 cases

**Source.** `requests.json`: 601 requests, including their follow-ups (24) as extra steps in the same case.

**Frame.** `role_for_problem[request.problem]` on the case's body. Genre is carried as metadata only, because the corpus is two street/travel bodies and a check is a delta, not a genre-specific truth (§4, risk 2).

**Also covered: the 12 taxonomy genre workflows.** They are not separate cases. Each workflow step whose module has a capability appears in the scenario set through the requests that name the same problem. `generation.md` prints the coverage of workflow steps: which steps are served today and which are refusals.

**Tool mode: 701 cases.**
- Every request runs once. Its body alternates by request-id parity (odd -> olympus, even -> sony_a7). That is 601 cases.
- The 100 P0 requests also run on the other body, for another 100 cases.

**Agent mode: 160 cases.** The 100 P0 requests plus the 60 impossible ones, on olympus. Agent-mode P0 cases are P0. Agent-mode impossible cases are P1.

**P0 selection rule (deterministic).** This replaces "the top 100 by frequency". The corpus has no frequency field, and a plain top-100 by demand collapses onto 4 problems.
1. Take the non-impossible requests (541). Their demand key is `problem`, or `intent + ":" + modules[0]` when `problem` is `n/a`.
2. Demand of a key = the number of requests with that key (24 keys; the largest is `flat/low contrast` with 71).
3. Quota per key = `round(100 × demand / 541)`, at least 1. Use largest-remainder rounding so the quotas sum to exactly 100.
4. Within a key, take requests in id order, one per cluster before any second from the same cluster.
5. Genre floor: each of the 14 genres gets at least 3. When a genre is short, swap in its highest-demand unpicked request for the lowest-ranked pick of the most over-represented genre.

Dry run today, from the data: 26 corrective, 16 colour, 13 tonal, 12 geometry, 11 workflow, 11 detail, 6 stylistic and 4 local. Every genre has 3 or more. By today's module match, about 8 are fully in vocabulary, about 29 partly, and about 62 out. So most of P0 passes only as honest refusals. That is the served-rate headline and Q3.

**Expectations by class (from the check map's capability):**
- `in`: `expect.kind = edit`. A refusal is a `wrong_refusal` (fail).
- `partial`: an edit of the served part, plus an honest statement of what was not done. In tool mode this means each unserved move is a typed refusal. In agent mode it means the final text names it.
- `out`: `refuse`. The request must get an honest refusal, and a substitute edit passes only when it is labelled as one.
- `impossible`: `refuse`.
- `workflow`: the verb's own check. For example, undo means `history_end` goes down by 1 and the params equal the previous snapshot. An "export for IG" request produces a file that `export_verify` accepts.

### 3.3 Bulk (P1): 40 cases

- 9 flows (`taxonomy.bulk`) × 2 bodies × 2 sizes, where the sizes are a 7-frame card and a 120-frame job. That is 36 cases.
- Plus 4 crash-mid-job cases on olympus, 120 frames each: `job_run`, `export_run`, `style_apply_set` and `library_import`. The runner SIGKILLs darktable after the job reports frame 50. It then relaunches, re-runs the same verb with the same job, and asserts:
  - every frame processed exactly once, with no duplicate history entries;
  - no frame lost, so 120 sidecars or exports verify;
  - the job journal says `resumed`.
- **Flow to verb mapping:**
  - `import_and_cull` -> `library_import`, `cull_propose`, `cull_apply`;
  - `hero_then_propagate` -> `preset_save`, `preset_apply_job`;
  - `copy_paste_history` -> `style_extract`, `style_apply_set`;
  - `styles` -> `style_extract`, `style_apply`;
  - `module_presets` -> `preset_save`, `preset_list`, `preset_apply`;
  - `batch_export_profiles` -> `export_run` with two profiles, then `export_verify`;
  - `sidecar_handling` -> `library_verify`, `history_read --source auto`, `library_relocate`;
  - `outlier_handling` -> `outliers_list`;
  - `time_lapse_deflicker` has no capability, so it expects an honest refusal.
- **Volume by duplication.** The 120-frame card is built from the body's EditingTrial frames. Each is copied round-robin as `DUP####_<orig>` under the scratch card (§4). Duplicates are exact copies, so cull and outlier assertions treat identical frames as ties. They do not assert a unique winner.
- **Checks:**
  - the per-frame sidecar diff is sampled on every frame (M1, cheap: XMP only);
  - M2 stats on 10 evenly spaced frames;
  - the frame count and the per-frame timing (S5), with budget 6 s/frame;
  - `job.run` has a no-auto-retry rule (watchdog design §2.3).

### 3.4 Looks (P1 documented and community targets, P2 inferred): 469 cases

**Cases.**
- 234 looks on the synthetic chart, with full target checks.
- 234 looks on one real frame, the `hero` role. Bodies alternate by look index, and these cases are direction-only.
- 1 golden-chart case (P7), which runs first in the layer.

**Look plan (tool mode).** The generator derives the Scouter moves for each look from its `targets` through today's vocabulary:
- `tone.shadows_ev`, `mids_ev` and `highlights_ev` -> colour balance rgb `shadows_luminance`, `midtones_luminance` and `highlights_luminance`;
- `tone.contrast` -> sigmoid `contrast`;
- `saturation_delta` -> `global_chroma`;
- `split_toning` -> shadows and highlights `hue` and `chroma`;
- `grey_cast` -> temperature (b\*) and tint (a\*, absolute);
- `black_lift` -> exposure `black`;
- `monochrome` (saturation −1) -> `global_saturation` at its lower bound. It is flagged approximate, because the real route is colour calibration gray, which is out of vocabulary.

Targets with no route today are recorded as `gap`, never as fail: `grain`, `vignette`, `clarity`, `halation`, `white_point`, `hue_shifts` (colour zones curves are unreachable), `bw_response`, and LUT-first looks. A look's score counts only its reachable targets. `gap` targets feed the roadmap.

The move magnitudes come from a linear per-param calibration. It is measured once per run on the chart, as Δmetric per notch for each of the 22 live params (this is part of the golden-chart case). The look plan is therefore reproducible and never hand-tuned.

**Checks.**
- On the chart:
  - tone at the three grey patches (EV);
  - saturation as the mean C\* of the 18 chromatic patches;
  - `grey_cast` a\*, b\* on the mid-grey patch;
  - `black_lift` as L\* of the darkest ramp patch.
- The tolerances are `looks.json` `conventions.suggested_tolerances` for the target's confidence. `inferred` targets are sign-only.
- On the real frame: the sign of ΔL\*, ΔC\* and the grey-world ratio change for the same targets.

**Look verdict.**
- pass: every reachable target is within tolerance, or has the right sign when it is sign-only;
- partial: at least 60% of reachable targets pass, and none moved the wrong way beyond tolerance;
- fail: otherwise.

**Gate.** On the chart, at least 90% per-target pass for documented and community-consensus targets, and at least 95% correct sign for inferred targets. Every miss is listed with its contact-sheet tile.

**Agent-mode looks** are not in the release run. After the release run, a P2 sample runs: the top 30 by `popularity_rank_2026_summer` plus one per family. Its purpose is to measure how the model translates "make it Portra 400". It is budget-gated (§2.3).

**Fuji recipe calibration sub-project (P1, after release, not in the counts).**
- Render the synthetic chart through the community darktable ports: the jssfr AgX styles (2026-01) and the bastibe/Sowerby LUTs through `lut3d`.
- Measure the same targets, and promote `inferred` to `documented` in `build_looks*.py` where they agree.
- This needs a network download and licence checks (Q5). It is deterministic scripting, not model work.

### 3.5 Robustness (P0: R1, R3, R12, R17, H1-H4; P1: the rest): 32 cases

- **R1-R24** from the JSON, one case each, 24 in total, using its `how_to_simulate`. Concrete harness choices for the non-obvious ones:
  - R2 crashed mid-call: SIGKILL darktable during a `darkroom_say`;
  - R4 stale state: relaunch, then call with the old session's job id;
  - R6 read-only sidecar: `chmod 444`;
  - R8 torn read: truncate a copy of the XMP to half and point `history_read` at it;
  - R9 library lock: a second `darktable --library` on the same db (owned by the runner);
  - R11 long path: a nested dir component of 255 bytes;
  - R14 huge job: shares the 120-frame card from bulk and adds a timeout on one frame;
  - R16 two clients: two MCP servers issue interleaved `darkroom_say` calls on the same image;
  - R18 disk nearly full: export to a 50 MB `hdiutil` disk image filled to 49 MB, detached afterwards;
  - R20 clock skew: set the XMP mtime into the future with `os.utime`, without touching the system clock;
  - R21 version mismatch: a fake diag answer through the bridge test double, host-free;
  - R22 plugin not loaded: `darkroom_set` on an iop that is absent;
  - R23 broken luarc: a temp luarc in the rehearsal configdir only;
  - R24 wrong profile: a Sony frame with the Olympus WB table forced.
- **H1-H4 plus the watchdog: 8 cases.**
  - H1 cold-launch gtk call: `history_commit` 1 s after launch must answer within budget, either the row-432 noop or `gui_not_ready`, never a hang;
  - H2 modal dialog: a dialog left open through a plugin test hook, if one exists, else skipped with a reason;
  - H3 pipe busy: 10 back-to-back `darkroom_say` calls on a full-resolution frame;
  - H4 view bounces: 50 alternating commits;
  - ping `alive` / `starting` / `unresponsive` (a wedged stub worker, as in the watchdog design §4 host test);
  - `auto_heal` on: exactly one heal and one retry, with a telemetry row;
  - heal cap reached: a typed `darktable_unresponsive`;
  - foreign pid: `refused_not_ours` and no signal sent, checked by pid spy.

  That is 4 + 4 = 8 cases.

Every robustness case's expectation is a typed error code, or a queue or progress answer for the capacity cases R14 and R16. It is never a hang or a generic failure.

### 3.6 MCP/CLI parity (P0 for the guide's verbs, P1 rest): 90 cases

- 45 MCP tools × 2 (one happy path and one error path).
- Each case runs the same arguments through the MCP stdio server and through `scouter <group> <verb> --json` on identical staged state. Write verbs get a fresh copy per side.
- The two envelopes are compared after normalising: `ok`, `outcome`, `error.code`, the `data` keys, and value equality for deterministic fields. Timings and ids are dropped.
- The existing `tests/test_layer_parity.py` stays as the unit-level guard. This layer is the live one.
- One more check sits inside the rehearsal and is not counted here: `claude mcp list` in the rehearsal HOME shows `scouter` pointing at `python -m scouter.mcp.server`.

### 3.7 Install rehearsal (P0): 10 cases

The 8 steps from `install_rehearsal`, plus an idempotent re-run of `scouter setup`, plus the teardown leak check. That makes 10 cases, each recorded like any other case.

**Environment.**
- The root is `~/Pictures/ScouterScratch/gauntlet/<stamp>/install/{home,config,cache}`.
- Every subprocess gets `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `SCOUTER_CONFIG`, `SCOUTER_CACHE` and darktable `--configdir` pointing inside the root.
- The Claude Code check runs `claude mcp add` and `claude mcp list` with `HOME` set to the rehearsal home, so Pratik's real `~/.claude.json` is never written.

**Leak check.**
- Precondition: no darktable the runner did not launch is running (the runner already refuses to start otherwise).
- Record a stamp file at the rehearsal start. Afterwards, `find ~/.config/darktable ~/Library/Application\ Support/darktable ~/.config/scouter -newer <stamp-file>` must return nothing. With no user darktable running, any hit is a leak.
- `~/.claude.json` is written constantly by Pratik's own Claude Code sessions, so mtime proves nothing there. Instead, the `mcpServers` object in it is hashed before and after, and the hashes must match.

This is not a true fresh macOS account. See §9 out of scope.

### 3.8 Learning mode + user guide walkthrough (P0): 35 cases

- **Walkthrough: 20 cases.** Every command in the vault user guide (`Knowledge/Scouter - User guide 2026-09-26`) and in the README's "A hero edit", "A hero session" and "Educative mode" sections runs literally, in order, against a staged hero frame. Each case asserts its documented result: the output line shape, the refusal text for "What refuses, and why", and the files it produces. When a document command drifts from the product, that is a failure of the doc or of the product, and triage decides which.
- **Educative levels: 15 cases.** Levels `off`, `brief` and `full` × 5 moves across 3 concepts, in one session per level. The asserted rules come from the README:
  - `off` adds no explanation;
  - `brief` adds exactly one sentence per move, with before and after values;
  - `full` explains each concept once per session and never twice.

## 4. Image corpus and scratch layout

### 4.1 Rules

- **EditingTrial originals are never opened, imported, edited or exported.**
  - The runner refuses any path argument whose realpath is under `~/Pictures/EditingTrial`, and the generator validates the same rule.
  - Before and after the run, `sha256` of all 55 originals is compared, and the result is printed in the S8 block.
- Copies are made with `_copy_readonly_source` (bench), which reads the original and writes a `0644` copy under the scratch root.
- Bench frames that already exist under `ScouterScratch` (bench-runs, acceptance) are reused only by copying them again. Scratch roots are never shared between runs.

### 4.2 Frame roles

A one-time **corpus survey** (`tests/gauntlet/corpus.py survey`) renders every original's default pipeline at 512 px through `darktable-cli`. It runs on copies, in a throwaway configdir, outside any session. It then stores `corpus.json` with each frame's `ppm.stats`, EXIF ISO, and body.

Roles are picked per body by these rules:

| Role | Rule | Used by problems |
|---|---|---|
| `dark` | lowest mean L\* (the bench's P5240151 for olympus) | underexposed |
| `bright` | highest p99 without clipping | harsh shadows, backlit |
| `blown` | highest `clipped_high_pct` (the bench's P5240140) | overexposed/blown sky |
| `hero` | the bench's hero frame | everything else, coverage, looks |
| `flat` | lowest L\* spread (p95−p5) | flat/low contrast, haze |
| `warm_cast` | highest `wb_ratio_rg` | colour cast orange sodium, mixed light |
| `cool_cast` | lowest `wb_ratio_rg` | colour cast blue shade |
| `noisy` | highest ISO | high ISO noise |
| `neutral` | `wb_ratio_rg` and `wb_ratio_bg` closest to 1 | colour cast green fluorescent, skin tone off |

The bench's existing `frames` block for olympus and sony wins where it already names a role. Body `sony_a7` uses the A7 frames and `sony_nex5n` uses the NEX-5N frames, and the `sony/` subfolder holds both. Both WB tables exist since acceptance run 2.

### 4.3 Synthetic chart (feasible with the stdlib)

`tests/gauntlet/chart.py` writes `chart-v1.pfm`, a 1536×1152 linear Rec.709 float image. PFM is a trivial header plus little-endian float32 data. It contains:
- the 24 ColorChecker patches, using the published post-2009 sRGB/Lab reference values as constants and converted to linear;
- an 11-step grey ramp at 18%·2^k for k = −5..+5, which includes the looks' 18%·2^−4, 18% and 18%·2^2 tone points.

Each patch is 128×128 on a mid-grey surround. Its rectangle is known, and the sampler reads the centre 60% of each patch. darktable imports PFM as a non-raw image with the scene-referred defaults. Every look delta is measured against the untouched chart render of the same run, as `looks.json` requires: the baseline is the default render of the same input.

The PFM import and render are verified by plan task 2's first host step. If darktable 5.4.1 rejects the PFM or clips it, the fallback is a 16-bit TIFF written with the stdlib (an uncompressed baseline TIFF is about 60 lines), with the same layout.

`golden/chart-v1.stats.json` (per-patch Lab) is committed after the first clean run. P7 compares against it, and it is re-baselined only by an explicit `--rebaseline` reviewed by Fable, never automatically.

### 4.4 Scratch layout

```
~/Pictures/ScouterScratch/gauntlet/<stamp>/
  corpus/<body>/<role>/<file>            # 0644 copies
  chart/chart-v1.pfm
  bulk/<flow>/<body>/<size>/card/DCIM/100GAUNT/DUP####_<orig>
  sessions/<layer>/<session>/{library.db, cache/, vault/, mcp.json}
  install/{home,config,cache}
  cases/<case_id>.json                    # one per case, atomic
  previews/<case_id>/{before,after}.jpg
  cases.jsonl  generation.md  log.md  progress.json
  report.md  report.json  contact-sheet.html
```

The run-level `config.json` follows `stage_body`'s pattern, with the backup target and `library_root` under the stamp. Plan the disk space at about 12 GB per full run: 120-frame duplicates × 2 bodies × 9 flows at about 20 MB each is the bulk of it. Each bulk case deletes its duplicates once it has verified them, so the peak is about 3 GB.

## 5. Measurement contract

### 5.1 Which metric for which check

| Check | Metric | Tolerance |
|---|---|---|
| set value landed | M1 decoded XMP | EV ±0.02; K ±50 or ±1%, whichever is larger; ratio ±0.01; % rows ±0.5 pp; hue ±1° (wrapped); booleans exact |
| adjust by n notches | M1 | Δ = n × step ± 10% of step. At a bound, the value equals the bound and the envelope reports the clamp. |
| phrase magnitude | M1 + P4 | explicit `by N`: M1 tolerance; intensity words: strict ordering only |
| brighter / darker, shadow / highlight luminance | M2 on a region (EV from linear means; L\* for region deltas) | direction beyond the floor; explicit EV amounts ±0.15 EV |
| clipping, blacks | M3 percentiles and clip % | direction only |
| warmer / cooler, casts | M4 grey-world ratio (direction only on real frames); M9 a\*, b\* on the chart grey | chart: ±2 a\*/b\* units when numeric |
| saturation / chroma / vibrance | M5 mean C\* (region) | direction; chart numeric ±0.08 fractional (documented) |
| contrast | M6 L\* spread p95−p5 | direction, with at least 2 L\* change |
| noise, sharpness | M7, M8 | refusal-only today (out of vocabulary); kept for the roadmap |
| "should not change" (undo, P5, P6, P7) | ΔE00 per chart patch; M2 floor on real frames | ΔE00 < 2.3 pass, 2.3-5 `drift` (listed, counts as partial), > 5 fail. These are darktable-tests' thresholds: max ΔE < 2.3, mean < 2.3/3. |
| looks | per §3.4, with `conventions.suggested_tolerances` | documented ±0.15 EV / ±5° / ±0.08 sat / ±2 ab; community ±0.25 / ±8° / ±0.12 / ±3; inferred sign only |

### 5.2 Noise floor

At the start of each session, the runner exports the untouched frame twice. The floor per metric is 3 × the observed difference, and never less than:
- 0.02 EV;
- 0.5 L\*;
- 0.2 ΔE00;
- 0.002 grey-world ratio.

A direction-only check needs |Δ| above the floor. When the floor comes out above twice those minimums, the frame is non-deterministic. That is a `harness_bug` finding, and the session stops.

### 5.3 Scoring and partial credit

- A case scores pass = 1, partial = 0.5, fail = 0. A silent failure is always 0, and it is always listed first.
- **partial** covers:
  - a multi-check case where at least half the checks pass, nothing moved in the wrong direction, and there is no silent failure;
  - the right direction but a numeric magnitude outside tolerance;
  - `collateral` (another decoded param moved);
  - `drift` (ΔE00 2.3-5).
- **over_budget** is a correct result that was slower than the case budget. It counts as pass for correctness and is listed in S5. The budgets: single move 3.5 s (row 427), commit 12 s, launch to `gui_ready` 15 s, job at most 6 s per frame, a look plan at most 40 s.
- A layer rate is the sum of the scores divided by N. P0 requires every case to score exactly 1.

### 5.4 What counts as an honest refusal

In tool mode, all of the following:
1. `ok: false`;
2. `error.code` is in the refusal set `{usage, value_unknown, not_pinned, unreachable, not_in_darkroom, unsupported_format, not_found, permission_denied, disk_full, library_locked, darktable_not_running, darktable_version_mismatch, module_unavailable}`;
3. `error.fix` or the message names what cannot be done and the nearest thing that can;
4. the sidecar history hash is unchanged. A multi-move phrase must not leave a half-applied edit behind: either it is rolled back, or every applied move is reported.

`gui_not_ready`, `bridge_timeout` and `commit_timeout` are not refusals. They are transient, and they end as `blocked` when the retry also fails.

In agent mode, all of the following:
1. no Scouter write for the refused part, checked through the sidecar;
2. the final text contains a refusal marker (regex list in `check_map.json`: "can't", "cannot", "not able", "isn't available", "doesn't support", "outside what", …);
3. the text does not contain a completion claim for the refused capability (regex: "done", "removed", "replaced", "I've …" together with the capability's noun).

A substitute edit passes only when the text names it as a substitute and the sidecar change matches what it names. When the text is ambiguous (both markers, or neither), the case is `needs_review`. It counts as partial and is listed for Pratik. It is never auto-passed.

A refusal of an `in`-class request is `wrong_refusal`, which is a fail.

## 6. Watchdog and hang mitigations (product change in this programme)

The watchdog design (`docs/research/scouter-watchdog-design.md`) is adopted, split into what ships with this release and what comes later.

**Now (before the gauntlet's first full run):**
1. **`scouter/core/dtprocess.py`**: `launch`, `owned_pid` (pid + `lstart` + exe, with an owner file), `terminate` (the ladder), and `wait_gui_ready` (polls `darktable_mcp.diag` for `gui_ready`, stall write-up item 3).
   - The quit ladder skips osascript when `alive()` has already failed. Today a wedged darktable costs about 60 s per quit; with the skip it is about 10 s (stall write-up §3).
   - The bench, the acceptance driver and the gauntlet all import it.
2. **`dtbridge.health()`** (diag with a 5 s timeout, called only after a timeout) and the typed **`DarktableUnresponsive`** (`darktable_unresponsive`, with its own exit code). Raised only when the call timed out AND the ping failed.
3. **`scouter doctor`** watchdog lines, `--ping` (exit 0/1), and `--heal`, which works only on an owned pid.
4. **Auto-heal**, built behind `watchdog.auto_heal: false` with `max_heals_per_job: 1` and `heal_budget_s: 60`. Retries are idempotent through the journal readback. `library_import` and `job.run` are never auto-retried. Telemetry goes to `events.jsonl` and `metrics.db`. The envelope carries `healed: true`.
   - It is off by default and off in the gauntlet, except in robustness cases §3.5.
   - A heal inside any gauntlet case other than those is recorded as a failure of the step.
5. Stale `library.db.lock` / `data.db.lock` are removed only when the owner file's pid is confirmed dead and the lock's pid matches it.
6. Tests: every test in the watchdog design §4. The host test is opt-in with `SCOUTER_HOST=1`.

**Later (after release, ordered by measured hang evidence from the gauntlet):**
- a dedicated `--configdir` per job, so no shared state carries between jobs (H4);
- fewer view bounces: commit-strategy work and the bounce cost in S5;
- an upstream darktable issue for `gtk_wrap` running `g_main_context_invoke_full` inline (lua/call.c:639/665), with the `.hang` stack as the repro;
- a closer lighttable-launch gate than the 20 s fallback (stall write-up, residual risk 2).

**Exit criterion for this section.** The robustness layer's H1-H4 cases and the eight watchdog cases pass, and the full run records zero `hung_first_launch`.

## 7. Vocabulary expansion policy

**How the roadmap is produced.** The report's S7 is computed, never written by hand:
1. Collect every honest refusal, `gap` target and `wrong_refusal` across the run.
2. Key each one by its capability (check map) and its darktable module family (`taxonomy.modules[].purpose_group` + `id`).
3. Demand score = (number of requests in `requests.json` needing it) + 3 × (the number of those that are P0) + (the number of agent-mode refusals on it) + (the number of look targets gapped on it) / 10.
4. Rank families by score. For each family, list the modules, the phrases (from the taxonomy), the typical ranges, the `on_host_5_4_1` flag, and the Lua reachability: can a `dt.gui.action` path be pinned, per the phase 3 pin rule?

**Order rule (row 433).** Expand one module at a time, in order of demand score.
- Skip a family that has a design-level question until Pratik answers it.
- Each module lands under the phase 3 rule: it is `live` only after a host probe pins it.
- Each landing is gated by a rerun of: the new module's coverage block (the same formula as §3.1); every scenario case that refused on this capability, which must flip to pass or partial; and the full P0 slice of every layer.

The provisional order from `requests.json` module mentions is lens correction (61), parametric mask (46), sharpen (43), retouch (41), rotate and perspective (38), denoise (26), tone equalizer (24), highlight reconstruction (23), color calibration (22), crop (16), local contrast (12), vignette (10), haze removal (9), graduated density (7). The measured score replaces it after run 1. "Shadows and highlights" (50) is not in this list, because most of those requests are served today by colour balance rgb's shadows/highlights luminance.

**Design-level questions for Pratik.** Fix authority stops at vocabulary families (row 433). Each gets a question with a recommendation:

| Family | Why it is design-level | Recommendation |
|---|---|---|
| **Masks / local edits** (parametric, drawn, "just her face", "only the sky") | darktable's Lua API exposes no masks. The candidate routes are styles or presets that carry a parametric mask, or the 5.2+ external raster mask (PFM). Each is a new mechanism, not a slider. | Start with parametric masks by luminance and hue range, carried in generated styles (for example "darken only the sky" = blue hue range + exposure). No drawn masks and no AI masks (5.6 only). |
| **Detail** (sharpen, denoise, diffuse or sharpen) | preset-driven modules; the quality risk is over-smoothing and halos, which the stdlib can measure only weakly (M7, M8) | Add denoise (profiled) strength and sharpen amount as sliders first. Diffuse or sharpen only through its named presets. |
| **Geometry** (crop, rotate, perspective, lens correction) | numeric crop needs exact coordinates; lens correction is a profile toggle; auto-perspective needs detected lines | Lens correction on/off first (the biggest demand, one toggle). Rotate by degrees and crop to an aspect ratio next. No auto-perspective. |
| **LUT 3D** | needs LUT files on disk, and their licences; a new file-management surface | Defer. Take it only through the Fuji calibration sub-project, with permissively licensed LUTs. |
| **Grain / vignette** (finishing) | simple sliders, but they add "look" finishing that the looks layer marks as `gap` in about half the film looks | Add both as plain sliders. This is the lowest risk and closes the most look `gap` targets. |

## 8. Runtime plan

The estimates use today's measured timings: a write 2.0 s (the first move on an image about 4.6 s with the bounce), `scout_stats` about 2.5 s, launch to ready about 2.5 s plus the gate, a quit about 4.5 s, and `job run` about 4 s per frame (acceptance run 3 and the 09-24 bench results).

| Layer | Cases | Est. machine time | Unattended overnight? | Needs |
|---|---|---|---|---|
| 0 offline | 2,314 | < 5 min | yes (CI) | nothing |
| corpus survey + chart golden | 1 + 56 renders | 15 min | yes | darktable-cli |
| 1 coverage | 1,316 | 2.0 h | yes | darktable GUI (machine usable, but do not use darktable) |
| 2 scenarios, tool | 701 | 1.5 h | yes | same |
| 2 scenarios, agent | 160 | 3.0 h | **no**: needs Pratik's usage OK (under 60%) | `claude -p` (subscription) |
| 3 bulk | 40 | 3.0 h | yes | about 3 GB peak disk |
| 4 looks | 469 | 4.5 h | yes | same |
| 5 robustness | 32 | 1.2 h | yes. It kills and relaunches only owned pids. Disk-full uses a disk image. | no user darktable open |
| 6 parity | 90 | 0.5 h | yes | |
| 7 install rehearsal | 10 | 0.3 h | yes | no user darktable open |
| 8 guide + learning | 35 | 0.4 h | yes | |
| **Total** | **5,167** | **about 16.5 h** for the first full pass | | |

**Schedule.**
- **Night 1**, about 13 h unattended: coverage, tool-mode scenarios, bulk, looks, robustness, parity, rehearsal and guide.
- **Day**: triage and the fix loop, the agent-mode batch once Pratik OKs usage, and layer reruns.
- **Night 2**: the full rerun on the release candidate commit.

That puts the earliest honest sign-off about 36 h after the plan's build tasks finish. It is not "tomorrow" unless night 1 comes back clean.

**Checkpoints.**
- After every layer, the runner writes `progress.json`: per-layer done/total, pass/partial/fail, silent failures, crashes, hangs, the ETA, and the current case.
- It appends a line to `log.md` in the acceptance driver's format.
- It stops the layer on a crash (§2.3).
- The controller republishes the **"Scouter Gauntlet" progress artifact** (same URL every time) from `progress.json` at each layer checkpoint and at the end, and updates the vault Status Board per the Capsule status rule.

**How Pratik sees progress.** He can `tail -f` `runs/<stamp>/log.md`, or open the artifact, which shows per-layer bars, the sign-off block, and links to failures. The final `report.md` is mirrored to the vault as `Knowledge/Scouter - Release gauntlet run <stamp>.md`.

## 9. Out of scope, risks, open questions

**Out of scope.**
- Windows and Linux.
- darktable 5.6: `vocab`/diag report the version, and R21 checks the refusal.
- A true fresh macOS user account or VM for the rehearsal. It is an isolated HOME/XDG only.
- Masks, detail, geometry, LUT and grain vocabulary (§7 expands them later).
- Other MCP clients than Claude Code.
- Performance tuning beyond reporting S5.
- Agent-mode runs on models other than Sonnet.
- Upstream darktable fixes.

**Risks.**
1. **P0 is mostly refusals today** (about 62 of 100 are out of vocabulary). The bar can pass while the product serves a minority of top demand. Mitigation: the served-rate headline, plus §7's roadmap ordering (Q3).
2. **The corpus is narrow**: two bodies, mostly street and travel, and no food, product or wedding frames. Genre-specific truth is not tested, only deltas on frames with matching problems. Mitigation: roles by measured stats. A future corpus drop (Pratik's own genre frames) plugs into §4.2 without spec changes.
3. **Tool-mode plans are ours.** They test Scouter's tools, not the model's translation. Only agent mode tests the real Claude Code path, and it is budget-limited to 160 cases (Q2).
4. **The synthetic chart is a non-raw input.** darktable's non-raw defaults differ from raw (no demosaic, a different input profile), so chart look deltas are pipeline-relative and not camera-true. Mitigation: all look deltas are measured against the same input's baseline, and the real-frame direction checks cover the raw path.
5. **Harness and product hangs share symptoms.** Mitigation: the health-ping classification plus `.hang` stack capture for every `blocked` case, so every hang is attributed. `harness_bug` is a first-class triage class.

**Open questions (each with a recommendation).**
- **Q1. Name.** "Hyperbolic Time Chamber" (a year of training in a day), or another DBZ name. *Recommend: Hyperbolic Time Chamber.*
- **Q2. Agent-mode scope and budget.** 160 cases (P0 100 + impossible 60) on Sonnet through `claude -p`, started only after you confirm usage under 60%. About 3 h of machine time and a slice of the weekly budget. *Recommend: yes. Add the 30-look agent sample only after release.*
- **Q3. Served-rate floor.** Honest refusals pass, so about 62% of P0 can pass as refusals. Add a minimum P0 served rate as a gate? *Recommend: no gate for this release. Headline it, and use it to order §7.*
- **Q4. Tier gates.** P1 at least 95% per layer; looks at least 90% per target (documented and community) and at least 95% correct sign (inferred); `needs_review` counts as partial until you rule on it. *Recommend: accept as written.*
- **Q5. Fuji/look calibration sub-project.** It downloads community darktable styles and LUTs (network, licences) to promote `inferred` targets. *Recommend: run it after release as P1, using only MIT/CC-BY-licensed ports (jssfr, bastibe), and record the licences in `looks.json` sources.*

## 10. Self-review record (2026-09-26)

- **Placeholders:** none. The two host-dependent unknowns are named, and each has its fallback in this spec: PFM import (§4.3, the TIFF fallback) and the H2 dialog hook (§3.5, skipped with a reason).
- **Contradictions fixed while writing:**
  - "12 genres" in the brief vs 14 in `requests.json`. Scenarios use the 14 request genres, and the 12 taxonomy workflows are reported as step coverage (§3.2).
  - "Top 100 by frequency" had no frequency field in the data, and a literal ranking collapsed onto 4 problems. It was replaced by the proportional-quota rule (§3.2).
  - The bench harness is on an unmerged branch, and the acceptance driver sits in a reaped scratchpad. Plan task 1 fixes both (§2.3).
- **Ambiguity resolved:**
  - "Silent failure" has six concrete conditions.
  - "Honest refusal" has tool-mode and agent-mode rules, with `needs_review` in place of auto-pass.
  - P0 membership is defined per layer.
- **Scope:** the watchdog is split into now and later. Vocabulary families are questions, not work. Agent mode is budget-gated.
