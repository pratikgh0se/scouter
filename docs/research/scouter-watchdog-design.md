# Scouter bridge watchdog: design proposal (row 434 candidate, no code)

**Goal:** the CLI and MCP server never hang on darktable. Each call ends in one of three ways: an answer, a typed error, or (when opted in) one supervised heal and a single retry.

**Today:** row 432 enforces the per-call timeout (`timeout_s` bounds the bridge wait), and row 433 adds the plugin's GUI-ready gate with its `gui_not_ready` answer.

## 1. What already exists (reuse, do not reinvent)

- **Bench runner.** `run_editor_benchmark.py` has:
  - `DarktableHost.launch()`: Popen with `--library`, then poll `view.current` until darkroom, then `darkroom.current` until it shows the frame.
  - `alive()`: `view.current` with a 10 s limit.
  - `quit()`: osascript quit, then wait 20 s, SIGTERM 10 s, SIGKILL.
  - `_setup_hung()` followed by "relaunch once, redo the steps that failed". This is exactly the heal loop, but it lives in test code.
- **Acceptance driver.** `acceptance_driver.py:226-258` has the same launch/quit ladder.
- **Plugin.** `darktable_mcp.diag` already works as a cheap read-only ping. Row 433 added `gui_ready`, `gui_ready_source` and `since_load_s` to it.

**Proposal:** move launch, alive, and the quit ladder into a new product module, `scouter/core/dtprocess.py`. The bench and acceptance drivers then import it, and the logic exists once.

## 2. Behaviour

1. **Health ping (5 s).** `dtbridge.health()` calls `darktable_mcp.diag` with `timeout=5`. The plugin exempts this verb from the GUI gate, and it touches no gtk-wrapped API, so an answer proves the Lua worker is alive.
   - It runs only after a call fails with a bridge timeout. It is never a background poll, which would keep the battery/poll-cost win from the adaptive poll.
   - Outcomes: `alive`, `starting` (`gui_ready=false`), or `unresponsive` (no answer within 5 s).
2. **Typed error.** New `DarktableUnresponsive(ScouterError)` in `scouter/errors.py`, with `code="darktable_unresponsive"` and an exit code in `EXIT_CODES`.
   - Detail fields: `pid`, `launched_by_scouter`, `last_method`, `ping_ms`, and `heals_used`/`heals_max`.
   - Fix text: "darktable stopped answering (Lua worker or GTK main loop wedged); quit it (Force Quit if needed) and relaunch, or enable `watchdog.auto_heal`."
   - It is raised only when the call timed out AND the ping failed. A slow verb with a live ping stays the existing `commit_timeout`/`bridge_timeout`.
3. **Auto-heal (opt-in).**
   - Config: `watchdog.auto_heal: false` (default) and `watchdog.max_heals_per_job: 1`.
   - Heal sequence: SIGTERM, wait 10 s, SIGKILL. Then relaunch with the same `--library` and `--configdir` Scouter recorded at launch. Re-open the job's frame with `darkroom.open` on the image in `state.opened_image`, then wait for `gui_ready`, capped at 30 s. Retry the failed call once.
   - The retry is safe because the journal already records whether a write landed:
     - Writers (`set_param`, `nudge`, `enable`/`reset`, `undo_last`) re-check through `_raw_and_before_state` / `_noop_shortcut` before re-sending.
     - Reads retry blindly.
     - `library_import` and `job.run` are never auto-retried. They report the heal, and the caller re-runs them.
   - The counter lives in job state, so it is per job and survives the MCP session. Once the counter is used up, the call raises `DarktableUnresponsive` with no further heal.
4. **Telemetry.** Every ping and heal appends to the job's `events.jsonl` and `metrics.db` using the existing event schema:
   - `verb="watchdog.ping"` or `"watchdog.heal"`
   - `outcome` is `alive`, `starting`, `unresponsive`, `healed`, `heal_failed` or `refused_not_ours`
   - `ms`, plus `detail.pid_old` and `detail.pid_new`
   It is also written to the audit log line of the call that triggered it.
5. **`scouter doctor`** gains these lines:
   - `watchdog: ping alive|starting|unresponsive (N ms)`
   - `gui_ready: true|false (source)`
   - `darktable pid: N (launched by scouter | user)`
   - `auto_heal: on|off, heals this job: k/max`
   - `last heal: <ts> <outcome>`
   `report["ok"]` stays a statement about the bridge; `unresponsive` makes it false.

## 3. Exact changes

