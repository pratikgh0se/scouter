"""The conversational vocabulary (phase 3 design §4).

One row per module parameter Scouter can move by words: unit, Scouter's own
conservative clamp, one conversational notch, where the current value is read
from, and the explanation text. `status` is DERIVED from the pins
(`dtbridge.ACTION_PATHS`, `xmp.LAYOUTS`), never stored: when a host probe pins
a path or an XMP slot, the row upgrades itself and the golden shows it.

Units are darktable's INTERNAL units (P3-R1): what `dt.gui.action(..., "set")`
takes. `%` rows carry `display_scale = 100` (the GUI shows 0.05 as 5 %).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from scouter import metrics
from scouter.core import dtbridge, xmp
from scouter.errors import UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import VocabResult

STATUSES = ("live", "absolute_only", "candidate", "unreachable")
CONCEPT_IDS = (
    "exposure", "tone_mapper", "white_balance", "color_balance", "chroma_saturation",
    "grading", "black_level", "color_zones", "presets",
)


@dataclass(frozen=True)
class ParamSpec:
    module: str
    param: str
    ui: str
    unit: str
    display_scale: float
    lo: float
    hi: float
    step: float
    value_source: str
    neutral: float | None
    wraps: bool
    concept: str
    why: str
    watch: str
    candidate_paths: tuple[str, ...] = ()
    reachable: bool = True
    note: str = ""


def _cb(param: str, ui: str, unit: str, lo: float, hi: float, step: float, concept: str,
        why: str, watch: str, candidates: tuple[str, ...] = (), *, wraps: bool = False,
        neutral: float | None = 0.0) -> ParamSpec:
    """A colour balance rgb row (`%` rows get display_scale 100)."""
    return ParamSpec(
        module="colorbalancergb", param=param, ui=f"color balance rgb › {ui}", unit=unit,
        display_scale=100.0 if unit == "%" else 1.0, lo=lo, hi=hi, step=step,
        value_source="xmp", neutral=None if wraps else neutral, wraps=wraps,
        concept=concept, why=why, watch=watch, candidate_paths=candidates,
    )


def _zone(zone: str, section: str, label: str) -> tuple[ParamSpec, ParamSpec, ParamSpec]:
    base = f"iop/colorbalancergb/{section}"
    flat = f"iop/colorbalancergb/{label}"
    return (
        _cb(f"{zone}_luminance", f"4 ways › {section} › luminance", "%", -0.25, 0.25, 0.02,
            "color_balance", f"lifts or lowers only the {label}",
            "noise in the darkest areas" if zone == "shadows" else
            ("clipping in the brightest areas" if zone == "highlights" else "skin"),
            (f"{base}/luminance", f"{flat} luminance")),
        _cb(f"{zone}_hue", f"4 ways › {section} › hue", "°", 0.0, 360.0, 10.0,
            "grading", f"picks the tint colour for the {label}",
            "neutral greys in that range", (f"{base}/hue", f"{flat} hue"), wraps=True),
        _cb(f"{zone}_chroma", f"4 ways › {section} › chroma", "%", 0.0, 0.3, 0.02,
            "grading", f"sets how strong the {label} tint is",
            "greys in that range turning coloured", (f"{base}/chroma", f"{flat} chroma")),
    )


PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("exposure", "exposure", "exposure › exposure", "EV", 1.0, -3.0, 4.0, 0.3,
              "xmp", None, False, "exposure",
              "brightens or darkens the whole frame evenly, before any tone mapping",
              "the highlights for clipping and the shadows for noise"),
    ParamSpec("exposure", "black", "exposure › black level correction", "ratio", 1.0,
              -0.1, 0.1, 0.005, "xmp", None, False, "black_level",
              "moves the point that renders as pure black",
              "the darkest areas: a few thousandths is a visible change"),
    ParamSpec("sigmoid", "contrast", "sigmoid › contrast", "ratio", 1.0, 0.5, 3.0, 0.1,
              "xmp", None, False, "tone_mapper",
              "steepens or flattens the middle of the tone curve",
              "shadow and highlight detail at the two ends"),
    ParamSpec("sigmoid", "skew", "sigmoid › skew", "ratio", 1.0, -1.0, 1.0, 0.1,
              "xmp", None, False, "tone_mapper",
              "shifts where the tone curve spends its contrast, toward shadows or highlights",
              "whether the midtones still sit where you want them"),
    ParamSpec("temperature", "temperature", "white balance › temperature", "K", 1.0,
              2000.0, 12000.0, 300.0, "wb_table", None, False, "white_balance",
              "tells darktable the light was bluer or redder, so the picture turns warmer or cooler",
              "neutral greys and skin"),
    ParamSpec("temperature", "tint", "white balance › tint", "ratio", 1.0, 0.5, 2.0, 0.05,
              "none", None, False, "white_balance",
              "trades green against magenta in the light", "skin and foliage"),
    _cb("contrast", "master › contrast", "%", -0.5, 0.5, 0.05, "color_balance",
        "adds or removes contrast around middle grey, after the tone mapper",
        "that it does not fight the sigmoid contrast"),
    _cb("global_saturation", "master › global saturation", "%", -0.5, 0.5, 0.05,
        "chroma_saturation",
        "changes colourfulness and darkens strong colours slightly, which reads natural",
        "skin and skies for over-cooked colour"),
    _cb("global_vibrance", "master › global vibrance", "%", -0.5, 0.5, 0.05,
        "chroma_saturation", "boosts the least colourful pixels most",
        "already-saturated areas, which should barely move",
        ("iop/colorbalancergb/global vibrance",)),
    _cb("global_chroma", "master › global chroma", "%", -0.5, 0.5, 0.05,
        "chroma_saturation", "changes colourfulness at constant brightness",
        "bright saturated colours going neon", ("iop/colorbalancergb/global chroma",)),
    *_zone("shadows", "shadows lift", "shadows"),
    *_zone("midtones", "power", "midtones"),
    *_zone("highlights", "highlights gain", "highlights"),
    ParamSpec("filmicrgb", "white_relative_exposure", "filmic rgb › scene › white relative exposure",
              "EV", 1.0, 2.0, 10.0, 0.25, "xmp", None, False, "tone_mapper",
              "sets which scene brightness maps to pure white", "clipped highlights",
              ("iop/filmicrgb/white relative exposure",)),
    ParamSpec("filmicrgb", "black_relative_exposure", "filmic rgb › scene › black relative exposure",
              "EV", 1.0, -14.0, -3.0, 0.25, "xmp", None, False, "tone_mapper",
              "sets which scene brightness maps to pure black", "blocked shadows",
              ("iop/filmicrgb/black relative exposure",)),
    ParamSpec("filmicrgb", "contrast", "filmic rgb › look › contrast", "ratio", 1.0, 0.5, 3.0,
              0.1, "xmp", None, False, "tone_mapper",
              "steepens or flattens the middle of the tone curve",
              "shadow and highlight detail at the two ends", ("iop/filmicrgb/contrast",)),
    ParamSpec("colorzones", "strength", "color zones › mix", "%", 100.0, 0.0, 2.0, 0.1,
              "xmp", None, False, "color_zones",
              "scales every colour-zone adjustment at once", "the zones you set in darktable",
              ("iop/colorzones/mix",)),
    ParamSpec("colorzones", "curves", "color zones › (zone curves)", "—", 1.0, 0.0, 0.0, 0.0,
              "none", None, False, "color_zones", "", "", (), False,
              "zones are curve nodes, not sliders: no dt.gui.action sets a zone value; "
              "use color balance rgb's per-zone sliders (4 ways) instead"),
)

_BY_KEY = {(s.module, s.param): s for s in PARAMS}


def spec(module: str, param: str) -> ParamSpec | None:
    return _BY_KEY.get((module, param))


def xmp_named(module: str, param: str) -> bool:
    """Is `param` a named field of any pinned XMP layout for `module`?"""
    for (operation, _version), layout in xmp.LAYOUTS.items():
        if operation != module:
            continue
        names = {f[0] for f in layout.get("fields", ())}
        names |= set(layout.get("named_slots", {}).values())
        if param in names:
            return True
    return False


def status(s: ParamSpec) -> str:
    if not s.reachable:
        return "unreachable"
    if (s.module, s.param) not in dtbridge.ACTION_PATHS:
        return "candidate"
    if s.value_source == "wb_table":
        return "live"
    if s.value_source == "xmp" and xmp_named(s.module, s.param):
        return "live"
    return "absolute_only"


def clamp(s: ParamSpec, value: float) -> tuple[float, bool]:
    """Scouter's safe range (P3-R15). Hue rows wrap modulo 360 instead."""
    if s.wraps:
        return value % 360.0, False
    if value < s.lo:
        return s.lo, True
    if value > s.hi:
        return s.hi, True
    return value, False


