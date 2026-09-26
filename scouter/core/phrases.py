"""Phrase -> moves (phase 3 design §5). Pure: no darktable, no model.

Grammar, in order: normalise; take one explicit `by N[unit]`; take one
intensity word; drop filler; exact synonym lookup. An unknown phrase is a
`usage` refusal carrying the nearest known phrases.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import asdict, dataclass

from scouter import metrics
from scouter.core import dtbridge, vocab
from scouter.errors import UsageError
from scouter.paths import current_job
from scouter.registry import ArgSpec, Verb, register
from scouter.types import ResolveResult

TONE = "@tone"
TONE_ROUTES = ["sigmoid", "filmicrgb"]


@dataclass(frozen=True)
class Move:
    module: str
    param: str
    kind: str  # "adjust" | "set" | "toggle" | "crop"
    notches: float | None = None
    value: float | None = None


@dataclass(frozen=True)
class Phrase:
    key: str
    synonyms: tuple[str, ...]
    moves: tuple[Move, ...]


@dataclass(frozen=True)
class Resolution:
    phrase: str
    normalised: str
    key: str
    intensity: float
    explicit: bool
    moves: tuple[Move, ...]


def _adj(module: str, param: str, notches: float) -> Move:
    return Move(module, param, "adjust", notches=notches)


def _set(module: str, param: str, value: float) -> Move:
    return Move(module, param, "set", value=value)


def _toggle(module: str, on: bool) -> Move:
    return Move(module, "__enabled", "toggle", value=1.0 if on else 0.0)


def _crop(aspect: float) -> Move:
    return Move("crop", "aspect", "crop", value=aspect)


CB = "colorbalancergb"
PHRASES: tuple[Phrase, ...] = (
    Phrase("brighter", ("brighter", "brighten", "lighter", "more exposure", "brighten up"),
           (_adj("exposure", "exposure", 1),)),
    Phrase("darker", ("darker", "darken", "less exposure", "lower exposure"),
           (_adj("exposure", "exposure", -1),)),
    Phrase("more_contrast", ("more contrast", "punchier", "add contrast", "increase contrast",
                             "more punch"), (_adj(TONE, "contrast", 1),)),
    Phrase("less_contrast", ("less contrast", "flatter", "reduce contrast", "lower contrast",
                             "softer contrast"), (_adj(TONE, "contrast", -1),)),
    Phrase("warmer", ("warmer", "warm up", "more warmth", "warm"),
           (_adj("temperature", "temperature", 1),)),
    Phrase("cooler", ("cooler", "colder", "cool down", "less warm", "cool"),
           (_adj("temperature", "temperature", -1),)),
    Phrase("more_magenta", ("more magenta", "less green"), (_adj("temperature", "tint", -1),)),
    Phrase("more_green", ("more green", "less magenta"), (_adj("temperature", "tint", 1),)),
    Phrase("lift_shadows", ("lift shadows", "lift the shadows", "open shadows", "open the shadows",
                            "open up the shadows", "brighter shadows"),
           (_adj(CB, "shadows_luminance", 1),)),
    Phrase("deepen_shadows", ("deeper shadows", "darker shadows", "crush the shadows",
                              "crush shadows"), (_adj(CB, "shadows_luminance", -1),)),
    Phrase("brighter_midtones", ("brighter midtones", "lift the midtones"),
           (_adj(CB, "midtones_luminance", 1),)),
    Phrase("darker_midtones", ("darker midtones", "lower the midtones"),
           (_adj(CB, "midtones_luminance", -1),)),
    Phrase("brighter_highlights", ("brighter highlights", "lift the highlights"),
           (_adj(CB, "highlights_luminance", 1),)),
    Phrase("tame_highlights", ("tame the highlights", "darker highlights",
                               "pull down the highlights", "recover highlights"),
           (_adj(CB, "highlights_luminance", -1),)),
    Phrase("deeper_blacks", ("deeper blacks", "richer blacks"), (_adj("exposure", "black", 1),)),
    Phrase("faded_blacks", ("faded blacks", "lift the blacks", "matte blacks"),
           (_adj("exposure", "black", -1),)),
    Phrase("more_saturation", ("more saturation", "more saturated", "more colour",
                               "more colourful", "saturate"),
           (_adj(CB, "global_saturation", 1),)),
    Phrase("less_saturation", ("less saturation", "less saturated", "desaturate", "muted",
                               "less colour"), (_adj(CB, "global_saturation", -1),)),
    Phrase("more_vibrance", ("more vibrance", "more vibrant"), (_adj(CB, "global_vibrance", 1),)),
    Phrase("less_vibrance", ("less vibrance", "less vibrant"), (_adj(CB, "global_vibrance", -1),)),
    Phrase("more_chroma", ("more chroma",), (_adj(CB, "global_chroma", 1),)),
    Phrase("less_chroma", ("less chroma",), (_adj(CB, "global_chroma", -1),)),
    Phrase("warm_highlights", ("warm the highlights", "warmer highlights"),
           (_set(CB, "highlights_hue", 60.0), _adj(CB, "highlights_chroma", 1))),
    Phrase("cool_shadows", ("cool the shadows", "cooler shadows", "blue shadows"),
           (_set(CB, "shadows_hue", 220.0), _adj(CB, "shadows_chroma", 1))),
    Phrase("teal_orange", ("teal and orange", "orange and teal", "cinematic grade"),
           (_set(CB, "shadows_hue", 200.0), _adj(CB, "shadows_chroma", 1),
            _set(CB, "highlights_hue", 55.0), _adj(CB, "highlights_chroma", 1))),
    Phrase("stronger_grade", ("stronger colour grade", "stronger grade", "more grade",
                              "more colour grade"),
           (_adj(CB, "shadows_chroma", 1), _adj(CB, "midtones_chroma", 1),
            _adj(CB, "highlights_chroma", 1))),
    Phrase("weaker_grade", ("weaker colour grade", "weaker grade", "less grade",
                            "less colour grade"),
           (_adj(CB, "shadows_chroma", -1), _adj(CB, "midtones_chroma", -1),
            _adj(CB, "highlights_chroma", -1))),
    Phrase("lens_on", ("correct the lens", "lens correction", "turn on lens correction",
                       "fix the distortion", "fix lens distortion", "remove chromatic aberration"),
           (_toggle("lens", True),)),
    Phrase("lens_off", ("turn off lens correction", "no lens correction"),
           (_toggle("lens", False),)),
    Phrase("rotate_right", ("rotate right", "rotate clockwise", "tilt right"),
           (_adj("ashift", "rotation", 1),)),
    Phrase("rotate_left", ("rotate left", "rotate counterclockwise", "rotate anticlockwise",
                           "tilt left"), (_adj("ashift", "rotation", -1),)),
    Phrase("crop_square", ("crop square", "square crop", "crop to 1:1", "make it square"),
           (_crop(1.0),)),
    Phrase("crop_4x5", ("crop to 4:5", "crop 4:5", "portrait crop for instagram"), (_crop(0.8),)),
    Phrase("crop_16x9", ("crop to 16:9", "crop 16:9", "widescreen crop"), (_crop(16 / 9),)),
    Phrase("crop_3x2", ("crop to 3:2", "crop 3:2"), (_crop(1.5),)),
)

_INDEX: dict[str, Phrase] = {syn: p for p in PHRASES for syn in p.synonyms}

#: Longest first, so "a little bit" wins over "a little" and "a bit". Kept as
#: the brief specifies (controller ruling): the spec's literal "a lot more"
#: would swallow "more" and break "a lot more saturation" (a golden input).
INTENSITY: tuple[tuple[str, float], ...] = (
    ("a little bit", 0.5), ("a tiny bit", 0.5), ("a touch", 0.5), ("a little", 0.5),
    ("a bit", 0.5), ("slightly", 0.5),
    ("a lot", 2.0), ("strongly", 2.0), ("much", 2.0), ("lots", 2.0), ("way", 2.0),
)
#: "more of" per spec §5.1, on top of the brief's filler list.
FILLER = ("make the image", "make the photo", "make it", "can you", "the image", "the photo",
          "the picture", "please", "more of")
_DROP_TOKENS = {"it", "a", "bit"}
_BY_RE = re.compile(r"\bby\s+([+-]?\d+(?:\.\d+)?)\s*(ev|k|%|°|deg|degrees)?(?=\s|$)")
_UNIT_ALIASES = {"ev": "EV", "k": "K", "%": "%", "°": "°", "deg": "°", "degrees": "°"}


#: Fix round 1, M1: the subset of FILLER that is safe to strip from the ENDS
#: only, before a verbatim lookup. Deliberately excludes "make it"/"make
#: the image" etc: those are themselves inside a registered synonym ("make
#: it square"), so stripping them here would eat the synonym, not wrap it.
_OUTER_FILLER = ("please", "can you")


def _strip_outer_filler(text: str) -> str:
    """Strip `_OUTER_FILLER` phrases only from the ends, repeatedly, leaving
    anything in the middle alone. A verbatim synonym that itself contains a
    filler word ("make it square") must still match when it is wrapped in
    an outer "please"/"can you" -- those sit at the edges, so they can be
    peeled off without touching the synonym's own words."""
    s = text.strip()
    changed = True
    while changed:
        changed = False
        for filler in _OUTER_FILLER:
            start = re.sub(rf"^{re.escape(filler)}\b\s*", "", s)
            if start != s:
                s = start.strip()
                changed = True
                continue
            end = re.sub(rf"\s*\b{re.escape(filler)}$", "", s)
            if end != s:
                s = end.strip()
                changed = True
    return s


