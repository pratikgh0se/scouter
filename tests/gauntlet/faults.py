"""Layer 5 fault injection (spec 3.5). Each fault arms a condition on the
session's OWN darktable, frames and dirs (never the user's), and returns the
extra placeholders its steps use. `disarm` always runs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case
from tests.gauntlet import observe
from tests.gauntlet.corpus import copy_readonly
from tests.gauntlet.execute import VIRTUAL, substitute

#: Seconds after the fault step's call STARTS before kill_during_call fires.
KILL_DELAY_S = 1.0
#: Headroom left free on the disk-full image so filling it never ENOSPCs.
FILL_HEADROOM = 256 * 1024
_CHUNK = 1024 * 1024
_statvfs = os.statvfs  # seam: tests pass a fake


def _owned_kill(sig=signal.SIGKILL) -> None:
    """Signal ONLY the darktable this session launched (dtprocess.owned_pid())."""
    from scouter.core import dtprocess

    pid = dtprocess.owned_pid()
    if pid:
        os.kill(pid, sig)


def _snap(v: dict, path: Path) -> None:
    """Record a path's exact state (existence, bytes or dir, mode, times) so
    `disarm` can put it back byte-for-byte. The first snapshot of a path wins."""
    path = Path(path)
    snaps = v.setdefault("_restore", [])
    if any(s["path"] == str(path) for s in snaps):
        return
    if not os.path.lexists(path):
        snaps.append({"path": str(path), "exists": False})
        return
    st = path.stat()
    snaps.append({"path": str(path), "exists": True, "dir": path.is_dir(),
                  "bytes": None if path.is_dir() else path.read_bytes(),
                  "mode": st.st_mode & 0o7777, "ns": (st.st_atime_ns, st.st_mtime_ns)})


def _restore(snap: dict) -> None:
    path = Path(snap["path"])
    if not snap["exists"]:
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
            path.rmdir()  # only an empty dir the fault created; never a tree
        else:
            path.unlink(missing_ok=True)
        return
    if snap["dir"]:
        path.chmod(snap["mode"])
    else:
        if path.exists():
            path.chmod(0o600)  # the fault may have left it read-only
        path.write_bytes(snap["bytes"])
        path.chmod(snap["mode"])
    os.utime(path, ns=snap["ns"])


def _start_timer_on_call(session, v: dict) -> None:
    """kill_during_call: start the kill timer when the fault step's call starts
    (the first non-darkroom_open call; ensure_frame's open does not count), not
    at arm time. `disarm` cancels it and restores `session.call`."""
    orig = session.call
    had_own = "call" in vars(session)
    v["_restore_call"] = (had_own, orig)

    def call(tool, *args, **kwargs):
        if tool != "darkroom_open" and not v.get("_timer_started"):
            v["_timer_started"] = True
            v["_timer"].start()
        return orig(tool, *args, **kwargs)

    session.call = call


def arm(session, fault: str, v: dict | None = None) -> dict:
    """Arm `fault`. Every undo record lands in `v` as it is made, so a
    `disarm(session, v)` after a partial arm (arm raised) undoes what was done."""
    v = {} if v is None else v
    st = session.staged
    hero = st.frames["hero"]
    side = Path(str(hero) + ".xmp")
    if fault == "quit_darktable":
        v["_relaunch"] = True
        session.dt.terminate()
    elif fault == "kill_during_call":
        v["_relaunch"] = True
        timer = threading.Timer(KILL_DELAY_S, _owned_kill)
        timer.daemon = True
        v["_timer"] = timer
        _start_timer_on_call(session, v)
    elif fault in ("wedge_worker", "slow_bridge"):
        v["_resume_sigcont"] = True  # before the signal: a raise still resumes
        _owned_kill(signal.SIGSTOP)  # frozen process: the bridge cannot answer
    elif fault == "relaunch_stale_job":
        session.relaunch()
    elif fault == "delete_sidecar":
        _snap(v, side)
        if side.exists():
            side.unlink()
    elif fault == "chmod_sidecar_readonly":
        _snap(v, side)
        side.touch()
        side.chmod(0o444)
        v["_restore_mode"] = str(side)  # informational; `_restore` undoes it
    elif fault == "stale_sidecar":
        _snap(v, side)
        if side.exists():
            os.utime(side, (time.time() - 86400 * 30,) * 2)
    elif fault == "torn_sidecar_copy":
        torn = session.root / "torn" / hero.name
        copy_readonly(hero, torn)
        text = side.read_bytes() if side.exists() else b"<x:xmpmeta"
        Path(str(torn) + ".xmp").write_bytes(text[: len(text) // 2])
        v["torn_copy"] = str(torn)
    elif fault == "second_library_lock":
        lock = Path(str(st.library) + ".lock")
        _snap(v, lock)  # the live darktable's own lock: restored exactly
        lock.write_text(f"{os.getpid()}\n")
    elif fault == "unicode_path":
        v["unicode_copy"] = str(copy_readonly(hero, session.root / "ünï cødé dir" / f"frâme {hero.name}"))  # noqa: E501
    elif fault == "long_path":
        v["long_copy"] = str(session.root / ("x" * 255) / hero.name)
    elif fault == "none":
        v["missing_file"] = str(session.root / "does-not-exist.ORF")
        unsupported = session.root / "notes.txt"
        unsupported.write_text("not an image")
        v["unsupported_file"] = str(unsupported)
    elif fault == "duplicate_names":
        for sub in ("a", "b"):
            copy_readonly(hero, session.root / "dups" / sub / hero.name)
        v["bare_duplicate_name"] = hero.name
    elif fault == "disk_nearly_full":
        _small_disk(session, v)
    elif fault == "chmod_out_dir":
        d = session.root / "denied"
        _snap(v, d)
        d.mkdir(exist_ok=True)
        d.chmod(0o500)
        v["denied_dir"] = str(d)
        v["_restore_dir"] = str(d)  # informational; `_restore` undoes it
    elif fault == "future_mtime":
        _snap(v, side)
        side.touch()
        os.utime(side, (time.time() + 86400 * 365,) * 2)
    elif fault == "cold_launch_commit":
        session.relaunch()  # the step then runs ~1 s after launch, before any settle
    elif fault == "wrong_wb_table":
        wb = Path(st.env["SCOUTER_CONFIG"]).parent / "wb"
        src = wb / "em10iii.json"
        if src.exists():
            _snap(v, wb / "a7.json")
            (wb / "a7.json").write_bytes(src.read_bytes())
    elif fault == "broken_luarc":
        cfg = session.root / "broken-configdir"
        (cfg / "lua").mkdir(parents=True, exist_ok=True)
        (cfg / "luarc").write_text('error("htc: deliberately broken luarc")\n')
        v["_relaunch"] = True  # before anything: disarm brings the normal one back
        # _quit raises SessionBlocked unless the quit ladder reports ok, so a
        # second darktable is never started beside the first.
        session._quit("broken_luarc")
        from scouter.core import dtprocess

        dtprocess.launch(hero, session.root / "broken-library" / "library.db", cfg)
    elif fault == "huge_job_one_timeout":
        from tests.gauntlet.corpus import build_card

        card = session.root / "huge" / "card"
        frames = build_card(session.corpus, case_body(session), 200, card)
        frames[100].write_bytes(frames[100].read_bytes()[:4096])  # a truncated raw
        v["_job_frames"] = (list(st.job_frames),)
        st.job_frames = frames
        v["card"] = str(card)
        v["bad_frame"] = frames[100].name
    elif fault == "modal_dialog":
        v["_skip"] = "no modal-dialog test hook in the darktable_mcp plugin (spec 3.5 H2)"
    elif fault in ("fake_version", "two_clients", "full_res_pipe_busy", "view_bounces"):
        pass  # handled in run_fault_case / by the case's own steps
    return v


def case_body(session) -> str:
    return session.body


def disarm(session, v: dict) -> list[str]:
    """Undo everything `arm` recorded in `v`. Each step is guarded on its own:
    a failing step is reported in the returned list and never stops the rest
    or raises."""
    errors: list[str] = []

    def guard(what: str, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - reported, never raised
            errors.append(f"{what}: {type(e).__name__}: {e}"[:300])

    timer = v.get("_timer")
    if timer is not None:
        def stop_timer():
            timer.cancel()
            if v.get("_timer_started"):
                timer.join(5)
        guard("cancel kill timer", stop_timer)
    if v.get("_restore_call"):
        had_own, orig = v["_restore_call"]

        def restore_call():
            if had_own:
                session.call = orig
            else:
                del session.call
        guard("restore session.call", restore_call)
    if v.get("_resume_sigcont"):
        guard("SIGCONT owned darktable", lambda: _owned_kill(signal.SIGCONT))
    for snap in reversed(v.get("_restore", [])):
        guard(f"restore {snap['path']}", lambda snap=snap: _restore(snap))
    if v.get("_relaunch") or v.get("_resume_sigcont"):
        guard("relaunch", session.relaunch)
    if v.get("_detach"):
        guard("hdiutil detach", lambda: _hdiutil("detach", v["_detach"], "-force"))
    if v.get("_job_frames"):
        (old,) = v["_job_frames"]

        def restore_frames():
            session.staged.job_frames = old
        guard("restore job_frames", restore_frames)
    return errors


def _hdiutil(*args: str, timeout: float = 60):
    return subprocess.run(["hdiutil", *args], capture_output=True, text=True,
                          timeout=timeout, check=True)


def _fill(mount: Path) -> None:
    """Fill the volume to within FILL_HEADROOM of full: sized from statvfs, in
    chunks, stopping (not raising) if the volume still runs out."""
    import errno

    sv = _statvfs(str(mount))
    left = max(0, sv.f_bavail * sv.f_frsize - FILL_HEADROOM)
    with open(mount / "filler.bin", "wb") as f:
        while left > 0:
            n = min(_CHUNK, left)
            try:
                f.write(b"\0" * n)
                f.flush()
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    break
                raise
            left -= n


def _small_disk(session, v: dict) -> None:
    img = session.root / "full.dmg"
    _hdiutil("create", "-ov", "-size", "50m", "-fs", "HFS+", "-volname", "HTCFULL", str(img))
    out = _hdiutil("attach", str(img), "-nobrowse").stdout
    lines = out.strip().splitlines()
    mount = lines[-1].split("\t")[-1].strip() if lines else ""
    if not mount:
        device = lines[0].split("\t")[0].strip() if lines else ""
        if device:
            _hdiutil("detach", device, "-force")
        raise RuntimeError(f"hdiutil attach: no mount point in {out!r}")
    v["_detach"] = mount  # registered the moment attach succeeds
    v["full_disk"] = mount
    try:
        _fill(Path(mount))
    except BaseException:
        try:
            _hdiutil("detach", mount, "-force")
            v.pop("_detach", None)
        except Exception:  # noqa: BLE001 - disarm retries the detach
            pass
        raise


def run_fault_case(session, case: Case) -> Observation:
    """Arm, run, disarm. Never raises: a failing arm or step is a typed
    `blocked`; failing disarm steps land in obs.extra["disarm_errors"]."""
    from tests.gauntlet.execute import run_tool_case
    from tests.gauntlet.session import SessionBlocked

    fault = case.meta["fault"]
    if fault == "fake_version":
        try:
            return _fake_version(session)
        except Exception as e:  # noqa: BLE001
            return Observation(blocked=f"harness_bug: {type(e).__name__}: {e}"[:500])
    v: dict = {}
    obs: Observation | None = None
    try:
        arm(session, fault, v)
        session.staged.vars.update({k: val for k, val in v.items() if not k.startswith("_")})
        if v.get("_skip"):
            obs = Observation(blocked=f"skipped: {v['_skip']}")
        elif case.id.startswith("rob-h"):
            obs = _hang_case(session, case)
        elif fault == "two_clients":
            obs = _two_clients(session, case)
        else:
            obs = run_tool_case(session, case)
            _post_facts(fault, obs, v)
    except SessionBlocked as e:
        obs = Observation(blocked=f"session: {e}"[:500])
    except Exception as e:  # noqa: BLE001 - a raise here is the harness's bug
        obs = Observation(blocked=f"harness_bug: {type(e).__name__}: {e}"[:500])
    finally:
        errors = disarm(session, v)
    if errors:
        obs.extra["disarm_errors"] = errors
    return obs


def _post_facts(fault: str, obs: Observation, v: dict) -> None:
    import json as _json

    if fault == "unicode_path":
        obs.extra["sidecar_changed"] = obs.before.get("fingerprint") != \
            obs.after.get("fingerprint") or any(c.envelope and c.envelope.get("ok")
                                                for c in obs.calls[1:])
    if fault == "huge_job_one_timeout":
        text = _json.dumps([c.envelope for c in obs.calls])
        from tests.gauntlet.exec_bulk import reported_n

        obs.extra["frames_reported"] = reported_n([c.envelope for c in obs.calls]) or 0
        obs.extra["timeout_frame_reported"] = v.get("bad_frame", "@@") in text
    if fault == "wrong_wb_table":
        env = (obs.calls[-1].envelope or {}) if obs.calls else {}
        data = env.get("data") or {}
        obs.extra["flagged_or_refused"] = (not env.get("ok")) or data.get("kelvin") is None \
            or bool(data.get("warnings"))


def _fake_version(session) -> Observation:
    """R21 host-free: a bridge double answering diag with darktable 5.6.0.

    Runs in-process, so the session's isolated SCOUTER_* env is applied to
    os.environ for the call (never the runner's real ~/.config/scouter). No
    product code gates on `darktable_version` today (task-22 report), so the
    call answers ok and the case fails its typed-error check -- it reports the
    gap instead of raising."""
    from scouter import registry
    from scouter.core import dtbridge
    from tests.scouter_fakes import FakeBridge

    keys = ("SCOUTER_CONFIG", "SCOUTER_CACHE", "SCOUTER_DARKTABLE_LIBRARY_DB", "SCOUTER_JOB",
            "OBSIDIAN_VAULT_PATH")
    staged_env = session.staged.env
    if not staged_env.get("SCOUTER_CONFIG") or not staged_env.get("SCOUTER_CACHE"):
        return Observation(blocked="harness_bug: fake_version needs an isolated "
                                   "SCOUTER_CONFIG and SCOUTER_CACHE")
    fake = FakeBridge(responses={"darktable_mcp.diag": {"darktable_version": "5.6.0",
                                                        "gui_ready": True},
                                 "view.current": {"view": "darkroom"},
                                 "darkroom.status": {"position": 0.5, "nan": False,
                                                     "view": "darkroom"}})
    saved = {k: os.environ.get(k) for k in keys}
    old = getattr(dtbridge, "_bridge", None)  # never construct a real Bridge here
    dtbridge.set_bridge(fake)
    try:
        for k in keys:
            if staged_env.get(k):
                os.environ[k] = staged_env[k]
            else:
                os.environ.pop(k, None)
        try:
            env = registry.call("darkroom.status", "gauntlet", module="exposure",
                                param="exposure")
            env = env if isinstance(env, dict) else getattr(env, "to_dict", lambda: {})()
        except Exception as e:  # noqa: BLE001 - a raise is an observation, not a crash
            env = {"ok": False, "outcome": "error",
                   "error": {"code": f"raised: {type(e).__name__}", "message": str(e)[:300]}}
    finally:
        dtbridge.set_bridge(old)
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    rec = CallRecord("darkroom_status", {}, env)
    if not env.get("ok"):
        rec.error = (env.get("error") or {}).get("code") or env.get("outcome")
    return Observation(calls=[rec])


def _two_clients(session, case: Case) -> Observation:
    """R16: a second MCP server on the same image; interleaved darkroom_say."""
    second = session.mcp_factory(session.staged.env, session.root)
    obs = Observation(before=observe.snapshot(session.staged.frames["hero"]))
    results: list = []

    def fire(client, phrase):
        try:
            results.append(client.call_tool("darkroom_say", {"phrase": phrase}, 60))
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "error": {"code": f"transport: {e}"}})

    threads = [threading.Thread(target=fire, args=(c, p)) for c, p in
               ((session.mcp, "brighter"), (second, "warmer"), (session.mcp, "darker"),
                (second, "cooler"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    second.close()
    obs.after = observe.snapshot(session.staged.frames["hero"])
    ok_calls = sum(1 for r in results if r.get("ok"))
    grew = (obs.after.get("history_end") or 0) - (obs.before.get("history_end") or 0)
    obs.calls = [CallRecord("darkroom_say", {}, r, write=True) for r in results]
    obs.extra["no_interleaved_writes"] = grew >= 0 and all(
        r.get("ok") or (r.get("error") or {}).get("code") in ("state_locked", "db_locked")
        for r in results) and (ok_calls == 0 or grew >= 1)
    return obs


def _hang_case(session, case: Case) -> Observation:
    """H1-H4: every call must answer within its cap with an allowed code or ok."""
    obs = Observation()
    hung = 0
    allowed = set(case.meta.get("allowed_codes", []))
    for st in case.steps:
        rec = session.call(st.tool, substitute(st.args, session, obs.calls), st.budget_s,
                           write=st.write)
        obs.calls.append(rec)
        if rec.error == "timeout":
            hung += 1
        elif rec.error and allowed and rec.error not in allowed:
            obs.extra.setdefault("unexpected_codes", []).append(rec.error)
    obs.extra["hung_calls"] = hung
    obs.extra["every_call_answered"] = hung == 0 and not obs.extra.get("unexpected_codes")
    return obs


def watchdog_probe(session, case, step, obs) -> CallRecord:
    """The 4 watchdog cases (spec 3.5) through `scouter doctor` and a SIGSTOPped
    owned darktable; `foreign_pid` uses a sleeper process the runner owns.

    Deviation from the brief (adapter-context, Tasks 29-30 merged product):
    the heal scenarios probe with `darkroom_enable` (module "exposure"),
    not the brief's `darkroom_say`. `scouter/core/watchdog.py`'s RETRY_SAFE
    allow-list only auto-retries darkroom.set/enable/reset/open (and
    read-only verbs); `darkroom.say` is NOT in it, so after a heal it always
    reports `healed` in the error's `detail` without a retry and returns
    ok:false -- the brief's `first.envelope.get("healed") and
    first.envelope.get("ok")` check could never be true for that verb. A
    RETRY_SAFE verb exercises the actual auto-heal-then-retry-successfully
    path the case is meant to probe.
    """
    import json as _json
    import sys

    scen = step.args["scenario"]
    env = dict(session.staged.env)

    def doctor(*flags):
        p = subprocess.run([sys.executable, "-m", "scouter", "doctor", *flags], env=env,
                           capture_output=True, text=True, timeout=90)
        try:
            return p.returncode, _json.loads(p.stdout)
        except ValueError:
            return p.returncode, {}

    ok = False
    detail: dict = {}
    if scen == "ping_states":
        code_alive, rep_alive = doctor("--ping")
        _owned_kill(signal.SIGSTOP)
        try:
            code_dead, rep_dead = doctor("--ping")
        finally:
            _owned_kill(signal.SIGCONT)
        ok = code_alive == 0 and code_dead == 1
        detail = {"alive": rep_alive.get("watchdog"), "unresponsive": rep_dead.get("watchdog")}
    elif scen in ("auto_heal_once", "heal_cap_reached"):
        cfg = Path(env["SCOUTER_CONFIG"])
        original = cfg.read_text() if cfg.exists() else None  # restored EXACTLY
        try:
            doc = _json.loads(original) if original else {}
            doc["watchdog"] = {"auto_heal": True, "max_heals_per_job": 1, "heal_budget_s": 60}
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(_json.dumps(doc))
            _owned_kill(signal.SIGSTOP)
            first = session.call("darkroom_enable", {"module": "exposure"}, 90.0, write=True)
            detail["first"] = first.envelope
            if scen == "auto_heal_once":
                ok = bool((first.envelope or {}).get("healed")) and \
                    (first.envelope or {}).get("ok")
            else:
                _owned_kill(signal.SIGSTOP)
                second = session.call("darkroom_enable", {"module": "exposure"}, 90.0,
                                      write=True)
                detail["second"] = second.envelope
                ok = ((second.envelope or {}).get("error") or {}).get("code") == \
                    "darktable_unresponsive"
        finally:
            # auto_heal must never be left on, and the owned darktable never
            # left stopped: each undo runs even if the one before it raises.
            try:
                _owned_kill(signal.SIGCONT)
            finally:
                try:
                    if original is None:
                        cfg.unlink(missing_ok=True)
                    else:
                        cfg.write_text(original)
                finally:
                    session.relaunch()
    elif scen == "foreign_pid":
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            from scouter.core import dtprocess

            res = dtprocess.terminate(sleeper.pid)
            ok = res["how"] == "refused_not_ours" and sleeper.poll() is None
            detail = res
        finally:
            sleeper.kill()
    obs.extra["scenario_ok"] = bool(ok)
    obs.extra["watchdog_detail"] = detail
    return CallRecord("watchdog_probe", step.args, {"ok": bool(ok), "outcome": "ok",
                                                    "data": detail})


VIRTUAL["watchdog_probe"] = watchdog_probe
