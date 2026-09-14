"""install-plugin / uninstall-plugin subcommands.

Library functions take an explicit `home` for testability. The CLI entry
points pass NO home and go through `bridge.client.plugin_path()` instead, so
they write exactly where the bridge reads -- including when
`DARKTABLE_MCP_PLUGIN_PATH` points somewhere other than
`~/.config/darktable` (fix round 2, item 2). Installing to one path while the
bridge looks at another is the kind of mismatch that produces a
"plugin not installed" error with the plugin visibly present.
"""

from __future__ import annotations

import argparse
import importlib.resources
from pathlib import Path

from darktable_mcp.bridge.client import plugin_path as bridge_plugin_path

REQUIRE_LINE = 'require "darktable_mcp"'


def _plugin_dir(home: Path) -> Path:
    return home / ".config" / "darktable" / "lua"


def _luarc_path(home: Path) -> Path:
    return home / ".config" / "darktable" / "luarc"


def _targets(home: Path | None) -> tuple[Path, Path]:
    """`(plugin_file, luarc)`.

    With a `home`, the classic layout under it (what the tests drive). With
    `home=None`, the bridge's own resolver decides the plugin file and the
    luarc sits beside its directory -- `<plugin>/../../luarc` -- which is the
    same relationship the real darktable config has and the host tests use.
    """
    if home is not None:
        return _plugin_dir(home) / "darktable_mcp.lua", _luarc_path(home)
    plugin_file = bridge_plugin_path()
    return plugin_file, plugin_file.parent.parent / "luarc"


def _packaged_lua_bytes() -> bytes:
    return importlib.resources.files("darktable_mcp").joinpath("lua/darktable_mcp.lua").read_bytes()


def _is_active_require_line(line: str) -> bool:
    """True if `line` (a single luarc line) actively loads our plugin.

    Matches the bare `require "darktable_mcp"` form, and the same with a
    trailing inline `--` comment. Does NOT match commented-out forms like
    `-- require "darktable_mcp"`.
    """
    no_comment = line.split("--", 1)[0].rstrip()
    return no_comment == REQUIRE_LINE


def install(home: Path | None = None) -> None:
    """Write the packaged plugin and ensure luarc loads it.

    `home=None` (the CLI's own call) resolves through
    `bridge.client.plugin_path()`, so the override is honoured.
    """
    plugin_file, luarc = _targets(home)
    plugin_file.parent.mkdir(parents=True, exist_ok=True)
    plugin_file.write_bytes(_packaged_lua_bytes())

    if luarc.exists():
        text = luarc.read_text(encoding="utf-8")
    else:
        text = ""
    already_active = any(_is_active_require_line(ln) for ln in text.splitlines())
    if not already_active:
        if text and not text.endswith("\n"):
            text += "\n"
        text += REQUIRE_LINE + "\n"
        luarc.parent.mkdir(parents=True, exist_ok=True)
        luarc.write_text(text, encoding="utf-8")


def uninstall(home: Path | None = None) -> None:
    """Remove the plugin and its luarc line. `home=None` resolves through
    `bridge.client.plugin_path()`, same as `install`."""
    plugin_file, luarc = _targets(home)
    if plugin_file.exists():
        plugin_file.unlink()

    if luarc.exists():
        lines = luarc.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [ln for ln in lines if not _is_active_require_line(ln)]
        luarc.write_text("".join(kept), encoding="utf-8")


def install_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="darktable-mcp install-plugin",
        description="Install the darktable_mcp Lua plugin into the user's darktable config.",
    )
    parser.parse_args(argv)
    install()
    plugin_file, luarc = _targets(None)
    print(f"✓ wrote {plugin_file}")
    print(f"✓ ensured 'require \"darktable_mcp\"' in {luarc}")
    print("Restart darktable to load the plugin.")
    return 0


def uninstall_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="darktable-mcp uninstall-plugin",
        description="Remove the darktable_mcp Lua plugin from the user's darktable config.",
    )
    parser.parse_args(argv)
    uninstall()
    plugin_file, luarc = _targets(None)
    print(f"✓ removed {plugin_file}")
    print(f"✓ removed 'require \"darktable_mcp\"' from {luarc}")
    return 0
