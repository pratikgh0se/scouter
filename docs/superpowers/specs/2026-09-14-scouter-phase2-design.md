# Scouter Phase 2: Volume (library import, cull, style engine for a set, delivery, backup)

**Date:** 2026-09-14
**Phase:** 2 of the system spec's delivery table ("Volume").
**System spec:** `~/second-brain/Specs/darkroom-agent.md` (approved, row 275) — sections 4.4, 4.5 (memory is phase 3; only the file layout is reserved here), 4.6, 4.7, 4.8, 5, 6, 6.1, 6.2, 7, 8, 10, 11.
**Phase 1 design:** `docs/superpowers/specs/2026-09-13-scouter-phase1-design.md` (built, merged into local `main` at `9f85584`).
**Phase 1 live findings:** `~/second-brain/Knowledge/Scouter - Phase 1 build and live findings 2026-09-14.md` (the darktable facts this design is built on).
**Phase 1 ledger:** `.superpowers/sdd/2026-09-13-scouter-phase1/progress.md` — rulings 1-35 are binding conventions and are restated in section 3.1.
**Measurements:** `docs/measurements.md`.
**Plan:** `docs/superpowers/plans/2026-09-14-scouter-phase2.md`

---

## 1. Goal and exit criteria

Phase 1 made one frame editable by conversation. Phase 2 makes a **job** deliverable: a card becomes a named, checksummed, backed-up film roll; a proposal culls it; one hero's recipe becomes a style that lands on the whole set; the frames that fell outside tolerance are listed by number; proofs and finals leave with a manifest. Every step is a deterministic core function that runs with **no model** (spec 6.2).

The system spec's phase 2 exit is *one 300-frame job delivered end to end*. That is a job Pratik runs, not something CI can assert, so this design carries a machine-checkable stand-in: a **30-frame synthetic job** built by copying the EditingTrial ORFs, driven entirely through the CLI. E1-E8 are the stand-in; E12 is the real exit.

| # | Criterion | How it is proven |
|---|---|---|
| E1 | `library.import` refuses with `backup_not_configured` when no backup target is configured, before it reads a single file | `tests/test_config.py::test_import_refuses_without_backup_target` (no darktable) |
| E2 | 30 frames go card → `<library>/<YYYY>/<YYYY-MM-DD slug>/raw/` with sha256 verified per file, renamed `<date>_<job>_<cam>_<seq>.<ext>`, originals `0444`, film roll registered, vault job note written | `tests/test_host_phase2.py::test_import_30_frames` (host-only) |
| E3 | `cull.propose` groups the 30 frames into the fixture's 6 bursts and proposes exactly one pick per burst, every proposal carrying its numbers; `cull.apply --yes` writes the ratings and verifies them from the XMP | `tests/test_host_phase2.py::test_cull_proposal_and_apply` |
| E4 | `style.extract --modules exposure,sigmoid` writes a `.dtstyle` containing **only** those operations and darktable imports it | `tests/test_style_dtstyle.py` (unit, against the pinned fixture) + `tests/test_host_phase2.py::test_style_extract_filtered` |
| E5 | `style.apply_set` applies that style to the other 29 frames with per-image results and `failed == 0` | `tests/test_host_phase2.py::test_style_apply_set` |
| E6 | `outliers.list` reports each frame's EV proxy, WB and clipping deltas against the hero with the spec's tolerances (±0.3 EV, ±300 K, >1 % clipped) | `tests/test_outliers.py` (unit, synthetic stats) + `tests/test_host_phase2.py::test_outliers_over_the_set` |
| E7 | `export.release --preset client-proof` writes 2048 px sRGB JPEGs into `<job>/exports/client-proof/` plus a manifest whose sha256 for every output re-hashes true | `tests/test_host_phase2.py::test_release_with_manifest` |
| E8 | A repeat `darkroom.set` with the value the XMP already carries costs **zero** bridge writes and **zero** view bounces (phase 1: two bounces, 5.5 s) | `tests/test_history_changed.py` (unit, counting FakeBridge calls) + a host timing assertion `< 1.5 s` recorded in `docs/measurements.md` |
| E9 | `backup.run` produces an encrypted archive + an rsync copy of the raws, and `backup.verify` decrypts, re-hashes and confirms both | `tests/test_backup.py` (unit, temp target, `openssl` present) + `tests/test_host_phase2.py::test_backup_round_trip` |
| E10 | Every new verb emits exactly one metrics event | the existing `tests/test_metrics.py::test_every_registered_verb_emits_one_event` picks them up from the registry |
| E11 | Three layers, one vocabulary, MCP schemas pinned | the existing `tests/test_layer_parity.py` and `tests/test_scouter_mcp_schemas.py` (golden file regenerated once, in task 14) |
| E12 | **Phase exit:** one 300-frame job delivered end to end | Pratik's own job; the run is recorded in a vault findings note with the per-stage times from `scouter metrics summary` |

Budget targets, to be measured and recorded in `docs/measurements.md` (not asserted as hard gates except where noted):

| Stage | Target on this Mac | Why |
|---|---|---|
| `library.import`, 300 × 20 MB | < 10 min | two sha256 passes per file (write + verify) over ~6 GB; I/O bound |
| set stats, 300 frames at 512 px, 4 workers | < 6 min | `darktable-cli` at 512 px measured ~1.3 s warm; 300/4 × 1.3 s ≈ 100 s plus contention |
| `style.apply_set`, 300 frames | < 2 min | `image:apply_style` is a library write, no pipe reload |
| `export.release`, 60 finals full size | < 8 min | full-size render dominates |
| no-op `darkroom.set` | **< 1.5 s asserted** (E8) | phase 1's 5.5 s is the single worst live cost |

---

## 2. Non-goals

Explicitly **not** in phase 2, so no task may grow to include them:

- **Style memory / `Dragon Radar`** — `looks.json`, vault look notes, the acceptance signal (phase 3, spec 4.5). Phase 2 only *reserves* the directory (`<library>/scouter/memory/`) so the backup job already covers it, and writes nothing into it.
- **Retouch / spot detection / guided healing** (phase 3, spec 8.3).
- **Gallery delivery, shared links, client picks flowing back** (phase 4, parked, row 274). `export.release` prints the folder; Pratik shares it by hand.
- **Cloud object storage for backups** (B2/R2, spec 13.3). Phase 2's backup target is a configured **local or mounted path**; the encrypted archive is upload-ready but nothing uploads, and no Potara key is needed. See open question 1.
- **Face / eyes-open culling** (spec 13.4: stats only, no model).
- **Grafana series and Telegram alerting** off `metrics.db` (spec 6.1) — still a Capsule-side task.
- **A second darktable instance, or any `library.db` read or write.** Unchanged invariant.
- **Multi-instance module addressing** beyond passing `instance` through, and **mask/blendop editing** of any kind. `.dtstyle` plugins carry their `blendop_params` through verbatim, byte for byte, and are never decoded or rewritten.
- **`style.apply --mode overwrite` through the bridge.** Still impossible (Lua 9.6.0 `image:apply_style` appends). Overwrite exists only for *renders*, as `darktable-cli --style-overwrite` inside `export.release`.
- **Video (NX5)**, and any raw format outside `.ORF`, `.ARW`, `.DNG` (+ their sibling JPEGs).
- **Inverting a slider's normalised status position into a value.** Still XMP-only. The one exception phase 2 adds is white balance, and it is a *measured table*, not an inversion (section 11).

---

## 3. Architecture deltas

Phase 1's picture is unchanged; phase 2 adds a configuration layer above the core, a library layer beside it, and two new read paths (EXIF, the stats cache).

```
                                  ~/.config/scouter/config.json   (NEW: library root, backup target,
                                            │                      camera slugs, tolerances, presets)
                                            ▼
Pratik ──chat──▶ Claude ──MCP──▶ scouter.mcp.server ─┐
shell/launchd ──────────────────▶ scouter.cli.main ──┼──▶ scouter.registry.call(verb, **args)   (31 verbs)
python import ──────────────────────────────────────▶┘            │
                                                                  ├─▶ core.config ─▶ config.json + wb/<camera>.json
                                                                  ├─▶ core.library ─▶ COPY card → <library>/<YYYY>/<YYYY-MM-DD slug>/
                                                                  │        │           raw/ exports/ .scouter/{checksums.sha256,import.json,…}
                                                                  │        ├─▶ core.exif  ──▶ TIFF IFD0 of the raw (NEW read path)
                                                                  │        ├─▶ core.dtbridge ─▶ import_batch  (film roll)
                                                                  │        └─▶ core.vault  ──▶ $OBSIDIAN_VAULT_PATH/Projects/Photo jobs/<job>.md
                                                                  ├─▶ core.cull ──┐
                                                                  ├─▶ core.style ─┼─▶ core.scout.stats_many ─▶ stats cache
                                                                  │      │        │      ~/.cache/scouter/stats/<sha1>.json   (NEW)
                                                                  │      │        └─▶ core.wb ─▶ Kelvin↔coefficient table
                                                                  │      └─▶ core.dtstyle ─▶ <cache>/styles/<name>.dtstyle ─▶ style.import_file
                                                                  ├─▶ core.export ─▶ darktable-cli (presets, --style-overwrite) + manifest
                                                                  ├─▶ core.backup ─▶ tarfile → age|openssl → <target>/archives/<ts>/
                                                                  │                  rsync -a <library>/ <target>/library/
                                                                  └─▶ scouter.metrics.emit  ──▶ events.jsonl + metrics.db + audit/<job>.jsonl
```

New modules: `scouter/config.py`, `scouter/core/{exif,library,vault,cull,dtstyle,wb,backup}.py`. Extended: `scouter/core/{style,export,scout,history,darkroom,xmp}.py`, `scouter/{paths,types,errors,registry-consumers}.py`, `darktable_mcp/lua/darktable_mcp.lua`, `darktable_mcp/bridge/client.py`, `darktable_mcp/darktable/cli_wrapper.py`.

### 3.1 Conventions carried forward (phase 1 rulings 1-35, binding)

1. **Three layers over one core.** Every capability is a plain typed function in `scouter/core/`, registered in `VERB_REGISTRY`, reachable from the CLI and from MCP with the same name, arguments and result shape. Nothing in phase 2 is CLI-only or MCP-only.
2. **One event per top-level verb.** Nested core calls go through `__wrapped__` (ruling 23). A verb that composes others — `library.import`, `cull.propose`, `export.release`, `backup.run` — emits exactly **one** event and one audit line.
3. **Dry runs never open, import or write.** A dry run resolves images through `image.lookup` only (ruling 24); it may `stat`, hash **nothing**, and must never move the darkroom.
4. **Ruling 30 (job-image guard) stands.** A darkroom write refuses when `darkroom.current` differs from the job's last opened image.
5. **stdlib + `mcp>=2,<3` only.** No Pillow, rawpy, numpy, tomllib (3.11+, above our 3.10 floor), exiftool, or `age` as a hard dependency.
6. **No `library.db` read or write.** Everything goes through the Lua API or `darktable-cli`.
7. **Host tests are gated by `SCOUTER_HOST_TESTS=1` *and* the darktable binary's presence** (ruling 17), print their skip reason, commit before quitting darktable (ruling 16), and never leave darktable running.
8. **Honest limits are surfaced in the result, never hidden.** No guessed parameter value, no phantom success, no invented capability. When a capability does not exist, the verb refuses with a typed code and says what does exist.
9. **Never overwrite blind.** Applies to files as well as notes: an existing destination, manifest, style, or vault note is appended to or refused, never silently replaced.

