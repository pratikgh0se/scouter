"""Layer 8: user-guide walkthrough (20) + educative levels (15) = 35, P0.

WALKTHROUGH is the literal command list from README "A hero edit" and "A
hero session" and the vault user guide (Knowledge/Scouter - User guide
2026-09-26), in document order. `test_guide_layer.py` re-extracts the README
commands and fails when the README drifts from this list."""

from __future__ import annotations

import json
import re

from gauntlet.case import Case, Check, Expect, Step, case_id
from tests.gauntlet import checkmap
from tests.gauntlet.taxonomy import DATA

LAYER, PREFIX = "guide", "gd"
#: Rows 458/461 (night-1 triage rank 7, GD-WB-preset4), wired the same way
#: coverage.py/scenarios.py already do: the corpus hero is darktable's
#: temperature preset 4, and check_map.json's `wb_route_a` block says a
#: warmth move there refuses (module_unavailable) until
#: channelmixerrgb.temperature is pinned. The README/user-guide "a touch
#: warmer" example and the edu warmer/cooler phrases hit the same guard --
#: this only fixes the EXPECTATION, never the guard itself, and never the
#: README/user-guide wording (Pratik decides that, night1 triage rank 7
#: "needs Pratik").
_CMAP = json.loads((DATA / "check_map.json").read_text(encoding="utf-8"))
_WB_WORD = re.compile(r"\bwarmer\b|\bcooler\b")


def _wb_phrase_key(text: str) -> str | None:
    m = _WB_WORD.search(text)
    return m.group(0) if m else None


def _wb_route_a(text: str) -> dict | None:
    key = _wb_phrase_key(text)
    return checkmap.wb_route_a_expect_for_hero(_CMAP, key) if key else None
#: (label, kind, payload, expected). kind "cli": argv after `scouter`; kind
#: "say": an MCP darkroom_say phrase from the guide's phrase table.
WALKTHROUGH: tuple[tuple[str, str, object, dict], ...] = (
    ("readme_open", "cli", ["darkroom", "open", "{frame:hero}"], {"ok": True}),
    ("readme_set_exposure", "cli", ["darkroom", "set", "exposure", "exposure", "1.2"],
     {"ok": True, "param": ("exposure.exposure", 1.2, "EV")}),
    ("readme_set_contrast", "cli", ["darkroom", "set", "sigmoid", "contrast", "1.35"],
     {"ok": True, "param": ("sigmoid.contrast", 1.35, "ratio")}),
    ("readme_stats", "cli", ["--pretty", "scout", "stats", "{frame:hero}", "--px", "1024"],
     {"exit": 0}),
    ("readme_undo", "cli", ["darkroom", "undo-last"], {"ok": True}),
    ("readme_history", "cli", ["--pretty", "history", "read", "{frame:hero}"], {"exit": 0}),
    ("session_touch_warmer", "cli", ["--pretty", "darkroom", "say", "a touch warmer"],
     {"exit": 0}),
    ("session_less_contrast_full", "cli",
     ["--pretty", "darkroom", "say", "less contrast", "--educative", "full"], {"exit": 0}),
    ("session_undo", "cli", ["darkroom", "undo-last"], {"ok": True}),
    ("session_preset_save", "cli", ["preset", "save", "harbour-warm"], {"ok": True}),
    ("session_preset_apply_job", "cli",
     ["preset", "apply-job", "harbour-warm", "--job-slug", "{job_slug}", "--yes"], {"ok": True}),
    ("guide_slightly_brighter", "say", "slightly brighter",
     {"ok": True, "delta": ("exposure.exposure", 0.15, "EV")}),
    ("guide_touch_warmer", "say", "a touch warmer",
     {"ok": True, "delta": ("temperature.temperature", 150.0, "K")}),
    ("guide_less_contrast", "say", "less contrast", {"ok": True}),
    ("guide_lift_shadows", "say", "lift the shadows", {"ok": True}),
    ("guide_more_vibrance", "say", "a bit more vibrance", {"ok": True}),
    ("guide_warm_highlights", "say", "warm the highlights",
     {"ok": True, "param": ("colorbalancergb.highlights_hue", 60.0, "°")}),
    ("guide_more_magenta_refuses", "say", "more magenta", {"code": "value_unknown"}),
    ("guide_job_run_dry", "cli", ["--pretty", "job", "run", "--job-slug", "{job_slug}",
                                  "--preset", "harbour-warm", "--hero", "{job_frame:1}",
                                  "--dry-run"], {"exit": 0}),
    ("guide_job_run", "cli", ["--pretty", "job", "run", "--job-slug", "{job_slug}", "--preset",
                              "harbour-warm", "--hero", "{job_frame:1}", "--yes"], {"exit": 0}),
)
EDU_MOVES = (("brighter", "exposure"), ("darker", "exposure"), ("warmer", "white_balance"),
             ("cooler", "white_balance"), ("more contrast", "tone_mapper"))
