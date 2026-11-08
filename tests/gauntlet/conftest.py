"""Shared fixtures for the Scouter gauntlet adapter tests."""

import shutil

import pytest


@pytest.fixture(autouse=True)
def _no_real_wb_tables(tmp_path, monkeypatch):
    """Safety: session.stage() copies WB tables from HTC_WB_TABLES (default: the
    real ~/.config/scouter/wb); point it at an empty tmp dir so no adapter test
    ever reads the user's config."""
    monkeypatch.setenv("HTC_WB_TABLES", str(tmp_path / "no-wb-tables"))


@pytest.fixture(autouse=True)
def _ample_free_disk(monkeypatch):
    """(t_90e75c20 fix round 1, I3): same rationale as gauntlet/tests/
    conftest.py -- a Runner run through ScouterAdapter (e.g.
    test_bulk_disk_retention.py) must never depend on the host's real free
    disk. Only `free` is inflated; `.total`/`.used` pass through."""
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real(path)._replace(free=10**15))


@pytest.fixture
def route_a_unpinned(monkeypatch):
    """The pre-promotion world (route A's action path not pinned, before the
    2026-11-08 host probe): the generators emit the typed refusal for warmth on
    a preset-4 hero. Tests that pin that shape ask for this fixture; the
    pinned world is the default and is covered by the pinned-edit tests."""
    from tests.gauntlet import checkmap

    monkeypatch.setattr(checkmap, "route_a_pinned", lambda cmap: False)
