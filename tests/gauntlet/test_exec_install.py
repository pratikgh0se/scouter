import json
import subprocess

from gauntlet.case import Case, Expect, Step
from tests.gauntlet import exec_install


def test_rehearsal_env_is_confined(tmp_path):
    r = exec_install.Rehearsal(tmp_path)
    ok, detail = r.action("check_env")
    assert ok and all(v.startswith(str(tmp_path)) for v in detail.values())


def test_rehearsal_env_pins_library_db(tmp_path):
    """I1: SCOUTER_DARKTABLE_LIBRARY_DB must be scrubbed of the real value and
    pinned inside the rehearsal root, or history/backup fall back to the
    running darktable's --library or the user's real library.db."""
    r = exec_install.Rehearsal(tmp_path)
    env = r.env()
    assert env["SCOUTER_DARKTABLE_LIBRARY_DB"] == str(tmp_path / "config" / "library.db")


def test_first_launch_passes_the_rehearsal_env(tmp_path, monkeypatch):
    """C1: darktable must not inherit the real HOME/XDG_*; only --configdir
    was scoped before this fix. No real darktable is launched here."""
    from scouter.core import dtprocess

    r = exec_install.Rehearsal(tmp_path)
    captured = {}

    def fake_launch(frame, library, configdir=None, **kwargs):
        captured["env"] = kwargs.get("env")
        cfg = tmp_path / "config" / "darktable"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "darktablerc").write_text("x")
        (cfg / "library.db").write_text("x")
        return dtprocess.Launched(pid=1, start_time="t", exe="darktable",
                                  library=str(library), configdir=str(configdir), frame=None)

    monkeypatch.setattr(dtprocess, "launch", fake_launch)
    monkeypatch.setattr(dtprocess, "terminate", lambda pid: {"ok": True, "how": "fake"})
    monkeypatch.setattr(exec_install.time, "sleep", lambda s: None)
    ok, _ = r.action("first_launch")
    assert ok
    assert captured["env"] == r.env()


def test_run_case_reports_timeout_as_blocked(tmp_path):
    """I2: a timed-out action must surface as Observation.blocked, not be
    indistinguishable from a plain failed assertion."""
    session = exec_install.InstallSession(tmp_path)

    def boom(name):
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=1)

    session.rehearsal.action = boom
    case = Case(id="ins-x", layer="install", tier="P0", mode="cli",
               steps=[Step("rehearsal", {"action": "setup"}, 120.0, False)],
               expect=Expect("workflow"), meta={"action": "setup"})
    obs = session.run_case(case)
    assert obs.blocked == "hang: install action setup exceeded its timeout"
    assert obs.hang_evidence and obs.extra["action_ok"] is False


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
