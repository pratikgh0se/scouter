"""Tests for darktable_mcp.cli.install_plugin install/uninstall functions."""

from darktable_mcp.cli.install_plugin import (
    _packaged_lua_bytes,
    install,
    install_main,
    uninstall,
    uninstall_main,
)


def test_install_writes_plugin_file_and_luarc(tmp_path):
    home = tmp_path
    install(home)
    plugin = home / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    luarc = home / ".config" / "darktable" / "luarc"
    assert plugin.is_file()
    assert "view_photos" in plugin.read_text()
    assert luarc.is_file()
    assert 'require "darktable_mcp"' in luarc.read_text()


def test_install_is_idempotent(tmp_path):
    install(tmp_path)
    install(tmp_path)  # second run should not duplicate the require line
    luarc_text = (tmp_path / ".config" / "darktable" / "luarc").read_text()
    assert luarc_text.count('require "darktable_mcp"') == 1


def test_install_preserves_existing_luarc_lines(tmp_path):
    luarc = tmp_path / ".config" / "darktable"
    luarc.mkdir(parents=True)
    (luarc / "luarc").write_text('-- user comment\nrequire "other_plugin"\n')
    install(tmp_path)
    text = (luarc / "luarc").read_text()
    assert "-- user comment" in text
    assert 'require "other_plugin"' in text
    assert 'require "darktable_mcp"' in text


def test_install_creates_luarc_if_missing(tmp_path):
    install(tmp_path)
    luarc = tmp_path / ".config" / "darktable" / "luarc"
    assert luarc.is_file()
    assert luarc.read_text().strip() == 'require "darktable_mcp"'


def test_uninstall_removes_plugin_file_and_require_line(tmp_path):
    install(tmp_path)
    uninstall(tmp_path)
    plugin = tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    luarc = tmp_path / ".config" / "darktable" / "luarc"
    assert not plugin.exists()
    assert luarc.is_file()
    assert 'require "darktable_mcp"' not in luarc.read_text()


def test_uninstall_preserves_other_luarc_lines(tmp_path):
    luarc_path = tmp_path / ".config" / "darktable" / "luarc"
    luarc_path.parent.mkdir(parents=True)
    luarc_path.write_text('-- comment\nrequire "other"\nrequire "darktable_mcp"\n')
    (tmp_path / ".config" / "darktable" / "lua").mkdir()
    (tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua").write_text("-- fake")
    uninstall(tmp_path)
    text = luarc_path.read_text()
    assert "-- comment" in text
    assert 'require "other"' in text
    assert 'require "darktable_mcp"' not in text


def test_uninstall_when_not_installed_is_noop(tmp_path):
    # No prior install. Should not raise.
    uninstall(tmp_path)


def test_install_overwrites_modified_plugin_file(tmp_path):
    install(tmp_path)
    plugin = tmp_path / ".config" / "darktable" / "lua" / "darktable_mcp.lua"
    plugin.write_text("-- user-modified garbage")
    install(tmp_path)
    assert "view_photos" in plugin.read_text()


def test_install_re_enables_commented_out_require(tmp_path):
    luarc_dir = tmp_path / ".config" / "darktable"
    luarc_dir.mkdir(parents=True)
    (luarc_dir / "luarc").write_text('-- some other comment\n-- require "darktable_mcp"\n')
    install(tmp_path)
    text = (luarc_dir / "luarc").read_text()
    # The commented-out form is still there (we don't touch it),
    # but a fresh active require line was appended.
    assert '-- require "darktable_mcp"' in text
    active_lines = [
        ln for ln in text.splitlines() if ln.split("--", 1)[0].rstrip() == 'require "darktable_mcp"'
    ]
    assert len(active_lines) == 1


def test_uninstall_removes_require_with_inline_comment(tmp_path):
    luarc_dir = tmp_path / ".config" / "darktable"
    luarc_dir.mkdir(parents=True)
    (luarc_dir / "luarc").write_text(
        'require "other"\nrequire "darktable_mcp"  -- with a comment\n'
    )
    (luarc_dir / "lua").mkdir()
    (luarc_dir / "lua" / "darktable_mcp.lua").write_text("-- fake")
    uninstall(tmp_path)
    text = (luarc_dir / "luarc").read_text()
    assert 'require "other"' in text
    assert "darktable_mcp" not in text


def test_install_with_no_home_writes_where_the_bridge_reads(tmp_path, monkeypatch):
    """Fix round 2, item 2: the CLI entry points install to the path the
    BRIDGE resolves, not one recomputed from `Path.home()`.

    Installing to one path while the bridge looks at another produces the
    worst kind of failure: a "plugin not installed" error with the plugin
    visibly present on disk.
    """
    from darktable_mcp.bridge.client import plugin_path as bridge_plugin_path

    plugin = tmp_path / "custom-config" / "lua" / "darktable_mcp.lua"
    monkeypatch.setenv("DARKTABLE_MCP_PLUGIN_PATH", str(plugin))

    install()  # no home: the CLI's own call

    assert plugin.is_file(), "the plugin must land on the overridden path"
    assert plugin.read_bytes() == _packaged_lua_bytes()
    assert plugin == bridge_plugin_path(), "installer and bridge must agree"
    # luarc sits beside the plugin's directory, the real darktable layout.
    luarc = tmp_path / "custom-config" / "luarc"
    assert luarc.is_file()
    assert 'require "darktable_mcp"' in luarc.read_text()
    # And nothing was written to the default location.
    assert not (tmp_path / ".config").exists()


def test_uninstall_with_no_home_removes_from_the_overridden_path(tmp_path, monkeypatch):
    plugin = tmp_path / "custom-config" / "lua" / "darktable_mcp.lua"
    monkeypatch.setenv("DARKTABLE_MCP_PLUGIN_PATH", str(plugin))
    install()
    luarc = tmp_path / "custom-config" / "luarc"
    assert plugin.is_file() and 'require "darktable_mcp"' in luarc.read_text()

    uninstall()

    assert not plugin.exists()
    assert 'require "darktable_mcp"' not in luarc.read_text()


def test_install_is_idempotent_under_the_override(tmp_path, monkeypatch):
    plugin = tmp_path / "cfg" / "lua" / "darktable_mcp.lua"
    monkeypatch.setenv("DARKTABLE_MCP_PLUGIN_PATH", str(plugin))
    install()
    install()
    luarc = (tmp_path / "cfg" / "luarc").read_text()
    assert luarc.count('require "darktable_mcp"') == 1


def test_an_explicit_home_still_wins_over_the_override(tmp_path, monkeypatch):
    """The library API keeps its explicit-`home` contract: the override is
    the CLI's default, not an ambient force that overrides a caller who said
    exactly where they wanted it."""
    monkeypatch.setenv("DARKTABLE_MCP_PLUGIN_PATH", str(tmp_path / "ignored" / "darktable_mcp.lua"))
    home = tmp_path / "home"

    install(home)

    assert (home / ".config" / "darktable" / "lua" / "darktable_mcp.lua").is_file()
    assert not (tmp_path / "ignored").exists()


def test_the_entry_points_print_the_resolved_paths(tmp_path, monkeypatch, capsys):
    plugin = tmp_path / "cfg" / "lua" / "darktable_mcp.lua"
    monkeypatch.setenv("DARKTABLE_MCP_PLUGIN_PATH", str(plugin))

    assert install_main([]) == 0
    out = capsys.readouterr().out
    assert str(plugin) in out
    assert str(tmp_path / "cfg" / "luarc") in out

    assert uninstall_main([]) == 0
    out = capsys.readouterr().out
    assert str(plugin) in out
