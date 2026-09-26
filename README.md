# Darktable MCP Server

A Model Context Protocol (MCP) server that exposes darktable operations
to MCP clients (Claude Desktop, Claude Code, etc.). The AI lives in the
client; this server drives darktable.

## Tools

**Library operations** (require `darktable-mcp install-plugin` and an open darktable session):

- `view_photos(filter?, rating_min?, limit?)` — Browse the library by filename substring and minimum rating. Returns id, filename, **absolute file path**, and rating per match — the path drops straight into `export_images`'s `photo_ids`.
- `rate_photos(photo_ids, rating)` — Apply -1..5 star ratings (-1 = reject, 0 = unrated).
- `import_batch(source_path, recursive?)` — Register a folder as a film roll. `recursive=true` is honoured in Lua by walking the tree and importing each directory, so it works regardless of darktable's `recurse_directories` preference. `recursive=false` cannot *stop* darktable recursing, so the response says so (`recursive_honoured: false`) rather than claiming a mode it did not deliver. If darktable's background scan has not settled when the plugin answers, the count is reported as a floor, not a total.
- `list_styles()` — Enumerate installed darktable styles (presets), returning name + description per entry.
- `apply_preset(photo_ids, preset_name)` — Apply a named darktable style to one or more photos. Use `list_styles` first to discover exact names.

**Camera ingest** (headless):

- `import_from_camera(destination?, camera_port?, timeout_seconds?)` — Detect a camera via libgphoto2 and copy photos to a local directory. Auto-merges hybrid setups (one card on PTP, the other mounted as USB Mass-Storage) into a single import — Nikon DSLRs in particular show up that way and the previous behavior silently halved the import.

  Files land **one subdirectory per camera folder or card, prefixed with the camera's identity** — never flat:

  ```
  <destination>/Nikon_D850_sn_30014567_store_00010001_DCIM_100NCD80/DSC_0001.NEF
  <destination>/Nikon_D850_sn_30014567_store_00020001_DCIM_100NCD80/DSC_0001.NEF   # same name, different photo
  <destination>/Canon_EOS_R6_EOS_DIGITAL_100EOSR6/IMG_0001.CR3
  <destination>/.import.log
  ```

  Camera filenames repeat across folders, across the two cards of a dual-slot body, and across *bodies* importing into the same destination — and the default destination `~/Pictures/import-YYYY-MM-DD/` is shared by every import on the same day. The old flat layout combined with gphoto2's `--skip-existing` silently dropped those duplicates; correctness no longer rests on that flag, which now only ever sees files the same run just wrote into its own private staging directory. Import the destination recursively.

  The serial number is read once per camera via `gphoto2 --get-config serialnumber` and omitted when the camera doesn't report one. On both paths a file that would collide with a *different* photo already on disk is written alongside it as `IMG_0001-2.CR3` and reported — never overwritten; sameness is judged on size plus the first and last 8 KB, and is only ever used to authorise a skip.

  Two bodies of the same model that report no serial share a destination subdirectory, and **both bodies' photos are kept there**. The PTP path downloads into a private staging area and, before skipping files the destination appears to already hold, re-checks a bounded sample of them against the bytes on disk — a second body fails that check and its folder is fetched in full. Re-running stays cheap: a body with a serial transfers nothing it already delivered, and one without transfers at most 3 files per folder. Any file the card lists that doesn't reach the destination is reported by name.

  **Residual limit:** that sample is bounded at 3 files per folder, so a second body whose sampled files are byte-identical to the first body's — in a folder holding more than 3 candidates — is still taken to be the same body. Folders with 3 or fewer candidates are checked exhaustively.

  `timeout_seconds` is an **overall budget for one camera**, shared across all its folders — not a per-folder timeout. Skipped files are counted and reported, and a post-flight shortfall (fewer files on disk than the camera said it held) is surfaced as a prominent `!! INCOMPLETE IMPORT` block, because that is the moment before someone formats the card.

**Vision-rating workflow** (headless, file-based — no library required, needs `[vision]` extra):

- `extract_previews(source_dir, output_dir?, max_dim?, thumb_dim?, overwrite?, max_workers?)` — Pull auto-rotated JPEG previews + small thumbs out of raws (NEF/CR2/ARW/DNG/...), with an EXIF summary per file. Per-file details (paths, EXIF, errors) land in `<output_dir>/.extract_previews.jsonl`; the tool response keeps only counts and the side-file path so 700+ NEFs don't overflow the agent's context. The scan is **recursive** and the output tree **mirrors the source tree**, so same-named raws in different subdirectories get distinct previews — read the path from each item rather than assuming `<output_dir>/<stem>.jpg`. Decoding runs on a thread pool (`max_workers`, default `min(8, cpu_count)`).
- `apply_ratings_batch(source_dir, ratings, log?, force?)` — Write XMP `xmp:Rating` sidecars for a `{stem: rating}` batch + an append-only `ratings.jsonl` log. Keys may be a bare stem or a source-relative path; a bare stem that matches raws in more than one subdirectory is rejected as ambiguous rather than resolved by guesswork.
- `open_in_darktable(source_dir, rating?, rating_min?, rating_max?)` — Launch the GUI on a folder. Auto-registers as a film roll; pre-applies any rating filter (exact, ≥, ≤, or inner range) via `dt.gui.libs.collect.filter`.

### Behaviour worth knowing

**Existing XMP sidecars are patched, never replaced.** `apply_ratings_batch` rewrites only the rating value and leaves every other byte intact, so darktable edit history survives. A sidecar with no recognisable rating is skipped with an error instead of being overwritten. `force=True` opts into wholesale replacement and **discards edit history** — it exists for the "reset these sidecars" case and nothing else.

**Ratings for photos darktable already knows need one manual step.** darktable prefers its own `library.db` over the sidecar for images already in the library, so writing a sidecar changes nothing on screen. When this is detected the summary warns and names the affected files; run *selected image(s) → read sidecar files* in the lighttable to pull them in.

**`open_in_darktable` no longer claims a launch it didn't get.** An already-open darktable is the *normal* state when the bridge-backed library tools are in use, and it holds the lock on `library.db`. What darktable then does is platform-dependent, and neither branch is a launch:

| | behaviour | reported as |
|---|---|---|
| Linux (session D-Bus present) | hands the folder to the running instance, child exits 0 | `handed_off_to_running_instance: true` — look at the open window |
| macOS (no session D-Bus) | handoff fails on a GLib assertion and the child **hangs forever** | `launched: false` with the reason; the hung child is killed |

The tool used to report a phantom pid in both cases. Detection reads what darktable *says*, not whether the process is alive — on macOS it stays alive indefinitely, so liveness proves nothing. Verified against darktable 5.6.0.

**Export:**

