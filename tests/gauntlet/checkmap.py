"""requests.json check strings -> engine checks + Scouter capabilities (spec 2.1).

Pure. The rules live in data/check_map.json; this module only applies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartCheck:
    text: str
    concept: str
    sign: int
    kind: str
    metric: str
    stat: str | None
    region: str
    capability: str | None


@dataclass(frozen=True)
class RequestPlan:
    request_id: str
    klass: str  # in | partial | out | impossible | workflow
    parts: tuple[PartCheck, ...]
    capabilities: tuple[str, ...]
    workflow_verb: str | None = None
    step_back: int = 0
    unmapped: tuple[str, ...] = field(default=())


def _strip_parens(s: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def split_parts(stat: str, cmap: dict) -> list[str]:
    s = stat.strip().lower()
    s = cmap["rewrites"].get(s, s)
    s = _strip_parens(s)
    parts = [s]
    for sep in cmap["separators"]:
        nxt: list[str] = []
        for p in parts:
            nxt.extend(x.strip() for x in p.split(sep) if x.strip())
        parts = nxt
    return parts


def direction_of(text: str, cmap: dict) -> dict:
    t = text.strip().lower()
    for rule in cmap["direction_rules"]:
        if rule["match"] in t:
            return rule
    return {"match": "", "kind": "none", "sign": 0}


def _embedded_sign(part: str, cmap: dict) -> tuple[str, int | None]:
    for w in cmap["part_direction_words"]:
        if part.endswith(w["suffix"]):
            return part[: -len(w["suffix"])].strip(), w["sign"]
    return part, None


def concept_of(part: str, cmap: dict) -> tuple[str, str | None]:
    """(concept, region override) for one stat part, or ("unmapped", None)."""
    for rule in cmap["stat_rules"]:
        if rule["match"] in part:
            region = None
            for rr in rule.get("region_rules", []):
                if rr["match"] in part:
                    region = rr["region"]
            return rule["concept"], region
    return "unmapped", None


def _capability(concept: str, sign: int, cmap: dict) -> str | None:
    caps = cmap["concepts"][concept]["caps"]
    if "*" in caps:
        return caps["*"]
    return caps.get(str(sign))


def plan_request(req: dict, cmap: dict) -> RequestPlan:
    rid = req["id"]
    kind = req["expected"]["kind"]
    check = req["expected"]["check"]
    if req["intent"] == "impossible" or kind == "refuse":
        return RequestPlan(rid, "impossible", (), ())
    if kind == "workflow":
        stat = check["stat_or_param"].lower()
        verb = "out"
        for rule in cmap["workflow_rules"]:
            if rule["match"] in stat:
                verb = rule["verb"]
                break
        d = direction_of(check["direction"], cmap)
        klass = "out" if verb == "out" else "workflow"
        return RequestPlan(rid, klass, (), (), workflow_verb=verb,
                           step_back=int(d.get("steps", 0)))
    overrides = cmap["request_overrides"].get(rid)
    if overrides:
        raw = [(o["concept"], o["concept"], o["sign"], "delta", None) for o in overrides]
    else:
        parts = split_parts(check["stat_or_param"], cmap)
        dir_text = check["direction"]
        dir_parts = [x.strip() for x in dir_text.split(",") if x.strip()]
        whole = direction_of(dir_text, cmap)
        raw = []
        for i, p in enumerate(parts):
            core, emb = _embedded_sign(p, cmap)
            concept, region = concept_of(core, cmap)
            if emb is not None:
                sign, kind_ = emb, "delta"
            elif len(dir_parts) == len(parts) and len(parts) > 1:
                d = direction_of(dir_parts[i], cmap)
                sign, kind_ = d["sign"], d["kind"]
            else:
                sign, kind_ = whole["sign"], whole["kind"]
            raw.append((p, concept, sign, kind_, region))
    out: list[PartCheck] = []
    unmapped: list[str] = []
    for text, concept, sign, kind_, region in raw:
        if concept == "unmapped":
            unmapped.append(text)
            continue
        c = cmap["concepts"][concept]
        if "fixed_sign" in c:
            sign = c["fixed_sign"]
        cap = _capability(concept, sign, cmap) if concept != "none" else None
        out.append(PartCheck(text=text, concept=concept, sign=sign, kind=kind_,
                             metric=c["metric"], stat=c.get("stat"),
                             region=region or c.get("region", "frame"), capability=cap))
    caps = tuple(dict.fromkeys(p.capability for p in out if p.capability))
    served = [p for p in out if p.capability]
    if unmapped:
        klass = "unmapped"
    elif served and len(served) == len(out):
        klass = "in"
    elif served:
        klass = "partial"
    else:
        klass = "out"
    return RequestPlan(rid, klass, tuple(out), caps, unmapped=tuple(unmapped))