---

## 4. Configuration

### 4.1 Format and location: JSON at `~/.config/scouter/config.json`

**Decision: JSON, not TOML.** `tomllib` is stdlib only from Python 3.11 and this package's floor is 3.10 (`pyproject.toml`, set by `mcp`'s own floor); a TOML config would either raise the floor or add a dependency, both forbidden by constraint 5. JSON costs a little beauty and buys zero dependencies and a writer (`json.dump`) we already ship — `scouter config set` can therefore edit the file safely, which a hand-rolled TOML writer could not.

Resolution order: `$SCOUTER_CONFIG`, else `$XDG_CONFIG_HOME/scouter/config.json`, else `~/.config/scouter/config.json`. Missing file is **not** an error for read verbs; it is an error for anything gated on it (section 4.3).

### 4.2 Schema (version 1)

```json
{
  "version": 1,
  "library_root": "/Users/pratik/Pictures/EditingTrial-library",
  "import": {
    "extensions": ["ORF", "ARW", "DNG", "JPG", "JPEG"],
    "read_only_originals": true,
    "seq_digits": 4,
    "skip_identical": true
  },
  "cameras": {
    "E-M10 Mark III": "em10iii",
    "ILCE-7": "a7"
  },
  "vault": {
    "jobs_dir": "Projects/Photo jobs"
  },
  "backup": {
    "enabled": true,
    "target": "/Volumes/ScouterBackup",
    "rsync_raws": true,
    "encryption": "openssl",
    "age_recipient": null,
    "openssl_passfile": "/Users/pratik/.config/scouter/backup.pass",
    "keep_archives": 14,
    "allow_unencrypted": false,
    "include_darktable_db": true
  },
  "tolerances": {
    "exposure_ev": 0.3,
    "temperature_k": 300,
    "clipped_high_pct": 1.0,
    "wb_ratio": 0.03
  },
  "cull": {
    "burst_gap_s": 2.0,
    "sharpness_floor": 0.20,
    "luminance_low": 25,
    "luminance_high": 230,
    "clipped_flag_pct": 5.0,
    "pick_rating": 2,
    "keep_rating": 1
  },
  "style": {
    "never_include": ["crop", "clipping", "retouch", "spots", "flip",
                      "rotatepixels", "censorize", "watermark", "ashift"]
  },
  "export": {
    "min_rating": 2,
    "final_label": null
  },
  "stats": {
    "set_px": 512,
    "workers": 4,
    "cache_max_age_days": 30
  }
}
```

Every key has a default in `scouter/config.py::DEFAULTS`; a config file may set any subset, and unknown keys are **rejected** (`usage`, listing the unknown key and the nearest known one) rather than ignored — a typo in `backup.target` must not silently disarm the gate. `library_root` has **no default**: a config without it fails every library verb with `usage`.

`Config` is a frozen dataclass tree (`Config`, `ImportConfig`, `BackupConfig`, `Tolerances`, `CullConfig`, `StyleConfig`, `ExportConfig`, `StatsConfig`) so core functions take `cfg: Config` and tests build one in three lines without touching the filesystem.

### 4.3 The backup gate (spec 4.6: "the import verb refuses to run if no backup target is configured")

`config.require_backup(cfg)` returns the `BackupConfig` or raises `BackupNotConfigured` (code `backup_not_configured`, exit 11). It refuses when **any** of these holds:

| Condition | Message names |
|---|---|
| no config file at all | the resolved path and `scouter config init` |
| `backup.enabled` is false | the key to flip |
| `backup.target` is null or empty | the key |
| `backup.target`'s **parent** does not exist, or the target is not writable | the path and its `stat` result (a missing mount point is the common real case) |
| `backup.encryption == "none"` and `allow_unencrypted` is not `true` | both keys, and that the spec calls for an encrypted archive |
| `encryption == "openssl"` and the passfile is missing, empty, or its mode is wider than `0600` | the passfile path and its octal mode |
| `encryption == "age"` and `age_recipient` is null, or `age` is not on `PATH` | the key and `brew install age` |

The gate is checked by `library.import` (before reading the card) and by `backup.run`. It is **not** checked by reads, by `cull.*`, `style.*`, `outliers.list` or `export.*` — those operate on a library that already passed the gate at import time, and blocking a proof export because a backup disk is unplugged would be gate theatre.

`scouter config init` writes a commented starter file (a `"_comment"` key per section, since JSON has no comments) with `backup.enabled: false`, so the very first run fails the gate loudly rather than importing unprotected.

### 4.4 Job layout

```
<library_root>/
  <YYYY>/
    <YYYY-MM-DD job-slug>/
      raw/         the copied, renamed originals (0444) + darktable's .xmp sidecars beside them
      exports/
        client-proof/  final/  print/          one directory per preset, created on release
      .scouter/
        import.json              what was copied, from where, with what EXIF (relative paths only)
        checksums.sha256         sha256sum-format lines over raw/, coreutils-compatible
        cull-<ts>.json           the proposal cull.apply consumes
        stats-<px>.json          the set's stats snapshot (cache mirror, so a moved library keeps it)
        manifest-<preset>-<ts>.json      delivery manifest
        manifest-<preset>-<ts>.sha256    its own checksum
  scouter/
    memory/        reserved for phase 3 (spec 4.5); empty, but inside the backup set
```

`job-slug` is `[a-z0-9][a-z0-9-]{0,47}`, validated like a job id; the directory name is `<date> <slug>` with a space, exactly as spec 4.6 writes it. darktable's film roll is `<job>/raw`, **not** the job directory, so the film roll never contains exports or Scouter's own files.

**Moving the library without re-import** (spec 13.2: internal disk with `~/Pictures/EditingTrial` until the SSD arrives). Three properties make the move cheap, and one honest caveat:

1. Everything Scouter writes inside a job uses paths **relative to the job directory**. `import.json`, `checksums.sha256` and every manifest are portable as-is; `library.verify` works at any root.
2. The only absolute path Scouter holds is `config.library_root`. `library.relocate --to <new root>` rewrites that one key, then re-verifies every job's checksums at the new root and reports per-job `ok`/`mismatch`/`missing`.
3. XMP sidecars travel **beside the raws**, so every edit, rating and style application survives the move untouched.
4. **Caveat, stated plainly:** darktable's own film rolls live in `library.db` with absolute paths, and Scouter may not write `library.db`. After a move, darktable still points at the old paths. `library.relocate` therefore prints the exact GUI step (import the new `<job>/raw` folders; darktable reads the sidecars beside them and the history comes back) and reports `darktable_rollpaths_stale: true`. No edit is lost, but darktable does need to be re-pointed. Claiming otherwise would be a phantom success.

---

## 5. EXIF: a stdlib TIFF reader

**Decision: a ~120-line stdlib TIFF/IFD0 reader in `scouter/core/exif.py`. Not `darktable-cli`, not `exiftool`.**

Justification:

- **`darktable-cli` cannot print EXIF.** It has no metadata dump mode; the only way to get a camera model out of it is to export an image and read the output's EXIF, i.e. a ~2 s render per frame. For 300 frames that is 10 minutes to learn something that takes 2 ms to read directly.
- **`exiftool` is not installed on this Mac** (verified 2026-09-14: `which exiftool` → not found) and is not stdlib, so it fails constraint 5 and would make import depend on a Homebrew package.
- **ORF, ARW and DNG are all TIFF containers**, so one IFD0 walk covers every format phase 2 accepts. Verified on the real file 2026-09-14:

```
/Users/pratik/Pictures/EditingTrial/P6281348.ORF
  byte order II (little endian), magic 20306 (0x4F52), IFD0 at offset 8, 23 entries
  0x010F Make  = "OLYMPUS CORPORATION    \0"
  0x0110 Model = "E-M10 Mark III  \0"
  0x0132 DateTime = "2026:06:28 15:49:28\0"
  0x8769 ExifIFD → 0x9003 DateTimeOriginal = "2026:06:28 15:49:28\0"
                   0x829A ExposureTime = rational 1/50
```

**Pinned fact that a naive reader gets wrong:** ORF's TIFF magic is **0x4F52 (20306)**, not 42. A strict `magic == 42` check rejects every Olympus raw. So `ACCEPTED_MAGIC = frozenset({42, 20306, 21330, 85})`: 42 (0x2A) is standard TIFF — ARW, DNG; 20306 (0x4F52) is the ORF spelling Pratik's camera writes and the one verified above; 21330 (0x5352) is the other ORF spelling in the wild; 85 (0x55) is Panasonic RW2, accepted because it costs one set member and stops the magic check from quietly becoming a format whitelist. Anything else raises `ExifUnreadable` with `detail.magic`.

Interface:

```python
@dataclass(frozen=True)
class ExifTags:
    make: str | None
    model: str | None
    captured_at: str | None       # "YYYY-MM-DD HH:MM:SS", from 0x9003, else 0x0132, else None
    subsec: int                   # 0x9291 SubSecTimeOriginal as an int, 0 when absent
    exposure_time: float | None   # seconds
    iso: int | None
    f_number: float | None
    byte_order: str               # "II" | "MM"
    magic: int
    source: str                   # "ifd0+exififd" | "ifd0" | "none"

def read_tags(path: str | Path, *, limit: int = 1 << 20) -> ExifTags
def camera_slug(tags: ExifTags, cfg: Config) -> str
def capture_key(tags: ExifTags, path: str | Path) -> tuple[str, int, str]
```

- Only the first `limit` bytes (1 MiB) are read; IFD0 and the Exif IFD sit in the header of every raw phase 2 accepts. A value whose offset is past `limit` is reported as `None` with `source` degraded, never as a guess.
- `camera_slug`: `cfg.cameras[model]` if mapped, else a slug of the model (`lower()`, non-alphanumerics → nothing: `"E-M10 Mark III"` → `em10markiii`), else — when `model` is `None` and no `--camera` was given — `ExifUnreadable` (exit 14) naming the file. The import never invents `unknown` as a camera: a wrong name is baked into 300 filenames forever.
- `capture_key` is the rename/burst sort key: `(captured_at or "", subsec, original filename)`. Files with no capture time sort last, by filename, and the import warns.

Rationals are read as `(num, den)` and divided with a zero guard. Strings are `rstrip(b"\x00 ")` then `decode("ascii", "replace")`, because the real Olympus Make is space-padded to 24 bytes.

---

## 6. `library.import`

```
scouter library import SOURCE --job-slug JOBSLUG [--date YYYY-MM-DD] [--camera SLUG]
                       [--extensions ORF,ARW] [--dry-run] [--yes]
```

Algorithm, in order, with the refusal points named:

