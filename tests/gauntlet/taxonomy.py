"""The Scouter gauntlet's data side (spec 2.1): the four research JSONs plus
check_map.json, loaded, shape-checked and count-asserted so a silent edit to
the data fails loudly. Pure stdlib; imports nothing from scouter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

#: Spec §0: the counts the loader asserts.
EXPECTED = {
    "modules": 69,
    "unique_phrases": 378,
    "workflows": 12,
    "bulk": 9,
    "look_components": 16,
    "cannot_do": 14,
    "looks": 234,
    "look_families": 16,
    "look_confidence": {"documented": 127, "community-consensus": 38, "inferred": 69},
    "requests": 601,
    "clusters": 141,
    "impossible": 60,
    "request_genres": 14,
    "followups": 24,
    "metrics": 10,
    "properties": 7,
    "robustness": 24,
    "hang_causes": 4,
    "install_steps": 8,
    "report_sections": 8,
}


class TaxonomyError(ValueError):
    """The data does not match the counts the spec pins."""


@dataclass(frozen=True)
class Taxonomy:
    modules: list[dict]
    workflows: list[dict]
    bulk: list[dict]
    look_components: list[dict]
    cannot_do: list[dict]
    looks: list[dict]
    look_conventions: dict
    requests: list[dict]
    measurement: dict
    check_map: dict

    def unique_phrases(self) -> list[str]:
        seen: dict[str, None] = {}
        for m in self.modules:
            for p in m["phrases"]:
                seen.setdefault(p, None)
            for prm in m["params"]:
                for p in prm.get("phrases", []):
                    seen.setdefault(p, None)
        return list(seen)

    def phrase_module(self) -> dict[str, str]:
        """First module (taxonomy order) that lists each phrase."""
        out: dict[str, str] = {}
        for m in self.modules:
            for p in m["phrases"]:
                out.setdefault(p, m["id"])
            for prm in m["params"]:
                for p in prm.get("phrases", []):
                    out.setdefault(p, m["id"])
        return out

    def module_by_name(self, name: str) -> dict | None:
        key = name.strip().lower()
        for m in self.modules:
            if key in (m["id"].lower(), m["ui_name"].lower()):
                return m
        for m in self.modules:
            if key in m["ui_name"].lower() or m["ui_name"].lower() in key:
                return m
        return None


def _read(name: str, data_dir: Path) -> dict:
    return json.loads((data_dir / name).read_text(encoding="utf-8"))


def _check(label: str, got: object, want: object, errors: list[str]) -> None:
    if got != want:
        errors.append(f"{label}: expected {want}, got {got}")


def load(data_dir: Path = DATA) -> Taxonomy:
    tax = _read("taxonomy-modules.json", data_dir)
    looks = _read("looks.json", data_dir)
    reqs = _read("requests.json", data_dir)["requests"]
    meas = _read("measurement-and-robustness.json", data_dir)
    cmap = _read("check_map.json", data_dir)
    t = Taxonomy(
        modules=tax["modules"],
        workflows=tax["workflows"],
        bulk=tax["bulk"],
        look_components=tax["look_components"],
        cannot_do=tax["cannot_do"],
        looks=looks["looks"],
        look_conventions=looks["conventions"],
        requests=reqs,
        measurement=meas,
        check_map=cmap,
    )
    errors: list[str] = []
    e = EXPECTED
    _check("modules", len(t.modules), e["modules"], errors)
    _check("unique phrases", len(t.unique_phrases()), e["unique_phrases"], errors)
    _check("workflows", len(t.workflows), e["workflows"], errors)
    _check("bulk flows", len(t.bulk), e["bulk"], errors)
    _check("look components", len(t.look_components), e["look_components"], errors)
    _check("cannot-do intents", len(t.cannot_do), e["cannot_do"], errors)
    _check("looks", len(t.looks), e["looks"], errors)
    _check("look families", len({x["family"] for x in t.looks}), e["look_families"], errors)
    conf: dict[str, int] = {}
    for x in t.looks:
        conf[x["confidence"]] = conf.get(x["confidence"], 0) + 1
    _check("look confidence", conf, e["look_confidence"], errors)
    _check("requests", len(t.requests), e["requests"], errors)
    _check("clusters", len({r["cluster_id"] for r in t.requests}), e["clusters"], errors)
    _check(
        "impossible", sum(r["intent"] == "impossible" for r in t.requests), e["impossible"], errors
    )
    _check("request genres", len({r["genre"] for r in t.requests}), e["request_genres"], errors)
    _check("follow-ups", sum(len(r["followups"]) for r in t.requests), e["followups"], errors)
    _check("metrics", len(meas["metrics"]), e["metrics"], errors)
    _check("properties", len(meas["properties"]), e["properties"], errors)
    _check("robustness", len(meas["robustness"]), e["robustness"], errors)
    _check("hang causes", len(meas["hang_causes"]), e["hang_causes"], errors)
    _check("install steps", len(meas["install_rehearsal"]["steps"]), e["install_steps"], errors)
    _check("report sections", len(meas["report_sections"]), e["report_sections"], errors)
    problems = {r["problem"] for r in t.requests}
    missing = sorted(problems - set(cmap["role_for_problem"]))
    if missing:
        errors.append(f"role_for_problem misses {missing}")
    if errors:
        raise TaxonomyError("; ".join(errors))
    return t
