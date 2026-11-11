"""A toy product adapter: proves the engine runs with no real product."""

from __future__ import annotations

from gauntlet.adapter import CallRecord, LayerSpec, Observation, Park, RefuseRun
from gauntlet.case import Case, Check, Expect, Step, case_id


class ToySession:
    def __init__(self, adapter: ToyAdapter) -> None:
        self.adapter = adapter
        self.closed = False

    def run_case(self, case: Case) -> Observation:
        self.adapter.ran.append(case.id)
        behaviour = self.adapter.behaviours.get(case.id, "ok")
        if behaviour == "park":
            raise Park("usage limit", until=None)
        if behaviour == "refuse":
            raise RefuseRun("foreign darktable: pid 4242")
        if behaviour == "sleep":
            import time

            time.sleep(case.meta.get("sleep_s", 5))
        if behaviour == "crash":
            self.adapter.crashes += 1
        value = 1.0 if behaviour in ("ok", "crash", "sleep") else 0.7
        env = {"ok": True, "outcome": "ok", "data": {}}
        return Observation(
            calls=[CallRecord("set", case.steps[0].args, env, wall_s=0.01, write=True)],
            before={"fingerprint": "a", "params": {"x.v": 0.7}},
            after={"fingerprint": "b" if value == 1.0 else "a", "params": {"x.v": value}},
        )

    def abort(self) -> None:
        self.adapter.aborts += 1

    def close(self) -> None:
        self.closed = True


class ToyAdapter:
    name = "toy"

    def __init__(self, behaviours: dict[str, str] | None = None, n: int = 4) -> None:
        self.behaviours = behaviours or {}
        self.n = n
        self.ran: list[str] = []
        self.crashes = 0
        self.aborts = 0
        self.sessions = 0

    def layers(self) -> list[LayerSpec]:
        return [LayerSpec("alpha", 1, self.n, True), LayerSpec("beta", 2, 2, True)]

    def generate(self, layer: str) -> list[Case]:
        n = self.n if layer == "alpha" else 2
        out = []
        for i in range(n):
            cid = case_id(layer, i)
            out.append(
                Case(
                    id=cid,
                    layer=layer,
                    tier="P0" if i == 0 else "P1",
                    mode="tool",
                    steps=[Step("set", {"v": 1.0}, budget_s=0.5, write=True)],
                    expect=Expect(
                        "edit",
                        checks=[Check("M1", kind="equals", target={"key": "x.v", "value": 1.0})],
                        sidecar={"changed": True, "key": "x.v"},
                    ),
                    meta={"sleep_s": 5},
                )
            )
        return out

    def open_session(self, layer: str, run_root) -> ToySession:
        self.sessions += 1
        return ToySession(self)

    def crash_count(self) -> int:
        return self.crashes

    def crash_reports_since(self, t: float) -> list[str]:
        return [f"toy-{i}.ips" for i in range(self.crashes)]

    def signoff_lines(self, run=None) -> list[str]:
        return ["toy originals unchanged: yes"]

    def generation_notes(self, cases: list[Case]) -> str:
        return "## Toy notes\n\nnothing to add"

    def write_tools(self) -> frozenset[str]:
        return frozenset({"set"})

    def policy(self):
        from gauntlet.evaluate import Policy

        return Policy(frozenset({"usage"}), frozenset({"bridge_timeout"}), ("can't",), ("done",))


def make() -> ToyAdapter:
    return ToyAdapter()
