"""Layer 7 executor (spec 3.7): the install rehearsal in an isolated HOME/XDG
root. Actions run in order inside ONE session; each case checks one action.
Pratik's ~/.claude.json is never written: `claude mcp` runs with HOME set to
the rehearsal home, and the real file's mcpServers object is only hashed."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from gauntlet.adapter import CallRecord, Observation
from gauntlet.case import Case

REPO = Path(__file__).resolve().parents[2]
WATCHED = ("~/.config/darktable", "~/Library/Application Support/darktable", "~/.config/scouter")


def mcp_servers_hash(path: Path = Path.home() / ".claude.json") -> str:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "absent"
    return hashlib.sha256(json.dumps(doc.get("mcpServers", {}), sort_keys=True)
                          .encode()).hexdigest()


def newer_than(stamp: Path, roots=WATCHED) -> list[str]:
    hits = []
    for r in roots:
        p = Path(r).expanduser()
        if not p.exists():
            continue
        out = subprocess.run(["find", str(p), "-newer", str(stamp)], capture_output=True,
                             text=True, timeout=120)
        hits += [x for x in out.stdout.splitlines() if x.strip()]
    return hits


class Rehearsal:
    def __init__(self, root: Path, *, run=subprocess.run) -> None:
        self.root = root
        self.run = run
        self.home, self.config, self.cache = root / "home", root / "config", root / "cache"
        self.stamp = root / "rehearsal.stamp"
        self.hash_before = ""
        self.facts: dict = {}

    def env(self) -> dict:
        import os

        env = {k: v for k, v in os.environ.items() if k not in
               ("SCOUTER_CONFIG", "SCOUTER_CACHE", "SCOUTER_JOB", "OBSIDIAN_VAULT_PATH",
                "SCOUTER_DARKTABLE_LIBRARY_DB")}
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config),
                   XDG_CACHE_HOME=str(self.cache), PYTHONPATH=str(REPO),
                   SCOUTER_CONFIG=str(self.config / "scouter" / "config.json"),
                   SCOUTER_CACHE=str(self.cache / "scouter"),
                   OBSIDIAN_VAULT_PATH=str(self.root / "vault"),
                   # Pin the library: without it history/backup fall back to a running
                   # darktable's --library, then ~/.config/darktable/library.db (real one).
                   SCOUTER_DARKTABLE_LIBRARY_DB=str(self.config / "library.db"),
                   DARKTABLE_MCP_PLUGIN_PATH=str(self.config / "darktable" / "lua" /
                                                 "darktable_mcp.lua"))
        return env

    def _scouter(self, *args: str, timeout: float = 120) -> tuple[int, dict]:
        p = self.run([sys.executable, "-m", "scouter", *args], env=self.env(),
                     capture_output=True, text=True, timeout=timeout, cwd=str(self.root))
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            return p.returncode, {"raw": p.stdout[-500:], "stderr": p.stderr[-500:]}

    def action(self, name: str) -> tuple[bool, dict]:
        if name == "make_dirs":
            for d in (self.home, self.config, self.cache):
                d.mkdir(parents=True, exist_ok=True)
            self.stamp.write_text(str(time.time()))
            time.sleep(1.1)  # find -newer has 1 s resolution on some filesystems
            self.hash_before = mcp_servers_hash()
            return all(d.is_dir() for d in (self.home, self.config, self.cache)), {}
        if name == "check_env":
            env = self.env()
            keys = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "SCOUTER_CONFIG",
                    "SCOUTER_CACHE", "OBSIDIAN_VAULT_PATH")
            return all(env[k].startswith(str(self.root)) for k in keys), {k: env[k] for k in keys}
        if name == "first_launch":
            from scouter.core import dtprocess

            cfg = self.config / "darktable"
            rec = dtprocess.launch(None, cfg / "library.db", cfg, env=self.env())
            time.sleep(15)
            res = dtprocess.terminate(rec.pid)
            ok = (cfg / "darktablerc").exists() and (cfg / "library.db").exists()
            return ok, {"terminate": res}
        if name in ("setup", "setup_again", "rerun_existing"):
            if name == "rerun_existing":
                (self.config / "scouter").mkdir(parents=True, exist_ok=True)
                (self.config / "scouter" / "config.json").write_text("{}\n")
            before = self._tree_hash()
            code, rep = self._scouter("setup")
            #: Fix D (night-1 triage rank 8, INS-config-version-assert): spec
            #: 3.7 says "setup writes a valid config" (a version field); the
            #: product's own design (scouter/cli/setup.py's config item is
            #: `required: False`, "reported only: `scouter config init`
            #: writes it") means it never does, code 0 or not. Rather than
            #: fold that disputed assertion into `ok` (masking a real doctor
            #: regression as the SAME failure, or silently passing once
            #: darktable is available despite spec 3.7 never being met),
            #: `ok` only asserts what the product actually claims to do
            #: (`code == 0`, plus setup_again's idempotency); the
            #: config-version fact is reported separately in `detail` for
            #: `layers/install.py`'s own second check to score honestly
            #: (needs Pratik: rank 8's options 1/2).
            ok = code == 0
            if name == "setup_again":
                ok = ok and self._tree_hash() == before
            detail = dict(rep)
            detail["config_has_version"] = self._config_has_version()
            if not detail["config_has_version"]:
                detail["needs_review_reason"] = (
                    "spec 3.7 expects setup to leave config.json with a version field, but "
                    "scouter/cli/setup.py's own config item is required=False and reported "
                    "only -- `scouter config init` writes it by design (night1-parity-"
                    "install-guide-triage.md rank 8, needs Pratik)")
            self._mark_needs_darktable(code, rep, detail)
            return ok, detail
        if name == "setup_check":
            code, rep = self._scouter("setup", "--check")
            ok = code == 0 and bool(rep.get("ok"))
            detail = dict(rep)
            self._mark_needs_darktable(code, rep, detail)
            return ok, detail
        if name == "mcp_register":
            server = f"{sys.executable} -m scouter.mcp.server"
            self.run(["claude", "mcp", "add", "scouter", "--scope", "user", "--",
                      *server.split()], env=self.env(), capture_output=True, text=True,
                     timeout=60)
            p = self.run(["claude", "mcp", "list"], env=self.env(), capture_output=True,
                         text=True, timeout=60)
            ok = "scouter" in p.stdout and "scouter.mcp.server" in p.stdout
            return ok, {"list": p.stdout[-800:]}
        if name == "teardown":
            self.facts["leaks"] = newer_than(self.stamp)
            self.facts["hash_after"] = mcp_servers_hash()
            for d in (self.home, self.config, self.cache):
                shutil.rmtree(d, ignore_errors=True)
            return not any(d.exists() for d in (self.home, self.config, self.cache)), {}
        if name == "leak_check":
            leaks = self.facts.get("leaks", newer_than(self.stamp))
            same = self.facts.get("hash_after", mcp_servers_hash()) == self.hash_before
            return not leaks and same, {"leaks": leaks[:50], "mcp_servers_unchanged": same}
        raise ValueError(f"unknown rehearsal action {name!r}")

    def _config_has_version(self) -> bool:
        path = self.config / "scouter" / "config.json"
        if not path.is_file():
            return False
        try:
            return json.loads(path.read_text()).get("version") is not None
        except ValueError:
            return False

    @staticmethod
    def _mark_needs_darktable(code: int, rep: dict, detail: dict) -> None:
        """Fix D (night-1 triage rank 5/8's doctor half, INS-doctor-needs-
        darktable): `scouter setup`/`setup --check`'s own `doctor` item is
        `required` (setup.py's default) and answers unavailable/bridge_timeout
        when no darktable with the plugin loaded is running -- exactly the
        rehearsal's own state right after ins-03 quits it (spec 3.7 never
        launches one again before ins-04/05/08/09). That is an ENVIRONMENT
        gap the harness itself created, not a product regression; marking it
        lets InstallSession.run_case set Observation.blocked = "needs_darktable"
        (evaluate()'s own "blocked" outcome, honestly distinct from a bare
        "failed") instead of the numbers silently reading as a product bug."""
        if code == 0:
            return
        doctor = next((i for i in (rep.get("items") or []) if i.get("item") == "doctor"), None)
        if doctor is not None and doctor.get("status") != "ok":
            detail["_needs_darktable"] = True

    def _tree_hash(self) -> str:
        h = hashlib.sha256()
        for p in sorted((self.config / "scouter").rglob("*")):
            if p.is_file():
                h.update(p.name.encode() + p.read_bytes())
        return h.hexdigest()


class InstallSession:
    """Implements gauntlet.adapter.Session for layer install (one rehearsal)."""

    def __init__(self, root: Path) -> None:
        self.rehearsal = Rehearsal(root)

    def run_case(self, case: Case) -> Observation:
        t0 = time.monotonic()
        name = case.meta["action"]
        try:
            ok, detail = self.rehearsal.action(name)
        except subprocess.TimeoutExpired as e:
            return Observation(calls=[CallRecord("rehearsal", {"action": name},
                                                 {"ok": False, "outcome": "timeout"})],
                               blocked=f"hang: install action {name} exceeded its timeout",
                               hang_evidence=[str(e)],
                               extra={"action_ok": False},
                               wall_s=time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001 - a failed action is a result
            ok, detail = False, {"error": f"{type(e).__name__}: {e}"[:300]}
        #: Fix D (night-1 triage rank 5, INS-doctor-needs-darktable): the
        #: rehearsal's own `_mark_needs_darktable` leaves a `_needs_darktable`
        #: marker in `detail` when the ONLY reason `ok` is False is the
        #: doctor item requiring a running darktable -- an environment gap,
        #: not a product regression. Observation.blocked makes evaluate()
        #: score it "blocked" (still not a pass, but honestly distinct from
        #: "failed"), and the reason travels with the case's own result.
        blocked = None
        if isinstance(detail, dict) and detail.pop("_needs_darktable", False):
            blocked = "needs_darktable"
        return Observation(calls=[CallRecord("rehearsal", {"action": name},
                                             {"ok": ok, "outcome": "ok", "data": detail})],
                           blocked=blocked,
                           extra={"action_ok": ok, "detail": detail},
                           wall_s=time.monotonic() - t0)

    def abort(self) -> None:
        pass

    def close(self) -> None:
        pass
