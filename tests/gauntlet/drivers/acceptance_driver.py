"""Row 429 part B: Scouter acceptance run, driven by run_acceptance.sh.

Run with darktable CLOSED. Every wait is bounded; every step is recorded in
~/Pictures/ScouterScratch/acceptance/log.md as it finishes, and the full JSON
of each step goes to <run>/steps/. Never raises: a failed step is a row.

Steps: (1) scouter setup (real) + setup --check; (2) Sony WB calibration,
a7 then nex5n, folder mode, darktable launched on the scratch copy first;
(3) acceptance per body (olympus, sony); (4) MCP parity over stdio;
(5) editor benchmark run 2 (plan v2) per body from tests/gauntlet/benchmark;
(6) darktable quit, final table.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path.home() / "CodeBase/scouter"
PY = str(REPO / ".venv" / "bin" / "python")
BENCH = REPO  # row 433 task 1: the harness now lives in tests/gauntlet/benchmark on main
DT = "/Applications/darktable.app/Contents/MacOS/darktable"
TRIAL = Path.home() / "Pictures/EditingTrial"
SONY = TRIAL / "sony"
ROOT = Path.home() / "Pictures" / "ScouterScratch" / "acceptance"
LOG = ROOT / "log.md"
CRASH_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
RUN = ROOT / "runs" / STAMP
SHORT = STAMP.replace("t", "").replace("z", "")[-10:]

BODIES = {
    "olympus": {
        "hero": TRIAL / "P6281348.ORF",
        "job": [TRIAL / n for n in ("P6281361.ORF", "P6281373.ORF", "P5240152.ORF",
                                    "P5240153.ORF", "P5240154.ORF", "P5240155.ORF")],
    },
    "sony": {
        "hero": SONY / "DSC01426.ARW",
        "job": [SONY / f"DSC0{n}.ARW" for n in (1427, 1428, 1429, 1430, 1431, 1432)],
    },
}
PHRASES = ("brighter by 0.5 ev", "warmer by 400k", "lift the shadows")

sys.path.insert(0, str(REPO))
ROWS: list[dict] = []
RESULTS: dict = {"run": STAMP, "run_dir": str(RUN), "log": str(LOG)}


# ---------------------------------------------------------------- logging


def log(text: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    print(text, flush=True)


def record(step: str, status: str, secs: float | None, note: str = "", payload=None) -> dict:
    row = {"step": step, "status": status, "secs": None if secs is None else round(secs, 2),
           "note": note}
    ROWS.append(row)
    n = len(ROWS)
    if payload is not None:
        out = RUN / "steps" / f"{n:02d}-{''.join(c if c.isalnum() else '-' for c in step)[:60]}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
            row["json"] = str(out)
        except OSError:
            pass
    s = "-" if secs is None else f"{secs:.2f}s"
    log(f"- **{step}**: {status}, {s}{' -- ' + note if note else ''}")
    return row


def crash_count() -> int:
    try:
        return sum(1 for p in CRASH_DIR.iterdir() if "darktable" in p.name.lower())
    except OSError:
        return -1


# ---------------------------------------------------------------- environments


def base_env() -> dict:
    env = dict(os.environ)
    for k in ("SCOUTER_CONFIG", "SCOUTER_CACHE", "SCOUTER_JOB"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO)
    return env


def acc_env(job: str | None = None) -> dict:
    env = base_env()
    env["SCOUTER_CONFIG"] = str(ROOT / "config" / "config.json")
    env["SCOUTER_CACHE"] = str(RUN / "cache")
    # library.import writes a job note: never into the real vault.
    env["OBSIDIAN_VAULT_PATH"] = str(RUN / "vault")
    if job:
        env["SCOUTER_JOB"] = job
    return env


def write_acceptance_config() -> None:
    cfg_dir = ROOT / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    passfile = cfg_dir / "backup.pass"
    if not passfile.exists():
        passfile.write_text(secrets.token_hex(24) + "\n")  # never printed
    passfile.chmod(0o600)
    for d in (RUN / "library", RUN / "backup", RUN / "cache", RUN / "vault"):
        d.mkdir(parents=True, exist_ok=True)
    cfg = {
        "_comment": "Row 429 acceptance config (scratch). Rewritten on every run.",
        "version": 1,
        "library_root": str(RUN / "library"),
        "cameras": {"E-M10 Mark III": "em10iii", "ILCE-7": "a7", "NEX-5N": "nex5n"},
        "educative": "brief",
        "backup": {
            "enabled": True,
            "target": str(RUN / "backup"),
            "encryption": "openssl",
            "openssl_passfile": str(passfile),
        },
        "vault": {"jobs_dir": "Projects/Photo jobs"},
    }
    (cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


# ---------------------------------------------------------------- the CLI


def cli(step: str, args: list[str], env: dict, timeout: float, *, ok_when=None,
        note_fn=None) -> tuple[str, dict]:
    """Run `python -m scouter <args>`; record ok/refused/timeout with wall time."""
    t0 = time.monotonic()
    try:
        p = subprocess.run([PY, "-m", "scouter", *args], env=env, cwd=str(REPO),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        record(step, "timeout", time.monotonic() - t0, f"no answer in {timeout:g}s",
               {"args": args})
        return "timeout", {}
    secs = time.monotonic() - t0
    try:
        env_out = json.loads(p.stdout)
    except ValueError:
        env_out = {}
    if ok_when is not None:
        ok = bool(ok_when(env_out))
    else:
        ok = bool(env_out.get("ok"))
    status = "ok" if ok else ("refused" if env_out else "fail")
    note = ""
    try:
        note = note_fn(env_out) if note_fn else ""
    except Exception as e:  # noqa: BLE001
        note = f"(note failed: {e})"
    if not ok:
        err = env_out.get("error") or {}
        why = err.get("message") or (env_out.get("data") or {}).get("reason") or p.stderr[-300:]
        note = (note + " " if note else "") + f"[{env_out.get('outcome', 'exit ' + str(p.returncode))}] {str(why)[:300]}"
    record(step, status, secs, note,
           {"args": args, "exit": p.returncode, "envelope": env_out, "stderr": p.stderr[-4000:]})
    return status, env_out


def data(envelope: dict) -> dict:
    return envelope.get("data") or {}


# ---------------------------------------------------------------- darktable


def darktable_running() -> bool:
    return subprocess.run(["pgrep", "-x", "darktable"], capture_output=True).returncode == 0


class Darktable:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def launch(self, step: str, frame: Path, library: Path, settle_s: float = 5.0) -> bool:
        from scouter.core import dtbridge, dtprocess

        library.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        dtprocess.launch(frame, library, log_path=RUN / "darktable.log")
        last = "never answered"
        in_darkroom = False
        while time.monotonic() - t0 < 120:
            try:
                last = dtbridge.current_view()
            except Exception as e:  # noqa: BLE001 - bridge not up yet
                last = str(e)[:120]
            if last == "darkroom":
                in_darkroom = True
                break
            if dtprocess.owned_pid() is None:
                last = "darktable exited"
                break
            time.sleep(1.0)
        if not in_darkroom:
            record(step, "fail", time.monotonic() - t0, f"never reached the darkroom: {last}")
            return False
        loaded = None
        t1 = time.monotonic()
        while time.monotonic() - t1 < 60:
            try:
                cur = dtbridge.call("darkroom.current", timeout=10.0)
                loaded = cur.get("path")
                if cur.get("image_id") is not None:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        ok = loaded is not None and os.path.realpath(loaded) == os.path.realpath(frame)
        secs = time.monotonic() - t0
        time.sleep(settle_s)  # let the first full render finish
        record(step, "ok" if ok else "fail", secs,
               f"darkroom on {Path(loaded).name if loaded else None} (+{settle_s:g}s settle)")
        return ok

    def quit(self, step: str) -> None:
        t0 = time.monotonic()
        from scouter.core import dtprocess

        res = dtprocess.terminate(responsive=None)
        how = res["how"]
        self.proc = None
        time.sleep(3)
        gone = not darktable_running()
        record(step, "ok" if gone else "fail", time.monotonic() - t0,
               f"{how}; crash reports now {crash_count()}")


# ---------------------------------------------------------------- MCP


class Mcp:
    """Newline-delimited JSON-RPC 2.0 over the server's stdio. Stdlib only."""

    def __init__(self, argv: list[str], env: dict, stderr_path: Path) -> None:
        self._err = open(stderr_path, "ab")
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._err, env=env, cwd=str(RUN))
        self._next = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                q = self._pending.pop(msg.get("id"), None) if msg.get("id") is not None else None
            if q is not None:
                q.put(msg)

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._next += 1
            mid = self._next
            q: queue.Queue = queue.Queue()
            self._pending[mid] = q
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method} did not answer within {timeout:g}s") from None
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"])[:300])
        return msg.get("result") or {}

    def call(self, name: str, arguments: dict, timeout: float) -> dict:
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout)
        content = result.get("content") or []
        text = content[0].get("text") if content else None
        return json.loads(text) if text else {"ok": False, "outcome": "error:empty"}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        finally:
            self._err.close()


