"""Agent mode (spec 2.3, row 436): real Claude Code, Sonnet, through
`claude -p --output-format stream-json`, one conversation per case, follow-ups
through `--resume <session_id>`. The usage meter is ignored: when a limit
stops the batch the case parks (gauntlet.adapter.Park) and resumes later."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation, Park
from gauntlet.case import Case
from tests.gauntlet import agent_budget, observe
from tests.gauntlet.layers.common import AGENT_TURN_TIMEOUT_S, WRITE_TOOLS
from tests.gauntlet.session import SessionBlocked

#: Night-1 triage rank 2 (SCA-session-limit-unparked): the Claude CLI's own
#: "session limit" wording ("You've hit your session limit · resets
#: 3:30pm (Asia/Bangkok)") is a distinct phrase from the usage/rate-limit
#: forms below and was never matched, so the case was scored as the agent's
#: own answer instead of parked (14 cases: 12 partial, 1 fail, 1 hollow pass
#: sca-r0159).
LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|limit will reset|"
                      r"out of (extra )?usage|5-hour limit|weekly limit|session limit)", re.I)
RESET_RE = re.compile(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
TURN_TIMEOUT_S = AGENT_TURN_TIMEOUT_S


def parse_stream(lines: list[str]) -> dict:
    """{session_id, text, tool_calls: [{name, input, result}], is_error, limit}."""
    out: dict = {"session_id": None, "text": "", "tool_calls": [], "is_error": False,
                 "limit": None}
    pending: dict[str, dict] = {}
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        out["session_id"] = msg.get("session_id") or out["session_id"]
        if kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    call = {"name": block.get("name"), "input": block.get("input"), "result": None}
                    pending[block.get("id")] = call
                    out["tool_calls"].append(call)
        elif kind == "user":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    pending[block["tool_use_id"]]["result"] = content
        elif kind == "result":
            out["text"] = msg.get("result") or ""
            out["is_error"] = bool(msg.get("is_error"))
    #: Fix round 1 (Opus finding 8): capsule-corp's own run.sh notes the
    #: limit text can arrive with `is_error` unset. A turn that matched the
    #: limit text AND made no tool calls is never the agent's own scored
    #: answer either way, so it parks independent of `is_error`.
    if LIMIT_RE.search(out["text"] or "") and (out["is_error"] or not out["tool_calls"]):
        out["limit"] = out["text"]
    return out


def reset_time(text: str, now: datetime | None = None) -> float | None:
    m = RESET_RE.search(text or "")
    if not m:
        return None
    now = now or datetime.now()
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        hour = hour % 12 + (12 if m.group(3).lower() == "pm" else 0)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def neutral_frame_path(root: Path, role: str, frame: Path) -> Path:
    """(row 501 item F, night-1 triage rank 8, SCA-fixture-frame-mismatch-
    and-leak): the staged frame's own directory is the corpus ROLE, which
    for several roles literally spells the diagnosis (`warm_cast`,
    `cool_cast`, `blown`, `flat`, `noisy`, `dark`, `bright`) --
    r0037's agent read its own tool-result path and used the folder name as
    evidence ("the folder is literally named warm_cast") instead of judging
    the photo it was shown. The prompt must never hand the agent a path
    that names the answer, so it gets a neutral alias -- a symlink (falling
    back to a copy) under a hashed, role-blind directory name, inside the
    session -- instead of the real staged path. The original file's name is
    kept (never a diagnosis word for the corpora this harness stages)."""
    alias_dir = root / "frames_view" / ("f" + hashlib.sha1(role.encode()).hexdigest()[:6])
    alias_dir.mkdir(parents=True, exist_ok=True)
    alias = alias_dir / frame.name
    if not alias.exists():
        try:
            alias.symlink_to(frame)
        except OSError:
            alias.write_bytes(frame.read_bytes())
    return alias


def mcp_config(staged) -> dict:
    """Registers ONLY scouter, with the session's isolated env."""
    import sys

    # (final fix M4): the library is pinned too, as session.isolated_env pins it
    # for the tool-mode MCP child -- never a running darktable's or the user's.
    env = {k: staged.env[k] for k in ("PYTHONPATH", "SCOUTER_CONFIG", "SCOUTER_CACHE",
                                      "OBSIDIAN_VAULT_PATH", "SCOUTER_JOB",
                                      "SCOUTER_DARKTABLE_LIBRARY_DB") if k in staged.env}
    return {"mcpServers": {"scouter": {"type": "stdio", "command": sys.executable,
                                       "args": ["-m", "scouter.mcp.server"], "env": env}}}


def claude_argv(prompt: str, mcp_json: Path, resume: str | None = None) -> list[str]:
    # (row 501 item E, night-1 triage rank 12, SCA-agent-sandbox-leak): with
    # only --allowedTools naming the scouter MCP tools, Claude's own
    # sandbox tools (Bash, Write, Edit) were still reachable -- r0031's
    # agent used Bash 8 times to grep its own tool-result file, and r0066's
    # used `find` on the session dir, both recorded as CallRecords with
    # `error:unparsed` (harness noise, not a tool failure) instead of being
    # unavailable in the first place. Read stays allowed (scout_preview
    # PNGs).
    argv = ["claude", "-p", prompt, "--model", "sonnet", "--output-format", "stream-json",
            "--verbose", "--mcp-config", str(mcp_json), "--strict-mcp-config",
            "--allowedTools", "mcp__scouter__*", "--disallowedTools", "Bash,Write,Edit"]
    if resume:
        argv += ["--resume", resume]
    return argv


class AgentAborted(RuntimeError):
    """The session was aborted (hard cap): no further claude turn may start."""


