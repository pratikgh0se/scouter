"""The evaluator policy for Scouter, read from check_map.json (spec 5.4)."""

from __future__ import annotations

import json

from gauntlet.evaluate import Policy
from tests.gauntlet.taxonomy import DATA


def load_policy() -> Policy:
    cm = json.loads((DATA / "check_map.json").read_text(encoding="utf-8"))
    return Policy(refusal_codes=frozenset(cm["refusal_codes"]),
                  transient_codes=frozenset(cm["transient_codes"]),
                  refusal_markers=tuple(cm["refusal_markers"]),
                  claim_markers=tuple(cm["claim_markers"]),
                  # Named, provisional overrides (check_map.json's own
                  # "flags_about"): absent here (older check_map.json with no
                  # "flags" key) reads as "nothing overridden", same as today.
                  flags=frozenset(cm.get("flags", [])))


POLICY = load_policy()
