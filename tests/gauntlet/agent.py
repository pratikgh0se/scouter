"""Agent mode (spec 2.3, row 436): real Claude Code, Sonnet, through
`claude -p --output-format stream-json`, one conversation per case, follow-ups
through `--resume <session_id>`. The usage meter is ignored: when a limit
stops the batch the case parks (gauntlet.adapter.Park) and resumes later."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation, Park
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.session import SessionBlocked

LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|limit will reset|"
                      r"out of (extra )?usage|5-hour limit|weekly limit)", re.I)
RESET_RE = re.compile(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
TURN_TIMEOUT_S = 600


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
    if out["is_error"] and LIMIT_RE.search(out["text"] or ""):
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


def mcp_config(staged) -> dict:
    """Registers ONLY scouter, with the session's isolated env."""
    import sys

    env = {k: staged.env[k] for k in ("PYTHONPATH", "SCOUTER_CONFIG", "SCOUTER_CACHE",
                                      "OBSIDIAN_VAULT_PATH", "SCOUTER_JOB") if k in staged.env}
    return {"mcpServers": {"scouter": {"type": "stdio", "command": sys.executable,
                                       "args": ["-m", "scouter.mcp.server"], "env": env}}}


def claude_argv(prompt: str, mcp_json: Path, resume: str | None = None) -> list[str]:
    argv = ["claude", "-p", prompt, "--model", "sonnet", "--output-format", "stream-json",
            "--verbose", "--mcp-config", str(mcp_json), "--strict-mcp-config",
            "--allowedTools", "mcp__scouter__*"]
    if resume:
        argv += ["--resume", resume]
    return argv


class AgentSession:
    """Implements gauntlet.adapter.Session for layer scenarios_agent. Wraps a
    ScouterSession (darktable + staged frames); the MCP server is Claude's."""

    def __init__(self, inner, *, run=subprocess.run, clock=time.monotonic) -> None:
        self.inner = inner
        self.run = run
        self.clock = clock

    def _turn(self, prompt: str, resume: str | None) -> dict:
        path = self.inner.root / "mcp.json"
        path.write_text(json.dumps(mcp_config(self.inner.staged)))
        p = self.run(claude_argv(prompt, path, resume), capture_output=True, text=True,
                     timeout=TURN_TIMEOUT_S, cwd=str(self.inner.root))
        parsed = parse_stream(p.stdout.splitlines())
        if parsed["limit"]:
            raise Park(f"agent usage limit: {parsed['limit'][:120]}",
                       until=reset_time(parsed["limit"]))
        return parsed

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
            obs.calls.append(CallRecord(name, c["input"] or {}, env,
                                        write=name.startswith(("darkroom_set", "darkroom_adjust",
                                                               "darkroom_say", "preset_",
                                                               "darkroom_undo"))))

    def run_case(self, case: Case) -> Observation:
        t0 = self.clock()
        obs = Observation()
        try:
            frame = self.inner.ensure_frame(case.frame_role or "hero")
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
        prompt = f"{case.meta['sentence']}\n\nThe photo open in darktable is {frame}."
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
        self.inner.abort()

    def close(self) -> None:
        self.inner.close()
