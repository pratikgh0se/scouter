"""The verb registry: one source of truth for all three layers.

The CLI builds its argparse tree from it, the MCP adapter builds its handler
map from it, and the parity + metrics tests use it as ground truth. A verb that
is registered but missing from a layer fails CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ArgSpec:
    """One argument, described once for every layer."""

    name: str
    type: str                      # "str" | "int" | "float" | "bool" | "image" | "list[str]"
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
    cli_name: str = ""             # defaults to the verb's tail with - for _

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
    "scouter.core.darkroom",
    "scouter.core.export",
    "scouter.core.history",
    "scouter.core.scout",
    "scouter.core.style",
    "scouter.metrics",
)


def load_verbs() -> dict[str, Verb]:
    """Import every core module so its `register(...)` calls run. Idempotent.

    Tolerates modules that don't exist yet: an `ImportError` for one module is
    swallowed so the others still load, and the registry returns whatever
    verbs exist. This lets the registry work before the core modules of
    tasks 7-9 land.
    """
    if not VERB_REGISTRY:
        import importlib

        for module_name in _CORE_MODULES:
            try:
                importlib.import_module(module_name)
            except ImportError:
                continue
    return VERB_REGISTRY


def call(verb_id: str, layer: str, **kwargs: Any) -> "Envelope":
    """Invoke a verb through the layer contract. Emits exactly one event."""
    from scouter import metrics
    from scouter.errors import ScouterError
    from scouter.paths import current_job
    from scouter.types import Envelope

    verbs = load_verbs()
    if verb_id not in verbs:
        raise KeyError(f"unknown verb {verb_id!r}; known: {sorted(verbs)}")
    verb = verbs[verb_id]
    job = kwargs.get("job") or current_job()
    kwargs["job"] = job

    token = metrics.LAYER.set(layer)
    try:
        result = verb.fn(**kwargs)
    except ScouterError as e:
        return Envelope(
            verb=verb_id, layer=layer, job=job, ok=False,
            outcome=f"error:{e.code}", ms=metrics.last_ms(), data={}, error=e.to_dict(),
        )
    finally:
        metrics.LAYER.reset(token)

    data = result.to_dict()
    outcome = data.get("outcome", "ok")
    return Envelope(
        verb=verb_id, layer=layer, job=job, ok=outcome != "error",
        outcome=outcome, ms=metrics.last_ms(), data=data,
    )
