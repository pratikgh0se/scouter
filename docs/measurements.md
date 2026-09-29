# Scouter measurements

Provenance: these numbers were produced by `tests/test_commit_trigger_host.py`
on task 1's host run (this Mac, darktable 5.4.1, Apple M4, 2026-09-13) and are
committed as the reviewed record. **The test no longer rewrites this file**: a
host run writes `docs/measurements-generated.md` instead, and numbers are
copied across here deliberately, by hand, with the change explained in the
commit (final review, should-fix 3). The isolated-configdir baseline below is
therefore stable and citable; task 12's numbers under a live GUI are in
`.superpowers/sdd/2026-09-13-scouter-phase1/task-12-report.md`.

## Commit trigger (phase 1 design 8.1)

- accepted candidates: none
- **strategy adopted for `history.commit(strategy="auto")`: `view_bounce`**
- view after C1: darkroom
- view bounce (C3) cost, wall clock: 510 ms
- view bounce (C3) cost, CPU time (os.clock, not wall clock): 241 ms
- global undo action probe: nan / darkroom undo: nan (phase-2 note only; phase 1 uses the journal, design 8.2)
- XMP mtime advanced during the run: True

## Scouter loop, 20 MP ORF at 1024 px (design 6.1)

- runs, (render+read s, stats s): [(2.068, 0.035), (2.08, 0.034), (1.929, 0.034)]
- best run: render+read: 1.929 s, stats pass: 0.034 s
- **loop total: 1.962 s** (budget 3.0 s)

## Raw probe records

    {"probe": "view_at_start", "value": "darkroom", "ms": 0, "note": null}
    {"probe": "enable_exposure", "value": 1.0, "ms": 0, "note": "iop/exposure"}
    {"probe": "set_exposure_0_9", "value": 0.89999997615814, "ms": 3, "note": "iop/exposure/exposure"}
    {"probe": "c1_selected_images", "value": "nan", "ms": 0, "note": "lib/selected image[s]/write sidecar files"}
    {"probe": "view_after_c1", "value": "darkroom", "ms": 0, "note": null}
    {"probe": "c2_metadata", "value": "nan", "ms": 1, "note": "lib/metadata/write sidecar files"}
    {"probe": "c2_image", "value": "nan", "ms": 0, "note": "lib/image/write sidecar files"}
    {"probe": "undo_global", "value": "nan", "ms": 0, "note": "global/undo"}
    {"probe": "undo_darkroom", "value": "nan", "ms": 39, "note": "views/darkroom/undo"}
    {"probe": "c3_view_bounce_cpu", "value": 1, "ms": 241, "note": "lighttable then darkroom (CPU time via os.clock, not wall clock)"}
    {"probe": "view_after_c3", "value": "darkroom", "ms": 0, "note": null}
    {"probe": "done"}

## Task 12 fix round 1: view switches from the worker coroutine (2026-09-14)

Host: this Mac (Apple M4), darktable 5.4.1, Lua API 9.6.0, real
`~/.config/darktable`, `P6281348.ORF`. Probes:
`spike/scouter_view_switch_probe.lua`, `spike/scouter_sidecar_probe.lua`,
`spike/scouter_sidecar_probe2.lua`, plus a heartbeat-instrumented copy of the
plugin (worker loop writes `~/.cache/darktable-mcp/heartbeat` every tick).

### View switching, measured from inside a `dt.control.dispatch` coroutine

- `dt.gui.current_view(v)` is **asynchronous**. Reading the view immediately
  after the call returns the OLD view: leaving the darkroom still reported
  `darkroom` at 0 ms, then `lighttable` after 150 ms of yielding.
- The switch **does land** from the worker coroutine, reliably and fast:
  darkroom -> lighttable 150 ms, lighttable -> darkroom **50 ms**. "Coroutine
  GUI mutations get silently dropped" is false.
- `dt.gui.views.darkroom.display_image(image)` called **from the lighttable**
  does not switch the view at all: `pcall` ok=true, view stayed `lighttable`
  for the full 10 s budget, twice. From **inside** the darkroom the same call
  works and swaps the loaded image.
