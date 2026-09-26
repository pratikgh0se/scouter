import json
from datetime import datetime

import pytest

from gauntlet.adapter import Park
from tests.gauntlet import agent


def _lines(*msgs):
    return [json.dumps(m) for m in msgs]


def test_parse_stream_collects_tools_results_and_text():
    lines = _lines(
        {"type": "system", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__scouter__darkroom_say",
             "input": {"phrase": "brighter"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "{\"ok\": true}"}]}]}},
        {"type": "result", "result": "Done, I've brightened it.", "is_error": False,
         "session_id": "s1"})
    p = agent.parse_stream(lines)
    assert p["session_id"] == "s1" and p["text"].startswith("Done")
    assert p["tool_calls"][0]["result"] == "{\"ok\": true}" and p["limit"] is None


def test_usage_limit_is_detected_with_reset_time():
    p = agent.parse_stream(_lines({"type": "result", "is_error": True,
                                   "result": "Claude usage limit reached. Resets at 4pm."}))
    assert p["limit"]
    now = datetime(2026, 9, 27, 13, 0)
    assert datetime.fromtimestamp(agent.reset_time(p["limit"], now)).hour == 16


def test_argv_registers_only_scouter_and_sonnet():
    argv = agent.claude_argv("hi", "/tmp/m.json", resume="s1")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in argv and argv[argv.index("--resume") + 1] == "s1"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scouter__*"


def test_limit_mid_followup_parks_the_whole_case(tmp_path, monkeypatch):
    """Review focus 5: a limit in a follow-up parks the case; on resume the
    whole case reruns from a fresh conversation (nothing is written)."""
    from gauntlet.case import Case, Expect

    answers = iter([
        _lines({"type": "result", "result": "Warmer now.", "session_id": "s1"}),
        _lines({"type": "result", "is_error": True, "result": "usage limit reached"}),
    ])

    class P:
        def __init__(self, out):
            self.stdout = "\n".join(out)

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                      "params": {}})
    s = agent.AgentSession(Inner(), run=lambda *a, **k: P(next(answers)))
    case = Case(id="sca-x", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": ["more"]})
    with pytest.raises(Park):
        s.run_case(case)