# ---------------------------------------------------------------- steps


def step1_setup() -> str | None:
    log("\n### 1. scouter setup")

    def setup_ok(e: dict) -> bool:
        items = {i.get("item"): i.get("status") for i in e.get("items") or []}
        return all(items.get(k) in ("ok", "installed", "updated") for k in ("plugin", "luarc"))

    def setup_note(e: dict) -> str:
        return ", ".join(f"{i.get('item')}={i.get('status')}" for i in e.get("items") or [])

    _, env_out = cli("setup (real: plugin, luarc, wb tables, doctor)", ["setup"], base_env(), 90,
                     ok_when=setup_ok, note_fn=setup_note)
    mcp_line = env_out.get("claude_mcp_add")
    RESULTS["claude_mcp_add"] = mcp_line
    if mcp_line:
        log(f"  - MCP line: `{mcp_line}`")
    # The acceptance config gets the shipped tables (em10iii) the same way.
    cli("setup (acceptance config: shipped wb tables)", ["setup"], acc_env(), 90,
        ok_when=setup_ok, note_fn=setup_note)
    return mcp_line


def setup_check(step: str) -> None:
    cli(step, ["setup", "--check"], base_env(), 90,
        note_fn=lambda e: ", ".join(f"{i.get('item')}={i.get('status')}"
                                    for i in e.get("items") or []))


