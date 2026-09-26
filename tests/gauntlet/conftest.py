"""Shared fixtures for the Scouter gauntlet adapter tests."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_wb_tables(tmp_path, monkeypatch):
    """Safety: session.stage() copies WB tables from HTC_WB_TABLES (default: the
    real ~/.config/scouter/wb); point it at an empty tmp dir so no adapter test
    ever reads the user's config."""
    monkeypatch.setenv("HTC_WB_TABLES", str(tmp_path / "no-wb-tables"))