- `export_images(photo_ids, output_path, format, quality?, max_width?, max_height?)` — Export to JPEG/PNG/TIFF via `darktable-cli`. Runs in an isolated config dir under `$XDG_CACHE_HOME/darktable-mcp/cli-config/`, so exports work even when the GUI is open (no `database is locked` race against the user's `~/.config/darktable/library.db`). Files export in parallel, and the output is `stat`ed afterwards — darktable-cli can exit 0 without writing anything, which used to be reported as success. Per-file results land in `<output_path>/.export_images.jsonl`; the tool response is bounded — counts, side-file path, and the first error if any.

  Output names are **de-collided**: two inputs with the same stem from different folders no longer overwrite each other, so read the real path from the `output` field rather than assuming `<stem>.<format>`. Note darktable-cli picks the extension itself — `jpeg` writes `.jpg` and `tiff` writes `.tif`. Each parallel worker gets its own config dir, because concurrent `darktable-cli` processes sharing one contend for the same `library.db` and one of them silently writes nothing.

  **Sidecar caveat:** because the config dir is isolated from the GUI's, exports read develop settings from XMP sidecars only. If darktable's *write sidecar file for each image* preference is off, files export **without their edits** and darktable-cli still reports success.

## Design rules

Use only the official darktable APIs: `darktable-cli` for export, the Lua API for everything else. No direct `library.db` reads or writes. Tools that return data to the AI must be headless; the GUI may launch only when the tool's purpose is to show the human something.

## Why some tools are parked

`darktable-cli` doesn't load the user's library and `darktable --lua` brings up the full GUI, so there's no headless one-shot path for library reads/writes. Iteration 2 (spec: `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`) shipped a long-running Lua plugin loaded into the user's interactive darktable session, with a file-based JSON RPC bridge. The library tools (`view_photos`, `rate_photos`, `import_batch`, `list_styles`, `apply_preset`) all ride on it.

`adjust_exposure` was retired during iteration 3 — see `docs/superpowers/specs/2026-04-28-iter3-design.md`. The darktable Lua API in 9.6.0 exposes neither `image.modules` nor `image.history`, and `dt.gui.action` requires an active darkroom view (single-image, GUI-driven). The realistic future paths (pre-created `.dtstyle` exposure presets + `apply_preset`, or `darktable-cli --style` for export-only) are workable but not "set +N EV from Lua" tools.

## Installation

Not on PyPI yet — install from the repository:

```bash
pip install 'git+https://github.com/w1ne/darktable-mcp'
# Optional: vision-rating workflow extras
pip install 'darktable-mcp[vision] @ git+https://github.com/w1ne/darktable-mcp'
# Install the Lua plugin into ~/.config/darktable/, then restart darktable
darktable-mcp install-plugin
```

You also need `darktable` (with `darktable-cli`) on `PATH`. The `[vision]` extra pulls in `rawpy`, `Pillow`, and `pyexiv2`, which need system `libraw` and `libexiv2`.

## Configuration

Add to your Claude Desktop config:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "darktable": {
      "command": "darktable-mcp"
    }
  }
}
```

## Vision-rating workflow

When darktable's library doesn't yet know about your shoot — typically straight off a card — you can rate by vision before any import:

1. `extract_previews` writes auto-rotated JPEGs and an EXIF summary so the client can iterate efficiently.
2. The client reads previews, decides ratings, and calls `apply_ratings_batch` to write XMP sidecars next to the raws.
3. `open_in_darktable` launches the GUI with the folder as a film roll, lighttable filtered to the rating range you want.

No SQLite poking, no half-imported state, no GUI launch until step 3.

## Requirements

- Python 3.10+ (the floor comes from `mcp`, which requires 3.10)
- darktable 4.0+ (with `darktable-cli` on `PATH`)
- An MCP-compatible client (Claude Desktop, Claude Code, etc.)
- Linux, or macOS for the parts that don't need `gphoto2`. The plugin installer writes to `~/.config/darktable/`, which is where darktable keeps its config on Linux and macOS but **not** on Windows — `import_from_camera` also needs `gphoto2`, which has no Windows build.

The MCP SDK is pinned to `mcp>=2,<3`. Tools are registered through the mcp 2.x low-level `Server(on_list_tools=…, on_call_tool=…)` handlers; the 1.x decorators this server used before do not exist in 2.x, so **mcp 1.x cannot run this code**.

The low-level API is deliberate. The high-level `MCPServer` derives each `inputSchema` from the handler signature, which cannot express `additionalProperties: {type: integer, minimum: -1, maximum: 5}` — the rating bounds on `apply_ratings_batch` silently disappear — and it injects a `title` into every property. The tool schemas here are handwritten and pinned by a golden-snapshot test, because their descriptions are what the calling model reads to decide behaviour.

## Scouter (live darktable editing core)

Scouter is the fork's own product: a live editing core for the **running**
darktable, driven by conversation or by script. It sits on top of this repo's
bridge (the Lua plugin and the file-spool JSON-RPC client), which is unchanged
and still serves the original `darktable-mcp` tools.

Three layers over one core, with the same verb names, arguments and result shape
in all three:

| Layer | How you use it |
|---|---|
| Python | `from scouter.core import darkroom; darkroom.set_param(module="exposure", param="exposure", value=1.2)` |
| CLI | `scouter darkroom set exposure exposure 1.2` — one JSON object on stdout, one exit code per error class |
| MCP | the `scouter-mcp` server, tools `darkroom_set`, `scout_stats`, … (handwritten schemas, golden-snapshot tested) |

Add the MCP server to your client's config alongside (or instead of) `darktable`:

```json
{
  "mcpServers": {
    "scouter": {
      "command": "scouter-mcp"
    }
  }
}
```

### Setup

```sh
uv sync --extra dev                 # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m scouter setup   # installs the Lua plugin (persistently), checks doctor,
                                    # prints the `claude mcp add` line