def step2_wb(dt: Darktable) -> None:
    log("\n### 2. Sony white-balance calibration (folder mode)")
    checked = False
    for cam in ("a7", "nex5n"):
        st, dry = cli(f"wb calibrate {cam} --dry-run",
                      ["wb", "calibrate", str(SONY), "--camera", cam, "--dry-run"], acc_env(), 60,
                      note_fn=lambda e: f"frame {Path(data(e).get('source_image') or '-').name}")
        d = data(dry)
        src, copy, table = d.get("source_image"), d.get("scratch_copy"), d.get("table_path")
        if st != "ok" or not (src and copy and table):
            record(f"wb {cam}: calibration", "skip", None, "no plan from --dry-run")
            continue
        Path(copy).parent.mkdir(parents=True, exist_ok=True)
        if not Path(copy).exists():
            shutil.copy2(src, copy)
            Path(copy).chmod(0o644)
        if not dt.launch(f"launch darktable on the {cam} scratch copy", Path(copy),
                         RUN / "wb-library" / "library.db"):
            dt.quit(f"quit darktable ({cam})")
            continue
        if not checked:
            setup_check("setup --check (darktable up)")
            checked = True
        cli(f"wb calibrate {cam} --yes", ["wb", "calibrate", str(SONY), "--camera", cam, "--yes"],
            acc_env(), 300,
            note_fn=lambda e: f"points {len(data(e).get('points') or [])}, written {data(e).get('written')}")
        exists = Path(table).is_file()
        record(f"wb {cam}: table where `wb kelvin` reads", "ok" if exists else "fail", None, table)
        cli(f"wb kelvin ({cam} scratch copy)", ["wb", "kelvin", copy], acc_env(), 60,
            ok_when=lambda e: data(e).get("kelvin") is not None,
            note_fn=lambda e: f"kelvin {data(e).get('kelvin')}")
        dt.quit(f"quit darktable ({cam})")
    RESULTS["wb_tables"] = sorted(str(p) for p in (ROOT / "config" / "wb").glob("*.json"))


def stage(body: str) -> tuple[Path, Path]:
    spec = BODIES[body]
    bdir = RUN / body
    card = bdir / "card" / "DCIM" / "100ACCPT"
    card.mkdir(parents=True, exist_ok=True)
    for src in [spec["hero"], *spec["job"]]:
        dst = card / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            dst.chmod(0o644)
    boot = bdir / "boot" / spec["hero"].name
    boot.parent.mkdir(parents=True, exist_ok=True)
    if not boot.exists():
        shutil.copyfile(spec["hero"], boot)
        boot.chmod(0o644)
    return bdir / "card", boot


