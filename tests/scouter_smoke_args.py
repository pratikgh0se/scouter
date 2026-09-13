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
    "darkroom.open": {"image": "/tmp/scouter-smoke.ORF"},
    "darkroom.set": {"module": "exposure", "param": "exposure", "value": 1.0,
                     "dry_run": True},
    "darkroom.nudge": {"module": "exposure", "param": "exposure", "steps": 1,
                       "dry_run": True},
    "darkroom.enable": {"module": "exposure", "dry_run": True},
    "darkroom.reset": {"module": "exposure"},            # gated: dry run by default
    "darkroom.undo_last": {"dry_run": True},
    "darkroom.status": {"module": "exposure", "param": "exposure"},
    "darkroom.describe_history": {"image": "/tmp/scouter-smoke.ORF"},
    "history.read": {"image": "/tmp/scouter-smoke.ORF"},
    "history.commit": {"image": "/tmp/scouter-smoke.ORF", "timeout_s": 0.05},
    "scout.stats": {"image": "/tmp/scouter-smoke.ORF", "px": 64},
    "scout.preview": {"image": "/tmp/scouter-smoke.ORF", "px": 64, "fmt": "ppm"},
    "scout.compare": {"a": "/tmp/scouter-smoke.ORF", "b": "/tmp/scouter-smoke.ORF",
                      "px": 64},
    "style.extract": {"image": "/tmp/scouter-smoke.ORF", "name": "smoke", "modules": []},
    "style.apply": {"image": "/tmp/scouter-smoke.ORF", "name": "smoke"},
    "export.run": {"images": ["/tmp/scouter-smoke.ORF"], "preset": "preview",
                   "out_dir": "/tmp/scouter-smoke-export"},   # out_dir overridden below
}


@contextlib.contextmanager
def install_doubles(verb_id: str) -> Iterator[dict]:
    """Install a FakeBridge (and a fake darktable-cli where needed) for one verb."""
    import os

    from scouter import paths
    from scouter.core import dtbridge
    from tests.scouter_fakes import FAKE_CLI_WRITES_PPM, FakeBridge, fake_cli_script

    raw = "/tmp/scouter-smoke.ORF"
    bridge = FakeBridge({
        "darkroom.open": {"image_id": 1, "path": raw, "view": "darkroom",
                          "opened": False, "already_open": True},
        "image.lookup": {"found": True, "image_id": 1, "path": raw},
        "darkroom.action": lambda p: {"nan": False, "status": p.get("value"),
                                      "view": "darkroom", **p},
        "darkroom.status": {"position": 0.5, "nan": False, "view": "darkroom"},
        "module.enable": {"nan": False, "view": "darkroom"},
        "module.disable": {"nan": False, "view": "darkroom"},
        "module.reset": {"nan": False, "view": "darkroom"},
        "history.commit": {"strategy_used": "sidecar_action", "ok": True,
                           "view_after": "darkroom", "cpu_ms": 10},
        "snapshot.take": {"taken": True, "view": "darkroom"},
        "style.create_from_current": {"name": "smoke", "created": True,
                                      "existing_replaced": False,
                                      "modules_requested": 0, "modules_honoured": False},
        "style.apply": {"name": "smoke", "mode": "append", "applied": 1,
                        "missed": [], "errors": []},
        "list_styles": {"styles": [], "count": 0},
    })
    dtbridge.set_bridge(bridge)
    saved_cli = os.environ.get("SCOUTER_DARKTABLE_CLI")
    extra: dict = {}
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
    if verb_id == "export.run":
        # `preview` writes a .png, which the fake CLI script above matches;
        # the out_dir lives under this test's own temp cache root.
        extra["out_dir"] = str(paths.cache_root() / "smoke-export")
    try:
        yield extra
    finally:
        dtbridge.set_bridge(None)
        if needs_cli:
            if saved_cli is None:
                os.environ.pop("SCOUTER_DARKTABLE_CLI", None)
            else:
                os.environ["SCOUTER_DARKTABLE_CLI"] = saved_cli
