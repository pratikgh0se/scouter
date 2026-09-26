"""Typed wrapper over the file-spool bridge. Turns bridge facts into error classes."""

from __future__ import annotations

import re
import time
from typing import Any

from darktable_mcp.bridge.client import (
    Bridge,
    BridgeError,
    BridgePluginNotInstalledError,
    BridgeTimeoutError,
)
from scouter.core import xmp
from scouter.errors import BridgeTimeout, NotInDarkroom, ScouterError, UsageError, WrongView
from scouter.types import ResolvedImage

#: `dt.gui.action` paths for the phase-1 modules. This table is a WHITELIST,
#: not documentation: a pair that is not in here is refused by
#: `action_path` (task 11 ruling, carried finding 1). Reason, measured on the
#: real host: `dt.gui.action` on a path darktable 5.4.1 does not recognise
#: wedges its Lua worker for the REST OF THE SESSION -- every later bridge
#: call, including an unrelated `view.current`, then times out
#: (`tests/test_host_phase2.py::PinWbCoefficientSlidersHostTest`,
#: `docs/measurements.md`). So a guessed `iop/<module>/<param>` fallback does
#: not cost one failed write, it costs the whole darkroom session. A new pair
#: is added here only after a host probe answers non-`nan` for it.
ACTION_PATHS: dict[tuple[str, str], str] = {
    ("exposure", "exposure"): "iop/exposure/exposure",
    ("exposure", "black"): "iop/exposure/black level correction",
    ("sigmoid", "contrast"): "iop/sigmoid/contrast",
    ("sigmoid", "skew"): "iop/sigmoid/skew",
    ("temperature", "temperature"): "iop/temperature/temperature",
    ("temperature", "tint"): "iop/temperature/tint",
    ("colorbalancergb", "contrast"): "iop/colorbalancergb/contrast",
    ("colorbalancergb", "global_saturation"): "iop/colorbalancergb/global saturation",
    # phase 3 task 10 host probe, 2026-09-24: answered non-nan, worker alive after
    # (one darktable 5.4.1 launch per candidate, 20 s settle + control path;
    # docs/measurements.md "Phase 3 host run").
    ("colorbalancergb", "global_vibrance"): "iop/colorbalancergb/global vibrance",
    ("colorbalancergb", "global_chroma"): "iop/colorbalancergb/global chroma",
    ("colorbalancergb", "midtones_luminance"): "iop/colorbalancergb/power/luminance",
    ("colorbalancergb", "midtones_hue"): "iop/colorbalancergb/power/hue",
    ("colorbalancergb", "midtones_chroma"): "iop/colorbalancergb/power/chroma",
    ("filmicrgb", "white_relative_exposure"): "iop/filmicrgb/white relative exposure",
    ("filmicrgb", "black_relative_exposure"): "iop/filmicrgb/black relative exposure",
    ("filmicrgb", "contrast"): "iop/filmicrgb/contrast",
    ("colorzones", "strength"): "iop/colorzones/mix",
    # phase 3 task 10 host probe (second pass), 2026-09-24: answered non-nan,
    # worker alive after. The spec's `shadows lift/<x>` / `highlights gain/<x>`
    # and flat spellings answered nan; darktable uses the short section word.
    ("colorbalancergb", "shadows_luminance"): "iop/colorbalancergb/lift/luminance",
    ("colorbalancergb", "shadows_hue"): "iop/colorbalancergb/lift/hue",
    ("colorbalancergb", "shadows_chroma"): "iop/colorbalancergb/lift/chroma",
    ("colorbalancergb", "highlights_luminance"): "iop/colorbalancergb/gain/luminance",
    ("colorbalancergb", "highlights_hue"): "iop/colorbalancergb/gain/hue",
    ("colorbalancergb", "highlights_chroma"): "iop/colorbalancergb/gain/chroma",
}

_DB_LOCKED_RE = re.compile(r"database is locked|could not get lock|another instance", re.I)