def step3_body(body: str, dt: Darktable, keep_open: bool) -> dict:
    log(f"\n### 3. acceptance: {body}")
    out: dict = {"body": body}
    slug = f"accept-{body}-{SHORT}"
    job_env = acc_env(job=f"accept-{body}-{STAMP}")
    card, boot = stage(body)
    library = RUN / body / "library.db"
    if not dt.launch(f"{body}: launch darktable (boot copy of the hero)", boot, library):
        dt.quit(f"{body}: quit darktable")
        return out
    st, imp = cli(f"{body}: library import", ["library", "import", str(card), "--job-slug", slug,
                                               "--yes"], job_env, 300,
                  note_fn=lambda e: f"{len(data(e).get('files') or [])} files, film roll "
                                    f"registered {data(e).get('film_roll_registered')}")
    d = data(imp)
    job_dir = Path(d.get("path") or "")
    by_source = {f.get("source_name"): f.get("dest") for f in d.get("files") or []}
    hero_name = BODIES[body]["hero"].name
    hero_dest = by_source.get(hero_name)
    if st != "ok" or not hero_dest:
        record(f"{body}: rest of the body", "skip", None, "import did not give the hero's job path")
        dt.quit(f"{body}: quit darktable")
        return out
    hero = hero_dest if os.path.isabs(hero_dest) else str(job_dir / hero_dest)
    out.update(job_slug=slug, job_dir=str(job_dir), hero=hero)
    applied = next((str(job_dir / v) if not os.path.isabs(v) else v
                    for k, v in by_source.items() if k != hero_name and v), None)
    cli(f"{body}: cull propose", ["cull", "propose", "--job-slug", slug], job_env, 600,
        note_fn=lambda e: f"outcome {e.get('outcome')}")
    cli(f"{body}: cull apply", ["cull", "apply", "--job-slug", slug, "--yes"], job_env, 300,
        note_fn=lambda e: f"outcome {e.get('outcome')}")
    # darktable cannot move the darkroom onto a frame imported in the same
    # session: relaunch on the job's hero (now a known library image).
    dt.quit(f"{body}: quit darktable (before relaunching on the job hero)")
    if not dt.launch(f"{body}: relaunch darktable on the job hero", Path(hero), library):
        dt.quit(f"{body}: quit darktable")
        return out
    cli(f"{body}: darkroom open hero", ["darkroom", "open", hero], job_env, 90,
        note_fn=lambda e: f"view {data(e).get('view')}")
    for phrase in PHRASES:
        cli(f"{body}: darkroom say '{phrase}'", ["darkroom", "say", phrase], job_env, 90,
            note_fn=lambda e: "; ".join(
                f"{r.get('action')}{' bounced' if r.get('bounced') else ''}"
                f"{' ' + str(r.get('ms')) + 'ms' if r.get('ms') is not None else ''}"
                for r in (data(e).get("plan") or []) if r.get("action") == "precommit")
            or f"outcome {e.get('outcome')}")
    cli(f"{body}: darkroom undo-last", ["darkroom", "undo-last"], job_env, 90,
        note_fn=lambda e: f"outcome {e.get('outcome')}")
    cli(f"{body}: preset save {body}-hero", ["preset", "save", f"{body}-hero", "--yes"],
        job_env, 90, note_fn=lambda e: f"modules {data(e).get('modules')}")
    cli(f"{body}: job run --preset {body}-hero", ["job", "run", "--job-slug", slug, "--preset",
                                                   f"{body}-hero", "--hero", hero, "--yes"],
        job_env, 1200,
        note_fn=lambda e: "; ".join(f"{r.get('step')}={r.get('outcome')}"
                                    for r in (data(e).get("steps") or []))
        + (f"; stopped_at {data(e).get('stopped_at')}" if data(e).get("stopped_at") else ""))
    if applied:
        cli(f"{body}: history read (applied frame, --source auto)",
            ["history", "read", applied, "--source", "auto"], job_env, 90,
            note_fn=lambda e: f"{Path(applied).name}: source {data(e).get('source')}, "
                              f"history_end {data(e).get('history_end')}, "
                              f"{len(data(e).get('entries') or [])} entries")
        out["applied_frame"] = applied
    exports = job_dir / "exports"
    out["exports"] = sorted(str(p) for p in exports.rglob("*") if p.is_file())[:50] \
        if exports.is_dir() else []
    if not keep_open:
        dt.quit(f"{body}: quit darktable")
    return out


