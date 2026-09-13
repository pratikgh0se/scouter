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


class EncryptionUnavailable(ScouterError):
    code = "encryption_unavailable"
    fix = (
        "install age (`brew install age`) and set `backup.age_recipient`, or create the "
        "openssl passfile at `detail.passfile` with mode 0600"
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
)


def exit_code_for(outcome: str) -> int:
    """Map an envelope outcome (`ok`, `dry_run`, `error:<code>`) to an exit code."""
    if outcome.startswith("error:"):
        return EXIT_CODES.get(outcome.split(":", 1)[1], EXIT_CODES["error"])
    return EXIT_CODES.get(outcome, EXIT_CODES["error"])
