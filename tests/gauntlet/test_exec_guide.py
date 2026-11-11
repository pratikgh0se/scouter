import json
import subprocess
from types import SimpleNamespace

from tests.gauntlet import exec_guide


def test_cli_handler_records_exit_and_json(tmp_path, monkeypatch):
    class P:
        returncode = 0
        stdout = json.dumps({"ok": True, "outcome": "ok", "data": {}})

    monkeypatch.setattr(exec_guide.subprocess, "run", lambda *a, **k: P())
    sess = SimpleNamespace(staged=SimpleNamespace(env={}, frames={"hero": tmp_path / "h.ORF"},
                                                  vars={}, job_frames=[]), root=tmp_path)
    obs = SimpleNamespace(calls=[], extra={})
    step = SimpleNamespace(args={"argv": ["darkroom", "open", "{frame:hero}"]}, budget_s=10)
    rec = exec_guide.cli(sess, None, step, obs)
    assert rec.envelope["ok"] and obs.extra["exit"] == 0
    assert rec.args["argv"][-1].endswith("h.ORF")


def test_cli_handler_reports_a_typed_hang_on_timeout(tmp_path, monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="scouter", timeout=40)

    monkeypatch.setattr(exec_guide.subprocess, "run", raise_timeout)
    sess = SimpleNamespace(staged=SimpleNamespace(env={}, frames={"hero": tmp_path / "h.ORF"},
                                                  vars={}, job_frames=[]), root=tmp_path)
    obs = SimpleNamespace(calls=[], extra={}, blocked=None, hang_evidence=[])
    step = SimpleNamespace(args={"argv": ["darkroom", "open", "{frame:hero}"]}, budget_s=10)
    rec = exec_guide.cli(sess, None, step, obs)
    assert rec.error == "timeout" and rec.envelope is None
    assert obs.blocked == "hang: scouter cli exceeded budget"
    assert obs.hang_evidence and "no answer in 40s" in obs.hang_evidence[0]
