"""Opt-in parallel mode (`run --workers N`): the parent shards the selected
cases at session granularity, spawns one child `run --shard K/N` per shard,
each with its own spool, darktable configdir and pid registry entries, then
merges their progress and logs. N = 1 never reaches this module's spawning."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from gauntlet.case import Case
from gauntlet.store import RunDir

SHARD_ENV = "GAUNTLET_SHARD"
REGISTRY_ENV = "GAUNTLET_DT_REGISTRY"  # == scouter.core.dtprocess.REGISTRY_ENV
EXIT_PARKED = 75


def parse_shard(text: str) -> tuple[int, int]:
    k, _, n = text.partition("/")
    try:
        shard = (int(k), int(n))
    except ValueError:
        raise SystemExit(f"gauntlet: --shard needs K/N, got {text!r}") from None
    if not 0 <= shard[0] < shard[1]:
        raise SystemExit(f"gauntlet: --shard {text!r} out of range")
    return shard


def shard_from_env(env: dict | None = None) -> tuple[int, int] | None:
    raw = (os.environ if env is None else env).get(SHARD_ENV)
    return parse_shard(raw) if raw else None


def next_session_n(n: int, shard: tuple[int, int] | None) -> int:
    """The next session number after `n`; in a shard only numbers == K mod N, so
    two workers never share a `sessions/<layer>/NNN-body` directory."""
    n += 1
    if shard is not None:
        while n % shard[1] != shard[0]:
            n += 1
    return n


def owns_session_dir(dirname: str, shard: tuple[int, int]) -> bool:
    head = dirname.partition("-")[0]
    return head.isdigit() and int(head) % shard[1] == shard[0]


def session_units(cases: list[Case], *, max_cases: int,
                  atomic_layers: frozenset[str] = frozenset()) -> list[list[Case]]:
    """Cases that share a darktable session. A session is one layer and one body
    (RoutedSession switches body, and the runner rotates a session after
    `max_cases`), so a unit is the layer+body cases in order, cut every
    `max_cases`. A walkthrough is one stateful session in document order and an
    atomic layer (install) one rehearsal: neither is ever cut."""
    groups: dict[tuple, list[Case]] = {}
    for c in cases:
        if c.layer in atomic_layers:
            key: tuple = (c.layer,)
        elif c.meta.get("session") == "walkthrough":
            key = (c.layer, "walkthrough")
        else:
            key = (c.layer, c.body or "")
        groups.setdefault(key, []).append(c)
    units = []
    for key, group in groups.items():
        cut = len(group) if len(key) == 1 or key[1] == "walkthrough" else max_cases
        units.extend(group[i:i + cut] for i in range(0, len(group), cut))
    return units


def partition(cases: list[Case], n: int, *, max_cases: int,
              atomic_layers: frozenset[str] = frozenset()) -> list[list[Case]]:
    """Deterministic, disjoint shards (union = `cases`, input order kept), whole
    units only, balanced by case count (largest unit first onto the lightest
    shard; the spread is at most one unit)."""
    order = {c.id: i for i, c in enumerate(cases)}
    units = sorted(session_units(cases, max_cases=max_cases, atomic_layers=atomic_layers),
                   key=lambda u: (-len(u), order[u[0].id]))
    shards: list[list[Case]] = [[] for _ in range(n)]
    for u in units:
        min(shards, key=len).extend(u)
    return [sorted(s, key=lambda c: order[c.id]) for s in shards]


def workers_dir(run: RunDir) -> Path:
    return run.root / "workers"


def shard_dir(run: RunDir, k: int) -> Path:
    return workers_dir(run) / f"w{k}"


def worker_env(run: RunDir, k: int, n: int, adapter, base: dict | None = None) -> dict[str, str]:
    """The environment of child K. XDG_CACHE_HOME moves the bridge spool for both
    the darktable process and the Python bridge client; the adapter adds the rest
    (own darktable configdir seeded with the plugin, isolated library check)."""
    d = shard_dir(run, k)
    env = dict(os.environ if base is None else base)
    cache = d / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    env.update({"XDG_CACHE_HOME": str(cache), SHARD_ENV: f"{k}/{n}",
                REGISTRY_ENV: str(workers_dir(run) / "pids")})
    (workers_dir(run) / "pids").mkdir(parents=True, exist_ok=True)
    hook = getattr(adapter, "worker_env", None)
    if hook is not None:
        env.update(hook(d))
    return env


def serial_first(adapter, todo: list[Case]) -> list[str]:
    """Ids of selected cases the adapter wants run serially in the parent before
    the shards spawn (e.g. a case that writes a run-wide file every shard reads).
    Optional adapter hook `serial_first_ids(todo)`; none = nothing to do."""
    hook = getattr(adapter, "serial_first_ids", None)
    ids = list(hook(todo)) if hook is not None else []
    return [c.id for c in todo if c.id in set(ids)]


def preflight_env(run: RunDir, n: int, adapter) -> dict[str, str]:
    """The env a parent-run pre-case uses: a worker's (registered pid registry,
    seeded configdir, spool) minus the shard marker, so the foreign-darktable
    guard treats its darktable like a worker's."""
    env = worker_env(run, 0, n, adapter)
    env.pop(SHARD_ENV, None)
    return env


