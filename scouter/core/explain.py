"""Educative mode (row 346; phase 3 design §7): a reply-phrasing layer ONLY.

It reads ParamSpec text, a move's numbers and the session's explained set,
and returns strings. It sends nothing anywhere and changes no value. The
session is the job (`--job` / $SCOUTER_JOB / the MCP server's pinned job);
the explained set lives in the job state file under "explained_concepts".
"""

from __future__ import annotations

from dataclasses import dataclass

from scouter import paths
from scouter.core import vocab
from scouter.errors import ScouterError, UsageError

LEVELS = ("off", "brief", "full")

CONCEPTS: dict[str, tuple[str, str]] = {
    "exposure": ("Scene-referred exposure",
                 "Exposure scales all the light in the scene by the same factor, measured in EV "
                 "(one EV doubles or halves it). In darktable's scene-referred workflow you set it "
                 "first, so the subject sits at a sensible brightness, and let the tone mapper "
                 "handle the extremes."),
    "tone_mapper": ("Tone mapping",
                    "Sigmoid (or filmic rgb) squeezes the scene's wide range of light into what a "
                    "screen can show. Its contrast slider sets how steep that curve is in the "
                    "middle: more contrast separates midtones but pushes shadows and highlights "
                    "toward the ends."),
    "white_balance": ("White balance",
                      "White balance tells darktable what colour the light was. Raising the "
                      "temperature slider says the light was bluer, so darktable compensates and "
                      "the picture turns warmer; tint trades green against magenta. Get it right "
                      "before any creative colour."),
    "color_balance": ("Color balance rgb",
                      "Color balance rgb is darktable's colour-grading module. Master sliders act "
                      "on the whole image; the 4-ways tab grades shadows (lift), midtones (power) "
                      "and highlights (gain) separately, each with luminance, hue and chroma."),
    "chroma_saturation": ("Chroma, saturation, vibrance",
                          "Chroma is colourfulness independent of brightness; saturation also "
                          "darkens as colour increases, which reads more natural on skin; "
                          "vibrance boosts the least colourful pixels most. Small numbers go a "
                          "long way: 5 % is a visible move."),
    "grading": ("Colour grading",
                "A grade gives a look by tinting shadows and highlights differently, classically "
                "cool shadows and warm highlights. Hue picks the tint, chroma picks how much; "
                "keep chroma low and judge on a neutral grey or skin."),
    "black_level": ("Black level",
                    "Black level correction moves the point that renders as pure black. Deeper "
                    "blacks add density; lifting them gives a faded, matte look. It is a strong "
                    "control: a few thousandths are visible."),
    "color_zones": ("Color zones",
                    "Color zones adjusts hue, chroma and lightness by the colour a pixel already "
                    "has (for example, only the blues). Scouter can reach only its global mix; "
                    "the zone curves need darktable's own editor."),
    "presets": ("Styles as presets",
                "A darktable style is a saved list of module settings. Applying one appends those "
                "settings to an image's history, so it acts like a filter you can stack; it never "
                "replaces what is there, and each application is visible in the history panel."),
    "detail": ("Denoise and sharpen",
               "Profiled denoise smooths sensor noise using a per-camera, per-ISO profile; "
               "sharpen adds edge contrast so detail reads crisper on screen. They pull in "
               "opposite directions, so push one and watch the other: too much denoise looks "
               "waxy, too much sharpening halos high-contrast edges and amplifies noise."),
}


@dataclass(frozen=True)
class MoveNote:
    spec: vocab.ParamSpec
    previous: float | None
    target: float | None
    kind: str = "adjust"  # "adjust" | "set"


def resolve_level(override: str | None) -> str:
    if override is not None:
        if override not in LEVELS:
            raise UsageError(f"educative must be one of {', '.join(LEVELS)}, got {override!r}",
                             detail={"educative": override})
        return override
    try:
        from scouter import config

        return config.load().educative
    except ScouterError:
        return "brief"  # a broken config must not break editing (P3-R8)


def with_unit(s: vocab.ParamSpec, number_text: str) -> str:
    if s.unit in ("ratio", "—"):
        return number_text
    if s.unit in ("%", "°"):
        return f"{number_text}{s.unit}"
    return f"{number_text} {s.unit}"


def _value(s: vocab.ParamSpec, v: float | None) -> str:
    return "unknown" if v is None else f"{vocab.to_display(s, v):.4g}"


def _delta_display(s: vocab.ParamSpec, previous: float, target: float) -> float:
    """The move's size in display units. Wrapping (hue) rows use the signed
    shortest angle so a wrapped move (e.g. 350 -> 10) reads "+20°", not the
    naive "-340°" a plain subtraction would give (controller ruling)."""
    if s.wraps:
        raw = ((target - previous + 180.0) % 360.0) - 180.0
    else:
        raw = target - previous
    return vocab.to_display(s, raw)


def brief_line(note: MoveNote) -> str:
    s = note.spec
    prev, target = _value(s, note.previous), _value(s, note.target)
    if note.kind == "set" or note.previous is None or note.target is None:
        head = f"{s.ui} set to {with_unit(s, target)} (was {with_unit(s, prev)})"
    else:
        delta = _delta_display(s, note.previous, note.target)
        direction = "up" if delta >= 0 else "down"
        head = (f"{s.ui} {direction} {with_unit(s, f'{delta:+.3g}')} "
                f"({with_unit(s, prev)} → {with_unit(s, target)})")
    return f"{head}: {s.why}; watch {s.watch}."


def concept_line(concept_id: str) -> str:
    title, text = CONCEPTS[concept_id]
    return f"[{title}] {text}"


def _new_concepts(concepts: list[str], job: str) -> list[str]:
    seen = set(paths.job_state_read(job).get("explained_concepts", []))
    out: list[str] = []
    for c in concepts:
        if c not in seen and c not in out:
            out.append(c)
    return out


def _mark(job: str, concepts: list[str]) -> None:
    if not concepts:
        return

    def _mutate(state: dict) -> None:
        state["explained_concepts"] = sorted(set(state.get("explained_concepts", [])) | set(concepts))

    paths.job_state_update(job, _mutate)


def lines(notes: list[MoveNote], *, level: str, job: str) -> list[str]:
    if level not in LEVELS:
        raise UsageError(f"educative must be one of {', '.join(LEVELS)}", detail={"educative": level})
    if level == "off" or not notes:
        return []
    if level == "brief":
        return [brief_line(n) for n in notes]
    new = _new_concepts([n.spec.concept for n in notes], job)
    out: list[str] = []
    emitted: set[str] = set()
    for n in notes:
        c = n.spec.concept
        if c in new and c not in emitted:
            out.append(concept_line(c))
            emitted.add(c)
        out.append(brief_line(n))
    _mark(job, new)
    return out


def preset_lines(action: str, name: str, modules: list[str], items: int | None, *,
                 level: str, job: str) -> list[str]:
    if level == "off":
        return []
    if action == "save":
        line = (f"saved {len(modules)} module(s) ({', '.join(modules)}) as style '{name}': "
                "apply it from darktable's styles panel or with preset apply.")
    else:
        count = "some" if items is None else items
        line = (f"applied style '{name}' ({count} history item(s) appended): compare with the "
                "history panel; darktable's own history is the way back.")
    out: list[str] = []
    if level == "full":
        new = _new_concepts(["presets"], job)
        if new:
            out.append(concept_line("presets"))
            _mark(job, new)
    out.append(line)
    return out
