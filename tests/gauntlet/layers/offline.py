"""Layer 0: the offline resolver sweep (spec 3.0), P2, no darktable.

979 phrase/sentence cases (378 taxonomy phrases + 601 request sentences) and
89 synonyms x 15 forms = 1,335 grammar cases. Total 2,314."""

from __future__ import annotations

from gauntlet.case import Case, Check, Expect, Step, case_id
from scouter.core import phrases
from tests.gauntlet.checkmap import plan_request
from tests.gauntlet.taxonomy import Taxonomy

LAYER = "offline"
PREFIX = "off"
#: The spec's independent copy of the intensity table (phrases.INTENSITY is
#: the thing under test, so the oracle must not read it).
INTENSITY_WORDS = {"a little bit": 0.5, "a tiny bit": 0.5, "a touch": 0.5, "a little": 0.5,
                   "a bit": 0.5, "slightly": 0.5, "a lot": 2.0, "strongly": 2.0, "much": 2.0,
                   "lots": 2.0, "way": 2.0}
EXPLICIT = {"EV": ("0.5 ev", "1 ev", "2 ev"), "K": ("200k", "500k", "1000k"),
            "%": ("2%", "5%", "10%"), "°": ("10°", "20°", "45°"), "ratio": ("0.1", "0.2", "0.5")}
#: The capability ids that resolve to a phrase key in check_map (phrase -> key).
SERVED_MODULES = {"exposure", "sigmoid", "temperature", "colorbalancergb", "filmicrgb",
                  "colorzones"}


def _key_for_phrase(phrase: str) -> str | None:
    for p in phrases.PHRASES:
        if phrase in p.synonyms:
            return p.key
    return None


def _capability_keys(cmap: dict) -> dict[str, str]:
    """capability id -> phrase key (tool-step capabilities map to None)."""
    out = {}
    for cap, spec in cmap["capabilities"].items():
        if "phrase" in spec:
            out[cap] = _key_for_phrase(spec["phrase"])
    return out


def _resolve_case(cid: str, text: str, allowed: list[str], src: dict) -> Case:
    return Case(
        id=cid, layer=LAYER, tier="P2", mode="offline",
        steps=[Step("vocab_resolve", {"phrase": text}, 1.0)],
        expect=Expect("workflow", checks=[Check("extra", target={
            "path": "offline_ok", "op": "truthy"})]),
        source=src, meta={"text": text, "allowed_keys": sorted(allowed)},
    )


def _module_keys(module_id: str) -> list[str]:
    keys = []
    for p in phrases.PHRASES:
        mods = {mv.module for mv in p.moves}
        if module_id in mods or ("@tone" in mods and module_id in ("sigmoid", "filmicrgb")):
            keys.append(p.key)
    return keys


def generate(tax: Taxonomy) -> list[Case]:
    cases: list[Case] = []
    owner = tax.phrase_module()
    for i, text in enumerate(tax.unique_phrases()):
        mod = owner[text]
        allowed = _module_keys(mod) if mod in SERVED_MODULES else []
        cases.append(_resolve_case(case_id(PREFIX, "tax", f"{i:03d}"), text, allowed,
                                   {"file": "taxonomy-modules.json", "module": mod}))
    cap_keys = _capability_keys(tax.check_map)
    for r in tax.requests:
        plan = plan_request(r, tax.check_map)
        allowed = sorted({cap_keys[c] for c in plan.capabilities if cap_keys.get(c)})
        cases.append(_resolve_case(case_id(PREFIX, "req", r["id"]), r["sentence"], allowed,
                                   {"file": "requests.json", "id": r["id"]}))
    for p in phrases.PHRASES:
        # Fix round 1, M6: `phrases.row_for` is the single place that maps a
        # `@tone`/`crop` move to its representative vocabulary row.
        row = phrases.row_for(p.moves[0])
        one_move = len(p.moves) == 1 and p.moves[0].kind == "adjust"
        # Fix round 1, M3: a toggle/crop phrase has no adjust move for an
        # intensity word to scale, so `resolve()` now reports intensity 1.0
        # for it regardless of which intensity word (if any) was typed.
        has_adjust = any(mv.kind == "adjust" for mv in p.moves)
        for syn in p.synonyms:
            forms = [("bare", syn, 1.0)]
            forms += [(w.replace(" ", "_"), f"{syn} {w}", f) for w, f in INTENSITY_WORDS.items()]
            forms += [(f"by_{a}", f"{syn} by {a}", None) for a in EXPLICIT[row.unit if row.unit in EXPLICIT else "ratio"]]  # noqa: E501
            for label, text, factor in forms:
                cid = case_id(PREFIX, "gram", p.key, syn, label)
                if factor is None:
                    expect = {"explicit": True} if one_move else {"refused": "usage"}
                else:
                    eff = factor if has_adjust else 1.0
                    expect = {"intensity": eff,
                              "notches": [(mv.notches or 0) * eff if mv.kind == "adjust"
                                          else None for mv in p.moves]}
                cases.append(Case(
                    id=cid, layer=LAYER, tier="P2", mode="offline",
                    steps=[Step("vocab_resolve", {"phrase": text}, 1.0)],
                    expect=Expect("workflow", checks=[Check("extra", target={
                        "path": "offline_ok", "op": "truthy"})]),
                    source={"phrase_key": p.key, "synonym": syn}, meta={"text": text,
                                                                        "grammar": expect,
                                                                        "key": p.key},
                ))
    return cases


#: 378 taxonomy phrases + 601 sentences + every synonym x 15 forms (89 today -> 2,314).
EXPECTED = 378 + 601 + sum(len(p.synonyms) for p in phrases.PHRASES) * 15
