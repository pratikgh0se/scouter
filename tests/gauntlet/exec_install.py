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
        #: N14: the darktable ins-03 launched (dtprocess.Launched), until it is
        #: confirmed gone. cleanup() quits it at layer end / abort / close.
        self.launched = None
        self.cleanup_result: dict | None = None

    def cache_root(self) -> Path:
        """The cache the rehearsal's launch wrote its owner record into (its
        env's SCOUTER_CACHE), never this process's ambient one."""
        return Path(self.env()["SCOUTER_CACHE"])

    def cleanup(self) -> dict:
        """N14: quit the darktable ins-03 launched, if still running. Idempotent.
        Polite quit then escalation through dtprocess.terminate, which only ever
        signals the pid the owner record in the REHEARSAL cache names (pid, start
        time and exe checked). If that record is gone (cache removed) the recorded
        pid is still SIGKILLed, but only while its start time matches the one
        recorded at launch, so a reused pid or any other darktable is never
        touched."""
        rec = self.launched
        if rec is None:
            return {"ok": True, "how": "not launched"}
        from scouter.core import dtprocess

        try:
            res = dtprocess.terminate(rec.pid, cache_root=self.cache_root())
        except Exception as e:  # noqa: BLE001 - cleanup must still reach the fallback
            res = {"ok": False, "how": f"error: {type(e).__name__}: {e}"[:200]}
        if not res.get("ok") or res.get("how") == "refused_not_ours":
            if self._same_process(rec):
                fallback = self._kill_recorded(rec)
                res = {**res, "fallback": fallback, "ok": fallback == "gone"}
            else:
                res = {**res, "ok": True, "how": "gone_or_reused"}
        if res.get("ok"):
            self.launched = None
        self.cleanup_result = res
        return res

    @staticmethod
    def _same_process(rec) -> bool:
        from scouter.core import dtprocess

        return dtprocess._lstart(rec.pid) == rec.start_time

    @staticmethod
    def _kill_recorded(rec) -> str:
        import os
        import signal

        from scouter.core import dtprocess

        if dtprocess._lstart(rec.pid) != rec.start_time:
            return "gone"
        try:
            os.kill(rec.pid, signal.SIGKILL)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "refused"
        for _ in range(20):
            if dtprocess._lstart(rec.pid) != rec.start_time:
                return "gone"
            time.sleep(0.25)
        return "alive"

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
            self.launched = rec  # N14: owned from here; cleanup() quits it on any exit
            try:
                time.sleep(15)
            finally:
                # N14: the owner record lives in the rehearsal cache (env's
                # SCOUTER_CACHE); the old bare terminate(pid) read the ambient
                # cache, got refused_not_ours and left darktable running.
                res = self.cleanup()
            ok = (bool(res.get("ok")) and (cfg / "darktablerc").exists()
                  and (cfg / "library.db").exists())
            return ok, {"terminate": res}
        if name in ("setup", "setup_again", "rerun_existing"):
            #: Review round 3, GR-2 (P1 blocker): `config init --yes`
            #: unconditionally replaces an existing config.json
            #: (scouter/config.py init: `os.replace`). Running it after
            #: EVERY setup-shaped action clobbered ins-08's own pre-seeded
            #: `{}` -- a product bug where `setup` itself overwrites a
            #: user's config would have become invisible, and
            #: config_has_version would have passed trivially either way.
            #: `config init` now runs ONLY for `setup` (ins-04, the fresh-
            #: machine case). `rerun_existing` (ins-08) snapshots the
            #: config bytes before `setup` and asserts they are byte-
            #: identical after (`detail["config_unchanged"]`) -- so a
            #: clobber is caught, not hidden. `setup_again` (ins-09) never
            #: runs init either, and it runs after 07 teardown (rmtree) and
            #: 08's `{}` seed, so its config carries no version field; the
            #: layer asserts only its idempotency there (`tree_unchanged`
            #: folded into `ok`), never config_has_version (review round 4,
            #: GR3-1).
            config_before = None
            if name == "rerun_existing":
                (self.config / "scouter").mkdir(parents=True, exist_ok=True)
                (self.config / "scouter" / "config.json").write_text("{}\n")
                config_before = self._config_bytes()
            before = self._tree_hash()
            code, rep = self._scouter("setup")
            detail = dict(rep)
            if name == "setup":
                #: Pratik's ruling, row 496 item 7 (triage rank 8's
                #: recommended option 1): `scouter/cli/setup.py`
                #: deliberately never writes config.json itself (its own
                #: config item is `required: False`, reported only) -- the
                #: REHEARSAL runs `scouter config init --yes` right after
                #: `scouter setup`, so the rehearsal as a whole still
                #: leaves a valid config behind. Note for the report: spec
                #: 3.7's wording ("setup writes a valid config") is to be
                #: amended to say the REHEARSAL is responsible for the
                #: config, not the `setup` verb alone.
                init_code, init_rep = self._scouter("config", "init", "--yes")
                detail["config_init"] = init_rep
                ok = code == 0 and init_code == 0
            else:
                ok = code == 0
            tree_unchanged = self._tree_hash() == before
            if name == "setup_again":
                ok = ok and tree_unchanged
            if name == "rerun_existing":
                detail["config_unchanged"] = self._config_bytes() == config_before
            detail["config_has_version"] = self._config_has_version()
            if not detail["config_has_version"]:
                detail["needs_review_reason"] = (
                    "spec 3.7 expects setup to leave config.json with a version field; "
                    "the rehearsal now runs `scouter config init --yes` itself right after "
                    "`scouter setup` (row 496 item 7, ins-04 only -- GR-2), which should make "
                    "this always true there -- still reported so a regression in `config init` "
                    "itself is never silently folded into the doctor-down 'blocked' path")
            #: Review round 2, finding 2 (GD-1): setup_again's own
            #: idempotency break must never be masked by the doctor marker
            #: below (blocked short-circuits every check in evaluate()).
            self._mark_needs_darktable(code, rep, detail,
                                       extra_ok=tree_unchanged if name == "setup_again" else True)
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
            self.cleanup()  # N14: before the rmtree below deletes the owner record
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

    def _config_bytes(self) -> bytes | None:
        """Review round 3, GR-2: the raw bytes of config.json, or None when
        it does not exist -- `rerun_existing`'s own before/after comparison
        (ins-08 must be able to catch a `setup` that clobbers a user's
        config, never trivially pass because `config init` overwrote it)."""
        path = self.config / "scouter" / "config.json"
        return path.read_bytes() if path.is_file() else None

    @staticmethod
    def _mark_needs_darktable(code: int, rep: dict, detail: dict, *, extra_ok: bool = True
                              ) -> None:
        """Fix D (night-1 triage rank 5/8's doctor half, INS-doctor-needs-
        darktable): `scouter setup`/`setup --check`'s own `doctor` item is
        `required` (setup.py's default) and answers unavailable/bridge_timeout
        when no darktable with the plugin loaded is running -- exactly the
        rehearsal's own state right after ins-03 quits it (spec 3.7 never
        launches one again before ins-04/05/08/09). That is an ENVIRONMENT
        gap the harness itself created, not a product regression; marking it
        lets InstallSession.run_case set Observation.blocked = "needs_darktable"
        (evaluate()'s own "blocked" outcome, honestly distinct from a bare
        "failed") instead of the numbers silently reading as a product bug.

        Review round 2, finding 2 (GD-1, P0 blocker): doctor being down must
        be the ONLY reason the action did not fully succeed, never a blanket
        excuse. `extra_ok` is the caller's own check of everything the
        doctor marker does not cover by itself (setup_again's tree-hash
        idempotency; the caller passes False when that broke). And every
        OTHER `required` item (plugin, luarc, wb_tables) must itself be
        `ok`/`installed`/`updated` -- a broken plugin install, with doctor
        also down, must still read as a real "failed", not "blocked"."""
        if code == 0 or not extra_ok:
            return
        items = rep.get("items") or []
        doctor = next((i for i in items if i.get("item") == "doctor"), None)
        if doctor is None or doctor.get("status") == "ok":
            return
        others_ok = all(i.get("status") in ("ok", "installed", "updated")
                        for i in items if i.get("item") != "doctor" and i.get("required", True))
        if others_ok:
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
        except KeyboardInterrupt:
            self.rehearsal.cleanup()  # N14: stopped early mid-action
            raise
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
        self.rehearsal.cleanup()  # N14: an owned scratch darktable never outlives the layer

    def close(self) -> None:
        self.rehearsal.cleanup()
