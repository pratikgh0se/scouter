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
    "commit_timeout": 10,
}

ERROR_CLASSES: tuple[type[ScouterError], ...] = (
    BridgeTimeout,
    WrongView,
    NotInDarkroom,
    CliWroteNothing,
    DbLocked,
    StylePartial,
    CommitTimeout,
)


def exit_code_for(outcome: str) -> int:
    """Map an envelope outcome (`ok`, `dry_run`, `error:<code>`) to an exit code."""
    if outcome.startswith("error:"):
        return EXIT_CODES.get(outcome.split(":", 1)[1], EXIT_CODES["error"])
    return EXIT_CODES.get(outcome, EXIT_CODES["error"])
