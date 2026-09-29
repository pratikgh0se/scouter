"""Host-test helpers shared by `tests/test_host_phase2.py` and `tests/test_host_phase3.py`.

Task 9 (phase 3): a straight refactor of `tests/test_host_phase2.py`'s
`_PluginInstall` (renamed `PluginInstall` here), `_quit` (renamed
`quit_darktable`) and the path constants they need, out to their own module
so a future host-test file does not have to re-copy them. No behaviour
change: every body below is byte-identical to what `test_host_phase2.py`
used to define inline.

Importing this module must never launch darktable or touch a real path --
it only builds `Path` objects and defines functions/classes. `launch()` and
`wait_ready()` are new (task 9): the same "start darktable on an image, then
poll until the bridge is ready" shape every host class in `test_host_phase2.py`
already hand-rolls in its own `_launch_and_wait`, available here for
`test_host_phase3.py` (task 10) to reuse without a fourth copy.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

DARKTABLE = Path("/Applications/darktable.app/Contents/MacOS/darktable")
EDITING_TRIAL = Path("/Users/pratik/Pictures/EditingTrial")
HERO = EDITING_TRIAL / "P6281348.ORF"
PLUGIN_SRC = Path(__file__).resolve().parent.parent / "darktable_mcp" / "lua" / "darktable_mcp.lua"
INSTALLED_PLUGIN = Path.home() / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
SCRATCH_DIR = Path.home() / "Pictures" / "ScouterScratch"


def _bridge_cache_dir() -> Path:
    """The bridge's file spool (`darktable_mcp/bridge/client.py::_cache_dir`),
    recomputed here rather than imported so this module still imports with
    no product dependency (matches this file's own "importing must never
    launch darktable or touch a real path" rule above)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "darktable-mcp"


def _darktable_is_running() -> bool:
    result = subprocess.run(["pgrep", "-x", "darktable"], capture_output=True, text=True)
    return result.returncode == 0


def _pending_bridge_files(cache_dir: Path) -> list[str]:
    """Names of files in `cache_dir` that mean a request is IN FLIGHT: the
    bridge's own `request-<id>.json` / `response-<id>.json` pair
    (`darktable_mcp/bridge/client.py::Bridge.call`). The `heartbeat` file and
    the `cli-config/` directory are always-present, normal residents of a
    live spool (confirmed by `ls` of `~/.cache/darktable-mcp`) -- neither
    means another session is mid-call, so neither counts here."""
    if not cache_dir.is_dir():
        return []
    return sorted(
        p.name for p in cache_dir.iterdir()
        if p.is_file() and p.suffix == ".json"
        and (p.name.startswith("request-") or p.name.startswith("response-"))
    )


def assert_no_concurrent_darktable(
    cache_dir: Path | None = None,
    darktable_running: Callable[[], bool] | None = None,
) -> None:
    """Z5 detector (incident inc_f9c5e8f5, card t_cc03dcb4 fix round 1, I2):
    refuse BEFORE `PluginInstall.install()` touches anything if a second
    darktable session could be sharing this one's plugin config, library.db
    or bridge spool -- exactly the contamination the incident's overlapping
    launches caused (two `hs.launch()` calls served from the same
    `~/.cache/darktable-mcp` spool, one run's history landing on the other's
    scratch copy). Two independent checks, either one enough to refuse:

    - a second darktable process (`pgrep -x darktable`; `darktable_running`
      lets a test inject a fake result without mocking `subprocess` itself),
    - a pending bridge request/response file in the spool (`cache_dir`
      defaults to the real one; a test passes a temp dir instead).

    Raises `RuntimeError` naming which check failed; callers must call this
    before any install/backup/launch so a failure here has touched nothing.
    """
    running = darktable_running or _darktable_is_running
    if running():
        raise RuntimeError(
            "another darktable is running (pgrep -x darktable found a pid) -- "
            "host probes run one darktable launch at a time; quit it first")
    cache = cache_dir if cache_dir is not None else _bridge_cache_dir()
    pending = _pending_bridge_files(cache)
    if pending:
        raise RuntimeError(
            f"pending bridge request/response file(s) in {cache}: {pending} -- "
            "another darktable session may still be using the spool "
            "(the heartbeat file and cli-config/ directory are normal residents, "
            "these are not)")


class PluginInstall:
    """Install this repo's Lua plugin over the user's, restorably.

    Fix round 1, minor g: every host class here had its own copy of this,
    each keeping the user's plugin bytes in a CLASS ATTRIBUTE -- so a hard
    kill, a power loss or a pytest crash between install and teardown left
    Scouter's development plugin installed in the user's real
    `~/.config/darktable` with no way to get theirs back. That is exactly
    what the backup existed to prevent.

    The copy now goes to `~/Pictures/ScouterScratch/dtconfig-backup-<ts>/`
    BEFORE anything is overwritten, `restore()` reads from that file rather
    than from memory, and the directory survives a crash (it is deleted only
    after a successful restore) so a human can undo it by hand from a path
    its own MANIFEST.txt names.

    **What is NOT backed up, and NOT restored: darktable's `library.db`.**
    A host run imports a film roll into it and writes ratings, and Scouter
    may not write `library.db` itself, so there is nothing here that could
    put it back. Stated rather than hidden.
    """

    def __init__(self) -> None:
        self.backup_dir: Path | None = None
        self.plugin_existed_before = False
        self.luarc_existed_before = False
        self.luarc_line_added = False
        self.luarc_path: Path | None = None

    def install(self) -> None:
        # ABSOLUTE FIRST (card t_cc03dcb4 fix round 1, I2 -- incident
        # inc_f9c5e8f5): refuse before touching anything at all if a second
        # darktable or a pending bridge request could contaminate this run.
        assert_no_concurrent_darktable()
        # FIRST, before any mkdir (fix round 2, minor m1): `install`'s own
        # `except BaseException` calls `restore()`, and `restore()` UNLINKS
        # the plugin when `plugin_existed_before` is False. Reading these
        # flags after a mkdir meant a failing mkdir left them at their
        # `False` defaults and the restore then deleted the user's real
        # plugin -- the cleanup destroying exactly what the backup exists to
        # protect. Two `exists()` calls, no side effects, nothing before them.
        luarc = INSTALLED_PLUGIN.parent.parent / "luarc"
        self.luarc_path = luarc
        self.plugin_existed_before = INSTALLED_PLUGIN.exists()
        self.luarc_existed_before = luarc.exists()

        try:
            INSTALLED_PLUGIN.parent.mkdir(parents=True, exist_ok=True)
            SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
            # The pid is in the name (minor m2) so two host classes running
            # inside the same second -- or two pytest processes -- cannot
            # share a backup directory and overwrite each other's copy of the
            # user's plugin.
            self.backup_dir = SCRATCH_DIR / (
                f"dtconfig-backup-{time.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
            )
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            # Everything needed to undo this, on disk, before the first write.
            if self.plugin_existed_before:
                (self.backup_dir / "darktable_mcp.lua").write_bytes(INSTALLED_PLUGIN.read_bytes())
            if self.luarc_existed_before:
                (self.backup_dir / "luarc").write_bytes(luarc.read_bytes())
            (self.backup_dir / "MANIFEST.txt").write_text(
                "Scouter host test backup of the user's darktable Lua config.\n"
                f"taken_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                f"plugin_path: {INSTALLED_PLUGIN}\n"
                f"plugin_existed_before: {self.plugin_existed_before}\n"
                f"luarc_path: {luarc}\n"
                f"luarc_existed_before: {self.luarc_existed_before}\n"
                "\n"
                "If this directory still exists, a test did not finish its\n"
                "restore. To undo by hand: copy darktable_mcp.lua and luarc\n"
                "back to the paths above, deleting them instead if the\n"
                "matching *_existed_before line says false.\n"
                "\n"
                "NOT backed up and NOT restored: darktable's library.db. A\n"
                "host run imports a film roll into it and writes ratings.\n",
                encoding="utf-8",
            )

            shutil.copy(PLUGIN_SRC, INSTALLED_PLUGIN)

            line = 'require "darktable_mcp"\n'
            if not luarc.exists() or line not in luarc.read_text():
                self.luarc_line_added = True
                with luarc.open("a") as fh:
                    fh.write(line)
        except BaseException:
            self.restore()
            raise

    def restore(self) -> None:
        backup = self.backup_dir
        try:
            if self.luarc_path is not None:
                saved_luarc = (backup / "luarc") if backup is not None else None
                if self.luarc_existed_before:
                    if saved_luarc is not None and saved_luarc.is_file():
                        self.luarc_path.write_bytes(saved_luarc.read_bytes())
                    elif self.luarc_line_added:
                        # No backup file to fall back on: strip just the line
                        # this install appended, which is all it changed.
                        line = 'require "darktable_mcp"\n'
                        text = self.luarc_path.read_text()
                        self.luarc_path.write_text(text.replace(line, "", 1))
                elif self.luarc_line_added:
                    self.luarc_path.unlink(missing_ok=True)

            saved_plugin = (backup / "darktable_mcp.lua") if backup is not None else None
            if self.plugin_existed_before:
                if saved_plugin is not None and saved_plugin.is_file():
                    INSTALLED_PLUGIN.write_bytes(saved_plugin.read_bytes())
            else:
                INSTALLED_PLUGIN.unlink(missing_ok=True)
        except OSError as e:
            print(
                "WARNING: could not restore the darktable Lua config; the backup is at "
                f"{backup} and its MANIFEST.txt says how to undo this by hand: {e}"
            )
            return
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        self.backup_dir = None

    def touched_paths(self) -> list[Path]:
        """Everything install()/restore() may write: the plugin file and
        luarc. Nothing else (P3-R18)."""
        return [INSTALLED_PLUGIN, INSTALLED_PLUGIN.parent.parent / "luarc"]


def quit_darktable(proc: subprocess.Popen[bytes]) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "darktable" to quit'],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass
    for wait, sig in ((20, None), (10, signal.SIGTERM), (10, signal.SIGKILL)):
        if sig is not None:
            proc.send_signal(sig) if sig == signal.SIGTERM else proc.kill()
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def launch(image: Path) -> subprocess.Popen:
    """Start darktable on `image`, so it comes up in the darkroom (phase 2 host fact)."""
    return subprocess.Popen([str(DARKTABLE), str(image)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(budget_s: float = 120.0) -> str:
    """Poll `view.current` until the bridge answers; return the view. Raises on budget."""
    from scouter.core import dtbridge

    deadline = time.monotonic() + budget_s
    last: object = "never answered"
    while time.monotonic() < deadline:
        try:
            return dtbridge.current_view()
        except Exception as e:  # noqa: BLE001 -- any failure means "not yet"
            last = e
            time.sleep(0.5)
    raise TimeoutError(f"darktable bridge not ready after {budget_s}s: {last}")