def step4_mcp(mcp_line: str | None, sony: dict) -> None:
    log("\n### 4. MCP parity (stdio, as `claude mcp add` registers it)")
    if not mcp_line:
        record("mcp: server command", "skip", None, "setup printed no claude mcp add line")
        return
    toks = shlex.split(mcp_line)
    argv = toks[toks.index("--") + 1:] if "--" in toks else []
    env = acc_env()  # scratch config, so job_run sees the acceptance job
    env.pop("SCOUTER_JOB", None)
    for i, t in enumerate(toks):
        if t == "--env" and i + 1 < len(toks) and "=" in toks[i + 1]:
            k, v = toks[i + 1].split("=", 1)
            env[k] = v
    if not argv:
        record("mcp: server command", "fail", None, f"could not parse: {mcp_line}")
        return
    t0 = time.monotonic()
    try:
        mcp = Mcp(argv, env, RUN / "mcp-server.stderr.log")
        mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "scouter-acceptance", "version": "1"}},
                    timeout=30)
        mcp._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        names = [t["name"] for t in mcp.request("tools/list", {}, timeout=30).get("tools", [])]
    except Exception as e:  # noqa: BLE001
        record("mcp: initialize + tools/list", "fail", time.monotonic() - t0, str(e)[:300])
        return
    need = ("darkroom_say", "preset_list", "job_run")
    missing = [n for n in need if n not in names]
    record("mcp: initialize + tools/list", "ok" if not missing else "fail",
           time.monotonic() - t0, f"{len(names)} tools" + (f"; missing {missing}" if missing else ""))
    calls = [
        ("mcp: darkroom_say 'a touch warmer'", "darkroom_say", {"phrase": "a touch warmer"}, 90),
        ("mcp: preset_list", "preset_list", {}, 60),
    ]
    if sony.get("job_slug"):
        calls.append(("mcp: job_run --dry-run", "job_run",
                      {"job_slug": sony["job_slug"], "preset": "sony-hero",
                       "hero": sony.get("hero"), "dry_run": True}, 120))
    else:
        record("mcp: job_run --dry-run", "skip", None, "no sony job from step 3")
    for step, tool, args, timeout in calls:
        t1 = time.monotonic()
        try:
            env_out = mcp.call(tool, args, timeout)
        except Exception as e:  # noqa: BLE001
            record(step, "timeout" if isinstance(e, TimeoutError) else "fail",
                   time.monotonic() - t1, str(e)[:300])
            continue
        ok = bool(env_out.get("ok"))
        note = f"outcome {env_out.get('outcome')}"
        if tool == "preset_list":
            presets = data(env_out).get("presets") or []
            note += f"; {len(presets)} presets"
        if not ok:
            note += f"; {str((env_out.get('error') or {}).get('message'))[:200]}"
        record(step, "ok" if ok else "refused", time.monotonic() - t1, note, env_out)
    mcp.close()