def shard_ids_path(run: RunDir, k: int) -> Path:
    return shard_dir(run, k) / "cases.txt"


def stop_path(run: RunDir) -> Path:
    return workers_dir(run) / "STOP"


def prepare_shard_files(run: RunDir, shards: list[list[Case]]) -> None:
    workers_dir(run).mkdir(parents=True, exist_ok=True)
    stop_path(run).unlink(missing_ok=True)
    for k, cases in enumerate(shards):
        d = shard_dir(run, k)
        d.mkdir(parents=True, exist_ok=True)
        shard_ids_path(run, k).write_text("".join(c.id + "\n" for c in cases), encoding="utf-8")
        for stale in ("progress.json", "log.md"):  # each run's deltas start from zero
            (d / stale).unlink(missing_ok=True)


def merge_logs(run: RunDir, shards: list[int]) -> None:
    with open(run.log_md, "a", encoding="utf-8") as out:
        for k in shards:
            log = shard_dir(run, k) / "log.md"
            if log.exists():
                out.write(f"\n## Worker {k}\n\n" + log.read_text(encoding="utf-8"))
                log.unlink()


def run_parallel(run: RunDir, shards: list[list[Case]], argv_for: Callable[[int, int], list[str]],
                 env_for: Callable[[int], dict], progress, *, popen=None,
                 sleep=time.sleep, poll_s: float = 1.0) -> int:
    """Spawn one child per non-empty shard and wait. If any child parks or fails
    to finish, the others are asked to stop politely (a STOP file the runner
    checks before each case) so nothing is killed mid-case; exit 75 then and
    `--resume` picks up. Returns the process exit code."""
    n = len(shards)
    prepare_shard_files(run, shards)
    live = [k for k, s in enumerate(shards) if s]
    popen = popen or subprocess.Popen
    procs = {k: popen(argv_for(k, n), env=env_for(k)) for k in live}
    codes: dict[int, int] = {}
    try:
        while len(codes) < len(procs):
            for k, p in procs.items():
                rc = p.poll()
                if rc is not None and k not in codes:
                    codes[k] = rc
                    if rc != 0:
                        stop_path(run).write_text(f"worker {k} exited {rc}\n")
            if len(codes) < len(procs):
                sleep(poll_s)
    except KeyboardInterrupt:
        stop_path(run).write_text("interrupted\n")
        for k, p in procs.items():
            codes.setdefault(k, p.wait())
    finally:
        merge_shard_outputs(run, progress, live)
    if any(c == 3 for c in codes.values()):
        return 3
    return EXIT_PARKED if any(codes.values()) else 0


def merge_shard_outputs(run: RunDir, progress, shard_ids: list[int]) -> None:
    for k in shard_ids:
        path = shard_dir(run, k) / "progress.json"
        if path.exists():
            progress.merge_shard(json.loads(path.read_text(encoding="utf-8")))
            path.unlink()
    merge_logs(run, shard_ids)