#: Lua fix round 2: a `darkroom.open` call for an image other than the one
#: darktable actually loaded no longer comes back as `{opened: False, error:
#: ...}` -- it is a JSON-RPC error whose message CONTAINS this phrase, and
#: `Bridge.call` raises a plain `BridgeError` for it. There is no `WrongImage`
#: class in `scouter.errors` (spec section 9's table), so this is classified as
#: `not_in_darkroom` with the plugin's own message preserved verbatim, per the
#: controller's ruling -- never folded into the generic `bridge_timeout` catch-all.
#:
#: Phase 3 task 8 / E12 D4 row 407: this used to be anchored
#: (`^wrong image loaded:`), which assumed the message started with those
#: exact words. `Bridge.call` (darktable_mcp/bridge/client.py) wraps a Lua
#: `error()` as `"handler raised: <file>:<line>: wrong image loaded: ..."` --
#: `darktable_mcp.lua`'s `error(...)` calls (lines ~1218/1224) pass no
#: explicit level, so Lua's OWN `error()` prepends `"<source>:<line>: "` to
#: the message, e.g. `"darktable_mcp.lua:1218: wrong image loaded: asked for
#: 113, darkroom shows 92 after 15000ms"`. A `^` anchor never matched that,
#: only the fabricated strings this codebase's own fakes used to hand-craft,
#: so it fell through to the generic `raise BridgeTimeout(...)` below: a
#: same-session import (R20, honest limit: darktable cannot load a frame
#: this job imported into the SAME running session) was misreported as
#: `bridge_timeout` with the fix text "start darktable ... restart
#: darktable" -- wrong, because darktable is running, and costs the 15s
#: `WAIT_FOR_IMAGE_BUDGET_MS` timeout on top. `search`, not `match`/`^`, so
#: the phrase is found wherever Lua's own position prefix put it.
_WRONG_IMAGE_RE = re.compile(r"wrong image loaded:", re.I)

#: The actionable fix for a wrong-image refusal specifically (E12 D4): never
#: the generic `NotInDarkroom.fix` (written for a nan *action*, i.e. a
#: module not in the pipeline), and never `BridgeTimeout.fix` ("start
#: darktable ... restart darktable") -- darktable IS running, it is just
#: showing a different frame than the one asked for.
_WRONG_IMAGE_FIX = (
    "darktable did not load the frame you asked for -- open it in darktable "
    "yourself (double-click it in the lighttable) or relaunch darktable on "
    "it, then retry"
)

#: Row 433: the plugin refuses a GUI-touching verb while darktable is still
#: starting (its GTK main loop is not running yet, and a gtk-wrapped Lua call
#: in that window self-deadlocks darktable -- see the GUI-ready gate in
#: darktable_mcp.lua). Nothing was done, darktable is healthy: retry, never
#: "restart darktable".
_GUI_NOT_READY_RE = re.compile(r"gui_not_ready:", re.I)
_GUI_NOT_READY_FIX = (
    "darktable is still starting (its window has not finished opening); "
    "retry in a few seconds -- `scouter doctor` shows gui_ready once it is up"
)

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


def action_path(module: str, param: str, *, probe_unpinned: bool = False) -> str:
    """The pinned `dt.gui.action` path for one module parameter.

    Refuses an unpinned pair with `usage` rather than guessing
    `iop/<module>/<param>`: see `ACTION_PATHS` for the measured reason (an
    unrecognised path wedges darktable's Lua worker for the session).

    `probe_unpinned=True` is the ONE bypass, and it exists for the host
    probes in `tests/test_host_phase2.py` whose whole job is to find out
    which candidate paths darktable accepts. No production code path passes
    it; `tests/test_dtbridge.py` asserts that.
    """
    pinned = ACTION_PATHS.get((module, param))
    if pinned is not None:
        return pinned
    if probe_unpinned:
        return f"iop/{module}/{param}"
    same_module = sorted(p for (m, p) in ACTION_PATHS if m == module)
    if same_module:
        detail_fix = f"pinned parameters for `{module}`: {', '.join(same_module)}"
    else:
        modules = sorted({m for (m, _) in ACTION_PATHS})
        detail_fix = f"no parameter of `{module}` is pinned; pinned modules: {', '.join(modules)}"
    raise UsageError(
        f"no pinned darktable action path for {module}.{param}",
        fix=(
            f"{detail_fix}. Adding a path is a host-probe job, not a guess: an "
            "unrecognised `dt.gui.action` path wedges darktable's Lua worker for the "
            "whole session, so pin it in scouter/core/dtbridge.py's ACTION_PATHS only "
            "after a probe answers non-nan for it."
        ),
        detail={
            "module": module,
            "param": param,
            "pinned_params_for_module": same_module,
        },
    )


