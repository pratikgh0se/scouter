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
}


@contextlib.contextmanager
def install_doubles(verb_id: str) -> Iterator[dict]:
    """Install a FakeBridge (and a fake darktable-cli where needed) for one verb."""
    from scouter.core import dtbridge
    from tests.scouter_fakes import FakeBridge

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
    })
    dtbridge.set_bridge(bridge)
    try:
        yield {}
    finally:
        dtbridge.set_bridge(None)
