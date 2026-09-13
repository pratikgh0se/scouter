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
class ImportResult(Payload):
    """`library.import`'s result. `files`/`totals` mirror `.scouter/import.json`
    (relative `raw/<name>` destinations, per design section 6) so the two never
    drift; a dry run leaves both empty and reports the same shape via `plan`."""

    job_slug: str = ""
    date: str = ""
    camera: str = ""
    camera_model: str | None = None
    camera_make: str | None = None
    source_root: str = ""
    files: list[dict] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    film_roll_registered: bool = False
    imported: int = 0
    scan_incomplete: bool | None = None
    recursive_honoured: bool | None = None


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
class SummaryResult(Payload):
    since: str = "7d"
    from_ts: str = ""
    verbs: list[dict] = field(default_factory=list)
    totals: dict[str, Any] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)


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