def to_display(s: ParamSpec, value: float) -> float:
    return value * s.display_scale


def closest_live(s: ParamSpec) -> ParamSpec | None:
    """The first `live` row with the same concept, else the same module, else None."""
    for pool in (
        [p for p in PARAMS if p.concept == s.concept],
        [p for p in PARAMS if p.module == s.module],
    ):
        for p in pool:
            if p is not s and status(p) == "live":
                return p
    return None


def table() -> list[dict]:
    rows = []
    for s in PARAMS:
        row = asdict(s)
        row["candidate_paths"] = list(s.candidate_paths)
        row["status"] = status(s)
        row["pinned_path"] = dtbridge.ACTION_PATHS.get((s.module, s.param))
        rows.append(row)
    return rows


@metrics.instrumented("vocab.list")
def list_vocab(module: str | None = None, *, job: str | None = None) -> VocabResult:
    """Every word Scouter can move, with its status today. No bridge calls."""
    job = job or current_job()
    rows = table()
    if module is not None:
        known = sorted({r["module"] for r in rows})
        if module not in known:
            raise UsageError(
                f"no vocabulary rows for module {module!r}",
                detail={"module": module, "known": known},
            )
        rows = [r for r in rows if r["module"] == module]
    counts = {name: sum(1 for r in rows if r["status"] == name) for name in STATUSES}
    return VocabResult(rows=rows, counts=counts)


register(
    Verb(
        id="vocab.list",
        group="vocab",
        fn=list_vocab,
        args=(ArgSpec("module", "str", default=None, help="only this module's rows"),),
        writes=False,
        gated=False,
        summary="The conversational vocabulary with each word's status today.",
    )
)