# restart darktable, open a photo in the darkroom, then:
scouter setup --check               # every item ok? exit 0; anything missing? exit 1, changes nothing
```

`scouter setup` is idempotent and does, in order:

| Item | What it does | `--check` reports |
|---|---|---|
| `plugin` | writes the packaged `darktable_mcp.lua` where the bridge reads it (`~/.config/darktable/lua/`, or `$DARKTABLE_MCP_PLUGIN_PATH`) and **leaves it installed** -- no uninstall on exit, unlike the host tests | `ok` / `missing` / `stale` (an older plugin) |
| `luarc` | adds `require "darktable_mcp"` to `~/.config/darktable/luarc` once, keeping every other line | `ok` / `missing` |
| `wb_tables` | copies the measured white-balance tables shipped in `scouter/data/wb/` (today: `em10iii`) into `<config_dir>/wb/` when that camera has none; never overwrites one | `ok` / `missing`, plus any other calibrated bodies |
| `config` | reported only: `scouter config init` writes it, because the backup gate is yours to arm. Single-frame editing works without it | `ok` / `missing` (not required) |
| `doctor` | the bridge half of `scouter doctor`: darktable running, plugin loaded, answering | `ok` / `bridge_timeout` / ... |

It then prints the exact `claude mcp add` command and the equivalent JSON
(see "Editing with Scouter from Claude Code" below). It never reads or writes
Claude Code's own config; registering is one command you run yourself.
The exit code is `0` when every required item is ok. Right after a fresh
install `doctor` is not ok until darktable is restarted -- the `next` list in
the output says so.

`darktable-mcp install-plugin` still exists and writes the same two files.

darktable must be **running**, with the image open in the **darkroom**: slider
actions exist only there, and `scouter` says so (`wrong_view`, exit code 4)
instead of failing quietly.

Leave darktable's own *store XMP sidecar files* preference at a setting that
writes sidecars (the default *on import and after edit*): the XMP sidecar is
how Scouter reads every value back. Scouter **never reads or changes that
preference** — it cannot, from Lua (a `dt.preferences.write` for it only
creates a script-namespaced key and leaves the core setting alone, which an
earlier round proved the hard way). With sidecar writing off, a commit simply
never lands and `scouter` reports `commit_timeout` (exit 10) rather than
claiming a write it cannot see.

The plugin loads through `~/.config/darktable/luarc` (`require "darktable_mcp"`),
same as the original `darktable-mcp` tools.

### A hero edit

```sh
scouter darkroom open ~/Pictures/EditingTrial/P6281348.ORF
scouter darkroom set exposure exposure 1.2
scouter darkroom set sigmoid contrast 1.35
scouter --pretty scout stats ~/Pictures/EditingTrial/P6281348.ORF --px 1024
scouter darkroom undo-last
scouter --pretty history read ~/Pictures/EditingTrial/P6281348.ORF
```

`--pretty` and `--job` belong to `scouter` itself, so they go **before** the
group and command (`scouter --pretty scout stats ...`); per-verb flags such as
`--px`, `--dry-run` and `--yes` go after it. `--pretty` is also accepted
*after* the group/command (`scouter cull propose --job-slug .. --pretty`, as
the phase 2 examples below show) — both positions work the same way.

Every write is one darktable history item you can see and undo yourself.
`undo-last` sets the parameter back to its previous value, which adds a history
item rather than rewriting history — so your own Ctrl-Z still works.

### Safety

- Writers take `--dry-run`; gated verbs (`darkroom reset`, `style extract` over
  an existing name, `export run` over existing files or more than one image)
  print the plan and do nothing until `--yes`.
  `style apply` always **appends** to history; `--mode overwrite` is not a
  gate but a refusal — it fails immediately with `unsupported_mode`, before
  anything reaches darktable, because no `--yes` can make darktable replace a
  history it has no API to replace.
- Scouter never renders its own pixels, and never touches `library.db`.
  Pixels come from `darktable-cli` in an isolated config dir; edits go through
  `dt.gui.action`; history is read from the XMP sidecar.

### Honest limits

These are load-bearing, not incidental — read them before scripting against Scouter:

- **Row 461: on darktable 5.4.x `auto` is the view bounce only; `no_bounce`
  is opt-in and unsafe there.** Gauntlet night1-20260926 found 15 writes where
  `no_bounce`'s rating touch rewrote the sidecar from an arbitrary *older*
  library.db state (history_end 112 -> 92, 145 -> 35, ...) and the row 430
  "history changed" check accepted it. So `auto` now goes straight to
  `view_bounce_action` on 5.4.x (and when the version is unreadable); only
  `--strategy no_bounce` still runs it. On every path a rewrite whose
  history_end went back is refused (`commit_timeout` with
  `detail.stale_rewrite`), and a stale `no_bounce` rewrite puts the
  pre-commit sidecar back (`stale_rewrite_restored`). darkroom writers also
  pass the module they touched, and a rewrite counts only when the newest
  history item is that module's new state (row 434). The row 429/430 notes
  below describe the old `auto` order and are superseded on 5.4.x.
- **The commit trigger: `no_bounce` first, a view bounce as the fallback --
  but `no_bounce` does not land the edit on darktable 5.4.1 (row 430).**
  darktable writes the XMP sidecar only when the history is flushed (leaving
  the darkroom, switching image, autosave, "synchronize selection") -- not per
  slider move (phase 0 finding 3). `history commit`'s default
  (`--strategy auto`) tries `no_bounce` first: select exactly the image, press
  the darkroom's "synchronize selection" (flushes history to library.db), and
  re-assign the image's rating (darktable rewrites the XMP from the DB) --
  the view never moves. Row 429 took "the sidecar file changed" (mtime/size)
  as proof that this committed. It does not: on this host the "synchronize
  selection" flush did not carry the darkroom's in-memory edit into
  library.db, so the rating touch rewrote the XMP with the *old* history and
  a fresh timestamp -- the file changed, nothing landed. The fix (row 430,
  a2b9640): a commit counts only when the XMP's history fingerprint itself
  changed; `no_bounce` now reports "not written" on a metadata-only rewrite,
  and `auto` falls back to `view_bounce_action`: out to the lighttable and
  back through darktable's own "switch views" commands. That view bounce is
  the commit that actually lands an edit today, and it costs roughly 1-3 s
  more per write than row 429's (false) `no_bounce` numbers -- see row 430 in
  `docs/measurements.md` for the live-check timings. Either way the commit is
  confirmed by watching the sidecar's history, never assumed from the file's
  mtime/size. darktable writes a sidecar only when the history actually
  changed, so a write asking for the value the image already has produces no
  write; Scouter resolves that by reading the value back and says so in a
  warning, and still reports `commit_timeout` when the value genuinely is not
  there. `--strategy` forces any one strategy (`no_bounce`,
  `view_bounce_action`, `view_bounce` -- the same bounce on the plugin's
  default transport -- or `sidecar_action`).
- **Row 418 crash: closed (row 429).** The editor benchmark (row 408 B1)
  measured the FIRST `history commit` after a fresh darktable launch
  intermittently wedging the bridge (5 of 13 sessions: no MCP answer in 30 s,
  then `view.current` itself dead). That hang WAS the row 418 crash:
  darktable runs every Lua call on its Lua thread pool, and the old bounce
  called `dt.gui.current_view(v)`, whose view switch can run directly on that
  pool thread; on macOS that segfaults (SIGSEGV in
  `dt_ctl_switch_mode_to_by_view`, thread `pool-1`), and whatever was waiting
  on the bridge saw a hang. Every view switch (commit bounce,
  `darkroom open`, the bounce's return leg) now presses darktable's own
  "global/switch views/<view>" command through `dt.gui.action`, which
  darktable runs on the GTK main thread. Host run 2026-09-26: three action
  bounces and two `no_bounce` commits, 0 new crash reports. The plugin never
  calls the `dt.gui.current_view` setter any more; an unknown view transport
  is refused, not attempted. (A first fix, a Lua-shortcut "main-thread
  proxy", was removed: darktable refuses a lua-owned action pressed from Lua.)
  `scouter doctor --probe-main-thread [--switch-views]` reports each route.
- **`darkroom status` reports slider *positions*, not values.** `dt.gui.action`'s
  read-back is a normalised 0..1 position in the slider's visible range, and
  phase 1 has no verified position→value table for every slider — use
  `history read` (the XMP) for actual values; `status` is a liveness/reachability
  check and the `wrong_view` signal, nothing more. Use `darkroom get` (phase 3)
  for a parameter's value.
- **`style extract --no-filter` keeps the whole history.** Since phase 2 the
  default `--modules` path filters for real (`modules_honoured: true`); only
  `--no-filter` captures every active operation (minus `never_include`), and
  says so.
- **`style apply --mode overwrite` is refused, not downgraded.** darktable's
  Lua API only appends a style (`image:apply_style`), so overwrite is rejected
  with `unsupported_mode` before any bridge call — there is no confirmation
  that makes it work, and it never silently falls back to append. An
  export-only overwrite is `darktable-cli --style-overwrite`.

### Observability

Every call in every layer writes one event to `~/.cache/scouter/events.jsonl`
and one row to `~/.cache/scouter/metrics.db`, plus a full-fidelity audit line to
`~/.cache/scouter/audit/<job>.jsonl`.

```sh
scouter --pretty metrics summary --since 7d
```

### Exit codes

`0` ok · `1` error · `2` usage · `3` bridge_timeout · `4` wrong_view ·
`5` confirmation required (a dry run was printed) · `6` not_in_darkroom ·
`7` cli_wrote_nothing · `8` db_locked · `9` style_partial / partial ·
`10` commit_timeout · `11` backup_not_configured · `12` checksum_mismatch ·
`13` destination_collision · `14` exif_unreadable · `15` style_write_failed ·
`16` encryption_unavailable · `17` manifest_unauthenticated ·
`18` export_all_failed · `19` no_sidecar · `20` value_unknown ·
`21` not_pinned · `22` state_locked · `23` noop
(`darkroom undo-last` with nothing to undo, on a real, non-dry call) ·
`24` darktable_unresponsive · `25` write_unverified (a committed write whose
read-back value is not the one asked for, and no darktable limit explains it)

### Tests

```sh
python3 -m pytest tests -q          # no darktable needed; host-only tests skip
SCOUTER_HOST_TESTS=1 python3 -m pytest tests/test_host_integration.py tests/test_commit_trigger_host.py -q -s   # needs darktable + the ORF, and this env var
lua tests/lua/test_dispatcher.lua   # the Lua plugin's own harness, no darktable needed

