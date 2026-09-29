"""Shared fixtures for the generic engine's own test suite."""

import shutil

import pytest


@pytest.fixture(autouse=True)
def _ample_free_disk(monkeypatch):
    """(t_90e75c20 fix round 1, I3): no test here may depend on the HOST's
    real free disk -- this card exists to keep the suite green DURING a real
    disk incident. Patches `shutil.disk_usage` itself (not `gauntlet.runner.
    free_disk_gb`, so `test_free_disk_gb_default_matches_shutil_disk_usage`
    keeps comparing the real function against itself and needs no change);
    only `free` is inflated, so callers that use `.total`/`.used` are
    unaffected. A test that wants a specific reading injects
    `Runner(..., free_disk_gb=...)` explicitly, which always wins, or
    monkeypatches `shutil.disk_usage` again itself (the later patch wins)."""
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real(path)._replace(free=10**15))
