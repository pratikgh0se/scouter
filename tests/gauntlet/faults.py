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


def _owned_kill(sig=signal.SIGKILL) -> None:
    from scouter.core import dtprocess

    pid = dtprocess.owned_pid()
    if pid:
        os.kill(pid, sig)


def arm(session, fault: str) -> dict:
    st = session.staged
    hero = st.frames["hero"]
    v: dict = {}
    if fault == "quit_darktable":
        session.dt.terminate()
    elif fault == "kill_during_call":
        threading.Timer(1.0, _owned_kill).start()
    elif fault in ("wedge_worker", "slow_bridge"):
        _owned_kill(signal.SIGSTOP)  # frozen process: the bridge cannot answer
        v["_resume_sigcont"] = True
    elif fault == "relaunch_stale_job":
        session.relaunch()
    elif fault == "delete_sidecar":
        side = Path(str(hero) + ".xmp")
        if side.exists():
            side.unlink()
    elif fault == "chmod_sidecar_readonly":
        side = Path(str(hero) + ".xmp")
        side.touch()
        side.chmod(0o444)
        v["_restore_mode"] = str(side)
    elif fault == "stale_sidecar":
        side = Path(str(hero) + ".xmp")
        if side.exists():
            os.utime(side, (time.time() - 86400 * 30,) * 2)
    elif fault == "torn_sidecar_copy":
        side = Path(str(hero) + ".xmp")
        torn = session.root / "torn" / hero.name
        copy_readonly(hero, torn)
        text = side.read_bytes() if side.exists() else b"<x:xmpmeta"
        Path(str(torn) + ".xmp").write_bytes(text[: len(text) // 2])
        v["torn_copy"] = str(torn)
    elif fault == "second_library_lock":
        lock = Path(str(st.library) + ".lock")
        lock.write_text(f"{os.getpid()}\n")
        v["_remove"] = str(lock)
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
        v.update(_small_disk(session))
    elif fault == "chmod_out_dir":
        d = session.root / "denied"
        d.mkdir(exist_ok=True)
        d.chmod(0o500)
        v["denied_dir"] = str(d)
        v["_restore_dir"] = str(d)
    elif fault == "future_mtime":
        side = Path(str(hero) + ".xmp")
        side.touch()
        os.utime(side, (time.time() + 86400 * 365,) * 2)
    elif fault == "cold_launch_commit":
        session.relaunch()  # the step then runs ~1 s after launch, before any settle
    elif fault == "wrong_wb_table":
        wb = Path(st.env["SCOUTER_CONFIG"]).parent / "wb"
        src = wb / "em10iii.json"
        if src.exists():
            (wb / "a7.json").write_bytes(src.read_bytes())
    elif fault == "broken_luarc":
        cfg = session.root / "broken-configdir"
        (cfg / "lua").mkdir(parents=True, exist_ok=True)
        (cfg / "luarc").write_text('error("htc: deliberately broken luarc")\n')
        session.dt.terminate()
        from scouter.core import dtprocess

        dtprocess.launch(hero, session.root / "broken-library" / "library.db", cfg)
        v["_relaunch"] = True
    elif fault == "huge_job_one_timeout":
        from tests.gauntlet.corpus import build_card

        card = session.root / "huge" / "card"
        frames = build_card(session.corpus, case_body(session), 200, card)
        frames[100].write_bytes(frames[100].read_bytes()[:4096])  # a truncated raw
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


def disarm(session, v: dict) -> None:
    if v.get("_relaunch"):
        session.relaunch()
    if v.get("_resume_sigcont"):
        _owned_kill(signal.SIGCONT)
        session.relaunch()
    for key in ("_restore_mode", "_restore_dir"):
        if v.get(key):
            os.chmod(v[key], 0o755 if key == "_restore_dir" else 0o644)
    if v.get("_remove"):
        Path(v["_remove"]).unlink(missing_ok=True)
    if v.get("_detach"):
        subprocess.run(["hdiutil", "detach", v["_detach"], "-force"], capture_output=True,
                       timeout=60)


def _small_disk(session) -> dict:
    img = session.root / "full.dmg"
    subprocess.run(["hdiutil", "create", "-size", "50m", "-fs", "HFS+", "-volname", "HTCFULL",
                    str(img)], capture_output=True, timeout=60, check=True)
    out = subprocess.run(["hdiutil", "attach", str(img), "-nobrowse"], capture_output=True,
                         text=True, timeout=60, check=True).stdout
    mount = out.strip().splitlines()[-1].split("\t")[-1].strip()
    with open(Path(mount) / "filler.bin", "wb") as f:
        f.write(b"\0" * (49 * 1024 * 1024))
    return {"full_disk": mount, "_detach": mount}


def run_fault_case(session, case: Case) -> Observation:
    from tests.gauntlet.execute import run_tool_case

    fault = case.meta["fault"]
    if fault == "fake_version":
        return _fake_version()
    v = arm(session, fault)
    session.staged.vars.update({k: val for k, val in v.items() if not k.startswith("_")})
    try:
        if v.get("_skip"):
            return Observation(blocked=f"skipped: {v['_skip']}")
        if case.id.startswith("rob-h"):
            return _hang_case(session, case)
        if fault == "two_clients":
            return _two_clients(session, case)
        obs = run_tool_case(session, case)
        _post_facts(fault, obs, v)
        return obs
    finally:
        disarm(session, v)


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


def _fake_version() -> Observation:
    """R21 host-free: a bridge double answering diag with darktable 5.6.0."""
    from scouter import registry
    from scouter.core import dtbridge
    from tests.scouter_fakes import FakeBridge

    fake = FakeBridge(responses={"darktable_mcp.diag": {"darktable_version": "5.6.0",
                                                        "gui_ready": True},
                                 "view.current": {"view": "darkroom"}})
    old = dtbridge.get_bridge()
    dtbridge.set_bridge(fake)
    try:
        env = registry.call("darkroom.status", "gauntlet", module="exposure", param="exposure")
        env = env if isinstance(env, dict) else getattr(env, "to_dict", lambda: {})()
    finally:
        dtbridge.set_bridge(old)
    return Observation(calls=[CallRecord("darkroom_status", {}, env)])


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
        doc = _json.loads(cfg.read_text())
        doc["watchdog"] = {"auto_heal": True, "max_heals_per_job": 1, "heal_budget_s": 60}
        cfg.write_text(_json.dumps(doc))
        _owned_kill(signal.SIGSTOP)
        first = session.call("darkroom_enable", {"module": "exposure"}, 90.0, write=True)
        detail["first"] = first.envelope
        if scen == "auto_heal_once":
            ok = bool((first.envelope or {}).get("healed")) and (first.envelope or {}).get("ok")
        else:
            _owned_kill(signal.SIGSTOP)
            second = session.call("darkroom_enable", {"module": "exposure"}, 90.0, write=True)
            detail["second"] = second.envelope
            ok = ((second.envelope or {}).get("error") or {}).get("code") == \
                "darktable_unresponsive"
            _owned_kill(signal.SIGCONT)
        doc.pop("watchdog")
        cfg.write_text(_json.dumps(doc))
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
