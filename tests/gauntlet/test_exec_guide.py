import json
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
