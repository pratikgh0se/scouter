"""`scouter setup` / `scouter setup --check`: everything real use needs, once.

Product close, row 427 item 2. Idempotent, and persistent on purpose: the
host tests install the Lua plugin and restore the previous state on exit;
this verb installs it and LEAVES it, because a user's darktable must load it
on every launch, not only during a test run.

Items, each reported `ok` or with what is missing:

- `plugin`   -- the packaged Lua plugin at the path the bridge reads
               (`bridge.client.plugin_path()`, so `DARKTABLE_MCP_PLUGIN_PATH`
               is honoured exactly as `darktable-mcp install-plugin` does);
- `luarc`    -- an active `require "darktable_mcp"` line beside it;
- `wb_tables`-- the measured white-balance tables shipped in
               `scouter/data/wb/` copied into `<config_dir>/wb/` when that
               camera has none yet (never overwritten; E12 D9);
- `config`   -- reported only: `scouter config init` writes it, because its
               backup gate is a decision the user makes, not a default;
- `doctor`   -- the bridge half of `scouter doctor` (darktable running with
               the plugin loaded and answering).

`--check` changes nothing. Registering the MCP server in Claude Code is the
one step this verb does not perform: it prints the exact `claude mcp add`
command and the equivalent JSON, and never reads or writes Claude Code's
own config.
"""

from __future__ import annotations

import importlib.resources
import os
import sys
from pathlib import Path

from darktable_mcp.cli import install_plugin

SERVER_NAME = "scouter"


def repo_root() -> Path:
    """The checkout (or site-packages dir) the running `scouter` came from."""
    import scouter

    return Path(scouter.__file__).resolve().parent.parent


def server_python(root: Path | None = None) -> str:
    """The repo's own venv interpreter when it has one, else this interpreter."""
    root = root or repo_root()
    venv = root / ".venv" / "bin" / "python"
    # normpath, never resolve(): a venv's python is a symlink to the base
    # interpreter, and resolving it would drop the venv (and its packages).
    return str(venv) if venv.is_file() else os.path.normpath(os.path.abspath(sys.executable))


def mcp_command(*, repo_root: Path, python: str) -> str:
    """The `claude mcp add` line: a stdio server, user scope, absolute paths.

    `PYTHONPATH` pins the checkout, so the server imports THIS tree even if
    the venv's editable install points at another worktree.
    """
    return (
        f"claude mcp add --transport stdio --scope user --env PYTHONPATH={repo_root} "
        f"{SERVER_NAME} -- {python} -m scouter.mcp.server"
    )


def mcp_json(*, repo_root: Path, python: str) -> dict:
    """The same server as an `mcpServers` entry (`.mcp.json` / Claude Desktop)."""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": python,
                "args": ["-m", "scouter.mcp.server"],
                "env": {"PYTHONPATH": str(repo_root)},
            }
        }
    }


def _plugin_item(check: bool) -> dict:
    plugin_file, _ = install_plugin._targets(None)
    wanted = install_plugin._packaged_lua_bytes()
    if plugin_file.is_file():
        state = "ok" if plugin_file.read_bytes() == wanted else "stale"
    else:
        state = "missing"
    if state != "ok" and not check:
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        plugin_file.write_bytes(wanted)
        state = "installed" if state == "missing" else "updated"
    return {"item": "plugin", "status": state, "detail": str(plugin_file)}


def _luarc_item(check: bool) -> dict:
    _, luarc = install_plugin._targets(None)
    text = luarc.read_text(encoding="utf-8") if luarc.is_file() else ""
    active = any(install_plugin._is_active_require_line(ln) for ln in text.splitlines())
    state = "ok" if active else "missing"
    if not active and not check:
        if text and not text.endswith("\n"):
            text += "\n"
        luarc.parent.mkdir(parents=True, exist_ok=True)
        luarc.write_text(text + install_plugin.REQUIRE_LINE + "\n", encoding="utf-8")
        state = "installed"
    return {"item": "luarc", "status": state, "detail": str(luarc)}


def _packaged_tables() -> dict[str, bytes]:
    folder = importlib.resources.files("scouter").joinpath("data/wb")
    out: dict[str, bytes] = {}
    try:
        entries = list(folder.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return out
    for entry in entries:
        if entry.name.endswith(".json"):
            out[entry.name[: -len(".json")]] = entry.read_bytes()
    return dict(sorted(out.items()))


def _wb_item(check: bool) -> dict:
    from scouter import paths

    wb_dir = paths.wb_dir()
    present, added, missing = [], [], []
    for camera, blob in _packaged_tables().items():
        target = wb_dir / f"{camera}.json"
        if target.exists():
            present.append(camera)
        elif check:
            missing.append(camera)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            added.append(camera)
    others = sorted(
        p.stem for p in wb_dir.glob("*.json") if p.stem not in present + added
    ) if wb_dir.is_dir() else []
    state = "missing" if missing else ("installed" if added else "ok")
    parts = []
    if present:
        parts.append(f"present: {', '.join(present)}")
    if added:
        parts.append(f"installed: {', '.join(added)}")
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    if others:
        parts.append(f"also calibrated: {', '.join(others)}")
    return {"item": "wb_tables", "status": state,
            "detail": f"{wb_dir} ({'; '.join(parts) or 'none shipped'})"}


def _config_item() -> dict:
    from scouter import paths

    cfg = Path(paths.config_path())
    return {"item": "config", "status": "ok" if cfg.is_file() else "missing",
            "detail": str(cfg), "required": False}


def _doctor_item() -> dict:
    from scouter.cli.main import _doctor

    report = _doctor()
    if report.get("ok"):
        return {"item": "doctor", "status": "ok", "detail": f"view: {report.get('view')}"}
    return {"item": "doctor", "status": str(report.get("bridge") or "error"),
            "detail": report.get("bridge_fix") or "bridge not answering"}


def run(check: bool = False) -> dict:
    """Run (or with `check`, only inspect) every setup item. Never raises for
    a missing item: the report says what is missing and the next step."""
    items = [_plugin_item(check), _luarc_item(check), _wb_item(check), _config_item()]
    items.append(_doctor_item())
    root = repo_root()
    python = server_python(root)
    installed_now = any(i["status"] in ("installed", "updated")
                        for i in items if i["item"] in ("plugin", "luarc"))
    required_ok = all(
        i["status"] in ("ok", "installed", "updated")
        for i in items if i.get("required", True)
    )
    nxt: list[str] = []
    by = {i["item"]: i for i in items}
    if check and (by["plugin"]["status"] != "ok" or by["luarc"]["status"] != "ok"):
        nxt.append("run `scouter setup` to install the Lua plugin and its luarc line")
    if check and by["wb_tables"]["status"] == "missing":
        nxt.append("run `scouter setup` to install the measured white-balance tables")
    if installed_now or by["doctor"]["status"] != "ok":
        nxt.append("restart darktable (it loads the plugin at launch), open an image in the "
                   "darkroom, then run `scouter setup --check`")
    if by["config"]["status"] == "missing":
        nxt.append("run `scouter config init` and set library_root and the backup target "
                   "(needed for library/job verbs, not for single-frame editing)")
    nxt.append("register the MCP server in Claude Code once: " + mcp_command(
        repo_root=root, python=python))
    return {
        "verb": "setup",
        "check": check,
        "ok": required_ok,
        "items": items,
        "claude_mcp_add": mcp_command(repo_root=root, python=python),
        "mcp_json": mcp_json(repo_root=root, python=python),
        "next": nxt,
    }
