"""Session-wide isolation for the Scouter test suite.

A suite run once wrote a 1.7 MB test job note into Pratik's REAL Obsidian
vault because the shell exported OBSIDIAN_VAULT_PATH and a test relied on the
environment. Nothing under tests/ may ever reach the real vault, the real
Scouter config, the real cache, the real darktable config or the real bridge
spool, whatever the shell environment says. This module forces every one of
those doors to a throwaway directory before any test module imports.

The doors, and why each one is here:

- `OBSIDIAN_VAULT_PATH` -- the incident above.
- `SCOUTER_CACHE` -- events, metrics.db, the audit trail, the stats cache.
- `SCOUTER_CONFIG` -- the library root and the backup target live in it.
- `SCOUTER_DARKTABLE_LIBRARY_DB` -- never read or written, but pinned so a
  regression cannot start.
- `SCOUTER_DARKTABLE_CLI` -- `scout.cli_binary()` falls back to the real Mac
  install path, which EXISTS on this dev host whether or not a test installed
  a FakeBridge (task 11 fix round 2). Pinned to a path that cannot exist, so
  an attempt fails fast instead of quietly succeeding against production
  software. NOT pinned under `SCOUTER_HOST_TESTS=1`: host renders need the
  real binary (fix round 3).
- `XDG_CACHE_HOME` and `DARKTABLE_MCP_PLUGIN_PATH` -- the bridge's REQUEST
  SPOOL (`$XDG_CACHE_HOME/darktable-mcp`) and the installed Lua plugin (task
  14 fix round 1, blocker 4). Without these a unit test that forgot its
  FakeBridge reached the real spool: it would create `~/.cache/darktable-mcp`,
  write a request file into it, and -- if a darktable happened to be running
  with the plugin installed -- get a REAL ANSWER from the user's live
  darktable. A unit test must not be able to do that even by accident, and
  `tests/test_bridge.py::test_the_unit_suite_never_creates_the_real_bridge_spool`
  asserts the real path stays absent.
"""

from __future__ import annotations

import contextlib
import os
import tempfile

import pytest

#: Captured BEFORE anything is pinned, so the "never the real user paths"
#: guard below knows what the real vault actually was on this machine rather
#: than assuming `~/second-brain` (fix round 1, minor d).
_REAL_VAULT_FROM_ENV = os.environ.get("OBSIDIAN_VAULT_PATH") or ""
_REAL_XDG_CACHE_FROM_ENV = os.environ.get("XDG_CACHE_HOME") or ""

_SESSION_TMP = tempfile.mkdtemp(prefix="scouter-tests-")

#: The real bridge spool, computed from the pre-pin environment. Exported so a
#: test can assert it is never created.
REAL_BRIDGE_SPOOL = os.path.join(
    _REAL_XDG_CACHE_FROM_ENV or os.path.join(os.path.expanduser("~"), ".cache"),
    "darktable-mcp",
)
#: The real installed-plugin path, likewise.
REAL_PLUGIN_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "darktable", "lua", "darktable_mcp.lua"
)

_FORCED = {
    "OBSIDIAN_VAULT_PATH": os.path.join(_SESSION_TMP, "vault"),
    "SCOUTER_CACHE": os.path.join(_SESSION_TMP, "cache"),
    "SCOUTER_CONFIG": os.path.join(_SESSION_TMP, "config.json"),
    "SCOUTER_DARKTABLE_LIBRARY_DB": os.path.join(_SESSION_TMP, "library.db"),
    # Blocker 4: the bridge spool and the plugin path.
    "XDG_CACHE_HOME": os.path.join(_SESSION_TMP, "xdg-cache"),
    "DARKTABLE_MCP_PLUGIN_PATH": os.path.join(
        _SESSION_TMP, "no-such-darktable-config", "lua", "darktable_mcp.lua"
    ),
}
if os.environ.get("SCOUTER_HOST_TESTS") != "1":
    _FORCED["SCOUTER_DARKTABLE_CLI"] = os.path.join(_SESSION_TMP, "no-such-darktable-cli")
