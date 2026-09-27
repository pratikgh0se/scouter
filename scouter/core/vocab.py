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
    "grading", "black_level", "color_zones", "presets", "finishing", "geometry", "detail",
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
    #: darktable's module default for this param (spec §4.3 `default`
    #: column: filled only where §4.3 asserts the value neutral by
    #: construction, blank everywhere else), declared here only once a real
    #: check has established it -- either §4.3's own citation, or a
    #: `tests/test_host_phase3.py` host probe on a fresh, untouched frame.
    #: Final review #1a ruling: a `set` move whose before value is
    #: unreadable journals this as its before (source "default") when the
    #: row has no `neutral`; a row with neither refuses the move
    #: `value_unknown`. Task 10: the colour balance rgb hue rows declare 0.0
    #: (darktable's module default, §4.3-cited).
    default: float | None = None
    #: A candidate value for `default`, believed correct but not yet
    #: established by either route above (row 474, final-review issue I1):
    #: only `tests/gauntlet/exec_looks.py`'s `chart_golden` may probe with
    #: it (recording `calibration_errors[key] = "default_unverified"` when
    #: it does, so the gauntlet evaluator treats a look depending on it
    #: exactly like a skipped calibration -- never `verified`). No other
    #: reader (`scouter/core/moves.py`, `scouter/core/darkroom.py`, the
    #: coverage/observe gauntlet layers) may treat this as a known value.
    #: Promote to `default` only once `ProbeCandidatePathsHostTest`'s sibling
    #: host probe (or an equivalent host measurement) pins it.
    default_candidate: float | None = None


#: darktable's colorbalancergb default for every hue param (degrees).
_CB_HUE_DEFAULT = 0.0


#: darktable's exposure module has TWO real, workflow-dependent defaults for
#: `black` (an additive ratio correction), per darktable's own source
#: (`src/iop/exposure.c`, `reload_defaults()`, master, fetched 2026-09-27):
#:
#:   if(scene_raw && self->multi_priority == 0) {
#:     d->exposure = mono ? 0.0f : 0.7f;
#:     d->black    = mono ? 0.0f : -0.000244140625f;
#:   } else { d->exposure = 0.0f; d->black = 0.0f; }
#:
#: -0.000244140625 == -(2**-12) is darktable's DECLARED default for a
#: scene-referred colour raw (most real camera raws) -- a deliberate
#: constant, not a quantisation step or a slot-floor rounding artifact (that
#: reading was reviewer-refuted: row 495/496 fix round 1, finding 2). Every
#: other workflow (monochrome, or a display-referred/non-scene-raw path)
#: still defaults `black` to exactly 0.0. Row 495/496:
#: `tests/test_host_phase3.py::ProbeExposureBlackDefaultHostTest` (host run
#: 2026-09-27) measured exactly this scene-raw constant on a fresh module.
#: `default = 0.0` stays the cited value for this row (the non-scene-raw
#: case, still the simpler one to declare); the tolerance below exists so a
#: read of the OTHER real default (scene-raw) is not mistaken for an
#: uncorrected offset.
EXPOSURE_BLACK_SCENE_RAW_DEFAULT = -(2 ** -12)

#: Per-row overrides of the "is this live value its declared default"
#: tolerance, keyed `(module, param)`. A row with no entry here keeps its
#: caller's generic epsilon (`darkroom._close`'s historical `1e-4`, tight
#: enough for every other default/neutral, which are all exact by
#: construction). `1.5x` the scene-raw default's magnitude: covers BOTH of
#: exposure.black's real darktable defaults (0.0 declared here, and
#: -0.000244140625 for a scene-referred colour raw, `exposure.c` above),
#: while still rejecting a real offset an order of magnitude bigger (e.g.
#: -0.0027, the stale-history live check's landed value against ITS target,
#: not its default -- not a default match either way, and correctly refused
#: by this margin).
_DEFAULT_TOLERANCE: dict[tuple[str, str], float] = {
    ("exposure", "black"): abs(EXPOSURE_BLACK_SCENE_RAW_DEFAULT) * 1.5,
}