def normalise(text: str) -> str:
    text = text.lower().replace("color", "colour")
    text = re.sub(r"[^a-z0-9.+%°: -]", " ", text)
    # Fix round 1, I2: a colon survives only between two digits ("4:5"), the
    # one place a crop-preset aspect needs it -- not trailing punctuation
    # ("warmer:") or a mid-sentence pause ("warmer: please"), which main
    # already resolved by dropping it.
    text = re.sub(r"(?<!\d):|:(?!\d)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tone_spec() -> vocab.ParamSpec:
    return vocab.spec("sigmoid", "contrast")  # type: ignore[return-value]


def row_for(move: Move) -> vocab.ParamSpec:
    """The vocabulary row that best represents `move`, for display and shape
    checks (never for gating: a crop move's gate checks all four crop rows,
    not just this one -- see `_refusal`). Fix round 1, M6: the single place
    that knows a `@tone` move stands for `sigmoid.contrast` and a `crop`
    move stands for `crop.left`, so gauntlet's coverage/offline layers and
    the phrase-row test don't each carry their own copy of this mapping."""
    if move.module == TONE:
        return _tone_spec()
    if move.module == "crop":
        return vocab.spec("crop", "left")  # type: ignore[return-value]
    return vocab.spec(move.module, move.param)  # type: ignore[return-value]


def resolve(text: str, amount: float | None = None) -> Resolution:
    if not isinstance(text, str) or not text.strip():
        raise UsageError("a phrase is required, e.g. 'warmer' or 'less contrast'")
    norm = normalise(text)
    core = norm
    explicit: tuple[float, str | None] | None = None
    m = _BY_RE.search(core)
    if m:
        raw_number = m.group(1)
        if raw_number.startswith("-"):
            raise UsageError(
                "give a positive amount: the direction words carry the sign "
                "(say 'cooler by 300k', not 'warmer by -300k')",
                detail={"phrase": text},
            )
        if float(raw_number) == 0:
            # Final review #5: "brighter by 0" is a zero-delta move -- a
            # write (and an undo entry) that changes nothing.
            raise UsageError(
                "give a non-zero amount: 'by 0' would change nothing",
                detail={"phrase": text},
            )
        explicit = (float(raw_number), m.group(2))
        core = (core[: m.start()] + " " + core[m.end():]).strip()
    factor = 1.0
    for word, f in INTENSITY:
        pattern = re.compile(rf"\b{re.escape(word)}\b")
        if pattern.search(core):
            core = pattern.sub(" ", core, count=1)
            factor = f
            break
    # A synonym registered verbatim wins outright (row 437: "make it square"
    # would otherwise lose its own "make it" to the filler strip below).
    phrase = _INDEX.get(core.strip())
    if phrase is None:
        # Fix round 1, M1: peel only OUTER filler words ("please make it
        # square", "make it square please", "can you make it square") and
        # retry the verbatim lookup before the full, position-agnostic strip
        # below -- which would also eat "make it" out of the synonym itself.
        outer = _strip_outer_filler(core)
        if outer != core.strip():
            phrase = _INDEX.get(outer)
    if phrase is None:
        for filler in FILLER:
            core = re.sub(rf"\b{re.escape(filler)}\b", " ", core)
        core = " ".join(t for t in core.split() if t not in _DROP_TOKENS)
        phrase = _INDEX.get(core)
    if phrase is None:
        suggestions = difflib.get_close_matches(core, list(_INDEX), n=3, cutoff=0.6)
        raise UsageError(
            f"no phrase matches {text!r}"
            + (f"; did you mean {', '.join(repr(s) for s in suggestions)}?" if suggestions else ""),
            fix="call `scouter vocab list` (MCP vocab_list) for every phrase and word",
            detail={"phrase": text, "normalised": core, "suggestions": suggestions},
        )
    if amount is not None:
        if explicit is not None:
            raise UsageError("pass either `amount` or 'by N' in the phrase, not both",
                             detail={"phrase": text, "amount": amount})
        if not (isinstance(amount, (int, float)) and math.isfinite(amount) and 0 < amount <= 5):
            raise UsageError("amount must be a number in (0, 5]", detail={"amount": amount})
        factor = float(amount)
    if explicit is not None:
        non_adjust_kinds = {mv.kind for mv in phrase.moves} - {"adjust"}
        if non_adjust_kinds:
            # Fix round 1, M2: a toggle/crop phrase has no numeric amount to
            # be "by N" of -- say so directly, instead of the one-move-phrase
            # count message ("'lens_on' makes 1 moves"), which is misleading
            # here since the real reason is the kind, not the move count.
            kind = "toggle" if "toggle" in non_adjust_kinds else "crop"
            raise UsageError(
                f"'by N' does not apply to {phrase.key!r}: a {kind} takes no amount",
                detail={"phrase": text, "key": phrase.key, "kind": kind},
            )
        adjusts = [mv for mv in phrase.moves if mv.kind == "adjust"]
        if len(phrase.moves) != 1 or len(adjusts) != 1:
            raise UsageError(
                f"'by N' works only for a one-move phrase; {phrase.key!r} makes "
                f"{len(phrase.moves)} moves — use `amount` as a multiplier instead",
                detail={"phrase": text, "key": phrase.key},
            )
        mv = adjusts[0]
        row = row_for(mv)
        number, unit = explicit
        if unit is not None and _UNIT_ALIASES[unit] != row.unit:
            raise UsageError(
                f"{row.ui} is measured in {row.unit}, not {_UNIT_ALIASES[unit]}",
                detail={"phrase": text, "unit": unit, "expected": row.unit},
            )
        sign = 1.0 if (mv.notches or 0) > 0 else -1.0
        moves = (Move(mv.module, mv.param, "adjust", None, sign * number / row.display_scale),)
        return Resolution(text, norm, phrase.key, 1.0, True, moves)
    moves = tuple(
        Move(mv.module, mv.param, "adjust", (mv.notches or 0.0) * factor, None)
        if mv.kind == "adjust" else mv
        for mv in phrase.moves
    )
    # Fix round 1, M3: a toggle/crop phrase has no adjust move for an
    # intensity word to scale -- "crop square a touch" must not report
    # intensity 0.5 for a move that never changes with it.
    if not any(mv.kind == "adjust" for mv in phrase.moves):
        factor = 1.0
    return Resolution(text, norm, phrase.key, factor, False, moves)


def resolution_dict(res: Resolution) -> dict:
    return {"key": res.key, "intensity": res.intensity, "explicit": res.explicit,
            "moves": [asdict(mv) for mv in res.moves]}


def move_view(mv: Move) -> dict:
    row = row_for(mv)
    view = asdict(mv)
    if mv.kind == "adjust":
        view["delta"] = mv.value if mv.notches is None else mv.notches * row.step
    view["unit"] = row.unit
    view["status"] = vocab.status(row)
    view["pinned_path"] = dtbridge.ACTION_PATHS.get((row.module, row.param))
    if mv.module == TONE:
        view["routes"] = list(TONE_ROUTES)
    return view


@metrics.instrumented("vocab.resolve")
def resolve_verb(phrase: str, *, amount: float | None = None,
                 job: str | None = None) -> ResolveResult:
    """What a phrase would do, with each move's status. No darktable calls."""
    job = job or current_job()
    res = resolve(phrase, amount)
    return ResolveResult(phrase=res.phrase, normalised=res.normalised, key=res.key,
                         intensity=res.intensity, explicit=res.explicit,
                         moves=[move_view(mv) for mv in res.moves])


register(
    Verb(
        id="vocab.resolve",
        group="vocab",
        fn=resolve_verb,
        args=(
            ArgSpec("phrase", "str", required=True, positional=True,
                    help="plain words, e.g. 'a touch warmer'"),
            ArgSpec("amount", "float", default=None,
                    help="multiplier on the phrase's step (0-5]; replaces 'slightly'/'much'"),
        ),
        writes=False,
        gated=False,
        summary="Show what a phrase would change, without touching darktable.",
    )
)
