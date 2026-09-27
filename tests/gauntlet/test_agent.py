import json
import signal
import subprocess
import threading
from datetime import datetime

import pytest

from gauntlet.adapter import Park
from tests.gauntlet import agent
from tests.gauntlet.session import SessionBlocked


def _lines(*msgs):
    return [json.dumps(m) for m in msgs]


class FakePopen:
    """A `claude -p` stand-in: never a real process. `out` is its stdout
    lines, or an exception communicate() raises, or "block" (it waits until
    killed through the fake killpg)."""

    spawned: list = []

    def __init__(self, out, pid=4242):
        self.out = out
        self.pid = pid
        self.returncode = None
        self.killed = threading.Event()
        self.kwargs = None

    def __call__(self, argv, **kw):
        self.argv, self.kwargs = argv, kw
        FakePopen.spawned.append(self)
        return self

    def communicate(self, timeout=None):
        if self.out == "block":
            self.killed.wait(5)
            self.returncode = -9
            return "", ""
        if isinstance(self.out, Exception):
            raise self.out
        self.returncode = 0
        return "\n".join(self.out), ""

    def poll(self):
        return self.returncode


def _popen_seq(*outs):
    """popen= factory answering each turn with the next FakePopen."""
    procs = iter([FakePopen(o, pid=4242 + i) for i, o in enumerate(outs)])
    return lambda argv, **kw: next(procs)(argv, **kw)


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


def test_session_limit_text_is_also_detected_with_reset_time():
    """Night-1 triage rank 2 (SCA-session-limit-unparked): the Claude CLI's
    own "session limit" wording (distinct from the usage/rate-limit forms
    LIMIT_RE already covered) never parked 14 cases -- it was scored as the
    agent's own answer, producing 12 partial, 1 fail and one hollow pass
    (sca-r0159)."""
    p = agent.parse_stream(_lines({"type": "result", "is_error": True,
                                   "result": "You've hit your session limit · resets "
                                             "3:30pm (Asia/Bangkok)"}))
    assert p["limit"]
    now = datetime(2026, 9, 27, 13, 51)
    assert datetime.fromtimestamp(agent.reset_time(p["limit"], now)).hour == 15


def test_an_ordinary_answer_is_never_mistaken_for_a_limit():
    p = agent.parse_stream(_lines({"type": "result", "is_error": False,
                                   "result": "Done, I've brightened it by half a stop."}))
    assert p["limit"] is None


def test_argv_registers_only_scouter_and_sonnet():
    argv = agent.claude_argv("hi", "/tmp/m.json", resume="s1")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in argv and argv[argv.index("--resume") + 1] == "s1"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scouter__*"


def test_argv_disallows_bash_write_edit():
    """(row 501 item E, night-1 rank 12, SCA-agent-sandbox-leak): the
    sandbox tools must be off, not merely unlisted in --allowedTools."""
    argv = agent.claude_argv("hi", "/tmp/m.json")
    assert argv[argv.index("--disallowedTools") + 1] == "Bash,Write,Edit"


def test_limit_mid_followup_parks_the_whole_case(tmp_path, monkeypatch):
    """Review focus 5: a limit in a follow-up parks the case; on resume the
    whole case reruns from a fresh conversation (nothing is written)."""
    from gauntlet.case import Case, Expect

    popen = _popen_seq(
        _lines({"type": "result", "result": "Warmer now.", "session_id": "s1"}),
        _lines({"type": "result", "is_error": True, "result": "usage limit reached"}))

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                      "params": {}})
    s = agent.AgentSession(Inner(), popen=popen)
    case = Case(id="sca-x", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": ["more"]})
    with pytest.raises(Park):
        s.run_case(case)


def test_turn_timeout_blocks_instead_of_raising(tmp_path, monkeypatch):
    """A hung `claude` process (subprocess.TimeoutExpired) must not escape
    run_case as a bare exception; the runner would mislabel it harness_error."""
    from gauntlet.case import Case, Expect

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

    proc = FakePopen(subprocess.TimeoutExpired(cmd="claude", timeout=agent.TURN_TIMEOUT_S))
    kills = []
    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                      "params": {}})
    s = agent.AgentSession(Inner(), popen=proc, killpg=lambda pid, sig: kills.append(pid))
    case = Case(id="sca-y", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": []})
    obs = s.run_case(case)
    assert obs.blocked and obs.blocked.startswith("hang:")
    assert any("hang:" in e for e in obs.hang_evidence)
    assert kills == [proc.pid]  # the timed-out turn's group is killed, not left running


def test_ensure_frame_session_blocked_blocks_instead_of_raising(tmp_path):
    """SessionBlocked from ensure_frame (failed frame switch) must be turned
    into a blocked Observation, matching execute.run_tool_case's handling."""
    from gauntlet.case import Case, Expect

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            raise SessionBlocked("frame switch failed")

    s = agent.AgentSession(Inner(), popen=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("popen must not be called")))
    case = Case(id="sca-z", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": []})
    obs = s.run_case(case)
    assert obs.blocked == "session: frame switch failed"
    assert "session: frame switch failed" in obs.hang_evidence


def _agent_case(followups=()):
    from gauntlet.case import Case, Expect

    return Case(id="sca-k", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": list(followups)})


def test_claude_runs_in_its_own_process_group_and_abort_kills_it(tmp_path, monkeypatch):
    """(final fix C1): the hard-cap abort must kill `claude -p` (its whole
    process group), not only darktable + the session's MCP child; and the
    aborted conversation must not start its follow-up turn."""
    aborted_inner = []

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

        def abort(self):
            aborted_inner.append(True)

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                      "params": {}})
    FakePopen.spawned = []
    proc = FakePopen("block", pid=5151)
    kills = []

    def killpg(pid, sig):
        kills.append((pid, sig))
        proc.killed.set()

    s = agent.AgentSession(Inner(), popen=proc, killpg=killpg)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", _safe(s.run_case,
                                                                  _agent_case(["more"]))))
    t.start()
    for _ in range(200):
        if s.proc is not None:
            break
        threading.Event().wait(0.01)
    assert proc.kwargs["start_new_session"] is True
    s.abort()
    t.join(5)
    assert not t.is_alive()
    assert kills == [(5151, signal.SIGKILL)] and aborted_inner == [True]
    assert FakePopen.spawned == [proc]  # the follow-up turn never started


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return e


