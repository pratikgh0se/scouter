"""Typed error classes. Every failure the agent can see has a stable code."""

from __future__ import annotations


class ScouterError(Exception):
    """Base for every classified Scouter failure.

    `code` is the stable machine name (spec section 7's error classes); `fix`
    is the actionable sentence the agent shows the user. Never raise this
    directly — raise a subclass, so the code is never invented at the call site.
    """

    code = "error"
    fix = ""

    def __init__(self, message: str, *, fix: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        if fix is not None:
            self.fix = fix
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "outcome": "error",
            "code": self.code,
            "message": self.message,
            "fix": self.fix,
            "detail": self.detail,
        }


class UsageError(ScouterError):
    """The caller's own arguments are wrong: exit code 2, like argparse's.

    Deliberately NOT in `ERROR_CLASSES` (which enumerates spec section 9's
    darktable/bridge failure classes, and which `tests/test_paths_errors.py`
    pins): `usage` is a caller mistake, not a runtime failure of darktable --
    but it does have a row in `EXIT_CODES` already, so a shell script can
    branch on it the same way it branches on a bad flag.
    """

    code = "usage"
    fix = "fix the argument and re-run; `--help` lists the accepted values"


class BridgeTimeout(ScouterError):
    code = "bridge_timeout"
    fix = (
        "start darktable and run `scouter doctor`; the Lua plugin must be "
        "installed (`darktable-mcp install-plugin`) and darktable restarted"
    )


class WrongView(ScouterError):
    code = "wrong_view"
    fix = "open the image in the darkroom first: `scouter darkroom open <image>`"


class NotInDarkroom(ScouterError):
    code = "not_in_darkroom"
    fix = (
        "the darkroom is active but the action returned nan: the module is not "
        "in this image's pipeline, or the image is still loading. Re-run "
        "`scouter darkroom open <image>` and retry"
    )


class CliWroteNothing(ScouterError):
    code = "cli_wrote_nothing"
    fix = (
        "darktable-cli reported success but wrote no file; check that darktable's "
        "'write sidecar file for each image' preference is on and the raw is supported"
    )


class DbLocked(ScouterError):
    code = "db_locked"
    fix = (
        "a darktable-cli run contended for a library lock; it must use Scouter's "
        "isolated configdir (see `detail.configdir`)"
    )


class StylePartial(ScouterError):
    code = "style_partial"
    fix = "see `detail.per_image`; re-run for the ids that were missed"


class CommitTimeout(ScouterError):
    code = "commit_timeout"
    fix = (
        "darktable did not write the XMP in time; leave and re-enter the darkroom, "
        "or raise the commit timeout"
    )


class BackupNotConfigured(ScouterError):
    code = "backup_not_configured"
    fix = (
        "configure a backup target before importing: run `scouter config init`, then set "
        "`backup.enabled` and `backup.target` in the config file (the check that failed is "
        "in `detail.check`)"
    )


class ChecksumMismatch(ScouterError):
    code = "checksum_mismatch"
    fix = (
        "the bytes on disk do not match the recorded sha256; see `detail.expected` and "
        "`detail.actual`. Re-copy from the source, or check the disk before trusting the job"
    )


class DestinationCollision(ScouterError):
    code = "destination_collision"
    fix = (
        "a different file already occupies that name (see `detail.path`); nothing was "
        "overwritten. Import with a different --job-slug, or move the existing file aside"
    )


class ExifUnreadable(ScouterError):
    code = "exif_unreadable"
    fix = (
        "pass --camera <slug> for this card, or add the model to `cameras` in the config; "
        "`detail.magic` shows what the file's TIFF header actually said"
    )


class StyleWriteFailed(ScouterError):
    code = "style_write_failed"
    fix = (
        "darktable refused the .dtstyle at `detail.path`, which is kept for inspection; "
        "re-run `style extract` with --no-filter for a whole-history style"
    )


