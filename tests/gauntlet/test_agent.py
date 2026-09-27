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


def test_limit_text_parks_even_when_is_error_is_unset():
    """Fix round 1 (Opus finding 8): capsule-corp's run.sh notes the CLI can
    send the limit text with `is_error` unset. A no-tool-call turn matching
    the limit text must still park, whatever `is_error` says."""
    p = agent.parse_stream(_lines({"type": "result", "is_error": False,
                                   "result": "You've hit your session limit · resets "
                                             "3:30pm (Asia/Bangkok)"}))
    assert p["limit"]


def test_limit_text_with_tool_calls_and_no_error_is_not_parked():
    """Control: a turn that actually did work (has tool calls) and never
    errored is never mistaken for a limit hit, even if its own free text
    happens to echo limit-like wording."""
    lines = _lines(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__scouter__darkroom_say",
             "input": {"phrase": "brighter"}}]}},
        {"type": "result", "result": "Done -- note the vignette's own usage limit setting was "
                                     "left untouched.", "is_error": False})
    p = agent.parse_stream(lines)
    assert p["limit"] is None


def test_argv_registers_only_scouter_and_sonnet():
    argv = agent.claude_argv("hi", "/tmp/m.json", resume="s1")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--strict-mcp-config" in argv and argv[argv.index("--resume") + 1] == "s1"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scouter__*"


def test_argv_whitelists_only_read_as_a_built_in_tool():
    """Fix round 1 (Opus finding 7): --disallowedTools named only three
    tools (Bash, Write, Edit), leaving Glob/Grep/WebFetch/WebSearch/
    NotebookEdit/Task reachable -- Glob/Grep alone can list a staged
    frame's directory. --tools Read (capsule-corp's own build_argv flag)
    whitelists Claude's own built-in tools to Read only."""
    argv = agent.claude_argv("hi", "/tmp/m.json")
    assert argv[argv.index("--tools") + 1] == "Read"
    assert "--disallowedTools" not in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scouter__*"


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


def test_frame_path_shown_to_the_agent_is_the_real_staged_path_and_names_no_diagnosis(
        tmp_path, monkeypatch):
    """(row 501 item F, fix round 1, Opus finding 1, BLOCKER): the earlier
    alias (a symlink under `frames_view/`) broke darktable's own path
    matching and still leaked the real, role-named path through every MCP
    envelope's own `path` field. Now the prompt's path is exactly the real,
    already-staged path the library, `ensure_frame` and every envelope
    share -- `session.stage()`'s `_neutral_role_dir` makes THAT path
    role-blind, so there is nothing left to substitute. Checked against the
    real corpus.ROLES list (the case generator's own problem list), not
    only against the request text."""
    from gauntlet.case import Case, Expect
    from tests.gauntlet.corpus import Corpus, ROLES
    from tests.gauntlet.session import stage

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "H.ORF").write_bytes(b"raw")
    roles = {r: "H.ORF" for r in ROLES}
    doc = {"frames": [{"rel": "H.ORF", "body": "olympus"}], "roles": {"olympus": roles}}
    staged = stage(Corpus(doc, trial), "olympus", tmp_path / "s1")
    real_path = staged.frames["warm_cast"]

    class Inner:
        root = tmp_path / "s1"
        floors = {}
        touched = set()

        def ensure_frame(self, role):
            #: This IS what ScouterSession.ensure_frame does on success: it
            #: hands back staged.frames[role] unchanged -- the exact path
            #: darkroom_open sent to the library.
            return real_path

    Inner.staged = staged
    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                       "params": {}})
    FakePopen.spawned = []
    #: A tool_result envelope carrying the real path back, as darkroom_open
    #: genuinely does -- proves the envelope's own `path` field is neutral
    #: too, not only the prompt text.
    envelope_lines = _lines(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__scouter__darkroom_open",
             "input": {"image": str(real_path)}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text",
                          "text": json.dumps({"ok": True, "data": {"path": str(real_path)}})}]}]}},
        {"type": "result", "result": "Looked at it, it's a touch dim.", "session_id": "s1"})
    popen = _popen_seq(envelope_lines)
    s = agent.AgentSession(Inner(), popen=popen)
    case = Case(id="sca-leak", layer="scenarios_agent", tier="P0", mode="agent", steps=[],
                expect=Expect("refuse"), body="olympus", frame_role="warm_cast",
                meta={"sentence": "fix the color balance", "followups": []})
    obs = s.run_case(case)
    prompt = FakePopen.spawned[-1].argv[2]
    prompt_path = prompt.rsplit(" is ", 1)[-1].rstrip(".")
    assert prompt_path == str(real_path)  # the prompt names the real, library-known path
    envelope_path = obs.calls[0].envelope["data"]["path"]
    for token in ROLES:
        assert token not in prompt_path, ("prompt", token, prompt_path)
        assert token not in envelope_path, ("envelope", token, envelope_path)


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
    with pytest.raises(Park, match="usage_meter") as exc_info:
        s.run_case(case)
    assert "usage_meter" in str(exc_info.value)


def test_run_case_stages_a_job_when_the_case_asks_for_one(tmp_path, monkeypatch):
    """(row 501 item I): sca-r0066-style cases (meta["stage_job"]) get a
    real staged job before the agent's first turn."""
    from gauntlet.case import Case, Expect

    staged_calls = []

    class Inner:
        root = tmp_path
        floors = {}
        touched = set()
        staged = type("S", (), {"env": {"PYTHONPATH": "."}})()

        def ensure_frame(self, role):
            return tmp_path / "f.ORF"

        def stage_job(self):
            staged_calls.append(True)

    monkeypatch.setattr(agent.observe, "snapshot", lambda p, k=None: {"fingerprint": "a",
                                                                       "params": {}})
    popen = _popen_seq(_lines({"type": "result", "result": "ok", "session_id": "s1"}))
    s = agent.AgentSession(Inner(), popen=popen)
    case = Case(id="sca-r0066-olympus-agent", layer="scenarios_agent", tier="P0", mode="agent",
                steps=[], expect=Expect("workflow", [], {"changed": False}), body="olympus",
                frame_role="hero", meta={"sentence": "client proof gallery", "followups": [],
                                        "stage_job": True})
    s.run_case(case)
    assert staged_calls == [True]


def test_mcp_config_pins_the_session_library(tmp_path):
    staged = type("S", (), {"env": {"PYTHONPATH": ".", "SCOUTER_CACHE": "/c",
                                    "SCOUTER_DARKTABLE_LIBRARY_DB": str(tmp_path / "l.db")}})()
    env = agent.mcp_config(staged)["mcpServers"]["scouter"]["env"]
    assert env["SCOUTER_DARKTABLE_LIBRARY_DB"] == str(tmp_path / "l.db")
