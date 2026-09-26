import pytest

from gauntlet.adapter import LayerSpec
from gauntlet.case import Case, Expect, Step
from gauntlet.generate import GenerationError, check_layer, generate_all, write_generation
from gauntlet.store import RunDir
from gauntlet.tests.toy import ToyAdapter


def _c(i, tier="P1", meta=None, layer="alpha"):
    return Case(id=f"{layer}-{i}", layer=layer, tier=tier, mode="tool",
                steps=[Step("set")], expect=Expect("edit"), meta=meta or {})


def test_writes_cases_and_generation_md_byte_identically(tmp_path):
    a = ToyAdapter()
    run1 = RunDir(tmp_path, "s1")
    run2 = RunDir(tmp_path, "s2")
    write_generation(run1, a)
    write_generation(run2, a)
    assert run1.cases_jsonl.read_bytes() == run2.cases_jsonl.read_bytes()
    md = run1.generation_md.read_text()
    assert "| 1 | alpha | 4 | 4 |" in md and "## Toy notes" in md


def test_count_gate_two_percent():
    spec = LayerSpec("alpha", 1, 100, True)
    check_layer(spec, [_c(i) for i in range(98)])
    with pytest.raises(GenerationError, match="2% off"):
        check_layer(spec, [_c(i) for i in range(97)])


def test_unmapped_p0_p1_fail_p2_listed():
    spec = LayerSpec("alpha", 1, 0, True)
    with pytest.raises(GenerationError, match="unmapped"):
        check_layer(spec, [_c(1, "P1", {"unmapped": ["x"]})])
    lc = check_layer(spec, [_c(1, "P2", {"unmapped": ["x"]})])
    assert lc.unmapped_p2 == ["alpha-1"]


def test_layer_filter_and_unknown_layer():
    cases, counts = generate_all(ToyAdapter(), ["beta"])
    assert [c.layer for c in cases] == ["beta", "beta"]
    with pytest.raises(GenerationError, match="unknown"):
        generate_all(ToyAdapter(), ["gamma"])


def test_duplicate_ids_refused():
    class Dup(ToyAdapter):
        def generate(self, layer):
            return [_c(1, layer=layer), _c(1, layer=layer)]

        def layers(self):
            return [LayerSpec("alpha", 1, 2, True)]

    with pytest.raises(GenerationError, match="duplicate"):
        generate_all(Dup())
