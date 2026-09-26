"""Result dataclasses. JSON-serialisable, no behaviour beyond `to_dict`."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _clean(value: Any) -> Any:
    """JSON has no inf/nan: emit null and let `params_hex` carry the truth."""
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


@dataclass
class Payload:
    """Shared tail of every result. Subclasses add their own defaulted fields."""

    outcome: str = "ok"
    warnings: list[str] = field(default_factory=list)
    image_id: int | None = None
    path: str | None = None
    plan: list[dict] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class ResolvedImage:
    image_id: int
    path: str
    xmp_path: str


@dataclass
class OpenResult(Payload):
    view: str = "unknown"
    opened: bool = False
    already_open: bool = False


@dataclass
class SetResult(Payload):
    module: str = ""
    param: str = ""
    instance: int = 0
    requested: float | None = None
    applied: float | None = None
    previous: float | None = None
    committed: bool = False
    source: str = "xmp"
    # Task 12: True only on the zero-bounce, zero-bridge-write shortcut --
    # the requested value already matches a FRESH-watermarked sidecar.
    noop: bool = False


@dataclass
class ModuleResult(Payload):
    module: str = ""
    action: str = ""
    instance: int = 0
    enabled: bool | None = None
    committed: bool = False


@dataclass
class UndoResult(Payload):
    undone: dict | None = None
    seq: int | None = None
    committed: bool = False
    #: Phase 3 §6.4: every journal entry undone as part of the same move group
    #: as `undone` (e.g. `darkroom.say`'s moves), so a caller sees the whole
    #: group revert rather than only its last entry.
    undone_group: list[dict] = field(default_factory=list)


@dataclass
class KelvinResult(Payload):
    """`wb.kelvin`'s result (task 11). No table -> `kelvin: None`, `reason`
    naming `scouter wb calibrate`, `table: None`; outside the calibrated
    range -> `kelvin: None`, `reason` naming the range -- never an
    extrapolated guess. `table`, when a table was found, is `{"path": str,
    "synthetic": bool}` (merged in from task 9's fix round, `phase-2`) -- a
    synthetic (placeholder or legacy) table is perfectly usable for testing
    the plumbing, but a caller can tell it apart from a real measured one."""

    kelvin: float | None = None
    interpolated: bool = False
    bracket: list[int] | None = None
    residual_ratio: float | None = None
    exact: bool = False
    camera: str = ""
    table: dict | None = None


@dataclass
class CalibrateResult(Payload):
    """`wb.calibrate`'s result (task 11). `points` is one `{k, coeff_r,
    coeff_g, coeff_b}` row per swept Kelvin value -- the same rows written to
    `table_path`'s JSON (design 11.4's object shape, under its own
    `samples` key, each `{k, coeff_r, coeff_g, coeff_b}` -- fix round 2:
    this used to say `{kelvin, coeffs}`, task 9's bare-array shape, which
    `wb.calibrate` has not written since fix round 1), so what Pratik reads
    back here and what landed on disk never drift."""

    camera: str = ""
    table_path: str = ""
    ks: list[int] = field(default_factory=list)
    estimated_s: float = 0.0
    points: list[dict] = field(default_factory=list)
    written: bool = False
    overwritten: bool = False
    #: Folder mode (product close, row 427 item 3): every raw of the chosen
    #: body (`{path, camera}`), the frame the sweep uses, and the scratch
    #: copy it actually sweeps -- the original is never touched.
    frames: list[dict] = field(default_factory=list)
    source_image: str | None = None
    scratch_copy: str | None = None


@dataclass
class StatusResult(Payload):
    view: str = "unknown"
    positions: dict[str, float | None] = field(default_factory=dict)
    nan: dict[str, bool] = field(default_factory=dict)


@dataclass
class HistoryEntry:
    num: int
    operation: str
    enabled: bool
    modversion: int
    multi_name: str
    multi_priority: int
    active: bool
    params_hex: str
    decoded: dict[str, Any] | None
    reason: str | None
    # Task 7: needed verbatim by the `.dtstyle` writer (scouter/core/dtstyle.py)
    # -- every real `<plugin>` darktable itself wrote carries its own blendop
    # params/version and a multi_name_hand_edited flag alongside multi_name,
    # pinned against tests/fixtures/hero.dtstyle (2026-09-14 host probe).
    blendop_params_hex: str = ""
    blendop_version: int = 0
    multi_name_hand_edited: bool = False


@dataclass
class HistoryResult(Payload):
    history_end: int = 0
    xmp_version: int = 0
    iop_order_version: int = 0
    current_hash: str | None = None
    xmp_mtime: float | None = None
    entries: list[HistoryEntry] = field(default_factory=list)
    source: str = "xmp"
    confirmed: bool = True


@dataclass
class CommitResult(Payload):
    strategy_used: str = ""
    committed: bool = False
    view_after: str = "unknown"
    ms: int = 0
    xmp_mtime: float | None = None
    # Task 12: True when this commit's own poll actually saw the sidecar
    # change; False only reachable with `expect_change=False`, when the
    # bridge reported nothing changed.
    changed: bool = True
    # Task 12 fix round 1, blocker 2: True when `changed`'s value is backed
    # by an actual observation (the poll saw the mtime/size move, or the
    # caller independently confirmed via readback) -- False ONLY on the
    # `expect_change=False` early-return path, where the bridge's own
    # `sidecar_written=False` proves nothing beyond "this one bounce found
    # nothing to flush". A caller must never stamp a watermark, or otherwise
    # treat the sidecar as known-fresh, from a `confirmed=False` result.
    confirmed: bool = True


@dataclass
class ChangedResult(Payload):
    """`history.changed`'s result -- a pure read, no bounce (task 12).

    Fix round 1 nit: the design spec's section 13.2 also lists a
    `history_end` field on this result. Deliberately not in this signature --
    `watermark`/`sidecar` are file-signature dicts from a `stat()` alone, and
    reporting `history_end` honestly would mean parsing the whole XMP (the
    cost `history.changed` exists to avoid). `history.read`'s own
    `HistoryResult.history_end` is the source for that number when a caller
    actually wants it.
    """

    fresh: bool = False
    watermark: dict | None = None
    sidecar: dict | None = None
    #: Carried finding 7 (task 12): `"<module>.<param>"` for every write
    #: Scouter journalled with `commit=False` for this image. Non-empty
    #: forces `fresh=False`: the sidecar's bytes can be identical to the
    #: watermark while darktable's live history has already moved on, and a
    #: file signature cannot see that -- the journal can.
    pending_uncommitted: list = field(default_factory=list)


@dataclass
class ChannelStats:
    mean: float
    median: int
    clipped_high_pct: float
    clipped_low_pct: float


@dataclass
class ImageStats:
    width: int
    height: int
    bit_depth: int
    channels: dict[str, ChannelStats]
    luminance_mean: float
    luminance_p01: int
    luminance_p50: int
    luminance_p99: int
    clipped_high_pct: float
    clipped_low_pct: float
    wb_ratio_rg: float
    wb_ratio_bg: float
    sharpness: float


@dataclass
class StatsResult(Payload):
    px_requested: int = 1024
    px_used: int = 1024
    render_ms: int = 0
    analyse_ms: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    histogram: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class PreviewResult(Payload):
    preview_path: str = ""
    fmt: str = "png"
    width: int = 0
    height: int = 0
    bytes: int = 0
    px_requested: int = 1024
    render_ms: int = 0
    cached: bool = False


@dataclass
class CompareResult(Payload):
    a: dict[str, Any] = field(default_factory=dict)
    b: dict[str, Any] = field(default_factory=dict)
    delta: dict[str, float] = field(default_factory=dict)


@dataclass
class StyleResult(Payload):
    name: str = ""
    mode: str | None = None
    modules_requested: list[str] = field(default_factory=list)
    modules_honoured: bool = False
    created: bool = False
    applied: int = 0
    missed: list[int] = field(default_factory=list)
    per_image: list[dict] = field(default_factory=list)
    note: str | None = None
    source: str | None = None
    # Task 7: the module filter is real now. `dtstyle_path` is set only on
    # the filtered path (never on the `no_filter` / whole-history fallback);
    # `operations_included` is the requested-and-present set actually
    # written; `modules_missing` is what was requested but absent from the
    # active history.
    dtstyle_path: str | None = None
    operations_included: list[str] = field(default_factory=list)
    modules_missing: list[str] = field(default_factory=list)
    # Fix round 1, should-fix 5: a requested module whose newest active entry
    # is disabled is still written (honouring the request literally), but
    # named here so the caller is never left thinking it is live.
    modules_disabled: list[str] = field(default_factory=list)


@dataclass
class StyleSetResult(Payload):
    """Task 8: `style.apply_set` -- a partial set is a RESULT, not an exception.

    `missed` carries every image id that did not apply (batch timeout, not
    found, or `apply_style` itself failing); `per_image` has one row per
    image actually attempted (never one per `requested` -- an id the run
    never got to, because two consecutive batches timed out, appears in
    neither list, and is only visible as `requested - applied - failed`
    plus a `warnings` line naming the count).
    """

    name: str = ""
    mode: str = "append"
    requested: int = 0
    applied: int = 0
    failed: int = 0
    batches: int = 0
    missed: list[int] = field(default_factory=list)
    per_image: list[dict] = field(default_factory=list)


@dataclass
class ExportedFile:
    input: str
    output: str | None
    ok: bool
    bytes: int
    error: str | None


@dataclass
class ExportResult(Payload):
    preset: str = ""
    out_dir: str = ""
    files: list[ExportedFile] = field(default_factory=list)
    exported: int = 0
    failed: int = 0


@dataclass
class ReleaseResult(Payload):
    """`export.release`'s result. `selection_rule`/`selected`/`of` mirror the
    manifest's own `selection` block, so the CLI's summary and the manifest on
    disk never drift; `manifest_path` is what `export.verify` re-reads."""

    job_slug: str = ""
    preset: str = ""
    out_dir: str = ""
    manifest_path: str = ""
    selection_rule: str = ""
    selected: int = 0
    of: int = 0
    files: list[ExportedFile] = field(default_factory=list)
    exported: int = 0
    failed: int = 0
    bytes: int = 0


@dataclass
class ImportResult(Payload):
    """`library.import`'s result. `files`/`totals` mirror `.scouter/import.json`
    (relative `raw/<name>` destinations, per design section 6) so the two never
    drift; a dry run leaves both empty and reports the same shape via `plan`.

    `duplicates` lists the byte-identical pairs the import deliberately KEPT
    BOTH of, as `[already_present_dest, new_dest]` (ruling R17): resume
    identity is `(source_name, sha256)`, so the same bytes arriving under a
    different camera filename are a second frame with their own `seq`, never
    a silent skip and never a refusal. A dry run cannot fill this in (it does
    not hash every candidate) and leaves it empty."""

    job_slug: str = ""
    date: str = ""
    camera: str = ""
    camera_model: str | None = None
    camera_make: str | None = None
    #: E12 D5/D8, row 407: every distinct body/model actually present across
    #: the WHOLE job (every import it has ever had), not just this run's --
    #: `camera`/`camera_model` above are `"mixed"`/`None` when this list has
    #: more than one entry.
    cameras: list[str] = field(default_factory=list)
    camera_models: list[str] = field(default_factory=list)
    #: Files this run's `--camera` filter excluded because their own EXIF
    #: names a different body (D5): `{"source_name", "camera"}` per file.
    skipped_other_camera: list[dict] = field(default_factory=list)
    #: A file with NO camera model in its EXIF at all, imported under an
    #: explicit `--camera X` anyway (review round 1, ruling 2):
    #: `{"source_name", "camera"}` per file.
    assumed_camera: list[dict] = field(default_factory=list)
    #: A file with no camera model in its EXIF, skipped because there was no
    #: `--camera` to fall back on (review round 1, ruling 2): `{"source_name"}`
    #: per file. The card never fails over this one file.
    skipped_no_body: list[dict] = field(default_factory=list)
    source_root: str = ""
    source_roots: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    film_roll_registered: bool = False
    imported: int = 0
    scan_incomplete: bool | None = None
    recursive_honoured: bool | None = None
    duplicates: list[list[str]] = field(default_factory=list)


@dataclass
class VerifyResult(Payload):
    job_slug: str = ""
    checked: int = 0
    ok: bool = True
    mismatched: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


@dataclass
class RegisterResult(Payload):
    job_slug: str = ""
    film_roll_registered: bool = False
    imported: int = 0
    scan_incomplete: bool | None = None
    recursive_honoured: bool | None = None


@dataclass
class JobsResult(Payload):
    jobs: list[dict] = field(default_factory=list)


@dataclass
class RelocateResult(Payload):
    old_root: str = ""
    new_root: str = ""
    jobs: list[dict] = field(default_factory=list)
    darktable_rollpaths_stale: bool = False
    next_step: str | None = None


@dataclass
class ConfigResult(Payload):
    config_path: str = ""
    config_exists: bool = False
    written: bool = False
    backup_gate: str = "unknown"
    config: dict = field(default_factory=dict)


@dataclass
class BackupResult(Payload):
    archive_dir: str = ""
    manifest_path: str = ""
    members: int = 0
    plaintext_bytes: int = 0
    ciphertext_bytes: int = 0
    encryption: str = "openssl"
    rsync_ran: bool = False
    rsync_files: int = 0
    rsync_bytes: int = 0
    rsync_exit: int | None = None
    pruned: list[str] = field(default_factory=list)
    ms_tar: int = 0
    ms_encrypt: int = 0
    ms_rsync: int = 0


@dataclass
class BackupVerifyResult(Payload):
    verified: bool = False
    archive: str = ""
    checks: list[dict] = field(default_factory=list)
    sampled_members: list[dict] = field(default_factory=list)
    sampled_raws: list[dict] = field(default_factory=list)


@dataclass
class SummaryResult(Payload):
    since: str = "7d"
    from_ts: str = ""
    verbs: list[dict] = field(default_factory=list)
    totals: dict[str, Any] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)


@dataclass
class CullResult(Payload):
    """`cull.propose`'s result. `proposals` carries one entry per frame, each
    with a `reasons` list that names its numbers (never a bare verdict); the
    same list is persisted verbatim to `proposal_path` so `cull.apply`
    consumes exactly the table Pratik read."""

    proposal_path: str = ""
    job_slug: str = ""
    groups: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutliersResult(Payload):
    """`outliers.list`'s result. `outliers` carries one row per frame that
    breached at least one tolerance, sorted worst-first by
    `max(|breach| / tolerance)`; `failed` is stats-failed frames, which are
    never counted in `within_tolerance` or promoted to `outliers` -- an
    unmeasured frame is not a passing frame. `wb_test` names which test ran
    across the set: `kelvin` when every frame resolved a real Kelvin value
    against the hero, `ratio` when none did (`wb.kelvin`'s designed
    fallback), `mixed` when it differed frame to frame."""

    reference: dict = field(default_factory=dict)
    tolerances: dict = field(default_factory=dict)
    wb_test: str = "ratio"
    checked: int = 0
    within_tolerance: int = 0
    outliers: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


@dataclass
class CullApplyResult(Payload):
    """`cull.apply`'s result. `verified`/`unverified` are reported separately
    (design 8.3) rather than folded into `applied`: darktable owns when it
    flushes a rating to the sidecar, so an unverified write is real, just not
    yet visible -- never claimed as a failure."""

    job_slug: str = ""
    proposal_path: str = ""
    applied: int = 0
    unchanged: int = 0
    would_change: int = 0
    missed: list[int] = field(default_factory=list)
    verified: int = 0
    unverified: int = 0
    per_image: list[dict] = field(default_factory=list)
    #: E12 D7, row 407: frames whose sidecar changed since the proposal was
    #: written -- skipped, never applied on stale numbers, but no longer a
    #: reason to refuse the rest of the job (paths, not just names, so a
    #: caller can act on them directly).
    stale: list[str] = field(default_factory=list)


@dataclass
class VocabResult(Payload):
    rows: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ResolveResult(Payload):
    phrase: str = ""
    normalised: str = ""
    key: str = ""
    intensity: float = 1.0
    explicit: bool = False
    moves: list[dict] = field(default_factory=list)


@dataclass
class ValueResult(Payload):
    module: str = ""
    param: str = ""
    instance: int = 0
    value: float | None = None
    display_value: float | None = None
    unit: str = ""
    source: str | None = None
    enabled: bool | None = None
    fresh: bool = False
    status: str = ""


@dataclass
class AdjustResult(Payload):
    module: str = ""
    param: str = ""
    instance: int = 0
    unit: str = ""
    delta: float | None = None
    previous: float | None = None
    target: float | None = None
    applied: float | None = None
    clamped: bool = False
    source: str | None = None
    committed: bool = False
    noop: bool = False
    summary: str = ""
    explain: list[str] = field(default_factory=list)


@dataclass
class SayResult(Payload):
    phrase: str = ""
    key: str = ""
    intensity: float = 1.0
    group: str | None = None
    moves: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    summary: str = ""
    explain: list[str] = field(default_factory=list)


@dataclass
class PresetResult(Payload):
    name: str = ""
    action: str = ""  # "save" | "apply"
    modules: list[str] = field(default_factory=list)
    dtstyle_path: str | None = None
    memory_path: str | None = None
    replaced: bool = False
    applied_to: str | None = None  # "darkroom" | "library"
    history_end_before: int | None = None
    history_end_after: int | None = None
    explain: list[str] = field(default_factory=list)


@dataclass
class PresetListResult(Payload):
    presets: list[dict] = field(default_factory=list)
    darktable_only: list[str] = field(default_factory=list)
    missing_in_darktable: list[str] = field(default_factory=list)


@dataclass
class Envelope:
    """What the CLI prints and the MCP adapter returns. One shape, all layers."""

    verb: str
    layer: str
    job: str
    ok: bool
    outcome: str
    ms: int
    data: dict
    error: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "verb": self.verb,
            "layer": self.layer,
            "job": self.job,
            "ok": self.ok,
            "outcome": self.outcome,
            "ms": self.ms,
            "data": _clean(self.data),
        }
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class JobRunResult(Payload):
    """`job.run`'s result (product close, row 427 item 5 prep). `steps` has
    one row per step attempted or skipped -- `{step, verb, outcome, counts,
    warnings, error?, reason?}` -- in chain order; `stopped_at` names the
    step that refused or came back partial (the chain stops there)."""

    job_slug: str = ""
    steps: list[dict] = field(default_factory=list)
    completed: int = 0
    stopped_at: str | None = None
