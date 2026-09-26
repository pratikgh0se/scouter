# Changelog

Scouter has no prior changelog; this file starts with the `fix/e12-findings`
branch, which fixes defects found by the E12 live evaluation against real
darktable (`Knowledge/Scouter - E12 live evaluation 2026-09-24.md` in the
vault; decision row 407).

## 1.0rc1 (product/close, 2026-09-26, decision row 427)

Phase 3 plus the product-close items, offered as the 1.0 release candidate.
Unit-tested with fakes; the items marked "not host-run" still need Pratik's
live darktable session.

- `scouter setup` / `scouter setup --check` (new, non-registry like `doctor`):
  installs the packaged Lua plugin and its `luarc` line **persistently**,
  copies the measured white-balance tables shipped in `scouter/data/wb/`
  (today `em10iii`) into `<config_dir>/wb/` when absent (never overwrites;
  E12 D9), reports `config` and `doctor`, and prints the exact
  `claude mcp add --transport stdio --scope user ...` command plus the
  equivalent JSON. `--check` changes nothing; exit 1 when a required item is
  missing. Not host-run.
- Timing (row 427 item 4): `darkroom say`/`adjust` trust the sidecar by
  default behind a staleness guard (`moves.precommit_decision`): the ~2.7 s
  pre-read bounce runs only when there is no sidecar, on the first move on an
  image in a session, after the image changed, or when the sidecar is older
  than the journal's last move. `--bounce` forces it (also on `darkroom get`);
  `--trust-sidecar` still skips it; both together is `usage`. Each result's
  `plan` records the decision. Not host-measured yet.
- `wb calibrate <folder> --camera <body>` (row 427 item 3): folder mode, one
  body at a time, sweeping a scratch copy of the body's first frame (never
  the original); `--dry-run` lists the frames, the chosen one, the copy and
  the table path with no darktable call. New `camera`/`scratch` args on CLI
  and MCP. README recipe for the Sony A7 and NEX-5N. Not host-run.
- `job run` / MCP `job_run` (row 427 item 5 prep): cull propose + apply,
  preset apply-set with exclusions (hero excluded), outliers against the
  hero, export release; counts per step; stops at the first refusal or
  partial step (`stopped_at`), no rollback; dry run without `--yes`.
- README: "Setup" rewritten; new "Editing with Scouter from Claude Code"
  user guide (six example sentences checked against live vocabulary rows by
  `tests/test_user_guide.py`), mirrored to the vault; "Honest limits,
  product 1.0rc".
- Version `0.3.0` -> `1.0rc1`. `PHASE` stays `3` (an integer column in
  `metrics.db`).

## Unreleased (phase-3-task10: host run)

- Phase 3 task 10 host run on darktable 5.4.1 (details in
  `docs/measurements.md`, "Phase 3 host run"). All 15 candidate vocabulary
  rows answered their host probe, and their action paths are pinned in
  `dtbridge.ACTION_PATHS`. 17 XMP slots are pinned by the sentinel method in
  `xmp.LAYOUTS`: colour balance rgb v5, sigmoid v3 skew, new filmic rgb v6
  and color zones v5 layouts. `scouter vocab list` now shows 22 live, 1
  absolute_only (tint), 1 unreachable (color zones curves), 0 candidate.
- Colour balance rgb hue rows declare darktable's module default (0°), so
  "warm the highlights", "cool the shadows" and "teal and orange" run on an
  image with no colour-balance history and undo back to 0°.
- `moves._set_recording_before` now records the neutral/default before-value
  on ANY Scouter error raised after the write landed, not only
  `CommitTimeout`, so a bridge timeout mid-commit no longer wedges
  `undo last`.
- Host probe method: it waits 20 s after darktable comes up and proves a
  known path answers before it judges a candidate. Probing earlier reported
  every path, including pinned ones, as a wedge.
- Open finding: a one-move `darkroom say` has a median of 4.1 s with the
  pre-commit bounce, which misses the 3.5 s target. With `--trust-sidecar`
  the median is 1.4 s.

## Unreleased (fix/e12-findings)

- CLI/README drift (D2): `cull propose`, `outliers list` and `export release`
  now accept `--pretty` after the group/verb, the same as the README's phase
  2 examples show, in addition to `scouter --pretty <group> <verb>`.
  `style apply-set --name ..`, `export release --job-slug ..` and
  `export verify --manifest ..` now work as options as well as the existing
  positional form. A new test (`tests/test_readme_cli_parity.py`) parses
  every `scouter ...` line shown in README.md against the real CLI parser.
