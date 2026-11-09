"""`python -m gauntlet` (spec 2.2/2.3/2.5/8).

  generate [--stamp S] [--layer L ...]
  run [--stamp S|latest] [--layer L ...] [--case ID ...] [--tier P0 ...] [--prefix P ...]
      [--where KEY=VALUE ...] [--resume] [--rerun] [--only-failed] [--wait-on-park]
      [--workers N]
  report --stamp S|latest
  progress-html --stamp S|latest [--out PATH] [--title T]
  du --stamp S|latest

The adapter and runs root come from ./gauntlet.json ({"adapter":
"module:factory", "runs_root": "...", "title": "..."}) unless given as flags."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from gauntlet import html as html_page
from gauntlet import report as report_mod
from gauntlet import workers as workers_mod
from gauntlet.adapter import load_adapter
from gauntlet.generate import GenerationError, write_generation
from gauntlet.runner import Runner
from gauntlet.store import RunDir, atomic_write_text, new_stamp

EXIT_PARKED = 75
#: (final fix I2): the run was refused (e.g. a foreign product process runs).
EXIT_REFUSED = 3


def _config(args: argparse.Namespace) -> dict:
    cfg: dict = {}
    path = Path(args.config)
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    if args.adapter:
        cfg["adapter"] = args.adapter
    if args.runs_root:
        cfg["runs_root"] = args.runs_root
    if "adapter" not in cfg or "runs_root" not in cfg:
        raise SystemExit("gauntlet: need an adapter and a runs root (gauntlet.json or flags)")
    return cfg


def _resolve_stamp(runs_root: Path, stamp: str | None) -> str:
    if stamp and stamp != "latest":
        return stamp
    runs = sorted(p.name for p in Path(runs_root).expanduser().iterdir()
                  if p.is_dir() and (p / "cases.jsonl").exists()) if \
        Path(runs_root).expanduser().exists() else []
    if not runs:
        raise SystemExit("gauntlet: no runs yet; run `python -m gauntlet generate` first")
    return runs[-1]


def _where(pairs: list[str]) -> dict[str, set[str]] | None:
    out: dict[str, set[str]] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"gauntlet: --where needs KEY=VALUE, got {pair!r}")
        out.setdefault(key, set()).add(value)
    return out or None


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _run_shard(args: argparse.Namespace, adapter, run: RunDir) -> int:
    """One `--workers` child: runs exactly its shard's cases, writing its own
    progress.json and log.md (the parent merges them)."""
    k, n = workers_mod.parse_shard(args.shard)
    os.environ.setdefault(workers_mod.SHARD_ENV, f"{k}/{n}")
    d = workers_mod.shard_dir(run, k)
    run.progress_json, run.log_md = d / "progress.json", d / "log.md"
    ids = {x for x in workers_mod.shard_ids_path(run, k).read_text(encoding="utf-8").split()}
    runner = Runner(adapter, run, commit=_commit(),
                    should_stop=workers_mod.stop_path(run).exists)
    summary = runner.run_layers(None, resume=False, case_ids=ids,
                                wait_on_park=args.wait_on_park)
    print(f"worker {k}/{n}: ran {summary.ran}, parked {len(summary.parked)}, "
          f"status {summary.status}")
    if summary.status.startswith("blocked"):
        return EXIT_REFUSED
    return EXIT_PARKED if summary.parked or summary.stopped_early else 0


def _run_parallel(args: argparse.Namespace, cfg: dict, adapter, run: RunDir) -> int:
    runner = Runner(adapter, run, commit=_commit())
    todo = runner.selection(
        args.layer, resume=not args.rerun and (args.resume or not args.case),
        only_failed=args.only_failed, case_ids=set(args.case) if args.case else None,
        tiers=set(args.tier) if args.tier else None, prefixes=tuple(args.prefix),
        where=_where(args.where))
    if not todo:
        print(f"{run.root}: nothing to run")
        return 0
    shards = workers_mod.partition(
        todo, args.workers, max_cases=runner.session_max_cases,
        atomic_layers=frozenset(getattr(adapter, "atomic_layers", ())))
    all_cases = run.read_cases()
    for spec in adapter.layers():  # layer totals, as run_layers records them
        mine = [c for c in all_cases if c.layer == spec.name]
        runner.progress.layer(spec.name, len(mine), sum(c.tier == "P0" for c in mine), spec.live)
        if any(c.layer == spec.name for c in todo):
            runner.progress.start_layer(spec.name)
    base = [sys.executable, "-m", "gauntlet", "--config", args.config]
    for flag, val in (("--adapter", args.adapter), ("--runs-root", args.runs_root)):
        if val:
            base += [flag, val]
    code = workers_mod.run_parallel(
        run, shards,
        lambda k, n: [*base, "run", "--stamp", run.stamp, "--shard", f"{k}/{n}",
                      *(["--wait-on-park"] if args.wait_on_park else [])],
        lambda k: workers_mod.worker_env(run, k, args.workers, adapter), runner.progress)
    runner.progress.finish("parked" if code == EXIT_PARKED else "done" if code == 0
                           else "blocked")
    print(f"{run.root}: {len(todo)} cases on {args.workers} workers "
          f"({', '.join(str(len(s)) for s in shards)}), exit {code}")
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m gauntlet")
    p.add_argument("--config", default="gauntlet.json")
    p.add_argument("--adapter")
    p.add_argument("--runs-root")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--stamp")
    g.add_argument("--layer", action="append")
    #: (row 501 item H, fix round 1): a deterministic, seeded, stratified
    #: subset of ONE layer (e.g. night-2's scenarios_agent), instead of
    #: every generated case. --subset-layer/--subset-keys are required
    #: together with --subset; --seed defaults to 0 (still deterministic,
    #: just not caller-chosen).
    g.add_argument("--subset", type=int,
                   help="keep only this many cases of --subset-layer, stratified on "
                        "--subset-keys")
    g.add_argument("--subset-layer", help="the one layer --subset applies to")
    g.add_argument("--subset-keys",
                   help="comma-separated case.meta keys to stratify on, e.g. class,genre")
    g.add_argument("--seed", type=int, default=0, help="the --subset selection's own seed")
    r = sub.add_parser("run")
    r.add_argument("--stamp")
    r.add_argument("--layer", action="append")
    r.add_argument("--case", action="append")
    r.add_argument("--resume", action="store_true")
    r.add_argument("--only-failed", action="store_true")
    r.add_argument("--rerun", action="store_true",
                   help="run the selected cases again even when they already have a result")
    r.add_argument("--wait-on-park", action="store_true")
    r.add_argument("--tier", action="append", choices=("P0", "P1", "P2"),
                   help="only cases of this tier (repeatable), e.g. the P0 slice")
    r.add_argument("--prefix", action="append", default=[],
                   help="only case ids starting with this (repeatable)")
    r.add_argument("--where", action="append", default=[],
                   help="only cases whose meta KEY equals VALUE, as KEY=VALUE (repeatable; "
                   "several values for one key are OR-ed)")
    r.add_argument("--workers", type=int, default=1,
                   help="run on N darktable instances at once (default 1, unchanged)")
    r.add_argument("--shard", help=argparse.SUPPRESS)  # K/N, set by the --workers parent
    rp = sub.add_parser("report")
    rp.add_argument("--stamp")
    h = sub.add_parser("progress-html")
    h.add_argument("--stamp")
    h.add_argument("--out")
    h.add_argument("--title")
    du_p = sub.add_parser("du")
    du_p.add_argument("--stamp")
    args = p.parse_args(argv)
    cfg = _config(args)
    runs_root = Path(cfg["runs_root"]).expanduser()
    adapter = load_adapter(cfg["adapter"])

    if args.cmd == "generate":
        if bool(args.subset) != bool(args.subset_layer) or (args.subset and not
                                                             args.subset_keys):
            print("gauntlet: --subset needs --subset-layer and --subset-keys together",
                 file=sys.stderr)
            return 2
        run = RunDir(runs_root, args.stamp or new_stamp())
        try:
            counts = write_generation(
                run, adapter, args.layer, subset=args.subset, subset_layer=args.subset_layer,
                subset_keys=(args.subset_keys.split(",") if args.subset_keys else None),
                seed=args.seed)
        except GenerationError as e:
            print(f"generation failed: {e}", file=sys.stderr)
            return 2
        print(f"{run.root}: {sum(c.got for c in counts)} cases")
        return 0

    stamp = _resolve_stamp(runs_root, args.stamp)
    run = RunDir(runs_root, stamp)
    if args.cmd == "run":
        if not run.cases_jsonl.exists():
            write_generation(run, adapter)
        if args.workers < 1:
            raise SystemExit("gauntlet: --workers must be >= 1")
        if args.shard:
            return _run_shard(args, adapter, run)
        if args.workers > 1:
            return _run_parallel(args, cfg, adapter, run)
        summary = Runner(adapter, run, commit=_commit()).run_layers(
            args.layer, resume=not args.rerun and (args.resume or not args.case),
            only_failed=args.only_failed,
            case_ids=set(args.case) if args.case else None, wait_on_park=args.wait_on_park,
            tiers=set(args.tier) if args.tier else None, prefixes=tuple(args.prefix),
            where=_where(args.where))
        print(f"{run.root}: ran {summary.ran}, parked {len(summary.parked)}, "
              f"stopped layers {summary.stopped_layers or '-'}, status {summary.status}")
        if summary.status.startswith("blocked"):
            return EXIT_REFUSED
        return EXIT_PARKED if summary.parked or summary.stopped_early else 0
    if args.cmd == "report":
        rep = report_mod.write(run, adapter, {"commit": _commit()})
        print(f"{run.report_md} (release bar met: {rep['S8_signoff']['release_bar_met']})")
        return 0
    if args.cmd == "du":
        totals = run.du()
        for layer_name in sorted(totals):
            print(f"{layer_name}: {totals[layer_name]} bytes")
        print(f"total: {sum(totals.values())} bytes")
        return 0
    if args.cmd == "progress-html":
        prog = json.loads(run.progress_json.read_text(encoding="utf-8"))
        rep = json.loads(run.report_json.read_text(encoding="utf-8")) \
            if run.report_json.exists() else None
        out = Path(args.out) if args.out else run.root / "progress.html"
        atomic_write_text(out, html_page.render(prog, rep, args.title or cfg.get("title",
                                                                                "Gauntlet")))
        print(out)
        return 0
    return 2