def test_routed_abort_reaches_the_running_agent(tmp_path):
    """RoutedSession.abort (what the runner calls on a hard cap) stops the
    agent case's claude too."""
    from gauntlet.store import RunDir
    from tests.gauntlet import adapter as adapter_mod

    stopped = []

    class A:
        aborted = False

        def stop_claude(self):
            stopped.append(self.aborted)

    run = RunDir(tmp_path, "s").ensure()
    routed = adapter_mod.RoutedSession(adapter_mod.ScouterAdapter(tmp_path), "scenarios_agent",
                                       run)
    routed.agent = A()
    routed.abort()
    assert stopped == [True] and routed.agent is None


# -- final fix M4 ------------------------------------------------------------

def test_agent_write_flag_uses_the_one_write_tool_set():
    from gauntlet.adapter import Observation
    from tests.gauntlet.layers.common import WRITE_TOOLS

    names = sorted(WRITE_TOOLS | {"darkroom_get", "darkroom_open", "darkroom_status"})
    obs = Observation()
    agent.AgentSession._record_calls(
        obs, [{"name": f"mcp__scouter__{n}", "input": {}, "result": "{}"} for n in names])
    assert {c.tool for c in obs.calls if c.write} == set(WRITE_TOOLS)


def test_frame_path_shown_to_the_agent_never_names_the_diagnosis(tmp_path, monkeypatch):
    """(row 501 item F, night-1 rank 8, SCA-fixture-frame-mismatch-and-
    leak): r0037's agent read the staged frame's own directory name
    ("the folder is literally named warm_cast") as evidence for its
    diagnosis. The prompt (and the path in it) must carry no problem-name
    token from the case generator's problem list (tests.gauntlet.corpus.
    ROLES)."""
    from gauntlet.case import Case, Expect
    from tests.gauntlet.corpus import ROLES

    frame = tmp_path / "frames" / "warm_cast" / "IMG_0001.ORF"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"raw")

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return frame

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                       "params": {}})
    FakePopen.spawned = []
    popen = _popen_seq(_lines({"type": "result", "result": "ok", "session_id": "s1"}))
    s = agent.AgentSession(Inner(), popen=popen)
    case = Case(id="sca-leak", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="warm_cast",
                meta={"sentence": "fix the color balance", "followups": []})
    s.run_case(case)
    prompt = FakePopen.spawned[-1].argv[2]
    alias_path = prompt.rsplit(" is ", 1)[-1].rstrip(".")
    for token in ROLES:
        if token in ("hero", "neutral"):
            continue
        assert token not in alias_path, (token, alias_path)


def test_agent_session_parks_on_the_usage_budget_before_spawning_claude(tmp_path, monkeypatch):
    """(row 501 item H): the pass parks itself once the usage meter is over
    threshold, without ever spawning a claude turn."""
    from gauntlet.case import Case, Expect

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                       "params": {}})

    def over_budget():
        raise Park("usage_meter: 75% >= 60%")

    s = agent.AgentSession(Inner(), popen=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("popen must not be called when parked on budget")),
        check_usage_budget=over_budget)
    case = Case(id="sca-budget", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="hero",
                meta={"sentence": "warm it", "followups": []})
    with pytest.raises(Park, match="usage_meter"):
        s.run_case(case)


def test_mcp_config_pins_the_session_library(tmp_path):
    staged = type("S", (), {"env": {"PYTHONPATH": ".", "SCOUTER_CACHE": "/c",
                                    "SCOUTER_DARKTABLE_LIBRARY_DB": str(tmp_path / "l.db")}})()
    env = agent.mcp_config(staged)["mcpServers"]["scouter"]["env"]
    assert env["SCOUTER_DARKTABLE_LIBRARY_DB"] == str(tmp_path / "l.db")
