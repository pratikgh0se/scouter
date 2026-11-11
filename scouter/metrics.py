"""Observability: one event per core call, in every layer (spec 6.1).

`@instrumented("darkroom.set")` wraps a core function so that calling it —
from Python, from the CLI, or from the MCP adapter — appends exactly one line
to events.jsonl, one row to metrics.db, and one line to the job's audit log.
The metrics write is best effort: it can never fail the verb.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from scouter import paths
from scouter.errors import ScouterError
from scouter.types import SummaryResult

#: Which layer the current call entered through. `registry.call` sets it; a
#: direct Python import leaves it at "core".
LAYER: contextvars.ContextVar[str] = contextvars.ContextVar("scouter_layer", default="core")

#: Duration of the most recent instrumented call, so the envelope can report it
#: without every core function threading a timer through its signature.
_LAST_MS: contextvars.ContextVar[int] = contextvars.ContextVar("scouter_last_ms", default=0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, job TEXT NOT NULL, verb TEXT NOT NULL, layer TEXT NOT NULL,
  args_digest TEXT NOT NULL, images TEXT NOT NULL, ms INTEGER NOT NULL,
  outcome TEXT NOT NULL, bytes_in INTEGER NOT NULL, bytes_out INTEGER NOT NULL,
  est_tokens INTEGER NOT NULL, phase INTEGER NOT NULL, version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_verb_outcome ON events(verb, outcome);
"""

EVENT_KEYS = (
    "ts",
    "job",
    "verb",
    "layer",
    "args_digest",
    "images",
    "ms",
    "outcome",
    "bytes_in",
    "bytes_out",
    "est_tokens",
    "phase",
    "version",
)


def last_ms() -> int:
    return _LAST_MS.get()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _utc_now_z() -> str:
    """UTC timestamp with microseconds and a `Z` suffix, per spec 11.1's example."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def args_digest(args: dict) -> str:
    payload = canonical_json({k: v for k, v in args.items() if k != "job"})
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.ensure(paths.db_path()), timeout=5.0)
    conn.executescript(SCHEMA)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def emit(event: dict) -> None:
    """Append one event to events.jsonl and metrics.db. Never raises."""
    try:
        with paths.ensure(paths.events_path()).open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(event) + "\n")
    except OSError as e:
        print(f"scouter: metrics jsonl write failed: {e}", file=sys.stderr)
    try:
        # `images` is a real JSON array in `event` (so events.jsonl matches
        # spec 11.1's `"images":[12]`); the sqlite column is TEXT, so encode
        # it to canonical JSON only for the row, not the in-memory event.
        row = tuple(canonical_json(event[k]) if k == "images" else event[k] for k in EVENT_KEYS)
        with contextlib.closing(_connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO events ("
                    + ",".join(EVENT_KEYS)
                    + ") VALUES ("
                    + ",".join("?" * len(EVENT_KEYS))
                    + ")",
                    row,
                )
    except (sqlite3.Error, OSError, KeyError) as e:
        print(f"scouter: metrics db write failed: {e}", file=sys.stderr)


def audit(job: str, line: dict) -> None:
    """Append the full-fidelity audit line (args included). Never raises."""
    try:
        with paths.ensure(paths.audit_path(job)).open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(line) + "\n")
    except OSError as e:
        print(f"scouter: audit write failed: {e}", file=sys.stderr)


F = TypeVar("F", bound=Callable[..., Any])


def instrumented(verb_id: str) -> Callable[[F], F]:
    """Wrap a core verb so one call produces exactly one event + audit line."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from scouter import PHASE, _PROCESS_STARTED_MONOTONIC, __version__

            job = kwargs.get("job") or paths.current_job()
            kwargs["job"] = job
            bound = dict(kwargs)
            if args:
                bound["_positional"] = list(args)
            started = time.monotonic()
            # Review round 1, should-fix (D3, row 407): time from process
            # start to THIS call's own verb start -- attributes a stall that
            # happens before the verb's body ever runs (a bridge handshake,
            # a contended lock, a hung import), which `ms` below cannot see
            # since it only starts timing here.
            startup_ms = int((started - _PROCESS_STARTED_MONOTONIC) * 1000)
            outcome = "ok"
            images: list[int] = []
            payload: dict = {}
            error: dict | None = None
            try:
                result = fn(*args, **kwargs)
                payload = result.to_dict()
                outcome = payload.get("outcome", "ok")
                if payload.get("image_id") is not None:
                    images = [int(payload["image_id"])]
                return result
            except ScouterError as e:
                outcome = f"error:{e.code}"
                error = e.to_dict()
                raise
            except Exception as e:  # unclassified: still one event
                outcome = "error:error"
                error = {"outcome": "error", "code": "error", "message": str(e)}
                raise
            finally:
                ms = int((time.monotonic() - started) * 1000)
                _LAST_MS.set(ms)
                bytes_out = len(canonical_json(payload or error or {}))
                bytes_in = len(canonical_json(bound))
                event = {
                    "ts": _utc_now_z(),
                    "job": job,
                    "verb": verb_id,
                    "layer": LAYER.get(),
                    "args_digest": args_digest(bound),
                    "images": images,
                    "ms": ms,
                    # Not in `EVENT_KEYS`/the sqlite schema on purpose (no
                    # migration path exists for metrics.db yet): carried in
                    # events.jsonl and the audit line only, which are
                    # schema-free JSON.
                    "startup_ms": startup_ms,
                    "outcome": outcome,
                    "bytes_in": bytes_in,
                    "bytes_out": bytes_out,
                    "est_tokens": math.ceil(bytes_out / 4),
                    "phase": PHASE,
                    "version": __version__,
                }
                emit(event)
                audit(job, {**event, "args": bound, "result": payload, "error": error})

        return wrapper  # type: ignore[return-value]

    return decorate


