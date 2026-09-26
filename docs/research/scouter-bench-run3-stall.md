# Scouter row 433: why benchmark run 3 still stalled

Fix: branch `fix/bench-stall`, commit `1d62110`, worktree `/Users/pratik/CodeBase/scouter/.worktrees/benchstall`, based on main `e8887c6`. Not merged, not pushed. The bench/editor runner is unchanged.

## Verdict

- **Stalled call:** the session setup step `history_commit {image: <frame>, timeout_s: 10}`. It is the first gtk-touching call after each launch. It was sent about 2.4 s after the launch, as soon as `darkroom.current` reported the frame.
- **Wall time:** Python gave up after 10.0 s (`error:commit_timeout`, `bridge_timeout: true`, ms 10002 to 10056). darktable never recovered.
- **Launches affected:** every one tonight, 16 of 16 (8 olympus, 8 sony). That includes every relaunch.
- **Where the stall is:** inside darktable, a self-deadlock that our Lua plugin triggers. It is not in the Python bridge or the runner. The bridge enforced `timeout_s` exactly as row 432 intended.
- **Did the plugin have the row 432 fix?** Yes. The installed `~/.config/darktable/lua/darktable_mcp.lua` has sha1 `22140d43…`, the same as `main:darktable_mcp/lua/darktable_mcp.lua` and `bench/editor`. It was installed at 16:18 by `scouter setup`.
- **Same cause as row 432?** No. It is a new root cause, and it was also behind most of the earlier "B1 first-commit hangs". Row 432 fixed a real but secondary problem: an unbounded bounce, and the bridge ignoring timeout_s.

## Evidence

1. **Audit log.** `bench-runs/20260926t092247z/olympus/cache/audit/mcp-20260926-092248-10234.jsonl` has 8 × `history.commit` calls, each ending `commit_timeout` with the message "darktable did not answer history.commit … within 10s". That message is the row 432 silent-plugin path, so the plugin never replied. `bench-runs/20260926t093533z/sony/…-13510.jsonl` shows the same four times.
2. **Results JSON.** `.worktrees/bench2/tests/benchmark/results/20260926t092247z-olympus.json`: every session has `hung_first_launch` and `hung_again`, and every quit fell back to **SIGKILL**. osascript quit was ignored, which means the GUI was not responding. The other four runs from today (073459z, 074955z, 083159z, 084654z) show the same thing: every first launch hung and needed SIGKILL.
3. **Timing.** Each launch-and-hang cycle takes about 82 s, which matches the darktable.log launch stamps (16:22:48, 16:24:11, 16:25:34, …). The cycle is 2.4 s to become ready, a 10 s commit, a 10 s `alive()` (`view.current` also gets no answer, because the single Lua worker is wedged), then about 60 s in `quit()` (osascript 30 s, wait 20 s, SIGTERM 10 s, then SIGKILL). The "0 % CPU, sleeping, 2+ min" state Pratik saw in session C was darktable deadlocked while the runner sat in `library_import`'s 120 s bridge wait and then in the quit ladder.
4. **The stack.** `/Library/Logs/DiagnosticReports/darktable_2026-09-26-150440_Pratiks-MacBook-Pro.hang` is from PID 51192, bench run 074955z sony, launched 15:03:02. It was unresponsive from about 4 s after launch. `Deadlocked: 1 thread - "pool-1" -> "pool-1"`.
   - **pool-1:** `run_async_thread_main → lua_pcallk (our worker) → gtk_wrap → g_main_context_invoke_full → dt_lua_gtk_wrap_callback → g_mutex_lock`. This thread is blocked by itself.
   - **main:** `apple_main → dt_gui_gtk_run → gtk_main → g_main_loop_run → g_main_context_wait_internal → g_cond_wait`. It is waiting to own the default context.
   - **lua thread:** `alien_job_dispatch → dt_lua_lock_internal`, waiting.
5. **The mechanism, from darktable 5.4.1 source (release-5.4.1 tag, fetched):**
   - `src/lua/call.c:665` `g_mutex_lock(&communication.end_mutex)`, then `:666` `g_main_context_invoke_full(NULL, …, dt_lua_gtk_wrap_callback, …)`. GLib runs the callback **inline** when the calling thread can acquire the default context, which it can whenever no thread owns it. The callback then locks the same non-recursive mutex again at `call.c:639`. The thread deadlocks on itself and keeps the context, so the main thread's `gtk_main` waits for ever.
   - The default context has no owner between `dt_lua_init` (`src/common/darktable.c:1949`, where our plugin loads and `dt.control.dispatch(worker_loop)` starts) and `gtk_main` (`src/gui/gtk.c:1631`). The comment at `darktable.c:1947` says no events are pumped after Lua init.
   - The command-line image is loaded into the darkroom within that window (`darktable.c:1980` `dt_load_from_string`). So `view.current` = darkroom and `darkroom.current` = the frame are already true. The runner's readiness check (`run_editor_benchmark.py:926-977`, `launch()`) passes, and the next call hits the window.
   - The first gtk-wrapped call in our code is `commit_no_bounce` → `pcall(dt.gui.selection, {image})` at `darktable_mcp/lua/darktable_mcp.lua:1847` on main (`e8887c6`). `dt.gui.selection`, `dt.gui.action`, `dt.gui.mimic` and `display_image` are all gtk-wrapped (`src/lua/gui.c:417-433`). The plugin's own notes at `:987-1001` already said that `dt.gui.current_view(v)` hits the un-owned-context race (row 418). They assumed the gtk-wrapped calls were safe, and they are not.
