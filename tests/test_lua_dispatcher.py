"""Pytest wrapper that runs the Lua dispatcher tests via a system lua interpreter.

The Lua half of the bridge is a real program with real bugs, so these tests
have to actually execute. A skipped test file reads as green and proves
nothing, so set ``DARKTABLE_MCP_REQUIRE_LUA=1`` (do this in CI) to turn the
"no interpreter" skip into a hard failure.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

#: Interpreters we accept, best first. Distros disagree on the binary name:
#: Homebrew and Arch ship `lua`, Debian/Ubuntu ship `lua5.4` / `lua5.3`.
LUA_CANDIDATES = ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit")

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "darktable_mcp" / "lua" / "darktable_mcp.lua"


def _find_lua() -> str | None:
    for name in LUA_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


LUA_BIN = _find_lua()

#: Opt-in strictness: without an interpreter the suite proves nothing, so a
#: CI job that means to gate the Lua side should refuse to let it skip.
REQUIRE_LUA = os.environ.get("DARKTABLE_MCP_REQUIRE_LUA") == "1"

_skip_no_lua = pytest.mark.skipif(
    LUA_BIN is None and not REQUIRE_LUA,
    reason=(
        "no lua interpreter found (tried: "
        + ", ".join(LUA_CANDIDATES)
        + "). Install one (brew install lua / apt install lua5.4), or set "
        "DARKTABLE_MCP_REQUIRE_LUA=1 to make this a failure."
    ),
)


@_skip_no_lua
def test_lua_dispatcher():
    """The Lua dispatcher unit suite must run and pass, not skip."""
    assert LUA_BIN is not None, (
        "DARKTABLE_MCP_REQUIRE_LUA=1 but no lua interpreter is installed "
        f"(tried: {', '.join(LUA_CANDIDATES)})"
    )
    result = subprocess.run(
        [LUA_BIN, "tests/lua/test_dispatcher.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Lua tests failed under {LUA_BIN}.\n" f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Guard against a suite that exits 0 without having asserted anything
    # (e.g. an early `return` or a load failure swallowed somewhere).
    assert "OK: all dispatcher tests passed" in result.stdout, (
        "Lua suite exited 0 but never printed its success line -- it did not "
        f"run to completion.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # The harness writes to stderr only on failure.
    assert result.stderr.strip() == "", f"unexpected Lua stderr: {result.stderr}"


# ---- Static guards on the plugin source ------------------------------------
# These do not need an interpreter, so they run everywhere.

_SHELL_CALL = re.compile(r"\b(?:io\.popen|os\.execute)\s*\(")


def test_every_shell_invocation_is_quoted():
    """No shell command may interpolate a path without shell_quote().

    cache_dir() derives from $HOME / $XDG_CACHE_HOME. Interpolating either
    raw (the old `'ls -1 "' .. dir .. '"'`) breaks on a double quote and
    executes attacker-chosen text on a backtick or $(...).
    """
    source = PLUGIN.read_text(encoding="utf-8")
    offenders = []
    for match in _SHELL_CALL.finditer(source):
        # A single os.execute(string.format(...)) can span several lines, so
        # look at a window rather than one line.
        window = source[match.start() : match.start() + 500]
        # A command built with NO Lua concatenation and no format call cannot
        # interpolate anything: it is a constant string (e.g. `uname -s`), so
        # there is nothing for shell_quote() to quote. Only the argument list
        # itself is inspected for that, not the 500-char window, so a nearby
        # unrelated concatenation cannot excuse a real offender.
        args = window[window.index("(") + 1 : window.find(")") if ")" in window else None]
        interpolates = ".." in args or "format" in args
        if interpolates and "shell_quote(" not in window:
            line_no = source[: match.start()].count("\n") + 1
            offenders.append(f"line {line_no}: {match.group(0)}")
    assert not offenders, (
        "shell invocation(s) in darktable_mcp.lua do not route their "
        "interpolated path through shell_quote(): " + "; ".join(offenders)
    )


def test_plugin_has_no_flat_poll_loop():
    """The worker must sleep an adaptive interval, not a hardcoded 100ms.

    A flat `dt.control.sleep(100)` in the worker forks ~36,000 `ls`
    subprocesses per hour for the whole darktable session, idle or not.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    # Look at the CALL SITE, not the definition -- `local function
    # poll_interval_ms(idle_ticks)` would satisfy a naive substring check even
    # with the worker back on a hardcoded sleep.
    assert "sleep(poll_interval_ms(" in source, (
        "worker_loop no longer sleeps poll_interval_ms(...); the adaptive "
        "backoff has been removed, renamed, or unwired from the loop"
    )
    worker = source.split("local function worker_loop", 1)
    assert len(worker) == 2, "worker_loop not found in the plugin"
    body = worker[1]
    assert "dt.control.sleep(100)" not in body, "worker_loop still contains a hardcoded 100ms sleep"


