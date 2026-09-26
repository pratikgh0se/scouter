from tests.gauntlet import taxonomy
from tests.gauntlet.layers import offline


def test_offline_count_ids_tier():
    cases = offline.generate(taxonomy.load())
    assert len(cases) == offline.EXPECTED == 2314
    assert len({c.id for c in cases}) == 2314
    assert {c.tier for c in cases} == {"P2"} and {c.mode for c in cases} == {"offline"}


def _tax_case(cases, i: int):
    return next(c for c in cases if c.id == f"off-tax-{i:03d}")


def test_tax_allowed_keys_include_the_phrases_own_synonym_key():
    """Item 3: a taxonomy phrase resolves through the vocabulary's own
    grammar, not through the taxonomy module that happens to list it first.
    off-tax-071 'deeper shadows' is filed under module `sigmoid` but is
    literally a `deepen_shadows` synonym; off-tax-143 'warmer' is filed
    under `channelmixerrgb` but is a `warmer` synonym."""
    cases = offline.generate(taxonomy.load())
    c71 = _tax_case(cases, 71)
    assert c71.meta["text"] == "deeper shadows"
    assert "deepen_shadows" in c71.meta["allowed_keys"]
    c143 = _tax_case(cases, 143)
    assert c143.meta["text"] == "warmer"
    assert "warmer" in c143.meta["allowed_keys"]


def test_tax_allowed_keys_do_not_grow_for_a_phrase_with_no_synonym_match():
    """A taxonomy phrase that is NOT itself a vocabulary synonym must not
    gain a key just because some other phrase happens to resolve near it --
    a resolution to an unrelated capability still fails."""
    cases = offline.generate(taxonomy.load())
    tax = taxonomy.load()
    owner = tax.phrase_module()
    unresolved = [c for c in cases if c.id.startswith("off-tax-")
                 and owner.get(c.meta["text"]) not in offline.SERVED_MODULES]
    # every such case's allowed_keys came only from its own synonym match
    assert any(not c.meta["allowed_keys"] for c in unresolved)
