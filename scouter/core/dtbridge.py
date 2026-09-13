"""Typed wrapper over the file-spool bridge. Turns bridge facts into error classes."""

from __future__ import annotations

import re
from typing import Any

from darktable_mcp.bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeTimeoutError,
)
from scouter.core import xmp
from scouter.errors import BridgeTimeout, NotInDarkroom, ScouterError, WrongView
from scouter.types import ResolvedImage

#: `dt.gui.action` paths for the phase-1 modules. A module/param pair not in
#: here is still usable -- the path is built as iop/<module>/<param> -- but the
#: table documents the ones the integration test proves.
ACTION_PATHS: dict[tuple[str, str], str] = {
    ("exposure", "exposure"): "iop/exposure/exposure",
    ("exposure", "black"): "iop/exposure/black level correction",
    ("sigmoid", "contrast"): "iop/sigmoid/contrast",
    ("sigmoid", "skew"): "iop/sigmoid/skew",
    ("temperature", "temperature"): "iop/temperature/temperature",
    ("temperature", "tint"): "iop/temperature/tint",
    ("colorbalancergb", "contrast"): "iop/colorbalancergb/contrast",
    ("colorbalancergb", "global_saturation"): "iop/colorbalancergb/global saturation",
}

_DB_LOCKED_RE = re.compile(r"database is locked|could not get lock|another instance", re.I)

#: Lua fix round 2: a `darkroom.open` call for an image other than the one
#: darktable actually loaded no longer comes back as `{opened: False, error:
#: ...}` -- it is a JSON-RPC error whose message starts with this prefix, and
#: `Bridge.call` raises a plain `BridgeError` for it. There is no `WrongImage`
#: class in `scouter.errors` (spec section 9's table), so this is classified as
#: `not_in_darkroom` with the plugin's own message preserved verbatim, per the
#: controller's ruling -- never folded into the generic `bridge_timeout` catch-all.
_WRONG_IMAGE_RE = re.compile(r"^wrong image loaded:", re.I)

_bridge: Bridge | None = None


def get_bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge


def set_bridge(bridge: Any) -> None:
    """Test seam: install a FakeBridge. Production code never calls this."""
    global _bridge
    _bridge = bridge


def action_path(module: str, param: str) -> str:
    return ACTION_PATHS.get((module, param), f"iop/{module}/{param}")


def call(method: str, params: dict | None = None, timeout: float | None = None) -> Any:
    """One bridge call, with the bridge's own failures mapped to error classes."""
    try:
        return get_bridge().call(method, params or {}, timeout=timeout)
    except (BridgeTimeoutError, BridgePluginNotInstalledError) as e:
        raise BridgeTimeout(str(e), detail={"method": method}) from e
    except BridgeError as e:
        message = str(e)
        if _WRONG_IMAGE_RE.search(message):
            raise NotInDarkroom(message, detail={"method": method}) from e
        if _DB_LOCKED_RE.search(message):
            from scouter.errors import DbLocked

            raise DbLocked(message, detail={"method": method}) from e
        raise BridgeTimeout(
            f"bridge method {method} failed: {message}", detail={"method": method}
        ) from e


def current_view() -> str:
    result = call("view.current")
    return str(result.get("view", "unknown"))


def require_darkroom() -> str:
    """Pre-flight: raise `wrong_view` before spending an action on a dead call.

    No `expect_image_id` parameter: `view.current` cannot tell us which image
    is loaded, so a parameter here would be decorative, not honest (fix round
    1, blocker 1). A caller that must confirm it is acting on a specific image
    reads `darkroom.current` (no params, no side effects) through `call()`
    first and compares the `image_id` itself -- fix round 2, blocker 1
    replaced the earlier approach of calling `darkroom.open {image_id: ...}`
    to force a mismatch to surface as an error, because that force-switches
    the darkroom onto the expected image instead of just checking it.
    """
    view = current_view()
    if view != "darkroom":
        raise WrongView(
            f"darktable is in the {view} view; slider actions only work in the darkroom",
            detail={"view": view},
        )
    return view


class ImageNotFound(ScouterError):
    """The image is not in darktable's library, and this verb may not import it.

    A `usage` outcome (exit 2), not one of spec section 9's darktable failure
    classes: nothing failed, the caller named an image darktable does not know.
    Deliberately not in `errors.ERROR_CLASSES` for that reason.
    """

    code = "usage"
    fix = (
        "open the image in darktable first (`scouter darkroom open <path>`), which "
        "imports it into the library, then re-run this read"
    )


def resolve_image(image: str | int, *, may_import: bool = False) -> ResolvedImage:
    """Accept a path or an image id and return both, plus the sidecar path.

    Resolution goes through `image.lookup`, which reads `dt.database` and
    touches no GUI state at all -- for BOTH an id and a path (final review,
    blocker 2). It used to go through `darkroom.open`, which meant every read
    verb (`history.read`, `scout.*`) and every dry run switched the darkroom
    onto the image being read, and imported it if it was unknown. Reading must
    never move the user's darkroom.

    `may_import=True` is for write verbs only, on a real (non-dry) call: an
    unknown path is then registered through `darkroom.open`, which is the Lua
    API's own way to add an image (ruling 4) -- and which does load it in the
    darkroom, so the caller is asking for that side effect knowingly.
    """
    as_id: int | None = None
    if isinstance(image, int):
        as_id = int(image)
    elif isinstance(image, str) and image.isdigit():
        as_id = int(image)

    key = {"image_id": as_id} if as_id is not None else {"path": str(image)}
    found = call("image.lookup", key)
    if not found.get("found"):
        if as_id is not None:
            # An id that is not in the library cannot be imported -- there is
            # no path to import. This is a dead reference either way.
            raise ImageNotFound(
                f"no image with id {as_id} in darktable's library",
                detail={"image_id": as_id},
            )
        if not may_import:
            raise ImageNotFound(
                f"{image} is not in darktable's library",
                detail={"path": str(image)},
            )
        found = call("darkroom.open", {"path": str(image)})
    path = str(found["path"])
    return ResolvedImage(
        image_id=int(found["image_id"]), path=path, xmp_path=str(xmp.sidecar_path(path))
    )


def action(
    path: str, *, instance: int = 0, element: str = "value", effect: str = "set", value: float = 0.0
) -> dict:
    """One `dt.gui.action`. A nan return is `not_in_darkroom`, never a value."""
    result = call(
        "darkroom.action",
        {"path": path, "instance": instance, "element": element, "effect": effect, "value": value},
    )
    if result.get("nan"):
        raise NotInDarkroom(
            f"action {path} returned nan (view is {result.get('view')})",
            detail={"path": path, "view": result.get("view"), "element": element, "effect": effect},
        )
    return result