def test_import_batch_does_not_echo_an_unhonoured_recursive_flag():
    """`recursive` must drive behaviour, not just be echoed back.

    It used to be read, defaulted to true, and returned in the response
    without ever being used -- so with darktable's recurse_directories
    preference off, importing an import_from_camera destination registered
    nothing while the tool reported success with ``recursive: true``.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("methods.import_batch = function", 1)
    assert len(body) == 2, "methods.import_batch not found in the plugin"
    body = body[1].split("\nmethods.", 1)[0]

    assert "list_directories_recursive(source_path)" in body, (
        "import_batch does not enumerate the tree, so `recursive` is not "
        "honoured -- recursion would silently fall back to the user's "
        "darktable preference"
    )
    assert "recursive_honoured" in body, (
        "import_batch does not report whether the requested recursion mode " "actually happened"
    )
    # import_batch must not rewrite the user's recursion preference: it has an
    # alternative (enumerate the tree itself) and takes it. The plugin-wide
    # version of this rule is test_the_plugin_never_writes_a_darktable_preference.
    assert "preferences.write" not in body, (
        "import_batch writes a darktable preference; flipping a persistent "
        "user setting from the background worker is its own bug, and "
        "import_batch does not need to -- it enumerates the tree instead"
    )


def test_loaded_darkroom_image_never_calls_the_display_image_getter():
    """The no-arg display_image() getter deadlocks the worker. Permanently.

    Host-proven with a per-tick heartbeat file (task 12 fix round 1,
    darktable 5.4.1): the first `darkroom.current` of a session freezes with
    the heartbeat reading "display_image getter -- about to call" and never
    advances -- not in 4s, not in 72s. The worker coroutine runs on
    darktable's own main thread, so it blocks the thread that would have to
    unblock it, and every later bridge request times out behind it. That, not
    a busy pixel pipeline, is what task 12 recorded as Finding 2.

    `pcall` does not rescue a block, so the only safe rule is: never call it.
    The one-argument SETTER is a different call and stays allowed.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("local function loaded_darkroom_image", 1)
    assert len(body) == 2, "loaded_darkroom_image not found in the plugin"
    body = body[1].split("\nend", 1)[0]
    assert "display_image" not in body, (
        "loaded_darkroom_image mentions display_image -- the no-arg getter "
        "deadlocks the worker coroutine for the rest of the session"
    )
    assert (
        "action_images" in body
    ), "loaded_darkroom_image must read the selection via dt.gui.action_images"
    # Nowhere in the plugin may display_image be called without an argument.
    # (Comment lines are excluded: the plugin documents the banned call by
    # name, which is the point of the comment.)
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
    for banned in ("pcall(view.display_image)", "pcall(v.display_image)", "display_image()"):
        assert banned not in code, f"the plugin calls the display_image getter: {banned}"


def test_the_plugin_never_writes_a_darktable_preference():
    """No persistent user preference is rewritten, anywhere in the plugin.

    Task 12 fix round 1 briefly set darktable's `write_sidecar_files` from
    `history.commit`, believing that preference gated whether a commit could
    land. Two host facts killed it: the Lua preferences API writes a
    SCRIPT-namespaced key (`lua/darktable/write_sidecar_files` appeared in
    darktablerc) and never touches the core setting, so the override was
    inert -- and the commits it appeared to fix were in fact fixed by the
    view-switch repair. All it actually did was litter the user's config.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
    assert "preferences.write" not in code, (
        "the plugin writes a darktable preference; it is inert for core keys "
        "and leaves a stray lua/<script>/<key> entry in the user's darktablerc"
    )


def test_history_commit_confirms_the_sidecar_on_disk():
    """A commit must report what actually reached disk, and why if it didn't."""
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split('methods["history.commit"] = function', 1)
    assert len(body) == 2, "history.commit not found in the plugin"
    body = body[1].split("\nmethods.", 1)[0].split('\nmethods["', 1)[0]

    assert "wait_for_sidecar(sidecar, before)" in body, (
        "history.commit does not watch the sidecar, so it can only report "
        "that the views moved -- which is not a commit"
    )
    assert "COMMIT_BOUNCE_ATTEMPTS" in body, (
        "history.commit does not retry the bounce; the FIRST commit of a "
        "session can land mid-render and flush nothing"
    )
    assert (
        "pcall(commit_view_bounce)" in body
    ), "the bounce must run under pcall so the loop can stop cleanly"
    for field in ("sidecar_written", "sidecar_wait_ms", "sidecar ="):
        assert field in body, f"history.commit does not report {field}"