class AgentSession:
    """Implements gauntlet.adapter.Session for layer scenarios_agent. Wraps a
    ScouterSession (darktable + staged frames); the MCP server is Claude's.

    (final fix C1): each `claude -p` turn runs through Popen in its own
    process group (start_new_session) and the live handle is kept, so
    abort() -- the runner's hard-cap path -- kills the whole group (claude
    and the MCP server it spawned) instead of leaving it running up to
    TURN_TIMEOUT_S against the next session's darktable."""

    def __init__(self, inner, *, popen=subprocess.Popen, killpg=os.killpg,
                 clock=time.monotonic, check_usage_budget=agent_budget.check_usage_budget
                 ) -> None:
        self.inner = inner
        self.popen = popen
        self.killpg = killpg
        self.clock = clock
        #: (row 501 item H): the pass parks itself (Park("usage_meter: ...")
        #: -- caught the same way the limit-Park above already is) once the
        #: usage meter passes its threshold, instead of running every turn
        #: until the CLI's own wall limit hits mid-case.
        self.check_usage_budget = check_usage_budget
        self.proc = None
        self.aborted = False

    def _turn(self, prompt: str, resume: str | None) -> dict:
        if self.aborted:
            raise AgentAborted("agent session aborted; no further claude turn")
        self.check_usage_budget()
        path = self.inner.root / "mcp.json"
        path.write_text(json.dumps(mcp_config(self.inner.staged)))
        proc = self.popen(claude_argv(prompt, path, resume), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                          cwd=str(self.inner.root), start_new_session=True)
        self.proc = proc
        if self.aborted:  # abort() ran between the check above and the handle
            self.stop_claude()
            raise AgentAborted("agent session aborted while a claude turn started")
        try:
            stdout, _ = proc.communicate(timeout=TURN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.stop_claude()
            raise
        finally:
            if self.proc is proc and proc.poll() is not None:
                self.proc = None
        if self.aborted:
            raise AgentAborted("agent session aborted during a claude turn")
        parsed = parse_stream((stdout or "").splitlines())
        if parsed["limit"]:
            raise Park(f"agent usage limit: {parsed['limit'][:120]}",
                       until=reset_time(parsed["limit"]))
        return parsed

    def stop_claude(self) -> None:
        """Kill the running claude turn's process group (ours: we started it
        with start_new_session, so its pgid is its pid) and reap it."""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            self.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001 - reaping is best effort
            pass

    @staticmethod
    def _record_calls(obs: Observation, calls: list[dict]) -> None:
        for c in calls:
            env = None
            if isinstance(c["result"], str):
                try:
                    env = json.loads(c["result"])
                except ValueError:
                    env = {"ok": False, "outcome": "error:unparsed", "raw": c["result"][:300]}
            name = (c["name"] or "").removeprefix("mcp__scouter__")
            # (final fix M4): the one write-tool set (layers.common.WRITE_TOOLS).
            obs.calls.append(CallRecord(name, c["input"] or {}, env,
                                        write=name in WRITE_TOOLS))

    def run_case(self, case: Case) -> Observation:
        t0 = self.clock()
        obs = Observation()
        try:
            frame = self.inner.ensure_frame(case.frame_role or "hero")
            if case.meta.get("stage_job") and getattr(self.inner, "stage_job", None):
                #: (row 501 item I): a real staged job for cases whose
                #: product tool (library_jobs/export_release) needs one to
                #: point at, e.g. sca-r0066 (client proof gallery).
                self.inner.stage_job()
        except SessionBlocked as e:
            obs.blocked = f"session: {e}"
            obs.hang_evidence.append(f"session: {e}")
            obs.wall_s = self.clock() - t0
            return obs
        kelvin = observe.kelvin_for({"olympus": "em10iii"}.get(case.body or ""))
        obs.before = observe.snapshot(frame, kelvin)
        stats = any(c.metric in ("M2", "M3", "M4", "M5", "M6") for c in case.expect.checks)
        masks = None
        if stats:
            r = observe.render_raster(frame, self.inner.root / "render" / "before.ppm")
            obs.stats_before, masks = observe.stats(r)
            obs.floors = dict(self.inner.floors)
        alias = neutral_frame_path(self.inner.root, case.frame_role or "hero", frame)
        prompt = f"{case.meta['sentence']}\n\nThe photo open in darktable is {alias}."
        texts: list[str] = []
        calls: list[dict] = []
        try:
            parsed = self._turn(prompt, None)  # a Park here parks the whole case (fresh on resume)
            texts.append(parsed["text"])
            calls.extend(parsed["tool_calls"])
            for f in case.meta.get("followups", []):
                more = self._turn(f, parsed["session_id"])
                texts.append(more["text"])
                calls.extend(more["tool_calls"])
        except subprocess.TimeoutExpired as e:
            obs.blocked = "hang: claude turn exceeded TURN_TIMEOUT_S"
            obs.hang_evidence.append(f"hang: claude turn exceeded TURN_TIMEOUT_S: {e}")
            self._record_calls(obs, calls)
            obs.agent_text = "\n\n".join(texts) if texts else None
            obs.wall_s = self.clock() - t0
            return obs
        self._record_calls(obs, calls)
        obs.agent_text = "\n\n".join(texts)
        obs.after = observe.snapshot(frame, kelvin)
        if stats:
            r = observe.render_raster(frame, self.inner.root / "render" / "after.ppm")
            obs.stats_after, _ = observe.stats(r, masks)
        self.inner.touched.add("*")
        obs.wall_s = self.clock() - t0
        return obs

    def abort(self) -> None:
        self.aborted = True
        try:
            self.stop_claude()
        finally:
            self.inner.abort()

    def close(self) -> None:
        try:
            self.stop_claude()
        finally:
            self.inner.close()
