"""The evaluator policy for Scouter, read from check_map.json (spec 5.4)."""

from __future__ import annotations

import json
import warnings
from datetime import date, datetime

from gauntlet.evaluate import KNOWN_FLAGS, Policy
from tests.gauntlet.taxonomy import DATA


class UnknownFlagError(ValueError):
    """A check_map.json "flags" entry names something `gauntlet.evaluate.
    KNOWN_FLAGS` does not recognise. Zenkai Rules Z4 (docs/ZENKAI.md): a
    flag is a scoring bypass, so an unrecognised or malformed one is refused
    outright, never silently ignored."""


def _active_flags(entries: list) -> frozenset[str]:
    """check_map.json's "flags" list holds Zenkai Z4 override records --
    `{"name", "reason", "card", "expires"}`, one per flag someone has
    actually turned ON. Refuses (`UnknownFlagError`) a name outside
    `KNOWN_FLAGS` or an entry missing a required field; an entry past its
    `expires` date is dropped with a loud warning, never silently kept
    (Z4: "an expired override still in effect" is its own violation)."""
    today = date.today()
    active: set[str] = set()
    for entry in entries:
        name = entry.get("name")
        if name not in KNOWN_FLAGS:
            raise UnknownFlagError(
                f"check_map.json flags: {name!r} is not in gauntlet.evaluate.KNOWN_FLAGS "
                f"({sorted(KNOWN_FLAGS)}) -- add it there in the same change that adds the "
                "evaluate() branch reading it")
        missing = [f for f in ("reason", "card", "expires") if not entry.get(f)]
        if missing:
            raise UnknownFlagError(
                f"check_map.json flags: {name!r} is missing {missing} (Zenkai Rules Z4: "
                "reason, rule named, fix card, expiry)")
        expires = datetime.strptime(entry["expires"], "%Y-%m-%d").date()
        if expires < today:
            warnings.warn(
                f"check_map.json flags: {name!r} expired on {entry['expires']} "
                f"(card {entry['card']!r}) -- ignored, not renewed", stacklevel=2)
            continue
        active.add(name)
    return frozenset(active)


def load_policy() -> Policy:
    cm = json.loads((DATA / "check_map.json").read_text(encoding="utf-8"))
    return Policy(refusal_codes=frozenset(cm["refusal_codes"]),
                  transient_codes=frozenset(cm["transient_codes"]),
                  refusal_markers=tuple(cm["refusal_markers"]),
                  claim_markers=tuple(cm["claim_markers"]),
                  # Named, provisional overrides (check_map.json's own
                  # "flags_about"): absent here (older check_map.json with no
                  # "flags" key) reads as "nothing overridden", same as today.
                  flags=_active_flags(cm.get("flags", [])),
                  offer_markers=tuple(cm.get("offer_markers", ())))


POLICY = load_policy()