- `dt.gui.views.darkroom.display_image()` (no argument, the documented
  *getter*) **deadlocks the worker coroutine permanently**. Heartbeat froze at
  "display_image getter -- about to call" and never advanced: not in 4 s, not
  in 72 s. `pcall` does not help -- it is a block, not an error. Every later
  bridge request then times out behind it.

### Darkroom readiness

- Task 12's 25 s blind settle was a misdiagnosis of the deadlock above. The
  pipe itself answers a real `dt.gui.action` on the **first attempt, 0 ms**
  after `view.current` first reports `darkroom`.
- Replaced by a real readiness check (poll `darkroom.current` until it reports
  a loaded image). Measured: **0.12-0.17 s**, four runs.

### Commit

- The view bounce writes the sidecar only when the history has actually
  **changed**. Setting a slider to the value it already holds adds no history
  item, so nothing is written -- correctly. `darkroom.set` resolves that by
  reading the value back from the XMP.
- No Lua API or action path writes a sidecar directly: every "write sidecar
  files" spelling returns nan from the lighttable (where that panel lives) as
  well as from the darkroom; there is no `dt.database.write_*`; and
  `dt.gui.libs.image` exposes no method.
- `dt.preferences.write("darktable", "write_sidecar_files", ...)` does **not**
  reach darktable's core preference: it creates `lua/darktable/write_sidecar_files`
  in darktablerc and leaves the core setting (`on import`) untouched. It is
  inert and was removed.
- `history.commit` latency, verified against the sidecar on disk:
  **765 / 770 / 781 / 1305 ms** (four runs).
- `darkroom.set` with a value the XMP already carried (the repeat-run case):
  5.5-5.7 s, which is the two bounces plus their sidecar waits before the
  readback resolves it. With a value that really moves: ~1.8 s.
- `darkroom.undo_last` (set + commit + XMP verification): 1106-1801 ms.
- Scouter loop at 1024 px, with a live darktable session in the same
  session: **2789 / 2829 / 2832 / 2948 / 3112 ms** (budget 3000 ms) -- the
  3112 ms run failed the budget assertion. Marginal on this host under load.

## Task 7: `.dtstyle` schema, pinned by a host probe (2026-09-14)

Provenance: `docs/measurements-generated.md` (gitignored, host-run output),
copied here deliberately, by hand, in task 7's commit -- same discipline as
the rest of this file (see the header note above).

Host: this Mac (Apple M4), real darktable app, real `~/.config/darktable`,
`P6281348.ORF`. Test: `tests/test_host_phase2.py::test_pin_dtstyle_format`.

Design section 9.2's original schema (read from darktable's source, never
verified) was wrong on two points, corrected in this task's commit:

- **No `<iop_order>` per plugin, at all** -- confirmed absent across all 30
  `<plugin>` elements of the captured whole-history style. The module filter
  is never blocked on this: there is no fallback to trigger.