# Phase 2's end-to-end job: 30 real ORFs, one darktable session, E1-E9 + metrics
SCOUTER_HOST_TESTS=1 python3 -m pytest tests/test_host_phase2.py::Phase2EndToEndTest -q -s
```

#### Test-honesty gate (`mutation_check`)

```sh
make gate BASE=<sha> HEAD=<sha>      # what the HTC merge step (Task 36 Step 2) calls
scripts/merge_gate.sh <base> <head>  # what `make gate` execs
```

`make gate` -> `scripts/merge_gate.sh` -> `scripts/mutation_check.sh` ->
`tests/mutation_check.py` is the test-honesty check capsule-corp's merge gate
runs, mirrored here per capsule-corp decision row 459 / card t_a995978e: a new
or changed test that can never fail proves nothing. For every new/changed
test file in the range it (b) reverts the production change and requires at
least one of that file's new/changed tests to fail, (c) weakens the first
`assertEqual`/`assertTrue`/bare `==` inside one of those tests and requires
the file to fail that way too, and (d) statically flags no-assert, a new
`@pytest.mark.skip`/`skipif`/`xfail`, a bare `except: pass`, or
`assertTrue(True)`. An import/collection error in a changed test module reads
`ERROR (not discriminating)`, never `FAIL (good)` - a module that could not
even be imported never actually exercised anything. It runs with
`-k "not host"` throughout, so a changed test that only a real-darktable
(`host`) test covers reads `n/a`, never a failure. Exit non-zero (with the
markdown table naming which file(s) didn't discriminate) is what the merge
step treats as a block.

Skippable ONLY by an explicit env var, always logged (mirrors capsule-corp's
`OPS_SKIP_MUTATION_CHECK` exactly): `SCOUTER_SKIP_MUTATION_CHECK=1 make gate
BASE=.. HEAD=..` skips the check and prints a tagged `WARN` line to stderr
naming the base/head - there is no silent skip path. Self-tested against a
tiny fixture repo built in tempfile, including that `make gate` and
`scripts/merge_gate.sh` actually invoke the real script end to end:
`tests/test_mutation_check.py`.

(scouter has no `docs/ZENKAI.md` mirror of capsule-corp's reliability
doctrine yet - this note lives here instead, per the same "note the repo
section, README as fallback" instruction that seeded this script.)

The host-only suite (`tests/test_host_integration.py`,
`tests/test_commit_trigger_host.py`) launches the **real** darktable against
`/Users/pratik/Pictures/EditingTrial/P6281348.ORF` and always quits it when
done — it is gated on `SCOUTER_HOST_TESTS=1` so it never runs by accident in
CI or a plain `pytest tests`.

Design: `docs/superpowers/specs/2026-09-13-scouter-phase1-design.md`.
Measurements: `docs/measurements.md`.

## Scouter phase 2 (volume: a whole job, delivered)

Phase 1 made one frame editable by conversation. Phase 2 makes a **job**
deliverable: a card becomes a named, checksummed, backed-up film roll; a
proposal culls it; one hero's recipe becomes a style that lands on the whole
set; the frames outside tolerance are listed by number; proofs and finals
leave with a manifest. Every step is a deterministic core function that runs
with **no model**.

### The config file

`~/.config/scouter/config.json` (override with `$SCOUTER_CONFIG`, or
`$XDG_CONFIG_HOME/scouter/config.json`). JSON rather than TOML on purpose:
`tomllib` is stdlib only from Python 3.11 and this package's floor is 3.10, so
TOML would mean either raising the floor or adding a dependency.

```sh
scouter config init          # writes a commented starter file, backup DISARMED
scouter --pretty config show # what is loaded, and whether the backup gate passes
scouter doctor               # + config_path, backup_gate, encryption_tool, library_root
```

Every key has a default except `library_root`, which has none — a config
without it fails every library verb with `usage`. **Unknown keys are
rejected**, with the nearest known key suggested: a typo in `backup.target`
must not silently disarm the gate.

### The job layout

```
<library_root>/
  2026/
    2026-06-28 harbour-wedding/
      raw/          the copied, renamed originals (0444) + darktable's .xmp sidecars
      exports/
        client-proof/  client-final/  print-tiff/     one directory per preset
      .scouter/
        import.json            what was copied, from where, with what EXIF
        checksums.sha256       coreutils-format sha256 lines over raw/
        cull-<ts>.json         the proposal `cull apply` consumes
        styles-applied.json    the per-job style idempotency ledger
        manifest-<preset>-<ts>.json   + .sha256, so the manifest itself verifies
  scouter/memory/   reserved for phase 3, empty, inside the backup set
```

Everything Scouter writes inside a job uses paths **relative to the job
directory**, so a job is portable; the only absolute path Scouter holds is
`library_root`, and `library relocate` rewrites that one key and re-verifies.

### The six new CLI groups

```sh
scouter config  init | show
scouter library import <card> --job-slug harbour-wedding --yes
scouter library verify harbour-wedding | jobs | register | relocate --to <root>
scouter cull    propose --job-slug harbour-wedding --pretty
scouter cull    apply   --job-slug harbour-wedding --yes
scouter style   extract <hero> --name hero-harbour --modules exposure,sigmoid --yes
scouter style   apply-set --name hero-harbour --job-slug harbour-wedding --exclude <hero> --yes
scouter outliers list --reference <hero> --job-slug harbour-wedding --pretty
scouter export  release --job-slug harbour-wedding --preset client-proof --yes
scouter export  verify --manifest <path>
scouter job     run --job-slug harbour-wedding --preset hero-harbour --hero <hero> --yes   # the chain, below
scouter backup  run --yes
scouter backup  verify
scouter wb      calibrate <scratch.orf> --yes   # measures this camera's Kelvin table
scouter wb      calibrate <folder> --camera a7 --dry-run   # folder mode: one body, a scratch copy
scouter wb      kelvin <image>
```

Every one of them is the same function the MCP server exposes, under the same
name and arguments — `tests/test_layer_parity.py` pins that.

### White-balance tables for a new body (Sony A7 and NEX-5N recipe)

Relative white balance ("warmer", "cooler by 300k") and `outliers list`'s
Kelvin test need a measured table per camera body at
`<config_dir>/wb/<body>.json` (`~/.config/scouter/wb/`, the same place
`wb kelvin` reads; `scouter setup` installs the shipped `em10iii` one).
`wb calibrate` takes a **folder of raws, one body at a time**: it reads each
file's EXIF body, picks the first frame of the body you name, copies it to a
scratch dir (`~/.cache/scouter/wb-scratch/<body>/`, or `--scratch DIR`) and
sweeps the Kelvin slider on the **copy** -- the original never gets a
history item. `--dry-run` lists the frames it would use, the chosen one, the
copy and the table path, with no darktable call.

The body slug comes from the config's `cameras` map (`scouter config init`
maps `ILCE-7` to `a7`), else from the EXIF model (`NEX-5N` -> `nex5n`,
unmapped `ILCE-7` -> `ilce7`). Not yet run -- this is the recipe for Pratik's
own session:

```sh
# 1. which frame each body would use (no darktable needed)
scouter --pretty wb calibrate ~/Pictures/EditingTrial/sony --camera a7 --dry-run
scouter --pretty wb calibrate ~/Pictures/EditingTrial/sony --camera nex5n --dry-run

# 2. A7: stage the copy, launch darktable ON it (Scouter cannot move the darkroom
#    onto a frame imported in the same session), then sweep ~21 points (~40 s)
mkdir -p ~/.cache/scouter/wb-scratch/a7
cp ~/Pictures/EditingTrial/sony/DSC01425.ARW ~/.cache/scouter/wb-scratch/a7/
/Applications/darktable.app/Contents/MacOS/darktable ~/.cache/scouter/wb-scratch/a7/DSC01425.ARW &
scouter wb calibrate ~/Pictures/EditingTrial/sony --camera a7 --yes

# 3. quit darktable, then the same for the NEX-5N
mkdir -p ~/.cache/scouter/wb-scratch/nex5n
cp ~/Pictures/EditingTrial/sony/DSC07448.ARW ~/.cache/scouter/wb-scratch/nex5n/
/Applications/darktable.app/Contents/MacOS/darktable ~/.cache/scouter/wb-scratch/nex5n/DSC07448.ARW &
scouter wb calibrate ~/Pictures/EditingTrial/sony --camera nex5n --yes