- Cull floor made relative per job (D1, row 407 call 2): `cull propose`'s
  singleton sharpness gate is now a percentile of the job's own sharpness
  distribution (`cull.sharpness_keep_top_pct`, default keeps the top 60%),
  not the fixed `cull.sharpness_floor` (0.20) real raws never cleared in the
  E12 live run (measured 0.003-0.04). `sharpness_floor` is kept only as a
  diagnostic field. `cull propose` now renders every frame from a scratch
  copy with no adjacent `.xmp`, so it scores the frame as imported rather
  than its current edited history (editing a hero no longer moves its
  burst's pick on the next proposal).
- Mixed-body import fixed (D5/D8, row 407 call 1): `library import` labels
  every file from its own EXIF body rather than refusing a mixed card, or
  (once `--camera` got past the refusal) mislabelling every file with the
  one chosen slug. `--camera X` now filters to that body and reports the
  rest in `skipped_other_camera`. `import.json`'s top-level `camera`,
  `cameras`, `camera_model`, `camera_models`, `totals` (`job_files`,
  `job_bytes`) and `source_roots` now describe the whole job across every
  import it has ever had, not just the run that just finished.
- `wb kelvin` refusal made actionable for a camera with no committed
  `temperature` module (row 407, the A7 case): the reason now also names a
  missing calibration table and points at `scouter wb calibrate`, the same
  as the no-table refusal does on its own, instead of only reporting the
  missing XMP module and leaving the table question unanswered.
- `darkroom open` on a same-session import now reports the real cause (D4,
  row 407): the wrong-image-loaded classifier in `scouter/core/dtbridge.py`
  was anchored to the start of the message (`^wrong image loaded:`), but
  Lua's own `error()` prepends a `source:line:` position prefix the Lua
  plugin never asked for, so the anchored match never fired and the refusal
  fell through to the generic `bridge_timeout` ("start darktable ...
  restart darktable" -- wrong, darktable is running). It is now
  `not_in_darkroom` (exit 6) with a fix naming the real cause: open the
  frame in darktable yourself or relaunch darktable on it.
- Bounded wait on the job state lock (D3, row 407 -- **hardening, not the
  D3 fix**): `state/<job>.json.lock` used to block on a plain
  `flock(LOCK_EX)` with no timeout at all. Now polls with a bound
  (`cache.lock_wait_s`, default 30s) and fails with a new `state_locked`
  error (exit 22) naming the pid holding the lock and since when, instead
  of waiting indefinitely. This is general hardening against lock
  contention; it is NOT what caused the E12 live run's two 15-18 minute
  stalls -- the evidence shows both happened before the verb was ever
  reached (one inside the bridge call, before this lock is taken; the
  other in a verb that never takes this lock at all), and each lock hold
  is microsecond-scale. **D3's root cause is still open** (README honest
  limits).
- `library import`'s auto date derivation no longer trusts a single outlier
  (D6, row 407): the earliest capture time is used only when it is within
  30 days of the card's median capture time; a bad clock (e.g. an unset RTC
  defaulting to 2013-01-01) falls back to the median with a warning,
  instead of setting the whole job's date. A normal midnight-spanning card
  is unaffected.
- `cull apply` no longer refuses the whole proposal over one stale frame
  (D7, row 407): a frame whose sidecar changed since `cull propose` wrote
  it (merely opening it in darktable is enough) is now skipped and listed
  in the new `stale` result field, with a warning; every other frame is
  still applied.
- `darkroom undo-last` on an empty journal, called for real (not a dry
  run), no longer reports `outcome: dry_run` / `ok: true` (row 408 B2,
  editor benchmark): it now reports a new "noop" outcome (exit 23), which
  `ok = outcome in ("ok", "dry_run")` correctly excludes from success. A
  genuine dry run is unaffected.
- `darkroom enable`/`darkroom reset` now refuse an unpinned module (row 408
  B6) before any bridge call, journal entry or XMP read: a new
  `dtbridge.MODULE_WHITELIST`/`require_known_module` mirrors `ACTION_PATHS`'
  reasoning -- an unrecognised `iop/<module>` wedges darktable's Lua worker
  for the whole session.
- `darkroom set` now refuses an unpinned `(module, param)` before any
  bridge call too (row 408 B8): `action_path` is computed once, up front,
  instead of only inside the real `darkroom.action` call after
  `darkroom.current`, `view.current` and an XMP read had already run.
- Documented, not fixed (row 408, no code change): `style apply-set` can
  return before darktable has written every sidecar (B7, 0.9-1.35s
  measured lag, no bridge-side completion signal exists to poll on) and
  the first `history commit` after a fresh darktable launch can wedge the
  bridge outright (B1, 5/13 sessions). Both are now README honest limits;
  neither is in scope here (B7 needs a real bridge signal that does not
  exist, B1 needs a launch-state isolation harness this branch does not
  have).
