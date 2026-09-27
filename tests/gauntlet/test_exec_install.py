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


# -- Fix D (night-1 triage rank 5/8, INS-doctor-needs-darktable + -config-version-assert)  # noqa: E501


def _setup_payload(*, doctor_ok: bool, plugin_ok: bool = True) -> dict:
    doctor = {"item": "doctor", "status": "ok" if doctor_ok else "bridge_timeout",
              "detail": "view: darkroom" if doctor_ok else "bridge not answering"}
    plugin = {"item": "plugin", "status": "ok" if plugin_ok else "failed"}
    return {"verb": "setup", "check": False, "ok": doctor_ok and plugin_ok,
            "items": [plugin, {"item": "luarc", "status": "ok"},
                      {"item": "wb_tables", "status": "ok"},
                      {"item": "config", "status": "missing", "required": False}, doctor],
            "next": []}


def _fake_run(payload: dict, code: int):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=code, stdout=json.dumps(payload),
                                           stderr="")
    return run


def test_setup_without_darktable_is_marked_needs_darktable_not_a_bare_fail(tmp_path):
    """The rehearsal's own doctor item requires a running darktable
    (setup.py: required by default); ins-03 quit the one it launched, and
    no case in the rehearsal restarts it before setup/setup_check run. That
    is an environment gap the harness itself created, not a product
    regression -- exec_install.InstallSession.run_case must mark the case
    Observation.blocked = "needs_darktable" (evaluate()'s "blocked" outcome),
    not let it read as a plain "failed"."""
    r = exec_install.Rehearsal(tmp_path, run=_fake_run(_setup_payload(doctor_ok=False), 1))
    ok, detail = r.action("setup")
    assert ok is False
    assert detail["_needs_darktable"] is True

    session = exec_install.InstallSession(tmp_path)
    session.rehearsal = r
    case = Case(id="ins-04", layer="install", tier="P0", mode="cli",
               steps=[Step("rehearsal", {"action": "setup"}, 120.0, False)],
               expect=Expect("workflow"), meta={"action": "setup"})
    obs = session.run_case(case)
    assert obs.blocked == "needs_darktable"
    assert "_needs_darktable" not in obs.extra["detail"]  # the internal marker never leaks


def test_setup_check_without_darktable_is_also_marked_needs_darktable(tmp_path):
    r = exec_install.Rehearsal(tmp_path, run=_fake_run(_setup_payload(doctor_ok=False), 1))
    ok, detail = r.action("setup_check")
    assert ok is False
    assert detail["_needs_darktable"] is True


def test_config_version_reason_is_reported_even_once_darktable_is_available(tmp_path):
    """Fix D (INS-config-version-assert): scouter/cli/setup.py never writes
    config.json by design, regardless of darktable -- this must show up as
    an honest, always-present note, not only when doctor also fails."""
    r = exec_install.Rehearsal(tmp_path, run=_fake_run(_setup_payload(doctor_ok=True), 0))
    ok, detail = r.action("setup")
    assert ok is True  # the product's own contract (code 0) is met
    assert detail["config_has_version"] is False
    assert "needs_review_reason" in detail
    assert "_needs_darktable" not in detail  # doctor was fine: not an environment block


def test_config_version_true_once_config_actually_has_it(tmp_path):
    (tmp_path / "config" / "scouter").mkdir(parents=True)
    (tmp_path / "config" / "scouter" / "config.json").write_text('{"version": 1}\n')
    r = exec_install.Rehearsal(tmp_path, run=_fake_run(_setup_payload(doctor_ok=True), 0))
    ok, detail = r.action("setup")
    assert ok is True
    assert detail["config_has_version"] is True
    assert "needs_review_reason" not in detail


# -- Review round 2, finding 2 (GD-1, P0 blocker) ----------------------------


def test_plugin_failed_and_doctor_down_gives_no_marker_verdict_failed(tmp_path):
    """A broken plugin install, with doctor also down (always true in the
    rehearsal), must never read as an environment gap -- doctor down must be
    the ONLY reason, or the case is a plain "failed"."""
    payload = _setup_payload(doctor_ok=False, plugin_ok=False)
    r = exec_install.Rehearsal(tmp_path, run=_fake_run(payload, 1))
    ok, detail = r.action("setup")
    assert ok is False
    assert "_needs_darktable" not in detail

    session = exec_install.InstallSession(tmp_path)
    session.rehearsal = r
    case = Case(id="ins-04", layer="install", tier="P0", mode="cli",
               steps=[Step("rehearsal", {"action": "setup"}, 120.0, False)],
               expect=Expect("workflow"), meta={"action": "setup"})
    obs = session.run_case(case)
    assert obs.blocked is None  # not blocked: a real product failure


def test_setup_again_tree_changed_and_doctor_down_gives_failed_not_blocked(tmp_path):
    """setup_again's own idempotency break (the tree hash moved) must not be
    hidden behind the doctor marker either -- blocked short-circuits every
    check in evaluate(), so a masked idempotency break would never surface."""
    (tmp_path / "config" / "scouter").mkdir(parents=True)
    (tmp_path / "config" / "scouter" / "config.json").write_text('{"a": 1}\n')
    payload = _setup_payload(doctor_ok=False)

    def run(argv, **kwargs):
        # the second `scouter setup` call changes the tree (writes a new file)
        (tmp_path / "config" / "scouter" / "extra.json").write_text('{"b": 2}\n')
        return subprocess.CompletedProcess(argv, returncode=1, stdout=json.dumps(payload),
                                           stderr="")

    r = exec_install.Rehearsal(tmp_path, run=run)
    ok, detail = r.action("setup_again")
    assert ok is False
    assert "_needs_darktable" not in detail  # the tree changed: not purely an environment gap