def default_tolerance(module: str, param: str, fallback: float = 1e-4) -> float:
    """Tolerance for "does this live value equal (module, param)'s declared
    `neutral`/`default`" -- applied wherever that comparison is made (today:
    `darkroom._module_at_defaults`), never special-cased to one caller.
    `fallback` is the caller's own generic epsilon for a row with no
    override here."""
    return _DEFAULT_TOLERANCE.get((module, param), fallback)


def _cb(param: str, ui: str, unit: str, lo: float, hi: float, step: float, concept: str,
        why: str, watch: str, candidates: tuple[str, ...] = (), *, wraps: bool = False,
        neutral: float | None = 0.0) -> ParamSpec:
    """A colour balance rgb row (`%` rows get display_scale 100)."""
    return ParamSpec(
        module="colorbalancergb", param=param, ui=f"color balance rgb › {ui}", unit=unit,
        display_scale=100.0 if unit == "%" else 1.0, lo=lo, hi=hi, step=step,
        value_source="xmp", neutral=None if wraps else neutral, wraps=wraps,
        concept=concept, why=why, watch=watch, candidate_paths=candidates,
        # Phase 3 task 10 ruling: a hue row declares darktable's module
        # default (0.0 degrees, read from the sidecar darktable itself wrote
        # for a freshly enabled colour balance rgb, tests/fixtures/
        # colorbalancergb.xmp slots 2/5/8), so "warm the highlights" can set
        # a hue on an image with no colour-balance history and still be
        # undone to what the module really held.
        default=_CB_HUE_DEFAULT if wraps else None,
    )


#: Touch-floor ruling (row: htc-touch-floor, provisional/reversible): each
#: zone's `touch` magnitude (INTENSITY factor 0.5 x step, see phrases.py) must
#: land at or above the M5 stat floor the gauntlet evaluator checks the
#: matching phrase against (`tests/gauntlet/layers/coverage.py:PHRASE_STAT`,
#: floors from `gauntlet.metrics.FLOOR_MIN`), as measured on the recorded
#: night-1 evidence (`~/Pictures/ScouterScratch/gauntlet/night1-20260926/
#: cases/cov-phrase-*-bare.json`, both hero bodies).
#:
#: `test_vocab.py` enforces `step <= (hi - lo) / 4` for every reachable row
#: (a Scouter safety invariant, not touched here), which caps how far a step
#: can rise inside the existing clamp. `shadows_luminance` on the dark
#: olympus hero needs a touch of ~0.50 to clear its 0.5 L* floor; the cap
#: inside its +-0.25 clamp is 0.0625. The zone-chroma family needs ~0.15-0.31
#: for `cool_shadows`/`teal_orange`; the cap inside its 0-0.3 clamp is
#: 0.0375. These stay a residual, documented in the triage doc's
#: D-bare-step-below-perceptual-floor cluster as needing a brighter/more
#: chromatic hero frame or a rescaled M5 floor (Pratik's roadmap call, not a
#: step size) -- every step below is raised to the invariant's ceiling.
_ZONE_LUMINANCE_STEP = {"shadows": 0.125, "midtones": 0.06, "highlights": 0.10}
#: All three zones share one clamp (0.0-0.3); the invariant ceiling is 0.075.
_ZONE_CHROMA_STEP = 0.075


def _zone(zone: str, section: str, label: str,
          short: str | None = None) -> tuple[ParamSpec, ParamSpec, ParamSpec]:
    base = f"iop/colorbalancergb/{section}"
    flat = f"iop/colorbalancergb/{label}"
    # Task 10 host probe: `power/<x>` answered but `shadows lift/<x>` and
    # `highlights gain/<x>` (and the flat forms) answered nan, so the short
    # section word (`lift`, `gain`) is tried first for those two zones.
    lead = (f"iop/colorbalancergb/{short}",) if short else ()
    return (
        _cb(f"{zone}_luminance", f"4 ways › {section} › luminance", "%", -0.25, 0.25,
            _ZONE_LUMINANCE_STEP[zone],
            "color_balance", f"lifts or lowers only the {label}",
            "noise in the darkest areas" if zone == "shadows" else
            ("clipping in the brightest areas" if zone == "highlights" else "skin"),
            (*(f"{b}/luminance" for b in lead), f"{base}/luminance", f"{flat} luminance")),
        _cb(f"{zone}_hue", f"4 ways › {section} › hue", "°", 0.0, 360.0, 10.0,
            "grading", f"picks the tint colour for the {label}",
            "neutral greys in that range", (*(f"{b}/hue" for b in lead), f"{base}/hue", f"{flat} hue"), wraps=True),
        _cb(f"{zone}_chroma", f"4 ways › {section} › chroma", "%", 0.0, 0.3, _ZONE_CHROMA_STEP,
            "grading", f"sets how strong the {label} tint is",
            "greys in that range turning coloured", (*(f"{b}/chroma" for b in lead), f"{base}/chroma", f"{flat} chroma")),
    )


PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("exposure", "exposure", "exposure › exposure", "EV", 1.0, -3.0, 4.0, 0.3,
              "xmp", None, False, "exposure",
              "brightens or darkens the whole frame evenly, before any tone mapping",
              "the highlights for clipping and the shadows for noise"),
    ParamSpec("exposure", "black", "exposure › black level correction", "ratio", 1.0,
              -0.1, 0.1, 0.005, "xmp", None, False, "black_level",
              "moves the point that renders as pure black",
              "the darkest areas: a few thousandths is a visible change",
              # Row 474 (final-review issue I1): §4.3's own table
              # (docs/superpowers/specs/2026-09-24-scouter-phase3-design.md:
              # 148-153) leaves this row's `default` column BLANK by design
              # -- it fills that column only where a value is asserted
              # neutral by construction, and groups exposure.black with
              # exposure.exposure/sigmoid.skew/temperature.tint as rows that
              # are not. `0.0` was a plausible CANDIDATE (darktable's
              # dt_iop_exposure_params_t.black is 0.0f in its C default, and
              # an additive correction with no active history entry applies
              # none), pending host verification -- unlike the sign
              # convention (§10, a rendered before/after scout.stats run),
              # which was already measured.
              #
              # Row 495/496: `tests/test_host_phase3.py::
              # ProbeExposureBlackDefaultHostTest` (host run 2026-09-27)
              # found a fresh exposure module's `black` at
              # -0.000244140625 == -(2**-12) -- darktable's OWN declared
              # default for `black` on a scene-referred colour raw
              # (`src/iop/exposure.c reload_defaults()`,
              # `EXPOSURE_BLACK_SCENE_RAW_DEFAULT`, this module), not a
              # quantisation artifact (fix round 1, finding 2: that reading
              # was wrong and reviewer-refuted). Every other workflow
              # (monochrome, non-scene-raw) defaults `black` to exactly
              # 0.0, the value declared here; `default_tolerance` covers
              # BOTH real defaults, so a fresh scene-raw module reads as
              # "at default" too. PASS: promoted from `default_candidate`
              # to the cited `default` below; this is NOT a spec §4.3 edit
              # (that table stays as-is, blank), only this repo's own
              # host-verified value.
              default=0.0),
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
    # Route A for warmth (decision rows 458/461, Pratik 2026-09-27): when the
    # white balance is in camera reference (late) mode (temperature preset 4),
    # warmth moves through color calibration's CAT illuminant temperature
    # instead (`darkroom.wb_route`). darktable's hard range is 1667-25000 K;
    # the clamp and the notch match the white-balance row. Only shown for a
    # daylight or planckian illuminant (2 / 6), which the route checks.
    # UNVERIFIED until tests/test_host_colorcal.py step 3 passes: the sign
    # (a higher illuminant K renders warmer, like the WB slider) is assumed
    # from darktable's CAT semantics, not yet measured. The 2000 K floor is
    # below the CIE daylight formula's 4000 K validity; darktable
    # extrapolates there (unmeasured).
    ParamSpec("channelmixerrgb", "temperature",
              "color calibration › CAT › illuminant temperature", "K", 1.0,
              2000.0, 12000.0, 300.0, "xmp", None, False, "white_balance",
              "tells color calibration the light was bluer or redder, so the picture turns "
              "warmer or cooler, without touching the white balance module",
              "neutral greys and skin",
              ("iop/channelmixerrgb/temperature",),
              note="used for warmer/cooler only while the white balance is in camera "
                   "reference (late) mode; needs a daylight or planckian illuminant; "
                   "direction (higher K = warmer) unverified until the host probe"),
    _cb("contrast", "master › contrast", "%", -0.5, 0.5, 0.05, "color_balance",
        "adds or removes contrast around middle grey, after the tone mapper",
        "that it does not fight the sigmoid contrast"),
    #: Touch-floor ruling (see `_ZONE_CHROMA_STEP` above): raised from 0.05
    #: each so `a touch` clears the M5 C* floor (0.5) on the night-1
    #: evidence. `global_saturation`/`global_chroma` reach it (0.25 is the
    #: invariant ceiling for their +-0.5 clamp: `step <= (hi-lo)/4`).
    #: `global_vibrance` needs a touch of ~0.58 (olympus) / ~0.45 (sony_a7);
    #: the same ceiling caps its touch at 0.125 -- the residual noted above.
    _cb("global_saturation", "master › global saturation", "%", -0.5, 0.5, 0.25,
        "chroma_saturation",
        "changes colourfulness and darkens strong colours slightly, which reads natural",
        "skin and skies for over-cooked colour"),
    _cb("global_vibrance", "master › global vibrance", "%", -0.5, 0.5, 0.25,
        "chroma_saturation", "boosts the least colourful pixels most",
        "already-saturated areas, which should barely move",
        ("iop/colorbalancergb/global vibrance",)),
    _cb("global_chroma", "master › global chroma", "%", -0.5, 0.5, 0.20,
        "chroma_saturation", "changes colourfulness at constant brightness",
        "bright saturated colours going neon", ("iop/colorbalancergb/global chroma",)),
    *_zone("shadows", "shadows lift", "shadows", "lift"),
    *_zone("midtones", "power", "midtones"),
    *_zone("highlights", "highlights gain", "highlights", "gain"),
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
    # Row 437 family 1 (finishing). Internal units are the module's own params
    # (grain strength is stored 0-100; vignette brightness -1..1). Candidate
    # until the Task 32 host probe pins the path and the XMP slot.
    ParamSpec("grain", "strength", "grain › strength", "%", 1.0, 0.0, 60.0, 5.0,
              "xmp", 0.0, False, "finishing",
              "adds film-like grain; more strength is more visible grain",
              "smooth skies and skin, where grain shows first",
              ("iop/grain/strength",)),
    ParamSpec("vignette", "brightness", "vignette › brightness", "ratio", 1.0, -0.8, 0.4, 0.05,
              "xmp", 0.0, False, "finishing",
              "darkens (negative) or lightens (positive) the corners around the centre",
              "the frame edges: a strong vignette reads as a tunnel",
              ("iop/vignette/brightness",)),
    # Row 437 family 2 (geometry). The lens toggle is a module on/off, live
    # whenever `lens` is a whitelisted module (the benchmark enabled it on the
    # host, L1/L2); rotation and crop margins wait for the Task 33 probe.
    ParamSpec("lens", "__enabled", "lens correction › on/off", "bool", 1.0, 0.0, 1.0, 1.0,
              "xmp", 0.0, False, "geometry",
              "applies the lens profile: distortion, vignetting and chromatic aberration",
              "straight lines near the edges"),
    ParamSpec("ashift", "rotation", "rotate and perspective › rotation", "°", 1.0, -10.0, 10.0,
              0.5, "xmp", 0.0, False, "geometry",
              "rotates the whole frame to level a tilted horizon; the corners are cropped",
              "the corners, which rotation crops away",
              ("iop/ashift/rotation", "iop/ashift/rotate")),
    *(ParamSpec("crop", side, f"crop › {side} margin", "ratio", 1.0, 0.0, 1.0, 0.01, "xmp",
                0.0 if side in ("left", "top") else 1.0, False, "geometry",
                f"moves the {side} edge of the crop", "what the new edge cuts off",
                (f"iop/crop/{side}",)) for side in ("left", "right", "top", "bottom")),
    # Row 437 family 3 (detail). Candidates until the Task 34 host probe.
    ParamSpec("denoiseprofile", "strength", "denoise (profiled) › strength", "ratio", 1.0,
              0.0, 4.0, 0.1, "xmp", 1.0, False, "detail",
              "how hard the profiled denoiser smooths noise for this camera and ISO",
              "fine texture and skin: too much looks waxy",
              ("iop/denoiseprofile/strength",)),
    ParamSpec("sharpen", "amount", "sharpen › amount", "ratio", 1.0, 0.0, 2.0, 0.1,
              "xmp", 0.5, False, "detail",
              "adds edge contrast so detail looks crisper",
              "halos along high-contrast edges and amplified noise",
              ("iop/sharpen/amount",)),
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
    if s.param == "__enabled":  # a module toggle: pinned by the module whitelist
        return "live" if s.module in dtbridge.MODULE_WHITELIST else "candidate"
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