def step5_bench() -> None:
    log("\n### 5. editor benchmark run 2 (plan v2) from tests/gauntlet/benchmark")
    if not BENCH.is_dir():
        record("bench: worktree", "skip", None, f"{BENCH} missing")
        return
    produced: list[str] = []
    for body in ("olympus", "sony"):
        if darktable_running():
            record(f"bench {body}", "skip", None, "a darktable is still running")
            continue
        t0 = time.monotonic()
        argv = [PY, "tests/gauntlet/benchmark/run_editor_benchmark.py", "--live", "--plan", "v2",
                "--body", body, "--label", "phase3-v2"]
        try:
            p = subprocess.run(argv, cwd=str(BENCH), env=base_env(), capture_output=True,
                               text=True, timeout=2400)
        except subprocess.TimeoutExpired:
            record(f"bench {body}", "timeout", time.monotonic() - t0, "no finish in 2400s")
            subprocess.run(["osascript", "-e", 'tell application "darktable" to quit'],
                           capture_output=True, timeout=30, check=False)
            continue
        (RUN / f"bench-{body}.stdout.log").write_text(p.stdout + "\n--- stderr\n" + p.stderr)
        paths = [ln.strip() for ln in p.stdout.splitlines()
                 if ln.strip().endswith((".json", ".md")) and "/results/" in ln]
        produced += paths
        summary = ""
        md = next((x for x in paths if x.endswith(".md")), None)
        if md and Path(md).is_file():
            summary = next((ln.strip("- *") for ln in Path(md).read_text().splitlines()
                            if " pass / " in ln), "")
        record(f"bench {body} (phase3-v2)", "ok" if p.returncode == 0 and paths else "fail",
               time.monotonic() - t0, summary or f"exit {p.returncode}; {p.stdout[-200:]}")
    RESULTS["bench_results"] = produced
    if produced:
        subprocess.run(["git", "-C", str(BENCH), "add", "tests/gauntlet/benchmark/results"],
                       capture_output=True, timeout=60)
        msg = ("bench: run 2 (plan v2, phase3-v2) after row 429's no_bounce/action-transport "
               f"fix, acceptance run {STAMP}\n\nCo-Authored-By: Claude Fable 5.1 "
               "<noreply@anthropic.com>\n")
        c = subprocess.run(["git", "-C", str(BENCH), "commit", "-q", "-m", msg],
                           capture_output=True, text=True, timeout=60)
        record("bench: commit results on bench/editor", "ok" if c.returncode == 0 else "fail",
               None, (c.stdout + c.stderr).strip()[:200])


def final_table(before: int) -> None:
    after = crash_count()
    RESULTS["crash_reports"] = {"before": before, "after": after}
    RESULTS["rows"] = ROWS
    (RUN / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str) + "\n")
    w = max(len(r["step"]) for r in ROWS) if ROWS else 10
    lines = ["", "## Final table", "", "| step | status | seconds | note |", "|---|---|---|---|"]
    for r in ROWS:
        s = "-" if r["secs"] is None else f"{r['secs']:.2f}"
        lines.append(f"| {r['step']} | {r['status']} | {s} | {r['note'][:160].replace('|', '/')} |")
    counts: dict[str, int] = {}
    for r in ROWS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines += ["", f"- totals: {counts}",
              f"- darktable crash reports: before {before}, after {after}"
              + (" (NO NEW CRASH)" if after == before else " (NEW CRASH REPORTS)"),
              f"- run dir: {RUN}", f"- results: {RUN / 'results.json'}", f"- log: {LOG}"]
    for k in ("wb_tables", "bench_results"):
        for v in RESULTS.get(k) or []:
            lines.append(f"- {k}: {v}")
    for b in ("olympus", "sony"):
        info = RESULTS.get(b) or {}
        if info.get("job_dir"):
            lines.append(f"- {b} job: {info['job_dir']} ({len(info.get('exports') or [])} export files)")
    log("\n".join(lines))
    print()
    print(f"{'STEP'.ljust(w)}  STATUS    SECONDS")
    for r in ROWS:
        s = "-" if r["secs"] is None else f"{r['secs']:.2f}"
        print(f"{r['step'].ljust(w)}  {r['status'].ljust(8)}  {s}")


def main() -> int:
    if darktable_running():
        print("darktable is running -- quit it first")
        return 1
    RUN.mkdir(parents=True, exist_ok=True)
    write_acceptance_config()
    before = crash_count()
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    log(f"\n## Acceptance run {STAMP} (main {head})\n\n- run dir: {RUN}\n"
        f"- crash reports before: {before}")
    dt = Darktable()
    try:
        mcp_line = step1_setup()
        step2_wb(dt)
        RESULTS["olympus"] = step3_body("olympus", dt, keep_open=False)
        RESULTS["sony"] = step3_body("sony", dt, keep_open=True)
        if darktable_running():
            step4_mcp(mcp_line, RESULTS["sony"])
        else:
            record("mcp parity", "skip", None, "darktable is not up on the sony hero")
        log("\n### 6a. quit darktable before the benchmark")
        dt.quit("quit darktable (after acceptance + MCP)")
        step5_bench()
    except Exception as e:  # noqa: BLE001 - the table must still print
        record("driver", "fail", None, f"{type(e).__name__}: {e}")
    finally:
        log("\n### 6. quit darktable")
        if darktable_running():
            dt.quit("final quit")
        else:
            record("final quit", "ok", 0.0, "darktable already closed")
        final_table(before)
    return 0


if __name__ == "__main__":
    sys.exit(main())
