# ruff: noqa: N806
import json
import os

import pytest

from tests.gauntlet import corpus


@pytest.fixture
def trial(tmp_path, monkeypatch):
    root = tmp_path / "EditingTrial"
    (root / "sony").mkdir(parents=True)
    for n in ("P1.ORF", "P2.ORF", "P3.ORF"):
        (root / n).write_bytes(n.encode() * 10)
    (root / "sony" / "DSC1.ARW").write_bytes(b"a7" * 10)
    (root / "P9.JPG").write_bytes(b"jpg")
    monkeypatch.setattr(corpus, "TRIAL", root)
    return root


def test_guard_refuses_originals_and_symlinks(trial, tmp_path):
    with pytest.raises(corpus.OriginalPathRefused):
        corpus.guard(trial / "P1.ORF")
    link = tmp_path / "link.ORF"
    os.symlink(trial / "P1.ORF", link)
    with pytest.raises(corpus.OriginalPathRefused):
        corpus.guard(link)
    assert corpus.guard(tmp_path / "copy.ORF")


def test_copy_is_0644_and_original_untouched(trial, tmp_path):
    before = corpus.sha256_originals(trial)
    dst = corpus.copy_readonly(trial / "P1.ORF", tmp_path / "out" / "P1.ORF")
    assert oct(dst.stat().st_mode & 0o777) == "0o644"
    with pytest.raises(corpus.OriginalPathRefused):
        corpus.copy_readonly(tmp_path / "out" / "P1.ORF", trial / "P1-copy.ORF")
    assert corpus.sha256_originals(trial) == before and len(before) == 4


def test_survey_roles_and_bench_override(trial, tmp_path):
    stats = {
        "P1.ORF": (20, 40, 200, 0.0, 1.3, 0.8),
        "P2.ORF": (60, 20, 250, 0.5, 0.9, 1.1),
        "P3.ORF": (45, 60, 230, 0.001, 1.0, 1.0),
        "sony/DSC1.ARW": (50, 50, 220, 0, 1, 1),
    }

    def render(p):
        rel = str(p.relative_to(tmp_path / "work"))
        L, sp, p99, clip, rg, bg = stats[rel]
        return {"L_mean": L, "spread": sp, "p99": p99, "clip_high": clip, "rg": rg, "bg": bg}

    def exif(p):
        return (
            ("ILCE-7", 100)
            if p.suffix == ".ARW"
            else ("E-M10 Mark III", 200 if "P2" in p.name else 6400)
        )

    frames = corpus.survey(render, exif, tmp_path / "work", trial)
    assert not list((tmp_path / "work").rglob("*.ORF"))  # survey copies are deleted
    roles = corpus.pick_roles(frames)
    assert roles["olympus"]["dark"] == "P1.ORF" and roles["olympus"]["blown"] == "P2.ORF"
    assert roles["olympus"]["flat"] == "P2.ORF" and roles["olympus"]["noisy"] == "P1.ORF"
    assert roles["sony_a7"]["hero"] == "sony/DSC1.ARW"


@pytest.mark.skipif(
    not all(
        os.path.exists(path) for roles in corpus.EXTRA_ROLES.values() for path in roles.values()
    ),
    reason=(
        "needs the local photo corpus copies under ~/Pictures/ScouterScratch/gauntlet "
        "(corpus.EXTRA_ROLES, e.g. frames-bright/P5240141.ORF); absent on CI runners and "
        "fresh clones, where verify_extra_roles() can only report 'missing'"
    ),
)
def test_hero_bright_extra_role_is_injected_and_copy_sha_matches():
    """Cluster D / row 458: hero_bright is an olympus-only role pointing at a
    COPY outside EditingTrial (corpus.EXTRA_ROLES), and its sha256 must match
    what was recorded when the copy was made (corpus.EXTRA_ROLE_SHA256)."""
    roles = corpus.pick_roles([])
    assert roles["olympus"]["hero_bright"] == str(corpus.SCRATCH / "frames-bright" / "P5240141.ORF")
    mismatches = corpus.verify_extra_roles()
    assert mismatches == {}, mismatches


def test_card_duplication_round_robin_and_disk_check(trial, tmp_path, monkeypatch):
    doc = {
        "frames": [{"rel": "P1.ORF", "body": "olympus"}, {"rel": "P2.ORF", "body": "olympus"}],
        "roles": {},
    }
    c = corpus.Corpus(doc, trial)
    files = corpus.build_card(c, "olympus", 5, tmp_path / "card")
    assert [f.name for f in files] == [
        "DUP0000_P1.ORF",
        "DUP0001_P2.ORF",
        "DUP0002_P1.ORF",
        "DUP0003_P2.ORF",
        "DUP0004_P1.ORF",
    ]
    monkeypatch.setattr(corpus, "free_space_ok", lambda p, n: False)
    with pytest.raises(OSError, match="free disk"):
        corpus.build_card(c, "olympus", 120, tmp_path / "card2")


def test_load_backfills_hero_bright_into_a_stale_cached_corpus_json(tmp_path):
    """Batch 3 / night-2 triage C1 (row 462): a corpus.json written before
    EXTRA_ROLES gained `hero_bright` has no `roles.olympus.hero_bright` key
    at all -- `Corpus.load` must merge it in so `corpus.roles("olympus")`
    (and the `[role]` lookups in tests/gauntlet/session.py) never raise
    `KeyError: 'hero_bright'` against a cache that predates row 462."""
    stale = {
        "schema": "scouter-gauntlet-corpus/1",
        "frames": [],
        "roles": {"olympus": {"hero": "P6281348.ORF", "dark": "P5240151.ORF"}},
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(stale), encoding="utf-8")

    loaded = corpus.Corpus.load(path)

    assert "hero_bright" not in stale["roles"]["olympus"]  # oracle A: the file on disk, untouched
    assert (
        loaded.doc["roles"]["olympus"]["hero_bright"]
        == corpus.EXTRA_ROLES["olympus"]["hero_bright"]
    )  # oracle B: the loaded object has it
    assert loaded.doc["roles"]["olympus"]["hero"] == "P6281348.ORF"  # existing roles untouched


def test_load_does_not_override_a_roles_entry_the_cache_already_has(tmp_path):
    """A cache that already carries its own hero_bright (a fresh survey, or
    one written after row 462) keeps its own value; the merge never
    clobbers what the cache says."""
    doc = {
        "schema": "scouter-gauntlet-corpus/1",
        "frames": [],
        "roles": {"olympus": {"hero_bright": "/already/surveyed/copy.ORF"}},
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    loaded = corpus.Corpus.load(path)

    assert loaded.doc["roles"]["olympus"]["hero_bright"] == "/already/surveyed/copy.ORF"
