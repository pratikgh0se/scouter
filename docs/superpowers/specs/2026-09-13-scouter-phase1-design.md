# Scouter Phase 1: Live Core (bridge darkroom verbs + Scouter loop + audit + confirm gates)

**Date:** 2026-09-13
**Phase:** 1 of the system spec's delivery table ("Live core").
**System spec:** `~/second-brain/Specs/darkroom-agent.md` (approved, row 275) — sections 3.1, 4.1, 4.2, 4.3, 6, 6.1, 6.2, 7, 8, 10, 11.
**Phase 0 findings:** `~/second-brain/Knowledge/Scouter - Phase 0 spike findings 2026-09-13.md` (verdict PASS).
**Repo:** `scouter` (fork of `w1ne/darktable-mcp`, MIT).
**Plan:** `docs/superpowers/plans/2026-09-13-scouter-phase1.md`

---

## 1. Goal and exit criteria

Phase 1 ships the live half of Scouter: an agent (or a shell script, or a Python import) can open one raw in the running darktable, change its parameters, see the numbers change, and undo the last change — with every step a real darktable history item.

**Exit criteria** (all must hold, each with a named test in section 11):

| # | Criterion | How it is proven |
|---|---|---|
| E1 | One hero ORF edited by scripted conversation: exposure, contrast, white balance | `tests/test_host_integration.py::test_edits_one_orf_end_to_end` (host-only) |
| E2 | Every step visible in darktable | the same test reads the XMP after a commit and asserts one `rdf:li` history entry per step, with the decoded value |
| E3 | Undoable | the same test calls `darkroom.undo_last` and asserts the XMP's newest entry restores the previous value |
| E4 | Scouter loop under 3 s for a 20 MP ORF at 1024 px | `docs/measurements.md`, written by task 1's host-only measurement test, asserting `< 3.0 s` |
| E5 | Every registered verb emits exactly one metrics event | `tests/test_metrics.py::test_every_registered_verb_emits_one_event` (runs without darktable) |
| E6 | Three layers, one vocabulary | `tests/test_layer_parity.py` asserts the verb registry, the CLI subcommand table and the MCP tool table cover the same verb ids with the same argument names |
| E7 | Confirm gates hold | `tests/test_confirm_gates.py` asserts every gated verb returns `outcome="dry_run"` and performs no bridge call without `confirm=True` |

---

## 2. Non-goals (phase 2 and later)

Explicitly **not** in phase 1, so no task in the plan may grow to include them:

- Library verbs: import from card, checksum, rename, film-roll registration, job folders, SSD layout, backup (phase 2, spec 4.6).
- Cull proposal / `Ki Sense` (phase 2, spec 4.8).
- `style.apply` to a **set** of images, outlier detection, tolerances (phase 2, spec 4.4). Phase 1 applies a style to **one** image only.
- Export presets beyond `client-final` and `preview`; manifests, checksums, share links (phase 2, spec 4.7).
- Style memory `looks.json`, vault look notes, acceptance signal, `Dragon Radar` (phase 3, spec 4.5).
- Retouch/spot detection and guidance (phase 3, spec 8.3).
- Gallery delivery (phase 4, parked).
- Grafana series and Telegram alerting off the metrics DB (spec 6.1). Phase 1 writes the events and ships `scouter metrics summary`; wiring the series into the existing Grafana is a Capsule-side task after phase 1.
- Inverting a slider's normalised status position into a real value. Phase 1 reads values from the XMP only (see 7.3); a `SLIDER_RANGES` table is phase 2.
- Multi-instance module addressing beyond passing `instance` through to `dt.gui.action` (no instance creation/deletion).

---

## 3. Package layout decision (three layers, least churn)

**Decision: keep `darktable_mcp/` exactly where it is and add `scouter/` on top.**

`darktable_mcp/` stays the *plumbing* layer — the Lua plugin, the file-spool `Bridge` client, the `CLIWrapper` around `darktable-cli`, the install-plugin command, w1ne's library/camera/preview tools and their ~10,000 lines of passing tests including the golden tool-schema snapshot. `scouter/` is the new *product* — the three layers of system spec 3.1 — and imports the plumbing.

Why not rename:

- A rename touches ~10,500 lines of tests and the golden snapshot for zero functional gain, and it would discard the upstream diff base (`origin` still points at `w1ne/darktable-mcp`; merging upstream fixes stays possible while the paths match).
- The existing `darktable-mcp` MCP server and its tools keep working unchanged. Scouter's server is a *second*, separate entry point, so a bad phase-1 day cannot break the tool that already works.
- The Lua plugin is extended in place (section 5), so there is exactly one worker loop and one spool directory — a second plugin would double the polling cost inside darktable for no reason.

