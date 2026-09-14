"""Session-wide isolation for the Scouter test suite.

A suite run once wrote a 1.7 MB test job note into Pratik's REAL Obsidian
vault because the shell exported OBSIDIAN_VAULT_PATH and a test relied on the
environment. Nothing under tests/ may ever reach the real vault, the real
Scouter config, the real cache or the real darktable config, whatever the
shell environment says. This fixture forces every one of those doors to a
throwaway directory before any test module imports.
"""

from __future__ import annotations

import os
import tempfile

import pytest

_SESSION_TMP = tempfile.mkdtemp(prefix="scouter-tests-")
_FORCED = {
    "OBSIDIAN_VAULT_PATH": os.path.join(_SESSION_TMP, "vault"),
    "SCOUTER_CACHE": os.path.join(_SESSION_TMP, "cache"),
    "SCOUTER_CONFIG": os.path.join(_SESSION_TMP, "config.json"),
    "SCOUTER_DARKTABLE_LIBRARY_DB": os.path.join(_SESSION_TMP, "library.db"),
    # Task 11 fix round 2, IMPORTANT 1: `scouter.core.scout.cli_binary()`
    # falls back to the real Mac install path
    # (/Applications/darktable.app/.../darktable-cli) when this is unset --
    # which EXISTS on this dev host regardless of whether a test installed a
    # FakeBridge. A non-host unit test must never be able to shell out to
    # the real binary; pointing this at a path that cannot exist (nested
    # under the throwaway session tmp dir, never created) makes every such
    # attempt fail fast and locally instead of silently succeeding against
    # production software.
}
# Task 11 fix round 3 (re-review of 815dcc4): the CLI pin must not leak into
# SCOUTER_HOST_TESTS=1 runs - host renders need the real binary and the real
# calibrate must record the real darktable version, not "unknown".
if os.environ.get("SCOUTER_HOST_TESTS") != "1":
    _FORCED["SCOUTER_DARKTABLE_CLI"] = os.path.join(_SESSION_TMP, "no-such-darktable-cli")
for _d in (_FORCED["OBSIDIAN_VAULT_PATH"], _FORCED["SCOUTER_CACHE"]):
    os.makedirs(_d, exist_ok=True)
for _k, _v in _FORCED.items():
    os.environ[_k] = _v


@pytest.fixture(autouse=True)
def _never_the_real_vault(monkeypatch):
    """Re-assert the isolation per test, so a test that pops an env var cannot
    fall back to the shell's real paths."""
    for k, v in _FORCED.items():
        if os.environ.get(k) != v and not os.environ.get(k, "").startswith(_SESSION_TMP):
            monkeypatch.setenv(k, v)
    yield
