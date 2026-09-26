from scouter.core import phrases
from tests.gauntlet import taxonomy
from tests.gauntlet.layers import offline


def test_offline_count_ids_tier():
    cases = offline.generate(taxonomy.load())
    assert len(cases) == offline.EXPECTED == 3214
    assert len({c.id for c in cases}) == 3214
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


#: Pinned 2026-09-26 (item 3): the ONLY off-tax cases whose allowed_keys go
#: beyond their owning module's keys, and the one key each gains -- the
#: phrase is, verbatim, a synonym of that vocabulary key. The row 437 vocab
#: families (grain/vignette, lens, denoise/sharpen) mostly do NOT add
#: entries here: SERVED_MODULES now covers those modules directly, so a
#: taxonomy phrase filed under its own now-served module gets its key from
#: ownership, not from this list. off-tax-239 ("sharpen", filed under
#: `diffuse`) is the one genuine cross-module case.
GROWN = {
    "off-tax-071": ("deeper shadows", "deepen_shadows"),
    "off-tax-072": ("open the shadows", "lift_shadows"),
    "off-tax-076": ("faded blacks", "faded_blacks"),
    "off-tax-085": ("deeper blacks", "deeper_blacks"),
    "off-tax-087": ("softer contrast", "less_contrast"),
    "off-tax-091": ("more colour", "more_saturation"),
    "off-tax-092": ("desaturate", "less_saturation"),
    "off-tax-099": ("lift the blacks", "faded_blacks"),
    "off-tax-109": ("lift the shadows", "lift_shadows"),
    "off-tax-110": ("pull down the highlights", "tame_highlights"),
    "off-tax-117": ("matte blacks", "faded_blacks"),
    "off-tax-129": ("lift shadows", "lift_shadows"),
    "off-tax-130": ("recover highlights", "tame_highlights"),
    "off-tax-143": ("warmer", "warmer"),
    "off-tax-144": ("cooler", "cooler"),
    "off-tax-193": ("more saturation", "more_saturation"),
    "off-tax-194": ("less saturation", "less_saturation"),
    "off-tax-195": ("more saturated", "more_saturation"),
    "off-tax-196": ("less saturated", "less_saturation"),
    "off-tax-209": ("blue shadows", "cool_shadows"),
    "off-tax-239": ("sharpen", "sharpen_more"),
}


def test_tax_allowed_keys_do_not_grow_for_a_phrase_with_no_synonym_match():
    """A taxonomy phrase that is NOT itself a vocabulary synonym must not
    gain a key just because some other phrase happens to resolve near it --
    a resolution to an unrelated capability still fails. Per case, equality:
    allowed_keys is exactly the owning module's keys, plus the one pinned
    synonym key for the 21 GROWN cases and nothing for every other case."""
    tax = taxonomy.load()
    owner = tax.phrase_module()
    tax_cases = [c for c in offline.generate(tax) if c.id.startswith("off-tax-")]
    assert len(tax_cases) == 378
    for c in tax_cases:
        mod = owner[c.meta["text"]]
        want = set(offline._module_keys(mod) if mod in offline.SERVED_MODULES else [])
        if c.id in GROWN:
            text, key = GROWN[c.id]
            assert c.meta["text"] == text
            # the pinned key really is a synonym of that vocabulary row
            assert any(p.key == key and text in p.synonyms for p in phrases.PHRASES)
            want.add(key)
        assert c.meta["allowed_keys"] == sorted(want), c.id


def test_served_modules_cover_every_taxonomy_named_phrase_module():
    """The bug behind off-tax-235/I1: `SERVED_MODULES` is a hand-maintained
    list, and a new vocabulary family (row 437's grain/vignette, lens, or
    denoise/sharpen) can add real phrase-move modules the taxonomy already
    names without anyone updating it -- so the taxonomy-ownership oracle
    silently stays blind to a module it should already recognise. Every
    module a phrase actually moves (`@tone` excepted: it is a resolver
    fiction for `sigmoid`/`filmicrgb`, never a taxonomy module id) that the
    taxonomy ALSO names must be in `SERVED_MODULES`."""
    tax = taxonomy.load()
    taxonomy_module_ids = {m["id"] for m in tax.modules}
    phrase_modules = {mv.module for p in phrases.PHRASES for mv in p.moves} - {"@tone"}
    missing = (phrase_modules & taxonomy_module_ids) - offline.SERVED_MODULES
    assert missing == set(), f"SERVED_MODULES is stale: missing {sorted(missing)}"