#: Row 408, B6: modules `darkroom.enable`/`darkroom.reset` may target.
#: Separate from `ACTION_PATHS` (which pins (module, param) SET targets,
#: not whole-module enable/reset) but the SAME reasoning: `_do_module_action`
#: used to send `iop/<module>` for any string, and an unrecognised path
#: wedges darktable's Lua worker for the rest of the session (same measured
#: cause `ACTION_PATHS` exists for). Every `ACTION_PATHS` module, plus the
#: three the editor benchmark exercised successfully via `enable` (`lens`,
#: `sharpen`, `denoiseprofile` -- L1/L2/S1/S2, all pass).
MODULE_WHITELIST: frozenset[str] = frozenset(
    {m for (m, _p) in ACTION_PATHS} | {"lens", "sharpen", "denoiseprofile"}
)


def require_known_module(module: str) -> None:
    """Refuse an unpinned module BEFORE any bridge call (row 408 B6/B8):
    the wedge risk `ACTION_PATHS`/`action_path` exists for applies here too,
    and the refusal must land before `journal.begin` or a bridge round trip,
    never after."""
    if module not in MODULE_WHITELIST:
        raise UsageError(
            f"no pinned module {module!r} for darkroom.enable/darkroom.reset",
            fix=(
                f"pinned modules: {', '.join(sorted(MODULE_WHITELIST))}. Adding one "
                "is a host-probe job, not a guess -- an unrecognised module wedges "
                "darktable's Lua worker for the whole session."
            ),
            detail={"module": module, "pinned_modules": sorted(MODULE_WHITELIST)},
        )


def call(method: str, params: dict | None = None, timeout: float | None = None) -> Any:
    """One bridge call, with the bridge's own failures mapped to error classes."""
    try:
        return get_bridge().call(method, params or {}, timeout=timeout)
    except (BridgeTimeoutError, BridgePluginNotInstalledError) as e:
        raise BridgeTimeout(str(e), detail={"method": method}) from e
    except BridgeError as e:
        message = str(e)
        if _WRONG_IMAGE_RE.search(message):
            raise NotInDarkroom(message, fix=_WRONG_IMAGE_FIX, detail={"method": method}) from e
        if _GUI_NOT_READY_RE.search(message):
            raise BridgeTimeout(
                message, fix=_GUI_NOT_READY_FIX, detail={"method": method, "gui_not_ready": True}
            ) from e
        if _DB_LOCKED_RE.search(message):
            from scouter.errors import DbLocked

            raise DbLocked(message, detail={"method": method}) from e
        raise BridgeTimeout(
            f"bridge method {method} failed: {message}", detail={"method": method}
        ) from e


#: Watchdog design 2.1: the ping budget. Only ever called after a timeout.
HEALTH_TIMEOUT_S = 5.0


def health(timeout: float = HEALTH_TIMEOUT_S) -> dict:
    """One `darktable_mcp.diag` call with a 5 s budget: `alive` (answered,
    GUI ready), `starting` (answered, gui_ready false) or `unresponsive` (no
    answer). diag is exempt from the plugin's GUI gate and touches no
    gtk-wrapped API, so an answer proves the Lua worker is alive."""
    t0 = time.monotonic()
    try:
        d = get_bridge().call("darktable_mcp.diag", {}, timeout=timeout) or {}
    except Exception as e:  # noqa: BLE001 - any failure to answer is "unresponsive"
        return {"state": "unresponsive", "ping_ms": int(1000 * (time.monotonic() - t0)),
                "error": str(e)[:200]}
    ready = d.get("gui_ready")
    return {"state": "starting" if ready is False else "alive",
            "ping_ms": int(1000 * (time.monotonic() - t0)), "gui_ready": ready,
            "gui_ready_source": d.get("gui_ready_source")}