Consequence to accept: two package names in one repo, and two cache namespaces (`~/.cache/darktable-mcp/` for the RPC spool, `~/.cache/scouter/` for Scouter's own state). The spool is a protocol detail shared with the Lua plugin; moving it would force a plugin reinstall and a darktable restart for every user of the fork. Documented in the README.

```
scouter/
  __init__.py            __version__, main() -> CLI
  errors.py              typed error classes + EXIT_CODES (section 9)
  types.py               result dataclasses + Envelope + to_json
  paths.py               ~/.cache/scouter/{events.jsonl,metrics.db,audit/,previews/,journal/}
  metrics.py             emit(), @instrumented, sqlite schema, summary query
  registry.py            VERB_REGISTRY: dict[str, Verb] — the single source of truth
  core/
    __init__.py
    darkroom.py          open, set_param, nudge, enable, reset, undo_last, status, describe_history
    history.py           read, commit
    scout.py             stats, preview, compare
    style.py             extract, apply
    export.py            run
    journal.py           per-job undo journal (section 8)
    xmp.py               XMP sidecar reader + module param decoder (section 7)
    ppm.py               P6/PGM parser, downscale, histogram, stats (section 6)
    dtbridge.py          thin typed wrapper over darktable_mcp.bridge.client.Bridge
  cli/
    __init__.py
    main.py              argparse tree, JSON on stdout, exit codes, --dry-run/--yes
  mcp/
    __init__.py
    server.py            ScouterServer: mcp 2.x low-level Server, handwritten schemas
    schemas.py           the tool table (golden-snapshot tested)
  lua/                   (nothing new — the plugin is extended in darktable_mcp/lua/)
```

New/changed outside `scouter/`:

- `darktable_mcp/lua/darktable_mcp.lua` — new `methods` entries (section 5). Same dispatcher, same spool, same tests.
- `darktable_mcp/bridge/client.py` — `DEFAULT_TIMEOUTS` entries for the new methods only.
- `tests/lua/test_dispatcher.lua`, `tests/test_lua_dispatcher.py` — extended per verb.
- `pyproject.toml` — `scouter` package + `[project.scripts] scouter = "scouter:main"` and `scouter-mcp = "scouter.mcp.server:main"`.

Dependencies stay **stdlib + `mcp>=2,<3`**. No Pillow, no rawpy, no numpy, no pytest-only constructs in new tests (they are `unittest.TestCase` classes, collected by `python3 -m pytest tests -q`).

---

## 4. Architecture

```
Pratik ──chat──▶ Claude ──MCP──▶ scouter.mcp.server ─┐
shell/launchd ──────────────────▶ scouter.cli.main ──┼──▶ scouter.registry.call(verb, **args)
python import ──────────────────────────────────────▶┘            │
                                                                  ├─▶ scouter.core.* (plain typed functions, no model calls)
                                                                  │      │
                                                                  │      ├─▶ core.dtbridge ─▶ darktable_mcp.bridge.Bridge
                                                                  │      │        file spool ~/.cache/darktable-mcp/
                                                                  │      │        ──▶ Lua plugin in the RUNNING darktable ──▶ dt.gui.action
                                                                  │      ├─▶ core.xmp  ──▶ <raw>.xmp   (the read path for history/params)
                                                                  │      └─▶ core.ppm  ──▶ darktable_mcp.CLIWrapper ──▶ darktable-cli (isolated configdir)
                                                                  └─▶ scouter.metrics.emit  ──▶ events.jsonl + metrics.db + audit/<job>.jsonl
```

Invariants, restated from spec 6 because every task must hold them:

1. The agent never renders its own pixels. Pixels come from `darktable-cli`; edits come from `dt.gui.action`.
2. `library.db` is never read or written by Scouter. The GUI owns it.
3. History and parameter values are read from the **XMP sidecar**, never from a Lua history API (there is none in Lua API 9.6.0).
4. Every core call emits exactly one metrics event and one audit line, including failures and dry runs.

---

## 5. Bridge protocol additions

Transport is unchanged (spec `docs/superpowers/specs/2026-04-27-ipc-bridge-mvp-design.md`): one `request-<uuid>.json` in `~/.cache/darktable-mcp/`, one `response-<uuid>.json` back, `{id, method, params}` → `{id, result}` or `{id, error}`.

Two protocol rules the new verbs add:

- **`nan` is never encoded as a number.** The Lua JSON encoder already maps `nan`/`inf` to `null`, so every result that carries a `dt.gui.action` return value also carries an explicit `"nan": true|false`. A `null` status with `nan: false` means "darktable returned no value at all"; `nan: true` is the wrong-view/not-in-darkroom signal (phase 0 finding 2).
- **Every new verb pre-flights the view.** `darkroom.*`, `module.*` and `snapshot.take` return `"view"` (`"darkroom" | "lighttable" | "map" | ...`) so the Python layer can raise `wrong_view` with the actionable message instead of interpreting a `nan`.

### 5.1 New methods

| Method | Params | Result |
|---|---|---|
| `view.current` | `{}` | `{"view": "darkroom"}` |
| `image.lookup` | `{"path": "/abs/file.ORF"}` | `{"image_id": 12, "path": "/abs/file.ORF", "filename": "file.ORF", "rating": 0, "found": true}` |
| `darkroom.open` | `{"image_id": 12}` **or** `{"path": "/abs/file.ORF"}` | `{"image_id": 12, "path": "/abs/file.ORF", "view": "darkroom", "opened": true, "already_open": false}` |
| `darkroom.action` | `{"path": "iop/exposure/exposure", "instance": 0, "element": "value", "effect": "set", "value": 1.5}` | `{"path": "...", "instance": 0, "element": "value", "effect": "set", "value": 1.5, "status": 1.5, "nan": false, "view": "darkroom"}` |
| `darkroom.status` | `{"path": "iop/exposure/exposure", "instance": 0, "element": "value"}` | `{"path": "...", "position": 0.6429, "nan": false, "view": "darkroom"}` |
| `module.enable` | `{"module": "exposure", "instance": 0}` | `{"module": "exposure", "action": "enable", "status": 1.0, "nan": false, "view": "darkroom"}` |
| `module.disable` | `{"module": "exposure", "instance": 0}` | as above with `"action": "disable"` |
| `module.reset` | `{"module": "exposure", "instance": 0}` | as above with `"action": "reset"` |
| `history.commit` | `{"strategy": "sidecar_action" \| "view_bounce" \| "auto"}` | `{"strategy_used": "sidecar_action", "ok": true, "view_after": "darkroom", "ms": 84, "detail": null}` |
| `snapshot.take` | `{}` | `{"taken": true, "view": "darkroom"}` |
| `style.create_from_current` | `{"name": "hero-P6281348", "modules": ["exposure","sigmoid","temperature"], "description": "…"}` | `{"name": "...", "created": true, "modules_requested": 3, "existing_replaced": false}` |
| `style.apply` | `{"image_ids": [12], "name": "hero-P6281348", "mode": "append"}` | `{"name": "...", "mode": "append", "applied": 1, "missed": [], "errors": []}` |

`darkroom.action` maps 1:1 onto `dt.gui.action(path, instance, element, effect, value)`; `value` is the **speed** argument, which for `effect="set"` is the absolute value in the slider's own unit (phase 0 finding 1). `effect` accepts `"set" | "up" | "down" | "on" | "off" | "toggle" | ""` (the empty string plus `value: 0` is the status query).

Error shape is w1ne's: the Lua handler raises, the dispatcher answers `{"id": ..., "error": "handler raised: ..."}`, and `Bridge.call` turns that into `BridgeError`, which `core.dtbridge` classifies (section 9).

### 5.2 Client timeouts

Added to `DEFAULT_TIMEOUTS` in `darktable_mcp/bridge/client.py`:

```python
"view.current": 5.0,
"image.lookup": 30.0,            # linear scan of dt.database, like view_photos
"darkroom.open": 30.0,           # pipeline load for a 20 MP raw
"darkroom.action": 10.0,
"darkroom.status": 5.0,
"module.enable": 10.0,
"module.disable": 10.0,
"module.reset": 10.0,
"history.commit": 60.0,          # view_bounce reloads the whole pipe
"snapshot.take": 15.0,
"style.create_from_current": 30.0,
"style.apply": 60.0,
```

The existing cross-language invariant holds: `STALE_AGE_SECONDS` (300) in the Lua plugin stays above the largest budget; `tests/test_lua_dispatcher.py` already asserts the relation and will pick these up.

---

## 6. Scouter loop (preview, histogram, stats)

### 6.1 Render

`darktable-cli <raw> <out.ppm> --width <px> --height <px> --out-ext ppm --core --configdir <isolated> --library :memory:`

Measured on this Mac while authoring this spec (2026-09-13, darktable 5.4.1, Apple M4, `P6281348.ORF`, 20 MP Olympus ORF, 1024 px):

| Step | Cold configdir | Warm configdir |
|---|---|---|
| `darktable-cli` PPM export | 2.21 s | 1.94 s |
| PPM parse + 3-channel histogram + stats (pure Python) | — | 0.04 s |
| **Loop total** | **2.25 s** | **1.98 s** |

`--out-ext ppm` **is supported** (confirmed: the file written is `P6 1024 766 65535`, i.e. binary PPM, 16 bits per sample, big-endian). The PNG-via-`zlib` fallback in spec 4.3 is therefore not needed for stats and is kept only for the `preview` verb's human-viewable output. Task 1 re-runs this measurement inside the repo and records the numbers in `docs/measurements.md`; the budget assertion is `< 3.0 s`.

Over-budget behaviour (spec 7): `scout.stats` retries once at 512 px and returns `warnings: ["loop over 3.0s at 1024px, fell back to 512px"]` with `px_used: 512`.

### 6.2 Parsing and statistics (pure Python, stdlib only)

`scouter/core/ppm.py`:

- `parse_p6(data: bytes) -> Raster` — header tokeniser that honours `#` comments and any whitespace, then a `memoryview` over the payload. Supports `maxval` 255 (1 byte/sample) and 65535 (2 bytes/sample, big-endian). Anything else raises `ValueError`.
- **8-bit reduction.** For a 16-bit raster the high byte of each sample is taken (`payload[0::2]`), giving 256-bin statistics from a 16-bit render. This is a documented approximation: it is the same precision as darktable's own on-screen histogram and costs one slice instead of a per-sample loop.
- `histogram(raster) -> dict[str, list[int]]` — per channel, 256 bins, via `collections.Counter` over the channel's byte slice (measured 13 ms/channel at 1024 px; a `bytes.count` loop was 143 ms and a per-pixel Python loop 78 ms, so `Counter` is the implementation).
- `stats(raster) -> ImageStats`:
  - `mean`, `median` per channel and for luminance (`0.2126R + 0.7152G + 0.0722B`, computed from the three histograms, not per pixel);
  - `clipped_high_pct` = share of samples with high byte ≥ 253 (≈ 99.2 % of full scale), `clipped_low_pct` = share ≤ 2, per channel and any-channel;
  - `p01`, `p50`, `p99` luminance percentiles from the cumulative luminance histogram;
  - `wb_ratio_rg = mean_r / mean_g`, `wb_ratio_bg = mean_b / mean_g` (the phase-1 white-balance estimate; a temperature estimate is phase 2);
  - `sharpness` = mean absolute horizontal luminance difference over a stride-4 sample of rows, normalised to 0..1 (cheap, monotone, enough for "did the crop get softer"; not comparable across scenes and labelled as such in the result).
- `downscale(raster, px) -> Raster` — nearest-neighbour stride decimation, used only when a caller asks for stats at a smaller size than the render it already has (`scout.compare` reuses one render for both sizes).

### 6.3 Preview

`scout.preview` renders **PNG** (`--out-ext png`) to `~/.cache/scouter/previews/<sha1(path+xmp_mtime+px)>.png` and returns the **path**, dimensions and byte size — never base64 in the result. Rationale: an inline 1024 px PNG is ~1 MB ≈ 250 k estimated tokens, which would dominate every observability number in spec 6.1. The MCP client reads the file itself. An `inline` flag is reserved for phase 2 and is not in the phase-1 schema.

---

## 7. XMP sidecar: the read path

### 7.1 Parsing

`scouter/core/xmp.py` parses with `xml.etree.ElementTree` and falls back to a regex scan of `<rdf:li ... />` attributes when the document is not well formed (darktable writes it, but a half-written file caught mid-write must not crash the loop; the caller then retries after the mtime settles).

Read from the `rdf:Description` element: `darktable:history_end`, `darktable:xmp_version`, `darktable:iop_order_version`, `darktable:history_current_hash`.
Read per `darktable:history/rdf:Seq/rdf:li`: `num`, `operation`, `enabled`, `modversion`, `params`, `multi_name`, `multi_priority`, `blendop_version`, `blendop_params`.

`history_end` is the cut: entries with `num < history_end` are active, the rest are redo-able tail. `describe_history` returns both, flagged.

### 7.2 Param decoding

`params` is either plain lowercase hex, or `gz<level><base64>` where the base64 payload is zlib-compressed (observed on `blendop_params` in the real sidecar). `decode_params(raw) -> bytes` handles both.

Decoded with `struct` for the phase-1 modules. Layouts **verified against the real sidecar** written by the phase 0 spike (`~/Pictures/EditingTrial/P6281348.ORF.xmp`, `history_end=14`):

| module | modversion | bytes | layout | verified values |
|---|---|---|---|---|
| `exposure` | 7 | 28 | `<i f f f f i i` → `mode, black, exposure, deflicker_percentile, deflicker_target_level, compensate_exposure_bias, _tail` | num 11: `mode=0, black=-0.000244, exposure=1.5500002` (the spike set 1.5 then nudged +0.05) |
| `sigmoid` | 3 | 56 | `<14f` with slot 0 = `contrast` | num 12: `contrast=1.3999999` (the spike set 1.4) |
| `temperature` | 4 | 20 | `<4f i` → `coeff_r, coeff_g, coeff_b, coeff_g2, preset` | num 13: `2.0125747, 1.0, 1.7052786, inf, 2` |
| `colorbalancergb` | 5 | float vector | `<Nf` with a named-slot map | **not present in the reference sidecar**; the map is pinned by task 5's host probe (set one slider by action, commit, diff the float vector, assert the expected slot moved). The probe's output is committed as `tests/fixtures/colorbalancergb.xmp` and the slot map is asserted against it, so it is a pinned fact, not a guess. |

Unknown module, unknown modversion, or a length that does not match the layout ⇒ the entry is returned as `{"operation": ..., "modversion": n, "params_hex": "...", "decoded": null, "reason": "no layout for sigmoid v4"}`. **Never a guessed value** — this is the honesty rule from w1ne's pass, applied to parameters.

`inf`/`nan` floats (real: `temperature.coeff_g2` is `inf`) are serialised as `null` with the raw hex kept in `params_hex`, because JSON has no `inf`.

### 7.3 Values, not positions

`dt.gui.action` status returns a **normalised position** in the slider's visible range (phase 0 finding 1), and phase 1 has no verified range table. So:

- Real parameter values in every Scouter result come from the XMP.
- `darkroom.status` positions are used for two things only: a liveness/reachability check, and the `nan` wrong-view signal. They are reported as `position` (0..1) and never relabelled as a value.

---

## 8. Commit trigger and undo

### 8.1 The commit-trigger experiment (task 1)

Phase 0 finding 3: history and the XMP commit on **leaving** the darkroom, not per slider move. So a `history.commit` verb is required before any read. Task 1 tries, in order, on the real darktable with the real ORF, and records latency and correctness for each in `docs/measurements.md`:

| # | Candidate | Lua | Accept if |
|---|---|---|---|
| C1 | Sidecar-write action | `dt.gui.action("lib/selected image[s]/write sidecar files", 0, "button", "activate", 1)` (with the darkroom image selected) | XMP mtime advances and the newest `rdf:li` carries the value just set, **and** the darkroom view is unchanged afterwards |
| C2 | Metadata-panel variant | same as C1 against `"lib/metadata/write sidecar files"` and `"lib/image/write sidecar files"` — the panel path differs between darktable builds, so all three spellings are probed and the one that returns non-`nan` wins | as C1 |
| C3 | View bounce (proven) | `dt.gui.current_view(dt.gui.views.lighttable)` then `dt.gui.current_view(dt.gui.views.darkroom)` | as C1; already proven to commit in phase 0, so this is the floor, not a risk |

**Task 1 result (`docs/measurements.md`, this Mac, darktable 5.4.1):** C1 and C2 (all three action-path spellings) were rejected — every `dt.gui.action` call against `lib/selected image[s]/write sidecar files`, `lib/metadata/write sidecar files`, and `lib/image/write sidecar files` returned `nan`, the same "not a real widget path in this build" signal phase 0 saw for absolute sets outside the darkroom. C3 (view bounce) was accepted, measured at 507 ms wall clock (229 ms is CPU time only, from Lua's `os.clock()`, and understates the real cost — `docs/measurements.md` records both), and is the only strategy `history.commit(strategy="auto")` ships with in phase 1.

**Committed design:** `history.commit(strategy="auto")` tries the cheapest candidate that task 1 accepted, and falls back to C3 on any `nan` or on the mtime not advancing. C3 is known to work, so `history.commit` cannot fail for lack of a mechanism — it can only be slow. If C1/C2 are rejected, phase 1 ships with C3 as the only strategy and the design is unchanged apart from the recorded loop cost.

**Fallback if even C3 regresses** (not expected, it is a phase 0 observed fact): `history.read` returns `source: "journal"`, `confirmed: false` and the values Scouter itself last set, and `describe_history` says loudly that darktable has not committed yet. No verb ever invents a confirmed value.

Python side of `history.commit(image, timeout_s=5.0)`:

1. stat the XMP, remember `mtime_ns` and size;
2. call the bridge verb;
3. poll the XMP every 50 ms until `mtime_ns` advances **and** the document parses, up to `timeout_s`;
4. on expiry raise `CommitTimeout` with the strategy used and the elapsed ms.

`commit=True` is the default on every writer verb, so a conversational `darkroom.set` is readable immediately. `--no-commit` / `commit=false` exists for a scripted run of several sets followed by one commit (the cheap path when C3 is the only strategy).

### 8.2 `undo_last`

**Decision: journal-based inverse `set`, not `history_end` manipulation and not XMP re-application.**

`scouter/core/journal.py` appends one line to `~/.cache/scouter/journal/<job>.jsonl` **before** each write verb:

```json
{"seq": 7, "ts": "...", "verb": "darkroom.set", "image_id": 12, "path": "/abs/P6281348.ORF",
 "module": "exposure", "instance": 0, "param": "exposure",
 "before": {"source": "xmp", "value": 0.7, "enabled": true, "history_end": 12},
 "after": {"value": 1.5}, "undone": false}
```

`undo_last` takes the newest entry for the current job with `undone: false`, re-issues the **inverse absolute set** (`darkroom.action(..., effect="set", value=before.value)`), commits, verifies from the XMP, and marks the entry `undone: true`.

Why this and not the alternatives:

- **`history_end` decrement** would mean writing darktable's own history pointer. There is no Lua API for it, and the only way to reach it is `library.db` or rewriting the XMP under a live GUI — both forbidden by spec 6 ("never library.db directly"; darktable owns the sidecar while the image is open).
- **Re-apply the previous XMP** has the same problem: it writes the file darktable is about to overwrite from its in-memory history, so the edit would be silently lost on the next commit.
- **A Lua global-undo action** (`dt.gui.action("global/undo")` or a `views/darkroom` equivalent) is unverified. Task 1 probes it and records the result as a phase-2 note; phase 1 does not depend on it.
- The journal approach uses only the one primitive phase 0 proved (absolute `set`), produces a **new, normal history item** Pratik can see and himself undo with Ctrl-Z, never rewrites history behind his back (so it does not trip the history-overwrite confirm gate), and is unit-testable with a fake bridge and no darktable.

Cost to accept: the history grows by one item per undo rather than shrinking, and an undo of `module.enable` cannot be an inverse set — for those entries `undo_last` issues `module.disable` (recorded in the journal as `before.enabled`), and for `module.reset` it refuses with `outcome="error"`, `code="unsupported_undo"` and the message "reset discarded the module's params; darktable's own Ctrl-Z is the only way back". Undo of a `reset` is exactly why `reset` is behind a confirm gate.

---

## 9. Error classes and exit codes

`scouter/errors.py`. Every class carries a stable `code`, a human `fix` string, and is JSON-serialisable into the envelope as `{"outcome": "error", "code": ..., "message": ..., "fix": ...}`.

| Class | `code` | Raised when | `fix` |
|---|---|---|---|
| `BridgeTimeout` | `bridge_timeout` | `BridgeTimeoutError` or `BridgePluginNotInstalledError` from the client | "start darktable and run `scouter doctor`; the plugin must be installed and darktable restarted" |
| `WrongView` | `wrong_view` | pre-flight `view.current` is not `darkroom` | "open image N in the darkroom (`scouter darkroom open N`)" |
| `NotInDarkroom` | `not_in_darkroom` | the view **is** darkroom but the action returned `nan` | "module `X` is not in this image's pipeline, or the image is still loading; retry after `scouter darkroom open`" |
| `CliWroteNothing` | `cli_wrote_nothing` | `darktable-cli` exited 0 (or non-zero) and the output file is missing or zero-byte | "check that darktable's 'write sidecar file for each image' preference is on and the raw is supported" |
| `DbLocked` | `db_locked` | `darktable-cli` stderr matches `database is locked` / `could not get lock` | "a darktable-cli run escaped the isolated configdir; report the configdir in the event" |
| `StylePartial` | `style_partial` | `style.apply` reports `applied < len(image_ids)` or a non-empty `errors` | "see `per_image` in the result; re-run for the missed ids" |
| `CommitTimeout` | `commit_timeout` | the XMP mtime did not advance within `timeout_s` | "leave and re-enter the darkroom, or raise `--commit-timeout`" |

`ConfirmationRequired` is **not** an error: a gated verb called without `confirm=True` returns `outcome="dry_run"` with the `plan` it would have executed, exit code 5, and performs no bridge call.

Exit codes (`scouter.errors.EXIT_CODES`):

```
0 ok           2 usage (argparse)      5 dry_run / confirmation required
1 error (unclassified)
3 bridge_timeout   4 wrong_view   6 not_in_darkroom   7 cli_wrote_nothing
8 db_locked        9 style_partial     10 commit_timeout
```

Each class gets an error-path test in `tests/test_error_paths.py` driven by a fake bridge / fake `darktable-cli`, none of which needs darktable installed (spec 10).

---

## 10. The three layers

### 10.1 Core signatures

All in `scouter/core/`. Every function is typed, has no model call, and returns a frozen dataclass from `scouter/types.py`. `job: str | None = None` defaults to the ambient job id (`$SCOUTER_JOB`, else `job-<YYYYmmdd-HHMMSS>-<pid>`).

```python
# scouter/core/darkroom.py
def open_image(image: str | int, *, job: str | None = None) -> OpenResult: ...
def set_param(module: str, param: str, value: float, *, instance: int = 0,
              commit: bool = True, dry_run: bool = False,
              job: str | None = None) -> SetResult: ...
def nudge(module: str, param: str, steps: int = 1, *, instance: int = 0,
          commit: bool = True, dry_run: bool = False,
          job: str | None = None) -> SetResult: ...
def enable(module: str, *, instance: int = 0, dry_run: bool = False,
           job: str | None = None) -> ModuleResult: ...
def reset(module: str, *, instance: int = 0, confirm: bool = False,
          dry_run: bool = False, job: str | None = None) -> ModuleResult: ...
def undo_last(*, dry_run: bool = False, job: str | None = None) -> UndoResult: ...
def status(module: str | None = None, param: str | None = None, *, instance: int = 0,
           job: str | None = None) -> StatusResult: ...
def describe_history(image: str | int | None = None, *,
                     job: str | None = None) -> HistoryResult: ...

# scouter/core/history.py
def read(image: str | int, *, job: str | None = None) -> HistoryResult: ...
def commit(image: str | int | None = None, *, timeout_s: float = 5.0,
           strategy: str = "auto", job: str | None = None) -> CommitResult: ...

# scouter/core/scout.py
def stats(image: str | int, *, px: int = 1024, budget_s: float = 3.0,
          job: str | None = None) -> StatsResult: ...
def preview(image: str | int, *, px: int = 1024, fmt: str = "png",
            job: str | None = None) -> PreviewResult: ...
def compare(a: str | int, b: str | int, *, px: int = 1024,
            job: str | None = None) -> CompareResult: ...

# scouter/core/style.py
def extract(image: str | int, name: str, modules: list[str], *, description: str = "",
            confirm: bool = False, dry_run: bool = False,
            job: str | None = None) -> StyleResult: ...
def apply(image: str | int, name: str, *, mode: str = "append",
          confirm: bool = False, dry_run: bool = False,
          job: str | None = None) -> StyleResult: ...

# scouter/core/export.py
def run(images: list[str | int], preset: str, out_dir: str, *,
        confirm: bool = False, dry_run: bool = False,
        job: str | None = None) -> ExportResult: ...

# scouter/metrics.py
def summary(*, since: str = "7d") -> SummaryResult: ...
```

`image` accepts an absolute path or a darktable image id throughout; `core.dtbridge.resolve_image(image)` returns `ResolvedImage(image_id, path, xmp_path)` via `image.lookup`, and every result echoes both.

### 10.2 Verb registry (the single source of truth)

```python
# scouter/registry.py
@dataclass(frozen=True)
class Verb:
    id: str                      # "darkroom.set"
    fn: Callable[..., Any]
    args: tuple[ArgSpec, ...]
    writes: bool
    gated: bool                  # needs confirm=True / --yes
    summary: str

VERB_REGISTRY: dict[str, Verb]   # 17 verbs, section 10.3
def call(verb_id: str, layer: str, /, **kwargs) -> Envelope   # emits the metrics event
```

The CLI builds its argparse tree from `VERB_REGISTRY`; the MCP server builds its handler map from it; `tests/test_layer_parity.py` and `tests/test_metrics.py` use it as ground truth. A verb added to the registry without a CLI subcommand, an MCP tool or an emitted event fails CI.

### 10.3 Verb table (identical in all three layers)

| Verb id | CLI | MCP tool | Writes | Gated |
|---|---|---|---|---|
| `darkroom.open` | `scouter darkroom open IMAGE` | `darkroom_open` | no | no |
| `darkroom.set` | `scouter darkroom set MODULE PARAM VALUE [--instance N] [--no-commit] [--dry-run]` | `darkroom_set` | yes | no |
| `darkroom.nudge` | `scouter darkroom nudge MODULE PARAM [--steps N] …` | `darkroom_nudge` | yes | no |
| `darkroom.enable` | `scouter darkroom enable MODULE` | `darkroom_enable` | yes | no |
| `darkroom.reset` | `scouter darkroom reset MODULE --yes` | `darkroom_reset` | yes | **yes** |
| `darkroom.undo_last` | `scouter darkroom undo-last` | `darkroom_undo_last` | yes | no |
| `darkroom.status` | `scouter darkroom status [MODULE] [PARAM]` | `darkroom_status` | no | no |
| `darkroom.describe_history` | `scouter darkroom describe-history [IMAGE]` | `darkroom_describe_history` | no | no |
| `history.read` | `scouter history read IMAGE` | `history_read` | no | no |
| `history.commit` | `scouter history commit [IMAGE]` | `history_commit` | yes | no |
| `scout.stats` | `scouter scout stats IMAGE [--px N]` | `scout_stats` | no | no |
| `scout.preview` | `scouter scout preview IMAGE [--px N] [--fmt png\|ppm]` | `scout_preview` | no | no |
| `scout.compare` | `scouter scout compare A B [--px N]` | `scout_compare` | no | no |
| `style.extract` | `scouter style extract IMAGE --name N --modules a,b [--yes]` | `style_extract` | yes | **yes** (may replace an existing style) |
| `style.apply` | `scouter style apply IMAGE --name N [--mode append]` | `style_apply` | yes | no (ruling 31: phase 1 is append-only on one image; `--mode overwrite` is refused in core with `unsupported_mode` because the Lua API can only append; overwrite returns in phase 2 as a gated verb) |
| `export.run` | `scouter export run IMAGE… --preset client-final\|preview --out-dir D [--yes]` | `export_run` | yes | **yes** when a planned output file exists |
| `metrics.summary` | `scouter metrics summary [--since 7d]` | `metrics_summary` | no | no |

Gate rule, from spec 6: `confirm=True` / `--yes` is required for **more than one image**, **history overwrite**, **delete/reject** (no phase-1 verb does this), and **export over existing files**. `darkroom.reset` is gated because it discards params irrecoverably (section 8.2). `style.apply --mode overwrite` would be a history overwrite; phase 1 refuses it outright (ruling 31), so `style.apply` carries no gate until phase 2. `style.extract` is gated only when a style of that name already exists — the dry run lists what would be replaced.

Every gated verb, called without confirm, returns:

```json
{"verb":"style.apply","outcome":"dry_run","ok":true,"job":"job-...",
 "plan":[{"action":"style.apply","image_id":12,"path":"/abs/P6281348.ORF","name":"hero","mode":"overwrite"}],
 "reason":"mode=overwrite replaces this image's history; re-run with --yes",
 "data":{"would_apply":1}}
```

### 10.4 CLI contract

- `scouter <group> <verb> [args]`, one JSON object on stdout, nothing else on stdout ever (logs and progress go to stderr).
- `--json` is accepted and ignored (JSON is always the output); `--pretty` indents.
- `--job ID` overrides the ambient job id; `--dry-run` on every writer; `--yes` on every gated verb.
- Exit codes per section 9. `scouter doctor` (ungated, not a registry verb — it is a diagnostic, excluded from parity by an explicit allow-list) prints the darktable binary, the plugin install state, the current view and the cache paths.

### 10.5 MCP adapter

`scouter/mcp/server.py` mirrors `darktable_mcp/server.py`: `mcp.server.Server` with `on_list_tools` / `on_call_tool`, handwritten `Tool` definitions in `scouter/mcp/schemas.py`, one golden snapshot test (`tests/test_scouter_mcp_schemas.py`) that diffs the served contract against the pinned one and fails on any drift, plus a test that the golden set equals `VERB_REGISTRY`'s keys mapped through `.` → `_`.

Schema shape, summarised (full JSON is in `schemas.py` and the golden test):

- every tool: `type: "object"`, `additionalProperties: false`, explicit `required`;
- `image` params: `{"type": ["string","integer"], "description": "absolute path to the raw, or a darktable image id"}`;
- `value`: `{"type": "number"}`; `px`: `{"type":"integer","minimum":64,"maximum":4096,"default":1024}`;
- writer tools carry `dry_run: {"type":"boolean","default":false}`; gated tools additionally carry `confirm: {"type":"boolean","default":false}` with the description naming what it authorises;
- `mode`: `{"enum":["append","overwrite"]}`; `preset`: `{"enum":["client-final","preview"]}`; `fmt`: `{"enum":["png","ppm"]}`.

Results come back as a single `TextContent` holding the envelope JSON, exactly as the core returned it — the agent sees the same bytes a shell script would.

---

## 11. Observability

### 11.1 Event schema

One JSON line per core call, appended to `~/.cache/scouter/events.jsonl`, **and** one row in `~/.cache/scouter/metrics.db`, **and** one line in `~/.cache/scouter/audit/<job>.jsonl` (the audit line additionally carries the full args and the result summary; the event carries only a digest, so events stay cheap and shareable).

```json
{"ts":"2026-09-13T22:55:01.123456Z","job":"job-20260913-225458-41207","verb":"darkroom.set",
 "layer":"mcp","args_digest":"sha256:9f2c1ab40d7e","images":[12],"ms":412,
 "outcome":"ok","bytes_in":96,"bytes_out":734,"est_tokens":183,"phase":1,"version":"0.1.0"}
```

- `layer` ∈ `core | cli | mcp`. The CLI and MCP adapters set the layer before calling `registry.call`, so one user action produces exactly one event, tagged with the layer it entered through.
- `outcome` ∈ `ok | dry_run | error:<code>` (e.g. `error:bridge_timeout`), so failure counts by class are one `GROUP BY` away.
- `args_digest` = `"sha256:" + sha256(canonical_json(args))[:12]`. Arguments themselves are in the audit log, not the event, because a path is personal data and the event file is the one that leaves the machine for Grafana.
- `est_tokens` = `ceil(bytes_out / 4)`, labelled as an estimate in the schema comment and in `metrics summary` output (spec 6.1: the MCP layer cannot see the model's real count).
- `bytes_in` = length of the canonical args JSON; `bytes_out` = length of the canonical JSON of the core result payload (the envelope's ~120-byte header is excluded, because the payload is what the agent actually reads).

```sql
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, job TEXT NOT NULL, verb TEXT NOT NULL, layer TEXT NOT NULL,
  args_digest TEXT NOT NULL, images TEXT NOT NULL, ms INTEGER NOT NULL,
  outcome TEXT NOT NULL, bytes_in INTEGER NOT NULL, bytes_out INTEGER NOT NULL,
  est_tokens INTEGER NOT NULL, phase INTEGER NOT NULL, version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_verb_outcome ON events(verb, outcome);
```

Writes are `sqlite3` with `PRAGMA journal_mode=WAL`, `timeout=5`, and **best-effort**: a metrics failure logs to stderr and never fails the verb. The JSONL append is the durable record.

### 11.2 `metrics summary`

`scouter metrics summary --since 7d` prints (as JSON, and as the same table the weekly brief shows when `--pretty`):

- per verb: calls, ok / dry_run / error counts, error rate, p50 and p95 ms, total and mean `est_tokens`;
- headline rows: total estimated tokens, Scouter loop p50/p95 (`scout.stats` ms), failure count by class;
- an `alerts` array listing every verb whose error rate exceeds 10 % over at least 20 calls (spec 6.1's threshold; the Telegram target itself is phase 2).

### 11.3 The coverage test

`tests/test_metrics.py::test_every_registered_verb_emits_one_event` iterates `VERB_REGISTRY`, calls each verb through `registry.call` with a fake bridge, a fake `darktable-cli` and a temp `XDG_CACHE_HOME`, and asserts exactly one event line and one DB row per call, with all schema keys present and `verb` matching. A new verb with no event, or with two events, fails CI.

---

## 12. Testing strategy

`unittest.TestCase` classes throughout, run by `python3 -m pytest tests -q`. **No new test requires darktable.**

| Layer | File | Doubles |
|---|---|---|
| Lua dispatcher | `tests/lua/test_dispatcher.lua` (+ the pytest wrapper) | stub `dt` table extended with `gui.action`, `gui.current_view`, `gui.views`, `styles`, `gui.action_images` |
| Bridge client | `tests/test_bridge.py` (existing) + new timeout-table assertions | fake worker writing response files |
| `core.dtbridge` classification | `tests/test_error_paths.py` | `FakeBridge` returning `nan`, wrong view, raising `BridgeTimeoutError` |
| XMP reader | `tests/test_xmp.py` | `tests/fixtures/P6281348.ORF.xmp` (a copy of the real spike sidecar) + a truncated copy + a `gz`-params copy |
| PPM/stats | `tests/test_ppm.py` | synthetic 8-bit and 16-bit P6 rasters built in the test (flat grey, clipped white, RGB ramp) with hand-computed expected stats |
| Journal / undo | `tests/test_journal.py` | temp cache dir, `FakeBridge` |
| Confirm gates | `tests/test_confirm_gates.py` | `FakeBridge` that fails the test if called |
| Metrics | `tests/test_metrics.py` | temp `XDG_CACHE_HOME` |
| Layer parity | `tests/test_layer_parity.py` | none |
| MCP schemas | `tests/test_scouter_mcp_schemas.py` | golden snapshot + a real in-process MCP session |
| CLI | `tests/test_cli.py` | `subprocess` on `python3 -m scouter`, monkeypatched core via an env-selected fake registry |
| **Host integration** | `tests/test_host_integration.py` | the real darktable and the real ORFs |

Host-only tests are marked with the module-level guard

```python
DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
SKIP_REASON = f"host-only: {DARKTABLE} not present (darktable not installed on this machine)"
```

and skipped with that printed reason when the binary is absent. They may write XMP sidecars **only** next to `~/Pictures/EditingTrial/*.ORF`, and must:

1. launch `/Applications/darktable.app/Contents/MacOS/darktable <ORF>` (the `open -a` form does not work on this Mac — phase 0 finding 5);
2. wait for the bridge by polling `view.current` until it answers `darkroom`, with a 90 s budget;
3. run the verbs;
4. quit darktable gracefully in a `finally`: `osascript -e 'tell application "darktable" to quit'`, wait 20 s, then `SIGTERM`, wait 10 s, then `SIGKILL` as the last resort — and **never** leave the process running. `SIGKILL` before a commit loses the edits (phase 0 finding 3), so the test commits before quitting.

---

## 13. Open items this design closes (decisions the system spec left to phase 1)

1. **Package layout**: add `scouter/`, keep `darktable_mcp/` (section 3).
2. **One plugin, one spool**: extend `darktable_mcp/lua/darktable_mcp.lua` rather than shipping a second plugin; Scouter's own state lives under `~/.cache/scouter/` (section 3).
3. **`undo_last` mechanism**: journal-based inverse absolute `set` (section 8.2).
4. **Commit trigger**: `auto` with the cheapest accepted strategy, C3 view-bounce as the proven floor (section 8.1).
5. **Status positions are never reported as values**; XMP is the only value source in phase 1 (section 7.3).
6. **PPM, not PNG, for stats** — `--out-ext ppm` is supported and 16-bit; PNG is only the human preview (section 6).
7. **Previews are returned as paths, not inline bytes** (section 6.3).
8. **Statistics are 8-bit** (high byte of the 16-bit render), documented as an approximation (section 6.2).
9. **`ConfirmationRequired` is a result, not an error**; exit code 5, `outcome="dry_run"` (section 9).
10. **`args_digest` in events, full args only in the audit log** (section 11.1).
11. **Two extra bridge verbs** beyond the system spec's list — `view.current` and `image.lookup` — because the `wrong_view` guard and path→id resolution need them and every other verb depends on them.
12. **`snapshot.compare` is not a phase-1 verb**, and `snapshot.take` exists at the bridge layer only — it is one action, Pratik uses darktable's own compare slider, and the phase-1 core verb list (section 10.3) deliberately has no snapshot verb. `scout.compare` covers the numeric comparison the agent needs. Promoting `snapshot.take` to a core verb is phase 2 and costs one registry entry.
