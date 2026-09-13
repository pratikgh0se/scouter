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
pip install -e '.[dev]'
darktable-mcp install-plugin        # copies the Lua plugin, then RESTART darktable
scouter doctor                      # darktable, plugin, current view, cache paths
```

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
same as the original `darktable-mcp` tools — `darktable-mcp install-plugin`
writes both the plugin file and that `luarc` line for you.

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
`--px`, `--dry-run` and `--yes` go after it.

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

- **The commit trigger is a view bounce.** darktable only writes the XMP
  sidecar when the darkroom is left, not per slider move (phase 0 finding 3),
  so every write's commit briefly bounces the view out to the lighttable and
  back (design 8.1's `view_bounce`, the only strategy phase 0/1 proved
  actually commits). Measured at ~500ms wall clock on this Mac against a
  small library (`docs/measurements.md`), ~0.8s end to end through the bridge
  under a live GUI. The view switch is asynchronous, so the plugin yield-polls
  until the view really changed and confirms the commit by watching the
  sidecar itself — never by assuming the bounce landed. darktable writes a
  sidecar only when the history actually changed, so a write asking for the
  value the image already has produces no write; Scouter resolves that by
  reading the value back and says so in a warning, and still reports
  `commit_timeout` when the value genuinely is not there.
- **`darkroom status` reports slider *positions*, not values.** `dt.gui.action`'s
  read-back is a normalised 0..1 position in the slider's visible range, and
  phase 1 has no verified position→value table for every slider — use
  `history read` (the XMP) for actual values; `status` is a liveness/reachability
  check and the `wrong_view` signal, nothing more.
- **`style apply --modules` can honestly fail to filter.** When darktable's
  build doesn't expose per-module style filtering, applying a style with a
  `modules` list still applies the *whole* style and reports
  `modules_honoured: false` — never a silent partial application dressed up
  as a filtered one.
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
`7` cli_wrote_nothing · `8` db_locked · `9` style_partial · `10` commit_timeout

### Tests

```sh
python3 -m pytest tests -q          # no darktable needed; host-only tests skip
SCOUTER_HOST_TESTS=1 python3 -m pytest tests/test_host_integration.py tests/test_commit_trigger_host.py -q -s   # needs darktable + the ORF, and this env var
```

The host-only suite (`tests/test_host_integration.py`,
`tests/test_commit_trigger_host.py`) launches the **real** darktable against
`/Users/pratik/Pictures/EditingTrial/P6281348.ORF` and always quits it when
done — it is gated on `SCOUTER_HOST_TESTS=1` so it never runs by accident in
CI or a plain `pytest tests`.

Design: `docs/superpowers/specs/2026-09-13-scouter-phase1-design.md`.
Measurements: `docs/measurements.md`.

## Contributing

Contributions welcome. Any change that reads or writes `library.db` directly will be rejected.

## License

MIT — see `LICENSE`.