def test_stale_age_outlasts_every_client_timeout():
    """The sweep must never delete a request whose caller is still waiting.

    The Lua worker is single-threaded, so a request can sit queued behind a
    call that legitimately runs for its full budget. With STALE_AGE_SECONDS at
    60 -- shorter than the 120s ``import_batch`` / ``apply_preset`` budgets --
    the sweep deleted the queued ``request-*.json`` before the worker ever got
    to it, and its caller waited out the whole 120s to be told "darktable is
    not running".

    This is the only place the two halves of the invariant are compared, since
    the numbers live in different languages.
    """
    from darktable_mcp.bridge.client import DEFAULT_TIMEOUTS, FALLBACK_TIMEOUT

    source = PLUGIN.read_text(encoding="utf-8")
    match = re.search(r"^local STALE_AGE_SECONDS\s*=\s*(\d+)", source, re.MULTILINE)
    assert match is not None, "STALE_AGE_SECONDS not found in the plugin"
    stale_age = int(match.group(1))

    max_client_timeout = max([*DEFAULT_TIMEOUTS.values(), FALLBACK_TIMEOUT])
    assert stale_age > 120, (
        f"STALE_AGE_SECONDS is {stale_age}s; it must exceed the 120s client "
        "budgets or the sweep deletes live requests"
    )
    assert stale_age > max_client_timeout, (
        f"STALE_AGE_SECONDS ({stale_age}s) must exceed the largest client "
        f"budget in DEFAULT_TIMEOUTS ({max_client_timeout}s). Raise the stale "
        "age, or the sweep will delete requests whose callers are still "
        "waiting for them."
    )
    # sweep_stale floors the age to whole minutes for `find -mmin`, so check
    # the value that actually reaches find, not the constant.
    assert (stale_age // 60) * 60 > max_client_timeout, (
        f"STALE_AGE_SECONDS ({stale_age}s) rounds down to "
        f"{(stale_age // 60) * 60}s for find -mmin, which no longer clears the "
        f"{max_client_timeout}s client budget"
    )
    # The comment is the only thing that will warn whoever edits either number.
    assert "DEFAULT_TIMEOUTS" in source, (
        "the STALE_AGE_SECONDS invariant no longer names DEFAULT_TIMEOUTS as "
        "the thing it must stay ahead of"
    )


SCOUTER_METHODS = (
    "view.current",
    "image.lookup",
    "darkroom.open",
    "darkroom.action",
    "darkroom.status",
    "module.enable",
    "module.disable",
    "module.reset",
    "history.commit",
    "snapshot.take",
    "style.create_from_current",
    "style.apply",
    "image.rate",
    "style.export_file",
    "style.import_file",
    "style.delete",
)

#: Matches both `methods["dotted.name"] = ...` and `methods.bare_name = ...`
#: registrations in the plugin source.
_METHOD_REGISTRATION = re.compile(r'methods\["([\w.]+)"\]\s*=|methods\.(\w+)\s*=')


def _registered_method_names(source: str) -> set[str]:
    names: set[str] = set()
    for match in _METHOD_REGISTRATION.finditer(source):
        names.add(match.group(1) or match.group(2))
    return names