- **`<info>` carries `<iop_list>`** (a comma-separated enumeration of
  darktable's entire built-in pipeline order, priority `0` throughout), not
  `<iop_order_version>` / `<iop_order_list>` as drafted. The XMP carries
  nothing resembling it (only `iop_order_version`, e.g. `"4"` on this
  camera -- "use the built-in order"), so `scouter/core/dtstyle.py`'s writer
  always emits `<iop_list></iop_list>` empty.

**Verdict: schema pinned, `<iop_order>` not required.** Verified live, in the
same darktable session: `style.extract(image=HERO, modules=["exposure"])`
(the real filtered path) built a `.dtstyle` with an empty `<iop_list>`,
`style.import_file` (`dt.styles.import`) accepted it without complaint, and
the resulting style showed up in `list_styles` afterwards -- the writer's
output round-trips through real darktable, not just through Scouter's own
reader.

The real, committed capture is `tests/fixtures/hero.dtstyle`; the full XML
and the exact `<info>`/`<plugin>` child order are in
`docs/measurements-generated.md` (gitignored) and this task's report.

### Fix round 2, item 3: dense 0..N-1 renumbering, multi-plugin, live round trip (2026-09-14)

Same host, one more launch (`SCOUTER_HOST_TESTS=1 pytest tests/test_host_phase2.py`,
clean quit confirmed via `pgrep` before and after). `dtstyle.build`'s `<num>`
renumbering wording was corrected this round: the writer numbers `<num>`
densely, 0..N-1, in **XMP history order** (the order `active_entries`
returns them, i.e. ascending by the XMP's own `num`) -- NOT "pipeline
order", which this writer has no access to (only a whole-history export
happens to have XMP order coincide with the true pipeline order).

Live-verified with a genuinely MULTI-plugin filtered request (not a single
module, which the earlier round's probe used):
`style.extract(image=HERO, modules=["exposure", "temperature", "colorin"])`.
Result: 3 `<plugin>` elements, `<num>` sequence exactly `['0', '1', '2']`
(dense, no gaps, regardless of the source XMP's own non-contiguous `num`
values for those entries), and the real `dt.styles.import` accepted the
file -- the resulting style (`scouter-probe-filtered-<pid>-<ts>`) appeared
in a live `list_styles` afterwards, then was deleted again via the new
`style.delete` verb (`confirm=true`) before the run ended.

**Verdict: dense, XMP-history-order 0..N-1 renumbering round-trips through
real darktable for a multi-plugin filtered style, confirmed live.**

## White balance (task 11, 2026-09-14, this Mac, darktable 5.4.1, real host)

Provenance: `SCOUTER_HOST_TESTS=1 pytest tests/test_host_phase2.py -k WhiteBalanceCalibrationHostTest`
and `... -k PinWbCoefficientSlidersHostTest`, copied here by hand from
`docs/measurements-generated.md` after review. Every run confirmed darktable
gone (`pgrep`) before and after.

**Coefficient-slider probe (step 3): tier 2 is NOT available on this
host/darktable build.** Two independent runs, each starting from a
DIFFERENT first candidate, both reproduced the same failure: the first
`dt.gui.action` call on an unrecognised path under the `temperature` module
(`iop/temperature/red` in run 1, `iop/temperature/channel coefficients/red`
in run 2) leaves darktable's Lua worker unresponsive to every LATER bridge
call in that session too -- including an unrelated `view.current` sanity
check. `dt.gui.action` does not fail gracefully on an unrecognised path
under this module on darktable 5.4.1; it wedges the worker. None of the
brief's ten candidate paths were confirmed non-`nan`, and none could be,
without accepting that risk per candidate (each wedge costs a full
darktable relaunch to recover from). `dtbridge.ACTION_PATHS` gets no new
`("temperature", "coeff_*")` entries from this task. `darkroom.undo_last`'s
tier 2 (restore through the coefficient sliders, `darkroom.py`'s
`coefficient_sliders`) therefore never fires on this host; tier 3 (Kelvin
inversion, now backed by a real measured table) and tier 4 (the improved
honest refusal) are what actually run. (Numbered to match
`scouter/core/darkroom.py` -- an earlier draft of this section was one tier
out, which made it disagree with the code it describes.)

**Calibration (step 7): real table measured, twice, both runs monotone.**
`wb.calibrate` on a scratch copy of the EditingTrial ORF
(`~/Pictures/ScouterScratch/em10iii.ORF`), `k_from=3000 k_to=12000 step=1000`
(10 points, ~18s estimated / ~27-29s actual wall time including the darkroom
open). Run 1's r/b ratios: 0.4517 (3000 K) rising to 2.1354 (12000 K), every
step strictly increasing. Run 2: 0.4587 -> 2.1635, same shape, small
per-launch variance in the absolute coefficients (expected -- a fresh
darktable process, not bit-identical rendering). Run 2's table is committed
as `tests/fixtures/wb-em10iii.json` (10 points, ~20 numbers, the "~20
numbers" the brief asks for).

---

# Phase 2 (task 14's live end-to-end run, 2026-09-14)

Host: this Mac (Apple M4), darktable 5.4.1, Lua API 9.6.0. Produced by
`tests/test_host_phase2.py::Phase2EndToEndTest`, green twice in a row, and
copied here **by hand** from `docs/measurements-generated.md` (phase 1's
convention: the test no longer rewrites this reviewed record).

The job: 30 real ORFs from Pratik's 2026-05-24 shoot
(`P5240141.ORF`..`P5240170.ORF`, 502 MiB), staged as a card under
`~/Pictures/ScouterScratch`, imported into a library root under the same
directory. `tests/make_synthetic_job.py` (the 6x5 synthetic card) stays the
unit-level fixture: copies of four ORFs have identical pixels, so every
sharpness inside a burst ties and the cull stage could only ever assert the
tie-break.

## Phase 2: import throughput

| | |
|---|---|
| `library.import`, 30 frames / 526,387,934 bytes | **1.35 s** (run 2: 1.53 s) |
| effective | **390 MB/s** (372 MiB/s), ~22 frames/s |
| staging the card (30 file copies, not part of the verb) | 0.40 s |

Two sha256 passes per file plus the copy, on the internal SSD with the OS page
cache warm from the staging copy. Extrapolated to the 300-frame / ~5 GB job
the spec's budget is written against (< 10 min): **~14 s** of hashing and
copying. The budget is not the binding constraint on this host; it was written
for an external disk.

## Set stats, 30 frames at 512 px

| | |
|---|---|
| `cull.propose` (30 frames, cold cache, 4 workers) | **33.5 s** (33.3 s of it in `stats_many`) |
| per frame | ~1.1 s |
| `outliers.list` (29 frames, after `style.apply_set` changed every XMP) | **33.3 s** |

The second stats pass costs the same as the first **by design**: the cache key
includes the XMP's mtime, so `style.apply_set` invalidating all 30 sidecars is
exactly the case the key exists to catch. Extrapolated to 300 frames: ~5.5 min
per pass, against the spec's < 6 min budget.

## Repeat-set cost (before: 5.5-5.7 s)

| | |
|---|---|
| repeat `darkroom.set exposure 0.6`, 1st | **0.22 s**, `noop: true` |
| repeat `darkroom.set exposure 0.6`, 2nd | **0.22 s**, `noop: true` |

E8's asserted budget is < 1.5 s; the measured cost is **25x better than phase
1** and the assertion passes with a 7x margin. Both repeats are no-ops (zero
bridge writes, zero view bounces) because the watermark was fresh and the
requested value already matched the sidecar.

Measured BEFORE the import, deliberately. With the job's 30 frames already in
the library, `history.commit`'s view bounce did not get the sidecar written
inside its 5 s budget: the bounce leaves the darkroom and darktable does not
reliably come back to the same image once the library has grown (same root
cause as the darkroom-navigation limitation below). Phase 1 measured commit at
765-1305 ms against a one-image library, and that is the condition this
measurement reproduces.

## `style.apply_set`

| | |
|---|---|
| 30 frames, one style, batches of 25 ids | **0.56 s** (19 ms/frame), `failed: 0` |

A library write with no pipe reload, as designed. Extrapolated to 300 frames:
~6 s, against the spec's < 2 min budget.

## `export.release client-proof`

| | |
|---|---|
| 30 frames, 2048 px longest edge, JPEG q90, sRGB | **67.3 s** (2.24 s/frame) |
| every output's longest edge, read from its SOF marker | 2048, all 30 |
| `export.verify` over the manifest | clean, 30 checked |

Extrapolated to 60 full-size finals the spec budgets at < 8 min: full-size
renders are slower than 2048 px, so this figure does not settle that budget --
it is the proof figure, and it is what `client-proof` is for.

## `backup.run` / `backup.verify`

| | |
|---|---|
| `backup.run` (openssl, + rsync of the raws) | **2.14 s** |
| tar / encrypt / rsync split | 128 ms / 137 ms / 1866 ms |
| archive size | 214,420 plaintext -> 214,448 ciphertext bytes |
| `backup.verify` | **0.24 s**, `verified: true`, 6 named checks all ok |

The archive is small because the raws travel by `rsync`, not inside the tar --
the tar holds `.scouter/`, the sidecars and the manifests. rsync is 87 % of the
wall time and is the part that scales with the job.

## Cull over real frames (new, phase 2)

30 consecutive frames of one evening shoot, `cull.burst_gap_s` 2.0:

- **20 bursts**, sizes `[7, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]`
  -- one real 7-frame burst (1 s apart), one of 3, two of 2, sixteen singletons.
  `cull.propose`'s grouping matches what the files' own EXIF implies, asserted
  frame by frame rather than against a number in a fixture.
- **0 of 20 bursts needed the `seq` tie-break.** Real pixels differ, so
  sharpness ties do not occur -- the opposite of the synthetic card, where
  every burst ties. Both are now covered: the real set here, the tie-break by
  `tests/test_cull.py`.
- `cull.apply`: 4 of 30 frames needed a rating change; all 4 were visible in
  the XMP within the 2.0 s poll window (`unverified: 0`).
- `outliers.list`: `wb_test: "ratio"` (no Kelvin table for this camera under
  the test's own config dir), 24 of 29 outside tolerance, 5 within -- an
  evening shoot with real exposure variation, which is what an outlier report
  is for.

## Darktable facts pinned by this run

**1. A library root under macOS's `/var/folders/...` temp tree does not work
at all.** `/var` is a symlink to `/private/var`; darktable records the
RESOLVED film path, so every `image.lookup` by the unresolved path misses.
`import_batch` reported 0 images and every later verb refused with "not in
darktable's library" -- nothing in Scouter was wrong, the test was asking
darktable about a path it does not use. Any caller handing darktable a path
should hand it a resolved one.

**2. Entering the darkroom for the first time needs an image argument.** From
a cold start with no image on the command line, darktable sits in the
lighttable and `dt.gui.current_view(darkroom)` from the worker coroutine never
lands -- "view is still lighttable" at a 5 s budget, again at 20 s, three runs
in a row. Task 12 measured lighttable -> darkroom at 50 ms, but always on an
instance that had already been in the darkroom once. The plugin's
`VIEW_ENTER_DARKROOM_BUDGET_MS` (20 s, larger than the 5 s mid-bounce budget)
and the client's matching 60 s `darkroom.open` timeout come from this; the
budgets are pinned against each other by
`tests/test_bridge.py::test_darkroom_open_client_timeout_outlasts_the_plugins_own_budget`.

**3. `display_image` is asynchronous, like `dt.gui.current_view`.** Reading the
loaded image immediately after the call returns the PREVIOUS one, so
`darkroom.open` reported "wrong image loaded" for a swap that was simply not
finished. Fixed with a yield-poll (`wait_for_darkroom_image`), the same
treatment task 12 gave the view.

**4. Scouter cannot move darktable's darkroom onto a frame from a film roll
imported during the same session.** `display_image(image)` returns without
error and the darkroom keeps showing the image it already had -- 15 s of
polling, `darkroom.current` unchanged. Bouncing out to the lighttable with
that image selected and switching back returns to the SAME previously-edited
image, not the selection. Five runs, consistent.

`darkroom.open` is honest about it: it raises rather than reporting
`opened: true` for an image that never loaded, and
`Phase2EndToEndTest::test_06a_darkroom_open_refuses_a_frame_imported_this_session`
pins the refusal so a future darktable that CAN do this fails the test loudly
instead of silently changing behaviour.

What follows for the product: the frame is chosen in darktable's GUI and
Scouter edits what is loaded. No set-wide verb is affected -- `cull.*`,
`style.*`, `outliers.list` and `export.*` all work off the XMP and
`darktable-cli` and never touch the darkroom, which is why the other ten
stages pass.

**5. darktable's `library.db` remembers a path's rating across a delete and
re-import.** Run 4 found every frame already carrying run 3's cull ratings in
a library directory that had been deleted and rebuilt. The test now carries
its process id in the job slug so two runs never share a library path; any
script that re-imports to the same path should expect darktable's own
remembered metadata, not a clean slate.

## Phase 3 host run (2026-09-24+)

darktable 5.4.1 on Pratik's Mac. A scratch copy of the hero ORF
(`~/Pictures/ScouterScratch/phase3-probe.ORF`) was used. The plugin was
installed through `tests/host_support.PluginInstall` (plugin file + luarc
only, P3-R18) and restored after every class. Raw lines are in
`docs/measurements-generated.md`.

### Probe method finding: settle before probing

The first probe pass marked every candidate `wedges`: no answer in 10 s. A
control launch showed the cause. The already-pinned
`iop/colorbalancergb/global saturation` got no answer either when the probe
fired about 2 s after `view.current` first answered, while darktable was
still loading the darkroom pipeline. After a 20 s settle, `darkroom.status`
answered it non-nan in 111 ms. The probe now waits `SETTLE_S = 20`, then
requires the proven `iop/exposure/exposure` to answer (a per-launch
positive control) before it judges a candidate. The invalid first pass is
kept, and marked, in the generated file. An unknown path now answers `nan`
in about 110 ms rather than wedging, once darktable has settled.

### Candidate paths (one launch per candidate, 20 s settle + control)

| module.param | path | verdict | ms |
|---|---|---|---|
| colorbalancergb.global_vibrance | `iop/colorbalancergb/global vibrance` | pinned | 113 |
| colorbalancergb.global_chroma | `iop/colorbalancergb/global chroma` | pinned | 111 |
| colorbalancergb.shadows_luminance | `iop/colorbalancergb/shadows lift/luminance` | absent | 120 |
| colorbalancergb.shadows_luminance | `iop/colorbalancergb/shadows luminance` | absent | 118 |
| colorbalancergb.shadows_luminance | `iop/colorbalancergb/lift/luminance` | pinned | 120 |
| colorbalancergb.shadows_hue | `iop/colorbalancergb/shadows lift/hue` | absent | 106 |
| colorbalancergb.shadows_hue | `iop/colorbalancergb/shadows hue` | absent | 170 |
| colorbalancergb.shadows_hue | `iop/colorbalancergb/lift/hue` | pinned | 111 |
| colorbalancergb.shadows_chroma | `iop/colorbalancergb/shadows lift/chroma` | absent | 108 |
| colorbalancergb.shadows_chroma | `iop/colorbalancergb/shadows chroma` | absent | 110 |
| colorbalancergb.shadows_chroma | `iop/colorbalancergb/lift/chroma` | pinned | 168 |
| colorbalancergb.midtones_luminance | `iop/colorbalancergb/power/luminance` | pinned | 178 |
| colorbalancergb.midtones_hue | `iop/colorbalancergb/power/hue` | pinned | 120 |
| colorbalancergb.midtones_chroma | `iop/colorbalancergb/power/chroma` | pinned | 112 |
| colorbalancergb.highlights_luminance | `iop/colorbalancergb/highlights gain/luminance` | absent | 113 |
| colorbalancergb.highlights_luminance | `iop/colorbalancergb/highlights luminance` | absent | 114 |
| colorbalancergb.highlights_luminance | `iop/colorbalancergb/gain/luminance` | pinned | 110 |
| colorbalancergb.highlights_hue | `iop/colorbalancergb/highlights gain/hue` | absent | 163 |
| colorbalancergb.highlights_hue | `iop/colorbalancergb/highlights hue` | absent | 117 |
| colorbalancergb.highlights_hue | `iop/colorbalancergb/gain/hue` | pinned | 108 |
| colorbalancergb.highlights_chroma | `iop/colorbalancergb/highlights gain/chroma` | absent | 120 |
| colorbalancergb.highlights_chroma | `iop/colorbalancergb/highlights chroma` | absent | 115 |
| colorbalancergb.highlights_chroma | `iop/colorbalancergb/gain/chroma` | pinned | 154 |
| filmicrgb.white_relative_exposure | `iop/filmicrgb/white relative exposure` | pinned | 121 |
| filmicrgb.black_relative_exposure | `iop/filmicrgb/black relative exposure` | pinned | 111 |
| filmicrgb.contrast | `iop/filmicrgb/contrast` | pinned | 174 |
| colorzones.strength | `iop/colorzones/mix` | pinned | 174 |

The spec's `shadows lift/<x>` and `highlights gain/<x>` spellings, and the
flat forms, answered nan. The `lift/<x>` and `gain/<x>` spellings were added
as first candidates after the `power/<x>` rows answered, and were probed in
a second pass. **15 pinned** (every candidate row). No candidate wedged.

### XMP slots (sentinel method)

Sentinels have at most two decimals. The first run showed darktable rounds a
`set` to the slider's displayed precision: skew 0.173 was stored as 0.17,
filmic black -9.917 as -9.92, and color zones mix 0.173 as 0.17. So a
three-decimal sentinel matched no slot.

| module v | param | sentinel | slot | floats |
|---|---|---|---|---|
| colorbalancergb 5 | shadows_luminance / chroma / hue | 0.161 / 0.153 / 41 | 0 / 1 / 2 | 33 |
| colorbalancergb 5 | midtones_luminance / chroma / hue | 0.149 / 0.141 / 44 | 3 / 4 / 5 | 33 |
| colorbalancergb 5 | highlights_luminance / chroma / hue | 0.137 / 0.129 / 47 | 6 / 7 / 8 | 33 |
| colorbalancergb 5 | global_chroma / global_saturation / global_vibrance | 0.165 / 0.173 / 0.169 | 17 / 19 / 29 | 33 |
| sigmoid 3 | skew | 0.16 | 1 | 14 |
| filmicrgb 6 | white_relative_exposure / contrast | 4.96 / 1.451 | 2 / 14 | 29 (116 B) |
| filmicrgb 6 | black_relative_exposure | -9.94 | 1 | 29 |
| colorzones 5 | strength (mix) | 0.16 | 127 | 130 (520 B) |

Each sentinel appeared at exactly one slot. The colour balance rgb slots,
global saturation f19 and sigmoid skew f1 agree with the editor benchmark.
Hue values are stored in degrees, and a freshly enabled module holds 0° (the
default the hue rows now declare). Fixture: `tests/fixtures/phase3-host.xmp`.

### colorzones.strength default (card t_29a9ef47, decision rows 548/549, 2026-09-29)

`tests/test_host_phase3.py::ProbeColorzonesStrengthDefaultHostTest` (host run 2026-09-29 18:49,
chief of staff, darktable exited cleanly, 125 s), two independent oracles each on their own
scratch copy and darktable launch:

> oracle A (fresh enable) strength=0.0; sentinel (0.16 requested) strength=0.1600027084350586;
> oracle B (post-reset) strength=0.0 (internal units 0..2, display_scale 100 i.e. GUI 100% ==
> internal 1.0)

The sentinel read confirms slot 127 (above) was live on this run, so the 0.0 reads are not a
dead/zero-filled slot. Oracle A and oracle B agree: `colorzones.strength`'s darktable default is
0.0. Promoted `default_candidate` to `default=0.0` in `scouter/core/vocab.py`, citing this run. An
earlier same-day run (18:41, one shared scratch copy/session for both oracles) read oracle A back
as the sentinel itself -- a contaminated measurement, discarded; the design was fixed to give each
oracle its own copy/launch before the 18:49 run above.

### Signs (scout.stats, 512 px)

| move | measured | expected | verdict |
|---|---|---|---|
| exposure.black +0.01 | luminance mean 118.2 → 108.4 | down | ok |
| temperature.tint set 0.9 | r/b 1.906 → 1.833 | down | ok |
| "warm the highlights" (hue 60°, chroma +1 notch) | r/b 1.907 → 2.158 | up | ok |

No phrase was flipped. (Whole-frame r/b means stand in for the spec's
brightest-decile r/b; `scout.stats` has no per-decile channel means.)

### Preset round trip

`darkroom say "warmer"` → `preset save scouter-probe-<pid>` → `preset list`
shows it → `darkroom say "cooler"` → `preset apply` on the open image
(history_end 80 → 84) → `style.delete` (deleted: true) → the memory copy was
removed. Passed on all three live runs.

### Conversation timing (five one-move `darkroom say` calls)

| mode | wall times (s) | median | target |
|---|---|---|---|
| default (pre-commit bounce) | 4.01, 4.17, 4.11, 4.12, 4.15 | **4.12** | < 3.5 — **missed** |
| `trust_sidecar=True` | 1.37, 1.75, 1.27, 1.93, 1.26 | 1.37 | < 3.5 |

The earlier run's default medians were 4.03 and 4.02, with some calls at
2.1 s. The pre-commit bounce (P3-R3) costs about 2.7 s per phrase.
`test_4_conversation_timing` keeps its 3.5 s assertion and fails on the
host. This is an open finding, not a code defect hidden by the test.

**Follow-up (product close, 2026-09-26, row 427 item 4).** The default is now
trust-the-sidecar behind a staleness guard (`moves.precommit_decision`): the
pre-read bounce runs only for the first move on an image in a session, after
the image changed, when there is no sidecar, or when the sidecar is older than
the journal's last move. `bounce=True` forces it. Unit-tested with the fake
bridge (`tests/test_precommit_guard.py`: a single move on a fresh sidecar
performs no bounce). **Not yet host-measured**: the expected steady-state
median is the `trust_sidecar` row above (1.37 s), with one ~4 s move per image
per session. `test_4_conversation_timing` now asserts the guarded default and
records a forced-bounce loop beside it.

## Commit strategies on the host (row 429, 2026-09-26)

**Superseded on darktable 5.4.x by row 461: `auto` no longer tries
`no_bounce` there.** Gauntlet night1-20260926 recorded 15 no_bounce rewrites
from an older library.db state (history_end went back: 112 -> 92, 139 ->
112, 145 -> 35, 154 -> 41, 313 -> 231). The row 430 "history changed" check
accepted them as commits. So `auto` = `view_bounce_action` only on 5.4.x. Every path
refuses a history_end that went back (`stale_rewrite`), and a stale
`no_bounce` rewrite restores the pre-commit sidecar. `no_bounce` is not
safe to use on 5.4.1 and is kept only as an explicit strategy.

**Superseded by row 430: `no_bounce` does not commit on 5.4.1.** The table
below took "the XMP file changed" (mtime/size) as proof of a commit. On this
host `no_bounce`'s DB flush did not land the darkroom's in-memory edit; the
XMP was rewritten with the *old* history and a fresh timestamp, so every row
marked `XMP changed: yes` was a false positive. See row 430 below for the
corrected numbers and the fix (a2b9640).

darktable 5.4.1, one ORF in the bench library, an exposure edit before each
commit, `history.commit` wall time from the bridge call:

| strategy | wall times (s) | XMP changed | notes |
|---|---|---|---|
| `no_bounce` | 1.17 (first of the session), 0.16 | yes (metadata-only rewrite, history unchanged -- see row 430) | select=y, synchronize_selection_db_flush=y, rating touch rewrote the XMP |
| `view_bounce_action` | 0.75, 0.80, 1.07 | yes | both legs via `global/switch views/*`, no crash |
| `view_bounce` (old proxy transport) | refused at once | no | nan: lua-owned action pressed from Lua; transport removed |
| `sidecar_action` | - | no | nan from the darkroom (copy_history is lighttable-only) |

`history read --source db` and `--source xmp` agreed afterwards
(history_end 12). 0 new crash reports.

Expected from here (not yet re-measured end to end): a commit costs
0.2-1.1 s (`auto` = `no_bounce`, falling back to `view_bounce_action`), and
one `darkroom say` move stays well under the 3.5 s target with the sidecar
trusted (row 427's guard; 1.37 s median measured with `trust_sidecar`). The
acceptance run (row 429, part B) re-measures both.

**This expectation did not hold** -- see row 430.

## Commit strategies on the host, corrected (row 430, 2026-09-26)

**Superseded on 5.4.x by row 461** (see the note under row 429): the
`auto` fallback order described here no longer applies there.

The row 429 acceptance run (`20260926t073011z`) surfaced the false positive:
white-balance calibrate was refused as non-monotone on both Sony bodies, the
benchmark scored olympus 10/27 and sony 0/27, and 12 tasks came back
`reported_ok_unverified`. Root cause: `no_bounce`'s "proof" of a commit was a
sidecar mtime/size check, which a metadata-only rewrite also satisfies. Fix
(a2b9640, merged to main at f2fbab1): a commit counts only when the XMP's
history fingerprint (history_end, history_current_hash, the
`<darktable:history>`/`<darktable:masks_history>` blocks) actually changed;
`no_bounce` now reports "not written" on a metadata-only rewrite, and `auto`
falls back to `view_bounce_action`.

Live check after the fix (run dir
`/Users/pratik/Pictures/ScouterScratch/adjust-check/runs/20260926t080536z`), PASS:

| operation | wall time (s) | history (before -> after) | on-disk value |
|---|---|---|---|
| `darkroom_say` "brighter by 1 ev" | 4.6 | 11 -> 12 | exposure 0.7 -> 1.7 |
| module enable (already-enabled module skipped, then a real enable) | 0.9 | 12 -> 13 | -- |
| `darkroom_open` | 1.0 | -- | -- |

Zero crashes (9 before the run, 9 after). The effective commit today is the
view bounce, which costs about 1-3 s more per write than row 429's (false)
0.2-1.1 s `no_bounce` figure. Enabling an already-enabled module now waits
out the 5 s readback poll before resolving.

