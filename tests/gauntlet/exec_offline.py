"""Layer 0 executor: phrases.resolve in-process, no darktable (spec 3.0)."""

from __future__ import annotations

import time

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case


def judge(case: Case) -> tuple[bool, dict]:
    from scouter.core import phrases
    from scouter.errors import UsageError

    text = case.meta["text"]
    try:
        res = phrases.resolve(text)
    except UsageError as e:
        refused = {"refused": "usage", "suggestions": (e.detail or {}).get("suggestions")}
        if "grammar" in case.meta:
            return case.meta["grammar"].get("refused") == "usage", refused
        # an honest refusal must carry its nearest phrases (possibly an empty list)
        return "suggestions" in (e.detail or {}), refused
    got = {
        "key": res.key,
        "intensity": res.intensity,
        "explicit": res.explicit,
        "notches": [mv.notches if mv.kind == "adjust" else None for mv in res.moves],
    }
    if "grammar" in case.meta:
        want = case.meta["grammar"]
        if "refused" in want:
            return False, got
        if want.get("explicit"):
            return res.explicit and res.key == case.meta["key"], got
        ok = (
            res.key == case.meta["key"]
            and abs(res.intensity - want["intensity"]) < 1e-9
            and all(
                (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 1e-9)
                for a, b in zip(got["notches"], want["notches"])
            )
        )
        return ok, got
    return res.key in case.meta["allowed_keys"], got


class OfflineSession:
    def run_case(self, case: Case) -> Observation:
        t0 = time.monotonic()
        ok, got = judge(case)
        env = {"ok": "refused" not in got, "outcome": "ok", "data": got}
        return Observation(
            calls=[
                CallRecord(
                    "vocab_resolve",
                    {"phrase": case.meta["text"]},
                    env,
                    wall_s=time.monotonic() - t0,
                )
            ],
            extra={"offline_ok": ok, "resolved": got},
            wall_s=time.monotonic() - t0,
        )

    def abort(self) -> None:
        pass

    def close(self) -> None:
        pass