def test_plugin_registers_every_scouter_method():
    """The plugin's registry must contain exactly the methods the client knows.

    Ruling 13 requires two independent checks, since either alone can lie:
    a static check that cannot see whether the assignment actually runs (a
    dead branch, a typo'd table name), and a dynamic check that cannot see
    whether some *other* method got left out of the client's timeout table.
    """
    from darktable_mcp.bridge.client import DEFAULT_TIMEOUTS

    # (1) Static: every SCOUTER_METHODS name is actually assigned into the
    # `methods` table in the plugin source (not just mentioned in a comment).
    source = PLUGIN.read_text(encoding="utf-8")
    registered = _registered_method_names(source)
    missing_from_source = [m for m in SCOUTER_METHODS if m not in registered]
    assert not missing_from_source, (
        f"{missing_from_source} not found as `methods[...] = ` assignments in " f"{PLUGIN}"
    )

    # (2) Dynamic: run the existing tests/lua harness, which stubs `dt` and
    # asserts (in tests/lua/test_dispatcher.lua's "registry completeness"
    # block) that plugin.methods[name] is actually a function for every one
    # of these names -- not merely present in the source text.
    if LUA_BIN is None:
        pytest.skip(
            "no lua interpreter found; the dynamic half of this check runs "
            "inside test_lua_dispatcher, which is skipped for the same reason"
        )
    result = subprocess.run(
        [LUA_BIN, "tests/lua/test_dispatcher.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0 and "OK: all dispatcher tests passed" in result.stdout, (
        "the tests/lua harness (which asserts plugin.methods registers every "
        "Scouter method) did not pass:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # (3) Every registered Scouter method needs an explicit client budget, or
    # it silently falls back to FALLBACK_TIMEOUT.
    for method in SCOUTER_METHODS:
        assert method in DEFAULT_TIMEOUTS, (
            f"{method} has no entry in DEFAULT_TIMEOUTS, so it would fall back to "
            "the 15s FALLBACK_TIMEOUT; add an explicit budget"
        )
    assert max(DEFAULT_TIMEOUTS.values()) < 300, (
        "STALE_AGE_SECONDS (300) in the Lua plugin must stay above every client "
        "budget, or the sweep deletes live requests"
    )


def test_history_commit_auto_does_not_reach_the_sidecar_ladder():
    """strategy="auto" must go straight to view_bounce (spec section 8.1).

    Task 1's measurement rejected every sidecar-write action spelling on this
    darktable build, so `history.commit` no longer tries them under "auto" --
    the ladder is reachable only via an explicit strategy="sidecar_action".
    Fix round 1 regression: the original code gated the sidecar ladder behind
    `strategy == "auto" or strategy == "sidecar_action"`.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split('methods["history.commit"] = function', 1)
    assert len(body) == 2, 'methods["history.commit"] not found in the plugin'
    body = body[1].split("\nmethods[", 1)[0]
    assert 'strategy == "auto" or strategy == "sidecar_action"' not in body, (
        'history.commit still gates the sidecar ladder behind "auto" -- auto '
        "must use only the proven view_bounce floor"
    )
    assert 'if strategy == "sidecar_action" then' in body, (
        "the sidecar ladder is no longer reachable via an explicit "
        'strategy="sidecar_action" request'
    )


def test_history_commit_reports_cpu_ms_not_ms():
    """The CPU-time payload field must be named `cpu_ms`, from os.clock(),
    everywhere history.commit returns it -- never the ambiguous `ms`, and
    never os.time() (whole-second resolution, so a sub-second commit always
    measured as 0ms or 1000ms). docs/measurements.md itself uses os.clock()
    and documents it as CPU time, not wall clock. Fix round 2 should-fix 3.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split('methods["history.commit"] = function', 1)
    assert len(body) == 2, 'methods["history.commit"] not found in the plugin'
    body = body[1].split("\nmethods[", 1)[0]
    assert "os.clock()" in body, "history.commit no longer times itself with os.clock()"
    assert "os.time()" not in body, "history.commit must not time itself with os.time()"
    assert body.count("cpu_ms = (os.clock()") == 3, (
        "history.commit must report cpu_ms = (os.clock() - started) * 1000 on "
        "all three return paths (sidecar success, sidecar failure, view_bounce)"
    )
    assert re.search(r"(?<!cpu_)\bms\s*=", body) is None, (
        "history.commit still returns the old ambiguous `ms` field alongside "
        "or instead of cpu_ms"
    )


def test_history_commit_rejects_unknown_strategy():
    """An unknown `strategy` must error, listing the valid ones, rather than
    silently falling back to auto. Fix round 2 should-fix 7."""
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split('methods["history.commit"] = function', 1)
    assert len(body) == 2, 'methods["history.commit"] not found in the plugin'
    body = body[1].split("\nmethods[", 1)[0]
    assert (
        "unknown strategy" in body
    ), "history.commit no longer rejects an unrecognised strategy value"
    for valid in ('"auto"', '"view_bounce"', '"sidecar_action"'):
        assert valid in body, f"unknown-strategy error must list {valid} as valid"


@_skip_no_lua
def test_ids_match_behaves_correctly():
    """`nil == nil` is true in Lua, so a naive tonumber(a) == tonumber(b)
    would treat "neither side has an id" as a match. Fix round 2 should-fix 1;
    fix round 3 replaces the static substring check below (which only proved
    the guard clause was PRESENT in the source, not that it actually behaves
    correctly) with the real thing: running the Lua harness, whose
    tests/lua/test_dispatcher.lua asserts plugin.ids_match's exported
    behaviour directly against (nil,nil)->false, (nil,5)->false, (5,nil)
    ->false, ("x","x")->false, (5,"5")->true, (5,5)->true.
    """
    assert LUA_BIN is not None
    result = subprocess.run(
        [LUA_BIN, "tests/lua/test_dispatcher.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0 and "OK: all dispatcher tests passed" in result.stdout, (
        "the tests/lua harness (which directly unit-tests plugin.ids_match) "
        f"did not pass:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Secondary, still-useful static check: darkroom.open must actually USE
    # ids_match, not the old bare unguarded tonumber(...) == tonumber(...).
    source = PLUGIN.read_text(encoding="utf-8")
    open_body = source.split('methods["darkroom.open"] = function', 1)[1].split("\nmethods[", 1)[0]
    assert "tonumber(loaded.id) == tonumber(image.id)" not in open_body, (
        "darkroom.open's already-open check reverted to the unguarded id " "comparison"
    )
    assert "tonumber(final_image.id) == tonumber(image.id)" not in open_body, (
        "darkroom.open's post-switch verification reverted to the unguarded " "id comparison"
    )
    assert open_body.count("ids_match(") == 2, (
        "darkroom.open should call ids_match(...) exactly twice: the "
        "already-open check and the post-switch verification"
    )


def test_darkroom_open_wrong_image_is_an_rpc_error():
    """A wrong-image outcome must be a JSON-RPC error (error(...)), not a
    {opened=false, error=...} result a caller could ignore. Fix round 2
    should-fix 4."""
    source = PLUGIN.read_text(encoding="utf-8")
    open_body = source.split('methods["darkroom.open"] = function', 1)[1].split("\nmethods[", 1)[0]
    assert (
        'error("wrong image loaded: "' in open_body
    ), "darkroom.open no longer raises on a wrong-image outcome"
    assert "opened = false," not in open_body, (
        "darkroom.open still builds an {opened=false, ...} result table for "
        "the wrong-image case instead of erroring"
    )


def test_switch_to_darkroom_selects_before_display_image():
    """dt.gui.selection must run BEFORE display_image is attempted, so the
    action_images fallback in loaded_darkroom_image is never stale if
    display_image's own getter turns out to be unreliable. Fix round 2
    should-fix 2."""
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("local function switch_to_darkroom(", 1)
    assert len(body) == 2, "switch_to_darkroom not found in the plugin"
    body = body[1].split("\nend", 1)[0]
    selection_pos = body.find("dt.gui.selection")
    display_image_pos = body.find("view.display_image")
    assert selection_pos != -1 and display_image_pos != -1, (
        "switch_to_darkroom no longer calls both dt.gui.selection and " "view.display_image"
    )
    assert selection_pos < display_image_pos, (
        "switch_to_darkroom calls display_image before selection -- the "
        "action_images fallback can read a stale selection"
    )


def test_commit_view_bounce_retry_sleeps_first():
    """The single retry of the second (into-darkroom) switch must be preceded
    by the same lighttable dwell as the first bounce. Fix round 2 should-fix 6."""
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("local function commit_view_bounce", 1)
    assert len(body) == 2, "commit_view_bounce not found in the plugin"
    body = body[1].split("\nend", 1)[0]
    assert body.count("dt.control.sleep(VIEW_BOUNCE_DWELL_MS)") == 2, (
        "commit_view_bounce must dwell in the lighttable before the first "
        "attempt AND again before the retry of the second switch"
    )


def test_commit_view_bounce_yield_polls_both_legs():
    """Neither leg may be verified by an immediate view read.

    Task 12 fix round 1, host-proven: `dt.gui.current_view(v)` returns before
    the switch has happened, so reading the view straight afterwards answers
    with the OLD view. Both legs must go through wait_for_view instead.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("local function commit_view_bounce", 1)[1].split("\nend", 1)[0]
    assert (
        'wait_for_view("lighttable")' in body
    ), "the outbound leg must yield-poll until the lighttable is really current"
    assert (
        "return_to_darkroom(" in body
    ), "the return leg must go through return_to_darkroom, which yield-polls"


def test_display_image_is_never_the_view_changing_call():
    """display_image must only be called from inside the darkroom.

    Host finding (task 12 fix round 1, spike/scouter_view_switch_probe.lua):
    `dt.gui.views.darkroom.display_image(image)` called from the lighttable
    reports pcall ok=true and does NOT switch the view -- it stayed
    "lighttable" for the full 10s budget, twice. Preferring it unconditionally
    (fix round 1) is what broke the commit bounce's return leg. So inside
    switch_to_darkroom the display_image call must be guarded by an
    already-in-the-darkroom test.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("local function switch_to_darkroom", 1)[1].split("\nend\n", 1)[0]
    first_display = body.index("view.display_image, image")
    guard = body.index('current_view_name() == "darkroom"')
    assert guard < first_display, (
        "switch_to_darkroom calls display_image before checking that it is in "
        "the darkroom -- from the lighttable that call is a silent no-op"
    )
    assert 'wait_for_view("darkroom")' in body, (
        "switch_to_darkroom must yield-poll the view after the switch, not " "read it immediately"
    )


def test_commit_view_bounce_refuses_outside_darkroom():
    """commit_view_bounce must check the starting view before doing anything.

    A bounce that starts outside the darkroom cannot commit anything, and the
    old code attempted it anyway. Fix round 1 blocker.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split("local function commit_view_bounce", 1)
    assert len(body) == 2, "commit_view_bounce not found in the plugin"
    body = body[1].split("\nend", 1)[0]
    assert '"not in darkroom"' in body, (
        "commit_view_bounce does not refuse when the starting view is not " "the darkroom"
    )


def test_darkroom_open_verifies_the_loaded_image():
    """darkroom.open must check what actually loaded, not just that the view
    switch call itself did not raise.

    dt.gui.current_view(darkroom) is a no-op when the darkroom is already
    open on a different image, so a switch that "succeeds" can still leave
    the wrong image loaded. Fix round 1 blocker.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split('methods["darkroom.open"] = function', 1)
    assert len(body) == 2, 'methods["darkroom.open"] not found in the plugin'
    body = body[1].split("\nmethods[", 1)[0]
    assert "wrong image loaded" in body, (
        "darkroom.open no longer verifies the loaded image id against the " "requested one"
    )


def test_darkroom_action_requires_a_numeric_value_for_set():
    """effect="set" must require a real numeric value, never default to 0."""
    source = PLUGIN.read_text(encoding="utf-8")
    body = source.split('methods["darkroom.action"] = function', 1)
    assert len(body) == 2, 'methods["darkroom.action"] not found in the plugin'
    body = body[1].split("\nmethods[", 1)[0]
    assert 'type(p.value) ~= "number"' in body, (
        'darkroom.action no longer guards effect="set" against a missing ' "or non-numeric value"
    )


def test_sweep_covers_orphan_responses():
    """sweep_stale must collect response-*.json, not just request-*.json.

    A client that times out deletes its own request file, but the worker may
    already be mid-flight and writes response-<uuid>.json afterwards. Unswept,
    those orphans grow the cache directory without bound across sessions.
    """
    source = PLUGIN.read_text(encoding="utf-8")
    sweep = source.split("local function sweep_stale", 1)
    assert len(sweep) == 2, "sweep_stale not found in the plugin"
    body = sweep[1].split("\nend", 1)[0]
    assert '-name "request-*.json"' in body, "sweep no longer covers requests"
    assert '-name "response-*.json"' in body, "sweep_stale does not delete orphan response files"