#: darktable's OWN hard limits (its params `$MIN`/`$MAX`), where Scouter has
#: host evidence of them -- not Scouter's safe range (`lo`/`hi`), which is
#: narrower and enforced before anything is sent. A `set` beyond one of these
#: is stored AT the bound by darktable, silently: night-1 gauntlet (row 433,
#: darktable 5.4.1) `cov-sigmoid.skew-*-set_hi_plus`/`set_lo_minus` (1.1 ->
#: 1.0, -1.1 -> -1.0) and `cov-colorbalancergb.*_chroma-*-set_lo_minus`
#: (-0.02 -> 0, no history item written). `None` = that side not proven.
#: Only a bound declared here may explain a read-back mismatch as a clamp;
#: anything else is a `write_unverified` error, never a guessed clamp.
DT_HARD_BOUNDS: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ("sigmoid", "skew"): (-1.0, 1.0),
    ("colorbalancergb", "shadows_chroma"): (0.0, None),
    ("colorbalancergb", "midtones_chroma"): (0.0, None),
    ("colorbalancergb", "highlights_chroma"): (0.0, None),
}


def dt_effective(module: str, param: str, value: float) -> tuple[float, float | None]:
    """What darktable will actually store for a `set` of `value`:
    `(stored, bound)`. `bound` is the declared hard limit that clamped it, or
    `None`. A hue row (`wraps`) is stored modulo 360."""
    s = spec(module, param)
    if s is not None and s.wraps:
        return value % 360.0, None
    lo, hi = DT_HARD_BOUNDS.get((module, param), (None, None))
    if lo is not None and value < lo:
        return lo, lo
    if hi is not None and value > hi:
        return hi, hi
    return value, None