6. **Why acceptance run 3 passed.** Its darkroom steps come after `library_import`, cull and similar steps, many seconds after launch, so `gtk_main` was already running. The bench is the only driver that makes a gtk-wrapped call about 2 s after a cold launch.

### Hypotheses ruled out

- **Pixelpipe race in `darkroom_open`, or `scout_stats` / preview export blocking.** Neither was ever reached; the first call hangs.
- **Relaunch reusing a locked `library.db`, or a modal dialog.** A modal dialog would run a nested main loop that owns the context. The GUI would then be busy, not deadlocked, and a DB lock surfaces as `DbLocked`. The stack shows neither. The hang is the same on first launches that follow a clean quit.
- **`gtk_window_add_accel_group` criticals.** Benign. They appear at every launch, including launches that worked.
- **Python side.** It behaved correctly and returned within `timeout_s`.

### After Pratik's force quit

Run 093533z sony is the same deadlock on all four launches. `acceptance/runs/20260926t091801z/bench-sony.stdout.log` was written at 16:39:45 with no output, and the MCP server died with BrokenPipe at 16:39:53. So the runner process ended at about 16:39:45, probably killed. That orphaned darktable PID 15769 (launched 16:39:41), still deadlocked, which is the "(not responding)" process seen until the controller killed it at about 16:58.

## Fix (1d62110)

The GUI-ready gate in `darktable_mcp.lua`:
- Every verb except `view.current`, `darkroom.current`, `darktable_mcp.diag` and `image.lookup` waits, using yield_sleep, until darktable's main loop is provably running.
- The proof is the first `pixelpipe-processing-complete` event, which the plugin registers for at load. After Lua init, a darkroom pipe starts only from `expose()` (`views/darkroom.c:682-684`), a GTK draw that only `gtk_main` dispatches. After `gtk_main` starts, the context stays owned until quit.
- Lighttable launches run no pipe, so the gate also opens 20 s after the plugin loads.
- The wait is bounded by the caller's `budget_ms` (default 10 s). Time spent waiting is taken off `budget_ms` before the commit runs.
- If the gate is still closed at the end of the wait, the verb answers `gui_not_ready: … Nothing was done; retry in a few seconds.` and never touches a gtk-wrapped API.
- `darktable_mcp.diag` now reports `gui_ready`, `gui_ready_source`, `gui_ready_event_registered` and `since_load_s`.
- `scouter/core/dtbridge.py` maps `gui_not_ready` to a `BridgeTimeout` with `detail.gui_not_ready = true` and a "retry" fix, never "restart darktable".

For the bench, the first `history_commit` should now wait about 1-3 s for the first pipe and then return the row 432 noop in about 2 s.

## Tests

Each test failed before the fix and passes after it.
- `tests/lua/test_dispatcher.lua`, row 433 block:
  - The plugin registers the pixelpipe handler at load.
  - While the gate is closed, `history.commit` never calls `dt.gui.selection` or `dt.gui.action`, answers `gui_not_ready`, and yields within `budget_ms`.
  - The exempt verbs answer, and diag reports `gui_ready=false`.
  - An event fired mid-wait releases the held call, and diag reports the source.
  - The elapsed-time fallback opens the gate.
  - The harness fires the event once after load, so all earlier tests run against a GUI that is up.
- `tests/test_dtbridge.py::test_gui_not_ready_is_a_retryable_bridge_timeout_not_a_restart`.

Full pytest: 1437 passed, 23 skipped. Lua suite: OK.

## What remains

1. **Live check (not run; darktable not launched per instructions).** After merge and `scouter setup`, run one bench session (`--sessions A`). Expected: no `hung_first_launch`, `history_commit` answers noop in under 12 s, and quit by osascript. Also run `scouter doctor` right after a cold launch and check that `gui_ready` goes from false to true.
2. **Residual risk.** In a lighttable-only launch where darktable spends more than 20 s between Lua init and `gtk_main` (a huge library or slow disk), the fallback could open the gate too early. The pipe event covers every darkroom launch, which is what the bench, acceptance and adjust checks use.
3. **Runner (bench/editor, optional).** `DarktableHost.launch` could poll `darktable_mcp.diag` until `gui_ready` before it returns ready. Its `quit()` ladder spends about 60 s per wedged darktable, so SIGKILL could come sooner when `alive()` has already failed. Neither change is needed with the plugin fix.
4. **Upstream.** darktable's `gtk_wrap` should not run `g_main_context_invoke_full` inline from a non-GUI thread (or it should use `g_main_context_invoke` with a recursive or unlocked handshake). This is worth an upstream issue, and the hang report stack is the reproduction.
5. **Row 431/432 write-up correction.** The 60 s `bridge_timeout`s in 083159z that it attributed to the unbounded bounce were most likely this deadlock, since every quit that run was SIGKILL. The row 432 changes (a bounded bridge wait and noop) are still correct and are what made tonight's failure show up quickly and cleanly.