- **`darktable_mcp/bridge/client.py`:** add `"darktable_mcp.diag": 5.0` to the health budget (a new `HEALTH_TIMEOUT`). `Bridge.call` is unchanged.
- **`scouter/core/dtbridge.py`:**
  - New `health() -> dict`.
  - In `call()`, on a `BridgeTimeoutError` from any method other than diag, run `health()`. If it answers `unresponsive`, raise `DarktableUnresponsive`. Otherwise raise the current `BridgeTimeout`. Keep the `gui_not_ready` and `DbLocked` mappings.
  - Guard so the ping never recurses.
- **`scouter/core/dtprocess.py` (new):**
  - `launch(frame, library, configdir) -> Launched`. It writes `cache_root/darktable.owner.json` containing `{pid, start_time, library, configdir, frame}`.
  - `owned_pid()`: pid alive, `ps -o lstart` equals the recorded start_time, and the executable path matches.
  - `terminate(pid)`, which is the ladder, and `wait_gui_ready(timeout)`.
- **`scouter/core/darkroom.py`:** `open_image()` and `_commit()` call `watchdog.with_heal(fn, verb, job)` (new helper in `scouter/core/watchdog.py`). The helper catches `DarktableUnresponsive`, checks config and the counter, calls `dtprocess.terminate`, `launch` and `open_image(state.opened_image)`, then retries once. The `set_param`/`nudge` pre-write readback already makes the retry idempotent.
- **`scouter/mcp/server.py`:**
  - `ScouterServer.call()` maps `DarktableUnresponsive` into the envelope. This already happens through `registry.call`'s `ScouterError` path, so the only addition is an `outcome` of `error:darktable_unresponsive`.
  - The session job pinned by `_pin_session_job` carries the heal counter.
  - Never run a heal inside `_on_call_tool` without the per-call deadline. The heal gets its own budget, `watchdog.heal_budget_s` (default 60), and reports it.
- **`scouter/cli/main.py`:** `_doctor()` adds the lines in section 2.5. Two new flags: `--ping`, which prints only the watchdog line and exits 0/1 for scripts and launchd, and `--heal`, a manual heal that obeys the same ownership rule.

## 4. Tests

- **Fake bridge that never answers.** A `FakeBridge` mode where every method raises `BridgeTimeoutError` after a sleep, and diag does the same.
  - Assert `DarktableUnresponsive` is raised within `timeout_s + 5 s + margin`.
  - Assert it is not raised when diag answers (the result stays `BridgeTimeout`/`CommitTimeout`).
  - Assert diag answering `gui_ready=false` gives `starting`, with a retry fix.
- **Fake darktable process.** A tiny Python script launched through `dtprocess.launch` that ignores SIGTERM and never answers the spool.
  - Assert the ladder escalates to SIGKILL within budget.
  - Assert the owner file matches.
  - Assert a heal relaunches, and that the retry answers once a second fake answers.
  - Assert `max_heals_per_job` stops the second heal.
  - Assert telemetry rows are written.
- **Ownership.**
  - A foreign pid (no owner file, or a start_time mismatch because the pid was reused) gives `refused_not_ours`, and no signal is sent. Assert with a spy on `os.kill`.
- **Retry idempotence.** Heal after a write that did land: the journal readback detects it and there is no double nudge. Heal after `job.run` gives no auto-retry.
- **Doctor.** Snapshot tests for each watchdog line state.
- **Host (opt-in, `SCOUTER_HOST=1`).** Wedge the real worker, for example by running the probe route in a stub that sleeps forever, and verify an end-to-end heal on a scratch library.

## 5. Risks and rules

- **Never kill a darktable Scouter did not launch.** A user's own session may hold unsaved darkroom history, since darktable autosaves only every 10 s. The heal requires `owned_pid()` to be true. Otherwise it answers `refused_not_ours` and gives the Force Quit instruction. Matching the pid with `start_time` defends against pid reuse.
- **Library lock after SIGKILL.** darktable leaves `library.db.lock` and `data.db.lock` behind. Remove them only when the owner file's pid is confirmed dead and the lock's pid matches it. Never delete a lock held by a live process. A lock we cannot clear gives `DbLocked` with no relaunch.
- **Heal loops and masking bugs.** Heals are opt-in, capped per job, always reported (telemetry plus a `healed: true` field in the envelope), and never used by the benchmark scorer. The benchmark must record heals as failures of the step.
- **Unsaved edits lost on SIGKILL.** The retry re-applies only journalled writes. A hand edit made in the GUI within the last 10 s is lost, and the heal message says so.
- **Relaunch in the startup window.** This is what row 433's gate is for. The heal waits for `gui_ready` before the retry. It does not trust `view.current`.