class NoSidecar(ScouterError):
    """`style.extract`'s filtered path needs a real sidecar to read the history
    from -- a typed refusal (fix round 1, blocker 2), never the raw
    `FileNotFoundError` `xmp.read_sidecar` raises.

    Deliberately NOT in `ERROR_CLASSES` (spec section 9's darktable failure
    classes, pinned by `tests/test_paths_errors.py`), same reasoning as
    `UsageError`: this is Scouter refusing to build a style from a history it
    cannot read, not one of darktable's own failure modes.
    """

    code = "no_sidecar"
    fix = "commit history first (`scouter history commit <image>`), then retry"


class EncryptionUnavailable(ScouterError):
    code = "encryption_unavailable"
    fix = (
        "install age (`brew install age`) and set `backup.age_recipient`, or create the "
        "openssl passfile at `detail.passfile` with mode 0600"
    )


class ManifestUnauthenticated(ScouterError):
    """`export.verify`'s manifest has no `.sha256` sidecar next to it.

    `export.release` always writes one alongside the manifest -- a missing
    sidecar means the manifest cannot be trusted before its own contents are
    used to check anything else, so this is a hard refusal (fix round 1,
    blocker 3), never a silently-skipped check.
    """

    code = "manifest_unauthenticated"
    fix = (
        "no <manifest>.sha256 next to the manifest: export.release always writes one. "
        "Restore it, or re-run export.release to produce a fresh, authenticated manifest"
    )


class StateLocked(ScouterError):
    """A job's `state/<job>.json.lock` (E12 D3, row 407) stayed held by
    another process for longer than `cache.lock_wait_s`, with no bound at
    all on the old blocking `flock`.

    Review round 1, should-fix: this bounded wait is HARDENING against lock
    contention in general -- it is not a fix for the E12 live run's own two
    15-18 minute stalls. Those stalls happened BEFORE the verb (and this
    lock, when the verb even takes it) was ever reached: run 036
    (`darkroom open`) failed inside `dtbridge.call`, before
    `_record_opened_image`'s lock; run 042 (`outliers list`) never takes
    this lock at all. D3's actual root cause is still open; `metrics.
    instrumented`'s new `startup_ms` (review round 1) is the follow-up that
    makes the NEXT such stall attributable. `detail.pid`/`detail.since` are
    the holder's own pid and when it took the lock, when the lock file
    recorded them."""

    code = "state_locked"
    fix = (
        "another Scouter process is holding this job's lock (see `detail.pid` "
        "and `detail.since`); wait for it to finish, or raise `cache.lock_wait_s` "
        "in the config if 30s is routinely too short for this machine"
    )


class ExportAllFailed(ScouterError):
    """Every frame `export.release` tried to render failed.

    A `ReleaseResult` with `exported=0` and `outcome="ok"` would read as
    success; this is the typed refusal instead (fix round 1, should-fix 7),
    raised before the manifest is written -- a manifest describing zero
    exported files is not a release.
    """

    code = "export_all_failed"
    fix = "every render failed; see detail.files for the per-image reasons"


class ValueUnknown(ScouterError):
    """Phase 3 §15: a relative move needs a current value Scouter cannot read."""

    code = "value_unknown"
    fix = (
        "Scouter cannot read this parameter's current value (see detail.reason). "
        "Set it absolutely with `scouter darkroom set <module> <param> <value>`; "
        "relative moves on it need its XMP slot pinned by a host probe (or, for "
        "white balance, `scouter wb calibrate`)."
    )


class NotPinned(ScouterError):
    """Phase 3 §15: a vocabulary row with no pinned darktable action path."""

    code = "not_pinned"
    fix = (
        "this parameter has no pinned darktable action path; pinning is a host-probe "
        "job (docs/superpowers/specs/2026-09-24-scouter-phase3-design.md §10.1). "
        "`scouter vocab list` shows which words work today."
    )


class DarktableUnresponsive(BridgeTimeout):
    """Watchdog design 2.2: a call timed out AND the 5 s health ping failed
    while a darktable process exists (fix round 1: a darktable that is not
    running stays a plain `BridgeTimeout`).

    A `BridgeTimeout` subclass so every existing `except BridgeTimeout` site
    keeps its tolerant behaviour (import, style batches); its own code and
    exit code (24) still say which of the two happened."""

    code = "darktable_unresponsive"
    fix = (
        "darktable stopped answering (Lua worker or GTK main loop wedged); quit it "
        "(Force Quit if needed) and relaunch, or enable `watchdog.auto_heal`."
    )