def current_view() -> str:
    result = call("view.current")
    return str(result.get("view", "unknown"))


def diag() -> dict:
    """`darktable_mcp.diag`: the plugin's default view-switch transport
    (`view_switch_transport`, "action" since row 429 removed the main-thread
    proxy) and commit order (`auto_commit_order`: no_bounce, then
    view_bounce_action). Read-only; presses nothing. `scouter doctor` shows
    the transport so an old plugin install is visible before a commit."""
    return call("darktable_mcp.diag")


def probe_main_thread(switch_views: bool = False) -> dict:
    """`darktable_mcp.probe_main_thread` (row 418/422): one row per route onto
    darktable's GTK main thread, each with what was observed and what the
    darktable 5.4.1 source predicts. `switch_views=True` additionally does a
    darkroom -> lighttable -> darkroom round trip through the global
    switch-views commands -- the only row that moves the view."""
    return call("darktable_mcp.probe_main_thread", {"switch_views": bool(switch_views)})


def view_transport(set_to: str | None = None) -> str:
    """Read (or with `set_to`, change) the plugin's default view-switch
    transport. Only "action" (darktable's own global switch-views commands,
    run on the GTK main thread) exists since row 429; the plugin refuses any
    other value, including the removed "proxy"."""
    params = {"set": set_to} if set_to else {}
    return str(call("darktable_mcp.view_transport", params).get("transport", "unknown"))


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


def _lookup_key(image: str | int) -> dict:
    """The single `image.lookup` argument for one caller-supplied image."""
    if isinstance(image, int):
        return {"image_id": int(image)}
    if isinstance(image, str) and image.isdigit():
        return {"image_id": int(image)}
    return {"path": str(image)}


def resolve_images(images: list[str | int]) -> list[ResolvedImage | ScouterError]:
    """Resolve a whole set in ONE `image.lookup` call (ruling R15).

    Returns one entry per input, in order: a `ResolvedImage`, or the
    `ImageNotFound` that `resolve_image` would have raised for it. A set
    resolution must never abort on one bad candidate (a stale entry in a
    300-file `import.json`), so the miss is returned rather than raised and
    the caller decides.

    Why the list form exists: the Lua handler's path match walks all of
    `dt.database`, so resolving 300 frames one call at a time was 300 full
    library scans plus 300 bridge round trips. The list form walks the
    database once. There is no `may_import` here -- importing is
    `darkroom.open`'s job, it moves the darkroom, and doing it implicitly
    for a whole set is exactly the side effect ruling 4 keeps explicit.

    A plugin too old to know the list form answers without `results`; that
    is detected and falls back to one call per image, so a stale installed
    plugin degrades in speed, never in correctness.
    """
    if not images:
        return []
    reply = call("image.lookup", {"items": [_lookup_key(i) for i in images]})
    records = reply.get("results") if isinstance(reply, dict) else None
    if not isinstance(records, list) or len(records) != len(images):
        return [_resolve_one_safely(i) for i in images]
    out: list[ResolvedImage | ScouterError] = []
    for image, record in zip(images, records):
        out.append(_from_lookup_record(image, record))
    return out


def _resolve_one_safely(image: str | int) -> ResolvedImage | ScouterError:
    try:
        return resolve_image(image)
    except ScouterError as e:
        return e


def _from_lookup_record(image: str | int, record: dict) -> ResolvedImage | ScouterError:
    if not record.get("found"):
        key = _lookup_key(image)
        if "image_id" in key:
            return ImageNotFound(
                f"no image with id {key['image_id']} in darktable's library",
                detail={"image_id": key["image_id"]},
            )
        return ImageNotFound(f"{image} is not in darktable's library", detail={"path": str(image)})
    path = str(record["path"])
    return ResolvedImage(
        image_id=int(record["image_id"]), path=path, xmp_path=str(xmp.sidecar_path(path))
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