else:
    # A host run talks to a REAL darktable through the real spool and the
    # plugin it installs into the real config dir, so those two pins are
    # lifted -- exactly as `SCOUTER_DARKTABLE_CLI` already was. Everything
    # else (vault, config, cache, library.db) stays pinned even on a host run:
    # a host test that needs its own tree says so via `allow_tree`.
    del _FORCED["XDG_CACHE_HOME"]
    del _FORCED["DARKTABLE_MCP_PLUGIN_PATH"]

for _d in (_FORCED["OBSIDIAN_VAULT_PATH"], _FORCED["SCOUTER_CACHE"]):
    os.makedirs(_d, exist_ok=True)
if "XDG_CACHE_HOME" in _FORCED:
    os.makedirs(_FORCED["XDG_CACHE_HOME"], exist_ok=True)
for _k, _v in _FORCED.items():
    os.environ[_k] = _v


#: Extra directory prefixes the per-test re-assertion below will accept in
#: place of the session pin. A test that needs its OWN throwaway tree (the
#: phase-2 end-to-end host test builds a whole library root, backup target
#: and vault under one temp dir in `setUpClass`, before any of its test
#: methods run) registers it with `allow_tree`.
#:
#: This is deliberately an explicit, in-repo opt-in rather than "accept
#: anything that looks like a temp dir": the rule this module exists to
#: enforce is that no test can reach the REAL vault, config, cache, plugin or
#: spool, and only code that has said out loud which tree it is using gets to
#: widen it. A prefix that is not an absolute path, or that is one of the real
#: paths, is refused.
_ALLOWED_TREES: list[str] = []


def _real_forbidden() -> tuple[str, ...]:
    """The paths `allow_tree` must never accept.

    The vault comes from the environment as it was BEFORE the pin (fix round
    1, minor d): hard-coding `~/second-brain` was a guess about this machine,
    and a user whose `OBSIDIAN_VAULT_PATH` pointed elsewhere would have had
    the one path that caused the original incident left unguarded.
    """
    candidates = [
        "~/.config/scouter",
        "~/.cache/scouter",
        "~/.hermes",
        "~/second-brain",  # the default, still refused when the env is unset
        REAL_BRIDGE_SPOOL,
        REAL_PLUGIN_PATH,
        os.path.dirname(os.path.dirname(REAL_PLUGIN_PATH)),  # ~/.config/darktable
    ]
    if _REAL_VAULT_FROM_ENV:
        candidates.append(_REAL_VAULT_FROM_ENV)
    return tuple(os.path.realpath(os.path.expanduser(p)) for p in candidates)


_REAL_FORBIDDEN = _real_forbidden()


def _register(prefix: str) -> str:
    resolved = os.path.realpath(prefix)
    if not os.path.isabs(resolved):
        raise ValueError(f"allow_tree needs an absolute path, got {prefix!r}")
    for forbidden in _REAL_FORBIDDEN:
        if resolved == forbidden or resolved.startswith(forbidden + os.sep):
            raise ValueError(f"allow_tree refuses a real user path: {prefix!r}")
    if os.path.realpath(os.path.expanduser("~")) == resolved:
        raise ValueError("allow_tree refuses $HOME itself")
    _ALLOWED_TREES.append(resolved)
    return prefix


def allow_tree(prefix: str) -> str:
    """Register `prefix` as an acceptable home for this test's own pinned
    paths, for the rest of the session.

    Returns it, so a caller can write `tmp = allow_tree(mkdtemp())`. Prefer
    `allowed_tree` (the context manager) where the lifetime is a class or a
    test -- a permanently registered prefix keeps widening the guard for every
    test that runs afterwards.
    """
    return _register(prefix)


@contextlib.contextmanager
def allowed_tree(prefix: str):
    """`allow_tree` with a lifetime (fix round 1, minor d).

    The entry is removed on exit, so a host class that pins its own tree in
    `setUpClass` stops widening the isolation guard for every test that runs
    after it -- which is the whole point of the guard.
    """
    _register(prefix)
    resolved = os.path.realpath(prefix)
    try:
        yield prefix
    finally:
        with contextlib.suppress(ValueError):
            _ALLOWED_TREES.remove(resolved)


