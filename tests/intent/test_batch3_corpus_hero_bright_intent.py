"""Runs tests/intent/batch3_corpus_hero_bright.json (Zenkai Rules Z1; format
pending t_450535e2). Two independent observations per example (Z2): the raw
JSON text still on disk after `Corpus.load` (oracle A, proves the merge
never rewrites the cache file) against the loaded `Corpus.doc` object
(oracle B, what the rest of the harness actually reads) -- neither oracle is
derived from the other."""

from __future__ import annotations

import json
from pathlib import Path

from tests.gauntlet import corpus

EXAMPLES = {
    e["id"]: e
    for e in json.loads((Path(__file__).parent / "batch3_corpus_hero_bright.json").read_text())[
        "examples"
    ]
}


def test_stale_cache_predating_hero_bright_loads_with_it_present(tmp_path):
    EXAMPLES["stale-cache-predating-hero-bright-loads-with-it-present"]  # documents the case
    doc = {
        "schema": "scouter-gauntlet-corpus/1",
        "frames": [],
        "roles": {
            "olympus": {
                "hero": "P6281348.ORF",
                "dark": "P5240151.ORF",
                "bright": "P6281368.ORF",
                "blown": "P5240140.ORF",
            }
        },
    }
    raw = json.dumps(doc)
    path = tmp_path / "corpus.json"
    path.write_text(raw, encoding="utf-8")

    loaded = corpus.Corpus.load(path)

    # Oracle A: the cache file on disk is untouched by the load.
    assert path.read_text(encoding="utf-8") == raw
    assert "hero_bright" not in json.loads(raw)["roles"]["olympus"]
    # Oracle B: the loaded object the harness actually reads has the role.
    assert (
        loaded.doc["roles"]["olympus"]["hero_bright"]
        == corpus.EXTRA_ROLES["olympus"]["hero_bright"]
    )
    assert (
        loaded.roles("olympus")["hero_bright"]
        == loaded.root / corpus.EXTRA_ROLES["olympus"]["hero_bright"]
    )


def test_cache_with_its_own_hero_bright_is_not_overwritten(tmp_path):
    EXAMPLES["cache-with-its-own-hero-bright-is-not-overwritten"]  # documents the case
    doc = {
        "schema": "scouter-gauntlet-corpus/1",
        "frames": [],
        "roles": {"olympus": {"hero_bright": "/already/surveyed/copy.ORF"}},
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    loaded = corpus.Corpus.load(path)

    # Oracle A: EXTRA_ROLES itself (the code's own override table) still
    # names the row 458 copy -- it was never mutated by the load.
    assert corpus.EXTRA_ROLES["olympus"]["hero_bright"] != "/already/surveyed/copy.ORF"
    # Oracle B: the loaded doc kept the cache's own value.
    assert loaded.doc["roles"]["olympus"]["hero_bright"] == "/already/surveyed/copy.ORF"
