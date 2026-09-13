"""Minimal arguments + doubles per verb, for the metrics coverage test.

Every task that registers a verb MUST add its entry here in the same commit.
That is the mechanism behind "a tool without an event fails CI".

Tasks 7-9 append their own verb groups below (one dict entry per verb, plus
any doubles the verb needs inside `install_doubles`) — do not replace this
file, extend it.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

SMOKE_ARGS: dict[str, dict] = {
    "metrics.summary": {"since": "1d"},
}


@contextlib.contextmanager
def install_doubles(verb_id: str) -> Iterator[dict]:
    """Patch whatever `verb_id` reaches for, and yield extra kwargs for it."""
    yield {}