#: Fallback read-back tolerance for a pair with no vocabulary row (every
#: pinned pair has one today): the historical `darkroom._close` tolerance.
_FALLBACK_TOLERANCE = 1e-4
#: float32 relative epsilon (2**-23), times a small safety factor: the XMP
#: stores float32, so a read-back can never be closer than this.
_F32_EPS = 2.0 ** -23


def _distance(s: ParamSpec | None, a: float, b: float) -> float:
    if s is not None and s.wraps:
        d = abs(a - b) % 360.0
        return min(d, 360.0 - d)
    return abs(a - b)


def verify_tolerance(module: str, param: str, value: float | None = None,
                     previous: float | None = None) -> float:
    """How close a read-back must be to the value asked for (row 433).

    A tenth of the row's step: darktable's sliders round to their displayed
    digits (night 1: exposure black -0.002744 is stored -0.0027, 4.4e-5 off;
    colour zones strength 2.1 -> 2.100015), far below a tenth of any step.
    Never looser than half the requested change (`value` vs `previous`), so
    "did not move at all" can never pass as "landed": a move smaller than
    the slider's own rounding is then honestly unverifiable. Never tighter
    than float32's resolution at `value` (the XMP stores float32)."""
    s = spec(module, param)
    floor = max(1e-9, 4 * _F32_EPS * abs(value or 0.0))
    tol = _FALLBACK_TOLERANCE if s is None or s.step <= 0 else s.step / 10.0
    if value is not None and previous is not None:
        tol = min(tol, _distance(s, value, previous) / 2.0)
    return max(tol, floor)


def values_match(module: str, param: str, a: float | None, b: float | None,
                 previous: float | None = None) -> bool:
    """Read-back `a` agrees with the value asked for, `b`, within
    `verify_tolerance` (circularly, for a hue row). `None` never matches."""
    if a is None or b is None:
        return False
    s = spec(module, param)
    return _distance(s, a, b) <= verify_tolerance(module, param, b, previous)


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
