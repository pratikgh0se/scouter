"""The case model (spec 2.2, schema gauntlet-case/1). Pure."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "gauntlet-case/1"
TIERS = ("P0", "P1", "P2")
MODES = ("tool", "agent", "offline", "cli")
EXPECT_KINDS = ("edit", "refuse", "workflow", "typed_error", "noop", "property")

_SLUG = re.compile(r"[^a-z0-9_.]+")


def slug(text: str) -> str:
    return _SLUG.sub("_", str(text).lower()).strip("_") or "x"


def case_id(layer: str, *parts: object) -> str:
    """`<layer>-<part>-<part>...`, every part slugged; stable across runs."""
    return "-".join([slug(layer), *(slug(p) for p in parts)])


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    budget_s: float = 8.0
    write: bool = False


@dataclass
class Check:
    metric: str  # M1..M10, "sidecar", "envelope", "file", "text"
    stat: str | None = None
    region: str = "frame"
    sign: int = 0
    kind: str = "delta"  # delta|toward_zero|to_zero|capped|restored|unchanged|equals|within|contains  # noqa: E501
    target: dict | None = None
    step: int | None = None
    tags: dict = field(default_factory=dict)


@dataclass
class Expect:
    kind: str
    checks: list[Check] = field(default_factory=list)
    sidecar: dict | None = None
    codes: list[str] = field(default_factory=list)
    property: str | None = None


@dataclass
class Case:
    id: str
    layer: str
    tier: str
    mode: str
    steps: list[Step]
    expect: Expect
    body: str | None = None
    frame_role: str | None = None
    source: dict = field(default_factory=dict)
    followups: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    repro: str = ""

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"{self.id}: tier {self.tier!r} not in {TIERS}")
        if self.mode not in MODES:
            raise ValueError(f"{self.id}: mode {self.mode!r} not in {MODES}")
        if self.expect.kind not in EXPECT_KINDS:
            raise ValueError(f"{self.id}: expect.kind {self.expect.kind!r} not in {EXPECT_KINDS}")
        if not self.repro:
            self.repro = f"python -m gauntlet run --case {self.id} --stamp <stamp>"

    def budget_cap_s(self) -> float:
        """Spec 2.3: each step's hard cap is budget + 30 s; the case cap is their sum."""
        steps = list(self.steps)
        for f in self.followups:
            steps.extend(_step(s) for s in f.get("steps", []))
        return sum(s.budget_s + 30.0 for s in steps) or 30.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = SCHEMA
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Case:
        d = {k: v for k, v in d.items() if k != "schema"}
        exp = d.pop("expect")
        return cls(
            steps=[_step(s) for s in d.pop("steps")],
            expect=Expect(
                kind=exp["kind"],
                checks=[Check(**c) for c in exp.get("checks", [])],
                sidecar=exp.get("sidecar"),
                codes=list(exp.get("codes", [])),
                property=exp.get("property"),
            ),
            **d,
        )


def _step(s: Any) -> Step:
    return s if isinstance(s, Step) else Step(**s)


def dumps(obj: Any) -> str:
    """Canonical JSON: sorted keys, no spaces drift, ASCII-safe. Byte-stable."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def to_jsonl(cases: list[Case]) -> str:
    return "".join(dumps(c.to_dict()) + "\n" for c in cases)


def from_jsonl(text: str) -> list[Case]:
    return [Case.from_dict(json.loads(line)) for line in text.splitlines() if line.strip()]