- Documented, not fixed: `config init` does not install any WB calibration
  table (E12 D9's remaining half, after fix 4 addressed the A7 refusal
  message) -- a table only ever reaches `<config_dir>/wb/<camera>.json` via
  `scouter wb calibrate` or a manual copy. Left for a real task (bundling
  and packaging the tables) rather than a bolt-on to `config init` here.

### Fix round 1 (independent review)

- **Blocker:** `cull propose`'s percentile threshold used to be computed
  over EVERY frame, including ones whose stats render failed (`sharpness`
  stand-in 0.0, never a real measurement) and ones with no capture time.
  Probed: with 4/10 renders failed, ALL 6 real frames were proposed
  `pick_rating` (100%, not the configured 60%); with 5/10 or all 10 failed,
  the threshold collapsed to 0.0, and every frame -- including the failed
  ones -- was proposed a pick. Both kinds of frame are now excluded from
  the percentile, and a `stats_error` frame is never proposed `pick_rating`
  regardless of what the threshold computes to.
- **Should-fix:** a file with no camera model in its EXIF at all used to
  abort the WHOLE card (`exif.camera_slug` now runs per file, before
  `--camera` is applied, so it raised even WITH `--camera` given -- the
  raised message "and no --camera was given" was then false). With an
  explicit `--camera X` such a file is now imported under X
  (`assumed_camera`); without one it is skipped (`skipped_no_body`), never
  the whole card.
- **Should-fix:** corrected the D3 (row 407) wording everywhere it appeared
  (this file, README, `errors.py`, `config.py`, `paths.py`): the bounded
  lock wait is hardening against lock contention in general, not the fix
  for the E12 live run's own two 15-18 minute stalls -- the evidence shows
  both happened before the verb was ever reached. D3's root cause stays
  open in the README's honest limits. Added `metrics.instrumented`'s new
  `startup_ms` (time from process start to a verb's own start, in every
  events.jsonl/audit line) so the next such stall is attributable.
- **Should-fix:** MCP `library_import`'s `camera` and `date` field
  descriptions (`scouter/mcp/schemas.py`) still said the old semantics
  ("camera slug override; required for a mixed-camera card", "default: the
  earliest capture time") -- agent-facing parity drift against the CLI
  help text, which D5/D6 already updated. Copied the new wording to both
  layers and regenerated `tests/fixtures/scouter_mcp_golden.json`.
- **Should-fix:** documented the percentile gate's two edge cases (README
  honest limits) -- a uniformly soft job still picks its top 60% (the gate
  cannot detect a bad job), a uniformly sharp job never rejects its
  softest 40% (a burst runner-up is not a bad frame). `cull propose` now
  adds a warning to the proposal when the job's p10-p90 sharpness spread is
  small, in either case; a `stats_error`/no-capture-time frame is excluded
  from the spread computation too, consistent with the blocker fix.
- **Should-fix:** `darkroom undo-last`'s "noop" reason (row 408 B2) now
  names the real cause, not just the symptom -- each call gets a fresh job
  unless `$SCOUTER_JOB`/`job=` is pinned across calls; the MCP server
  already pins one for you (phase 3 task 9's session job). `registry.call` also gives a "noop"
  outcome a structured `error.fix` over every layer (it is a RESULT, not a
  raised error, so it previously had nowhere else to put one) -- MCP now
  surfaces the same actionable hint the CLI's exit code alone could not.
- **Nit:** removed the dead `except ManifestCorrupt` around
  `import_card`'s prior-manifest read -- `_plan()` already reads (and
  would raise on) this same `import.json` earlier in the same run, so the
  catch never fires on the normal path, and would have silently dropped
  `source_roots` rather than surfacing a genuine corruption on the
  vanishingly narrow race where it could.
- **Nit:** burst-comparison reasons ("sharpest of N in burst...", "softer
  than the pick...") now print sharpness at `.4f`, like the singleton
  reasons already did -- at `.2f`, real ORF/ARW renders (0.003-0.04)
  printed as "0.02 vs 0.02", indistinguishable even when the burst's pick
  won by a real margin.
- **Nit:** the CLI/MCP `date`/`camera` help text for `library import` now
  says the job's date is derived from the WHOLE card before `--camera`
  filters it -- `--camera nex5n` on a mixed A7+NEX-5N card still takes its
  date from the A7 files too. Acceptable behaviour (a legitimate multi-day
  trip now gets the median date with a warning, per D6), just undocumented
  before this. Regenerated the golden MCP contract snapshot.

### Fix round 2

- A card where NOT A SINGLE file has a camera model in its EXIF, imported
  without `--camera`, now refuses (`UsageError` naming every file, fix
  "pass --camera X") instead of silently succeeding with an empty job
  directory. Round 1 correctly made a no-body FILE never fail the whole
  card (skipped, listed in `skipped_no_body`, the rest still imports) --
  this only fires when NOTHING is left to import; a card with some bodies
  present keeps round 1's behaviour unchanged.
