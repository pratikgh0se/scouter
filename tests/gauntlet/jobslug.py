"""A valid `library.import` job slug (`scouter.paths.SLUG_RE`) built from an
arbitrary harness-chosen string.

Shared by `tests/gauntlet/exec_bulk.py` (one per bulk case, from `case.id`)
and `tests/gauntlet/session.py` (`stage()`'s default `job_slug` var, from
`<body>-<stamp>`) -- both used to build the same `-`-joined, possibly
underscore-carrying strings that `scouter.paths.SLUG_RE` forbids."""

from __future__ import annotations

from scouter import paths as scouter_paths
from scouter.errors import UsageError


def job_slug_for(raw_id: str) -> str:
    """A valid job slug derived from `raw_id` (a gauntlet case id, or any
    other `-`/`_`-joined harness string).

    Gauntlet ids are joined with `-`, but a part may itself carry `_`
    (`gauntlet.case.slug()`) or come from a raw body name (`sony_a7`) --
    `SLUG_RE` (`^[a-z0-9][a-z0-9-]{0,47}$`) forbids underscores entirely.
    The old formula, `f"htc-{raw_id}"[-40:]`, kept the underscores AND
    sliced from the LEFT, which cut the disambiguating suffix off 34 of 40
    real bulk case ids and left every one failing `usage: invalid job
    slug`.

    Underscores become hyphens here (never dropped -- still legible, still
    unique), and any excess length is trimmed from the RIGHT one character
    at a time by asking `validate_slug` itself whether the result is valid
    -- the length bound and character class live in `scouter.paths` and are
    never duplicated here."""
    raw = "htc-" + raw_id.replace("_", "-")
    while True:
        try:
            return scouter_paths.validate_slug(raw)
        except UsageError:
            if len(raw) <= 1:
                raise
            raw = raw[:-1]
