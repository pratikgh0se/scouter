"""Layer 6: MCP/CLI parity (spec 3.6). 45 tools x {happy, error} = 90.

Happy arguments come from tests/scouter_smoke_args.SMOKE_ARGS (the one table
every registered verb must already have). The error variant breaks the first
string argument, or adds an unknown argument when there is none."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from scouter import registry
from tests.scouter_smoke_args import (
    SMOKE_ARGS,
    SMOKE_CONFIG_PATH,
    SMOKE_OUT_DIR,
    SMOKE_RAW,
    SMOKE_SOURCE_DIR,
)

LAYER, PREFIX = "parity", "par"
#: The verbs the user guide and README walkthrough use (P0 per spec 3.6).
GUIDE_VERBS = {"darkroom.open", "darkroom.set", "darkroom.say", "darkroom.undo_last",
               "scout.stats", "history.read", "preset.save", "preset.apply_job", "job.run",
               "vocab.list", "export.release", "wb.kelvin"}
PLACEHOLDERS = {SMOKE_RAW: "{frame:hero}", SMOKE_OUT_DIR: "{out_dir}",
                SMOKE_CONFIG_PATH: "{config_path}", SMOKE_SOURCE_DIR: "{card}"}
MISSING = "/nonexistent/htc-missing.ORF"


def _swap(v):
    if isinstance(v, str):
        return PLACEHOLDERS.get(v, v)
    if isinstance(v, list):
        return [_swap(x) for x in v]
    return v


def happy_args(verb_id: str) -> dict:
    return {k: _swap(v) for k, v in SMOKE_ARGS[verb_id].items()}


def error_args(verb: registry.Verb) -> dict:
    args = happy_args(verb.id)
    for spec in verb.args:
        if spec.type in ("str", "image") and spec.name in args:
            args[spec.name] = MISSING if spec.type == "image" else "__htc_bad_value__"
            return args
    args["__htc_unknown__"] = 1
    return args


def generate() -> list[Case]:
    verbs = registry.load_verbs()
    out: list[Case] = []
    for vid in sorted(verbs):
        verb = verbs[vid]
        tier = "P0" if vid in GUIDE_VERBS else "P1"
        for variant, args in (("happy", happy_args(vid)), ("error", error_args(verb))):
            out.append(Case(
                id=case_id(PREFIX, vid, variant), layer=LAYER, tier=tier, mode="tool",
                steps=[Step("parity_pair", {"verb": vid, "args": args}, 60.0, verb.writes)],
                expect=Expect("workflow", [Check("extra", target={"path": "envelopes_match",
                                                                  "op": "truthy"})]),
                body="olympus", frame_role="hero", source={"file": "registry", "id": vid},
                meta={"verb": vid, "variant": variant, "mcp": verb.mcp_name,
                      "cli": [verb.group, verb.cli_command]}))
    return out


#: 2 x the registered verbs (45 today -> 90; a new verb adds 2).
EXPECTED = 2 * len(registry.load_verbs())
