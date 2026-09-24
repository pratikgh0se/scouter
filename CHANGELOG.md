# Changelog

Scouter has no prior changelog; this file starts with the `fix/e12-findings`
branch, which fixes defects found by the E12 live evaluation against real
darktable (`Knowledge/Scouter - E12 live evaluation 2026-09-24.md` in the
vault; decision row 407).

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
