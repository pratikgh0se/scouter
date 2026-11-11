"""The verb registry: one source of truth for all three layers.

The CLI builds its argparse tree from it, the MCP adapter builds its handler
map from it, and the parity + metrics tests use it as ground truth. A verb that
is registered but missing from a layer fails CI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle: types imports nothing of ours
    from scouter.types import Envelope


@dataclass(frozen=True)
class ArgSpec:
    """One argument, described once for every layer."""

    name: str
    type: str  # "str" | "int" | "float" | "bool" | "image" | "list[str]"
    required: bool = False
    default: Any = None
    help: str = ""
    positional: bool = False
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verb:
    id: str
    group: str
    fn: Callable[..., Any]
    args: tuple[ArgSpec, ...]
    writes: bool
    gated: bool
    summary: str
    cli_name: str = ""  # defaults to the verb's tail with - for _

    @property
    def cli_command(self) -> str:
        return self.cli_name or self.id.split(".", 1)[1].replace("_", "-")

    @property
    def mcp_name(self) -> str:
        return self.id.replace(".", "_")


VERB_REGISTRY: dict[str, Verb] = {}


def register(verb: Verb) -> Verb:
    if verb.id in VERB_REGISTRY:
        raise RuntimeError(f"verb {verb.id} registered twice")
    VERB_REGISTRY[verb.id] = verb
    return verb


#: Core modules whose import registers verbs, plus the metrics module (also a
#: verb source via `metrics.summary`). Imported by module name, one at a time,
#: so a module that doesn't exist yet (tasks 7-9) doesn't block the others.
_CORE_MODULES: tuple[str, ...] = (
    "scouter.config",
    "scouter.core.darkroom",
    "scouter.core.moves",
    "scouter.core.export",
    "scouter.core.history",
    "scouter.core.scout",
    "scouter.core.style",
    "scouter.core.library",
    "scouter.core.cull",
    "scouter.core.wb",
    "scouter.core.backup",
    "scouter.core.vocab",
    "scouter.core.phrases",
    "scouter.core.presets",
    "scouter.core.jobrun",
    "scouter.metrics",
)


#: Set once `load_verbs()` has attempted every module in `_CORE_MODULES`, so a
#: direct import of one core module by other code (or another test) before
#: `load_verbs()` runs — which populates `VERB_REGISTRY` without this flag —
#: doesn't get mistaken for "fully loaded" and short-circuit the rest.
_FULLY_LOADED = False


def load_verbs() -> dict[str, Verb]:
    """Import every core module so its `register(...)` calls run. Idempotent.

    Tolerates modules that don't exist yet: an `ImportError` naming the module
    itself (or a parent package of it) is swallowed so the others still load,
    and the registry returns whatever verbs exist. This lets the registry work
    before the core modules of tasks 7-9 land. Any other `ImportError` — e.g. a
    typo inside a core module that *does* exist, which fails on some other
    name — is re-raised, so it isn't mistaken for an absent module.

    Re-importing an already-imported module is a cheap no-op, so every call
    walks the full `_CORE_MODULES` list until all of them have been attempted
    at least once (tracked by `_FULLY_LOADED`, not by `VERB_REGISTRY` being
    non-empty — a partial registry populated by someone else's direct import
    of a single core module must not look "done").
    """
    global _FULLY_LOADED
    if not _FULLY_LOADED:
        import importlib

        for module_name in _CORE_MODULES:
            try:
                importlib.import_module(module_name)
            except ImportError as e:
                missing = e.name
                if missing is not None and (
                    missing == module_name or module_name.startswith(missing + ".")
                ):
                    continue
                raise
        _FULLY_LOADED = True
    return VERB_REGISTRY


def call(verb_id: str, layer: str, **kwargs: Any) -> Envelope:
    """Invoke a verb through the layer contract. Emits exactly one event."""
    from scouter import metrics
    from scouter.errors import ScouterError
    from scouter.paths import current_job, validate_job
    from scouter.types import Envelope

    verbs = load_verbs()
    if verb_id not in verbs:
        raise KeyError(f"unknown verb {verb_id!r}; known: {sorted(verbs)}")
    verb = verbs[verb_id]
    # Validate the job id HERE, not just where it becomes a path: this is the
    # one door every layer comes through, and a bad job id must be a typed
    # `usage` error (exit 2) rather than a traversal into the cache root.
    job = validate_job(kwargs["job"]) if kwargs.get("job") else current_job()
    kwargs["job"] = job

    from scouter.core import darkroom as _darkroom
    from scouter.core import watchdog

    token = metrics.LAYER.set(layer)
    retries: list[str] = []
    retry_token = _darkroom.STALE_RETRIES.set(retries)
    healed = False
    try:
        result, healed = watchdog.run_with_heal(verb_id, lambda: verb.fn(**kwargs), job)
    except ScouterError as e:
        return Envelope(
            verb=verb_id,
            layer=layer,
            job=job,
            ok=False,
            outcome=f"error:{e.code}",
            ms=metrics.last_ms(),
            data={},
            error=e.to_dict(),
        )
    finally:
        metrics.LAYER.reset(token)
        _darkroom.STALE_RETRIES.reset(retry_token)

    data = result.to_dict()
    if healed:
        data["healed"] = True  # spec 6: always reported; a heal is never silent
    if retries:
        # A stale sidecar rewrite was retried once and landed: never silent, and
        # countable by the gauntlet.
        data["stale_rewrite_retried"] = True
        top = data.setdefault("warnings", [])
        top.extend(w for w in retries if w not in top)
    outcome = data.get("outcome", "ok")
    # A verb's own result can carry outcome "dry_run" (ConfirmationRequired) as
    # well as "ok"; only those two count as success, everything else — an
    # "error:<code>" string a core function returned instead of raising — does not.
    error = None
    # Review round 1, should-fix (row 408 B2): `darkroom.undo_last`'s "noop"
    # outcome is a RESULT, not a raised ScouterError, so it never got an
    # `error` key at all -- an agent (especially over MCP, which has no
    # other structured place to look) saw only `data.reason`'s prose. Scoped
    # to this one verb+outcome pair deliberately: "noop" has exactly one
    # producer today, and a future one would need its own fix text, not
    # this one borrowed wholesale.
    if verb_id == "darkroom.undo_last" and outcome == "noop":
        error = {
            "code": "noop",
            "message": data.get("reason") or "nothing to undo",
            "fix": (
                "pin a job across calls ($SCOUTER_JOB for the CLI, job= for "
                "MCP/Python) so undo_last sees the same journal the earlier write "
                "landed in; the MCP server already does this for you (phase 3 task "
                "9's session job pins $SCOUTER_JOB once per server process), so "
                "this now only bites a bare CLI/Python call that never pins one"
            ),
        }
    return Envelope(
        verb=verb_id,
        layer=layer,
        job=job,
        ok=outcome in ("ok", "dry_run"),
        outcome=outcome,
        ms=metrics.last_ms(),
        data=data,
        error=error,
    )
