"""Minimal arguments + doubles per verb, for the metrics coverage test.

Every task that registers a verb MUST add its entry here in the same commit.
That is the mechanism behind "a tool without an event fails CI".

Tasks 7-9 append their own verb groups below (one dict entry per verb, plus
any doubles the verb needs inside `install_doubles`) — do not replace this
file, extend it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

#: Placeholders `install_doubles` rewrites into paths under the CURRENT test's
#: temp cache root (final review nit). These used to be literal `/tmp/...`
#: paths, and `darkroom.set`'s smoke run really did write
#: `/tmp/scouter-smoke.ORF.xmp` on the host -- a unit test must not write
#: outside its own throwaway directory.
SMOKE_RAW = "<smoke-raw>"
SMOKE_OUT_DIR = "<smoke-out-dir>"
SMOKE_CONFIG_PATH = "<smoke-config-path>"
SMOKE_SOURCE_DIR = "<smoke-source-dir>"

SMOKE_ARGS: dict[str, dict] = {
    "metrics.summary": {"since": "1d"},
    # dry_run=True: config.init writes to disk otherwise, and this coverage
    # test must never touch the real ~/.config even by accident (no
    # SCOUTER_CONFIG override here, only SCOUTER_CACHE).
    "config.init": {"path": SMOKE_CONFIG_PATH, "dry_run": True},
    "config.show": {"path": SMOKE_CONFIG_PATH},
    "darkroom.open": {"image": SMOKE_RAW},
    "darkroom.set": {
        "module": "exposure",
        "param": "exposure",
        "value": 1.0,
        "commit": True,
    },  # real write path AND a real commit (see install_doubles)
    "darkroom.nudge": {"module": "exposure", "param": "exposure", "steps": 1, "dry_run": True},
    "darkroom.enable": {"module": "exposure", "dry_run": True},
    "darkroom.reset": {"module": "exposure"},  # gated: dry run by default
    "darkroom.undo_last": {"dry_run": True},
    "darkroom.status": {"module": "exposure", "param": "exposure"},
    "darkroom.describe_history": {"image": SMOKE_RAW},
    "history.read": {"image": SMOKE_RAW},
    "history.commit": {"image": SMOKE_RAW, "timeout_s": 0.05},
    "history.changed": {"image": SMOKE_RAW},
    "scout.stats": {"image": SMOKE_RAW, "px": 64},
    "scout.preview": {"image": SMOKE_RAW, "px": 64, "fmt": "ppm"},
    "scout.compare": {"a": SMOKE_RAW, "b": SMOKE_RAW, "px": 64},
    "style.extract": {"image": SMOKE_RAW, "name": "smoke", "modules": []},
    "style.apply": {"image": SMOKE_RAW, "name": "smoke"},
    # No confirm: always gated (task 8), so this is a dry run -- it still
    # calls list_styles (the smoke fixture's own empty answer makes this an
    # expected `usage` error, which the coverage test tolerates; the point
    # here is the one-event count, not a successful apply).
    "style.apply_set": {"images": [SMOKE_RAW], "name": "smoke"},
    "export.run": {"images": [SMOKE_RAW], "preset": "preview", "out_dir": SMOKE_OUT_DIR},
    # No job named "smoke-job" exists on disk here, so this fails fast (usage:
    # no such job) -- still exactly one event, which is all this coverage
    # test asserts (same trick as cull.apply below).
    "export.release": {"job_slug": "smoke-job", "preset": "preview"},
    # No manifest at this path, so this fails fast (usage: no manifest) --
    # still exactly one event.
    "export.verify": {"manifest": "/nonexistent/no-such-manifest.json"},
    # library.* calls `scouter.config.load()` with no override, exactly like
    # production -- there is no `--config` flag for these verbs (design
    # section 6). `SMOKE_SOURCE_DIR` resolves to this test's own (empty) cache
    # dir, so `_scan` finds nothing and the call fails fast one way or another
    # (the backup gate first, if a real config happens to exist on the host;
    # an empty scan otherwise) -- this coverage test only asserts one event,
    # not success, and `confirm` is deliberately omitted so nothing is ever
    # written even if the gate does pass.
    "library.import": {"source": SMOKE_SOURCE_DIR, "job_slug": "smoke-job"},
    "library.verify": {"job_slug": "smoke-job"},
    "library.register": {"job_slug": "smoke-job"},
    "library.jobs": {},
    # `confirm` deliberately omitted, same reasoning as library.import above:
    # a dry run at worst, a fast `usage` error (no config / no library_root /
    # new_root doesn't exist) at worst-worst -- either way exactly one event,
    # and nothing is ever written.
    "library.relocate": {"new_root": SMOKE_SOURCE_DIR},
    # No real job on disk, so `cull.propose` resolving `--images` (the one
    # frame `install_doubles` sets up) is enough to exercise it: EXIF comes
    # from a re-read (no import.json to draw on), and nothing is persisted
    # since no --job-slug is given -- this coverage test only asserts one
    # event, not that the proposal was written anywhere.
    "cull.propose": {"images": [SMOKE_RAW], "px": 64, "workers": 1},
    # No job named "smoke-job" exists on disk here, so this fails fast
    # (usage: no such job) -- still exactly one event, which is all this
    # coverage test asserts.
    "cull.apply": {"job_slug": "smoke-job"},
    # dry_run=True: backup.run's gate check runs even for a dry run (design
    # 4.3's order is gate -> enumerate -> ...), so with no SCOUTER_CONFIG
    # override here this legitimately errors out on an unconfigured gate --
    # which is fine, this coverage test only cares about the event count.
    "backup.run": {"dry_run": True},
    "backup.verify": {},
    # `reference` and the one `images` entry resolve to the SAME smoke raw
    # (`install_doubles` maps every SMOKE_RAW placeholder to one fixed path),
    # so the set is empty once the reference excludes itself -- a fast
    # `usage` error, still exactly one event, which is all this coverage
    # test asserts.
    "outliers.list": {"reference": SMOKE_RAW, "images": [SMOKE_RAW], "px": 64, "workers": 1},
    # No calibration table for whatever camera this fake raw resolves to (and
    # no readable EXIF at all, since this smoke raw is never written to disk)
    # -- kelvin() fails fast one way or another, still exactly one event.
    "wb.kelvin": {"image": SMOKE_RAW},
    # confirm omitted (same reasoning as darkroom.reset/library.import above):
    # wb.calibrate is gated, so this is a dry run -- one event, no darktable
    # writes, no table touched.
    "wb.calibrate": {"image": SMOKE_RAW, "k_from": 3000, "k_to": 3100},
    "vocab.list": {},
}


@contextlib.contextmanager
def install_doubles(verb_id: str) -> Iterator[dict]:
    """Install a FakeBridge (and a fake darktable-cli where needed) for one verb."""
    import os
    import time
    from pathlib import Path

    from scouter import paths
    from scouter.core import dtbridge
    from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, fake_cli_script

    # Everything this helper touches lives under the current test's temp cache
    # root ($SCOUTER_CACHE, set by TempCache), never in /tmp.
    raw_dir = paths.cache_root() / "smoke"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = str(raw_dir / "scouter-smoke.ORF")
    bridge = FakeBridge(
        {
            "darkroom.open": {
                "image_id": 1,
                "path": raw,
                "view": "darkroom",
                "opened": False,
                "already_open": True,
            },
            "darkroom.current": {
                "image_id": 1,
                "path": raw,
                "view": "darkroom",
                "source": "action_images",
            },
            "image.lookup": {"found": True, "image_id": 1, "path": raw},
            "darkroom.action": lambda p: {
                "nan": False,
                "status": p.get("value"),
                "view": "darkroom",
                **p,
            },
            "darkroom.status": {"position": 0.5, "nan": False, "view": "darkroom"},
            "module.enable": {"nan": False, "view": "darkroom"},
            "module.disable": {"nan": False, "view": "darkroom"},
            "module.reset": {"nan": False, "view": "darkroom"},
            "history.commit": {
                "strategy_used": "sidecar_action",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 10,
            },
            "snapshot.take": {"taken": True, "view": "darkroom"},
            "style.create_from_current": {
                "name": "smoke",
                "created": True,
                "existing_replaced": False,
                "modules_requested": 0,
                "modules_honoured": False,
            },
            "style.apply": {
                "name": "smoke",
                "mode": "append",
                "applied": 1,
                "missed": [],
                "errors": [],
            },
            "list_styles": {"styles": [], "count": 0},
            # "import_batch" needs no override here: FakeBridge's own default
            # (tests/scouter_fakes.py) covers library.import/library.register.
        }
    )
    dtbridge.set_bridge(bridge)
    saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")

    def _real(value):
        if value == SMOKE_RAW:
            return raw
        if value == SMOKE_OUT_DIR:
            return str(paths.cache_root() / "smoke-export")
        if value == SMOKE_CONFIG_PATH:
            return str(paths.cache_root() / "smoke-config.json")
        if value == SMOKE_SOURCE_DIR:
            return str(raw_dir)
        if isinstance(value, list):
            return [_real(item) for item in value]
        return value

    # Every placeholder in this verb's SMOKE_ARGS, resolved against this
    # test's own cache root. Merged OVER SMOKE_ARGS by the caller.
    extra: dict = {k: _real(v) for k, v in SMOKE_ARGS[verb_id].items()}
    needs_cli = verb_id.startswith("scout.") or verb_id == "export.run"
    if needs_cli:
        # scout.* and export.run shell out to darktable-cli (via CLIWrapper);
        # point it at a fake script that writes a valid P6 for any
        # *.ppm/*.png output, inside this test's own cache root, so nothing
        # touches a real darktable install.
        cli_dir = paths.cache_root() / "smoke-cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        script = fake_cli_script(cli_dir, FAKE_CLI_WRITES_PPM)
        os.environ["SCOUTER_DARKTABLE_CLI"] = str(script)
    if verb_id == "darkroom.set":
        # SMOKE_ARGS sends commit=True here: exercise the REAL history.commit
        # (task 8), not just a stubbed acknowledgement, the same trick
        # DarkroomCommitSeamTest (tests/test_darkroom_core.py) uses -- write a
        # minimal well-formed sidecar and bump its mtime forward, so
        # history.commit's own poll (mtime advanced + parses) succeeds.
        sidecar = Path(raw + ".xmp")

        def commit_response(_params: dict) -> dict:
            sidecar.write_text(
                '<?xml version="1.0"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                '<rdf:Description rdf:about="" xmlns:darktable="http://darktable.sf.net/" '
                'darktable:history_end="0"><darktable:history><rdf:Seq/></darktable:history>'
                "</rdf:Description></rdf:RDF></x:xmpmeta>",
                encoding="utf-8",
            )
            future = time.time() + 2
            os.utime(sidecar, (future, future))
            return {
                "strategy_used": "view_bounce",
                "ok": True,
                "view_after": "darkroom",
                "cpu_ms": 5,
            }

        bridge.responses["history.commit"] = commit_response
    try:
        yield extra
    finally:
        dtbridge.set_bridge(None)
        if needs_cli:
            if saved_cli is None:
                os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
            else:
                os.environ["SCOUTER_DARKTABLE_CLI"] = saved_cli
        if verb_id == "darkroom.set":
            Path(raw + ".xmp").unlink(missing_ok=True)