1. **Load the config and the backup gate** (section 4.3). Nothing is read from the card before this passes.
2. **Scan `SOURCE`** recursively (depth ≤ 8, symlinks not followed) for files whose suffix is in `import.extensions`, case-insensitively. Empty result → `usage` naming the extensions searched.
3. **Read EXIF** for every candidate (section 5). `--date` defaults to the **earliest** `captured_at` in the set, as `YYYY-MM-DD`; `--camera` defaults to the single slug the set agrees on, and a set spanning two camera models without an explicit `--camera` is a `usage` refusal listing both (a mixed-camera card would otherwise get one body's name).
4. **Plan the names.** Sort by `capture_key`; assign `seq` from 1, zero-padded to `import.seq_digits`. Files sharing a stem (`P6281348.ORF` + `P6281348.JPG`) share one `seq`. Name: `<date>_<slug>_<cam>_<seq>.<ext>` with the extension **lowercased** (`2026-06-28_harbour-wedding_em10iii_0001.orf`). Collisions inside one plan are impossible by construction and asserted anyway.
5. **Dry run / confirm.** The plan (source → destination, bytes, camera, capture time) is returned as `outcome="dry_run"` unless `confirm=True`. An import is always > 1 file, so it is **always** gated (spec 6). Dry runs hash nothing and create no directories (ruling 3).
6. **Create the layout** (section 4.4), `raw/` and `.scouter/` only. `exports/` is created by `export.release`.
7. **Copy and verify, file by file.** `shutil.copyfileobj` in 1 MiB chunks, hashing the source *as it is read*; then re-read the destination and hash it. Equal → ok. Unequal → delete the destination, raise `ChecksumMismatch` (exit 12) naming both digests, and stop (a bad card or a failing disk must not produce 299 more files).
8. **Resume-safe.** A destination that already exists is hashed: identical → `skipped_identical` (when `import.skip_identical`), different → `DestinationCollision` (exit 13), never overwritten.
9. **`chmod 0444`** on each destination when `import.read_only_originals` (spec 6: originals are read-only). The XMP sidecars darktable writes beside them are **not** touched — darktable must be able to write those.
10. **Write `.scouter/checksums.sha256`** (`<hex>  raw/<name>` lines, two spaces, coreutils order, sorted by name) and `.scouter/import.json` (see below), both atomically (temp file + `os.replace`).
11. **Register the film roll** through the existing bridge verb: `import_batch {source_path: "<job>/raw", recursive: false}`. Its honest fields are carried straight into the result: `imported`, `scan_incomplete`, `recursive_honoured`, `note`. When darktable is not running the import does **not** fail — the files are copied and verified, which is the irreversible part; `film_roll_registered: false` plus the bridge error's `fix` go into `warnings`, and `library.register <job>` re-tries it later.
12. **Write the vault job note** (section 7). A vault failure is likewise a warning, never a rollback.

`.scouter/import.json`:

```json
{
  "version": 1,
  "job_slug": "harbour-wedding",
  "date": "2026-06-28",
  "camera": "em10iii",
  "camera_model": "E-M10 Mark III",
  "camera_make": "OLYMPUS CORPORATION",
  "imported_at": "2026-09-14T10:22:41Z",
  "scouter_version": "0.2.0",
  "source_root": "/Volumes/OLYMPUS/DCIM",
  "files": [
    {"seq": 1, "dest": "raw/2026-06-28_harbour-wedding_em10iii_0001.orf",
     "source_name": "P6281348.ORF", "bytes": 14417349,
     "sha256": "…", "captured_at": "2026-06-28 15:49:28", "subsec": 0,
     "exposure_time": 0.02, "iso": 200, "f_number": 5.6, "status": "copied"}
  ],
  "totals": {"planned": 30, "copied": 30, "skipped_identical": 0, "bytes": 432520470}
}
```

`source_root` is the only absolute path recorded, and it describes a card that will be gone; every other path is relative (section 4.4, property 1).

`library.verify JOBSLUG` re-hashes `raw/` against `checksums.sha256` and returns `{checked, ok, mismatched: [...], missing: [...], extra: [...]}`. `extra` lists files in `raw/` that the manifest does not know (darktable's `.xmp` sidecars are excluded by suffix, not by silence).

`library.jobs` lists every `<YYYY>/<date slug>` under `library_root` with its frame count, bytes, ratings histogram (from the XMPs' `xmp:Rating`, cheap) and which manifests exist. This is the one verb that makes the layout browsable without `ls`.

---

## 7. The vault job note

Path: `$OBSIDIAN_VAULT_PATH/Projects/Photo jobs/<YYYY-MM-DD job-slug>.md` (from `cfg.vault.jobs_dir`, which may be overridden). `Projects/Photo jobs/` does not exist yet and is created.

**Degradation (user-level rule):** if `$OBSIDIAN_VAULT_PATH` is unset or the directory is missing, `core.vault.write_job_note` returns `{written: false, reason: "vault not present"}` and the import carries it as a warning. Nothing fails.

**Never overwrite blind (user-level rule):** if the note exists, the writer **appends** a `## Import <ISO ts>` section and merges frontmatter keys additively (`frames`, `bytes` are summed; lists are unioned; scalars already present are left alone and the new value is noted in the appended section). It never rewrites the body.

New note, AI-first:

```markdown
---
type: photo-job
date: 2026-06-28
tags: [photo-job, scouter, darktable, capsule-corp]
ai-first: true
project: "[[Capsule Corp]]"
related: ["[[Specs/darkroom-agent]]", "[[Knowledge/Scouter - Phase 1 build and live findings 2026-09-14]]"]
job_slug: harbour-wedding
camera: em10iii
camera_model: E-M10 Mark III
frames: 30
bytes: 432520470
library_path: 2026/2026-06-28 harbour-wedding
imported_at: 2026-09-14T10:22:41Z
scouter_version: 0.2.0
status: imported
---

## For future agent
Photo job `harbour-wedding`, shot 2026-06-28 on the E-M10 Mark III (30 frames,
412 MiB), imported by Scouter 0.2.0 on 2026-09-14. The raws are read-only at
`<library_root>/2026/2026-06-28 harbour-wedding/raw` with sha256 in
`.scouter/checksums.sha256`; `library_root` lives in `~/.config/scouter/config.json`,
so read it from there rather than assuming a path. Edits are darktable history
in the `.xmp` beside each raw. Stages below are appended as they happen — a
stage with no section has not run. Verify anything numeric with
`scouter library verify harbour-wedding --json`.

## Import 2026-09-14T10:22:41Z
| | |
|---|---|
| source | `/Volumes/OLYMPUS/DCIM` |
| copied / skipped | 30 / 0 |
| film roll | registered (darktable reported 30) |
| checksums | `.scouter/checksums.sha256` |

## Stages
- [ ] cull proposed
- [ ] hero style extracted
- [ ] style applied to the set
- [ ] outliers reviewed
- [ ] proofs released
- [ ] finals released
```

`cull.apply`, `style.apply_set`, `outliers.list` and `export.release` each append their own `## <Stage> <ts>` section with the numbers they produced (not prose) and tick the matching box. One writer, `core.vault.append_stage(job_slug, stage, rows, checklist_key)`, so the format cannot drift between verbs.

---

## 8. Cull assistant (`Ki Sense`, stats only)

### 8.1 The set-stats engine (shared by cull and outliers)

`scout.stats_many(paths_in, *, px, workers, job_dir=None)` — an internal helper (the parameter is `paths_in` because `scout.py` already imports the `scouter.paths` module), **not** a verb (nothing calls it that the two verbs above do not, and a verb would need a result shape for 300 frames). It:

- **caches on disk**: `~/.cache/scouter/stats/<sha1(abs path + xmp mtime_ns + px + scouter version)>.json`. The XMP mtime is in the key, so re-running after `style.apply_set` recomputes exactly the frames whose history changed and reuses the rest — which is what makes "stats before the style, stats after the style" affordable. Entries older than `stats.cache_max_age_days` are pruned by the same LRU code phase 1 uses for previews (`_prune_preview_cache`, generalised to take a directory and a budget).
- **parallelises** with `concurrent.futures.ThreadPoolExecutor(max_workers=workers)`. Each worker calls the existing `scout.render`, which already builds its **own** isolated configdir per call (`_render_configdir()` uses a per-call token) and deletes it — so K concurrent `darktable-cli` processes cannot contend for a library lock. `workers` defaults to `cfg.stats.workers` (4) and is clamped to `os.cpu_count() or 2`.
- **mirrors** the result into `<job>/.scouter/stats-<px>.json` when a job is known, so a moved or re-imported library keeps its numbers.
- **never fails the set for one frame**: a frame that raises returns `{path, error: {code, message}}` and the caller reports `failed`.

### 8.2 `cull.propose`

```
scouter cull propose --job-slug JOBSLUG [--px 512] [--workers 4] [--pretty]
```

Inputs per frame: `sharpness`, `clipped_high_pct`, `luminance_mean` from `stats_many`; `captured_at`, `subsec`, `exposure_time`, `iso` from EXIF (already in `import.json`, so no re-read); `rating_now` from the XMP's `xmp:Rating`.

**Burst grouping.** Sort by `capture_key`. A frame joins the previous burst when the gap to it is ≤ `cull.burst_gap_s` (2.0) **and** the camera matches. Bursts of one are singletons. Frames with no capture time form a single `no-time` group and are never proposed for a rating change (their reason says why).

**Proposal rules** (deterministic, no model — spec 6.2):

| Case | Proposed | Reason text carries |
|---|---|---|
| best `sharpness` in a burst of ≥ 2 | `cull.pick_rating` (2) | `"sharpest of 5 in burst b3 (0.52 vs 0.31 next)"` |
| other frames in that burst | `cull.keep_rating` (1) | `"softer than the pick of burst b3 (0.31 vs 0.52)"` |
| singleton, `sharpness ≥ cull.sharpness_floor`, no exposure flag | `cull.pick_rating` (2) | `"singleton, sharpness 0.44 ≥ floor 0.20"` |
| singleton, flagged | `cull.keep_rating` (1) | the flags with their numbers |
| `luminance_mean` outside `[25, 230]`, or `clipped_high_pct > 5.0` | never raises the rating above 1 | `"clipped 7.4 % > 5.0 %"` / `"mean luminance 18 < 25"` |
| `rating_now > proposed` | proposed = `rating_now` | `"keeping your existing rating 3"` |

**No frame is ever proposed for rating 0 or for reject.** Spec 4.8 is stats-only and spec 13.4 declined the blink model; a stats-only culler that auto-rejects would throw away frames for reasons it cannot see (eyes, expression, a deliberate motion blur). Flagged frames get rating 1 with their numbers and Pratik decides. Ties in `sharpness` (inevitable on a synthetic set of copies) break by `seq`, and the result says `tie_broken_by: "seq"` for that burst.

Result: `CullResult{proposal_path, groups: [{burst_id, frames, span_s}], proposals: [{image_id, path, seq, burst_id, rating_now, rating_proposed, sharpness, clipped_high_pct, luminance_mean, reasons: [...]}], counts: {pick, keep, unchanged, no_time}, stats: {cached, computed, failed, ms}}`.

The proposal is written to `<job>/.scouter/cull-<ts>.json` and `proposal_path` points at it, so **`cull.apply` consumes exactly the table that was shown** — no recomputation, no drift between the proposal Pratik read and the ratings that land. `--pretty` prints the same rows as an aligned table.

### 8.3 `cull.apply`

```
scouter cull apply --job-slug JOBSLUG [--proposal PATH] [--dry-run] --yes
```

Always gated (many images). Loads the newest `cull-*.json` for the job unless `--proposal` names one; refuses (`usage`) if any frame's XMP mtime has advanced since the proposal was written — the proposal is stale and Pratik should see fresh numbers.

Ratings are written through the new `image.rate` bridge verb (section 12), grouped by rating value, in batches of 50 ids, so a 300-frame set is ~6 calls. Then, **verification**: for each changed frame, poll its XMP for up to `cfg`-free 2.0 s (50 ms interval) for `xmp:Rating` to equal the proposal. darktable owns when it flushes a rating to the sidecar, so the result reports `verified` and `unverified` separately and the warning for the unverified set says the rating is in darktable's database and the sidecar will catch up — it never claims a write it cannot see. `xmp:Rating` is real and present in Pratik's sidecars (verified 2026-09-14: `xmp:Rating="1"` in `P6281348.ORF.xmp`).

Result: `CullApplyResult{applied, unchanged, missed: [...], verified, unverified, per_image: [{image_id, rating_before, rating_after, verified}]}`. Appends its stage section to the vault note.

---

## 9. Style engine (`Fusion`)

### 9.1 `style.extract` with a **real** module filter

Phase 1's `style.extract` calls `dt.styles.create`, which takes the whole history; `modules` was recorded and `modules_honoured` was `false`. Phase 2 makes the filter real by building the `.dtstyle` ourselves:

1. Read the image's XMP (existing `core.xmp.parse`) and take the **active** entries (`num < history_end`).
2. Keep entries whose `operation` is in `modules`, minus `cfg.style.never_include` (crop, retouch, spots, flip, …: per-image geometry and repairs must never ride a style — spec 4.4). A requested module that is in `never_include` is a `usage` refusal naming it, not a silent drop.
3. A requested module **absent from the history** is reported in `modules_missing` and the extract continues; all requested modules missing is a `usage` refusal (an empty style is not a style).
4. Write `<cache>/styles/<name>.dtstyle` and import it through the new bridge verb `style.import_file` (section 12), which wraps `dt.styles.import`.

`params` and `blendop_params` are copied **verbatim from the XMP attribute strings** — the same hex or `gz<level><base64>` text, never decoded and re-encoded. Phase 1's decoder exists for reading; a round-trip through it would risk changing a single float. `modversion` and `blendop_version` likewise come straight from the entry.

### 9.2 The `.dtstyle` schema is **pinned by a probe, not assumed**

The expected shape (from darktable's `src/common/styles.c`, **unverified here**) is:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<darktable_style version="1.0">
  <info>
    <name>hero-harbour</name>
    <description>phase 2 hero</description>
    <iop_order_version>4</iop_order_version>
    <iop_order_list></iop_order_list>
  </info>
  <style>
    <plugin>
      <num>0</num><module>7</module><operation>exposure</operation>
      <op_params>0000…</op_params><enabled>1</enabled>
      <blendop_params>gz11eJxj…</blendop_params><blendop_version>14</blendop_version>
      <multi_priority>0</multi_priority><multi_name></multi_name>
      <iop_order>2.0</iop_order>
    </plugin>
  </style>
</darktable_style>
```

Because that is an unverified claim about another project's file format, **task 7 pins it before the writer is trusted**: a host probe creates a style with the existing `style.create_from_current`, exports it with the new `style.export_file` verb (`dt.styles.export`), and commits the real file as `tests/fixtures/hero.dtstyle`. The writer is then asserted **against the fixture's element names and nesting**, not against this section. If the real file names elements differently, the fixture wins and this section is corrected in task 7's commit (the phase 1 precedent: the `colorbalancergb` slot map was pinned the same way).

Two fields the XMP does not carry:

- **`<iop_order>`** (a float per plugin). Not in the sidecar. If the pinned fixture requires it, the writer omits the element and the **host** test decides: if darktable imports the style and applying it produces the expected history, the omission is fine and that is recorded in `docs/measurements.md`. If it does not, `style.extract` falls back to `dt.styles.create` (whole history) with `modules_honoured: false` and a warning naming the limitation — phase 1's behaviour, honestly labelled, rather than a broken style.
- **`<iop_order_list>`** — `darktable:iop_order_list` is **absent** from Pratik's real sidecar (verified 2026-09-14; it carries `darktable:iop_order_version="4"` instead, i.e. a built-in order). The writer copies the list when the XMP has one and emits an empty element when it does not.

`style.extract` result gains `dtstyle_path`, `operations_included: [...]`, `modules_missing: [...]`, and `modules_honoured` now genuinely reports `true`.

### 9.3 `style.apply_set`

```
scouter style apply-set --name NAME [--job-slug JOBSLUG | IMAGE...] [--exclude IMAGE,...]
                        [--mode append] [--batch 25] [--dry-run] --yes
```

- Images come from `--job-slug` (every raw in `raw/`, minus `--exclude`, minus the hero when `--exclude-hero` is passed) or from an explicit list. Always gated (many images).
- The dry run returns the full plan with a count and the resolved style name, and verifies the style **exists** (`list_styles`) so a typo costs no round trips.
- Applies in batches of `batch` (25) ids per `style.apply` bridge call — the existing Lua verb already takes `image_ids` as a list and reports `applied`, `missed` and per-image `errors`; only the batching and the 300 s client timeout are new.
- `mode="overwrite"` is still refused with `unsupported_mode`, and the message now names the one place overwrite exists: `export.release --style-overwrite`.

**Partial results must not lose the successes.** Phase 1's single-image `apply` raises `StylePartial`, which discards the payload. For a set that is wrong: 280 of 300 applied is a result, not an exception. So `style.apply_set` returns `outcome="partial"` when `failed > 0` and `applied > 0`, with the full `per_image` list in the payload; `registry.call` already maps any outcome that is not `ok`/`dry_run` to `ok=False`, and `EXIT_CODES` gains `"partial": 9` (the same code `style_partial` uses, because a shell script wants one branch for "some of it worked"). `applied == 0` still raises `StylePartial`. Phase 1's single-image `style.apply` is **unchanged**.

### 9.4 `outliers.list`

```
scouter outliers list --reference IMAGE [--job-slug JOBSLUG | IMAGE...] [--px 512] [--pretty]
```

Compares each frame's post-style stats to the hero's, with `cfg.tolerances` (spec 4.4: ±0.3 EV, ±300 K, > 1 % clipped).

| Measure | Computation | Honesty label |
|---|---|---|
| `ev_proxy_delta` | `log2(frame.luminance_mean / reference.luminance_mean)`, both `+0.5` guarded against zero | **`ev_proxy`**, never `ev`: it is a rendered-luminance ratio on a 1024/512 px preview, not a metered exposure difference. Scene content moves it. Named and documented so no caller mistakes it for an EV reading. |
| `temperature_k_delta` | `wb.kelvin(frame) - wb.kelvin(reference)`, from the coefficient vectors in the two XMPs through the camera's calibration table (section 11) | `null` with `reason: "no wb calibration table for em10iii; run scouter wb calibrate"` when the table is absent. Never estimated from pixel ratios and called Kelvin. |
| `wb_ratio_rg_delta`, `wb_ratio_bg_delta` | difference of `wb_ratio_rg` / `wb_ratio_bg` | the always-available WB signal; its own tolerance `tolerances.wb_ratio` (0.03) is used **in place of** the Kelvin test when the table is missing, and the result says which test ran (`wb_test: "kelvin" \| "ratio"`). |
| `clipped_high_pct` | absolute, from the frame's own stats | `> tolerances.clipped_high_pct` (1.0) |

A frame is an outlier when **any** enabled test fails, and `reasons` carries one sentence per failed test with both numbers and the tolerance. Result: `OutliersResult{reference: {...}, tolerances: {...}, wb_test, checked, within_tolerance, outliers: [...], failed: [...]}`. Sorted worst-first by the largest normalised breach, so the live pass has an order.

---

## 10. Delivery (spec 4.7)

### 10.1 Export presets

`core/export.py::PRESETS` (phase 1's two names keep their meaning; two are added):

| preset | ext | longest edge | quality / depth | ICC | darktable-cli flags |
|---|---|---|---|---|---|
| `preview` (phase 1) | png | 1024 | — | — | `--out-ext png --width 1024 --height 1024` |
| `client-proof` **(new)** | jpg | 2048 | q90 | sRGB | `--width 2048 --height 2048 --icc-type SRGB --core --conf plugins/imageio/format/jpeg/quality=90` |
| `client-final` (phase 1) | jpg | 0 (full) | q95 | sRGB | `--icc-type SRGB --core --conf plugins/imageio/format/jpeg/quality=95` |
| `print-tiff` **(new)** | tif | 0 (full) | 16-bit | AdobeRGB | `--icc-type ADOBERGB --core --conf plugins/imageio/format/tiff/bpp=16 --conf plugins/imageio/format/tiff/compress=5` |

Watermark is **not** implemented (spec 4.7 says "watermark optional"; it needs a darktable watermark module preset and an SVG, and Pratik has not asked for one). `PRESETS` carries a `note` per preset, which lands in `warnings`, so the result always says what was actually rendered.

`CLIWrapper.export_image` gains one **optional** keyword, `extra_args: list[str] | None = None`, appended after its own flags and before the paths. Additive: every existing call site and the golden schema test are unaffected. The flags above are built in `core/export.py::preset_args(spec)` so the preset table is the only place they are spelled.

### 10.2 `export.release`

```
scouter export release --job-slug JOBSLUG --preset client-proof [--min-rating 2]
                       [--images IMAGE,...] [--style NAME] [--overwrite-style]
                       [--dry-run] --yes
```

1. Selection: `--images` if given, else every raw in `raw/` whose `xmp:Rating ≥ --min-rating` (default `cfg.export.min_rating`, 2). An empty selection is a `usage` refusal naming the rating threshold and the histogram it found — never a zero-file "success".
2. Destination `<job>/exports/<preset>/`, output name `<input stem>.<ext>`. An existing output is a gate (phase 1's rule) and, with `--yes`, is replaced atomically by `scout.render`'s existing temp-then-`os.replace` path.
3. `--style NAME` adds `--style NAME`; `--overwrite-style` adds `--style-overwrite` — **the only place in Scouter where a style overwrites history, and it is render-only: nothing in the library changes** (spec 4.4).
4. Render each file; one failure does not lose the rest (phase 1's `files[].error` shape is kept).
5. Manifest `<job>/.scouter/manifest-<preset>-<ts>.json`, plus `manifest-<preset>-<ts>.sha256` over it, so the manifest itself is verifiable:

```json
{
  "version": 1, "job_slug": "harbour-wedding", "preset": "client-proof",
  "released_at": "2026-09-14T12:04:11Z", "scouter_version": "0.2.0",
  "darktable": "5.4.1", "style": "hero-harbour", "style_overwrite": false,
  "selection": {"rule": "rating>=2", "selected": 12, "of": 30},
  "files": [{"input": "raw/2026-06-28_harbour-wedding_em10iii_0001.orf",
             "input_sha256": "…", "output": "exports/client-proof/2026-06-28_harbour-wedding_em10iii_0001.jpg",
             "output_sha256": "…", "bytes": 1841233, "px": 2048, "render_ms": 2210}],
  "totals": {"exported": 12, "failed": 0, "bytes": 21114882}
}
```

`input_sha256` is **read from `import.json`**, not recomputed — it is the same bytes, the import already verified them, and re-hashing 6 GB to write a proof manifest is waste. The manifest says `input_sha256_source: "import.json"` so that is not a hidden shortcut.

6. Result carries `out_dir` (the folder to share) and `manifest_path`; the stage section is appended to the vault note.

`export.verify --manifest PATH` re-hashes every output against the manifest and returns the same shape as `library.verify`. (One extra verb, and it is what makes "delivered" checkable a week later.)

---

## 11. White balance: Kelvin ↔ coefficients

### 11.1 The fact, and what follows from it

Phase 1 pinned the params (design §7.2, verified against the real sidecar): `temperature` modversion 4 is `<4f i` = `coeff_r, coeff_g, coeff_b, coeff_g2, preset`, with `coeff_g2 = inf` on this camera. darktable's UI slider is **Kelvin**; the stored state is **coefficients**. darktable converts one to the other inside `src/iop/temperature.c` using (a) the camera's XYZ→camera matrix from its own camera-support data and (b) a blackbody/daylight illuminant model. **Neither input is in the XMP**, and Scouter may not read darktable's internal data files as an API.

Therefore: **a faithful closed-form Kelvin↔coefficient conversion from the XMP alone is not possible, and this design does not pretend otherwise.** The honest alternative is the one the brief names — journal the coefficients and undo by restoring them — implemented in three tiers, each of which is a real mechanism with its own measured cost.

### 11.2 Tier 1 (unconditional): journal the coefficient vector

`core.darkroom._before_state` currently records one named param via `xmp.latest_value`. Phase 2 adds `xmp.latest_params(parsed, operation, instance) -> dict | None` (the whole decoded dict plus `params_hex`), and every `temperature` write journals the **full vector**: `before: {source: "xmp", params: {coeff_r: 2.0125747, coeff_g: 1.0, coeff_b: 1.7052786, coeff_g2: null, preset: 2}, params_hex: "…", enabled: true, history_end: 13}`.

This is unconditional and cheap, and it is what makes every later tier possible. Even in the worst case the refusal now hands Pratik the exact numbers to type back in.

### 11.3 Tier 2 (probe-gated): undo through the coefficient sliders

darktable's `white balance` module has a coefficients section as well as the temp/tint sliders. If those sliders are addressable by `dt.gui.action`, restoring them **is** an exact undo. Task 11 probes, in the darkroom, on the real ORF, and pins whichever answers non-`nan`:

```
iop/temperature/red          iop/temperature/green          iop/temperature/blue
iop/temperature/channel coefficients/red   …/green   …/blue   …/emerald
iop/temperature/scene illuminant temp      iop/temperature/settings
```

If pinned: `darkroom.undo_last` for a `temperature` entry issues one absolute `set` per coefficient from the journal, commits once, and verifies the vector from the XMP — the same journal-based inverse-set mechanism phase 1 uses for every other module (design §8.2), just with three or four values instead of one. `ACTION_PATHS` gains the pinned entries. Phase 1's honest refusal for `temperature` is then **removed**, and the removal is justified by a pinned host probe, not by hope.

### 11.4 Tier 3 (built regardless): the per-camera calibration table

`outliers.list` needs Kelvin from coefficients whether or not tier 2 works, so the table is built either way — and it is also tier 2's fallback.

```
scouter wb calibrate IMAGE [--k-from 2000] [--k-to 12000] [--step 500] [--dry-run] --yes
```

For each K: `darkroom.set("temperature","temperature",K)` → commit → read the coefficient vector from the XMP. Writes `~/.config/scouter/wb/<camera-slug>.json`:

```json
{"version": 1, "camera": "em10iii", "camera_model": "E-M10 Mark III",
 "measured_at": "2026-09-14T…", "darktable": "5.4.1", "tint": 1.0,
 "source_image": "2026-06-28_harbour-wedding_em10iii_0001.orf",
 "samples": [{"k": 2000, "coeff_r": 1.201, "coeff_g": 1.0, "coeff_b": 3.884},
             {"k": 2500, "coeff_r": 1.377, "coeff_g": 1.0, "coeff_b": 3.112}]}
```

`wb.kelvin` inverts it on the ratio `r = coeff_r / coeff_b`, which is **monotone in K at fixed tint** (more blue gain at low K, more red gain at high K — asserted by the calibrate verb itself, which refuses to write a non-monotone table and says which pair broke it). Inversion is a bisection over the sorted samples plus linear interpolation between the two bracketing ones. Outside the sampled range: `null` with `reason: "coefficient ratio 0.21 is outside the calibrated range 2000-12000 K"` — never extrapolated.

```python
def kelvin(image=None, *, coeffs=None, camera=None, job=None) -> WbKelvinResult
# -> {kelvin: 5217, interpolated: true, bracket: [5000, 5500], residual_ratio: 0.004,
#     camera: "em10iii", table: "~/.config/scouter/wb/em10iii.json", exact: false}
```

Cost, and how it is paid: ~21 sets × ~1.8 s ≈ 40 s per camera, once, and it adds ~21 history items to whichever frame it runs on. So `wb.calibrate` **refuses to run on a frame inside a job's `raw/`** and requires a scratch copy (`--allow-in-job` overrides, and says what it will do to the history). The plan calibrates one copy of `P6281348.ORF` in a scratch directory and commits the resulting table as `tests/fixtures/wb-em10iii.json`, so the unit tests for `wb.kelvin` and `outliers.list` run against real numbers with no darktable. See open question 5.

### 11.5 Tier 4: the refusal, improved

If tier 2's probe finds nothing and no table exists, `darkroom.undo_last` on a `temperature` entry still refuses — with `unsupported_undo`, and now with the journalled coefficient vector, the target Kelvin, and the sentence "darktable stores white balance as camera-relative coefficients; run `scouter wb calibrate` to make this undoable, or restore these coefficients in the module's coefficients tab". A refusal that hands over the numbers is a different thing from a refusal that shrugs.

---

## 12. Bridge protocol additions

Transport, error shape and the `nan`/`view` rules are unchanged (phase 1 design §5).

### 12.1 New methods

| Method | Params | Result |
|---|---|---|
| `style.export_file` | `{"name": "hero-harbour", "dir": "/abs/dir"}` | `{"name": "...", "path": "/abs/dir/hero-harbour.dtstyle", "bytes": 4211, "exported": true}` — wraps `dt.styles.export(style, dir)`; used by the schema-pinning probe and by `style.extract --export` |
| `style.import_file` | `{"path": "/abs/x.dtstyle", "name": "hero-harbour", "overwrite": false}` | `{"name": "...", "imported": true, "replaced": false, "styles_before": 3, "styles_after": 4}` — wraps `dt.styles.import(path)`; when a style of that name exists and `overwrite` is false it errors before importing; with `overwrite` it deletes the old one (`dt.styles.delete`) first and says so in `replaced` |
| `image.rate` | `{"image_ids": [12, 13], "rating": 2}` | `{"rating": 2, "rated": 2, "missed": [], "per_image": [{"image_id": 12, "rating_before": 0, "rating_after": 2}]}` |

`image.rate` exists rather than reusing w1ne's `rate_photos {photo_ids, rating} -> {updated}` because `rate_photos` returns only a count and **silently skips ids darktable does not know** — for a 300-frame cull that is exactly the missing-frame case that must be visible. `rate_photos` is left untouched for its existing MCP tool.

### 12.2 Modified method: `history.commit`

```
params: {"strategy": "auto"|"view_bounce"|"sidecar_action",
         "attempts": 1..3        (default: COMMIT_BOUNCE_ATTEMPTS, 2),
         "expect_change": bool   (default: true)}
result: … existing fields …, "attempts_used": 1, "expect_change": false
```

`COMMIT_BOUNCE_ATTEMPTS` becomes the **default** for `attempts`, not a constant. `expect_change: false` clamps `attempts` to 1 and makes `ok` true when the bounce completed even though the sidecar did not change — `sidecar_written: false` still reports the fact. This is what turns phase 1's 5.5 s no-op set into one bounce (section 13).

### 12.3 Client timeouts

Added to `DEFAULT_TIMEOUTS` in `darktable_mcp/bridge/client.py`:

```python
"style.export_file": 30.0,
"style.import_file": 60.0,
"image.rate": 60.0,     # 50 ids per call, each a library write
```

and one **raise**: `"style.apply": 60.0 → 300.0`, because `style.apply_set` now hands it 25 ids per call and each is a history write inside the worker loop. The cross-language invariant holds: the Lua plugin's `STALE_AGE_SECONDS` is 300, equal to the largest budget, so `tests/test_lua_dispatcher.py`'s assertion that it stays **≥** the largest budget still passes; if that test asserts strict `>`, `STALE_AGE_SECONDS` rises to 600 in the same task (task 8 checks and records which).

---

## 13. The repeat-set cost

Phase 1's measured worst case: a `darkroom.set` asking for the value the XMP already carries costs **5.5-5.7 s**, because `history.commit` bounces, sees no sidecar change (correctly — darktable writes a sidecar only when history changes), retries a second time, waits again, and only then does the Python readback resolve it. A frame Pratik has already set is a common case in a volume job, and it is the single worst live cost in the system.

Three changes, cheapest first:

1. **Pre-check (zero bounces).** Before issuing the action, `darkroom.set` already reads the current value from the XMP for its `previous` field. If the requested value is within `_close()` of it, the module is enabled, **and the sidecar is known-fresh** (2 below), the verb returns immediately: `outcome="ok"`, `noop: true`, `applied == previous`, `committed: true`, `warnings: ["no-op: exposure is already 1.55 in the sidecar; nothing was sent to darktable"]`. **Zero** bridge writes, zero bounces. Cost: the one `image.lookup` + one XMP read the verb already pays. Target < 0.2 s.
2. **The sidecar watermark (what makes 1 safe).** The XMP can be *stale*: an uncommitted darkroom change means the file on disk is older than darktable's in-memory history, and skipping on a stale read would silently drop a real edit. So `core.history` records, in the job-state file phase 1 already owns (`paths.job_state_path`), a watermark per image after every successful commit: `{"image_id": 12, "xmp": {"mtime_ns": …, "size": …, "history_end": 29}}`. The sidecar is *known-fresh* only when its current signature equals the watermark. A mismatch (or no watermark, e.g. a fresh session or an edit Pratik made by hand) means the pre-check is **not** trusted and path 3 runs.
3. **One bounce instead of two, when a no-op is expected.** When the pre-check says "this looks like a no-op but the watermark is stale", the action is issued and the commit is called with `expect_change=False, attempts=1` (section 12.2): one bounce, one sidecar wait, then the readback. Target ≈ 1.0 s. A set that really moves is unchanged (~1.8 s, `expect_change=True`).

New read verb, because the agent and the shell both want the same signal:

```
scouter history changed IMAGE      ->  {"image_id": 12, "fresh": true, "watermark": {...},
                                        "sidecar": {...}, "reason": null}
```

`history.changed` is a pure read (`image.lookup` + one `stat`), moves nothing, and is what a script calls before deciding to spend a bounce.

`SetResult` gains `noop: bool = False`, and `docs/measurements.md` gains a "repeat-set cost" section with the before (5.5 s) and after numbers from task 12's host run. **E8 asserts < 1.5 s** and that the fresh-watermark path makes zero bridge write calls.

---

## 14. Backup (spec 4.6: nightly encrypted archive + rsync of raws, its own launchd job)

### 14.1 What is archived, and with what

Two halves, because the data has two shapes:

- **Small and precious** (the archive): every `.xmp` under `library_root`, every job's `.scouter/`, `~/.config/scouter/` (config + the WB tables), `~/.cache/scouter/metrics.db` and `events.jsonl`, and `<library>/scouter/memory/` (empty in phase 2, reserved for `Dragon Radar`). Total for a 300-frame job: a few MB. **darktable's `library.db` is included when `backup.include_darktable_db` is true (default true) — copied, never opened**, with `sqlite3`'s own `.backup` not used (that would be reading darktable's database; a file copy of a database darktable may be writing is explicitly labelled `consistency: "file-copy, may be mid-write"` in the manifest, and the XMPs are the authoritative record either way).
- **Large and reproducible-but-not-really** (the rsync): the raws. `rsync -a --partial --human-readable --stats --exclude '.scouter/' --exclude 'exports/' <library_root>/ <target>/library/`. Raws are `0444`, which `-a` preserves. Exports are excluded: they are regenerable from raw + XMP, and they are the biggest thing on disk.

**Archive tool: stdlib `tarfile`, not the `tar` binary.** We already have the exact file list, and `tarfile` lets the same pass compute the plaintext sha256 while streaming — one read instead of two, and no shell quoting of paths with spaces (the job directory names contain spaces by design).

**Encryption: `age` when available, `openssl enc` as the shipped default.** Decided and justified:

- `age -r <recipient>` is the better tool: authenticated (ChaCha20-Poly1305), and a **public** recipient key means the nightly launchd job holds no secret at all. But `age` is **not installed on this Mac** (verified 2026-09-14: `which age` → not found), so making it the hard requirement would make the import gate unsatisfiable today.
- So the shipped default is `openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha512 -salt -pass file:<passfile>` (verified present: OpenSSL 3.6.3 at `/opt/homebrew/bin/openssl`). `openssl enc` provides **confidentiality but not authentication** — it cannot detect tampering or bit rot. That gap is closed explicitly: the manifest records the sha256 of the **plaintext** tar, and `backup.verify` decrypts and re-hashes, which is the integrity check `openssl enc` does not give us. The design says this out loud rather than implying AEAD.
- `encryption: "age"` is fully implemented and selected automatically by `config init` when `age` is on `PATH`; switching later is one config key. See open question 2.

### 14.2 `backup.run` and `backup.verify`

```
scouter backup run [--dry-run] [--yes]
scouter backup verify [--archive 2026-09-14T033000Z] [--sample 10]
```

`backup.run`, in order: gate (section 4.3) → enumerate → `tarfile` with a **sorted, deterministic** member list, `mtime` preserved, hashing as it streams → encrypt to `<target>/archives/<ts>/scouter-<ts>.tar.gz.<enc|age>` → rsync (unless `rsync_raws` is false) → write `<target>/archives/<ts>/backup.json` → prune archive directories beyond `keep_archives` (oldest first, and **never** the newest two, whatever `keep_archives` says). Gated (`--yes`): it writes outside the library and deletes old archives.

```json
{"version": 1, "created": "2026-09-14T03:30:02Z", "scouter_version": "0.2.0",
 "library_root": "…", "encryption": "openssl", "openssl_args": "enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha512 -salt",
 "archive": {"name": "scouter-2026-09-14T033000Z.tar.gz.enc",
             "plaintext_sha256": "…", "plaintext_bytes": 4211990,
             "ciphertext_sha256": "…", "ciphertext_bytes": 4212032,
             "members": 341, "authenticated": false,
             "note": "openssl enc gives confidentiality, not authentication; integrity is the plaintext sha256 above"},
 "members": [{"name": "library/2026/2026-06-28 harbour-wedding/raw/….orf.xmp", "bytes": 15470, "sha256": "…"}],
 "rsync": {"ran": true, "target": "…/library", "files_transferred": 30, "bytes": 432520470, "exit": 0},
 "darktable_db": {"included": true, "bytes": 8421376, "consistency": "file-copy, may be mid-write"}}
```

`backup.verify`: decrypt the newest (or `--archive`) to a temp directory → compare the ciphertext sha256 → compare the plaintext sha256 → open with `tarfile` and compare the member count → re-hash `--sample` randomly chosen members against the manifest → sample the rsync side by checking `--sample` raw checksums from the jobs' `import.json` against the copies under `<target>/library/`. Returns `{verified, archive, checks: [{name, ok, detail}], sampled_members, sampled_raws}`. `verified` is false if **any** check fails, and the failing check is named. Read-only: it writes nothing outside its temp directory.

### 14.3 launchd

`ops/launchd/com.pratik.scouter-backup.plist.template` with `@PYTHON@`, `@REPO@`, `@VAULT@` and `@LOG@` placeholders, `StartCalendarInterval` at 03:30 daily, `RunAtLoad: false`, `StandardOutPath`/`StandardErrorPath` to `~/Library/Logs/scouter-backup.log`, `EnvironmentVariables` carrying `SCOUTER_JOB=backup-nightly` (so the whole night's events land in one audit file) and `OBSIDIAN_VAULT_PATH`. `ops/install-backup-job.sh` renders the template, validates it with `plutil -lint`, and `launchctl bootstrap gui/$UID`s it; `--uninstall` reverses it. **No test installs it**; task 13's test renders the template and lints it only. The command it runs is `scouter backup run --yes --json`, whose exit code launchd records — the same shape as the existing Capsule launchd jobs (row 226).

---

## 15. Core signatures

All in `scouter/core/` unless noted. Typed, no model calls, frozen-dataclass results from `scouter/types.py`, `job: str | None = None` defaulting to the ambient job id.

```python
# scouter/config.py
def load(path: str | None = None) -> Config: ...
def require_backup(cfg: Config) -> BackupConfig: ...                 # raises BackupNotConfigured
def job_dir(cfg: Config, date: str, slug: str) -> Path: ...
def find_job(cfg: Config, slug: str, date: str | None = None) -> Path: ...
# the slug validator lives in `scouter/paths.py` beside `validate_job`, because
# both turn caller input into a file name and must be validated at one door:
# paths.validate_slug(slug) -> str
def init(path: str | None = None, *, confirm: bool = False,
         dry_run: bool = False, job: str | None = None) -> ConfigResult: ...   # verb config.init
def show(*, job: str | None = None) -> ConfigResult: ...                       # verb config.show

# scouter/core/exif.py
def read_tags(path: str | Path, *, limit: int = 1 << 20) -> ExifTags: ...
def camera_slug(tags: ExifTags, cfg: Config) -> str: ...
def capture_key(tags: ExifTags, path: str | Path) -> tuple[str, int, str]: ...

# scouter/core/library.py
def import_card(source: str, job_slug: str, *, date: str | None = None,
                camera: str | None = None, extensions: list[str] | None = None,
                confirm: bool = False, dry_run: bool = False,
                job: str | None = None) -> ImportResult: ...          # verb library.import
def verify(job_slug: str, *, date: str | None = None,
           job: str | None = None) -> VerifyResult: ...               # verb library.verify
def register(job_slug: str, *, date: str | None = None, confirm: bool = False,
             dry_run: bool = False, job: str | None = None) -> RegisterResult: ...
def jobs(*, job: str | None = None) -> JobsResult: ...                # verb library.jobs
def relocate(new_root: str, *, confirm: bool = False, dry_run: bool = False,
             job: str | None = None) -> RelocateResult: ...           # verb library.relocate

# scouter/core/vault.py                       (not verbs; called by the verbs above)
def write_job_note(cfg: Config, manifest: dict) -> dict: ...
def append_stage(cfg: Config, job_slug: str, stage: str, rows: list[tuple[str, str]],
                 checklist_key: str | None = None) -> dict: ...

# scouter/core/cull.py
def propose(*, job_slug: str | None = None, images: list[str | int] | None = None,
            px: int | None = None, workers: int | None = None,
            job: str | None = None) -> CullResult: ...                # verb cull.propose
def apply(*, job_slug: str | None = None, proposal: str | None = None,
          confirm: bool = False, dry_run: bool = False,
          job: str | None = None) -> CullApplyResult: ...             # verb cull.apply

# scouter/core/dtstyle.py                     (not a verb; the .dtstyle reader/writer)
def build(parsed: xmp.ParsedXmp, name: str, description: str, operations: list[str]) -> str: ...
def write(path: Path, text: str) -> Path: ...
def read_operations(path: Path) -> list[str]: ...

# scouter/core/style.py                       (additions; phase 1's extract/apply keep their signatures)
def apply_set(name: str, *, job_slug: str | None = None, images: list[str | int] | None = None,
              exclude: list[str | int] | None = None, mode: str = "append", batch: int = 25,
              confirm: bool = False, dry_run: bool = False,
              job: str | None = None) -> StyleSetResult: ...          # verb style.apply_set
def outliers(reference: str | int, *, job_slug: str | None = None,
             images: list[str | int] | None = None, px: int | None = None,
             workers: int | None = None, job: str | None = None) -> OutliersResult: ...  # verb outliers.list

# scouter/core/scout.py                       (addition; not a verb)
def stats_many(paths_in: list[str], *, px: int, workers: int,
               job_dir: Path | None = None) -> list[dict]: ...

# scouter/core/wb.py
def calibrate(image: str | int, *, k_from: int = 2000, k_to: int = 12000, step: int = 500,
              allow_in_job: bool = False, confirm: bool = False, dry_run: bool = False,
              job: str | None = None) -> WbCalibrationResult: ...     # verb wb.calibrate
def kelvin(image: str | int | None = None, *, coeffs: list[float] | None = None,
           camera: str | None = None, job: str | None = None) -> WbKelvinResult: ...  # verb wb.kelvin

# scouter/core/export.py                      (additions)
def release(job_slug: str, preset: str, *, min_rating: int | None = None,
            images: list[str | int] | None = None, style: str | None = None,
            overwrite_style: bool = False, confirm: bool = False, dry_run: bool = False,
            job: str | None = None) -> ReleaseResult: ...             # verb export.release
def verify_manifest(manifest: str, *, job: str | None = None) -> VerifyResult: ...   # verb export.verify

# scouter/core/history.py                     (additions / change)
def commit(image: str | int | None = None, *, timeout_s: float = 5.0, strategy: str = "auto",
           attempts: int = 2, expect_change: bool = True,
           job: str | None = None) -> CommitResult: ...               # CHANGED: two new kwargs
def changed(image: str | int, *, job: str | None = None) -> ChangedResult: ...        # verb history.changed
def watermark_write(job: str, image_id: int, xmp_path: str) -> dict: ...              # not a verb
def watermark_fresh(job: str, image_id: int, xmp_path: str) -> tuple[bool, str | None]: ...

# scouter/core/backup.py
def run(*, confirm: bool = False, dry_run: bool = False,
        job: str | None = None) -> BackupResult: ...                  # verb backup.run
def verify(*, archive: str | None = None, sample: int = 10,
           job: str | None = None) -> BackupVerifyResult: ...         # verb backup.verify
```

New result dataclasses in `scouter/types.py`: `ConfigResult`, `ImportResult`, `VerifyResult`, `RegisterResult`, `JobsResult`, `RelocateResult`, `CullResult`, `CullApplyResult`, `StyleSetResult`, `OutliersResult`, `WbCalibrationResult`, `WbKelvinResult`, `ReleaseResult`, `ChangedResult`, `BackupResult`, `BackupVerifyResult`. All subclass `Payload`. `SetResult` gains `noop: bool = False`; `StyleResult` gains `dtstyle_path: str | None`, `operations_included: list[str]`, `modules_missing: list[str]`.

---

## 16. The three layers

### 16.1 Verb table (phase 2 additions; all 17 phase-1 verbs keep their rows)

| Verb id | CLI | MCP tool | Writes | Gated |
|---|---|---|---|---|
| `config.init` | `scouter config init [--path P] --yes` | `config_init` | yes | **yes** |
| `config.show` | `scouter config show` | `config_show` | no | no |
| `library.import` | `scouter library import SOURCE --job-slug S [--date D] [--camera C] [--extensions a,b] [--dry-run] --yes` | `library_import` | yes | **yes** (always > 1 file) |
| `library.verify` | `scouter library verify JOBSLUG [--date D]` | `library_verify` | no | no |
| `library.register` | `scouter library register JOBSLUG [--date D] --yes` | `library_register` | yes | **yes** |
| `library.jobs` | `scouter library jobs` | `library_jobs` | no | no |
| `library.relocate` | `scouter library relocate NEWROOT [--dry-run] --yes` | `library_relocate` | yes | **yes** |
| `cull.propose` | `scouter cull propose --job-slug S [--px N] [--workers N]` | `cull_propose` | no | no |
| `cull.apply` | `scouter cull apply --job-slug S [--proposal P] [--dry-run] --yes` | `cull_apply` | yes | **yes** |
| `style.apply_set` | `scouter style apply-set --name N [--job-slug S \| IMAGE…] [--exclude a,b] [--mode append] [--batch N] [--dry-run] --yes` | `style_apply_set` | yes | **yes** |
| `outliers.list` | `scouter outliers list --reference IMAGE [--job-slug S \| IMAGE…] [--px N]` | `outliers_list` | no | no |
| `wb.calibrate` | `scouter wb calibrate IMAGE [--k-from N] [--k-to N] [--step N] [--allow-in-job] [--dry-run] --yes` | `wb_calibrate` | yes | **yes** |
| `wb.kelvin` | `scouter wb kelvin [IMAGE] [--coeffs r,g,b] [--camera C]` | `wb_kelvin` | no | no |
| `export.release` | `scouter export release --job-slug S --preset P [--min-rating N] [--images a,b] [--style N] [--overwrite-style] [--dry-run] --yes` | `export_release` | yes | **yes** |
| `export.verify` | `scouter export verify --manifest PATH` | `export_verify` | no | no |
| `history.changed` | `scouter history changed IMAGE` | `history_changed` | no | no |
| `backup.run` | `scouter backup run [--dry-run] --yes` | `backup_run` | yes | **yes** |
| `backup.verify` | `scouter backup verify [--archive TS] [--sample N]` | `backup_verify` | no | no |

18 new verbs; 35 total. Gate rule is spec 6's, unchanged: more than one image, history overwrite, delete/reject, export over existing files — plus, new in phase 2, **anything that writes outside the library** (`config.init`, `library.relocate`, `backup.run`).

### 16.2 CLI

`scouter <group> <verb>` with the existing contract: one JSON object on stdout, logs to stderr, exit codes from `errors.EXIT_CODES`, `--dry-run` on every writer, `--yes` on every gated verb, `--job` and `--pretty` global. Three new groups appear automatically from the registry: `config`, `library`, `cull`, `outliers`, `wb`, `backup` (six). `--pretty` additionally prints an aligned table instead of indented JSON for the three verbs whose payload is a table Pratik reads — `cull.propose`, `outliers.list`, `library.jobs` — via a small `cli/tables.py` renderer keyed by verb id; JSON stays the default and the only machine contract.

`scouter doctor` gains four lines: the resolved config path and whether it parses, `backup_gate: "ok" | "<code>: <message>"`, the encryption tool it would use (`age` / `openssl` / none, with the resolved binary), and `library_root` with its free space.

### 16.3 MCP

Same adapter, same golden-snapshot test. Every new tool follows phase 1's schema conventions: `additionalProperties: false`, explicit `required`, `image` as `{"type": ["string","integer"]}`, writers carry `dry_run`, gated tools carry `confirm` whose description names what it authorises. New shared schema fragments in `scouter/mcp/schemas.py`: `JOB_SLUG` (`{"type":"string","pattern":"^[a-z0-9][a-z0-9-]{0,47}$"}`), `PRESET` (enum over `PRESETS`), `RATING` (`{"type":"integer","minimum":0,"maximum":5}`), `IMAGES` (`{"type":"array","items":IMAGE,"minItems":1}`).

The golden file `tests/fixtures/scouter_mcp_schemas.json` is regenerated **once**, in task 14, with the diff reviewed in that commit — never silently by a test run.

---

## 17. Error classes and exit codes

`scouter/errors.py` gains six classes. Each is in `ERROR_CLASSES` and `EXIT_CODES`, and each gets a test in `tests/test_error_paths.py` driven by fakes (no darktable).

| Class | `code` | Exit | Raised when | `fix` |
|---|---|---|---|---|
| `BackupNotConfigured` | `backup_not_configured` | 11 | the gate in 4.3 fails | "configure a backup target: `scouter config init`, then set `backup.target` and `backup.enabled` in ~/.config/scouter/config.json (the failing check is in `detail.check`)" |
| `ChecksumMismatch` | `checksum_mismatch` | 12 | a copy, a manifest re-hash, or a backup verification disagrees | "the file on disk does not match its recorded sha256; see `detail.expected` / `detail.actual`. Re-copy from the source, or re-run `library verify` after checking the disk" |
| `DestinationCollision` | `destination_collision` | 13 | a planned destination exists with different content | "a different file already has that name; see `detail.path`. Import with a different `--job-slug`, or remove the file yourself" |
| `ExifUnreadable` | `exif_unreadable` | 14 | no TIFF header, unknown magic, or no Make/Model and no `--camera` | "pass `--camera <slug>` for this card, or add the model to `cameras` in the config; `detail.magic` shows what the file's header said" |
| `StyleWriteFailed` | `style_write_failed` | 15 | `style.import_file` returned `imported: false`, or the imported style does not contain the requested operations | "darktable refused the .dtstyle at `detail.path`; it is kept for inspection. Re-run with `--no-filter` to fall back to a whole-history style" |
| `EncryptionUnavailable` | `encryption_unavailable` | 16 | no `age`/`openssl`, or the passfile is missing or mode-wide | "install age (`brew install age`) and set `backup.age_recipient`, or create the openssl passfile at `detail.passfile` with mode 0600" |

Plus one row, no class: `EXIT_CODES["partial"] = 9`, for `style.apply_set`'s `outcome="partial"` — the same code as `style_partial`, so one shell branch covers "some of it worked" whether it arrived as an exception or as a payload.

`UsageError` (exit 2) continues to carry the caller's own mistakes: bad slug, unknown config key, empty selection, mixed-camera card, stale proposal, all-modules-missing style.

---

## 18. Observability

No schema change. `scouter/__init__.py`'s `PHASE` becomes `2`, so every phase-2 event is distinguishable in `metrics.db` (`GROUP BY phase`), and phase 1's rows keep saying `1`.

The composite verbs matter for spec 6.1's numbers, so each one puts its own counters in the **payload** (which the audit log captures in full) rather than inventing event fields: `library.import` → `{planned, copied, skipped_identical, bytes, ms_copy, ms_hash}`; `cull.propose` → `{frames, bursts, stats_cached, stats_computed, stats_failed}`; `style.apply_set` → `{applied, failed, batches}`; `export.release` → `{selected, exported, failed, bytes}`; `backup.run` → `{members, plaintext_bytes, rsync_files, ms_tar, ms_encrypt, ms_rsync}`.

`scouter metrics summary` needs no change (it groups by verb), and the new verbs appear in it automatically. Spec 6.1's acceptance-rate numbers for phase 2 come from two ratios the payloads now make computable: **cull acceptance** = proposals applied unchanged / proposals made, and **style-apply outlier rate** = `outliers.list` outliers / frames checked. Both are recorded in the vault job note's stage sections, so the first real 300-frame job produces them without any new instrumentation.

---

## 19. Testing

`unittest.TestCase` throughout, run by `uv run --extra dev python -m pytest tests -q`. **No new test requires darktable except the host file**, which is gated by `SCOUTER_HOST_TESTS=1` **and** the binary's presence (ruling 17).

| Layer | File | Doubles / fixtures |
|---|---|---|
| Config + backup gate | `tests/test_config.py` | temp `$SCOUTER_CONFIG`, temp target dirs, a chmod-0644 passfile for the mode check |
| EXIF reader | `tests/test_exif.py` | `tests/fixtures/exif-orf-header.bin` (the **first 64 KiB of the real ORF**, committed — it carries IFD0 and the Exif IFD), plus synthesised big-endian, magic-42 and truncated headers |
| Import plan + copy + checksums | `tests/test_library.py` | temp "card" of small synthetic files carrying real ORF headers; no darktable (`import_batch` via `FakeBridge`) |
| Vault note | `tests/test_vault.py` | temp `$OBSIDIAN_VAULT_PATH`; asserts append-not-overwrite and the unset-vault degradation |
| Set stats + cache | `tests/test_stats_many.py` | fake `scout.render` writing a canned PPM; asserts cache hit/miss on a bumped XMP mtime and that workers > 1 does not share a configdir |
| Cull | `tests/test_cull.py` | synthetic stats + EXIF tables; asserts burst boundaries at the 2.0 s gap, one pick per burst, no rating 0 ever, tie-break by seq, stale-proposal refusal |
| `.dtstyle` | `tests/test_style_dtstyle.py` | `tests/fixtures/hero.dtstyle` **pinned from the real darktable export** (task 7) + the real XMP; asserts the writer's output parses, contains exactly the requested operations, and carries `params` byte-identical to the XMP's |
| `style.apply_set` | `tests/test_style_set.py` | `FakeBridge` returning partial `missed`/`errors`; asserts batching, `outcome="partial"`, exit 9, and that successes survive |
| Outliers | `tests/test_outliers.py` | synthetic stats + `tests/fixtures/wb-em10iii.json`; asserts each tolerance boundary and the `wb_test: "ratio"` fallback with a null Kelvin |
| WB table | `tests/test_wb.py` | the pinned table; asserts monotonicity refusal, bracketing, out-of-range `null`, and interpolation against hand-computed values |
| Export presets + manifest | `tests/test_export_release.py` | fake `CLIWrapper`; asserts the flag list per preset, the empty-selection refusal, manifest shape and its `.sha256` |
| Repeat-set | `tests/test_history_changed.py` | `FakeBridge` **counting calls**; asserts zero write calls on the fresh-watermark no-op and `attempts=1` on the stale path |
| Backup | `tests/test_backup.py` | temp library + temp target, real `openssl` (skipped with a reason if absent); asserts the tar member list is deterministic, the plaintext hash round-trips, pruning never removes the newest two, and `verify` fails loudly on one flipped byte |
| launchd template | `tests/test_ops_launchd.py` | renders the template and runs `plutil -lint`; installs nothing |
| Lua dispatcher | `tests/lua/test_dispatcher.lua`, `tests/test_lua_dispatcher.py` | stub `dt.styles.import/export/delete`, `dt.database[...]`.rating; asserts the new methods are registered and the timeout/`STALE_AGE_SECONDS` relation still holds |
| Layer parity / MCP golden / metrics | existing three files | unchanged code, new verbs picked up from the registry |
| **Host, phase 2** | `tests/test_host_phase2.py` | the real darktable + the 30-frame synthetic job |

### 19.1 The 30-frame synthetic job

`tests/make_synthetic_job.py` (also runnable by hand: `python3 -m tests.make_synthetic_job --out ~/Pictures/ScouterSynthetic`) builds a fake card at `<out>/card/DCIM/100OLYMP/`:

- 30 files, by copying the four EditingTrial ORFs round-robin (`P6281348`, `P6281361`, `P6281368`, `P6281373`).
- **Distinct capture times, patched in place.** The EXIF `DateTimeOriginal` and `DateTime` strings are fixed-length ASCII (`"2026:06:28 15:49:28"`), so the builder locates their byte offsets with `core.exif`'s own IFD walk and overwrites them with equal-length strings: **6 bursts of 5 frames, 1 s apart inside a burst, 45 s between bursts**. Same length in, same length out — no re-encoding, no library, and the reader that finds the offsets is the reader under test.
- The pixels of copies are identical, so `sharpness` ties inside a burst. The test asserts **one pick per burst**, not which frame wins, and asserts the result reports `tie_broken_by: "seq"`. Stated as a limitation of the fixture, not hidden.
- The synthetic tree is created under `$TMPDIR` by default in CI-shaped runs and under `~/Pictures/ScouterSynthetic` for the host run; it is deleted in `tearDownClass` unless `SCOUTER_KEEP_SYNTHETIC=1`.

The host test runs the whole exit path in one darktable session, in order, with `SCOUTER_JOB=phase2-e2e`:

```
config init (temp config, temp backup target)          → E1 (refusal first, with the gate off)
library import card --job-slug synth-30 --yes          → E2
library verify synth-30
cull propose --job-slug synth-30 --px 512              → E3
cull apply --job-slug synth-30 --yes
darkroom open <hero> ; darkroom set exposure 0.6 ; darkroom set sigmoid contrast 1.3
darkroom set exposure 0.6   (repeat: the no-op path)   → E8
style extract <hero> --name synth-hero --modules exposure,sigmoid --yes   → E4
style apply-set --name synth-hero --job-slug synth-30 --exclude <hero> --yes  → E5
outliers list --reference <hero> --job-slug synth-30   → E6
export release --job-slug synth-30 --preset client-proof --min-rating 1 --yes → E7
export verify --manifest <path>
backup run --yes ; backup verify                       → E9
```

and, in `finally`: `history.commit` (ruling 16) then quit darktable with `osascript` → `SIGTERM` → `SIGKILL`, never leaving it running. Per-stage wall times are written to `docs/measurements-generated.md`; `docs/measurements.md` is updated **by hand** in task 14's commit, per the phase 1 convention.

---

## 20. Open questions for Pratik

Five, each with a recommendation. None blocks the plan: every one has a working default that the recommendation would only change.

1. **Backup target for phase 2: a local/mounted path now, or B2/R2 straight away?**
   The spec (13.3) chose cloud object storage "at phase 2", encrypted before upload, key via Potara. But the import gate only needs *a* configured target, and a cloud target adds a Potara dependency, credentials in a launchd job, and an upload budget — none of which exist today, and all of which would block the first import.
   **Recommendation: a local target now** (`/Volumes/<disk>/ScouterBackup`, or a second folder on the internal disk until the SSD arrives), with the archive already encrypted and upload-ready. Add a `backup.remote` block and an upload step in phase 3 alongside the Potara key. The gate is satisfied, nothing is unprotected, and no secret enters a nightly job this month.

2. **Install `age`, or ship `openssl enc`?**
   `age` is not on this Mac. `openssl enc` works and is the shipped default, but it is unauthenticated — integrity comes from the plaintext sha256 in the manifest, checked by `backup.verify`.
   **Recommendation: `brew install age` and set `backup.encryption: "age"`.** One command, and it buys authenticated encryption plus a *public* recipient key, so the nightly job holds no secret at all (the openssl path needs a passfile on disk). Both paths are implemented either way, so this is a config flip, not a rebuild.

3. **Should the cull ever propose rating 0 (a reject)?**
   The design says no: flagged frames get rating 1 with their numbers. Stats cannot see closed eyes, expression, or deliberate motion blur, and spec 13.4 declined the blink model.
   **Recommendation: keep "no auto-rejects".** If the first real job's cull is too timid, the cheap next step is a `cull.reject_below` config key (default off) rather than changing the rule, because it stays your decision and it stays visible in the config.

4. **What selects the frames for `export.release`?**
   Default is `rating ≥ 2` for every preset. Real delivery has two selections: everything worth proofing, then the client's picks for finals.
   **Recommendation: `rating ≥ 2` for `client-proof`, and a colour label (green) for `client-final` / `print-tiff`.** That needs one config key (`export.final_label: "green"`) and one bridge read of the label; you set green on the client's picks in the lighttable in one pass, which is what you would do anyway. No new verb.

5. **Where does the WB calibration run, and do we commit the table?**
   `wb.calibrate` is ~21 sets ≈ 40 s and adds ~21 history items to whichever frame it uses, so it refuses to run inside a job's `raw/`.
   **Recommendation: calibrate once per body on a scratch copy** (`~/Pictures/ScouterScratch/<camera>.ORF`) and **commit the resulting tables to the repo** as `tests/fixtures/wb-em10iii.json` / `wb-a7.json`. They are ~20 numbers per camera, they make `outliers.list`'s Kelvin real and its unit tests honest, and they only change if you upgrade darktable — at which point `wb.calibrate` re-runs in 40 s. The A7 table needs one ARW; if you have not shot one for this, phase 2 ships with the ORF table only and `outliers.list` falls back to the ratio test for the A7, saying so.

---

## 21. Decisions this design closes

1. Config is **JSON at `~/.config/scouter/config.json`** (tomllib is 3.11+; our floor is 3.10) — §4.1.
2. Unknown config keys are **rejected**, not ignored, so a typo cannot disarm the backup gate — §4.2.
3. The backup gate reports **one named check per failing condition** (eleven names over the table's seven rows) and guards `library.import` and `backup.run` **only** — §4.3.
4. EXIF comes from a **stdlib TIFF IFD0 reader**; `darktable-cli` cannot dump EXIF and `exiftool` is absent and non-stdlib — §5. ORF's magic is **0x4F52, not 42** (pinned).
5. A missing camera model is a **refusal**, never `unknown` baked into 300 filenames — §5.
6. The import **copies and verifies with two sha256 passes**, is resume-safe, and stops on the first mismatch — §6.
7. A darktable-down import still **succeeds** (the copy is the irreversible part) and reports `film_roll_registered: false` — §6.
8. `.scouter/` holds Scouter's per-job files; the film roll is `<job>/raw`, so exports and manifests never enter darktable's library — §4.4.
9. A library move needs **no re-import of Scouter state** (relative paths + one config key) but darktable's film rolls **do** need re-pointing; `library.relocate` says so — §4.4.
10. The vault job note is **appended, never rewritten**, and a missing vault degrades to a warning — §7.
11. Set stats are **cached on disk keyed by the XMP mtime** and computed by 4 threads, each with its own configdir — §8.1.
12. The cull **never proposes a reject**; ties break by `seq` and the result says so — §8.2.
13. The proposal is **persisted** and `cull.apply` consumes that file, so what Pratik read is what lands — §8.2/8.3.
14. Ratings are written through a **new `image.rate`** verb with per-image results, not w1ne's count-only `rate_photos` — §12.1.
15. `style.extract` filters **for real** by writing a `.dtstyle` from the XMP and importing it; `params`/`blendop_params` are copied **verbatim** — §9.1.
16. The `.dtstyle` schema is **pinned by a host probe** (`style.export_file`) before the writer is trusted; if `<iop_order>` turns out to be required and unavailable, the verb falls back to whole-history with `modules_honoured: false` — §9.2.
17. `style.apply_set` returns `outcome="partial"` with the full per-image payload; only `applied == 0` raises — §9.3.
18. Outlier EV is labelled **`ev_proxy`** and Kelvin is **`null` with a reason** when no calibration table exists; the ratio test runs instead and the result says which — §9.4.
19. Delivery presets are **client-proof 2048 / client-final full / print-tiff 16-bit AdobeRGB**; watermark is out — §10.1.
20. `input_sha256` in a delivery manifest is **read from `import.json`**, not recomputed, and says so — §10.2.
21. A faithful Kelvin↔coefficient conversion from the XMP alone is **impossible**; the design journals the coefficient vector, restores it through **probe-pinned coefficient sliders**, and inverts Kelvin from a **measured per-camera table** — §11.
22. A repeat `darkroom.set` costs **zero** bounces on a fresh watermark and **one** on a stale one; `history.changed` exposes the signal — §13.
23. Backups use **stdlib `tarfile`** plus **`age` if present, else `openssl enc`**, and close `openssl enc`'s missing authentication with a plaintext sha256 that `backup.verify` checks — §14.1.
24. Pruning **never** deletes the newest two archives, whatever `keep_archives` says — §14.2.
25. `PHASE` becomes **2**, so the two phases' events stay separable in one `metrics.db` — §18.
26. The 30-frame fixture patches EXIF capture times **in place** (equal-length ASCII) to make bursts real, and the sharpness tie it cannot avoid is asserted as a tie — §19.1.