LEVELS = ("off", "brief", "full")


def _walk_case(i: int, label: str, kind: str, payload, want: dict) -> Case:
    checks: list[Check] = []
    if kind == "cli":
        step = Step("cli", {"argv": list(payload)}, 60.0, True)
        if "exit" in want:
            checks.append(Check("extra", target={"path": "exit", "op": "eq",
                                                 "value": want["exit"]}))
    else:
        step = Step("darkroom_say", {"phrase": payload}, 8.0, True)
    if want.get("ok"):
        checks.append(Check("envelope", step=0, target={"path": "ok", "op": "eq",
                                                        "value": True}))
    if "param" in want:
        k, v, unit = want["param"]
        checks.append(Check("M1", kind="equals", target={"key": k, "value": v, "unit": unit}))
    if "delta" in want:
        k, d, unit = want["delta"]
        checks.append(Check("M1", kind="delta", target={"key": k, "delta": d, "unit": unit,
                                                        "tol": abs(d) * 0.1}))
    exp = Expect("typed_error", checks, codes=[want["code"]]) if "code" in want else \
        Expect("workflow", checks)
    text = payload[-1] if kind == "cli" else payload
    wb = _wb_route_a(text)
    if wb is not None and wb["expect"] == "typed_error":
        exp = Expect("typed_error", codes=list(wb["codes"]))
    return Case(id=case_id(PREFIX, f"{i:02d}", label), layer=LAYER, tier="P0",
                mode="cli" if kind == "cli" else "tool", steps=[step], expect=exp,
                body="olympus", frame_role="hero", source={"file": "README/user guide",
                                                           "label": label},
                meta={"order": i, "kind": kind, "session": "walkthrough"})


def _edu_case(level: str, j: int, phrase: str, concept: str) -> Case:
    seen_before = any(c == concept for _p, c in EDU_MOVES[:j])
    if level == "off":
        chk = Check("envelope", step=0, target={"path": "data.explain", "op": "len_eq",
                                                "value": 0})
    elif level == "brief" or seen_before:
        chk = Check("envelope", step=0, target={"path": "data.explain", "op": "len_eq",
                                                "value": 1})
    else:
        chk = Check("envelope", step=0, target={"path": "data.explain", "op": "len_gte",
                                                "value": 2})
    checks = [chk]
    if level == "brief":
        checks.append(Check("envelope", step=0, target={"path": "data.explain.0",
                                                        "op": "regex", "value": r"\d.*\d"}))
    exp = Expect("workflow", checks)
    wb = _wb_route_a(phrase)
    if wb is not None and wb["expect"] == "typed_error":
        exp = Expect("typed_error", codes=list(wb["codes"]))
    return Case(id=case_id(PREFIX, "edu", level, f"{j}", phrase), layer=LAYER, tier="P0",
                mode="tool", steps=[Step("darkroom_say", {"phrase": phrase, "educative": level},
                                         8.0, True)],
                expect=exp, body="olympus", frame_role="hero",
                source={"file": "README", "section": "Educative mode"},
                meta={"session": f"edu-{level}", "order": j, "concept": concept})


def generate() -> list[Case]:
    out = [_walk_case(i, *row) for i, row in enumerate(WALKTHROUGH)]
    for level in LEVELS:
        out.extend(_edu_case(level, j, p, c) for j, (p, c) in enumerate(EDU_MOVES))
    return out


EXPECTED = 35