class WriteUnverified(ScouterError):
    """A write verb committed, but the value read back from the sidecar is not
    the one it asked for, and no darktable hard limit explains the difference
    (row 433 silent-failure fix, night-1 gauntlet `cov-phrase-faded_blacks-
    olympus-touch`: target -0.0027, sidecar 0.0098, reported ok).

    Raised instead of an `ok` envelope, never instead of a clamp: a value that
    darktable pinned to one of its own hard bounds is reported as
    `clamped: true`. `detail` carries `requested`, `expected` (what darktable
    should have stored), `applied` (what the sidecar says), `previous`,
    `tolerance` and `history_end`. The journal entry is kept: the action was
    sent, so `undo_last` can still revert it.
    """

    code = "write_unverified"
    fix = (
        "darktable was sent the value but the sidecar does not show it (it may have been "
        "rewritten from an older library state). Read the value again with bounce "
        "(`scouter darkroom get <module> <param> --bounce`, MCP darkroom_get bounce=true) "
        "and retry the move if it is still not there; `undo_last` reverts this write"
    )


#: Process exit codes. 0/1/2 follow shell convention; the rest are one per error
#: class, so a shell script can branch on the failure without parsing JSON.
EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "error": 1,
    "usage": 2,
    "bridge_timeout": 3,
    "wrong_view": 4,
    "dry_run": 5,
    "not_in_darkroom": 6,
    "cli_wrote_nothing": 7,
    "db_locked": 8,
    "style_partial": 9,
    # `style.apply_set` reports a partial set as a RESULT (outcome="partial") rather
    # than an exception, so the successes survive; it shares 9 so one shell branch
    # covers "some of it worked" however it arrived.
    "partial": 9,
    "commit_timeout": 10,
    "backup_not_configured": 11,
    "checksum_mismatch": 12,
    "destination_collision": 13,
    "exif_unreadable": 14,
    "style_write_failed": 15,
    "encryption_unavailable": 16,
    "manifest_unauthenticated": 17,
    "export_all_failed": 18,
    "no_sidecar": 19,
    "value_unknown": 20,
    "not_pinned": 21,
    # Fix branch (E12 D3) originally claimed 20; renumbered to the next free
    # value once phase 3 landed "value_unknown"/"not_pinned" at 20/21.
    "state_locked": 22,
    # Row 408, B2: `darkroom.undo_last` on an empty journal, called for REAL
    # (not a dry run) -- distinct from both "ok" (0) and "dry_run" (5), so a
    # shell script (or `ok = outcome in ("ok", "dry_run")`) never mistakes
    # "there was nothing to undo" for "the undo happened". Fix branch
    # originally claimed 21; renumbered to the next free value after
    # "state_locked" took 22.
    "noop": 23,
    # Watchdog (spec 6): a timed-out call whose health ping also failed.
    "darktable_unresponsive": 24,
    # Row 433 night-1 fix: a committed write whose read-back value is not the
    # one requested (and not a darktable clamp).
    "write_unverified": 25,
}

ERROR_CLASSES: tuple[type[ScouterError], ...] = (
    BridgeTimeout,
    WrongView,
    NotInDarkroom,
    CliWroteNothing,
    DbLocked,
    StylePartial,
    CommitTimeout,
    BackupNotConfigured,
    ChecksumMismatch,
    DestinationCollision,
    ExifUnreadable,
    StyleWriteFailed,
    EncryptionUnavailable,
    ManifestUnauthenticated,
    ExportAllFailed,
    ValueUnknown,
    NotPinned,
    StateLocked,
    DarktableUnresponsive,
    WriteUnverified,
)


def exit_code_for(outcome: str) -> int:
    """Map an envelope outcome (`ok`, `dry_run`, `error:<code>`) to an exit code."""
    if outcome.startswith("error:"):
        return EXIT_CODES.get(outcome.split(":", 1)[1], EXIT_CODES["error"])
    return EXIT_CODES.get(outcome, EXIT_CODES["error"])
