"""Test doubles: a temp cache, a fake bridge, a fake darktable-cli."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from scouter.config import BackupConfig, Config


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


def config_fixture(tmp: Path, *, backup: bool = True, encryption: str = "openssl") -> Config:
    """A Config pointing at temp directories, with the gate satisfiable when backup=True."""
    root = tmp / "library"
    root.mkdir(parents=True, exist_ok=True)
    target = tmp / "backup"
    target.mkdir(parents=True, exist_ok=True)
    passfile = tmp / "backup.pass"
    passfile.write_text("test-passphrase\n")
    passfile.chmod(0o600)
    return Config(
        library_root=str(root),
        path=str(tmp / "config.json"),
        exists=True,
        backup=BackupConfig(
            enabled=backup,
            target=str(target),
            encryption=encryption,
            openssl_passfile=str(passfile),
        ),
        cameras=(("E-M10 Mark III", "em10iii"),),
    )


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
            # Row 422/429: a sane default for `darktable_mcp.diag` (`scouter
            # doctor`'s view-transport line), the shape the plugin answers
            # since row 429 removed the main-thread proxy.
            "darktable_mcp.diag": {
                "view_switch_transport": "action",
                "view_transports": ["action"],
                "default_commit_strategy": "auto",
                "auto_commit_order": ["no_bounce", "view_bounce_action"],
                "view": "darkroom",
            },
            # A sane default for `library.import`/`library.register`'s
            # `import_batch` call, so a test that isn't specifically exercising
            # the film-roll-registration path doesn't have to restate the
            # plugin's whole result shape (task 3; see the Lua handler's real
            # return value in darktable_mcp.lua).
            "import_batch": {
                "imported": 0,
                "source_path": "",
                "recursive": False,
                "recursive_honoured": True,
                "note": None,
                "directories_imported": 1,
                "scan_incomplete": False,
            },
            # A sane default for `export.release`'s finals selection
            # (task 10, ruling R12): every requested id comes back known,
            # with no colour labels, so a test that isn't specifically
            # exercising label selection doesn't have to restate this
            # shape. Override per-test (see tests/test_export_release.py)
            # for a frame that actually carries a label.
            "image.color_labels": lambda p: {
                "missing": [],
                "per_image": [
                    {"image_id": image_id, "labels": []} for image_id in p.get("image_ids", [])
                ],
            },
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

    def count(self, method: str) -> int:
        """How many times `method` was called -- task 12's E8 asserts zero writes."""
        return sum(1 for m, _ in self.calls if m == method)


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


#: A minimal, real, parseable sidecar -- one active `exposure` entry, nothing
#: from `cfg.style.never_include`'s default list (crop, clipping, retouch,
#: spots, flip, rotatepixels, censorize, watermark, ashift). Fix round 2,
#: blocker 1: `style.extract(no_filter=True)` now reads the sidecar too (to
#: enforce `never_include` against the whole active history, not just a
#: caller-requested `modules` list), so every fake raw file that exercises
#: it needs a real one; this is the shared, deliberately forbidden-free one
#: for tests that want phase 1's native `style.create_from_current` path
#: (which only runs when nothing is forbidden) to actually run.
MINIMAL_STYLE_SIDECAR = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:darktable="http://darktable.sf.net/"
   darktable:history_end="1"
   darktable:iop_order_version="4"
   darktable:xmp_version="5">
   <darktable:history>
    <rdf:Seq>
     <rdf:li
      darktable:num="0"
      darktable:operation="exposure"
      darktable:enabled="1"
      darktable:modversion="7"
      darktable:params="00000000000080b93333333f00004842000080c00100000001000000"
      darktable:multi_name=""
      darktable:multi_name_hand_edited="0"
      darktable:multi_priority="0"
      darktable:blendop_version="14"
      darktable:blendop_params="gz11eJxjYIAACQYYOOHEgAZY0QWAgBGLGANDgz0Ej1Q+dcF/IADRAGpyHQU="/>
    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def write_minimal_style_sidecar(raw_path: Path | str) -> Path:
    """Write `MINIMAL_STYLE_SIDECAR` next to `raw_path`. Returns the sidecar path."""
    sidecar = Path(str(raw_path) + ".xmp")
    sidecar.write_text(MINIMAL_STYLE_SIDECAR, encoding="utf-8")
    return sidecar


class FakeCLIWrapper:
    """Stand-in for `darktable_mcp.darktable.cli_wrapper.CLIWrapper`, monkeypatched
    onto `scouter.core.scout.CLIWrapper` (task 10).

    Unlike `FakeCli` below (which fakes `CLIWrapper.export`, an interface no
    production code actually calls), this fakes the REAL `export_image`
    method `scout.render` invokes, so `export.release`'s tests can assert
    the exact `extra_args` list that reached the wrapper without shelling
    out to a fake darktable-cli script and parsing argv back out of it.

    `calls` is a CLASS attribute (not per-instance): `scout.render` builds a
    fresh `CLIWrapper` per call, so a test resets `FakeCLIWrapper.calls = []`
    in `setUp` and reads it after, across as many renders as the test made.
    `raises` maps an input path (str) to an exception `export_image` raises
    instead of writing anything, for `files[].error` / partial-failure tests.
    """

    calls: list[dict] = []
    raises: dict[str, BaseException] = {}

    def __init__(
        self, darktable_cli_path: str | None = None, configdir: Path | None = None
    ) -> None:
        self.darktable_cli_path = darktable_cli_path
        self.configdir = configdir

    def export_image(
        self,
        input_path: Path,
        output_path: Path,
        format_type: str = "jpeg",
        quality: int = 95,
        max_width: int | None = None,
        max_height: int | None = None,
        timeout: int = 180,
        extra_args: list[str] | None = None,
    ) -> Path:
        FakeCLIWrapper.calls.append(
            {
                "input_path": str(input_path),
                "output_path": str(output_path),
                "format_type": format_type,
                "quality": quality,
                "max_width": max_width,
                "max_height": max_height,
                "timeout": timeout,
                "extra_args": list(extra_args) if extra_args else extra_args,
            }
        )
        exc = FakeCLIWrapper.raises.get(str(input_path))
        if exc is not None:
            raise exc
        Path(output_path).write_bytes(bytes(1024))
        return Path(output_path)

    def get_version(self) -> str:
        return "5.4.1-fake"


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


_history_commits = {"n": 0}


def with_new_history_hash(text: str) -> str:
    """`text` as darktable writes it after a REAL commit: same decoded values,
    a new `history_current_hash` (same length, so the size is unchanged).

    Row 430: `history.commit` proves a commit by the sidecar's HISTORY
    changing, not by its mtime moving, so a commit double that only rewrites
    identical text models the metadata-only rewrite bug, not a commit.
    """
    import re

    _history_commits["n"] += 1
    fresh = f"{_history_commits['n']:032x}"
    new, count = re.subn(
        r'darktable:history_current_hash="[0-9a-f]*"',
        f'darktable:history_current_hash="{fresh}"',
        text,
        count=1,
    )
    if not count:
        raise AssertionError("fixture sidecar has no history_current_hash to change")
    return new
