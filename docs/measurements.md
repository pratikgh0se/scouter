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
