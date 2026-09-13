"""Test doubles: a temp cache, a fake bridge, a fake darktable-cli."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class TempCache:
    """Redirect every Scouter path into a fresh temp dir for one test."""

    def __init__(self) -> None:
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._saved: dict[str, str | None] = {}
        self.root = Path()

    def start(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="scouter-test-")
        self.root = Path(self._tmp.name)
        for key, value in (("SCOUTER_CACHE", str(self.root)), ("SCOUTER_JOB", "job-test")):
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self.root

    def stop(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._tmp is not None:
            self._tmp.cleanup()

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def audit(self, job: str = "job-test") -> list[dict]:
        path = self.root / "audit" / f"{job}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def db_rows(self) -> list[tuple]:
        import sqlite3

        path = self.root / "metrics.db"
        if not path.exists():
            return []
        with sqlite3.connect(path) as conn:
            return conn.execute("SELECT verb, layer, outcome, ms FROM events").fetchall()


class FakeBridge:
    """Stand-in for darktable_mcp.bridge.client.Bridge.

    `responses` maps a method name to a dict, or to a callable taking params.
    `raises` maps a method name to an exception instance to raise.
    Every call is recorded in `calls` so tests can assert a gated verb made none.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        raises: dict[str, BaseException] | None = None,
    ) -> None:
        self.responses: dict[str, Any] = {
            "view.current": {"view": "darkroom"},
            **(responses or {}),
        }
        self.raises = raises or {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
        self.calls.append((method, dict(params)))
        # `darkroom.open {}` is NOT a query for "which image is loaded" -- the
        # real plugin errors out (`darkroom.open: no image for nil`) because
        # there is nothing to load. A fake that answered it anyway made the
        # inoperable no-arg `history.commit` path look fine in the unit suite
        # for three rounds (final review, blocker 1). Read-only "what is
        # loaded" has its own method: `darkroom.current`.
        if method == "darkroom.open" and not (params.get("image_id") or params.get("path")):
            raise AssertionError(
                "darkroom.open was called with no image_id and no path; the real "
                "plugin raises for that. Use darkroom.current to READ the loaded image."
            )
        if method in self.raises:
            raise self.raises[method]
        value = self.responses.get(method)
        if value is None:
            raise AssertionError(f"FakeBridge has no response for {method!r}")
        if callable(value):
            return value(params)
        return value

    def methods_called(self) -> list[str]:
        return [m for m, _ in self.calls]


def fake_cli_script(tmp: Path, body: str) -> Path:
    """Write an executable fake darktable-cli and return its path."""
    script = tmp / "darktable-cli"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


#: A fake darktable-cli that writes a valid 2x1 8-bit P6 for any output path.
FAKE_CLI_WRITES_PPM = (
    'out=""\n'
    'for a in "$@"; do case "$a" in *.ppm|*.png) out="$a";; esac; done\n'
    'printf "P6\\n2 1\\n255\\n" > "$out"\n'
    'printf "\\377\\000\\000\\000\\377\\000" >> "$out"\n'
)

#: A fake darktable-cli that exits 0 and writes nothing (the known failure).
FAKE_CLI_WRITES_NOTHING = "exit 0\n"

#: A fake darktable-cli that fails with darktable's own lock message.
FAKE_CLI_DB_LOCKED = 'echo "ERROR: database is locked, probably another instance" >&2\nexit 1\n'


class FakeCli:
    """Stub of CLIWrapper.export: returns a fixture PPM path without shelling out.

    `path` is the fixture PPM to hand back for every export call. `calls`
    records every invocation so tests can assert what was requested.
    """

    def __init__(self, path: Path | str, raises: BaseException | None = None) -> None:
        self.path = Path(path)
        self.raises = raises
        self.calls: list[dict] = []

    def export(self, **kwargs: Any) -> Path:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        return self.path
