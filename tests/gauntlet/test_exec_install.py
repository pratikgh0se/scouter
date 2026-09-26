import json

from tests.gauntlet import exec_install


def test_rehearsal_env_is_confined(tmp_path):
    r = exec_install.Rehearsal(tmp_path)
    ok, detail = r.action("check_env")
    assert ok and all(v.startswith(str(tmp_path)) for v in detail.values())


def test_mcp_servers_hash_ignores_other_keys(tmp_path):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"mcpServers": {"a": 1}, "numStartups": 1}))
    h1 = exec_install.mcp_servers_hash(p)
    p.write_text(json.dumps({"mcpServers": {"a": 1}, "numStartups": 2}))
    assert exec_install.mcp_servers_hash(p) == h1
    assert exec_install.mcp_servers_hash(tmp_path / "none.json") == "absent"


def test_leak_check_finds_new_files(tmp_path):
    stamp = tmp_path / "stamp"
    stamp.write_text("x")
    import os
    import time

    os.utime(stamp, (time.time() - 10,) * 2)
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "new.txt").write_text("leak")
    assert exec_install.newer_than(stamp, (str(watched),))