def _since_to_ts(since: str) -> str:
    unit = since[-1]
    factor = {"d": "days", "h": "hours", "m": "minutes"}.get(unit)
    if factor is None:
        raise ValueError(f"--since must end in d/h/m, got {since!r}")
    delta = timedelta(**{factor: int(since[:-1])})
    return (
        (datetime.now(timezone.utc) - delta)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


@dataclass
class _VerbRow:
    verb: str
    calls: int = 0
    ok: int = 0
    dry_run: int = 0
    errors: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    ms: list[int] = field(default_factory=list)
    est_tokens: int = 0


@instrumented("metrics.summary")
def summary(*, since: str = "7d", job: str | None = None) -> SummaryResult:
    """Per-verb calls, error rate, latency and estimated tokens since `since`."""
    from_ts = _since_to_ts(since)
    rows: dict[str, _VerbRow] = {}
    try:
        with contextlib.closing(_connect()) as conn:
            with conn:
                cursor = conn.execute(
                    "SELECT verb, outcome, ms, est_tokens FROM events WHERE ts >= ?", (from_ts,)
                )
                records = cursor.fetchall()
    except (sqlite3.Error, OSError):
        records = []

    for verb, outcome, ms, tokens in records:
        row = rows.setdefault(verb, _VerbRow(verb=verb))
        row.calls += 1
        row.ms.append(int(ms))
        row.est_tokens += int(tokens)
        if outcome == "ok":
            row.ok += 1
        elif outcome == "dry_run":
            row.dry_run += 1
        else:
            row.errors += 1
            code = outcome.split(":", 1)[-1]
            row.by_class[code] = row.by_class.get(code, 0) + 1

    verbs = []
    alerts = []
    for row in sorted(rows.values(), key=lambda r: r.verb):
        rate = row.errors / row.calls if row.calls else 0.0
        verbs.append(
            {
                "verb": row.verb,
                "calls": row.calls,
                "ok": row.ok,
                "dry_run": row.dry_run,
                "errors": row.errors,
                "error_rate": round(rate, 4),
                "by_class": row.by_class,
                "p50_ms": _percentile(row.ms, 0.5),
                "p95_ms": _percentile(row.ms, 0.95),
                "est_tokens": row.est_tokens,
                "est_tokens_mean": round(row.est_tokens / row.calls, 1) if row.calls else 0.0,
            }
        )
        if row.calls >= 20 and rate > 0.10:
            alerts.append(
                {
                    "verb": row.verb,
                    "error_rate": round(rate, 4),
                    "calls": row.calls,
                    "message": f"{row.verb} failed {rate:.0%} of {row.calls} calls "
                    "(threshold 10% over 20 calls)",
                }
            )

    loop = rows.get("scout.stats", _VerbRow(verb="scout.stats"))
    return SummaryResult(
        since=since,
        from_ts=from_ts,
        verbs=verbs,
        totals={
            "calls": sum(r.calls for r in rows.values()),
            "errors": sum(r.errors for r in rows.values()),
            "est_tokens": sum(r.est_tokens for r in rows.values()),
            "scouter_loop_p50_ms": _percentile(loop.ms, 0.5),
            "scouter_loop_p95_ms": _percentile(loop.ms, 0.95),
            "failures_by_class": {
                code: sum(r.by_class.get(code, 0) for r in rows.values())
                for code in sorted({c for r in rows.values() for c in r.by_class})
            },
        },
        alerts=alerts,
    )


def _register() -> None:
    from scouter.registry import ArgSpec, Verb, register

    register(
        Verb(
            id="metrics.summary",
            group="metrics",
            fn=summary,
            args=(ArgSpec("since", "str", default="7d", help="window, e.g. 7d / 12h / 30m"),),
            writes=False,
            gated=False,
            summary="Per-verb calls, error rate, latency and estimated tokens.",
        )
    )


_register()