def _is_allowed(value: str) -> bool:
    """Is `value` inside the session tmp dir or a registered tree?

    Both sides are `realpath`-ed before comparing (fix round 2, minor m3).
    They were not: `value` was resolved and `_SESSION_TMP` was not, so the
    equality arm never matched on macOS -- `/var` is a symlink to
    `/private/var`, so the session root itself resolved to a different string
    than the one it was compared against. Only paths NESTED under it matched,
    via the `startswith` arm which did resolve its side. A test pinning an
    env var at the session root exactly would have been "not allowed" and
    silently re-pinned.
    """
    if not value:
        return False
    real = os.path.realpath(value)
    for prefix in (_SESSION_TMP, *_ALLOWED_TREES):
        resolved_prefix = os.path.realpath(prefix)
        if real == resolved_prefix or real.startswith(resolved_prefix + os.sep):
            return True
    return False


@pytest.fixture(autouse=True)
def _never_the_real_vault(monkeypatch):
    """Re-assert the isolation per test, so a test that pops an env var cannot
    fall back to the shell's real paths."""
    for k, v in _FORCED.items():
        if os.environ.get(k) != v and not _is_allowed(os.environ.get(k, "")):
            monkeypatch.setenv(k, v)
    yield


#: Fix round 2 (spool isolation): `test_the_unit_suite_never_creates_the_real_
#: bridge_spool` used to prove "nothing wrote to the real spool" by scanning
#: the real, SHARED spool directory for files newer than the session start.
#: That is not proof of what THIS suite did -- it is proof of what anyone did.
#: A live run (the htc gauntlet on the main checkout, `5501801`, talks to a
#: real darktable through this exact spool) writing its own request file
#: during the session made the scan flag it as if the unit suite had done it.
#:
#: Watching this PROCESS's own `os.rename` calls instead is immune to that: a
#: concurrent process's writes happen in a different interpreter and never go
#: through this module's patched `os.rename`, so they can never be recorded
#: here, while a genuine leak from this suite (a `Bridge()` built without a
#: cache_dir override, on a run where the `XDG_CACHE_HOME` pin was somehow not
#: honoured) still performs its atomic tmp+rename write through this exact
#: function and is still caught.
_WATCHED_ROOTS: dict[str, list[str]] = {}
_real_os_rename = os.rename


def _guarded_rename(src, dst, *args, **kwargs):
    try:
        real_dst = os.path.realpath(os.fspath(dst))
    except OSError:
        real_dst = os.fspath(dst)
    for root, sink in _WATCHED_ROOTS.items():
        if real_dst == root or real_dst.startswith(root + os.sep):
            sink.append(real_dst)
    return _real_os_rename(src, dst, *args, **kwargs)


os.rename = _guarded_rename


def watch_renames_into(root: str) -> list[str]:
    """Start recording every `os.rename` this process performs whose
    destination lands under `root`.

    Returns the (initially empty) list that is appended to live as writes
    happen -- inspect it directly, no polling needed. Call `stop_watching`
    with the same root when done, or it keeps recording for the rest of the
    session (harmless, but unbounded).
    """
    resolved = os.path.realpath(root)
    sink: list[str] = []
    _WATCHED_ROOTS[resolved] = sink
    return sink


def stop_watching(root: str) -> None:
    _WATCHED_ROOTS.pop(os.path.realpath(root), None)


#: Watch the real bridge spool for the whole session, not just inside one
#: assertion -- a leak should fail whichever test triggers it. Only on a
#: non-host run: a host run deliberately lifts the `XDG_CACHE_HOME` pin and
#: talks to the real spool on purpose.
REAL_SPOOL_WRITES: list[str] = (
    watch_renames_into(REAL_BRIDGE_SPOOL) if "XDG_CACHE_HOME" in _FORCED else []
)