# 4. check: a Kelvin number instead of "no wb calibration table"
scouter --pretty wb kelvin ~/.cache/scouter/wb-scratch/a7/DSC01425.ARW
```

The frame names above are what `--dry-run` picks (first by name per body);
trust its output over this text. A table that is not strictly monotone in
r/b is refused, never written. To ship a table with Scouter (so `scouter
setup` installs it on a fresh config), copy it into `scouter/data/wb/`.

### The backup gate

`library import` **refuses to run** if no backup target is configured, before
it reads a single file from the card: exit code `11`,
`backup_not_configured`, with the message naming the exact key to fix. It
refuses when the config file is missing, `backup.enabled` is false,
`backup.target` is null or its parent does not exist or is not writable,
`encryption` is `"none"` without `allow_unencrypted`, the `openssl` passfile is
missing/empty/wider than `0600`, or `age` is selected without a recipient or
without `age` on `$PATH`. `scouter config init` writes `backup.enabled: false`
deliberately, so the very first run fails loudly rather than importing
unprotected.

The gate is **not** checked by reads, `cull.*`, `style.*`, `outliers list` or
`export.*`: those work on a library that already passed it at import time, and
blocking a proof export because a backup disk is unplugged would be gate
theatre.

### The nightly backup job (launchd)

```sh
ops/install-backup-job.sh            # render, lint and load com.pratik.scouter-backup
ops/install-backup-job.sh --uninstall
```

It refuses to install while the backup gate is not `ok` — a launchd job firing
against an unconfigured target would just accumulate silent failures in
`~/Library/Logs/scouter-backup.log`.

### Honest limits, phase 2

These are load-bearing. Read them before scripting against phase 2.

- **darktable's film rolls go stale after a move.** darktable's own film rolls
  live in `library.db` with absolute paths, and Scouter may never write
  `library.db`. After `library relocate`, darktable still points at the old
  paths: the result says `darktable_rollpaths_stale: true` and prints the exact
  GUI step (re-import each new `<job>/raw`; darktable reads the sidecars beside
  the raws and the history comes back). No edit is lost, but darktable does
  need re-pointing. Claiming otherwise would be a phantom success.
- **`openssl enc` is unauthenticated, and that hole is closed rather than
  hidden.** `openssl enc -aes-256-cbc` provides confidentiality only: it has no
  MAC, so a tampered ciphertext decrypts to garbage rather than being rejected.
  Scouter therefore writes its own sha256 manifest **inside** the archive and
  `backup verify` re-hashes every sampled member against it after decrypting —
  the integrity check `openssl enc` does not provide. `backup.encryption:
  "age"` needs no such workaround (ChaCha20-Poly1305 is authenticated) and the
  result says which of the two it used, per archive.
- **`ev_proxy` is not EV.** `outliers list`'s `ev_proxy_delta` is
  `log2(frame_luminance / reference_luminance)` on a rendered 512 px preview —
  a rendered-luminance ratio, not a metered exposure difference, and scene
  content moves it. It is named `ev_proxy` everywhere, in the field name and in
  the docs, so no caller mistakes it for an EV reading.
- **Kelvin needs a measured calibration table.** darktable's white-balance UI
  is Kelvin; the stored state is coefficients, and the conversion uses the
  camera's XYZ matrix and an illuminant model that live in darktable's own
  data, not in the XMP. There is no faithful closed-form inversion from the
  sidecar alone, and Scouter does not pretend there is: `wb kelvin` returns
  `kelvin: null` with a reason naming `scouter wb calibrate` when the camera has
  no table, and `null` with the measured range when a value falls outside it —
  never an extrapolated guess. When the image has no committed `temperature`
  module at all (so there are no coefficients to invert in the first place),
  the reason names *that*, and — if the camera also has no calibration table
  — says so in the same reason too, exactly like the no-table case does on
  its own: neither fact is allowed to hide the other (E12 row 407). `outliers
  list` says which test actually ran (`wb_test: "kelvin" | "ratio"`) and
  falls back to the always-available coefficient ratio. A table only ever
  reaches `<config_dir>/wb/<camera>.json` two ways: `scouter wb calibrate`
  writes it there itself, or you copy one there by hand (E12 D9) --
  `config init` does not install any table, including the repo's own
  committed fixtures, so a camera you have already calibrated once still
  needs its table copied into every new config directory.
- **Scouter cannot move darktable's darkroom onto a frame imported in the same
  session.** Measured five times on darktable 5.4.1 (`docs/measurements.md`,
  phase 2 fact 4): `display_image` returns without error and the darkroom
  keeps showing the image it already had; bouncing out to the lighttable with
  the target selected and switching back returns to the same previously-edited
  image. `darkroom.open` raises rather than reporting `opened: true` for an
  image that never loaded. **So the workflow is: pick the frame in
  darktable, and Scouter edits what is loaded.** No set-wide verb is affected
  -- `cull.*`, `style.*`, `outliers list` and `export.*` all work off the XMP
  and `darktable-cli` and never touch the darkroom. This refusal is
  `not_in_darkroom` (exit 6), with a fix naming the real cause -- "open it in
  darktable yourself (double-click it in the lighttable) or relaunch
  darktable on it" -- never `bridge_timeout` (exit 3, "start darktable"):
  darktable IS running, it is just showing a different frame (E12 D4, row
  407). The Lua plugin's own `error()` calls prepend a `source:line:`
  position to the message before Scouter ever sees it; the classifier
  matches the phrase wherever that prefix put it, not just at the start.
- **Hand darktable RESOLVED paths.** `/var` is a symlink to `/private/var` on
  macOS and darktable records the resolved film path, so a `library_root`
  reached through a symlink makes every `image.lookup` miss and every film
  roll register as 0 images (phase 2 fact 1).
- **An unpinned slider path is refused, not guessed.** `dt.gui.action` on a path
  darktable 5.4.1 does not recognise wedges its Lua worker for the rest of the
  session, so `dtbridge.ACTION_PATHS` is a whitelist and an unlisted
  `(module, param)` is a `usage` refusal. Adding one is a host-probe job.
- **`style apply-set --mode overwrite` is still refused.** The Lua API only
  appends. The one place overwrite exists is render-only:
  `export release --overwrite-style`.
- **`style apply-set` returns before darktable has necessarily written every
  sidecar.** The Lua `style.apply` handler applies via `image:apply_style`
  directly against `dt.database` and returns as soon as that call returns --
  darktable then writes each XMP on its own internal schedule, not
  synchronously, and there is no bridge-side "sidecar written" signal to
  wait on (unlike `darkroom.set`'s commit, which has the view-bounce trick
  to confirm against). The editor benchmark measured 0.9-1.35s of lag: an
  immediate `history_read` or `outliers list` right after `style apply-set`
  can still see the PRE-style state (row 408 B7). `apply_set`'s own
  idempotency ledger (`history_end_before`/`history_end_after`) already
  tracks this per image for its OWN re-run safety, but that is a different
  question from "has this specific caller's next read landed after the
  write" -- if you read a frame's history right after applying a style,
  either poll `history read` until it changes or add a short pause first.
- **A job's state lock now fails fast instead of blocking for minutes --
  hardening, not a fix for D3 (row 407, still open).**
  `~/.cache/scouter/state/<job>.json.lock` guards `darkroom.open`'s and
  `history.commit`'s read-modify-write of a job's small state file. A
  caller now waits at most `cache.lock_wait_s` (default 30s) before failing
  with `state_locked` (exit `22`), naming the pid that holds it and since
  when, instead of blocking forever. This is general hardening against
  lock contention, **not** the fix for the E12 live run's own two 15-18
  minute stalls: the evidence shows both happened BEFORE the verb was ever
  reached (`darkroom open`'s stall was inside the bridge call, before this
  lock is even taken; `outliers list`'s verb never takes this lock at
  all), and each lock hold here is microsecond-scale, never
  multi-minute. **D3's root cause is still open.** `metrics.instrumented`
  now records `startup_ms` (time from process start to a verb's own start)
  in every event line, so the next such stall is attributable.
- **`cull propose`'s sharpness gate is relative to the job, not an absolute
  number.** A singleton frame is proposed `pick_rating` when its sharpness is
  in the sharpest `cull.sharpness_keep_top_pct` fraction of THAT job's own
  frames (default keeps the top 60%), never against a fixed number tuned on
  one fixture -- real ORF/ARW renders measured 0.003-0.04 in the E12 live
  run, well under the old fixed `cull.sharpness_floor` of 0.20, so nothing
  was ever picked on its own merits. `sharpness_floor` still exists in the
  config and in every proposal's `tolerances_used`, but only as a diagnostic
  field now; it gates nothing. A frame whose stats render FAILED, or that
  has no capture time, is excluded from the percentile itself (review round
  1 blocker: left in, a job with enough failed renders collapsed the
  threshold toward 0.0, which a failed frame's own 0.0 stand-in then also
  cleared -- proposing a pick on numbers Scouter never measured); a failed
  frame is never proposed `pick_rating` regardless.

  `cull propose` also renders every frame from a sidecar-less scratch copy,
  so it always scores the frame as imported, never its current edited
  history -- editing a hero no longer moves its burst's pick to a different
  frame on the next `cull propose`.

  **The percentile is relative, so it cannot detect a BAD job** (review
  round 1, should-fix): in a uniformly soft or blurry job, the sharpest 60%
  are still proposed a pick -- there is no absolute floor left to warn
  that the whole job is soft. In a uniformly sharp job, the softest 40% of
  singletons still get `keep_rating` (1), never rejected -- a burst's
  runner-up is not a bad frame, just not the pick. The percentile is
  computed over EVERY frame that has a real sharpness number, burst
  members included, not only singletons. When the job's own p10-p90
  sharpness spread is small (either case above), `cull propose` adds a
  warning saying so, so the split is worth a second look by eye rather
  than trusted outright.
- **`library import` labels a mixed-body card from each file's own EXIF, and
  `--camera` filters rather than refuses.** A card holding more than one
  camera body used to be refused outright ("pass --camera to choose one"),
  and once `--camera` was given every file was stamped with that ONE slug --
  including the other body's, with `camera_model` cleared to `null` for all
  of them. Every file is now always named from its own EXIF body; `--camera
  X` imports only that body's files and the result's `skipped_other_camera`
  lists the rest (`{"source_name", "camera"}` per file), never silently
  drops them. `import.json`'s top-level `camera`/`cameras`/`camera_model`/
  `totals` describe the WHOLE job (every import it has ever had), not just
  the run that just finished -- an appending import used to overwrite these
  with only its own numbers even though `files` already held every prior
  import's frames too. A file with NO camera model in its EXIF at all never
  fails the whole card either (review round 1, ruling 2): with an explicit
  `--camera X`, it is imported under X and listed in `assumed_camera`
  (`{"source_name", "camera"}`); without one, it is skipped and listed in
  `skipped_no_body` (`{"source_name"}`), and the rest of the card still
  imports. A card where NOT A SINGLE file has a camera model, imported
  without `--camera`, still refuses outright (fix round 2) -- naming every
  file and pointing at `--camera X` -- rather than silently succeeding
  with an empty job directory.
- **`library import`'s auto-derived date no longer trusts a single
  outlier.** A card's date used to default to the EARLIEST capture time,
  full stop -- so one file with an unset clock (the E12 Sony card's own
  DSC01425, defaulting to 2013-01-01) set the whole job's date. The
  earliest time is still used when it is within 30 days of the card's
  median capture time (a normal midnight-spanning card is unaffected); an
  earlier outlier past that falls back to the median instead, with a
  warning naming the outlier so the camera clock gets checked (D6, row
  407).
- **`cull apply` no longer refuses the whole job over one stale frame.**
  Merely opening a frame in darktable rewrites its XMP, which used to make
  `cull apply` refuse the ENTIRE proposal the moment any one frame's
  sidecar had moved since `cull propose` wrote it. The stale frame(s) are
  now skipped and named in `result.stale`/a warning; every other frame in
  the proposal is still applied as planned (D7, row 407).
- **`darkroom undo-last` never reports a real no-op as `ok`.** A REAL
  (non-dry) call on an empty journal used to report `outcome: dry_run`
  unconditionally -- `ok = outcome in ("ok", "dry_run")` counted it as a
  success indistinguishable from a genuine preview. It now reports
  `outcome: "noop"` (exit `23`) for a real call with nothing to undo; a
  true dry run still reports `dry_run` as before (row 408 B2, editor
  benchmark). The reason names the real cause, not just the symptom: each
  call gets a fresh job unless `$SCOUTER_JOB` (CLI) or `job=` (MCP/Python)
  is pinned across calls, so a fresh call's `undo_last` looks at an empty
  journal by default; the MCP server already pins one for you (phase 3
  task 9's session job), so this now only bites a bare CLI/Python call
  that never pins one. A "noop" also carries a structured
  `error.fix` over every layer (review round 1) -- it is a RESULT, not a
  raised error, so it would otherwise have had nowhere else to put one.
- **`darkroom enable`/`darkroom reset` refuse an unpinned module before any
  bridge call.** They used to send `iop/<module>` for any string; an
  unrecognised path wedges darktable's Lua worker for the rest of the
  session, the same measured risk `ACTION_PATHS` exists for on the SET side.
  `dtbridge.MODULE_WHITELIST` (every `ACTION_PATHS` module, plus `lens`,
  `sharpen` and `denoiseprofile`, which the editor benchmark exercised
  successfully) is checked first, before `darkroom.current`, `journal.begin`
  or any other bridge round trip (row 408 B6/B8).
- **`darkroom set` also refuses an unpinned `(module, param)` before any
  bridge call.** `action_path` used to be evaluated only inside the real
  `darkroom.action` call, after `darkroom.current`, `view.current` and an
  XMP read had already happened -- an agent's guessed pair cost all three
  before it was refused. It is now computed once, up front, before either
  the dry-run or the real path does anything else (row 408 B8).

### Phase 2 exit criteria

E1-E9 each have a named passing test (`tests/test_host_phase2.py::
Phase2EndToEndTest`, which runs on **30 real ORFs** from one shoot, staged as
a card under `~/Pictures/ScouterScratch`); E10 and E11 are covered by
`tests/test_metrics.py` and `tests/test_layer_parity.py` +
`tests/test_scouter_mcp_schemas.py`. **E12 — one real 300-frame job, delivered
end to end — is Pratik's own run**, not something CI can assert.

## Scouter phase 3 (conversation: say it, see it, save it)

Phase 3 adds a plain-English layer over the phase 1/2 core: relative moves
("a touch warmer"), an educative mode that explains what a move does and
why, and named presets that round-trip through darktable's own styles. 44
verbs total (9 new).

### A hero session

```sh
export SCOUTER_JOB=teach-harbour
scouter --pretty darkroom say "a touch warmer"
scouter --pretty darkroom say "less contrast" --educative full
scouter darkroom undo-last
scouter preset save harbour-warm && scouter preset apply-job harbour-warm --job-slug harbour-wedding --yes
```

Setting `SCOUTER_JOB` pins one journal/session for the whole conversation —
without it, every CLI call is its own session, and the MCP server pins its
own session job once, at startup (see `_pin_session_job` below).

### Educative mode

`config.json` gains a top-level `"educative": "off" | "brief" | "full"` key
(default `"brief"`); override per call with `--educative` / `educative=`.
`off` adds no explanation. `brief` adds one sentence per move: what changed,
its before/after values, and what to watch. `full` also explains the
underlying concept (exposure, tone mapping, white balance, …) the first time
that concept comes up in the session, then just the brief sentence after
that — never repeated twice.

### Exit codes, phase 3

Two more exit codes join the phase 1 table: `20` `value_unknown` (a relative
move needs a value phase 3 cannot read yet — no sidecar, an unpinned slider,
or no wb calibration table) and `21` `not_pinned` (the vocabulary word's
action path has not been confirmed by a host probe). `scouter vocab list`
shows which words work today.

`26` `module_unavailable`: the module cannot take this write in the mode
darktable holds it in, refused before anything is sent. Today that means
white balance in darktable 5.4's "camera reference (late)" mode (the
scene-referred default): moving temperature or tint through darktable's
sliders would switch off the late D65 correction and turn the frame cooler
whichever way you asked. In that mode darktable adjusts warmth in color
calibration, which Scouter does not drive yet.

### Honest limits, phase 3

- **Relative moves read values from the XMP, not from sliders.** A slider
  darktable reports only as a position is `absolute_only` until a host probe
  pins its XMP slot. `darkroom adjust` refuses it with `value_unknown` and
  names the absolute `darkroom set` form.
- **A vocabulary word is only as real as its pin.** Rows whose action path no
  probe has confirmed are `candidate`, and every relative or phrase verb
  refuses them with `not_pinned`. `scouter vocab list` shows the current
  status of every word.
- **Relative white balance needs the camera's measured table and assumes
  tint 1.0.** Without a table: `value_unknown`, naming `scouter wb
  calibrate`.
- **`darkroom adjust`/`darkroom say` trust the sidecar by default, behind a
  staleness guard** (product close, row 427 item 4). The pre-read view
  bounce -- which makes darktable write a hand-moved slider to disk -- cost
  ~2.7 s of a 4.1 s one-move `say` on the 2026-09-24 host run (1.37 s
  without it; target 3.5 s). It now runs only when Scouter cannot show the
  sidecar is current: no sidecar yet, the first move on an image in this
  session (or the image changed since the last move), or the sidecar is
  older than the journal's last move (a `--no-commit` move). Each result's
  `plan` says whether it bounced and why. So a conversation pays the bounce
  once per image and then runs at the trust-sidecar speed; the guarded
  default is not yet host-measured (`tests/test_host_phase3.py::
  test_4_conversation_timing` measures it and a forced-bounce loop).
  What the guard cannot see is a slider **you** moved by hand in darktable
  since Scouter's last commit -- darktable writes nothing until the darkroom
  is left. Pass `--bounce` (MCP `bounce: true`) after touching sliders
  yourself; `--trust-sidecar` still skips the bounce unconditionally.
  `darkroom get` never bounces unless `--bounce`. That pre-read commit is
  `history commit --strategy auto`, which tries `no_bounce` (no view switch)
  first, but row 430 found `no_bounce` does not land the edit on darktable
  5.4.1 -- it only rewrites the sidecar's metadata, which `auto` now detects
  and falls back to `view_bounce_action` for. So the real cost today is the
  view bounce, roughly 1-3 s (row 430), not the sub-second `no_bounce` figure
  row 429 reported, and not the older ~2.7 s bounce either.
- **Undoing a preset application is refused.** darktable's Lua API cannot
  remove history items. Use the history panel.
- **Color zones' zone curves are unreachable.** Only its global mix
  (`colorzones strength`) is reachable.
- **White balance tint stays `absolute_only`.** darktable stores it only as
  channel coefficients, so there is no value to move relatively; set it with
  `darkroom set temperature tint <value>`.
- **Presets are darktable styles.** They are not Lightroom presets, and
  applying one always appends.
- **A relative move with no sidecar at all refuses `value_unknown`**, rather
  than assuming the module's neutral value. When a sidecar exists but the
  module has no history entry yet, a move starts from the vocabulary row's
  declared `neutral` (colour balance rgb's sliders: 0), and it stays
  undoable: the journal records that neutral as the entry's `before` value,
  even when darktable does not confirm the commit afterwards.
- **A move whose current value cannot be read, on a row that declares no
  neutral or darktable default, refuses `value_unknown`** before anything is
  written, so Scouter never makes an edit it could not undo. The colour-grade
  hue rows declare darktable's own module default (0°), so "warm the
  highlights", "cool the shadows" and "teal and orange" run on an image with
  no colour-balance history and undo back to 0°.
- **"teal and orange" lands whole or not at all on pinning.** All four of its
  moves are pinned today. If any were unpinned the whole phrase would refuse
  `not_pinned` before any write; a bridge failure after a move has landed is
  reported `partial` (exit 9), and the landed moves still undo as one group.
- **`preset.apply` refuses with `no_sidecar`** when the image's history
  cannot be read before the style is sent. Once darktable confirms the style
  is applied, the result is reported as applied. When the history count on
  disk did not visibly grow, it comes with a warning instead of a failure.
  Either way, Scouter cannot undo a preset application; `undo last` says so
  and points to darktable's history panel.
- **Relative moves and `preset save` first ask darktable to write its
  pending edits.** Only a timeout there becomes a warning ("the value read
  may predate darktable's live state"). A wrong view or a lost bridge still
  stops the call.
- **Applying a preset to the open image reports its timing as `cpu_ms`.**
  That is CPU time, not wall-clock time, and it covers the whole trip out of
  the darkroom and back, not just the moment the style is applied.
- **22 of 24 vocabulary rows are `live`** after the task 10 host run
  (below): tint is `absolute_only` and color zones' curves `unreachable`.
- **The Lua `darkroom.apply_style` bounce ran in darktable 5.4.1 itself** on
  the host run (preset round trip passed); the unit harness is Lua 5.5.1.
- Exit codes: `0` ok · `1` error · `2` usage · `3` bridge_timeout ·
  `4` wrong_view · `5` confirmation required · `6` not_in_darkroom ·
  `7` cli_wrote_nothing · `8` db_locked · `9` style_partial ·
  `10` commit_timeout · `20` value_unknown · `21` not_pinned

## Phase 3 host run (task 10), 2026-09-24

Run on darktable 5.4.1 against a scratch copy of the hero ORF
(`tests/test_host_phase3.py`; results in `docs/measurements.md`, "Phase 3
host run").

- **Action paths.** All 15 candidate rows answered non-nan, one darktable
  launch per candidate, and were pinned in `dtbridge.ACTION_PATHS`. The 4-ways
  shadows and highlights rows use darktable's short section words
  (`lift/…`, `gain/…`); the midtones rows use `power/…`.
- **XMP slots.** The sentinel method pinned 17 slots across colour balance
  rgb v5, sigmoid v3 (skew), filmic rgb v6 and color zones v5
  (`xmp.LAYOUTS`, fixture `tests/fixtures/phase3-host.xmp`).
- **Signs.** All three matched the spec, and no phrase was flipped. Black
  level +0.01 darkens, tint 0.9 lowers r/b, and "warm the highlights" raises
  r/b.
- **Preset round trip** (save, list, apply to the open image, delete)
  passed.
- **Timing.** A one-move `darkroom say` had a median of 4.1 s with the
  pre-commit bounce, which missed the 3.5 s target; with `--trust-sidecar`
  the median was 1.4 s. The default is now the staleness guard (see
  "Honest limits, phase 3"), pending a host re-measure.

To re-run it, close darktable first and have the hero ORF present:

```sh
SCOUTER_HOST_TESTS=1 uv run --extra dev python -m pytest tests/test_host_phase3.py -q -s
```

## Honest limits, product 1.0rc (2026-09-26)

Read these with the phase 1-3 limits above; they are what the product close
did not, or could not, settle.

- **Global edits only.** No masks, no local or spot edits, no retouch:
  darktable's Lua API exposes none of them. White-balance tint is set, never
  nudged (`absolute_only`); color zones' zone curves are unreachable. The
  user guide's "What refuses, and why" lists every refusal.
- **The staleness guard cannot see your hands.** It trusts the sidecar when
  the sidecar is newer than Scouter's last move on the same image. A slider
  you moved yourself in darktable since then is invisible until darktable
  writes the sidecar; pass `--bounce` (MCP `bounce: true`) after hand edits.
  The guarded default's timing is not host-measured yet (row 415 measured
  4.12 s with the old bounce every time, 1.37 s without). Row 429 expected a
  commit at 0.2-1.1 s via `no_bounce`, but the row 429 acceptance run found
  `no_bounce` never actually commits on this host (row 430): the honest
  figure is a commit via `view_bounce_action`, roughly 1-3 s, with one move
  end to end (row 430's live check) at 4.6 s -- still well under the 5 s poll
  ceiling.
- **`scouter setup` does not register the MCP server.** It prints the `claude
  mcp add` command and never reads or writes Claude Code's config. Right
  after installing the plugin, `doctor` is not ok until darktable restarts.
  Not yet run against the real `~/.config/darktable`.
- **`wb calibrate` folder mode still needs darktable to show the copy.** The
  sweep runs on a scratch copy, and darktable cannot be moved onto a frame
  imported in the same session, so the recipe launches darktable on the copy
  first. The A7 and NEX-5N tables are **not measured yet**; until they are,
  warmer/cooler on Sony frames refuses `value_unknown` and `outliers list`
  falls back to the r/b ratio.
- **`job run` stops; it does not roll back.** Ratings applied by the cull and
  styles appended by the preset step stay when a later step refuses. Styles
  cannot be removed through the Lua API at all. The 2 s settle before
  `outliers` is a measured-lag allowance (row 408 B7), not a sidecar-written
  signal. Not yet run on a real job.
- **Earlier rounds.** The first `history commit` after a fresh darktable
  launch intermittently wedging the bridge (row 408 B1) was the row 418
  crash, closed in row 429 (see "The commit trigger" and "Row 418 crash" in
  the honest limits above). The E12 15-18 minute pre-verb stalls (D3) share
  that crash signature but were not re-run; treat them as open until the
  acceptance run shows none.

## Editing with Scouter from Claude Code

Scouter edits the photo open in **your running darktable**, from a Claude
Code conversation. You talk; Claude calls Scouter's MCP tools; each change is
a normal darktable history item you can see, keep, or undo yourself. It
works on the whole image (global edits). It does not paint masks.

### Register the server (once)

1. `scouter setup` (see "Setup" above): plugin installed, `luarc` line, the
   measured WB tables, then restart darktable.
2. Register the server. `scouter setup` prints this line with your own
   paths; from this checkout it is:

```
claude mcp add --transport stdio --scope user --env PYTHONPATH=/Users/pratik/CodeBase/scouter scouter -- /Users/pratik/CodeBase/scouter/.venv/bin/python -m scouter.mcp.server
```

A stdio server, user scope (every project sees it), the repo's own venv
python, absolute paths; `PYTHONPATH` pins this checkout even if the venv's
editable install points at a worktree. The same server as JSON, for a
project `.mcp.json` or Claude Desktop:

```json
{
  "mcpServers": {
    "scouter": {
      "type": "stdio",
      "command": "/Users/pratik/CodeBase/scouter/.venv/bin/python",
      "args": ["-m", "scouter.mcp.server"],
      "env": {"PYTHONPATH": "/Users/pratik/CodeBase/scouter"}
    }
  }
}
```

3. `claude mcp list` should show `scouter: ... - Connected`. Each server
   process pins one Scouter session job, so undo works across the whole
   conversation. Start a new Claude Code session after registering.

### Single frame by phrases

Open the photo in darktable's **darkroom** yourself (double-click it in the
lighttable). Scouter edits whatever the darkroom shows; it cannot switch
darktable to a frame imported in the same darktable session. Then just say
what you want. Claude sends it to `darkroom_say`, and you watch it land in
darktable. These six sentences map to live vocabulary rows today
(`tests/test_user_guide.py` checks each one):

| You say | Scouter moves | Row(s) |
|---|---|---|
| "slightly brighter" | exposure up half a step (+0.15 EV; one step is 0.3 EV) | `exposure.exposure` |
| "a touch warmer" | white balance +150 K, read through the camera's measured Kelvin table | `temperature.temperature` |
| "less contrast" | tone-mapper contrast down (sigmoid, or filmic rgb when filmic is the active tone mapper) | `sigmoid.contrast` |
| "lift the shadows" | colour balance rgb shadows luminance up | `colorbalancergb.shadows_luminance` |
| "a bit more vibrance" | colour balance rgb global vibrance up | `colorbalancergb.global_vibrance` |
| "warm the highlights" | highlights hue set to 60° (orange), highlights chroma up one step | `colorbalancergb.highlights_hue`, `highlights_chroma` |

Amounts: "slightly" / "a touch" / "a bit" / "much" scale one step, or ask for
a number ("cooler by 300k"). `vocab_list` (CLI `scouter vocab list`) shows every
word and its status. A phrase with several moves ("teal and orange") lands
as one group. For an exact value, Claude uses `darkroom_set`
(`scouter darkroom set exposure exposure 1.2`); for an exact relative change,
`darkroom_adjust`. `darkroom_get` reads a value back.

**Speed.** One phrase measured ~1.4 s on the host with the pre-read bounce
skipped (2026-09-24; the guarded default below is not host-measured yet). A
commit only counts once the sidecar's history actually changed (row 430:
`no_bounce`'s old "sidecar file changed" check was fooled by a metadata-only
rewrite, so it never landed the edit on darktable 5.4.1); the commit that
actually lands an edit today is the view bounce, roughly 1-3 s, not the
sub-second `no_bounce` figure row 429 reported. A live check (row 430) had
one `darkroom_say` move take 4.6 s wall end to end, still well under the
5 s poll ceiling. If you moved sliders **by hand** in darktable
since Scouter's last move, say so ("I changed the exposure myself"): Claude
passes `bounce: true` so Scouter reads your values, not the stale sidecar.

### Undo

"Undo that" -> `darkroom_undo_last`: the whole last phrase is reverted as one
group ("teal and orange" undoes all four moves). `single: true` undoes only
the last single move. Undo writes a new history item setting the old value
back; darktable's own history panel and Ctrl-Z still work alongside it.
Undo cannot remove a **preset application** (darktable's Lua API cannot
delete history items); use darktable's history panel for that.

### Save as a preset

"Save this look as harbour-warm" -> `preset_save`: the modules changed in
this session on the open photo become a darktable style named `harbour-warm`,
with a copy under `<library_root>/scouter/memory/presets/` when a library
is configured. Replacing an existing preset needs confirmation.
`preset_list` lists them; `preset_apply` puts one on the open photo (live) or
on another image.

### Apply to a job

A job is a folder `scouter library import` made (see phase 2 above). "Apply
harbour-warm to the whole harbour-wedding job, except the hero" ->
`preset_apply_job` with `exclude`. It is gated: Claude shows the dry run
(how many frames) first and asks before `confirm: true`. Applying always
**appends** to each frame's history.

### Batch: `job run`

One call for the whole delivery chain -> `job_run` (CLI `scouter job run`):

```sh
scouter --pretty job run --job-slug harbour-wedding --preset harbour-warm --hero <hero> --dry-run
scouter --pretty job run --job-slug harbour-wedding --preset harbour-warm --hero <hero> --yes
```

In order: **cull** (propose ratings with the job-relative sharpness gate,
then apply them), **preset** (apply-set to every frame except `--exclude`
and the hero), **outliers** (frames outside tolerance against the hero,
after a 2 s pause for darktable's sidecar writes), **export** (release
through `--export-preset`, default `client-proof`, with a checksum
manifest). Every step reports counts. The chain **stops at the first step
that refuses or comes back partial**, says which (`stopped_at`) and why;
earlier steps are not rolled back. `--steps cull,export` runs a subset.
Without `--yes` nothing is written.

### Export

"Export proofs for harbour-wedding" -> `export_release` with
`preset: client-proof` (2048 px sRGB JPEG, frames rated at or above the
proof floor). `client-final` / `print-tiff` export the frames you gave
darktable's **green** colour label. Files land in the job's `exports/<preset>/`
with a `manifest-*.json` you can check with `export_verify`. Export renders
through `darktable-cli` from the XMP sidecars, so darktable's "write sidecar"
preference must be on.

### Educative mode

Scouter can explain what each move did, for learning while you edit. Set it
per call ("explain that fully") or in `config.json` (`"educative"`):

| Level | What you get |
|---|---|
| `off` | only the summary line (`exposure › exposure: +0.15 EV (0.7 EV → 0.85 EV)`) |
| `brief` (default) | one sentence per move: what changed, before/after, what to watch |
| `full` | the brief sentence plus the underlying concept (exposure, tone mapping, white balance, ...) the first time it comes up in the session, never repeated |

### What refuses, and why

Scouter refuses rather than guess. Each refusal names the fix.

- **Masks and local edits: not available.** No drawn or parametric masks, no
  retouch, no spot or graduated edits: darktable's Lua API exposes no masks.
  Every Scouter edit is global. Do local work in darktable by hand.
- **White-balance tint: set it, don't nudge it.** "more magenta" refuses
  `value_unknown`: darktable stores tint only inside the channel
  coefficients, so there is no value to move relatively. Set it absolutely:
  `darkroom set temperature tint 1.05`.
- **Color zones' zone curves: unreachable.** Zones are curve nodes, not
  sliders; only color zones' overall `strength` mix is reachable. Use colour
  balance rgb phrases ("warm the highlights", "teal and orange") instead.
- **Warmer/cooler on a body with no WB table** refuses `value_unknown`,
  naming `wb calibrate` (Sony A7 / NEX-5N: see the recipe above).
- **A frame darktable is not showing** refuses `not_in_darkroom`: open it in
  darktable yourself.
- **A word whose darktable path is not probed** refuses `not_pinned`
  (`scouter vocab list` shows none today: 22 live, tint absolute-only,
  zone curves unreachable).
- **Overwriting history** is refused: darktable's Lua API only appends.


## Contributing

Contributions welcome. Any change that reads or writes `library.db` directly will be rejected.

## License

MIT — see `LICENSE`.
